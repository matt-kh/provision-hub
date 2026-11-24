"""Spec envelope and schema composition: the public, versioned format of ``spec.toml``.

Every item is ``<root>/<kind>/<name>/spec.toml``. A spec starts with the envelope
(``api_version``, ``kind``, ``backend``), then the kind's core keys (``src/prov/kinds/``),
then one table named after the backend (``[ubuntu-autoinstall]``) whose JSON Schema fragment
the backend contributes through its ``SCHEMA``, and, for a kind that declares ``REFS``, the
optional envelope table ``[refs]`` (alias -> ``"<kind>/<name>"``, ``prov.refs``), and the
optional envelope table ``[state]`` (lifecycle intent and state reporting, ``prov.state``) in every kind.
``spec_schema()`` composes them. The
published file ``schema/<kind>.<version>.schema.json`` is ``spec_schema()`` over the
built-in backends of that kind, and a test keeps the two identical.

This module knows nothing about any particular kind or backend: both are passed in.

Versioning policy (see docs/kinds.md and CLAUDE.md): ``prov/v1alpha*`` may break, with a
migration note in CHANGELOG.md. From 1.0 ``prov/v1`` is frozen to additive, optional
changes; anything breaking becomes ``prov/v2`` with a reader for v1.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .text import json_text

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on 3.10 only
    import tomli as tomllib

API_GROUP = "prov"
DRAFT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_BASE_URL = "https://raw.githubusercontent.com/matt-kh/provision-hub/main/schema/"
SPEC_FILE = "spec.toml"

# Item names keep the host-name shape (one DNS label); backend names and prov's own kind
# names are ids.
NAME_PATTERN = "^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
ID_PATTERN = "^[a-z][a-z0-9-]*$"
# Kind names: a bare id (reserved for kinds of the provision-hub distribution), or a dotted
# third-party name ``<label>.<group>`` (``registry.example.org``) whose group is a dotted DNS
# name (Kubernetes CRD style: ``example.org``, never just ``org``) and the kind's api group
# (``example.org/v1alpha1``). At most KIND_MAX_LENGTH characters.
_LABEL = "[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
KIND_PATTERN = f"^([a-z][a-z0-9-]*|[a-z]([a-z0-9-]{{0,61}}[a-z0-9])?(\\.{_LABEL}){{2,}})$"
KIND_MAX_LENGTH = 253

ENVELOPE_KEYS = ("api_version", "kind", "backend")
# Optional envelope tables: ``[refs]`` (alias -> "<kind>/<name>"), present in a kind's schema
# only when the kind declares REFS, and ``[state]`` (the item's lifecycle intent, read by
# ``prov status``; docs/state.md), present in every kind's schema. Reserved like the envelope
# keys: no kind property and no backend NAME may use them.
OPTIONAL_ENVELOPE_KEYS = ("refs", "state")
# A duration as ``prov.state.parse_duration`` and systemd read it: ``<n>s|m|h|d``.
DURATION_PATTERN = "^[1-9][0-9]{0,5}[smhd]$"
# A known_hosts line without a marker (``@cert-authority``/``@revoked``): host patterns, key
# type, base64 key. No comment: a line is one key.
KNOWN_HOSTS_PATTERN = (
    "^[A-Za-z0-9.:,*?|=+/_\\[\\]-]+ "
    "(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|ecdsa-sha2-nistp521|"
    "sk-ssh-ed25519@openssh\\.com|sk-ecdsa-sha2-nistp256@openssh\\.com) [A-Za-z0-9+/]+={0,2}$"
)
# An ssh git URL: ``ssh://user@host[:port]/path`` or scp-like ``user@host:path``. No part may
# start with ``-`` (it would become an ssh or git option), and nothing outside these
# characters (no spaces, quotes or shell syntax).
SSH_REPO_PATTERN = (
    "^(ssh://[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*(:[0-9]{1,5})?/[A-Za-z0-9._~][A-Za-z0-9._~/-]*"
    "|[A-Za-z0-9_][A-Za-z0-9._-]*@[A-Za-z0-9][A-Za-z0-9.-]*:[A-Za-z0-9._~][A-Za-z0-9._~/-]*)$"
)
# ``[state.report]``: the host reports its observed state (docs/state.md). Rendered by a
# backend that declares ``STATE_AGENT = True``; core checks it (``prov.state.report_findings``).
STATE_REPORT_TABLE: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repo", "known_hosts"],
    "description": "Render a report-only state agent: the host writes its state record to a state repository.",
    "properties": {
        "repo": {
            "type": "string",
            "pattern": SSH_REPO_PATTERN,
            "maxLength": 512,
            "description": "ssh git URL of the state repository (ssh://user@host/path or user@host:path); the host needs write access.",
        },
        "branch": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_]+([./-][A-Za-z0-9_]+)*$",
            "maxLength": 100,
            "default": "main",
            "description": "Branch of the state repository the agent writes to.",
        },
        "interval": {
            "type": "string",
            "pattern": DURATION_PATTERN,
            "default": "15m",
            "description": "How often the agent checks (<n>s|m|h|d).",
        },
        "heartbeat": {
            "type": "string",
            "pattern": DURATION_PATTERN,
            "default": "6h",
            "description": "Push at least this often even when nothing but last_seen changed (<n>s|m|h|d).",
        },
        "identity_file": {
            "type": "string",
            "pattern": "^/[A-Za-z0-9._/-]+$",
            "maxLength": 256,
            "default": "/var/lib/prov-state/id_ed25519",
            "description": "Path ON THE HOST of the ssh deploy key; a human places it there, prov never renders a key.",
        },
        "known_hosts": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {"type": "string", "pattern": KNOWN_HOSTS_PATTERN, "maxLength": 2048},
            "description": "known_hosts lines (public host keys) of the repository's ssh server; host-key checking stays on.",
        },
    },
}
# The ``[state]`` envelope table. Kind-neutral and optional. ``decommissioned`` never changes
# a render; ``report`` does (the backend renders the agent).
STATE_TABLE: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "description": "Lifecycle intent and state reporting of the item (docs/state.md).",
    "properties": {
        "decommissioned": {
            "type": "boolean",
            "description": "The item is retired: prov status shows it as decommissioned whatever its host still reports.",
        },
        "report": STATE_REPORT_TABLE,
    },
}
# A reference alias (a key of ``[refs]`` and of a kind's REFS): a lowercase id that may hold
# ``_`` as well as ``-``, at most 63 characters.
ALIAS_PATTERN = "^[a-z][a-z0-9_-]{0,62}$"
# Top-level keys of the pre-v1alpha1 host.toml format; reserved, and answered with a migration hint.
LEGACY_KEYS = ("schema_version", "distro")
# Tables of that format; a hint only when api_version is missing (a backend could own the name later).
LEGACY_TABLES = ("ubuntu",)


class ConfigError(Exception):
    """A usage or configuration error (exit code 2)."""


def kind_group(name: str) -> str:
    """The api group of a kind name: ``prov`` for a bare name, the domain part of a dotted one.

    ``os-install`` -> ``prov``; ``registry.example.org`` -> ``example.org``.
    """
    _, dot, domain = name.partition(".")
    return domain if dot else API_GROUP


def api_versions(kind: Any) -> List[str]:
    """The ``api_version`` values a kind accepts, oldest first (``prov/v1alpha1``, ``example.org/v1alpha1``)."""
    group = kind_group(kind.NAME)
    return [f"{group}/{v}" for v in kind.VERSIONS]


def schema_id(kind: Any) -> str:
    """The ``$id`` of a kind's composed schema.

    A kind's ``SCHEMA_URL`` is a base URL (``https://.../``) to which the file name
    ``<NAME>.<version>.schema.json`` is appended, like ``SCHEMA_BASE_URL`` for the published
    files of provision-hub's bare kinds; a dotted kind without one gets
    ``urn:prov:schema:<NAME>:<version>``.
    """
    base = getattr(kind, "SCHEMA_URL", None)
    if base is not None:
        return base + schema_file_name(kind)
    if "." not in kind.NAME:
        return SCHEMA_BASE_URL + schema_file_name(kind)
    return f"urn:prov:schema:{kind.NAME}:{kind.VERSIONS[-1]}"


def schema_file_name(kind: Any) -> str:
    """``<kind>.<current version>.schema.json``, the published file of a kind."""
    return f"{kind.NAME}.{kind.VERSIONS[-1]}.schema.json"


def spec_schema(kind: Any, backends: Mapping[str, Any], defs: Mapping[str, Any]) -> Dict[str, Any]:
    """Compose the JSON Schema of one kind for the given backends (keyed by NAME).

    ``backends`` should hold backends of ``kind`` only; ``defs`` are the shared definitions
    (``prov.kinds.common.DEFS``) that core properties and backend fragments ``$ref``.
    """
    names = sorted(backends)
    backend: Dict[str, Any] = {"type": "string", "description": "Backend that renders this spec; names the backend table."}
    if names:
        backend["enum"] = names
    properties: Dict[str, Any] = {
        "api_version": {"const": api_versions(kind)[-1], "description": "Spec format version; required."},
        "kind": {"const": kind.NAME, "description": "Kind of item; equals the directory <root>/<kind>/."},
        "backend": backend,
    }
    properties.update(copy.deepcopy(dict(kind.PROPERTIES)))
    required = list(ENVELOPE_KEYS) + list(kind.REQUIRED)
    refs = refs_schema(kind.REFS)  # checked by prov.kinds.check_kind
    if refs is not None:
        properties["refs"] = refs
        if refs.get("required"):
            required.append("refs")
    properties["state"] = copy.deepcopy(STATE_TABLE)
    for name in names:
        properties[name] = copy.deepcopy(backends[name].SCHEMA)
    version = kind.VERSIONS[-1]
    doc = (getattr(kind, "__doc__", None) or "").strip().splitlines()
    return {
        "$schema": DRAFT,
        "$id": schema_id(kind),
        "title": f"prov {kind.NAME} {version}",
        "description": doc[0] if doc else f"prov {kind.NAME} spec.",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
        "allOf": [
            {
                "if": {"properties": {"backend": {"const": name}}, "required": ["backend"]},
                "then": {"required": [name]},
            }
            for name in names
        ],
        "$defs": copy.deepcopy(dict(defs)),
    }


def ref_pattern(kind_name: str) -> str:
    """The pattern of a ref to an item of ``kind_name``: ``^registry\\.example\\.org/<item name>$``.

    Kind names hold only ``[a-z0-9.-]``, so escaping the dots is enough (and keeps the pattern
    valid ECMA-262, where ``\\-`` outside a class is not).
    """
    return "^" + kind_name.replace(".", "\\.") + "/" + NAME_PATTERN.strip("^$") + "$"


def refs_schema(declared: Mapping[str, Any]) -> Any:
    """The ``refs`` property for a kind's ``REFS``, or None when it declares none.

    One string property per alias, its pattern pinning the target kind; unknown aliases are
    refused; aliases declared ``required`` are required (and then the table itself).
    ``required`` defaults to false and ``description`` to none (``prov.kinds.declared_refs``).
    """
    if not declared:
        return None

    def prop(decl: Mapping[str, Any]) -> Dict[str, Any]:
        out: Dict[str, Any] = {"type": "string", "pattern": ref_pattern(decl["kind"])}
        if decl.get("description"):
            out["description"] = decl["description"]
        return out

    schema: Dict[str, Any] = {
        "type": "object",
        "additionalProperties": False,
        "description": 'References to other items: alias = "<kind>/<name>".',
        "properties": {
            alias: prop(decl) for alias, decl in sorted(declared.items())
        },
    }
    required = sorted(alias for alias, decl in declared.items() if decl.get("required", False))
    if required:
        schema["required"] = required
    return schema


def schema_json(schema: Mapping[str, Any]) -> str:
    """Canonical serialisation used for the published file and ``prov schema`` (``prov.text.json_text``).

    ASCII only: any non-ASCII character (including a plugin description's controls or bidi
    marks) is a JSON ``\\uNNNN`` escape, so the output is safe to print and still valid JSON.
    """
    return json_text(schema)


MAX_SPEC_FILE_BYTES = 1024 * 1024


def read_toml(path: Path) -> Dict[str, Any]:
    """Parse a spec.toml; every failure becomes a ConfigError (exit 2).

    spec.toml must be a regular file of at most 1 MiB. Symlinks are refused outright (as are
    FIFOs and devices, which would block or never end): keep spec files as plain files.
    """
    from .text import esc, show
    from .util import FileTooLargeError, NotRegularFileError, read_regular_bytes

    shown = show(str(path))

    try:
        return tomllib.loads(read_regular_bytes(path, MAX_SPEC_FILE_BYTES).decode("utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"{shown}: not found") from None
    except IsADirectoryError:
        raise ConfigError(f"{shown}: is a directory, expected a file") from None
    except NotRegularFileError:
        raise ConfigError(f"{shown}: not a regular file; symlinks and special files are refused") from None
    except FileTooLargeError:
        raise ConfigError(f"{shown}: larger than {MAX_SPEC_FILE_BYTES} bytes") from None
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{shown}: not valid UTF-8: {esc(str(exc))}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{shown}: invalid TOML: {esc(str(exc))}") from None
    except OSError as exc:
        raise ConfigError(f"{shown}: cannot read: {esc(str(exc.strerror or exc))}") from None
