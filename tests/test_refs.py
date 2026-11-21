"""References: [refs] in a spec, REFS on a kind, resolution into Context.refs.

Two third-party kinds rendered by two different backends, installed only through fake
entry points: registry.example.org (exports a url) and mirror.example.org, whose items refer
to a registry (``upstream``) and optionally to other mirrors (``peer``, ``backup``).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import types

import jsonschema
import pytest
from conftest import fake_entry_point
from test_kind_plugins import BACKEND, KIND, fake_backend, fake_kind

from prov import kinds
from prov.cli import main
from prov.kinds.common import DEFS
from prov.refs import MAX_DEPTH, freeze, resolve
from prov.registry import check_contract, discover, for_kind
from prov.schema import ConfigError, read_toml, spec_schema
from prov.spec import check_spec

MIRROR = "mirror.example.org"
MIRROR_BACKEND = "example-mirror-text"
ABSENT = "cache.example.org"  # a kind no plugin installs
HUB = f"{KIND}/hub"
HOSTILE = "ev\x1b[2Jil\nerror: forged‮"

OUTPUTS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url"],
    "properties": {"url": {"type": "string"}, "tags": {"type": "array", "items": {"type": "string"}}, "meta": {"type": "object"}},
}

MIRROR_REFS = {
    "upstream": {"kind": KIND, "required": True, "description": "The registry this mirror pulls from."},
    "peer": {"kind": MIRROR, "required": False, "description": "Another mirror to sync with."},
    "backup": {"kind": MIRROR, "required": False, "description": "A fallback mirror."},
    "cache": {"kind": ABSENT, "required": False, "description": "A cache of a kind that is not installed."},
}


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def clean(text):
    return all(ch.isprintable() or ch in "\n\t" for ch in text)


def registry_kind(calls=None):
    def outputs(spec):
        if calls is not None:
            calls.append(spec["hostname"])
        return {"url": f"https://{spec['hostname']}:{spec[BACKEND]['port']}", "tags": ["a", "b"], "meta": {"n": spec["replicas"]}}

    return fake_kind(OUTPUTS=OUTPUTS, outputs=outputs)


def mirror_kind(**extra):
    kind = types.ModuleType(MIRROR)
    kind.__doc__ = "A test-only kind: a registry mirror."
    kind.NAME = MIRROR
    kind.PLUGIN_API = 1
    kind.VERSIONS = ("v1alpha1",)
    kind.PROPERTIES = {"hostname": {"type": "string", "pattern": "^[a-z0-9.-]+$"}}
    kind.REQUIRED = ["hostname"]
    kind.REFS = MIRROR_REFS
    kind.semantic = lambda spec: []
    for key, value in extra.items():
        setattr(kind, key, value)
    return kind


def mirror_backend(seen=None, **extra):
    mod = types.ModuleType(MIRROR_BACKEND)
    mod.__doc__ = "Renders a mirror as a text file."
    mod.NAME = MIRROR_BACKEND
    mod.KIND = MIRROR
    mod.SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}}

    def check(spec, ctx):
        if seen is not None:
            seen.append(ctx)
        return []

    mod.check = check
    mod.render = lambda spec, ctx: {"mirror.txt": f"{spec['hostname']} mirrors {ctx.refs['upstream']['outputs']['url']}\n"}
    mod.validate = lambda rendered_dir: []
    mod.plan = lambda spec, ctx: [f"# pulls from {ctx.refs['upstream']['ref']}", "cat mirror.txt"]
    mod.starter = lambda item: mirror_toml(item)
    for key, value in extra.items():
        setattr(mod, key, value)
    return mod


def mirror_toml(name, **refs):
    refs.setdefault("upstream", HUB)
    table = "".join(f'{alias} = "{value}"\n' for alias, value in refs.items() if value is not None)
    return (
        f'api_version = "example.org/v1alpha1"\nkind = "{MIRROR}"\nbackend = "{MIRROR_BACKEND}"\n'
        f'hostname = "{name}.example.org"\n\n[refs]\n{table}\n[{MIRROR_BACKEND}]\n'
    )


@pytest.fixture
def setup(tmp_path, extra_kinds, extra_backends):
    """setup(registry=..., mirror=..., backend=...) installs both kinds and backends and writes registry hub."""

    def go(registry=None, mirror=None, backend=None):
        extra_kinds(fake_entry_point(registry or registry_kind(), value="prov_registry.kind"), fake_entry_point(mirror or mirror_kind(), value="prov_mirror.kind"))
        extra_backends(fake_entry_point(fake_backend(), value="prov_registry.text"), fake_entry_point(backend or mirror_backend(), value="prov_mirror.text"))
        write(tmp_path, HUB, fake_backend().starter("hub"))
        return str(tmp_path)

    return go


def write(root, ref, text):
    item = root / ref
    item.mkdir(parents=True, exist_ok=True)
    (item / "spec.toml").write_text(text)
    return item / "spec.toml"


# --- the happy path -----------------------------------------------------------------------


def test_consumer_renders_a_target_output(setup, tmp_path, capsys):
    root = setup()
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1"))
    code, out, err = run(capsys, "--root", root, "render", f"{MIRROR}/m1")
    assert (code, err) == (0, "") and "1 change(s)" in out
    rendered = tmp_path / MIRROR / "m1/rendered"
    first = {p.name: p.read_bytes() for p in rendered.iterdir()}
    assert first == {"mirror.txt": b"m1.example.org mirrors https://hub.example.org:5001\n"}
    code, out, _ = run(capsys, "--root", root, "render", f"{MIRROR}/m1")
    assert code == 0 and "0 change(s)" in out
    assert {p.name: p.read_bytes() for p in rendered.iterdir()} == first
    assert run(capsys, "--root", root, "diff", f"{MIRROR}/m1") == (0, f"{MIRROR}/m1: rendered output is up to date\n", "")
    assert run(capsys, "--root", root, "validate", f"{MIRROR}/m1") == (0, f"ok: {MIRROR}/m1\n", "")
    code, out, _ = run(capsys, "--root", root, "plan", f"{MIRROR}/m1")
    assert code == 0 and f"# pulls from {HUB}" in out.splitlines()
    doc = json.loads(run(capsys, "--root", root, "validate", f"{MIRROR}/m1", "--json")[1])
    assert doc["ok"] is True and doc["refs"] == {"upstream": {"ref": HUB, "kind": KIND}} and doc["outputs"] == {}
    hub = json.loads(run(capsys, "--root", root, "validate", HUB, "--json")[1])
    assert hub["refs"] == {}  # a kind without REFS: empty


def test_target_change_makes_the_consumer_out_of_date(setup, tmp_path, capsys):
    root = setup()
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1"))
    assert run(capsys, "--root", root, "render", f"{MIRROR}/m1")[0] == 0
    hub = tmp_path / HUB / "spec.toml"
    hub.write_text(hub.read_text().replace("port = 5001", "port = 5002"))
    code, out, _ = run(capsys, "--root", root, "diff", f"{MIRROR}/m1")
    assert code == 1 and "+m1.example.org mirrors https://hub.example.org:5002" in out
    code, _, err = run(capsys, "--root", root, "validate", f"{MIRROR}/m1")
    assert code == 1 and "error: rendered: out of date with spec.toml" in err


def test_backend_gets_read_only_refs(setup, tmp_path, capsys):
    seen = []
    root = setup(backend=mirror_backend(seen))
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1"))
    assert run(capsys, "--root", root, "validate", f"{MIRROR}/m1")[0] == 1  # not rendered yet
    ctx = seen[-1]
    assert ctx.name == "m1" and list(ctx.refs) == ["upstream"]
    entry = ctx.refs["upstream"]
    assert dict(entry) == {"ref": HUB, "kind": KIND, "name": "hub", "outputs": entry["outputs"]}
    assert entry["outputs"]["tags"] == ("a", "b") and entry["outputs"]["meta"]["n"] == 1
    with pytest.raises(TypeError):
        ctx.refs["upstream"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        entry["outputs"]["url"] = "x"  # type: ignore[index]
    with pytest.raises(TypeError):
        entry["outputs"]["meta"]["n"] = 2  # type: ignore[index]
    with pytest.raises(AttributeError):
        entry["outputs"]["tags"].append("c")  # type: ignore[attr-defined]


def test_target_kind_without_outputs_exports_nothing(setup, tmp_path, capsys):
    root = setup(registry=fake_kind(), backend=mirror_backend(render=lambda spec, ctx: {"o.txt": json.dumps(dict(ctx.refs["upstream"]["outputs"]))}))
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1"))
    assert run(capsys, "--root", root, "render", f"{MIRROR}/m1")[0] == 0
    assert (tmp_path / MIRROR / "m1/rendered/o.txt").read_text() == "{}"


def test_diamond_reads_each_target_once(setup, tmp_path, capsys):
    calls = []
    root = setup(registry=registry_kind(calls))
    write(tmp_path, f"{MIRROR}/a", mirror_toml("a", peer=f"{MIRROR}/b", backup=f"{MIRROR}/c"))
    write(tmp_path, f"{MIRROR}/b", mirror_toml("b", peer=f"{MIRROR}/d"))
    write(tmp_path, f"{MIRROR}/c", mirror_toml("c", peer=f"{MIRROR}/d"))
    write(tmp_path, f"{MIRROR}/d", mirror_toml("d"))
    calls.clear()
    assert run(capsys, "--root", root, "render", f"{MIRROR}/a")[0] == 0
    assert calls == ["hub.example.org"]  # hub reached four ways, checked once


def test_resolve_needs_no_subprocess_or_network(setup, tmp_path, capsys, monkeypatch):
    root = setup()
    write(tmp_path, f"{MIRROR}/a", mirror_toml("a", peer=f"{MIRROR}/b"))
    write(tmp_path, f"{MIRROR}/b", mirror_toml("b"))

    def forbidden(*args, **kwargs):
        raise AssertionError(f"reference resolution tried to execute or connect: {args!r}")

    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        monkeypatch.setattr(subprocess, name, forbidden)
    for name in ("system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnve", "posix_spawn", "fork"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, forbidden)
    for name in ("socket", "create_connection", "getaddrinfo"):
        monkeypatch.setattr(socket, name, forbidden)
    for verb in ("render", "diff", "validate", "plan"):
        assert run(capsys, "--root", root, verb, f"{MIRROR}/a")[0] == 0, verb


def test_resolve_api(setup, tmp_path):
    setup()
    spec = {"refs": {"upstream": HUB}}
    refs, findings = resolve(tmp_path, MIRROR, "m1", spec, discover())
    assert findings == [] and refs["upstream"]["outputs"]["url"] == "https://hub.example.org:5001"
    with pytest.raises(TypeError):
        refs["x"] = 1  # type: ignore[index]
    assert freeze({"a": [1, {"b": [2]}]})["a"][1]["b"] == (2,)


# --- errors: validate exits 1, render / diff / plan exit 2 ---------------------------------

ERRORS = {
    "missing_target": ({"upstream": f"{KIND}/nothere"}, f"error: refs.upstream: invalid target: {KIND}/nothere: no such item (no {KIND}/nothere/spec.toml under the root)"),
    "kind_not_installed": ({"cache": f"{ABSENT}/x"}, f"error: refs.cache: {ABSENT}/x: kind '{ABSENT}' is not installed"),
    "wrong_kind": ({"upstream": f"{MIRROR}/m1"}, f"error: refs.upstream: '{MIRROR}/m1' does not match"),
    "unknown_alias": ({"other": HUB}, "error: refs: unknown key 'other'"),
    "self_reference": ({"peer": f"{MIRROR}/m1"}, f"error: refs.peer: {MIRROR}/m1 refers to the item itself"),
    "invalid_ref": ({"upstream": f"{KIND}/../x"}, "error: refs.upstream: "),
}


@pytest.mark.parametrize("case", sorted(ERRORS))
def test_reference_errors_on_every_verb(setup, tmp_path, capsys, case):
    refs, needle = ERRORS[case]
    root = setup()
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1", **refs))
    check_every_verb(capsys, root, f"{MIRROR}/m1", needle)


def check_every_verb(capsys, root, ref, needle):
    code, out, err = run(capsys, "--root", root, "validate", ref)
    assert (code, out) == (1, ""), (code, out, err)
    assert needle in err and clean(err) and "Traceback" not in err, err
    doc = json.loads(run(capsys, "--root", root, "validate", ref, "--json")[1])
    assert doc["ok"] is False and doc["refs"] is None and doc["outputs"] is None
    for verb in ("render", "diff", "plan"):
        code, out, err = run(capsys, "--root", root, verb, ref)
        assert (code, out) == (2, ""), (verb, out, err)
        assert "invalid spec:" in err and needle in err and clean(err) and "Traceback" not in err, (verb, err)
    assert not (root_path(root) / ref / "rendered").exists()  # nothing rendered


def root_path(root):
    from pathlib import Path

    return Path(root)


def test_required_reference_missing(setup, tmp_path, capsys):
    root = setup()
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1", upstream=None))
    check_every_verb(capsys, root, f"{MIRROR}/m1", "error: refs: missing required key 'upstream'")
    spec = tmp_path / MIRROR / "m1/spec.toml"
    spec.write_text(spec.read_text().replace("[refs]\n", ""))
    check_every_verb(capsys, root, f"{MIRROR}/m1", "error: <root>: missing required key 'refs'")


@pytest.mark.parametrize(
    "mutate, needle",
    [
        (lambda t: t.replace('hostname = "hub.example.org"', 'hostname = "hub.example.org"\nreplicas = 2'), f"invalid target: {HUB}: replicas: replicas must be odd"),
        (lambda t: t.replace(f'backend = "{BACKEND}"', 'backend = "nope"'), f"invalid target: {HUB}: spec.toml: unknown backend 'nope'"),
        (lambda t: t + "[[[", f"invalid target: {HUB}: spec.toml: invalid TOML"),
        (lambda t: t.replace('hostname = "hub.example.org"\n', ""), f"invalid target: {HUB}: <root>: missing required key 'hostname'"),
    ],
    ids=["semantic", "envelope", "toml", "schema"],
)
def test_invalid_target(setup, tmp_path, capsys, mutate, needle):
    root = setup()
    hub = tmp_path / HUB / "spec.toml"
    hub.write_text(mutate(hub.read_text()))
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1"))
    check_every_verb(capsys, root, f"{MIRROR}/m1", needle)


def test_invalid_target_through_a_chain(setup, tmp_path, capsys):
    root = setup()
    write(tmp_path, f"{MIRROR}/a", mirror_toml("a", peer=f"{MIRROR}/b"))
    write(tmp_path, f"{MIRROR}/b", mirror_toml("b", peer=f"{MIRROR}/c"))
    write(tmp_path, f"{MIRROR}/c", mirror_toml("c", upstream=f"{KIND}/gone"))
    check_every_verb(capsys, root, f"{MIRROR}/a", f"refs.peer: invalid target: {MIRROR}/b -> {MIRROR}/c -> {KIND}/gone: no such item")


def test_cycle(setup, tmp_path, capsys):
    root = setup()
    write(tmp_path, f"{MIRROR}/a", mirror_toml("a", peer=f"{MIRROR}/b"))
    write(tmp_path, f"{MIRROR}/b", mirror_toml("b", peer=f"{MIRROR}/c"))
    write(tmp_path, f"{MIRROR}/c", mirror_toml("c", peer=f"{MIRROR}/a"))
    cycle = f"reference cycle: {MIRROR}/a -> {MIRROR}/b -> {MIRROR}/c -> {MIRROR}/a"
    check_every_verb(capsys, root, f"{MIRROR}/a", cycle)
    check_every_verb(capsys, root, f"{MIRROR}/b", f"reference cycle: {MIRROR}/b -> {MIRROR}/c -> {MIRROR}/a -> {MIRROR}/b")


def test_depth_limit(setup, tmp_path, capsys):
    root = setup()
    n = MAX_DEPTH + 2
    for i in range(n):
        write(tmp_path, f"{MIRROR}/m{i}", mirror_toml(f"m{i}", **({"peer": f"{MIRROR}/m{i + 1}"} if i + 1 < n else {})))
    check_every_verb(capsys, root, f"{MIRROR}/m0", f"more than {MAX_DEPTH} references in a row ({MIRROR}/m0 -> ... -> {MIRROR}/m{MAX_DEPTH + 1})")
    # the same chain, entered closer to its end, is fine
    assert run(capsys, "--root", root, "render", f"{MIRROR}/m2")[0] == 0


def test_depth_limit_through_a_checked_target(setup, tmp_path, capsys):
    """A target checked earlier in the run (backup, shallow) and reached again deeper still counts its height."""
    root = setup()
    n = MAX_DEPTH
    for i in range(1, n + 1):
        write(tmp_path, f"{MIRROR}/m{i}", mirror_toml(f"m{i}", **({"peer": f"{MIRROR}/m{i + 1}"} if i < n else {})))
    # top -> backup m1 (height 31: 32 hops in all, fine); top -> peer x -> m1 makes 33
    write(tmp_path, f"{MIRROR}/x", mirror_toml("x", peer=f"{MIRROR}/m1"))
    write(tmp_path, f"{MIRROR}/top", mirror_toml("top", backup=f"{MIRROR}/m1", peer=f"{MIRROR}/x"))
    code, _, err = run(capsys, "--root", root, "validate", f"{MIRROR}/top")
    assert code == 1 and f"more than {MAX_DEPTH} references in a row" in err, err


# --- symlinks: never followed ---------------------------------------------------------------


@pytest.mark.parametrize("where", ["kind_dir", "item_dir", "spec_file"])
def test_symlinked_targets_are_refused(setup, tmp_path, capsys, where):
    root = setup()
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    write(outside, HUB, fake_backend().starter("evil").replace("evil.example.org", "evil-outside.example.org"))
    hub = tmp_path / HUB
    for p in sorted(hub.rglob("*"), reverse=True):
        p.unlink()
    hub.rmdir()
    if where == "kind_dir":
        (tmp_path / KIND).rmdir()
        os.symlink(outside / KIND, tmp_path / KIND)
    elif where == "item_dir":
        os.symlink(outside / HUB, hub)
    else:
        hub.mkdir()
        os.symlink(outside / HUB / "spec.toml", hub / "spec.toml")
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1"))
    needle = "not a regular file; symlinks and special files are refused" if where == "spec_file" else "is a symlink"
    check_every_verb(capsys, root, f"{MIRROR}/m1", needle)
    code, _, err = run(capsys, "--root", root, "validate", f"{MIRROR}/m1")
    assert "evil-outside" not in err


# --- hostile text ----------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["alias", "ref"])
def test_hostile_aliases_and_refs_are_escaped(setup, tmp_path, capsys, which):
    root = setup()
    text = mirror_toml("m1")
    if which == "alias":
        text = text.replace("[refs]\n", f'[refs]\n"{HOSTILE.replace(chr(10), chr(92) + "n").replace(chr(27), chr(92) + "u001b")}" = "{HUB}"\n')
    else:
        text = text.replace(f'upstream = "{HUB}"', 'upstream = "a\\u001b[2J/b\\nerror: forged\\u202e"')
    write(tmp_path, f"{MIRROR}/m1", text)
    for verb in ("validate", "render", "diff", "plan"):
        code, out, err = run(capsys, "--root", root, verb, f"{MIRROR}/m1")
        assert code in (1, 2) and clean(out) and clean(err) and "Traceback" not in err, (verb, err)
        assert not any(line.startswith(("forged", "error: forged")) for line in (out + err).splitlines()), (verb, err)
        assert "\\x1b[2J" in err if which == "alias" else "control characters" in err
    code, out, _ = run(capsys, "--root", root, "validate", f"{MIRROR}/m1", "--json")
    assert code == 1 and all(" " <= ch <= "~" or ch == "\n" for ch in out)


def test_hostile_target_text_is_escaped(setup, tmp_path, capsys):
    def semantic(spec):
        from prov.validate import ERROR, Finding

        return [Finding(ERROR, f"bad {HOSTILE}", f"w{HOSTILE}")]

    root = setup(registry=fake_kind(semantic=semantic))
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1"))
    for verb in ("validate", "render"):
        code, out, err = run(capsys, "--root", root, verb, f"{MIRROR}/m1")
        assert code in (1, 2) and clean(err) and "bad ev\\x1b[2Jil\\x0aerror: forged\\u202e" in err, err
        assert not any(line.startswith(("forged", "error: forged")) for line in err.splitlines())


def test_broken_target_plugin_is_exit_2(setup, tmp_path, capsys):
    def outputs(spec):
        raise RuntimeError(HOSTILE)

    root = setup(registry=fake_kind(OUTPUTS=OUTPUTS, outputs=outputs))
    write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1"))
    for verb in ("validate", "render"):
        code, out, err = run(capsys, "--root", root, verb, f"{MIRROR}/m1")
        assert (code, out) == (2, "") and f"kind '{KIND}' failed in outputs: RuntimeError: ev\\x1b[2Jil" in err
        assert err.count("\n") == 1 and clean(err)


# --- the spec side: schema, reserved names ----------------------------------------------------


def test_refs_schema(setup, capsys):
    setup()
    schema = json.loads(run(capsys, "schema", MIRROR)[1])
    jsonschema.Draft202012Validator.check_schema(schema)
    refs = schema["properties"]["refs"]
    assert refs["additionalProperties"] is False and refs["required"] == ["upstream"]
    assert "refs" in schema["required"]
    assert refs["properties"]["upstream"]["pattern"] == "^registry\\.example\\.org/[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
    assert refs["properties"]["peer"]["description"] == "Another mirror to sync with."
    good = {"api_version": "example.org/v1alpha1", "kind": MIRROR, "backend": MIRROR_BACKEND, "hostname": "m", "refs": {"upstream": HUB}, MIRROR_BACKEND: {}}
    validator = jsonschema.Draft202012Validator(schema)
    validator.validate(good)
    assert not validator.is_valid({**good, "refs": {"upstream": "registryxexample.org/hub"}})  # the dots are literal
    assert not validator.is_valid({**good, "refs": {"upstream": f"{MIRROR}/x"}})
    assert not validator.is_valid({**good, "refs": {}})


def test_kinds_without_refs_have_no_refs_table(setup, tmp_path, capsys):
    root = setup()
    assert "refs" not in json.loads(run(capsys, "schema", KIND)[1])["properties"]
    assert "refs" not in spec_schema(kinds.get("os-install"), for_kind(discover(), "os-install"), DEFS)["properties"]
    hub = tmp_path / HUB / "spec.toml"
    hub.write_text(hub.read_text().replace(f"[{BACKEND}]", f'[refs]\nx = "{HUB}"\n\n[{BACKEND}]'))
    code, _, err = run(capsys, "--root", root, "validate", HUB)
    assert code == 1 and "error: <root>: unknown key 'refs'" in err


def test_kinds_json_lists_refs(setup, capsys):
    setup()
    doc = json.loads(run(capsys, "kinds", "--json")[1])
    by_name = {k["name"]: k for k in doc["kinds"]}
    assert by_name[MIRROR]["refs"] == MIRROR_REFS and by_name[KIND]["refs"] == {} and by_name["os-install"]["refs"] == {}


def test_without_a_root_refs_are_syntax_only(setup, tmp_path, capsys):
    seen = []
    setup(backend=mirror_backend(seen))
    raw = read_toml(write(tmp_path, f"{MIRROR}/m1", mirror_toml("m1", upstream=f"{KIND}/nothere")))
    spec, findings = check_spec(raw, MIRROR, "m1", discover())
    assert spec is not None and findings == [] and seen == []  # not resolved, backend check skipped
    spec, findings = check_spec(raw, MIRROR, "m1", discover(), root=tmp_path)
    assert spec is None and findings[0].where == "refs.upstream"
    # init writes a starter with references without resolving them
    code, _, err = run(capsys, "--root", str(tmp_path), "init", f"{MIRROR}/fresh", "--backend", MIRROR_BACKEND)
    assert (code, err) == (0, "") and seen == []


# --- the kind and backend contracts ------------------------------------------------------------


@pytest.mark.parametrize(
    "refs, needle",
    [
        ({"Bad": {"kind": KIND, "required": True, "description": ""}}, "REFS alias 'Bad' must match"),
        ({1: {"kind": KIND, "required": True, "description": ""}}, "REFS alias 1 must match"),
        ({"a": {"kind": KIND, "required": True}}, "REFS['a'] must be a dict with exactly the keys description, kind, required"),
        ({"a": {"kind": KIND, "required": True, "description": "", "x": 1}}, "must be a dict with exactly the keys"),
        ({"a": [KIND]}, "must be a dict with exactly the keys"),
        ({"a": {"kind": "Bad Kind", "required": True, "description": ""}}, "REFS['a'] kind 'Bad Kind' is not a kind name"),
        ({"a": {"kind": KIND, "required": 1, "description": ""}}, "REFS['a'] required must be true or false"),
        ({"a": {"kind": KIND, "required": False, "description": None}}, "REFS['a'] description must be a str"),
    ],
)
def test_check_kind_validates_refs(refs, needle):
    with pytest.raises(ConfigError) as err:
        kinds.check_kind(mirror_kind(REFS=refs))
    assert needle in str(err.value)


def test_refs_is_reserved(extra_kinds):
    with pytest.raises(ConfigError, match=r"may not redefine envelope keys \['refs'\]"):
        kinds.check_kind(fake_kind(PROPERTIES={"hostname": {"type": "string"}, "refs": {"type": "string"}}))
    extra_kinds(fake_entry_point(fake_kind(), value="prov_registry.kind"))
    with pytest.raises(ConfigError, match="NAME 'refs' collides with a core property of the spec envelope"):
        check_contract(fake_backend(name="refs"), "refs = fake")


def test_backend_refs_must_be_empty(extra_kinds):
    extra_kinds(fake_entry_point(fake_kind(), value="prov_registry.kind"))
    check_contract(fake_backend(REFS={}), "x = fake")
    with pytest.raises(ConfigError, match=f"REFS is reserved and must be empty; references are declared by the kind \\(REFS of kind '{KIND}'\\)"):
        check_contract(fake_backend(REFS={"image": {}}), "x = fake")
