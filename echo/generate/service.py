"""Data service for one generated app (I-20 read wiring, I-21 write wiring, I-22 serving).

FastAPI over the Echo DB. One route per captured screen under the captured path structure (so
URL-based selectors and NetLog comparisons stay valid); a query per slot group through the
relation graph; search per search box with the semantics observed (default: contains,
case-insensitive, ordered by the column shown first). Blocks that the real app filled over XHR
are rendered on demand at /echo-block/... Form posts go to /echo-write/... and are mapped onto rows
with the provenance-derived write config. Interactions with no evidence are logged to
unobserved.jsonl by the behavior runtime. The real app's scripts are never served (ADR-008).
"""

from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from echo.data.closure import Relation
from echo.generate import models as GM
from echo.generate import template as T
from echo.generate.context import gather, rows_where
from echo.util import append_jsonl, iso, read_json

log = logging.getLogger(__name__)
NUMERIC = re.compile(r"INT|NUMERIC|DECIMAL|NUMBER|FLOAT|REAL|DOUBLE", re.I)


class Replica:
    def __init__(
        self, dist: Path, echo: Engine, log_dir: Path | None = None, baseline: str | None = None
    ) -> None:
        self.dist = Path(dist)
        self.cfg = read_json(self.dist / "app.json")
        self.version = read_json(self.dist / "VERSION.json")
        self.behaviors = read_json(self.dist / "behaviors.json")
        rules_p = self.dist / "rules.json"
        self.rules: dict[str, dict] = read_json(rules_p) if rules_p.exists() else {}
        self.echo = echo
        self.rels = [Relation(**r) for r in self.cfg["relations"]]
        self.pks = self.cfg["pks"]
        self.log_dir = Path(log_dir or self.dist.parent / "logs")
        self.baseline = baseline
        self.templates = {
            s["name"]: (self.dist / s["template"]).read_text(encoding="utf-8")
            for s in self.cfg["screens"]
        }
        self.block_tpl = {
            (s["name"], b["id"]): (self.dist / b["template"]).read_text(encoding="utf-8")
            for s in self.cfg["screens"]
            for b in s["blocks"]
        }

    # ------------------------------------------------------------ read
    def ctx_for(self, root: dict, value: str | None) -> dict[str, list[dict]]:
        if not root or root.get("kind") == "none" or value is None:
            return {}
        op = root.get("op", "eq")
        rows = rows_where(self.echo, root["table"], root["column"], value, root.get("order"), op=op)
        if op in ("contains", "prefix"):
            return {root["table"]: rows}
        skip = set(self.cfg.get("write_tables", [])) - {root["table"]}
        return gather(self.echo, self.rels, self.pks, root["table"], rows, skip_tables=skip)

    def screen_behaviors(self, name: str) -> dict:
        sb = next((s for s in self.behaviors["screens"] if s["screen"] == name), None)
        scr = next(s for s in self.cfg["screens"] if s["name"] == name)
        return {
            "app": self.cfg["app"],
            "screen": name,
            "rules": (sb or {}).get("rules", []),
            "observed": (sb or {}).get("observed", []),
            "blocks": [{k: v for k, v in b.items() if k != "template"} for b in scr["blocks"]],
            "forms": [
                {k: v for k, v in f.items() if k != "write"} | {"writes": bool(f.get("write"))}
                for f in scr["forms"]
            ],
        }

    def render_page(self, name: str, value: str | None) -> str:
        scr = next(s for s in self.cfg["screens"] if s["name"] == name)
        html = T.render(self.templates[name], self.ctx_for(scr["root"], value))
        data = json.dumps(self.screen_behaviors(name)).replace("</", "<\\/")
        foot = (
            f'<footer class="echo-version">Prod Echo replica &middot; {self.cfg["app"]} '
            f"&middot; version {self.version['version']} &middot; capture "
            f"{self.version['capture_date']}"
            + (f" &middot; baseline {self.baseline}" if self.baseline else "")
            + "</footer>"
        )
        inject = (
            f'{foot}<script type="application/json" id="echo-behaviors">{data}</script>'
            '<script src="/echo-runtime.js"></script>'
        )
        return html.replace("</body>", inject + "</body>") if "</body>" in html else html + inject

    # ------------------------------------------------------------ write
    def write(self, name: str, form_sel: str, values: dict) -> dict:
        scr = next(s for s in self.cfg["screens"] if s["name"] == name)
        form = next((f for f in scr["forms"] if f["selector"] == form_sel), None)
        if form is None or not form.get("write"):
            raise HTTPException(404, "no write wiring for this form")
        try:  # the human-written server-side rules (apps/<app>/rules.yaml), before the DB
            GM.check(self.rules.get(f"{name} {form_sel}", {}), values)
        except GM.RuleViolation as v:
            raise HTTPException(400, v.message) from None  # the status the real app used
        w = form["write"]
        row: dict = {}
        flagged = []
        with self.echo.begin() as con:
            for col, spec in w["columns"].items():
                if "from" in spec:
                    v = values.get(spec["from"]) if spec["from"] else None
                    v = None if v in ("", None) else v
                    if v is not None and NUMERIC.search(spec.get("type", "")):
                        try:
                            v = (
                                float(v)
                                if "." in str(v) or "NUMERIC" in spec["type"].upper()
                                else int(v)
                            )
                        except ValueError:
                            raise HTTPException(400, f"{col}: not a number") from None
                    row[col] = v
                elif spec.get("rule") == "generated":
                    cur = con.execute(
                        text(f'SELECT MAX("{col}") FROM "{w["table"]}" WHERE "{col}" >= :s'),
                        {"s": spec["start"]},
                    ).scalar()
                    row[col] = (cur + 1) if cur is not None else spec["start"]
                elif spec.get("rule") == "timestamp":
                    row[col] = iso()
                elif spec.get("rule") == "default":
                    row[col] = spec.get("value")
                else:
                    row[col] = None
                    if col in w.get("flagged_computed", []):
                        flagged.append(col)
            cols = list(row)
            try:
                con.execute(
                    text(
                        f'INSERT INTO "{w["table"]}" ('
                        + ", ".join(f'"{c}"' for c in cols)
                        + ") VALUES ("
                        + ", ".join(f":p{i}" for i in range(len(cols)))
                        + ")"
                    ),
                    {f"p{i}": row[c] for i, c in enumerate(cols)},  # column names may not be
                )  # valid bind names (ORDER#, AMT$)
            except IntegrityError as exc:
                raise HTTPException(400, f"rejected by the database: {exc.orig}") from None
        redirect = T.fill(w["redirect"], {w["table"]: [row]}) if w.get("redirect") else None
        return {"created": {w["pk"]: row[w["pk"]]}, "redirect": redirect, "flagged": flagged}


