"""Shared test helpers. Set PROV_UPDATE_GOLDENS=1 to rewrite goldens and the published schema."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
SCHEMA_FILE = REPO / "schema" / "host.v1.schema.json"
UPDATE_GOLDENS = os.environ.get("PROV_UPDATE_GOLDENS") == "1"


def example_hosts():
    """(root, host) for every example host: examples/<name>/hosts/<host>/host.toml."""
    return sorted((p.parent.parent.parent, p.parent.name) for p in EXAMPLES.glob("*/hosts/*/host.toml"))


@pytest.fixture(autouse=True)
def _no_external_tools(monkeypatch):
    """External validators are optional; keep test results independent of the machine."""
    from prov.distros import ubuntu

    monkeypatch.setattr(ubuntu.shutil, "which", lambda name: None)
