"""Proxmox VE automated installation: answer.toml for an os-install spec.

Renders ``answer.toml`` for the Proxmox VE automated installer (ISO 8.4-1 or later; keys in
kebab-case). The installer creates only root: the ssh keys of ``sudo = true`` users become
root's keys, and root's password hash comes from the backend table. Static validation only;
no answer file of this backend has been installed.
"""

from __future__ import annotations

import ipaddress
import json
import re
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from ..schema import tomllib
from ..text import esc, short_repr
from ..util import NotRegularFileError, read_regular_text
from ..validate import ERROR, SKIPPED, WARNING, Finding

NAME = "proxmox-answer"
KIND = "os-install"
PLUGIN_API = 1

ANSWER = "answer.toml"
TOOL = "proxmox-auto-install-assistant"

# Keyboard ids the installer accepts (Automated_Installation, [global] keyboard).
KEYBOARDS = (
    "de", "de-ch", "dk", "en-gb", "en-us", "es", "fi", "fr", "fr-be", "fr-ca", "fr-ch", "hu", "is",
    "it", "jp", "lt", "mk", "nl", "no", "pl", "pt", "pt-br", "se", "si", "tr",
)  # fmt: skip
# Console layout (os-install keyboard) -> installer id, where the mapping is unambiguous.
# Others ("ch", "be", "ca", ...) need [proxmox-answer] keyboard.
KEYBOARD_MAP = {
    "us": "en-us", "gb": "en-gb", "uk": "en-gb", "br": "pt-br",
    **{k: k for k in ("de", "dk", "es", "fi", "fr", "hu", "is", "it", "jp", "lt", "mk", "nl", "no", "pl", "pt", "se", "si", "tr")},
}  # fmt: skip

FILESYSTEMS = ("ext4", "xfs", "zfs", "btrfs")
RAID_LEVELS = {
    "zfs": {"raid0": 1, "raid1": 2, "raid10": 4, "raidz-1": 3, "raidz-2": 4, "raidz-3": 5},
    "btrfs": {"raid0": 1, "raid1": 2, "raid10": 4},
}
# A kernel device name the installer's disk-list takes (sda, vda, nvme0n1, mmcblk0).
KERNEL_DISK = re.compile(r"/dev/([a-z][a-z0-9]*)")
LINK_DISK = re.compile(r"/dev/disk/[a-z-]+/[A-Za-z0-9._:+-]+")
DEFAULT_LOCALE = "en_US.UTF-8"

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": "Proxmox VE automated-installer settings (answer.toml, ISO 8.4-1 or later).",
    "additionalProperties": False,
    "required": ["country", "mailto", "root_password_hash", "storage"],
    "properties": {
        "country": {"type": "string", "pattern": "^[a-z]{2}$", "description": "Two-letter country code (mirror and locale defaults)."},
        "mailto": {
            "type": "string",
            "pattern": "^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+$",
            "description": "Address root's mail is sent to.",
        },
        "root_password_hash": {
            "type": "string",
            "pattern": "^\\$(y|gy|6|5)\\$[./0-9A-Za-z$=,]+$",
            "description": "crypt(3) hash of root's password (root-password-hashed); the installer requires one. Plain passwords are never accepted.",
        },
        "keyboard": {"enum": list(KEYBOARDS), "description": "Installer keyboard id; default: mapped from the core keyboard."},
        "reboot_on_error": {"type": "boolean", "default": False, "description": "Reboot instead of dropping to a shell when the install fails."},
        "storage": {
            "type": "object",
            "additionalProperties": False,
            "required": ["filesystem"],
            "properties": {
                "filesystem": {"enum": list(FILESYSTEMS), "description": "ext4 and xfs: LVM on one disk; zfs and btrfs: raid over all disks."},
                "raid": {
                    "enum": ["raid0", "raid1", "raid10", "raidz-1", "raidz-2", "raidz-3"],
                    "description": "zfs and btrfs only (btrfs: raid0, raid1, raid10).",
                },
            },
        },
    },
}

_HASH = "$6$EXAMPLEsalt$EXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhash0123456789"


def _table(filesystem: str = "ext4", raid: str | None = None) -> Dict[str, Any]:
    """A fresh fixture table (never shared between fixtures)."""
    storage: Dict[str, Any] = {"filesystem": filesystem}
    if raid:
        storage["raid"] = raid
    return {"country": "us", "mailto": "ops@example.net", "root_password_hash": _HASH, "storage": storage}


