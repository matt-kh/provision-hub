"""``prov plan``: print the apply steps for a human. Never executes anything.

The plugin's ``plan(host)`` returns lines (shell commands and ``#`` comments) written
relative to the host's rendered directory; this module adds the common header and refuses
any line that would print as more than one line (a host value smuggling a newline could
otherwise turn a comment into a command).
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List

from .schema import ConfigError


def plan_lines(plugin: Any, host: Dict[str, Any], rendered_dir: Path) -> List[str]:
    lines = [
        f"# prov plan for {shlex.quote(str(host['hostname']))} ({plugin.NAME}) - prov never runs these steps;",
        "# review them and run them yourself. Render first: prov render <host>.",
        f"cd {shlex.quote(str(rendered_dir))}",
    ]
    lines.extend(str(line) for line in plugin.plan(host))
    for n, line in enumerate(lines):
        if "\n" in line or "\r" in line:
            raise ConfigError(f"plan line {n} contains a line break; refusing to print it: {line!r}")
    return lines
