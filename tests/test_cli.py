"""End to end: init -> render -> validate -> diff -> plan in a temporary root."""

from __future__ import annotations

import os
import re
import types

import pytest

from prov import __version__
from prov.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_acceptance_flow(tmp_path, capsys):
    root = str(tmp_path)
    assert run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")[0] == 0
    assert (tmp_path / "os-install/demo/spec.toml").is_file()

    code, out, _ = run(capsys, "--root", root, "render", "os-install/demo")
    assert code == 0 and "wrote user-data" in out
    rendered = tmp_path / "os-install/demo/rendered"
    assert sorted(p.name for p in rendered.iterdir()) == ["kernel-cmdline", "meta-data", "user-data"]
    assert rendered.joinpath("user-data").read_text().startswith("#cloud-config\nautoinstall:\n")
    assert rendered.joinpath("kernel-cmdline").read_text() == "autoinstall ds=nocloud\n"

    code, out, err = run(capsys, "--root", root, "validate", "os-install/demo")
    assert code == 0, err
    assert "skipped: cloud-init not installed" in out and "ok: os-install/demo" in out

    code, out, _ = run(capsys, "--root", root, "diff", "os-install/demo")
    assert code == 0 and "up to date" in out

    code, out, _ = run(capsys, "--root", root, "plan", "os-install/demo")
    assert code == 0
    assert "cloud-localds seed.iso user-data meta-data" in out
    assert "xorriso -as genisoimage -output seed.iso -volid cidata -joliet -rock user-data meta-data" in out
    assert "--extra-args 'autoinstall ds=nocloud console=ttyS0'" in out
    assert f"cd {rendered}" in out

    # Rendering again is a no-op (deterministic output).
    code, out, _ = run(capsys, "--root", root, "render", "os-install/demo")
    assert code == 0 and "0 change(s)" in out


