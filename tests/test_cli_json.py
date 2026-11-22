"""``--json`` on kinds, backends, schema, validate and plan: shapes, stability, exit codes, streams."""

from __future__ import annotations

import json
import shutil

import pytest
from conftest import EXAMPLES, fake_entry_point
from test_kind_plugins import BACKEND, KIND, fake_backend, fake_kind

from prov.cli import JSON_FORMAT, main
from prov.text import json_text

ITEM = "os-install/demo-lvm"
FORMAT = "prov.cli/v1alpha1"


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def document(out):
    """Exactly one canonical JSON document: parses, and re-serialises to the same text."""
    doc = json.loads(out)
    assert out == json_text(doc) and out.endswith("}\n") and out.count("\n}\n") == 1
    assert doc.pop("format") == FORMAT == JSON_FORMAT  # every --json document but schema's
    return doc


@pytest.fixture
def root(tmp_path, capsys):
    """A copy of the examples root, so validate and plan never touch the committed goldens."""
    dest = tmp_path / "root"
    shutil.copytree(EXAMPLES, dest, symlinks=True)
    return dest


# --- shapes ------------------------------------------------------------------------------


def test_kinds_json_shape(capsys):
    code, out, err = run(capsys, "kinds", "--json")
    assert code == 0 and err == ""
    doc = document(out)
    assert list(doc) == ["kinds"]
    entry = next(k for k in doc["kinds"] if k["name"] == "os-install")
    assert entry == {
        "name": "os-install",
        "group": "prov",
        "api_versions": ["prov/v1alpha1"],
        "summary": "One machine's operating-system install: host identity, users, network and whole disks.",
        "builtin": True,
        "outputs": None,
        "refs": {},
        "fixtures": [],
    }


def test_kinds_json_third_party_kind(capsys, extra_kinds):
    extra_kinds(fake_entry_point(fake_kind(), value="prov_registry.kind"))
    doc = document(run(capsys, "kinds", "--json")[1])
    assert [k["name"] for k in doc["kinds"]] == sorted(k["name"] for k in doc["kinds"])
    entry = next(k for k in doc["kinds"] if k["name"] == KIND)
    assert entry == {
        "name": KIND,
        "group": "example.org",
        "api_versions": ["example.org/v1alpha1"],
        "summary": "A test-only kind: a container registry.",
        "builtin": False,
        "outputs": None,
        "refs": {},
        "fixtures": [],
    }


def test_backends_json_shape(capsys):
    code, out, err = run(capsys, "backends", "--json")
    assert code == 0 and err == ""
    doc = document(out)
    assert list(doc) == ["backends"]
    by_name = {b["name"]: b for b in doc["backends"]}
    assert by_name["nixos-disko"] == {
        "name": "nixos-disko",
        "kind": "os-install",
        "summary": "NixOS with disko: a pinned flake holding the target system and an offline USB installer.",
        "builtin": True,
        "starter": True,
        "check": True,
        "preserve": ["flake.lock"],
        "fixtures": [],
        "unsupported_fixtures": {},
    }
    assert by_name["ubuntu-autoinstall"]["preserve"] == [] and by_name["ubuntu-autoinstall"]["builtin"] is True


def test_backends_json_third_party_and_kind_filter(capsys, extra_kinds, extra_backends):
    extra_kinds(fake_entry_point(fake_kind(), value="prov_registry.kind"))
    backend = fake_backend()
    del backend.starter
    extra_backends(fake_entry_point(backend, value="prov_registry.text"))
    doc = document(run(capsys, "backends", "--json")[1])
    names = [b["name"] for b in doc["backends"]]
    assert names == [b for _, b in sorted((b["kind"], b["name"]) for b in doc["backends"])]  # (KIND, NAME) like the text
    code, out, err = run(capsys, "backends", "--kind", KIND, "--json")
    assert code == 0 and err == ""
    assert document(out) == {
        "backends": [
            {
                "name": BACKEND,
                "kind": KIND,
                "summary": "Renders a registry as a text file.",
                "builtin": False,
                "starter": False,
                "check": False,
                "preserve": [],
        "fixtures": [],
        "unsupported_fixtures": {},
            }
        ]
    }
    code, out, err = run(capsys, "backends", "--kind", "os-install", "--json")
    assert code == 0 and BACKEND not in out


def test_text_goldens_kinds_and_backends(capsys):
    """Without --json the text output stays byte-identical."""
    assert run(capsys, "kinds") == (0, "os-install\tprov/v1alpha1\tOne machine's operating-system install: host identity, users, network and whole disks.\n", "")
    expected = (
        "nixos-disko\tos-install\tNixOS with disko: a pinned flake holding the target system and an offline USB installer.\n"
        "ubuntu-autoinstall\tos-install\tUbuntu Server autoinstall (subiquity): NoCloud seed for an os-install spec.\n"
    )
    assert run(capsys, "backends") == (0, expected, "")
    assert run(capsys, "backends", "--kind", "os-install") == (0, expected, "")


