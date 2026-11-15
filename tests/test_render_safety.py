"""render/init never write or delete through symlinks (review findings C1, C2, S8)."""

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
    assert run(capsys, "--root", str(root), "init", "ubuntu", "demo")[0] == 0


@pytest.mark.parametrize("link", ["hosts", "hosts/demo", "hosts/demo/rendered"])
def test_render_refuses_symlinked_directories(tmp_path, capsys, outside, link):
    root = tmp_path / "root"
    root.mkdir()
    real = tmp_path / "real-root"
    _seed_host(capsys, real)  # a real host tree to point the symlink into
    target = {
        "hosts": real / "hosts",
        "hosts/demo": real / "hosts/demo",
        "hosts/demo/rendered": outside,
    }[link]
    if link != "hosts":
        (root / "hosts").mkdir()
    if link == "hosts/demo/rendered":
        (root / "hosts/demo").mkdir()
        (root / "hosts/demo/host.toml").write_text((real / "hosts/demo/host.toml").read_text())
    if link == "hosts/demo" or link == "hosts":
        (outside / "rendered").mkdir(exist_ok=True)
    (root / link).symlink_to(target, target_is_directory=True)

    code, out, err = run(capsys, "--root", str(root), "render", "demo")
    assert code == 2 and "symlink" in err, (out, err)
    assert (outside / "precious.txt").read_text() == "keep me\n"
    assert sorted(p.name for p in outside.iterdir()) in (["precious.txt"], ["precious.txt", "rendered"])


def test_init_refuses_symlinked_hosts_dir(tmp_path, capsys, outside):
    root = tmp_path / "root"
    root.mkdir()
    (root / "hosts").symlink_to(outside, target_is_directory=True)
    code, _, err = run(capsys, "--root", str(root), "init", "ubuntu", "demo")
    assert code == 2 and "symlink" in err
    assert not (outside / "demo").exists()


def test_init_refuses_symlinked_host_toml(tmp_path, capsys, outside):
    hdir = tmp_path / "hosts/demo"
    hdir.mkdir(parents=True)
    (hdir / "host.toml").symlink_to(outside / "precious.txt")
    code, _, err = run(capsys, "--root", str(tmp_path), "init", "ubuntu", "demo", "--force")
    assert code == 2 and "symlink" in err
    assert (outside / "precious.txt").read_text() == "keep me\n"


def test_symlinked_root_itself_is_allowed(tmp_path, capsys):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "alias").symlink_to(real, target_is_directory=True)
    _seed_host(capsys, tmp_path / "alias")
    assert run(capsys, "--root", str(tmp_path / "alias"), "render", "demo")[0] == 0
    assert (real / "hosts/demo/rendered/user-data").is_file()


def test_file_symlinks_inside_rendered_are_removed_not_followed(tmp_path, capsys, outside):
    _seed_host(capsys, tmp_path)
    rendered = tmp_path / "hosts/demo/rendered"
    rendered.mkdir()
    (rendered / "user-data").symlink_to(outside / "precious.txt")
    (rendered / "stale-link").symlink_to(outside / "precious.txt")
    (rendered / "dir-link").symlink_to(outside, target_is_directory=True)

    code, out, _ = run(capsys, "--root", str(tmp_path), "diff", "demo")
    assert code == 1 and "symlink user-data would be removed" in out

    code, out, _ = run(capsys, "--root", str(tmp_path), "render", "demo")
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
    rendered = tmp_path / "hosts/demo/rendered"
    rendered.mkdir()
    os.link(outside / "precious.txt", rendered / "user-data")
    assert run(capsys, "--root", str(tmp_path), "render", "demo")[0] == 0
    assert (outside / "precious.txt").read_text() == "keep me\n"
    assert (rendered / "user-data").read_text().startswith("#cloud-config")
    assert os.stat(rendered / "user-data").st_nlink == 1


def test_atomic_write_leaves_no_temp_files(tmp_path, capsys):
    _seed_host(capsys, tmp_path)
    run(capsys, "--root", str(tmp_path), "render", "demo")
    names = [p.name for p in (tmp_path / "hosts/demo").rglob("*")]
    assert not [n for n in names if n.endswith(".prov-tmp")]
    assert oct((tmp_path / "hosts/demo/rendered/user-data").stat().st_mode & 0o777) == "0o644"


def test_fifo_in_rendered_never_blocks(tmp_path, capsys):
    _seed_host(capsys, tmp_path)
    rendered = tmp_path / "hosts/demo/rendered"
    run(capsys, "--root", str(tmp_path), "render", "demo")
    os.mkfifo(rendered / "pipe")
    os.unlink(rendered / "meta-data")
    os.mkfifo(rendered / "meta-data")
    code, out, _ = run(capsys, "--root", str(tmp_path), "diff", "demo")
    assert code == 1 and "special file pipe would be removed" in out
    code, _, err = run(capsys, "--root", str(tmp_path), "validate", "demo")
    assert code == 1 and "rendered/pipe: symlink or special file" in err
    code, out, _ = run(capsys, "--root", str(tmp_path), "render", "demo")
    assert code == 0 and "removed special file pipe" in out and "removed special file meta-data" in out
    assert run(capsys, "--root", str(tmp_path), "validate", "demo")[0] == 0


def test_validate_never_reads_through_symlinks(tmp_path, capsys, outside):
    """Review W-B: a planted symlink must not make validate read and echo another file."""
    (outside / "secret.yaml").write_text("TOPSECRET: x\n: [\n")
    _seed_host(capsys, tmp_path)
    run(capsys, "--root", str(tmp_path), "render", "demo")
    rendered = tmp_path / "hosts/demo/rendered"
    for name in ("user-data", "kernel-cmdline"):
        (rendered / name).unlink()
        (rendered / name).symlink_to(outside / "secret.yaml")
    code, out, err = run(capsys, "--root", str(tmp_path), "validate", "demo")
    assert code == 1 and "TOPSECRET" not in out + err
    assert "rendered/user-data: symlink or special file" in err


def test_plugin_validate_refuses_non_regular_files(tmp_path, outside):
    from prov.distros import ubuntu

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
