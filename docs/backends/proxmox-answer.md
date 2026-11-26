# Backend `proxmox-answer` (kind `os-install`)

Renders `answer.toml` for the Proxmox VE automated installer (ISO 8.4-1 or later). A human
checks it with `proxmox-auto-install-assistant validate-answer`, embeds it into a copy of
the Proxmox VE ISO with `prepare-iso`, writes that ISO to a USB stick and boots the target
from it. prov never prepares the ISO, writes media or installs anything.

> **Static validation only — not installed.** prov parses the rendered TOML, checks the
> sections and keys the installer requires, and runs
> `proxmox-auto-install-assistant validate-answer` when that tool is on `PATH` (otherwise it
> reports `skipped`). No answer file of this backend has been run through the installer.
> The **DEVLINKS disk filter** (below) is the least proven part: the installer's
> documentation does not say how globs are matched against udev's space-separated
> `DEVLINKS`, so check the match on the target before installing.

Reference: the Proxmox VE wiki page *Automated Installation* (answer file format, the
`proxmox-auto-install-assistant` tool, and which ISO release added which key). Keys are
rendered in kebab-case, which the installer accepts from 8.4-1 and prefers from 9.0-1
(snake_case is deprecated there).

## Spec

The os-install core keys ([kinds.md](../kinds.md)) plus a `[proxmox-answer]` table:

```toml
api_version = "prov/v1alpha1"
kind = "os-install"
backend = "proxmox-answer"
hostname = "demo"
domain = "example.com"          # required: the installer needs an FQDN
timezone = "Etc/UTC"
keyboard = "us"                 # mapped to the installer id en-us

[[users]]                       # sudo users only: their keys are given to root
name = "ops"
sudo = true
ssh_authorized_keys = ["ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE ops@example.com"]

[network.dns]
nameservers = ["192.0.2.53"]    # static installs: exactly one

[[network.interfaces]]          # exactly one interface
name = "eno1"
addresses = ["192.0.2.10/24"]
gateway4 = "192.0.2.1"

[storage]
disks = ["/dev/sda"]

[proxmox-answer]
country = "us"                  # two-letter code
mailto = "ops@example.com"
root_password_hash = "$6$..."   # crypt(3) hash, e.g. mkpasswd --method=yescrypt
# keyboard = "de-ch"            # installer keyboard id; overrides the mapped core keyboard
# reboot_on_error = false       # reboot instead of a shell when the install fails

[proxmox-answer.storage]
filesystem = "ext4"             # ext4 | xfs | zfs | btrfs
# raid = "raid1"                # zfs: raid0 raid1 raid10 raidz-1 raidz-2 raidz-3; btrfs: raid0 raid1 raid10
```

`prov init os-install/<name> --backend proxmox-answer` writes a synthetic starter. The
examples are `examples/os-install/demo-pve-ext4` (one by-id disk, static IPv4, NIC matched by
MAC) and `examples/os-install/demo-pve-zfs` (ZFS mirror over two kernel-named disks, DHCP,
keyboard override).

## How the core keys map

| Core key | answer.toml | Rule (`check`) |
|---|---|---|
| `hostname`, `domain` | `global.fqdn = "<hostname>.<domain>"` | `domain` is required. |
| `timezone` | `global.timezone` | |
| `keyboard` | `global.keyboard` | Mapped (`us` → `en-us`, `gb` → `en-gb`, `br` → `pt-br`, same-name ids as is); anything else (`ch`, `be`, `ca`, ...) needs `[proxmox-answer] keyboard`. |
| `locale` | — | The installer has no locale key (it follows `country`); a non-default locale is a warning. |
| `arch` | — | `amd64` only. |
| `firmware` | — | The installer detects UEFI or BIOS itself. |
| `users` | `global.root-ssh-keys` | The installer creates only root. The ssh keys of `sudo = true` users become root's keys, in spec order, without duplicates; their `groups`, `shell` and `gecos` are not used, and a `password_hash` there is a warning (root's password is `root_password_hash`). A user without `sudo` is an error. |
| `network` | `[network]` | Required, with exactly one interface (the installer configures one NIC; add the others after the install). DHCP (`dhcp4` or `dhcp6`) → `source = "from-dhcp"`, with no static values (a `macaddress` is a warning: the installer picks the NIC). Static → `source = "from-answer"` with exactly one address, the gateway of its family, and exactly one nameserver; the NIC is matched by `filter.ID_NET_NAME_MAC` when `macaddress` is set, else by `filter.ID_NET_NAME` (the predictable name). `mtu` is an error. `dns.search` may only repeat `domain` (the installer derives the search domain from the FQDN). |
| `storage.disks` | `[disk-setup]` | Kernel devices (`/dev/sda`, `/dev/nvme0n1`) → `disk-list = ["sda", ...]`. One `/dev/disk/...` link → `filter.DEVLINKS = "*<path>*"`. Several links, or a link mixed with others, are an error (one filter key per udev property); other paths (`/dev/mapper/...`) too. |

Storage rules: `ext4` and `xfs` install to exactly one disk (LVM, the installer's default
sizes) and take no `raid`. `zfs` and `btrfs` need `raid`, with these minimum disk counts:
raid0 1, raid1 2, raid10 4 (even), raidz-1 3, raidz-2 4, raidz-3 5. The installer's
`lvm.*`, `zfs.*` tuning keys (ashift, compression, sizes) and the `[first-boot]` and
`[post-installation-webhook]` sections are not rendered yet.

## Rendered files

`rendered/answer.toml` only: `[global]`, `[network]` and `[disk-setup]`, keys in a fixed order,
values as TOML basic strings. `validate` checks the TOML syntax, the required `[global]`
keys, exactly one of `root-password` and `root-password-hashed`, a known keyboard id, the
`[network]` source and its keys, exactly one of `disk-list` and `filter`, and the raid level
of `zfs`/`btrfs`; then `proxmox-auto-install-assistant validate-answer` when installed.

## Plan

`prov plan` prints, for a human: download and verify a Proxmox VE ISO (8.4-1 or later), run
`validate-answer` and `prepare-iso … --fetch-from iso --answer-file answer.toml`, check the
disk selection on the target (the `device-match disk DEVLINKS='…'` command for a by-id disk),
write the prepared ISO to a USB stick with `dd` (the stick is a placeholder), boot the target,
then log in as root on port 8006 or over ssh. The disks the install wipes are named in the
plan.

## Conformance

`prov conformance proxmox-answer` runs the os-install negative fixtures. The positive ones
are skipped with a reason: three have no `domain` (the installer needs an FQDN),
`conf-uefi-static` has a search domain other than its domain, and `conf-two-disks` names two
`/dev/disk/...` links. The backend's own tests (`tests/test_proxmox.py`) and the examples
cover rendering.
