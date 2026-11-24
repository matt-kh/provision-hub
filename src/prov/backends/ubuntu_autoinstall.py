"""Ubuntu Server autoinstall (subiquity): NoCloud seed for an os-install spec.

Renders a NoCloud seed: ``user-data`` (``#cloud-config`` with ``autoinstall:`` version 1,
explicit curtin storage actions, netplan v2 network, ssh-key identity through the
``user-data`` section), ``meta-data`` and ``kernel-cmdline`` (``autoinstall ds=nocloud``).
"""

from __future__ import annotations

import copy
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import yaml

from ..text import esc
from ..util import NotRegularFileError, dump_yaml, read_regular_text
from ..validate import ERROR, SKIPPED, WARNING, Finding

NAME = "ubuntu-autoinstall"
KIND = "os-install"
PLUGIN_API = 1

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Ubuntu Server autoinstall settings.",
    "additionalProperties": False,
    "required": ["release", "storage"],
    "properties": {
        "release": {"enum": ["22.04", "24.04"], "description": "LTS release of the live-server ISO."},
        "packages": {
            "type": "array",
            "description": "Extra packages installed by autoinstall.",
            "items": {"type": "string", "pattern": "^[a-z0-9][a-z0-9+.-]+$"},
            "uniqueItems": True,
            "default": [],
        },
        "updates": {"enum": ["security", "all"], "default": "security"},
        "storage": {
            "type": "object",
            "additionalProperties": False,
            "required": ["layout"],
            "properties": {
                "layout": {
                    "enum": ["lvm", "zfs", "mdraid"],
                    "description": "lvm: one VG over all disks; zfs: root pool on one disk; mdraid: md array over all disks.",
                },
                "raid_level": {"enum": [1, 5, 6, 10], "description": "mdraid only; default 1."},
            },
        },
    },
}

# Tables for the os-install conformance fixtures (prov conformance ubuntu-autoinstall).
_FIXTURE_TABLE: Dict[str, Any] = {"release": "24.04", "storage": {"layout": "lvm"}}
FIXTURES: Dict[str, Any] = {
    name: copy.deepcopy(_FIXTURE_TABLE)  # one table per fixture, never shared
    for name in ("conf-uefi-static", "conf-uefi-dhcp", "conf-bios-dhcp", "conf-two-disks", "conf-bad-gateway-family", "conf-duplicate-user")
}

KERNEL_CMDLINE = "autoinstall ds=nocloud"
RAID_MIN_DISKS = {1: 2, 5: 3, 6: 4, 10: 4}

MiB = 1024**2
GiB = 1024**3
ESP_SIZE = 1 * GiB
BOOT_SIZE = 2 * GiB


# --- contract: check ----------------------------------------------------------------------


def check(host: Dict[str, Any], ctx: Any) -> List[Finding]:
    """Ubuntu-specific rules beyond the JSON Schema."""
    out: List[Finding] = []
    storage = host[NAME]["storage"]
    layout = storage["layout"]
    disks = host["storage"]["disks"]
    if "network" not in host:
        out.append(Finding(ERROR, "ubuntu-autoinstall needs [network] with at least one interface (netplan is rendered from it)", "network"))
    if "raid_level" in storage and layout != "mdraid":
        out.append(Finding(ERROR, "raid_level applies to layout 'mdraid' only", f"{NAME}.storage.raid_level"))
    if layout == "mdraid":
        level = storage.get("raid_level", 1)
        if len(disks) < RAID_MIN_DISKS[level]:
            out.append(Finding(ERROR, f"raid{level} needs at least {RAID_MIN_DISKS[level]} disks, got {len(disks)}", "storage.disks"))
    if layout == "zfs" and len(disks) != 1:
        out.append(
            Finding(ERROR, "layout 'zfs' supports exactly one disk (curtin's zpool action builds no mirror vdevs)", "storage.disks")
        )
    if not any(u["ssh_authorized_keys"] for u in host["users"]):
        out.append(Finding(ERROR, "no user has ssh_authorized_keys; the installed host would have no login", "users"))
    if host["arch"] == "arm64" and host["firmware"] == "bios":
        out.append(Finding(ERROR, "arm64 boots with UEFI only", "firmware"))
    return out


# --- contract: render ---------------------------------------------------------------------


