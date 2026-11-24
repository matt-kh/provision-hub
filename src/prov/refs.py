"""References between items: the ``[refs]`` table, resolved into ``Context.refs``.

A kind declares the references its items may hold (``REFS``: alias -> target kind, whether
required, a description); a spec names the targets in one envelope table::

    [refs]
    registry = "registry.example.org/hub"

``Resolution`` (and ``resolve``) turns that table into the read-only mapping a backend gets
as ``ctx.refs``: alias -> ``{"ref", "kind", "name", "outputs"}``, the outputs being those of
the target item (``prov.kinds.item_outputs`` of its validated spec).

Rules (docs/kinds.md "References"):

- Reads only ``<root>/<kind>/<name>/spec.toml`` files of targets (``spec_path`` +
  ``prov.schema.read_toml``): no symlinked kind or item directory, no symlinked or special
  ``spec.toml``, nothing outside the root, and nothing else of the target (its
  ``rendered/`` and ``build/`` do not matter). Never writes, never runs anything, never
  touches the network. Messages name targets by ref, never by absolute path.
- Every target is checked fully (envelope, schema, semantic checks, its backend's ``check``)
  with its own references resolved, so the whole closure is validated; a consumer gets
  one hop (its direct targets' outputs).
- Errors are findings at ``refs.<alias>``: target kind not installed, missing target item,
  invalid target (its first error, through the chain of references that leads to it),
  self-reference, cycle (``a -> b -> a``), a chain of more than ``MAX_DEPTH`` references
  from the item being checked. A target kind without ``OUTPUTS`` is allowed (its outputs are
  ``{}``). A broken plugin of a target (an exception in its kind's ``semantic`` or
  ``outputs``, or its backend's ``check``) stays a ConfigError (exit 2), naming the target.
- One ``Resolution`` is one run: each target is read and checked once (a diamond is not
  re-read) and its result shared, read-only, by every item that refers to it. The one
  exception: an item found invalid only because a chain below it was too long *from where
  it was reached* is checked again when reached by a shorter path (at most ``MAX_DEPTH``
  times, as the hop count only decreases).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from . import kinds
from .schema import SPEC_FILE, ConfigError, read_toml
from .text import esc, short_repr, show
from .validate import ERROR, Finding

# The longest chain of references from the item being checked (a -> b -> ... ), a guard
# against runaway recursion; deeper chains are an error.
MAX_DEPTH = 32


def freeze(value: Any) -> Any:
    """A deeply read-only copy of JSON data: dicts become ``MappingProxyType``, lists tuples."""
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """The plain JSON data of a ``freeze``-d value (mappings to dicts, tuples to lists)."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def chain_text(refs: Sequence[str]) -> str:
    """``a -> b -> c``; a long chain keeps its first three and last two refs (``a -> b -> c -> ... -> y -> z``)."""
    items = list(refs)
    if len(items) > 5:
        items = items[:3] + ["..."] + items[-2:]
    return " -> ".join(items)


class Refused(Exception):
    """A target path prov will not read (symlink, outside the root); the text is target-relative."""


def spec_path(root: Path, kind: str, name: str) -> Path:
    """``<root>/<kind>/<name>/spec.toml`` for reading only; Refused on a symlinked ``<kind>/`` or ``<name>/``.

    The root may be a symlink (it is resolved). Unlike ``prov.render.safe_item_dir`` the
    item's ``rendered/`` and ``build/`` are not looked at: a reference reads ``spec.toml``
    only. ``spec.toml`` itself is checked by ``read_toml`` (regular file, never followed).
    ``kind`` and ``name`` are a validated ref, so they hold no ``/`` or ``..``.
    """
    root = Path(root).resolve()
    kdir = root / kind
    idir = kdir / name
    for path, label in ((kdir, kind), (idir, f"{kind}/{name}")):
        if path.is_symlink():
            raise Refused(f"{label}: is a symlink; refused")
        if path.exists() and not path.resolve().is_relative_to(root):
            raise Refused(f"{label}: resolves outside the root; refused")
    return idir / SPEC_FILE


