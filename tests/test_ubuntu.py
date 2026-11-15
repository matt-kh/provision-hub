"""Ubuntu plugin: rendered-output validation, optional external tools, registry contract."""

from __future__ import annotations

import re
import subprocess
import types

import pytest

from prov.distros import ubuntu
from prov.registry import ENTRY_POINT_GROUP, check_contract, discover
from prov.schema import ConfigError, tomllib
from prov.validate import ERROR, SKIPPED, check_host


@pytest.fixture
def rendered(tmp_path):
    host, _ = check_host(tomllib.loads(ubuntu.starter("demo")), discover())
    for name, text in ubuntu.render(host).items():
        (tmp_path / name).write_text(text)
    return tmp_path


def messages(findings, level=ERROR):
    return [f.message for f in findings if f.level == level]


def test_clean_render_validates(rendered):
    findings = ubuntu.validate(rendered)
    assert messages(findings) == []
    assert messages(findings, SKIPPED) == ["cloud-init not installed (cloud-init schema)"]


@pytest.mark.parametrize(
    "edit,needle",
    [
        (lambda t: t.replace("#cloud-config\n", ""), "#cloud-config"),
        (lambda t: t.replace("  version: 1\n", "  version: 2\n"), "version must be 1"),
        (lambda t: t.replace("autoinstall:", "autoinstal:"), "'autoinstall' mapping"),
        (lambda t: t.replace("id: vg0", "id: vg9"), "'vg0' is not defined"),
        (lambda t: t.replace("path: /\n", "path: /srv\n"), "exactly one root"),
        (lambda t: t + "  : [\n", "invalid YAML"),
    ],
)
def test_broken_user_data(rendered, edit, needle):
    path = rendered / "user-data"
    path.write_text(edit(path.read_text()))
    assert any(needle in m for m in messages(ubuntu.validate(rendered))), messages(ubuntu.validate(rendered))


def test_missing_files(tmp_path):
    msgs = [str(f) for f in ubuntu.validate(tmp_path)]
    assert "error: user-data: missing; run prov render" in msgs
    assert "error: meta-data: missing; run prov render" in msgs
    assert "error: kernel-cmdline: missing; run prov render" in msgs


def test_external_validator_runs_when_present(rendered, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Error: bad key")

    monkeypatch.setattr(ubuntu.shutil, "which", lambda name: "/usr/bin/cloud-init")
    monkeypatch.setattr(ubuntu.subprocess, "run", fake_run)
    findings = ubuntu.validate(rendered)
    assert calls == [["/usr/bin/cloud-init", "schema", "--config-file", str(rendered / "user-data")]]
    assert messages(findings) == ["cloud-init schema: Error: bad key"]


def test_ubuntu_registered_via_entry_point():
    from importlib.metadata import entry_points

    names = {ep.name: ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)}
    assert names.get("ubuntu") == "prov.distros.ubuntu"
    assert discover()["ubuntu"] is ubuntu


def test_contract_check_rejects_incomplete_plugin():
    fake = types.SimpleNamespace(NAME="talos", SCHEMA={}, render=lambda h: {})
    with pytest.raises(ConfigError, match="validate, plan"):
        check_contract(fake, "talos = fake")


def test_render_paths_and_newlines(rendered):
    for path in rendered.iterdir():
        text = path.read_text()
        assert text.endswith("\n") and not any(line != line.rstrip() for line in text.splitlines())


@pytest.mark.parametrize(
    "exc,needle",
    [
        (subprocess.TimeoutExpired(["cloud-init"], 120), "timed out"),
        (PermissionError(13, "Permission denied"), "could not run: Permission denied"),
    ],
)
def test_external_validator_failures_are_findings(rendered, monkeypatch, exc, needle):
    def fake_run(argv, **kwargs):
        raise exc

    monkeypatch.setattr(ubuntu.shutil, "which", lambda name: "/usr/bin/cloud-init")
    monkeypatch.setattr(ubuntu.subprocess, "run", fake_run)
    assert any(needle in m for m in messages(ubuntu.validate(rendered)))


@pytest.mark.parametrize(
    "user_data",
    [
        "#cloud-config\n- a list\n",
        "#cloud-config\nautoinstall: [1, 2]\n",
        "#cloud-config\nautoinstall:\n  version: 1\n  user-data: [x]\n  ssh: [x]\n  network: [x]\n  storage: [x]\n",
        "#cloud-config\nautoinstall:\n  version: 1\n  user-data: {users: [x]}\n  storage:\n    config:\n"
        "    - {type: disk, id: [x]}\n    - {type: partition, id: p, device: [x]}\n"
        "    - {type: raid, id: r, devices: notalist}\n    - {type: raid, id: r2, devices: [[x]]}\n    - 5\n",
    ],
)
def test_hand_edited_shapes_never_crash(rendered, user_data):
    (rendered / "user-data").write_text(user_data)
    assert messages(ubuntu.validate(rendered))  # findings, no exception


