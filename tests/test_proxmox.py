"""proxmox-answer backend: check rules, rendered answer.toml, validation, optional tool, plan."""

from __future__ import annotations

import copy
import subprocess

import pytest

from prov.backends import proxmox_answer as pve
from prov.registry import Context, discover
from prov.schema import tomllib
from prov.spec import check_spec
from prov.validate import ERROR, SKIPPED, WARNING

KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE ops@example.com"
KEY2 = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEKEYFORBACKUPEXAMPLEKEYFORBACKUP00 backup@example.org"


def starter_raw():
    return tomllib.loads(pve.starter("demo"))


def check(raw):
    return check_spec(raw, "os-install", "demo", discover())


def errors(raw, level=ERROR):
    _, findings = check(raw)
    return [str(f) for f in findings if f.level == level]


def answer_of(raw):
    spec, findings = check(raw)
    assert [f for f in findings if f.level == ERROR] == []
    return tomllib.loads(pve.render(spec, Context("demo"))["answer.toml"])


@pytest.fixture
def rendered(tmp_path):
    spec, _ = check(starter_raw())
    for name, text in pve.render(spec, Context("demo")).items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_starter_is_valid_and_renders_static_answer():
    data = answer_of(starter_raw())
    assert data["global"] == {
        "country": "us",
        "fqdn": "demo.example.com",
        "keyboard": "en-us",
        "mailto": "ops@example.com",
        "reboot-on-error": False,
        "root-password-hashed": starter_raw()["proxmox-answer"]["root_password_hash"],
        "root-ssh-keys": [KEY],
        "timezone": "Etc/UTC",
    }
    assert data["network"] == {"source": "from-answer", "cidr": "192.0.2.10/24", "dns": "192.0.2.53", "gateway": "192.0.2.1", "filter": {"ID_NET_NAME": "eno1"}}
    assert data["disk-setup"] == {"filesystem": "ext4", "disk-list": ["sda"]}


def test_dhcp_mac_ipv6_by_id_and_zfs():
    raw = starter_raw()
    raw["network"] = {"interfaces": [{"name": "enp1s0", "dhcp4": True}]}
    raw["storage"]["disks"] = ["/dev/nvme0n1", "/dev/nvme1n1"]
    raw["proxmox-answer"]["storage"] = {"filesystem": "zfs", "raid": "raid1"}
    data = answer_of(raw)
    assert data["network"] == {"source": "from-dhcp"}
    assert data["disk-setup"] == {"filesystem": "zfs", "disk-list": ["nvme0n1", "nvme1n1"], "zfs": {"raid": "raid1"}}

    raw = starter_raw()
    raw["network"] = {
        "interfaces": [{"name": "eno1", "macaddress": "02:00:5e:00:53:01", "addresses": ["2001:db8::10/64"], "gateway6": "2001:db8::1"}],
        "dns": {"nameservers": ["2001:db8::53"]},
    }
    raw["storage"]["disks"] = ["/dev/disk/by-id/ata-EXAMPLE_DISK_0001"]
    data = answer_of(raw)
    assert data["network"]["gateway"] == "2001:db8::1" and data["network"]["filter"] == {"ID_NET_NAME_MAC": "*02005e005301"}
    assert data["disk-setup"] == {"filesystem": "ext4", "filter": {"DEVLINKS": "*/dev/disk/by-id/ata-EXAMPLE_DISK_0001*"}}


def test_sudo_keys_go_to_root_deduplicated():
    raw = starter_raw()
    raw["users"] = [
        {"name": "ops", "sudo": True, "ssh_authorized_keys": [KEY]},
        {"name": "adm", "sudo": True, "ssh_authorized_keys": [KEY2, KEY]},
    ]
    assert answer_of(raw)["global"]["root-ssh-keys"] == [KEY, KEY2]
    raw["users"] = []
    assert "root-ssh-keys" not in answer_of(raw)["global"]


def test_keyboard_override_and_mapping():
    raw = starter_raw()
    raw["keyboard"] = "ch"
    assert errors(raw) == ["error: keyboard: no installer keyboard id for 'ch'; set proxmox-answer.keyboard"]
    raw["proxmox-answer"]["keyboard"] = "fr-ch"
    assert answer_of(raw)["global"]["keyboard"] == "fr-ch"
    raw = starter_raw()
    raw["keyboard"] = "br"
    assert answer_of(raw)["global"]["keyboard"] == "pt-br"


def _edit(path, value):
    raw = starter_raw()
    node = raw
    for key in path[:-1]:
        node = node[key]
    if value is None:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return raw


