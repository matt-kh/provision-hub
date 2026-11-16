"""The Finding type and a small JSON Schema validator for spec files.

The JSON Schema check is a built-in validator for the keyword subset spec schemas use (listed in ``SUPPORTED_KEYWORDS``), so prov has no runtime dependency on a JSON Schema
library. The test suite cross-checks it against the ``jsonschema`` reference validator.
"""

from __future__ import annotations

import copy
import functools
import re
from dataclasses import dataclass
from typing import Any, List, Mapping, Optional

from .schema import CONTROL_CHARS, short_repr


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

def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_TYPES = {
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, list),
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    # JSON Schema 2020-12: a number with a zero fractional part is an integer (1.0 is valid).
    "integer": lambda v: _is_number(v) and float(v).is_integer(),
    "number": _is_number,
}

_CONTROL = CONTROL_CHARS


def json_equal(a: Any, b: Any) -> bool:
    """JSON equality: true != 1, 1 == 1.0, containers compared element-wise."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if _is_number(a) and _is_number(b):
        return a == b
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def ecma_to_python(pattern: str) -> str:
    """Translate an ECMA-262 pattern's ``$`` to Python's end-of-string ``\\Z``.

    Python's ``$`` also matches before a trailing newline, so ``^[a-z]+$`` would accept
    ``"demo\\n"``. Only an unescaped ``$`` outside a character class is an anchor: a ``$``
    inside ``[...]`` or after an odd number of backslashes is a literal and stays as is.
    """
    out: List[str] = []
    in_class = False
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "\\":
            out.append(pattern[i : i + 2])  # escape pair: copied verbatim, never an anchor
            i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
        elif c == "[":
            in_class = True
            if pattern[i + 1 : i + 2] == "^":
                out.append("[^")
                i += 2
                if pattern[i : i + 1] == "]":  # leading ] is a literal in a class
                    out.append("]")
                    i += 1
                continue
            if pattern[i + 1 : i + 2] == "]":
                out.append("[]")
                i += 2
                continue
        elif c == "$":
            out.append("\\Z")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


@functools.lru_cache(maxsize=None)
def compile_pattern(pattern: str) -> "re.Pattern[str]":
    """Compile a JSON Schema ``pattern`` with ECMA ``$`` semantics (cached).

    ``re.ASCII`` keeps ``\\d`` and ``\\w`` to ASCII as in ECMA-262; prov's own patterns use
    explicit character classes only, so the flag never makes them looser.
    """
    return re.compile(ecma_to_python(pattern), re.ASCII)


def ecma_search(pattern: str, text: str) -> bool:
    """``re.search`` with ECMA-262 ``$`` semantics."""
    return compile_pattern(pattern).search(text) is not None


_COUNT_KEYWORDS = ("minLength", "maxLength", "minItems", "maxItems")


def check_schema_fragment(
    schema: Any,
    where: str = "<schema>",
    defs: Optional[Mapping[str, Any]] = None,
    allow_defs: bool = True,
) -> None:
    """Raise ValueError if a schema is not one prov's validator can evaluate safely.

    Checks keyword names *and* values (types, counts, patterns). ``defs`` are the
    definitions ``$ref`` may point at; with ``allow_defs=False`` (backend fragments, kind
    properties) a ``$defs`` of its own is refused, because only the shared ``$defs`` are
    composed. Run at plugin load so a broken backend ``SCHEMA`` is a clear error, not a crash
    later.
    """
    if not isinstance(schema, dict):
        raise ValueError(f"{where}: a schema must be an object, got {type(schema).__name__}")
    unknown = set(schema) - SUPPORTED_KEYWORDS
    if unknown:
        raise ValueError(f"{where}: unsupported keyword(s) {sorted(unknown)}")

    def fail(key: str, why: str) -> None:
        raise ValueError(f"{where}.{key}: {why}")

    if "type" in schema:
        types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not types or not all(isinstance(t, str) and t in _TYPES for t in types):
            fail("type", f"must be one of {sorted(_TYPES)} or a non-empty list of them")
    for key in _COUNT_KEYWORDS:
        if key in schema and not (type(schema[key]) is int and schema[key] >= 0):
            fail(key, "must be a non-negative integer")
    for key in ("minimum", "maximum"):
        if key in schema and not _is_number(schema[key]):
            fail(key, "must be a number")
    if "required" in schema and not (isinstance(schema["required"], list) and all(isinstance(k, str) for k in schema["required"])):
        fail("required", "must be a list of strings")
    if "enum" in schema and not (isinstance(schema["enum"], list) and schema["enum"]):
        fail("enum", "must be a non-empty list")
    if "allOf" in schema and not isinstance(schema["allOf"], list):
        fail("allOf", "must be a list")
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        fail("uniqueItems", "must be a boolean")
    if "additionalProperties" in schema and not isinstance(schema["additionalProperties"], (bool, dict)):
        fail("additionalProperties", "must be a boolean or a schema")
    for key in ("title", "description", "$schema", "$id"):
        if key in schema and not isinstance(schema[key], str):
            fail(key, "must be a string")
    for key in ("properties", "$defs"):
        if key in schema and not (isinstance(schema[key], dict) and all(isinstance(k, str) for k in schema[key])):
            fail(key, "must be an object keyed by strings")
    if "$defs" in schema and not allow_defs:
        fail("$defs", "fragments may not define $defs; reference the shared #/$defs/<name>")
    if "pattern" in schema:
        if not isinstance(schema["pattern"], str):
            fail("pattern", "must be a string")
        try:
            compile_pattern(schema["pattern"])
        except re.error as exc:
            fail("pattern", f"invalid regular expression {schema['pattern']!r}: {exc}")
    if "$ref" in schema:
        ref = schema["$ref"]
        if not (isinstance(ref, str) and ref.startswith("#/$defs/")):
            fail("$ref", "only '#/$defs/<name>' references are supported")
        if defs is not None and ref[len("#/$defs/"):] not in defs:
            fail("$ref", f"{ref!r} does not resolve (known: {sorted(defs)})")

    def sub(child: Any, name: str) -> None:
        check_schema_fragment(child, f"{where}.{name}", defs, allow_defs)

    for name, child in schema.get("properties", {}).items():
        sub(child, f"properties.{name}")
    for name, child in schema.get("$defs", {}).items():
        sub(child, f"$defs.{name}")
    for key in ("items", "if", "then"):
        if key in schema:
            sub(schema[key], key)
    if isinstance(schema.get("additionalProperties"), dict):
        sub(schema["additionalProperties"], "additionalProperties")
    for n, child in enumerate(schema.get("allOf", [])):
        sub(child, f"allOf[{n}]")


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
    if "const" in schema and not json_equal(instance, schema["const"]):
        errors.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and not any(json_equal(instance, e) for e in schema["enum"]):
        errors.append(f"{where}: {short_repr(instance)} is not one of {schema['enum']}")

    if isinstance(instance, str):
        if _CONTROL.search(instance):
            errors.append(f"{where}: control characters (newline, tab, ...) are not allowed")
        elif "pattern" in schema and not ecma_search(schema["pattern"], instance):
            errors.append(f"{where}: {short_repr(instance)} does not match {schema['pattern']}")
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{where}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{where}: longer than {schema['maxLength']}")
    if _is_number(instance):
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
                if any(json_equal(item, other) for other in seen):
                    errors.append(f"{where}: duplicate item {short_repr(item)}")
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
            if _CONTROL.search(key):
                errors.append(f"{where}: control characters in key {short_repr(key)}")
            elif key in props:
                errors += schema_errors(instance[key], props[key], root, _join(path, key))
            elif extra is False:
                errors.append(f"{where}: unknown key {short_repr(key)}")
            elif isinstance(extra, dict):
                errors += schema_errors(instance[key], extra, root, _join(path, key))

    for sub in schema.get("allOf", []):
        errors += schema_errors(instance, sub, root, path)
    if "if" in schema and not schema_errors(instance, schema["if"], root, path):
        errors += schema_errors(instance, schema.get("then", {}), root, path)
    return errors


def apply_defaults(instance: Any, schema: Mapping[str, Any], root: Mapping[str, Any] | None = None) -> Any:
    """Return a deep copy of a *valid* instance with schema ``default`` values filled in.

    Integral floats where the schema wants an integer (``mtu = 1500.0``, ``raid_level = 5.0``)
    are normalised to ``int``.
    """
    root = schema if root is None else root
    if "$ref" in schema:
        return apply_defaults(instance, _resolve(schema["$ref"], root), root)
    result = copy.deepcopy(instance)
    if isinstance(result, float) and result.is_integer():
        wants_int = schema.get("type") == "integer" or (
            "enum" in schema and all(_is_number(e) and float(e).is_integer() for e in schema["enum"])
        )
        if wants_int:
            return int(result)
    if isinstance(result, dict):
        for key, sub in schema.get("properties", {}).items():
            if key not in result and "default" in sub:
                result[key] = copy.deepcopy(sub["default"])
            if key in result:
                result[key] = apply_defaults(result[key], sub, root)
    elif isinstance(result, list) and "items" in schema:
        result = [apply_defaults(item, schema["items"], root) for item in result]
    return result
