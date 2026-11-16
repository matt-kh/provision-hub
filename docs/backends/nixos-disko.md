# Backend `nixos-disko` (kind `os-install`)

Renders a pinned NixOS flake that holds both the target system and a custom installer ISO.
A human builds the ISO, writes it to a USB stick, boots the bare-metal machine from it and
runs one confirmed command that partitions the spec disk with
[disko](https://github.com/nix-community/disko) and installs offline. prov never builds,
writes or installs anything.

> **Static validation only — not yet installed on hardware.** prov checks the rendered
> files structurally and with `nix-instantiate --parse`. The example flake has also been
> locked and evaluated (`nix eval`, never built) in the official `nixos/nix` container for
> UEFI and BIOS, with and without a hardware file: the target's toplevel and the ISO
> derivations evaluate, and the target evaluated from the installer's offline flake copy
> (directly and through disko-install's own `install-cli.nix`, with Nix 2.35 and with the
> nixpkgs Nix the ISO ships) gives the same toplevel derivation as the one on the ISO. The
> `install-<hostname>` script passes shellcheck (from the pinned nixpkgs), and its refusal
> paths are tested with stubbed `lsblk`/`findmnt`, never against real devices. No machine
> has been installed from these files yet. Test in a throwaway VM or on a spare
> disk first.

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
| `timezone`, `locale` | `time.timeZone`, `i18n.defaultLocale` |
| `keyboard` | an XKB layout (as for ubuntu-autoinstall): `services.xserver.xkb.layout`, with `console.useXkbConfig = true` so the console keymap follows it; X itself stays disabled |
| `users` | `users.users.<name>`: `isNormalUser`, `description` (gecos), `extraGroups` (groups, plus `wheel` when `sudo`), `openssh.authorizedKeys.keys`, `hashedPassword` |
| `users[].sudo` | wheel plus a passwordless `security.sudo.extraRules` entry |
| `users[].shell` | `/bin/bash` (default) → NixOS default; `/bin/zsh`, `/bin/fish` (also under `/usr/bin/`, `/run/current-system/sw/bin/`) → `programs.<shell>.enable` and `shell = pkgs.<shell>`; anything else is an error |
| `network` | systemd-networkd (see the note on `macaddress` below): one `systemd.network.networks."10-<name>"` per interface (DHCP, addresses, gateways, MTU), `systemd.network.links."10-<name>"` renames a NIC by `macaddress`; `networking.nameservers` / `networking.search`; `systemd.network.wait-online.anyInterface = true` so boot waits for one configured interface, not all (an unplugged second NIC would otherwise stall `network-online.target` until its timeout) |
| `storage.disks` | `disko.devices.disk.main.device` |

Always set: openssh enabled with password and keyboard-interactive authentication and root
login off; `nix.settings.experimental-features = [ "nix-command" "flakes" ]`.

Checks: layout `ext4` needs exactly one disk; a disk path outside `/dev/disk/by-id/` is a
warning (kernel names like `/dev/sda` can change between boots); arm64 needs UEFI; some user
must have an ssh key or a password hash; package names may not be Nix keywords; user names
may not be NixOS system accounts (`root`, `nobody`, `messagebus`, `sshd`, `nscd`,
`systemd-network`, `systemd-resolve`, `systemd-timesync`, `nixbld<N>`); a gecos may not
contain `:`.

On bare metal give every interface a `macaddress`: the `.link` file then names the NIC
exactly as in the spec. Without it the unit matches the kernel's predictable name (such as
`enp3s0`), which you must know in advance; an interface `name` that matches no NIC leaves
that NIC unconfigured. An `eth<N>` interface without `macaddress` is a warning, since NixOS
does not use `eth<N>` names by default. The starter uses a synthetic, locally administered
MAC (`02:00:00:00:00:01`): replace it with the NIC's real address.

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
  so the install needs no network. The closure reaches the ISO through disko's documented
  `environment.etc."install-closure"` pattern, which has the same effect as listing the
  paths in `isoImage.storeContents`. Nothing runs at boot. Building the ISO fails with
  "commit rendered/flake.lock ..." unless `rendered/flake.lock` is git-tracked: without a
  lock, disko-install would try to lock the inputs on the offline stick, after the wipe was
  confirmed.
