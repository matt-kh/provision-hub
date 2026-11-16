"""render/init never write or delete through symlinks (review findings C1, C2, S8).

Covered at every level of an item: <kind>/, <kind>/<name>/, rendered/ and the reserved build/.
"""

from __future__ import annotations

import os

import pytest

from prov.cli import main
from prov.render import _write, diff_rendered, write_rendered
from prov.schema import ConfigError


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


@pytest.fixture
def outside(tmp_path):
    """A directory outside the prov root holding a file that must survive."""
    d = tmp_path / "outside"
    d.mkdir()
    (d / "precious.txt").write_text("keep me\n")
    return d


def _seed_host(capsys, root):
    assert run(capsys, "--root", str(root), "init", "os-install/demo", "--backend", "ubuntu-autoinstall")[0] == 0


LINKS = ["os-install", "os-install/demo", "os-install/demo/rendered", "os-install/demo/build"]


def _symlinked_tree(tmp_path, capsys, outside, link):
    """A root whose ``link`` is a symlink: into another real item tree, or (rendered/build) outside."""
    root = tmp_path / "root"
    real = tmp_path / "real-root"
    _seed_host(capsys, real)  # a real item tree to point the symlink into
    (outside / "rendered").mkdir(exist_ok=True)
    target = {
        "os-install": real / "os-install",
        "os-install/demo": real / "os-install/demo",
        "os-install/demo/rendered": outside,
        "os-install/demo/build": outside,
    }[link]
    parent = (root / link).parent
    parent.mkdir(parents=True, exist_ok=True)
    if link in ("os-install/demo/rendered", "os-install/demo/build"):
        (root / "os-install/demo/spec.toml").write_text((real / "os-install/demo/spec.toml").read_text())
    (root / link).symlink_to(target, target_is_directory=True)
    return root


@pytest.mark.parametrize("verb", ["render", "validate", "diff", "plan"])
@pytest.mark.parametrize("link", LINKS)
def test_every_verb_refuses_symlinked_item_directories(tmp_path, capsys, outside, link, verb):
    root = _symlinked_tree(tmp_path, capsys, outside, link)
    code, out, err = run(capsys, "--root", str(root), verb, "os-install/demo")
    assert code == 2 and "is a symlink; prov refuses to write through symlinks" in err, (out, err)
    assert (outside / "precious.txt").read_text() == "keep me\n"
    assert sorted(p.name for p in outside.iterdir()) == ["precious.txt", "rendered"]
    assert not any(outside.joinpath("rendered").iterdir())


@pytest.mark.parametrize("link", LINKS)
def test_init_refuses_symlinked_item_directories(tmp_path, capsys, outside, link):
    root = _symlinked_tree(tmp_path, capsys, outside, link)
    code, _, err = run(capsys, "--root", str(root), "init", "os-install/demo", "--backend", "ubuntu-autoinstall", "--force")
    assert code == 2 and "symlink" in err
    assert sorted(p.name for p in outside.iterdir()) == ["precious.txt", "rendered"]


def test_init_refuses_symlinked_kind_dir_to_outside(tmp_path, capsys, outside):
    root = tmp_path / "root"
    root.mkdir()
    (root / "os-install").symlink_to(outside, target_is_directory=True)
    code, _, err = run(capsys, "--root", str(root), "init", "os-install/demo", "--backend", "ubuntu-autoinstall")
    assert code == 2 and "symlink" in err
    assert not (outside / "demo").exists()


def test_safe_item_dir_refuses_a_kind_dir_resolving_outside_the_root(tmp_path, monkeypatch):
    from pathlib import Path

    from prov.render import safe_item_dir

    root = tmp_path / "root"
    (root / "os-install/demo").mkdir(parents=True)
    assert safe_item_dir(root, "os-install", "demo") == root.resolve() / "os-install/demo"
    elsewhere = tmp_path / "elsewhere"
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: elsewhere if self.name == "os-install" else real_resolve(self, *a, **k))
    with pytest.raises(ConfigError, match="resolves outside"):
        safe_item_dir(root, "os-install", "demo")


def test_init_refuses_symlinked_spec_toml(tmp_path, capsys, outside):
    hdir = tmp_path / "os-install/demo"
    hdir.mkdir(parents=True)
    (hdir / "spec.toml").symlink_to(outside / "precious.txt")
    code, _, err = run(capsys, "--root", str(tmp_path), "init", "os-install/demo", "--backend", "ubuntu-autoinstall", "--force")
    assert code == 2 and "symlink" in err
    assert (outside / "precious.txt").read_text() == "keep me\n"