def _boot_partitions(i: int, firmware: str, mount_esp: bool) -> List[Dict[str, Any]]:
    disk = f"disk{i}"
    if firmware == "bios":
        return [
            {"type": "partition", "id": f"{disk}-biosgrub", "device": disk, "number": 1, "size": MiB, "flag": "bios_grub", "preserve": False}
        ]
    esp = f"{disk}-esp"
    actions = [
        {"type": "partition", "id": esp, "device": disk, "number": 1, "size": ESP_SIZE, "flag": "boot", "grub_device": True, "preserve": False},
        {"type": "format", "id": f"{esp}-fs", "volume": esp, "fstype": "fat32", "preserve": False},
    ]
    if mount_esp:
        actions.append({"type": "mount", "id": f"{esp}-mount", "device": f"{esp}-fs", "path": "/boot/efi"})
    return actions


def _disk(i: int, path: str, firmware: str) -> Dict[str, Any]:
    return {
        "type": "disk",
        "id": f"disk{i}",
        "path": path,
        "ptable": "gpt",
        "wipe": "superblock-recursive",
        "preserve": False,
        "grub_device": firmware == "bios",
    }


def _partition(i: int, number: int, suffix: str, size: int) -> Dict[str, Any]:
    return {"type": "partition", "id": f"disk{i}-{suffix}", "device": f"disk{i}", "number": number, "size": size, "preserve": False}


def _fs(volume: str, fstype: str, path: str) -> List[Dict[str, Any]]:
    return [
        {"type": "format", "id": f"{volume}-fs", "volume": volume, "fstype": fstype, "preserve": False},
        {"type": "mount", "id": f"{volume}-mount", "device": f"{volume}-fs", "path": path},
    ]


