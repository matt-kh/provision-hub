"""``prov plan``: print the apply steps for a human. Never executes anything.

The plugin's ``plan(host)`` returns lines (shell commands and ``#`` comments) written
relative to the host's rendered directory; this module adds the common header.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, Dict, List


def plan_lines(plugin: Any, host: Dict[str, Any], rendered_dir: Path) -> List[str]:
    lines = [
        f"# prov plan for {host['hostname']} ({plugin.NAME}) - prov never runs these steps;",
        "# review them and run them yourself. Render first: prov render <host>.",
        f"cd {shlex.quote(str(rendered_dir))}",
    ]
    lines.extend(str(line) for line in plugin.plan(host))
    return lines
