# 0016. Observed state: host-written records in a separate repository

## Status

accepted

Decided in: #27 (issue #26). See CHANGELOG.md, Unreleased, "Added", "Observed state".

## Context

prov knows an item's **desired** state: `spec.toml` and the committed `rendered/` tree.
Nothing recorded what a host actually applied, from which render, when, or whether it still
matches. prov cannot collect that itself: it never runs a command, opens a connection or
touches a host ([0001](0001-render-only-never-apply.md)).

## Decision

- Something on the host (a later pull agent, or a human after an apply) writes a small JSON
  **state record**, `<state-dir>/<kind>/<name>.json`, format `prov.state/v1alpha1`
  (`schema/prov.state.v1alpha1.schema.json`). prov never writes one.
- prov only reads: `prov digest <ref>` prints `sha256:<64 hex>`, the identity of
  `rendered/` (also in the `prov plan` header), and `prov status` compares the records with
  the desired state from local files only.
- The records live outside the infrastructure repository: in a separate repository, or a
  directory that only hosts write.
- Retiring an item is declared in the spec's optional `[state]` table
  (`decommissioned = true`), never in the record. No render reads `[state]`.

## Alternatives considered

- **Records in the infrastructure repository** — rejected: a host would need write access
  to the specs, state updates would conflict with spec changes, and a state commit could
  trigger an apply.
- **Decommission recorded in the state record** — rejected: retiring an item is a decision,
  so it belongs in the reviewed spec.
- **A separate `installed` lifecycle state** — dropped in favour of a separate sync column
  (`in-sync`, `pending`, `drifted`, `unknown`).

## Consequences

- prov stays render-only; `tests/test_state.py` guards that `digest` and `status` read
  local files only.
- The digest is reproducible with GNU coreutils and findutils, so an agent can compute it
  without prov.
- The record carries the base (`applied_rev`, `render_digest`) a pull agent needs for a
  three-way comparison, so "git changed" can be told apart from "the host changed". That
  agent, and the rest of `[state]`, are later steps.
- `state` is a reserved name like `refs`: no kind property and no backend `NAME` may use it.

The rules: [state.md](../state.md) ([State records](../state.md#state-records),
[How an agent uses this](../state.md#how-an-agent-uses-this-later-releases)).
