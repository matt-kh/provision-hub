# Architecture

Read this page first. It says what `prov` is, how its parts fit together and where the
rules for each part live. The reference docs hold the rules; the
[ADRs](adr/README.md) hold the reasons. Where this page and a reference doc disagree, the
reference doc wins.

## What prov is and is not

`prov` is a Python CLI that turns one reviewed `<root>/<kind>/<name>/spec.toml` into
provisioning artefacts in `<root>/<kind>/<name>/rendered/`, checks them statically and
prints the apply steps for a human. It never applies anything: it does not write disks,
create VMs, open ssh sessions, call the network or run the commands it prints
([ADR 0001](adr/0001-render-only-never-apply.md)). The same spec always renders the same
bytes; agents author reviewed specs and code, never render-time config
([ADR 0002](adr/0002-deterministic-rendering-agents-as-authors.md)). It is a catalog of
tool-neutral definitions, one kind at a time; application deployment, cloud resources and
day-2 reconciliation are out of scope ([ADR 0015](adr/0015-scope-and-non-goals.md)).

## Overview

```text
 <root>/<kind>/<name>/spec.toml
 +--------------------------------------------------+
 | api_version = "prov/v1alpha1"   envelope (core)  |
 | kind = "os-install"                              |
 | backend = "ubuntu-autoinstall"                   |
 | hostname = "demo" ...           core keys (kind) |
 | [refs]   alias = "<kind>/<name>"  envelope       |
 | [state]  decommissioned = true    envelope       |
 | [ubuntu-autoinstall] ...        backend table    |
 +--------------------------------------------------+
            |                         |
            v                         v
   kind plugin (prov.kinds)   backend plugin (prov.backends)
   schema, semantic, REFS,    SCHEMA fragment, check,
   outputs, FIXTURES          render, validate, plan
            \                         /
             v                       v
        core pipeline: envelope -> schema -> semantic -> refs -> check
                                   |
                                   v
              <root>/<kind>/<name>/rendered/   (committed, reviewed)
                                   |
             prov plan prints steps | a human applies them (outside prov)
                                   v
                                 host
                                   | a host agent (never prov) writes
                                   v
              <state-dir>/<kind>/<name>.json   (prov.state/v1alpha1)
                                   |
                                   v
              prov status: reads spec.toml, rendered/ and records
```

## Layers

### Spec envelope

