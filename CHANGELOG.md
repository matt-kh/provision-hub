# Changelog

All notable changes to provision-hub (`prov`) are recorded here. Formats marked `v1alpha*`
may change incompatibly; every such change has a Migration paragraph. See
[docs/kinds.md](docs/kinds.md) for the versioning policy.

## Unreleased

### Added

- Observed state ([docs/state.md](docs/state.md)), read-only and from local files only:
  - `prov digest <ref> [--json]` prints `sha256:<hex>`, the identity of `rendered/`: sha256
    over a `sha256sum` manifest of every regular file (`PRESERVE` files included, modes not),
    reproducible with coreutils. `prov plan` prints it in its header (`# render digest: …`;
    `render_digest` in `plan --json`, `null` when not rendered), so plans now have one more
    header line.
  - State records `<state-dir>/<kind>/<name>.json`, written by a host agent (never by prov),
    format `prov.state/v1alpha1`, published as `schema/prov.state.v1alpha1.schema.json`:
    `ref`, `last_seen`, `drift {status, detail}`, optional `applied_rev`, `render_digest`,
    `generation`, `facts`, `agent_version`. Strings may not hold control characters, as in
    `spec.toml`.
  - `prov status [ref ...] [--state-dir DIR] [--stale-after 72h] [--now T] [--json]`: per item
    a lifecycle (`planned`, `active`, `stale`, `decommissioned`, or `invalid`) and a sync
    state (`in-sync`, `pending`, `drifted`, `unknown`), plus a warning per record without an
    item. It never writes, runs or fetches anything. `--state-dir` defaults to
    `$PROV_STATE_DIR`.
  - The optional, kind-neutral envelope table `[state]` with `decommissioned = true`, in every
    kind's schema; renders never read it. `state` becomes a reserved name like `refs`: no
    kind property and no backend `NAME` may use it (no built-in one did; regenerate
    third-party schemas).

