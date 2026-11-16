"""NixOS with disko: a pinned flake holding the target system and an offline USB installer.

Renders ``flake.nix`` (nixpkgs + disko inputs; outputs ``nixosConfigurations.<host>`` and
``nixosConfigurations.<host>-installer``), ``configuration.nix`` (the target system),
``disko.nix`` (GPT, ESP or BIOS boot partition, optional swap, ext4 root) and
``installer.nix`` (minimal installer ISO carrying the target closure and an
``install-<host>`` command that asks for the hostname before it wipes the disk).

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

# The disko release the flake pins (a tag of github.com/nix-community/disko). Its
# docs/disko-install.md at this tag is the source of the offline-installer pattern.
DISKO_REF = "v1.13.0"
DISKO_REF_COMMIT = "de5708739256238fb912c62f03988815db89ec9a"

# Human-created files render keeps in rendered/ (core honours PRESERVE).
PRESERVE = ("flake.lock",)

FILES = ("configuration.nix", "disko.nix", "flake.nix", "installer.nix")

_ATTR = "[A-Za-z_][A-Za-z0-9_-]*"

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
            "enum": ["ext4"],
            "default": "ext4",
            "description": "Disk layout. ext4: GPT on one disk with an ESP (UEFI) or BIOS boot partition, optional swap and an ext4 root.",
        },
        "swap_size": {
            "type": "string",
            "pattern": "^[1-9][0-9]*[MG]$",
            "description": "Optional swap partition size (e.g. 8G); no swap partition when absent.",
        },
        "packages": {
            "type": "array",
            "description": "nixpkgs attribute names added to environment.systemPackages (e.g. git, python3Packages.requests).",
            "items": {"type": "string", "pattern": f"^{_ATTR}(\\.{_ATTR})*$"},
            "uniqueItems": True,
            "default": [],
        },
        "cpu_vendor": {"enum": ["intel", "amd"], "description": "Enables the CPU microcode updates of this vendor."},
    },
}

ESP_SIZE = "512M"
BIOS_BOOT_SIZE = "1M"
BY_ID = "/dev/disk/by-id/"
INITRD_MODULES = ("nvme", "ahci", "xhci_pci", "usb_storage", "sd_mod", "usbhid")
SYSTEMS = {"amd64": "x86_64-linux", "arm64": "aarch64-linux"}
SHELLS = {"bash": None, "zsh": "zsh", "fish": "fish"}  # basename -> programs.<x> / pkgs.<x>
SHELL_DIRS = ("/bin/", "/usr/bin/", "/run/current-system/sw/bin/")
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


# --- contract: check ----------------------------------------------------------------------


def check(spec: Dict[str, Any], ctx: Any) -> List[Finding]:
    """NixOS-specific rules beyond the JSON Schema."""
    out: List[Finding] = []
    nixos = spec[NAME]
    disks = spec["storage"]["disks"]
    if nixos["layout"] == "ext4" and len(disks) != 1:
        out.append(Finding(ERROR, f"layout 'ext4' installs to exactly one disk, got {len(disks)}", "storage.disks"))
    for d, disk in enumerate(disks):
        if not disk.startswith(BY_ID):
            out.append(
                Finding(WARNING, f"{short_repr(disk)} is not a {BY_ID} path; kernel names like /dev/sda can change between boots", f"storage.disks[{d}]")
            )
    if spec["arch"] == "arm64" and spec["firmware"] == "bios":
        out.append(Finding(ERROR, "arm64 boots with UEFI only", "firmware"))
    for n, user in enumerate(spec["users"]):
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
    release = nixos["release"][len("nixos-"):]
    if nixos["state_version"] > release:
        out.append(
            Finding(WARNING, f"state_version {nixos['state_version']} is newer than release {release}; set it to the release first installed", f"{NAME}.state_version")
        )
    return out


# --- contract: render ---------------------------------------------------------------------

HEADER = "# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render."


def flake_nix(spec: Dict[str, Any]) -> str:
    host = spec["hostname"]
    system = SYSTEMS[spec["arch"]]
    release = spec[NAME]["release"]
    return "\n".join(
        [
            HEADER,
            "# Commit this directory, flake.lock and ../hardware-configuration.nix (if any) to git:",
            "# Nix copies only git-tracked files of a flake.",
            "{",
            f"  description = {nix_str(f'NixOS host {host} (prov os-install, backend nixos-disko)')};",
            "",
            "  inputs = {",
            f"    nixpkgs.url = {nix_str(f'github:NixOS/nixpkgs/{release}')};",
            f"    disko.url = {nix_str(f'github:nix-community/disko/{DISKO_REF}')};",
            '    disko.inputs.nixpkgs.follows = "nixpkgs";',
            "  };",
            "",
            "  outputs = { self, nixpkgs, disko, ... }: {",
            "    # The target system.",
            f"    nixosConfigurations.{nix_attr(host)} = nixpkgs.lib.nixosSystem {{",
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
    lines = [
        "  # Network: systemd-networkd, one unit per interface; NICs with a MAC are renamed by .link files.",
        "  networking.useDHCP = false;",
        "  networking.useNetworkd = true;",
        "  systemd.network.enable = true;",
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
    for user in spec["users"]:
        groups = list(user["groups"])
        if user["sudo"]:
            sudoers.append(user["name"])
            if "wheel" not in groups:
                groups.append("wheel")
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
    lines = [
        HEADER,
        "# The target system. Static validation only: prov has not installed it on hardware.",
        "{ config, lib, pkgs, modulesPath, ... }:",
        "let",
        "  # A human-owned hardware-configuration.nix next to spec.toml (outside rendered/) is",
        "  # imported when it exists and is git-tracked; render never writes or deletes it. The",
        "  # installer copies it into its offline flake as ./hardware-configuration.nix.",
        "  hardware =",
        "    if builtins.pathExists ./hardware-configuration.nix then [ ./hardware-configuration.nix ]",
        "    else if builtins.pathExists ../hardware-configuration.nix then [ ../hardware-configuration.nix ]",
        "    else [ ];",
        "in",
        "{",
        '  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ] ++ hardware;',
        "",
        "  # Fallback for hardware without a hardware-configuration.nix: broad initrd storage/USB drivers.",
        f"  boot.initrd.availableKernelModules = {nix_list(list(INITRD_MODULES))};",
    ]
    if "cpu_vendor" in nixos:
        lines.append(f"  hardware.cpu.{nixos['cpu_vendor']}.updateMicrocode = true;")
    lines.append("")
    if spec["firmware"] == "uefi":
        lines += [
            "  # UEFI: systemd-boot on the ESP (disko.nix mounts it at /boot).",
            "  boot.loader.systemd-boot.enable = true;",
            "  boot.loader.efi.canTouchEfiVariables = true;",
        ]
    else:
        lines += [
            "  # BIOS: GRUB in the MBR gap; disko sets boot.loader.grub.devices from the EF02 partition.",
            "  boot.loader.grub.enable = true;",
        ]
    lines += ["", f"  networking.hostName = {nix_str(spec['hostname'])};"]
    if "domain" in spec:
        lines.append(f"  networking.domain = {nix_str(spec['domain'])};")
    lines += [
        f"  time.timeZone = {nix_str(spec['timezone'])};",
        f"  i18n.defaultLocale = {nix_str(spec['locale'])};",
        f"  console.keyMap = {nix_str(spec['keyboard'])};",
        "",
    ]
    lines += _network(spec)
    lines.append("")
    lines += _users(spec)
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


def disko_nix(spec: Dict[str, Any]) -> str:
    nixos = spec[NAME]
    disk = spec["storage"]["disks"][0]
    lines = [
        HEADER,
        "# Partitioning (disko). WARNING: disko-install wipes this disk.",
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
            f"          size = {nix_str(ESP_SIZE)};",
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
    if "swap_size" in nixos:
        lines += [
            "        swap = {",
            f"          size = {nix_str(nixos['swap_size'])};",
            '          content = { type = "swap"; };',
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


def installer_nix(spec: Dict[str, Any]) -> str:
    host = spec["hostname"]
    disk = spec["storage"]["disks"][0]
    target = f"self.nixosConfigurations.{nix_attr(host)}"
    efi = "--write-efi-boot-entries " if spec["firmware"] == "uefi" else ""
    return "\n".join(
        [
            HEADER,
            "# USB installer: the NixOS minimal ISO plus an offline copy of the target system and an",
            f"# install-{host} command. Nothing runs at boot; a human starts the install.",
            "#",
            "# Offline closure: the pattern of disko's docs/disko-install.md (\"Using disko-install in an",
            f"# offline installer\") at tag {DISKO_REF} (commit {DISKO_REF_COMMIT}),",
            "# plus toplevel.drvPath from its tests/disko-install/default.nix.",
            "{ config, lib, pkgs, modulesPath, self, ... }:",
            "let",
            f"  hostName = {nix_str(host)};",
            f"  disk = {nix_str(disk)};",
            f"  target = {target};",
            "  disko = self.inputs.disko.packages.${pkgs.stdenv.hostPlatform.system}.disko;",
            "",
            "  # The flake disko-install evaluates on the stick. disko-install copies only the flake",
            "  # directory, so ../hardware-configuration.nix would be lost: it is copied in as",
            "  # ./hardware-configuration.nix, which configuration.nix checks first.",
            '  installFlake = pkgs.runCommand "install-flake-${hostName}" { } (',
            "    ''",
            '      mkdir -p "$out"',
            '      cp ${./flake.nix} "$out/flake.nix"',
            '      cp ${./configuration.nix} "$out/configuration.nix"',
            '      cp ${./disko.nix} "$out/disko.nix"',
            '      cp ${./installer.nix} "$out/installer.nix"',
            "    ''",
            "    + lib.optionalString (builtins.pathExists ./flake.lock) ''",
            '      cp ${./flake.lock} "$out/flake.lock"',
            "    ''",
            "    + lib.optionalString (builtins.pathExists ../hardware-configuration.nix) ''",
            '      cp ${../hardware-configuration.nix} "$out/hardware-configuration.nix"',
            "    ''",
            "  );",
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
            "    target.config.system.build.toplevel.drvPath",
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
            "    runtimeInputs = [ pkgs.coreutils pkgs.util-linux disko ];",
            "    text = ''",
            "      disk=${lib.escapeShellArg disk}",
            "      host=${lib.escapeShellArg hostName}",
            '      if [ "$(id -u)" -ne 0 ]; then',
            '        echo "install-$host: run it as root: sudo install-$host" >&2',
            "        exit 1",
            "      fi",
            '      if [ ! -b "$disk" ]; then',
            '        echo "install-$host: disk $disk not found; nothing was changed. Disks present:" >&2',
            "        ls -l /dev/disk/by-id/ >&2 || true",
            "        exit 1",
            "      fi",
            '      echo "WARNING: this ERASES ALL DATA on $disk ($(readlink -f "$disk")):"',
            '      lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS "$disk"',
            '      printf "Type the hostname (%s) to wipe this disk and install NixOS: " "$host"',
            '      answer=""',
            "      read -r answer || true",
            '      if [ "$answer" != "$host" ]; then',
            '        echo "install-$host: confirmation did not match; aborted, nothing was changed" >&2',
            "        exit 1",
            "      fi",
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
    ("configuration.nix", "builtins.pathExists ../hardware-configuration.nix", "the guarded hardware-configuration.nix import"),
    ("configuration.nix", "networking.hostName = ", "networking.hostName"),
    ("configuration.nix", "services.openssh", "services.openssh"),
    ("configuration.nix", "system.stateVersion = ", "system.stateVersion"),
    ("disko.nix", "disko.devices.disk.main", "disko.devices.disk.main"),
    ("disko.nix", 'mountpoint = "/";', "a root filesystem"),
    ("installer.nix", "installation-cd-minimal.nix", "the installation-cd-minimal.nix base"),
    ("installer.nix", "closureInfo", "the offline closure (closureInfo)"),
    ("installer.nix", 'if [ "$answer" != "$host" ]; then', "the typed-hostname confirmation"),
    ("installer.nix", "disko-install ", "the disko-install call"),
]

_STRING = r'"((?:[^"\\]|\\.)*)"'


def _string_value(text: str, key: str) -> Optional[str]:
    """The (unescaped) string assigned to ``key`` at the start of a line, or None."""
    m = re.search(rf"^\s*{re.escape(key)} = {_STRING};", text, re.M)
    if m is None:
        return None
    return re.sub(r"\\(.)", lambda c: {"n": "\n", "r": "\r", "t": "\t"}.get(c.group(1), c.group(1)), m.group(1))


def _external(paths: List[Path]) -> List[Finding]:
    """``nix-instantiate --parse`` on each file (parse only: no evaluation, no fetching)."""
    tool = shutil.which("nix-instantiate")
    if tool is None:
        return [Finding(SKIPPED, "nix not installed (nix-instantiate --parse)")]
    out: List[Finding] = []
    for path in paths:
        try:
            proc = subprocess.run(
                [tool, "--readonly-mode", "--parse", str(path)],
                capture_output=True,
                text=True,
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
            detail = proc.stderr.strip().splitlines()
            out.append(Finding(ERROR, "nix-instantiate --parse: " + esc(" | ".join(detail[-5:])), path.name))
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
        targets = [n for n in names if not n.endswith("-installer")]
        if len(targets) != 1 or f"{targets[0]}-installer" not in names:
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
    if disko is not None and inst is not None:
        device, disk = _string_value(disko, "device"), _string_value(inst, "disk")
        if device is None:
            findings.append(Finding(ERROR, "disko.devices.disk.main.device is missing", "disko.nix"))
        elif disk != device:
            findings.append(Finding(ERROR, f"installs to {short_repr(disk)} but disko.nix partitions {short_repr(device)}", "installer.nix"))
    if inst is not None and re.search(r"systemd\.(user\.)?services|boot\.postBootCommands|services\.getty\.autologinUser", inst):
        findings.append(Finding(ERROR, "the installer must not run anything at boot", "installer.nix"))

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
    host = spec["hostname"]
    disk = spec["storage"]["disks"][0]
    disks = " ".join(q(d) for d in spec["storage"]["disks"])
    installer = f".#nixosConfigurations.{q(host)}-installer.config.system.build.isoImage"
    mode = "UEFI" if spec["firmware"] == "uefi" else "legacy BIOS"
    login = next((u["name"] for u in spec["users"] if u["ssh_authorized_keys"]), None)
    address = next((a.split("/")[0] for i in spec["network"]["interfaces"] for a in i["addresses"]), None)
    ssh = f"ssh {q(login)}@{q(address) if address else '<host-ip>'}" if login else None
    lines = [
        "",
        "# Static validation only: prov has not installed this configuration on hardware.",
        "# Nix copies only git-tracked files of a flake: keep this item in a git repository and",
        "# git add rendered/, flake.lock and ../hardware-configuration.nix before every nix command.",
        "",
        "# 1. Optional, recommended: capture the target's hardware configuration. Boot the stock",
        "#    NixOS minimal ISO on the target and run:",
        "nixos-generate-config --no-filesystems --show-hardware-config > hardware-configuration.nix",
        "#    Copy that file next to spec.toml (../hardware-configuration.nix from here), then:",
        "git add ../hardware-configuration.nix",
        "#    configuration.nix imports it when present; without it a broad initrd driver set is used.",
        "",
        "# 2. Lock the flake inputs here and commit the lock (render keeps rendered/flake.lock):",
        "nix flake lock",
        f"git add flake.lock && git commit -m {q(f'Lock the {host} flake inputs')}",
        "",
        "# 3. Build the installer ISO (large: it carries the target closure for an offline install):",
        f"nix build {installer}",
        "ls result/iso/",
        "",
        "# 4. Write the ISO to a USB stick. WARNING: this WIPES the stick. Find its by-id path with",
        "#    ls -l /dev/disk/by-id/ | grep usb   and double-check it. A GUI writer (GNOME Disks,",
        "#    balenaEtcher, Rufus in DD mode) is safer than dd:",
        "sudo dd if=result/iso/<ISO-FILE> of=/dev/disk/by-id/usb-<YOUR-STICK> bs=4M conv=fsync status=progress",
        "",
        f"# 5. WARNING: the install WIPES {disks} on the target machine.",
        f"#    Boot the target from the USB stick in {mode} mode, then run on its console:",
        f"sudo {q(f'install-{host}')}",
        f"#    It shows {q(disk)}, asks you to type {q(host)} to confirm, then runs disko-install",
        "#    offline (partition, format, install). Any other answer aborts without changes.",
        "",
        "# 6. Post-install checks: remove the stick, reboot into the installed system, then",
    ]
    lines.append(f"{ssh}" if ssh else "#    log in on the console (no user has an ssh key).")
    lines += [
        "#    and on the installed host:",
        "nixos-version",
        "systemctl --failed",
        "findmnt / /boot" if spec["firmware"] == "uefi" else "findmnt /",
        "swapon --show",
        "ip -br addr",
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
"""


def starter(hostname: str) -> str:
    """A valid synthetic spec.toml for ``prov init os-install/<name> --backend nixos-disko``."""
    return STARTER.format(hostname=hostname)
