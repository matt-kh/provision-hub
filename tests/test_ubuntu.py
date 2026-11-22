"""ubuntu-autoinstall backend: rendered-output validation, optional external tools, layouts."""

from __future__ import annotations

import subprocess

import pytest

from prov.backends import ubuntu_autoinstall as ubuntu
from prov.registry import ENTRY_POINT_GROUP, Context, discover
from prov.schema import tomllib
from prov.spec import check_spec
from prov.validate import ERROR, SKIPPED, WARNING


def check_host(raw):
    return check_spec(raw, "os-install", "demo", discover())


@pytest.fixture
def rendered(tmp_path):
    host, _ = check_host(tomllib.loads(ubuntu.starter("demo")))
    for name, text in ubuntu.render(host, Context("demo")).items():
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
    assert names.get("ubuntu-autoinstall") == "prov.backends.ubuntu_autoinstall"
    assert discover()["ubuntu-autoinstall"] is ubuntu
    assert ubuntu.KIND == "os-install"


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
    raw["ubuntu-autoinstall"]["storage"]["layout"] = "zfs"
    host, findings = check_host(raw)
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


def test_check_receives_ctx_and_names_its_table():
    raw = tomllib.loads(ubuntu.starter("demo"))
    raw["ubuntu-autoinstall"]["storage"]["raid_level"] = 1
    host, findings = check_host(raw)
    assert host is None
    assert "error: ubuntu-autoinstall.storage.raid_level: raid_level applies to layout 'mdraid' only" in [str(f) for f in findings]


def test_starter_name_mismatch_is_only_a_warning():
    raw = tomllib.loads(ubuntu.starter("other"))
    host, findings = check_host(raw)
    assert host is not None
    assert [(f.level, f.where) for f in findings] == [(WARNING, "hostname")]


def test_name_warning_caps_the_value():
    from prov.spec import name_findings
    from prov import kinds

    findings = name_findings(kinds.get("os-install"), {"hostname": "h" * 500}, "demo")
    assert len(findings) == 1 and len(str(findings[0])) < 200
