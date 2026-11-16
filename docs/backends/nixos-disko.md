# Backend `nixos-disko` (kind `os-install`)

Renders a pinned NixOS flake that holds both the target system and a custom installer ISO.
A human builds the ISO, writes it to a USB stick, boots the bare-metal machine from it and
runs one confirmed command that partitions the spec disk with
[disko](https://github.com/nix-community/disko) and installs offline. prov never builds,
writes or installs anything.

> **Static validation only — not yet installed on hardware.** prov checks the rendered
> files structurally and with `nix-instantiate --parse`; the example flake has also been
> locked and evaluated (`nix eval` of the target and ISO derivations, not built) in the
> official `nixos/nix` container. No machine has been installed from these files yet. Test
> in a throwaway VM or a spare disk first.

## Spec

The os-install core keys ([kinds.md](../kinds.md)) plus a `[nixos-disko]` table:

```toml
api_version = "prov/v1alpha1"
kind = "os-install"
backend = "nixos-disko"
hostname = "demo"
# ... domain, arch, firmware, timezone, locale, keyboard, [[users]], [network] ...

[storage]
disks = ["/dev/disk/by-id/nvme-EXAMPLE_SSD_SERIAL_0000000000"]   # exactly one; it is wiped

[nixos-disko]
release = "nixos-26.05"      # nixpkgs branch the flake follows
state_version = "26.05"      # system.stateVersion: the release first installed; never change it
layout = "ext4"              # the only layout so far
swap_size = "8G"             # optional swap partition
packages = ["git", "vim"]    # nixpkgs attribute names
cpu_vendor = "intel"         # optional: intel | amd, enables microcode updates
```

| Key | Type (default) | Notes |
|---|---|---|
| `release` | `nixos-YY.MM`, required | `inputs.nixpkgs.url = "github:NixOS/nixpkgs/<release>"` |
| `state_version` | `YY.MM`, required | warning when newer than `release` |
| `layout` | `ext4` (`ext4`) | btrfs, ZFS and LUKS are follow-ups |
| `swap_size` | `^[1-9][0-9]*[MG]$`, optional | no swap partition when absent |
| `packages` | list of attribute paths (`[]`) | `pkgs.<attr>` in `environment.systemPackages`; e.g. `python3Packages.requests` |
| `cpu_vendor` | `intel` \| `amd`, optional | `hardware.cpu.<vendor>.updateMicrocode = true` |

disko is pinned to the release tag `v1.13.0` (`DISKO_REF` in the backend), with
`disko.inputs.nixpkgs.follows = "nixpkgs"`. `flake.lock` pins the exact commits.

How the core keys map:

| Core key | NixOS |
|---|---|
| `hostname`, `domain` | `networking.hostName`, `networking.domain`; the flake outputs are `nixosConfigurations.<hostname>` and `<hostname>-installer` |
| `arch` | `nixpkgs.hostPlatform`: `x86_64-linux` / `aarch64-linux` |
| `firmware` | UEFI: systemd-boot (`canTouchEfiVariables = true`, the install writes an EFI boot entry). BIOS: GRUB, installed to the disk through disko's EF02 partition |
| `timezone`, `locale`, `keyboard` | `time.timeZone`, `i18n.defaultLocale`, `console.keyMap` |
| `users` | `users.users.<name>`: `isNormalUser`, `description` (gecos), `extraGroups` (groups, plus `wheel` when `sudo`), `openssh.authorizedKeys.keys`, `hashedPassword` |
| `users[].sudo` | wheel plus a passwordless `security.sudo.extraRules` entry |
| `users[].shell` | `/bin/bash` (default) → NixOS default; `/bin/zsh`, `/bin/fish` (also under `/usr/bin/`, `/run/current-system/sw/bin/`) → `programs.<shell>.enable` and `shell = pkgs.<shell>`; anything else is an error |
| `network` | systemd-networkd: one `systemd.network.networks."10-<name>"` per interface (DHCP, addresses, gateways, MTU), `systemd.network.links."10-<name>"` renames a NIC by `macaddress`; `networking.nameservers` / `networking.search` |
| `storage.disks` | `disko.devices.disk.main.device` |

Always set: openssh enabled with password and keyboard-interactive authentication and root
login off; `nix.settings.experimental-features = [ "nix-command" "flakes" ]`.

Checks: layout `ext4` needs exactly one disk; a disk path outside `/dev/disk/by-id/` is a
warning (kernel names like `/dev/sda` can change between boots); arm64 needs UEFI; some user
must have an ssh key or a password hash; package names may not be Nix keywords.

Groups are passed through as given: NixOS does not create groups such as `adm` or `sudo`
that Ubuntu has. Users stay mutable (`users.mutableUsers` keeps its default), so a password
set with `passwd` survives rebuilds.

## Rendered files

```text
<root>/os-install/<name>/spec.toml
<root>/os-install/<name>/hardware-configuration.nix   # optional, yours; never touched by prov
<root>/os-install/<name>/rendered/flake.nix
<root>/os-install/<name>/rendered/configuration.nix
<root>/os-install/<name>/rendered/disko.nix
<root>/os-install/<name>/rendered/installer.nix
<root>/os-install/<name>/rendered/flake.lock          # yours: created by nix flake lock, kept by render
```

- **`configuration.nix`** — the target. It imports `not-detected.nix` and a broad initrd
  module set (`nvme ahci xhci_pci usb_storage sd_mod usbhid`) so it boots on most hardware
  without a hardware file.
- **`disko.nix`** — GPT on the spec disk: a 512M ESP (vfat, `/boot`, `umask=0077`) on UEFI or
  a 1M EF02 BIOS boot partition on BIOS, an optional swap partition, and an ext4 root taking
  the rest.
- **`installer.nix`** — `installation-cd-minimal.nix` plus an `install-<hostname>` command.
  The ISO carries the target's closure, the build inputs disko-install needs, and the flake
  with its inputs, following disko's
  [offline installer pattern](https://github.com/nix-community/disko/blob/v1.13.0/docs/disko-install.md#using-disko-install-in-an-offline-installer),
  so the install needs no network. Nothing runs at boot.
- **`flake.lock`** is not rendered. `render` keeps a `rendered/flake.lock` you create (the
  backend declares it in `PRESERVE`), and `diff`/`validate` do not call it stale; `validate`
  checks that it is JSON.

### The hardware file and git

`configuration.nix` imports `../hardware-configuration.nix` (next to `spec.toml`, outside
`rendered/`) when `builtins.pathExists` finds it. **This works only when the item is in a
git repository and the file is `git add`ed:** Nix copies only the git-tracked files of a
flake's repository into the store, so an untracked hardware file (or an untracked
`flake.lock`) is invisible to Nix. The same applies to every rendered file.

disko-install copies only the flake directory, not the whole repository. The installer
therefore builds its own copy of the flake with `hardware-configuration.nix` inside it, and
`configuration.nix` checks `./hardware-configuration.nix` first; both evaluate to the same
system (checked with `nix eval` for the example).

## USB walkthrough

`prov plan os-install/<name>` prints these steps; it runs none of them.

1. **Optional, recommended: capture the hardware configuration.** Boot the stock NixOS
   minimal ISO (from nixos.org) on the target and run
   `nixos-generate-config --no-filesystems --show-hardware-config > hardware-configuration.nix`.
   Copy the file next to `spec.toml` and `git add` it.
2. **Lock.** In `rendered/`: `nix flake lock`, then `git add flake.lock` and commit.
3. **Build the ISO.** `nix build .#nixosConfigurations.<hostname>-installer.config.system.build.isoImage`;
   the image is in `result/iso/`. It is large (several GB): it holds the target closure and
   the build inputs for an offline install.
4. **Write the USB stick. This wipes the stick.** Find the stick with
   `ls -l /dev/disk/by-id/ | grep usb`, then
   `sudo dd if=result/iso/<ISO-FILE> of=/dev/disk/by-id/usb-<YOUR-STICK> bs=4M conv=fsync status=progress`.
   A GUI writer (GNOME Disks, balenaEtcher, Rufus in DD mode) is safer than dd.
5. **Install. This wipes the spec disk.** Boot the target from the stick (UEFI or legacy
   BIOS mode, matching `firmware`), then run `sudo install-<hostname>`. It checks that the
   spec disk exists, shows it with `lsblk` (model, serial, size, partitions), and asks you to
   type the hostname; any other answer aborts without changes. Then it runs
   `disko-install --flake <flake>#<hostname> --disk main <disk>` (plus
   `--write-efi-boot-entries` on UEFI), which partitions, formats and installs.
6. **Post-install checks.** Remove the stick, reboot, log in with ssh, and check
   `nixos-version`, `systemctl --failed`, `findmnt / /boot`, `swapon --show`, `ip -br addr`.

## Building the ISO from WSL2

Install nix in the WSL2 distribution (the multi-user installer from nixos.org, with systemd
enabled in `/etc/wsl.conf`, or the Determinate Systems installer) and enable flakes:

```sh
mkdir -p ~/.config/nix && echo 'experimental-features = nix-command flakes' >> ~/.config/nix/nix.conf
cd <hosts-repo>/os-install/<name>/rendered
nix flake lock && git add flake.lock
nix build .#nixosConfigurations.<hostname>-installer.config.system.build.isoImage
cp result/iso/*.iso /mnt/c/Users/<you>/Downloads/
```

Keep the hosts repository on the Linux file system (`~/...`), not under `/mnt/c`: Nix and
git are much slower there. WSL2 cannot write raw USB devices without extra tooling, so copy
the ISO to Windows and write the stick with Rufus (DD mode) or balenaEtcher. An amd64 ISO
builds natively on an x86_64 machine; an arm64 ISO needs an aarch64 builder or binfmt
emulation.

## Not covered (follow-ups)

Building the ISO or writing the USB from prov, unattended wipe-on-boot installers,
nixos-anywhere / install over ssh, btrfs / ZFS / LUKS layouts, home-manager, non-flake
output.
