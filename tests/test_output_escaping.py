"""Untrusted text never reaches stdout/stderr raw: names, plugin findings, errors, tool output."""

from __future__ import annotations

import os
import subprocess
import sys
import types

import pytest
from conftest import fake_entry_point

from prov.cli import main
from prov.registry import Context, call_plugin
from prov.schema import ConfigError
from prov.validate import ERROR, WARNING, Finding

HOSTILE = "ev\x1b[2Jil\nerror: forged‮​\x85\x07"


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def clean(text):
    """Every character printable, newline or tab; and the text encodes strictly as UTF-8."""
    text.encode("utf-8", "strict")
    return all(ch.isprintable() or ch in "\n\t" for ch in text)


# --- W1: non-UTF-8 names, under strict and surrogateescape output encodings ---------------


def _prov(root, *argv, encoding):
    env = dict(os.environ, PYTHONIOENCODING=encoding)
    proc = subprocess.run([sys.executable, "-m", "prov.cli", "--root", str(root), *argv], capture_output=True, env=env, timeout=60)
    return proc.returncode, proc.stdout, proc.stderr


@pytest.mark.parametrize("encoding", ["utf-8:surrogateescape", "utf-8:strict"])
def test_non_utf8_names_in_rendered(tmp_path, capsys, encoding):
    assert run(capsys, "--root", str(tmp_path), "init", "os-install/demo", "--backend", "ubuntu-autoinstall")[0] == 0
    assert run(capsys, "--root", str(tmp_path), "render", "os-install/demo")[0] == 0
    rendered = os.fsencode(tmp_path / "os-install/demo/rendered")
    for raw in (b"s\xff", b"t\x9b2J"):
        with open(os.path.join(rendered, raw), "wb") as fh:
            fh.write(b"TOPSECRET\n")

    code, out, err = _prov(tmp_path, "diff", "os-install/demo", encoding=encoding)
    text = out.decode("utf-8") + err.decode("utf-8")  # strict decode: nothing raw slipped out
    assert code == 1 and clean(text) and "Traceback" not in text and "TOPSECRET" not in text
    assert "file with an unsafe name s\\udcff would be removed" in text
    assert "file with an unsafe name t\\udc9b2J would be removed" in text

    code, out, err = _prov(tmp_path, "validate", "os-install/demo", encoding=encoding)
    text = out.decode("utf-8") + err.decode("utf-8")
    assert code == 1 and clean(text) and "Traceback" not in text and "error: rendered/s\\udcff: symlink" in text

    code, out, err = _prov(tmp_path, "render", "os-install/demo", encoding=encoding)
    text = out.decode("utf-8") + err.decode("utf-8")
    assert code == 0 and err == b"" and clean(text), text
    assert "removed file with an unsafe name s\\udcff" in text and "rendered os-install/demo (ubuntu-autoinstall): 2 change(s)" in text
    assert sorted(os.listdir(rendered)) == [b"kernel-cmdline", b"meta-data", b"user-data"]


# --- W2: plugin text is escaped at the printing boundary ----------------------------------


def test_finding_str_escapes_every_field():
    text = str(Finding(ERROR + "\x1b", HOSTILE, "wh\nere"))
    assert clean(text) and "\n" not in text and "\\x1b[2J" in text and "\\u202e" in text and "wh\\x0aere" in text


def test_call_plugin_escapes_exception_text_and_plugin_config_errors():
    def boom(*a):
        raise ValueError(HOSTILE)

    def config(*a):
        raise ConfigError(HOSTILE)

    for fn in (boom, config):
        plugin = types.SimpleNamespace(NAME="stub", render=fn)
        with pytest.raises(ConfigError) as exc:
            call_plugin(plugin, "render", {}, Context("x"))
        assert clean(str(exc.value)) and "\n" not in str(exc.value) and "backend 'stub' failed in render" in str(exc.value)


@pytest.mark.parametrize("bad", ["echo \x1b[2J", "echo ‮evil", "echo a\rb", "echo a\nb", "echo \x85", "echo  "])
def test_plan_refuses_lines_with_unsafe_characters(tmp_path, bad):
    from prov.plan import plan_lines

    plugin = types.SimpleNamespace(NAME="stub", plan=lambda spec, ctx: ["# ok", bad])
    with pytest.raises(ConfigError) as exc:
        plan_lines(plugin, {}, Context("demo"), "os-install/demo", tmp_path)
    message = str(exc.value)
    assert "backend 'stub' plan line 1 contains a line break or an unsafe character" in message and clean(message) and "\n" not in message


def test_plan_allows_tabs(tmp_path):
    from prov.plan import plan_lines

    plugin = types.SimpleNamespace(NAME="stub", plan=lambda spec, ctx: ["printf 'a\\tb'\t# tab"])
    assert plan_lines(plugin, {}, Context("demo"), "os-install/demo", tmp_path)[-1] == "printf 'a\\tb'\t# tab"


def test_external_tool_output_is_escaped(tmp_path, monkeypatch):
    from prov.backends import ubuntu_autoinstall as ubuntu

    monkeypatch.setattr(ubuntu.shutil, "which", lambda name: "/usr/bin/cloud-init")
    monkeypatch.setattr(ubuntu.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="bad\x1b[31m‮key"))
    findings = ubuntu._external(tmp_path / "user-data")
    assert findings[0].message == "cloud-init schema: bad\\x1b[31m\\u202ekey"


