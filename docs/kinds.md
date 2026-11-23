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
argument (`prov render os-install/demo`) and the syntax future cross-references will use.

| Part | Owned by | Defined in |
|---|---|---|
| Envelope: `api_version`, `kind`, `backend` | prov core | `src/prov/schema.py`, `src/prov/spec.py` |
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
| `REFS` | cross-spec references; reserved, empty for now |
| `NAME_KEY` | optional: the core key that should equal the item name (a mismatch is a warning) |
| `SCHEMA_URL` | optional: an `https://` base URL ending in `/`; the composed schema's `$id` is `SCHEMA_URL` + `<NAME>.<version>.schema.json` |
| `OUTPUTS` | optional, together with `outputs`: object-schema fragment of what an item exports (see [Outputs](#outputs)) |
| `outputs(spec)` | optional, together with `OUTPUTS`: `dict`, a pure function of the validated spec with defaults |

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
cluster's API endpoint, the name of the Secret that holds a CA. Cross-spec references
(later) will hand them to the backends of the items that refer to it; today they are shown
by `prov validate --json`. A kind declares both members or neither:

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
  applied: the envelope and the kind's core keys, **never the backend's table**, so an
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
  exit 2, with or without `--json`); `render`, `diff` and `plan` do not need them yet.
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

`prov kinds`, `backends`, `schema`, `validate` and `plan` take `--json` and then print
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
{"format", "kinds": [{"name", "group", "api_versions", "summary", "builtin", "outputs"}, ...]}  # ordered by name

prov backends [--kind K] --json
{"format", "backends": [{"name", "kind", "summary", "builtin", "starter", "check", "preserve"}, ...]}
                                                     # ordered by (kind, name), like the text
prov schema <kind> --json                            # the schema itself; --json changes nothing

prov validate <ref> --json                           # exit 0 ok, 1 findings
{"format", "ref", "backend", "ok", "outputs", "findings": [{"level", "where", "message"}, ...]}

prov plan <ref> --json
{"format", "ref", "backend", "rendered_dir", "lines": [...]}
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
| `ok` | no finding of level `error`; the exit code is 0 exactly when it is `true` |
| `findings` | every finding, warnings and `skipped` ones included: errors first, then by `where`, `message`, as in the text output; `where` is `""` for the whole item |
| `rendered_dir` | absolute path of the item's `rendered/` directory, the plan's `cd` target |
| `lines` | exactly the lines the text output prints, in order, header comments and `cd` included (text = the lines joined by newlines); a line with an unsafe character is refused, never escaped |

**Stability.** `format` is versioned on its own, independent of the spec `api_version`s.
Adding a key to these objects (as `outputs` was; references and conformance fixtures
come later) keeps `format` unchanged: consumers ignore keys they do not know. Removing or renaming a
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

Cross-spec references (`REFS`, `ctx.refs`), `prov graph` and `prov build` (artefacts under
`build/`) come in later releases.

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
