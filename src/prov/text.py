"""Output escaping: the one place that decides which characters prov may print raw.

Everything prov prints that it did not compose itself (file and directory names, plugin
findings, exception text, tool output, spec values) passes through these helpers, so a
hostile name or message can never forge output lines, move the cursor, switch terminal
modes, reorder text (bidi controls) or crash a strict output encoding (lone surrogates from
non-UTF-8 file names).

- ``unsafe(ch)``: not printable (``str.isprintable``) — C0/C1 controls, DEL, lone
  surrogates, format characters (Cf: bidi overrides, zero-width), line/paragraph separators,
  other separators except the ASCII space, unassigned code points.
- ``esc(text, keep="")``: replace each unsafe character (outside ``keep``) by its
  ``\\xNN`` / ``\\uNNNN`` / ``\\UNNNNNNNN`` escape; safe text is unchanged.
- ``show(name)``: ``esc`` for names (no character kept, so a name is always one line).
- ``short_repr(value)``: ``repr`` of a spec value for messages, with a capped output length.
- ``json_text(value)``: the one JSON serialisation prov prints or writes (``--json``, schemas).
"""

from __future__ import annotations

import json
from typing import Any

REPR_LIMIT = 80


def unsafe(ch: str) -> bool:
    """True for a character prov never prints raw (see the module docstring)."""
    return not ch.isprintable()


def has_unsafe(text: str) -> bool:
    return not text.isprintable()


def _escape(ch: str) -> str:
    code = ord(ch)
    if code <= 0xFF:
        return f"\\x{code:02x}"
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def esc(text: str, keep: str = "") -> str:
    """``text`` with every unsafe character not in ``keep`` replaced by its escape."""
    if text.isprintable():
        return text
    return "".join(ch if ch.isprintable() or ch in keep else _escape(ch) for ch in text)


def show(name: str) -> str:
    """A file, directory or other filesystem-derived name as one printable line."""
    return esc(name)


def short_repr(value: object, limit: int = REPR_LIMIT) -> str:
    """``repr(value)`` for messages; the output is at most ``limit`` + 3 characters.

    A string is shortened *before* ``repr`` until its repr fits, so an escape sequence is
    never cut in half, and "…" marks the cut. Other values are repr'd, escaped and cut.
    ``repr`` escapes every non-printable character of a string.
    """
    if isinstance(value, str):
        n = min(len(value), limit)
        while n > 0 and len(repr(value[:n])) > limit + 2:
            n -= 1
        text = repr(value[:n])
        return text if n == len(value) else text + "…"
    text = esc(repr(value))
    return text if len(text) <= limit + 2 else text[: limit + 2] + "…"


def json_text(value: Any) -> str:
    """Canonical JSON: sorted keys, indent 2, ASCII only, one trailing newline.

    Used for every JSON document prov prints (``--json``, ``prov schema``) or publishes
    (``schema/*.schema.json``). Values go in raw, never ``esc``-ed: ``ensure_ascii`` is the
    printing boundary, turning every non-ASCII character (controls, bidi marks, lone
    surrogates from non-UTF-8 names) into a JSON ``\\uNNNN`` escape, so the text is printable
    ASCII (every character outside space to ``~``, DEL included, is escaped) apart from its
    newlines, and still decodes to the exact values.
    """
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
