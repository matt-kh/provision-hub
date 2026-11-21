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
```

| Command | Does | Exit codes |
|---|---|---|
| `prov kinds` | list the installed kinds (plugins) and their api versions | 0, 2 (broken kind plugin) |
| `prov backends [--kind K]` | list registered backends (name, kind) | 0, 2 |
| `prov list` | list every item under the root (ref, backend); findings on stderr | 0 (also with warnings, e.g. a dotted directory whose kind is not installed), 1 findings (symlinked, unreadable or invalidly named entries), 2 missing root |
| `prov schema <kind> [--backend B]` | print the JSON Schema of a kind | 0, 2 |
| `prov init <ref> --backend B [--force]` | write a starter `<kind>/<name>/spec.toml` | 0, 2 |
| `prov render <ref>` | make `<kind>/<name>/rendered/` hold exactly the rendered files | 0, 2 |
| `prov validate <ref>` | envelope, schema and semantic checks, rendered-file checks, staleness | 0, 1 findings, 2 |
| `prov diff <ref>` | unified diff from `rendered/` to a fresh render | 0, 1 diff, 2 |
| `prov plan <ref>` | print the apply steps for a human | 0, 2 |

A ref is `<kind>/<name>`, e.g. `os-install/demo`. Exit code 2 means a usage or configuration
error (missing item, unknown kind or backend, bad envelope, invalid `spec.toml`).

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
PROV_UPDATE_GOLDENS=1 uv run pytest   # accept intended changes to goldens and the schema file
```

Agents working on this repository: see [CLAUDE.md](CLAUDE.md).

## Contributing

Work on a branch and open a pull request against
`main`; direct pushes to it are blocked. Changes are squash-merged.
Tickets are GitHub Issues; mention them as `#N` in the PR body.

## License

MIT — see [LICENSE](LICENSE).
