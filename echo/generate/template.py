"""Template extraction (I-19): DOM snapshot -> template bound to columns.

1. Normalize: scripts, inline handlers and script preloads are removed (ADR-008); ids, names,
   classes, aria and data-* attributes and the structure are kept byte-for-byte.
2. Every text node and attribute value equal to (or, for attributes, containing) a value of the
   captured record's rows becomes a slot `{TABLE.COLUMN}`; a few formatters are tried
   (case, "first last" concatenation, row count).
3. Repeating siblings whose slots differ only by row collapse into a loop over the table.
4. Text that looks like data but did not bind is listed in `unbound` for a human.
5. Leakage check: rendered for a different record, no literal value of the captured one remains.

Slots are attributes: `data-echo-tpl="{CUSTOMER.FULL_NAME}"` (text), `data-echo-attrs` (JSON
name -> template), `data-echo-loop="ACCT"` on the repeated element. The data service renders them
(render() below); the browser only receives finished HTML plus blocks fetched on demand.
"""

from __future__ import annotations

import json
import logging
import re
from html import escape

from echo.generate import html as H
from echo.generate.context import sort_key
from echo.util import loose_value, normalize_value

log = logging.getLogger(__name__)
SLOT_RE = re.compile(r"\{(#count:)?([A-Z0-9_]+)\.?([A-Z0-9_]*)(?:@(\d+))?\}")
DATA_LIKE = re.compile(r"\d{3,}|^[A-Z][A-Z .'-]{3,}$")


OPAQUE_TAGS = ("canvas", "embed", "object")


def mark_opaque(root: H.Node) -> list[dict]:
    """Content the replica cannot reproduce: a canvas stays (empty: the real script that drew it
    is never served), an embedded object loses its source (it may be a customer's document).
    Each is marked `data-echo-opaque` so the behavior runtime logs clicks on it as unobserved."""
    out = []
    for e in root.elements():
        if e.tag not in OPAQUE_TAGS:
            continue
        e.attrs = [a for a in e.attrs if a[0] not in ("src", "data", "data-echo-opaque")]
        e.attrs.append(["data-echo-opaque", e.tag])
        if e.tag == "object":
            e.children = []  # fallback content and <param>s may carry the document's source
        out.append({"element": e.get("id") or e.tag, "kind": e.tag})
    return out


def normalize_dom(root: H.Node) -> H.Node:
    def walk(n: H.Node) -> None:
        keep = []
        for c in n.children:
            if c.tag in ("script", "#comment"):  # comments can carry captured values too
                continue
            if c.tag == "link" and (c.get("as") == "script" or c.get("rel") == "modulepreload"):
                continue
            if c.tag and not c.tag.startswith("#"):
                c.attrs = [a for a in c.attrs if not a[0].lower().startswith("on")]
                walk(c)
            keep.append(c)
        n.children = keep

    walk(root)
    return root


def value_index(ctx: dict[str, list[dict]]) -> dict[str, list[tuple[str, str, int]]]:
    idx: dict[str, list[tuple[str, str, int]]] = {}
    for t, rows in ctx.items():
        for i, r in enumerate(rows):
            for c, v in r.items():
                if v is None or c == "ECHO_BATCH":
                    continue
                s = normalize_value(v)
                if s:
                    idx.setdefault(s, []).append((t, c, i))
    return idx


def _concat_match(text: str, ctx: dict[str, list[dict]]) -> str | None:
    parts = text.split()
    if len(parts) < 2:
        return None
    for t, rows in ctx.items():
        for i, r in enumerate(rows[:5]):
            vals = {normalize_value(v): c for c, v in r.items() if v is not None}
            first, rest = normalize_value(parts[0]), normalize_value(" ".join(parts[1:]))
            if first in vals and rest in vals:
                suf = "" if i == 0 else f"@{i}"
                return f"{{{t}.{vals[first]}{suf}}} {{{t}.{vals[rest]}{suf}}}"
    return None


def _slot(hit: tuple[str, str, int]) -> str:
    t, c, i = hit
    return f"{{{t}.{c}}}" if i == 0 else f"{{{t}.{c}@{i}}}"


def shape(txt: str) -> str:
    """Letters become A/a and digits 9, punctuation stays: enough to see what kind of value
    did not bind, without the value."""
    return "".join(
        "9" if c.isdigit() else ("A" if c.isupper() else "a") if c.isalpha() else c for c in txt
    )