def test_schema_json_is_a_no_op(capsys):
    code, out, err = run(capsys, "schema", "os-install", "--json")
    assert (code, out, err) == run(capsys, "schema", "os-install") and '"format"' not in out
    assert run(capsys, "schema", "os-install", "--backend", "nixos-disko", "--json") == run(capsys, "schema", "os-install", "--backend", "nixos-disko")


def test_validate_json_ok(root, capsys):
    code, out, err = run(capsys, "--root", str(root), "validate", ITEM, "--json")
    assert code == 0 and err == ""
    doc = document(out)
    assert doc == {
        "ref": ITEM,
        "backend": "ubuntu-autoinstall",
        "ok": True,
        "outputs": {},
        "refs": {},
        "findings": [{"level": "skipped", "where": "", "message": "cloud-init not installed (cloud-init schema)"}],
    }


def test_validate_json_reports_every_level_on_stdout(root, capsys, monkeypatch):
    from prov.backends import ubuntu_autoinstall as ubuntu
    from prov.validate import ERROR, SKIPPED, WARNING, Finding

    monkeypatch.setattr(ubuntu, "validate", lambda d: [Finding(SKIPPED, "tool not installed", "x"), Finding(WARNING, "w", "b"), Finding(ERROR, "e", "a")])
    code, out, err = run(capsys, "--root", str(root), "validate", ITEM, "--json")
    assert code == 1 and err == ""
    doc = document(out)
    assert doc["ok"] is False and doc["backend"] == "ubuntu-autoinstall"
    assert doc["findings"] == [
        {"level": "error", "where": "a", "message": "e"},
        {"level": "warning", "where": "b", "message": "w"},
        {"level": "skipped", "where": "x", "message": "tool not installed"},
    ]
    # the text output orders the same findings the same way, and splits them over the streams
    code, out, err = run(capsys, "--root", str(root), "validate", ITEM)
    assert code == 1 and err == "error: a: e\n" and out == "warning: b: w\nskipped: x: tool not installed\n"


def test_validate_json_warnings_only_exit_0(root, capsys, monkeypatch):
    from prov.backends import ubuntu_autoinstall as ubuntu
    from prov.validate import WARNING, Finding

    monkeypatch.setattr(ubuntu, "validate", lambda d: [Finding(WARNING, "w", "b")])
    code, out, err = run(capsys, "--root", str(root), "validate", ITEM, "--json")
    assert code == 0 and err == "" and document(out)["ok"] is True


def test_validate_json_not_rendered(root, capsys):
    shutil.rmtree(root / ITEM / "rendered")
    code, out, err = run(capsys, "--root", str(root), "validate", ITEM, "--json")
    assert code == 1 and err == ""
    doc = document(out)
    assert {"level": "error", "where": "rendered", "message": f"not rendered yet; run prov render {ITEM}"} in doc["findings"]
    assert doc["outputs"] == {}  # the spec is valid: outputs are computed even when rendered/ is missing


def _break_hostname(root):
    spec = root / ITEM / "spec.toml"
    spec.write_text(spec.read_text().replace('hostname = "demo-lvm"', 'hostname = "Not_A_Label"'))


def test_validate_json_invalid_spec_echoes_backend(root, capsys):
    _break_hostname(root)
    code, out, err = run(capsys, "--root", str(root), "validate", ITEM, "--json")
    assert code == 1 and err == ""
    doc = document(out)
    assert doc["backend"] == "ubuntu-autoinstall" and doc["ok"] is False and doc["outputs"] is None
    assert any(f["level"] == "error" and "Not_A_Label" in f["message"] for f in doc["findings"]), doc


def test_validate_json_backend_of_another_kind_is_null(root, capsys, monkeypatch, extra_kinds, extra_backends):
    """backend is null unless it names a registered backend of the item's kind."""
    from prov import cli
    from prov.validate import ERROR, Finding

    extra_kinds(fake_entry_point(fake_kind(), value="prov_registry.kind"))
    extra_backends(fake_entry_point(fake_backend(), value="prov_registry.text"))
    _break_hostname(root)
    spec = root / ITEM / "spec.toml"
    spec.write_text(spec.read_text().replace('backend = "ubuntu-autoinstall"', f'backend = "{BACKEND}"'))
    # an unknown or foreign backend is an envelope error: exit 2, text on stderr, empty stdout
    code, out, err = run(capsys, "--root", str(root), "validate", ITEM, "--json")
    assert code == 2 and out == "" and err.startswith("prov: error: ")
    # should a kind ever accept it at finding level, the document still says null
    from prov.registry import Context
    from prov.schema import read_toml

    monkeypatch.setattr(cli, "check_item", lambda root, kind, name, backends: (read_toml(spec), None, [Finding(ERROR, "e", "x")], Context(name)))
    code, out, err = run(capsys, "--root", str(root), "validate", ITEM, "--json")
    assert code == 1 and err == "" and document(out)["backend"] is None


