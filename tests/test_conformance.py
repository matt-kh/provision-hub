"""prov conformance: kind FIXTURES rendered through one backend, checked statically.

A third-party dotted kind (site.example.org) and its backend (example-site-goss), installed
only through fake entry points, plus the registry kind of test_refs as a reference target.
The backend renders a behavioural test file (goss.yaml) and its plan prints the goss
command; prov never runs it.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import tempfile
import types

import pytest
from conftest import fake_entry_point
from test_kind_plugins import fake_backend, fake_kind
from test_refs import registry_kind

from prov import kinds
from prov.cli import main
from prov.conformance import export_fixtures, synthetic_ref
from prov.registry import discover, for_kind
from prov.text import json_text
from prov.validate import ERROR, SKIPPED, WARNING, Finding

SITE = "site.example.org"
BACKEND = "example-site-goss"
HOSTILE = "ev\x1b[2Jil\nerror: forged‮​\x85\x07"


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def clean(text):
    return all(ch.isprintable() or ch in "\n\t" for ch in text)


def fixtures():
    return {
        "basic": {
            "spec": {"hostname": "web.example.org"},
            "expect": {"outputs": {"url": "https://web.example.org"}, "contains": ["host web.example.org"], "absent": ["password"]},
        },
        "even-replicas": {"spec": {"hostname": "x.example.org", "replicas": 2}, "expect": {"valid": False}},
        "with-registry": {
            "spec": {"hostname": "m.example.org"},
            "refs": {"registry": {"url": "https://hub.example.org:5000"}},
            "expect": {"contains": ["registry https://hub.example.org:5000"]},
        },
        "cached": {"spec": {"hostname": "c.example.org"}, "refs": {"cache": {"size": 1}}},
    }


def site_kind(**extra):
    kind = types.ModuleType(SITE)
    kind.__doc__ = "A test-only kind: a web site."
    kind.NAME = SITE
    kind.PLUGIN_API = 1
    kind.VERSIONS = ("v1alpha1",)
    kind.PROPERTIES = {"hostname": {"type": "string", "pattern": "^[a-z0-9.-]+$"}, "replicas": {"type": "integer", "minimum": 1, "default": 1}}
    kind.REQUIRED = ["hostname"]
    kind.REFS = {"registry": {"kind": "registry.example.org"}, "cache": {"kind": "cache.example.org"}}
    kind.semantic = lambda spec: [Finding(ERROR, "replicas must be odd", "replicas")] if spec["replicas"] % 2 == 0 else []
    kind.OUTPUTS = {"type": "object", "additionalProperties": False, "required": ["url"], "properties": {"url": {"type": "string"}}}
    kind.outputs = lambda spec: {"url": f"https://{spec['hostname']}"}
    kind.FIXTURES = fixtures()
    for key, value in extra.items():
        setattr(kind, key, value)
    return kind


def site_backend(**extra):
    mod = types.ModuleType(BACKEND)
    mod.__doc__ = "Renders a site config and a goss test file."
    mod.NAME = BACKEND
    mod.KIND = SITE
    mod.PLUGIN_API = 1
    mod.SCHEMA = {"type": "object", "additionalProperties": False, "properties": {"port": {"type": "integer", "default": 8080}}}

    def render(spec, ctx):
        registry = ctx.refs["registry"]["outputs"]["url"] if "registry" in ctx.refs else "-"
        return {
            "site.conf": f"host {spec['hostname']}\nport {spec[BACKEND]['port']}\nregistry {registry}\n",
            "tests/goss.yaml": f"http:\n  https://{spec['hostname']}:\n    status: 200\n",
        }

    mod.render = render
    mod.validate = lambda rendered_dir: [] if (rendered_dir / "site.conf").is_file() else [Finding(ERROR, "missing", "site.conf")]
    mod.plan = lambda spec, ctx: ["# behavioural tests: run them on the host yourself", "goss --gossfile tests/goss.yaml validate"]
    mod.FIXTURES = {"basic": {"port": 8081}}
    for key, value in extra.items():
        setattr(mod, key, value)
    return mod


@pytest.fixture
def install(extra_kinds, extra_backends):
    """install(kind=..., backend=...): the site kind + backend and the registry kind, through fake entry points only."""

    def go(kind=None, backend=None, registry=True):
        eps = [fake_entry_point(kind or site_kind(), value="prov_site.kind")]
        if registry:
            eps.append(fake_entry_point(registry_kind(), value="prov_registry.kind"))
        extra_kinds(*eps)
        extra_backends(fake_entry_point(backend or site_backend(), value="prov_site.goss"))

    return go


PASSING = [
    "ok\tbasic\tspec\tvalid",
    "ok\tbasic\toutputs\tequal to expect.outputs",
    "ok\tbasic\trender\t2 file(s), equal on a second render",
    "ok\tbasic\twrite\t2 change(s) in a fresh temporary rendered/; diff empty; a second write makes 0 changes",
    "ok\tbasic\tvalidate\tno errors",
    "ok\tbasic\tcontains\t1 string(s) found",
    "ok\tbasic\tabsent\t1 string(s) absent",
    "ok\tbasic\tplan\t5 line(s) pass the plan checks; prov never runs them",
    "skipped\tcached\trefs\trefs.cache: kind 'cache.example.org' is not installed; its literal outputs were checked as JSON data only",
    "ok\tcached\tspec\tvalid",
    "ok\tcached\toutputs\t1 output(s); no expect.outputs to compare",
    "ok\tcached\trender\t2 file(s), equal on a second render",
    "ok\tcached\twrite\t2 change(s) in a fresh temporary rendered/; diff empty; a second write makes 0 changes",
    "ok\tcached\tvalidate\tno errors",
    "ok\tcached\tplan\t5 line(s) pass the plan checks; prov never runs them",
    "ok\teven-replicas\tspec\trejected as expected: error: replicas: replicas must be odd",
    "ok\twith-registry\trefs\t1 reference(s) match their target kinds' OUTPUTS",
    "ok\twith-registry\tspec\tvalid",
    "ok\twith-registry\toutputs\t1 output(s); no expect.outputs to compare",
    "ok\twith-registry\trender\t2 file(s), equal on a second render",
    "ok\twith-registry\twrite\t2 change(s) in a fresh temporary rendered/; diff empty; a second write makes 0 changes",
    "ok\twith-registry\tvalidate\tno errors",
    "ok\twith-registry\tcontains\t1 string(s) found",
    "ok\twith-registry\tplan\t5 line(s) pass the plan checks; prov never runs them",
    f"conformance {BACKEND} ({SITE}): 4 fixture(s): 4 ok, 0 FAIL, 0 skipped; 1 check(s) skipped",
]


# --- the acceptance path: a third-party kind + backend through entry points only -----------


def test_third_party_kind_and_backend_pass(install, capsys):
    install()
    code, out, err = run(capsys, "conformance", BACKEND)
    assert (code, err) == (0, "") and out.splitlines() == PASSING


def test_json_document(install, capsys):
    install()
    code, out, err = run(capsys, "conformance", BACKEND, "--json")
    assert (code, err) == (0, "")
    doc = json.loads(out)
    assert sorted(doc) == ["backend", "fixtures", "format", "kind", "ok", "skipped_checks"]
    assert (doc["format"], doc["backend"], doc["kind"], doc["ok"], doc["skipped_checks"]) == ("prov.cli/v1alpha1", BACKEND, SITE, True, 1)
    assert [f["name"] for f in doc["fixtures"]] == ["basic", "cached", "even-replicas", "with-registry"]
    assert all(f["status"] == "ok" for f in doc["fixtures"])
    lines = [f"{c['status']}\t{f['name']}\t{c['check']}\t{c['detail']}" for f in doc["fixtures"] for c in f["checks"]]
    assert lines == PASSING[:-1]  # the same statuses; JSON spells FAIL as "fail"


def test_runs_are_byte_identical(install, capsys):
    install()
    for argv in (("conformance", BACKEND), ("conformance", BACKEND, "--json")):
        assert run(capsys, *argv) == run(capsys, *argv)


def test_fixture_refs_reach_the_backend(install, capsys):
    seen = []

    def render(spec, ctx):
        seen.append({alias: (entry["ref"], entry["kind"], entry["name"], dict(entry["outputs"])) for alias, entry in ctx.refs.items()})
        return site_backend().render(spec, ctx)

    install(backend=site_backend(render=render))
    assert run(capsys, "conformance", BACKEND)[0] == 0
    assert {"registry": ("registry.example.org/registry", "registry.example.org", "registry", {"url": "https://hub.example.org:5000"})} in seen
    assert {"cache": ("cache.example.org/cache", "cache.example.org", "cache", {"size": 1})} in seen
    assert synthetic_ref("x.example.org", "a_b--c_") == "x.example.org/a-b-c"


def test_goss_rendered_plan_printed_nothing_executed(install, capsys, monkeypatch, tmp_path):
    """The backend renders goss.yaml and plans the goss command; conformance runs nothing, connects nowhere."""
    install()
    plans = []
    real_plan = site_backend().plan

    def plan(spec, ctx):
        plans.append(real_plan(spec, ctx))
        return plans[-1]

    install(backend=site_backend(plan=plan))

    def forbidden(*args, **kwargs):
        raise AssertionError(f"prov conformance tried to execute or connect: {args!r}")

    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnve", "posix_spawn", "fork"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 0
    assert plans and all(p[-1] == "goss --gossfile tests/goss.yaml validate" for p in plans)
    assert "goss --gossfile" not in out  # plan lines are checked, never printed by conformance


def test_temp_dir_removed_and_nothing_under_root(install, capsys, monkeypatch, tmp_path):
    install()
    made = []
    real = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    root = tmp_path / "root"
    root.mkdir()
    monkeypatch.chdir(root)
    assert run(capsys, "--root", str(root), "conformance", BACKEND)[0] == 0
    assert len(made) == 1 and not os.path.exists(made[0])
    assert not str(root) in made[0]
    assert list(root.iterdir()) == []


def test_temp_dir_removed_when_a_plugin_fails(install, capsys, monkeypatch):
    made = []
    real = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: made.append(real(*a, **k)) or made[-1])

    def render(spec, ctx):
        raise RuntimeError("boom")

    install(backend=site_backend(render=render))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 1 and "FAIL\tbasic\trender\tbackend 'example-site-goss' failed in render: RuntimeError: boom" in out
    assert made and not os.path.exists(made[0])


# --- failure modes ------------------------------------------------------------------------


def _flaky():
    calls = []

    def render(spec, ctx):
        calls.append(1)
        return {"site.conf": f"host {spec['hostname']}\nrun {len(calls)}\n"}

    return render


def _fixture(name, **expect):
    table = fixtures()
    table[name]["expect"] = {**table[name].get("expect", {}), **expect}
    return table


FAILURES = {
    "nondeterministic": ({}, {"render": _flaky()}, "FAIL\tbasic\trender\tnot deterministic: a second render differs in site.conf"),
    "validate-error": ({}, {"validate": lambda d: [Finding(ERROR, "port out of range", "site.conf")]}, "FAIL\tbasic\tvalidate\terror: site.conf: port out of range"),
    "missing-contains": ({"FIXTURES": _fixture("basic", contains=["nowhere"])}, {}, "FAIL\tbasic\tcontains\tnot in any rendered file: 'nowhere'"),
    "present-absent": ({"FIXTURES": _fixture("basic", absent=["port 8081"])}, {}, "FAIL\tbasic\tabsent\tfound in a rendered file: 'port 8081'"),
    "wrong-outputs": (
        {"FIXTURES": _fixture("basic", outputs={"url": "https://other.example.org"})},
        {},
        "FAIL\tbasic\toutputs\tdiffer from expect.outputs: got {'url': 'https://web.example.org'}, expected {'url': 'https://other.example.org'}",
    ),
    "negative-accepted": ({"semantic": lambda spec: []}, {}, "FAIL\teven-replicas\tspec\taccepted, but the fixture expects it to be rejected (expect.valid = false)"),
    "refs-literal": (
        {"FIXTURES": {**fixtures(), "with-registry": {"spec": {"hostname": "m.example.org"}, "refs": {"registry": {"port": 1}}}}},
        {},
        "FAIL\twith-registry\trefs\tliteral outputs do not match OUTPUTS of kind 'registry.example.org': refs.registry: missing required key 'url'; refs.registry: unknown key 'port'",
    ),
    "plan-refused": ({}, {"plan": lambda spec, ctx: ["echo a\nrm -rf ~"]}, "FAIL\tbasic\tplan\tbackend 'example-site-goss' plan line 0 contains a line break"),
    "invalid-table": ({}, {"FIXTURES": {"basic": {"port": "eighty"}}}, "FAIL\tbasic\tspec\terror: example-site-goss.port: expected integer"),
}


@pytest.mark.parametrize("case", sorted(FAILURES))
def test_failure_modes(install, capsys, case):
    kind_extra, backend_extra, line = FAILURES[case]
    install(kind=site_kind(**kind_extra), backend=site_backend(**backend_extra))
    code, out, err = run(capsys, "conformance", BACKEND)
    assert (code, err) == (1, "")
    assert any(o.startswith(line) for o in out.splitlines()), out
    assert out.splitlines()[-1].startswith(f"conformance {BACKEND} ({SITE}): 4 fixture(s): ")
    assert ", 0 FAIL," not in out.splitlines()[-1]


def test_unsupported_fixture_is_skipped(install, capsys):
    install(backend=site_backend(UNSUPPORTED_FIXTURES={"cached": "this backend has no cache"}))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 0
    assert "skipped\tcached\tsupported\tthe backend cannot express this fixture: this backend has no cache" in out.splitlines()
    assert not any(line.startswith(("ok\tcached", "FAIL\tcached")) for line in out.splitlines())
    assert out.splitlines()[-1].endswith("4 fixture(s): 3 ok, 0 FAIL, 1 skipped; 0 check(s) skipped")
    doc = json.loads(run(capsys, "conformance", BACKEND, "--json")[1])
    assert doc["ok"] is True and next(f for f in doc["fixtures"] if f["name"] == "cached")["status"] == "skipped"


def test_validate_skipped_findings(install, capsys):
    install(backend=site_backend(validate=lambda d: [Finding(SKIPPED, "goss not installed (goss validate)"), Finding(WARNING, "w")]))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 0
    assert "skipped\tbasic\tvalidate\tno errors (1 warning(s)); skipped: goss not installed (goss validate)" in out.splitlines()


def test_missing_table_with_required_schema(install, capsys):
    schema = {"type": "object", "additionalProperties": False, "required": ["port"], "properties": {"port": {"type": "integer"}}}
    install(backend=site_backend(SCHEMA=schema))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 1
    assert "FAIL\tcached\ttable\tno backend table for fixture; the backend's SCHEMA requires port: add FIXTURES['cached'] or UNSUPPORTED_FIXTURES['cached']" in out.splitlines()
    assert not any(line.startswith("FAIL\tbasic") for line in out.splitlines())


def test_temp_path_never_printed(install, capsys):
    install(backend=site_backend(validate=lambda d: [Finding(ERROR, f"bad file {d / 'site.conf'}")]))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 1 and "FAIL\tbasic\tvalidate\terror: bad file <tmp>/site.example.org/basic/rendered/site.conf" in out.splitlines()
    assert tempfile.gettempdir() not in out


# --- exit 2 -------------------------------------------------------------------------------


def test_unknown_backend(capsys):
    code, out, err = run(capsys, "conformance", "no-such\x1b[2J", "--json")
    assert (code, out) == (2, "") and err.startswith("prov: error: unknown backend 'no-such\\x1b[2J'") and clean(err)


def test_third_party_kind_without_fixtures(extra_kinds, extra_backends, capsys):
    extra_kinds(fake_entry_point(fake_kind(), value="prov_registry.kind"))
    extra_backends(fake_entry_point(fake_backend(), value="prov_registry.text"))
    code, out, err = run(capsys, "conformance", "example-registry-text")
    assert (code, out) == (2, "") and "kind registry.example.org has no fixtures" in err


# --- the contracts ------------------------------------------------------------------------


BAD_KIND_FIXTURES = {
    "not a dict": ([], "FIXTURES must be a dict"),
    "bad name": ({"Bad_Name": {"spec": {}}}, "FIXTURES name 'Bad_Name' must match"),
    "no spec": ({"x": {}}, "has no spec"),
    "unknown key": ({"x": {"spec": {}, "extra": 1}}, "unknown key(s) 'extra'"),
    "envelope in spec": ({"x": {"spec": {"backend": "b"}}}, "spec may not hold envelope keys ['backend']"),
    "refs in spec": ({"x": {"spec": {"refs": {}}}}, "spec may not hold envelope keys ['refs']"),
    "not json": ({"x": {"spec": {"a": {1, 2}}}}, "is not plain JSON data"),
    "undeclared alias": ({"x": {"spec": {}, "refs": {"nope": {}}}}, "refs alias 'nope' is not declared in REFS"),
    "refs not outputs": ({"x": {"spec": {}, "refs": {"registry": {"admin_password": "x"}}}}, "looks like a secret value"),
    "bad valid": ({"x": {"spec": {}, "expect": {"valid": "yes"}}}, "expect.valid must be true or false"),
    "negative with contains": ({"x": {"spec": {}, "expect": {"valid": False, "contains": ["a"]}}}, "stops at the spec check"),
    "contains not list": ({"x": {"spec": {}, "expect": {"contains": "a"}}}, "expect.contains must be a list of non-empty strings"),
    "empty absent": ({"x": {"spec": {}, "expect": {"absent": [""]}}}, "expect.absent must be a list of non-empty strings"),
    "unknown expect key": ({"x": {"spec": {}, "expect": {"valid": True, "exit": 0}}}, "expect has unknown key(s) 'exit'"),
    "outputs vs OUTPUTS": ({"x": {"spec": {}, "expect": {"outputs": {"size": 1}}}}, "expect.outputs does not match OUTPUTS"),
}


@pytest.mark.parametrize("case", sorted(BAD_KIND_FIXTURES))
def test_bad_kind_fixtures_are_refused(extra_kinds, capsys, case):
    table, message = BAD_KIND_FIXTURES[case]
    extra_kinds(fake_entry_point(site_kind(FIXTURES=table), value="prov_site.kind"))
    code, out, err = run(capsys, "kinds")
    assert (code, out) == (2, "") and message in err and clean(err), err


def test_expect_outputs_of_a_kind_without_outputs(extra_kinds, capsys):
    kind = fake_kind(FIXTURES={"x": {"spec": {}, "expect": {"outputs": {"a": 1}}}})
    extra_kinds(fake_entry_point(kind, value="prov_registry.kind"))
    code, _, err = run(capsys, "kinds")
    assert code == 2 and "the kind has no OUTPUTS, so its outputs are {}" in err


BAD_BACKEND_FIXTURES = {
    "non-str name": ({"FIXTURES": {1: {}}}, "FIXTURES must be a dict of fixture name -> backend table"),
    "both": ({"UNSUPPORTED_FIXTURES": {"basic": "why"}}, "'basic' in both FIXTURES and UNSUPPORTED_FIXTURES"),
    "table not dict": ({"FIXTURES": {"basic": []}}, "FIXTURES must be a dict of fixture name -> backend table"),
    "empty reason": ({"UNSUPPORTED_FIXTURES": {"cached": " "}}, "UNSUPPORTED_FIXTURES must be a dict of fixture name -> reason"),
    "table not json": ({"FIXTURES": {"basic": {"port": float("nan")}}}, "FIXTURES['basic'] is not plain JSON data"),
}


@pytest.mark.parametrize("case", sorted(BAD_BACKEND_FIXTURES))
def test_bad_backend_fixtures_are_refused(install, capsys, case):
    extra, message = BAD_BACKEND_FIXTURES[case]
    install(backend=site_backend(**extra))
    code, out, err = run(capsys, "backends")
    assert (code, out) == (2, "") and message in err and clean(err), err


def test_unknown_fixture_names_fail_in_conformance_only(install, capsys):
    """A stale fixture name in a backend does not stop it loading: other verbs work, conformance fails."""
    install(backend=site_backend(FIXTURES={"basic": {"port": 8081}, "renamed": {}}, UNSUPPORTED_FIXTURES={"gone\x1b[2J": "old"}))
    assert run(capsys, "backends", "--kind", SITE)[0] == 0
    assert run(capsys, "kinds")[0] == 0 and run(capsys, "schema", SITE)[0] == 0
    code, out, err = run(capsys, "conformance", BACKEND)
    assert (code, err) == (1, "") and clean(out)
    lines = out.splitlines()
    assert lines[-3:] == [
        "FAIL\tgone\\x1b[2J\ttable\tUNSUPPORTED_FIXTURES names unknown fixture 'gone\\x1b[2J' (fixtures of kind site.example.org: basic, cached, even-replicas, with-registry)",
        "FAIL\trenamed\ttable\tFIXTURES names unknown fixture 'renamed' (fixtures of kind site.example.org: basic, cached, even-replicas, with-registry)",
        f"conformance {BACKEND} ({SITE}): 6 fixture(s): 4 ok, 2 FAIL, 0 skipped; 1 check(s) skipped",
    ]
    doc = json.loads(run(capsys, "conformance", BACKEND, "--json")[1])
    assert doc["ok"] is False and doc["fixtures"][-1] == {"name": "renamed", "status": "fail", "checks": [{"check": "table", "status": "fail", "detail": lines[-2].split("\t")[3]}]}


def test_backend_fixtures_of_a_kind_without_fixtures(extra_kinds, extra_backends, capsys):
    extra_kinds(fake_entry_point(fake_kind(), value="prov_registry.kind"))
    extra_backends(fake_entry_point(fake_backend(FIXTURES={"basic": {}}), value="prov_registry.text"))
    assert run(capsys, "backends")[0] == 0
    code, _, err = run(capsys, "conformance", "example-registry-text")
    assert code == 2 and "kind registry.example.org has no fixtures" in err


def test_kinds_and_backends_json_list_fixtures(install, capsys):
    install(backend=site_backend(UNSUPPORTED_FIXTURES={"cached": "no cache"}))
    kind = next(k for k in json.loads(run(capsys, "kinds", "--json")[1])["kinds"] if k["name"] == SITE)
    assert kind["fixtures"] == ["basic", "cached", "even-replicas", "with-registry"]
    backend = next(b for b in json.loads(run(capsys, "backends", "--json")[1])["backends"] if b["name"] == BACKEND)
    assert (backend["fixtures"], backend["unsupported_fixtures"]) == (["basic"], {"cached": "no cache"})
    assert kinds.fixtures(kinds.get(SITE))["basic"]["expect"]["valid"] is True


# --- hostile plugin text ------------------------------------------------------------------


def test_hostile_plugin_text_is_escaped(install, capsys):
    def plan(spec, ctx):
        raise RuntimeError(HOSTILE)

    backend = site_backend(
        validate=lambda d: [Finding(ERROR, f"says {HOSTILE}", HOSTILE)],
        plan=plan,
        UNSUPPORTED_FIXTURES={"cached": f"reason {HOSTILE}"},
    )
    kind = site_kind(FIXTURES=_fixture("basic", contains=[HOSTILE]))
    install(kind=kind, backend=backend)
    code, out, err = run(capsys, "conformance", BACKEND)
    assert code == 1 and err == "" and clean(out)
    assert not any(line.startswith(("forged", "error: forged")) for line in out.splitlines())
    lines = out.splitlines()
    assert all(line.count("\t") == 3 for line in lines[:-1])
    assert "skipped\tcached\tsupported\tthe backend cannot express this fixture: reason ev\\x1b[2Jil\\x0aerror: forged" in out
    code, out, err = run(capsys, "conformance", BACKEND, "--json")
    assert code == 1 and err == "" and all(" " <= ch <= "~" or ch == "\n" for ch in out)
    doc = json.loads(out)
    checks = {(f["name"], c["check"]): c for f in doc["fixtures"] for c in f["checks"]}
    assert checks[("cached", "supported")]["detail"] == f"the backend cannot express this fixture: reason {HOSTILE}"  # raw in JSON
    assert checks[("basic", "validate")]["status"] == "fail"
    assert next(f for f in doc["fixtures"] if f["name"] == "basic")["status"] == "fail"


# --- temporary directory: no leaks, always removed ---------------------------------------


@pytest.fixture
def made_temps(monkeypatch):
    """Record every directory mkdtemp creates."""
    made = []
    real = tempfile.mkdtemp

    def mkdtemp(*args, **kwargs):
        made.append(real(*args, **kwargs))
        return made[-1]

    monkeypatch.setattr(tempfile, "mkdtemp", mkdtemp)
    return made


def test_temp_path_never_printed_through_a_symlinked_tmpdir(install, capsys, monkeypatch, tmp_path, made_temps):
    """TMPDIR through a symlink: the resolved, unresolved and show() forms and the basename all read <tmp>."""
    real = tmp_path / "real-tmp"
    real.mkdir()
    link = tmp_path / "link-tmp"
    link.symlink_to(real)
    monkeypatch.setattr(tempfile, "tempdir", str(link))

    def validate(d):
        unresolved = str(d).replace(str(real), str(link))
        return [Finding(ERROR, f"a {d} b {os.path.realpath(d)} c {unresolved} d {show_name(d)} e {os.path.basename(made_temps[0])}")]

    install(backend=site_backend(validate=validate))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 1
    assert made_temps[0].startswith(str(link)) and not os.path.exists(made_temps[0])
    line = next(line for line in out.splitlines() if line.startswith("FAIL\tbasic\tvalidate"))
    assert line == "FAIL\tbasic\tvalidate\terror: a <tmp>/site.example.org/basic/rendered b <tmp>/site.example.org/basic/rendered c <tmp>/site.example.org/basic/rendered d <tmp>/site.example.org/basic/rendered e <tmp>"
    for leak in (str(real), str(link), "prov-conformance-"):
        assert leak not in out


def show_name(path):
    from prov.text import show

    return show(str(path))


def test_os_error_while_writing_is_a_fail(install, capsys, monkeypatch, made_temps):
    import errno

    import prov.render

    def full(target, text):
        raise OSError(errno.ENOSPC, "No space left on device", str(target))

    monkeypatch.setattr(prov.render, "atomic_write", full)
    install()
    code, out, err = run(capsys, "conformance", BACKEND)
    assert (code, err) == (1, "")
    assert "FAIL\tbasic\twrite\t<tmp>/site.example.org/basic/rendered/site.conf: No space left on device" in out.splitlines()
    assert "prov-conformance-" not in out and not os.path.exists(made_temps[0])


def test_cleanup_when_a_plugin_removes_the_tree(install, capsys, made_temps):
    import shutil

    def validate(d):
        shutil.rmtree(made_temps[0])
        return []

    install(backend=site_backend(validate=validate))
    assert run(capsys, "conformance", BACKEND)[0] == 0
    assert not os.path.exists(made_temps[0])


def test_cleanup_when_a_plugin_locks_a_directory(install, capsys, made_temps):
    def validate(d):
        os.chmod(d / "tests", 0)
        os.chmod(d, 0o500)
        return []

    install(backend=site_backend(validate=validate))
    assert run(capsys, "conformance", BACKEND)[0] == 0
    assert not os.path.exists(made_temps[0])


def test_cleanup_failure_is_exit_2(install, capsys, monkeypatch, made_temps):
    import prov.conformance

    monkeypatch.setattr(prov.conformance, "_remove", lambda tmp: False)
    install()
    code, out, err = run(capsys, "conformance", BACKEND)
    assert (code, out) == (2, "") and "could not remove the temporary directory <tmp>" in err
    assert os.path.basename(made_temps[0]) not in err
    import shutil

    shutil.rmtree(made_temps[0])  # the test's own cleanup: _remove was stubbed out


def test_system_exit_in_a_plugin_is_a_fail_and_cleans_up(install, capsys, made_temps):
    def render(spec, ctx):
        raise SystemExit(3)

    install(backend=site_backend(render=render))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 1 and "FAIL\tbasic\trender\tbackend 'example-site-goss' failed in render: SystemExit: 3" in out
    assert not os.path.exists(made_temps[0])


def test_keyboard_interrupt_propagates_and_cleans_up(install, capsys, monkeypatch, made_temps):
    import prov.conformance

    def validate(d):
        raise KeyboardInterrupt

    def broken_remove(tmp):  # a failing cleanup must not replace the interrupt
        real_remove(tmp)
        raise OSError("cleanup trouble")

    real_remove = prov.conformance._remove
    monkeypatch.setattr(prov.conformance, "_remove", broken_remove)
    install(backend=site_backend(validate=validate))
    with pytest.raises(KeyboardInterrupt):
        main(["conformance", BACKEND])
    assert made_temps and not os.path.exists(made_temps[0])


# --- negative fixtures: rejected for the right reason ------------------------------------


REQUIRED_PORT = {"type": "object", "additionalProperties": False, "required": ["port"], "properties": {"port": {"type": "integer"}}}


def test_negative_fixture_with_a_valid_backend_table_is_rejected_by_the_kind(install, capsys):
    """The backend SCHEMA requires port; the negative fixture's real table is valid, so the kind's semantic decides."""
    tables = {name: {"port": 8081} for name in fixtures()}
    install(backend=site_backend(SCHEMA=REQUIRED_PORT, FIXTURES=tables))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 0 and "ok\teven-replicas\tspec\trejected as expected: error: replicas: replicas must be odd" in out.splitlines()


def test_negative_fixture_not_passed_by_a_backend_table_error(install, capsys):
    """The review's case: the negative fixture's table is invalid and the kind's semantic would accept; the table error does not count."""
    tables = {**{name: {"port": 8081} for name in fixtures()}, "even-replicas": {"port": "eighty"}}
    install(kind=site_kind(semantic=lambda spec: []), backend=site_backend(SCHEMA=REQUIRED_PORT, FIXTURES=tables))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 1
    assert f"FAIL\teven-replicas\tspec\trejected only inside the backend table '{BACKEND}', which does not count: error: {BACKEND}.port: expected integer" in [
        line[: line.index("integer") + 7] for line in out.splitlines() if "integer" in line
    ]


