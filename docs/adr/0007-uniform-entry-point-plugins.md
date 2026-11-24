# 0007. Kinds and backends are uniform entry-point plugins

## Status

accepted

Decided in: #17 (issue #16), the catalog stack. See CHANGELOG.md, Unreleased, "Added",
"Kind plugins".

## Context

After [0003](0003-kind-backend-split.md), backends were plugins registered under the
`prov.backends` entry-point group, but kinds were not: `src/prov/kinds/__init__.py` held a
`KINDS` table that imported `os_install` directly. Every new kind, built-in or not, was a
change to core.

The catalog work (#16) wants many kinds, some of them from other packages. Third-party
kinds needed a way in, and core had to stay neutral while they arrived.

## Decision

- Every kind is a plugin registered under the `prov.kinds` entry-point group, exactly like
  backends under `prov.backends`. The entry-point name equals the plugin's `NAME`.
  provision-hub registers its own kinds and backends this way in its `pyproject.toml`; core
  imports no kind or backend module.
- Built-in and third-party plugins take the same path: discovery, contract checks,
  `prov conformance` and `--json` treat them alike. `prov.kinds.builtin()` and
  `prov.registry.builtin()` read only the entry points of the `provision-hub`
  distribution.
- `PLUGIN_API = 1` (`prov.plugin.PLUGIN_API`) is the contract version. A kind must declare
  it; a backend may (default 1, as backends predate it). A plugin without it where
  required, or with another value, is refused at load time with exit code 2.
- Bare kind names (`os-install`) are reserved for kinds the `provision-hub` distribution
  registers; every other kind is dotted ([0008](0008-dotted-third-party-kind-names.md)).

## Alternatives considered

_Recorded retrospectively in #28; not in the original record._

- **Keep built-in kinds in core and add a separate mechanism for third-party kinds** —
  rejected: built-ins would not exercise the contract third parties get, and conformance
  and `--json` would need two code paths.

## Consequences

- A built-in kind proves the third-party contract every time the tests run;
  `tests/test_kinds.py` and `tests/test_kind_plugins.py` run core with fake kinds and
  backends and scan its imports.
- An incompatible plugin fails loudly at load time instead of misbehaving later. The
  number changes only when the contract changes incompatibly.
- A broken third-party kind never changes the published schemas, which are generated from
  the `provision-hub` distribution's entry points only.
- The bare-name reservation is a **naming rule, not a security boundary**: installed
  plugins are trusted code, any package can register an entry point and claim a
  distribution name, and Python packaging gives no isolation. A fork that wants to own bare
  names must keep the distribution name `provision-hub`.

The rules: [kinds.md](../kinds.md#kind-plugins) and
[adding-a-backend.md](../adding-a-backend.md), section 1.
