"""Spec schemas: published files in sync, round-trip with a reference validator, defaults."""

from __future__ import annotations

import copy
import json

import jsonschema
import pytest
from conftest import SCHEMA_DIR, UPDATE_GOLDENS, example_items

from prov import kinds
from prov.backends import ubuntu_autoinstall as ubuntu
from prov.kinds.common import DEFS
from prov.registry import Context, builtin, discover, for_kind
from prov.schema import ConfigError, read_toml, schema_file_name, schema_json, spec_schema, tomllib
from prov.spec import check_spec, load_spec
from prov.validate import ERROR, schema_errors

STARTER = tomllib.loads(ubuntu.starter("demo"))
OS_INSTALL = kinds.KINDS["os-install"]


def check_host(raw, backends=None):
    return check_spec(raw, "os-install", "demo", backends or discover())


def published(kind=OS_INSTALL):
    return json.loads((SCHEMA_DIR / schema_file_name(kind)).read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(kinds.KINDS))
def test_published_schema_matches_code(name):
    kind = kinds.KINDS[name]
    path = SCHEMA_DIR / schema_file_name(kind)
    text = schema_json(spec_schema(kind, for_kind(builtin(), name), DEFS))
    if UPDATE_GOLDENS:
        path.write_text(text, encoding="utf-8")
    assert path.read_text(encoding="utf-8") == text, "regenerate: PROV_UPDATE_GOLDENS=1 uv run pytest"


def test_no_stale_published_schemas():
    expected = {schema_file_name(k) for k in kinds.KINDS.values()}
    assert {p.name for p in SCHEMA_DIR.glob("*.json")} == expected


@pytest.mark.parametrize("name", sorted(kinds.KINDS))
def test_published_schema_is_valid_draft_2020_12(name):
    jsonschema.Draft202012Validator.check_schema(published(kinds.KINDS[name]))


@pytest.mark.parametrize("root,kind,name", example_items())
def test_examples_round_trip_against_published_schema(root, kind, name):
    raw = read_toml(root / kind / name / "spec.toml")
    schema = published(kinds.KINDS[kind])
    jsonschema.Draft202012Validator(schema).validate(raw)
    assert schema_errors(raw, schema) == []


def test_starter_validates():
    jsonschema.Draft202012Validator(published()).validate(STARTER)
    host, findings = check_host(STARTER)
    assert host is not None and not findings


def test_missing_api_version():
    raw = copy.deepcopy(STARTER)
    del raw["api_version"]
    with pytest.raises(ConfigError, match=r"api_version is required \(this prov supports: prov/v1alpha1\)"):
        check_host(raw)


@pytest.mark.parametrize("version", ["prov/v1", "prov/v1alpha2", "v1alpha1", 1, True])
def test_unknown_api_version(version):
    with pytest.raises(ConfigError, match="unsupported api_version"):
        check_host(dict(STARTER, api_version=version))


