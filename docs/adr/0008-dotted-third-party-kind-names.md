# 0008. Dotted third-party kind names with a dotted API group

## Status

accepted

Decided in: #17 (issue #16), the catalog stack. See CHANGELOG.md, Unreleased, "Added",
"Kind plugins".

## Context

Once kinds are plugins ([0007](0007-uniform-entry-point-plugins.md)), packages that do not
know about each other name kinds. Names must not collide, should show who owns them, and
must fit where a kind name already appears: the item path `<root>/<kind>/<name>/spec.toml`
and refs written as `<kind>/<name>`. A kind also needs an api group for its `api_version`.

## Decision

- A third-party kind name is dotted, Kubernetes CRD style: `<label>.<group>`, one lowercase
  DNS label followed by a dotted domain the owner controls (`registry.example.org`), at
  most 253 characters. The group needs a dot itself, so `registry.example` is refused.
- The domain part is the kind's api group: `api_version = "example.org/v1alpha1"` for
  `registry.example.org`. Bare kinds use the group `prov`; a dotted kind's group always
  contains a dot, so it can never be `prov`.
- The name stays one path segment: `<root>/registry.example.org/<name>/spec.toml`, ref
  `registry.example.org/<name>`.
- `SCHEMA_URL`, when a kind sets it, is an https base URL to which prov appends
  `<NAME>.<version>.schema.json`. A dotted kind without one gets the `$id`
  `urn:prov:schema:<NAME>:<version>`.

## Alternatives considered

- **A slash, `example.org/registry`** (the first idea in the #16 plan) — rejected: the kind
  is a path segment and refs say `<kind>/<name>`, so a slash would break both the layout
  and the parsing.

_Recorded retrospectively in #28; not in the original record._

- **A vendor prefix, `acme-registry`** — rejected: it collides easily and carries no
  ownership signal.
- **CRD style, label plus DNS group** — chosen: the domain shows who owns the name,
  operators and agents already know the pattern, and the group/version split mirrors CRDs.

## Consequences

- A kind name says who owns it, and two owners of different domains cannot collide.
- prov runs on POSIX file systems only; a dotted name is a valid directory name there.
- `prov list` warns about a dotted directory with items whose kind is not installed, and
  about a symlinked dotted directory (never followed).
- A spec of a dotted kind with `api_version = "prov/v1alpha1"` is rejected like any other
  unsupported version.
- Backend names follow a different rule ([0009](0009-backend-names-flat-vendor-prefix.md)).

The rules: [kinds.md](../kinds.md#kind-plugins) ("Names", "api group", "Schema `$id`").
