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
``REFS``        references to other items: ``{alias: {"kind", "required"?, "description"?}}``
                (``{}`` for none); the spec's ``[refs]`` table, resolved into ``ctx.refs``
``NAME_KEY``    optional: the core key that should equal the item name (warning otherwise)
``SCHEMA_URL``  optional: https base URL (ending in ``/``) of the composed schema's ``$id``
``OUTPUTS``     optional, with ``outputs``: object-schema fragment of what an item exports
``outputs``     optional, with ``OUTPUTS``: ``outputs(spec) -> dict``, a pure function of the
                validated spec with defaults (``item_outputs`` calls it)
``FIXTURES``    optional: conformance fixtures ``{name: {"spec", "refs"?, "expect"?}}``
                that ``prov conformance <backend>`` renders through each backend

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
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set, Tuple

from .. import plugin as plugins
from ..schema import ALIAS_PATTERN, ENVELOPE_KEYS, ID_PATTERN, KIND_MAX_LENGTH, KIND_PATTERN, LEGACY_KEYS, NAME_PATTERN, OPTIONAL_ENVELOPE_KEYS, ConfigError
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
    clash = sorted(set(kind.PROPERTIES) & set(ENVELOPE_KEYS + OPTIONAL_ENVELOPE_KEYS + LEGACY_KEYS))
    if clash:
        raise ConfigError(f"{who}: core properties may not redefine envelope keys {clash}")
    _check_refs(kind.REFS, who)
    if not callable(kind.semantic):
        raise ConfigError(f"{who}: semantic is not callable")
    name_key = getattr(kind, "NAME_KEY", None)
    if name_key is not None and name_key not in kind.PROPERTIES:
        raise ConfigError(f"{who}: NAME_KEY {short_repr(name_key)} is not a core property")
    url = getattr(kind, "SCHEMA_URL", None)
    if url is not None and not (isinstance(url, str) and re.fullmatch("https://[!-~]+/", url)):
        raise ConfigError(f"{who}: SCHEMA_URL must be an https base URL ending in '/' (printable ASCII, no spaces); prov appends <NAME>.<version>.schema.json")
    problem = _json_problem(kind.PROPERTIES, "", strict=False, max_depth=SCHEMA_MAX_DEPTH)
    if problem:
        raise ConfigError(f"{who}: PROPERTIES is not plain JSON data: {esc(problem)}")
    try:
        check_schema_fragment({"type": "object", "properties": kind.PROPERTIES}, f"<kind {esc(str(name))}>", defs=DEFS, allow_defs=False)
    except ValueError as exc:
        raise ConfigError(f"{who}: invalid PROPERTIES: {esc(str(exc))}") from None
    _check_outputs(kind, who)
    _check_fixtures(kind, who)


REF_FIELDS = ("description", "kind", "required")
REF_DEFAULTS = {"required": False, "description": ""}


def _check_refs(declared: Mapping[Any, Any], who: str) -> None:
    """``REFS``: alias -> ``{"kind": <kind name>, "required": bool (false), "description": str ("")}``.

    ``kind`` is required, the others optional, any other key refused. The target kind need
    not be installed now: a missing one is a finding when a spec's reference is resolved
    (``prov.refs``).
    """
    for alias, decl in declared.items():
        if not (type(alias) is str and _full(ALIAS_PATTERN, alias)):
            raise ConfigError(f"{who}: REFS alias {short_repr(alias)} must match {ALIAS_PATTERN}")
        if not (type(decl) is dict and all(type(key) is str for key in decl)):
            raise ConfigError(f"{who}: REFS[{alias!r}] must be a dict with the key kind (and optionally required, description)")
        unknown = sorted(set(decl) - set(REF_FIELDS))
        if unknown or "kind" not in decl:
            what = f"unknown key(s) {', '.join(short_repr(k) for k in unknown)}" if unknown else "no kind"
            raise ConfigError(f"{who}: REFS[{alias!r}] has {what}; keys: kind (required), required, description")
        decl = {**REF_DEFAULTS, **decl}
        target = decl["kind"]
        if not (type(target) is str and len(target) <= KIND_MAX_LENGTH and _full(KIND_PATTERN, target)):
            raise ConfigError(f"{who}: REFS[{alias!r}] kind {short_repr(target)} is not a kind name (an id, or dotted like 'registry.example.org')")
        if type(decl["required"]) is not bool:
            raise ConfigError(f"{who}: REFS[{alias!r}] required must be true or false")
        if type(decl["description"]) is not str:
            raise ConfigError(f"{who}: REFS[{alias!r}] description must be a str")


