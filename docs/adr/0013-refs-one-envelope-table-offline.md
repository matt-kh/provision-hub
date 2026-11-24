# 0013. `[refs]` is one envelope table, resolved offline

## Status

accepted

Decided in: #17 (issue #16), the catalog stack. See CHANGELOG.md, Unreleased, "Added",
"References".

## Context

Before #16, `REFS` and `Context.refs` were reserved but unused: items could not hand results
to each other. With [outputs](0012-outputs-pure-function-of-tool-neutral-spec.md), an item
needs a way to name the items whose outputs it uses, and core needs a way to resolve those
names that keeps prov offline, deterministic and confined to the root.

## Decision

- A spec names its targets in one optional envelope table, `[refs]`, mapping an alias to a
  ref `<kind>/<name>`. Aliases match `^[a-z][a-z0-9_-]{0,62}$`.
- The kind declares its aliases in `REFS`: `kind` required, `required` and `description`
  optional. `refs` is a reserved envelope key.
- Core resolves references offline, from the targets' `spec.toml` only. Each target is
  checked in full, its own references included, and its kind's outputs reach the
  consumer's backend as `ctx.refs[alias]` = `{"ref", "kind", "name", "outputs"}`.
- Resolution never follows a symlink and never leaves the root. Self-references, cycles and
  chains of more than 32 references (`MAX_DEPTH`) are findings.
- `ctx.refs` is deeply read-only. A target kind without `OUTPUTS` gives `{}`.

## Alternatives considered

_Recorded retrospectively in #28; not in the original record._

- **Per-kind reference keys inside the core properties** — rejected: every kind would
  reinvent parsing, and core could not resolve them uniformly.
- **Reading the target's `rendered/`** — rejected: it couples the consumer to the
  producer's backend.
- **Live lookups** — rejected: non-deterministic, and they need the network.

## Consequences

- Repository content cannot redirect reads outside the root; prov still never writes, runs
  or connects to anything while resolving.
- A target that cannot be rendered does not block the items that refer to it: its
  `rendered/` and `build/` are never read.
- Editing a target can make its consumers' `rendered/` out of date; `prov validate` reports
  it and `prov diff` shows it. Render the consumers of an item after changing it.
- A kind with `REFS = {}` has no `refs` table in its schema, so os-install's schema did not
  change. Backends declare no references of their own (`REFS` must be empty).
- Known limitation: the symlink checks look at a path and then open it, the same exposure
  as for the item's own `spec.toml`. Run prov on roots that only you write to.

The rules: [kinds.md](../kinds.md#references).
