"""Kinds: what an item is (os-install, later image, vm, cluster), discovered as plugins.

Every kind, prov's own ``os-install`` included, is a plugin registered under the
``prov.kinds`` entry-point group (entry-point name = ``NAME``), exactly like backends under
``prov.backends``. Core looks kinds up through ``discover()`` and never imports a kind
module. Each kind module exposes (see docs/kinds.md):

``NAME``        kind id; the directory ``<root>/<NAME>/``. Bare (``os-install``) only for
                kinds of the provision-hub distribution; every other kind is dotted,
                ``<label>.<group>`` with a dotted group (``registry.example.org``)
``PLUGIN_API``  the kind contract version it implements (``prov.plugin.PLUGIN_API``)
``VERSIONS``    accepted versions, oldest first (``("v1alpha1",)`` -> ``<group>/v1alpha1``)
``PROPERTIES``  JSON Schema of the core keys (may ``$ref`` ``#/$defs/<name>`` of ``common.DEFS``)
``REQUIRED``    required core keys
``semantic``    ``semantic(spec) -> list[Finding]``: checks JSON Schema cannot express
``REFS``        cross-spec references (reserved; empty for now)
``NAME_KEY``    optional: the core key that should equal the item name (warning otherwise)
``SCHEMA_URL``  optional: https base URL (ending in ``/``) of the composed schema's ``$id``

The api group (``prov/v1alpha1``, ``example.org/v1alpha1``) follows from NAME
(``prov.schema.kind_group``); the group ``prov`` belongs to provision-hub's bare kinds, and
a dotted kind's group always contains a dot, so it can never be ``prov``.
"""

from __future__ import annotations

import re
from importlib.metadata import entry_points
from types import ModuleType
from typing import Any, Dict, Optional, Tuple

from .. import plugin as plugins
from ..schema import ENVELOPE_KEYS, ID_PATTERN, KIND_MAX_LENGTH, KIND_PATTERN, LEGACY_KEYS, ConfigError
from ..text import esc, short_repr
from .common import DEFS

ENTRY_POINT_GROUP = "prov.kinds"
REQUIRED_MEMBERS = ("NAME", "PLUGIN_API", "VERSIONS", "PROPERTIES", "REQUIRED", "semantic", "REFS")

Kind = Any  # a module or object implementing the contract

# (every kind by NAME, the names registered by the provision-hub distribution); see reset().
_cache: Optional[Tuple[Dict[str, Kind], frozenset]] = None


def _full(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern.strip("^$"), value) is not None


def check_kind(kind: Kind, origin: Optional[str] = None) -> None:
    """Raise ConfigError when ``kind`` does not implement the kind contract.

    ``origin`` (an escaped ``<entry point> = <value>``) names the plugin in messages.
    """
    from ..validate import check_schema_fragment

    name = getattr(kind, "NAME", "?")
    who = f"kind plugin {origin}" if origin else f"kind {short_repr(name)}"
    missing = [m for m in REQUIRED_MEMBERS if not hasattr(kind, m)]
    if missing:
        raise ConfigError(f"{who} lacks required member(s): {', '.join(missing)}")
    if isinstance(kind.NAME, str) and kind.NAME.count(".") == 1 and _full(KIND_PATTERN, kind.NAME + ".x"):
        raise ConfigError(
            f"{who}: NAME {short_repr(kind.NAME)} needs a dotted group (Kubernetes CRD style): "
            f"<label>.<domain>.<tld>, e.g. 'registry.example.org' (group 'example.org')"
        )
    if not (isinstance(kind.NAME, str) and len(kind.NAME) <= KIND_MAX_LENGTH and _full(KIND_PATTERN, kind.NAME)):
        raise ConfigError(f"{who}: NAME must match {ID_PATTERN} or be dotted like 'registry.example.org' (at most {KIND_MAX_LENGTH} characters)")
    plugins.check_api(kind, who, required=True)
    if not (isinstance(kind.VERSIONS, tuple) and kind.VERSIONS and all(isinstance(v, str) and re.fullmatch("v[0-9]+((alpha|beta)[0-9]+)?", v) for v in kind.VERSIONS)):
        raise ConfigError(f"{who}: VERSIONS must be a non-empty tuple like ('v1alpha1',)")
    if not isinstance(kind.PROPERTIES, dict) or not isinstance(kind.REFS, dict):
        raise ConfigError(f"{who}: PROPERTIES and REFS must be dicts")
    if not (isinstance(kind.REQUIRED, list) and set(kind.REQUIRED) <= set(kind.PROPERTIES)):
        raise ConfigError(f"{who}: REQUIRED must be a list of PROPERTIES keys")
    clash = sorted(set(kind.PROPERTIES) & set(ENVELOPE_KEYS + LEGACY_KEYS))
    if clash:
        raise ConfigError(f"{who}: core properties may not redefine envelope keys {clash}")
    if not callable(kind.semantic):
        raise ConfigError(f"{who}: semantic is not callable")
    name_key = getattr(kind, "NAME_KEY", None)
    if name_key is not None and name_key not in kind.PROPERTIES:
        raise ConfigError(f"{who}: NAME_KEY {short_repr(name_key)} is not a core property")
    url = getattr(kind, "SCHEMA_URL", None)
    if url is not None and not (isinstance(url, str) and re.fullmatch("https://[!-~]+/", url)):
        raise ConfigError(f"{who}: SCHEMA_URL must be an https base URL ending in '/' (printable ASCII, no spaces); prov appends <NAME>.<version>.schema.json")
    try:
        check_schema_fragment({"type": "object", "properties": kind.PROPERTIES}, f"<kind {esc(str(name))}>", defs=DEFS, allow_defs=False)
    except ValueError as exc:
        raise ConfigError(f"{who}: invalid PROPERTIES: {esc(str(exc))}") from None


