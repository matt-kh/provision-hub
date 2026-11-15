# provision-hub

Render and validate install artefacts for Linux distros (Ubuntu, Proxmox VE, NixOS, Talos) from one host schema

## Overview

- Kind: cli
- Languages: python (uv)
- Notes: prov CLI: versioned host.toml schema, per-distro renderer plugins (render/validate/plan), never applies; synthetic examples only (RFC 5737/3849 IPs, example.com); Python 3.10+ with uv.

`prov` turns one versioned `host.toml` per machine into the install artefacts of its distro,
checks them, and prints the steps a human runs to apply them. It never applies anything: no
disk writes outside the host's `rendered/` directory, no ssh, no VM creation, no network
calls.

| Distro | Artefacts | Storage layouts |
|---|---|---|
| `ubuntu` (22.04, 24.04) | NoCloud seed: autoinstall `user-data`, `meta-data`, `kernel-cmdline` | `lvm`, `zfs`, `mdraid` |

Talos, NixOS and Proxmox VE follow through the same plugin contract
([docs/adding-a-distro.md](docs/adding-a-distro.md)).

## Usage

```sh
uv sync
uv run prov init ubuntu demo      # writes hosts/demo/host.toml with synthetic values
uv run prov render demo           # writes hosts/demo/rendered/
uv run prov validate demo         # checks host.toml and the rendered files
uv run prov diff demo             # fresh render vs. rendered/ on disk
uv run prov plan demo             # prints the seed-ISO and boot steps; runs nothing
```

| Command | Does | Exit codes |
|---|---|---|
| `prov distros` | list registered distro plugins | 0 |
| `prov schema` | print the host JSON Schema for the registered distros | 0 |
| `prov init <distro> <host> [--force]` | write a starter `hosts/<host>/host.toml` | 0, 2 |
| `prov render <host>` | make `hosts/<host>/rendered/` hold exactly the rendered files | 0, 2 |
| `prov validate <host>` | schema + semantic checks, rendered-file checks, staleness | 0, 1 findings, 2 |
| `prov diff <host>` | unified diff from `rendered/` to a fresh render | 0, 1 diff, 2 |
| `prov plan <host>` | print the apply steps for a human | 0, 2 |

Exit code 2 means a usage or configuration error (missing host, invalid `host.toml`).

**Root.** `--root DIR`, else `$PROV_ROOT`, else the current directory. Layout:

```text
<root>/hosts/<host>/host.toml      # you write this (prov init gives a start)
<root>/hosts/<host>/rendered/      # owned by prov render; stale files are removed
```

**Host schema.** Every `host.toml` starts with `schema_version = 1`; a missing or unknown
version is rejected. The schema is published as JSON Schema in
[schema/host.v1.schema.json](schema/host.v1.schema.json) (editors can use it through
taplo/Even Better TOML). Common keys: `hostname`, `domain`, `distro`, `arch`, `firmware`,
`timezone`, `locale`, `keyboard`, `[[users]]`, `[network]`, `[storage]`; each distro adds
its own table (`[ubuntu]`). Secrets are never inline: identity is ssh-key based and a user
password, if any, is a crypt hash (`password_hash`). Within schema version 1 changes are
additive only; anything else bumps the version.

**Examples.** Each directory under [examples/](examples/) is a root with one host and its
committed rendered output, for example:

```sh
uv run prov --root examples/ubuntu-lvm diff demo-lvm
```

They use only documentation addresses (192.0.2.0/24, 198.51.100.0/24, 203.0.113.0/24,
2001:db8::/32), reserved domains (example.com, example.org) and fake ssh keys.

**External validators** are optional: `prov validate` runs `cloud-init schema` when
cloud-init is installed and otherwise reports `skipped: cloud-init not installed`.

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