def test_negative_fixture_without_a_table_needs_one_when_the_schema_requires_keys(install, capsys):
    tables = {name: {"port": 8081} for name in ("basic", "cached", "with-registry")}
    install(backend=site_backend(SCHEMA=REQUIRED_PORT, FIXTURES=tables))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 1
    assert any(line.startswith("FAIL\teven-replicas\ttable\tno backend table for fixture; the backend's SCHEMA requires port") for line in out.splitlines())


def test_negative_fixture_rejected_only_inside_the_table(install, capsys):
    install(kind=site_kind(semantic=lambda spec: []), backend=site_backend(check=lambda spec, ctx: [Finding(ERROR, "port refused", f"{BACKEND}.port")] if spec["replicas"] == 2 else []))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 1
    assert f"FAIL\teven-replicas\tspec\trejected only inside the backend table '{BACKEND}', which does not count: error: {BACKEND}.port: port refused" in out.splitlines()


@pytest.mark.parametrize("wanted, status", [("must be odd", "ok"), ("must be prime", "FAIL")])
def test_expect_error(install, capsys, wanted, status):
    table = fixtures()
    table["even-replicas"]["expect"]["error"] = wanted
    install(kind=site_kind(FIXTURES=table))
    code, out, _ = run(capsys, "conformance", BACKEND)
    line = next(line for line in out.splitlines() if line.startswith(("ok\teven-replicas", "FAIL\teven-replicas")))
    assert line.startswith(f"{status}\teven-replicas\tspec\t") and code == (0 if status == "ok" else 1)
    if status == "FAIL":
        assert "rejected, but no error contains expect.error 'must be prime': error: replicas: replicas must be odd" in line