def test_symlinked_root_itself_is_allowed(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    _seed_host(capsys, tmp_path / "alias")
    assert run(capsys, "--root", str(tmp_path / "alias"), "render", "os-install/demo")[0] == 0
    assert (real / "os-install/demo/rendered/user-data").is_file()


def test_file_symlinks_inside_rendered_are_removed_not_followed(tmp_path, capsys, outside):
    _seed_host(capsys, tmp_path)
    rendered = tmp_path / "os-install/demo/rendered"
    rendered.mkdir()
    (rendered / "user-data").symlink_to(outside / "precious.txt")
    (rendered / "stale-link").symlink_to(outside / "precious.txt")
    (rendered / "dir-link").symlink_to(outside, target_is_directory=True)

    code, out, _ = run(capsys, "--root", str(tmp_path), "diff", "os-install/demo")
    assert code == 1 and "symlink user-data would be removed" in out

    code, out, _ = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 0
    assert "removed symlink user-data" in out and "removed symlink stale-link" in out and "removed symlink dir-link" in out
    assert (outside / "precious.txt").read_text() == "keep me\n"
    assert sorted(p.name for p in outside.iterdir()) == ["precious.txt"]
    assert not (rendered / "user-data").is_symlink() and (rendered / "user-data").read_text().startswith("#cloud-config")
    assert sorted(p.name for p in rendered.iterdir()) == ["kernel-cmdline", "meta-data", "user-data"]


def test_write_replaces_a_symlink_instead_of_following_it(tmp_path, outside):
    (tmp_path / "user-data").symlink_to(outside / "precious.txt")
    _write(tmp_path, "user-data", "x\n")
    assert not (tmp_path / "user-data").is_symlink() and (tmp_path / "user-data").read_text() == "x\n"
    assert (outside / "precious.txt").read_text() == "keep me\n"


def test_render_breaks_hard_links_instead_of_writing_through(tmp_path, capsys, outside):
    _seed_host(capsys, tmp_path)
    rendered = tmp_path / "os-install/demo/rendered"
    rendered.mkdir()
    os.link(outside / "precious.txt", rendered / "user-data")
    assert run(capsys, "--root", str(tmp_path), "render", "os-install/demo")[0] == 0
    assert (outside / "precious.txt").read_text() == "keep me\n"
    assert (rendered / "user-data").read_text().startswith("#cloud-config")
    assert os.stat(rendered / "user-data").st_nlink == 1


def test_atomic_write_leaves_no_temp_files(tmp_path, capsys):
    old_umask = os.umask(0o022)
    try:
        _seed_host(capsys, tmp_path)
    finally:
        os.umask(old_umask)
    run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    names = [p.name for p in (tmp_path / "os-install/demo").rglob("*")]
    assert not [n for n in names if n.endswith(".prov-tmp")]
    assert oct((tmp_path / "os-install/demo/rendered/user-data").stat().st_mode & 0o777) == "0o644"


def test_fifo_in_rendered_never_blocks(tmp_path, capsys):
    _seed_host(capsys, tmp_path)
    rendered = tmp_path / "os-install/demo/rendered"
    run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    os.mkfifo(rendered / "pipe")
    os.unlink(rendered / "meta-data")
    os.mkfifo(rendered / "meta-data")
    code, out, _ = run(capsys, "--root", str(tmp_path), "diff", "os-install/demo")
    assert code == 1 and "special file pipe would be removed" in out
    code, _, err = run(capsys, "--root", str(tmp_path), "validate", "os-install/demo")
    assert code == 1 and "rendered/pipe: symlink, special file or unsafely named file" in err
    code, out, _ = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 0 and "removed special file pipe" in out and "removed special file meta-data" in out
    assert run(capsys, "--root", str(tmp_path), "validate", "os-install/demo")[0] == 0


def test_validate_never_reads_through_symlinks(tmp_path, capsys, outside):
    """Review W-B: a planted symlink must not make validate read and echo another file."""
    (outside / "secret.yaml").write_text("TOPSECRET: x\n: [\n")
    _seed_host(capsys, tmp_path)
    run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    rendered = tmp_path / "os-install/demo/rendered"
    for name in ("user-data", "kernel-cmdline"):
        (rendered / name).unlink()
        (rendered / name).symlink_to(outside / "secret.yaml")
    code, out, err = run(capsys, "--root", str(tmp_path), "validate", "os-install/demo")
    assert code == 1 and "TOPSECRET" not in out + err
    assert "rendered/user-data: symlink, special file or unsafely named file" in err


def test_plugin_validate_refuses_non_regular_files(tmp_path, outside):
    from prov.backends import ubuntu_autoinstall as ubuntu

    (outside / "secret.yaml").write_text("TOPSECRET: x\n: [\n")
    for name in ("user-data", "meta-data", "kernel-cmdline"):
        (tmp_path / name).symlink_to(outside / "secret.yaml")
    findings = [str(f) for f in ubuntu.validate(tmp_path)]
    assert "TOPSECRET" not in "".join(findings)
    for name in ("user-data", "meta-data", "kernel-cmdline"):
        assert f"error: {name}: not a regular file (symlink or special file); not read" in findings


def test_write_refuses_symlinked_subdirectory(tmp_path, outside):
    (tmp_path / "sub").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError, match="symlink"):
        _write(tmp_path, "sub/file", "x\n")
    assert sorted(p.name for p in outside.iterdir()) == ["precious.txt"]