async def _json_object(request: Request) -> dict:
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(400, "body is not JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(400, "body must be a JSON object")
    return body


def _values(body: dict) -> dict:
    v = body.get("values") or {}
    if not isinstance(v, dict):
        raise HTTPException(400, "values must be an object of field name -> value")
    return {str(k): (None if x is None else str(x)) for k, x in v.items()}


def create_app(
    dist: Path, echo: Engine, log_dir: Path | None = None, baseline: str | None = None
) -> FastAPI:
    rep = Replica(dist, echo, log_dir, baseline)
    api = FastAPI(title=f"Prod Echo replica {rep.cfg['app']}", docs_url=None, redoc_url=None)
    api.state.replica = rep

    @api.get("/echo-runtime.js")
    def runtime_js() -> FileResponse:
        return FileResponse(rep.dist / "echo-runtime.js", media_type="text/javascript")

    @api.get("/echo-version")
    def version() -> dict:
        return rep.version

    @api.get("/echo-block/{screen}/{block}", response_class=HTMLResponse)
    def block(screen: str, block: str, v: str = "") -> str:
        scr = next((s for s in rep.cfg["screens"] if s["name"] == screen), None)
        b = next((b for b in (scr or {}).get("blocks", []) if b["id"] == block), None)
        if b is None:
            raise HTTPException(404)
        ctx = rep.ctx_for({**b["root"], "kind": "query"}, v)
        if not ctx.get(b["root"]["table"]):
            return '<p id="notFound">No customer</p>'
        return T.render(rep.block_tpl[(screen, block)], ctx)

    @api.post("/echo-write/{screen}")
    async def write(screen: str, request: Request) -> JSONResponse:
        body = await _json_object(request)
        return JSONResponse(rep.write(screen, body.get("form", ""), _values(body)))

    @api.post("/echo-unobserved")
    async def unobserved(request: Request) -> dict:
        body = await request.json()
        append_jsonl(
            rep.log_dir / "unobserved.jsonl",
            {"ts": iso(), "app": rep.cfg["app"], "version": rep.version["version"], **body},
        )
        return {"logged": True}

    # The replica answers on the same paths the real app used (the block XHR, the form POST),
    # so request-level parity compares like with like.
    for scr in rep.cfg["screens"]:
        for b in scr["blocks"]:
            if b.get("endpoint"):

                def ep_block(request: Request, scr=scr, b=b) -> HTMLResponse:  # noqa: ANN001, B008
                    # The catalog's delay is replayed where the real app spent it: in the request.
                    time.sleep((b.get("delay_ms") or 0) / 1000.0)
                    return HTMLResponse(
                        block(
                            scr["name"], b["id"], request.query_params.get(b["param"]["name"], "")
                        )
                    )

                api.add_api_route(
                    b["endpoint"], ep_block, methods=["GET"], response_class=HTMLResponse
                )
        for f in scr["forms"]:
            if f.get("write") and f.get("action"):

                async def ep_write(request: Request, scr=scr, f=f) -> JSONResponse:  # noqa: ANN001, B008
                    body = await _json_object(request)
                    return JSONResponse(rep.write(scr["name"], f["selector"], _values(body)))

                api.add_api_route(f["action"], ep_write, methods=["POST"])

    assets = read_json(rep.dist / "assets" / "index.json")
    for a in assets:

        def serve_asset(a=a) -> FileResponse:  # noqa: ANN001, B008
            return FileResponse(rep.dist / "assets" / a["file"], media_type=a["mime"])

        api.add_api_route(a["path"], serve_asset, methods=["GET"])

    screens = sorted(rep.cfg["screens"], key=lambda s: len(s["params"]))
    for scr in screens:

        def page(request: Request, scr=scr) -> HTMLResponse:  # noqa: ANN001, B008
            root = scr["root"]
            value = None
            if root.get("kind") == "path":
                value = request.path_params.get(root["param"])
            elif root.get("kind") == "query":
                value = request.query_params.get(root["param"])
            return HTMLResponse(rep.render_page(scr["name"], value))

        api.add_api_route(scr["route"], page, methods=["GET"], response_class=HTMLResponse)
    if "/" not in {s["route"] for s in screens} and screens:
        first = screens[0]["route"]

        @api.get("/")
        def root_redirect() -> RedirectResponse:
            return RedirectResponse(first)

    @api.get("/favicon.ico")
    def favicon() -> JSONResponse:
        return JSONResponse({}, status_code=404)

    return api
