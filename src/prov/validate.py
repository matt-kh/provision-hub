"""Host validation: schema version, JSON Schema, semantic checks; plus the Finding type.

The JSON Schema check is a small built-in validator for the keyword subset the host schema
uses (listed in ``SUPPORTED_KEYWORDS``), so prov has no runtime dependency on a JSON Schema
library. The test suite cross-checks it against the ``jsonschema`` reference validator.
"""

from __future__ import annotations

import copy
import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Tuple

from .schema import SUPPORTED_SCHEMA_VERSIONS, ConfigError, host_schema, read_toml

ERROR = "error"
WARNING = "warning"
SKIPPED = "skipped"
LEVELS = (ERROR, WARNING, SKIPPED)

SUPPORTED_KEYWORDS = {
    "$schema", "$id", "$ref", "$defs", "title", "description", "default",
    "type", "enum", "const", "pattern", "minLength", "maxLength", "minimum", "maximum",
    "properties", "required", "additionalProperties", "items", "minItems", "maxItems",
    "uniqueItems", "allOf", "if", "then",
}  # fmt: skip


@dataclass(frozen=True, order=True)
class Finding:
    """One validation result. ``level`` is error | warning | skipped."""

    level: str
    message: str
    where: str = ""

    def __str__(self) -> str:
        return f"{self.level}: {self.where + ': ' if self.where else ''}{self.message}"


def has_errors(findings: List[Finding]) -> bool:
    return any(f.level == ERROR for f in findings)


# --- JSON Schema subset -----------------------------------------------------------------

_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
}