# The secret-name lint over output names (docs/kinds.md "Outputs"). A lint, not a guarantee,
# and not part of PLUGIN_API: the lists may grow between releases (with a CHANGELOG entry).
# A name is split into words (camelCase, then _ - . and spaces), lowercased.
SECRET_WORDS = frozenset({"password", "passwd", "passphrase", "pwd", "secret", "secrets", "credential", "credentials", "kubeconfig"})
SECRET_PAIRS = frozenset({("private", "key"), ("api", "key"), ("ssh", "key"), ("tls", "key")})
TOKEN_WORDS = frozenset({"token", "tokens"})  # only as the last word, and not after a counter word
COUNTER_WORDS = frozenset({"max", "num"})
REFERENCE_WORDS = ("path", "name", "ref", "file")  # a last word that makes the name a reference


def name_words(name: str) -> List[str]:
    """``"sshPrivateKey_path"`` -> ``["ssh", "private", "key", "path"]``; ``"APIKey"`` -> ``["api", "key"]``."""
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name)
    return [w.lower() for w in re.split(r"[_\-.\s]+", spaced) if w]


def secret_looking(name: str) -> bool:
    """True for an output name that looks like it carries a secret value (``admin_password``,
    ``join_token``, ``sshPrivateKey``), False for a reference to one (``admin_password_path``,
    ``tls_secret_name``) and for look-alikes (``max_tokens``, ``tokenizer``, ``secretary``)."""
    words = name_words(name)
    if not words or words[-1] in REFERENCE_WORDS:
        return False
    if any(w in SECRET_WORDS for w in words) or any(pair in SECRET_PAIRS for pair in zip(words, words[1:])):
        return True
    return words[-1] in TOKEN_WORDS and not (len(words) > 1 and words[-2] in COUNTER_WORDS)


def _secret_message(name: str) -> str:
    return (
        f"{short_repr(name)} looks like a secret value; outputs never carry secrets: export where the human keeps it, "
        f"as a name ending in {', '.join('_' + w for w in REFERENCE_WORDS)} (e.g. {short_repr(name + '_path')})"
    )


def _property_names(schema: Any, seen: Set[str]) -> Iterator[str]:
    """Every property name declared in a (checked) schema fragment, following ``$ref`` into DEFS once."""
    if not isinstance(schema, dict):
        return
    ref = schema.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/") and ref not in seen:
        seen.add(ref)
        yield from _property_names(DEFS.get(ref[len("#/$defs/"):]), seen)
    for key in schema.get("required", []):
        yield key
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
    problem = _json_problem(schema, "", strict=False, max_depth=SCHEMA_MAX_DEPTH)
    if problem:
        raise ConfigError(f"{who}: OUTPUTS is not plain JSON data: {esc(problem)}")
    if not (isinstance(schema, dict) and schema.get("type") == "object" and isinstance(schema.get("properties"), dict) and schema.get("additionalProperties") is False):
        raise ConfigError(f"{who}: OUTPUTS must be an object schema with properties and \"additionalProperties\": false")
    try:
        check_schema_fragment(schema, f"<outputs {esc(str(kind.NAME))}>", defs=DEFS, allow_defs=False)
    except ValueError as exc:
        raise ConfigError(f"{who}: invalid OUTPUTS: {esc(str(exc))}") from None
    for prop in _property_names(schema, set()):
        if secret_looking(prop):
            raise ConfigError(f"{who}: OUTPUTS property {_secret_message(prop)}")


