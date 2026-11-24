# 0002. Deterministic rendering; agents author reviewed code, never render-time config

## Status

accepted

Decided in: #3 (committed rendered examples doubling as goldens); the principle was
written down as [determinism.md](../determinism.md) in #17.

## Context

Reviewers decide what gets installed by reading two diffs: the diff of `spec.toml` and the
diff of `rendered/`. That only works if nothing else can change the output. A render that
reads the clock, randomness, the network or a model's answer would produce a different tree
on another machine or another day, and the reviewed diff would no longer be what gets
installed.

Agents are prov's main users. They can write specs and code quickly, which raises the
question of where their output may enter.

## Decision

- `render` is a pure function of the spec and the backend's code: no I/O, no clock, no
  randomness, no network. Output is stable (sorted keys, stable ordering, no timestamps or
  random ids, text only, final newline, no trailing whitespace).
- Inputs a render points at are pinned: by the backend's code (e.g. the disko tag), by the
  spec (`release`, a flake input with a tag), or by a human-made lock file kept through
  `PRESERVE`.
- Agents author **specs, kinds and backends**, and humans review them: specs and host-owned
  fragments through a PR to the repository that holds the items, kinds and backends as code
  with goldens and tests. Nothing is generated at render time by an agent, a model or a
  network service.
- When an item needs something no backend can express, the fix is a spec key, a backend
  change or a host-owned fragment (the nixos-disko `local.nix` pattern), each reviewed.

## Alternatives considered

- **Agents generating native config per item at render time** — listed as a non-goal in
  the design notes for the catalog work (#16): it would make the output depend on a model
  rather than on reviewed input, which breaks the two-diff review above.

_Recorded retrospectively in #28; not in the original record._

- **An "agent recipe" tier** — proposed in the #16 design discussion: for a tool without a
  backend, an agent would write that tool's native config for each item. Rejected for the same
  reason: every render would carry unreviewed, non-reproducible output. The cost of rejecting
  it is that a tool without a backend cannot be used until someone (often an agent) writes the
  backend as code, usually one reviewed PR. The non-determinism then happens once, at
  development time, and review and goldens freeze it.

## Consequences

- The committed `examples/<kind>/<name>/rendered/` trees are goldens:
  `tests/test_goldens.py` renders every example twice and compares both with the committed
  files. A golden that changes needs a reason in the PR.
- `prov diff` against a fresh render is empty, and rendering twice is a no-op.
- Upgrading a pinned input is a code change with its own golden diff.
- Third-party kinds and backends carry the same guarantee and prove it the same way;
  `prov conformance` renders every fixture twice and compares the bytes.
- Escape hatches stay outside `rendered/`, opt in explicitly in the spec, and are checked
  by the backend.

The rules: [determinism.md](../determinism.md) and
[adding-a-backend.md](../adding-a-backend.md), section 3.
