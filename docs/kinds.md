# Kinds

A **kind** says what an item is; a **backend** says how it is rendered. Every item lives at
`<root>/<kind>/<name>/` and its `spec.toml` names both:

```toml
api_version = "prov/v1alpha1"
kind = "os-install"
backend = "ubuntu-autoinstall"
hostname = "demo"
# ... the kind's core keys ...

[ubuntu-autoinstall]
# ... the backend's own table ...
```

The ref `<kind>/<name>` (`os-install/demo`) is the item's path below the root, the CLI
argument (`prov render os-install/demo`) and the value of a reference in the optional
`[refs]` table (see [References](#references)). The optional `[state]` table holds the
item's lifecycle intent (`decommissioned = true`) for `prov status`; every kind accepts it
and no render reads it ([state.md](state.md)).

| Part | Owned by | Defined in |
|---|---|---|
| Envelope: `api_version`, `kind`, `backend`; optional `[refs]` and `[state]` | prov core | `src/prov/schema.py`, `src/prov/spec.py` |
| Core keys of the kind (`hostname`, `storage`, ...) | the kind | the kind plugin's module (`src/prov/kinds/os_install.py`) |
| Backend table (`[ubuntu-autoinstall]`) | the backend | its module's `SCHEMA` |
| Shared definitions (`#/$defs/user`, `#/$defs/interface`) | prov core | `src/prov/kinds/common.py` |

Kinds and backends are both **plugins**, registered through entry points: prov's own
`os-install` the same way as a third-party kind (see [Kind plugins](#kind-plugins)), and
backends as in [adding-a-backend.md](adding-a-backend.md). Core never imports a particular
kind or backend. All backends of a kind read its core keys, so a kind is a shared public
format: changes to it are reviewed where the kind lives (this repository for provision-hub's
own kinds). provision-hub ships kinds and backends but champions none: the core keys of a
kind describe the item, not one tool's input format. Kinds and backends alike, built-in or
third-party, render deterministically ([determinism.md](determinism.md)).

## Item directories

```text
<root>/<kind>/<name>/spec.toml     # you write this (prov init gives a start)
<root>/<kind>/<name>/rendered/     # owned by prov render; text only; committed
                                   # (a backend may name files it keeps there, e.g. flake.lock)
<root>/<kind>/<name>/build/        # reserved for a later `prov build`; gitignored
```

The kind directory must be an installed kind (`prov kinds`), the name one DNS label
(`[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?`). `<kind>/`, `<name>/`, `rendered/` and `build/` may
not be symlinks.

## Envelope checks

All of these fail with a clear message and exit code 2:

- `api_version` missing, or not one of the kind's versions (today only `prov/v1alpha1`);
- a key of the old host.toml format — `schema_version` or `distro`, or an `[ubuntu]` table
  in a file without `api_version`: the file predates `prov/v1alpha1`; see the migration in
  [CHANGELOG.md](../CHANGELOG.md);
- `kind` missing or different from the directory kind;
- `backend` missing, not registered, or registered for another kind.

`refs` and `state` are reserved as well: a kind may not define a core property and a backend
may not be named `refs` or `state`.

Values echoed in these messages are shown as a repr capped at 80 characters.

Schema and semantic errors after the envelope are findings (`prov validate` exits 1;
`render`, `diff` and `plan` exit 2 because they need a valid spec).

## The kind contract

A kind module exposes:

| Member | Meaning |
|---|---|
| `NAME` | kind id; the directory `<root>/<NAME>/` and the entry-point name. Bare (`[a-z][a-z0-9-]*`) only for provision-hub's own kinds; third-party kinds are dotted (see [Kind plugins](#kind-plugins)) |
| `PLUGIN_API` | the kind contract version the module implements; required, `1` today |
| `VERSIONS` | accepted versions, oldest first: `("v1alpha1",)` → `api_version = "<group>/v1alpha1"` (`prov/v1alpha1` for `os-install`) |
| `PROPERTIES` | JSON Schema of the core keys; may `$ref` `#/$defs/<name>` from `common.DEFS` |
| `REQUIRED` | required core keys |
| `semantic(spec)` | `list[Finding]` for rules JSON Schema cannot express |
| `REFS` | the references an item may hold: `{alias: {"kind", "required", "description"}}` (`required` and `description` optional), `{}` for none (see [References](#references)) |
| `NAME_KEY` | optional: the core key that should equal the item name (a mismatch is a warning) |
| `SCHEMA_URL` | optional: an `https://` base URL ending in `/`; the composed schema's `$id` is `SCHEMA_URL` + `<NAME>.<version>.schema.json` |
| `OUTPUTS` | optional, together with `outputs`: object-schema fragment of what an item exports (see [Outputs](#outputs)) |
| `outputs(spec)` | optional, together with `OUTPUTS`: `dict`, a pure function of the validated spec with defaults |
| `FIXTURES` | optional: conformance fixtures `{name: {"spec", "refs", "expect"}}` that `prov conformance <backend>` renders through each backend (see [Conformance fixtures](#conformance-fixtures)) |

The module docstring's first line is shown by `prov kinds`. Every call into a kind
(`semantic`, `outputs`) goes through a guarded call: an exception, or a result of the wrong
type, is reported as `kind '<NAME>' failed in <method>: ...` with exit code 2, its text
escaped like a backend's.

Core composes `envelope + PROPERTIES + every backend table` into one JSON Schema per kind
(`prov schema <kind>`); for provision-hub's own kinds the published file is
`schema/<kind>.<version>.schema.json` (composed over the built-in backends), and a test keeps
it equal to the code.

## Outputs

A kind may export a few values of each item, for other items to read: a registry's URL, a
cluster's API endpoint, the name of the Secret that holds a CA. [References](#references)
hand them to the backends of the items that refer to it (`ctx.refs`), and `prov validate
--json` shows them. A kind declares both members or neither:

```python
OUTPUTS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["url"],
    "properties": {
        "url": {"type": "string", "pattern": "^https://[a-z0-9.-]+$"},
        "tls_secret_name": {"type": "string"},
    },
}


def outputs(spec):
    return {"url": f"https://{spec['hostname']}", "tls_secret_name": f"{spec['hostname']}-tls"}
```

- **`OUTPUTS`** is an object-schema fragment under the same rules as a backend `SCHEMA`
  ([adding-a-backend.md](adding-a-backend.md), section 2): supported keywords only, no
  `$defs` of its own, `$ref` only to the shared `#/$defs/<name>`. Its top level is
  `"type": "object"` with `properties` and `"additionalProperties": false`, so every output
  name is declared. Like `PROPERTIES`, it must be plain JSON data itself (no bytes, sets,
  tuples, objects or NaN in a `default`, `enum` or `const`). It is public API, versioned
  with the kind.
- **`outputs(spec)`** gets the tool-neutral part of the validated spec with defaults
  applied: `api_version`, `kind` and the kind's core keys, **never the backend's name or
  table**, so an
  item's outputs mean the same whichever backend renders it, and switching backends never
  changes what referring items render. The spec only: no item name, no file, no other item. It must be a **pure,
  deterministic** function of that spec: the same spec gives equal outputs on every run,
  machine and Python version, with no clock, randomness, environment, files, network or
  subprocesses. It gets a copy of the spec, so changing it changes nothing.
- The result must be plain JSON data: a `dict` with string keys whose values are `str`,
  `int`, finite `float`, `bool`, `None`, `list` or `dict` (exact types: no subclasses such
  as `IntEnum`; at most 32 levels deep), valid against `OUTPUTS`. Integers are 64-bit
  (-2^63 to 2^63-1); strings and keys, like spec values, hold no control characters (C0,
  DEL, C1, U+2028, U+2029); no key, at any depth, looks like a secret value (the lint
  below), so an open nested object cannot return one either. prov checks it on every call
  and returns a copy. An exception, another type,
  a non-JSON value or a schema violation is exit code 2:
  `kind '<NAME>' failed in outputs: ...`, escaped.
- `prov validate` computes the outputs of a valid spec (so a broken `outputs` fails it with
  exit 2, with or without `--json`); `render`, `diff` and `plan` do not compute an item's
  own outputs, only those of the items it refers to ([References](#references)).
  A kind without `OUTPUTS` exports `{}`. os-install declares none.

**Post-apply facts are references, never values.** prov never reads live state: no
cluster, host, cloud API or secret store. Outputs are known before anything is applied,
because they follow from the spec alone. A fact that exists only after the apply step (a
generated join token, a kubeconfig, a certificate, an admin password) is exported only as a
**reference the human writes in the spec**: a path where the human keeps it, or the name of
the secret that holds it (`kubeconfig_path`, `ca_secret_name`), never the value.

**The secret-name lint.** `check_kind` checks every `OUTPUTS` property name (at any depth,
in `properties` and `required`, through `$ref` too), and `item_outputs` every key of the
result. A name is split into words (camelCase first, then `_`, `-`, `.` and spaces;
`sshPrivateKey` is `ssh private key`) and looks like a secret value when:

- its last word is not `path`, `name`, `ref` or `file` (those make it a reference:
  `admin_password_path`, `tls_secret_name`, `kubeconfig_file`), and
- a word is `password`, `passwd`, `passphrase`, `pwd`, `secret`, `secrets`, `credential`,
  `credentials` or `kubeconfig`; or two adjacent words are `private key`, `api key`,
  `ssh key` or `tls key`; or the last word is `token` or `tokens`, not after `max` or `num`
  (`join_token`, but not `max_tokens`, `token_count` or `tokenizer`).

It is a lint, not a guarantee: it catches honest mistakes, not every secret (a kind can
still export one under another name), and reviewers of a kind check what its outputs carry.
It is not part of `PLUGIN_API`: its word lists may grow between releases, each time with a
CHANGELOG entry.

## References

An item may refer to other items, by ref, to use their [outputs](#outputs): a mirror names
the registry it pulls from, a VM the image it boots, a cluster the machines of its node
pools. The kind declares which references its items may hold; the spec names the targets
in one optional envelope table, `[refs]`:

```toml
api_version = "example.org/v1alpha1"
kind = "mirror.example.org"
backend = "example-mirror-text"
hostname = "m1.example.org"

[refs]
upstream = "registry.example.org/hub"   # alias = "<kind>/<name>"

[example-mirror-text]
```

```python
REFS = {
    "upstream": {"kind": "registry.example.org", "required": True, "description": "The registry this mirror pulls from."},
    "peer": {"kind": "mirror.example.org"},  # optional, no description
}
```

**Declaring.** `REFS` maps each alias to a dict with `kind`, the kind name of the target
(bare or dotted; it need not be installed when the kind is loaded), and optionally
`required` (`true` or `false`, default `false`) and `description` (a string, default
`""`); any other key is refused. `prov kinds --json` shows every entry with all three keys,
defaults filled in. An alias matches `^[a-z][a-z0-9_-]{0,62}$`: a lowercase letter, then
up to 62 lowercase letters, digits, `_` or `-`. A kind with
`REFS = {}` has no `[refs]` table: its schema does not contain one, and a spec that has one
fails with `unknown key 'refs'`. For a kind with references, the composed schema
(`prov schema <kind>`) has a `refs` object with one string property per alias whose
`pattern` pins the target kind (`^registry\.example\.org/<item name>$`) and that carries
the alias's `description` when it has one, no other aliases (`additionalProperties:
false`), and the required aliases in its `required` (then `refs` itself is required too). os-install declares none, so its schema is unchanged.

**Resolution.** `render`, `diff`, `plan` and `validate` resolve the references of the item
against the root before calling its backend; the backend's `check`, `render` and `plan` get
them as `ctx.refs` ([adding-a-backend.md](adding-a-backend.md)):

```text
ctx.refs["upstream"] == {"ref": "registry.example.org/hub", "kind": "registry.example.org",
                         "name": "hub", "outputs": {"url": "https://hub.example.org:5001"}}
```

- A target is read from `<root>/<kind>/<name>/spec.toml` and **checked in full**: envelope,
  schema, semantic checks, its backend's `check`, and its own references, so the whole
  closure of references is valid; its outputs are then computed (`outputs(spec)`). The
  consumer gets one hop: the outputs of its direct targets, not of theirs.
- A target kind without `OUTPUTS` is allowed, by rule: its items can be referred to, and
  their outputs are `{}` (the reference still records the dependency, and the target is
  still checked).
- Only aliases present in `[refs]` appear in `ctx.refs` (an optional one left out is
  absent). The mapping is deeply read-only: mappings are `types.MappingProxyType`, arrays
  tuples, so a backend cannot change what another call or another item sees; copy them
  (`dict(...)`, `list(...)`) before changing or serialising them.
- Each target is read and checked once per command, however many paths lead to it. Only
  a target found invalid because a chain below it was too long *from where it was reached*
  is checked again when a shorter path reaches it (the depth limit counts from the item
  the command is about, not from the target).
- Only `spec.toml` of a target is read: its `rendered/` and `build/` are not looked at, so
  a target that cannot be rendered does not block the items that refer to it.
- `prov init` checks the starter's `[refs]` by the schema only (the targets may not exist
  yet) and then skips the backend's `check`.

**Errors** are findings at `refs.<alias>` (`prov validate` exits 1; `render`, `diff` and
`plan` exit 2 with the spec's error list, like any invalid spec):

| Finding | Meaning |
|---|---|
| `refs.<alias>: '<value>' does not match ^...$` | the value is not a ref of the declared target kind (wrong kind, invalid name) |
| `refs: unknown key '<alias>'` / `refs: missing required key '<alias>'` | an alias the kind does not declare / a required one is missing |
| `<ref>: kind '<kind>' is not installed` | no plugin provides the target kind |
| `invalid target: <ref>: no such item (...)` | no `spec.toml` at the target's path |
| `invalid target: <ref>: <where>: <message>` | the target is invalid; its first error is shown (`run prov validate <ref>` for all) |
| `invalid target: <a> -> <b>: ...` | an invalid item further down the chain of references, through the refs that lead to it |
| `invalid target: <ref>: <ref>: is a symlink; refused` | the target's `<kind>/` or `<name>/` directory is a symlink (never followed); a symlinked `spec.toml` reads `spec.toml: not a regular file; ...` |
| `<ref> refers to the item itself` | self-reference |
| `reference cycle: <a> -> <b> -> <a>` | a cycle; every item on it is invalid |
| `more than 32 references in a row (<a> -> ... -> <z>)` | a chain longer than 32 references from the item checked |

Findings name targets by ref, never by absolute path. A chain longer than five refs is
shortened to its first three and last two (`a -> b -> c -> ... -> y -> z`). A broken plugin
of a target (an exception in its kind's `semantic` or `outputs` or its backend's `check`)
is exit 2, as for the item itself, naming the target and the plugin:
`reference target registry.example.org/hub: kind 'registry.example.org' failed in outputs: ...`.

**Staleness.** A consumer's rendered files may depend on its targets' outputs, so editing a
target's `spec.toml` can make the consumer's `rendered/` out of date: `prov diff` shows the
change and `prov validate` reports `rendered: out of date with spec.toml` until the
consumer is rendered again. This is intended: render the consumers of an item after
changing it. The output stays deterministic ([determinism.md](determinism.md)): the same
specs of the item and its targets render a byte-identical tree.

**Security.** Resolution only reads `spec.toml` files of targets below the root: it never
follows a symlink (a symlinked `<kind>/` or `<name>/` directory, or a `spec.toml` that is a
symlink or special file, is an error, never read), never leaves the root (refs are a kind
name and one DNS label: no `..`, no `/`), never writes, runs or connects to anything, and
reads no `rendered/` or `build/` files. Values from target specs reach the consumer's
backend only through the target kind's `outputs`.

**Known limitations.** The symlink checks look at a path and then open it, so a process
that swaps a target's directory for a symlink between the check and the read could make
prov read another file: the same exposure as for the item's own `spec.toml` today (the
file itself is opened with `O_NOFOLLOW`, never through a final symlink). Run prov on roots
that only you write to.

## Conformance fixtures

A kind may ship fixtures: small specs, with what every backend must make of them.
`prov conformance <backend>` renders each fixture of the backend's kind through that
backend and checks the result. The check is **static and rendered only**: prov never runs a
plan, a behavioural test runner, a subprocess for a printed command, ssh, a VM or anything
on the network. A backend may render behavioural test files (a `goss.yaml`, a test script)
among its files, and its `plan` prints the command that runs them, for a human
([adding-a-backend.md](adding-a-backend.md#conformance)).

```python
FIXTURES = {
    "basic": {
        "spec": {"hostname": "web.example.org"},          # core keys only
        "expect": {
            "outputs": {"url": "https://web.example.org"},  # optional: equal to the item's outputs
            "contains": ["web.example.org"],              # optional: each in some rendered file
            "absent": ["password"],                       # optional: in no rendered file
        },
    },
    "even-replicas": {"spec": {"hostname": "x.example.org", "replicas": 2}, "expect": {"valid": False}},
    "with-registry": {
        "spec": {"hostname": "m.example.org"},
        "refs": {"registry": {"url": "https://hub.example.org:5000"}},  # alias -> the target's outputs
    },
}
```

**Format.** A fixture name is an item name (one DNS label). Each fixture is plain JSON data
with the keys `spec` (required), `refs` and `expect` (optional), checked when the kind loads
(a bad one is exit 2, like any kind contract error):

- `spec` holds the kind's core keys, never the envelope (`api_version`, `kind`, `backend`)
  or `refs`: prov composes those.
- `refs` maps aliases declared in `REFS` to literal outputs of the target (plain JSON outputs,
  the same rules as [Outputs](#outputs)). No target item is read: the fixture's item gets a
  `[refs]` table with one synthetic ref per alias, `<target kind>/<alias>` (each run of
  characters other than `[a-z0-9]` becomes `-`; when two aliases of the same target kind
  come out equal, the later one in alias order gets `-2`, `-3`, ...), and `ctx.refs[alias]` =
  `{"ref", "kind", "name", "outputs"}` with the literal as `outputs`. Whether a literal
  matches its target kind's `OUTPUTS` is checked by `prov conformance`, because that kind
  may load after this one or not be installed at all: when it is not installed, the literal
  is checked as JSON data only and the `refs` check is `skipped`, saying so.
- `expect`: `valid` (default `true`); `outputs`, checked against the kind's own `OUTPUTS`
  when the kind loads; `contains` and `absent`, lists of non-empty strings; `error`, only
  with `valid = false`, a non-empty string. A fixture with `valid = false` takes no other
  `expect` key: it must be rejected, and stops there.

**Negative fixtures** (`valid = false`) test the kind's rules, not the backend's table. They
are composed like valid ones, with the backend's own table for the fixture (required, as
for a valid fixture, when the backend's `SCHEMA` requires keys; else `{}` without one), and
checked through the real backend. Give a **valid** table: the fixture passes only when the
spec has an error **outside the backend table** (a finding whose `where`, or a schema error
whose message, does not start with the backend's `NAME`): from the schema of the core keys
or `[refs]`, the kind's `semantic`, or the backend's `check` about core keys. An error
inside the table does not count (`rejected only inside the backend table ...` is a `FAIL`),
and as schema errors stop the check before `semantic` runs, an invalid table also hides
the kind's rule. With
`expect.error`, one of those errors must contain that string (matched against
`<where>: <message>`); use it to pin the rule the fixture is about.

A backend may give a table per fixture (`FIXTURES`) and name the fixtures it cannot
express (`UNSUPPORTED_FIXTURES`, with a reason). When the backend is discovered only their
shape is checked (str keys, plain JSON tables, non-empty reasons, no fixture in both; exit 2
otherwise); a key that is not a fixture of the kind (one the kind renamed or removed) does
not stop the backend loading, and `prov conformance` reports it as an extra entry after the
kind's fixtures, `FAIL\t<name>\ttable\tFIXTURES names unknown fixture '<name>' (...)`. A
valid fixture without a table uses `{}` when the backend's `SCHEMA` requires no keys.

**Checks**, per fixture in name order, as `<status>\t<fixture>\t<check>\t<detail>` lines:

| Check | Passes when |
|---|---|
| `supported` | (only for a fixture in the backend's `UNSUPPORTED_FIXTURES`) always `skipped`, with the reason; nothing else runs |
| `table` | (only on failure) the backend has no table for the fixture although its `SCHEMA` requires keys, or names a fixture the kind does not have |
| `refs` | (only with `refs`) every literal matches its target kind's `OUTPUTS`; `skipped` when a target kind is not installed |
| `spec` | the composed spec (envelope with the kind's current `api_version`, the fixture spec, the backend table, `[refs]`) passes the schema, semantic and backend `check` like a `spec.toml`; for `valid = false`: with the backend's real table, it is rejected by an error outside that table (containing `expect.error` when given) |
| `outputs` | the kind's outputs compute, and equal `expect.outputs` when given |
| `render` | two renders return equal file maps (deterministic) |
| `write` | the files are written to a fresh temporary `<tmp>/<kind>/<fixture>/rendered/`, after which `prov diff`'s comparison is empty and a second write changes nothing; an OS error (disk full) is a `FAIL` here |
| `validate` | the backend's `validate` on that directory reports no error; `skipped` when it reports skipped findings (an external tool not installed), listed in the detail |
| `contains` / `absent` | (only when given) each string appears in some rendered file / in none |
| `plan` | the plan lines pass the refusal checks of `prov plan` (no line break or unsafe character); prov never runs them, and conformance does not print them for a human to run |

A failure in `table`, `refs`, `spec`, `render` or `write` ends the fixture (later checks
depend on it). A plugin failure (an exception or a wrongly typed result) is a `FAIL` of the
check it happened in, with escaped text. A fixture's status is `FAIL` when any check failed,
`skipped` when the backend does not support it, else `ok`: a `skipped` check inside a
fixture that ran (`refs` with a target kind not installed, `validate` without its external
tool) leaves the fixture `ok`, and is counted on the summary line, the last line:
`conformance <backend> (<kind>): 4 fixture(s): 3 ok, 0 FAIL, 1 skipped; 2 check(s) skipped`
(the `supported` line of an unsupported fixture is not counted as a skipped check).

**Exit codes.** 0 when no fixture failed (ok or skipped, all of them skipped included), 1
when one did, 2 for an unknown backend, a kind without fixtures (`kind <kind> has no
fixtures`), a broken plugin that fails to load, or a temporary
directory that could not be removed.

**JSON** (`--json`): `{"format", "backend", "kind", "ok", "skipped_checks", "fixtures":
[{"name", "status", "checks": [{"check", "status", "detail"}]}]}`. Statuses are lowercase,
`"ok"`, `"fail"`, `"skipped"` (the text spells a failure `FAIL`), at fixture and check
level; `ok` is `true` when no fixture failed and `skipped_checks` is the summary line's
count. Details are those of the text, raw (plugin text unescaped; [Machine-readable
output](#machine-readable-output)).

**Stability.** The check names above, the line shape, the summary line, the statuses and the
JSON keys are part of the `prov.cli/v1alpha1` format: while it is alpha they change only
with a CHANGELOG entry; new checks and keys are additions (consumers ignore what they do
not know).

**Where it writes.** Only to a fresh temporary directory from `$TMPDIR`
(`prov-conformance-*`), never in the item tree (`prov conformance` does not read the root),
and removed before the command returns, failure or not: best effort against a plugin that
removes the directory or makes part of it unreadable, and a directory that cannot be removed
is an exit-2 error naming it `<tmp>` (look for `prov-conformance-*` in `$TMPDIR`). Its path
never appears in the output: plugin text that names it, resolved through a symlinked
`$TMPDIR` or not, reads `<tmp>`. The output is deterministic (the same plugins give
byte-identical text and JSON).

### Exported fixtures

The fixtures of every kind prov ships, with each built-in backend's tables and reasons, are
also published as generated, language-neutral files next to `schema/`:
`conformance/<kind>.<version>.fixtures.json` (today
[conformance/os-install.v1alpha1.fixtures.json](../conformance/os-install.v1alpha1.fixtures.json)).
They are written by `prov.conformance.export_fixtures(kind, backends)` with the same
canonical JSON as the schema files (sorted keys, indent 2, ASCII only, one trailing
newline), regenerated with `PROV_UPDATE_GOLDENS=1 uv run pytest -q`, and a test fails when
they drift from the code (`tests/test_conformance_builtin.py`). A kind without fixtures gets
no file.

```json
{
  "api_version": "prov/v1alpha1",
  "backends": {
    "<backend>": {
      "fixtures": {"<fixture>": {"...": "the backend table"}},
      "unsupported": {"<fixture>": "why the backend cannot express it"},
      "specs": {"<fixture>": {"api_version": "...", "kind": "...", "backend": "<backend>", "...": "core keys", "<backend>": {"...": "the table"}}}
    }
  },
  "fixtures": {
    "<fixture>": {"spec": {"...": "core keys"}, "refs": {}, "expect": {"valid": true, "contains": ["..."]}}
  },
  "format": "prov.conformance/v1alpha1",
  "kind": "os-install"
}
```

- `format` names this layout; `kind` and `api_version` (the kind's current version) are the
  envelope of every case.
- `fixtures` is the kind's `FIXTURES` in normalised form: every fixture has `spec`, `refs`
  (`{}` without) and `expect` with `valid` filled in; `error`, `outputs`, `contains` and
  `absent` only when given. The meaning of each key is the one above.
- `backends` holds the built-in backends of the kind, each with `fixtures` (its table per
  fixture), `unsupported` (fixture -> reason; a fixture in neither uses `{}` as its table)
  and `specs`: for every fixture the backend does not mark unsupported, negative ones
  included, the fully composed spec exactly as `prov conformance` checks it (one composition
  path, `prov.conformance.compose_spec`): the envelope (`api_version`, `kind`, `backend`),
  the fixture's core keys, `refs` with the synthetic refs when the fixture has literals, and
  the backend table under the backend's name. Every composed spec is valid against
  `schema/<kind>.<version>.schema.json` (negative fixtures break semantic rules only; a test
  checks it).

**Running the same cases in another implementation** (a port of a backend, or a different
renderer reading prov specs): for each backend, take each spec in `specs` as the item's
`spec.toml` content (the item name is the fixture name; with `refs`, the resolved
`ctx.refs[alias].outputs` is the fixture's literal), then apply the checks of the table
above that your tool can express:
schema (`schema/<kind>.<version>.schema.json`) and your semantic rules, rejection for
`valid = false` (with `error` contained in `<where>: <message>` of an error outside the
backend table), deterministic render, the `contains` / `absent` strings in the rendered
files, and your own static validator. These are **static** checks of rendered files only:
passing them says nothing about whether an install works. The format is
`prov.conformance/v1alpha1`: while alpha it changes only with a CHANGELOG entry, and new
keys are additions that readers ignore.

**Exporting a third-party kind.** `prov conformance --export <kind> DIR` writes
`<kind>.<version>.fixtures.json` for any installed kind with fixtures and its installed
backends (all of them, not only the built-in ones) into `DIR`, in the same format, and prints
`wrote <path>`. `DIR` must be given, exist and be a real directory (not a symlink), and must
not lie inside the item tree `<root>/<kind>/` (both resolved; the root is `--root`, else
`$PROV_ROOT`, else the current directory). An existing target file is replaced atomically;
a symlink or other non-regular file in its place is refused, never written through. Exit 0,
or 2 for an unknown kind, a kind without fixtures, or a refused directory or target;
`--export` takes no backend and no `--json`. The repository's own `conformance/` files are
generated from the built-in kinds and backends only, by the test suite.

## Kind plugins

A kind is a module registered under the `prov.kinds` entry-point group; the entry-point name
equals `NAME`. provision-hub registers its own kinds this way in its `pyproject.toml`:

```toml
[project.entry-points."prov.kinds"]
os-install = "prov.kinds.os_install"
```

A third-party package does the same in its own `pyproject.toml` (then `uv add` it next to
prov and run `uv sync`; `uv run prov kinds` should list it):

```toml
[project.entry-points."prov.kinds"]
"registry.example.org" = "prov_registry.kind"
```

**Names.** Bare names (`os-install`) are reserved for kinds the `provision-hub`
distribution registers. Every other kind is **dotted**, Kubernetes CRD style:
`<label>.<group>`, one lowercase DNS label followed by a dotted domain you control
(`registry.example.org`), at most 253 characters. The group needs a dot itself, so
`registry.example` (group `example`) is refused. The name is still one path segment, a
directory name on Linux/POSIX file systems (prov runs on POSIX systems only):

```text
<root>/registry.example.org/<name>/spec.toml     # ref: registry.example.org/<name>
```

**api group.** The domain part is the kind's api group: `api_version =
"example.org/v1alpha1"` for `registry.example.org`. Bare kinds use the group `prov`
(`prov/v1alpha1`); a dotted kind's group always contains a dot, so it can never be `prov`,
and a spec of a dotted kind with `api_version = "prov/v1alpha1"` is rejected like any
other unsupported version.

**Schema `$id`.** `SCHEMA_URL`, when a kind sets it, is a base URL: `https://`, printable
ASCII without spaces, ending in `/`. prov appends the file name, just as it does for
provision-hub's published files: `SCHEMA_URL = "https://schemas.example.org/prov/"` gives
`$id` `https://schemas.example.org/prov/registry.example.org.v1alpha1.schema.json`. A dotted
kind without one gets `urn:prov:schema:<NAME>:<version>`
(`urn:prov:schema:registry.example.org:v1alpha1`). provision-hub's kinds keep the URL of
their published file.

**`PLUGIN_API`.** Each kind declares the contract version it implements, `PLUGIN_API = 1`;
a kind without it, or with another value, is refused. Backends may declare it too (optional,
default 1). The number changes only when the contract changes incompatibly.

**Discovery.** prov loads every kind once per run. It refuses, with exit code 2 and one
line naming the entry point (both, for a duplicate), a kind that cannot be imported, breaks
the contract above, whose entry-point name differs from `NAME`, that is registered twice,
or that uses a bare name without coming from the `provision-hub` distribution.
`prov.kinds.builtin()` reads only that distribution's entry points, so a broken third-party
kind never changes the published schemas.

The distribution check is a **naming rule, not a security boundary**: installed plugins
are trusted code, imported and run by prov like any other dependency, and a package can
claim any distribution name. It keeps third-party kinds out of the bare namespace by
convention; a fork that wants to own bare names must keep the distribution name
`provision-hub`.

`prov list` warns about a dotted directory under the root that holds items but whose kind
is not installed, and about a symlinked dotted directory (never followed). Its warnings go
to stderr, like its errors, so stdout stays the `<ref>\t<backend>` listing; warnings alone
exit 0.

**Backends of a plugin kind** follow [adding-a-backend.md](adding-a-backend.md) with
`KIND = "registry.example.org"`. A backend `NAME` may not equal an envelope key or one of
its own kind's core properties; other kinds' properties do not matter.

## Machine-readable output

`prov kinds`, `backends`, `schema`, `validate`, `plan`, `conformance`, `digest` and `status` take `--json` and then print
exactly one JSON document on stdout: keys sorted, indented by 2, ASCII only (any other
character, controls and bidi marks included, is a `\uNNNN` escape), one trailing newline.
Plugin-supplied fields (summaries, `OUTPUTS` schemas, outputs, plugin findings' `where` and `message`) are raw, as the
plugin gave them, not escaped like the text output: decode the JSON and escape before
showing a value to a human. prov's own messages are composed once for both outputs, so
where core quotes a spec value or a file name (in a finding's `message` or `where`, e.g.
`rendered/line\x0aforged`) it already carries prov's `\xNN` escapes. Exit codes are the
same as without `--json`. An exit-2 error (unknown kind or backend, missing item, invalid
envelope or spec for `plan`, broken plugin, refused plan line) is text on stderr
(`prov: error: …`, possibly with indented continuation lines, e.g. one per schema error of
an invalid spec), and stdout stays empty. `prov list` has no `--json`.

```text
prov kinds --json
{"format", "kinds": [{"name", "group", "api_versions", "summary", "builtin", "outputs", "refs", "fixtures"}, ...]}  # ordered by name

prov backends [--kind K] --json
{"format", "backends": [{"name", "kind", "summary", "builtin", "starter", "check", "preserve",
                          "fixtures", "unsupported_fixtures"}, ...]}
                                                     # ordered by (kind, name), like the text
prov schema <kind> --json                            # the schema itself; --json changes nothing

prov validate <ref> --json                           # exit 0 ok, 1 findings
{"format", "ref", "backend", "ok", "outputs", "refs", "findings": [{"level", "where", "message"}, ...]}

prov plan <ref> --json
{"format", "ref", "backend", "rendered_dir", "render_digest", "lines": [...]}   # render_digest null when not rendered or unreadable (the text header says why)

prov digest <ref> --json
{"format", "ref", "digest"}

prov status [ref ...] --json                         # exit 0, 1 an invalid spec or record (docs/state.md)
{"format", "now", "stale_after_seconds", "state_dir",
 "items": [{"ref", "backend", "lifecycle", "sync", "desired_digest", "decommissioned", "record"}, ...],
 "findings": [{"level", "where", "message"}, ...]}

prov conformance <backend> --json                    # exit 0 ok, 1 a fixture failed
{"format", "backend", "kind", "ok", "skipped_checks", "fixtures": [{"name", "status", "checks": [{"check", "status", "detail"}, ...]}, ...]}
```

| Key | Meaning |
|---|---|
| `format` | the version of these document formats, `prov.cli/v1alpha1`; not in `schema --json`, which is a JSON Schema document byte-identical to `prov schema` |
| `name`, `kind` | kind or backend `NAME`; a backend's `KIND` |
| `group`, `api_versions` | the kind's api group and accepted `api_version` values, oldest first |
| `summary` | first line of the plugin's docstring (`""` without one) |
| `builtin` | registered by the `provision-hub` distribution |
| `starter`, `check` | the backend implements `starter` (`prov init`) / `check` |
| `preserve` | the backend's `PRESERVE` paths (`[]` without) |
| `ref`, `backend` | the item ref and the spec's `backend` value, also when other keys fail validation; `null` only when it does not name a registered backend of the item's kind (in practice an exit-2 envelope error) |
| `outputs` (kinds) | the kind's `OUTPUTS` schema, or `null` for a kind without outputs |
| `outputs` (validate) | the item's outputs ([Outputs](#outputs)), raw like plugin text: `{}` for a kind without `OUTPUTS`; `null` while the spec is invalid (schema, semantic or backend `check` errors), so outputs were not computed. Computed even when `rendered/` is missing or out of date |
| `refs` (kinds) | the kind's `REFS`, defaults filled in: `{alias: {"kind", "required", "description"}}` (`{}` without references) |
| `refs` (validate) | the item's resolved references, `{alias: {"ref", "kind", "outputs"}}` with the target's outputs, raw like plugin text (`{}` without references); `null` while the spec is invalid, including when a reference does not resolve |
| `ok` | no finding of level `error`; the exit code is 0 exactly when it is `true` |
| `findings` | every finding, warnings and `skipped` ones included: errors first, then by `where`, `message`, as in the text output; `where` is `""` for the whole item |
| `fixtures` (kinds) | the names of the kind's conformance `FIXTURES`, sorted (`[]` without) |
| `fixtures`, `unsupported_fixtures` (backends) | the fixture names the backend has a table for, a sorted list, and `{fixture: reason}` of its `UNSUPPORTED_FIXTURES` (`[]` / `{}` without); stable like every key here |
| `fixtures` (conformance) | one entry per fixture, ordered by name; see [Conformance fixtures](#conformance-fixtures) |
| `rendered_dir` | absolute path of the item's `rendered/` directory, the plan's `cd` target |
| `lines` | exactly the lines the text output prints, in order, header comments and `cd` included (text = the lines joined by newlines); a line with an unsafe character is refused, never escaped |

**Stability.** `format` is versioned on its own, independent of the spec `api_version`s.
Adding a key to these objects (as `outputs`, `refs` and `fixtures` were) keeps `format` unchanged: consumers ignore keys they do not know. Removing or renaming a
key, or changing a value's type or meaning, moves `format` on (`prov.cli/v1alpha2`, then
`prov.cli/v1` with 1.0) and carries a CHANGELOG Migration paragraph; check `format` before
reading a document. From `prov.cli/v1` on, changes are additive only, like the spec
formats.

## os-install (v1alpha1)

One machine's operating-system install: host identity, users, network and whole disks.
Authoritative schema: [schema/os-install.v1alpha1.schema.json](../schema/os-install.v1alpha1.schema.json).

| Key | Type (default) | Notes |
|---|---|---|
| `hostname` | DNS label, required | `NAME_KEY`: should equal the item name (warning otherwise) |
| `domain` | DNS domain, optional | FQDN is `hostname.domain` |
| `arch` | `amd64` \| `arm64` (`amd64`) | |
| `firmware` | `uefi` \| `bios` (`uefi`) | |
| `timezone` | IANA zone (`Etc/UTC`) | |
| `locale` | POSIX locale (`en_US.UTF-8`) | |
| `keyboard` | layout (`us`) | |
| `[[users]]` | list of `#/$defs/user` (`[]`) | `name`, `gecos`, `groups`, `shell`, `sudo`, `ssh_authorized_keys`, `password_hash` (crypt hash only) |
| `[network]` | optional | `interfaces` (≥ 1 `#/$defs/interface` when the table is present), `dns.nameservers`, `dns.search`; backends say in `check` when they need it (`ubuntu-autoinstall` always, `nixos-disko` with `networking = "networkd"`) |
| `[storage]` | required | `disks`: whole-disk device paths; the first is the boot disk; all may be wiped |

Semantic checks: addresses in CIDR form, gateways and nameservers are IPs of the right
family, interface and user names unique, every interface has an address or DHCP (the
network checks only when `[network]` is present).

Conformance fixtures (`prov conformance <backend>`; exported to
[conformance/os-install.v1alpha1.fixtures.json](../conformance/os-install.v1alpha1.fixtures.json)),
synthetic data only; each hostname equals its fixture name (the item name conformance
uses), and each positive fixture expects its hostname and every disk path in the rendered
files:

| Fixture | Case | Expected |
|---|---|---|
| `conf-uefi-static` | UEFI, domain example.com, one by-id disk, one interface with a static 192.0.2.10/24 address, gateway 192.0.2.254, nameserver and search domain example.org, one sudo user with an `EXAMPLE` ssh key | valid; hostname, domain, search domain, disk, address (without prefix length), gateway, nameserver and key rendered |
| `conf-uefi-dhcp` | UEFI, one disk, one DHCP interface, the same user | valid; hostname, disk and key rendered; no 192.0.2. address |
| `conf-bios-dhcp` | as `conf-uefi-dhcp` with `firmware = "bios"` | valid; hostname, disk and key rendered (firmware handling is not checked by content: no tool-neutral marker) |
| `conf-two-disks` | two disks, DHCP | valid; hostname and both disks rendered (that the first one is the boot disk is not checked) |
| `conf-bad-gateway-family` | an IPv6 address as `gateway4` | rejected: `network.interfaces[0].gateway4: 2001:db8::1 is not an IPv4 address` |
| `conf-duplicate-user` | the same user twice | rejected: `users: user 'ops' defined more than once` |

Skipped by a built-in backend: `nixos-disko` cannot express `conf-two-disks` (its layouts
install to exactly one disk). Static validation only: passing them means the rendered files
are consistent and pass the backend's `validate`, not that an install works.

Backends: `ubuntu-autoinstall` (Ubuntu Server 22.04/24.04 NoCloud seed; layouts `lvm`,
`zfs`, `mdraid`), `nixos-disko` (NixOS flake with disko and an offline USB installer; layout
`ext4`, `ext4-luks`; systemd-networkd or NetworkManager, optional GNOME;
[backends/nixos-disko.md](backends/nixos-disko.md)). Candidates: Proxmox VE answer
files, Talos machine configs.

## Planned kinds (sketches, not implemented)

These show the direction; their keys will change before they land.

- **image** — a reusable OS image: base distro and release, architecture, packages, users
  baked in, output format (qcow2, raw, ISO). Backends could be cloud-image customisation
  (cloud-image-nocloud), Packer with QEMU, or an image schematic service (talos-schematic).
  This is the second kind of the first wave.
- **vm** — one virtual machine: CPU, memory, disks, NICs, firmware, the image or
  os-install item it boots (a reference such as `image/ubuntu-24-04`), the hypervisor it
  targets. Backends render hypervisor definitions; prov still never creates the VM.
- **cluster** — a Kubernetes cluster built from a distribution: distribution and version,
  node pools referencing vm or os-install items, network ranges, add-ons. Backends per
  distribution (Talos, k3s, RKE2, kubeadm), none preferred.

`prov graph` and `prov build` (artefacts under `build/`) come in later releases.

## Versioning policy

- `api_version` is `<group>/<version>` (`prov/<version>` for provision-hub's own kinds);
  each kind lists the versions it accepts in `VERSIONS`.
- **`v1alpha*` may break.** While a kind is alpha, keys may be renamed, removed or tightened;
  every breaking change carries a migration note in [CHANGELOG.md](../CHANGELOG.md), and the
  version moves on (`v1alpha2`) when existing files stop validating.
- **1.0 freezes `prov/v1`.** From the 1.0 release, `prov/v1` changes are additive and
  optional only: a new optional key, a new enum value, a new backend. Anything breaking
  (removing or renaming a key, tightening a constraint, changing a meaning) becomes
  `prov/v2`, and prov keeps a reader for `prov/v1` files.
- Backend tables follow the same policy as the kind they belong to.
