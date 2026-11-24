# 0001. Render, validate and plan only; never apply

## Status

accepted

Decided in: #3 (issue #2), the first release skeleton.

## Context

Provisioning ends in destructive steps: wiping disks, booting installers, creating VMs,
switching a running system. A tool that both generates the inputs and runs these steps
needs credentials, network access and access to the target, and a bug in it can destroy a
machine.

prov's users are humans and, mostly, AI agents working through an agent harness. Both need
to produce and review provisioning input safely; neither should be able to apply it by
running prov.

## Decision

prov renders, validates and plans. It never applies.

- `render` writes only under `<kind>/<name>/rendered/`.
- `validate` checks files statically and may run read-only local validators
  (`cloud-init schema`, `nix-instantiate --parse`); a missing tool is reported as
  `skipped`.
- `plan` only returns text: the exact steps a human runs, with every spec value
  shell-quoted.
- No command writes disks, opens ssh connections, creates VMs, calls the network or runs
  the commands it prints.

Later verbs keep this line: `conformance` is static and writes only to a temporary
directory it removes, except `conformance --export`, which writes one file, atomically,
into an existing real directory outside `<root>/<kind>/`; `digest` and `status` only read local files, and state records are
written by host agents, never by prov ([state.md](../state.md)). `build/` is reserved for a
later `prov build`, which will write only there.

## Alternatives considered

Not recorded at the time as a comparison of options. The design notes for the catalog work
(#16) restate it as a non-goal: applying anything and day-2 reconciliation stay outside
prov ("prov renders, humans apply"), and stateful tools can only be render targets.

## Consequences

- prov needs no credentials and no access to targets; it is safe to run anywhere,
  including in CI and by agents.
- Every apply step is a human's, copied from `prov plan`. Plans must be exact and
  paste-safe: core refuses plan lines with unprintable characters instead of escaping them.
- prov cannot tell whether an artefact works. `validate` never claims it does; behavioural
  tests (a VM install, hardware) stay outside prov, and docs say "static validation only"
  until an install has been done.
- What a host actually runs is observed outside prov and only read back from state records.
- Tests enforce it: `tests/test_plan.py` (plan executes nothing),
  `tests/test_conformance.py` and `tests/test_state.py`.

The rules: CLAUDE.md "Never add apply behaviour", [determinism.md](../determinism.md)
("Outside `prov`") and [adding-a-backend.md](../adding-a-backend.md), section 3.