@pytest.mark.parametrize("expect", [{"valid": True, "error": "x"}, {"valid": False, "error": ""}, {"valid": False, "error": 1}])
def test_bad_expect_error_is_refused(extra_kinds, capsys, expect):
    extra_kinds(fake_entry_point(site_kind(FIXTURES={"x": {"spec": {}, "expect": expect}}), value="prov_site.kind"))
    code, _, err = run(capsys, "kinds")
    assert code == 2 and "expect.error is a non-empty string, only for a fixture with expect.valid = false" in err


# --- smaller cases ------------------------------------------------------------------------


def test_all_fixtures_unsupported_exits_0(install, capsys):
    reasons = {name: "not this one" for name in fixtures()}
    install(backend=site_backend(FIXTURES={}, UNSUPPORTED_FIXTURES=reasons))
    code, out, _ = run(capsys, "conformance", BACKEND)
    assert code == 0 and out.splitlines()[-1] == f"conformance {BACKEND} ({SITE}): 4 fixture(s): 0 ok, 0 FAIL, 4 skipped; 0 check(s) skipped"


def test_synthetic_ref_collisions():
    assert synthetic_ref("x.example.org", "a_b") == "x.example.org/a-b"
    assert synthetic_ref("x.example.org", "a-b", frozenset({"x.example.org/a-b"})) == "x.example.org/a-b-2"
    assert synthetic_ref("x.example.org", "a-b", frozenset({"x.example.org/a-b", "x.example.org/a-b-2"})) == "x.example.org/a-b-3"
    long = "a" * 63
    ref = synthetic_ref("x.example.org", long, frozenset({f"x.example.org/{long}"}))
    assert ref == f"x.example.org/{'a' * 61}-2"


