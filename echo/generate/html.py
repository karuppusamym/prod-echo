"""A minimal HTML tree (stdlib html.parser) with faithful re-serialization: tag names, attribute
names and values (ids, names, classes, aria, data-*) survive byte-for-byte."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser

VOID = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}
RAW = {"script", "style"}


@dataclass
class Node:
    tag: str | None  # None = text, "#comment", "#doctype"
    attrs: list[list] = field(default_factory=list)  # [[name, value|None], ...]
    children: list[Node] = field(default_factory=list)
    text: str = ""
    parent: Node | None = field(default=None, repr=False)

    def get(self, name: str) -> str | None:
        for k, v in self.attrs:
            if k == name:
                return v
        return None

    def set(self, name: str, value: str | None) -> None:
        for a in self.attrs:
            if a[0] == name:
                a[1] = value
                return
        self.attrs.append([name, value])

    def remove_attr(self, name: str) -> None:
        self.attrs = [a for a in self.attrs if a[0] != name]

    def has(self, name: str) -> bool:
        return any(k == name for k, _ in self.attrs)

    @property
    def classes(self) -> list[str]:
        return (self.get("class") or "").split()

    def elements(self) -> Iterator[Node]:
        for c in self.children:
            if c.tag and not c.tag.startswith("#"):
                yield c
                yield from c.elements()

    def element_children(self) -> list[Node]:
        return [c for c in self.children if c.tag and not c.tag.startswith("#")]

    def text_content(self) -> str:
        if self.tag is None:
            return self.text
        return "".join(c.text_content() for c in self.children if c.tag != "#comment")

    def find_id(self, id_: str) -> Node | None:
        return next((e for e in self.elements() if e.get("id") == id_), None)


class _Builder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node(tag="#root")
        self.cur = self.root

    def handle_decl(self, decl: str) -> None:
        self.cur.children.append(Node(tag="#doctype", text=decl, parent=self.cur))

    def handle_starttag(self, tag: str, attrs: list) -> None:
        n = Node(tag=tag, attrs=[list(a) for a in attrs], parent=self.cur)
        self.cur.children.append(n)
        if tag not in VOID:
            self.cur = n

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        self.cur.children.append(Node(tag=tag, attrs=[list(a) for a in attrs], parent=self.cur))

    def handle_endtag(self, tag: str) -> None:
        n = self.cur
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.cur = n.parent

    def handle_data(self, data: str) -> None:
        self.cur.children.append(Node(tag=None, text=data, parent=self.cur))

    def handle_comment(self, data: str) -> None:
        self.cur.children.append(Node(tag="#comment", text=data, parent=self.cur))


def parse(html: str) -> Node:
    b = _Builder()
    b.feed(html)
    b.close()
    return b.root


def serialize(n: Node) -> str:
    if n.tag is None:
        p = n.parent.tag if n.parent else None
        return n.text if p in RAW else escape(n.text, quote=False)
    if n.tag == "#comment":
        return f"<!--{n.text}-->"
    if n.tag == "#doctype":
        return f"<!{n.text}>"
    inner = "".join(serialize(c) for c in n.children)
    if n.tag == "#root":
        return inner
    attrs = "".join(f" {k}" if v is None else f' {k}="{escape(v, quote=True)}"' for k, v in n.attrs)
    if n.tag in VOID:
        return f"<{n.tag}{attrs}>"
    return f"<{n.tag}{attrs}>{inner}</{n.tag}>"


def clone(n: Node, parent: Node | None = None) -> Node:
    c = Node(tag=n.tag, attrs=[list(a) for a in n.attrs], text=n.text, parent=parent)
    c.children = [clone(x, c) for x in n.children]
    return c


def shadow_root(host: Node) -> Node | None:
    """The declarative shadow root (<template shadowrootmode>) a captured host carries."""
    return next(
        (c for c in host.element_children() if c.tag == "template" and c.get("shadowrootmode")),
        None,
    )


def select(root: Node, key: str) -> Node | None:
    """Resolve the keys the page script produces: '#id', '[name=x]', 'a > b:nth-of-type(n)',
    'tag#id' / "tag[attr='v']" (frame owners), and '#host >>> #inner' (open shadow roots)."""
    if " >>> " in key:
        host_key, _, rest = key.partition(" >>> ")
        host = select(root, host_key)
        sr = shadow_root(host) if host is not None else None
        return select(sr, rest) if sr is not None else None
    m = re.fullmatch(r"([a-z][\w-]*)(?:#([\w-]+)|\[(\w+)='([^']*)'\])", key)
    if m:
        tag, id_, attr, val = m.groups()
        for e in root.elements():
            if e.tag == tag and (e.get("id") == id_ if id_ else e.get(attr) == val):
                return e
        return None
    if key.startswith("#") and " " not in key:
        return root.find_id(key[1:])
    if key.startswith("[name=") and key.endswith("]"):
        name = key[6:-1]
        return next((e for e in root.elements() if e.get("name") == name), None)
    parts = [p.strip() for p in key.split(">")]
    cur: Node | None = None
    for i, p in enumerate(parts):
        if i == 0 and p.startswith("#"):
            cur = root.find_id(p[1:])
            continue
        base = cur if cur is not None else root
        tag_cls, _, nth = p.partition(":nth-of-type(")
        tag, *cls = tag_cls.split(".")
        idx = int(nth.rstrip(")")) if nth else 1
        same = [c for c in base.element_children() if c.tag == tag]
        if len(same) < idx:
            return None
        cur = same[idx - 1]
        if any(c not in cur.classes for c in cls):
            return None
    return cur
