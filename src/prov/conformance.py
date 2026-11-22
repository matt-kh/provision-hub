"""``prov conformance <backend>``: render a kind's fixtures through one backend and check the result.

Static and rendered only (docs/kinds.md "Conformance fixtures"): nothing here runs a
behavioural test, a plan command, a subprocess, ssh or the network. A backend may render a
behavioural test file (``goss.yaml``) and print the command that runs it in its plan; prov
only checks that the plan lines pass the refusal checks of ``prov.plan``.

Per fixture of the backend's kind (``prov.kinds.fixtures``), in name order:

``supported``  the backend lists the fixture in ``UNSUPPORTED_FIXTURES``: skipped, with its reason
``table``      the backend has no table for the fixture although its ``SCHEMA`` requires keys
``refs``       each literal ``refs`` entry matches its target kind's ``OUTPUTS`` (skipped
               when that kind is not installed: then checked as JSON data only)
``spec``       envelope + fixture spec + backend table (+ ``[refs]``) checked like a spec file
               (``prov.spec.check_raw``) with ``ctx.refs`` from the literals; a fixture with
               ``expect.valid = false`` must be rejected, and stops here: it is composed with
               the backend's real table (like a valid one) and checked through the real
               backend, and only errors outside that table count (one containing
               ``expect.error``, if given)
``outputs``    the kind's outputs; equal to ``expect.outputs`` when given
``render``     two renders give equal file maps
``write``      written to a fresh temporary ``<tmp>/<kind>/<fixture>/rendered/``: then
               ``diff_rendered`` is empty and a second write changes nothing
``validate``   the backend's ``validate`` on that directory reports no error (``skipped``
               when it reports skipped findings, such as a missing external tool)
``contains``   each ``expect.contains`` string appears in a rendered file
``absent``     no ``expect.absent`` string appears in any rendered file
``plan``       ``prov.plan.plan_lines`` accepts the plan (never executed, never printed
               for a human to run)

A failure in ``table``, ``refs``, ``spec``, ``render`` or ``write`` ends the fixture: the
later checks depend on it. A plugin failure (exception, wrong return type) is a FAIL of the
check it happened in, not an exit-2 error. A name in the backend's ``FIXTURES`` or
``UNSUPPORTED_FIXTURES`` that is not a fixture of the kind is an extra entry with a FAIL of
``table``, after the kind's fixtures.

Files are written only to a fresh temporary directory (``tempfile.mkdtemp``, under
``$TMPDIR``), never in the item tree, and removed before ``run`` returns, best effort
against plugins that change permissions or remove it; its path never appears in a result:
plugin text that names it, resolved or not, reads ``<tmp>`` instead.
"""

from __future__ import annotations

import copy
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

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
JSON_STATUS = {OK: "ok", FAIL: "fail", SKIP: "skipped"}  # the --json spelling of each status


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

    @property
    def skipped_checks(self) -> int:
        """Checks skipped inside fixtures that ran (an unsupported fixture's ``supported`` is not counted)."""
        return sum(1 for f in self.fixtures if not f.unsupported for c in f.checks if c.status == SKIP)


class _Stop(Exception):
    """Ends one fixture after a failed prerequisite check."""


def _finding_text(f: Finding) -> str:
    where = str(f.where)
    return f"{f.level}: {where + ': ' if where else ''}{f.message}"


def _first_errors(findings: List[Finding]) -> str:
    errors = sorted((f for f in findings if f.level == ERROR), key=lambda f: (str(f.where), str(f.message)))
    more = f" (and {len(errors) - 1} more error(s))" if len(errors) > 1 else ""
    return _finding_text(errors[0]) + more


def synthetic_ref(kind_name: str, alias: str, taken: frozenset = frozenset()) -> str:
    """``<target kind>/<alias>`` with every run of characters other than ``[a-z0-9]`` as one ``-``: a valid ref.

    A ref already in ``taken`` (``a_b`` and ``a-b`` to the same kind) gets the first free
    suffix ``-2``, ``-3``, ...
    """
    name = "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in alias)
    while "--" in name:
        name = name.replace("--", "-")
    base = name.strip("-")
    ref, n = f"{kind_name}/{base[:63].rstrip('-')}", 1
    while ref in taken:
        n += 1
        suffix = f"-{n}"
        ref = f"{kind_name}/{base[: 63 - len(suffix)].rstrip('-')}{suffix}"
    return ref


def _in_table(finding: Finding, table: str) -> bool:
    """True for a finding about the backend table ``table``: its ``where``, or a schema error's message, starts with it."""
    where, message = str(finding.where), str(finding.message)
    text = where if where else message.partition(": ")[0]
    return text == table or text.startswith((table + ".", table + "["))


def _os_error(exc: OSError) -> str:
    name = f"{show(os.fsdecode(exc.filename))}: " if exc.filename is not None else ""
    return f"{name}{esc(str(exc.strerror or exc))}"