def test_load_spec_raises_config_error(tmp_path):
    path = tmp_path / "spec.toml"
    path.write_text('hostname = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="api_version is required"):
        load_spec(path, "os-install", "x", discover())
    path.write_text("not = [toml", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_spec(path, "os-install", "x", discover())
    path.write_text('api_version = "prov/v1alpha1"\nkind = "os-install"\nbackend = "ubuntu-autoinstall"\nhostname = "x"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid spec:\n  error: <root>: missing required key 'storage'"):
        load_spec(path, "os-install", "x", discover())


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
    "bad_layout": lambda r: r["ubuntu-autoinstall"]["storage"].update(layout="btrfs"),
    "missing_ubuntu_table": lambda r: r.pop("ubuntu-autoinstall"),
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
    host, findings = check_host(raw)
    assert host is None and findings and all(f.level == ERROR for f in findings)


SEMANTIC = {
    "address_without_prefix": (lambda r: r["network"]["interfaces"][0].update(addresses=["192.0.2.10"]), "CIDR"),
    "gateway_wrong_family": (lambda r: r["network"]["interfaces"][0].update(gateway4="2001:db8::1"), "not an IPv4"),
    "bad_nameserver": (lambda r: r["network"]["dns"].update(nameservers=["dns.example.com"]), "not an IP"),
    "no_ssh_keys": (lambda r: r["users"][0].update(ssh_authorized_keys=[]), "no login"),
    "no_users": (lambda r: r.pop("users"), "no login"),
    "interface_down": (lambda r: r["network"]["interfaces"].append({"name": "eth9"}), "stay down"),
    "zfs_two_disks": (lambda r: (r["ubuntu-autoinstall"]["storage"].update(layout="zfs"), r["storage"].update(disks=["/dev/vda", "/dev/vdb"])), "exactly one disk"),
    "raid1_one_disk": (lambda r: r["ubuntu-autoinstall"]["storage"].update(layout="mdraid"), "at least 2 disks"),
    "raid_level_on_lvm": (lambda r: r["ubuntu-autoinstall"]["storage"].update(raid_level=1), "mdraid' only"),
    "arm64_bios": (lambda r: r.update(arch="arm64", firmware="bios"), "UEFI only"),
    # network is optional in the kind; ubuntu-autoinstall renders netplan from it and says so
    "no_network": (lambda r: r.pop("network"), "ubuntu-autoinstall needs [network] with at least one interface"),
}


@pytest.mark.parametrize("name", sorted(SEMANTIC))
def test_semantic_errors(name):
    fn, needle = SEMANTIC[name]
    host, findings = check_host(_mutate(fn))
    assert host is None
    assert any(needle in f.message for f in findings), findings


def test_defaults_are_applied():
    raw = _mutate(lambda r: [r.pop(k) for k in ("arch", "firmware", "timezone", "locale", "keyboard")])
    host, _ = check_host(raw)
    assert (host["arch"], host["firmware"], host["timezone"], host["locale"], host["keyboard"]) == (
        "amd64", "uefi", "Etc/UTC", "en_US.UTF-8", "us",
    )
    assert host["users"][0]["shell"] == "/bin/bash" and host["ubuntu-autoinstall"]["updates"] == "security"


# Differential test: prov's validator and the jsonschema reference must agree on every
# mutated host, in both directions. The only allowed disagreement is STRICTER: prov applies
# ECMA-262 `$` semantics (Python's `$` also matches before a trailing newline, which the
# reference inherits) and rejects control characters in every string.
DIFFERENTIAL = {
    # accept direction
    "starter": (lambda r: None, True),
    "mtu_integral_float": (lambda r: r["network"]["interfaces"][0].update(mtu=1500.0), True),
    "mtu_int": (lambda r: r["network"]["interfaces"][0].update(mtu=1500), True),
    "raid_level_5": (lambda r: (r["ubuntu-autoinstall"]["storage"].update(layout="mdraid", raid_level=5), r["storage"].update(disks=["/dev/sda", "/dev/sdb", "/dev/sdc"])), True),
    "raid_level_float": (lambda r: (r["ubuntu-autoinstall"]["storage"].update(layout="mdraid", raid_level=1.0), r["storage"].update(disks=["/dev/sda", "/dev/sdb"])), True),
    "password_hash": (lambda r: r["users"][0].update(password_hash="$6$EXAMPLEsalt$EXAMPLEhash"), True),
    "disk_by_id": (lambda r: r["storage"].update(disks=["/dev/disk/by-id/nvme-EXAMPLE_SERIAL-part1"]), True),
    "no_dns": (lambda r: r["network"].pop("dns"), True),
    "no_network": (lambda r: r.pop("network"), True),
    "minimal_users": (lambda r: r.update(users=[{"name": "ops", "ssh_authorized_keys": r["users"][0]["ssh_authorized_keys"]}]), True),
    # reject direction
    "network_without_interfaces": (lambda r: r.update(network={"dns": {"nameservers": ["192.0.2.53"]}}), False),
    "network_empty_interfaces": (lambda r: r["network"].update(interfaces=[]), False),
    "raid_level_true": (lambda r: r["ubuntu-autoinstall"]["storage"].update(layout="mdraid", raid_level=True), False),
    "mtu_fraction": (lambda r: r["network"]["interfaces"][0].update(mtu=1500.5), False),
    "dhcp4_int": (lambda r: r["network"]["interfaces"][0].update(dhcp4=1), False),
    "api_version_other": (lambda r: r.update(api_version="prov/v2"), False),
    "kind_other": (lambda r: r.update(kind="image"), False),
    "backend_unknown": (lambda r: r.update(backend="plan9"), False),
    "other_backend_table_missing": (lambda r: r.pop("ubuntu-autoinstall"), False),
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


def test_kind_semantic_skips_a_missing_network():
    from prov.kinds import os_install

    raw = _mutate(lambda r: r.pop("network"))
    assert os_install.semantic(dict(raw, users=[])) == []


def test_integral_floats_are_normalised_to_int():
    raw = _mutate(DIFFERENTIAL["raid_level_float"][0])
    raw["network"]["interfaces"][0]["mtu"] = 9000.0
    host, findings = check_host(raw)
    assert host is not None, findings
    assert type(host["ubuntu-autoinstall"]["storage"]["raid_level"]) is int
    assert type(host["network"]["interfaces"][0]["mtu"]) is int
    assert "raidlevel: 1\n" in ubuntu.render(host, Context("demo"))["user-data"]


def test_control_characters_rejected_everywhere():
    for value in ("demo\n", "de\rmo", "de\x00mo"):
        host, findings = check_host(dict(STARTER, hostname=value))
        assert host is None and any("control characters" in f.message for f in findings)
    host, findings = check_host(_mutate(lambda r: r["network"].update({"dns": {"search": ["example.com\n"]}})))
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
    host, findings = check_host(_mutate(lambda r: r["users"][0].update(gecos=f"a{char}b")))
    assert host is None and any("control characters" in f.message for f in findings)


def test_spec_values_echoed_in_findings_are_capped():
    huge = "Z" * 5000
    for fn in (
        lambda r: r.update(arch=huge),
        lambda r: r.update(hostname=huge),
        lambda r: r.update({huge: 1}),
        lambda r: r["users"][0].update(groups=[huge, huge]),
    ):
        host, findings = check_host(_mutate(fn))
        assert host is None and findings and all(len(str(f)) < 400 for f in findings), findings