- **`flake.lock`** is not rendered. `render` keeps a `rendered/flake.lock` you create (the
  backend declares it in `PRESERVE`), and `diff`/`validate` do not call it stale; `validate`
  checks that it is JSON.

### The hardware file and git

`configuration.nix` imports `../hardware-configuration.nix` (next to `spec.toml`, outside
`rendered/`) when `builtins.pathExists` finds it. **This works only when the item is in a
git repository and the file is `git add`ed:** Nix copies only the git-tracked files of a
flake's repository into the store, so an untracked hardware file (or an untracked
`flake.lock`) is invisible to Nix. The same applies to every rendered file.

disko-install copies only the flake directory into the store, not the whole repository
(it also drops a `?dir=` part of a flake reference), so on the stick `../` would be the
store directory itself. The installer therefore builds its own flat copy of the flake with
the hardware file inside it as `hardware-configuration.installer-copy.nix`;
`configuration.nix` imports that name first, and does not look at `../` when the flake sits
directly in the store. A `rendered/hardware-configuration.nix` is never imported (and the
next `prov render` deletes it): keep the file next to `spec.toml`.

## USB walkthrough

`prov plan os-install/<name>` prints these steps; it runs none of them.

1. **Optional, recommended: capture the hardware configuration.** On the target, booted
   from the stock NixOS minimal ISO (from nixos.org), run
   `nixos-generate-config --no-filesystems --show-hardware-config > hardware-configuration.nix`.
   Copy the file to your workstation next to `spec.toml` (not into `rendered/`) and
   `git add` it.
2. **Track and lock.** In `rendered/`: `git add . ../spec.toml` (Nix sees only tracked
   files), `nix flake lock`, then `git add flake.lock` and commit.
3. **Build the ISO.**
   `mkdir -p "$HOME"/prov-iso` and
   `nix build .#nixosConfigurations.<hostname>-installer.config.system.build.isoImage --out-link "$HOME"/prov-iso/<hostname>-installer`;
   the image is in `~/prov-iso/<hostname>-installer/iso/`. The out-link is the ISO's
   garbage-collection root, kept outside the hosts repository: a default `result` link in
   `rendered/` would make `validate`/`diff` fail and be deleted by the next `render`,
   after which `nix-collect-garbage` could delete the ISO. Remove the link when you no
   longer need the ISO. The ISO is large (several GB): it holds the target closure and the
   build inputs for an offline install.
4. **Write the USB stick. This wipes the stick.** Find the stick with
   `ls -l /dev/disk/by-id/ | grep usb`, then set the two variables and write:

   ```sh
   ISO=$(echo "$HOME"/prov-iso/<hostname>-installer/iso/*.iso)
   USB=/dev/disk/by-id/usb-CHANGE-ME
   if [ -b "$USB" ]; then sudo dd if="$ISO" of="$USB" bs=4M conv=fsync status=progress; else echo "USB=$USB is not a block device; set USB first" >&2; fi
   ```

   A GUI writer (GNOME Disks, balenaEtcher, Rufus in DD mode) is safer than dd.
5. **Install. This wipes the spec disk.** Boot the target from the stick (UEFI or legacy
   BIOS mode, matching `firmware`), then run `sudo install-<hostname>` on the console. It
   stops, changing nothing, unless all of these hold: stdin is a terminal; the spec disk
   exists; it is a whole disk (`lsblk -dno TYPE` is `disk`); nothing on it (partitions,
   dm, md or loop devices on top) is mounted or used as swap
   (`lsblk -nro MOUNTPOINTS` is empty); and it is none of the disks the boot medium
   mounted at `/iso` sits on (every `disk` in `lsblk -nrpso NAME,TYPE` of the `/iso`
   source, so a stick behind a partition or a dm device is caught too). It then shows the
   disk with `lsblk` (model, serial, size, partitions) and asks you to type the last 4
   characters of the disk's serial, or the hostname when the disk reports no serial. Any
   other answer aborts without changes. Then it runs
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
git add . ../spec.toml && nix flake lock && git add flake.lock && git commit -m 'Lock the flake inputs'
mkdir -p "$HOME"/prov-iso
nix build .#nixosConfigurations.<hostname>-installer.config.system.build.isoImage --out-link "$HOME"/prov-iso/<hostname>-installer
cp "$HOME"/prov-iso/<hostname>-installer/iso/*.iso /mnt/c/Users/<you>/Downloads/
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
