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


# --- argv echoed by argparse, diff line splitting, plugin edge cases ---------------------

HOSTILE_ARGS = ["\x1b]0;pwn\x07X", "a\nerror: forged", "\u202eevil", os.fsdecode(b"s\xff")]


@pytest.mark.parametrize("arg", HOSTILE_ARGS)
@pytest.mark.parametrize(
    "argv",
    [
        ["kinds", "{}"],
        ["backends", "{}"],
        ["list", "{}"],
        ["schema", "os-install", "{}"],
        ["render", "os-install/demo", "{}"],
        ["plan", "os-install/demo", "--{}"],
        ["{}"],
        ["init", "os-install/demo", "--backend", "ubuntu-autoinstall", "{}"],
    ],
)
def test_hostile_argv_is_escaped_by_the_parser(tmp_path, capsys, argv, arg):
    """Review W1: argparse's "unrecognized arguments" / "invalid choice" echo argv."""
    with pytest.raises(SystemExit) as exc:
        main(["--root", str(tmp_path), *[a.replace("{}", arg) for a in argv]])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert clean(err) and "prov: error:" in err
    assert not any(line.startswith("error: forged") for line in err.splitlines())


def test_hostile_ref_and_backend_arguments_are_escaped(tmp_path, capsys):
    for argv in (["render", HOSTILE_ARGS[0]], ["init", "os-install/demo", "--backend", HOSTILE_ARGS[1]], ["schema", HOSTILE_ARGS[2]], ["backends", "--kind", HOSTILE_ARGS[3]]):
        code, out, err = run(capsys, "--root", str(tmp_path), *argv)
        assert code == 2 and clean(out + err) and err.count("\n") == 1, (argv, err)


def test_main_escapes_unescaped_messages_at_the_boundary(tmp_path, capsys, monkeypatch):
    """Review S5: the last escaping layer in main() is load-bearing."""
    import prov.cli as cli

    def boom(args):
        raise ConfigError("raw \x1b]0;pwn\x07 \u202e text\nsecond line \x1b[2J")

    monkeypatch.setattr(cli, "build_parser", _parser_calling(boom))
    code, out, err = run(capsys, "kinds")
    assert code == 2 and out == ""
    assert err == "prov: error: raw \\x1b]0;pwn\\x07 \\u202e text\nsecond line \\x1b[2J\n"


def test_diff_splits_lines_on_newline_only(tmp_path):
    """Review S1: \\r, \\x0b, \\x0c, \\x1c-\\x1e, \\x85, U+2028/2029 are not line breaks of the file."""
    from prov.render import diff_rendered, write_rendered

    rendered = tmp_path / "rendered"
    write_rendered(rendered, {"f": "one\rtwo\x0bthree\x85four\u2028five\nsix\n"})
    diff = diff_rendered(rendered, {"f": "one\rtwo\x0bthree\x85four\u2028five\nSIX\n"})
    assert diff.splitlines() == [
        "--- a/f",
        "+++ b/f",
        "@@ -1,2 +1,2 @@",
        " one\\x0dtwo\\x0bthree\\x85four\\u2028five",
        "-six",
        "+SIX",
    ]
    assert clean(diff)
    no_eol = diff_rendered(rendered, {"f": "one\rtwo\x0bthree\x85four\u2028five\nsix"})
    assert no_eol.splitlines()[-2:] == ["-six", "+six"]


def test_render_with_lone_surrogates_fails_before_touching_rendered(tmp_path, capsys, monkeypatch):
    """Review S2: text that cannot be written as UTF-8 is a plugin failure naming the backend."""
    from prov.backends import ubuntu_autoinstall as ubuntu

    root = str(tmp_path)
    run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    run(capsys, "--root", root, "render", "os-install/demo")
    rendered = tmp_path / "os-install/demo/rendered"
    before = {p.name: p.read_bytes() for p in rendered.iterdir()}
    for files in ({"user-data": "x\udcff\n"}, {"s\udcff": "x\n"}):
        monkeypatch.setattr(ubuntu, "render", lambda spec, ctx, files=files: files)
        for verb in ("render", "diff", "validate"):
            code, out, err = run(capsys, "--root", root, verb, "os-install/demo")
            assert code == 2 and "backend 'ubuntu-autoinstall' failed in render: returned dict" in err and "UTF-8" in err, (verb, err)
            assert clean(out + err)
        assert {p.name: p.read_bytes() for p in rendered.iterdir()} == before


@pytest.mark.parametrize("method", ["render", "validate", "plan", "check", "starter"])
def test_plugin_sys_exit_is_a_plugin_failure(tmp_path, capsys, monkeypatch, method):
    """Review S3: a backend calling sys.exit cannot end prov with its own code or raw text."""
    from prov.backends import ubuntu_autoinstall as ubuntu

    root = str(tmp_path)
    run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    run(capsys, "--root", root, "render", "os-install/demo")

    def bye(*args):
        sys.exit("bye \x1b[2J\nerror: forged")

    monkeypatch.setattr(ubuntu, method, bye)
    verb = {"render": "render", "validate": "validate", "plan": "plan", "check": "validate", "starter": "init"}[method]
    argv = ["init", "os-install/other", "--backend", "ubuntu-autoinstall"] if verb == "init" else [verb, "os-install/demo"]
    code, out, err = run(capsys, "--root", root, *argv)
    assert code == 2 and f"backend 'ubuntu-autoinstall' failed in {method}: SystemExit: bye \\x1b[2J\\x0aerror: forged" in err
    assert clean(out + err)


def test_plugin_sys_exit_at_import_is_a_load_error(capsys, monkeypatch):
    import prov.registry as registry

    def load():
        sys.exit("import-time \x1b]0;x\x07 exit")

    ep = types.SimpleNamespace(name="talos", value="prov_talos", load=load)
    monkeypatch.setattr(registry, "entry_points", lambda group: [ep])
    code, out, err = run(capsys, "backends")
    assert code == 2 and "cannot load backend plugin talos = prov_talos: import-time \\x1b]0;x\\x07 exit" in err and clean(err)


def test_keyboard_interrupt_is_not_swallowed(monkeypatch):
    from prov.registry import call_plugin

    def stop(*args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        call_plugin(types.SimpleNamespace(NAME="stub", render=stop), "render", {}, Context("x"))
