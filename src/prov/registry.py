"""Distro plugin registry, discovered through the ``prov.distros`` entry-point group.

A plugin is a module (or any object) exposing the contract described in
``docs/adding-a-distro.md``: ``NAME``, ``SCHEMA``, ``render``, ``validate``, ``plan``, and
optionally ``check`` and ``starter``.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from types import ModuleType
from typing import Any, Dict

from .schema import ConfigError

ENTRY_POINT_GROUP = "prov.distros"
REQUIRED_MEMBERS = ("NAME", "SCHEMA", "render", "validate", "plan")

Distro = Any  # a module or object implementing the contract


def check_contract(plugin: Distro, origin: str) -> None:
    """Raise ConfigError when ``plugin`` does not implement the required contract."""
    missing = [m for m in REQUIRED_MEMBERS if not hasattr(plugin, m)]
    if missing:
        raise ConfigError(f"distro plugin {origin} lacks required member(s): {', '.join(missing)}")
    for fn in ("render", "validate", "plan"):
        if not callable(getattr(plugin, fn)):
            raise ConfigError(f"distro plugin {origin}: {fn} is not callable")
    if not isinstance(plugin.NAME, str) or not isinstance(plugin.SCHEMA, dict):
        raise ConfigError(f"distro plugin {origin}: NAME must be a str and SCHEMA a dict")
    from .schema import DEFS
    from .validate import check_schema_fragment  # local: validate imports nothing from here

    try:
        check_schema_fragment(plugin.SCHEMA, f"[{plugin.NAME}]", defs=DEFS, allow_defs=False)
    except ValueError as exc:
        raise ConfigError(f"distro plugin {origin}: invalid SCHEMA: {exc}") from None
    except Exception as exc:  # backstop: a schema shape the checker did not anticipate
        raise ConfigError(f"distro plugin {origin}: invalid SCHEMA: {type(exc).__name__}: {exc}") from None


def discover() -> Dict[str, Distro]:
    """Load every registered distro plugin, keyed by NAME (sorted)."""
    found: Dict[str, Distro] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        origin = f"{ep.name} = {ep.value}"
        try:
            plugin = ep.load()
        except Exception as exc:  # a broken third-party plugin must not hide the others' errors
            raise ConfigError(f"cannot load distro plugin {origin}: {exc}") from exc
        check_contract(plugin, origin)
        if plugin.NAME != ep.name:
            raise ConfigError(f"distro plugin {origin}: entry-point name differs from NAME {plugin.NAME!r}")
        if plugin.NAME in found:
            raise ConfigError(f"distro {plugin.NAME!r} is registered twice")
        found[plugin.NAME] = plugin
    return dict(sorted(found.items()))


def builtin() -> Dict[str, ModuleType]:
    """The distros shipped with prov; the published JSON Schema is generated from these."""
    from .distros import ubuntu

    return {ubuntu.NAME: ubuntu}
