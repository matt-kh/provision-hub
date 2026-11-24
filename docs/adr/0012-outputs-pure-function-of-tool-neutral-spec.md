# 0012. Outputs are a pure function of the tool-neutral spec

## Status

accepted

Decided in: #17 (issue #16), the catalog stack; the backend-blind input was decided in its
merge review. See CHANGELOG.md, Unreleased, "Added", "Kind outputs".

## Context

Items of the catalog's layers depend on each other: a mirror needs its registry's URL, a
cluster the name of the Secret that holds a CA. Outputs are the typed hand-off between
those layers. The promise of [0003](0003-kind-backend-split.md) is that an item can switch
backends; across layers that holds only if a consumer never depends on which tool rendered
the item it refers to.

prov never reads live state ([0001](0001-render-only-never-apply.md)), and renders must stay
deterministic ([0002](0002-deterministic-rendering-agents-as-authors.md)).

## Decision

- A kind may declare `OUTPUTS` (an object-schema fragment) together with `outputs(spec)`.
- `outputs(spec)` gets only the tool-neutral part of the validated spec: `api_version`,
  `kind` and the kind's core keys, **never the backend's name or table**. It is a pure,
  deterministic function of that spec and returns plain JSON data valid against `OUTPUTS`.
- Post-apply facts and credentials are exported only as **references** the human writes in
  the spec (a path, a secret name), never as values.
- Only `prov validate` computes an item's own outputs; consumers get them through
  [refs](0013-refs-one-envelope-table-offline.md).

## Alternatives considered

- **Outputs that see the backend's name or table** — first implemented that way, rejected
  in the merge review of #17: switching backends would change what referring items render.
- **Outputs computed from `rendered/` or live state** — rejected: they would need a render
  or an apply, and lose determinism.
- **Secret values as outputs** — rejected: post-apply facts and credentials are references,
  never values.

## Consequences

- Switching an item's backend never changes what the items that refer to it render.
- Outputs are known before anything is applied, because they follow from the spec alone.
- prov checks every result (types, 64-bit integers, no control characters, at most 32
  levels deep, valid against `OUTPUTS`); a failure is exit 2 naming the kind.
- The secret-name lint over `OUTPUTS` names and result keys is a **lint, not a guarantee**:
  a name cannot prove that a value is not a secret, so reviewers of a kind check what its
  outputs carry. It is not part of `PLUGIN_API` and may grow between releases.

The rules: [kinds.md](../kinds.md#outputs).
