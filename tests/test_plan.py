"""prov plan prints; it never executes a command or writes a file."""

from __future__ import annotations

import os
import subprocess

import pytest

from prov.cli import main


def _forbidden(*args, **kwargs):
    raise AssertionError(f"prov plan tried to execute something: {args!r}")


def _tree(root):
    return sorted((str(p), p.stat().st_mtime_ns) for p in root.rglob("*"))


@pytest.mark.parametrize("firmware", ["uefi", "bios"])
def test_plan_never_executes_or_writes(tmp_path, capsys, monkeypatch, firmware):
    root = str(tmp_path)
    assert main(["--root", root, "init", "ubuntu", "demo"]) == 0
    toml = tmp_path / "hosts/demo/host.toml"
    toml.write_text(toml.read_text().replace('firmware = "uefi"', f'firmware = "{firmware}"'))
    assert main(["--root", root, "render", "demo"]) == 0
    capsys.readouterr()
    before = _tree(tmp_path)

    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        monkeypatch.setattr(subprocess, name, _forbidden)
    for name in ("system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnve", "posix_spawn", "fork"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _forbidden)

    assert main(["--root", root, "plan", "demo"]) == 0
    out = capsys.readouterr().out
    assert "cloud-localds seed.iso user-data meta-data" in out
    assert ("--boot uefi" in out) == (firmware == "uefi")
    assert _tree(tmp_path) == before