@pytest.mark.parametrize(
    "path,value,expected",
    [
        (("domain",), None, "error: domain: the installer needs an FQDN; set domain"),
        (("arch",), "arm64", "error: arch: Proxmox VE installs on amd64 only"),
        (("users",), [{"name": "backup", "ssh_authorized_keys": [KEY2]}], "error: users[0]: the installer creates only root; 'backup' has no sudo"),
        (("storage", "disks"), ["/dev/sda", "/dev/sdb"], "error: storage.disks: ext4 installs to exactly one disk, got 2"),
        (("storage", "disks"), ["/dev/mapper/vg-root"], "is neither a whole-disk kernel device"),
        (("storage", "disks"), ["/dev/sda1"], "is neither a whole-disk kernel device"),
        (("storage", "disks"), ["/dev/nvme0n1p2"], "is neither a whole-disk kernel device"),
        (("storage", "disks"), ["/dev/disk/by-uuid/EXAMPLE-0001"], "is neither a whole-disk kernel device"),
        (("domain",), "a" * 63 + "." + "b" * 63 + "." + "c" * 63 + "." + "d" * 60 + ".com", "longer than 253 characters"),
        (("storage", "disks"), ["/dev/disk/by-id/a-EXAMPLE", "/dev/disk/by-id/b-EXAMPLE"], "cannot name several disks"),
        (("proxmox-answer", "storage"), {"filesystem": "ext4", "raid": "raid1"}, "raid applies to zfs and btrfs only, not ext4"),
        (("proxmox-answer", "storage"), {"filesystem": "zfs"}, "error: proxmox-answer.storage.raid: zfs needs raid"),
        (("proxmox-answer", "storage"), {"filesystem": "btrfs", "raid": "raidz-1"}, "btrfs supports raid raid0, raid1, raid10, not raidz-1"),
        (("proxmox-answer", "storage"), {"filesystem": "zfs", "raid": "raid1"}, "zfs raid1 needs at least 2 disks, got 1"),
        (("network",), None, "error: network: proxmox-answer needs [network]"),
        (("network", "interfaces"), [{"name": "a", "dhcp4": True}, {"name": "b", "dhcp4": True}], "exactly one NIC, got 2"),
        (("network", "interfaces"), [{"name": "eno1", "dhcp4": True}], "with DHCP the installer takes address, gateway and DNS"),
        (("network", "interfaces"), [{"name": "eno1", "addresses": ["192.0.2.10/24", "192.0.2.11/24"], "gateway4": "192.0.2.1"}], "exactly one address, got 2"),
        (("network", "interfaces"), [{"name": "eno1", "addresses": ["192.0.2.10/24"]}], "a static IPv4 install needs gateway4"),
        (("network", "interfaces"), [{"name": "eno1", "addresses": ["192.0.2.10/24"], "gateway4": "192.0.2.1", "gateway6": "2001:db8::1"}], "remove gateway6"),
        (("network", "interfaces"), [{"name": "eno1", "addresses": ["192.0.2.10/24"], "gateway4": "192.0.2.1", "mtu": 9000}], "cannot set an MTU"),
        (("network", "dns"), {"nameservers": ["192.0.2.53", "192.0.2.54"]}, "exactly one nameserver, got 2"),
        (("network", "dns"), {"nameservers": ["192.0.2.53"], "search": ["example.org"]}, "search may only repeat domain"),
    ],
)
def test_check_rules(path, value, expected):
    found = errors(_edit(path, value))
    assert any(expected in e for e in found), found


def test_raid_disk_counts():
    raw = starter_raw()
    raw["storage"]["disks"] = ["/dev/sda", "/dev/sdb", "/dev/sdc", "/dev/sdd", "/dev/sde"]
    raw["proxmox-answer"]["storage"] = {"filesystem": "zfs", "raid": "raid10"}
    assert any("even number of disks, got 5" in e for e in errors(raw))
    raw["proxmox-answer"]["storage"] = {"filesystem": "zfs", "raid": "raidz-3"}
    assert errors(raw) == []
    raw["storage"]["disks"] = raw["storage"]["disks"][:4]
    assert any("raidz-3 needs at least 5 disks, got 4" in e for e in errors(raw))


def test_warnings():
    raw = starter_raw()
    raw["locale"] = "de_DE.UTF-8"
    raw["users"][0]["password_hash"] = raw["proxmox-answer"]["root_password_hash"]
    raw["network"] = {"interfaces": [{"name": "eno1", "macaddress": "02:00:5e:00:53:01", "dhcp4": True}]}
    assert errors(raw) == []
    found = errors(raw, WARNING)
    assert len(found) == 4, found
    assert any("'ops' is not created, its ssh keys are given to root" in w for w in found)


