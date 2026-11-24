# 0004. Minimal runtime dependencies

## Status

accepted

Decided in: #3, the first release skeleton ("Runtime deps: `pyyaml`, and `tomli` on 3.10
only").

## Context

prov is a command-line tool installed next to private repositories that pin its releases
([0005](0005-generic-hub-private-hosts-repositories.md)), and it runs on Python 3.10 and
newer. It reads TOML, writes and parses YAML, composes and checks JSON Schema, and prints
diffs. Each runtime dependency is one more thing every user installs and one more input
that has to stay pinned and reviewed.

The repository does not record a longer rationale than the rule itself.

## Decision

Runtime dependencies stay minimal:

- `pyyaml`: deterministic emission of autoinstall and cloud-init files, and parsing them
  in `validate`;
- `tomli`: TOML reading on Python 3.10 only; 3.11 and newer use the standard library's
  `tomllib`.

Everything else uses the standard library. A new runtime dependency must be justified in
its PR. Test-only tools go in the `dev` dependency group.

## Alternatives considered

Not recorded at the time.

One choice is visible in the code: the JSON Schema checks run on prov's own code in
`src/prov/schema.py`, and `jsonschema` is a dev dependency only, used by the tests to
cross-check the published schema with a reference validator.

## Consequences

- Installing prov pulls in at most two small packages.
- Features a library would give for free (schema validation, diffing, YAML emission
  details) are written and tested in prov, and the published schema is cross-checked
  against `jsonschema` in tests.
- Each dependency in `pyproject.toml` carries a comment with its reason; `uv.lock` is
  committed.
- Optional external tools (`cloud-init`, `nix`) are never dependencies: `validate` reports
  them as skipped, e.g. `skipped: cloud-init not installed (cloud-init schema)`.

The rules: CLAUDE.md ("Dependencies") and the comments in `pyproject.toml`.