def storage_config(host: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Explicit curtin storage actions for the host's layout (deterministic order)."""
    disks = host["storage"]["disks"]
    firmware = host["firmware"]
    layout = host[NAME]["storage"]["layout"]
    actions: List[Dict[str, Any]] = []

    if layout == "lvm":
        pvs = []
        for i, path in enumerate(disks):
            actions.append(_disk(i, path, firmware))
            if i == 0:
                actions += _boot_partitions(0, firmware, mount_esp=True)
                actions.append(_partition(0, 2, "boot", BOOT_SIZE))
                actions.append(_partition(0, 3, "pv", -1))
                pvs.append("disk0-pv")
            else:
                actions.append(_partition(i, 1, "pv", -1))
                pvs.append(f"disk{i}-pv")
        actions.append({"type": "lvm_volgroup", "id": "vg0", "name": "vg0", "devices": pvs, "preserve": False})
        actions.append({"type": "lvm_partition", "id": "lv-root", "name": "root", "volgroup": "vg0", "preserve": False})
        actions += _fs("disk0-boot", "ext4", "/boot")
        actions += _fs("lv-root", "ext4", "/")

    elif layout == "mdraid":
        members = []
        for i, path in enumerate(disks):
            actions.append(_disk(i, path, firmware))
            actions += _boot_partitions(i, firmware, mount_esp=(i == 0))
            actions.append(_partition(i, 2, "raid", -1))
            members.append(f"disk{i}-raid")
        level = int(host[NAME]["storage"].get("raid_level", 1))
        actions.append({"type": "raid", "id": "md0", "name": "md0", "raidlevel": level, "devices": members, "spare_devices": [], "preserve": False})
        actions += _fs("md0", "ext4", "/")

    elif layout == "zfs":
        actions.append(_disk(0, disks[0], firmware))
        actions += _boot_partitions(0, firmware, mount_esp=True)
        actions.append(_partition(0, 2, "boot", BOOT_SIZE))
        actions.append(_partition(0, 3, "zfs", -1))
        actions.append(
            {
                "type": "zpool",
                "id": "zpool-rpool",
                "pool": "rpool",
                "vdevs": ["disk0-zfs"],
                "mountpoint": "/",
                "pool_properties": {"ashift": 12},
                "fs_properties": {"acltype": "posixacl", "canmount": "off", "relatime": "on", "xattr": "sa"},
            }
        )
        # Container dataset first, then the root filesystem (curtin "ZFS Root" example and
        # subiquity's guided ZFS layout: ROOT canmount=off mountpoint=none, ROOT/<name> at /).
        actions.append({"type": "zfs", "id": "zfs-root-container", "pool": "zpool-rpool", "volume": "ROOT", "properties": {"canmount": "off", "mountpoint": "none"}})
        actions.append({"type": "zfs", "id": "zfs-root", "pool": "zpool-rpool", "volume": "ROOT/ubuntu", "properties": {"mountpoint": "/"}})
        actions += _fs("disk0-boot", "ext4", "/boot")
    else:  # pragma: no cover - the schema enum prevents it
        raise ValueError(f"unknown layout {layout!r}")
    # curtin mounts in list order: emit mounts last, parents before children ("/" first).
    mounts = [a for a in actions if a["type"] == "mount"]
    mounts.sort(key=lambda a: (0 if a["path"] == "/" else a["path"].count("/"), a["path"]))
    return [a for a in actions if a["type"] != "mount"] + mounts


def network_config(host: Dict[str, Any]) -> Dict[str, Any]:
    """Netplan v2 for the host's interfaces."""
    dns = host["network"]["dns"]
    ethernets: Dict[str, Any] = {}
    for iface in host["network"]["interfaces"]:
        eth: Dict[str, Any] = {"dhcp4": iface["dhcp4"], "dhcp6": iface["dhcp6"]}
        if iface["addresses"]:
            eth["addresses"] = list(iface["addresses"])
        routes = [{"to": "default", "via": iface[gw]} for gw in ("gateway4", "gateway6") if gw in iface]
        if routes:
            eth["routes"] = routes
        if dns["nameservers"] or dns["search"]:
            eth["nameservers"] = {}
            if dns["nameservers"]:
                eth["nameservers"]["addresses"] = list(dns["nameservers"])
            if dns["search"]:
                eth["nameservers"]["search"] = list(dns["search"])
        if "macaddress" in iface:
            eth["match"] = {"macaddress": iface["macaddress"]}
            eth["set-name"] = iface["name"]
        if "mtu" in iface:
            eth["mtu"] = iface["mtu"]
        ethernets[iface["name"]] = eth
    return {"version": 2, "ethernets": ethernets}


def _user(user: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"name": user["name"], "shell": user["shell"]}
    if "gecos" in user:
        out["gecos"] = user["gecos"]
    if user["groups"]:
        out["groups"] = list(user["groups"])
    if "password_hash" in user:
        out["lock_passwd"] = False
        out["passwd"] = user["password_hash"]
    else:
        out["lock_passwd"] = True
    if user["sudo"]:
        out["sudo"] = "ALL=(ALL) NOPASSWD:ALL"
    if user["ssh_authorized_keys"]:
        out["ssh_authorized_keys"] = list(user["ssh_authorized_keys"])
    return out


def user_data(host: Dict[str, Any]) -> Dict[str, Any]:
    ubuntu = host[NAME]
    cloud: Dict[str, Any] = {
        "disable_root": True,
        "hostname": host["hostname"],
        "timezone": host["timezone"],
        "users": [_user(u) for u in host["users"]],
    }
    if "domain" in host:
        cloud["fqdn"] = f"{host['hostname']}.{host['domain']}"
    return {
        "autoinstall": {
            "version": 1,
            "locale": host["locale"],
            "keyboard": {"layout": host["keyboard"]},
            "network": network_config(host),
            "storage": {"config": storage_config(host)},
            "ssh": {"install-server": True, "allow-pw": False},
            "packages": list(ubuntu["packages"]),
            "updates": ubuntu["updates"],
            "shutdown": "reboot",
            "user-data": cloud,
        }
    }


def render(host: Dict[str, Any], ctx: Any) -> Dict[str, str]:
    """Return the NoCloud seed files (relative path -> text)."""
    meta = {"instance-id": f"iid-{host['hostname']}", "local-hostname": host["hostname"]}
    return {
        "user-data": "#cloud-config\n" + dump_yaml(user_data(host)),
        "meta-data": dump_yaml(meta),
        "kernel-cmdline": KERNEL_CMDLINE + "\n",
    }


# --- contract: validate -------------------------------------------------------------------

# Keys that reference earlier action ids, per curtin action type (other keys are names).
_REFS = {
    "partition": ("device",),
    "format": ("volume",),
    "mount": ("device",),
    "lvm_partition": ("volgroup",),
    "zfs": ("pool",),
    "dm_crypt": ("volume",),
}
_LIST_REFS = {"lvm_volgroup": ("devices",), "raid": ("devices", "spare_devices"), "zpool": ("vdevs",)}


def _check_storage(config: Any) -> List[str]:
    if not isinstance(config, list) or not config:
        return ["autoinstall.storage.config must be a non-empty list"]
    problems, ids = [], set()
    for n, action in enumerate(config):
        if not isinstance(action, dict) or "type" not in action or "id" not in action:
            problems.append(f"autoinstall.storage.config[{n}] needs 'type' and 'id'")
            continue
        if not isinstance(action["id"], str) or not isinstance(action["type"], str):
            problems.append(f"autoinstall.storage.config[{n}]: 'type' and 'id' must be strings")
            continue
        if action["id"] in ids:
            problems.append(f"autoinstall.storage.config[{n}]: duplicate id {action['id']!r}")
        for key in _REFS.get(action["type"], ()):
            if key in action and (not isinstance(action[key], str) or action[key] not in ids):
                problems.append(f"autoinstall.storage.config[{n}].{key}: {action[key]!r} is not defined earlier")
        for key in _LIST_REFS.get(action["type"], ()):
            refs = action.get(key, [])
            if not isinstance(refs, list):
                problems.append(f"autoinstall.storage.config[{n}].{key} must be a list")
                continue
            for ref in refs:
                if not isinstance(ref, str) or ref not in ids:
                    problems.append(f"autoinstall.storage.config[{n}].{key}: {ref!r} is not defined earlier")
        ids.add(action["id"])
    roots = [a for a in config if isinstance(a, dict) and (a.get("type") == "mount" and a.get("path") == "/" or a.get("type") == "zpool" and a.get("mountpoint") == "/")]
    if len(roots) != 1:
        problems.append(f"autoinstall.storage.config must mount exactly one root filesystem, found {len(roots)}")
    return problems


def _read(path: Path, findings: List[Finding]) -> str | None:
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


def _load_yaml(path: Path, findings: List[Finding]) -> tuple:
    """(text, parsed YAML) of a regular file; (None, None) with a finding otherwise."""
    text = _read(path, findings)
    if text is None:
        return None, None
    try:
        return text, yaml.safe_load(text)
    except yaml.YAMLError as exc:
        findings.append(Finding(ERROR, f"invalid YAML: {exc}", path.name))
        return None, None


def _external(path: Path) -> List[Finding]:
    """Optional external validators; a missing tool is reported, never an error."""
    tool = shutil.which("cloud-init")
    if tool is None:
        return [Finding(SKIPPED, "cloud-init not installed (cloud-init schema)")]
    try:
        proc = subprocess.run(
            [tool, "schema", "--config-file", str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [Finding(ERROR, "cloud-init schema timed out after 120 s; output not checked", path.name)]
    except OSError as exc:
        return [Finding(ERROR, f"cloud-init schema could not run: {exc.strerror or exc}", path.name)]
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip().splitlines()
        return [Finding(ERROR, "cloud-init schema: " + esc(" | ".join(detail[-5:])), path.name)]
    return []


def validate(rendered_dir: Path) -> List[Finding]:
    """Check the rendered seed: YAML shape, required autoinstall keys, optional tools."""
    findings: List[Finding] = []
    rendered_dir = Path(rendered_dir)
    ud_path = rendered_dir / "user-data"
    text, data = _load_yaml(ud_path, findings)
    if text is not None:
        if text.splitlines()[:1] != ["#cloud-config"]:
            findings.append(Finding(ERROR, "first line must be '#cloud-config'", "user-data"))
        ai = data.get("autoinstall") if isinstance(data, dict) else None
        if not isinstance(ai, dict):
            findings.append(Finding(ERROR, "top-level 'autoinstall' mapping is missing", "user-data"))
        else:
            version = ai.get("version")
            if type(version) is not int or version != 1:  # true == 1 in Python; not here
                findings.append(Finding(ERROR, "autoinstall.version must be 1", "user-data"))
            if "identity" not in ai and "user-data" not in ai:
                findings.append(Finding(ERROR, "autoinstall needs 'identity' or 'user-data'", "user-data"))
            if "user-data" in ai and not (isinstance(ai["user-data"], dict) and ai["user-data"].get("users")):
                findings.append(Finding(ERROR, "autoinstall.user-data.users is empty; nobody could log in", "user-data"))
            if "network" in ai and not (isinstance(ai["network"], dict) and ai["network"].get("version") == 2):
                findings.append(Finding(ERROR, "autoinstall.network must be netplan version 2", "user-data"))
            storage = ai.get("storage")
            if not isinstance(storage, dict):
                findings.append(Finding(ERROR, "autoinstall.storage is missing", "user-data"))
            elif "config" in storage:
                findings += [Finding(ERROR, p, "user-data") for p in _check_storage(storage["config"])]
            elif "layout" not in storage:
                findings.append(Finding(ERROR, "autoinstall.storage needs 'config' or 'layout'", "user-data"))
            ssh = ai.get("ssh", {})
            if not isinstance(ssh, dict):
                findings.append(Finding(ERROR, "autoinstall.ssh must be a mapping", "user-data"))
            elif ssh.get("allow-pw", False):
                findings.append(Finding(WARNING, "ssh password authentication is enabled", "user-data"))

    _, meta = _load_yaml(rendered_dir / "meta-data", findings)
    if meta is not None:
        for key in ("instance-id", "local-hostname"):
            if not (isinstance(meta, dict) and meta.get(key)):
                findings.append(Finding(ERROR, f"{key} is missing", "meta-data"))

    cmdline = _read(rendered_dir / "kernel-cmdline", findings)
    if cmdline is not None and not set(KERNEL_CMDLINE.split()) <= set(cmdline.split()):
        findings.append(Finding(ERROR, f"must contain {KERNEL_CMDLINE!r}", "kernel-cmdline"))

    if not any(f.level == ERROR for f in findings):
        findings += _external(ud_path)
    return findings


# --- contract: plan -----------------------------------------------------------------------


def plan(host: Dict[str, Any], ctx: Any) -> List[str]:
    """Seed-ISO and boot steps for a human, relative to the rendered directory.

    Every spec value is shell-quoted, in comments too, so a copied line never splits.
    """
    q = shlex.quote
    name = host["hostname"]
    release = host[NAME]["release"]
    arch = host["arch"]
    iso = q(f"ubuntu-{release}-live-server-{arch}.iso")
    disks = host["storage"]["disks"]
    logins = [q(u["name"]) for u in host["users"] if u["ssh_authorized_keys"]]
    boot = "--boot uefi " if host["firmware"] == "uefi" else ""
    disk_args = " ".join("--disk size=32" for _ in disks)
    os_variant = q("ubuntu" + release)
    return [
        "",
        "# 1. Build the NoCloud seed ISO (volume label must be 'cidata'), either with cloud-image-utils:",
        "cloud-localds seed.iso user-data meta-data",
        "#    or with xorriso:",
        "xorriso -as genisoimage -output seed.iso -volid cidata -joliet -rock user-data meta-data",
        "",
        f"# 2. Download the Ubuntu {q(release)} live-server ISO for {q(arch)} (latest point release) from",
        f"#    https://releases.ubuntu.com/{q(release)}/ (arm64: https://cdimage.ubuntu.com/releases/{q(release)}/release/)",
        f"#    and verify it against SHA256SUMS; below it is called {iso}.",
        "",
        f"# 3. WARNING: the install wipes {' '.join(q(d) for d in disks)} on the target machine.",
        "#    Boot the target from the live-server ISO with seed.iso attached as a second drive",
        "#    (VM: second CD-ROM; bare metal: seed.iso written to a USB stick).",
        "#    Option A - libvirt VM (adjust memory, disk sizes and network):",
        f"virt-install --name {q(name)} --memory 4096 --vcpus 2 {boot}--os-variant {os_variant} "
        f"{disk_args} --disk path=seed.iso,device=cdrom "
        f"--location {iso},kernel=casper/vmlinuz,initrd=casper/initrd "
        f"--extra-args {q(KERNEL_CMDLINE + ' console=ttyS0')} --graphics none --network network=default",
        "#    Option B - any other machine: at the GRUB menu press 'e', append to the 'linux' line:",
        f"#        {KERNEL_CMDLINE}",
        "#    then boot with Ctrl-X / F10. Without these arguments the installer asks for confirmation.",
        "",
        f"# 4. The installer partitions, installs and reboots; remove both ISOs, then log in with ssh",
        f"#    as one of: {' '.join(logins)}.",
    ]


# --- optional: starter --------------------------------------------------------------------

STARTER = """\
# prov spec (os-install, ubuntu-autoinstall). Synthetic starter values: replace them before real use.
# Documentation addresses only (RFC 5737 / RFC 3849); example.com is reserved (RFC 2606).
api_version = "prov/v1alpha1"
kind = "os-install"
backend = "ubuntu-autoinstall"
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
groups = ["adm", "sudo"]
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
addresses = ["192.0.2.10/24", "2001:db8::10/64"]
gateway4 = "192.0.2.1"
gateway6 = "2001:db8::1"

[storage]
# Every listed disk is wiped by the installer.
disks = ["/dev/vda"]

[ubuntu-autoinstall]
release = "24.04"
packages = ["qemu-guest-agent"]

[ubuntu-autoinstall.storage]
layout = "lvm"
"""


def starter(hostname: str) -> str:
    """A valid synthetic spec.toml for ``prov init os-install/<name> --backend ubuntu-autoinstall``."""
    return STARTER.format(hostname=hostname)
