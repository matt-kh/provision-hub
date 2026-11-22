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


# The distribution whose entry points are prov's built-ins; only its kinds may use bare names.
PROVISION_HUB = "provision-hub"


def fake_entry_point(plugin, name=None, value="tests.fake", dist=None):
    """An entry point whose load() returns ``plugin``; ``dist`` names the distribution that registers it.

    Without ``dist`` the plugin is a third-party one; ``dist=PROVISION_HUB`` makes it look
    built-in (a bare test kind such as ``widget`` needs that).
    """
    return types.SimpleNamespace(
        name=name or plugin.NAME, value=value, load=lambda: plugin, dist=types.SimpleNamespace(name=dist) if dist else None
    )


@pytest.fixture(autouse=True)
def _fresh_kinds():
    """Kinds are discovered once per process; every test starts and ends with a fresh lookup."""
    from prov import kinds

    kinds.reset()
    yield
    kinds.reset()


@pytest.fixture
def extra_kinds(monkeypatch):
    """Register additional kinds next to the installed ones: extra_kinds(ep, ...)."""
    import prov.kinds as kinds

    real = list(entry_points(group=kinds.ENTRY_POINT_GROUP))

    def install(*eps):
        monkeypatch.setattr(kinds, "entry_points", lambda group: real + list(eps))
        kinds.reset()

    return install


@pytest.fixture
def extra_backends(monkeypatch):
    """Register additional backends next to the installed ones: extra_backends(ep, ...)."""
    import prov.registry as registry

    real = list(entry_points(group=registry.ENTRY_POINT_GROUP))

    def install(*eps):
        monkeypatch.setattr(registry, "entry_points", lambda group: real + list(eps))

    return install
