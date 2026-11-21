"""NixOS with disko: a pinned flake holding the target system and an offline USB installer.

Renders ``flake.nix`` (nixpkgs + disko inputs; outputs ``nixosConfigurations.<host>`` and
``nixosConfigurations.<host>-installer``), ``configuration.nix`` (the target system),
``disko.nix`` (GPT, ESP or BIOS boot partition, then optional swap and an ext4 root, or
for ``ext4-luks`` LVM inside LUKS with swap, root and extra volumes) and
``installer.nix`` (minimal installer ISO carrying the target closure and an
``install-<host>`` command that checks the disk, then asks for the last 4 characters of its
serial, or the hostname when it reports none, before it wipes the disk).

prov never builds, writes or installs anything: ``plan`` prints the ``nix flake lock`` /
``nix build`` / USB / install steps for a human. ``validate`` checks the files structurally
and runs the read-only ``nix-instantiate --parse`` when nix is installed; it never evaluates
or fetches.
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..text import esc, short_repr
from ..util import NotRegularFileError, read_regular_text
from ..validate import ERROR, SKIPPED, WARNING, Finding

NAME = "nixos-disko"
KIND = "os-install"
PLUGIN_API = 1

# The disko release the flake pins (a tag of github.com/nix-community/disko). Its
# docs/disko-install.md at this tag is the source of the offline-installer pattern.
DISKO_REF = "v1.13.0"
DISKO_REF_COMMIT = "de5708739256238fb912c62f03988815db89ec9a"

# Human-created files render keeps in rendered/ (core honours PRESERVE).
PRESERVE = ("flake.lock",)

FILES = ("configuration.nix", "disko.nix", "flake.nix", "installer.nix")

# Name of the hardware file inside the installer's offline flake copy (never rendered; a
# human-created rendered/hardware-configuration.nix is not imported).
HW_COPY = "hardware-configuration.installer-copy.nix"
# The host-owned module (local_module = true) next to spec.toml, and its installer copy.
LOCAL_FILE = "local.nix"
LOCAL_COPY = "local.installer-copy.nix"
# Flake input names prov uses itself (plus the outputs function's own argument).
RESERVED_INPUTS = frozenset({"nixpkgs", "disko", "self"})

_ATTR = "[A-Za-z_][A-Za-z0-9_-]*"
_SIZE = "[1-9][0-9]*[MG]"
# A volume may not be mounted at / or at or below these (ESP, Nix store, kernel/runtime trees).
RESERVED_MOUNT_TREES = ("/boot", "/nix", "/proc", "/sys", "/dev", "/run", "/var/run", "/var/lock")
# An owner may not take over a shared system directory: these exactly, and anything at or
# below the trees (a directory below /srv or /home, such as /srv/data, is fine).
SHARED_DIRS = ("/home", "/tmp", "/srv", "/opt", "/mnt", "/media")
SHARED_TREES = ("/var", "/etc", "/usr", "/root", "/bin", "/lib", "/lib64", "/sbin")


def _paths(paths: tuple) -> str:
    return ", ".join(paths)


# One wording for the schema descriptions and the findings, built from the lists above.
_RESERVED_TEXT = f"/, nor {_paths(RESERVED_MOUNT_TREES)} or anything below them"
_SHARED_TEXT = f"{_paths(SHARED_DIRS)} themselves, or {_paths(SHARED_TREES)} and anything below them"

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "NixOS (flake + disko) settings.",
    "additionalProperties": False,
    "required": ["release", "state_version"],
    "properties": {
        "release": {
            "type": "string",
            "pattern": "^nixos-[0-9]{2}\\.[0-9]{2}$",
            "description": "nixpkgs stable branch the flake follows, e.g. nixos-26.05.",
        },
        "state_version": {
            "type": "string",
            "pattern": "^[0-9]{2}\\.[0-9]{2}$",
            "description": "system.stateVersion: the release first installed; never change it afterwards.",
        },
        "layout": {
            "enum": ["ext4", "ext4-luks"],
            "default": "ext4",
            "description": (
                "Disk layout. ext4: GPT on one disk with an ESP (UEFI) or BIOS boot partition, optional swap and an ext4 root. "
                "ext4-luks (UEFI only): an ESP and one LUKS2 partition holding LVM volume group 'pool' with optional swap, "
                "an ext4 root and the ext4 volumes; the passphrase is typed at install time and at every boot."
            ),
        },
        "swap_size": {
            "type": "string",
            "pattern": f"^{_SIZE}$",
            "description": "Optional swap size (e.g. 8G): a partition (ext4) or a logical volume (ext4-luks); no swap when absent.",
        },
        "esp_size": {
            "type": "string",
            "pattern": f"^{_SIZE}$",
            "default": "512M",
            "description": "Size of the EFI system partition mounted at /boot (UEFI only, at least 256M; leave the default on BIOS).",
        },
        "hibernate": {
            "type": "boolean",
            "default": False,
            "description": "Resume from hibernation on the swap device (boot.resumeDevice); needs swap_size, at least the RAM size.",
        },
        "root_size": {
            "type": "string",
            "pattern": f"^({_SIZE}|100%FREE)$",
            "default": "100%FREE",
            "description": "ext4-luks: size of the root logical volume (at least 8G), or 100%FREE for the space left. Exactly one of root and volumes takes 100%FREE.",
        },
        "volumes": {
            "type": "array",
            "default": [],
            "description": "ext4-luks: extra ext4 logical volumes in volume group 'pool', each mounted at its mountpoint.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "mountpoint", "size"],
                "properties": {
                    "name": {"type": "string", "pattern": "^[a-z][a-z0-9-]*$", "maxLength": 32, "description": "Logical volume name: unique, not root or swap, not starting with snapshot or pvmove."},
                    "mountpoint": {
                        "type": "string",
                        "pattern": "^/[A-Za-z0-9._/-]*$",
                        "maxLength": 255,
                        "description": f"Absolute, normalised, unique mount point; not {_RESERVED_TEXT}.",
                    },
                    "size": {"type": "string", "pattern": f"^({_SIZE}|100%FREE)$", "description": "Size (e.g. 200G) or 100%FREE for the space left."},
                    "mount_options": {
                        "type": "array",
                        "default": [],
                        "description": "ext4 mount options, one per item (e.g. noatime, nodev, nosuid, noexec).",
                        "items": {"type": "string", "pattern": "^[A-Za-z0-9][A-Za-z0-9_.=:+-]*$", "maxLength": 64},
                        "uniqueItems": True,
                    },
                    "owner": {
                        "type": "string",
                        "pattern": "^[a-z_][a-z0-9_-]{0,31}$",
                        "description": (
                            "A [[users]] name: the mount point becomes owned by this user (group users, mode 0750) via "
                            f"systemd-tmpfiles. Not on a shared system directory ({_SHARED_TEXT}) and not on a declared "
                            "user's home /home/<name> (NixOS keeps it at mode 0700)."
                        ),
                    },
                },
            },
        },
        "packages": {
            "type": "array",
            "description": "nixpkgs attribute names added to environment.systemPackages (e.g. git, python3Packages.requests).",
            "items": {"type": "string", "pattern": f"^{_ATTR}(\\.{_ATTR})*$"},
            "uniqueItems": True,
            "default": [],
        },
        "local_module": {
            "type": "boolean",
            "default": False,
            "description": (
                f"Import the host-owned NixOS module ../{LOCAL_FILE} next to spec.toml (outside rendered/; git add it). "
                "For what the spec cannot express: unfree packages, services, programs.*, fonts. It must be "
                "self-contained (no other relative paths): the installer copies this one file. Gets the flake's "
                "inputs as the module argument `inputs`."
            ),
        },
        "inputs": {
            "type": "object",
            "default": {},
            "description": (
                "Extra flake inputs, name -> flake URL pinned to a tag or rev (e.g. github:owner/tool/v1.2.3), "
                f"for ../{LOCAL_FILE} to use as inputs.<name>. Names: lowercase identifiers, not nixpkgs, disko or self."
            ),
            "additionalProperties": {"type": "string", "pattern": "^[a-z][a-z0-9+.-]*:[^\\s\"'${}\\\\]+$", "maxLength": 512},
        },
        "cpu_vendor": {"enum": ["intel", "amd"], "description": "Enables the CPU microcode updates of this vendor."},
        "networking": {
            "enum": ["networkd", "networkmanager"],
            "default": "networkd",
            "description": (
                "networkd: systemd-networkd rendered from [network] (required). networkmanager: NetworkManager, "
                "connections (Wi-Fi included) made on the machine with nmtui, nmcli or GNOME Settings; [network] is not allowed."
            ),
        },
        "desktop": {
            "enum": ["none", "gnome"],
            "default": "none",
            "description": "none: no graphical desktop. gnome: GNOME on Wayland with GDM; needs networking = networkmanager and release nixos-25.11 or newer.",
        },
    },
}

ESP_MIN_MIB = 256
BIOS_BOOT_SIZE = "1M"
FREE = "100%FREE"
LUKS_NAME = "crypted"  # /dev/mapper/crypted; boot.initrd.luks.devices.crypted (set by disko)
VG_NAME = "pool"
# Logical volume names prov uses itself; LVM rejects names starting with these prefixes.
RESERVED_LVS = frozenset({"root", "swap"})
RESERVED_LV_PREFIXES = ("snapshot", "pvmove")
ESP_DEFAULT = "512M"
# First release with services.displayManager.gdm / services.desktopManager.gnome (moved out
# of services.xserver in NixOS 25.11).
GNOME_MIN_RELEASE = "25.11"
ROOT_MIN_MIB = 8 * 1024
BY_ID = "/dev/disk/by-id/"
INITRD_MODULES = ("nvme", "ahci", "xhci_pci", "usb_storage", "sd_mod", "usbhid")
SYSTEMS = {"amd64": "x86_64-linux", "arm64": "aarch64-linux"}
SHELLS = {"bash": None, "zsh": "zsh", "fish": "fish"}  # basename -> programs.<x> / pkgs.<x>
SHELL_DIRS = ("/bin/", "/usr/bin/", "/run/current-system/sw/bin/")
# Accounts NixOS defines itself; a [[users]] entry with one of these names would redefine a
# system account. Deliberately small: the common ones plus the nixbld<N> build users.
RESERVED_USERS = frozenset({"root", "nobody", "messagebus", "sshd", "nscd", "systemd-network", "systemd-resolve", "systemd-timesync"})
NIX_KEYWORDS = frozenset({"assert", "else", "if", "in", "inherit", "let", "or", "rec", "then", "with"})


# --- Nix text helpers ---------------------------------------------------------------------


def nix_str(value: str) -> str:
    """A Nix double-quoted string literal for ``value`` (no interpolation can leak in)."""
    out = value.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    out = out.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return f'"{out}"'


def nix_list(values: List[str]) -> str:
    return "[ " + " ".join(nix_str(v) for v in values) + " ]" if values else "[ ]"


def nix_attr(name: str) -> str:
    """``name`` as a Nix attribute name: bare when it is an identifier, quoted otherwise."""
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_'-]*", name) and name not in NIX_KEYWORDS:
        return name
    return nix_str(name)


def _shell(path: str) -> Optional[str]:
    """``None`` for bash, the program name for zsh/fish; raises ValueError otherwise."""
    for prefix in SHELL_DIRS:
        if path.startswith(prefix) and path[len(prefix):] in SHELLS:
            return SHELLS[path[len(prefix):]]
    raise ValueError(path)


def _mib(size: str) -> int:
    """MiB in a schema size such as ``512M`` or ``1G``."""
    return int(size[:-1]) * (1024 if size.endswith("G") else 1)


def _at_or_below(path: str, trees: tuple) -> bool:
    return any(path == t or path.startswith(t + "/") for t in trees)


# --- contract: check ----------------------------------------------------------------------


def _check_storage(spec: Dict[str, Any]) -> List[Finding]:
    """Layout rules: ESP size, hibernation, ext4-luks firmware, root_size and volumes."""
    out: List[Finding] = []
    nixos = spec[NAME]
    luks = nixos["layout"] == "ext4-luks"
    if spec["firmware"] == "uefi" and _mib(nixos["esp_size"]) < ESP_MIN_MIB:
        out.append(Finding(ERROR, f"esp_size {nixos['esp_size']} is too small; use at least {ESP_MIN_MIB}M", f"{NAME}.esp_size"))
    if spec["firmware"] != "uefi" and nixos["esp_size"] != ESP_DEFAULT:
        out.append(Finding(ERROR, "esp_size applies to firmware 'uefi' only; BIOS uses a 1M BIOS boot partition", f"{NAME}.esp_size"))
    if nixos["hibernate"] and "swap_size" not in nixos:
        out.append(Finding(ERROR, "hibernate needs swap_size (at least the RAM size) to resume from", f"{NAME}.hibernate"))
    if luks and spec["firmware"] != "uefi":
        out.append(
            Finding(
                ERROR,
                "layout 'ext4-luks' needs firmware 'uefi': it keeps /boot on an unencrypted ESP; "
                "GRUB unlocking an encrypted /boot (cryptodisk) is not supported",
                "firmware",
            )
        )
    if not luks:
        if nixos["root_size"] != FREE:
            out.append(Finding(ERROR, "root_size applies to layout 'ext4-luks' only; the ext4 root takes the rest of the disk", f"{NAME}.root_size"))
        if nixos["volumes"]:
            out.append(Finding(ERROR, "volumes need layout 'ext4-luks' (logical volumes); layout 'ext4' has a single root partition", f"{NAME}.volumes"))
        return out
    if nixos["root_size"] != FREE and _mib(nixos["root_size"]) < ROOT_MIN_MIB:
        out.append(Finding(ERROR, f"root_size {nixos['root_size']} is too small for a NixOS root; use at least 8G or {FREE}", f"{NAME}.root_size"))
    free = (["root"] if nixos["root_size"] == FREE else []) + [v["name"] for v in nixos["volumes"] if v["size"] == FREE]
    if len(free) != 1:
        which = "none does" if not free else "several do: " + ", ".join(short_repr(n) for n in free)
        out.append(Finding(ERROR, f"exactly one of root_size and the volumes' size must be {FREE} (the space left); {which}", f"{NAME}.volumes"))
    users = {u["name"] for u in spec["users"]}
    homes = {f"/home/{u}": u for u in sorted(users)}
    names: Dict[str, int] = {}
    mounts: Dict[str, int] = {}
    for i, vol in enumerate(nixos["volumes"]):
        where = f"{NAME}.volumes[{i}]"
        name, mount = vol["name"], vol["mountpoint"]
        if name in RESERVED_LVS or name.startswith(RESERVED_LV_PREFIXES):
            out.append(Finding(ERROR, f"volume name {short_repr(name)} is reserved (root, swap, or starting with snapshot or pvmove)", f"{where}.name"))
        elif name in names:
            out.append(Finding(ERROR, f"volume name {short_repr(name)} is already used by volumes[{names[name]}]", f"{where}.name"))
        names.setdefault(name, i)
        parts = mount.split("/")[1:]
        if mount != "/" and (mount.endswith("/") or any(p in ("", ".", "..") for p in parts)):
            out.append(Finding(ERROR, f"mountpoint {short_repr(mount)} is not normalised (no //, trailing /, . or ..)", f"{where}.mountpoint"))
        elif mount == "/" or _at_or_below(mount, RESERVED_MOUNT_TREES):
            out.append(
                Finding(ERROR, f"mountpoint {short_repr(mount)} is reserved: not {_RESERVED_TEXT}", f"{where}.mountpoint")
            )
        elif mount in mounts:
            out.append(Finding(ERROR, f"mountpoint {short_repr(mount)} is already used by volumes[{mounts[mount]}]", f"{where}.mountpoint"))
        mounts.setdefault(mount, i)
        if "owner" in vol and vol["owner"] not in users:
            out.append(Finding(ERROR, f"owner {short_repr(vol['owner'])} is not a [[users]] name", f"{where}.owner"))
        elif "owner" in vol and mount in homes:
            out.append(
                Finding(
                    ERROR,
                    f"owner on {short_repr(mount)}: that is the home of user {short_repr(homes[mount])}, which NixOS creates and "
                    "keeps at mode 0700 (users.users.<name>.homeMode); a 0750 tmpfiles rule would fight it. Drop owner, or "
                    "mount the volume elsewhere",
                    f"{where}.owner",
                )
            )
        elif "owner" in vol and (mount in SHARED_DIRS or _at_or_below(mount, SHARED_TREES)):
            out.append(
                Finding(
                    ERROR,
                    f"owner on {short_repr(mount)}: a shared system directory ({_SHARED_TEXT}) may not belong to one "
                    "user; mount the volume below it instead (e.g. /srv/data)",
                    f"{where}.owner",
                )
            )
    return out


def _check_networking(spec: Dict[str, Any]) -> List[Finding]:
    """networking (networkd needs [network], NetworkManager refuses it) and desktop rules."""
    out: List[Finding] = []
    nixos = spec[NAME]
    nm = nixos["networking"] == "networkmanager"
    if not nm and "network" not in spec:
        out.append(
            Finding(
                ERROR,
                "networking 'networkd' needs [network] with at least one interface; "
                'on a machine that joins networks interactively set networking = "networkmanager"',
                "network",
            )
        )
    if nm and "network" in spec:
        out.append(
            Finding(
                ERROR,
                "[network] is not used with networking 'networkmanager' (connections are made on the machine with "
                'nmtui or nmcli); remove it or set networking = "networkd"',
                "network",
            )
        )
    if nixos["desktop"] == "gnome":
        if not any("password_hash" in u for u in spec["users"]):
            out.append(
                Finding(WARNING, "desktop 'gnome' but no user has password_hash: nobody can log in at the GDM login screen (ssh keys only work over ssh)", "users")
            )
        if not nm:
            out.append(
                Finding(ERROR, 'desktop \'gnome\' needs networking = "networkmanager" (GNOME manages connections through NetworkManager)', f"{NAME}.desktop")
            )
        if nixos["release"][len("nixos-"):] < GNOME_MIN_RELEASE:
            out.append(
                Finding(
                    ERROR,
                    f"desktop 'gnome' needs release nixos-{GNOME_MIN_RELEASE} or newer (its GDM and GNOME options moved out of "
                    f"services.xserver there); got {nixos['release']}",
                    f"{NAME}.release",
                )
            )
    return out


# A flake URL is pinned when it names a commit (40 hex digits) or a version-like tag (v1.2.3,
# 2.0, 2026-11-15...), as github:o/r/<ref>, ?ref= or ?rev=. A branch (main, nixos-unstable)
# moves on nix flake update; a static check cannot tell a tag from a branch beyond this.
_URL_REF = re.compile(r"^(?:github|gitlab|sourcehut):[^/?]+/[^/?]+/([^/?]+)|[?&](?:ref|rev)=([^&]+)")
_PINNED_REF = re.compile(r"[0-9a-f]{40}|v?[0-9][A-Za-z0-9._+-]*")


def _pinned(url: str) -> bool:
    found = [m.group(1) or m.group(2) for m in _URL_REF.finditer(url)]
    return any(_PINNED_REF.fullmatch(ref) for ref in found)


def _check_inputs(nixos: Dict[str, Any]) -> List[Finding]:
    out: List[Finding] = []
    for name in sorted(nixos["inputs"]):
        where = f"{NAME}.inputs[{short_repr(name)}]"
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name) or len(name) > 64 or name in NIX_KEYWORDS:
            out.append(
                Finding(
                    ERROR,
                    f"input name {short_repr(name)} must be a lowercase identifier (a-z, 0-9, -; at most 64) and not a Nix keyword",
                    where,
                )
            )
        elif name in RESERVED_INPUTS:
            out.append(Finding(ERROR, f"input name {short_repr(name)} is used by prov itself; choose another name", where))
        if not _pinned(nixos["inputs"][name]):
            out.append(
                Finding(
                    WARNING,
                    f"{short_repr(nixos['inputs'][name])} names no version tag or commit; flake.lock pins it, but nix flake "
                    "update moves a branch or bare repository on. Pin a release (github:owner/repo/v1.2.3) or a commit",
                    where,
                )
            )
    if nixos["inputs"] and not nixos["local_module"]:
        out.append(
            Finding(WARNING, f"inputs are only usable from ../{LOCAL_FILE}; set local_module = true and use them there", f"{NAME}.inputs")
        )
    return out


def check(spec: Dict[str, Any], ctx: Any) -> List[Finding]:
    """NixOS-specific rules beyond the JSON Schema."""
    out: List[Finding] = []
    nixos = spec[NAME]
    disks = spec["storage"]["disks"]
    if len(disks) != 1:
        out.append(Finding(ERROR, f"layout {nixos['layout']!r} installs to exactly one disk, got {len(disks)}", "storage.disks"))
    out += _check_storage(spec)
    for d, disk in enumerate(disks):
        if not disk.startswith(BY_ID) or "/../" in disk or "/./" in disk or disk.endswith(("/..", "/.")):
            out.append(
                Finding(WARNING, f"{short_repr(disk)} is not a plain {BY_ID} path; kernel names like /dev/sda can change between boots", f"storage.disks[{d}]")
            )
    out += _check_networking(spec)
    for i, iface in enumerate(spec.get("network", {}).get("interfaces", [])):
        if "macaddress" not in iface and re.fullmatch(r"eth[0-9]+", iface["name"]):
            out.append(
                Finding(
                    WARNING,
                    f"{short_repr(iface['name'])} has no macaddress; NixOS names NICs predictably (e.g. enp3s0), so it likely "
                    "matches no NIC. Add macaddress to name the NIC, or use its predictable name",
                    f"network.interfaces[{i}]",
                )
            )
    if spec["arch"] == "arm64" and "cpu_vendor" in nixos:
        out.append(Finding(ERROR, "cpu_vendor (intel/amd microcode) applies to amd64 only", f"{NAME}.cpu_vendor"))
    if spec["arch"] == "arm64" and spec["firmware"] == "bios":
        out.append(Finding(ERROR, "arm64 boots with UEFI only", "firmware"))
    for n, user in enumerate(spec["users"]):
        if user["name"] in RESERVED_USERS or re.fullmatch(r"nixbld[0-9]*", user["name"]):
            out.append(Finding(ERROR, f"user name {short_repr(user['name'])} is a NixOS system account; choose another name", f"users[{n}].name"))
        if ":" in user.get("gecos", ""):
            out.append(Finding(ERROR, "gecos may not contain ':' (the passwd field separator)", f"users[{n}].gecos"))
        try:
            _shell(user["shell"])
        except ValueError:
            out.append(
                Finding(
                    ERROR,
                    f"shell {short_repr(user['shell'])} is not supported; use /bin/bash (the default), /bin/zsh or /bin/fish",
                    f"users[{n}].shell",
                )
            )
    if not any(u["ssh_authorized_keys"] or "password_hash" in u for u in spec["users"]):
        out.append(Finding(ERROR, "no user has ssh_authorized_keys or password_hash; nobody could log in", "users"))
    for p, attr in enumerate(nixos["packages"]):
        bad = [part for part in attr.split(".") if part in NIX_KEYWORDS]
        if bad:
            out.append(Finding(ERROR, f"{short_repr(attr)}: {short_repr(bad[0])} is a Nix keyword, not a package attribute", f"{NAME}.packages[{p}]"))
    out += _check_inputs(nixos)
    release = nixos["release"][len("nixos-"):]
    if nixos["state_version"] > release:
        out.append(
            Finding(WARNING, f"state_version {nixos['state_version']} is newer than release {release}; set it to the release first installed", f"{NAME}.state_version")
        )
    return out


# --- contract: render ---------------------------------------------------------------------

HEADER = "# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render."


def _extras(spec: Dict[str, Any]) -> bool:
    """True when the target system gets the flake inputs (an extra input or local.nix)."""
    return bool(spec[NAME]["inputs"]) or spec[NAME]["local_module"]


def flake_nix(spec: Dict[str, Any]) -> str:
    host = spec["hostname"]
    system = SYSTEMS[spec["arch"]]
    release = spec[NAME]["release"]
    extra = spec[NAME]["inputs"]
    commit = "flake.lock and ../hardware-configuration.nix (if any)"
    if spec[NAME]["local_module"]:
        commit = f"flake.lock, ../{LOCAL_FILE} and ../hardware-configuration.nix (if any)"
    inputs = [
        f"    nixpkgs.url = {nix_str(f'github:NixOS/nixpkgs/{release}')};",
        f"    disko.url = {nix_str(f'github:nix-community/disko/{DISKO_REF}')};",
        '    disko.inputs.nixpkgs.follows = "nixpkgs";',
    ]
    if extra:
        inputs.append("    # Extra inputs from spec.toml (inputs), each with its own locked dependencies.")
        inputs += [f"    {nix_attr(name)}.url = {nix_str(extra[name])};" for name in sorted(extra)]
    target = ["    # The target system."]
    if _extras(spec):
        among = f" (../{LOCAL_FILE} among them)" if spec[NAME]["local_module"] else ""
        target += [
            f"    # Its modules{among} get the flake inputs as `inputs`, without self: on the",
            "    # installer stick self is a copy of this flake, and a system built from it would differ.",
        ]
    target += [f"    nixosConfigurations.{nix_attr(host)} = nixpkgs.lib.nixosSystem {{"]
    if _extras(spec):
        target.append('      specialArgs = { inputs = builtins.removeAttrs inputs [ "self" ]; };')
    return "\n".join(
        [
            HEADER,
            f"# Commit this directory, {commit} to git:",
            "# Nix copies only git-tracked files of a flake.",
            "{",
            f"  description = {nix_str(f'NixOS host {host} (prov os-install, backend nixos-disko)')};",
            "",
            "  inputs = {",
            *inputs,
            "  };",
            "",
            "  outputs = { self, nixpkgs, disko, ... }" + ("@inputs" if _extras(spec) else "") + ": {",
            *target,
            "      modules = [",
            f"        {{ nixpkgs.hostPlatform = {nix_str(system)}; }}",
            "        disko.nixosModules.disko",
            "        ./configuration.nix",
            "        ./disko.nix",
            "      ];",
            "    };",
            "",
            "    # The USB installer: nix build .#nixosConfigurations.<host>-installer.config.system.build.isoImage",
            f"    nixosConfigurations.{nix_attr(host + '-installer')} = nixpkgs.lib.nixosSystem {{",
            "      specialArgs = { inherit self; };",
            "      modules = [",
            f"        {{ nixpkgs.hostPlatform = {nix_str(system)}; }}",
            "        ./installer.nix",
            "      ];",
            "    };",
            "  };",
            "}",
            "",
        ]
    )


def _network(spec: Dict[str, Any]) -> List[str]:
    if spec[NAME]["networking"] == "networkmanager":
        where = "nmtui, nmcli or GNOME Settings" if spec[NAME]["desktop"] == "gnome" else "nmtui or nmcli"
        return [
            "  # Network: NetworkManager. Connections, Wi-Fi included, are made on the machine with",
            f"  # {where} after the first boot; this file holds no network credentials.",
            "  networking.networkmanager.enable = true;",
        ]
    lines = [
        "  # Network: systemd-networkd, one unit per interface; NICs with a MAC are renamed by .link files.",
        "  networking.useDHCP = false;",
        "  networking.useNetworkd = true;",
        "  systemd.network.enable = true;",
        "  # Boot waits for one configured interface, not all (an unplugged NIC would stall it).",
        "  systemd.network.wait-online.anyInterface = true;",
    ]
    dns = spec["network"]["dns"]
    if dns["nameservers"]:
        lines.append(f"  networking.nameservers = {nix_list(dns['nameservers'])};")
    if dns["search"]:
        lines.append(f"  networking.search = {nix_list(dns['search'])};")
    for iface in spec["network"]["interfaces"]:
        name = iface["name"]
        unit = nix_str(f"10-{name}")
        if "macaddress" in iface:
            lines += [
                f"  systemd.network.links.{unit} = {{",
                f"    matchConfig.PermanentMACAddress = {nix_str(iface['macaddress'])};",
                f"    linkConfig.Name = {nix_str(name)};",
                "  };",
            ]
        dhcp = {(True, True): "yes", (True, False): "ipv4", (False, True): "ipv6", (False, False): "no"}[(iface["dhcp4"], iface["dhcp6"])]
        lines += [
            f"  systemd.network.networks.{unit} = {{",
            f"    matchConfig.Name = {nix_str(name)};",
            f"    networkConfig.DHCP = {nix_str(dhcp)};",
        ]
        if iface["addresses"]:
            lines.append(f"    address = {nix_list(iface['addresses'])};")
        gateways = [iface[gw] for gw in ("gateway4", "gateway6") if gw in iface]
        if gateways:
            lines.append(f"    gateway = {nix_list(gateways)};")
        if "mtu" in iface:
            lines.append(f"    linkConfig.MTUBytes = {nix_str(str(iface['mtu']))};")
        lines.append("  };")
    return lines


def _users(spec: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    shells = set()
    sudoers = []
    nm = spec[NAME]["networking"] == "networkmanager"
    for user in spec["users"]:
        groups = list(user["groups"])
        if user["sudo"]:
            sudoers.append(user["name"])
            if "wheel" not in groups:
                groups.append("wheel")
        # Passwordless sudo is root already: NetworkManager need not ask these users either.
        if nm and user["sudo"] and "networkmanager" not in groups:
            groups.append("networkmanager")
        lines += [f"  users.users.{nix_attr(user['name'])} = {{", "    isNormalUser = true;"]
        if "gecos" in user:
            lines.append(f"    description = {nix_str(user['gecos'])};")
        if groups:
            lines.append(f"    extraGroups = {nix_list(groups)};")
        shell = _shell(user["shell"])
        if shell:
            shells.add(shell)
            lines.append(f"    shell = pkgs.{shell};")
        if "password_hash" in user:
            lines.append(f"    hashedPassword = {nix_str(user['password_hash'])};")
        if user["ssh_authorized_keys"]:
            lines.append("    openssh.authorizedKeys.keys = [")
            lines += [f"      {nix_str(key)}" for key in user["ssh_authorized_keys"]]
            lines.append("    ];")
        lines.append("  };")
    for shell in sorted(shells):
        lines.append(f"  programs.{shell}.enable = true;")
    if sudoers:
        lines += [
            "  # Passwordless sudo for the users with sudo = true (they are in wheel too).",
            "  security.sudo.extraRules = [",
            "    {",
            f"      users = {nix_list(sudoers)};",
            '      commands = [ { command = "ALL"; options = [ "NOPASSWD" ]; } ];',
            "    }",
            "  ];",
        ]
    return lines


def configuration_nix(spec: Dict[str, Any]) -> str:
    nixos = spec[NAME]
    luks = nixos["layout"] == "ext4-luks"
    lines = [
        HEADER,
        "# The target system. Static validation only: prov has not installed it on hardware.",
        "{ config, lib, pkgs, modulesPath, ... }:",
        "let",
        "  # A human-owned hardware-configuration.nix next to spec.toml (outside rendered/) is",
        "  # imported when it exists and is git-tracked; render never writes or deletes it.",
        f"  # On the USB stick disko-install evaluates a copy of this directory alone in the store,",
        f"  # where ../ is the store directory itself: the installer copies the file in as",
        f"  # ./{HW_COPY}, and ../ is not looked at there.",
        "  storeRoot = dirOf (toString ./.) == builtins.storeDir;",
        "  hardware =",
        f"    if builtins.pathExists ./{HW_COPY} then [ ./{HW_COPY} ]",
        "    else if !storeRoot && builtins.pathExists ../hardware-configuration.nix then [ ../hardware-configuration.nix ]",
        "    else [ ];",
    ]
    imports = "hardware"
    if nixos["local_module"]:
        lines += [
            f"  # local_module: the host-owned ../{LOCAL_FILE} next to spec.toml (outside rendered/), for",
            f"  # what spec.toml cannot express. Required: git add it. The installer copies it in as ./{LOCAL_COPY}.",
            "  local =",
            f"    if builtins.pathExists ./{LOCAL_COPY} then [ ./{LOCAL_COPY} ]",
            f"    else assert lib.assertMsg (!storeRoot && builtins.pathExists ../{LOCAL_FILE})",
            f'      "local_module = true: create ../{LOCAL_FILE} next to spec.toml and git add it";',
            f"    [ ../{LOCAL_FILE} ];",
        ]
        imports = "hardware ++ local"
    lines += [
        "in",
        "{",
        f'  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ] ++ {imports};',
        "",
        "  # Fallback for hardware without a hardware-configuration.nix: broad initrd storage/USB drivers.",
    ]
    lines.append(f"  boot.initrd.availableKernelModules = {nix_list(list(INITRD_MODULES))};")
    if luks:
        lines += [
            "  # ext4-luks: disko.nix declares boot.initrd.luks.devices.crypted (from its luks partition)",
            "  # and the LVM volume group; nixpkgs' default initrd modules carry the USB/HID keyboard",
            "  # drivers. The console keymap is loaded early, before the passphrase prompt.",
            "  console.earlySetup = true;",
        ]
    if "cpu_vendor" in nixos:
        lines.append(f"  hardware.cpu.{nixos['cpu_vendor']}.updateMicrocode = true;")
    lines.append("")
    if spec["firmware"] == "uefi":
        lines += [
            "  # UEFI: systemd-boot on the ESP (disko.nix mounts it at /boot).",
            "  boot.loader.systemd-boot.enable = true;",
            "  boot.loader.systemd-boot.configurationLimit = 10; # keeps the "
            + ("512M ESP" if nixos["esp_size"] == ESP_DEFAULT else "ESP")
            + " from filling up",
            "  boot.loader.efi.canTouchEfiVariables = true;",
        ]
    else:
        lines += [
            "  # BIOS: GRUB in the MBR gap; disko sets boot.loader.grub.devices from the EF02 partition.",
            "  boot.loader.grub.enable = true;",
            "  boot.loader.grub.configurationLimit = 10;",
        ]
    lines += ["", f"  networking.hostName = {nix_str(spec['hostname'])};"]
    if "domain" in spec:
        lines.append(f"  networking.domain = {nix_str(spec['domain'])};")
    lines += [
        f"  time.timeZone = {nix_str(spec['timezone'])};",
        f"  i18n.defaultLocale = {nix_str(spec['locale'])};",
    ]
    if nixos["desktop"] == "gnome":
        lines.append("  # keyboard is an XKB layout; the console keymap is derived from it and the desktop uses the same setting.")
    else:
        lines.append("  # keyboard is an XKB layout; the console keymap is derived from it (X stays disabled).")
    lines += [
        f"  services.xserver.xkb.layout = {nix_str(spec['keyboard'])};",
        "  console.useXkbConfig = true;",
        "",
    ]
    lines += _network(spec)
    if nixos["networking"] == "networkmanager" and any(u["sudo"] for u in spec["users"]):
        lines.append("  # Users with sudo = true are also in the networkmanager group (they have passwordless sudo).")
    lines.append("")
    if nixos["desktop"] == "gnome":
        lines += [
            "  # Desktop: GNOME on Wayland with the GDM login screen (option names of NixOS 25.11 and later).",
            "  services.displayManager.gdm.enable = true;",
            "  services.desktopManager.gnome.enable = true;",
            "",
        ]
    lines += _users(spec)
    owned = sorted((v["mountpoint"], v["owner"]) for v in nixos["volumes"] if "owner" in v)
    if owned:
        lines += ["  # Owned volumes: the mount point belongs to its owner (group users, mode 0750)."]
        lines += ["  systemd.tmpfiles.rules = ["] + [f"    {nix_str(f'd {m} 0750 {o} users -')}" for m, o in owned] + ["  ];"]
    lines += [
        "",
        "  services.openssh = {",
        "    enable = true;",
        "    settings = {",
        "      PasswordAuthentication = false;",
        "      KbdInteractiveAuthentication = false;",
        '      PermitRootLogin = "no";',
        "    };",
        "  };",
        "",
        '  nix.settings.experimental-features = [ "nix-command" "flakes" ];',
    ]
    packages = nixos["packages"]
    if packages:
        lines += ["  environment.systemPackages = ["] + [f"    pkgs.{p}" for p in packages] + ["  ];"]
    else:
        lines.append("  environment.systemPackages = [ ];")
    lines += [
        "",
        "  # The release first installed; never change it after the install.",
        f"  system.stateVersion = {nix_str(nixos['state_version'])};",
        "}",
        "",
    ]
    return "\n".join(lines)


def _swap_content(nixos: Dict[str, Any]) -> str:
    if nixos["hibernate"]:
        return 'content = { type = "swap"; resumeDevice = true; }; # boot.resumeDevice: resume from hibernation'
    return 'content = { type = "swap"; };'


def _filesystem(mountpoint: str, options: List[str], indent: str) -> List[str]:
    lines = [
        f"{indent}content = {{",
        f'{indent}  type = "filesystem";',
        f'{indent}  format = "ext4";',
        f"{indent}  mountpoint = {nix_str(mountpoint)};",
    ]
    if options:
        lines.append(f"{indent}  mountOptions = {nix_list(options)};")
    return lines + [f"{indent}}};"]


def _lvm_vg(nixos: Dict[str, Any]) -> List[str]:
    """The LVM volume group inside the LUKS container: swap, root and the volumes.

    disko v1.13.0 (lib/types/lvm_vg.nix) creates the LVs sorted by their ``priority``, whose
    default is 1000, plus 251 when the size contains "100%": the one 100%FREE LV (check()
    enforces exactly one) is created after every fixed-size LV, whatever its name.
    """
    lines = [
        f"  disko.devices.lvm_vg.{VG_NAME} = {{",
        '    type = "lvm_vg";',
        "    lvs = {",
    ]
    if "swap_size" in nixos:
        lines += [
            "      swap = {",
            f"        size = {nix_str(nixos['swap_size'])};",
            f"        {_swap_content(nixos)}",
            "      };",
        ]
    lines += ["      root = {", f"        size = {nix_str(nixos['root_size'])};"]
    lines += _filesystem("/", [], "        ") + ["      };"]
    for vol in nixos["volumes"]:
        lines += [f"      {nix_attr(vol['name'])} = {{", f"        size = {nix_str(vol['size'])};"]
        lines += _filesystem(vol["mountpoint"], vol["mount_options"], "        ") + ["      };"]
    return lines + ["    };", "  };"]


def disko_nix(spec: Dict[str, Any]) -> str:
    nixos = spec[NAME]
    luks = nixos["layout"] == "ext4-luks"
    disk = spec["storage"]["disks"][0]
    lines = [HEADER, "# Partitioning (disko). WARNING: disko-install wipes this disk."]
    if luks:
        lines += [
            f"# Layout ext4-luks: ESP + LUKS2 partition -> LVM volume group {VG_NAME}. No key or password file is",
            "# set, so disko asks for the new passphrase on the terminal when it formats the disk.",
        ]
    lines += [
        "{",
        "  disko.devices.disk.main = {",
        '    type = "disk";',
        f"    device = {nix_str(disk)};",
        "    content = {",
        '      type = "gpt";',
        "      partitions = {",
    ]
    if spec["firmware"] == "uefi":
        lines += [
            "        ESP = {",
            '          type = "EF00";',
            f"          size = {nix_str(nixos['esp_size'])};",
            "          content = {",
            '            type = "filesystem";',
            '            format = "vfat";',
            '            mountpoint = "/boot";',
            '            mountOptions = [ "umask=0077" ];',
            "          };",
            "        };",
        ]
    else:
        lines += [
            "        boot = {",
            '          type = "EF02"; # BIOS boot partition for GRUB',
            f"          size = {nix_str(BIOS_BOOT_SIZE)};",
            "        };",
        ]
    if luks:
        lines += [
            "        luks = {",
            '          size = "100%";',
            "          content = {",
            '            type = "luks";',
            f"            name = {nix_str(LUKS_NAME)};",
            '            extraFormatArgs = [ "--type" "luks2" ];',
            "            settings.allowDiscards = true; # TRIM on SSDs; reveals which blocks are unused",
            f'            content = {{ type = "lvm_pv"; vg = {nix_str(VG_NAME)}; }};',
            "          };",
            "        };",
            "      };",
            "    };",
            "  };",
        ]
        lines += _lvm_vg(nixos)
        return "\n".join(lines + ["}", ""])
    if "swap_size" in nixos:
        lines += [
            "        swap = {",
            f"          size = {nix_str(nixos['swap_size'])};",
            f"          {_swap_content(nixos)}",
            "        };",
        ]
    lines += [
        "        root = {",
        '          size = "100%";',
        "          content = {",
        '            type = "filesystem";',
        '            format = "ext4";',
        '            mountpoint = "/";',
        "          };",
        "        };",
        "      };",
        "    };",
        "  };",
        "}",
        "",
    ]
    return "\n".join(lines)


# ext4-luks: disko's create steps are idempotent (vgcreate only without a VG of that name,
# luks open only when the mapping is not active, mkfs only without a file system), so a VG
# "pool" or an open "crypted" mapping on ANOTHER disk would be reused silently and the
# install would land there. Refuse before the confirmation; every probe fails closed.
_LUKS_GUARD = [
    f"      # ext4-luks: refuse when a VG {VG_NAME} or LUKS mapping {LUKS_NAME} already exists (on any disk):",
    "      # disko would reuse them instead of creating them on the target disk.",
    '      vgnames=$(vgs --noheadings -o vg_name) || refuse "cannot list the LVM volume groups (vgs)"',
    "      while read -r vg; do",
    f'        if [ "$vg" = {VG_NAME} ]; then',
    f'          refuse "an LVM volume group named {VG_NAME} already exists. On another disk: unplug it or rename it '
    f"(vgrename {VG_NAME} <other-name>). Left over from an earlier attempt on this disk: vgchange -an {VG_NAME}, "
    f'then cryptsetup close {LUKS_NAME}"',
    "        fi",
    '      done <<< "$vgnames"',
    "      # cryptsetup status: 0 active, 4 inactive (-ENODEV); anything else is an unanswered question.",
    "      crypt_rc=0",
    f"      cryptsetup status {LUKS_NAME} >/dev/null 2>&1 || crypt_rc=$?",
    f'      if [ -e /dev/mapper/{LUKS_NAME} ] || [ "$crypt_rc" = 0 ]; then',
    f'        refuse "/dev/mapper/{LUKS_NAME} is already open. Left over from an earlier attempt: vgchange -an {VG_NAME}, '
    f'then cryptsetup close {LUKS_NAME}. From another disk: unplug it"',
    '      elif [ "$crypt_rc" != 4 ]; then',
    f'        refuse "cannot query the LUKS mapping {LUKS_NAME} (cryptsetup status exited $crypt_rc); '
    'load device-mapper: modprobe dm_mod, then retry"',
    "      fi",
]

# Every layout: disko addresses its partitions by partition label (disk-main-ESP, -root,
# -swap, -luks, -boot) and skips mkfs/luksFormat on a partition that already has one, so a
# disk-main-* label left on ANOTHER disk could capture /boot or / at install and at boot
# (udev points /dev/disk/by-partlabel/<label> at one of the two). Refuse unless every such
# label positively sits on the target disk: every disk or loop ancestor must be the target,
# and at least one must be found; each probe fails closed.
_PARTLABEL_GUARD = [
    "      # Partition labels disko uses (disk-main-*) must not exist on any other disk.",
    '      parts=$(lsblk -nrpo NAME,PARTLABEL) || refuse "cannot list the partition labels (lsblk)"',
    "      while read -r pname plabel; do",
    '        case "$plabel" in',
    "          disk-main-*)",
    '            owners=$(lsblk -nrpso NAME,TYPE "$pname") || refuse "cannot read the disk under $pname"',
    "            on_target=0",
    "            while read -r oname okind; do",
    '              if [ "$okind" = "disk" ] || [ "$okind" = "loop" ]; then',
    '                o=$(readlink -f "$oname") || refuse "cannot resolve $oname"',
    '                if [ "$o" != "$dev" ]; then',
    '                  refuse "$pname on disk $o carries the partition label $plabel that disko uses for this install; '
    "it could capture a mount at install or boot time. Unplug that disk, or wipe its partition table yourself "
    '(wipefs -a or sgdisk --zap-all on $o, NOT on the target disk)"',
    "                fi",
    "                on_target=1",
    "              fi",
    '            done <<< "$owners"',
    '            if [ "$on_target" != 1 ]; then',
    '              refuse "$pname carries the partition label $plabel but does not sit on $dev; disko could pick it up. '
    'Unplug or wipe the device it is on"',
    "            fi",
    "            ;;",
    "        esac",
    '      done <<< "$parts"',
]


def installer_nix(spec: Dict[str, Any]) -> str:
    host = spec["hostname"]
    disk = spec["storage"]["disks"][0]
    target = f"self.nixosConfigurations.{nix_attr(host)}"
    efi = "--write-efi-boot-entries " if spec["firmware"] == "uefi" else ""
    luks = spec[NAME]["layout"] == "ext4-luks"
    passphrase: List[str] = []
    guard: List[str] = []
    local_copy: List[str] = []
    if spec[NAME]["local_module"]:
        # Not guarded by pathExists: configuration.nix requires the file, so a missing one fails
        # here as well. Only the copy in the store (storeRoot) has no ../ to read.
        local_copy = [
            "      + lib.optionalString (!storeRoot) (",
            f"        assert lib.assertMsg (builtins.pathExists ../{LOCAL_FILE})",
            f'          "local_module = true: create ../{LOCAL_FILE} next to spec.toml and git add it";',
            "        ''",
            f'          cp ${{../{LOCAL_FILE}}} "$out/{LOCAL_COPY}"',
            "        ''",
            "      )",
        ]
    inputs = "pkgs.coreutils pkgs.util-linux disko"
    if luks:
        inputs += " pkgs.lvm2 pkgs.cryptsetup"
        passphrase = ['      echo "install-$host: disko will now ask for the new disk passphrase twice"']
        guard = _LUKS_GUARD
    return "\n".join(
        [
            HEADER,
            "# USB installer: the NixOS minimal ISO plus an offline copy of the target system and an",
            f"# install-{host} command. Nothing runs at boot; a human starts the install.",
            "#",
            "# Offline closure: the pattern of disko's docs/disko-install.md (\"Using disko-install in an",
            f"# offline installer\") at tag {DISKO_REF} (commit {DISKO_REF_COMMIT}),",
            "# Only outputs of the target system are copied, never its build closure: disko-install",
            "# re-evaluates the same flake with the same disk, which yields the same derivations, so",
            "# nothing of the system is rebuilt on the stick.",
            "{ config, lib, pkgs, modulesPath, self, ... }:",
            "let",
            f"  hostName = {nix_str(host)};",
            f"  disk = {nix_str(disk)};",
            f"  target = {target};",
            "  disko = self.inputs.disko.packages.${pkgs.stdenv.hostPlatform.system}.disko;",
            "  # As in configuration.nix: no ../ when this file sits directly in the store.",
            "  storeRoot = dirOf (toString ./.) == builtins.storeDir;",
            "",
            "  # The flake disko-install evaluates on the stick. disko-install copies only the flake",
            "  # directory (a ?dir= flake ref loses its dir there), so ../hardware-configuration.nix",
            f"  # is copied in as ./{HW_COPY},",
            "  # which configuration.nix checks first.",
            *([f"  # local_module: ../{LOCAL_FILE} is copied in as ./{LOCAL_COPY} the same way."] if local_copy else []),
            "  # Without a git-tracked flake.lock, disko-install would try to lock the inputs offline,",
            "  # after the wipe is confirmed: refuse to build the installer instead.",
            "  installFlake =",
            "    assert lib.assertMsg (builtins.pathExists ./flake.lock)",
            '      "commit rendered/flake.lock (nix flake lock && git add flake.lock) before building the installer";',
            '    pkgs.runCommand "install-flake-${hostName}" { } (',
            "      ''",
            '        mkdir -p "$out"',
            '        cp ${./flake.nix} "$out/flake.nix"',
            '        cp ${./configuration.nix} "$out/configuration.nix"',
            '        cp ${./disko.nix} "$out/disko.nix"',
            '        cp ${./installer.nix} "$out/installer.nix"',
            '        cp ${./flake.lock} "$out/flake.lock"',
            "      ''",
            "      + lib.optionalString (!storeRoot && builtins.pathExists ../hardware-configuration.nix) ''",
            f'        cp ${{../hardware-configuration.nix}} "$out/{HW_COPY}"',
            "      ''",
            *local_copy,
            "    );",
            "",
            "  # Every flake input (recursively), so evaluating the flake needs no network.",
            "  flakeOutPaths =",
            "    let",
            "      collector = parent:",
            "        map (child: [ child.outPath ] ++ (if child ? inputs && child.inputs != { } then collector child else [ ]))",
            "          (lib.attrValues parent.inputs);",
            "    in",
            "    lib.unique (lib.flatten (collector self));",
            "",
            "  dependencies = [",
            "    target.config.system.build.toplevel",
            "    target.config.system.build.diskoScript",
            "    target.config.system.build.diskoScript.drvPath",
            "    target.pkgs.stdenv.drvPath",
            "    target.pkgs.perlPackages.ConfigIniFiles",
            "    target.pkgs.perlPackages.FileSlurp",
            "    (target.pkgs.closureInfo { rootPaths = [ ]; }).drvPath",
            "    installFlake",
            "  ] ++ flakeOutPaths;",
            "",
            "  closureInfo = pkgs.closureInfo { rootPaths = dependencies; };",
            "",
            "  install = pkgs.writeShellApplication {",
            '    name = "install-${hostName}";',
            f"    runtimeInputs = [ {inputs} ];",
            "    text = ''",
            "      disk=${lib.escapeShellArg disk}",
            "      host=${lib.escapeShellArg hostName}",
            "      # Every probe is captured on its own line with an explicit refusal: a failing lsblk",
            "      # (or a pipe cut short) must stop the install, never let it through.",
            '      refuse() { echo "install-$host: $*; refusing, nothing was changed" >&2; exit 1; }',
            '      if [ ! -t 0 ]; then',
            '        echo "install-$host: run it on a terminal; the confirmation must be typed" >&2',
            "        exit 1",
            "      fi",
            '      uid=$(id -u) || refuse "cannot read the user id"',
            '      if [ "$uid" != "0" ]; then',
            '        echo "install-$host: run it as root: sudo install-$host" >&2',
            "        exit 1",
            "      fi",
            '      if [ ! -b "$disk" ]; then',
            '        echo "install-$host: disk $disk not found; nothing was changed. Disks present:" >&2',
            "        ls -l /dev/disk/by-id/ >&2 || true",
            "        exit 1",
            "      fi",
            '      dev=$(readlink -f "$disk") || refuse "cannot resolve $disk"',
            '      type=$(lsblk -dno TYPE "$dev") || refuse "cannot read the type of $dev"',
            '      if [ "$type" != "disk" ]; then',
            '        refuse "$disk ($dev) is a $type, not a whole disk"',
            "      fi",
            "      # Anything in the disk's tree mounted or used as swap (partitions, dm, loop, md).",
            '      mounts=$(lsblk -nro MOUNTPOINTS "$dev") || refuse "cannot read the mounts on $dev"',
            '      mounts=$(printf %s "$mounts" | tr -d "[:space:]")',
            '      if [ -n "$mounts" ]; then',
            '        lsblk -o NAME,TYPE,MOUNTPOINTS "$dev" >&2 || true',
            '        refuse "$disk ($dev) or a device on it is mounted or used as swap"',
            "      fi",
            "      # Best effort: the installer medium is mounted at /iso (iso-image.nix); every disk it",
            "      # sits on (through partitions, dm or md) must differ from the target. Without a block",
            "      # device there (copytoram, netboot) nothing is compared; the mounted-disk check above",
            "      # and the typed serial still apply.",
            '      medium=""',
            "      if mountpoint -q /iso; then",
            '        medium=$(findmnt -no SOURCE /iso) || refuse "cannot read the source of /iso"',
            "      fi",
            '      if [ -n "$medium" ] && [ -b "$medium" ]; then',
            '        ancestors=$(lsblk -nrpso NAME,TYPE "$medium") || refuse "cannot read the devices the boot medium $medium sits on"',
            "        while read -r name kind; do",
            '          if [ "$kind" = "disk" ]; then',
            '            r=$(readlink -f "$name") || refuse "cannot resolve $name"',
            '            if [ "$r" = "$dev" ]; then',
            '              refuse "$disk ($dev) is the USB stick this installer booted from"',
            "            fi",
            "          fi",
            '        done <<< "$ancestors"',
            "      else",
            '        echo "install-$host: no block device at /iso; the boot medium was not compared" >&2',
            "      fi",
        ]
        + _PARTLABEL_GUARD
        + guard
        + [
            '      serial=$(lsblk -dno SERIAL "$dev") || refuse "cannot read the serial of $dev"',
            '      serial=$(printf %s "$serial" | tr -d "[:space:]")',
            '      if [ "$(printf %s "$serial" | wc -c)" -ge 4 ]; then',
            '        expect=$(printf %s "$serial" | tail -c 4)',
            '        what="the last 4 characters of the disk serial ($serial)"',
            "      else",
            '        expect="$host"',
            '        what="the hostname ($host; the disk reports no serial)"',
            "      fi",
            '      echo "WARNING: this ERASES ALL DATA on $disk ($dev) to install NixOS host $host:"',
            '      lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS "$dev"',
            '      shown="$serial"',
            '      if [ -z "$shown" ]; then shown="none"; fi',
            '      printf "Host: %s  Disk serial: %s\\nType %s to wipe this disk: " "$host" "$shown" "$what"',
            '      answer=""',
            "      read -r answer || true",
            '      if [ "$answer" != "$expect" ]; then',
            '        echo "install-$host: confirmation did not match; aborted, nothing was changed" >&2',
            "        exit 1",
            "      fi",
        ]
        + passphrase
        + [
            f'      exec disko-install {efi}--flake ${{lib.escapeShellArg "${{installFlake}}#${{hostName}}"}} --disk main "$disk"',
            "    '';",
            "  };",
            "in",
            "{",
            '  imports = [ (modulesPath + "/installer/cd-dvd/installation-cd-minimal.nix") ];',
            "",
            "  # Pulls the whole offline closure onto the ISO.",
            '  environment.etc."install-closure".source = "${closureInfo}/store-paths";',
            "  environment.systemPackages = [ install ];",
            '  nix.settings.experimental-features = [ "nix-command" "flakes" ];',
            "  # The live ISO never imports a ZFS root; the 26.11 default, set early to silence the warning.",
            "  boot.zfs.forceImportRoot = false;",
            "}",
            "",
        ]
    )


def render(spec: Dict[str, Any], ctx: Any) -> Dict[str, str]:
    """Return the flake files (relative path -> text)."""
    return {
        "configuration.nix": configuration_nix(spec),
        "disko.nix": disko_nix(spec),
        "flake.nix": flake_nix(spec),
        "installer.nix": installer_nix(spec),
    }


# --- contract: validate -------------------------------------------------------------------


def _read(path: Path, findings: List[Finding]) -> Optional[str]:
    """Text of a regular file, or None with a finding (missing, symlink/special, not UTF-8)."""
    try:
        return read_regular_text(path)
    except FileNotFoundError:
        findings.append(Finding(ERROR, "missing; run prov render", path.name))
    except NotRegularFileError:
        findings.append(Finding(ERROR, "not a regular file (symlink or special file); not read", path.name))
    except UnicodeDecodeError as exc:
        findings.append(Finding(ERROR, f"not valid UTF-8: {exc}", path.name))
    except OSError as exc:
        findings.append(Finding(ERROR, f"cannot read: {exc.strerror or exc}", path.name))
    return None


# (file, required substring, what is missing)
_REQUIRED = [
    ("flake.nix", "inputs.nixpkgs.follows", "disko.inputs.nixpkgs.follows"),
    ("flake.nix", "disko.nixosModules.disko", "the disko NixOS module"),
    ("flake.nix", "./configuration.nix", "an import of ./configuration.nix"),
    ("flake.nix", "./disko.nix", "an import of ./disko.nix"),
    ("flake.nix", "./installer.nix", "an import of ./installer.nix"),
    ("configuration.nix", "!storeRoot && builtins.pathExists ../hardware-configuration.nix", "the guarded hardware-configuration.nix import"),
    ("configuration.nix", "networking.hostName = ", "networking.hostName"),
    ("configuration.nix", "services.openssh", "services.openssh"),
    ("configuration.nix", "system.stateVersion = ", "system.stateVersion"),
    ("disko.nix", "disko.devices.disk.main", "disko.devices.disk.main"),
    ("disko.nix", 'mountpoint = "/";', "a root filesystem"),
    ("installer.nix", "installation-cd-minimal.nix", "the installation-cd-minimal.nix base"),
    ("installer.nix", "closureInfo", "the offline closure (closureInfo)"),
    ("installer.nix", 'if [ "$answer" != "$expect" ]; then', "the typed confirmation"),
    ("installer.nix", 'if [ "$type" != "disk" ]; then', "the whole-disk check"),
    ("installer.nix", 'ancestors=$(lsblk -nrpso NAME,TYPE "$medium") || refuse', "the installer-medium check"),
    ("installer.nix", 'mounts=$(lsblk -nro MOUNTPOINTS "$dev") || refuse', "the mounted-disk check"),
    ("installer.nix", "if [ ! -t 0 ]; then", "the terminal check"),
    ("installer.nix", "builtins.pathExists ./flake.lock", "the flake.lock assertion"),
    ("installer.nix", "disko-install ", "the disko-install call"),
]

_STRING = r'"((?:[^"\\]|\\.)*)"'

# A rendered ext4-luks disko.nix: the LUKS container on the spec disk holding the volume group.
_LUKS_REQUIRED = [
    ('type = "luks";', "the luks partition"),
    (f"name = {nix_str(LUKS_NAME)};", f"the LUKS name {LUKS_NAME}"),
    (f'content = {{ type = "lvm_pv"; vg = {nix_str(VG_NAME)}; }};', f"the LVM physical volume of volume group {VG_NAME}"),
    (f"disko.devices.lvm_vg.{VG_NAME} = {{", f"disko.devices.lvm_vg.{VG_NAME}"),
    ('type = "lvm_vg";', "the lvm_vg type"),
]
# The install script's refusal of a VG / LUKS mapping another disk already provides.
_LUKS_GUARD_REQUIRED = [
    ('vgnames=$(vgs --noheadings -o vg_name) || refuse', "the existing-VG check (vgs)"),
    (f'if [ "$vg" = {VG_NAME} ]; then', f"the VG {VG_NAME} comparison"),
    (f"cryptsetup status {LUKS_NAME} >/dev/null 2>&1 || crypt_rc=$?", f"the {LUKS_NAME} status probe"),
    (f'if [ -e /dev/mapper/{LUKS_NAME} ] || [ "$crypt_rc" = 0 ]; then', f"the open-{LUKS_NAME} check"),
    ('elif [ "$crypt_rc" != 4 ]; then', f"the fail-closed {LUKS_NAME} status check"),
]
# Every layout: the foreign disk-main-* partition label refusal.
_PARTLABEL_GUARD_REQUIRED = [
    ('parts=$(lsblk -nrpo NAME,PARTLABEL) || refuse', "the partition-label scan"),
    ("          disk-main-*)", "the disk-main-* label match"),
    ('owners=$(lsblk -nrpso NAME,TYPE "$pname") || refuse', "the label's disk lookup"),
    ('if [ "$o" != "$dev" ]; then', "the foreign-disk label refusal"),
    ('if [ "$on_target" != 1 ]; then', "the on-target label requirement"),
]
# Secrets that must never be rendered: disko/NixOS key and password file options and plain
# password attributes (hashedPassword stays allowed), as an attribute: at the start of a
# line or after { or ;, with an optional dotted prefix (settings.keyFile =), as the final
# attribute assigned a value. A name prov renders as an attribute set (users.users.password
# = {, an LV named password = {) is not a value. String literals and comments are blanked
# first, so a gecos or comment that says "password = x" is not one.
_SECRET = re.compile(
    r"(?:^|[{;])[ \t]*(?:[A-Za-z_][A-Za-z0-9_'-]*\.)*(passwordFile|keyFile|additionalKeyFiles|initialPassword|password)[ \t]*=(?!=)(?![ \t]*\{)",
    re.M,
)


def _guard_findings(inst: str, required: List[Any], prefix: str) -> List[Finding]:
    """Each guard line must be present and come before the disko-install call."""
    out: List[Finding] = []
    call = inst.find("exec disko-install")
    for needle, what in required:
        pos = inst.find(needle)
        if pos < 0:
            out.append(Finding(ERROR, f"{prefix}{what} is missing", "installer.nix"))
        elif call >= 0 and pos > call:
            out.append(Finding(ERROR, f"{prefix}{what} comes after exec disko-install", "installer.nix"))
    return out


# Network credentials that must never be rendered (NetworkManager connections are made on the
# machine): wpa_supplicant networks (networking.wireless.networks.<ssid>.psk / pskRaw) and its
# secretsFile, and NetworkManager's declarative ensureProfiles. Matched on attribute paths, so
# a user, volume or package that happens to be named psk is not one (see _net_secret).
_NIX_ATTR = r'(?:[A-Za-z_][A-Za-z0-9_\'-]*|"")'  # string literals are blanked to "" first
_NIX_TOKEN = re.compile(
    rf"(?P<open>\{{)|(?P<close>\}})"
    rf"|(?:(?<=[{{;\n])|^)[ \t]*(?P<path>{_NIX_ATTR}(?:\.{_NIX_ATTR})*)[ \t]*=(?!=)(?P<brace>[ \t]*\{{)?",
    re.M,
)
# systemd-networkd as an attribute (line start or after { or ;): boot.initrd.systemd.network
# (the initrd's own networkd) does not count.
_NETWORKD = re.compile(r"(?:^|[{;])[ \t]*(?:networking\.useNetworkd[ \t]*=[ \t]*true\b|systemd\.network\.)", re.M)
_NETWORKMANAGER = re.compile(r"\bnetworking\.networkmanager\.enable[ \t]*=[ \t]*true\b")
_GNOME = re.compile(r"\bservices\.desktopManager\.gnome\.enable[ \t]*=[ \t]*true\b")
_GDM = re.compile(r"\bservices\.displayManager\.gdm\.enable[ \t]*=[ \t]*true\b")
# Names NixOS 25.11 renamed (services.xserver.{desktop,display}Manager.{gnome,gdm} and friends).
_OLD_DESKTOP = re.compile(r"\bservices\.xserver\.(desktopManager\.gnome|displayManager\.gdm)\b")


def _code(text: str) -> str:
    """Nix ``text`` with double-quoted string literals blanked and comments removed."""
    code = re.sub(_STRING, '""', text)
    return re.sub(r"#[^\n]*", "", code)


def _secret(text: str) -> Optional[str]:
    """The first secret attribute name assigned in Nix ``text``, or None."""
    m = _SECRET.search(_code(text))
    return m.group(1) if m else None


def _net_secret_path(path: List[str]) -> Optional[str]:
    """The credential option a full attribute path sets, or None."""
    for a, b in zip(path, path[1:]):
        if a == "wireless" and b in ("networks", "secretsFile"):
            return f"wireless.{b}"
    if "ensureProfiles" in path:
        return "ensureProfiles"
    if path[-1] in ("psk", "pskRaw") and "wireless" in path:
        return path[-1]
    return None


def _net_secret(text: str) -> Optional[str]:
    """The first network-credential option assigned in Nix ``text`` (code only), or None.

    Tracks nested attribute sets (``networking.wireless = { networks = ...; }``,
    ``networkmanager = { ensureProfiles = ...; }``) by brace depth: each ``{`` opened right
    after ``path =`` contributes ``path`` to the paths assigned inside it; other braces
    (function arguments, list items) contribute nothing. A best-effort scanner, not a Nix
    parser: ``rec``/``let`` sets inside attribute values and ``with`` scopes are not followed,
    nor are sets passed to a function (``lib.mkIf c { ... }``, ``lib.mkMerge [ ... ]``,
    ``lib.mkDefault { ... }``): their ``{`` does not follow ``path =`` directly, so the path
    assigned before the call is not carried into them.
    """
    stack: List[List[str]] = []
    for m in _NIX_TOKEN.finditer(_code(text)):
        if m.group("open"):
            stack.append([])
        elif m.group("close"):
            if stack:
                stack.pop()
        else:
            path = m.group("path").split(".")
            full = [p for frame in stack for p in frame] + path
            hit = _net_secret_path(full)
            if hit:
                return hit
            if m.group("brace"):
                stack.append(path)
    return None


def _string_value(text: str, key: str) -> Optional[str]:
    """The (unescaped) string assigned to ``key`` at the start of a line, or None."""
    m = re.search(rf"^\s*{re.escape(key)} = {_STRING};", text, re.M)
    if m is None:
        return None
    return re.sub(r"\\(.)", lambda c: {"n": "\n", "r": "\r", "t": "\t"}.get(c.group(1), c.group(1)), m.group(1))


def _external(paths: List[Path]) -> List[Finding]:
    """``nix-instantiate --parse`` on each file (parse only: no evaluation, no fetching).

    ``--store dummy://`` keeps it away from any real store; a failure whose output does not
    name the file (the tool itself broken, not a syntax error) is reported as skipped.
    """
    tool = shutil.which("nix-instantiate")
    if tool is None:
        return [Finding(SKIPPED, "nix not installed (nix-instantiate --parse)")]
    out: List[Finding] = []
    for path in paths:
        try:
            proc = subprocess.run(
                [tool, "--readonly-mode", "--store", "dummy://", "--parse", str(path)],
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                errors="replace",
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            out.append(Finding(ERROR, "nix-instantiate --parse timed out after 60 s", path.name))
            continue
        except OSError as exc:
            out.append(Finding(ERROR, f"nix-instantiate could not run: {exc.strerror or exc}", path.name))
            continue
        if proc.returncode != 0:
            detail = " | ".join(line.strip() for line in proc.stderr.strip().splitlines()[-5:])
            if str(path) in proc.stderr:
                out.append(Finding(ERROR, "nix-instantiate --parse: " + esc(detail), path.name))
            else:
                out.append(Finding(SKIPPED, "nix-instantiate --parse could not run: " + esc(detail), path.name))
    return out


def _validate_desktop(code: str) -> List[Finding]:
    """configuration.nix (code only): one network stack; GNOME with GDM and NetworkManager."""
    out: List[Finding] = []
    networkd, nm = bool(_NETWORKD.search(code)), bool(_NETWORKMANAGER.search(code))
    if networkd and nm:
        out.append(Finding(ERROR, "enables both systemd-networkd and NetworkManager; use exactly one", "configuration.nix"))
    elif not networkd and not nm:
        out.append(Finding(ERROR, "enables neither systemd-networkd nor NetworkManager", "configuration.nix"))
    if _GNOME.search(code):
        if not _GDM.search(code):
            out.append(Finding(ERROR, "GNOME is enabled without services.displayManager.gdm.enable", "configuration.nix"))
        if not nm:
            out.append(Finding(ERROR, "GNOME is enabled without NetworkManager", "configuration.nix"))
    old = _OLD_DESKTOP.search(code)
    if old:
        out.append(
            Finding(ERROR, f"services.xserver.{old.group(1)} is the pre-25.11 name; use services.{old.group(1)} (NixOS 25.11 and later)", "configuration.nix")
        )
    return out


def validate(rendered_dir: Path) -> List[Finding]:
    """Check the rendered flake: files, key attributes, cross-file consistency, optional parse."""
    findings: List[Finding] = []
    rendered_dir = Path(rendered_dir)
    texts = {name: _read(rendered_dir / name, findings) for name in FILES}
    for name, needle, what in _REQUIRED:
        text = texts[name]
        if text is not None and needle not in text:
            findings.append(Finding(ERROR, f"{what} is missing", name))

    flake, conf, disko, inst = (texts[n] for n in ("flake.nix", "configuration.nix", "disko.nix", "installer.nix"))
    if flake is not None:
        attrs = re.findall(rf"^\s*nixosConfigurations\.(?:{_STRING}|([A-Za-z_][A-Za-z0-9_'-]*)) = ", flake, re.M)
        names = [quoted or bare for quoted, bare in attrs]
        targets = [n for n in names if f"{n}-installer" in names]
        if len(names) != 2 or len(targets) != 1:
            findings.append(Finding(ERROR, "needs nixosConfigurations.<host> and nixosConfigurations.<host>-installer", "flake.nix"))
        else:
            host = targets[0]
            if conf is not None and _string_value(conf, "networking.hostName") != host:
                findings.append(Finding(ERROR, f"networking.hostName differs from the flake's nixosConfigurations.{esc(host)}", "configuration.nix"))
            if inst is not None:
                if _string_value(inst, "hostName") != host:
                    findings.append(Finding(ERROR, f"hostName differs from the flake's nixosConfigurations.{esc(host)}", "installer.nix"))
                if f"target = self.nixosConfigurations.{nix_attr(host)};" not in inst:
                    findings.append(Finding(ERROR, f"does not take its offline closure from self.nixosConfigurations.{esc(host)}", "installer.nix"))
    if conf is not None and "boot.loader.systemd-boot.enable = true;" not in conf and "boot.loader.grub.enable = true;" not in conf:
        findings.append(Finding(ERROR, "no boot loader (systemd-boot or GRUB) is enabled", "configuration.nix"))
    if conf is not None:
        findings += _validate_desktop(_code(conf))
    if disko is not None and inst is not None:
        device, disk = _string_value(disko, "device"), _string_value(inst, "disk")
        if device is None:
            findings.append(Finding(ERROR, "disko.devices.disk.main.device is missing", "disko.nix"))
        elif disk != device:
            findings.append(Finding(ERROR, f"installs to {short_repr(disk)} but disko.nix partitions {short_repr(device)}", "installer.nix"))
    if inst is not None and re.search(r"systemd\.(user\.)?services|boot\.postBootCommands|services\.getty\.autologinUser", inst):
        findings.append(Finding(ERROR, "the installer must not run anything at boot", "installer.nix"))
    if disko is not None and re.search(r'type = "(luks|lvm_pv|lvm_vg)";', disko):
        for needle, what in _LUKS_REQUIRED:
            if needle not in disko:
                findings.append(Finding(ERROR, f"layout ext4-luks: {what} is missing", "disko.nix"))
        if 'type = "EF02";' in disko or 'type = "EF00";' not in disko:
            findings.append(Finding(ERROR, "layout ext4-luks needs an EF00 ESP and no EF02 BIOS boot partition (UEFI only)", "disko.nix"))
        if inst is not None:
            if "pkgs.lvm2 pkgs.cryptsetup" not in inst:
                findings.append(Finding(ERROR, "layout ext4-luks: lvm2 and cryptsetup in runtimeInputs is missing", "installer.nix"))
            findings += _guard_findings(inst, _LUKS_GUARD_REQUIRED, "layout ext4-luks: ")
    if inst is not None:
        findings += _guard_findings(inst, _PARTLABEL_GUARD_REQUIRED, "")
    for name in FILES:
        text = texts[name]
        secret = _secret(text) if text is not None else None
        if secret:
            findings.append(Finding(ERROR, f"{secret} =: no key file, password file or password may be rendered", name))
        net = _net_secret(text) if text is not None else None
        if net:
            findings.append(Finding(ERROR, f"{net}: no network credentials may be rendered (connections are made on the machine)", name))

    lock_path = rendered_dir / "flake.lock"
    if lock_path.exists() or lock_path.is_symlink():
        lock = _read(lock_path, findings)
        if lock is not None:
            try:
                data = json.loads(lock)
            except ValueError as exc:
                findings.append(Finding(ERROR, f"invalid JSON: {esc(str(exc))}", "flake.lock"))
            else:
                if not (isinstance(data, dict) and isinstance(data.get("nodes"), dict)):
                    findings.append(Finding(ERROR, "not a flake lock file (no 'nodes')", "flake.lock"))

    if not any(f.level == ERROR for f in findings):
        findings += _external([rendered_dir / name for name in FILES])
    return findings


# --- contract: plan -----------------------------------------------------------------------


def plan(spec: Dict[str, Any], ctx: Any) -> List[str]:
    """Lock, ISO, USB and install steps for a human, relative to the rendered directory.

    Every spec value is shell-quoted, in comments too, so a copied line never splits.
    """
    q = shlex.quote
    nixos = spec[NAME]
    luks = nixos["layout"] == "ext4-luks"
    host = spec["hostname"]
    disk = spec["storage"]["disks"][0]
    disks = " ".join(q(d) for d in spec["storage"]["disks"])
    installer = f".#nixosConfigurations.{q(host)}-installer.config.system.build.isoImage"
    mode = "UEFI" if spec["firmware"] == "uefi" else "legacy BIOS"
    login = next((u["name"] for u in spec["users"] if u["ssh_authorized_keys"]), None)
    address = next((a.split("/")[0] for i in spec.get("network", {}).get("interfaces", []) for a in i["addresses"]), None)
    nm = nixos["networking"] == "networkmanager"
    gnome = nixos["desktop"] == "gnome"
    # The ISO's out-link (its GC root) lives outside the hosts repository and outside every
    # prov-owned directory: render would delete a link in rendered/, and validate/diff flag it.
    link = f'"$HOME"/prov-iso/{q(f"{host}-installer")}'
    lines = [
        "",
        "# Static validation only: prov has not installed this configuration on hardware.",
        "# Nix copies only git-tracked files of a flake: keep this item in a git repository and",
        "# git add the rendered files, flake.lock"
        + (f", ../{LOCAL_FILE}" if nixos["local_module"] else "")
        + " and ../hardware-configuration.nix before nix runs.",
        "",
        "# 1. Optional, recommended: capture the target's hardware configuration. ON THE TARGET,",
        "#    booted from the stock NixOS minimal ISO, run:",
        "#        nixos-generate-config --no-filesystems --show-hardware-config > hardware-configuration.nix",
        "#    and copy the file to this machine NEXT TO spec.toml (not into rendered/, which render",
        "#    owns): ../hardware-configuration.nix from here. Then:",
        "if [ -f ../hardware-configuration.nix ]; then git add ../hardware-configuration.nix; fi",
        "#    configuration.nix imports it when present; without it a broad initrd driver set is used.",
        "",
        "# 2. Track the rendered files, lock the flake inputs and commit the lock",
        "#    (render keeps rendered/flake.lock):",
        *([f"#    local_module: write ../{LOCAL_FILE} next to spec.toml first (see the backend docs)."] if nixos["local_module"] else []),
        "git add . ../spec.toml" + (f" ../{LOCAL_FILE}" if nixos["local_module"] else ""),
        "nix --extra-experimental-features 'nix-command flakes' flake lock",
        f"git add flake.lock && git commit -m {q(f'Lock the {host} flake inputs')}",
        "",
        "# 3. Build the installer ISO (large: it carries the target closure for an offline install).",
        "#    The out-link under ~/prov-iso keeps the ISO from garbage collection; delete the link",
        "#    when you no longer need the ISO.",
    ]
    if gnome:
        lines += [
            "#    desktop gnome: the ISO carries the whole desktop closure, likely several GB more than a",
            "#    server install (not measured): a longer build, more store space; use a 16 GB+ stick.",
        ]
    lines += [
        'mkdir -p "$HOME"/prov-iso',
        f"nix --extra-experimental-features 'nix-command flakes' build {installer} --out-link {link}",
        f"ls {link}/iso/",
        "",
        "# 4. Write the ISO to a USB stick. WARNING: this WIPES the stick. Find its by-id path with",
        "#    ls -l /dev/disk/by-id/ | grep usb   and set USB to it below (double-check it).",
        "#    Unmount the stick's partitions first (desktops often automount them). A GUI writer",
        "#    (GNOME Disks, balenaEtcher, Rufus in DD mode) is safer than dd.",
        f"ISO=$(echo {link}/iso/*.iso)",
        "USB=/dev/disk/by-id/usb-CHANGE-ME",
        'case "$USB" in *-part[0-9]*) echo "USB=$USB is a partition; use the whole stick" >&2 ;; '
        '*) if [ -b "$USB" ]; then sudo dd if="$ISO" of="$USB" bs=4M conv=fsync status=progress; '
        'else echo "USB=$USB is not a block device; set USB first" >&2; fi ;; esac',
        "#    WSL2 has no USB block devices: copy the ISO to Windows instead, e.g.",
        '#        cp -L "$ISO" /mnt/c/Users/CHANGE-ME/Downloads/',
        "#    and write it there with Rufus (DD image mode) or balenaEtcher.",
        "",
        f"# 5. WARNING: the install WIPES {disks} on the target machine.",
    ]
    if spec["firmware"] == "uefi":
        lines.append("#    Disable Secure Boot in the firmware setup first: neither the installer stick nor systemd-boot is signed.")
    lines += [
        f"#    Boot the target from the USB stick in {mode} mode, then run on its console:",
    ]
    if luks:
        lines += [
            "#    ext4-luks: after the serial, disko asks TWICE for a NEW disk passphrase. Store it in a",
            "#    password manager first: it cannot be recovered, and it is typed at every boot.",
            "#    If the two entries differ, disko exits after the disk was already wiped and",
            f"#    partitioned: run sudo {q(f'install-{host}')} again.",
            "#    The fixed sizes (swap, root, volumes) must fit the disk. A failure after the LUKS format",
            f"#    leaves {LUKS_NAME} open; clean up with   vgchange -an {VG_NAME}   then   cryptsetup close {LUKS_NAME}",
            f"#    (in that order) before retrying. It also refuses while an LVM volume group {VG_NAME} exists",
            f"#    or /dev/mapper/{LUKS_NAME} is open: on another disk, unplug it or vgrename {VG_NAME} <other>.",
        ]
    lines += [
        f"sudo {q(f'install-{host}')}",
        f"#    It stops unless {q(disk)} is a whole disk, has nothing mounted or used as swap,",
        "#    is not the USB stick it booted from, and the command runs on a terminal. It shows",
        "#    the disk, asks for the last 4 characters of its serial (or the hostname when the",
        "#    disk reports none), then runs disko-install offline. Any other answer aborts.",
        "#    It also refuses while another disk carries a disko-install partition label (disk-main-*),",
        "#    which could capture /boot or / at install or boot time: unplug that disk, or wipe its",
        "#    partition table yourself (wipefs -a or sgdisk --zap-all on THAT disk).",
        "",
        "# 6. Post-install checks: remove the stick, reboot into the installed system, then",
    ]
    if nm:
        lines += [
            "#    on the TARGET console join a network with   sudo nmtui"
            + ("   (Ctrl+Alt+F3 for a text console, or use GNOME Settings)" if gnome else "")
            + ";",
            "#    network credentials stay on the machine, never in spec.toml. Still on the target console:",
            "nmcli device status",
            "#    then from this machine:",
        ]
    if login and address:
        lines.append(f"ssh {q(login)}@{q(address)}")
    elif login:
        lines += ["HOST_IP=CHANGE-ME", f'ssh {q(login)}@"$HOST_IP"']
    else:
        lines.append("#    log in on the console (no user has an ssh key).")
    mounts = ["/", "/boot"] if spec["firmware"] == "uefi" else ["/"]
    mounts += [v["mountpoint"] for v in nixos["volumes"]]
    lines += [
        "#    and on the installed host:",
        "nixos-version",
        "systemctl --failed",
    ]
    lines += [f"findmnt {q(m)}" for m in mounts]
    lines += [
        "swapon --show",
        "ip -br addr",
    ]
    if gnome:
        lines.append("systemctl is-active display-manager")
    if luks:
        lines += [f"sudo cryptsetup status {LUKS_NAME}", "sudo lvs"]
    if nixos["hibernate"]:
        lines += [
            "#    hibernate: the swap device must hold the RAM contents, so swap_size should be at least",
            f"#    the RAM size ({q(nixos.get('swap_size', ''))} here). The kernel's hibernation mode:",
            "cat /sys/power/disk",
            "#    Then test it once with   systemctl hibernate   and check the session comes back.",
        ]
    lines += [
        "",
        "# 7. Later changes need no new ISO: edit spec.toml"
        + (f" or ../{LOCAL_FILE}" if nixos["local_module"] else "")
        + ", prov render, commit, then ON THE HOST,",
        "#    in a git checkout of this repository, from this rendered/ directory, as your user",
        "#    (--sudo evaluates as you and activates as root; Nix may refuse a checkout owned by",
        "#    another user when it runs as root). Not yet run on an installed host:",
        f"#        git pull && nixos-rebuild switch --flake .#{q(host)} --sudo",
        "#    Each switch adds a boot menu entry to roll back to.",
    ]
    return lines


# --- optional: starter --------------------------------------------------------------------

STARTER = """\
# prov spec (os-install, nixos-disko). Synthetic starter values: replace them before real use.
# Documentation addresses only (RFC 5737 / RFC 3849); example.com is reserved (RFC 2606).
api_version = "prov/v1alpha1"
kind = "os-install"
backend = "nixos-disko"
hostname = "{hostname}"
domain = "example.com"
arch = "amd64"
firmware = "uefi"
timezone = "Etc/UTC"
locale = "en_US.UTF-8"
keyboard = "us"

[[users]]
name = "ops"
gecos = "Operator"
sudo = true
# Identity is ssh-key based; no password is set. Replace with your real public key.
ssh_authorized_keys = [
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE ops@example.com",
]

[network.dns]
nameservers = ["192.0.2.53", "2001:db8::53"]
search = ["example.com"]

[[network.interfaces]]
name = "eth0"
# Locally administered synthetic MAC: replace it with the NIC's real address.
macaddress = "02:00:00:00:00:01"
addresses = ["192.0.2.10/24", "2001:db8::10/64"]
gateway4 = "192.0.2.1"
gateway6 = "2001:db8::1"

[storage]
# The one disk disko-install wipes. Use the stable /dev/disk/by-id/ path of the target disk.
disks = ["/dev/disk/by-id/nvme-EXAMPLE_SSD_SERIAL_0000000000"]

[nixos-disko]
release = "nixos-26.05"
state_version = "26.05"
layout = "ext4"
swap_size = "8G"
packages = ["git", "vim"]
# Optional (defaults shown or described):
# esp_size = "512M"            # EFI system partition (UEFI), at least 256M
# hibernate = false            # resume from swap; needs swap_size of at least the RAM size
# Encrypted disk (UEFI only): layout = "ext4-luks" puts LVM inside LUKS; the passphrase is
# typed at install time and at every boot. Then, exactly one of root_size and the volumes'
# size is "100%FREE":
# root_size = "64G"            # default "100%FREE"
# [[nixos-disko.volumes]]
# name = "home"
# mountpoint = "/home"
# size = "100%FREE"
# mount_options = ["noatime"]  # optional
# owner = "ops"                # optional: a [[users]] name; owns the mount point (0750)
# networking = "networkd"      # or "networkmanager": connections (Wi-Fi) made on the machine
#                              # with nmtui/nmcli; then remove [network] above
# desktop = "none"             # or "gnome" (GNOME + GDM): needs networking = "networkmanager"
# local_module = false         # true: import the hand-written ../local.nix next to this file
#                              # (unfree packages, services, programs.*); git add it
# [nixos-disko.inputs]         # extra flakes for local.nix (inputs.<name>), pinned to a tag
# tool = "github:example/tool/v1.2.3"
"""


def starter(hostname: str) -> str:
    """A valid synthetic spec.toml for ``prov init os-install/<name> --backend nixos-disko``."""
    return STARTER.format(hostname=hostname)