def test_clean_render_validates(rendered):
    findings = pve.validate(rendered)
    assert [f for f in findings if f.level == ERROR] == []
    assert [f.message for f in findings if f.level == SKIPPED] == ["proxmox-auto-install-assistant not installed (proxmox-auto-install-assistant validate-answer)"]


@pytest.mark.parametrize(
    "edit,needle",
    [
        (lambda t: t.replace("[global]", "[globl]"), "[global] is missing"),
        (lambda t: t.replace('keyboard = "en-us"', 'keyboard = "us"'), "not an installer keyboard id"),
        (lambda t: t.replace("root-password-hashed", "root-password-hashd"), "exactly one of root-password and root-password-hashed"),
        (lambda t: t.replace('source = "from-answer"', 'source = "from-dhcp"'), "takes no other keys"),
        (lambda t: t.replace('source = "from-answer"', 'source = "manual"'), "network.source must be"),
        (lambda t: t.replace('disk-list = ["sda"]', ""), "exactly one of disk-list and filter"),
        (lambda t: t.replace('filesystem = "ext4"', 'filesystem = "zfs"'), "disk-setup.zfs.raid must be one of"),
        (lambda t: t + "[global\n", "invalid TOML"),
        (lambda t: t.replace('filesystem = "ext4"', 'filesystem = ["zfs"]'), "disk-setup.filesystem must be one of"),
        (lambda t: t.replace('filesystem = "ext4"', "filesystem = {a = 1}"), "disk-setup.filesystem must be one of"),
        (lambda t: t.replace('filesystem = "ext4"', 'filesystem = "zfs"\nzfs.raid = ["raid1"]'), "disk-setup.zfs.raid must be one of"),
        (lambda t: t.replace('filesystem = "ext4"', 'filesystem = "zfs"\nzfs = {raid = {a = 1}}'), "disk-setup.zfs.raid must be one of"),
        (lambda t: t.replace('disk-list = ["sda"]', "disk-list = [1]"), "non-empty list of disk names"),
        (lambda t: t.replace('disk-list = ["sda"]', 'filter = "sda"'), "disk-setup.filter must be a non-empty table"),
        (lambda t: t.replace("root-ssh-keys = [", "root-ssh-keys = [1, "), "root-ssh-keys must be a list of strings"),
    ],
)
def test_broken_answer(rendered, edit, needle):
    path = rendered / "answer.toml"
    path.write_text(edit(path.read_text()))
    msgs = [f.message for f in pve.validate(rendered) if f.level == ERROR]
    assert any(needle in m for m in msgs), msgs


def test_missing_answer(tmp_path):
    assert [str(f) for f in pve.validate(tmp_path)] == ["error: answer.toml: missing; run prov render"]


def test_external_validator(rendered, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "Error: missing field `fqdn`\n")

    monkeypatch.setattr(pve.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(pve.subprocess, "run", fake_run)
    msgs = [f.message for f in pve.validate(rendered) if f.level == ERROR]
    assert calls == [["/usr/bin/proxmox-auto-install-assistant", "validate-answer", str(rendered / "answer.toml")]]
    assert msgs == ["proxmox-auto-install-assistant validate-answer: Error: missing field `fqdn`"]


def test_plan_lists_steps_and_runs_nothing(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("plan must not execute anything")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    spec, _ = check(starter_raw())
    lines = pve.plan(spec, Context("demo"))
    assert "proxmox-auto-install-assistant validate-answer answer.toml" in lines
    assert any(line.startswith("proxmox-auto-install-assistant prepare-iso ") and "--answer-file answer.toml" in line for line in lines)
    assert any("wipes /dev/sda" in line for line in lines)

    raw = starter_raw()
    raw["storage"]["disks"] = ["/dev/disk/by-id/ata-EXAMPLE_DISK_0001"]
    spec, _ = check(raw)
    assert any("DEVLINKS='*/dev/disk/by-id/ata-EXAMPLE_DISK_0001*'" in line for line in pve.plan(spec, Context("demo")))


def test_fixture_tables_are_not_shared():
    tables = list(pve.FIXTURES.values())
    assert tables[0] == tables[1] and tables[0] is not tables[1]
    assert tables[0]["storage"] is not tables[1]["storage"]
    copy.deepcopy(pve.FIXTURES)