# Tables for the os-install conformance fixtures (prov conformance proxmox-answer). The
# installer needs an FQDN, and the fixtures without a domain are refused by check, so only
# the negative fixtures run; the backend's own tests and example cover the rest.
FIXTURES: Dict[str, Any] = {"conf-bad-gateway-family": _table(), "conf-duplicate-user": _table()}
UNSUPPORTED_FIXTURES: Dict[str, str] = {
    "conf-uefi-static": "the installer derives the DNS search domain from the FQDN; a search domain other than the spec's domain cannot be expressed",
    "conf-uefi-dhcp": "the installer needs an FQDN; the fixture has no domain",
    "conf-bios-dhcp": "the installer needs an FQDN; the fixture has no domain",
    "conf-two-disks": "the installer needs an FQDN, and several /dev/disk/... paths cannot be selected (one udev filter key per property)",
}


# --- contract: check ----------------------------------------------------------------------


def _disks(disks: List[str]) -> List[Finding]:
    links = [d for d in disks if LINK_DISK.fullmatch(d)]
    names = [d for d in disks if KERNEL_DISK.fullmatch(d)]
    out: List[Finding] = []
    for n, disk in enumerate(disks):
        if disk not in links and disk not in names:
            out.append(
                Finding(ERROR, f"{short_repr(disk)} is neither a kernel device (/dev/sda, /dev/nvme0n1) nor a /dev/disk/... link", f"storage.disks[{n}]")
            )
    if links and (len(disks) > 1):
        out.append(
            Finding(
                ERROR,
                "a /dev/disk/... path selects its disk with one udev filter (DEVLINKS), which cannot name several disks; "
                "list kernel devices (/dev/sda, /dev/sdb) instead",
                "storage.disks",
            )
        )
    return out


def _storage(spec: Dict[str, Any]) -> List[Finding]:
    storage = spec[NAME]["storage"]
    fs = storage["filesystem"]
    count = len(spec["storage"]["disks"])
    where = f"{NAME}.storage"
    if fs in ("ext4", "xfs"):
        out = []
        if "raid" in storage:
            out.append(Finding(ERROR, f"raid applies to zfs and btrfs only, not {fs}", f"{where}.raid"))
        if count != 1:
            out.append(Finding(ERROR, f"{fs} installs to exactly one disk, got {count}", "storage.disks"))
        return out
    if "raid" not in storage:
        return [Finding(ERROR, f"{fs} needs raid ({', '.join(RAID_LEVELS[fs])})", f"{where}.raid")]
    raid = storage["raid"]
    if raid not in RAID_LEVELS[fs]:
        return [Finding(ERROR, f"{fs} supports raid {', '.join(RAID_LEVELS[fs])}, not {raid}", f"{where}.raid")]
    need = RAID_LEVELS[fs][raid]
    if count < need:
        return [Finding(ERROR, f"{fs} {raid} needs at least {need} disks, got {count}", "storage.disks")]
    if raid == "raid10" and count % 2:
        return [Finding(ERROR, f"{fs} raid10 needs an even number of disks, got {count}", "storage.disks")]
    return []


def _network(spec: Dict[str, Any]) -> List[Finding]:
    if "network" not in spec:
        return [Finding(ERROR, "proxmox-answer needs [network] with one interface (the installer configures one NIC)", "network")]
    network = spec["network"]
    ifaces = network["interfaces"]
    if len(ifaces) != 1:
        return [Finding(ERROR, f"the installer configures exactly one NIC, got {len(ifaces)} interfaces; add the others after the install", "network.interfaces")]
    iface = ifaces[0]
    where = "network.interfaces[0]"
    dns = network["dns"]
    out: List[Finding] = []
    if "mtu" in iface:
        out.append(Finding(ERROR, "the installer cannot set an MTU; remove mtu and set it after the install", f"{where}.mtu"))
    search = dns["search"]
    if search and search != [spec.get("domain")]:
        out.append(Finding(ERROR, "the installer derives the DNS search domain from the FQDN; search may only repeat domain", "network.dns.search"))
    dhcp = iface["dhcp4"] or iface["dhcp6"]
    if dhcp:
        if iface["addresses"] or "gateway4" in iface or "gateway6" in iface or dns["nameservers"]:
            out.append(Finding(ERROR, "with DHCP the installer takes address, gateway and DNS from the DHCP server; remove the static values", where))
        if "macaddress" in iface:
            out.append(Finding(WARNING, "with DHCP the installer picks the NIC itself; macaddress is not used", f"{where}.macaddress"))
        return out
    if len(iface["addresses"]) != 1:
        out.append(Finding(ERROR, f"a static install takes exactly one address, got {len(iface['addresses'])}", f"{where}.addresses"))
        return out
    try:
        version = ipaddress.ip_interface(iface["addresses"][0]).version
    except ValueError:
        return out  # reported by the kind
    gateway = f"gateway{version}"
    other = f"gateway{10 - version}"
    if gateway not in iface:
        out.append(Finding(ERROR, f"a static IPv{version} install needs {gateway}", where))
    if other in iface:
        out.append(Finding(ERROR, f"the installer takes one gateway, of the address's family; remove {other}", f"{where}.{other}"))
    if len(dns["nameservers"]) != 1:
        out.append(Finding(ERROR, f"a static install takes exactly one nameserver, got {len(dns['nameservers'])}", "network.dns.nameservers"))
    return out


