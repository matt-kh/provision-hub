# Changelog

All notable changes to provision-hub (`prov`) are recorded here. Formats marked `v1alpha*`
may change incompatibly; every such change has a Migration paragraph. See
[docs/kinds.md](docs/kinds.md) for the versioning policy.

## Unreleased

### Added

- Docs: `docs/determinism.md`, the determinism principle: the same spec and pinned inputs
  render a byte-identical tree; specs and host-owned fragments (the nixos-disko `local.nix`
  pattern) are authored and reviewed, kinds and backends are reviewed code with goldens,
  and apply and behavioural tests stay outside prov.
- nixos-disko: `local_module = true` imports a hand-written `../local.nix` next to
  `spec.toml` (proprietary packages, services, `programs.*`, fonts), required once set and
  copied onto the installer stick like the hardware file; `[nixos-disko.inputs]` adds
  extra flake inputs (name to URL; a warning when a URL pins no tag or rev), passed with
  every other input to the target's modules as `inputs`. `prov plan` gains step 7, changing
  an installed host with `nixos-rebuild switch --flake`. Defaults leave rendered files
  unchanged. Example `examples/os-install/demo-nixos-local-module/` (locked and `nix
  eval`-ed only).
- Backend `nixos-disko` for `os-install`: renders a pinned NixOS flake (`flake.nix`,
  `configuration.nix`, `disko.nix`, `installer.nix`) with the target system and an offline
  USB installer ISO whose `install-<hostname>` command stops unless it runs on a terminal
  and the spec disk is a whole disk with nothing mounted or used as swap that the boot
  medium does not sit on, then asks for the disk serial's last 4 characters (or the
  hostname) before disko-install wipes the disk; building the ISO requires a git-tracked
  `rendered/flake.lock`. `keyboard` maps to the XKB layout (console follows it). Table
  `[nixos-disko]`: `release`, `state_version`, `layout` (`ext4`), `swap_size`, `packages`,
  `cpu_vendor`. `validate` runs `nix-instantiate --parse`
  when nix is installed; `plan` prints lock, ISO build, USB write and install steps. Static
  validation only: the example was locked and `nix eval`-ed, never installed. Example
  `examples/os-install/demo-nixos-ext4/`; docs in `docs/backends/nixos-disko.md`.
- nixos-disko: layout `ext4-luks` (UEFI only): an ESP and one LUKS partition (`crypted`,
  discards allowed) holding LVM volume group `pool` with an optional swap LV, an ext4 root
  LV of `root_size` and ext4 `volumes` (`name`, `mountpoint`, `size`, `mount_options`,
  `owner`; exactly one of them or the root is `100%FREE`, created last by disko's LV
  priority), LUKS2 pinned with `--type luks2`. No key or password file: disko asks for the
  passphrase twice at install time (the install script and `plan` warn first). The install
  script refuses while any volume group `pool` or open `/dev/mapper/crypted` exists:
  another disk's would be reused by disko, and a leftover on the target disk must be
  closed first (`vgchange -an pool`, then `cryptsetup close crypted`). New keys `esp_size` (default `512M`, at
  least 256M), `hibernate` (`resumeDevice` on the swap device; needs `swap_size`),
  `root_size` and `volumes`; an `owner` makes the mount point the user's through
  `systemd.tmpfiles.rules`. `validate` checks the LUKS/LVM structure and refuses rendered
  key files, password files or passwords. Example
  `examples/os-install/demo-nixos-gnome-luks/` (locked and `nix eval`-ed only, never built
  or installed). Existing specs render unchanged apart from the install
  script's partition-label guard (see Security).
