"""XAML parser (I-07): walk a UiPath project (project.json, *.xaml, invoked workflows) and emit
the process-map skeleton: apps, screens (by window title), fields by selector, io, SQL, the
invocation tree and the bot-carried data flow between fields.

UiPath selectors are strings of tags inside an attribute, not XML; they are parsed with the tiny
tag-attribute parser in robotlog.parse_selector. Activities of interest and what they carry:
TypeInto (Text), Click, GetText/GetAttribute (output variable), SelectItem (Item), ExecuteQuery /
ExecuteNonQuery (Sql), InvokeWorkflowFile (WorkflowFileName + arguments), Assign, NavigateTo /
OpenBrowser (Url). Everything else is recorded by type only.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from echo.analyze.models import AppDef, Field_, Flow, ProcessMap, ScreenDef, SqlUse
from echo.analyze.robotlog import parse_selector
from echo.util import slug

log = logging.getLogger(__name__)
VAR_RE = re.compile(r"^\[\s*([A-Za-z_]\w*)\s*\]$")
IO = {
    "TypeInto": "typed",
    "SetText": "typed",
    "Click": "click",
    "GetText": "read",
    "GetAttribute": "read",
    "SelectItem": "select",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@dataclass
class XActivity:
    workflow: str
    type: str
    display: str | None
    selector: dict[str, str] | None = None
    selector_raw: str | None = None
    value_var: str | None = None
    url: str | None = None
    sql: str | None = None
    invoke: str | None = None
    args: dict[str, str] = field(default_factory=dict)
    private: bool = False
    app: str | None = None


@dataclass
class XProject:
    name: str
    main: str
    activities: list[XActivity] = field(default_factory=list)
    invocations: list[dict] = field(default_factory=list)
    other_types: dict[str, int] = field(default_factory=dict)


def _var(expr: str | None) -> str | None:
    if not expr:
        return None
    m = VAR_RE.match(expr.strip())
    return m.group(1) if m else None


def _target_selector(el: ET.Element) -> str | None:
    if el.get("Selector"):
        return el.get("Selector")
    for child in el.iter():
        if _local(child.tag) == "Target" and child.get("Selector"):
            return child.get("Selector")
    return None


def _walk_file(
    root: Path, rel: str, proj: XProject, scope: dict[str, str], stack: list[str]
) -> None:
    path = root / rel
    if rel in stack:
        log.warning("recursive invoke of %s ignored", rel)
        return
    if not path.exists():
        log.warning("invoked workflow %s not found", rel)
        return
    tree = ET.parse(path)
    stack = stack + [rel]
    for el in tree.getroot().iter():
        t = _local(el.tag)
        if (
            t in ("Activity", "Sequence", "Members", "Property", "Variable", "Target")
            or "." in t
            or t in ("InArgument", "OutArgument", "If", "Flowchart")
        ):
            continue
        a = XActivity(
            workflow=rel,
            type=t,
            display=el.get("DisplayName"),
            private=(el.get("Private") or "").lower() == "true",
        )
        if t in IO:
            a.selector_raw = _target_selector(el)
            a.selector = parse_selector(a.selector_raw)
            attr = {
                "TypeInto": "Text",
                "SetText": "Text",
                "GetText": "Value",
                "GetAttribute": "Result",
                "SelectItem": "Item",
            }.get(t)
            v = _var(el.get(attr)) if attr else None
            a.value_var = scope.get(v, v) if v else None
        elif t in ("NavigateTo", "OpenBrowser"):
            a.url = el.get("Url")
        elif t in ("ExecuteQuery", "ExecuteNonQuery"):
            a.sql = el.get("Sql")
        elif t == "InvokeWorkflowFile":
            a.invoke = el.get("WorkflowFileName")
            for arg in el.iter():
                key = arg.get("{http://schemas.microsoft.com/winfx/2006/xaml}Key")
                if key and _local(arg.tag) in ("InArgument", "OutArgument"):
                    v = _var(arg.text or "")
                    if v:
                        a.args[key] = scope.get(v, v)
        elif t == "Assign":
            pass
        elif el.get("DisplayName") is None:
            continue
        else:
            proj.other_types[t] = proj.other_types.get(t, 0) + 1
            if t != "WaitUiElementAppear":
                continue
            a.selector_raw = _target_selector(el)
            a.selector = parse_selector(a.selector_raw)
        proj.activities.append(a)
        if a.invoke:
            proj.invocations.append({"from": rel, "to": a.invoke, "args": a.args})
            _walk_file(root, a.invoke, proj, dict(a.args), stack)


def parse_project(root: Path) -> XProject:
    root = Path(root)
    meta = json.loads((root / "project.json").read_text(encoding="utf-8"))
    proj = XProject(name=meta.get("name", root.name), main=meta.get("main", "Main.xaml"))
    _walk_file(root, proj.main, proj, {}, [])
    _assign_apps(proj)
    return proj


def _assign_apps(proj: XProject) -> None:
    origins: list[str] = []
    cur: str | None = None
    for a in proj.activities:
        if a.url:
            o = "{0.scheme}://{0.netloc}".format(urlsplit(a.url))
            if o not in origins:
                origins.append(o)
            cur = f"app{origins.index(o) + 1}"
        if a.type in IO or a.url or a.type == "WaitUiElementAppear":
            a.app = cur
        elif a.sql:
            a.app = cur


def screen_name(title: str | None) -> str:
    """'CRM - New Loan' -> 'new_loan' (the part after the application prefix)."""
    if not title:
        return "unknown"
    part = title.split(" - ", 1)[1] if " - " in title else title
    return slug(part.rstrip("*"))


def field_name(sel: dict[str, str] | None) -> str:
    sel = sel or {}
    return sel.get("id") or sel.get("name") or sel.get("cls") or sel.get("aaname") or "unknown"


def css_of(sel: dict[str, str] | None) -> str:
    sel = sel or {}
    if sel.get("id"):
        return f"#{sel['id']}"
    tag = (sel.get("tag") or "").lower()
    if sel.get("name"):
        return f"{tag}[name='{sel['name']}']"
    if sel.get("cls"):
        return f"{tag}.{sel['cls']}"
    return tag or "*"


def to_process_map(proj: XProject, xaml_version: str | None = None) -> ProcessMap:
    apps: dict[str, AppDef] = {}
    reads: dict[str, str] = {}
    flows: list[Flow] = []
    for a in proj.activities:
        if a.app is None:
            continue
        app = apps.setdefault(a.app, AppDef(app=a.app))
        if a.url and "origin" not in app.match:
            o = urlsplit(a.url)
            app.match["origin"] = f"{o.scheme}://{o.netloc}"
        if a.type not in IO:
            continue
        title = (a.selector or {}).get("title")
        if title and " - " in title:
            app.match.setdefault("title", title.split(" - ", 1)[0] + "*")
        sname = screen_name(title)
        screen = next((s for s in app.screens if s.name == sname), None)
        if screen is None:
            screen = ScreenDef(name=sname, title=title)
            app.screens.append(screen)
        fname = field_name(a.selector)
        fid = f"{a.app}.{sname}.{fname}"
        if not any(f.name == fname for f in screen.fields):
            screen.fields.append(
                Field_(
                    name=fname,
                    selector=css_of(a.selector),
                    io=IO[a.type],
                    private=a.private,
                    display=a.display,
                    uipath_selector=a.selector_raw,
                )
            )
        if IO[a.type] == "read" and a.value_var:
            reads[a.value_var] = fid
        elif IO[a.type] in ("typed", "select") and a.value_var in reads:
            flows.append(
                Flow(
                    **{
                        "from": reads[a.value_var],
                        "to": fid,
                        "carried_by": "bot",
                        "variable": a.value_var,
                    }
                )
            )
    sql = [SqlUse(activity=a.type, text=a.sql, app=a.app) for a in proj.activities if a.sql]
    return ProcessMap(
        process=proj.name,
        xaml_version=xaml_version,
        apps=list(apps.values()),
        sql=sql,
        flow=flows,
        invocations=proj.invocations,
        other_activity_types=proj.other_types,
    )
