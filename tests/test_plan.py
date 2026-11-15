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


HOSTILE = {
    "hostname": "demo;touch${IFS}/tmp/pwned",
    "disk": "/dev/x;rm${IFS}-rf${IFS}~;#",
    "user": "ops$(id)",
}


def _hostile_host():
    from prov.distros import ubuntu
    from prov.registry import discover
    from prov.schema import tomllib
    from prov.validate import check_host

    host, _ = check_host(tomllib.loads(ubuntu.starter("demo")), discover())
    host["hostname"] = HOSTILE["hostname"]
    host["storage"]["disks"] = [HOSTILE["disk"], "/dev/vdb"]
    host["users"][0]["name"] = HOSTILE["user"]
    return ubuntu, host


def test_plan_quotes_hostile_values(tmp_path):
    """Defence in depth: even values that bypassed validation cannot break out of a line."""
    import shlex

    from prov.plan import plan_lines

    plugin, host = _hostile_host()
    lines = plan_lines(plugin, host, tmp_path / "ren dered")
    for line in lines:
        assert "\n" not in line and "\r" not in line
        if line and not line.startswith("#"):
            tokens = shlex.split(line)  # parses: no unbalanced quoting
            # hostile fragments stay inside one quoted token; none becomes its own word
            assert not {"rm", "-rf", "touch", "id"} & set(tokens), line
    virt = next(line for line in lines if line.startswith("virt-install"))
    tokens = shlex.split(virt)
    assert tokens[tokens.index("--name") + 1] == HOSTILE["hostname"]
    assert shlex.split(next(line for line in lines if line.startswith("cd ")))[1] == str(tmp_path / "ren dered")
    for value in (HOSTILE["disk"], HOSTILE["user"], HOSTILE["hostname"]):
        assert shlex.quote(value) in "\n".join(lines)


@pytest.mark.parametrize("field", ["hostname", "disk", "user"])
def test_plan_refuses_values_with_line_breaks(tmp_path, field):
    from prov.plan import plan_lines
    from prov.schema import ConfigError

    plugin, host = _hostile_host()
    if field == "hostname":
        host["hostname"] = "demo\nrm -rf ~"
    elif field == "disk":
        host["storage"]["disks"] = ["/dev/vda\nrm -rf ~"]
    else:
        host["users"][0]["name"] = "ops\rrm -rf ~"
    with pytest.raises(ConfigError, match="line break"):
        plan_lines(plugin, host, tmp_path)
