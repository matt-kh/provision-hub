"""Golden files: every example's committed rendered/ output equals a fresh render.

The examples cover every Ubuntu storage layout (lvm, zfs, mdraid). After an intended
renderer change, regenerate with:  PROV_UPDATE_GOLDENS=1 uv run pytest tests/test_goldens.py
"""

from __future__ import annotations

import pytest
from conftest import UPDATE_GOLDENS, example_items

from prov.registry import Context, discover, preserved
from prov.render import diff_rendered, write_rendered
from prov.schema import SPEC_FILE
from prov.spec import load_spec
from prov.validate import ERROR

EXAMPLES = example_items()


def test_every_ubuntu_layout_has_an_example():
    backends = discover()
    layouts = set()
    for root, kind, name in EXAMPLES:
        data, _ = load_spec(root / kind / name / SPEC_FILE, kind, name, backends)
        if data["backend"] == "ubuntu-autoinstall":
            layouts.add(data["ubuntu-autoinstall"]["storage"]["layout"])
    assert layouts == {"lvm", "zfs", "mdraid"}


@pytest.mark.parametrize("root,kind,name", EXAMPLES, ids=[f"{k}/{n}" for _, k, n in EXAMPLES])
def test_golden(root, kind, name):
    backends = discover()
    idir = root / kind / name
    data, _ = load_spec(idir / SPEC_FILE, kind, name, backends)
    plugin = backends[data["backend"]]
    ctx = Context(name)
    files = plugin.render(data, ctx)
    if UPDATE_GOLDENS:
        write_rendered(idir / "rendered", files, preserved(plugin))
    diff = diff_rendered(idir / "rendered", files, preserved(plugin))
    assert not diff, f"golden out of date (PROV_UPDATE_GOLDENS=1 to accept):\n{diff}"
    assert plugin.render(data, ctx) == files, "render is not deterministic"
    errors = [f for f in plugin.validate(idir / "rendered") if f.level == ERROR]
    assert not errors
