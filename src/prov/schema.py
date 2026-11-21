"""Spec envelope and schema composition: the public, versioned format of ``spec.toml``.

Every item is ``<root>/<kind>/<name>/spec.toml``. A spec starts with the envelope
(``api_version``, ``kind``, ``backend``), then the kind's core keys (``src/prov/kinds/``),
then one table named after the backend (``[ubuntu-autoinstall]``) whose JSON Schema fragment
the backend contributes through its ``SCHEMA``. ``spec_schema()`` composes the three. The
published file ``schema/<kind>.<version>.schema.json`` is ``spec_schema()`` over the
built-in backends of that kind, and a test keeps the two identical.

This module knows nothing about any particular kind or backend: both are passed in.

Versioning policy (see docs/kinds.md and CLAUDE.md): ``prov/v1alpha*`` may break, with a
migration note in CHANGELOG.md. From 1.0 ``prov/v1`` is frozen to additive, optional
changes; anything breaking becomes ``prov/v2`` with a reader for v1.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping

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
        "required": list(ENVELOPE_KEYS) + list(kind.REQUIRED),
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


def schema_json(schema: Mapping[str, Any]) -> str:
    """Canonical serialisation used for the published file and ``prov schema``.

    ASCII only: any non-ASCII character (including a plugin description's controls or bidi
    marks) is a JSON ``\\uNNNN`` escape, so the output is safe to print and still valid JSON.
    """
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=True) + "\n"


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
