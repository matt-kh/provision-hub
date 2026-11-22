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
> `ext4-luks` example (`demo-nixos-gnome-luks`) was locked and evaluated the same way
> (toplevel and ISO derivations; the evaluated LUKS device, LVM volumes, mounts, resume
> device and the disko format script's `lvcreate` order are recorded under
> [Encrypted layout](#encrypted-layout-ext4-luks)); it has not been built, formatted or
> booted, so the passphrase prompts, the initrd unlock and resume from hibernation are
> unverified on hardware. The current `install-<hostname>` script text of both examples
> (`demo-nixos-ext4` and `demo-nixos-gnome-luks`, every guard included), taken from the
> evaluated ISO configuration, passes shellcheck 0.11.0 from the pinned nixpkgs, and its
> refusal paths are tested with stubbed `lsblk`/`findmnt`/`vgs`/`cryptsetup`, never against
> real devices. The NetworkManager and GNOME settings of `demo-nixos-gnome-luks` are
> evaluated only (the option values are recorded under
> [Desktop host](#desktop-host-wi-fi-and-gnome)); no connection, Wi-Fi join or GNOME
> session has been tried. No machine
> has been installed from these files yet. Test in a throwaway VM or on a spare
> disk first.

## Spec

The os-install core keys ([kinds.md](../kinds.md)) plus a `[nixos-disko]` table:

```toml
api_version = "prov/v1alpha1"
kind = "os-install"
backend = "nixos-disko"
hostname = "demo"
# ... domain, arch, firmware, timezone, locale, keyboard, [[users]], [network] (networkd) ...

[storage]
disks = ["/dev/disk/by-id/nvme-EXAMPLE_SSD_SERIAL_0000000000"]   # exactly one; it is wiped

[nixos-disko]
release = "nixos-26.05"      # nixpkgs branch the flake follows
state_version = "26.05"      # system.stateVersion: the release first installed; never change it
layout = "ext4"              # ext4 | ext4-luks
swap_size = "8G"             # optional swap partition (ext4) or logical volume (ext4-luks)
esp_size = "512M"            # optional, the default; UEFI only, at least 256M
hibernate = false            # optional: resume from swap (needs swap_size)
packages = ["git", "vim"]    # nixpkgs attribute names
cpu_vendor = "intel"         # optional: intel | amd, enables microcode updates
networking = "networkd"      # networkd (needs [network]) | networkmanager (no [network])
desktop = "none"             # none | gnome (needs networkmanager, release nixos-25.11+)
local_module = false         # optional: import the hand-written ../local.nix (below)

[nixos-disko.inputs]         # optional: extra flakes for local.nix, pinned to a tag or rev
tool = "github:example/tool/v1.2.3"
```

The encrypted layout adds the root size and extra volumes (see
[Encrypted layout](#encrypted-layout-ext4-luks) and the example
`examples/os-install/demo-nixos-gnome-luks/`):

```toml
[nixos-disko]
# ...
layout = "ext4-luks"
esp_size = "1G"
swap_size = "8G"             # at least the RAM size when hibernate = true
hibernate = true
root_size = "64G"            # default "100%FREE"

[[nixos-disko.volumes]]
name = "data"
mountpoint = "/srv/data"
size = "200G"
mount_options = ["noatime", "nodev", "nosuid", "noexec"]
owner = "alice"              # a [[users]] name

[[nixos-disko.volumes]]
name = "home"
mountpoint = "/home"
size = "100%FREE"            # exactly one of root_size and the volumes' size
```

| Key | Type (default) | Notes |
|---|---|---|
| `release` | `nixos-YY.MM`, required | `inputs.nixpkgs.url = "github:NixOS/nixpkgs/<release>"` |
| `state_version` | `YY.MM`, required | warning when newer than `release` |
| `layout` | `ext4` \| `ext4-luks` (`ext4`) | `ext4-luks` needs `firmware = "uefi"`; btrfs and ZFS are follow-ups |
| `swap_size` | `^[1-9][0-9]*[MG]$`, optional | a partition (`ext4`) or LV `swap` (`ext4-luks`); no swap when absent |
| `esp_size` | `^[1-9][0-9]*[MG]$` (`512M`) | ESP size on UEFI; less than `256M` is an error; any other value than the default on BIOS is an error (BIOS has no ESP) |
| `hibernate` | bool (`false`) | disko's `resumeDevice = true` on the swap device, so `boot.resumeDevice` (and `resume=`) point at it; needs `swap_size` (error otherwise), which should be at least the RAM size. On `ext4` the swap partition is unencrypted, so the hibernation image (all of RAM, keys and secrets included) is written to disk in clear: use `ext4-luks` to keep it encrypted |
| `root_size` | size or `100%FREE` (`100%FREE`) | `ext4-luks` only (error on `ext4`): size of LV `root`, at least `8G` |
| `volumes` | list (`[]`) | `ext4-luks` only (error on `ext4`): extra ext4 LVs, below |
| `packages` | list of attribute paths (`[]`) | `pkgs.<attr>` in `environment.systemPackages`; e.g. `python3Packages.requests` |
| `cpu_vendor` | `intel` \| `amd`, optional | `hardware.cpu.<vendor>.updateMicrocode = true` |
| `networking` | `networkd` \| `networkmanager` (`networkd`) | `networkd`: systemd-networkd from `[network]`, which is then required. `networkmanager`: `networking.networkmanager.enable = true` only; `[network]` is an error. See [Desktop host](#desktop-host-wi-fi-and-gnome) |
| `desktop` | `none` \| `gnome` (`none`) | `gnome`: `services.displayManager.gdm.enable` and `services.desktopManager.gnome.enable` (GNOME on Wayland); needs `networking = "networkmanager"` and `release` nixos-25.11 or newer |

disko is pinned to the release tag `v1.13.0` (`DISKO_REF` in the backend), with
`disko.inputs.nixpkgs.follows = "nixpkgs"`. `flake.lock` pins the exact commits.

How the core keys map:

| Core key | NixOS |
|---|---|
| `hostname`, `domain` | `networking.hostName`, `networking.domain`; the flake outputs are `nixosConfigurations.<hostname>` and `<hostname>-installer` |
| `arch` | `nixpkgs.hostPlatform`: `x86_64-linux` / `aarch64-linux` |
| `firmware` | UEFI: systemd-boot (`canTouchEfiVariables = true`, the install writes an EFI boot entry). BIOS: GRUB, installed to the disk through disko's EF02 partition. Both keep at most 10 generations in the boot menu (`configurationLimit = 10`), so the 512M ESP does not fill up |
| `timezone`, `locale` | `time.timeZone`, `i18n.defaultLocale` |
| `keyboard` | an XKB layout (as for ubuntu-autoinstall): `services.xserver.xkb.layout`, with `console.useXkbConfig = true` so the console keymap follows it; X itself stays disabled (with `desktop = "gnome"` too: GNOME runs on Wayland, and the same layout is written to `/etc/X11/xorg.conf.d/00-keyboard.conf`, the system default GDM and GNOME read) |
| `users` | `users.users.<name>`: `isNormalUser`, `description` (gecos), `extraGroups` (groups, plus `wheel` when `sudo`), `openssh.authorizedKeys.keys`, `hashedPassword` |
| `users[].sudo` | wheel plus a passwordless `security.sudo.extraRules` entry |
| `users[].sudo` with NetworkManager | with `networking = "networkmanager"`, a user with `sudo = true` also gets the `networkmanager` group (appended once, after the given groups and `wheel`); see [Desktop host](#desktop-host-wi-fi-and-gnome) |
| `users[].shell` | `/bin/bash` (default) → NixOS default; `/bin/zsh`, `/bin/fish` (also under `/usr/bin/`, `/run/current-system/sw/bin/`) → `programs.<shell>.enable` and `shell = pkgs.<shell>`; anything else is an error |
| `network` | `networking = "networkd"` only: systemd-networkd (see the note on `macaddress` below): one `systemd.network.networks."10-<name>"` per interface (DHCP, addresses, gateways, MTU), `systemd.network.links."10-<name>"` renames a NIC by `macaddress`; `networking.nameservers` / `networking.search`; `systemd.network.wait-online.anyInterface = true` so boot waits for one configured interface, not all (an unplugged second NIC would otherwise stall `network-online.target` until its timeout) |
| `storage.disks` | `disko.devices.disk.main.device` |

`volumes` entries (no other keys are allowed):

| Key | Type | Notes |
|---|---|---|
| `name` | `^[a-z][a-z0-9-]*$`, required | LV name; unique; not `root`, `swap` (prov's own LVs) and not starting with `snapshot` or `pvmove` (LVM rejects those) |
| `mountpoint` | absolute path of `A-Za-z0-9._/-`, required | normalised (no `//`, trailing `/`, `.` or `..`), unique, not `/`, and neither `/boot`, `/nix`, `/proc`, `/sys`, `/dev`, `/run`, `/var/run`, `/var/lock` nor anything below them |
| `size` | `^[1-9][0-9]*[MG]$` or `100%FREE`, required | exactly one of `root_size` and all volume sizes is `100%FREE` (none or several is an error) |
| `mount_options` | list of `^[A-Za-z0-9][A-Za-z0-9_.=:+-]*$` (`[]`) | one option per item (no commas); disko's `mountOptions`, i.e. `fileSystems.<mountpoint>.options` |
| `owner` | user name, optional | must be a `[[users]]` name: `systemd.tmpfiles.rules` gets `d <mountpoint> 0750 <owner> users -` (sorted by mount point), so the mounted volume's root belongs to that user. Not allowed on a shared system directory: `/home`, `/tmp`, `/srv`, `/opt`, `/mnt`, `/media` themselves, or `/var`, `/etc`, `/usr`, `/root`, `/bin`, `/lib`, `/lib64`, `/sbin` and anything below them (mount the volume below a shared directory instead, e.g. `/srv/data`). Not allowed on a declared user's home `/home/<name>` either: NixOS creates it and keeps it at mode 0700 (`homeMode`), which a 0750 tmpfiles rule would fight |

Always set: openssh enabled with password and keyboard-interactive authentication and root
login off; `nix.settings.experimental-features = [ "nix-command" "flakes" ]`.

Checks: `networking = "networkd"` needs `[network]`, `networkmanager` refuses it;
`desktop = "gnome"` needs `networkmanager` and `release` nixos-25.11 or newer, and warns
when no user has a `password_hash` (GDM logs in with a password); both layouts
need exactly one disk; `ext4-luks` needs UEFI; `hibernate` needs
`swap_size`; `esp_size` is at least 256M (and stays at its default on BIOS); `root_size` is
at least 8G; the `volumes` rules above; a disk path outside `/dev/disk/by-id/` is a
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

## Partition labels on other disks

Every layout. disko names the partitions by GPT
partition label (`disk-main-ESP`, `disk-main-root`, `disk-main-swap`, `disk-main-luks`,
`disk-main-boot`) and addresses them as `/dev/disk/by-partlabel/<label>` both in its
format/mount scripts and in the installed system's `fileSystems`, `swapDevices` and
`boot.initrd.luks.devices`. When another disk still carries such a label (a disk from an
earlier disko install), udev points the by-partlabel link at one of the two, so at install
time disko could mount (and, finding a file system there, not format) the other disk's
partition, and at every boot `/boot`, `/` or the LUKS device could resolve to the wrong
disk. `install-<hostname>` therefore refuses, before the serial prompt, when any partition
listed by `lsblk -nrpo NAME,PARTLABEL` has a `disk-main-*` label unless it positively sits
on the target disk: its `disk` and `loop` ancestors (`lsblk -nrpso NAME,TYPE`, as for the
boot medium) must all resolve to the target, and at least one must be found (a labelled
device with no disk or loop under it is refused too). A failing `lsblk` refuses as well. Labels on the target disk itself (a reinstall) are fine.
Remediation, by a human: unplug that disk, or wipe its partition table
(`wipefs -a` or `sgdisk --zap-all` on **that** disk, never the target). The labels on a
disk added after the install are not checked: keep disks from other disko installs out of
the machine, or wipe them. `validate` requires these guard lines in every rendered
`installer.nix`, before `exec disko-install`.

## Encrypted layout (`ext4-luks`)

disko's LUKS-on-LVM pattern (`example/luks-lvm.nix` at `v1.13.0`), UEFI only:

```text
<spec disk>  GPT
├─ ESP    esp_size   vfat  /boot  umask=0077    (unencrypted: systemd-boot, kernels, initrd)
└─ luks   100%       LUKS2 "crypted"  (--type luks2)  settings.allowDiscards = true
   └─ LVM PV -> volume group "pool"
      ├─ swap   swap_size   swap   (resumeDevice = true when hibernate)   optional
      ├─ root   root_size   ext4   /
      └─ <name> <size>      ext4   <mountpoint>  mountOptions             one per volume
```

**Passphrase.** No `passwordFile`, `keyFile` or `settings.keyFile` is rendered, and
`validate` reports any of them (and `additionalKeyFiles`, `initialPassword` or a plain
`password`) assigned as an attribute in a rendered file as an error: at the start of a line
or after `{` or `;`, with or without a dotted prefix (`settings = { keyFile = ...; }` too);
string literals and comments are ignored, so a gecos that mentions a password is not one. In
disko `v1.13.0` (`lib/types/luks.nix`) `askPassword` then defaults to true: its format step
prompts "Enter password for <device>" and "... again to be safe" with `read -s`, then runs
`cryptsetup luksFormat` with that passphrase. If the two entries differ, its `exit 1`
inside the prompt function ends the whole format script (the "please try again" loop
around it never runs again), and disko-install fails after the disk was already wiped and
partitioned: run `sudo install-<hostname>` again (the confirmation is asked again too).
prov pins the
format with `extraFormatArgs = [ "--type" "luks2" ]` (the option of `lib/types/luks.nix`,
passed to `luksFormat`). `allowDiscards` passes TRIM through to the SSD, which keeps it fast
but lets someone with the raw disk see which blocks are unused (file system type and
fill level), not their contents. disko-install runs
the format script in the foreground of `install-<hostname>`, which first prints "disko will
now ask for the new disk passphrase twice" (after the typed serial confirmation). The
passphrase is never written to a file, cannot be recovered, and is typed at every boot:
store it in a password manager before the install. disko also adds
`boot.initrd.luks.devices.crypted` (device `/dev/disk/by-partlabel/disk-main-luks`,
`allowDiscards = true`) and the LVM initrd modules (`dm-snapshot`); the evaluated format
script runs `cryptsetup luksFormat ... --type luks2`. `configuration.nix` does not repeat
them. It adds no keyboard drivers either: nixpkgs' default initrd modules
(`boot.initrd.includeDefaultModules`) already carry `usbhid`, `hid_generic` and the
common vendor HID drivers (checked with `nix eval` on a bare nixos-26.05 configuration).
It sets `console.earlySetup = true` so the console keymap is loaded before the prompt. On nixos-26.05 the
evaluated system uses the systemd initrd (`boot.initrd.systemd.enable = true`) with
`lvm2` and an `/etc/crypttab` in the initrd.

**Volume group and mapping already present.** disko's create steps are idempotent: in
`v1.13.0` `vgcreate` runs only when `vgdisplay pool` fails, `lvcreate` only when the LV is
missing, the LUKS container is opened only when `cryptsetup status crypted` fails, and
`mkfs` only when `blkid` finds no file system. A volume group `pool` or an open
`/dev/mapper/crypted` on another disk (a second NVMe from an earlier install, a stick)
would therefore be reused silently, and the install would land on that disk, possibly
unencrypted. For `ext4-luks` the `install-<hostname>` script refuses, before the typed
confirmation and changing nothing, when `vgs --noheadings -o vg_name` lists a `pool`, when
`vgs` itself fails, or when `/dev/mapper/crypted` exists or `cryptsetup status crypted`
answers 0, or when `cryptsetup status` answers anything but 0 or 4 ("cannot query": in
cryptsetup 2.8.6 an inactive name is `-ENODEV`, exit 4, while a missing device-mapper
gives 1; the refusal then suggests loading device-mapper with `modprobe dm_mod` and
retrying). `lvm2` and `cryptsetup` are in the script's `runtimeInputs`. Remediation, by a
human: a `pool` on **another** disk, unplug that disk or rename its group
(`vgrename pool <other-name>`); a leftover of an earlier attempt on the target disk,
`vgchange -an pool` **then** `cryptsetup close crypted`, in that order. Such a leftover
comes from a failure after the LUKS format, for example fixed sizes (swap, `root_size`,
volumes) that do not fit the disk: `lvcreate` fails with `crypted` open and `pool`
active. A passphrase mismatch is different: disko exits before `luksFormat`, nothing is
left open, and a rerun of `sudo install-<hostname>` is all it takes.
The names stay fixed (`pool`, `crypted`) rather than derived from the hostname: a host
reinstalled from a second disk would clash with its own old names anyway, a fixed name
keeps the post-install commands identical on every host, and the guard catches every clash
regardless of the name. `validate` requires these guard lines in a rendered `ext4-luks`
`installer.nix`, and an EF00 ESP (and no EF02) in its `disko.nix`.

**Logical volume order.** disko `v1.13.0` (`lib/types/lvm_vg.nix`) creates the LVs sorted by
an option `priority` ("smaller values are created first"), not by attribute name. Its
default is 1000, plus 251 when the size contains `100%` (the option's `defaultText` says
9001, but the code adds 251), so the one `100%FREE` LV gets 1251 and is created after
every fixed-size LV, which is what makes `100%FREE` mean "the space left". prov therefore
relies on the default and does not set `priority`; `check` allows exactly one `100%FREE`
LV. `100%FREE` is passed to `lvcreate -l 100%FREE` unchanged. Evaluated for the example:
priorities `data`/`root`/`swap` 1000, `home` 1251, and the rendered format script runs
`lvcreate` for `data`, `root`, `swap`, then `home`.

**Hibernate.** `hibernate = true` renders `resumeDevice = true` on the swap content, which
disko turns into `boot.resumeDevice` (evaluated: `/dev/pool/swap`, kernel parameter
`resume=/dev/pool/swap`). The swap LV sits inside LUKS, so the hibernation image is
encrypted. Swap should be at least the RAM size; `plan` says so and lists
`cat /sys/power/disk` and a one-off `systemctl hibernate` test after the install. On
`ext4` the same flag marks the swap partition, which is not encrypted: the hibernation
image, a copy of RAM including keys and secrets, lands on the disk in clear. Use
`ext4-luks` when that matters.

**Mounts.** Evaluated `fileSystems` for the example: `/` on `/dev/pool/root`, `/boot` on the
ESP (`umask=0077`), `/home` on `/dev/pool/home` (`defaults`), `/srv/data` on
`/dev/pool/data` with `noatime,nodev,nosuid,noexec`.

## Desktop host: Wi-Fi and GNOME

A laptop or desktop that joins networks interactively sets `networking = "networkmanager"`
and leaves `[network]` out (the example `demo-nixos-gnome-luks` does). `configuration.nix`
then enables NetworkManager and nothing else: no systemd-networkd, no `.link` files, no
`useNetworkd`. Connections, Wi-Fi included, are made on the machine after the first boot
with `nmtui` or `nmcli`, or in GNOME Settings. **No network credential is ever in
`spec.toml` or a rendered file**: there is no key for one, and `validate` refuses an
assignment to `networking.wireless.networks` (and so its `psk` / `pskRaw`),
`wireless.secretsFile` or NetworkManager `ensureProfiles` in any rendered file. It matches
attribute paths, nested sets included (`networking.wireless = { networks = ...; }`,
`networkmanager = { ensureProfiles = ...; }`), after blanking strings and comments, so a
user, volume or package named `psk` is not one. It is a scanner, not a Nix parser: a `with`
scope, a `let`/`rec` set inside an attribute value, and a set passed to a function
(`lib.mkIf c { ... }`, `lib.mkMerge [ ... ]`, `lib.mkDefault { ... }`) are not followed, so
`networking.wireless = lib.mkIf c { networks = ...; }` is not caught. `validate` also
requires exactly one of systemd-networkd (`networking.useNetworkd`, `systemd.network.*`;
the initrd's `boot.initrd.systemd.network` does not count) and NetworkManager in
`configuration.nix`.

**Who may change connections.** NetworkManager asks polkit. On nixos-26.05 (evaluated for
the example, NetworkManager 1.56.0): `security.polkit.adminIdentities` is
`[ "unix-group:wheel" ]`, so a `wheel` user is a polkit administrator. NetworkManager's own
policy lets the user of an active local session control devices and scan Wi-Fi without a
prompt (`network-control`, `wifi.scan`: `yes`), but adding or changing a system-wide
connection (`settings.modify.system`, what nmtui and GNOME Settings create by default) needs
administrator authentication (`auth_admin_keep`). So:

- in GNOME Settings a `wheel` user is asked for their own password (GNOME Shell is the
  polkit agent); a non-`wheel` user cannot add system-wide connections, but in an active
  local session can still add and change connections restricted to themselves
  (`settings.modify.own`: `yes`, e.g. `nmcli connection add ... connection.permissions
  user:<name>`);
- on the console nmtui has no polkit agent, so run `sudo nmtui` (or `nmcli --ask`, which can
  act as a polkit agent in the terminal; `nmcli agent polkit` starts one explicitly);
- users with `sudo = true` (passwordless sudo, root already) are put in the
  `networkmanager` group;
- any user can opt in with `groups = ["networkmanager"]`. Know what it grants: NixOS's
  rule for that group returns polkit `YES` for **every** `org.freedesktop.NetworkManager.*`
  (and ModemManager) action, with no check that the session is local or active, so it also
  applies over ssh: the member can change any connection, DNS and the hostname without a
  password.

`desktop = "gnome"` adds GNOME on Wayland with the GDM login screen, using the option names
of NixOS 25.11 and later (`services.displayManager.gdm`, `services.desktopManager.gnome`);
older releases are refused, and `validate` rejects the old `services.xserver.desktopManager` /
`services.xserver.displayManager` names and a GNOME without GDM or NetworkManager. prov sets
no audio, printing or X11 option: the GNOME module's own defaults apply (evaluated on
nixos-26.05: `services.pipewire.enable = true`, `services.xserver.enable = false`).

Password sudo on a desktop: GDM logs in with a password, so give the user a
`password_hash` (`mkpasswd -m sha-512`). For sudo that asks for that password instead of
the passwordless rule `sudo = true` renders, put the user in `wheel` directly:

```toml
[[users]]
name = "alice"
groups = ["wheel"]           # NixOS: wheel may sudo with its password
sudo = false                 # no NOPASSWD rule
password_hash = "$6$..."     # crypt hash, never the password
```

Before the install, **disable Secure Boot** in the firmware setup: neither the installer
stick nor systemd-boot is signed. The GNOME ISO carries the whole desktop closure, likely
several GB more than a server install (not measured: no ISO has been built): the build
takes longer and needs more store space; use a stick of 16 GB or more. From WSL2, which has no USB block devices, copy the ISO to
Windows (`cp -L "$ISO" /mnt/c/Users/<you>/Downloads/`) and write it with Rufus in DD image
mode or balenaEtcher.

After the install, join a network on the target console with `sudo nmtui` (with GNOME,
Ctrl+Alt+F3 for a text console, or use GNOME Settings after logging in), check it there with `nmcli device status`, and `systemctl is-active display-manager`
should print `active`.

Evaluated (locked and `nix eval`-ed in the `nixos/nix` container against nixpkgs
`nixos-26.05`, never built or booted) for the example: `networking.networkmanager.enable =
true`, `networking.useNetworkd = false`, `networking.useDHCP = false` (no `dhcpcd` or
`systemd-networkd` service defined, `NetworkManager` is), `services.displayManager.gdm.enable
= true`, `services.desktopManager.gnome.enable = true`, `services.xserver.enable = false`,
`users.users.alice.extraGroups = [ "wheel" ]` (password sudo, so no `networkmanager` group),
`security.polkit.adminIdentities = [ "unix-group:wheel" ]`, `security.sudo.wheelNeedsPassword
= true`, the `networkmanager` group polkit rule above in `security.polkit.extraConfig`, no
evaluation warnings; the LUKS device is unchanged.

## Rendered files

```text
<root>/os-install/<name>/spec.toml
<root>/os-install/<name>/hardware-configuration.nix   # optional, yours; never touched by prov
<root>/os-install/<name>/local.nix                    # yours when local_module = true; never touched by prov
<root>/os-install/<name>/rendered/flake.nix
<root>/os-install/<name>/rendered/configuration.nix
<root>/os-install/<name>/rendered/disko.nix
<root>/os-install/<name>/rendered/installer.nix
<root>/os-install/<name>/rendered/flake.lock          # yours: created by nix flake lock, kept by render
```

- **`configuration.nix`** — the target. It imports `not-detected.nix` and a broad initrd
  module set (`nvme ahci xhci_pci usb_storage sd_mod usbhid`) so it boots on most hardware
  without a hardware file.
- **`disko.nix`** — GPT on the spec disk: an `esp_size` (512M by default) ESP (vfat, `/boot`,
  `umask=0077`) on UEFI or a 1M EF02 BIOS boot partition on BIOS; then, for `ext4`, an
  optional swap partition and an ext4 root taking the rest, and for `ext4-luks` one LUKS
  partition taking the rest with the LVM volume group inside (below).
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
next `prov render` deletes it): keep the file next to `spec.toml`. Only that one file is
copied onto the stick, so it must be self-contained: no `imports` of other relative
files (the output of `nixos-generate-config` already is).

## Host-owned module and extra inputs

`spec.toml` covers what every host needs; the rest stays in NixOS's own language in a
hand-written module. With `local_module = true`, `configuration.nix` imports
`../local.nix` (next to `spec.toml`, outside `rendered/`; `render` never writes or deletes
it). Use it for what the spec cannot express: proprietary packages, services,
`programs.*`, fonts. The import is required, not optional: a missing or untracked file
stops the build with "local_module = true: create ../local.nix next to spec.toml and git
add it". The installer copies it onto the stick as `local.installer-copy.nix`, exactly
like the hardware file, so it must be self-contained too: no other relative paths.

`[nixos-disko.inputs]` adds flake inputs, name to flake URL, for tools that ship as their
own flake rather than in nixpkgs. Each keeps its own locked dependencies (no `follows`), so
it builds as its authors tested it; that may add another nixpkgs to the store. `nix flake
lock` pins every input, and a URL that names no tag or rev gets a warning, since `nix flake
update` would move it. Names are lowercase identifiers, not `nixpkgs`, `disko` or `self`.
When either key is set, the target system's modules get the flake inputs as the module
argument `inputs`, without `self`: on the installer stick `self` is a copy of the flake, so
a system that used it would differ from the one on the ISO and could not be built offline.
A URL counts as pinned when it names a version-like tag (`v1.2.3`, `2026-11-15`) or a
40-digit commit; a branch (`main`, `nixos-unstable`) gets the warning. Inputs without
`local_module = true` get a warning too: nothing could use them.

A `local.nix` (from `examples/os-install/demo-nixos-local-module/`):

```nix
{ config, lib, pkgs, inputs, ... }:
{
  # Proprietary packages, allowed one by one.
  nixpkgs.config.allowUnfreePredicate = pkg: builtins.elem (lib.getName pkg) [ "vscode" ];

  environment.systemPackages = [
    pkgs.vscode
    # A package from an extra flake input (spec.toml [nixos-disko.inputs]).
    inputs.nh.packages.${pkgs.stdenv.hostPlatform.system}.default
  ];

  # A service with its own group.
  virtualisation.docker.enable = true;
  users.users.alice.extraGroups = [ "docker" ];

  # zsh (the user's shell from spec.toml) with the powerlevel10k prompt and its font.
  programs.zsh.promptInit = "source ${pkgs.zsh-powerlevel10k}/share/zsh-powerlevel10k/powerlevel10k.zsh-theme";
  fonts.packages = [ pkgs.nerd-fonts.meslo-lg ];
}
```

Static validation only: this example was locked and `nix eval`-ed (system, installer ISO,
and the stick's flake copy evaluating to the same system derivation), never installed.

### Changing an installed host

No new ISO is needed after the install. Edit `spec.toml` or `local.nix`, `prov render`,
commit, then on the host, in a git checkout of the repository, from `rendered/`:
`git pull && nixos-rebuild switch --flake .#<hostname> --sudo` (`prov plan` step 7), as
your user: `--sudo` evaluates as you and activates as root, where Nix may refuse a git
checkout owned by another user. Each switch adds a boot menu entry to roll back to. Not yet
run on an installed host.

## USB walkthrough

`prov plan os-install/<name>` prints these steps; it runs none of them.

1. **Optional, recommended: capture the hardware configuration.** On the target, booted
   from the stock NixOS minimal ISO (from nixos.org), run
   `nixos-generate-config --no-filesystems --show-hardware-config > hardware-configuration.nix`.
   Copy the file to your workstation next to `spec.toml` (not into `rendered/`) and
   `git add` it.
2. **Track and lock.** In `rendered/`: `git add . ../spec.toml` (Nix sees only tracked
   files), `nix flake lock`, then `git add flake.lock` and commit. The plan's `nix` lines
   pass `--extra-experimental-features 'nix-command flakes'`, so they work without flakes
   enabled in `nix.conf`.
3. **Build the ISO.**
   `mkdir -p "$HOME"/prov-iso` and
   `nix build .#nixosConfigurations.<hostname>-installer.config.system.build.isoImage --out-link "$HOME"/prov-iso/<hostname>-installer`;
   the image is in `~/prov-iso/<hostname>-installer/iso/`. The out-link is the ISO's
   garbage-collection root, kept outside the hosts repository: a default `result` link in
   `rendered/` would make `validate`/`diff` fail and be deleted by the next `render`,
   after which `nix-collect-garbage` could delete the ISO. Remove the link when you no
   longer need the ISO. The ISO is large (several GB, more with `desktop = "gnome"`; not measured): it holds the target closure and the
   build inputs for an offline install.
4. **Write the USB stick. This wipes the stick.** Find the stick with
   `ls -l /dev/disk/by-id/ | grep usb`, then set the two variables and write:

   ```sh
   ISO=$(echo "$HOME"/prov-iso/<hostname>-installer/iso/*.iso)
   USB=/dev/disk/by-id/usb-CHANGE-ME
   case "$USB" in *-part[0-9]*) echo "USB=$USB is a partition; use the whole stick" >&2 ;; *) if [ -b "$USB" ]; then sudo dd if="$ISO" of="$USB" bs=4M conv=fsync status=progress; else echo "USB=$USB is not a block device; set USB first" >&2; fi ;; esac
   ```

   Unmount the stick's partitions first (desktops often automount them). The line refuses
   a by-id path of a partition (`…-part1`) and anything that is not a block device.

   A GUI writer (GNOME Disks, balenaEtcher, Rufus in DD mode) is safer than dd. From WSL2
   see [Building the ISO from WSL2](#building-the-iso-from-wsl2).
5. **Install. This wipes the spec disk.** On UEFI, disable Secure Boot first (nothing on
   the stick is signed). Boot the target from the stick (UEFI or legacy
   BIOS mode, matching `firmware`), then run `sudo install-<hostname>` on the console. It
   stops, changing nothing, unless all of these hold: stdin is a terminal; the spec disk
   exists; it is a whole disk (`lsblk -dno TYPE` is `disk`); nothing on it (partitions,
   dm, md or loop devices on top) is mounted or used as swap
   (`lsblk -nro MOUNTPOINTS` is empty); and it is none of the disks the boot medium
   mounted at `/iso` sits on (every `disk` in `lsblk -nrpso NAME,TYPE` of the `/iso`
   source, so a stick behind a partition or a dm device is caught too). The boot-medium
   comparison is best effort: when `/iso` has no block device (copytoram, netboot) it
   prints "no block device at /iso; the boot medium was not compared" and skips it, while
   the mounted-disk check and the typed serial still apply. If any of these reads fails
   (including `findmnt` on a mounted `/iso`), it refuses as well. It then shows the
   disk with `lsblk` (model, serial, size, partitions) and asks you to type the last 4
   characters of the disk's serial, or the hostname when the disk reports no serial. Any
   other answer aborts without changes. With `ext4-luks` it also refuses, before the
   confirmation, while any disk provides an LVM volume group `pool` or an open
   `/dev/mapper/crypted`. Every layout refuses while another disk carries a `disk-main-*`
partition label (see [Partition labels on other disks](#partition-labels-on-other-disks)). With
`ext4-luks` disko then asks twice for the new disk passphrase (see [Encrypted layout](#encrypted-layout-ext4-luks)). Then it runs
   `disko-install --flake <flake>#<hostname> --disk main <disk>` (plus
   `--write-efi-boot-entries` on UEFI), which partitions, formats and installs.
6. **Post-install checks.** Remove the stick, reboot (with `ext4-luks`, type the passphrase
   at the initrd prompt), with `networkmanager` join a network on the target console
   (`sudo nmtui`, then `nmcli device status`), log in with ssh, and check `nixos-version`, `systemctl --failed`,
   `findmnt /`, `findmnt /boot` (one `findmnt` per mount point, every volume included),
`swapon --show`, `ip -br addr`; with
   `ext4-luks` also `sudo cryptsetup status crypted` and `sudo lvs`; with `hibernate`,
   `cat /sys/power/disk` and one `systemctl hibernate` test; with `gnome`,
   `systemctl is-active display-manager`.

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

## Conformance

`uv run prov conformance nixos-disko` renders the os-install fixtures
([kinds.md](../kinds.md#os-install-v1alpha1)) with the table
`release = "nixos-26.05"`, `state_version = "26.05"`. `conf-two-disks` is skipped
(`UNSUPPORTED_FIXTURES`): the layouts install to exactly one disk. The other fixtures
render without a `flake.lock` (only the ISO build needs one; `validate` checks it only when
present), and `validate` reports `skipped: nix not installed` where nix is missing. Static
checks only: nothing is locked, evaluated, built or installed.

## Not covered (follow-ups)

Building the ISO or writing the USB from prov, unattended wipe-on-boot installers,
nixos-anywhere / install over ssh, btrfs / ZFS layouts, LUKS key files, TPM2 or FIDO2
unlocking, encrypted `/boot`, LUKS on BIOS, home-manager, non-flake output, `follows` for extra inputs.
