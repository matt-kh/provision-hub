# 0011. `--json` prints one document with a `format` key

## Status

accepted

Decided in: #17 (issue #16), the catalog stack; extended to `digest` and `status` in #27. See CHANGELOG.md, Unreleased, "Added",
"`--json`".

## Context

prov's text output is for humans. Its other consumers, mostly AI agents and scripts, had to
parse that text. They need a stable, machine-readable result, and a way to tell when its
shape changes incompatibly.

## Decision

- `prov kinds`, `backends`, `schema`, `validate`, `plan`, `conformance`, `digest` and
  `status` take `--json` and then print exactly one JSON document on stdout.
- Every document carries a top-level `"format": "prov.cli/v1alpha1"`, versioned on its
  own, independent of the spec `api_version`s. `schema` is the exception: its output
  already is a JSON Schema document, byte-identical to `prov schema`.
- Exit codes are the same as without `--json`. An exit-2 error stays text on stderr
  (`prov: error: …`) with stdout empty; the verb's own result is the JSON document.
- Every document is serialised by `prov.text.json_text`: sorted keys, indent 2, ASCII only
  (`ensure_ascii`), NaN and infinities refused (`allow_nan=False`), one trailing newline.

## Alternatives considered

_Recorded retrospectively in #28; not in the original record._

- **JSON lines** — rejected: each command answers with one result.
- **No format key** — rejected: consumers could not detect an incompatible change.
- **JSON errors on exit 2** — rejected: a parser would see half a document. Usage and
  loading errors stay text on stderr with stdout empty.

## Consequences

- The output is deterministic like everything else prov prints
  ([0002](0002-deterministic-rendering-agents-as-authors.md)).
- Adding a key keeps `format` unchanged; consumers ignore keys they do not know. Removing
  or renaming a key, or changing a value's type or meaning, moves `format` on
  (`prov.cli/v1alpha2`, then `prov.cli/v1` with 1.0) with a CHANGELOG Migration paragraph.
- Plugin-supplied fields are raw, not escaped: a consumer escapes before showing a value to
  a human. `ensure_ascii` is the printing boundary of the document itself.
- `prov list` has no `--json`.

The rules: [kinds.md](../kinds.md#machine-readable-output).
