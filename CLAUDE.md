# CLAUDE.md — working on provision-hub (`prov`)

`prov` renders, validates and plans provisioning artefacts from
`<root>/<kind>/<name>/spec.toml`. Every spec has a `kind` (plugin, e.g. `os-install`) and a
`backend` (plugin, e.g. `ubuntu-autoinstall`). Public and organisation-neutral; private
repos pin its releases, so the spec format is a public API. Generic across OS installs,
images, VMs and Kubernetes distributions; it champions no backend.

## Commands

```sh
uv sync                                  # dev env (Python 3.12, .python-version)
uv run pytest -q                         # all tests
uv run --python 3.10 pytest -q           # oldest supported Python
uv run prov kinds && uv run prov backends --kind os-install
uv run prov --root examples validate os-install/demo-lvm --json   # also kinds/backends/schema/plan
uv run prov --root <dir> init os-install/demo --backend ubuntu-autoinstall
uv run prov --root <dir> render os-install/demo && uv run prov --root <dir> validate os-install/demo
uv run prov --root <dir> plan os-install/demo && uv run prov --root <dir> list
uv run prov --root examples validate os-install/demo-lvm
uv run prov --root examples plan os-install/demo-nixos-ext4   # nixos-disko USB steps
uv run prov conformance ubuntu-autoinstall && uv run prov conformance nixos-disko   # os-install FIXTURES, static only
uv run prov --root examples digest os-install/demo-lvm && uv run prov --root examples status   # observed state (docs/state.md)
uv run prov --root examples plan os-install/demo-nixos-state   # [state.report]: rendered agent, deploy-key step
pre-commit run --all-files               # same hooks as the CI lint job
```

Dependencies: `uv add <pkg>` / `uv add --group dev <pkg>`; commit `uv.lock`. Runtime deps
stay minimal (pyyaml; tomli on 3.10 only) — justify any new one in the PR.

## Layout

- `src/prov/kinds/` — built-in kinds (`os_install.py`), discovery and the kind contract
  (`__init__.py`: `discover`, `builtin`, `get`, `check_kind`, `call_kind`, `item_outputs`) and shared `$defs`
  (`common.py`). Kinds are plugins registered through the `prov.kinds` entry-point group in
  `pyproject.toml` (entry-point name = `NAME`, `PLUGIN_API = 1`); bare names only for this
  distribution, third-party kinds are dotted (`registry.example.org`, api group
  `example.org`). See `docs/kinds.md` "Kind plugins".
- `src/prov/backends/` — built-in backends (`ubuntu_autoinstall.py`, `nixos_disko.py`),
  registered through the `prov.backends` entry-point group in `pyproject.toml` (entry-point
  name = `NAME`); `prov.registry.builtin()` and `prov.kinds.builtin()` are the entry points of
  the `provision-hub` distribution, never a list of imports. Per-backend docs:
  `docs/backends/<name>.md`. A backend's optional `PRESERVE` names human-created files
  `render` keeps in `rendered/` (nixos-disko: `flake.lock`).
- `src/prov/registry.py` — backend discovery, contract checks, `Context`, `call_plugin` (every
  call into a backend goes through it). `src/prov/plugin.py` — what kinds and backends share:
  entry-point loading, `PLUGIN_API`, the guarded call. `src/prov/spec.py` — refs and
  envelope checks, `check_item`/`load_item` (every verb that needs a valid item).
  `src/prov/refs.py` — resolves a spec's `[refs]` into `Context.refs`: reads only targets'
  `spec.toml` under the root, never through symlinks.
  `src/prov/conformance.py` — `prov conformance <backend>`: kind `FIXTURES` (+ backend
  `FIXTURES` / `UNSUPPORTED_FIXTURES`) composed, checked, rendered and validated statically;
  `compose_spec` is the one composition path (conformance runs and exports), and
  `export_fixtures` builds `conformance/*.fixtures.json` (and `prov conformance --export`).
  `src/prov/state.py` — observed state (docs/state.md): `render_digest` (`prov digest`, the
  plan header), the state record schema `RECORD_SCHEMA` and `load_record`, `item_status`
  for `prov status`, and `report_findings` (core checks of `[state.report]`; backends opt in
  with `STATE_AGENT = True`); reads local files only. nixos-disko renders the report-only
  agent from `src/prov/backends/nixos_disko_agent.sh` (rendered as `prov-state-agent.sh`,
  run only by `tests/test_state_agent.py` against a local bare repo and fake system paths).
