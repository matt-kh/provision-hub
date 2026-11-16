"""Shared JSON Schema definitions for kinds and backend fragments.

Core properties of a kind and backend ``SCHEMA`` fragments reference these as
``"#/$defs/<name>"``; the composed schema of every kind carries all of them. Adding a
definition is additive; changing one changes every kind that uses it.
"""

from __future__ import annotations

from typing import Any, Dict


def string(description: str, **extra: Any) -> Dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def string_list(description: str, item: Dict[str, Any] | None = None, **extra: Any) -> Dict[str, Any]:
    return {
        "type": "array",
        "description": description,
        "items": item or {"type": "string"},
        "uniqueItems": True,
        **extra,
    }


USER = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {
        "name": string("Login name.", pattern="^[a-z_][a-z0-9_-]{0,31}$"),
        "gecos": string("Full name / comment field."),
        "groups": string_list("Supplementary groups.", default=[]),
        "shell": string("Login shell.", pattern="^/", default="/bin/bash"),
        "sudo": {"type": "boolean", "description": "Passwordless sudo for this user.", "default": False},
        "ssh_authorized_keys": string_list(
            "OpenSSH public keys (identity is ssh-key based by default).",
            item={
                "type": "string",
                "pattern": "^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com) [A-Za-z0-9+/=]+( .*)?$",
            },
            default=[],
        ),
        "password_hash": string(
            "Optional crypt(3) hash (yescrypt, SHA-512 or SHA-256). Plain passwords are never accepted.",
            pattern="^\\$(y|gy|6|5)\\$[./0-9A-Za-z$=,]+$",
        ),
    },
}

INTERFACE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name"],
    "properties": {
        "name": string("Interface name after install (renamed via macaddress when given).", pattern="^[A-Za-z0-9._-]{1,15}$"),
        "macaddress": string("Match the NIC by MAC address.", pattern="^([0-9a-f]{2}:){5}[0-9a-f]{2}$"),
        "dhcp4": {"type": "boolean", "default": False},
        "dhcp6": {"type": "boolean", "default": False},
        "addresses": string_list("Static addresses in CIDR form (IPv4 or IPv6).", default=[]),
        "gateway4": string("IPv4 default gateway."),
        "gateway6": string("IPv6 default gateway."),
        "mtu": {"type": "integer", "minimum": 576, "maximum": 9216},
    },
}

# Shared definitions every kind and backend fragment may reference as "#/$defs/<name>".
DEFS: Dict[str, Any] = {"interface": INTERFACE, "user": USER}