class _Run:
    """One fixture through one backend; results go to ``self.result``."""

    def __init__(self, plugin: Any, kind: Any, backends: Mapping[str, Any], name: str, fixture: Dict[str, Any], tmp: Path, scrub: Callable[[str], str]) -> None:
        self.plugin, self.kind, self.backends = plugin, kind, backends
        self.name, self.fixture, self.tmp, self.scrub = name, fixture, tmp, scrub
        self.result = FixtureResult(name)

    def add(self, check: str, status: str, detail: str) -> None:
        self.result.checks.append(Check(check, status, self.scrub(detail)))

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
        self._plan(spec, ctx)

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
            out[alias] = (target, synthetic_ref(target, alias, frozenset(ref for _, ref, _ in out.values())), outputs)
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

        expect = self.fixture["expect"]
        try:
            spec, findings, resolved = check_raw(raw, kind.NAME, name, self.backends, resolver)
        except ConfigError as exc:
            self.stop("spec", str(exc))
        rejected = any(f.level == ERROR for f in findings)
        if not expect["valid"]:
            self._rejected(findings)
            return None, Context(name)
        if rejected:
            self.stop("spec", _first_errors(findings))
        warnings = sum(1 for f in findings if f.level == WARNING)
        self.add("spec", OK, "valid" + (f" ({warnings} warning(s))" if warnings else ""))
        return spec, Context(name, resolved)

    def _rejected(self, findings: List[Finding]) -> None:
        """A negative fixture: an error outside the backend table, containing ``expect.error`` when given."""
        errors = [f for f in findings if f.level == ERROR]
        counted = [f for f in errors if not _in_table(f, self.plugin.NAME)]
        wanted = self.fixture["expect"].get("error")
        if wanted is not None:
            matching = [f for f in counted if wanted in _finding_text(f)]
            if matching:
                self.add("spec", OK, f"rejected as expected: {_first_errors(matching)}")
            elif counted:
                self.add("spec", FAIL, f"rejected, but no error contains expect.error {short_repr(wanted)}: {_first_errors(counted)}")
            else:
                self.add("spec", FAIL, self._not_rejected(errors))
            return
        if counted:
            self.add("spec", OK, f"rejected as expected: {_first_errors(counted)}")
        else:
            self.add("spec", FAIL, self._not_rejected(errors))

    def _not_rejected(self, errors: List[Finding]) -> str:
        if errors:
            return f"rejected only inside the backend table {self.plugin.NAME!r}, which does not count: {_first_errors(errors)}"
        return "accepted, but the fixture expects it to be rejected (expect.valid = false)"

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
        except OSError as exc:
            self.stop("write", _os_error(exc))
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
        except OSError as exc:  # guarded_call turns a plugin's own OSError into ConfigError; this is a backstop
            self.add("validate", FAIL, _os_error(exc))
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

    def _plan(self, spec: Dict[str, Any], ctx: Context) -> None:
        placeholder = Path("<tmp>") / self.kind.NAME / self.name / "rendered"  # the header's cd line; never printed
        try:
            lines = plan_lines(self.plugin, copy.deepcopy(spec), ctx, f"{self.kind.NAME}/{self.name}", placeholder)
        except ConfigError as exc:
            self.add("plan", FAIL, str(exc))
            return
        self.add("plan", OK, f"{len(lines)} line(s) pass the plan checks; prov never runs them")


def _scrubber(raw: str, tmp: Path) -> Callable[[str], str]:
    """Replace every spelling of the temporary directory in a detail by ``<tmp>``.

    The forms as created, absolute and resolved (``$TMPDIR`` may be a symlink), raw and as
    ``show`` prints them, longest first; then the random basename itself.
    """
    forms = {raw, os.path.abspath(raw), os.path.realpath(raw), str(tmp)}
    forms |= {show(form) for form in forms}
    ordered = sorted(forms, key=lambda form: (-len(form), form))
    base = os.path.basename(raw)

    def scrub(text: str) -> str:
        for form in ordered:
            text = text.replace(form, "<tmp>")
        return text.replace(base, "<tmp>")

    return scrub


def _unlock(top: Path) -> None:
    """Make every directory below ``top`` (and ``top``) writable and searchable again; symlinks are never followed."""
    try:
        os.chmod(top, 0o700)
    except OSError:
        pass
    for dirpath, dirnames, _ in os.walk(top):  # top-down: children are unlocked before they are listed
        for name in dirnames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                try:
                    os.chmod(path, 0o700)
                except OSError:
                    pass


def _remove(tmp: Path) -> bool:
    """Remove the temporary directory, best effort; True when it is gone."""

    def retry(func: Any, path: str, exc: BaseException) -> None:
        if isinstance(exc, FileNotFoundError):
            return
        try:
            os.chmod(os.path.dirname(path), 0o700)
            func(path)
        except OSError:
            pass

    for _ in range(2):
        if not os.path.lexists(tmp):
            return True
        _unlock(tmp)
        if sys.version_info >= (3, 12):
            shutil.rmtree(tmp, onexc=retry)
        else:  # pragma: no cover - Python 3.10 and 3.11
            shutil.rmtree(tmp, onerror=lambda func, path, info: retry(func, path, info[1]))
    return not os.path.lexists(tmp)