def check(spec: Dict[str, Any], ctx: Any) -> List[Finding]:
    """What the Proxmox VE installer can express, beyond the JSON Schema."""
    out: List[Finding] = []
    if "domain" not in spec:
        out.append(Finding(ERROR, "the installer needs an FQDN; set domain", "domain"))
    if spec["arch"] != "amd64":
        out.append(Finding(ERROR, "Proxmox VE installs on amd64 only", "arch"))
    if "keyboard" not in spec[NAME] and spec["keyboard"] not in KEYBOARD_MAP:
        out.append(Finding(ERROR, f"no installer keyboard id for {short_repr(spec['keyboard'])}; set {NAME}.keyboard", "keyboard"))
    if spec["locale"] != DEFAULT_LOCALE:
        out.append(Finding(WARNING, f"the installer has no locale setting (it follows country); {short_repr(spec['locale'])} is not used", "locale"))
    for n, user in enumerate(spec["users"]):
        if not user["sudo"]:
            out.append(
                Finding(ERROR, f"the installer creates only root; {short_repr(user['name'])} has no sudo, so its keys cannot be given to root", f"users[{n}]")
            )
        elif "password_hash" in user:
            out.append(Finding(WARNING, f"password_hash of {short_repr(user['name'])} is not used; root's password is {NAME}.root_password_hash", f"users[{n}].password_hash"))
    out += _disks(spec["storage"]["disks"])
    out += _storage(spec)
    out += _network(spec)
    return out


# --- contract: render ---------------------------------------------------------------------


def _toml(value: Any) -> str:
    """A TOML value: JSON's string, bool and array spellings are valid TOML."""
    return json.dumps(value, ensure_ascii=False)


def _section(title: str, items: List[tuple]) -> List[str]:
    return [f"[{title}]"] + [f"{key} = {_toml(value)}" for key, value in items]


def root_keys(spec: Dict[str, Any]) -> List[str]:
    """ssh keys of the sudo users, in spec order, without duplicates."""
    keys: List[str] = []
    for user in spec["users"]:
        if user["sudo"]:
            keys += [k for k in user["ssh_authorized_keys"] if k not in keys]
    return keys


def answer(spec: Dict[str, Any]) -> str:
    table = spec[NAME]
    keyboard = table.get("keyboard") or KEYBOARD_MAP[spec["keyboard"]]
    glob = [
        ("country", table["country"]),
        ("fqdn", f"{spec['hostname']}.{spec['domain']}"),
        ("keyboard", keyboard),
        ("mailto", table["mailto"]),
        ("reboot-on-error", table["reboot_on_error"]),
        ("root-password-hashed", table["root_password_hash"]),
    ]
    keys = root_keys(spec)
    if keys:
        glob.append(("root-ssh-keys", keys))
    glob.append(("timezone", spec["timezone"]))

    iface = spec["network"]["interfaces"][0]
    if iface["dhcp4"] or iface["dhcp6"]:
        net = [("source", "from-dhcp")]
    else:
        address = iface["addresses"][0]
        gateway = iface["gateway4"] if ipaddress.ip_interface(address).version == 4 else iface["gateway6"]
        net = [("source", "from-answer"), ("cidr", address), ("dns", spec["network"]["dns"]["nameservers"][0]), ("gateway", gateway)]
        if "macaddress" in iface:
            net.append(("filter.ID_NET_NAME_MAC", "*" + iface["macaddress"].replace(":", "")))
        else:
            net.append(("filter.ID_NET_NAME", iface["name"]))

    storage = table["storage"]
    fs = storage["filesystem"]
    disks = spec["storage"]["disks"]
    disk = [("filesystem", fs)]
    if LINK_DISK.fullmatch(disks[0]):
        # One /dev/disk/... link: match it among the disk's udev DEVLINKS (space separated).
        disk.append(("filter.DEVLINKS", f"*{disks[0]}*"))
    else:
        disk.append(("disk-list", [KERNEL_DISK.fullmatch(d).group(1) for d in disks]))  # type: ignore[union-attr]
    if fs in RAID_LEVELS:
        disk.append((f"{fs}.raid", storage["raid"]))

    lines = [
        "# Proxmox VE automated installation answer file, rendered by prov (proxmox-answer).",
        "# Every disk it selects is wiped. Validate: proxmox-auto-install-assistant validate-answer answer.toml",
        "",
        *_section("global", glob),
        "",
        *_section("network", net),
        "",
        *_section("disk-setup", disk),
    ]
    return "\n".join(lines) + "\n"


