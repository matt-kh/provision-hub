"""``prov plan``: print the apply steps for a human. Never executes anything.

The backend's ``plan(spec, ctx)`` returns lines (shell commands and ``#`` comments) written
relative to the item's rendered directory; this module adds the common header and refuses
any line that would print as more than one line (a spec value smuggling a newline could
otherwise turn a comment into a command).
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List

from .registry import Context, call_plugin
from .schema import ConfigError


def plan_lines(plugin: Any, spec: Dict[str, Any], ctx: Context, ref: str, rendered_dir: Path) -> List[str]:
    lines = [
        f"# prov plan for {shlex.quote(ref)} ({plugin.NAME}) - prov never runs these steps;",
        f"# review them and run them yourself. Render first: prov render {shlex.quote(ref)}.",
        f"cd {shlex.quote(str(rendered_dir))}",
    ]
    lines.extend(call_plugin(plugin, "plan", spec, ctx))
    for n, line in enumerate(lines):
        if "\n" in line or "\r" in line:
            raise ConfigError(f"plan line {n} contains a line break; refusing to print it: {line!r}")
    return lines
