# provision-hub

Render, validate and plan reusable provisioning artefacts (OS installs, OS images, VMs, Kubernetes distributions) from versioned kind + backend specs

## Overview

- Kind: cli
- Languages: python (uv)
- Notes: prov CLI: versioned spec.toml per item (`<kind>/<name>`), kind and backend plugins (render/validate/plan), never applies; synthetic examples only (RFC 5737/3849 IPs, example.com); Python 3.10+ with uv.

`prov` turns one versioned `spec.toml` per item into the artefacts its backend produces,
checks them, and prints the steps a human runs to apply them. It never applies anything: no
disk writes outside the item's `rendered/` directory, no ssh, no VM creation, no network
calls.

provision-hub is generic across domains — OS installs, OS images, VM setups, Kubernetes
distributions — and champions no backend: a **kind** fixes what an item is (its core keys),
any number of **backends** render it. The first wave is OS installs (`os-install`, shipped)
and OS images (`image`, planned). See [docs/kinds.md](docs/kinds.md).

| Kind | Backend | Artefacts | Notes |
|---|---|---|---|
| `os-install` | `ubuntu-autoinstall` (22.04, 24.04) | NoCloud seed: autoinstall `user-data`, `meta-data`, `kernel-cmdline` | storage layouts `lvm`, `zfs`, `mdraid` |
| `os-install` | `nixos-disko` (nixpkgs `nixos-YY.MM`) | pinned flake: `flake.nix`, `configuration.nix`, `disko.nix`, `installer.nix` (offline USB installer ISO) | layouts `ext4`, `ext4-luks` (LVM on LUKS); systemd-networkd or NetworkManager, optional GNOME desktop; see [docs/backends/nixos-disko.md](docs/backends/nixos-disko.md) |
| `os-install` | `proxmox-answer` (Proxmox VE ISO 8.4-1 or later) | `answer.toml` for the automated installer (`prepare-iso --fetch-from iso`) | root only (sudo users' keys), one NIC, `ext4`/`xfs` on one disk or `zfs`/`btrfs` raid; see [docs/backends/proxmox-answer.md](docs/backends/proxmox-answer.md) |

New backends follow [docs/adding-a-backend.md](docs/adding-a-backend.md); new kinds are
plugins too, dotted for third parties (`registry.example.org`,
[docs/kinds.md](docs/kinds.md#kind-plugins)). Same spec and pinned inputs, same rendered
bytes: [docs/determinism.md](docs/determinism.md).

## Usage

```sh
uv sync
uv run prov kinds                                             # os-install ...
uv run prov backends --kind os-install                        # nixos-disko, ubuntu-autoinstall ...
uv run prov init os-install/demo --backend ubuntu-autoinstall # writes os-install/demo/spec.toml (synthetic values)
uv run prov render os-install/demo                            # writes os-install/demo/rendered/
uv run prov validate os-install/demo                          # checks spec.toml and the rendered files
uv run prov diff os-install/demo                              # fresh render vs. rendered/ on disk
uv run prov plan os-install/demo                              # prints the seed-ISO and boot steps; runs nothing
uv run prov list                                              # every item under the root
uv run prov validate os-install/demo --json                   # one JSON document (kinds, backends, schema, validate, plan, conformance)
uv run prov conformance <backend>                             # render the kind's fixtures through a backend; static checks only
uv run prov conformance --export os-install out/              # write out/os-install.v1alpha1.fixtures.json (fixtures, tables, composed specs)
uv run prov digest os-install/demo                            # sha256 identity of rendered/ (also in the plan header)
uv run prov status --state-dir ../state                       # lifecycle and sync of every item from host-written state records
```

| Command | Does | Exit codes |
|---|---|---|
| `prov kinds [--json]` | list the installed kinds (plugins) and their api versions | 0, 2 (broken kind plugin) |
| `prov backends [--kind K] [--json]` | list registered backends (name, kind) | 0, 2 |
| `prov list` | list every item under the root (ref, backend); findings on stderr | 0 (also with warnings, e.g. a dotted directory whose kind is not installed), 1 findings (symlinked, unreadable or invalidly named entries), 2 missing root |
| `prov schema <kind> [--backend B] [--json]` | print the JSON Schema of a kind (`--json` changes nothing) | 0, 2 |
| `prov init <ref> --backend B [--force]` | write a starter `<kind>/<name>/spec.toml` | 0, 2 |
| `prov render <ref>` | make `<kind>/<name>/rendered/` hold exactly the rendered files | 0, 2 |
| `prov validate <ref> [--json]` | envelope, schema and semantic checks, rendered-file checks, staleness | 0, 1 findings, 2 |
| `prov diff <ref>` | unified diff from `rendered/` to a fresh render | 0, 1 diff, 2 |
| `prov plan <ref> [--json]` | print the apply steps for a human (the header carries the render digest) | 0, 2 |
| `prov digest <ref> [--json]` | print the `sha256:` digest of `rendered/` ([docs/state.md](docs/state.md)) | 0, 2 not rendered or unsafe entries |
| `prov status [ref ...] [--state-dir DIR] [--stale-after D] [--now T] [--json]` | lifecycle (planned, active, stale, decommissioned) and sync (in-sync, pending, drifted, unknown) of items, from `spec.toml`, `rendered/` and host-written state records; reads local files only ([docs/state.md](docs/state.md)) | 0, 1 invalid spec or record, 2 |
| `prov conformance <backend> [--json]` | render each conformance fixture of the backend's kind through it and check the result statically (spec, outputs, deterministic render, clean rewrite, `validate`, expected strings, plan refusal checks); writes only to a fresh temporary directory from `$TMPDIR` that it removes, never the item tree, and runs nothing | 0 all ok or skipped, 1 a fixture failed, 2 unknown backend, kind without fixtures or temporary directory left behind |
| `prov conformance --export <kind> DIR` | write `<kind>.<version>.fixtures.json` (the kind's fixtures, its installed backends' tables, unsupported reasons and composed specs) into the existing real directory DIR, outside `<root>/<kind>/`; atomic, never through a symlink | 0, 2 unknown kind, kind without fixtures, refused directory or target |

A ref is `<kind>/<name>`, e.g. `os-install/demo`. Exit code 2 means a usage or configuration
error (missing item, unknown kind or backend, bad envelope, invalid `spec.toml`).

`--json` prints one JSON document instead of the text (sorted keys, ASCII only, a
`"format": "prov.cli/v1alpha1"` key except for `schema`) with the same exit codes;
`validate --json` puts every finding in it and nothing on stderr, and an exit-2 error
stays text on stderr (`prov: error: …`, possibly with indented continuation lines) with
nothing on stdout. Shapes:
[docs/kinds.md](docs/kinds.md#machine-readable-output).

**Root.** `--root DIR`, else `$PROV_ROOT`, else the current directory. Layout:

```text
<root>/<kind>/<name>/spec.toml     # you write this (prov init gives a start)
<root>/<kind>/<name>/rendered/     # owned by prov render; stale files are removed (except files the
                                   # backend preserves, e.g. nixos-disko's flake.lock); commit it
<root>/<kind>/<name>/build/        # reserved for a later prov build; gitignored
```

**Specs.** Every `spec.toml` starts with the envelope `api_version = "prov/v1alpha1"`,
`kind` (equal to its directory) and `backend`; then the kind's core keys; then one table
named after the backend (`[ubuntu-autoinstall]`). A missing or unknown `api_version`, a
mismatched `kind`, an unknown backend or a legacy `schema_version` key is rejected. Each
kind's schema is published as JSON Schema, e.g.
[schema/os-install.v1alpha1.schema.json](schema/os-install.v1alpha1.schema.json) (editors
can use it through taplo/Even Better TOML). Secrets are never inline: identity is ssh-key
based and a user password, if any, is a crypt hash (`password_hash`). `v1alpha*` formats may
still break, always with a migration note in [CHANGELOG.md](CHANGELOG.md); from 1.0
`prov/v1` changes are additive only.

**Examples.** [examples/](examples/) is one root with an item per Ubuntu storage layout, a
NixOS item per layout (`demo-nixos-ext4`, `demo-nixos-gnome-luks`), one with a host-owned
`local.nix` and an extra flake input (`demo-nixos-local-module`), and their committed
rendered output:

```sh
uv run prov --root examples list
uv run prov --root examples diff os-install/demo-lvm
```

They use only documentation addresses (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24,
2001:db8::/32), reserved domains (example.com, example.org) and fake ssh keys.

**Status of the layouts.** Rendered artefacts are validated statically only: prov's own
checks plus `cloud-init schema` / `nix-instantiate --parse` where installed. No layout has been exercised by a real
install in a VM yet, ZFS least of all (it follows curtin's documented ZFS root example and
subiquity's guided ZFS datasets, on a single disk). Test in a throwaway VM before relying on
one.

**Safety.** `render` and `init` refuse to write through symlinks (`<kind>/`, the item
directory, `rendered/`, `build/`, files inside `rendered/`); symlinks found inside
`rendered/` are removed, never followed. `spec.toml` itself must be a regular file of at
most 1 MiB: symlinks, FIFOs and devices are refused. Written files get the umask's mode, or
keep the mode of the file they replace. Spec values may not contain control characters, and
`plan` shell-quotes every spec value it prints. Names prov did not write itself (files in
`rendered/`, entries under the root) are escaped when they hold an unprintable character,
and a file or directory in `rendered/` with such a name is reported and removed, never read.
All output is escaped at one boundary: any character that is not printable (controls, bidi
and zero-width marks, lone surrogates from non-UTF-8 names) is printed as `\xNN`, `\uNNNN`
or `\UNNNNNNNN` — in names, findings, errors and `prov diff` file contents alike (so `diff`
output is for reading, not a patch) — while `plan` refuses such lines instead, so what it
prints is exactly what you paste.

**External validators** are optional: `prov validate` runs `cloud-init schema` when
cloud-init is installed (ubuntu-autoinstall) and `nix-instantiate --parse` when nix is
installed (nixos-disko; parse only, never evaluates or fetches), and otherwise reports
`skipped: <tool> not installed`.

## Development

Install the git hooks once per clone:

```sh
pre-commit install
```

Then:

```sh
uv sync                               # Python 3.12 by default (.python-version)
uv run pytest -q
uv run --python 3.10 pytest -q        # the oldest supported Python
uv run prov conformance ubuntu-autoinstall   # os-install fixtures through a backend (static only)
PROV_UPDATE_GOLDENS=1 uv run pytest   # accept intended changes to goldens, schema and conformance files
```

Agents working on this repository: see [CLAUDE.md](CLAUDE.md).

## Contributing

Work on a branch and open a pull request against
`main`; direct pushes to it are blocked. Changes are squash-merged.
Tickets are GitHub Issues; mention them as `#N` in the PR body.

## License

MIT — see [LICENSE](LICENSE).
