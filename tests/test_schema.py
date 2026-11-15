"""Host schema v1: published file in sync, round-trip with a reference validator, versioning."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest
from conftest import SCHEMA_FILE, UPDATE_GOLDENS, example_hosts

from prov.distros import ubuntu
from prov.registry import builtin, discover
from prov.schema import ConfigError, host_schema, read_toml, schema_json, tomllib
from prov.validate import ERROR, check_host, load_host, schema_errors

STARTER = tomllib.loads(ubuntu.starter("demo"))


def published():
    return json.loads(SCHEMA_FILE.read_text(encoding="utf-8"))


def test_published_schema_matches_code():
    text = schema_json(host_schema(builtin()))
    if UPDATE_GOLDENS:
        SCHEMA_FILE.write_text(text, encoding="utf-8")
    assert SCHEMA_FILE.read_text(encoding="utf-8") == text, "regenerate: PROV_UPDATE_GOLDENS=1 uv run pytest"


def test_published_schema_is_valid_draft_2020_12():
    jsonschema.Draft202012Validator.check_schema(published())


@pytest.mark.parametrize("root,host", example_hosts())
def test_examples_round_trip_against_published_schema(root, host):
    raw = read_toml(root / "hosts" / host / "host.toml")
    jsonschema.Draft202012Validator(published()).validate(raw)
    assert schema_errors(raw, published()) == []


def test_starter_validates():
    jsonschema.Draft202012Validator(published()).validate(STARTER)
    host, findings = check_host(STARTER, discover())
    assert host is not None and not findings


def test_missing_schema_version():
    raw = copy.deepcopy(STARTER)
    del raw["schema_version"]
    host, findings = check_host(raw, discover())
    assert host is None
    assert [str(f) for f in findings] == ["error: schema_version: schema_version is required (this prov supports: 1)"]


@pytest.mark.parametrize("version", [0, 2, "1", True])
def test_unknown_schema_version(version):
    raw = dict(STARTER, schema_version=version)
    host, findings = check_host(raw, discover())
    assert host is None
    assert "unsupported schema_version" in findings[0].message


def test_load_host_raises_config_error(tmp_path):
    path = tmp_path / "host.toml"
    path.write_text('hostname = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="schema_version is required"):
        load_host(path, discover())
    path.write_text("not = [toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_host(path, discover())


def _mutate(fn):
    raw = copy.deepcopy(STARTER)
    fn(raw)
    return raw


INVALID = {
    "unknown_top_level_key": lambda r: r.update(password="hunter2"),
    "plain_password": lambda r: r["users"][0].update(password_hash="hunter2"),
    "unknown_user_key": lambda r: r["users"][0].update(password="hunter2"),
    "bad_hostname": lambda r: r.update(hostname="Bad_Name"),
    "user_without_name": lambda r: r.update(users=[{"sudo": True}]),
    "bad_layout": lambda r: r["ubuntu"]["storage"].update(layout="btrfs"),
    "missing_ubuntu_table": lambda r: r.pop("ubuntu"),
    "bad_disk": lambda r: r["storage"].update(disks=["sda"]),
    "bad_key_type": lambda r: r["users"][0].update(ssh_authorized_keys=["not-a-key"]),
    "mtu_type": lambda r: r["network"]["interfaces"][0].update(mtu="1500"),
    "bad_arch": lambda r: r.update(arch="riscv64"),
}


@pytest.mark.parametrize("name", sorted(INVALID))
def test_invalid_hosts_agree_with_reference_validator(name):
    raw = _mutate(INVALID[name])
    assert not jsonschema.Draft202012Validator(published()).is_valid(raw)
    assert schema_errors(raw, published())
    host, findings = check_host(raw, discover())
    assert host is None and findings and all(f.level == ERROR for f in findings)


SEMANTIC = {
    "address_without_prefix": (lambda r: r["network"]["interfaces"][0].update(addresses=["192.0.2.10"]), "CIDR"),
    "gateway_wrong_family": (lambda r: r["network"]["interfaces"][0].update(gateway4="2001:db8::1"), "not an IPv4"),
    "bad_nameserver": (lambda r: r["network"]["dns"].update(nameservers=["dns.example.com"]), "not an IP"),
    "no_ssh_keys": (lambda r: r["users"][0].update(ssh_authorized_keys=[]), "no login"),
    "no_users": (lambda r: r.pop("users"), "no login"),
    "interface_down": (lambda r: r["network"]["interfaces"].append({"name": "eth9"}), "stay down"),
    "zfs_two_disks": (lambda r: (r["ubuntu"]["storage"].update(layout="zfs"), r["storage"].update(disks=["/dev/vda", "/dev/vdb"])), "exactly one disk"),
    "raid1_one_disk": (lambda r: r["ubuntu"]["storage"].update(layout="mdraid"), "at least 2 disks"),
    "raid_level_on_lvm": (lambda r: r["ubuntu"]["storage"].update(raid_level=1), "mdraid' only"),
    "arm64_bios": (lambda r: r.update(arch="arm64", firmware="bios"), "UEFI only"),
    "unknown_distro": (lambda r: r.update(distro="plan9"), "unknown distro"),
}


@pytest.mark.parametrize("name", sorted(SEMANTIC))
def test_semantic_errors(name):
    fn, needle = SEMANTIC[name]
    host, findings = check_host(_mutate(fn), discover())
    assert host is None
    assert any(needle in f.message for f in findings), findings


def test_defaults_are_applied():
    raw = _mutate(lambda r: [r.pop(k) for k in ("arch", "firmware", "timezone", "locale", "keyboard")])
    host, _ = check_host(raw, discover())
    assert (host["arch"], host["firmware"], host["timezone"], host["locale"], host["keyboard"]) == (
        "amd64", "uefi", "Etc/UTC", "en_US.UTF-8", "us",
    )
    assert host["users"][0]["shell"] == "/bin/bash" and host["ubuntu"]["updates"] == "security"
