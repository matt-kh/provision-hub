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


def test_write_uses_o_nofollow(tmp_path, outside):
    (tmp_path / "user-data").symlink_to(outside / "precious.txt")
    with pytest.raises(OSError):
        _write(tmp_path, "user-data", "x\n")
    assert (outside / "precious.txt").read_text() == "keep me\n"


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
