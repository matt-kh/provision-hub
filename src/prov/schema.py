"""Host schema v1: the public, versioned format of ``host.toml``.

The common part lives here; each distro contributes the JSON Schema fragment of its own
table (``[ubuntu]``, ``[talos]`` ...) through its plugin's ``SCHEMA``. ``host_schema()``
composes both. The published file ``schema/host.v1.schema.json`` is ``host_schema()`` over
the built-in distros, and a test keeps the two identical.

Compatibility policy (see CLAUDE.md): within ``schema_version = 1`` changes may only be
additive and optional (new optional keys, new enum values). Removing or renaming a key,
tightening a constraint or changing a meaning needs ``schema_version = 2`` and a migration
note.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, Mapping

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = (1,)
SCHEMA_ID = "https://raw.githubusercontent.com/matt-kh/provision-hub/main/schema/host.v1.schema.json"
DRAFT = "https://json-schema.org/draft/2020-12/schema"

HOSTNAME_PATTERN = "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"


class ConfigError(Exception):
    """A usage or configuration error (exit code 2)."""


def _string(description: str, **extra: Any) -> Dict[str, Any]:
    return {"type": "string", "description": description, **extra}


def _string_list(description: str, item: Dict[str, Any] | None = None, **extra: Any) -> Dict[str, Any]:
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
        "name": _string("Login name.", pattern="^[a-z_][a-z0-9_-]{0,31}$"),
        "gecos": _string("Full name / comment field."),
        "groups": _string_list("Supplementary groups.", default=[]),
        "shell": _string("Login shell.", pattern="^/", default="/bin/bash"),
        "sudo": {"type": "boolean", "description": "Passwordless sudo for this user.", "default": False},
        "ssh_authorized_keys": _string_list(
            "OpenSSH public keys (identity is ssh-key based by default).",
            item={
                "type": "string",
                "pattern": "^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(256|384|521)|sk-ssh-ed25519@openssh.com|sk-ecdsa-sha2-nistp256@openssh.com) [A-Za-z0-9+/=]+( .*)?$",
            },
            default=[],
        ),
        "password_hash": _string(
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
        "name": _string("Interface name after install (renamed via macaddress when given).", pattern="^[A-Za-z0-9._-]{1,15}$"),
        "macaddress": _string("Match the NIC by MAC address.", pattern="^([0-9a-f]{2}:){5}[0-9a-f]{2}$"),
        "dhcp4": {"type": "boolean", "default": False},
        "dhcp6": {"type": "boolean", "default": False},
        "addresses": _string_list("Static addresses in CIDR form (IPv4 or IPv6).", default=[]),
        "gateway4": _string("IPv4 default gateway."),
        "gateway6": _string("IPv6 default gateway."),
        "mtu": {"type": "integer", "minimum": 576, "maximum": 9216},
    },
}

COMMON_PROPERTIES: Dict[str, Any] = {
    "schema_version": {"const": SCHEMA_VERSION, "description": "Host schema version; required."},
    "hostname": _string("Short host name (one DNS label).", pattern=HOSTNAME_PATTERN),
    "domain": _string("DNS domain; the FQDN is hostname.domain.", pattern="^([a-z0-9]([a-z0-9-]*[a-z0-9])?\\.)+[a-z]{2,}$"),
    "distro": {"type": "string", "description": "Distro plugin that renders this host."},
    "arch": {"enum": ["amd64", "arm64"], "default": "amd64"},
    "firmware": {"enum": ["uefi", "bios"], "default": "uefi"},
    "timezone": _string("IANA time zone.", pattern="^[A-Za-z0-9_+/-]+$", default="Etc/UTC"),
    "locale": _string("POSIX locale.", pattern="^[A-Za-z0-9_.@-]+$", default="en_US.UTF-8"),
    "keyboard": _string("Console keyboard layout.", pattern="^[a-z0-9_-]+$", default="us"),
    "users": {"type": "array", "description": "Local users; distros without local users ignore or reject them.", "items": {"$ref": "#/$defs/user"}, "default": []},
    "network": {
        "type": "object",
        "additionalProperties": False,
        "required": ["interfaces"],
        "properties": {
            "interfaces": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/interface"}},
            "dns": {
                "type": "object",
                "additionalProperties": False,
                "default": {},
                "properties": {
                    "nameservers": _string_list("DNS servers (IP addresses).", default=[]),
                    "search": _string_list("DNS search domains.", default=[]),
                },
            },
        },
    },
    "storage": {
        "type": "object",
        "additionalProperties": False,
        "required": ["disks"],
        "properties": {
            "disks": _string_list(
                "Whole disks the installer may claim and wipe, in order (first = boot disk).",
                item={"type": "string", "pattern": "^/dev/[A-Za-z0-9._:+/-]+$"},
                minItems=1,
            ),
        },
    },
}

# Shared definitions every distro fragment may reference as "#/$defs/<name>".
DEFS: Dict[str, Any] = {"interface": INTERFACE, "user": USER}

COMMON_REQUIRED = ["schema_version", "hostname", "distro", "network", "storage"]


def host_schema(distros: Mapping[str, Any]) -> Dict[str, Any]:
    """Compose the full host JSON Schema for the given distro plugins (keyed by NAME)."""
    names = sorted(distros)
    properties = copy.deepcopy(COMMON_PROPERTIES)
    properties["distro"]["enum"] = names
    for name in names:
        properties[name] = copy.deepcopy(distros[name].SCHEMA)
    return {
        "$schema": DRAFT,
        "$id": SCHEMA_ID,
        "title": "prov host v1",
        "description": "One host.toml describes one machine; a distro plugin renders its install artefacts.",
        "type": "object",
        "additionalProperties": False,
        "required": list(COMMON_REQUIRED),
        "properties": properties,
        "allOf": [
            {
                "if": {"properties": {"distro": {"const": name}}, "required": ["distro"]},
                "then": {"required": [name]},
            }
            for name in names
        ],
        "$defs": copy.deepcopy(DEFS),
    }


def schema_json(schema: Mapping[str, Any]) -> str:
    """Canonical serialisation used for the published file."""
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


MAX_HOST_FILE_BYTES = 1024 * 1024


def read_toml(path: Path) -> Dict[str, Any]:
    """Parse a host.toml; every failure becomes a ConfigError (exit 2).

    host.toml must be a regular file of at most 1 MiB. Symlinks are refused outright (as are
    FIFOs and devices, which would block or never end): keep host files as plain files.
    """
    from .util import FileTooLargeError, NotRegularFileError, read_regular_bytes

    try:
        return tomllib.loads(read_regular_bytes(path, MAX_HOST_FILE_BYTES).decode("utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{path}: not found") from None
    except IsADirectoryError:
        raise ConfigError(f"{path}: is a directory, expected a file") from None
    except NotRegularFileError:
        raise ConfigError(f"{path}: not a regular file; symlinks and special files are refused") from None
    except FileTooLargeError:
        raise ConfigError(f"{path}: larger than {MAX_HOST_FILE_BYTES} bytes") from None
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{path}: not valid UTF-8: {exc}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc.strerror or exc}") from None
