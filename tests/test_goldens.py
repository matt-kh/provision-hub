"""Golden files: every example's committed rendered/ output equals a fresh render.

The examples cover every Ubuntu storage layout (lvm, zfs, mdraid). After an intended
renderer change, regenerate with:  PROV_UPDATE_GOLDENS=1 uv run pytest tests/test_goldens.py
"""

from __future__ import annotations

import pytest
from conftest import UPDATE_GOLDENS, example_hosts

from prov.registry import discover
from prov.render import diff_rendered, write_rendered
from prov.validate import ERROR, load_host

EXAMPLES = example_hosts()


def test_every_ubuntu_layout_has_an_example():
    distros = discover()
    layouts = set()
    for root, host in EXAMPLES:
        data, _ = load_host(root / "hosts" / host / "host.toml", distros)
        if data["distro"] == "ubuntu":
            layouts.add(data["ubuntu"]["storage"]["layout"])
    assert layouts == {"lvm", "zfs", "mdraid"}


@pytest.mark.parametrize("root,host", EXAMPLES, ids=[f"{r.name}/{h}" for r, h in EXAMPLES])
def test_golden(root, host):
    distros = discover()
    hdir = root / "hosts" / host
    data, _ = load_host(hdir / "host.toml", distros)
    plugin = distros[data["distro"]]
    files = plugin.render(data)
    if UPDATE_GOLDENS:
        write_rendered(hdir / "rendered", files)
    diff = diff_rendered(hdir / "rendered", files)
    assert not diff, f"golden out of date (PROV_UPDATE_GOLDENS=1 to accept):\n{diff}"
    assert plugin.render(data) == files, "render is not deterministic"
    errors = [f for f in plugin.validate(hdir / "rendered") if f.level == ERROR]
    assert not errors