def bind_tree(root: H.Node, ctx: dict[str, list[dict]]) -> dict:
    """Turn matching text/attributes into slots in place. Returns {'slots', 'unbound'}."""
    idx = value_index(ctx)
    slots: list[dict] = []
    unbound: list[dict] = []
    body = next((e for e in root.elements() if e.tag == "body"), root)
    for el in body.elements():
        kids = el.element_children()
        txt = el.text_content().strip()
        if txt and not kids and el.tag not in ("option", "style", "title"):
            norm = normalize_value(txt)
            tpl = None
            if norm in idx:
                tpl = _slot(_pick(el, idx[norm], idx))
            else:
                tpl = _concat_match(txt, ctx)
            if tpl is None and norm.isdigit():
                counts = {t: len(r) for t, r in ctx.items()}
                hit = next((t for t, n in counts.items() if str(n) == norm and n > 1), None)
                if hit:
                    tpl = f"{{#count:{hit}}}"
            if tpl:
                el.set("data-echo-tpl", tpl)
                el.children = []
                slots.append({"element": _path(el), "tpl": tpl})
            elif DATA_LIKE.search(txt) and len(txt) < 80:
                # The shape, not the text: the text may be a customer value.
                unbound.append({"element": _path(el), "shape": shape(txt)})
        attrs = {}
        names = ["href", "value", "action", "src", "title", "aria-label", "placeholder", "alt"]
        names += [
            a for a, _v in el.attrs if a.startswith("data-") and not a.startswith("data-echo")
        ]
        for name in names:
            v = el.get(name)
            if not v or el.tag == "option":
                continue
            new = _attr_tpl(v, idx)
            if new != v:
                attrs[name] = new
                el.set(name, "")
        if attrs:
            el.set("data-echo-attrs", json.dumps(attrs, separators=(",", ":")))
            slots.append({"element": _path(el), "attrs": attrs})
    return {"slots": slots, "unbound": unbound}


ROW_TAGS = ("tr", "li")


def _row_of(el: H.Node) -> H.Node | None:
    n = el.parent
    while n is not None and n.tag not in (None, "#root"):
        if n.tag in ROW_TAGS:
            return n
        n = n.parent
    return None


def _pick(
    el: H.Node, hits: list[tuple[str, str, int]], idx: dict[str, list[tuple[str, str, int]]]
) -> tuple[str, str, int]:
    """Among equal values, prefer the table (and row) the rest of the element's row binds to;
    outside a row, the first hit (root table first, nearest tables next)."""
    row = _row_of(el)
    if row is None or len(hits) == 1:
        return hits[0]
    votes: dict[tuple[str, int], int] = {}
    for cell in row.elements():
        if cell.element_children():
            continue
        cands = idx.get(normalize_value(cell.text_content().strip()), [])
        for t, _c, i in {(t, c, i) for t, c, i in cands}:
            votes[(t, i)] = votes.get((t, i), 0) + 1
    ranked = sorted(hits, key=lambda h: -votes.get((h[0], h[2]), 0))
    return ranked[0]


def _token_re(form: str) -> re.Pattern:
    # a value counts only as a whole token: "MMA" must not bind inside "summary"
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(form) + r"(?![A-Za-z0-9])")


def _attr_tpl(v: str, idx: dict) -> str:
    # Longest values first so 0048812231 wins over 4471-like fragments.
    best = None
    for s, hits in idx.items():
        if len(s) < 3:
            continue
        for form in {s, s.lower(), s.replace(" ", "+"), s.replace(" ", "%20")}:
            if not form or form not in v or (best is not None and len(form) <= len(best[0])):
                continue
            if _token_re(form).search(v):
                best = (form, hits[0])
    if best is None:
        return v
    return _token_re(best[0]).sub(lambda _m: _slot(best[1]), v)


def _path(el: H.Node) -> str:
    parts = []
    n: H.Node | None = el
    while n is not None and n.tag not in (None, "#root"):
        if n.get("id"):
            parts.append(f"#{n.get('id')}")
            break
        same = [c for c in (n.parent.element_children() if n.parent else []) if c.tag == n.tag]
        parts.append(f"{n.tag}:nth-of-type({same.index(n) + 1})" if len(same) > 1 else n.tag)
        n = n.parent
    return " > ".join(reversed(parts))