def test_write_rendered_refuses_symlinked_rendered_dir(tmp_path, outside):
    (tmp_path / "rendered").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ConfigError, match="symlink"):
        write_rendered(tmp_path / "rendered", {"a": "b\n"})
    assert diff_rendered(tmp_path / "rendered", {}) == ""
    assert (outside / "precious.txt").read_text() == "keep me\n"


def test_stale_files_and_empty_dirs_are_pruned(tmp_path):
    rendered = tmp_path / "rendered"
    (rendered / "old/deeper").mkdir(parents=True)
    (rendered / "old/deeper/file").write_text("x\n")
    (rendered / "empty").mkdir()
    changes = write_rendered(rendered, {"keep/new": "y\n"})
    assert "removed old/deeper/file" in changes and "wrote keep/new" in changes
    assert sorted(os.listdir(rendered)) == ["keep"]


def test_preserved_files_are_kept_and_not_diffed(tmp_path, outside):
    rendered = tmp_path / "rendered"
    rendered.mkdir()
    (rendered / "flake.lock").write_text("{}\n")
    (rendered / "stale").write_text("x\n")
    changes = write_rendered(rendered, {"flake.nix": "{ }\n"}, preserve=("flake.lock",))
    assert "removed stale" in changes and not any("flake.lock" in c for c in changes)
    assert (rendered / "flake.lock").read_text() == "{}\n"
    assert diff_rendered(rendered, {"flake.nix": "{ }\n"}, preserve=("flake.lock",)) == ""
    assert "flake.lock" in diff_rendered(rendered, {"flake.nix": "{ }\n"})  # without PRESERVE it is stale
    # a symlink is never preserved, whatever its name
    (rendered / "flake.lock").unlink()
    (rendered / "flake.lock").symlink_to(outside / "precious.txt")
    assert "would be removed" in diff_rendered(rendered, {"flake.nix": "{ }\n"}, preserve=("flake.lock",))
    changes = write_rendered(rendered, {"flake.nix": "{ }\n"}, preserve=("flake.lock",))
    assert "removed symlink flake.lock" in changes
    assert not os.path.lexists(rendered / "flake.lock") and (outside / "precious.txt").read_text() == "keep me\n"


@pytest.fixture
def alarm():
    """Fail instead of hanging forever if a read blocks (e.g. on a FIFO)."""
    import signal

    def on_alarm(signum, frame):
        raise TimeoutError("blocked on a read")

    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.alarm(10)
    yield
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old)


@pytest.mark.parametrize("verb", ["validate", "render", "plan", "diff"])
def test_spec_toml_fifo_is_refused_without_blocking(tmp_path, capsys, alarm, verb):
    hdir = tmp_path / "os-install/f2"
    hdir.mkdir(parents=True)
    os.mkfifo(hdir / "spec.toml")
    code, _, err = run(capsys, "--root", str(tmp_path), verb, "os-install/f2")
    assert code == 2 and "not a regular file" in err


@pytest.mark.parametrize("target", ["/dev/zero", "inside"])
def test_spec_toml_symlinks_are_refused(tmp_path, capsys, alarm, target):
    _seed_host(capsys, tmp_path)
    hdir = tmp_path / "os-install/demo"
    real = hdir / "real.toml"
    (hdir / "spec.toml").rename(real)
    (hdir / "spec.toml").symlink_to("/dev/zero" if target == "/dev/zero" else real)
    code, _, err = run(capsys, "--root", str(tmp_path), "validate", "os-install/demo")
    assert code == 2 and "symlinks and special files are refused" in err


def test_spec_toml_size_is_capped(tmp_path, capsys):
    from prov.schema import MAX_SPEC_FILE_BYTES

    _seed_host(capsys, tmp_path)
    toml = tmp_path / "os-install/demo/spec.toml"
    toml.write_text(toml.read_text() + "#" * MAX_SPEC_FILE_BYTES + "\n")
    code, _, err = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 2 and "larger than 1048576 bytes" in err


