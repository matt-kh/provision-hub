"""Helpers shared by distro plugins: deterministic YAML emission, safe file reads."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

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
    """Raised by ``read_regular_text`` for symlinks, FIFOs, devices and directories."""


def read_regular_text(path: Path) -> str:
    """Read a UTF-8 text file that must be a regular file; never follows a symlink.

    Raises FileNotFoundError, NotRegularFileError or UnicodeDecodeError. Plugins use it in
    ``validate`` so a planted symlink cannot make prov read (and echo) another file.
    """
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise NotRegularFileError(f"{path} is not a regular file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise NotRegularFileError(f"{path} is not a regular file")
        return fh.read().decode("utf-8")
