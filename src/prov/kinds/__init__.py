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
``OUTPUTS``     optional, with ``outputs``: object-schema fragment of what an item exports
``outputs``     optional, with ``OUTPUTS``: ``outputs(spec) -> dict``, a pure function of the
                validated spec with defaults (``item_outputs`` calls it)

The api group (``prov/v1alpha1``, ``example.org/v1alpha1``) follows from NAME
(``prov.schema.kind_group``); the group ``prov`` belongs to provision-hub's bare kinds, and
a dotted kind's group always contains a dot, so it can never be ``prov``.
"""

from __future__ import annotations

import copy
import math
import re
from importlib.metadata import entry_points
from types import ModuleType
from typing import Any, Dict, Iterator, Mapping, Optional, Set, Tuple

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
    _check_outputs(kind, who)


# Substrings of an OUTPUTS property name (lowercased) that suggest a secret value, and the
# suffixes that make it a reference to one instead. A lint, not a guarantee: see docs/kinds.md.
SECRET_WORDS = ("password", "passwd", "token", "secret", "private_key", "api_key", "credential")
REFERENCE_SUFFIXES = ("_path", "_name", "_ref")


def secret_looking(name: str) -> bool:
    """True for an output name that looks like it carries a secret value (``admin_password``),
    False for a reference to one (``admin_password_path``, ``token_secret_name``)."""
    lowered = name.lower()
    return any(word in lowered for word in SECRET_WORDS) and not lowered.endswith(REFERENCE_SUFFIXES)


def _property_names(schema: Any, seen: Set[str]) -> Iterator[str]:
    """Every property name declared in a (checked) schema fragment, following ``$ref`` into DEFS once."""
    if not isinstance(schema, dict):
        return
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/") and ref not in seen:
        seen.add(ref)
        yield from _property_names(DEFS.get(ref[len("#/$defs/"):]), seen)
    for key, child in schema.get("properties", {}).items():
        yield key
        yield from _property_names(child, seen)
    for key in ("items", "if", "then", "additionalProperties"):
        yield from _property_names(schema.get(key), seen)
    for child in schema.get("allOf", []):
        yield from _property_names(child, seen)


def _check_outputs(kind: Kind, who: str) -> None:
    """``OUTPUTS`` and ``outputs`` come together; OUTPUTS is an object fragment naming no secret value."""
    from ..validate import check_schema_fragment

    has_schema, has_fn = hasattr(kind, "OUTPUTS"), hasattr(kind, "outputs")
    if has_schema != has_fn:
        raise ConfigError(f"{who}: OUTPUTS and outputs come together; it defines {'OUTPUTS' if has_schema else 'outputs'} only")
    if not has_schema:
        return
    if not callable(kind.outputs):
        raise ConfigError(f"{who}: outputs is not callable")
    schema = kind.OUTPUTS
    if not (isinstance(schema, dict) and schema.get("type") == "object" and isinstance(schema.get("properties"), dict) and schema.get("additionalProperties") is False):
        raise ConfigError(f"{who}: OUTPUTS must be an object schema with properties and \"additionalProperties\": false")
    try:
        check_schema_fragment(schema, f"<outputs {esc(str(kind.NAME))}>", defs=DEFS, allow_defs=False)
    except ValueError as exc:
        raise ConfigError(f"{who}: invalid OUTPUTS: {esc(str(exc))}") from None
    for prop in _property_names(schema, set()):
        if secret_looking(prop):
            raise ConfigError(
                f"{who}: OUTPUTS property {short_repr(prop)} looks like a secret value; outputs never carry secrets: "
                f"export where the human keeps it, as a name ending in {', '.join(REFERENCE_SUFFIXES)} (e.g. {short_repr(prop + '_path')})"
            )


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


def builtin_names() -> frozenset:
    """NAMEs of the discovered kinds that the provision-hub distribution registers (``prov kinds --json``)."""
    return _discovered()[1]


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
RETURN_CHECKS = {
    "semantic": (_is_findings, "a list of prov.validate.Finding"),
    "outputs": (lambda value: type(value) is dict, "a dict"),
}


def call_kind(kind: Kind, method: str, *args: Any) -> Any:
    """Run a kind method through ``prov.plugin.guarded_call`` (exit 2 naming the kind on failure)."""
    check, expected = RETURN_CHECKS.get(method, (lambda value: True, ""))
    return plugins.guarded_call(f"kind {short_repr(getattr(kind, 'NAME', '?'))}", kind, method, args, check, expected)


# How deep an outputs value may nest; deeper (or cyclic) values are refused, never recursed into.
OUTPUTS_MAX_DEPTH = 32


def _json_problem(value: Any, path: str, depth: int = 0) -> Optional[str]:
    """Why ``value`` is not plain JSON data (None when it is): exact dict, list, str, int, finite float, bool, None."""
    if depth > OUTPUTS_MAX_DEPTH:
        return f"{path}: nested deeper than {OUTPUTS_MAX_DEPTH} levels (or cyclic)"
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return None
    if kind is float:
        return None if math.isfinite(value) else f"{path}: {short_repr(value)} is not a finite number"
    if kind is list:
        for n, item in enumerate(value):
            problem = _json_problem(item, f"{path}[{n}]", depth + 1)
            if problem:
                return problem
        return None
    if kind is dict:
        for key, item in value.items():
            if type(key) is not str:
                return f"{path}: key {short_repr(key)} is not a string"
            problem = _json_problem(item, f"{path}.{key}" if path else key, depth + 1)
            if problem:
                return problem
        return None
    return f"{path or '<root>'}: {esc(kind.__name__)} is not a JSON value (str, int, float, bool, None, list, dict)"


def item_outputs(kind: Kind, spec: Mapping[str, Any]) -> Dict[str, Any]:
    """What an item of ``kind`` exports: ``kind.outputs(spec)`` checked against ``kind.OUTPUTS``.

    ``spec`` is the validated spec with defaults (``prov.spec.check_spec``); the kind gets a
    deep copy, so it cannot change the caller's. A kind without OUTPUTS exports ``{}``. The
    result must be plain JSON data valid against OUTPUTS (its strings, like spec values,
    without control characters); anything else, or an exception in ``outputs``, is a
    ConfigError (exit 2) naming the kind, with escaped text. Returns a deep copy.
    """
    from ..validate import schema_errors

    if not hasattr(kind, "OUTPUTS"):
        return {}
    who = f"kind {short_repr(getattr(kind, 'NAME', '?'))}"
    result = call_kind(kind, "outputs", copy.deepcopy(dict(spec)))
    problem = _json_problem(result, "")
    if problem:
        raise ConfigError(f"{who} failed in outputs: {esc(problem)}")
    errors = schema_errors(result, {**kind.OUTPUTS, "$defs": DEFS})
    if errors:
        raise ConfigError(f"{who} failed in outputs: does not match OUTPUTS: {esc('; '.join(errors))}")
    return copy.deepcopy(result)