FIXTURE_FIELDS = ("expect", "refs", "spec")
EXPECT_FIELDS = ("absent", "contains", "outputs", "valid")


def fixtures(kind: Kind) -> Dict[str, Dict[str, Any]]:
    """A (checked) kind's conformance ``FIXTURES``, ordered by name, ``expect`` defaults filled in.

    Each value is ``{"spec": {...}, "refs": {alias: {outputs}}, "expect": {"valid": bool, ...}}``
    (``refs`` ``{}`` without; ``outputs``, ``contains`` and ``absent`` only when given). A
    deep copy: the caller may change it.
    """
    out: Dict[str, Dict[str, Any]] = {}
    for name, fixture in sorted(getattr(kind, "FIXTURES", {}).items()):
        expect = {"valid": True, **fixture.get("expect", {})}
        out[name] = copy.deepcopy({"spec": fixture["spec"], "refs": fixture.get("refs", {}), "expect": expect})
    return out


def _check_fixtures(kind: Kind, who: str) -> None:
    """``FIXTURES`` (optional): ``{name: {"spec": {core keys}, "refs"?: {alias: {outputs}}, "expect"?: {...}}}``.

    Checked here: names (item names), plain JSON data, no envelope keys in ``spec``, ``refs``
    aliases declared in ``REFS``, the ``expect`` keys and their types, and ``expect.outputs``
    against the kind's own ``OUTPUTS``. Whether ``spec`` is valid, and whether a ``refs``
    literal matches its target kind's ``OUTPUTS`` (that kind may load later, or not at all),
    is for ``prov conformance`` (``prov.conformance``).
    """
    from ..validate import schema_errors

    if not hasattr(kind, "FIXTURES"):
        return
    table = kind.FIXTURES
    if type(table) is not dict:
        raise ConfigError(f"{who}: FIXTURES must be a dict of fixture name -> {{spec, refs, expect}}")
    reserved = set(ENVELOPE_KEYS + OPTIONAL_ENVELOPE_KEYS + LEGACY_KEYS)
    for name, fixture in table.items():
        if not (type(name) is str and _full(NAME_PATTERN, name)):
            raise ConfigError(f"{who}: FIXTURES name {short_repr(name)} must match {NAME_PATTERN} (an item name)")
        at = f"{who}: FIXTURES[{name!r}]"
        if not (type(fixture) is dict and all(type(k) is str for k in fixture)):
            raise ConfigError(f"{at} must be a dict with the key spec (and optionally refs, expect)")
        unknown = sorted(set(fixture) - set(FIXTURE_FIELDS))
        if unknown or "spec" not in fixture:
            what = f"unknown key(s) {', '.join(short_repr(k) for k in unknown)}" if unknown else "no spec"
            raise ConfigError(f"{at} has {what}; keys: spec (required), refs, expect")
        spec = fixture["spec"]
        if type(spec) is not dict:
            raise ConfigError(f"{at}: spec must be a dict of core keys")
        problem = _json_problem(spec, "spec", strict=False, max_depth=SCHEMA_MAX_DEPTH)
        if problem:
            raise ConfigError(f"{at} is not plain JSON data: {esc(problem)}")
        clash = sorted(set(spec) & reserved)
        if clash:
            raise ConfigError(f"{at}: spec may not hold envelope keys {clash}; prov conformance composes the envelope (and refs from the fixture's refs)")
        refs = fixture.get("refs", {})
        if type(refs) is not dict:
            raise ConfigError(f"{at}: refs must be a dict of alias -> literal outputs")
        for alias, outputs in refs.items():
            if alias not in kind.REFS:
                raise ConfigError(f"{at}: refs alias {short_repr(alias)} is not declared in REFS (declared: {', '.join(sorted(kind.REFS)) or 'none'})")
            if type(outputs) is not dict:
                raise ConfigError(f"{at}: refs[{alias!r}] must be a dict: the target's outputs")
            problem = _json_problem(outputs, f"refs.{alias}")
            if problem:
                raise ConfigError(f"{at} is not plain JSON outputs: {esc(problem)}")
        expect = fixture.get("expect", {})
        if not (type(expect) is dict and all(type(k) is str for k in expect)):
            raise ConfigError(f"{at}: expect must be a dict with the keys valid, outputs, contains, absent (all optional)")
        unknown = sorted(set(expect) - set(EXPECT_FIELDS))
        if unknown:
            raise ConfigError(f"{at}: expect has unknown key(s) {', '.join(short_repr(k) for k in unknown)}; keys: valid, outputs, contains, absent")
        valid = expect.get("valid", True)
        if type(valid) is not bool:
            raise ConfigError(f"{at}: expect.valid must be true or false")
        if not valid and set(expect) - {"valid"}:
            raise ConfigError(f"{at}: a fixture expected to be invalid stops at the spec check; drop expect.outputs, contains and absent")
        for key in ("contains", "absent"):
            texts = expect.get(key, [])
            if not (type(texts) is list and all(type(t) is str and t for t in texts)):
                raise ConfigError(f"{at}: expect.{key} must be a list of non-empty strings")
        if "outputs" in expect:
            outputs = expect["outputs"]
            problem = _json_problem(outputs, "expect.outputs") if type(outputs) is dict else "expect.outputs: must be a dict"
            if problem:
                raise ConfigError(f"{at} is not plain JSON outputs: {esc(problem)}")
            schema = getattr(kind, "OUTPUTS", None)
            errors = schema_errors(outputs, {**schema, "$defs": DEFS}, None, "expect.outputs") if schema is not None else ([] if outputs == {} else ["the kind has no OUTPUTS, so its outputs are {}"])
            if errors:
                raise ConfigError(f"{at}: expect.outputs does not match OUTPUTS: {esc('; '.join(errors))}")


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
    owners = {key: "the spec envelope" for key in ENVELOPE_KEYS + OPTIONAL_ENVELOPE_KEYS + LEGACY_KEYS}
    kind = _discovered()[0].get(kind_name)
    if kind is not None:
        for key in kind.PROPERTIES:
            owners.setdefault(key, f"kind {kind.NAME!r}")
    return owners