@dataclass(frozen=True)
class _Target:
    """The result of checking one target item, shared by every reference to it in a run."""

    outputs: Optional[Mapping[str, Any]]  # read-only; None when the item is invalid
    height: int = 0  # the longest chain of references below the item
    chain: Tuple[str, ...] = ()  # invalid: the refs that lead to the item at fault, this one first
    cause: str = ""  # invalid: that item's first error
    at_hops: Optional[int] = None  # invalid because of the depth limit, reached this many hops from the root item


@dataclass
class Resolution:
    """One resolution run against ``root``: memoised targets and the stack of items being checked.

    Called as ``resolution(kind_name, name, spec)`` (``prov.spec.check_raw``'s resolver) for the
    item being checked and, recursively, for every target. Returns (read-only mapping alias ->
    ``{"ref", "kind", "name", "outputs"}``, findings).
    """

    root: Path
    backends: Mapping[str, Any]
    _done: Dict[str, _Target] = field(default_factory=dict, init=False)
    _stack: List[str] = field(default_factory=list, init=False)
    _heights: Dict[str, int] = field(default_factory=dict, init=False)
    _deep: Dict[str, bool] = field(default_factory=dict, init=False)  # item -> a reference failed on the depth limit
    _via: Dict[Tuple[str, str], _Target] = field(default_factory=dict, init=False)  # (item, alias) -> invalid target

    def __call__(self, kind_name: str, name: str, spec: Mapping[str, Any]) -> Tuple[Mapping[str, Any], List[Finding]]:
        me = f"{kind_name}/{name}"
        declared = kinds.declared_refs(kinds.get(kind_name))
        table = spec.get("refs") or {}
        resolved: Dict[str, Any] = {}
        findings: List[Finding] = []
        height, deep = 0, False
        self._stack.append(me)
        try:
            for alias in sorted(table):
                where = f"refs.{alias}"
                target, problem, too_deep = self._target(me, alias, table[alias], declared.get(alias))
                deep = deep or too_deep
                if problem is not None:
                    findings.append(Finding(ERROR, problem, where))
                    continue
                assert target is not None
                ref, entry = target
                if entry.outputs is None:
                    self._via[(me, alias)] = entry
                    deep = deep or entry.at_hops is not None
                    findings.append(Finding(ERROR, f"invalid target: {chain_text(entry.chain)}: {entry.cause}", where))
                    continue
                height = max(height, entry.height + 1)
                kind, _, item = ref.partition("/")
                resolved[alias] = MappingProxyType({"ref": ref, "kind": kind, "name": item, "outputs": entry.outputs})
        finally:
            self._stack.pop()
        self._heights[me] = height
        self._deep[me] = deep
        return MappingProxyType(resolved), findings

    def _target(self, me: str, alias: str, value: Any, decl: Optional[Mapping[str, Any]]) -> Tuple[Optional[Tuple[str, _Target]], Optional[str], bool]:
        """((ref, target), None, False) or (None, problem, failed on the depth limit)."""
        from .spec import split_ref

        # Defence in depth: through prov.spec the composed schema already refused unknown
        # aliases, non-strings, malformed refs and refs to another kind; resolve() may be
        # called with any spec.
        if decl is None:
            return None, f"unknown reference alias {short_repr(alias)} (this kind declares: {', '.join(sorted(kinds.get(me.partition('/')[0]).REFS)) or 'none'})", False
        if not isinstance(value, str):
            return None, f"{short_repr(value)} is not a ref <kind>/<name>", False
        try:
            kind, name = split_ref(value)
        except ConfigError as exc:
            return None, str(exc), False
        ref = f"{kind}/{name}"
        if kind != decl["kind"]:
            return None, f"{ref} is a {kind} item; {short_repr(alias)} refers to a {decl['kind']} item", False
        # From here on the checks are reachable through the CLI.
        if kind not in kinds.discover():
            return None, f"{ref}: kind {short_repr(kind)} is not installed (install the plugin that provides it)", False
        if ref == me:
            return None, f"{ref} refers to the item itself", False
        if ref in self._stack:
            return None, f"reference cycle: {chain_text(self._stack[self._stack.index(ref):] + [ref])}", False
        hops = len(self._stack)  # from the item being checked to this target
        entry = self._done.get(ref)
        if entry is not None and entry.at_hops is not None and hops < entry.at_hops:
            entry = None  # invalid only because it was reached too deep: check it again from here
        if entry is None and hops > MAX_DEPTH or entry is not None and entry.outputs is not None and hops + entry.height > MAX_DEPTH:
            return None, f"more than {MAX_DEPTH} references in a row ({chain_text(self._stack + [ref])})", True
        if entry is None:
            entry = self._check(kind, name, hops)
            self._done[ref] = entry
        return (ref, entry), None, False

    def _check(self, kind_name: str, name: str, hops: int) -> _Target:
        """Read and fully check one target item; its own references are resolved by this run."""
        from .spec import check_raw, resolve_backend

        ref = f"{kind_name}/{name}"
        path = None
        try:
            path = spec_path(self.root, kind_name, name)
            if not os.path.lexists(path):
                return _Target(None, chain=(ref,), cause=f"no such item (no {ref}/{SPEC_FILE} under the root)")
            raw = read_toml(path)
        except Refused as exc:
            return _Target(None, chain=(ref,), cause=str(exc))
        except ConfigError as exc:  # symlinked or special spec.toml, unreadable or invalid TOML
            return _Target(None, chain=(ref,), cause=str(exc).replace(show(str(path)) + ": ", f"{SPEC_FILE}: "))
        except OSError as exc:
            return _Target(None, chain=(ref,), cause=f"cannot read: {esc(str(exc.strerror or exc))}")
        try:
            resolve_backend(raw, kind_name, self.backends)
        except ConfigError as exc:  # the target's envelope: a finding of the consumer, not exit 2
            return _Target(None, chain=(ref,), cause=f"{SPEC_FILE}: {exc}")
        try:
            spec, findings, _ = check_raw(raw, kind_name, name, self.backends, self)
            outputs = kinds.item_outputs(kinds.get(kind_name), spec) if spec is not None else None
        except ConfigError as exc:  # a broken plugin of the target: exit 2, naming the target
            raise ConfigError(f"reference target {ref}: {exc}") from None
        if spec is None:
            deep = self._deep.get(ref, False)
            at_hops = hops if deep else None
            errors = sorted((str(f.where), str(f.message)) for f in findings if f.level == ERROR)  # plugin fields may be any type
            first_where, first_message = errors[0]
            alias = first_where[len("refs."):] if first_where.startswith("refs.") else None
            below = self._via.get((ref, alias)) if alias is not None else None
            if below is not None:
                return _Target(None, chain=(ref,) + below.chain, cause=below.cause, at_hops=at_hops)
            # the depth limit counts from the root item, so `prov validate <target>` may well pass
            suffix = f" (and {len(errors) - 1} more error(s); run prov validate {ref})" if len(errors) > 1 and not deep else ""
            where = f"{first_where}: " if first_where else ""
            return _Target(None, chain=(ref,), cause=f"{where}{first_message}{suffix}", at_hops=at_hops)
        return _Target(freeze(outputs), height=self._heights.get(ref, 0))


def resolve(root: Path, kind_name: str, name: str, spec: Mapping[str, Any], backends: Mapping[str, Any]) -> Tuple[Mapping[str, Any], List[Finding]]:
    """The references of a validated spec, resolved against ``root``: (read-only mapping, findings).

    See ``Resolution``; one call is one run.
    """
    return Resolution(Path(root), backends)(kind_name, name, spec)
