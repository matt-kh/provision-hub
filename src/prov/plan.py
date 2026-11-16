"""``prov plan``: print the apply steps for a human. Never executes anything.

The backend's ``plan(spec, ctx)`` returns lines (shell commands and ``#`` comments) written
relative to the item's rendered directory; this module adds the common header and refuses
any line holding a line break or another unsafe character (``prov.text.unsafe``): a spec
value smuggling a newline could otherwise turn a comment into a command, and an escape or
bidi control could make a pasted line differ from what the terminal shows. Plan lines are
refused, never escaped, so what is printed is exactly what a human pastes.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List

from .registry import Context, call_plugin
from .schema import ConfigError
from .text import short_repr, unsafe


def plan_lines(plugin: Any, spec: Dict[str, Any], ctx: Context, ref: str, rendered_dir: Path) -> List[str]:
    lines = [
        f"# prov plan for {shlex.quote(ref)} ({plugin.NAME}) - prov never runs these steps;",
        f"# review them and run them yourself. Render first: prov render {shlex.quote(ref)}.",
        f"cd {shlex.quote(str(rendered_dir))}",
    ]
    for n, line in enumerate(lines):
        if _refused(line):
            raise ConfigError(f"plan header line {n} contains a line break or an unsafe character; refusing to print it: {short_repr(line)}")
    for n, line in enumerate(call_plugin(plugin, "plan", spec, ctx)):
        if _refused(line):
            raise ConfigError(
                f"backend {short_repr(plugin.NAME)} plan line {n} contains a line break or an unsafe character; "
                f"refusing to print it: {short_repr(line)}"
            )
        lines.append(line)
    return lines


def _refused(line: str) -> bool:
    """Plan lines must stay paste-able, so they are refused rather than escaped: a line break
    (``\\n``, ``\\r``) or any character ``prov.text.unsafe`` flags, except the tab."""
    return any(ch != "\t" and unsafe(ch) for ch in line)
