"""prov: render, validate and plan provisioning artefacts from versioned specs.

Every item is ``<root>/<kind>/<name>/spec.toml`` with a ``kind`` (os-install, ...) and a
``backend`` (ubuntu-autoinstall, ...). prov never applies anything: no disk writes outside
the item's ``rendered/`` directory, no ssh, no VM creation, no network calls. Apply steps are
printed by ``prov plan`` for a human.
"""

__version__ = "0.1.0.dev0"