def render(spec: Dict[str, Any], ctx: Any) -> Dict[str, str]:
    """Return the answer file (relative path -> text)."""
    return {ANSWER: answer(spec)}


# --- contract: validate -------------------------------------------------------------------


def _read(path: Path, findings: List[Finding]) -> str | None:
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


def _shape(data: Dict[str, Any]) -> List[str]:
    """Required sections and keys of a (possibly hand-edited) answer file."""
    problems: List[str] = []
    glob = data.get("global")
    if not isinstance(glob, dict):
        problems.append("[global] is missing")
    else:
        for key in ("country", "fqdn", "keyboard", "mailto", "timezone"):
            if not isinstance(glob.get(key), str) or not glob[key]:
                problems.append(f"global.{key} must be a non-empty string")
        if ("root-password" in glob) == ("root-password-hashed" in glob):
            problems.append("global needs exactly one of root-password and root-password-hashed")
        if isinstance(glob.get("keyboard"), str) and glob["keyboard"] not in KEYBOARDS:
            problems.append(f"global.keyboard {short_repr(glob['keyboard'])} is not an installer keyboard id")
    net = data.get("network")
    if not isinstance(net, dict):
        problems.append("[network] is missing")
    elif net.get("source") == "from-dhcp":
        extra = sorted(k for k in net if k != "source")
        if extra:
            problems.append(f"network: source = 'from-dhcp' takes no other keys, found {', '.join(extra)}")
    elif net.get("source") == "from-answer":
        for key in ("cidr", "dns", "gateway"):
            if not isinstance(net.get(key), str):
                problems.append(f"network.{key} must be a string with source = 'from-answer'")
    else:
        problems.append("network.source must be 'from-dhcp' or 'from-answer'")
    disk = data.get("disk-setup")
    if not isinstance(disk, dict):
        problems.append("[disk-setup] is missing")
    else:
        fs = disk.get("filesystem")
        if fs not in FILESYSTEMS:
            problems.append(f"disk-setup.filesystem must be one of {', '.join(FILESYSTEMS)}")
        if ("disk-list" in disk) == ("filter" in disk):
            problems.append("disk-setup needs exactly one of disk-list and filter")
        if "disk-list" in disk and not (isinstance(disk["disk-list"], list) and disk["disk-list"]):
            problems.append("disk-setup.disk-list must be a non-empty list")
        if fs in RAID_LEVELS:
            sub = disk.get(fs)
            if not (isinstance(sub, dict) and sub.get("raid") in RAID_LEVELS[fs]):
                problems.append(f"disk-setup.{fs}.raid must be one of {', '.join(RAID_LEVELS[fs])}")
    return problems


