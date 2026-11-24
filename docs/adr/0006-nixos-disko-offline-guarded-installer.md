# 0006. nixos-disko: offline, guarded USB installer

## Status

accepted

Decided in: #7 (issue #6); the closure change in #13; the partition-label guard in #9
(CHANGELOG.md, Unreleased, "Security").

## Context

The `nixos-disko` backend installs NixOS on bare metal from one `spec.toml`. prov never
applies ([0001](0001-render-only-never-apply.md)), so a human builds the media and runs the
install. The install wipes a whole disk, and on bare metal the wrong disk (the USB stick,
a second data disk) is one mistyped path away. The installed system should also be exactly
the one that was reviewed and locked, not one resolved on the day of the install.

## Decision

`render` writes a pinned flake that holds both the target system and a custom installer
ISO. A human locks the flake, builds the ISO, writes it to a USB stick and runs one
confirmed command on the target:

- **Offline.** The ISO carries the target's closure, the inputs disko-install needs and a
  copy of the flake, following disko's documented offline installer pattern. Building the
  ISO fails unless `rendered/flake.lock` is git-tracked; without a lock, disko-install
  would try to lock the inputs on the offline stick after the wipe was confirmed.
- **Nothing runs at boot.** The human runs `install-<hostname>`.
- **Guarded, fail closed.** The command refuses, changing nothing, unless stdin is a
  terminal and the spec disk is a whole disk with nothing mounted or used as swap that the
  boot medium does not sit on. It refuses while another disk carries a `disk-main-*`
  partition label, and with `ext4-luks` while a volume group `pool` or an open
  `/dev/mapper/crypted` exists. A failing probe refuses too.
- **Typed confirmation.** It shows the disk and asks for the last 4 characters of the
  disk's serial (the hostname when the disk reports none), then runs `disko-install`.

## Alternatives considered

- **Typing the hostname to confirm** (issue #6's acceptance criterion) — replaced in #7 by
  the disk serial, so the human has to look at the disk being wiped. The hostname stays the
  fallback.
- **Carrying the target's build closure on the ISO** (`toplevel.drvPath`, taken from
  disko's install test) — dropped in #13: it put every compiler and source needed to build
  the system on the ISO, while disko-install yields the same system and disk-script
  derivations from the outputs alone.
- **Unattended wipe-on-boot installers and nixos-anywhere / install over ssh** — listed as
  follow-ups, not built.

## Consequences

- The install needs no network, and the installed system is the locked one.
- The ISO is large (several GB; not measured).
- `validate` requires the guard lines in every rendered `installer.nix`; the install
  script's refusal paths are tested with stubbed `lsblk`, `findmnt`, `vgs` and
  `cryptsetup`, never against real devices.
- Static validation only: the examples were locked and `nix eval`-ed only, never built or
  installed. The docs say so until an install has been done.
- Later changes to an installed host need no new ISO: `nixos-rebuild switch --flake` from
  a checkout (`prov plan` step 7).

The rules: [backends/nixos-disko.md](../backends/nixos-disko.md) ("Rendered files", "USB
walkthrough", "Partition labels on other disks").