- nixos-disko: `networking` (`networkd`, the default, rendered from `[network]`, or
  `networkmanager`: NetworkManager only, connections and Wi-Fi made on the machine with
  nmtui, nmcli or GNOME Settings, `[network]` refused, users with `sudo = true` also get
  the `networkmanager` group) and `desktop` (`none`, the default, or `gnome`: GNOME on Wayland
  with GDM through the NixOS 25.11+ options `services.displayManager.gdm` and
  `services.desktopManager.gnome`; needs `networkmanager` and `release` nixos-25.11 or
  newer). `validate` requires exactly one network stack, GDM and NetworkManager with GNOME,
  refuses the pre-25.11 `services.xserver.{desktop,display}Manager` names and any rendered
  network credential (`networking.wireless.networks` and its `psk`/`pskRaw`,
  `wireless.secretsFile`, `ensureProfiles`, also in nested attribute sets). `check` warns
  when GNOME is chosen but no user has a `password_hash`. `plan` adds the
  Secure Boot note (UEFI), a WSL2 note for writing the stick from Windows, the GNOME ISO
  size note and the `nmtui` / `nmcli device status` / `systemctl is-active display-manager`
  checks. The example `demo-nixos-gnome-luks` now uses both (no `[network]`); it was locked
  and `nix eval`-ed only, never built or installed. Existing specs render unchanged.
