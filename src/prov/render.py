"""Writing rendered artefacts to disk and diffing them against a fresh render.

``<root>/hosts/<host>/rendered/`` is owned by prov: ``render`` rewrites it so that it holds
exactly the files the distro plugin produced. Stale files, stale symlinks and directories
left empty are removed. Symlinks are never followed: not on the path from the root to
``rendered/`` (see ``safe_host_dir``) and not inside it.
"""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Tuple

from .schema import ConfigError


def safe_host_dir(root: Path, host: str) -> Path:
    """``<root>/hosts/<host>``, refusing symlinks on the way to ``rendered/``.

    The root itself may be a symlink (it is resolved first); ``hosts``, ``hosts/<host>`` and
    ``hosts/<host>/rendered`` may not, and every resolved path must stay inside
    ``<root>/hosts``.
    """
    root = Path(root).resolve()
    hosts = root / "hosts"
    hdir = hosts / host
    for path in (hosts, hdir, hdir / "rendered"):
        if path.is_symlink():
            raise ConfigError(f"{path} is a symlink; prov refuses to write through symlinks")
    for path in (hdir, hdir / "rendered"):
        if path.exists() and not path.resolve().is_relative_to(hosts.resolve()):
            raise ConfigError(f"{path} resolves outside {hosts}")
    return hdir


def check_paths(files: Dict[str, str]) -> None:
    """Reject absolute, escaping or control-character paths returned by a plugin."""
    for name in files:
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not p.parts or any(ord(c) < 32 or c == "\x7f" for c in name):
            raise ConfigError(f"plugin returned an unsafe output path: {name!r}")


def _walk(rendered_dir: Path) -> Tuple[List[Path], List[Path], List[Path]]:
    """(regular files, others, directories) under ``rendered_dir``, classified by lstat.

    "Others" are symlinks and special files (FIFOs, sockets, devices): never followed, never
    opened (opening a FIFO would block).
    """
    files, others, dirs = [], [], []
    for top, dirnames, filenames in os.walk(rendered_dir, followlinks=False):
        for name in dirnames + filenames:
            path = Path(top) / name
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                dirs.append(path)
            elif stat.S_ISREG(mode):
                files.append(path)
            else:
                others.append(path)
    return sorted(files), sorted(others), sorted(dirs)


def special_entries(rendered_dir: Path) -> List[str]:
    """Relative paths of symlinks and special files under ``rendered_dir``."""
    if rendered_dir.is_symlink() or not rendered_dir.is_dir():
        return []
    return [p.relative_to(rendered_dir).as_posix() for p in _walk(rendered_dir)[1]]


def _read(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return ""
        return fh.read().decode("utf-8", errors="replace")


def existing_files(rendered_dir: Path) -> Dict[str, str]:
    """Read every regular file under ``rendered_dir`` (relative POSIX path -> text)."""
    if rendered_dir.is_symlink() or not rendered_dir.is_dir():
        return {}
    files, _, _ = _walk(rendered_dir)
    return {p.relative_to(rendered_dir).as_posix(): _read(p) for p in files}


def atomic_write(target: Path, text: str) -> None:
    """Write via a fresh temp file in the same directory, then rename over ``target``.

    The temp file is created with O_EXCL|O_NOFOLLOW (``tempfile.mkstemp``), and the rename
    replaces a symlink or hard link at ``target`` instead of writing through it.
    """
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".prov-tmp")
    try:
        os.fchmod(fd, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, target)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise


def _write(rendered_dir: Path, name: str, text: str) -> None:
    target = rendered_dir / name
    base = rendered_dir.resolve()
    current = rendered_dir
    for part in PurePosixPath(name).parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ConfigError(f"{current} is a symlink; prov refuses to write through symlinks")
        current.mkdir(exist_ok=True)
    if not target.parent.resolve().is_relative_to(base):
        raise ConfigError(f"{target} resolves outside {rendered_dir}")
    atomic_write(target, text)


def write_rendered(rendered_dir: Path, files: Dict[str, str]) -> List[str]:
    """Make ``rendered_dir`` hold exactly ``files``; return a sorted change summary."""
    check_paths(files)
    if rendered_dir.is_symlink():
        raise ConfigError(f"{rendered_dir} is a symlink; prov refuses to write through symlinks")
    changes: List[str] = []
    if rendered_dir.is_dir():
        _, others, _ = _walk(rendered_dir)
        for other in others:  # never prov output; unlink does not touch a link's target
            kind = "symlink" if other.is_symlink() else "special file"
            other.unlink()
            changes.append(f"removed {kind} {other.relative_to(rendered_dir).as_posix()}")
    current = existing_files(rendered_dir)
    for name in sorted(set(current) - set(files)):
        (rendered_dir / name).unlink()
        changes.append(f"removed {name}")
    if rendered_dir.is_dir():
        _, _, dirs = _walk(rendered_dir)
        for path in sorted(dirs, key=lambda p: len(p.parts), reverse=True):
            if not any(path.iterdir()):
                path.rmdir()
    rendered_dir.mkdir(parents=True, exist_ok=True)
    for name in sorted(files):
        if current.get(name) == files[name]:
            continue
        _write(rendered_dir, name, files[name])
        changes.append(f"{'updated' if name in current else 'wrote'} {name}")
    return sorted(changes, key=lambda c: c.split(" ")[-1])


def diff_rendered(rendered_dir: Path, files: Dict[str, str]) -> str:
    """Unified diff from what is on disk to a fresh render; empty when identical."""
    check_paths(files)
    current = existing_files(rendered_dir)
    chunks = []
    if rendered_dir.is_dir() and not rendered_dir.is_symlink():
        for other in _walk(rendered_dir)[1]:
            kind = "symlink" if other.is_symlink() else "special file"
            chunks.append(f"{kind} {other.relative_to(rendered_dir).as_posix()} would be removed\n")
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
