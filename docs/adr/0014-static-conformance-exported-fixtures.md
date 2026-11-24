# 0014. Conformance is static; fixtures are exported

## Status

accepted

Decided in: #17 (issue #16), the catalog stack. See CHANGELOG.md, Unreleased, "Added",
"Conformance" and "os-install conformance fixtures".

## Context

A kind's core keys are a contract every backend of the kind reads
([0003](0003-kind-backend-split.md)). Before #16 the only check was an optional
per-backend `check()`; nothing showed that two backends made the same thing of the same
spec. Third-party backends, and implementations outside Python, needed the same yardstick.

prov never applies anything ([0001](0001-render-only-never-apply.md)).

## Decision

- A kind may ship `FIXTURES`: small specs and what every backend must make of them. A
  backend gives its table per fixture (`FIXTURES`) and names those it cannot express
  (`UNSUPPORTED_FIXTURES`, with a reason).
- `prov conformance <backend>` is **static and rendered only**: the spec checks, outputs, a
  double render compared byte for byte, a write to a fresh temporary directory from
  `$TMPDIR` that it removes, the backend's `validate`, `contains` / `absent` checks and the
  plan refusal checks. Nothing else.
- A negative fixture (`expect.valid = false`) is composed with the backend's real table,
  and only an error outside that table counts, optionally matching `expect.error`.
- The fixtures of every built-in kind, with the built-in backends' tables, reasons and
  fully composed specs, are exported as `conformance/<kind>.<version>.fixtures.json`
  (format `prov.conformance/v1alpha1`); `prov conformance --export <kind> DIR` writes the
  same file for any installed kind with fixtures.

## Alternatives considered

- **Running behavioural tests (goss, VMs, real installs) inside `prov conformance`** —
  rejected: it would execute things, depend on the environment and break never-apply.
  Backends may run behavioural tests in their own CI.
- **Negative fixtures without a backend table** (the first idea) — replaced: it forced
  loosening the backend schema.

## Consequences

- Passing conformance says the rendered files are consistent and pass the backend's
  `validate`, not that an install works.
- Non-Python implementations, such as a Terraform module or an Ansible role, can test
  against the same cases from the exported file.
- The exported files are generated and drift-tested (`tests/test_conformance_builtin.py`);
  `tests/test_conformance.py` guards that conformance runs nothing and writes only to its
  temporary directory. The one exception is a backend's `validate`, which conformance calls:
  the built-in backends run a read-only local validator when it is installed
  (`cloud-init schema`, `nix-instantiate --parse`); conformance core itself runs nothing.
- A backend lists what it cannot express instead of relaxing `validate` or `check`.

The rules: [kinds.md](../kinds.md#conformance-fixtures),
[kinds.md](../kinds.md#exported-fixtures) and
[adding-a-backend.md](../adding-a-backend.md#6-conformance).
