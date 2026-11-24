# Architecture decision records

An architecture decision record (ADR) explains one decision that shapes provision-hub: the
problem, what was chosen, what else was on the table and what follows from it. The format
is a short form of [MADR](https://github.com/adr/madr).

ADRs record **why**. The normative rules stay in the reference docs, and when an ADR and a
reference doc disagree, the reference doc wins:

- [kinds.md](../kinds.md): kinds, the spec envelope, outputs, references, conformance and
  the versioning policy;
- [adding-a-backend.md](../adding-a-backend.md): the backend contract;
- [determinism.md](../determinism.md): what "same spec, same bytes" means and who authors
  what;
- [state.md](../state.md): observed state, `prov digest` and `prov status`;
- [backends/](../backends/): one page per built-in backend, e.g.
  [nixos-disko.md](../backends/nixos-disko.md).

## Format

File name: `NNNN-kebab-title.md`, numbered in order and never reused (`0001-...`).

Each record has these sections:

- **Status**: one of `proposed`, `accepted`, `deprecated` or `superseded by NNNN`
  (with a link to the record that replaces it).
- **Context**: the problem and the forces at the time.
- **Decision**: what was chosen, in a few sentences.
- **Alternatives considered**: each option and why it lost. Where the history does not
  record an alternative, the record says "Not recorded at the time." rather than
  inventing one. An entry that is today's reasoning rather than a recorded one sits below
  the line `_Recorded retrospectively in #28; not in the original record._`, after the
  entries the history records.
- **Consequences**: what the decision makes easy, hard or mandatory, and where the rules
  live.

A record may name the issue, PR or CHANGELOG section where the decision was made
("Decided in: ..."). Records carry no dates of their own.

## Adding a record

1. Copy [0000-template.md](0000-template.md) to the next free number with a short kebab
   title.
2. Fill in every section; keep it short and link the reference doc that holds the rules.
3. Add a row to the index below.
4. Link the record from the reference doc that holds the normative rule, so a reader of
   the rule can find the reason.
5. To replace a decision, write a new record and set the old one to
   `superseded by NNNN`; do not rewrite the old record's reasoning.

Use synthetic data only (example.com/org/net, 192.0.2.0/24, 2001:db8::/32):
`tests/test_synthetic_data.py` scans `docs/`.

## Index

| Number | Title | Status |
|---|---|---|
| [0001](0001-render-only-never-apply.md) | Render, validate and plan only; never apply | accepted |
| [0002](0002-deterministic-rendering-agents-as-authors.md) | Deterministic rendering; agents author reviewed code, never render-time config | accepted |
| [0003](0003-kind-backend-split.md) | Kind / backend split; the hub champions no backend | accepted |
| [0004](0004-minimal-runtime-dependencies.md) | Minimal runtime dependencies | accepted |
| [0005](0005-generic-hub-private-hosts-repositories.md) | Generic public hub, real hosts in private repositories | accepted |
| [0006](0006-nixos-disko-offline-guarded-installer.md) | nixos-disko: offline, guarded USB installer | accepted |
| [0007](0007-uniform-entry-point-plugins.md) | Kinds and backends are uniform entry-point plugins | accepted |
| [0008](0008-dotted-third-party-kind-names.md) | Dotted third-party kind names with a dotted API group | accepted |
| [0009](0009-backend-names-flat-vendor-prefix.md) | Backend names stay flat, with a vendor-prefix convention | accepted |
| [0010](0010-single-distribution-package-split-deferred.md) | One distribution; package split deferred | accepted |
| [0011](0011-json-documents-with-format-key.md) | `--json` prints one document with a `format` key | accepted |
| [0012](0012-outputs-pure-function-of-tool-neutral-spec.md) | Outputs are a pure function of the tool-neutral spec | accepted |
| [0013](0013-refs-one-envelope-table-offline.md) | `[refs]` is one envelope table, resolved offline | accepted |
| [0014](0014-static-conformance-exported-fixtures.md) | Conformance is static; fixtures are exported | accepted |
| [0015](0015-scope-and-non-goals.md) | Scope: a layered catalog, and its non-goals | accepted |
| [0016](0016-observed-state-records-separate-repository.md) | Observed state: host-written records in a separate repository | accepted |
