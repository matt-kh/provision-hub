# 0005. Generic public hub, real hosts in private repositories

## Status

accepted

Decided in: #3 for the synthetic-data rule ("documentation IP ranges and reserved domains
only, enforced by a test").

## Context

The renderer is generic: the same kinds and backends serve any organisation. The items it
renders are not: real host names, disk serials, MAC and IP addresses, users and ssh keys
describe one organisation's machines and must not be published.

Mixing the two in one repository would either expose real hosts or keep a generic tool
private.

## Decision

- provision-hub is a **public, organisation-neutral** repository: the `prov` tool, its
  kinds and backends, examples and docs.
- Real hosts live in **private repositories** that hold the items
  (`<root>/<kind>/<name>/spec.toml` and the committed `rendered/`) and pin provision-hub
  releases. The spec format is therefore a public API.
- Everything committed to provision-hub uses **synthetic data only**: documentation
  addresses (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24, 2001:db8::/32), the reserved
  domains example.com, example.org and example.net, and fake ssh keys containing `EXAMPLE`.

## Alternatives considered

Not recorded at the time.

## Consequences

- `tests/test_synthetic_data.py` enforces the rule. It scans `examples/`, `tests/`,
  `docs/`, `schema/`, `conformance/`, README.md, CLAUDE.md and CHANGELOG.md, the `prov init`
  starters and the built-in conformance fixtures, and fails on any non-documentation
  address, any domain outside the reserved ones and a short list of public project
  references, and any ssh key without `EXAMPLE`.
- Hub issues, examples and docs describe hosts generically; real details stay in the
  private repositories.
- Because private repositories pin releases, format changes follow the versioning policy:
  `v1alpha*` may break with a Migration paragraph in CHANGELOG.md, and from 1.0 `prov/v1`
  is additive only.
- Host-owned files (a `hardware-configuration.nix`, a `local.nix`) and lock files live in
  the private repository next to the item, never in the hub.

The rules: CLAUDE.md ("Synthetic data only", "Schema policy") and
[kinds.md](../kinds.md#versioning-policy).