def _join(path: str, key: Any) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _resolve(ref: str, root: Mapping[str, Any]) -> Mapping[str, Any]:
    if not ref.startswith("#/"):
        raise ValueError(f"unsupported $ref {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def schema_errors(instance: Any, schema: Mapping[str, Any], root: Mapping[str, Any] | None = None, path: str = "") -> List[str]:
    """Validate ``instance`` against ``schema`` (subset of draft 2020-12); return messages."""
    root = schema if root is None else root
    unknown = set(schema) - SUPPORTED_KEYWORDS
    if unknown:
        raise ValueError(f"schema at {path or '<root>'} uses unsupported keyword(s): {sorted(unknown)}")
    where = path or "<root>"
    if "$ref" in schema:
        return schema_errors(instance, _resolve(schema["$ref"], root), root, path)

    errors: List[str] = []
    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[t](instance) for t in types):
            return [f"{where}: expected {' or '.join(types)}, got {type(instance).__name__}"]
    if "const" in schema and instance != schema["const"]:
        errors.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{where}: {instance!r} is not one of {schema['enum']}")

    if isinstance(instance, str):
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{where}: {instance!r} does not match {schema['pattern']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']}")
    if _TYPES["number"](instance):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{where}: {instance} is less than {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{where}: {instance} is greater than {schema['maximum']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{where}: needs at least {schema['minItems']} item(s)")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{where}: allows at most {schema['maxItems']} item(s)")
        if schema.get("uniqueItems"):
            seen: List[Any] = []
            for item in instance:
                if item in seen:
                    errors.append(f"{where}: duplicate item {item!r}")
                    break
                seen.append(item)
        if "items" in schema:
            for i, item in enumerate(instance):
                errors += schema_errors(item, schema["items"], root, _join(path, i))

    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{where}: missing required key {key!r}")
        props = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key in sorted(instance):
            if key in props:
                errors += schema_errors(instance[key], props[key], root, _join(path, key))
            elif extra is False:
                errors.append(f"{_join(path, key)}: unknown key")
            elif isinstance(extra, dict):
                errors += schema_errors(instance[key], extra, root, _join(path, key))

    for sub in schema.get("allOf", []):
        errors += schema_errors(instance, sub, root, path)
    if "if" in schema and not schema_errors(instance, schema["if"], root, path):
        errors += schema_errors(instance, schema.get("then", {}), root, path)
    return errors


def apply_defaults(instance: Any, schema: Mapping[str, Any], root: Mapping[str, Any] | None = None) -> Any:
    """Return a deep copy of a *valid* instance with schema ``default`` values filled in."""
    root = schema if root is None else root
    if "$ref" in schema:
        return apply_defaults(instance, _resolve(schema["$ref"], root), root)
    result = copy.deepcopy(instance)
    if isinstance(result, dict):
        for key, sub in schema.get("properties", {}).items():
            if key not in result and "default" in sub:
                result[key] = copy.deepcopy(sub["default"])
            if key in result:
                result[key] = apply_defaults(result[key], sub, root)
    elif isinstance(result, list) and "items" in schema:
        result = [apply_defaults(item, schema["items"], root) for item in result]
    return result


# --- host checks ------------------------------------------------------------------------


def _semantic(host: Dict[str, Any]) -> List[Finding]:
    """Checks JSON Schema cannot express (addresses, cross-field rules)."""
    out: List[Finding] = []
    ifaces = host["network"]["interfaces"]
    names = [i["name"] for i in ifaces]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        out.append(Finding(ERROR, f"interface {dup!r} defined more than once", "network.interfaces"))
    for n, iface in enumerate(ifaces):
        where = f"network.interfaces[{n}]"
        families = set()
        for a, addr in enumerate(iface["addresses"]):
            if "/" not in addr:
                out.append(Finding(ERROR, f"{addr!r} needs a prefix length (CIDR form)", f"{where}.addresses[{a}]"))
                continue
            try:
                families.add(ipaddress.ip_interface(addr).version)
            except ValueError:
                out.append(Finding(ERROR, f"{addr!r} is not an IP address in CIDR form", f"{where}.addresses[{a}]"))
        for key, version in (("gateway4", 4), ("gateway6", 6)):
            if key not in iface:
                continue
            try:
                gw = ipaddress.ip_address(iface[key])
            except ValueError:
                out.append(Finding(ERROR, f"{iface[key]!r} is not an IP address", f"{where}.{key}"))
                continue
            if gw.version != version:
                out.append(Finding(ERROR, f"{iface[key]} is not an IPv{version} address", f"{where}.{key}"))
            elif version not in families and not iface[f"dhcp{version}"]:
                out.append(Finding(WARNING, f"no static IPv{version} address on this interface", f"{where}.{key}"))
        if not iface["addresses"] and not iface["dhcp4"] and not iface["dhcp6"]:
            out.append(Finding(ERROR, "no addresses and DHCP disabled; the interface would stay down", where))
    for s, server in enumerate(host["network"]["dns"]["nameservers"]):
        try:
            ipaddress.ip_address(server)
        except ValueError:
            out.append(Finding(ERROR, f"{server!r} is not an IP address", f"network.dns.nameservers[{s}]"))

    users = [u["name"] for u in host["users"]]
    for dup in sorted({n for n in users if users.count(n) > 1}):
        out.append(Finding(ERROR, f"user {dup!r} defined more than once", "users"))
    return out


def check_host(raw: Mapping[str, Any], distros: Mapping[str, Any]) -> Tuple[Dict[str, Any] | None, List[Finding]]:
    """Validate a parsed host.toml. Returns (host with defaults or None, findings)."""
    supported = ", ".join(str(v) for v in SUPPORTED_SCHEMA_VERSIONS)
    if "schema_version" not in raw:
        return None, [Finding(ERROR, f"schema_version is required (this prov supports: {supported})", "schema_version")]
    version = raw["schema_version"]
    if isinstance(version, bool) or version not in SUPPORTED_SCHEMA_VERSIONS:
        return None, [
            Finding(
                ERROR,
                f"unsupported schema_version {version!r} (this prov supports: {supported}); "
                "use a prov release that supports it or migrate the file",
                "schema_version",
            )
        ]
    distro = raw.get("distro")
    if isinstance(distro, str) and distro not in distros:
        known = ", ".join(sorted(distros)) or "none"
        return None, [Finding(ERROR, f"unknown distro {distro!r} (registered: {known})", "distro")]

    schema = host_schema(distros)
    findings = [Finding(ERROR, msg) for msg in schema_errors(raw, schema)]
    if findings:
        return None, findings
    host = apply_defaults(raw, schema)
    findings = _semantic(host)
    plugin = distros[host["distro"]]
    if hasattr(plugin, "check"):
        findings += list(plugin.check(host))
    if has_errors(findings):
        return None, findings
    return host, findings


def load_host(path: Path, distros: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[Finding]]:
    """Read and validate host.toml; raise ConfigError listing every error."""
    host, findings = check_host(read_toml(path), distros)
    if host is None:
        lines = "\n".join(f"  {f}" for f in findings if f.level == ERROR)
        raise ConfigError(f"{path}: invalid host:\n{lines}")
    return host, findings