def test_prov_root_env(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PROV_ROOT", str(tmp_path))
    assert run(capsys, "init", "os-install/envhost", "--backend", "ubuntu-autoinstall")[0] == 0
    assert (tmp_path / "os-install/envhost/spec.toml").is_file()


def test_cwd_default(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PROV_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert run(capsys, "init", "os-install/cwdhost", "--backend", "ubuntu-autoinstall")[0] == 0
    assert (tmp_path / "os-install/cwdhost/spec.toml").is_file()


def test_diff_and_validate_detect_drift(tmp_path, capsys):
    root = str(tmp_path)
    run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    code, _, err = run(capsys, "--root", root, "validate", "os-install/demo")
    assert code == 1 and "not rendered yet" in err
    run(capsys, "--root", root, "render", "os-install/demo")
    host_toml = tmp_path / "os-install/demo/spec.toml"
    host_toml.write_text(host_toml.read_text().replace('"Etc/UTC"', '"Asia/Kuala_Lumpur"'))
    code, out, _ = run(capsys, "--root", root, "diff", "os-install/demo")
    assert code == 1 and "+    timezone: Asia/Kuala_Lumpur" in out
    code, _, err = run(capsys, "--root", root, "validate", "os-install/demo")
    assert code == 1 and "out of date" in err
    (tmp_path / "os-install/demo/rendered/stale.txt").write_text("x\n")
    run(capsys, "--root", root, "render", "os-install/demo")
    assert not (tmp_path / "os-install/demo/rendered/stale.txt").exists()
    assert run(capsys, "--root", root, "validate", "os-install/demo")[0] == 0


def test_validate_reports_invalid_spec_with_exit_1(tmp_path, capsys):
    path = tmp_path / "os-install/demo/spec.toml"
    path.parent.mkdir(parents=True)
    path.write_text('api_version = "prov/v1alpha1"\nkind = "os-install"\nbackend = "ubuntu-autoinstall"\nhostname = "demo"\n')
    code, _, err = run(capsys, "--root", str(tmp_path), "validate", "os-install/demo")
    assert code == 1 and "missing required key 'network'" in err


def test_validate_legacy_host_file_exits_2(tmp_path, capsys):
    path = tmp_path / "os-install/demo/spec.toml"
    path.parent.mkdir(parents=True)
    path.write_text('schema_version = 1\nhostname = "demo"\ndistro = "ubuntu"\n')
    code, _, err = run(capsys, "--root", str(tmp_path), "validate", "os-install/demo")
    assert code == 2 and "predates prov/v1alpha1; see CHANGELOG migration" in err


@pytest.mark.parametrize(
    "argv,needle",
    [
        (["render", "os-install/nohost"], "not found"),
        (["plan", "os-install/nohost"], "not found"),
        (["init", "os-install/demo", "--backend", "plan9"], "unknown backend 'plan9' for kind 'os-install'"),
        (["init", "os-install/Bad_Host", "--backend", "ubuntu-autoinstall"], "invalid name"),
        (["init", "gizmo/demo", "--backend", "ubuntu-autoinstall"], "unknown kind 'gizmo' (known: os-install)"),
        (["render", "gizmo/demo"], "unknown kind"),
        (["render", "demo"], "invalid ref 'demo': expected <kind>/<name>"),
        (["render", "Os-Install/demo"], "invalid ref"),
        (["render", "os-install/demo/rendered"], "invalid name"),
        (["schema", "gizmo"], "unknown kind"),
        (["schema", "os-install", "--backend", "plan9"], "unknown backend"),
        (["backends", "--kind", "gizmo"], "unknown kind"),
    ],
)
def test_config_errors_exit_2(tmp_path, capsys, argv, needle):
    code, _, err = run(capsys, "--root", str(tmp_path), *argv)
    assert code == 2 and needle in err


def test_render_invalid_host_exits_2(tmp_path, capsys):
    path = tmp_path / "os-install/demo/spec.toml"
    path.parent.mkdir(parents=True)
    path.write_text('hostname = "demo"\n')
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 2 and "api_version is required" in err


def test_init_refuses_overwrite(tmp_path, capsys):
    root = str(tmp_path)
    assert run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")[0] == 0
    assert run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")[0] == 2
    assert run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall", "--force")[0] == 0


def test_usage_errors_exit_2(capsys):
    with pytest.raises(SystemExit) as exc:
        main([])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        main(["render"])
    assert exc.value.code == 2


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"prov {__version__}"


def test_kinds_backends_and_schema(capsys):
    code, out, _ = run(capsys, "kinds")
    assert code == 0 and out.startswith("os-install\tprov/v1alpha1\tOne machine's operating-system install")
    code, out, _ = run(capsys, "backends")
    assert code == 0 and out.startswith("ubuntu-autoinstall\tos-install\tUbuntu Server autoinstall")
    code, out2, _ = run(capsys, "backends", "--kind", "os-install")
    assert code == 0 and out2 == out
    code, out, _ = run(capsys, "schema", "os-install")
    assert code == 0 and '"api_version"' in out and '"ubuntu-autoinstall"' in out and '"schema_version"' not in out
    code, out2, _ = run(capsys, "schema", "os-install", "--backend", "ubuntu-autoinstall")
    assert code == 0 and out2 == out  # the only built-in backend
    with pytest.raises(SystemExit) as exc:  # removed in favour of kinds/backends
        main(["distros"])
    assert exc.value.code == 2


def test_list(tmp_path, capsys):
    root = str(tmp_path)
    assert run(capsys, "--root", root, "list") == (0, "", "")
    for name in ("web", "db"):
        run(capsys, "--root", root, "init", f"os-install/{name}", "--backend", "ubuntu-autoinstall")
    (tmp_path / "os-install/no-spec").mkdir()  # not an item: skipped
    (tmp_path / "os-install/README.md").write_text("notes\n")  # not a directory: skipped
    (tmp_path / "unknown-kind/x").mkdir(parents=True)  # not a kind: skipped
    code, out, err = run(capsys, "--root", root, "list")
    assert (code, out, err) == (0, "os-install/db\tubuntu-autoinstall\nos-install/web\tubuntu-autoinstall\n", "")
    (tmp_path / "os-install/bad").mkdir()
    (tmp_path / "os-install/bad/spec.toml").write_text("not = [toml")
    code, out, err = run(capsys, "--root", root, "list")
    assert code == 1 and "os-install/web\tubuntu-autoinstall" in out and "error: os-install/bad: invalid TOML" in err


def test_list_examples(capsys):
    from conftest import EXAMPLES

    code, out, _ = run(capsys, "--root", str(EXAMPLES), "list")
    assert code == 0 and out.splitlines() == [f"os-install/demo-{n}\tubuntu-autoinstall" for n in ("lvm", "mdraid", "zfs")]


def test_list_refuses_symlinks(tmp_path, capsys):
    real = tmp_path / "real"
    run(capsys, "--root", str(real), "init", "os-install/web", "--backend", "ubuntu-autoinstall")
    root = tmp_path / "root"
    (root / "os-install").mkdir(parents=True)
    (root / "os-install/web").symlink_to(real / "os-install/web", target_is_directory=True)
    code, out, err = run(capsys, "--root", str(root), "list")
    assert code == 1 and out == "" and "error: os-install/web: is a symlink; refused" in err
    (root / "os-install/web").unlink()
    (root / "os-install").rmdir()
    (root / "os-install").symlink_to(real / "os-install", target_is_directory=True)
    code, out, err = run(capsys, "--root", str(root), "list")
    assert code == 1 and out == "" and "error: os-install: is a symlink; refused" in err


def test_init_requires_backend(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--root", str(tmp_path), "init", "os-install/demo"])
    assert exc.value.code == 2
    assert not (tmp_path / "os-install").exists()


def test_init_writes_the_envelope(tmp_path, capsys):
    assert run(capsys, "--root", str(tmp_path), "init", "os-install/demo", "--backend", "ubuntu-autoinstall")[0] == 0
    text = (tmp_path / "os-install/demo/spec.toml").read_text()
    assert 'api_version = "prov/v1alpha1"\nkind = "os-install"\nbackend = "ubuntu-autoinstall"\n' in text
    assert "\n[ubuntu-autoinstall]\n" in text and "schema_version" not in text


@pytest.mark.parametrize("host", ["demo\n", "demo\r", "Demo", "de/mo", "..", "a" * 64])
def test_hostile_host_names_exit_2(tmp_path, capsys, host):
    code, _, err = run(capsys, "--root", str(tmp_path), "init", f"os-install/{host}", "--backend", "ubuntu-autoinstall")
    assert code == 2 and "invalid name" in err
    assert not any(tmp_path.iterdir())


def test_unreadable_host_files_exit_2(tmp_path, capsys):
    hdir = tmp_path / "os-install/demo"
    hdir.mkdir(parents=True)
    (hdir / "spec.toml").mkdir()
    for verb in ("render", "validate", "plan", "diff"):
        code, _, err = run(capsys, "--root", str(tmp_path), verb, "os-install/demo")
        assert code == 2 and "is a directory" in err, (verb, err)
    (hdir / "spec.toml").rmdir()
    (hdir / "spec.toml").write_bytes(b'schema_version = 1\nhostname = "\xff"\n')
    for verb in ("render", "validate"):
        code, _, err = run(capsys, "--root", str(tmp_path), verb, "os-install/demo")
        assert code == 2 and "UTF-8" in err, (verb, err)


def test_oserror_maps_to_exit_2(tmp_path, capsys, monkeypatch):
    import prov.cli as cli

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied", str(tmp_path / "x"))

    run(capsys, "--root", str(tmp_path), "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    monkeypatch.setattr(cli, "write_rendered", boom)
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 2 and "Permission denied" in err


def test_hand_edited_rendered_garbage_is_a_finding_not_a_crash(tmp_path, capsys):
    root = str(tmp_path)
    run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    run(capsys, "--root", root, "render", "os-install/demo")
    (tmp_path / "os-install/demo/rendered/user-data").write_bytes(b"#cloud-config\n\xff\xfe\n")
    code, _, err = run(capsys, "--root", root, "validate", "os-install/demo")
    assert code == 1 and "not valid UTF-8" in err


def test_hostname_differs_from_directory_warns(tmp_path, capsys):
    root = str(tmp_path)
    run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    toml = tmp_path / "os-install/demo/spec.toml"
    toml.write_text(toml.read_text().replace('hostname = "demo"', 'hostname = "other"'))
    run(capsys, "--root", root, "render", "os-install/demo")
    code, out, _ = run(capsys, "--root", root, "validate", "os-install/demo")
    assert code == 0 and "warning: hostname: hostname 'other' differs from the directory name 'demo'" in out



@pytest.fixture
def broken_plugin(monkeypatch):
    """Make the ubuntu-autoinstall backend's methods blow up the way a buggy third-party plugin might."""
    from prov.backends import ubuntu_autoinstall as ubuntu

    def boom(*args, **kwargs):
        raise KeyError("oops")

    def install(method):
        monkeypatch.setattr(ubuntu, method, boom)

    return install


@pytest.mark.parametrize("verb,method", [("render", "render"), ("diff", "render"), ("plan", "plan"), ("validate", "validate"), ("validate", "check")])
def test_plugin_exceptions_exit_2_naming_the_plugin(tmp_path, capsys, broken_plugin, verb, method):
    root = str(tmp_path)
    run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    run(capsys, "--root", root, "render", "os-install/demo")
    broken_plugin(method)
    code, _, err = run(capsys, "--root", root, verb, "os-install/demo")
    assert code == 2 and f"backend 'ubuntu-autoinstall' failed in {method}: KeyError" in err, err


def test_init_plugin_starter_exception_exits_2(tmp_path, capsys, broken_plugin):
    broken_plugin("starter")
    code, _, err = run(capsys, "--root", str(tmp_path), "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    assert code == 2 and "failed in starter" in err


def test_unsafe_plugin_output_path_exits_2(tmp_path, capsys, monkeypatch):
    from prov.backends import ubuntu_autoinstall as ubuntu

    run(capsys, "--root", str(tmp_path), "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    monkeypatch.setattr(ubuntu, "render", lambda spec, ctx: {"../escape": "x\n"})
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 2 and "unsafe output path" in err
    assert not (tmp_path / "os-install/demo/escape").exists()


@pytest.mark.parametrize("exc,needle", [(ValueError("unsupported keyword(s) ['format']"), "unsupported keyword"), (re.error("bad escape"), "invalid regular expression")])
def test_value_and_regex_errors_exit_2(tmp_path, capsys, monkeypatch, exc, needle):
    import prov.cli as cli

    def boom(*args, **kwargs):
        raise exc

    run(capsys, "--root", str(tmp_path), "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    monkeypatch.setattr(cli, "load_spec", boom)
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 2 and needle in err


@pytest.mark.parametrize(
    "method,value,needle",
    [
        ("render", ["user-data"], "failed in render: returned list"),
        ("render", {"user-data": 1}, "failed in render: returned dict, expected a dict of str paths"),
        ("validate", None, "failed in validate: returned NoneType"),
        ("check", ("not", "findings"), "failed in check: returned tuple"),
        ("plan", "echo hi", "failed in plan: returned str"),
        ("plan", [b"bytes"], "failed in plan: returned list, expected a list of str"),
    ],
)
def test_wrongly_typed_plugin_results_exit_2(tmp_path, capsys, monkeypatch, method, value, needle):
    from prov.backends import ubuntu_autoinstall as ubuntu

    root = str(tmp_path)
    run(capsys, "--root", root, "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    run(capsys, "--root", root, "render", "os-install/demo")
    monkeypatch.setattr(ubuntu, method, lambda *a: value)
    verb = {"render": "render", "validate": "validate", "check": "validate", "plan": "plan"}[method]
    code, _, err = run(capsys, "--root", root, verb, "os-install/demo")
    assert code == 2 and f"backend 'ubuntu-autoinstall' {needle}" in err, err


@pytest.mark.parametrize("name", ["x\nerror: forged line\x1b[2K", "\x1b]0;title\x07", "Bad Name\r"])
def test_list_never_prints_raw_hostile_names(tmp_path, capsys, name):
    """Review W1: a planted name cannot forge output lines or terminal escapes."""
    from prov.text import esc

    root = tmp_path / "root"
    run(capsys, "--root", str(root), "init", "os-install/web", "--backend", "ubuntu-autoinstall")
    (root / "os-install" / name).symlink_to(tmp_path, target_is_directory=True)
    code, out, err = run(capsys, "--root", str(root), "list")
    assert code == 1 and out == "os-install/web\tubuntu-autoinstall\n"
    assert "\x1b" not in out + err and "\r" not in out + err
    assert err.count("\n") == 1 and "forged line\n" not in err
    assert f"error: os-install/{esc(name)}: is a symlink; refused" in err
    (root / "os-install" / name).unlink()
    (root / "os-install" / name).mkdir()
    (root / "os-install" / name / "spec.toml").write_text("x = 1\n")
    code, out, err = run(capsys, "--root", str(root), "list")
    assert code == 1 and "\x1b" not in out + err and err.count("\n") == 1
    assert f"error: os-install/{esc(name)}: invalid item name '{esc(name)}'" in err


def test_list_invalid_name_without_spec_is_ignored(tmp_path, capsys):
    (tmp_path / "os-install/Not_An_Item").mkdir(parents=True)
    assert run(capsys, "--root", str(tmp_path), "list") == (0, "", "")


def test_list_missing_root_exits_2(tmp_path, capsys):
    code, out, err = run(capsys, "--root", str(tmp_path / "nope"), "list")
    assert code == 2 and out == "" and "does not exist or is not a directory" in err
    (tmp_path / "file").write_text("x\n")
    assert run(capsys, "--root", str(tmp_path / "file"), "list")[0] == 2


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_list_unreadable_kind_dir_is_one_finding(tmp_path, capsys, monkeypatch):
    from prov import kinds

    widget = types.ModuleType("widget")
    for key in ("VERSIONS", "PROPERTIES", "REQUIRED", "semantic", "REFS"):
        setattr(widget, key, getattr(kinds.KINDS["os-install"], key))
    widget.NAME = "widget"
    monkeypatch.setitem(kinds.KINDS, "widget", widget)
    run(capsys, "--root", str(tmp_path), "init", "os-install/web", "--backend", "ubuntu-autoinstall")
    (tmp_path / "widget").mkdir()
    (tmp_path / "widget").chmod(0)
    try:
        code, out, err = run(capsys, "--root", str(tmp_path), "list")
    finally:
        (tmp_path / "widget").chmod(0o755)
    assert code == 1 and out == "os-install/web\tubuntu-autoinstall\n"
    assert err == "error: widget: cannot read: Permission denied\n"


@pytest.mark.parametrize("make", ["file", "fifo"])
def test_list_kind_entry_that_is_not_a_directory_is_a_finding(tmp_path, capsys, make):
    if make == "file":
        (tmp_path / "os-install").write_text("x\n")
    else:
        os.mkfifo(tmp_path / "os-install")
    code, out, err = run(capsys, "--root", str(tmp_path), "list")
    assert (code, out) == (1, "") and err == "error: os-install: is not a directory; a kind directory holds <name>/ items\n"


def test_plan_line_break_refusal_caps_the_line(tmp_path):
    from prov.plan import plan_lines
    from prov.registry import Context
    from prov.schema import ConfigError

    plugin = types.SimpleNamespace(NAME="fake", plan=lambda spec, ctx: ["x" * 5000 + "\nrm -rf ~"])
    with pytest.raises(ConfigError) as exc:
        plan_lines(plugin, {}, Context("demo"), "os-install/demo", tmp_path)
    assert len(str(exc.value)) < 200 and "\n" not in str(exc.value)
