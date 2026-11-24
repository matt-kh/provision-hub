"""Items and specs: refs, the envelope check, and full spec validation.

An item is ``<root>/<kind>/<name>/`` holding ``spec.toml``; its ref ``<kind>/<name>`` is the
path, the CLI argument and the value of a ``[refs]`` entry. Nothing here is specific to a
kind or a backend: both are looked up (``prov.kinds``, ``prov.registry``).

References are resolved only against a root: ``check_item`` / ``load_item`` (and
``check_spec`` / ``load_spec`` given ``root=``) resolve the spec's ``[refs]`` through
``prov.refs`` and hand the backend a populated ``Context.refs``. Without a root, ``[refs]``
is checked by the schema only, and the backend's ``check`` is skipped when the spec has
references (its ``ctx.refs`` would be empty).
"""

from __future__ import annotations

import re
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import kinds
from .kinds.common import DEFS
from .registry import Context, call_plugin, for_kind, lookup
from .render import safe_item_dir
from .state import report_findings
from .schema import KIND_PATTERN, LEGACY_KEYS, LEGACY_TABLES, NAME_PATTERN, SPEC_FILE, ConfigError, api_versions, read_toml, spec_schema
from .text import show, short_repr
from .validate import ERROR, WARNING, Finding, apply_defaults, has_errors, schema_errors


def _full(pattern: str, value: str) -> bool:
    return re.fullmatch(pattern.strip("^$"), value) is not None


def split_ref(ref: str) -> Tuple[str, str]:
    """``"<kind>/<name>"`` -> (kind, name) by syntax alone; ConfigError for anything else.

    The kind must have a kind name's shape (bare, or dotted like ``registry.example.org``)
    and the name be one DNS label, so a ref can never escape the root (no ``..``, no extra
    ``/``, no control characters). Whether the kind is installed is not checked.
    """
    kind, sep, name = ref.partition("/")
    if not sep or not _full(KIND_PATTERN, kind):
        raise ConfigError(f"invalid ref {short_repr(ref)}: expected <kind>/<name>, for example os-install/demo")
    if not _full(NAME_PATTERN, name):
        raise ConfigError(f"invalid name {short_repr(name)} in ref {short_repr(ref)} (lowercase letters, digits, '-'; one DNS label)")
    return kind, name


def parse_ref(ref: str) -> Tuple[str, str]:
    """``"<kind>/<name>"`` -> (kind, name) of an installed kind; ConfigError for anything else (``split_ref``)."""
    kind, name = split_ref(ref)
    kinds.get(kind)
    return kind, name


def resolve_backend(raw: Mapping[str, Any], kind_name: str, backends: Mapping[str, Any]) -> Any:
    """Check the envelope of a parsed spec and return its backend; ConfigError (exit 2) otherwise.

    Order: legacy keys (``schema_version``, ``distro``; an ``[ubuntu]`` table without
    ``api_version``), ``api_version``, ``kind`` (must equal the directory kind), ``backend``
    (must be registered and render this kind).
    """
    kind = kinds.get(kind_name)
    supported = ", ".join(api_versions(kind))
    legacy = [k for k in LEGACY_KEYS if k in raw]
    if legacy and "api_version" in raw:  # a half-migrated file
        raise ConfigError(f"`{legacy[0]}` is a pre-v1alpha1 key; remove it (see CHANGELOG migration)")
    if not legacy and "api_version" not in raw:
        legacy = [k for k in LEGACY_TABLES if k in raw]
    if legacy:
        raise ConfigError(
            f"{legacy[0]}: this file predates prov/v1alpha1; see CHANGELOG migration "
            f"(use api_version = {api_versions(kind)[-1]!r}, kind and backend; tables are named after the backend)"
        )
    if "api_version" not in raw:
        raise ConfigError(f"api_version is required (this prov supports: {supported})")
    version = raw["api_version"]
    if not isinstance(version, str) or version not in api_versions(kind):
        raise ConfigError(
            f"unsupported api_version {short_repr(version)} for kind {kind_name!r} (this prov supports: {supported}); "
            "use a prov release that supports it or migrate the file"
        )
    if "kind" not in raw:
        raise ConfigError(f"kind is required (the directory says {kind_name!r})")
    if raw["kind"] != kind_name:
        raise ConfigError(f"kind {short_repr(raw['kind'])} does not match the directory kind {kind_name!r}")
    if "backend" not in raw:
        known = ", ".join(for_kind(backends, kind_name)) or "none"
        raise ConfigError(f"backend is required (registered for {kind_name}: {known})")
    return lookup(backends, kind_name, raw["backend"])


def name_findings(kind: Any, spec: Mapping[str, Any], name: str) -> List[Finding]:
    """Warn when the kind's NAME_KEY value differs from the item directory name."""
    key = getattr(kind, "NAME_KEY", None)
    if key and key in spec and spec[key] != name:
        return [Finding(WARNING, f"{key} {short_repr(spec[key])} differs from the directory name {name!r}", key)]
    return []


