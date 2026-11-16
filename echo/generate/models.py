"""Contracts for the human-written files that sit next to a generated app (docs/04, docs/07).

`apps/<app>/rules.yaml` holds the server-side validations the real app applies and the capture
cannot see (the replica would otherwise accept anything the database accepts). A person writes it
from the app's own specification or an SME; nothing here infers it. Fields are named by the form
field name the browser posts, the same name the write wiring maps from.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class FieldRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = False
    min: Decimal | None = None
    max: Decimal | None = None
    max_length: int | None = Field(default=None, ge=1)
    pattern: str | None = None
    one_of: list[str] | None = None
    message: str | None = None  # shown on the form, as the real app words it

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: str | None) -> str | None:
        if v is not None:
            re.compile(v)
        return v


class FormRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    screen: str
    form: str | None = None  # form selector ("#loanForm"); None = every write form on the screen
    fields: dict[str, FieldRule]


class ServerRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: str
    written_by: str
    source: str  # where the rules come from: the app's spec, an SME session, a defect ticket
    forms: list[FormRules]


class RuleViolation(ValueError):
    def __init__(self, field: str, rule: str, message: str) -> None:
        super().__init__(message)
        self.field, self.rule, self.message = field, rule, message


def load_rules(path: Path) -> ServerRules | None:
    p = Path(path)
    if not p.exists():
        return None
    return ServerRules(**(yaml.safe_load(p.read_text(encoding="utf-8")) or {}))


def _number(v: str) -> Decimal | None:
    try:
        return Decimal(v.replace(",", ""))
    except InvalidOperation:
        return None


def check(rules: dict[str, dict], values: dict[str, str | None]) -> None:
    """Raise RuleViolation for the first field (in rule order) that breaks a rule."""
    for name, raw in rules.items():
        r = FieldRule(**raw)
        v = values.get(name)
        v = None if v is None or str(v).strip() == "" else str(v)

        def bad(rule: str, default: str, name: str = name, r: FieldRule = r) -> RuleViolation:
            return RuleViolation(name, rule, r.message or default)

        if v is None:
            if r.required:
                raise bad("required", f"{name} is required")
            continue
        if r.max_length is not None and len(v) > r.max_length:
            raise bad("max_length", f"{name} is longer than {r.max_length} characters")
        if r.pattern is not None and not re.fullmatch(r.pattern, v):
            raise bad("pattern", f"{name} is not in the expected format")
        if r.one_of is not None and v not in r.one_of:
            raise bad("one_of", f"{name} is not an allowed value")
        if r.min is not None or r.max is not None:
            n = _number(v)
            if n is None:
                raise bad("number", f"{name} is not a number")
            if r.min is not None and n < r.min:
                raise bad("min", f"{name} is below {r.min}")
            if r.max is not None and n > r.max:
                raise bad("max", f"{name} is above {r.max}")


def resolve(rules: ServerRules, cfg: dict) -> dict[str, dict[str, dict]]:
    """Attach rules to the package's write forms: {"<screen> <selector>": {field: rule}}.

    Fails when a rule names a screen, a form or a field the package does not have, so a stale
    rules.yaml is caught at build time rather than silently ignored.
    """
    out: dict[str, dict[str, dict]] = {}
    screens = {s["name"]: s for s in cfg["screens"]}
    for fr in rules.forms:
        scr = screens.get(fr.screen)
        if scr is None:
            raise ValueError(f"rules.yaml: screen {fr.screen!r} is not in {rules.app}")
        forms = [f for f in scr["forms"] if f.get("write")]
        if fr.form is not None:
            forms = [f for f in forms if f["selector"] == fr.form]
        if not forms:
            raise ValueError(f"rules.yaml: no write form {fr.form or ''} on {fr.screen}")
        for f in forms:
            posted = {c["from"] for c in f["write"]["columns"].values() if c.get("from")}
            unknown = sorted(set(fr.fields) - posted)
            if unknown:
                raise ValueError(
                    f"rules.yaml: {fr.screen} {f['selector']} posts no field named {unknown}"
                )
            key = f"{fr.screen} {f['selector']}"
            out.setdefault(key, {}).update(
                {k: v.model_dump(mode="json", exclude_none=True) for k, v in fr.fields.items()}
            )
    return out
