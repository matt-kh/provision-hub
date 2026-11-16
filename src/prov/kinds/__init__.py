"""Core kinds: what an item is (os-install, later image, vm, cluster).

Kinds are part of prov core and are not pluggable: a kind fixes the core keys of a spec,
which every backend of that kind reads, so adding one is a reviewed change to this package
(see docs/kinds.md). Each kind module exposes:

``NAME``        kind id, ``[a-z][a-z0-9-]*``; the directory ``<root>/<NAME>/``
``VERSIONS``    accepted versions, oldest first (``("v1alpha1",)`` -> ``prov/v1alpha1``)
``PROPERTIES``  JSON Schema of the core keys (may ``$ref`` ``#/$defs/<name>`` of ``common.DEFS``)
``REQUIRED``    required core keys
``semantic``    ``semantic(spec) -> list[Finding]``: checks JSON Schema cannot express
``REFS``        cross-spec references (reserved; empty for now)
``NAME_KEY``    optional: the core key that should equal the item name (warning otherwise)

``KINDS`` is the table core looks kinds up in; tests may add a kind to it.
"""

from __future__ import annotations

import re
from types import ModuleType
from typing import Any, Dict

from ..schema import ENVELOPE_KEYS, ID_PATTERN, LEGACY_KEYS, ConfigError
from . import os_install
from .common import DEFS

KINDS: Dict[str, Any] = {os_install.NAME: os_install}

REQUIRED_MEMBERS = ("NAME", "VERSIONS", "PROPERTIES", "REQUIRED", "semantic", "REFS")


def get(name: str) -> ModuleType:
    """The kind called ``name``; ConfigError (exit 2) when there is none."""
    if name not in KINDS:
        raise ConfigError(f"unknown kind {name!r} (known: {', '.join(sorted(KINDS)) or 'none'})")
    return KINDS[name]


def core_keys() -> Dict[str, str]:
    """Every top-level key core owns, mapped to its owner: envelope keys and each kind's properties.

    A backend NAME may not be one of them, because it names a top-level table.
    """
    owners = {key: "the spec envelope" for key in ENVELOPE_KEYS + LEGACY_KEYS}
    for kind in KINDS.values():
        for key in kind.PROPERTIES:
            owners.setdefault(key, f"kind {kind.NAME!r}")
    return owners


def check_kind(kind: Any) -> None:
    """Raise ConfigError when ``kind`` does not implement the kind contract."""
    from ..validate import check_schema_fragment

    name = getattr(kind, "NAME", "?")
    missing = [m for m in REQUIRED_MEMBERS if not hasattr(kind, m)]
    if missing:
        raise ConfigError(f"kind {name!r} lacks required member(s): {', '.join(missing)}")
    if not (isinstance(kind.NAME, str) and re.fullmatch(ID_PATTERN.strip("^$"), kind.NAME)):
        raise ConfigError(f"kind {name!r}: NAME must match {ID_PATTERN}")
    if not (isinstance(kind.VERSIONS, tuple) and kind.VERSIONS and all(isinstance(v, str) and re.fullmatch("v[0-9]+((alpha|beta)[0-9]+)?", v) for v in kind.VERSIONS)):
        raise ConfigError(f"kind {name!r}: VERSIONS must be a non-empty tuple like ('v1alpha1',)")
    if not isinstance(kind.PROPERTIES, dict) or not isinstance(kind.REFS, dict):
        raise ConfigError(f"kind {name!r}: PROPERTIES and REFS must be dicts")
    if not (isinstance(kind.REQUIRED, list) and set(kind.REQUIRED) <= set(kind.PROPERTIES)):
        raise ConfigError(f"kind {name!r}: REQUIRED must be a list of PROPERTIES keys")
    clash = sorted(set(kind.PROPERTIES) & set(ENVELOPE_KEYS + LEGACY_KEYS))
    if clash:
        raise ConfigError(f"kind {name!r}: core properties may not redefine envelope keys {clash}")
    if not callable(kind.semantic):
        raise ConfigError(f"kind {name!r}: semantic is not callable")
    name_key = getattr(kind, "NAME_KEY", None)
    if name_key is not None and name_key not in kind.PROPERTIES:
        raise ConfigError(f"kind {name!r}: NAME_KEY {name_key!r} is not a core property")
    try:
        check_schema_fragment({"type": "object", "properties": kind.PROPERTIES}, f"<kind {name}>", defs=DEFS, allow_defs=False)
    except ValueError as exc:
        raise ConfigError(f"kind {name!r}: invalid PROPERTIES: {exc}") from None