@pytest.fixture
def umask():
    def set_umask(value):
        old = os.umask(value)
        restore.append(old)

    restore = []
    yield set_umask
    for old in restore:
        os.umask(old)


def test_new_files_honour_the_umask(tmp_path, capsys, umask):
    umask(0o027)
    _seed_host(capsys, tmp_path)
    run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert oct((tmp_path / "os-install/demo/spec.toml").stat().st_mode & 0o777) == "0o640"
    assert oct((tmp_path / "os-install/demo/rendered/user-data").stat().st_mode & 0o777) == "0o640"


def test_replacing_a_file_keeps_its_mode(tmp_path, capsys, umask):
    umask(0o022)
    _seed_host(capsys, tmp_path)
    run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    user_data = tmp_path / "os-install/demo/rendered/user-data"
    user_data.chmod(0o600)
    toml = tmp_path / "os-install/demo/spec.toml"
    toml.write_text(toml.read_text().replace('"Etc/UTC"', '"Europe/Berlin"'))
    code, out, _ = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 0 and "updated user-data" in out
    assert oct(user_data.stat().st_mode & 0o777) == "0o600"


HOSTILE_NAMES = ["evil\nok: forged\x1b[2K", "\x1b[31mred", "\x1b]0;title\x07", "a b", "c\x85d"]


def _has_raw_controls(text):
    """True if ``text`` holds anything but printable characters, newlines and tabs."""
    from prov.text import has_unsafe

    return has_unsafe(text.replace("\n", "").replace("\t", ""))


@pytest.mark.parametrize("name", HOSTILE_NAMES)
@pytest.mark.parametrize("nested", [False, True])
def test_unsafe_names_in_rendered_are_reported_never_printed_raw(tmp_path, capsys, name, nested):
    """Review W2: a committed file name cannot forge output lines or terminal escapes."""
    _seed_host(capsys, tmp_path)
    run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    rendered = tmp_path / "os-install/demo/rendered"
    if nested:
        (rendered / name).mkdir()
        bad = rendered / name / "file"
        rel = f"{name}/file"
    else:
        bad = rendered / name
        rel = name
    bad.write_text("TOPSECRET\n")
    from prov.text import esc

    shown = esc(rel)

    code, out, err = run(capsys, "--root", str(tmp_path), "diff", "os-install/demo")
    kind = "directory with an unsafe name" if nested else "file with an unsafe name"
    expected = f"file with an unsafe name {shown} would be removed\n"
    if nested:  # the directory sorts before its content
        expected = f"directory with an unsafe name {esc(name)} would be removed\n" + expected
    assert code == 1 and out == expected
    assert "TOPSECRET" not in out + err and not _has_raw_controls(out + err)

    code, out, err = run(capsys, "--root", str(tmp_path), "validate", "os-install/demo")
    assert code == 1 and f"error: rendered/{shown}: symlink, special file or unsafely named file" in err
    assert err.count("\n") == (3 if nested else 2) and "error: rendered: out of date" in err  # one line per finding
    assert "TOPSECRET" not in out + err and not _has_raw_controls(out + err)

    code, out, err = run(capsys, "--root", str(tmp_path), "render", "os-install/demo")
    assert code == 0 and f"removed file with an unsafe name {shown}\n" in out and kind
    assert not _has_raw_controls(out + err) and not any(line.startswith("ok: forged") for line in out.splitlines())
    assert sorted(p.name for p in rendered.iterdir()) == ["kernel-cmdline", "meta-data", "user-data"]
    assert run(capsys, "--root", str(tmp_path), "validate", "os-install/demo")[0] == 0


def test_diff_headers_and_change_lines_use_show(tmp_path):
    from prov.render import diff_rendered, write_rendered

    # names a backend may return are safe by check_paths; show() is the second line of defence
    rendered = tmp_path / "rendered"
    write_rendered(rendered, {"a b": "x\n"})
    assert diff_rendered(rendered, {"a b": "y\n"}).splitlines()[:2] == ["--- a/a b", "+++ b/a b"]


@pytest.mark.parametrize("name", ["a\nb", "a\x1bb", "a\x85b", "a b", "a b", "a\x7fb", "/abs", "../up", ""])
def test_check_paths_rejects_control_characters_and_escapes(name):
    from prov.render import check_paths

    with pytest.raises(ConfigError, match="unsafe output path") as exc:
        check_paths({name: "x\n"})
    assert not _has_raw_controls(str(exc.value))
