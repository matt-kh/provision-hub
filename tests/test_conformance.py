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
from prov.conformance import synthetic_ref
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
    "ok\tbasic\tplan\t5 line(s), for a human to run; prov never runs them",
    "skipped\tcached\trefs\trefs.cache: kind 'cache.example.org' is not installed; its literal outputs were checked as JSON data only",
    "ok\tcached\tspec\tvalid",
    "ok\tcached\toutputs\t1 output(s); no expect.outputs to compare",
    "ok\tcached\trender\t2 file(s), equal on a second render",
    "ok\tcached\twrite\t2 change(s) in a fresh temporary rendered/; diff empty; a second write makes 0 changes",
    "ok\tcached\tvalidate\tno errors",
    "ok\tcached\tplan\t5 line(s), for a human to run; prov never runs them",
    "ok\teven-replicas\tspec\trejected as expected: error: replicas: replicas must be odd",
    "ok\twith-registry\trefs\t1 reference(s) match their target kinds' OUTPUTS",
    "ok\twith-registry\tspec\tvalid",
    "ok\twith-registry\toutputs\t1 output(s); no expect.outputs to compare",
    "ok\twith-registry\trender\t2 file(s), equal on a second render",
    "ok\twith-registry\twrite\t2 change(s) in a fresh temporary rendered/; diff empty; a second write makes 0 changes",
    "ok\twith-registry\tvalidate\tno errors",
    "ok\twith-registry\tcontains\t1 string(s) found",
    "ok\twith-registry\tplan\t5 line(s), for a human to run; prov never runs them",
    f"conformance {BACKEND} ({SITE}): 4 fixture(s): 4 ok, 0 FAIL, 0 skipped",
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
    assert sorted(doc) == ["backend", "fixtures", "format", "kind", "ok"]
    assert (doc["format"], doc["backend"], doc["kind"], doc["ok"]) == ("prov.cli/v1alpha1", BACKEND, SITE, True)
    assert [f["name"] for f in doc["fixtures"]] == ["basic", "cached", "even-replicas", "with-registry"]
    assert all(f["status"] == "ok" for f in doc["fixtures"])
    lines = [f"{c['status']}\t{f['name']}\t{c['check']}\t{c['detail']}" for f in doc["fixtures"] for c in f["checks"]]
    assert lines == PASSING[:-1]


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
    assert out.splitlines()[-1].endswith("4 fixture(s): 3 ok, 0 FAIL, 1 skipped")
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


def test_builtin_backend_kind_without_fixtures(capsys):
    assert run(capsys, "conformance", "ubuntu-autoinstall") == (2, "", "prov: error: kind os-install has no fixtures (FIXTURES); nothing to check for backend ubuntu-autoinstall\n")


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
    "unknown fixture": ({"FIXTURES": {"nope": {}}}, "FIXTURES names 'nope', not a fixture of kind 'site.example.org' (fixtures: basic, cached, even-replicas, with-registry)"),
    "unknown unsupported": ({"UNSUPPORTED_FIXTURES": {"nope": "why"}}, "UNSUPPORTED_FIXTURES names 'nope'"),
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


def test_backend_fixtures_of_a_kind_without_fixtures(extra_kinds, extra_backends, capsys):
    extra_kinds(fake_entry_point(fake_kind(), value="prov_registry.kind"))
    extra_backends(fake_entry_point(fake_backend(FIXTURES={"basic": {}}), value="prov_registry.text"))
    code, _, err = run(capsys, "backends")
    assert code == 2 and "fixtures: none: kind 'registry.example.org' declares no FIXTURES" in err


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
    assert checks[("basic", "validate")]["status"] == "FAIL"
