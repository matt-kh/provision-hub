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
def test_invalid_hosts_rejected_by_both_validators(name):
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


# Differential test: prov's validator and the jsonschema reference must agree on every
# mutated host, in both directions. The only allowed disagreement is STRICTER: prov applies
# ECMA-262 `$` semantics (Python's `$` also matches before a trailing newline, which the
# reference inherits) and rejects control characters in every string.
DIFFERENTIAL = {
    # accept direction
    "starter": (lambda r: None, True),
    "mtu_integral_float": (lambda r: r["network"]["interfaces"][0].update(mtu=1500.0), True),
    "mtu_int": (lambda r: r["network"]["interfaces"][0].update(mtu=1500), True),
    "raid_level_5": (lambda r: (r["ubuntu"]["storage"].update(layout="mdraid", raid_level=5), r["storage"].update(disks=["/dev/sda", "/dev/sdb", "/dev/sdc"])), True),
    "raid_level_float": (lambda r: (r["ubuntu"]["storage"].update(layout="mdraid", raid_level=1.0), r["storage"].update(disks=["/dev/sda", "/dev/sdb"])), True),
    "password_hash": (lambda r: r["users"][0].update(password_hash="$6$EXAMPLEsalt$EXAMPLEhash"), True),
    "disk_by_id": (lambda r: r["storage"].update(disks=["/dev/disk/by-id/nvme-EXAMPLE_SERIAL-part1"]), True),
    "no_dns": (lambda r: r["network"].pop("dns"), True),
    "minimal_users": (lambda r: r.update(users=[{"name": "ops", "ssh_authorized_keys": r["users"][0]["ssh_authorized_keys"]}]), True),
    # reject direction
    "raid_level_true": (lambda r: r["ubuntu"]["storage"].update(layout="mdraid", raid_level=True), False),
    "mtu_fraction": (lambda r: r["network"]["interfaces"][0].update(mtu=1500.5), False),
    "dhcp4_int": (lambda r: r["network"]["interfaces"][0].update(dhcp4=1), False),
    "schema_version_float_ok": (lambda r: r.update(schema_version=1.0), True),
    "duplicate_groups": (lambda r: r["users"][0].update(groups=["sudo", "sudo"]), False),
    "disk_shell_metachar": (lambda r: r["storage"].update(disks=["/dev/x;rm${IFS}-rf${IFS}~;#"]), False),
    "disk_space": (lambda r: r["storage"].update(disks=["/dev/sd a"]), False),
    # prov stricter than the (Python-regex based) reference
    "hostname_trailing_newline": (lambda r: r.update(hostname="demo\n"), False),
    "disk_trailing_newline": (lambda r: r["storage"].update(disks=["/dev/vda\n"]), False),
    "gecos_tab": (lambda r: r["users"][0].update(gecos="a\tb"), False),
    "gecos_escape": (lambda r: r["users"][0].update(gecos="a\x1b[2Jb"), False),
}
STRICTER = {"hostname_trailing_newline", "disk_trailing_newline", "gecos_tab", "gecos_escape"}


@pytest.mark.parametrize("name", sorted(DIFFERENTIAL))
def test_differential_against_reference_validator(name):
    fn, expected = DIFFERENTIAL[name]
    raw = _mutate(fn)
    ours = not schema_errors(raw, published())
    reference = jsonschema.Draft202012Validator(published()).is_valid(raw)
    assert ours is expected
    if name in STRICTER:
        assert reference is True and ours is False
    else:
        assert reference is ours


def test_integral_floats_are_normalised_to_int():
    raw = _mutate(DIFFERENTIAL["raid_level_float"][0])
    raw["network"]["interfaces"][0]["mtu"] = 9000.0
    host, findings = check_host(raw, discover())
    assert host is not None, findings
    assert type(host["ubuntu"]["storage"]["raid_level"]) is int
    assert type(host["network"]["interfaces"][0]["mtu"]) is int
    assert "raidlevel: 1\n" in ubuntu.render(host)["user-data"]


def test_control_characters_rejected_everywhere():
    for value in ("demo\n", "de\rmo", "de\x00mo"):
        host, findings = check_host(dict(STARTER, hostname=value), discover())
        assert host is None and any("control characters" in f.message for f in findings)
    host, findings = check_host(_mutate(lambda r: r["network"].update({"dns": {"search": ["example.com\n"]}})), discover())
    assert host is None


@pytest.mark.parametrize(
    "pattern,text,ok",
    [
        ("^[$]$", "$", True),  # $ inside a class is a literal (was: re.error bad escape \\Z)
        ("^[$]$", "$\n", False),
        ("^a\\$$", "a$", True),  # escaped $ is a literal, the final $ an anchor
        ("^\\\\$", "\\", True),  # escaped backslash, then an anchor (review S-1)
        ("^\\\\$", "\\\n", False),
        ("^[]a]$", "]", True),
        ("^[^]x]$", "y", True),
        ("^[a-z]+$", "demo\n", False),
    ],
)
def test_ecma_dollar_semantics(pattern, text, ok):
    from prov.validate import ecma_search

    assert ecma_search(pattern, text) is ok


def test_unknown_keys_are_echoed_with_repr():
    raw = _mutate(lambda r: r.update({"evil\u202ekey": 1}))
    errors = schema_errors(raw, published())
    assert errors == ["<root>: unknown key 'evil\\u202ekey'"]  # bidi control escaped, not echoed raw


@pytest.mark.parametrize("char", ["\x85", "\x9b", " ", " ", "\x7f"])
def test_c1_controls_and_unicode_line_separators_are_rejected(char):
    host, findings = check_host(_mutate(lambda r: r["users"][0].update(gecos=f"a{char}b")), discover())
    assert host is None and any("control characters" in f.message for f in findings)
