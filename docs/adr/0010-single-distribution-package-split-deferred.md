# 0010. One distribution; package split deferred

## Status

accepted

Decided in: the #16 design discussion; #17 kept one distribution.

## Context

With kinds and backends as plugins ([0007](0007-uniform-entry-point-plugins.md)), core,
the built-in kinds and the built-in backends could ship as separate packages. Other
implementations (a Terraform module, an Ansible role) may also want the spec without the
Python CLI.

At the time the contract was `prov/v1alpha1` and `PLUGIN_API = 1`, and core, kinds and
backends changed together.

## Decision

provision-hub stays one distribution, `provision-hub`, holding the CLI, the built-in kinds
and the built-in backends. A split is deferred until the contract stabilises (a beta
`api_version` or a stable `PLUGIN_API`), or until a third party needs kinds without the
CLI.

## Alternatives considered

_Recorded retrospectively in #28; not in the original record._

- **Split now into a core package, kind packages and backend packages** — deferred: while
  everything moves in lockstep, a split multiplies releases and the version matrix.
- **Publish the spec (schemas and fixtures) as its own artefact** — deferred: the exported
  `schema/*.schema.json` and `conformance/*.fixtures.json` already give non-Python
  implementations a language-neutral spec.

## Consequences

- One release carries every change to the contract; there is no compatibility matrix
  between core and the built-in plugins.
- A later split is mechanical: entry points mean users change no imports.
- The bare-name reservation is tied to the `provision-hub` distribution name
  (`prov.plugin.DISTRIBUTION`), so a split must carry that rule over.

The rules: [kinds.md](../kinds.md#kind-plugins) (bare names and the `provision-hub`
distribution) and [kinds.md](../kinds.md#exported-fixtures).
