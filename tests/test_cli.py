"""End to end: init -> render -> validate -> diff -> plan in a temporary root."""

from __future__ import annotations

import re

import pytest

from prov import __version__
from prov.cli import main


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_acceptance_flow(tmp_path, capsys):
    root = str(tmp_path)
    assert run(capsys, "--root", root, "init", "ubuntu", "demo")[0] == 0
    assert (tmp_path / "hosts/demo/host.toml").is_file()

    code, out, _ = run(capsys, "--root", root, "render", "demo")
    assert code == 0 and "wrote user-data" in out
    rendered = tmp_path / "hosts/demo/rendered"
    assert sorted(p.name for p in rendered.iterdir()) == ["kernel-cmdline", "meta-data", "user-data"]
    assert rendered.joinpath("user-data").read_text().startswith("#cloud-config\nautoinstall:\n")
    assert rendered.joinpath("kernel-cmdline").read_text() == "autoinstall ds=nocloud\n"

    code, out, err = run(capsys, "--root", root, "validate", "demo")
    assert code == 0, err
    assert "skipped: cloud-init not installed" in out and "ok: demo" in out

    code, out, _ = run(capsys, "--root", root, "diff", "demo")
    assert code == 0 and "up to date" in out

    code, out, _ = run(capsys, "--root", root, "plan", "demo")
    assert code == 0
    assert "cloud-localds seed.iso user-data meta-data" in out
    assert "xorriso -as genisoimage -output seed.iso -volid cidata -joliet -rock user-data meta-data" in out
    assert "--extra-args 'autoinstall ds=nocloud console=ttyS0'" in out
    assert f"cd {rendered}" in out

    # Rendering again is a no-op (deterministic output).
    code, out, _ = run(capsys, "--root", root, "render", "demo")
    assert code == 0 and "0 change(s)" in out