- `src/prov/text.py` — the output-escaping predicate and helpers (`unsafe`, `esc`, `show`,
  `short_repr`) and `json_text`, the one JSON serialisation (schemas, every `--json`
  document; `ensure_ascii` is its printing boundary, values go in raw).
- Core modules (`cli`, `spec`, `registry`, `render`, `plan`, `schema`, `validate`, `text`,
  `util`, `plugin`, `refs`, `conformance`, `state`, `kinds/__init__`) must stay kind- and backend-neutral and never import a
  specific kind or backend module; `tests/test_kinds.py` and `tests/test_kind_plugins.py`
  prove it with fake kinds + backends (tests register them with the `extra_kinds` /
  `extra_backends` fixtures) and an import scan.

## Rules

- **Never add apply behaviour.** No command may write disks, ssh, create VMs, call the
  network or run the commands it prints. `plan` only returns text; `render` writes only
  under `<kind>/<name>/rendered/`; `build/` is reserved for a later `prov build`.
  `validate` may run read-only local validators (e.g. `cloud-init schema`) and reports a
  missing tool as `skipped: <tool> not installed`. `tests/test_plan.py` enforces that plan
  executes nothing. `conformance` is static + rendered only: it writes only to a fresh
  temporary directory from `$TMPDIR` (never the item tree) that it removes, and never runs
  a plan or a behavioural test (a backend may render e.g. `goss.yaml`; its plan prints the
  command for a human; `tests/test_conformance.py` guards it). `conformance --export KIND DIR`
  writes one file, atomically, into an existing real directory outside `<root>/<kind>/`,
  never through a symlink. `digest` and `status` only read: specs, `rendered/` and
  state records (written by host agents, never by prov); `tests/test_state.py` guards it.
  prov renders the state agent for `[state.report]` but never runs it.
- **Synthetic data only.** Examples, fixtures and tests use documentation addresses
  (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32), example.com/org/net and
  fake ssh keys containing `EXAMPLE`. `tests/test_synthetic_data.py` enforces it. Never
  commit real hosts, keys or addresses.
- **Schema policy** (`docs/kinds.md`). `api_version = "prov/v1alpha1"`. While a kind is
  `v1alpha*`, breaking changes are allowed but need a Migration paragraph in
  `CHANGELOG.md` (Unreleased). From 1.0, `prov/v1` changes are additive and optional only;
  anything breaking becomes `prov/v2` with a reader for v1. After any schema change
  regenerate `schema/<kind>.<version>.schema.json` (below); a test fails if it drifts.
- **Static validation only so far.** No layout has been installed in a VM; ZFS is the least
  proven; nixos-disko has been locked and `nix eval`-ed only (not built, not installed). Say
  so when changing layouts, and never claim an artefact "works" without an install.
- **No writes through symlinks** (`<kind>/`, `<name>/`, `rendered/`, `build/`); plan output
  shell-quotes every spec value, comments included (`tests/test_render_safety.py`,
  `tests/test_plan.py`).
- **Escape at the printing boundary.** Never print a file name, plugin text, exception text,
  tool output or spec value raw: route it through `prov.text` (`show` for names, `esc` for
  text, `short_repr` for values; `json_text` for a `--json` document). Plan lines with an unsafe character are refused, never
  escaped. `tests/test_output_escaping.py` sweeps every verb against a hostile root.
- **Deterministic output.** Sorted keys, stable ordering, no timestamps or random ids;
  goldens depend on it. The principle: [docs/determinism.md](docs/determinism.md).
- New backends follow [docs/adding-a-backend.md](docs/adding-a-backend.md).
- Don't edit `.github/workflows/*` or `.harness.toml` unless the user asks.

## Goldens

The committed `examples/<kind>/<name>/rendered/` directories are the golden files, and
`schema/<kind>.<version>.schema.json`, `schema/prov.state.v1alpha1.schema.json` (the state
record) and `conformance/<kind>.<version>.fixtures.json` (kind
`FIXTURES` + built-in backends' tables and reasons) are generated from the code. After an
intended change:

```sh
PROV_UPDATE_GOLDENS=1 uv run pytest -q   # rewrites goldens, schema and conformance files
git diff examples/ schema/ conformance/   # review every changed line before committing
```