def test_zfs_layout_matches_curtin_and_subiquity():
    raw = tomllib.loads(ubuntu.starter("demo"))
    raw["ubuntu"]["storage"]["layout"] = "zfs"
    host, findings = check_host(raw, discover())
    assert host is not None, findings
    config = ubuntu.storage_config(host)
    pool = next(a for a in config if a["type"] == "zpool")
    datasets = [a for a in config if a["type"] == "zfs"]
    assert pool["mountpoint"] == "/" and pool["fs_properties"]["canmount"] == "off"
    assert [(d["volume"], d["properties"]) for d in datasets] == [
        ("ROOT", {"canmount": "off", "mountpoint": "none"}),
        ("ROOT/ubuntu", {"mountpoint": "/"}),
    ]
    assert [a["path"] for a in config if a["type"] == "mount"] == ["/boot", "/boot/efi"]


def test_version_true_is_not_version_1(rendered):
    path = rendered / "user-data"
    path.write_text(path.read_text().replace("  version: 1\n", "  version: true\n"))
    assert "autoinstall.version must be 1" in messages(ubuntu.validate(rendered))


@pytest.mark.parametrize(
    "schema,needle",
    [
        ({"type": "object", "properties": {"x": {"type": "string", "pattern": "^[a-"}}}, "invalid regular expression"),
        ({"type": "object", "format": "uri"}, "unsupported keyword"),
        ({"type": "object", "properties": {"x": {"$ref": "https://example.com/s.json"}}}, "$ref"),
        ({"type": "object", "properties": {"x": "string"}}, "must be an object"),
    ],
)
def test_contract_check_rejects_bad_schema(schema, needle):
    fake = types.SimpleNamespace(NAME="talos", SCHEMA=schema, render=lambda h: {}, validate=lambda d: [], plan=lambda h: [])
    with pytest.raises(ConfigError, match=re.escape(needle)):
        check_contract(fake, "talos = fake")


BAD_SCHEMAS = {
    "type_typo": ({"type": "strin"}, "type: must be one of"),
    "type_empty_list": ({"type": []}, "type: must be one of"),
    "minLength_string": ({"type": "string", "minLength": "3"}, "minLength: must be a non-negative integer"),
    "minItems_bool": ({"type": "array", "minItems": True}, "minItems: must be a non-negative integer"),
    "maxLength_negative": ({"type": "string", "maxLength": -1}, "maxLength: must be a non-negative integer"),
    "minimum_string": ({"type": "integer", "minimum": "1"}, "minimum: must be a number"),
    "properties_list": ({"type": "object", "properties": []}, "properties: must be an object"),
    "required_string": ({"type": "object", "required": "x"}, "required: must be a list of strings"),
    "enum_empty": ({"enum": []}, "enum: must be a non-empty list"),
    "allOf_dict": ({"allOf": {}}, "allOf: must be a list"),
    "uniqueItems_string": ({"type": "array", "uniqueItems": "yes"}, "uniqueItems: must be a boolean"),
    "additionalProperties_string": ({"type": "object", "additionalProperties": "no"}, "additionalProperties: must be a boolean"),
    "own_defs": ({"type": "object", "$defs": {"x": {"type": "string"}}, "properties": {"a": {"$ref": "#/$defs/x"}}}, "may not define $defs"),
    "unresolved_ref": ({"type": "object", "properties": {"a": {"$ref": "#/$defs/disk"}}}, "does not resolve"),
    "items_not_schema": ({"type": "array", "items": "string"}, "must be an object"),
}


@pytest.mark.parametrize("name", sorted(BAD_SCHEMAS))
def test_schema_values_are_validated_at_load(name):
    schema, needle = BAD_SCHEMAS[name]
    fake = types.SimpleNamespace(NAME="talos", SCHEMA=schema, render=lambda h: {}, validate=lambda d: [], plan=lambda h: [])
    with pytest.raises(ConfigError, match=re.escape(needle)):
        check_contract(fake, "talos = fake")


def test_shared_defs_are_referenceable():
    schema = {"type": "object", "properties": {"admins": {"type": "array", "items": {"$ref": "#/$defs/user"}}}}
    fake = types.SimpleNamespace(NAME="talos", SCHEMA=schema, render=lambda h: {}, validate=lambda d: [], plan=lambda h: [])
    check_contract(fake, "talos = fake")


def test_composed_host_schema_passes_its_own_check():
    from prov.registry import builtin
    from prov.schema import host_schema
    from prov.validate import check_schema_fragment

    composed = host_schema(builtin())
    check_schema_fragment(composed, "<host>", defs=composed["$defs"])


def test_broken_third_party_plugin_breaks_no_command_with_a_traceback(monkeypatch, capsys):
    import prov.registry as registry
    from prov.cli import main

    bad = types.SimpleNamespace(NAME="talos", SCHEMA={"type": "object", "properties": []}, render=lambda h: {}, validate=lambda d: [], plan=lambda h: [])
    ep = types.SimpleNamespace(name="talos", value="prov_talos", load=lambda: bad)
    monkeypatch.setattr(registry, "entry_points", lambda group: [ep])
    assert main(["distros"]) == 2
    assert "distro plugin talos = prov_talos: invalid SCHEMA" in capsys.readouterr().err


@pytest.mark.parametrize(
    "pattern,text,ok",
    [
        ("^[0-9]+$", "٣", False),
        ("^\\d+$", "٣", False),  # re.ASCII: \d is ASCII-only, as in ECMA-262
        ("^\\d+$", "3", True),
    ],
)
def test_patterns_are_ascii(pattern, text, ok):
    from prov.validate import ecma_search

    assert ecma_search(pattern, text) is ok