Every spec starts with `api_version`, `kind` and `backend`. `kind` must equal the
directory kind and `backend` must be a registered backend of that kind. The optional
envelope tables are `[refs]` and `[state]`. Envelope errors are exit 2; schema and semantic
errors are findings. `prov.schema.spec_schema` composes envelope, core keys, the shared
`$defs` and every backend's table into one JSON Schema.
Rules: [kinds.md](kinds.md#envelope-checks), [Versioning policy](kinds.md#versioning-policy).
Why: [ADR 0003](adr/0003-kind-backend-split.md).

### Kind plugins

A kind says what an item is. It declares its core keys (`PROPERTIES`, `REQUIRED`), a
`semantic` check, `VERSIONS`, `REFS`, and optionally `OUTPUTS` with `outputs` and
conformance `FIXTURES`. The built-in kind is `os-install`. Kinds of the provision-hub
distribution have bare names; third-party kinds are dotted (`registry.example.org`).
Rules: [Kind plugins](kinds.md#kind-plugins), [The kind contract](kinds.md#the-kind-contract).
Why: [ADR 0007](adr/0007-uniform-entry-point-plugins.md),
[ADR 0008](adr/0008-dotted-third-party-kind-names.md).

### Backend plugins

A backend says how an item is rendered. It provides `NAME`, `KIND`, `SCHEMA`,
`render(spec, ctx)`, `validate(rendered_dir)` and `plan(spec, ctx)`, and optionally
`check`, `starter`, `PRESERVE`, `FIXTURES` and `UNSUPPORTED_FIXTURES`. `artifacts`, `build`
and `BUILD_TOOLS` are reserved: checked at discovery, never called. `REFS` is reserved for
backends too and must be empty: references are declared by the kind and reach the backend
as `ctx.refs`. The built-ins are
`ubuntu-autoinstall` and `nixos-disko`. Every call goes through `prov.registry.call_plugin`,
which turns exceptions and wrongly typed results into an exit-2 error naming the backend.
Rules: [adding-a-backend.md](adding-a-backend.md), [backends/](backends/nixos-disko.md).
Why: [ADR 0003](adr/0003-kind-backend-split.md),
[ADR 0009](adr/0009-backend-names-flat-vendor-prefix.md),
[ADR 0010](adr/0010-single-distribution-package-split-deferred.md).

### Core pipeline

Core is kind- and backend-neutral and never imports a kind or backend module; both are
found through entry points. `prov.spec.check_raw` runs the checks in order: envelope, JSON
Schema (prov's own validator for a keyword subset, no `jsonschema` at runtime), defaults,
the kind's `semantic`, references, the backend's `check`, then the item-name check.
Runtime dependencies are pyyaml, plus tomli on Python 3.10.
Rules: [kinds.md](kinds.md), [determinism.md](determinism.md).
Why: [ADR 0004](adr/0004-minimal-runtime-dependencies.md),
[ADR 0007](adr/0007-uniform-entry-point-plugins.md).

### References and outputs

A kind's `outputs(spec)` is a pure function of the validated spec. A spec's `[refs]`
table maps an alias to a `<kind>/<name>` ref. `prov.refs` resolves it under the root into
`ctx.refs`: alias to `{"ref", "kind", "name", "outputs"}`, deeply read-only. It reads only
targets' `spec.toml` files, never through symlinks, and checks the whole closure (cycles,
missing targets, chains longer than `MAX_DEPTH`).
Rules: [Outputs](kinds.md#outputs), [References](kinds.md#references).
Why: [ADR 0012](adr/0012-outputs-pure-function-of-tool-neutral-spec.md),
[ADR 0013](adr/0013-refs-one-envelope-table-offline.md).

### Conformance and exported fixtures

`prov conformance <backend>` composes each kind fixture with the backend's table
(`compose_spec`), checks the spec, renders twice, writes to a temporary directory, runs the
backend's `validate`, checks `expect.contains` / `expect.absent` and passes the plan
through the plan refusal checks. It never runs a plan or a behavioural test.
`export_fixtures` builds `conformance/<kind>.<version>.fixtures.json` for other
implementations of a kind.
Rules: [Conformance fixtures](kinds.md#conformance-fixtures),
[Exported fixtures](kinds.md#exported-fixtures).
Why: [ADR 0014](adr/0014-static-conformance-exported-fixtures.md).

### Observed state

`prov digest` is sha256 over a `sha256sum`-style manifest of every regular file in
`rendered/`; `prov plan` prints it in its header. A host agent, never prov, writes a state
record `<state-dir>/<kind>/<name>.json` with `api_version = "prov.state/v1alpha1"`.
`prov status` derives each item's lifecycle (planned, active, stale, decommissioned,
invalid) and sync (in-sync, pending, drifted, unknown) from specs, `rendered/` and records.
The `[state]` table (`decommissioned = true`) is lifecycle intent; no render reads it.
Rules: [state.md](state.md).
Why: [ADR 0016](adr/0016-observed-state-records-separate-repository.md).

### JSON surface

`kinds`, `backends`, `schema`, `validate`, `plan`, `digest`, `status` and `conformance`
take `--json` and print exactly one document through `prov.text.json_text`. Every document
except `schema`'s carries `"format": "prov.cli/v1alpha1"`. Exit codes are unchanged; an
exit-2 error stays text on stderr. `list` has no `--json`.
Rules: [Machine-readable output](kinds.md#machine-readable-output).
Why: [ADR 0011](adr/0011-json-documents-with-format-key.md).

## Module map

| Module | Responsibility |
|---|---|
| `src/prov/__init__.py` | package version |
| `src/prov/cli.py` | argument parser (`build_parser`), one `cmd_*` per verb, exit codes 0/1/2, `--json` printing |
| `src/prov/plugin.py` | what kinds and backends share: entry-point loading, `PLUGIN_API`, guarded calls |
| `src/prov/registry.py` | backend discovery and contract checks, `Context`, `call_plugin`, `PRESERVE` |
| `src/prov/kinds/__init__.py` | kind discovery and contract (`discover`, `builtin`, `get`, `check_kind`, `call_kind`, `item_outputs`) |
| `src/prov/kinds/common.py` | shared JSON Schema `$defs` (`DEFS`) |
| `src/prov/kinds/os_install.py` | the `os-install` kind: core keys, `semantic`, fixtures |
| `src/prov/schema.py` | envelope constants, `spec_schema` composition, `read_toml`, `ConfigError` |
| `src/prov/spec.py` | ref parsing (`split_ref`, `parse_ref`), envelope check, `check_raw`, `check_item` / `load_item` |
| `src/prov/refs.py` | `[refs]` resolution into `Context.refs` |
| `src/prov/validate.py` | `Finding`, the built-in JSON Schema subset validator, defaults |
| `src/prov/render.py` | `safe_item_dir`, `write_rendered`, `diff_rendered`, `atomic_write` |
| `src/prov/plan.py` | `plan_lines`: header with digest, refusal of unsafe lines |
| `src/prov/conformance.py` | fixture runs, `compose_spec`, `export_fixtures`, `export` |
| `src/prov/state.py` | `render_digest`, `RECORD_SCHEMA` / `load_record`, `item_status` |
| `src/prov/text.py` | output escaping (`unsafe`, `esc`, `show`, `short_repr`) and `json_text` |
| `src/prov/util.py` | helpers for backends: deterministic YAML (`dump_yaml`), regular-file reads |
| `src/prov/backends/__init__.py` | package of the built-in backends |
| `src/prov/backends/ubuntu_autoinstall.py` | `ubuntu-autoinstall`: NoCloud seed (`user-data`, `meta-data`, `kernel-cmdline`) |
| `src/prov/backends/nixos_disko.py` | `nixos-disko`: pinned flake, disko layout and an offline USB installer |

## Data flow per verb

`prov --help` lists these verbs:

| Verb | Help text | `--json` |
|---|---|---|
| `kinds` | list the kinds prov knows and their api versions | yes |
| `backends` | list registered backends (NAME, KIND) | yes |
| `list` | list every item under the root (ref, backend) | no |
| `schema` | print the JSON Schema of a kind for its registered backends | yes (no change) |
| `init` | write a synthetic starter `<kind>/<name>/spec.toml` | no |
| `render` | render artefacts into `<kind>/<name>/rendered/` | no |
| `validate` | validate spec.toml and the rendered artefacts | yes |
| `diff` | diff a fresh render against `<kind>/<name>/rendered/` | no |
| `plan` | print the apply steps for a human (never runs them) | yes |
| `digest` | print the sha256 digest of `<kind>/<name>/rendered/` (docs/state.md) | yes |
| `status` | lifecycle and sync of items from spec.toml, rendered/ and state records (local files only) | yes |
| `conformance` | render the backend's kind fixtures and check them statically (never runs a plan or test) | yes |

**render.** `load_item` reads `spec.toml`, runs `check_raw` (envelope, schema, semantic,
refs, backend `check`) and exits 2 on an invalid item. The backend's `render(spec, ctx)`
returns a file map; `write_rendered` makes `rendered/` hold exactly those files, removing
stale ones and keeping `PRESERVE` files.

**validate.** `check_item` runs the same checks but reports findings instead of exiting.
For a valid spec it computes outputs, checks that `rendered/` exists and holds no symlink or
special file, calls the backend's `validate` and compares a fresh in-memory render with
`rendered/`. Exit 1 on any error finding.

**diff, plan, digest.** `diff` renders in memory and prints a unified diff (exit 1 when
it differs). `plan` loads the item and prints `plan_lines`: a header with the render
digest and a `cd` into `rendered/`, then the backend's lines. `digest` hashes `rendered/`
and exits 2 when it is missing.

**conformance.** `conformance.run` takes each fixture of the backend's kind in name order
through the checks listed in [Conformance fixtures](kinds.md#conformance-fixtures). A
plugin failure is a FAIL of that check, not exit 2.

**status.** Without refs, it scans the root like `list`. Per item it runs `check_item`,
hashes `rendered/`, loads the record from `--state-dir` (or `$PROV_STATE_DIR`) and derives
lifecycle and sync. Records without an item are warnings.

## Boundaries

- **Determinism.** Same spec and pinned inputs, same bytes: sorted keys, stable order, no
  timestamps or random ids. See [determinism.md](determinism.md).
- **Never apply, no network.** No verb writes disks, opens a connection or runs what it
  prints; `plan` only returns text (`tests/test_plan.py`). Core runs no subprocess. The one
  exception is a backend's `validate`, reached through `prov validate` and the `validate`
  check of `prov conformance`: the built-ins run a read-only local validator when it is
  installed (`cloud-init schema` for ubuntu-autoinstall, `nix-instantiate --parse` with
  `--store dummy://` for nixos-disko) and report a missing tool as a `skipped` finding.
  `render`, `diff`, `plan`, `digest`, `status`, `list`, `init`, `kinds`, `backends` and
  `schema` run no tool.
- **Printing boundary.** Text prov did not compose (file names, plugin text, exception
  text, tool output, spec values) goes through `prov.text`: `show`, `esc`, `short_repr`.
  `--json` documents go through `json_text` (ASCII only, values raw). Plan lines with an
  unsafe character are refused, never escaped. `tests/test_output_escaping.py` sweeps every
  verb against a hostile root.
- **Write locations.** `render` writes only `<kind>/<name>/rendered/`; `init` writes one
  `<kind>/<name>/spec.toml`; `conformance` writes only to a fresh temporary directory under
  `$TMPDIR` that it removes; `conformance --export KIND DIR` writes one file, atomically,
  into an existing real directory outside `<root>/<kind>/`. `build/` is reserved for a later
  `prov build`. `digest`, `status` and `validate` only read (`tests/test_state.py` guards
  `status`).
- **Symlinks.** prov never writes through a symlink: `safe_item_dir` refuses symlinked
  `<kind>/`, `<name>/`, `rendered/` and `build/`; `render` removes symlinks inside
  `rendered/` and never follows them; `refs`, state records and `--export` refuse them too
  (`tests/test_render_safety.py`).
- **Generated and golden artefacts.** Regenerate with `PROV_UPDATE_GOLDENS=1 uv run pytest
  -q` and review the diff.

  | Artefact | Generated from | Drift test |
  |---|---|---|
  | `examples/<kind>/<name>/rendered/` | `prov render` of each example | `tests/test_goldens.py` |
  | `schema/<kind>.<version>.schema.json` | `spec_schema` over built-in backends | `tests/test_schema.py` |
  | `schema/prov.state.v1alpha1.schema.json` | `prov.state.RECORD_SCHEMA` | `tests/test_state.py` |
  | `conformance/<kind>.<version>.fixtures.json` | `export_fixtures` | `tests/test_conformance_builtin.py` |

- **Neutral core.** `tests/test_kinds.py` and `tests/test_kind_plugins.py` prove core works
  with fake kinds and backends and imports none of the built-ins.

## Extension points

- **Adding a kind.** Write a module with the kind contract and register it in the
  `prov.kinds` entry-point group (entry-point name = `NAME`, `PLUGIN_API = 1`). Third-party
  kinds use dotted names. See [Kind plugins](kinds.md#kind-plugins).
- **Adding a backend.** Write a module with the backend contract, register it in the
  `prov.backends` entry-point group, add an example with goldens and cover every fixture of
  its kind. See [adding-a-backend.md](adding-a-backend.md).
- **Recording a decision.** Add an ADR as described in [adr/README.md](adr/README.md).