- State reporting ([docs/state.md](docs/state.md#reporting-state-statereport)): the optional
  `[state.report]` table (`repo`, `known_hosts`; `branch` main, `interval` 15m, `heartbeat`
  6h, `identity_file` /var/lib/prov-state/id_ed25519) asks the backend to render a
  report-only state agent. Core checks it for every kind: a backend renders it only when it
  declares the new optional contract member `STATE_AGENT = True` (others refuse the spec),
  `heartbeat` may not be shorter than `interval` (a warning at prov status's 72h stale
  threshold), and a `known_hosts` line must name the repository's host.
  - nixos-disko renders `prov-state.nix` (a hardened systemd service and timer) and
    `prov-state-agent.sh`, imported by `configuration.nix` and copied onto the installer
    stick. The system carries its render digest, computed at evaluation time with prov
    digest's manifest, so a host installed from the stick and one rebuilt from the checkout
    report the same value. The agent writes `render_digest`, `generation`, drift (`drifted`
    when the running system is not the system profile or is a rolled-back generation) and
    facts (hostname, kernel, NixOS version, pending reboot, addresses) over ssh with a
    pinned host key and a deploy key placed on the host (`prov plan` step 8). It never
    changes the system and pushes only on a change or after `heartbeat`. `applied_rev` is
    not reported yet.

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
  Kubernetes CRD style, `<label>.<group>` with a dotted group (`registry.example.org`, items
  under `<root>/registry.example.org/<name>/`), the group being their api group
  (`api_version = "example.org/v1alpha1"`), and a schema `$id` of `SCHEMA_URL` (an https
  base URL to which `<NAME>.<version>.schema.json` is appended) or
  `urn:prov:schema:<NAME>:<version>`; bare names are reserved for provision-hub. Kinds and backends declare `PLUGIN_API = 1` (required for kinds, optional
  for backends, default 1); a broken, misnamed, duplicate or wrong-API plugin exits 2 with one
  escaped line, as does a kind whose `semantic` raises. `prov list` warns, on stderr
  and with exit 0, about a dotted directory with items whose kind is not installed. Docs: "Kind plugins" in
  `docs/kinds.md`; `docs/adding-a-backend.md` gains `PLUGIN_API` and a vendor-prefix naming
  convention for third-party backends (`acme-talos`). os-install's schema is unchanged.
- `--json` on `prov kinds`, `backends`, `schema` (a no-op: the schema is JSON already),
  `validate` and `plan`: one JSON document on stdout, sorted keys, ASCII only, raw plugin
  values, a top-level `"format": "prov.cli/v1alpha1"` (not in `schema`'s output)
  (`kinds`: name, group, api_versions, summary, builtin; `backends`: name, kind, summary,
  builtin, starter, check, preserve; `validate`: ref, backend, ok, findings with level,
  where, message; `plan`: ref, backend, rendered_dir, lines identical to the text). Exit
  codes are unchanged; `validate --json` writes nothing on stderr; exit-2 errors stay text
  on stderr (`prov: error: …`, possibly with indented continuation lines) with empty
  stdout, and `plan` still refuses unsafe lines. Text output without `--json` is
  unchanged. Shapes and the stability promise: "Machine-readable output" in `docs/kinds.md`.
- Kind outputs: a kind may declare `OUTPUTS` (an object-schema fragment with
  `"additionalProperties": false`, under the backend `SCHEMA` rules) together with
  `outputs(spec)`, a pure, deterministic function of the tool-neutral part of the validated spec (`api_version`, `kind` and core keys, never the backend's name or table) that returns plain
  JSON data (exact types, 64-bit integers, no control characters in strings or keys) valid
  against `OUTPUTS`. Declaring only one, an `OUTPUTS` or `PROPERTIES` that is not plain JSON
  data, or an output name that looks like a secret value is refused at load; the same
  secret-name lint runs over every key of the result. The lint splits a name into words and
  flags `password`, `passwd`, `passphrase`, `pwd`, `secret(s)`, `credential(s)`,
  `kubeconfig`, `private key`, `api key`, `ssh key`, `tls key` and a final `token(s)` (not
  after `max`/`num`), unless the last word is `path`, `name`, `ref` or `file`; it is not
  part of `PLUGIN_API` and may grow between releases. JSON output refuses NaN and infinities.
  `prov validate` computes them (a raising or invalid `outputs` exits 2 naming the kind);
  `validate --json` adds `"outputs"` (`{}` without `OUTPUTS`, `null` while the spec is
  invalid) and `kinds --json` adds the `OUTPUTS` schema (`null` without); `format` stays
  `prov.cli/v1alpha1`, text output is unchanged. Post-apply facts are exported only as a
  path or secret-name reference the human writes in the spec, never a value; prov never
  reads live state. os-install declares no outputs. Docs: "Outputs" in `docs/kinds.md`.
- References: a kind may declare `REFS` (`{alias: {"kind", "required", "description"}}`,
  `required` defaulting to false and `description` to `""`; aliases match
  `^[a-z][a-z0-9_-]{0,62}$`; checked when the kind is loaded) and its specs then name targets in one optional envelope
  table, `[refs]` (`upstream = "registry.example.org/hub"`); the composed schema gains a
  `refs` object only for such kinds (one pattern per alias pinning the target kind, required
  aliases required), so `schema/os-install.v1alpha1.schema.json` is unchanged. `render`,
  `diff`, `plan` and `validate` resolve the references against the root (new
  `prov.refs`, `prov.spec.check_item` / `load_item`): only each target's `spec.toml` is
  read, without following symlinks (its `rendered/` and `build/` do not matter), checked
  in full with its own references, and its kind's outputs (`{}` for a kind without
  `OUTPUTS`) reach the backend as the read-only `ctx.refs[alias]` =
  `{"ref", "kind", "name", "outputs"}`. Missing, invalid or uninstalled targets,
  self-references, cycles and chains of more than 32 references are findings at
  `refs.<alias>` naming targets by ref (`validate` exits 1, the other verbs 2); a broken
  target plugin is exit 2 naming the target. Editing a target makes its
  consumers' `rendered/` out of date, by design. `kinds --json` adds `"refs"` (the kind's
  `REFS`, defaults filled in), `validate --json` adds `"refs"` (`{alias: {"ref", "kind",
  "outputs"}}`, `null` while the spec is invalid); `format` stays `prov.cli/v1alpha1`. Docs: "References" in `docs/kinds.md`.
- Conformance: a kind may declare `FIXTURES` (`{name: {"spec", "refs", "expect"}}`: core
  keys, literal outputs per reference alias, and `expect` with `valid` (default true),
  `outputs`, `contains`, `absent`; checked when the kind loads), and a backend `FIXTURES`
  (its table per fixture) and `UNSUPPORTED_FIXTURES` (fixture -> reason; both checked for
  shape only when the backend is discovered; a name the kind does not have is a `FAIL` of
  `table` in conformance, not a load error). New verb
  `prov conformance <backend> [--json]` (`prov.conformance`) composes each fixture into a
  spec with synthetic refs (`<target kind>/<alias>`) and checks, static and rendered only:
  literal refs outputs against the target kind's `OUTPUTS` (skipped when it is not
  installed), the spec (a negative fixture is composed with the backend's real table,
  like a valid one, and must be rejected by an error outside that table, containing the
  optional `expect.error`), outputs, two equal renders, a write to a temporary `rendered/`
  that diffs clean and rewrites with 0 changes, the backend's `validate`,
  `contains`/`absent`, and the plan's refusal checks. Lines
  `ok|FAIL|skipped<TAB><fixture><TAB><check><TAB><detail>` and a summary line with the
  number of skipped checks (a skipped check leaves its fixture ok); exit 0, 1 when a
  fixture failed, 2 for an unknown backend, a kind without fixtures or a temporary
  directory that could not be removed. `--json` statuses are `"ok"`, `"fail"`,
  `"skipped"`, plus `"skipped_checks"`; check names, the summary line and the keys are part
  of the stable format. It never runs a plan, a test runner or a subprocess, and writes
  only to a fresh temporary directory from `$TMPDIR` (never the item tree) that it removes,
  best effort; the directory's path never appears in the output. `kinds --json` adds
  `"fixtures"`, `backends --json` adds `"fixtures"` (sorted names) and
  `"unsupported_fixtures"` (`{name: reason}`); `format` stays `prov.cli/v1alpha1`. Docs: "Conformance fixtures" in `docs/kinds.md`, "Conformance" in
  `docs/adding-a-backend.md`.
- os-install conformance fixtures: `conf-uefi-static`, `conf-uefi-dhcp`, `conf-bios-dhcp`,
  `conf-two-disks` and the negative `conf-bad-gateway-family` and `conf-duplicate-user`
  (synthetic data, tool-neutral `contains`/`absent` strings, `expect.error` pinning the
  rule; firmware and the boot disk are not checked by content). `ubuntu-autoinstall` and
  `nixos-disko` give a table for each; nixos-disko skips `conf-two-disks` (one-disk
  layouts). Both pass `prov conformance` (static checks of rendered files only; nothing
  installed). Exported as generated, drift-tested
  `conformance/os-install.v1alpha1.fixtures.json` (format `prov.conformance/v1alpha1`:
  `kind`, `api_version`, `fixtures`, `backends` with `fixtures` tables, `unsupported`
  reasons and `specs`, the fully composed spec per supported fixture from the same
  composition path `prov conformance` checks), regenerated with `PROV_UPDATE_GOLDENS=1` and
  included in the sdist, so other implementations can run the same cases. New
  `prov conformance --export <kind> DIR` writes that file for any installed kind with
  fixtures and its installed backends into an existing real directory outside
  `<root>/<kind>/` (atomic, never through a symlink; exit 2 for an unknown kind or a kind
  without fixtures). Docs: "Exported fixtures" and the os-install fixtures in
  `docs/kinds.md`.

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
- Plugin contracts: `refs` is reserved as an envelope key (no kind property, no backend
  `NAME`), and a backend's `REFS`, reserved before, must now be empty. `Context.refs` is
  populated for items with references; `prov.spec.check_spec` / `load_spec` take an
  optional `root=` to resolve them (without it, `[refs]` is checked by the schema only and
  the backend's `check` is skipped for a spec that has references).

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

Kind plugins change the Python API, not the spec format: `prov.kinds.KINDS` is gone (use
`prov.kinds.discover()`, `get(name)` or `builtin()`), and `prov.kinds.core_keys(kind_name)`
now takes the kind whose properties a backend NAME may not use. Code that registered a kind
by adding it to `KINDS` registers it through the `prov.kinds` entry-point group instead.

`state` is now a reserved envelope name (the optional `[state]` table, [docs/state.md](docs/state.md)):
a third-party kind with a core property named `state`, or a backend named `state`, must
rename it. Plan output gained a header line (`# render digest: …`); scripts that count or
slice plan lines by position should skip `#` lines instead. Nothing else changes in
existing specs or renders.

`[state]` used to promise that no render reads it. `decommissioned` still changes nothing;
the new `[state.report]` does change the render and its digest (nixos-disko gains two
files). Specs without it render as before.
