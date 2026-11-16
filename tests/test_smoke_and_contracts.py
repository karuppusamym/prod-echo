"""I-00 scaffold and the docs/04 contracts: every example in specs/ validates."""

from __future__ import annotations

import json
from pathlib import Path

import yaml
from typer.testing import CliRunner

from echo.analyze.models import BotTrace
from echo.capture.models import Manifest
from echo.cli import app
from echo.data.models import Associations, Provenance, SubsetSpec

ROOT = Path(__file__).resolve().parents[1]
SPECS = ROOT / "specs"


def test_cli_help_lists_every_subcommand_group() -> None:
    out = CliRunner().invoke(app, ["--help"]).output
    for group in (
        "capture",
        "analyze",
        "data",
        "mask",
        "generate",
        "runtime",
        "harness",
        "bundle",
        "pipeline",
    ):
        assert group in out


def test_bundle_manifest_example_validates() -> None:
    Manifest(**json.loads((SPECS / "bundle-manifest.example.json").read_text()))


def test_bot_trace_example_validates() -> None:
    BotTrace(**json.loads((SPECS / "bot-trace.example.json").read_text()))


def test_provenance_example_validates() -> None:
    Provenance(**json.loads((SPECS / "provenance.example.json").read_text()))


def test_subset_spec_example_validates() -> None:
    SubsetSpec(**yaml.safe_load((SPECS / "subset.example.yaml").read_text()))


def test_associations_example_validates() -> None:
    Associations(**yaml.safe_load((SPECS / "associations.example.yaml").read_text()))


def test_no_connection_string_or_key_is_written_in_specs() -> None:
    for p in SPECS.iterdir():
        text = p.read_text(encoding="utf-8").lower()
        assert "password" not in text and "://" not in text.replace("http://127.0.0.1", "")
