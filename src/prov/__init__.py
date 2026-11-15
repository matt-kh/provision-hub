"""prov: render, validate and plan OS install artefacts from one versioned host.toml.

prov never applies anything: no disk writes outside the host's ``rendered/`` directory, no
ssh, no VM creation, no network calls. Apply steps are printed by ``prov plan`` for a human.
"""

__version__ = "0.1.0.dev0"
