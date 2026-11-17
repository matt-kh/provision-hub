# Changelog

All notable changes to provision-hub (`prov`) are recorded here. Formats marked `v1alpha*`
may change incompatibly; every such change has a Migration paragraph. See
[docs/kinds.md](docs/kinds.md) for the versioning policy.

## Unreleased

### Added

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
- Backend contract: optional `PRESERVE`, relative paths of human-created regular files in
  `rendered/` that `render` keeps and `diff`/`validate` do not report as stale (used for
  nixos-disko's `flake.lock`).

### Changed

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