# --- prov conformance --export KIND DIR ---------------------------------------------------


def _export_file(directory):
    return directory / f"{SITE}.v1alpha1.fixtures.json"


def test_export_writes_the_kind_and_its_installed_backends(install, capsys, tmp_path):
    install()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    code, out, err = run(capsys, "--root", str(tmp_path / "root"), "conformance", "--export", SITE, str(out_dir))
    target = _export_file(out_dir)
    assert (code, out, err) == (0, f"wrote {target}\n", "")
    text = target.read_text(encoding="utf-8")
    assert text == json_text(export_fixtures(kinds.get(SITE), for_kind(discover(), SITE)))
    doc = json.loads(text)
    assert (doc["format"], doc["kind"], doc["api_version"]) == ("prov.conformance/v1alpha1", SITE, "example.org/v1alpha1")
    entry = doc["backends"][BACKEND]
    assert entry["fixtures"] == {"basic": {"port": 8081}} and entry["unsupported"] == {}
    assert entry["specs"]["with-registry"]["refs"] == {"registry": "registry.example.org/registry"}
    assert entry["specs"]["basic"] == {"api_version": "example.org/v1alpha1", "kind": SITE, "backend": BACKEND, "hostname": "web.example.org", BACKEND: {"port": 8081}}
    assert run(capsys, "conformance", "--export", SITE, str(out_dir))[0] == 0  # replaces it
    assert target.read_text(encoding="utf-8") == text
    assert sorted(p.name for p in out_dir.iterdir()) == [target.name]  # no temp file left


