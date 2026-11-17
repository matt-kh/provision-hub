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
