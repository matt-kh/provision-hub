# 0009. Backend names stay flat, with a vendor-prefix convention

## Status

accepted

Decided in: #17 (issue #16), the catalog stack. See CHANGELOG.md, Unreleased, "Added",
"Kind plugins" ("a vendor-prefix naming convention for third-party backends").

## Context

Kinds got dotted, owner-qualified names ([0008](0008-dotted-third-party-kind-names.md)).
Backends are plugins too, and third parties ship them, so the same question arose for
backend names. A backend's `NAME` appears in every spec (`backend = "..."`) and names the
backend's own table (`[ubuntu-autoinstall]`), so a longer name costs every spec.

## Decision

- Backend names are not namespaced: `NAME` is an id, `[a-z][a-z0-9-]*`, in one flat,
  global set. A name registered twice is refused, and a spec's `backend` must name a
  backend of the item's kind.
- Third parties should use a vendor prefix, `<vendor>-<tool>` (`acme-talos`, not `talos`),
  so they do not collide with a backend another package, or provision-hub itself, ships
  later. This is a convention, not a check.
- The convention is revisited before 1.0.

## Alternatives considered

_Recorded retrospectively in #28; not in the original record._

- **Namespacing backends like kinds** — rejected: it would add ceremony to every spec for
  little gain.

## Consequences

- Specs stay short and backend tables keep plain names.
- Nothing stops two packages from shipping the same backend name; installing both is a
  load error (exit 2, "registered twice"), not a silent override.
- A backend `NAME` may not equal an envelope key (`refs` and `state` included) or a core
  property of its own kind.

The rules: [adding-a-backend.md](../adding-a-backend.md#naming-third-party-backends) and
section 1 of the same page.
