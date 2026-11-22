"""``prov conformance <backend>``: render a kind's fixtures through one backend and check the result.

Static and rendered only (docs/kinds.md "Conformance fixtures"): nothing here runs a
behavioural test, a plan command, a subprocess, ssh or the network. A backend may render a
behavioural test file (``goss.yaml``) and print the command that runs it in its plan; prov
only checks that the plan lines pass the refusal checks of ``prov.plan``.

Per fixture of the backend's kind (``prov.kinds.fixtures``), in name order:

``supported``  the backend lists the fixture in ``UNSUPPORTED_FIXTURES``: skipped, with its reason
``table``      the backend has no table for the fixture and its ``SCHEMA`` requires keys
``refs``       each literal ``refs`` entry matches its target kind's ``OUTPUTS`` (skipped
               when that kind is not installed: then checked as JSON data only)
``spec``       envelope + fixture spec + backend table (+ ``[refs]``) checked like a spec file
               (``prov.spec.check_raw``) with ``ctx.refs`` from the literals; a fixture with
               ``expect.valid = false`` must be rejected, and stops here
``outputs``    the kind's outputs; equal to ``expect.outputs`` when given
``render``     two renders give equal file maps
``write``      written to a fresh temporary ``<tmp>/<kind>/<fixture>/rendered/``: then
               ``diff_rendered`` is empty and a second write changes nothing
``validate``   the backend's ``validate`` on that directory reports no error (``skipped``
               when it reports skipped findings, such as a missing external tool)
``contains``   each ``expect.contains`` string appears in a rendered file
``absent``     no ``expect.absent`` string appears in any rendered file
``plan``       ``prov.plan.plan_lines`` accepts the plan (never executed, never printed)

A failure in ``table``, ``refs``, ``spec``, ``render`` or ``write`` ends the fixture: the
later checks depend on it. A plugin failure (exception, wrong return type) is a FAIL of the
check it happened in, not an exit-2 error.

The temporary directory is outside ``--root`` (nothing is written there), removed before
``run`` returns, and its path never appears in a result: plugin text that names it reads
``<tmp>`` instead.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import kinds
from .kinds.common import DEFS
from .plan import plan_lines
from .refs import freeze
from .registry import Context, call_plugin, discover, preserved
from .render import diff_rendered, write_rendered
from .schema import ConfigError, api_versions
from .spec import check_raw
from .text import esc, short_repr, show
from .validate import ERROR, SKIPPED, WARNING, Finding, json_equal, schema_errors

OK, FAIL, SKIP = "ok", "FAIL", "skipped"


@dataclass(frozen=True)
class Check:
    check: str
    status: str
    detail: str  # raw: plugin text is escaped at the printing boundary (text_lines, json_text)


@dataclass
class FixtureResult:
    name: str
    checks: List[Check] = field(default_factory=list)
    unsupported: bool = False

    @property
    def status(self) -> str:
        if any(c.status == FAIL for c in self.checks):
            return FAIL
        return SKIP if self.unsupported else OK


@dataclass(frozen=True)
class Result:
    backend: str
    kind: str
    fixtures: Tuple[FixtureResult, ...]

    @property
    def ok(self) -> bool:
        return all(f.status != FAIL for f in self.fixtures)


class _Stop(Exception):
    """Ends one fixture after a failed prerequisite check."""


def _finding_text(f: Finding) -> str:
    where = str(f.where)
    return f"{f.level}: {where + ': ' if where else ''}{f.message}"


def _first_errors(findings: List[Finding]) -> str:
    errors = sorted((f for f in findings if f.level == ERROR), key=lambda f: (str(f.where), str(f.message)))
    more = f" (and {len(errors) - 1} more error(s))" if len(errors) > 1 else ""
    return _finding_text(errors[0]) + more


def synthetic_ref(kind_name: str, alias: str) -> str:
    """``<target kind>/<alias>`` with every run of characters other than ``[a-z0-9]`` as one ``-``: a valid ref."""
    name = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in alias)
    while "--" in name:
        name = name.replace("--", "-")
    return f"{kind_name}/{name.strip('-')[:63].rstrip('-')}"


class _Run:
    """One fixture through one backend; results go to ``self.result``."""

    def __init__(self, plugin: Any, kind: Any, backends: Mapping[str, Any], name: str, fixture: Dict[str, Any], tmp: Path) -> None:
        self.plugin, self.kind, self.backends = plugin, kind, backends
        self.name, self.fixture, self.tmp = name, fixture, tmp
        self.result = FixtureResult(name)

    def add(self, check: str, status: str, detail: str) -> None:
        detail = detail.replace(str(self.tmp), "<tmp>").replace(show(str(self.tmp)), "<tmp>")
        self.result.checks.append(Check(check, status, detail))

    def stop(self, check: str, detail: str) -> None:
        self.add(check, FAIL, detail)
        raise _Stop

    def go(self) -> FixtureResult:
        try:
            self._go()
        except _Stop:
            pass
        return self.result

    # -- the checks, in order ---------------------------------------------------------------

    def _go(self) -> None:
        plugin, name = self.plugin, self.name
        reasons = getattr(plugin, "UNSUPPORTED_FIXTURES", {})
        if name in reasons:
            self.result.unsupported = True
            self.add("supported", SKIP, f"the backend cannot express this fixture: {reasons[name]}")
            return
        table = self._table()
        refs = self._refs()
        spec, ctx = self._spec(table, refs)
        if spec is None:
            return
        self._outputs(spec)
        files = self._render(spec, ctx)
        rendered = self._write(files)
        self._validate(rendered)
        self._texts(files)
        self._plan(spec, ctx, rendered)

    def _table(self) -> Dict[str, Any]:
        tables = getattr(self.plugin, "FIXTURES", {})
        if self.name in tables:
            return copy.deepcopy(tables[self.name])
        if not self.plugin.SCHEMA.get("required"):
            return {}
        self.stop("table", f"no backend table for fixture; the backend's SCHEMA requires {', '.join(map(str, self.plugin.SCHEMA['required']))}: add FIXTURES[{self.name!r}] or UNSUPPORTED_FIXTURES[{self.name!r}]")
        return {}  # unreachable

    def _refs(self) -> Dict[str, Tuple[str, str, Dict[str, Any]]]:
        """alias -> (target kind, synthetic ref, literal outputs); the ``refs`` check when there are any."""
        literals = self.fixture["refs"]
        if not literals:
            return {}
        declared = kinds.declared_refs(self.kind)
        installed = kinds.discover()
        out: Dict[str, Tuple[str, str, Dict[str, Any]]] = {}
        absent: List[str] = []
        for alias, outputs in sorted(literals.items()):
            target = declared[alias]["kind"]
            out[alias] = (target, synthetic_ref(target, alias), outputs)
            if target not in installed:
                absent.append(f"refs.{alias}: kind {short_repr(target)} is not installed; its literal outputs were checked as JSON data only")
                continue
            schema = getattr(installed[target], "OUTPUTS", None)
            if schema is None:
                errors = [] if outputs == {} else [f"refs.{alias}: the kind has no OUTPUTS, so its outputs are {{}}"]
            else:
                errors = schema_errors(outputs, {**schema, "$defs": DEFS}, None, f"refs.{alias}")
            if errors:
                self.stop("refs", f"literal outputs do not match OUTPUTS of kind {short_repr(target)}: {'; '.join(errors)}")
        if absent:
            self.add("refs", SKIP, "; ".join(absent))
        else:
            self.add("refs", OK, f"{len(out)} reference(s) match their target kinds' OUTPUTS")
        return out

    def _spec(self, table: Dict[str, Any], refs: Dict[str, Tuple[str, str, Dict[str, Any]]]) -> Tuple[Optional[Dict[str, Any]], Context]:
        kind, plugin, name = self.kind, self.plugin, self.name
        raw: Dict[str, Any] = {"api_version": api_versions(kind)[-1], "kind": kind.NAME, "backend": plugin.NAME}
        raw.update(copy.deepcopy(self.fixture["spec"]))
        if refs:
            raw["refs"] = {alias: ref for alias, (_, ref, _) in refs.items()}
        raw[plugin.NAME] = table

        def resolver(kind_name: str, item: str, spec: Mapping[str, Any]) -> Tuple[Mapping[str, Any], List[Finding]]:
            resolved = {}
            for alias, ref in sorted((spec.get("refs") or {}).items()):
                target, _, outputs = refs[alias]
                target_name = ref.partition("/")[2]
                resolved[alias] = MappingProxyType({"ref": ref, "kind": target, "name": target_name, "outputs": freeze(copy.deepcopy(outputs))})
            return MappingProxyType(resolved), []

        expect_valid = self.fixture["expect"]["valid"]
        try:
            spec, findings, resolved = check_raw(raw, kind.NAME, name, self.backends, resolver)
        except ConfigError as exc:
            self.stop("spec", str(exc))
        rejected = any(f.level == ERROR for f in findings)
        if not expect_valid:
            if rejected:
                self.add("spec", OK, f"rejected as expected: {_first_errors(findings)}")
            else:
                self.add("spec", FAIL, "accepted, but the fixture expects it to be rejected (expect.valid = false)")
            return None, Context(name)
        if rejected:
            self.stop("spec", _first_errors(findings))
        warnings = sum(1 for f in findings if f.level == WARNING)
        self.add("spec", OK, "valid" + (f" ({warnings} warning(s))" if warnings else ""))
        return spec, Context(name, resolved)

    def _outputs(self, spec: Dict[str, Any]) -> None:
        try:
            outputs = kinds.item_outputs(self.kind, spec)
        except ConfigError as exc:
            self.add("outputs", FAIL, str(exc))
            return
        expect = self.fixture["expect"]
        if "outputs" not in expect:
            self.add("outputs", OK, f"{len(outputs)} output(s); no expect.outputs to compare")
        elif json_equal(outputs, expect["outputs"]):
            self.add("outputs", OK, "equal to expect.outputs")
        else:
            self.add("outputs", FAIL, f"differ from expect.outputs: got {short_repr(outputs, 200)}, expected {short_repr(expect['outputs'], 200)}")

    def _render(self, spec: Dict[str, Any], ctx: Context) -> Dict[str, str]:
        try:
            first = call_plugin(self.plugin, "render", copy.deepcopy(spec), ctx)
            second = call_plugin(self.plugin, "render", copy.deepcopy(spec), ctx)
        except ConfigError as exc:
            self.stop("render", str(exc))
        if first != second:
            differ = sorted(set(first) ^ set(second) | {p for p in set(first) & set(second) if first[p] != second[p]})
            shown = ", ".join(show(p) for p in differ[:5]) + (f" (and {len(differ) - 5} more)" if len(differ) > 5 else "")
            self.stop("render", f"not deterministic: a second render differs in {shown}")
        self.add("render", OK, f"{len(first)} file(s), equal on a second render")
        return first

    def _write(self, files: Dict[str, str]) -> Path:
        rendered = self.tmp / self.kind.NAME / self.name / "rendered"
        keep = preserved(self.plugin)
        try:
            changes = write_rendered(rendered, files, keep)
            if diff_rendered(rendered, files, keep):
                self.stop("write", "the written tree differs from the render (diff not empty)")
            again = write_rendered(rendered, files, keep)
        except ConfigError as exc:
            self.stop("write", str(exc))
        if again:
            self.stop("write", f"a second write made {len(again)} change(s): {', '.join(again[:5])}")
        self.add("write", OK, f"{len(changes)} change(s) in a fresh temporary rendered/; diff empty; a second write makes 0 changes")
        return rendered

    def _validate(self, rendered: Path) -> None:
        try:
            findings = call_plugin(self.plugin, "validate", rendered)
        except ConfigError as exc:
            self.add("validate", FAIL, str(exc))
            return
        if any(f.level == ERROR for f in findings):
            self.add("validate", FAIL, _first_errors(findings))
            return
        skipped = sorted(str(f.message) for f in findings if f.level == SKIPPED)
        warnings = sum(1 for f in findings if f.level == WARNING)
        note = f" ({warnings} warning(s))" if warnings else ""
        if skipped:
            self.add("validate", SKIP, f"no errors{note}; skipped: {'; '.join(skipped)}")
        else:
            self.add("validate", OK, f"no errors{note}")

    def _texts(self, files: Dict[str, str]) -> None:
        texts = [files[p] for p in sorted(files)]
        expect = self.fixture["expect"]
        if "contains" in expect:
            missing = [t for t in expect["contains"] if not any(t in text for text in texts)]
            if missing:
                self.add("contains", FAIL, f"not in any rendered file: {', '.join(short_repr(t) for t in missing)}")
            else:
                self.add("contains", OK, f"{len(expect['contains'])} string(s) found")
        if "absent" in expect:
            present = [t for t in expect["absent"] if any(t in text for text in texts)]
            if present:
                self.add("absent", FAIL, f"found in a rendered file: {', '.join(short_repr(t) for t in present)}")
            else:
                self.add("absent", OK, f"{len(expect['absent'])} string(s) absent")

    def _plan(self, spec: Dict[str, Any], ctx: Context, rendered: Path) -> None:
        try:
            lines = plan_lines(self.plugin, copy.deepcopy(spec), ctx, f"{self.kind.NAME}/{self.name}", rendered)
        except ConfigError as exc:
            self.add("plan", FAIL, str(exc))
            return
        self.add("plan", OK, f"{len(lines)} line(s), for a human to run; prov never runs them")


def run(backend_name: str) -> Result:
    """Run every fixture of the backend's kind through it; ConfigError (exit 2) for an unknown
    backend or a kind without fixtures."""
    backends = discover()
    if backend_name not in backends:
        raise ConfigError(f"unknown backend {short_repr(backend_name)} (registered: {', '.join(backends) or 'none'})")
    plugin = backends[backend_name]
    kind = kinds.get(plugin.KIND)
    fixtures = kinds.fixtures(kind)
    if not fixtures:
        raise ConfigError(f"kind {kind.NAME} has no fixtures (FIXTURES); nothing to check for backend {plugin.NAME}")
    tmp = Path(tempfile.mkdtemp(prefix="prov-conformance-"))
    try:
        results = tuple(_Run(plugin, kind, backends, name, fixture, tmp).go() for name, fixture in fixtures.items())
    finally:
        shutil.rmtree(tmp)
    return Result(plugin.NAME, kind.NAME, results)


def text_lines(result: Result) -> List[str]:
    """``<status>\\t<fixture>\\t<check>\\t<detail>`` per check, then a summary line; escaped."""
    lines = [f"{c.status}\t{f.name}\t{c.check}\t{esc(c.detail)}" for f in result.fixtures for c in f.checks]
    counts = {status: sum(1 for f in result.fixtures if f.status == status) for status in (OK, FAIL, SKIP)}
    lines.append(
        f"conformance {result.backend} ({result.kind}): {len(result.fixtures)} fixture(s): "
        f"{counts[OK]} ok, {counts[FAIL]} FAIL, {counts[SKIP]} skipped"
    )
    return lines


def document(result: Result) -> Dict[str, Any]:
    """The ``--json`` document (without ``format``, which the CLI adds); raw values."""
    return {
        "backend": result.backend,
        "kind": result.kind,
        "ok": result.ok,
        "fixtures": [
            {"name": f.name, "status": f.status, "checks": [{"check": c.check, "status": c.status, "detail": c.detail} for c in f.checks]}
            for f in result.fixtures
        ],
    }