def test_main_escapes_error_messages(tmp_path, capsys, monkeypatch):
    import prov.cli as cli

    def boom(args):
        raise OSError(2, "No such\x1b[2J file", os.fsdecode(b"/tmp/x\xff\n"))

    monkeypatch.setattr(cli, "cmd_kinds", boom)
    monkeypatch.setattr(cli, "build_parser", _parser_calling(boom))
    code, out, err = run(capsys, "kinds")
    assert code == 2 and clean(err) and err.count("\n") == 1 and "/tmp/x\\udcff\\x0a" in err and "\\x1b[2J" in err


def _parser_calling(func):
    import prov.cli as cli

    real = cli.build_parser

    def build():
        parser = real()
        parser.set_defaults(func=func)
        for action in parser._subparsers._group_actions:
            for sub in action.choices.values():
                sub.set_defaults(func=func)
        return parser

    return build


# --- the sweep: every verb against a hostile root ----------------------------------------


def _stub(name, hostile):
    mod = types.ModuleType(name)
    mod.__doc__ = f"Stub backend {HOSTILE} for escaping tests."
    mod.NAME = name
    mod.KIND = "os-install"
    mod.SCHEMA = {"type": "object", "additionalProperties": False, "description": f"stub {HOSTILE}", "properties": {}}
    mod.starter = lambda item: (
        'api_version = "prov/v1alpha1"\nkind = "os-install"\n'
        f'backend = "{name}"\nhostname = "{item}"\n'
        '[[users]]\nname = "ops"\ngecos = "Op ‮erator​"\n'
        '[network]\n[[network.interfaces]]\nname = "eth0"\naddresses = ["192.0.2.10/24"]\n'
        f'[storage]\ndisks = ["/dev/vda"]\n[{name}]\n'
    )
    if hostile == "findings":
        mod.check = lambda spec, ctx: [Finding(WARNING, f"check says {HOSTILE}", f"where{HOSTILE}")]
        mod.render = lambda spec, ctx: {"out.txt": f"{spec['users'][0]['gecos']}\n"}
        mod.validate = lambda d: [Finding(ERROR, f"validate says {HOSTILE}", HOSTILE)]
        mod.plan = lambda spec, ctx: ["# fine", f"echo {HOSTILE}"]
    else:
        def raiser(*args):
            raise RuntimeError(HOSTILE)

        mod.render = raiser
        mod.validate = raiser
        mod.plan = raiser
    return mod


@pytest.fixture
def hostile_root(tmp_path, capsys, extra_backends):
    extra_backends(fake_entry_point(_stub("stub-findings", "findings")), fake_entry_point(_stub("stub-raise", "raise")))
    root = tmp_path / "root"
    assert run(capsys, "--root", str(root), "init", "os-install/web", "--backend", "ubuntu-autoinstall")[0] == 0
    assert run(capsys, "--root", str(root), "render", "os-install/web")[0] == 0
    rendered = os.fsencode(root / "os-install/web/rendered")
    for raw in (b"s\xff", "‮x".encode(), b"a\x1b[2J", b"line\nforged"):
        with open(os.path.join(rendered, raw), "wb") as fh:
            fh.write(b"x\n")
    assert run(capsys, "--root", str(root), "init", "os-install/finder", "--backend", "stub-findings")[0] == 0
    assert run(capsys, "--root", str(root), "init", "os-install/raiser", "--backend", "stub-raise")[0] == 0
    os.symlink(tmp_path, os.path.join(os.fsencode(root / "os-install"), b"lnk\xff\x1b"))
    os.mkdir(os.path.join(os.fsencode(root / "os-install"), b"bad\xffname"))
    with open(os.path.join(os.fsencode(root / "os-install"), b"bad\xffname", b"spec.toml"), "w") as fh:
        fh.write("x = 1\n")
    capsys.readouterr()
    return root


def test_every_verb_writes_only_printable_text(hostile_root, capsys):
    root = str(hostile_root)
    argvs = [
        ["kinds"],
        ["backends"],
        ["backends", "--kind", "os-install"],
        ["list"],
        ["schema", "os-install"],
        ["init", "os-install/fresh", "--backend", "stub-findings"],
        ["init", "os-install/fresh", "--backend", "stub-raise", "--force"],
    ]
    for item in ("web", "finder", "raiser", "fresh"):
        for verb in ("validate", "diff", "plan", "render"):
            argvs.append([verb, f"os-install/{item}"])
    seen = []
    for argv in argvs:
        code, out, err = run(capsys, "--root", root, *argv)
        assert code in (0, 1, 2), argv
        assert clean(out) and clean(err), (argv, out, err)
        assert "Traceback" not in err, argv
        assert not any(line.startswith(("forged", "error: forged")) for line in (out + err).splitlines()), argv
        seen.append((argv[0], code))
    # the hostile parts were exercised, not skipped
    joined = dict(((a, c) for a, c in seen))
    assert joined["list"] == 1 and ("plan", 2) in seen and ("render", 2) in seen and ("validate", 1) in seen