@pytest.mark.parametrize("verb, code_", [("plan", 2), ("validate", 1)])
def test_schema_invalid_spec_streams(root, capsys, verb, code_):
    """Envelope valid, schema invalid: plan errors (multi-line text, empty stdout), validate reports findings."""
    _break_hostname(root)
    code, out, err = run(capsys, "--root", str(root), verb, ITEM, "--json")
    assert code == code_
    if verb == "plan":
        lines = err.splitlines()
        assert out == "" and lines[0].startswith("prov: error: ") and lines[0].endswith("invalid spec:") and len(lines) > 1
        assert all(line.startswith("  ") for line in lines[1:]) and "Not_A_Label" in err
    else:
        assert err == "" and document(out)["ok"] is False


def test_json_flag_before_the_verb_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--json", "kinds"])
    out = capsys.readouterr()
    assert exc.value.code == 2 and out.out == "" and "prov: error:" in out.err


@pytest.mark.parametrize("verb", ["validate", "plan"])
def test_exit_2_errors_stay_text_on_stderr(root, capsys, verb):
    code, out, err = run(capsys, "--root", str(root), verb, "os-install/missing", "--json")
    assert code == 2 and out == "" and err.startswith("prov: error: ") and err.count("\n") == 1
    code, out, err = run(capsys, "--root", str(root), verb, "nokind/x", "--json")
    assert code == 2 and out == "" and err.startswith("prov: error: ")


@pytest.mark.parametrize(
    "argv, broken",
    [(["kinds", "--json"], True), (["backends", "--json"], True), (["backends", "--kind", "nokind", "--json"], False), (["schema", "nokind", "--json"], False)],
)
def test_exit_2_on_catalog_verbs(capsys, extra_kinds, argv, broken):
    if broken:  # a kind plugin written for another contract
        extra_kinds(fake_entry_point(fake_kind(PLUGIN_API=2), value="prov_registry.kind"))
    code, out, err = run(capsys, *argv)
    assert code == 2 and out == "" and err.startswith("prov: error: ") and err.count("\n") == 1


def test_plan_json(root, capsys):
    code, text, err = run(capsys, "--root", str(root), "plan", ITEM)
    assert code == 0 and err == ""
    code, out, err = run(capsys, "--root", str(root), "plan", ITEM, "--json")
    assert code == 0 and err == ""
    doc = document(out)
    assert sorted(doc) == ["backend", "lines", "ref", "rendered_dir"]
    assert doc["ref"] == ITEM and doc["backend"] == "ubuntu-autoinstall"
    assert doc["rendered_dir"] == str((root / ITEM / "rendered").resolve())
    assert "\n".join(doc["lines"]) + "\n" == text  # the same lines, nothing added or escaped


@pytest.mark.parametrize("bad", ["echo a\nb", "echo \x1b[2J", "echo ‮evil"])
def test_plan_json_refuses_unsafe_lines(root, capsys, monkeypatch, bad):
    from prov.backends import ubuntu_autoinstall as ubuntu

    monkeypatch.setattr(ubuntu, "plan", lambda spec, ctx: ["# fine", bad])
    code, out, err = run(capsys, "--root", str(root), "plan", ITEM, "--json")
    assert code == 2 and out == "" and "plan line 1 contains a line break or an unsafe character" in err


def test_list_has_no_json(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--root", str(tmp_path), "list", "--json"])
    assert exc.value.code == 2 and capsys.readouterr().out == ""


# --- stability and the text output -------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [["kinds"], ["backends"], ["backends", "--kind", "os-install"], ["schema", "os-install"], ["validate", ITEM], ["plan", ITEM]],
)
def test_json_is_stable_across_runs(root, capsys, argv):
    first = run(capsys, "--root", str(root), *argv, "--json")
    second = run(capsys, "--root", str(root), *argv, "--json")
    assert first == second and first[0] == 0 and first[2] == ""
    assert all(ch.isprintable() and ord(ch) < 128 or ch == "\n" for ch in first[1])


@pytest.mark.parametrize("argv", [["kinds"], ["backends"], ["backends", "--kind", "os-install"], ["validate", ITEM], ["plan", ITEM]])
def test_text_output_is_not_json(root, capsys, argv):
    """Without --json the verbs keep their text output (byte-identical: tests/test_cli.py)."""
    code, out, _ = run(capsys, "--root", str(root), *argv)
    assert code == 0 and not out.startswith("{")
