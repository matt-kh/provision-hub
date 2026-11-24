# Observed state: `prov digest`, state records and `prov status`

prov knows an item's **desired** state: `spec.toml` and the committed `rendered/` tree. What
a host actually runs is **observed** state. prov never collects it, because it never runs a
command, opens a connection or touches a host. Something on the host (the agent a backend
renders for [`[state.report]`](#reporting-state-statereport), or a human after an apply)
writes a small JSON **state record**, and `prov status` compares the records with the
desired state from local files only.

```text
 infrastructure repo (desired)            state repo (observed)
 <root>/<kind>/<name>/spec.toml           <state-dir>/<kind>/<name>.json
 <root>/<kind>/<name>/rendered/   ──┐     written by the host, never by prov
                                    │              │
                         prov digest│              │ render_digest, last_seen, drift ...
                                    ▼              ▼
                              prov status --state-dir <state-dir>
```

Keep the records out of the infrastructure repository: a separate repository (or a
directory that only hosts write) means a host never needs write access to the specs, state
updates never conflict with spec changes, and a state commit can never trigger an apply.

## Render digest

`prov digest <ref>` prints the identity of a render, `sha256:<64 hex>`. It covers every
regular file in `rendered/`, including files the backend preserves (`PRESERVE`, e.g.
nixos-disko's `flake.lock`, which pins what gets built). File modes are not included.
`prov plan` prints the same value in its header, so the person who applies a render can
record what they applied.

The digest is sha256 over a `sha256sum` manifest: one `<sha256 hex>  <path>` line per file
(two spaces, POSIX relative path, newline-terminated), ordered by the path's UTF-8 bytes. An
agent reproduces it with GNU coreutils and findutils alone (BusyBox and BSD `find` lack
`-printf`), run inside `rendered/`:

```sh
set -o pipefail   # an unreadable file or directory must fail, not hash part of the tree
find . -type f -printf '%P\0' | LC_ALL=C sort -z | xargs -0r sha256sum -- | sha256sum
# prefix the first field with "sha256:"
```

An empty `rendered/` has the digest of the empty manifest. `prov digest` exits 2 when the
item is not rendered, when `rendered/` holds a symlink, a special file, an unsafely named
file or a name with a backslash (`sha256sum` would escape it; `prov render` never writes
any of those), or when a file or directory in it cannot be read.

## State records

A record is `<state-dir>/<kind>/<name>.json`: one JSON object, at most 256 KiB, a regular
file (no symlinks, and the `<kind>` directory may not be one either). Its schema is
[schema/prov.state.v1alpha1.schema.json](../schema/prov.state.v1alpha1.schema.json):

```json
{
  "api_version": "prov.state/v1alpha1",
  "ref": "os-install/demo",
  "last_seen": "2026-01-10T11:00:00Z",
  "applied_rev": "0123456789abcdef0123456789abcdef01234567",
  "render_digest": "sha256:<64 hex>",
  "generation": "42",
  "drift": {"status": "clean", "detail": "free text for a human"},
  "facts": {"hostname": "demo", "addresses": ["192.0.2.10/24"]},
  "agent_version": "0.1.0"
}
```

| Key | Required | Meaning |
|---|---|---|
| `api_version` | yes | `prov.state/v1alpha1` |
| `ref` | yes | the item, `<kind>/<name>`; must equal the record's path |
| `last_seen` | yes | when the host last reported, RFC 3339 with `Z` or a numeric offset |
| `drift` | yes | `status`: `clean`, `drifted` (the host no longer matches what it applied) or `unknown` (the agent cannot tell); optional `detail`, free text |
| `applied_rev` | no | the infrastructure-repository commit the host last applied (lowercase hex, 7–64) |
| `render_digest` | no | the `prov digest` of the render the host last applied |
| `generation` | no | backend-specific generation of the running system, as a string |
| `facts` | no | backend-specific facts (hardware, disks, addresses); free-form |
| `agent_version` | no | the version of whatever wrote the record |

Unknown keys are refused. As in `spec.toml`, strings may not contain control characters,
including newlines and tabs, so an agent writes `detail` and `facts` text as single lines.
While the format is `v1alpha1` it changes only with a CHANGELOG entry.

## Lifecycle intent: the `[state]` table

Retiring an item is a decision, so it lives in the spec, not in the record: the optional,
kind-neutral envelope table `[state]`.

```toml
[state]
decommissioned = true   # prov status shows the item as decommissioned
```

No render reads `decommissioned`: adding or changing it never changes `rendered/` or its
digest. `[state.report]` (below) does.

## Reporting state: `[state.report]`

`[state.report]` asks the backend to render a **report-only state agent**: a program the
host runs on a timer that writes the item's record to the state repository. prov only
renders it; it never runs it, and the agent never changes the host.

```toml
[state.report]
repo = "git@git.example.com:example/hosts-state.git"   # ssh git URL; the host needs write access
known_hosts = ["git.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLE"]   # host keys
# branch = "main"                                        # defaults shown
# interval = "15m"                                       # how often the agent checks
# heartbeat = "6h"                                       # push at least this often when nothing changed
# identity_file = "/var/lib/prov-state/id_ed25519"       # the deploy key's path ON THE HOST
```

| Key | Default | Meaning |
|---|---|---|
| `repo` | required | `ssh://user@host[:port]/path` or `user@host:path`; no part may start with `-` |
| `known_hosts` | required | 1–16 known_hosts lines (no `@cert-authority`/`@revoked` markers); one must name the repository's host (`[host]:port` for a port other than 22; hashed lines are not checked) |
| `branch` | `main` | branch the agent writes to |
| `interval` | `15m` | how often the agent checks, `<n>` plus `s`, `m`, `h` or `d` |
| `heartbeat` | `6h` | the agent pushes when the record changed apart from `last_seen`, or when the last push is older than this; must not be shorter than `interval`; a warning from `72h` (`prov status`'s default `--stale-after`), when an idle host would show as stale |
| `identity_file` | `/var/lib/prov-state/id_ed25519` | path of the ssh deploy key on the host; prov never renders a key, `prov plan` prints how to create one there |

Core checks these for every kind; a backend renders the agent only when it declares
`STATE_AGENT = True` ([adding-a-backend.md](adding-a-backend.md)), and any other backend
refuses a spec with `[state.report]`. Backends with an agent today: **nixos-disko**
([its docs](backends/nixos-disko.md#state-agent-statereport)). Unlike `decommissioned`,
`[state.report]` changes `rendered/` and therefore the digest.

**Credentials.** Give each host its own deploy key with write access to the **state**
repository only. The infrastructure repository never needs a writable key on a host: a
host that could push there could push to the branch every host applies from. Host keys are
pinned from the spec (`StrictHostKeyChecking=yes`), so a host never trusts a server on
first use.

## `prov status`

```sh
prov status [ref ...] [--state-dir DIR] [--stale-after 72h] [--now RFC3339] [--json]
```

`prov status` reads the specs, `rendered/` and the records. It never writes, runs or
fetches anything. Without refs it shows every item under the root (the items `prov list`
shows), plus a warning for each record whose item no longer exists. `--state-dir` defaults
to `$PROV_STATE_DIR`. Without either, there are no records and every item is `planned`.
`--now` replaces the clock, which gives reproducible output in tests and CI.

The text output has one line per item: `<ref>\t<lifecycle>\t<sync>\t<applied rev, 12
chars>\t<last seen>` (`-` where there is none). Findings go to stderr.

| Lifecycle | When |
|---|---|
| `planned` | no record yet |
| `active` | the record's `last_seen` is within `--stale-after` (default 72h) |
| `stale` | the record is older than that: the host stopped reporting |
| `decommissioned` | `[state] decommissioned = true`, whatever a valid record says; still reporting within `--stale-after` is a warning |
| `invalid` | the spec or the record is invalid, or `spec.toml` cannot be read (error findings, exit 1); wins over `decommissioned` |

| Sync | When (the first row that matches) |
|---|---|
| `-` | no record |
| `drifted` | the record's `drift.status` is `drifted` |
| `unknown` | the record has no `render_digest`, or `rendered/` is missing or unreadable |
| `pending` | the record's `render_digest` differs from the digest of `rendered/`: a newer render waits to be applied |
| `unknown` | `drift.status` is `unknown` |
| `in-sync` | the host applied the current render and reports no drift |

Exit codes: 0 whatever the lifecycle and sync (drift, pending and stale are states, not
errors), 1 when an item, a record or the scan has an error (an unreadable `rendered/` file
included), 2 for a usage error (an unknown or missing named ref, a missing state directory,
a bad `--stale-after` or `--now`). `--state-dir ""` means no records even when
`$PROV_STATE_DIR` is set. A
`last_seen` more than five minutes in the future is a warning (clock skew on the host).

`--json` prints one document ([Machine-readable output](kinds.md#machine-readable-output)):

```text
{"format", "now", "stale_after_seconds", "state_dir",
 "items": [{"ref", "backend", "lifecycle", "sync", "desired_digest", "decommissioned", "record"}, ...],
 "findings": [{"level", "where", "message"}, ...]}
```

`record` is the record exactly as written (raw values: escape them before showing them to a
human), or `null`.

## How an agent uses this

Today's agent (nixos-disko, `[state.report]`) only reports: `render_digest`, `generation`,
the drift it can see on the host, and facts. Applying changes from git (converge) is
planned, not part of this release. The record format is designed for a pull agent that
compares three things on every poll, like a three-way merge:

- **base**: what the host last applied (`applied_rev` and `render_digest` in its own last record);
- **host**: what the host runs now (`generation`, its drift check);
- **desired**: the newest render of its item on the infrastructure repository's default branch.

If only `desired` moved, the change can be applied, by the agent or by a human, depending
on the host's policy. If the host drifted and `desired` also moved, the agent holds and
reports `drifted` rather than overwriting the host or adopting its changes. A human then
either adopts the change through a reviewed spec change or discards it. Without the base in
the record, "git changed" could not be told apart from "the host changed".
