"""Writing rendered artefacts to disk and diffing them against a fresh render.

``<root>/hosts/<host>/rendered/`` is owned by prov: ``render`` rewrites it so that it holds
exactly the files the distro plugin produced (stale files are removed).
"""

from __future__ import annotations

import difflib
from pathlib import Path, PurePosixPath
from typing import Dict, List


def check_paths(files: Dict[str, str]) -> None:
    """Reject absolute or escaping paths returned by a plugin."""
    for name in files:
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise ValueError(f"plugin returned an unsafe output path: {name!r}")


def existing_files(rendered_dir: Path) -> Dict[str, str]:
    """Read every regular file under ``rendered_dir`` (relative POSIX path -> text)."""
    if not rendered_dir.is_dir():
        return {}
    out = {}
    for path in sorted(rendered_dir.rglob("*")):
        if path.is_file() and not path.is_symlink():
            out[path.relative_to(rendered_dir).as_posix()] = path.read_text(encoding="utf-8")
    return out


def write_rendered(rendered_dir: Path, files: Dict[str, str]) -> List[str]:
    """Make ``rendered_dir`` hold exactly ``files``; return a sorted change summary."""
    check_paths(files)
    changes = []
    current = existing_files(rendered_dir)
    for name in sorted(set(current) - set(files)):
        (rendered_dir / name).unlink()
        changes.append(f"removed {name}")
    for name in sorted(files):
        if current.get(name) == files[name]:
            continue
        target = rendered_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(files[name])
        changes.append(f"{'updated' if name in current else 'wrote'} {name}")
    return changes


def diff_rendered(rendered_dir: Path, files: Dict[str, str]) -> str:
    """Unified diff from what is on disk to a fresh render; empty when identical."""
    check_paths(files)
    current = existing_files(rendered_dir)
    chunks = []
    for name in sorted(set(current) | set(files)):
        old, new = current.get(name), files.get(name)
        if old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                (new or "").splitlines(keepends=True),
                fromfile=f"a/{name}" if old is not None else "/dev/null",
                tofile=f"b/{name}" if new is not None else "/dev/null",
            )
        )
    return "".join(line if line.endswith("\n") else line + "\n" for line in chunks)
