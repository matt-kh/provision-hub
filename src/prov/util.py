"""Helpers shared by distro plugins: deterministic YAML emission, safe file reads."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any, Optional

import yaml


class _Dumper(yaml.SafeDumper):
    """SafeDumper without anchors/aliases, so shared objects never become &id001."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_yaml(data: Any) -> str:
    """Serialise ``data`` as block-style YAML with sorted keys; stable across runs."""
    return yaml.dump(
        data,
        Dumper=_Dumper,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
        indent=2,
    )


class NotRegularFileError(OSError):
    """Raised for symlinks, FIFOs, devices, sockets and directories."""


class FileTooLargeError(OSError):
    """Raised when a file exceeds the caller's size cap."""


def read_regular_bytes(path: Path, max_bytes: Optional[int] = None) -> bytes:
    """Read a file that must be a regular file; never follows a symlink, never blocks.

    lstat first (a symlink, FIFO or device is refused before anything is opened), then open
    with O_NOFOLLOW|O_NONBLOCK and re-check the opened descriptor with fstat. With
    ``max_bytes`` a larger file raises FileTooLargeError instead of being read whole.
    """
    mode = os.lstat(path).st_mode
    if stat.S_ISDIR(mode):
        raise IsADirectoryError(21, "is a directory, expected a file", str(path))
    if not stat.S_ISREG(mode):
        raise NotRegularFileError(f"{path} is not a regular file (symlinks and special files are refused)")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise NotRegularFileError(f"{path} is not a regular file")
        data = fh.read() if max_bytes is None else fh.read(max_bytes + 1)
    if max_bytes is not None and len(data) > max_bytes:
        raise FileTooLargeError(f"{path} is larger than {max_bytes} bytes")
    return data


def read_regular_text(path: Path, max_bytes: Optional[int] = None) -> str:
    """UTF-8 text of a regular file (see ``read_regular_bytes``); may raise UnicodeDecodeError.

    Plugins use it in ``validate`` so a planted symlink cannot make prov read (and echo)
    another file.
    """
    return read_regular_bytes(path, max_bytes).decode("utf-8")
