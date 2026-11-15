"""Helpers shared by distro plugins: deterministic YAML emission."""

from __future__ import annotations

from typing import Any

import yaml


class _Dumper(yaml.SafeDumper):
    """SafeDumper without anchors/aliases, so shared objects never become &id001."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


def dump_yaml(data: Any) -> str:
    """Serialise ``data`` as block-style YAML with sorted keys; stable across runs."""
    return yaml.dump(
        data,
        Dumper=_Dumper,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=True,
        width=4096,
        indent=2,
    )
