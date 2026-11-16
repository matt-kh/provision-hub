"""Shared test helpers. Set PROV_UPDATE_GOLDENS=1 to rewrite goldens and the published schemas."""

from __future__ import annotations

import os
import types
from importlib.metadata import entry_points
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "examples"
SCHEMA_DIR = REPO / "schema"
UPDATE_GOLDENS = os.environ.get("PROV_UPDATE_GOLDENS") == "1"


def example_items():
    """(root, kind, name) for every example item: examples/<kind>/<name>/spec.toml."""
    return sorted((EXAMPLES, p.parent.parent.name, p.parent.name) for p in EXAMPLES.glob("*/*/spec.toml"))


@pytest.fixture(autouse=True)
def _no_external_tools(monkeypatch):
    """External validators are optional; keep test results independent of the machine."""
    from prov.backends import nixos_disko, ubuntu_autoinstall

    monkeypatch.setattr(ubuntu_autoinstall.shutil, "which", lambda name: None)
    monkeypatch.setattr(nixos_disko.shutil, "which", lambda name: None)


def fake_entry_point(plugin, name=None, value="tests.fake"):
    """An entry point whose load() returns ``plugin``."""
    return types.SimpleNamespace(name=name or plugin.NAME, value=value, load=lambda: plugin)


@pytest.fixture
def extra_backends(monkeypatch):
    """Register additional backends next to the installed ones: extra_backends(ep, ...)."""
    import prov.registry as registry

    real = list(entry_points(group=registry.ENTRY_POINT_GROUP))

    def install(*eps):
        monkeypatch.setattr(registry, "entry_points", lambda group: real + list(eps))

    return install