def _slot_tables(el: H.Node) -> list[tuple[str, int]]:
    out = []
    for e in [el, *el.elements()]:
        for src in [e.get("data-echo-tpl") or ""] + list(
            json.loads(e.get("data-echo-attrs") or "{}").values()
        ):
            for m in SLOT_RE.finditer(src):
                if not m.group(1):
                    out.append((m.group(2), int(m.group(4) or 0)))
    return out


def _strip_rows(el: H.Node, table: str) -> None:
    for e in [el, *el.elements()]:
        if e.get("data-echo-tpl"):
            e.set(
                "data-echo-tpl",
                re.sub(
                    rf"\{{{table}\.([A-Z0-9_]+)@\d+\}}", rf"{{{table}.\1}}", e.get("data-echo-tpl")
                ),
            )
        if e.get("data-echo-attrs"):
            d = json.loads(e.get("data-echo-attrs"))
            d = {
                k: re.sub(rf"\{{{table}\.([A-Z0-9_]+)@\d+\}}", rf"{{{table}.\1}}", v)
                for k, v in d.items()
            }
            e.set("data-echo-attrs", json.dumps(d, separators=(",", ":")))


def detect_loops(root: H.Node, pks: dict[str, list[str]]) -> list[dict]:
    loops = []
    for parent in [root, *root.elements()]:
        groups: dict[tuple, list[H.Node]] = {}
        for c in parent.element_children():
            groups.setdefault((c.tag, c.get("class") or ""), []).append(c)
        for (tag, _cls), members in groups.items():
            tabs = [_slot_tables(m) for m in members]
            if not all(tabs):
                continue
            names = {t for ts in tabs for t, _ in ts}
            if len(names) != 1:
                continue
            table = names.pop()
            rows = [{i for _, i in ts} for ts in tabs]
            distinct = (
                len(members) >= 2
                and all(len(r) == 1 for r in rows)
                and len({next(iter(r)) for r in rows}) == len(members)
            )
            single = (
                len(members) == 1 and tag in ("tr", "li") and parent.tag in ("tbody", "ul", "ol")
            )
            if not (distinct or single):
                continue
            keep = members[0]
            for m in members[1:]:
                parent.children.remove(m)
            _strip_rows(keep, table)
            first_col = next(
                (
                    m.group(3)
                    for e in [keep, *keep.elements()]
                    for m in SLOT_RE.finditer(e.get("data-echo-tpl") or "")
                    if m.group(3)
                ),
                None,
            )
            order = [c for c in [first_col] + pks.get(table, []) if c]
            keep.set("data-echo-loop", table)
            keep.set("data-echo-order", ",".join(dict.fromkeys(order)))
            loops.append(
                {
                    "element": _path(keep),
                    "table": table,
                    "order": order,
                    "captured_rows": len(members),
                }
            )
    return loops


def apply_initial_state(root: H.Node, states: list[dict]) -> None:
    """Initial visibility / enabled / options as they were when the screen first settled."""
    invisible = set()
    for s in states:
        el = H.select(root, s["key"])
        if el is not None and not s.get("visible", True):
            invisible.add(id(el))

    def under_invisible(el: H.Node) -> bool:
        n = el.parent
        while n is not None:
            if id(n) in invisible or n.has("hidden"):
                return True
            n = n.parent
        return False

    for s in states:
        el = H.select(root, s["key"])
        if el is None:
            continue
        if (
            not s.get("visible", True)
            and not el.has("hidden")
            and el.tag not in ("option",)
            and not (el.tag == "input" and el.get("type") == "hidden")
            and not under_invisible(el)
        ):
            el.set("hidden", None)
        if s.get("visible") and el.has("hidden"):
            el.remove_attr("hidden")
        if s.get("enabled") is False and not el.has("disabled"):
            el.set("disabled", None)
        if s.get("enabled") is True and el.has("disabled"):
            el.remove_attr("disabled")
        if el.tag == "select" and s.get("options") is not None:
            opts = []
            for v, t in s["options"]:
                o = H.Node(tag="option", attrs=[["value", v]], parent=el)
                o.children = [H.Node(tag=None, text=t, parent=o)]
                opts.append(o)
            el.children = opts


