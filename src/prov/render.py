"""Writing rendered artefacts to disk and diffing them against a fresh render.

``<root>/<kind>/<name>/rendered/`` is owned by prov: ``render`` rewrites it so that it holds
exactly the files the backend produced. Stale files, stale symlinks and directories left
empty are removed, except the regular files a backend lists in its optional ``PRESERVE``
(human-created files such as a lock file); ``diff`` ignores those too. Symlinks are never
followed: not on the path from the root to ``rendered/`` or the reserved ``build/`` (see
``safe_item_dir``) and not inside it.
"""

from __future__ import annotations

import difflib
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Callable, Dict, Iterable, List, Optional, Tuple

from .schema import ConfigError
from .text import esc, has_unsafe, show


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
            raise ConfigError(f"{show(str(path))} is a symlink; prov refuses to write through symlinks")
    if kdir.exists() and not kdir.resolve().is_relative_to(root):
        raise ConfigError(f"{show(str(kdir))} resolves outside {show(str(root))}")
    for path in (idir,) + owned:
        if path.exists() and not path.resolve().is_relative_to(kdir.resolve()):
            raise ConfigError(f"{show(str(path))} resolves outside {show(str(kdir))}")
    return idir


def check_paths(files: Dict[str, str]) -> None:
    """Reject absolute, escaping or control-character paths returned by a plugin."""
    for name in files:
        p = PurePosixPath(name)
        if p.is_absolute() or ".." in p.parts or not p.parts or has_unsafe(name):
            raise ConfigError(f"plugin returned an unsafe output path: '{show(name)}'")


def _walk(rendered_dir: Path, onerror: Optional[Callable[[OSError], None]] = None) -> Tuple[List[Path], List[Path], List[Path]]:
    """(regular files, others, directories) under ``rendered_dir``, classified by lstat.

    "Others" are symlinks and special files (FIFOs, sockets, devices): never followed, never
    opened (opening a FIFO would block). Any entry whose relative path holds an unsafe
    character (``prov.text.unsafe``: controls, bidi/zero-width, lone surrogates from a
    non-UTF-8 name ...) is an "other" too, directories included: no backend can have written
    it (``check_paths``), so it is never read or diffed, only reported and removed.
    ``onerror`` is ``os.walk``'s: without it a directory that cannot be listed is skipped.
    """
    files, others, dirs = [], [], []
    for top, dirnames, filenames in os.walk(rendered_dir, onerror=onerror, followlinks=False):
        for name in dirnames + filenames:
            path = Path(top) / name
            mode = os.lstat(path).st_mode
            if has_unsafe(path.relative_to(rendered_dir).as_posix()):
                others.append(path)
            elif stat.S_ISDIR(mode):
                dirs.append(path)
            elif stat.S_ISREG(mode):
                files.append(path)
            else:
                others.append(path)
    return sorted(files), sorted(others), sorted(dirs)


def describe_other(path: Path) -> str:
    """What kind of "other" entry ``path`` is, for messages."""
    mode = os.lstat(path).st_mode
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISREG(mode):
        return "file with an unsafe name"
    if stat.S_ISDIR(mode):
        return "directory with an unsafe name"
    return "special file"


def special_entries(rendered_dir: Path) -> List[str]:
    """Relative paths of symlinks, special files and unsafely named files under ``rendered_dir``.

    The paths are raw; print them through ``prov.text.show``.
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


def tree_entries(rendered_dir: Path) -> Tuple[List[Path], List[Path]]:
    """(regular files, others) under ``rendered_dir``, sorted; "others" as in ``_walk`` (never read).

    Unlike ``_walk``, a directory that cannot be listed raises (OSError): a caller that hashes
    the tree must not silently hash part of it.
    """

    def fail(exc: OSError) -> None:
        raise exc

    files, others, _ = _walk(rendered_dir, onerror=fail)
    return files, others


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
            raise ConfigError(f"{show(str(current))} is a symlink; prov refuses to write through symlinks")
        current.mkdir(exist_ok=True)
    if not target.parent.resolve().is_relative_to(base):
        raise ConfigError(f"{show(str(target))} resolves outside {show(str(rendered_dir))}")
    atomic_write(target, text)


def _kept(preserve: Iterable[str], files: Dict[str, str]) -> frozenset:
    """The preserved names that do not collide with ``files``: a name that is a rendered file
    or a parent directory of one is not preserved (render's output wins)."""
    parents = {parent.as_posix() for name in files for parent in PurePosixPath(name).parents}
    taken = set(files) | parents
    return frozenset(preserve) - taken


def write_rendered(
    rendered_dir: Path, files: Dict[str, str], preserve: Iterable[str] = ()
) -> List[str]:
    """Make ``rendered_dir`` hold exactly ``files``; return a sorted change summary.

    Regular files named in ``preserve`` (relative POSIX paths) are kept when ``files`` does
    not hold them; symlinks and special files are removed whatever their name.
    """
    preserve = _kept(preserve, files)
    check_paths(files)
    if rendered_dir.is_symlink():
        raise ConfigError(f"{show(str(rendered_dir))} is a symlink; prov refuses to write through symlinks")
    changes: List[str] = []
    if rendered_dir.is_dir():
        _, others, _ = _walk(rendered_dir)
        # never prov output; deepest first so an unsafely named directory is empty when removed;
        # unlink does not touch a link's target
        for other in sorted(others, key=lambda p: len(p.parts), reverse=True):
            kind = describe_other(other)
            if kind == "directory with an unsafe name":
                other.rmdir()
            else:
                other.unlink()
            changes.append(f"removed {kind} {show(other.relative_to(rendered_dir).as_posix())}")
    current = existing_files(rendered_dir)
    for name in sorted(set(current) - set(files) - preserve):
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


def _lines(text: str) -> List[str]:
    """Lines split on "\\n" only, ends kept: ``str.splitlines`` would also break on \\r,
    \\x0b, \\x0c, \\x1c-\\x1e, \\x85 and U+2028/2029 and misstate the file's lines."""
    parts = text.split("\n")
    lines = [part + "\n" for part in parts[:-1]]
    if parts[-1]:
        lines.append(parts[-1])
    return lines


def diff_rendered(
    rendered_dir: Path, files: Dict[str, str], preserve: Iterable[str] = ()
) -> str:
    """Unified diff from what is on disk to a fresh render; empty when identical.

    Regular files named in ``preserve`` and absent from ``files`` are not compared.

    File names are shown through ``show``; in file contents every unsafe character except
    newline and tab is escaped too (``prov.text.esc``), so the output is for reading, not a
    patch to apply.
    """
    check_paths(files)
    current = existing_files(rendered_dir)
    for name in _kept(preserve, files):
        current.pop(name, None)
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
                _lines(old or ""),
                _lines(new or ""),
                fromfile=f"a/{show(name)}" if old is not None else "/dev/null",
                tofile=f"b/{show(name)}" if new is not None else "/dev/null",
            )
        )
    return "".join(esc(line if line.endswith("\n") else line + "\n", keep="\n\t") for line in chunks)