def run(backend_name: str) -> Result:
    """Run every fixture of the backend's kind through it; ConfigError (exit 2) for an unknown
    backend, a kind without fixtures, or a temporary directory that could not be removed."""
    backends = discover()
    if backend_name not in backends:
        raise ConfigError(f"unknown backend {short_repr(backend_name)} (registered: {', '.join(backends) or 'none'})")
    plugin = backends[backend_name]
    kind = kinds.get(plugin.KIND)
    fixtures = kinds.fixtures(kind)
    if not fixtures:
        raise ConfigError(f"kind {kind.NAME} has no fixtures (FIXTURES); nothing to check for backend {plugin.NAME}")
    raw = tempfile.mkdtemp(prefix="prov-conformance-")
    tmp = Path(raw).resolve()
    scrub = _scrubber(raw, tmp)
    try:
        results = [_Run(plugin, kind, backends, name, fixture, tmp, scrub).go() for name, fixture in fixtures.items()]
    except BaseException:  # KeyboardInterrupt included: clean up, never replace the exception in flight
        try:
            _remove(tmp)
        except Exception:
            pass
        raise
    if not _remove(tmp):
        raise ConfigError("could not remove the temporary directory <tmp> (a prov-conformance-* directory in $TMPDIR); remove it by hand")
    results += _unknown_names(plugin, kind, fixtures)
    return Result(plugin.NAME, kind.NAME, tuple(results))


def _unknown_names(plugin: Any, kind: Any, fixtures: Mapping[str, Any]) -> List[FixtureResult]:
    """An entry with a FAIL of ``table`` per backend key that names no fixture of the kind."""
    out = []
    known = ", ".join(fixtures)
    for member in ("FIXTURES", "UNSUPPORTED_FIXTURES"):
        for name in sorted(getattr(plugin, member, {})):
            if name not in fixtures:
                detail = f"{member} names unknown fixture {short_repr(name)} (fixtures of kind {kind.NAME}: {known})"
                out.append(FixtureResult(name, [Check("table", FAIL, detail)]))
    return sorted(out, key=lambda f: f.name)


EXPORT_FORMAT = "prov.conformance/v1alpha1"


def fixtures_file_name(kind: Any) -> str:
    """``<kind>.<current version>.fixtures.json``, the exported conformance file of a kind."""
    return f"{kind.NAME}.{kind.VERSIONS[-1]}.fixtures.json"


def export_fixtures(kind: Any, backends: Mapping[str, Any]) -> Dict[str, Any]:
    """The language-neutral conformance document of one kind (docs/kinds.md "Exported fixtures").

    ``{"format", "kind", "api_version", "fixtures", "backends"}``: the kind's fixtures in the
    ``prov.kinds.fixtures`` form (``expect`` defaults filled in) and, per backend of that kind
    in ``backends`` (keyed by NAME), its tables (``fixtures``) and reasons (``unsupported``).
    Kind-neutral: the caller chooses the backends (the published files use
    ``prov.registry.builtin()``). Serialise with ``prov.text.json_text``.
    """
    return {
        "format": EXPORT_FORMAT,
        "kind": kind.NAME,
        "api_version": api_versions(kind)[-1],
        "fixtures": kinds.fixtures(kind),
        "backends": {
            name: {
                "fixtures": copy.deepcopy(dict(getattr(plugin, "FIXTURES", {}))),
                "unsupported": dict(getattr(plugin, "UNSUPPORTED_FIXTURES", {})),
            }
            for name, plugin in sorted(backends.items())
            if plugin.KIND == kind.NAME
        },
    }


def text_lines(result: Result) -> List[str]:
    """``<status>\\t<fixture>\\t<check>\\t<detail>`` per check, then a summary line; escaped."""
    lines = [f"{c.status}\t{esc(f.name)}\t{c.check}\t{esc(c.detail)}" for f in result.fixtures for c in f.checks]
    counts = {status: sum(1 for f in result.fixtures if f.status == status) for status in (OK, FAIL, SKIP)}
    lines.append(
        f"conformance {result.backend} ({result.kind}): {len(result.fixtures)} fixture(s): "
        f"{counts[OK]} ok, {counts[FAIL]} FAIL, {counts[SKIP]} skipped; {result.skipped_checks} check(s) skipped"
    )
    return lines


def document(result: Result) -> Dict[str, Any]:
    """The ``--json`` document (without ``format``, which the CLI adds); raw values."""
    return {
        "backend": result.backend,
        "kind": result.kind,
        "ok": result.ok,
        "skipped_checks": result.skipped_checks,
        "fixtures": [
            {
                "name": f.name,
                "status": JSON_STATUS[f.status],
                "checks": [{"check": c.check, "status": JSON_STATUS[c.status], "detail": c.detail} for c in f.checks],
            }
            for f in result.fixtures
        ],
    }