# ------------------------------------------------------------------ rendering
def fill(tpl: str, ctx: dict[str, list[dict]], fmt=lambda v: str(v)) -> str:  # noqa: ANN001
    def rep(m: re.Match) -> str:
        if m.group(1):
            return str(len(ctx.get(m.group(2)) or []))
        rows = ctx.get(m.group(2)) or []
        i = int(m.group(4) or 0)
        if i >= len(rows):
            return ""
        v = rows[i].get(m.group(3))
        return "" if v is None else fmt(v)

    return SLOT_RE.sub(rep, tpl)


def _display(v: object) -> str:
    return str(v)


def render_tree(root: H.Node, ctx: dict[str, list[dict]]) -> H.Node:
    out = H.clone(root)

    def walk(n: H.Node, c: dict[str, list[dict]]) -> None:
        new_children = []
        for ch in n.children:
            if ch.tag and not ch.tag.startswith("#") and ch.get("data-echo-loop"):
                table = ch.get("data-echo-loop")
                order = [x for x in (ch.get("data-echo-order") or "").split(",") if x]
                rows = sorted(c.get(table) or [], key=lambda r: sort_key(r, order))
                for r in rows:
                    cc = H.clone(ch, n)
                    sub = dict(c)
                    sub[table] = [r]
                    _fill_el(cc, sub)
                    walk(cc, sub)
                    new_children.append(cc)
                continue
            if ch.tag and not ch.tag.startswith("#"):
                _fill_el(ch, c)
                walk(ch, c)
            new_children.append(ch)
        n.children = new_children

    walk(out, ctx)
    return out


def _fill_el(el: H.Node, ctx: dict[str, list[dict]]) -> None:
    if el.get("data-echo-tpl") is not None:
        el.children = [
            H.Node(tag=None, text=fill(el.get("data-echo-tpl"), ctx, _display), parent=el)
        ]
    if el.get("data-echo-attrs"):
        for k, v in json.loads(el.get("data-echo-attrs")).items():
            el.set(k, fill(v, ctx, _display))


def render(template_html: str, ctx: dict[str, list[dict]]) -> str:
    return H.serialize(render_tree(H.parse(template_html), ctx))


def literal_values(ctx: dict[str, list[dict]], min_len: int = 4) -> set[str]:
    return set(literal_sources(ctx, min_len))


def literal_sources(ctx: dict[str, list[dict]], min_len: int = 4) -> dict[str, str]:
    """value -> 'TABLE.COLUMN' it came from (first seen)."""
    out: dict[str, str] = {}
    for table, rows in ctx.items():
        for r in rows:
            for c, v in r.items():
                if v is None or c == "ECHO_BATCH":
                    continue
                s = str(v).strip()
                if len(s) >= min_len:
                    out.setdefault(s, f"{table}.{c}")
    return out


LOOSE_MIN = 6  # punctuation-insensitive matching only for values long enough to be specific


def leakage(
    rendered: str, captured_ctx: dict[str, list[dict]], other_ctx: dict[str, list[dict]]
) -> list[str]:
    """Columns whose captured-record value is still present in output rendered for another
    record. Returns 'TABLE.COLUMN' names, never the values themselves (they are customer data).

    Matching is exact (case-insensitive) and also punctuation-insensitive for longer values, so a
    phone stored as 5551234567 is caught when shown as (555) 123-4567."""
    tree = H.parse(rendered)
    text_ = rendered.upper()
    loose_text = loose_value(H.serialize(tree)) + loose_value(_visible_text(tree))
    allowed = {s.upper() for s in literal_values(other_ctx, 1)}
    allowed_loose = {loose_value(s) for s in allowed}
    # Fixed vocabulary of the app (select options) is UI, not record data.
    for o in tree.elements():
        if o.tag == "option":
            allowed |= {(o.get("value") or "").upper(), o.text_content().strip().upper()}
    leaks: set[str] = set()
    for v, src in sorted(literal_sources(captured_ctx).items()):
        u = v.upper()
        if u in allowed or u.replace(" ", "+") in allowed:
            continue
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*", v):
            continue  # audit timestamps are not shown on screens
        lv = loose_value(v)
        if u in text_ or escape(u) in text_:
            leaks.add(src)
        elif len(lv) >= LOOSE_MIN and lv not in allowed_loose and lv in loose_text:
            leaks.add(src)
    return sorted(leaks)


def _visible_text(tree: H.Node) -> str:
    return " ".join(e.text_content() for e in tree.elements() if not e.element_children())