def test_prov_root_env(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PROV_ROOT", str(tmp_path))
    assert run(capsys, "init", "ubuntu", "envhost")[0] == 0
    assert (tmp_path / "hosts/envhost/host.toml").is_file()


def test_cwd_default(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("PROV_ROOT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert run(capsys, "init", "ubuntu", "cwdhost")[0] == 0
    assert (tmp_path / "hosts/cwdhost/host.toml").is_file()


def test_diff_and_validate_detect_drift(tmp_path, capsys):
    root = str(tmp_path)
    run(capsys, "--root", root, "init", "ubuntu", "demo")
    code, _, err = run(capsys, "--root", root, "validate", "demo")
    assert code == 1 and "not rendered yet" in err
    run(capsys, "--root", root, "render", "demo")
    host_toml = tmp_path / "hosts/demo/host.toml"
    host_toml.write_text(host_toml.read_text().replace('"Etc/UTC"', '"Asia/Kuala_Lumpur"'))
    code, out, _ = run(capsys, "--root", root, "diff", "demo")
    assert code == 1 and "+    timezone: Asia/Kuala_Lumpur" in out
    code, _, err = run(capsys, "--root", root, "validate", "demo")
    assert code == 1 and "out of date" in err
    (tmp_path / "hosts/demo/rendered/stale.txt").write_text("x\n")
    run(capsys, "--root", root, "render", "demo")
    assert not (tmp_path / "hosts/demo/rendered/stale.txt").exists()
    assert run(capsys, "--root", root, "validate", "demo")[0] == 0


def test_validate_reports_invalid_host_with_exit_1(tmp_path, capsys):
    path = tmp_path / "hosts/demo/host.toml"
    path.parent.mkdir(parents=True)
    path.write_text('schema_version = 7\nhostname = "demo"\n')
    code, _, err = run(capsys, "--root", str(tmp_path), "validate", "demo")
    assert code == 1 and "unsupported schema_version 7" in err


@pytest.mark.parametrize(
    "argv,needle",
    [
        (["render", "nohost"], "not found"),
        (["plan", "nohost"], "not found"),
        (["init", "plan9", "demo"], "unknown distro"),
        (["init", "ubuntu", "Bad_Host"], "invalid host name"),
    ],
)
def test_config_errors_exit_2(tmp_path, capsys, argv, needle):
    code, _, err = run(capsys, "--root", str(tmp_path), *argv)
    assert code == 2 and needle in err


def test_render_invalid_host_exits_2(tmp_path, capsys):
    path = tmp_path / "hosts/demo/host.toml"
    path.parent.mkdir(parents=True)
    path.write_text('hostname = "demo"\n')
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "demo")
    assert code == 2 and "schema_version is required" in err


def test_init_refuses_overwrite(tmp_path, capsys):
    root = str(tmp_path)
    assert run(capsys, "--root", root, "init", "ubuntu", "demo")[0] == 0
    assert run(capsys, "--root", root, "init", "ubuntu", "demo")[0] == 2
    assert run(capsys, "--root", root, "init", "ubuntu", "demo", "--force")[0] == 0


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


def test_distros_and_schema(capsys):
    code, out, _ = run(capsys, "distros")
    assert code == 0 and out.startswith("ubuntu\t")
    code, out, _ = run(capsys, "schema")
    assert code == 0 and '"schema_version"' in out


@pytest.mark.parametrize("host", ["demo\n", "demo\r", "Demo", "de/mo", "..", "a" * 64])
def test_hostile_host_names_exit_2(tmp_path, capsys, host):
    code, _, err = run(capsys, "--root", str(tmp_path), "init", "ubuntu", host)
    assert code == 2 and "invalid host name" in err
    assert not (tmp_path / "hosts").exists() or not any((tmp_path / "hosts").iterdir())


def test_unreadable_host_files_exit_2(tmp_path, capsys):
    hdir = tmp_path / "hosts/demo"
    hdir.mkdir(parents=True)
    (hdir / "host.toml").mkdir()
    for verb in ("render", "validate", "plan", "diff"):
        code, _, err = run(capsys, "--root", str(tmp_path), verb, "demo")
        assert code == 2 and "is a directory" in err, (verb, err)
    (hdir / "host.toml").rmdir()
    (hdir / "host.toml").write_bytes(b'schema_version = 1\nhostname = "\xff"\n')
    for verb in ("render", "validate"):
        code, _, err = run(capsys, "--root", str(tmp_path), verb, "demo")
        assert code == 2 and "UTF-8" in err, (verb, err)


def test_oserror_maps_to_exit_2(tmp_path, capsys, monkeypatch):
    import prov.cli as cli

    def boom(*a, **k):
        raise PermissionError(13, "Permission denied", str(tmp_path / "x"))

    run(capsys, "--root", str(tmp_path), "init", "ubuntu", "demo")
    monkeypatch.setattr(cli, "write_rendered", boom)
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "demo")
    assert code == 2 and "Permission denied" in err


def test_hand_edited_rendered_garbage_is_a_finding_not_a_crash(tmp_path, capsys):
    root = str(tmp_path)
    run(capsys, "--root", root, "init", "ubuntu", "demo")
    run(capsys, "--root", root, "render", "demo")
    (tmp_path / "hosts/demo/rendered/user-data").write_bytes(b"#cloud-config\n\xff\xfe\n")
    code, _, err = run(capsys, "--root", root, "validate", "demo")
    assert code == 1 and "not valid UTF-8" in err


def test_hostname_differs_from_directory_warns(tmp_path, capsys):
    root = str(tmp_path)
    run(capsys, "--root", root, "init", "ubuntu", "demo")
    toml = tmp_path / "hosts/demo/host.toml"
    toml.write_text(toml.read_text().replace('hostname = "demo"', 'hostname = "other"'))
    run(capsys, "--root", root, "render", "demo")
    code, out, _ = run(capsys, "--root", root, "validate", "demo")
    assert code == 0 and "warning: hostname: hostname 'other' differs from the directory name 'demo'" in out



@pytest.fixture
def broken_plugin(monkeypatch):
    """Make the ubuntu plugin's methods blow up the way a buggy third-party plugin might."""
    from prov.distros import ubuntu

    def boom(*args, **kwargs):
        raise KeyError("oops")

    def install(method):
        monkeypatch.setattr(ubuntu, method, boom)

    return install


@pytest.mark.parametrize("verb,method", [("render", "render"), ("diff", "render"), ("plan", "plan"), ("validate", "validate"), ("validate", "check")])
def test_plugin_exceptions_exit_2_naming_the_plugin(tmp_path, capsys, broken_plugin, verb, method):
    root = str(tmp_path)
    run(capsys, "--root", root, "init", "ubuntu", "demo")
    run(capsys, "--root", root, "render", "demo")
    broken_plugin(method)
    code, _, err = run(capsys, "--root", root, verb, "demo")
    assert code == 2 and f"distro plugin 'ubuntu' failed in {method}: KeyError" in err, err


def test_init_plugin_starter_exception_exits_2(tmp_path, capsys, broken_plugin):
    broken_plugin("starter")
    code, _, err = run(capsys, "--root", str(tmp_path), "init", "ubuntu", "demo")
    assert code == 2 and "failed in starter" in err


def test_unsafe_plugin_output_path_exits_2(tmp_path, capsys, monkeypatch):
    from prov.distros import ubuntu

    run(capsys, "--root", str(tmp_path), "init", "ubuntu", "demo")
    monkeypatch.setattr(ubuntu, "render", lambda host: {"../escape": "x\n"})
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "demo")
    assert code == 2 and "unsafe output path" in err
    assert not (tmp_path / "hosts/demo/escape").exists()


@pytest.mark.parametrize("exc,needle", [(ValueError("unsupported keyword(s) ['format']"), "unsupported keyword"), (re.error("bad escape"), "invalid regular expression")])
def test_value_and_regex_errors_exit_2(tmp_path, capsys, monkeypatch, exc, needle):
    import prov.cli as cli

    def boom(*args, **kwargs):
        raise exc

    run(capsys, "--root", str(tmp_path), "init", "ubuntu", "demo")
    monkeypatch.setattr(cli, "load_host", boom)
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "demo")
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
    from prov.distros import ubuntu

    root = str(tmp_path)
    run(capsys, "--root", root, "init", "ubuntu", "demo")
    run(capsys, "--root", root, "render", "demo")
    monkeypatch.setattr(ubuntu, method, lambda *a: value)
    verb = {"render": "render", "validate": "validate", "check": "validate", "plan": "plan"}[method]
    code, _, err = run(capsys, "--root", root, verb, "demo")
    assert code == 2 and f"distro plugin 'ubuntu' {needle}" in err, err