def _load_one(ep: Any) -> Kind:
    """Load and check one kind entry point."""
    origin = plugins.origin(ep)
    kind = plugins.load(ep, "kind")
    check_kind(kind, origin)
    if kind.NAME != ep.name:
        raise ConfigError(f"kind plugin {origin}: entry-point name differs from NAME {short_repr(kind.NAME)}")
    return kind


def _load() -> Tuple[Dict[str, Kind], frozenset]:
    found: Dict[str, Kind] = {}
    origins: Dict[str, str] = {}
    builtin_names = set()
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        origin = plugins.origin(ep)
        kind = _load_one(ep)
        own = plugins.from_distribution(ep)
        if not own and "." not in kind.NAME:
            raise ConfigError(
                f"kind plugin {origin}: bare kind names are reserved for the {plugins.DISTRIBUTION} distribution; "
                f"name a third-party kind <label>.<your domain>, e.g. '{kind.NAME}.example.org'"
            )
        if kind.NAME in found:
            raise ConfigError(f"kind {short_repr(kind.NAME)} is registered twice: {origins[kind.NAME]} and {origin}")
        found[kind.NAME] = kind
        origins[kind.NAME] = origin
        if own:
            builtin_names.add(kind.NAME)
    return dict(sorted(found.items())), frozenset(builtin_names)


def _discovered() -> Tuple[Dict[str, Kind], frozenset]:
    global _cache
    if _cache is None:
        _cache = _load()  # an error is raised again on the next call: nothing is cached
    return _cache


def discover() -> Dict[str, Kind]:
    """Every registered kind, keyed and ordered by NAME; loaded once per process (``reset``)."""
    return dict(_discovered()[0])


def reset() -> None:
    """Forget the discovered kinds, so the next ``discover()`` reads the entry points again."""
    global _cache
    _cache = None


def builtin() -> Dict[str, ModuleType]:
    """The kinds the provision-hub distribution registers; the published schemas cover these.

    Read from that distribution's entry points only (like ``prov.registry.builtin``), so a
    broken third-party kind does not change or break the published files.
    """
    found: Dict[str, Kind] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if plugins.from_distribution(ep):
            found[ep.name] = _load_one(ep)
    return dict(sorted(found.items()))


def unknown_hint(known: Dict[str, Kind]) -> str:
    """The list of known kinds for an "unknown kind" message, or a hint when there is none."""
    if known:
        return ", ".join(known)
    return f"none; is {plugins.DISTRIBUTION} installed? run uv sync"


def get(name: str) -> Kind:
    """The kind called ``name``; ConfigError (exit 2) when there is none."""
    found = _discovered()[0]
    if name not in found:
        raise ConfigError(f"unknown kind {short_repr(name)} (known: {unknown_hint(found)})")
    return found[name]


def core_keys(kind_name: str) -> Dict[str, str]:
    """Top-level keys a backend of ``kind_name`` may not use as NAME, mapped to their owner.

    A backend NAME names a top-level table, so it may not be an envelope key or one of its
    own kind's core properties. Other kinds' properties do not matter: a spec has one kind.
    """
    owners = {key: "the spec envelope" for key in ENVELOPE_KEYS + LEGACY_KEYS}
    kind = _discovered()[0].get(kind_name)
    if kind is not None:
        for key in kind.PROPERTIES:
            owners.setdefault(key, f"kind {kind.NAME!r}")
    return owners


def _is_findings(value: Any) -> bool:
    from ..validate import Finding

    return isinstance(value, list) and all(isinstance(f, Finding) for f in value)


# What each kind method must return; anything else is a plugin bug reported as exit 2.
RETURN_CHECKS = {"semantic": (_is_findings, "a list of prov.validate.Finding")}


def call_kind(kind: Kind, method: str, *args: Any) -> Any:
    """Run a kind method through ``prov.plugin.guarded_call`` (exit 2 naming the kind on failure)."""
    check, expected = RETURN_CHECKS.get(method, (lambda value: True, ""))
    return plugins.guarded_call(f"kind {short_repr(getattr(kind, 'NAME', '?'))}", kind, method, args, check, expected)
