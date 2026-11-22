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
| Core keys of the kind (`hostname`, `storage`, ...) | the kind | `src/prov/kinds/<kind>.py` |
| Backend table (`[ubuntu-autoinstall]`) | the backend | its module's `SCHEMA` |
| Shared definitions (`#/$defs/user`, `#/$defs/interface`) | prov core | `src/prov/kinds/common.py` |

Kinds are part of prov core and **not pluggable**: all backends of a kind read its core keys,
so a kind is a shared public format and changes to it are reviewed here. Backends are
plugins ([adding-a-backend.md](adding-a-backend.md)) and anyone can add one for an existing
kind without touching core code. provision-hub ships backends but champions none: the core
keys of a kind describe the item, not one tool's input format. Kinds and backends alike
render deterministically ([determinism.md](determinism.md)).

## Item directories

```text
<root>/<kind>/<name>/spec.toml     # you write this (prov init gives a start)
<root>/<kind>/<name>/rendered/     # owned by prov render; text only; committed
                                   # (a backend may name files it keeps there, e.g. flake.lock)
<root>/<kind>/<name>/build/        # reserved for a later `prov build`; gitignored
```

The kind directory must be a kind prov knows (`prov kinds`), the name one DNS label
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
| `NAME` | kind id, `[a-z][a-z0-9-]*`; the directory `<root>/<NAME>/` |
| `VERSIONS` | accepted versions, oldest first: `("v1alpha1",)` → `api_version = "prov/v1alpha1"` |
| `PROPERTIES` | JSON Schema of the core keys; may `$ref` `#/$defs/<name>` from `common.DEFS` |
| `REQUIRED` | required core keys |
| `semantic(spec)` | `list[Finding]` for rules JSON Schema cannot express |
| `REFS` | cross-spec references; reserved, empty for now |
| `NAME_KEY` | optional: the core key that should equal the item name (a mismatch is a warning) |

Core composes `envelope + PROPERTIES + every backend table` into one JSON Schema per kind
(`prov schema <kind>`); the published file is `schema/<kind>.<version>.schema.json`, and a
test keeps it equal to the code.

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

- `api_version` is `prov/<version>`; each kind lists the versions it accepts in `VERSIONS`.
- **`v1alpha*` may break.** While a kind is alpha, keys may be renamed, removed or tightened;
  every breaking change carries a migration note in [CHANGELOG.md](../CHANGELOG.md), and the
  version moves on (`v1alpha2`) when existing files stop validating.
- **1.0 freezes `prov/v1`.** From the 1.0 release, `prov/v1` changes are additive and
  optional only: a new optional key, a new enum value, a new backend. Anything breaking
  (removing or renaming a key, tightening a constraint, changing a meaning) becomes
  `prov/v2`, and prov keeps a reader for `prov/v1` files.
- Backend tables follow the same policy as the kind they belong to.