NO_REFS: Mapping[str, Any] = MappingProxyType({})

# resolver(kind_name, name, spec) -> (read-only alias mapping, findings); see prov.refs.Resolution.
Resolver = Callable[[str, str, Dict[str, Any]], Tuple[Mapping[str, Any], List[Finding]]]


def check_raw(
    raw: Mapping[str, Any], kind_name: str, name: str, backends: Mapping[str, Any], resolver: Optional[Resolver]
) -> Tuple[Optional[Dict[str, Any]], List[Finding], Mapping[str, Any]]:
    """Validate a parsed spec.toml. Returns (spec with defaults or None, findings, resolved refs).

    Envelope errors raise ConfigError; schema, semantic, reference and backend ``check``
    errors are findings. With a ``resolver`` the spec's references are resolved and the
    backend's ``check`` gets them in ``ctx.refs``; it is skipped when they did not resolve.
    Without one, a spec with references skips the backend's ``check`` (``ctx.refs`` would be
    empty). The refs are ``NO_REFS`` unless the spec is valid.
    """
    backend = resolve_backend(raw, kind_name, backends)
    kind = kinds.get(kind_name)
    schema = spec_schema(kind, for_kind(backends, kind_name), DEFS)
    findings = [Finding(ERROR, msg) for msg in schema_errors(raw, schema)]
    if findings:
        return None, findings, NO_REFS
    spec = apply_defaults(raw, schema)
    findings = list(kinds.call_kind(kind, "semantic", spec)) + report_findings(spec, backend)
    refs, resolved = NO_REFS, not spec.get("refs")
    if resolver is not None:
        refs, ref_findings = resolver(kind_name, name, spec)
        findings += ref_findings
        resolved = not has_errors(ref_findings)
    if resolved and hasattr(backend, "check"):
        findings += call_plugin(backend, "check", spec, Context(name, refs))
    findings += name_findings(kind, spec, name)
    if has_errors(findings):
        return None, findings, NO_REFS
    return spec, findings, refs


def _resolver(root: Optional[Path], backends: Mapping[str, Any]) -> Optional[Resolver]:
    if root is None:
        return None
    from .refs import Resolution  # prov.refs reads target items through this module

    return Resolution(root, backends)


def check_spec(
    raw: Mapping[str, Any], kind_name: str, name: str, backends: Mapping[str, Any], root: Optional[Path] = None
) -> Tuple[Optional[Dict[str, Any]], List[Finding]]:
    """Validate a parsed spec.toml. Returns (spec with defaults or None, findings).

    Envelope errors raise ConfigError; schema and semantic errors are findings. With
    ``root`` the spec's references are resolved against it (their errors are findings at
    ``refs.<alias>``); without, ``[refs]`` is checked by the schema only (``check_raw``).
    """
    spec, findings, _ = check_raw(raw, kind_name, name, backends, _resolver(root, backends))
    return spec, findings


def _invalid(path: Path, findings: List[Finding]) -> ConfigError:
    lines = "\n".join(f"  {f}" for f in findings if f.level == ERROR)
    return ConfigError(f"{show(str(path))}: invalid spec:\n{lines}")


def load_spec(
    path: Path, kind_name: str, name: str, backends: Mapping[str, Any], root: Optional[Path] = None
) -> Tuple[Dict[str, Any], List[Finding]]:
    """Read and validate spec.toml; raise ConfigError listing every error (``root``: see ``check_spec``)."""
    raw = read_toml(path)
    try:
        spec, findings = check_spec(raw, kind_name, name, backends, root=root)
    except ConfigError as exc:
        raise ConfigError(f"{show(str(path))}: {exc}") from None
    if spec is None:
        raise _invalid(path, findings)
    return spec, findings


def check_item(
    root: Path, kind_name: str, name: str, backends: Mapping[str, Any]
) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], List[Finding], Context]:
    """Read ``<root>/<kind>/<name>/spec.toml`` and check it with its references resolved.

    Returns (raw spec, spec with defaults or None, findings, Context with the resolved
    refs). Envelope errors, unreadable files and plugin failures raise ConfigError (exit 2).
    """
    path = safe_item_dir(root, kind_name, name) / SPEC_FILE
    raw = read_toml(path)
    try:
        spec, findings, refs = check_raw(raw, kind_name, name, backends, _resolver(root, backends))
    except ConfigError as exc:
        raise ConfigError(f"{show(str(path))}: {exc}") from None
    return raw, spec, findings, Context(name, refs)


def load_item(root: Path, kind_name: str, name: str, backends: Mapping[str, Any]) -> Tuple[Any, Dict[str, Any], List[Finding], Context]:
    """(backend, spec, findings, ctx) of a valid item, references resolved; ConfigError (exit 2) otherwise."""
    _, spec, findings, ctx = check_item(root, kind_name, name, backends)
    if spec is None:
        raise _invalid(safe_item_dir(root, kind_name, name) / SPEC_FILE, findings)
    return backends[spec["backend"]], spec, findings, ctx