- Backend contract: optional `PRESERVE`, relative paths of human-created regular files in
  `rendered/` that `render` keeps and `diff`/`validate` do not report as stale (used for
  nixos-disko's `flake.lock`).
- Kind plugins: kinds register under the `prov.kinds` entry-point group exactly like
  backends under `prov.backends`; `os-install` is registered that way, and core imports no
  kind or backend module (`prov.kinds.builtin()` / `prov.registry.builtin()` are the
  entry points of the `provision-hub` distribution). Third-party kinds are dotted,
  `<label>.<dns-subdomain>` (`registry.example.org`, items under
  `<root>/registry.example.org/<name>/`), with the domain part as their api group
  (`api_version = "example.org/v1alpha1"`; the group `prov` stays reserved) and a schema
  `$id` of `SCHEMA_URL` or `urn:prov:schema:<NAME>:<version>`; bare names are reserved for
  provision-hub. Kinds and backends declare `PLUGIN_API = 1` (required for kinds, optional
  for backends, default 1); a broken, misnamed, duplicate or wrong-API plugin exits 2 with one
  escaped line, as does a kind whose `semantic` raises. `prov list` warns about a dotted
  directory with items whose kind is not installed. Docs: "Kind plugins" in
  `docs/kinds.md`; `docs/adding-a-backend.md` gains `PLUGIN_API` and a vendor-prefix naming
  convention for third-party backends (`acme-talos`). os-install's schema is unchanged.

### Security

- nixos-disko: `install-<hostname>` (every layout) refuses, before the serial prompt, while
  another disk carries a `disk-main-*` GPT partition label; disko addresses its partitions
  by these labels and skips formatting a partition that already has a file system, so a
  leftover label could capture `/boot`, `/` or the LUKS device at install and boot time.
  This changes the rendered `installer.nix` of existing nixos-disko items (golden
  `demo-nixos-ext4`): re-render them (`prov render`), since `validate` now requires the
  guard. `plan` now prints one `findmnt` line per mount point and warns about foreign
  `disk-main-*` labels before the install step.

### Changed

- nixos-disko: the installer ISO no longer carries the target system's build closure. The
  rendered `installer.nix` dropped `toplevel.drvPath` from its offline closure (taken from
  disko's install test, not its documented offline pattern); with it, `closureInfo` copied
  every compiler, source and `-dev` output needed to build the system onto the ISO (a GNOME
  dry run: 50 GiB more unpacked). disko-install re-evaluates the same flake with the same
  disk and yields the same system and disk-script derivations, so their outputs suffice.
  Re-render existing nixos-disko items (`installer.nix` changes).
- prov is generic over **kinds** and **backends**. Every item is
  `<root>/<kind>/<name>/spec.toml`; its ref `<kind>/<name>` is the CLI argument. The first
  kind is `os-install`; the Ubuntu renderer is its backend `ubuntu-autoinstall`.
- Specs start with the envelope `api_version = "prov/v1alpha1"`, `kind` and `backend`; the
  backend table is named after the backend (`[ubuntu-autoinstall]`).
- Backend plugins register under the `prov.backends` entry-point group with `NAME` and
  `KIND`; `render`, `plan` and `check` take `(spec, ctx)` where `ctx` is
  `prov.registry.Context(name, refs)`.
- CLI: new `prov kinds`, `prov backends [--kind K]`, `prov list`; `prov schema <kind>
  [--backend B]`; `prov init <ref> --backend B [--force]`; `render`, `validate`, `diff`,
  `plan` take a ref. `prov distros` is removed. Exit codes are unchanged.
- The published schema is `schema/os-install.v1alpha1.schema.json` (replaces
  `schema/host.v1.schema.json`).
- `<kind>/<name>/build/` is reserved for a later `prov build` and refused as a symlink.
- Output is escaped at one boundary (`prov.text`): names, findings, errors, tool output and
  `prov diff` contents print unprintable characters as `\xNN`/`\uNNNN`/`\UNNNNNNNN`;
  rendered/ entries with such names are reported and removed; plan lines holding one are
  refused (exit 2); `prov schema` output is ASCII-only JSON.
- Rendered output of the Ubuntu examples is byte-identical; they moved to
  `examples/os-install/demo-{lvm,mdraid,zfs}/`.
- os-install: `[network]` is optional (it was required). The kind still checks a present
  table (at least one interface, addresses, gateways, nameservers); backends that need it
  say so in `check`: `ubuntu-autoinstall` (netplan is rendered from it) and nixos-disko with
  `networking = "networkd"`.
- Backend contract: a backend `NAME` now collides only with the envelope keys and its own
  kind's core properties; another kind's properties no longer block it. `prov.kinds.KINDS` is
  gone: look kinds up with `prov.kinds.discover()` / `get()`, and register test kinds through
  entry points (the test suite's `extra_kinds` fixture).

### Known limitations

- Output escaping covers non-printable characters only. Printable but invisible or
  confusable characters (e.g. U+3164 HANGUL FILLER, combining marks) are printed as is.

### Migration

Move `hosts/<host>/host.toml` to `os-install/<host>/spec.toml` (and `hosts/<host>/rendered/`
to `os-install/<host>/rendered/`). In each file replace `schema_version = 1` and
`distro = "ubuntu"` with `api_version = "prov/v1alpha1"`, `kind = "os-install"` and
`backend = "ubuntu-autoinstall"`, and rename the tables `[ubuntu]` / `[ubuntu.storage]` to
`[ubuntu-autoinstall]` / `[ubuntu-autoinstall.storage]`; all other keys are unchanged. Run
`prov render os-install/<host>`: the rendered files do not change. Commands take refs now
(`prov render demo` → `prov render os-install/demo`, `prov init ubuntu demo` →
`prov init os-install/demo --backend ubuntu-autoinstall`), and `prov distros` is
`prov backends`. Third-party plugins move from the `prov.distros` entry-point group to
`prov.backends`, add `KIND = "os-install"`, and accept `ctx` as the second argument of
`render`, `plan` and `check`. A file that still has `schema_version` or `distro` (or an
`[ubuntu]` table and no `api_version`) fails with "predates prov/v1alpha1; see CHANGELOG
migration"; a half-migrated file that has `api_version` but still `distro` or
`schema_version` is told to remove that key.

`[network]` became optional in `os-install`. Existing specs are unaffected: every spec that
validated before still validates and renders the same files. Third-party `os-install`
backends must handle a missing `spec["network"]` in `render`, `plan` and `check`
(`"network" in spec`); a backend that needs it should report an error in `check`, which runs
before `render` and `plan`, as `ubuntu-autoinstall` does.
