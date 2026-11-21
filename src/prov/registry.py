"""Backend registry, discovered through the ``prov.backends`` entry-point group.

A backend is a module (or any object) exposing the contract in ``docs/adding-a-backend.md``:
``NAME``, ``KIND``, ``SCHEMA``, ``render(spec, ctx)``, ``validate(rendered_dir)``,
``plan(spec, ctx)``, and optionally ``PLUGIN_API`` (default 1), ``check(spec, ctx)``,
``starter(name)``, ``PRESERVE``, ``REFS``, ``artifacts(spec)`` and ``build(spec, ctx)`` with
``BUILD_TOOLS`` (build and references are reserved: checked here, not called yet).

prov's own backends register the same way as third-party ones; ``builtin()`` is the set the
provision-hub distribution registers, and core never imports a backend module.

Backends are looked up by (KIND, NAME); NAME is globally unique, so it is the dict key.
Every call into a backend goes through ``call_plugin``, which turns exceptions and wrongly
typed results into a ConfigError (exit 2) naming the backend.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import PurePosixPath
from types import MappingProxyType, ModuleType
from typing import Any, Callable, Dict, Mapping, Tuple

from . import kinds
from . import plugin as plugins
from .kinds.common import DEFS
from .render import check_paths
from .schema import ID_PATTERN, ConfigError
from .text import esc, short_repr
from .validate import Finding, check_schema_fragment

ENTRY_POINT_GROUP = "prov.backends"
REQUIRED_MEMBERS = ("NAME", "KIND", "SCHEMA", "render", "validate", "plan")
OPTIONAL_CALLABLES = ("check", "starter", "artifacts", "build")

Backend = Any  # a module or object implementing the contract


@dataclass(frozen=True)
class Context:
    """What a backend gets besides the spec: the item name and resolved references (none yet)."""

    name: str
    refs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))


def check_contract(plugin: Backend, origin: str) -> None:
    """Raise ConfigError when ``plugin`` does not implement the backend contract."""
    missing = [m for m in REQUIRED_MEMBERS if not hasattr(plugin, m)]
    if missing:
        raise ConfigError(f"backend plugin {origin} lacks required member(s): {', '.join(missing)}")
    for fn in ("render", "validate", "plan") + tuple(m for m in OPTIONAL_CALLABLES if hasattr(plugin, m)):
        if not callable(getattr(plugin, fn)):
            raise ConfigError(f"backend plugin {origin}: {fn} is not callable")
    if not isinstance(plugin.NAME, str) or not isinstance(plugin.KIND, str) or not isinstance(plugin.SCHEMA, dict):
        raise ConfigError(f"backend plugin {origin}: NAME and KIND must be str and SCHEMA a dict")
    if not re.fullmatch(ID_PATTERN.strip("^$"), plugin.NAME):
        raise ConfigError(f"backend plugin {origin}: NAME {short_repr(plugin.NAME)} must match {ID_PATTERN}")
    plugins.check_api(plugin, f"backend plugin {origin}", required=False)
    known = kinds.discover()
    if plugin.KIND not in known:
        raise ConfigError(f"backend plugin {origin}: unknown KIND {short_repr(plugin.KIND)} (known kinds: {kinds.unknown_hint(known)})")
    owner = kinds.core_keys(plugin.KIND).get(plugin.NAME)
    if owner is not None:
        raise ConfigError(f"backend plugin {origin}: NAME {short_repr(plugin.NAME)} collides with a core property of {owner}")
    if hasattr(plugin, "PRESERVE"):
        keep = plugin.PRESERVE
        if not (isinstance(keep, (tuple, list)) and all(isinstance(p, str) for p in keep)):
            raise ConfigError(f"backend plugin {origin}: PRESERVE must be a list of relative paths")
        try:
            check_paths(dict.fromkeys(keep, ""))
        except ConfigError as exc:
            raise ConfigError(f"backend plugin {origin}: PRESERVE: {exc}") from None
        for path in keep:
            if PurePosixPath(path).as_posix() != path:  # "./x", "a//b", "a/": never what render writes
                raise ConfigError(f"backend plugin {origin}: PRESERVE: {short_repr(path)} is not a normalised relative path")
    if hasattr(plugin, "REFS") and not isinstance(plugin.REFS, dict):
        raise ConfigError(f"backend plugin {origin}: REFS must be a dict")
    if hasattr(plugin, "build"):
        tools = getattr(plugin, "BUILD_TOOLS", None)
        if not (isinstance(tools, (tuple, list)) and all(isinstance(t, str) for t in tools)):
            raise ConfigError(f"backend plugin {origin}: defines build but no BUILD_TOOLS (a list of the local tools build runs)")
    try:
        check_schema_fragment(plugin.SCHEMA, f"[{plugin.NAME}]", defs=DEFS, allow_defs=False)
    except ValueError as exc:
        raise ConfigError(f"backend plugin {origin}: invalid SCHEMA: {esc(str(exc))}") from None
    except Exception as exc:  # backstop: a schema shape the checker did not anticipate
        raise ConfigError(f"backend plugin {origin}: invalid SCHEMA: {esc(type(exc).__name__)}: {esc(str(exc))}") from None


def discover() -> Dict[str, Backend]:
    """Load every registered backend, keyed by NAME, ordered by (KIND, NAME)."""
    found: Dict[str, Backend] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        origin = plugins.origin(ep)
        plugin = plugins.load(ep, "backend")
        check_contract(plugin, origin)
        if plugin.NAME != ep.name:
            raise ConfigError(f"backend plugin {origin}: entry-point name differs from NAME {short_repr(plugin.NAME)}")
        if plugin.NAME in found:
            raise ConfigError(f"backend {short_repr(plugin.NAME)} is registered twice")
        found[plugin.NAME] = plugin
    return dict(sorted(found.items(), key=lambda item: (item[1].KIND, item[0])))


def preserved(plugin: Backend) -> Tuple[str, ...]:
    """Files in ``rendered/`` that render keeps and diff ignores (the backend's ``PRESERVE``)."""
    return tuple(getattr(plugin, "PRESERVE", ()))


def for_kind(backends: Mapping[str, Backend], kind: str) -> Dict[str, Backend]:
    """The backends of one kind, keyed by NAME."""
    return {name: b for name, b in backends.items() if b.KIND == kind}


def lookup(backends: Mapping[str, Backend], kind: str, name: Any) -> Backend:
    """The backend ``name`` for ``kind``; ConfigError when unknown or of another kind."""
    if not isinstance(name, str) or name not in backends:
        known = ", ".join(for_kind(backends, kind)) or "none"
        raise ConfigError(f"unknown backend {short_repr(name)} for kind {kind!r} (registered: {known})")
    plugin = backends[name]
    if plugin.KIND != kind:
        raise ConfigError(f"backend {name!r} renders kind {plugin.KIND!r}, not {kind!r}")
    return plugin


def builtin() -> Dict[str, ModuleType]:
    """The backends the provision-hub distribution registers; the published JSON Schemas are generated from these.

    Read from the entry points (never imported by name), so third-party plugins, broken or
    not, do not change the published files.
    """
    found: Dict[str, ModuleType] = {}
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        if plugins.from_distribution(ep):
            found[ep.name] = plugins.load(ep, "backend")
    return dict(sorted(found.items()))


def _utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates
        return False
    return True


def _is_file_map(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(k, str) and isinstance(v, str) and _utf8(k) and _utf8(v) for k, v in value.items()
    )


def _is_findings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(f, Finding) for f in value)


def _is_lines(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(line, str) for line in value)


# What each backend method must return; anything else is a plugin bug reported as exit 2.
RETURN_CHECKS: Dict[str, Tuple[Callable[[Any], bool], str]] = {
    "render": (_is_file_map, "a dict of str paths to str texts, all encodable as UTF-8"),
    "validate": (_is_findings, "a list of prov.validate.Finding"),
    "check": (_is_findings, "a list of prov.validate.Finding"),
    "plan": (_is_lines, "a list of str"),
    "starter": (lambda v: isinstance(v, str), "a str"),
}


def call_plugin(plugin: Backend, method: str, *args: Any) -> Any:
    """Run a backend method; exceptions and wrongly typed results become a ConfigError.

    Plugin text in the message (exception text, a plugin's own ConfigError) is escaped with
    ``prov.text.esc``, so it is always one printable line.
    """
    check, expected = RETURN_CHECKS.get(method, (lambda v: True, ""))
    return plugins.guarded_call(f"backend {short_repr(getattr(plugin, 'NAME', '?'))}", plugin, method, args, check, expected)