def test_export_skips_unsupported_specs(install, capsys, tmp_path):
    install(backend=site_backend(UNSUPPORTED_FIXTURES={"cached": "no cache"}))
    assert run(capsys, "conformance", "--export", SITE, str(tmp_path))[0] == 0
    entry = json.loads(_export_file(tmp_path).read_text(encoding="utf-8"))["backends"][BACKEND]
    assert "cached" not in entry["specs"] and entry["unsupported"] == {"cached": "no cache"}


def test_export_unknown_kind_and_kind_without_fixtures(extra_kinds, capsys, tmp_path):
    code, out, err = run(capsys, "conformance", "--export", "no-such\x1b[2J", str(tmp_path))
    assert (code, out) == (2, "") and err.startswith("prov: error: unknown kind 'no-such\\x1b[2J'") and clean(err)
    extra_kinds(fake_entry_point(fake_kind(), value="prov_registry.kind"))
    code, out, err = run(capsys, "conformance", "--export", "registry.example.org", str(tmp_path))
    assert (code, out) == (2, "") and "kind registry.example.org has no fixtures (FIXTURES); nothing to export" in err
    assert list(tmp_path.iterdir()) == []


def test_export_refuses_bad_directories(install, capsys, tmp_path):
    install()
    root = tmp_path / "root"
    (root / SITE / "web").mkdir(parents=True)
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    (tmp_path / "into-tree").symlink_to(root / SITE, target_is_directory=True)
    (tmp_path / "file").write_text("x", encoding="utf-8")
    cases = {
        str(tmp_path / "missing\x1b[2J"): "not an existing directory",
        str(tmp_path / "file"): "not an existing directory",
        str(tmp_path / "link"): "is a symlink",
        str(root / SITE): f"inside the item tree <root>/{SITE}/",
        str(root / SITE / "web"): f"inside the item tree <root>/{SITE}/",
        str(tmp_path / "into-tree" / "web"): f"inside the item tree <root>/{SITE}/",
    }
    for directory, message in cases.items():
        code, out, err = run(capsys, "--root", str(root), "conformance", "--export", SITE, directory)
        assert (code, out) == (2, "") and message in err and clean(err), directory
    assert list(real.iterdir()) == [] and sorted(p.name for p in (root / SITE).iterdir()) == ["web"]
    assert run(capsys, "--root", str(root), "conformance", "--export", SITE, str(root))[0] == 0  # the root itself is outside <root>/<kind>/


def test_export_never_writes_through_a_symlinked_target(install, capsys, tmp_path):
    install()
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    _export_file(out_dir).symlink_to(victim)
    code, out, err = run(capsys, "conformance", "--export", SITE, str(out_dir))
    assert (code, out) == (2, "") and "is a symlink; not written through" in err
    assert victim.read_text(encoding="utf-8") == "keep" and _export_file(out_dir).is_symlink()
    _export_file(out_dir).unlink()
    _export_file(out_dir).mkdir()
    code, _, err = run(capsys, "conformance", "--export", SITE, str(out_dir))
    assert code == 2 and "exists and is not a regular file" in err


@pytest.mark.parametrize(
    "argv, message",
    [
        (["conformance"], "conformance needs a backend NAME (or --export KIND DIR)"),
        (["conformance", BACKEND, "--export", SITE, "."], "takes no backend and no --json"),
        (["conformance", "--json", "--export", SITE, "."], "takes no backend and no --json"),
    ],
)
def test_export_usage_errors(install, capsys, argv, message):
    install()
    code, out, err = run(capsys, *argv)
    assert (code, out) == (2, "") and message in err