def declared_refs(kind: Kind) -> Dict[str, Dict[str, Any]]:
    """A (checked) kind's ``REFS`` with the defaults filled in: alias -> ``{"kind", "required", "description"}``."""
    return {alias: {**REF_DEFAULTS, **decl} for alias, decl in sorted(kind.REFS.items())}


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
# The same guard for a kind's schemas (PROPERTIES, OUTPUTS), which nest two levels per property.
SCHEMA_MAX_DEPTH = 128
INT_MIN, INT_MAX = -(2**63), 2**63 - 1  # outputs integers are 64-bit, as most JSON readers expect

_KEY = re.compile("[A-Za-z_][A-Za-z0-9_-]*")


def _key_path(path: str, key: str) -> str:
    """``a.b`` for a plain key, ``a['x y']`` for any other (a repr, so it is one printable line)."""
    if _KEY.fullmatch(key):
        return f"{path}.{key}" if path else key
    return f"{path}[{short_repr(key)}]"


def _json_problem(value: Any, path: str, depth: int = 0, strict: bool = True, max_depth: int = OUTPUTS_MAX_DEPTH) -> Optional[str]:
    """Why ``value`` is not plain JSON data (None when it is): exact dict, list, str, int, finite float, bool, None.

    ``strict`` (outputs) also refuses control characters in strings and keys, integers
    outside 64 bits and keys that look like secret values; schemas are checked without it
    (descriptions are plugin text, escaped by JSON). Unknown objects are named by their
    type only, never ``repr``-ed: a plugin's ``__repr__`` could raise.
    """
    from ..validate import CONTROL

    where = path or "<root>"
    if depth > max_depth:
        return f"{where}: nested deeper than {max_depth} levels (or cyclic)"
    kind = type(value)
    if value is None or kind is bool:
        return None
    if kind is str:
        return f"{where}: control characters (newline, tab, ...) are not allowed" if strict and CONTROL.search(value) else None
    if kind is int:
        return f"{where}: integer out of range (64-bit)" if strict and not INT_MIN <= value <= INT_MAX else None
    if kind is float:
        return None if math.isfinite(value) else f"{where}: {short_repr(value)} is not a finite number"
    if kind is list:
        for n, item in enumerate(value):
            problem = _json_problem(item, f"{path}[{n}]", depth + 1, strict, max_depth)
            if problem:
                return problem
        return None
    if kind is dict:
        for key in value:
            if type(key) is not str:
                shown = short_repr(key) if type(key) is int else f"of type {esc(type(key).__name__)}"
                return f"{where}: key {shown} is not a string"
            if strict and CONTROL.search(key):
                return f"{where}: control characters in key {short_repr(key)}"
            if strict and secret_looking(key):
                return f"{where}: key {_secret_message(key)}"
            problem = _json_problem(value[key], _key_path(path, key), depth + 1, strict, max_depth)
            if problem:
                return problem
        return None
    return f"{where}: {esc(kind.__name__)} is not a JSON value (str, int, float, bool, None, list, dict)"


