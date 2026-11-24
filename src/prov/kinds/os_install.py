"""One machine's operating-system install: host identity, users, network and whole disks.

The core keys every os-install backend reads; the backend adds its own table (for example
``[ubuntu-autoinstall]``) with release, packages and storage layout.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Dict, List

from ..schema import NAME_PATTERN
from ..text import short_repr
from ..validate import ERROR, WARNING, Finding
from .common import string, string_list

NAME = "os-install"
PLUGIN_API = 1
VERSIONS = ("v1alpha1",)

# The core property that should equal the item directory name; a mismatch is a warning.
NAME_KEY = "hostname"

PROPERTIES: Dict[str, Any] = {
    "hostname": string("Short host name (one DNS label).", pattern=NAME_PATTERN),
    "domain": string("DNS domain; the FQDN is hostname.domain.", pattern="^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$"),
    "arch": {"enum": ["amd64", "arm64"], "default": "amd64"},
    "firmware": {"enum": ["uefi", "bios"], "default": "uefi"},
    "timezone": string("IANA time zone.", pattern="^[A-Za-z0-9_+/-]+$", default="Etc/UTC"),
    "locale": string("POSIX locale.", pattern="^[A-Za-z0-9_.@-]+$", default="en_US.UTF-8"),
    "keyboard": string("Console keyboard layout.", pattern="^[a-z0-9_-]+$", default="us"),
    "users": {"type": "array", "description": "Local users; backends without local users ignore or reject them.", "items": {"$ref": "#/$defs/user"}, "default": []},
    "network": {
        "type": "object",
        "description": "Optional; backends say when they need it.",
        "additionalProperties": False,
        "required": ["interfaces"],
        "properties": {
            "interfaces": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/interface"}},
            "dns": {
                "type": "object",
                "additionalProperties": False,
                "default": {},
                "properties": {
                    "nameservers": string_list("DNS servers (IP addresses).", default=[]),
                    "search": string_list("DNS search domains.", default=[]),
                },
            },
        },
    },
    "storage": {
        "type": "object",
        "additionalProperties": False,
        "required": ["disks"],
        "properties": {
            "disks": string_list(
                "Whole disks the installer may claim and wipe, in order (first = boot disk).",
                item={"type": "string", "pattern": "^/dev/[A-Za-z0-9._:+/-]+$"},
                minItems=1,
            ),
        },
    },
}

REQUIRED = ["hostname", "storage"]

# Cross-spec references (reserved; none yet).
REFS: Dict[str, Any] = {}


def _network(network: Dict[str, Any]) -> List[Finding]:
    """Interface and nameserver checks of a present ``[network]`` table."""
    out: List[Finding] = []
    ifaces = network["interfaces"]
    names = [i["name"] for i in ifaces]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        out.append(Finding(ERROR, f"interface {short_repr(dup)} defined more than once", "network.interfaces"))
    for n, iface in enumerate(ifaces):
        where = f"network.interfaces[{n}]"
        families = set()
        for a, addr in enumerate(iface["addresses"]):
            if "/" not in addr:
                out.append(Finding(ERROR, f"{short_repr(addr)} needs a prefix length (CIDR form)", f"{where}.addresses[{a}]"))
                continue
            try:
                families.add(ipaddress.ip_interface(addr).version)
            except ValueError:
                out.append(Finding(ERROR, f"{short_repr(addr)} is not an IP address in CIDR form", f"{where}.addresses[{a}]"))
        for key, version in (("gateway4", 4), ("gateway6", 6)):
            if key not in iface:
                continue
            try:
                gw = ipaddress.ip_address(iface[key])
            except ValueError:
                out.append(Finding(ERROR, f"{short_repr(iface[key])} is not an IP address", f"{where}.{key}"))
                continue
            if gw.version != version:
                out.append(Finding(ERROR, f"{iface[key]} is not an IPv{version} address", f"{where}.{key}"))
            elif version not in families and not iface[f"dhcp{version}"]:
                out.append(Finding(WARNING, f"no static IPv{version} address on this interface", f"{where}.{key}"))
        if not iface["addresses"] and not iface["dhcp4"] and not iface["dhcp6"]:
            out.append(Finding(ERROR, "no addresses and DHCP disabled; the interface would stay down", where))
    for s, server in enumerate(network["dns"]["nameservers"]):
        try:
            ipaddress.ip_address(server)
        except ValueError:
            out.append(Finding(ERROR, f"{short_repr(server)} is not an IP address", f"network.dns.nameservers[{s}]"))
    return out


def semantic(spec: Dict[str, Any]) -> List[Finding]:
    """Checks JSON Schema cannot express (addresses, cross-field rules)."""
    out: List[Finding] = []
    if "network" in spec:
        out += _network(spec["network"])
    users = [u["name"] for u in spec["users"]]
    for dup in sorted({n for n in users if users.count(n) > 1}):
        out.append(Finding(ERROR, f"user {short_repr(dup)} defined more than once", "users"))
    return out


# --- conformance fixtures (docs/kinds.md "Conformance fixtures") ---------------------------
#
# Synthetic intent specs every os-install backend should render (or say it cannot): the
# expectations are tool-neutral strings any backend's files must contain. Static checks only;
# none of these has been installed. Exported to conformance/os-install.v1alpha1.fixtures.json.
#
# Each hostname equals its fixture name: prov conformance uses the fixture name as the item
# name, and os-install's NAME_KEY warns when they differ. Every entry is built by its own
# function call, so no dict or list is shared between fixtures (or between users).
#
# Limits, stated because they are not checked: firmware handling is not checked by content
# (there is no tool-neutral marker for BIOS against UEFI), and neither is the rule that the
# first disk is the boot disk; the fixtures check only that the hostname, the disks and the
# network values reach the rendered files.

_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE ops@example.net"
_DISK0 = "/dev/disk/by-id/virtio-EXAMPLE_DISK_SERIAL_0000"
_DISK1 = "/dev/disk/by-id/virtio-EXAMPLE_DISK_SERIAL_0001"


def _ops() -> Dict[str, Any]:
    """One sudo user with a fake key; no groups (sudo = true carries the intent, group names differ per OS)."""
    return {"name": "ops", "groups": [], "sudo": True, "ssh_authorized_keys": [_KEY]}


def _dhcp() -> Dict[str, Any]:
    return {"interfaces": [{"name": "enp1s0", "dhcp4": True}]}


def _spec(hostname: str, network: Dict[str, Any], disks: List[str], **extra: Any) -> Dict[str, Any]:
    return {"hostname": hostname, "users": [_ops()], "network": network, "storage": {"disks": list(disks)}, **extra}


FIXTURES: Dict[str, Any] = {
    "conf-uefi-static": {
        "spec": _spec(
            "conf-uefi-static",
            {
                "interfaces": [{"name": "enp1s0", "addresses": ["192.0.2.10/24"], "gateway4": "192.0.2.254"}],
                "dns": {"nameservers": ["192.0.2.53"], "search": ["example.org"]},
            },
            [_DISK0],
            domain="example.com",
            firmware="uefi",
        ),
        # The domain (example.com), the search domain (example.org) and the key comment
        # (example.net) differ, so each string is found only where that value is rendered.
        # "192.0.2.10" without its prefix length: backends spell the address differently
        # (192.0.2.10/24, or address + prefixLength); the gateway is .254 so that it is not
        # a substring of the address.
        "expect": {"contains": ["conf-uefi-static", "example.com", "example.org", _DISK0, "192.0.2.10", "192.0.2.254", "192.0.2.53", _KEY]},
    },
    "conf-uefi-dhcp": {
        "spec": _spec("conf-uefi-dhcp", _dhcp(), [_DISK0]),
        "expect": {"contains": ["conf-uefi-dhcp", _DISK0, _KEY], "absent": ["192.0.2."]},
    },
    # Firmware is not checked by content (no tool-neutral marker): this fixture proves only that
    # a backend accepts and renders firmware = "bios" with the same hostname, disk and key.
    "conf-bios-dhcp": {
        "spec": _spec("conf-bios-dhcp", _dhcp(), [_DISK0], firmware="bios"),
        "expect": {"contains": ["conf-bios-dhcp", _DISK0, _KEY]},
    },
    # Both disks must appear; which one boots is not checked by content.
    "conf-two-disks": {
        "spec": _spec("conf-two-disks", _dhcp(), [_DISK0, _DISK1]),
        "expect": {"contains": ["conf-two-disks", _DISK0, _DISK1]},
    },
    "conf-bad-gateway-family": {
        "spec": _spec(
            "conf-bad-gateway-family",
            {"interfaces": [{"name": "enp1s0", "addresses": ["192.0.2.10/24"], "gateway4": "2001:db8::1"}]},
            [_DISK0],
        ),
        "expect": {"valid": False, "error": "network.interfaces[0].gateway4: 2001:db8::1 is not an IPv4 address"},
    },
    "conf-duplicate-user": {
        "spec": {**_spec("conf-duplicate-user", _dhcp(), [_DISK0]), "users": [_ops(), _ops()]},
        "expect": {"valid": False, "error": "users: user 'ops' defined more than once"},
    },
}