def _external(path: Path) -> List[Finding]:
    """The installer's own validator when installed; a missing tool is reported, never an error."""
    tool = shutil.which(TOOL)
    if tool is None:
        return [Finding(SKIPPED, f"{TOOL} not installed ({TOOL} validate-answer)")]
    try:
        proc = subprocess.run([tool, "validate-answer", str(path)], capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        return [Finding(ERROR, f"{TOOL} validate-answer timed out after 120 s; answer not checked", path.name)]
    except OSError as exc:
        return [Finding(ERROR, f"{TOOL} validate-answer could not run: {exc.strerror or exc}", path.name)]
    if proc.returncode != 0:
        detail = (proc.stdout + proc.stderr).strip().splitlines()
        return [Finding(ERROR, f"{TOOL} validate-answer: " + esc(" | ".join(detail[-5:])), path.name)]
    return []


def validate(rendered_dir: Path) -> List[Finding]:
    """Check the rendered answer file: TOML syntax, required sections and keys, optional tool."""
    findings: List[Finding] = []
    path = Path(rendered_dir) / ANSWER
    text = _read(path, findings)
    if text is None:
        return findings
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [Finding(ERROR, f"invalid TOML: {exc}", ANSWER)]
    findings += [Finding(ERROR, p, ANSWER) for p in _shape(data)]
    if not findings:
        findings += _external(path)
    return findings


# --- contract: plan -----------------------------------------------------------------------


def plan(spec: Dict[str, Any], ctx: Any) -> List[str]:
    """ISO preparation and boot steps for a human, relative to the rendered directory.

    Every spec value is shell-quoted, in comments too, so a copied line never splits.
    """
    q = shlex.quote
    fqdn = q(f"{spec['hostname']}.{spec['domain']}")
    first = spec["storage"]["disks"][0]
    disks = " ".join(q(d) for d in spec["storage"]["disks"])
    if LINK_DISK.fullmatch(first):
        select = [
            "#    The disk is selected by a udev DEVLINKS glob (not yet proven on a real install): before",
            "#    installing, check on the target (e.g. the unmodified ISO's debug shell) that it matches one disk:",
            f"#        {TOOL} device-match disk DEVLINKS={q('*' + first + '*')}",
        ]
    else:
        select = ["#    The disks are selected by kernel name (disk-list); check the names on the target first (lsblk)."]
    return [
        "",
        "# 1. Download a Proxmox VE ISO (8.4-1 or later) from https://www.proxmox.com/en/downloads",
        "#    and verify it against the published SHA256 checksum; below it is called proxmox-ve.iso.",
        "",
        f"# 2. On any Debian-based machine with {TOOL} and xorriso installed (Proxmox VE repository),",
        "#    check the answer file and embed it into a copy of the ISO:",
        f"{TOOL} validate-answer answer.toml",
        f"{TOOL} prepare-iso proxmox-ve.iso --fetch-from iso --answer-file answer.toml --output proxmox-ve-auto.iso",
        "",
        f"# 3. WARNING: the install wipes {disks} on the target machine.",
        *select,
        "#    Write the prepared ISO to a USB stick (replace /dev/sdX with the stick, double-check it):",
        "dd if=proxmox-ve-auto.iso of=/dev/sdX bs=4M conv=fsync status=progress",
        f"#    then boot {fqdn} from it; the prepared ISO's boot menu selects the automated installation.",
        "",
        f"# 4. The installer partitions, installs and reboots; remove the stick, then log in as root",
        f"#    at https://{fqdn}:8006 or over ssh.",
    ]


# --- optional: starter --------------------------------------------------------------------

STARTER = """\
# prov spec (os-install, proxmox-answer). Synthetic starter values: replace them before real use.
# Documentation addresses only (RFC 5737); example.com is reserved (RFC 2606).
api_version = "prov/v1alpha1"
kind = "os-install"
backend = "proxmox-answer"
hostname = "{hostname}"
domain = "example.com"
arch = "amd64"
firmware = "uefi"
timezone = "Etc/UTC"
keyboard = "us"

# The installer creates only root: the ssh keys of sudo users are given to root.
[[users]]
name = "ops"
sudo = true
ssh_authorized_keys = [
  "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE ops@example.com",
]

[network.dns]
nameservers = ["192.0.2.53"]

# The installer configures exactly one NIC.
[[network.interfaces]]
name = "eno1"
addresses = ["192.0.2.10/24"]
gateway4 = "192.0.2.1"

[storage]
# Every listed disk is wiped by the installer.
disks = ["/dev/sda"]

[proxmox-answer]
country = "us"
mailto = "ops@example.com"
# crypt(3) hash of root's password, e.g. from: mkpasswd --method=yescrypt
root_password_hash = "$6$EXAMPLEsalt$EXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhash0123456789"

[proxmox-answer.storage]
filesystem = "ext4"
"""


def starter(hostname: str) -> str:
    """A valid synthetic spec.toml for ``prov init os-install/<name> --backend proxmox-answer``."""
    return STARTER.format(hostname=hostname)