json_problem = _json_problem  # for core modules (registry: backend FIXTURES tables)


def neutral_spec(kind: Kind, spec: Mapping[str, Any]) -> Dict[str, Any]:
    """A deep copy of the tool-neutral part of ``spec``: ``api_version``, ``kind`` and the core keys.

    The backend's name and table (and anything else) are left out, so a kind cannot make
    its outputs depend on which backend renders the item.
    """
    keep = {"api_version", "kind"} | set(kind.PROPERTIES)  # not the backend name either
    return copy.deepcopy({key: value for key, value in spec.items() if key in keep})


def item_outputs(kind: Kind, spec: Mapping[str, Any]) -> Dict[str, Any]:
    """What an item of ``kind`` exports: ``kind.outputs(spec)`` checked against ``kind.OUTPUTS``.

    ``spec`` is the validated spec with defaults (``prov.spec.check_spec``); the kind gets a
    deep copy of its tool-neutral part only (``neutral_spec``: ``api_version``, ``kind`` and
    the core keys, never the backend's name or table), so outputs mean the same whichever backend renders
    the item, and it cannot change the caller's spec. A kind without OUTPUTS exports ``{}``. The
    result must be plain JSON data valid against OUTPUTS: strings and keys without control
    characters (like spec values), 64-bit integers, no key that looks like a secret value
    (``secret_looking``, at any depth); anything else, or an exception in ``outputs``, is a
    ConfigError (exit 2) naming the kind, with escaped text. Returns a deep copy.
    """
    from ..validate import schema_errors

    if not hasattr(kind, "OUTPUTS"):
        return {}
    who = f"kind {short_repr(getattr(kind, 'NAME', '?'))}"
    result = call_kind(kind, "outputs", neutral_spec(kind, spec))
    problem = _json_problem(result, "")
    if problem:
        raise ConfigError(f"{who} failed in outputs: {esc(problem)}")
    errors = schema_errors(result, {**kind.OUTPUTS, "$defs": DEFS})
    if errors:
        raise ConfigError(f"{who} failed in outputs: does not match OUTPUTS: {esc('; '.join(errors))}")
    return copy.deepcopy(result)
