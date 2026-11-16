"""Writing rendered artefacts to disk and diffing them against a fresh render.

``<root>/<kind>/<name>/rendered/`` is owned by prov: ``render`` rewrites it so that it holds
exactly the files the backend produced. Stale files, stale symlinks and directories left
empty are removed. Symlinks are never followed: not on the path from the root to
``rendered/`` or the reserved ``build/`` (see ``safe_item_dir``) and not inside it.
"""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Dict, List, Tuple

from .schema import CONTROL_CHARS, ConfigError, short_repr, show


def safe_item_dir(root: Path, kind: str, name: str) -> Path:
    """``<root>/<kind>/<name>``, refusing symlinks on the way to ``rendered/`` and ``build/``.

    The root itself may be a symlink (it is resolved first); ``<kind>``, ``<kind>/<name>``,
    and the item's ``rendered`` and ``build`` directories may not, and every resolved path
    must stay inside ``<root>/<kind>``. Callers validate ``kind`` and ``name`` first
    (``prov.spec.parse_ref``).
    """
    root = Path(root).resolve()
    kdir = root / kind
    idir = kdir / name
    owned = (idir / "rendered", idir / "build")
    for path in (kdir, idir) + owned:
        if path.is_symlink():
            raise ConfigError(f"{path} is a symlink; prov refuses to write through symlinks")
    if kdir.exists() and not kdir.resolve().is_relative_to(root):
        raise ConfigError(f"{kdir} resolves outside {root}")
    for path in (idir,) + owned:
        if path.exists() and not path.resolve().is_relative_to(kdir.resolve()):
            raise ConfigError(f"{path} resolves outside {kdir}")
    return idir


def check_paths(files: Dict[str, str]) -> None:
    """Reject absolute, escaping or control-character paths returned by a plugin."""
    for name in files:
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not p.parts or CONTROL_CHARS.search(name):
            raise ConfigError(f"plugin returned an unsafe output path: {short_repr(name)}")


def _walk(rendered_dir: Path) -> Tuple[List[Path], List[Path], List[Path]]:
    """(regular files, others, directories) under ``rendered_dir``, classified by lstat.

    "Others" are symlinks and special files (FIFOs, sockets, devices): never followed, never
    opened (opening a FIFO would block). Regular files whose relative path holds a control
    character are "others" too: no backend can have written them (``check_paths``), so they
    are never read or diffed, only reported and removed.
    """
    files, others, dirs = [], [], []
    for top, dirnames, filenames in os.walk(rendered_dir, followlinks=False):
        for name in dirnames + filenames:
            path = Path(top) / name
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                dirs.append(path)
            elif stat.S_ISREG(mode) and not CONTROL_CHARS.search(path.relative_to(rendered_dir).as_posix()):
                files.append(path)
            else:
                others.append(path)
    return sorted(files), sorted(others), sorted(dirs)


def describe_other(path: Path) -> str:
    """What kind of "other" entry ``path`` is, for messages."""
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    return "file with an unsafe name" if stat.S_ISREG(mode) else "special file"


def special_entries(rendered_dir: Path) -> List[str]:
    """Relative paths of symlinks, special files and unsafely named files under ``rendered_dir``.

    The paths are raw; print them through ``prov.schema.show``.
    """
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
    replaces a symlink or hard link at ``target`` instead of writing through it. The mode is
    the replaced regular file's mode, else ``0o666`` minus the umask (like a plain open).
    """
    try:
        st = os.lstat(target)
        mode = stat.S_IMODE(st.st_mode) if stat.S_ISREG(st.st_mode) else None
    except FileNotFoundError:
        mode = None
    if mode is None:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.", suffix=".prov-tmp")
    try:
        os.fchmod(fd, mode)
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
            kind = describe_other(other)
            other.unlink()
            changes.append(f"removed {kind} {show(other.relative_to(rendered_dir).as_posix())}")
    current = existing_files(rendered_dir)
    for name in sorted(set(current) - set(files)):
        (rendered_dir / name).unlink()
        changes.append(f"removed {show(name)}")
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
        changes.append(f"{'updated' if name in current else 'wrote'} {show(name)}")
    return sorted(changes, key=lambda c: c.split(" ")[-1])


def diff_rendered(rendered_dir: Path, files: Dict[str, str]) -> str:
    """Unified diff from what is on disk to a fresh render; empty when identical.

    File names are shown through ``show``; file contents appear raw, behind the diff's
    ``+``/``-``/`` `` prefix, as in ``git diff``.
    """
    check_paths(files)
    current = existing_files(rendered_dir)
    chunks = []
    if rendered_dir.is_dir() and not rendered_dir.is_symlink():
        for other in _walk(rendered_dir)[1]:
            kind = describe_other(other)
            chunks.append(f"{kind} {show(other.relative_to(rendered_dir).as_posix())} would be removed\n")
    for name in sorted(set(current) | set(files)):
        old, new = current.get(name), files.get(name)
        if old == new:
            continue
        chunks.extend(
            difflib.unified_diff(
                (old or "").splitlines(keepends=True),
                (new or "").splitlines(keepends=True),
                fromfile=f"a/{show(name)}" if old is not None else "/dev/null",
                tofile=f"b/{show(name)}" if new is not None else "/dev/null",
            )
        )
    return "".join(line if line.endswith("\n") else line + "\n" for line in chunks)
