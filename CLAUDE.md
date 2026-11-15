# CLAUDE.md — working on provision-hub (`prov`)

`prov` renders, validates and plans OS install artefacts from `hosts/<host>/host.toml`.
Public and organisation-neutral; a private hosts repo pins its releases, so the host schema
is a public API.

## Commands

```sh
uv sync                                  # dev env (Python 3.12, .python-version)
uv run pytest -q                         # all tests
uv run --python 3.10 pytest -q           # oldest supported Python
uv run prov --root <dir> init ubuntu demo && uv run prov --root <dir> render demo
uv run prov --root <dir> validate demo && uv run prov --root <dir> plan demo
pre-commit run --all-files               # same hooks as the CI lint job
```

Dependencies: `uv add <pkg>` / `uv add --group dev <pkg>`; commit `uv.lock`. Runtime deps
stay minimal (pyyaml; tomli on 3.10 only) — justify any new one in the PR.

## Rules

- **Never add apply behaviour.** No command may write disks, ssh, create VMs, call the
  network or run the commands it prints. `plan` only returns text; `render` writes only
  under `hosts/<host>/rendered/`. `validate` may run read-only local validators (e.g.
  `cloud-init schema`) and reports a missing tool as `skipped: <tool> not installed`.
  `tests/test_plan.py` enforces that plan executes nothing.
- **Synthetic data only.** Examples, fixtures and tests use documentation addresses
  (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32), example.com/org/net and
  fake ssh keys containing `EXAMPLE`. `tests/test_synthetic_data.py` enforces it. Never
  commit real hosts, keys or addresses.
- **Schema compatibility.** Within `schema_version = 1` only additive, optional changes
  (new optional key, new enum value). Removing/renaming a key, tightening a constraint or
  changing a meaning needs `schema_version = 2`, support for reading v1, and a migration
  note in the PR. After any schema change regenerate `schema/host.v1.schema.json`
  (below); a test fails if it drifts from the code.
- **Static validation only so far.** No layout has been installed in a VM; ZFS is the least
  proven. Say so when changing layouts, and never claim an artefact "works" without an install.
- **No writes through symlinks**; plan output shell-quotes every host value, comments
  included (`tests/test_render_safety.py`, `tests/test_plan.py`).
- **Deterministic output.** Sorted keys, stable ordering, no timestamps or random ids;
  goldens depend on it.
- New distros follow [docs/adding-a-distro.md](docs/adding-a-distro.md).
- Don't edit `.github/workflows/*` or `.harness.toml` unless the user asks.

## Goldens

The committed `examples/*/hosts/*/rendered/` directories are the golden files, and
`schema/host.v1.schema.json` is generated from the code. After an intended change:

```sh
PROV_UPDATE_GOLDENS=1 uv run pytest -q   # rewrites goldens and the schema file
git diff examples/ schema/                # review every changed line before committing
```
