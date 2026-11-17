"""nixos-disko backend: checks, rendered files, validation, installer guard, plan."""

from __future__ import annotations

import copy
import re
import os
import shlex
import shutil
import subprocess

import pytest

from prov.backends import nixos_disko as nixos
from prov.cli import main
from prov.plan import plan_lines
from prov.registry import ENTRY_POINT_GROUP, Context, discover
from prov.schema import tomllib
from prov.spec import check_spec
from prov.validate import ERROR, SKIPPED, WARNING

DISK = "/dev/disk/by-id/nvme-EXAMPLE_SSD_SERIAL_0000000000"
BASH = shutil.which("bash")  # looked up at import: the autouse fixture stubs shutil.which


def check_host(raw):
    return check_spec(raw, "os-install", "demo", discover())


def starter_raw():
    return tomllib.loads(nixos.starter("demo"))


def host(edit=None):
    raw = starter_raw()
    if edit:
        edit(raw)
    spec, findings = check_host(raw)
    assert spec is not None, findings
    return spec


def messages(findings, level=ERROR):
    return [f.message for f in findings if f.level == level]


def spec_findings(edit):
    raw = starter_raw()
    edit(raw)
    _, findings = check_host(raw)
    return findings


@pytest.fixture
def rendered(tmp_path):
    for name, text in nixos.render(host(), Context("demo")).items():
        (tmp_path / name).write_text(text)
    return tmp_path


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


# --- registration, starter ----------------------------------------------------------------


def test_registered_via_entry_point():
    from importlib.metadata import entry_points

    names = {ep.name: ep.value for ep in entry_points(group=ENTRY_POINT_GROUP)}
    assert names.get("nixos-disko") == "prov.backends.nixos_disko"


def test_starter_is_valid_without_findings():
    spec, findings = check_host(starter_raw())
    assert spec is not None and findings == []
    assert spec["nixos-disko"]["layout"] == "ext4" and spec["nixos-disko"]["release"] == "nixos-26.05"


# --- check --------------------------------------------------------------------------------


@pytest.mark.parametrize("disks", [[DISK, "/dev/disk/by-id/ata-EXAMPLE_HDD_SERIAL_1"]])
def test_ext4_needs_exactly_one_disk(disks):
    findings = spec_findings(lambda r: r["storage"].update(disks=disks))
    assert "layout 'ext4' installs to exactly one disk, got 2" in messages(findings)


def test_zero_disks_is_rejected():
    findings = spec_findings(lambda r: r["storage"].update(disks=[]))
    assert "storage.disks: needs at least 1 item(s)" in messages(findings)  # the kind's schema, before check


def test_non_by_id_disk_warns():
    findings = spec_findings(lambda r: r["storage"].update(disks=["/dev/sda"]))
    assert messages(findings) == []
    assert any("is not a plain /dev/disk/by-id/ path" in m for m in messages(findings, WARNING))


@pytest.mark.parametrize("disk", ["/dev/disk/by-id/../../sda", "/dev/disk/by-id/./nvme-EXAMPLE_1", "/dev/disk/by-id/nvme-EXAMPLE_1/.."])
def test_by_id_paths_with_dot_segments_warn(disk):
    findings = spec_findings(lambda r: r["storage"].update(disks=[disk]))
    assert any("is not a plain /dev/disk/by-id/ path" in m for m in messages(findings, WARNING))


def test_cpu_vendor_on_arm64_is_rejected():
    findings = spec_findings(lambda r: (r.update(arch="arm64"), r["nixos-disko"].update(cpu_vendor="intel")))
    assert "cpu_vendor (intel/amd microcode) applies to amd64 only" in messages(findings)


@pytest.mark.parametrize("shell", ["/bin/sh", "/usr/local/bin/zsh", "/bin/tcsh", "/bin/bash2"])
def test_unsupported_shell_is_an_error(shell):
    findings = spec_findings(lambda r: r["users"][0].update(shell=shell))
    assert any("is not supported; use /bin/bash (the default), /bin/zsh or /bin/fish" in m for m in messages(findings))


def test_nobody_could_log_in():
    findings = spec_findings(lambda r: r["users"][0].update(ssh_authorized_keys=[]))
    assert "no user has ssh_authorized_keys or password_hash; nobody could log in" in messages(findings)


def test_arm64_bios_is_rejected():
    findings = spec_findings(lambda r: r.update(arch="arm64", firmware="bios"))
    assert "arm64 boots with UEFI only" in messages(findings)


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: r["nixos-disko"].update(release="nixos-unstable"),
        lambda r: r["nixos-disko"].update(release="26.05"),
        lambda r: r["nixos-disko"].update(layout="zfs"),
        lambda r: r["nixos-disko"].update(swap_size="8GB"),
        lambda r: r["nixos-disko"].update(swap_size="0G"),
        lambda r: r["nixos-disko"].update(packages=["git; rm"]),
        lambda r: r["nixos-disko"].update(packages=["${evil}"]),
        lambda r: r["nixos-disko"].update(cpu_vendor="arm"),
        lambda r: r["nixos-disko"].pop("state_version"),
    ],
)
def test_schema_rejections(edit):
    spec, findings = check_host((lambda r: (edit(r), r)[1])(starter_raw()))
    assert spec is None and messages(findings)


@pytest.mark.parametrize("name", ["root", "nobody", "messagebus", "sshd", "nixbld", "nixbld7"])
def test_system_account_names_are_rejected(name):
    findings = spec_findings(lambda r: r["users"][0].update(name=name))
    assert any("is a NixOS system account" in m for m in messages(findings))


def test_gecos_with_colon_is_rejected():
    findings = spec_findings(lambda r: r["users"][0].update(gecos="Ops: on call"))
    assert "gecos may not contain ':' (the passwd field separator)" in messages(findings)


def test_nix_keyword_package_is_rejected():
    findings = spec_findings(lambda r: r["nixos-disko"].update(packages=["python3Packages.with"]))
    assert any("is a Nix keyword" in m for m in messages(findings))


def test_state_version_newer_than_release_warns():
    findings = spec_findings(lambda r: r["nixos-disko"].update(state_version="27.05"))
    assert any("newer than release" in m for m in messages(findings, WARNING))


# --- render -------------------------------------------------------------------------------


def test_render_files_and_determinism():
    spec = host()
    files = nixos.render(spec, Context("demo"))
    assert sorted(files) == ["configuration.nix", "disko.nix", "flake.nix", "installer.nix"]
    assert nixos.render(copy.deepcopy(spec), Context("demo")) == files
    for text in files.values():
        assert text.endswith("\n") and not any(line != line.rstrip() for line in text.splitlines())
    assert "hardware-configuration.nix" not in files


def test_flake_inputs_and_outputs():
    flake = nixos.render(host(), Context("demo"))["flake.nix"]
    assert 'nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";' in flake
    assert f'disko.url = "github:nix-community/disko/{nixos.DISKO_REF}";' in flake
    assert 'disko.inputs.nixpkgs.follows = "nixpkgs";' in flake
    assert "nixosConfigurations.demo = nixpkgs.lib.nixosSystem {" in flake
    assert "nixosConfigurations.demo-installer = nixpkgs.lib.nixosSystem {" in flake
    assert 'nixpkgs.hostPlatform = "x86_64-linux";' in flake
    arm = nixos.render(host(lambda r: r.update(arch="arm64")), Context("demo"))["flake.nix"]
    assert 'nixpkgs.hostPlatform = "aarch64-linux";' in arm


def test_uefi_renders_systemd_boot_and_esp():
    files = nixos.render(host(), Context("demo"))
    assert "boot.loader.systemd-boot.enable = true;" in files["configuration.nix"]
    assert "grub" not in files["configuration.nix"]
    assert 'type = "EF00";' in files["disko.nix"] and 'size = "512M";' in files["disko.nix"]
    assert 'mountOptions = [ "umask=0077" ];' in files["disko.nix"] and "EF02" not in files["disko.nix"]
    assert "--write-efi-boot-entries" in files["installer.nix"]


def test_bios_renders_grub_and_bios_boot_partition():
    files = nixos.render(host(lambda r: r.update(firmware="bios")), Context("demo"))
    assert "boot.loader.grub.enable = true;" in files["configuration.nix"]
    assert "systemd-boot" not in files["configuration.nix"]
    assert 'type = "EF02";' in files["disko.nix"] and 'size = "1M";' in files["disko.nix"]
    assert "EF00" not in files["disko.nix"] and "--write-efi-boot-entries" not in files["installer.nix"]


def test_swap_present_or_absent():
    with_swap = nixos.render(host(), Context("demo"))["disko.nix"]
    assert 'size = "8G";' in with_swap and 'content = { type = "swap"; };' in with_swap
    without = nixos.render(host(lambda r: r["nixos-disko"].pop("swap_size")), Context("demo"))["disko.nix"]
    assert "swap" not in without


def test_disk_and_root_filesystem():
    disko = nixos.render(host(), Context("demo"))["disko.nix"]
    assert f'device = "{DISK}";' in disko and 'format = "ext4";' in disko and 'size = "100%";' in disko


def test_hardware_configuration_import_is_guarded():
    conf = nixos.render(host(), Context("demo"))["configuration.nix"]
    assert "storeRoot = dirOf (toString ./.) == builtins.storeDir;" in conf
    assert f"if builtins.pathExists ./{nixos.HW_COPY} then [ ./{nixos.HW_COPY} ]" in conf
    assert "else if !storeRoot && builtins.pathExists ../hardware-configuration.nix then [ ../hardware-configuration.nix ]" in conf
    # a human-created rendered/hardware-configuration.nix (which render deletes) is never imported
    assert not re.search(r"(?<!\.)\./hardware-configuration\.nix", conf)
    assert '(modulesPath + "/installer/scan/not-detected.nix")' in conf
    for module in nixos.INITRD_MODULES:
        assert f'"{module}"' in conf


def test_microcode_from_cpu_vendor():
    assert "updateMicrocode" not in nixos.render(host(), Context("demo"))["configuration.nix"]
    conf = nixos.render(host(lambda r: r["nixos-disko"].update(cpu_vendor="amd")), Context("demo"))["configuration.nix"]
    assert "hardware.cpu.amd.updateMicrocode = true;" in conf


def test_users_sudo_shell_and_password():
    def edit(r):
        r["users"][0].update(groups=["video"], password_hash="$6$EXAMPLE$abc")
        r["users"].append({"name": "fishy", "shell": "/usr/bin/fish", "ssh_authorized_keys": []})

    conf = nixos.render(host(edit), Context("demo"))["configuration.nix"]
    assert 'extraGroups = [ "video" "wheel" ];' in conf
    assert 'hashedPassword = "$6$EXAMPLE$abc";' in conf
    assert 'users = [ "ops" ];' in conf and 'options = [ "NOPASSWD" ]' in conf
    assert "shell = pkgs.fish;" in conf and "programs.fish.enable = true;" in conf
    assert "programs.zsh" not in conf and "programs.bash" not in conf


def test_network_static_dhcp_mac_mtu():
    def edit(r):
        r["network"]["interfaces"][0].update(macaddress="02:00:00:00:00:01", mtu=9000)
        r["network"]["interfaces"].append({"name": "eth1", "dhcp4": True, "dhcp6": True})

    conf = nixos.render(host(edit), Context("demo"))["configuration.nix"]
    assert 'matchConfig.PermanentMACAddress = "02:00:00:00:00:01";' in conf and 'linkConfig.Name = "eth0";' in conf
    assert 'address = [ "192.0.2.10/24" "2001:db8::10/64" ];' in conf
    assert 'gateway = [ "192.0.2.1" "2001:db8::1" ];' in conf
    assert 'linkConfig.MTUBytes = "9000";' in conf
    assert 'networkConfig.DHCP = "no";' in conf and 'networkConfig.DHCP = "yes";' in conf
    assert 'networking.nameservers = [ "192.0.2.53" "2001:db8::53" ];' in conf


def test_ssh_hardening_and_basics():
    conf = nixos.render(host(), Context("demo"))["configuration.nix"]
    for needle in (
        "PasswordAuthentication = false;",
        "KbdInteractiveAuthentication = false;",
        'nix.settings.experimental-features = [ "nix-command" "flakes" ];',
        'system.stateVersion = "26.05";',
        'networking.hostName = "demo";',
        'networking.domain = "example.com";',
        'time.timeZone = "Etc/UTC";',
        'i18n.defaultLocale = "en_US.UTF-8";',
        'services.xserver.xkb.layout = "us";',
        "console.useXkbConfig = true;",
        "systemd.network.wait-online.anyInterface = true;",
        "pkgs.git",
    ):
        assert needle in conf, needle


def test_nix_strings_are_escaped():
    assert nixos.nix_str('a"b\\c${x}$y') == '"a\\"b\\\\c\\${x}$y"'
    conf = nixos.render(host(lambda r: r["users"][0].update(gecos='Op "${builtins.abort 1}" \\')), Context("demo"))["configuration.nix"]
    assert 'description = "Op \\"\\${builtins.abort 1}\\" \\\\";' in conf
    assert nixos.nix_attr("1host") == '"1host"' and nixos.nix_attr("demo-1") == "demo-1" and nixos.nix_attr("in") == '"in"'


def test_installer_guard_and_offline_closure():
    inst = nixos.render(host(), Context("demo"))["installer.nix"]
    assert '(modulesPath + "/installer/cd-dvd/installation-cd-minimal.nix")' in inst
    assert 'name = "install-${hostName}";' in inst and 'hostName = "demo";' in inst and f'disk = "{DISK}";' in inst
    assert 'if [ ! -b "$disk" ]; then' in inst
    assert 'lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS "$dev"' in inst
    assert "read -r answer || true" in inst
    guard = inst.index('if [ "$answer" != "$expect" ]; then')
    assert "aborted, nothing was changed" in inst[guard:] and "exit 1" in inst[guard:]
    assert guard < inst.index("exec disko-install")
    assert 'exec disko-install --write-efi-boot-entries --flake ${lib.escapeShellArg "${installFlake}#${hostName}"} --disk main "$disk"' in inst
    for dep in ("toplevel", "toplevel.drvPath", "diskoScript", "diskoScript.drvPath", "stdenv.drvPath", "closureInfo { rootPaths = [ ]; }).drvPath"):
        assert dep in inst, dep
    assert "flakeOutPaths" in inst and 'environment.etc."install-closure".source' in inst
    assert nixos.DISKO_REF_COMMIT in inst
    assert "systemd.services" not in inst and "autologinUser" not in inst


def test_installer_refuses_partitions_and_the_boot_medium():
    inst = nixos.render(host(), Context("demo"))["installer.nix"]
    prompt = inst.index("read -r answer")
    whole = inst.index('if [ "$type" != "disk" ]; then')
    assert 'type=$(lsblk -dno TYPE "$dev")' in inst and whole < prompt
    assert 'refuse "$disk ($dev) is a $type, not a whole disk"' in inst
    assert 'refuse() { echo "install-$host: $*; refusing, nothing was changed" >&2; exit 1; }' in inst
    medium = inst.index('done <<< "$ancestors"')
    assert 'ancestors=$(lsblk -nrpso NAME,TYPE "$medium") || refuse' in inst
    assert 'medium=$(findmnt -no SOURCE /iso) || refuse "cannot read the source of /iso"' in inst
    assert "no block device at /iso; the boot medium was not compared" in inst
    assert 'r=$(readlink -f "$name") || refuse' in inst and 'uid=$(id -u) || refuse' in inst
    assert 'refuse "$disk ($dev) is the USB stick this installer booted from"' in inst and medium < prompt
    mounted = inst.index('mounts=$(lsblk -nro MOUNTPOINTS "$dev") || refuse')
    assert 'refuse "$disk ($dev) or a device on it is mounted or used as swap"' in inst and mounted < prompt
    assert "grep" not in inst and "< <(" not in inst  # no pipe or process substitution that can fail open
    tty = inst.index("if [ ! -t 0 ]; then")
    assert "run it on a terminal; the confirmation must be typed" in inst and tty < prompt
    assert all(f"{probe} || refuse" in inst for probe in ('dev=$(readlink -f "$disk")', 'type=$(lsblk -dno TYPE "$dev")', 'serial=$(lsblk -dno SERIAL "$dev")'))


def test_installer_confirms_with_serial_or_hostname():
    inst = nixos.render(host(), Context("demo"))["installer.nix"]
    assert 'serial=$(printf %s "$serial" | tr -d "[:space:]")' in inst
    assert 'expect=$(printf %s "$serial" | tail -c 4)' in inst
    assert 'what="the last 4 characters of the disk serial ($serial)"' in inst
    assert 'expect="$host"' in inst and "the disk reports no serial" in inst
    assert 'printf "Host: %s  Disk serial: %s\\nType %s to wipe this disk: " "$host" "$shown" "$what"' in inst
    assert "''${" not in inst and "${serial" not in inst  # no bash ${...} inside the Nix '' string


def test_installer_requires_a_committed_flake_lock():
    inst = nixos.render(host(), Context("demo"))["installer.nix"]
    assert "assert lib.assertMsg (builtins.pathExists ./flake.lock)" in inst
    assert "commit rendered/flake.lock (nix flake lock && git add flake.lock) before building the installer" in inst
    assert 'cp ${./flake.lock} "$out/flake.lock"' in inst
    assert "optionalString (builtins.pathExists ./flake.lock)" not in inst  # the copy is unconditional
    assert f'cp ${{../hardware-configuration.nix}} "$out/{nixos.HW_COPY}"' in inst


def test_bios_install_line_writes_no_efi_entries():
    inst = nixos.render(host(lambda r: r.update(firmware="bios")), Context("demo"))["installer.nix"]
    line = next(line for line in inst.splitlines() if "exec disko-install" in line)
    assert line.strip() == 'exec disko-install --flake ${lib.escapeShellArg "${installFlake}#${hostName}"} --disk main "$disk"'
    assert "--write-efi-boot-entries" not in line and '--disk main "$disk"' in line


# --- validate -----------------------------------------------------------------------------


def test_clean_render_validates(rendered):
    findings = nixos.validate(rendered)
    assert messages(findings) == []
    assert messages(findings, SKIPPED) == ["nix not installed (nix-instantiate --parse)"]


@pytest.mark.parametrize(
    "name,edit,needle",
    [
        ("flake.nix", lambda t: t.replace("disko.inputs.nixpkgs.follows", "# no follows"), "follows is missing"),
        ("flake.nix", lambda t: t.replace("nixosConfigurations.demo-installer", "nixosConfigurations.other"), "nixosConfigurations.<host>-installer"),
        ("configuration.nix", lambda t: t.replace('networking.hostName = "demo"', 'networking.hostName = "other"'), "networking.hostName differs"),
        ("configuration.nix", lambda t: t.replace("boot.loader.systemd-boot.enable = true;", ""), "no boot loader"),
        ("configuration.nix", lambda t: t.replace("../hardware-configuration.nix", "../hw.nix"), "guarded hardware-configuration.nix import"),
        ("disko.nix", lambda t: t.replace(DISK, "/dev/disk/by-id/other"), "but disko.nix partitions"),
        ("disko.nix", lambda t: t.replace('mountpoint = "/";', 'mountpoint = "/srv";'), "a root filesystem is missing"),
        ("installer.nix", lambda t: t.replace('if [ "$answer" != "$expect" ]; then', "if false; then"), "the typed confirmation"),
        ("installer.nix", lambda t: t.replace('if [ "$type" != "disk" ]; then', "if false; then"), "the whole-disk check"),
        ("installer.nix", lambda t: t.replace('ancestors=$(lsblk -nrpso NAME,TYPE "$medium") || refuse', "ancestors="), "the installer-medium check"),
        ("installer.nix", lambda t: t.replace('mounts=$(lsblk -nro MOUNTPOINTS "$dev") || refuse', "mounts="), "the mounted-disk check"),
        ("installer.nix", lambda t: t.replace("if [ ! -t 0 ]; then", "if false; then"), "the terminal check"),
        ("installer.nix", lambda t: t.replace("builtins.pathExists ./flake.lock", "true"), "the flake.lock assertion"),
        ("installer.nix", lambda t: t.replace('hostName = "demo";', 'hostName = "x";'), "hostName differs"),
        ("installer.nix", lambda t: t.replace("in\n{\n", 'in\n{\n  systemd.services.go.script = "install-demo";\n'), "must not run anything at boot"),
    ],
)
def test_broken_files_are_caught(rendered, name, edit, needle):
    path = rendered / name
    path.write_text(edit(path.read_text()))
    msgs = messages(nixos.validate(rendered))
    assert any(needle in m for m in msgs), msgs


def test_missing_files(tmp_path):
    msgs = [str(f) for f in nixos.validate(tmp_path)]
    for name in nixos.FILES:
        assert f"error: {name}: missing; run prov render" in msgs


def test_flake_lock_is_checked_when_present(rendered):
    (rendered / "flake.lock").write_text('{"nodes": {"root": {}}, "root": "root", "version": 7}\n')
    assert messages(nixos.validate(rendered)) == []
    (rendered / "flake.lock").write_text("not json\n")
    assert any("invalid JSON" in m for m in messages(nixos.validate(rendered)))


def test_nix_parse_runs_when_present(rendered, monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        bad = argv[-1].endswith("disko.nix")
        err = f"error: syntax error\x1b[31m\n       at {argv[-1]}:2:1:" if bad else ""
        return subprocess.CompletedProcess(argv, 1 if bad else 0, stdout="", stderr=err)

    monkeypatch.setattr(nixos.shutil, "which", lambda name: "/usr/bin/nix-instantiate" if name == "nix-instantiate" else None)
    monkeypatch.setattr(nixos.subprocess, "run", fake_run)
    findings = nixos.validate(rendered)
    assert calls == [["/usr/bin/nix-instantiate", "--readonly-mode", "--store", "dummy://", "--parse", str(rendered / n)] for n in nixos.FILES]
    assert [str(f) for f in findings if f.level == ERROR] == [f"error: disko.nix: nix-instantiate --parse: error: syntax error\\x1b[31m | at {rendered / 'disko.nix'}:2:1:"]


def _which_nix(monkeypatch):
    monkeypatch.setattr(nixos.shutil, "which", lambda name: "/usr/bin/nix-instantiate" if name == "nix-instantiate" else None)


def test_nix_parse_tool_failure_is_skipped_not_an_error(rendered, monkeypatch):
    _which_nix(monkeypatch)
    monkeypatch.setattr(
        nixos.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, 1, stdout="", stderr="error: cannot open connection to remote store")
    )
    findings = nixos.validate(rendered)
    assert messages(findings) == []
    assert messages(findings, SKIPPED) == ["nix-instantiate --parse could not run: error: cannot open connection to remote store"] * 4


def test_nix_parse_timeout_and_oserror(rendered, monkeypatch):
    _which_nix(monkeypatch)

    def timeout(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 60)

    monkeypatch.setattr(nixos.subprocess, "run", timeout)
    assert messages(nixos.validate(rendered)) == ["nix-instantiate --parse timed out after 60 s"] * 4

    def broken(argv, **kw):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(nixos.subprocess, "run", broken)
    assert messages(nixos.validate(rendered)) == ["nix-instantiate could not run: Permission denied"] * 4


def test_nix_parse_is_called_safely(rendered, monkeypatch):
    _which_nix(monkeypatch)
    seen = []

    def fake_run(argv, **kw):
        seen.append(kw)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(nixos.subprocess, "run", fake_run)
    assert messages(nixos.validate(rendered)) == []
    assert all(kw["stdin"] is subprocess.DEVNULL and kw["errors"] == "replace" and kw["timeout"] == 60 for kw in seen)


@pytest.mark.parametrize("hostname", ["in", "1abc"])
def test_quoted_hostnames_render_and_validate(tmp_path, hostname):
    spec = host(lambda r: r.update(hostname=hostname))
    files = nixos.render(spec, Context("demo"))
    assert f'nixosConfigurations."{hostname}" = ' in files["flake.nix"]
    installer = nixos.nix_attr(f"{hostname}-installer")
    assert f"nixosConfigurations.{installer} = " in files["flake.nix"]
    assert f'target = self.nixosConfigurations."{hostname}";' in files["installer.nix"]
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    assert messages(nixos.validate(tmp_path)) == []


# --- CLI: init/render/validate/plan, preserved lock, hardware file ------------------------


@pytest.fixture
def item(tmp_path, capsys):
    root = str(tmp_path)
    assert run(capsys, "--root", root, "init", "os-install/demo", "--backend", "nixos-disko")[0] == 0
    assert run(capsys, "--root", root, "render", "os-install/demo")[0] == 0
    return tmp_path, tmp_path / "os-install/demo"


def test_fresh_root_round_trip(item, capsys):
    root, idir = item
    code, out, err = run(capsys, "--root", str(root), "validate", "os-install/demo")
    assert code == 0 and "ok: os-install/demo" in out and "skipped: nix not installed" in out


def test_render_keeps_flake_lock_and_hardware_file(item, capsys):
    root, idir = item
    lock = '{"nodes": {"root": {}}, "root": "root", "version": 7}\n'
    (idir / "rendered/flake.lock").write_text(lock)
    (idir / "hardware-configuration.nix").write_text("{ }\n")
    (idir / "rendered/stray.txt").write_text("x\n")
    code, out, _ = run(capsys, "--root", str(root), "render", "os-install/demo")
    assert code == 0 and "removed stray.txt" in out and "flake.lock" not in out
    assert (idir / "rendered/flake.lock").read_text() == lock
    assert (idir / "hardware-configuration.nix").read_text() == "{ }\n"
    assert run(capsys, "--root", str(root), "diff", "os-install/demo")[0] == 0
    assert run(capsys, "--root", str(root), "validate", "os-install/demo")[0] == 0


def _forbidden(*args, **kwargs):
    raise AssertionError(f"prov plan tried to execute something: {args!r}")


@pytest.mark.parametrize("firmware", ["uefi", "bios"])
def test_plan_order_and_no_execution(tmp_path, capsys, monkeypatch, firmware):
    root = str(tmp_path)
    assert run(capsys, "--root", root, "init", "os-install/demo", "--backend", "nixos-disko")[0] == 0
    toml = tmp_path / "os-install/demo/spec.toml"
    toml.write_text(toml.read_text().replace('firmware = "uefi"', f'firmware = "{firmware}"'))
    assert run(capsys, "--root", root, "render", "os-install/demo")[0] == 0
    before = sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))
    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        monkeypatch.setattr(subprocess, name, _forbidden)
    for name in ("system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnve", "posix_spawn", "fork"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _forbidden)
    code, out, _ = run(capsys, "--root", root, "plan", "os-install/demo")
    assert code == 0
    lines = out.splitlines()
    order = [
        "#        nixos-generate-config --no-filesystems --show-hardware-config > hardware-configuration.nix",
        "git add . ../spec.toml",
        "nix --extra-experimental-features 'nix-command flakes' flake lock",
        "nix --extra-experimental-features 'nix-command flakes' build .#nixosConfigurations.demo-installer.config.system.build.isoImage"
        ' --out-link "$HOME"/prov-iso/demo-installer',
        "USB=/dev/disk/by-id/usb-CHANGE-ME",
        'case "$USB" in *-part[0-9]*) echo "USB=$USB is a partition; use the whole stick" >&2 ;; '
        '*) if [ -b "$USB" ]; then sudo dd if="$ISO" of="$USB" bs=4M conv=fsync status=progress; '
        'else echo "USB=$USB is not a block device; set USB first" >&2; fi ;; esac',
        "sudo install-demo",
        "nixos-version",
    ]
    positions = [lines.index(line) for line in order]
    assert positions == sorted(positions)
    assert any("WIPES the stick" in line for line in lines) and any("GUI writer" in line for line in lines)
    assert any(line.startswith("# 5. WARNING: the install WIPES " + DISK) for line in lines)
    assert ("UEFI mode" in out) == (firmware == "uefi")
    assert "if [ -f ../hardware-configuration.nix ]; then git add ../hardware-configuration.nix; fi" in lines
    assert sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*")) == before
    # no default ./result link inside the item; no bare <placeholder> a paste could read as a redirect
    build = next(line for line in lines if " build .#" in line)
    assert "--out-link" in build and not shlex.split(build)[-1].startswith((".", "result"))
    assert not any("result" in line for line in lines if not line.startswith("#"))
    assert not any("<" in line for line in lines if not line.startswith("#"))


def test_plan_without_a_static_address_uses_a_variable():
    spec = host(lambda r: r["network"]["interfaces"][0].update(addresses=[], dhcp4=True))
    lines = nixos.plan(spec, Context("demo"))
    assert lines[lines.index("HOST_IP=CHANGE-ME") + 1] == 'ssh ops@"$HOST_IP"'


def test_plan_quotes_hostile_values(tmp_path):
    spec = host()
    spec["hostname"] = "demo;touch${IFS}/tmp/pwned"
    spec["storage"]["disks"] = ["/dev/x;rm${IFS}-rf${IFS}~;#"]
    spec["users"][0]["name"] = "ops$(id)"
    spec["network"]["interfaces"][0]["addresses"] = ["192.0.2.10/24;reboot"]
    lines = plan_lines(nixos, spec, Context("demo"), "os-install/demo", tmp_path)
    for line in lines:
        if line and not line.startswith("#"):
            tokens = shlex.split(line)
            assert not {"rm", "-rf", "touch", "id", "reboot"} & set(tokens), line
    joined = "\n".join(lines)
    for value in ("/dev/x;rm${IFS}-rf${IFS}~;#", "demo;touch${IFS}/tmp/pwned"):
        assert shlex.quote(value) in joined
    install = next(line for line in lines if line.startswith("sudo install-") or line.startswith("sudo 'install-"))
    assert shlex.split(install) == ["sudo", "install-demo;touch${IFS}/tmp/pwned"]


@pytest.mark.parametrize("field", ["hostname", "disk", "user"])
def test_plan_refuses_values_with_line_breaks(tmp_path, field):
    from prov.schema import ConfigError

    spec = host()
    if field == "hostname":
        spec["hostname"] = "demo\nrm -rf ~"
    elif field == "disk":
        spec["storage"]["disks"] = ["/dev/vda\nrm -rf ~"]
    else:
        spec["users"][0]["name"] = "ops\rrm -rf ~"
    with pytest.raises(ConfigError, match="line break"):
        plan_lines(nixos, spec, Context("demo"), "os-install/demo", tmp_path)


# --- second review: pairing, warnings, guards ---------------------------------------------


def test_hostname_ending_in_installer_validates(tmp_path, capsys):
    root = str(tmp_path)
    assert run(capsys, "--root", root, "init", "os-install/box-installer", "--backend", "nixos-disko")[0] == 0
    assert run(capsys, "--root", root, "render", "os-install/box-installer")[0] == 0
    code, out, err = run(capsys, "--root", root, "validate", "os-install/box-installer")
    assert code == 0 and "ok: os-install/box-installer" in out, err
    flake = (tmp_path / "os-install/box-installer/rendered/flake.nix").read_text()
    assert "nixosConfigurations.box-installer = " in flake and "nixosConfigurations.box-installer-installer = " in flake


def test_eth_name_without_macaddress_warns():
    findings = spec_findings(lambda r: r["network"]["interfaces"][0].pop("macaddress"))
    assert messages(findings) == []
    assert any("'eth0' has no macaddress" in m for m in messages(findings, WARNING))
    renamed = spec_findings(lambda r: (r["network"]["interfaces"][0].pop("macaddress"), r["network"]["interfaces"][0].update(name="enp3s0")))
    assert not messages(renamed, WARNING)


def test_starter_names_its_nic_by_a_synthetic_mac():
    iface = host()["network"]["interfaces"][0]
    assert iface["macaddress"] == "02:00:00:00:00:01" and int(iface["macaddress"][:2], 16) & 0b10  # locally administered


def test_installer_reads_the_parent_hardware_file_only_outside_the_store_root():
    inst = nixos.render(host(), Context("demo"))["installer.nix"]
    assert "storeRoot = dirOf (toString ./.) == builtins.storeDir;" in inst
    assert "lib.optionalString (!storeRoot && builtins.pathExists ../hardware-configuration.nix)" in inst


def test_keyboard_is_an_xkb_layout():
    conf = nixos.render(host(lambda r: r.update(keyboard="de")), Context("demo"))["configuration.nix"]
    assert 'services.xserver.xkb.layout = "de";' in conf and "console.useXkbConfig = true;" in conf
    assert "console.keyMap" not in conf and "services.xserver.enable" not in conf


# --- behaviour of the rendered install script (stubbed lsblk/findmnt/readlink/id) ---------

STUBS = {
    "id": 'if [ -n "${STUB_ID_FAIL:-}" ]; then exit 1; fi; echo "${STUB_UID-0}"\n',
    "readlink": 'p="${@: -1}"; if [ "$p" = "${STUB_LINK_FROM:-}" ]; then echo "$STUB_LINK_TO"; else echo "$p"; fi\n',
    "findmnt": 'if [ -n "${STUB_FINDMNT_FAIL:-}" ]; then exit 1; fi; printf "%s\\n" "$STUB_MEDIUM"\n',
    "mountpoint": '[ "${STUB_ISO_MOUNTED:-1}" = 1 ]\n',
    "lsblk": r'''case "$*" in *"${STUB_FAIL:-@@}"*) echo "lsblk: failed" >&2; exit 32 ;; esac
# the partition-label scan lists every device: NAME PARTLABEL lines from STUB_PARTS
if [ "$*" = "-nrpo NAME,PARTLABEL" ]; then printf "%b" "${STUB_PARTS:-}"; exit 0; fi
# the ancestor walk of a labelled partition <disk>p<N>: the partition, then its disk
# (STUB_PART_ANCESTORS overrides the lines; STUB_PART_FAIL=1 makes this walk fail)
case "$*" in "-nrpso NAME,TYPE "*) if [ "${@: -1}" != "$STUB_MEDIUM" ]; then
  if [ -n "${STUB_PART_FAIL:-}" ]; then echo "lsblk: failed" >&2; exit 32; fi
  if [ -n "${STUB_PART_ANCESTORS:-}" ]; then printf "%b" "$STUB_PART_ANCESTORS"; exit 0; fi
  p="${@: -1}"; printf "%s part\n%s disk\n" "$p" "${p%p[0-9]*}"; exit 0; fi ;; esac
# the device argument must be the resolved target disk, or the medium for the ancestor walk
want="$STUB_DEV"; case "$*" in "-nrpso NAME,TYPE "*) want="$STUB_MEDIUM" ;; esac
if [ "${@: -1}" != "$want" ]; then echo "lsblk stub: called on ${@: -1}, expected $want" >&2; exit 99; fi
case "$*" in
  "-dno TYPE "*) echo "$STUB_TYPE" ;;
  "-nro MOUNTPOINTS "*) printf "%b" "$STUB_MOUNTS" ;;
  "-nrpso NAME,TYPE "*) printf "%b" "$STUB_ANCESTORS" ;;
  "-dno SERIAL "*) echo "$STUB_SERIAL" ;;
  *) echo "lsblk $*" ;;
esac
''',
    "disko-install": 'printf "%s\\n" "$@" > "$STUB_LOG"\n',
    "vgs": 'if [ -n "${STUB_VGS_FAIL:-}" ]; then echo "vgs: failed" >&2; exit 5; fi; printf "%b" "${STUB_VGS:-}"\n',
    # cryptsetup status <name>: 0 when the mapping is active, 4 otherwise
    "cryptsetup": 'if [ "$1 $2" = "status crypted" ] && [ "${STUB_CRYPT_OPEN:-}" = 1 ]; then exit 0; fi; exit "${STUB_CRYPT_RC:-4}"\n',
}


def _script(firmware, disk, tmp_path, layout="ext4"):
    """The install-<host> script as writeShellApplication would run it.

    Nix interpolations are substituted as Nix would; the three file-type tests that cannot
    be stubbed ([ -b ] on the disk and the medium, [ -t 0 ]) become [ -e ] / always-true.
    """
    if layout == "ext4-luks":
        inst = nixos.render(luks_host(lambda r: r.update(firmware=firmware)), Context("demo"))["installer.nix"]
    else:
        inst = nixos.render(host(lambda r: r.update(firmware=firmware)), Context("demo"))["installer.nix"]
    body = inst.split("    text = ''\n", 1)[1].split("\n    '';\n", 1)[0]
    lines = [line[6:] if line.startswith("      ") else line for line in body.split("\n")]
    text = "\n".join(lines)

    def sub(old, new):
        nonlocal text
        assert old in text, f"script no longer contains {old!r}"
        text = text.replace(old, new)

    sub("${lib.escapeShellArg disk}", shlex.quote(disk))
    sub("${lib.escapeShellArg hostName}", "demo")
    sub('${lib.escapeShellArg "${installFlake}#${hostName}"}', "/nix/store/fake-install-flake#demo")
    assert "${" not in text, "an unsubstituted Nix interpolation is left"
    sub('[ ! -b "$disk" ]', '[ ! -e "$disk" ]')
    sub('[ -b "$medium" ]', '[ -e "$medium" ]')
    sub("[ ! -t 0 ]", '[ -n "${STUB_NOTTY:-}" ]')
    if layout == "ext4-luks":
        sub("[ -e /dev/mapper/crypted ]", '[ -e "$STUB_MAPPER" ]')
    path = tmp_path / "install-demo"
    path.write_text("#!/usr/bin/env bash\nset -o errexit -o nounset -o pipefail\n" + text + "\n")
    return path


@pytest.fixture
def script_env(tmp_path):
    bash = BASH
    if bash is None:
        pytest.skip("bash not available")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in STUBS.items():
        (bin_dir / name).write_text("#!/usr/bin/env bash\n" + body)
        (bin_dir / name).chmod(0o755)
    disk = tmp_path / "disk-by-id"
    disk.write_text("")
    medium = tmp_path / "iso-medium"
    medium.write_text("")
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "STUB_TYPE": "disk",
        "STUB_MOUNTS": "",
        "STUB_MEDIUM": str(medium),
        "STUB_ANCESTORS": f"{medium} part\\n/dev/sdz disk\\n",
        "STUB_SERIAL": "EXAMPLE-SERIAL-9876",
        "STUB_LOG": str(tmp_path / "disko-install.args"),
        "STUB_DEV": str(disk),
        "STUB_MAPPER": str(tmp_path / "no-such-mapper"),
    }

    def run_script(firmware="uefi", answer="9876\n", layout="ext4", **overrides):
        script = _script(firmware, str(disk), tmp_path, layout)
        proc = subprocess.run(
            [bash, str(script)], input=answer, capture_output=True, text=True, timeout=30, env={**env, **overrides}
        )
        log = tmp_path / "disko-install.args"
        args = log.read_text().splitlines() if log.exists() else None
        if log.exists():
            log.unlink()
        return proc, args

    return run_script, str(disk), str(medium)


def test_script_refuses_a_partition(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_TYPE="part")
    assert proc.returncode == 1 and "is a part, not a whole disk; refusing" in proc.stderr and args is None


def test_script_refuses_a_disk_with_something_mounted(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_MOUNTS="/mnt\n")
    assert proc.returncode == 1 and "mounted or used as swap; refusing" in proc.stderr and args is None


def test_script_refuses_the_boot_medium_two_levels_down(script_env):
    run_script, disk, medium = script_env
    # /iso on a dm device on a partition of the target disk (Ventoy-style)
    proc, args = run_script(STUB_ANCESTORS=f"{medium} dm\\n/dev/sdz1 part\\n{disk} disk\\n")
    assert proc.returncode == 1 and "is the USB stick this installer booted from; refusing" in proc.stderr and args is None


def test_script_refuses_without_a_terminal(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_NOTTY="1")
    assert proc.returncode == 1 and "run it on a terminal" in proc.stderr and args is None


@pytest.mark.parametrize("answer", ["nope\n", "demo\n", "", "98765\n"])
def test_script_aborts_on_a_wrong_confirmation(script_env, answer):
    run_script, _, _ = script_env
    proc, args = run_script(answer=answer)
    assert proc.returncode == 1 and "confirmation did not match; aborted, nothing was changed" in proc.stderr and args is None
    if answer:
        assert "Disk serial: EXAMPLE-SERIAL-9876" in proc.stdout and "last 4 characters of the disk serial" in proc.stdout


@pytest.mark.parametrize("firmware", ["uefi", "bios"])
def test_script_runs_disko_install_after_the_serial(script_env, firmware):
    run_script, disk, _ = script_env
    proc, args = run_script(firmware=firmware)
    assert proc.returncode == 0, proc.stderr
    efi = ["--write-efi-boot-entries"] if firmware == "uefi" else []
    assert args == efi + ["--flake", "/nix/store/fake-install-flake#demo", "--disk", "main", disk]


def test_script_falls_back_to_the_hostname_without_a_serial(script_env):
    run_script, disk, _ = script_env
    proc, args = run_script(STUB_SERIAL="", answer="demo\n")
    assert proc.returncode == 0, proc.stderr
    assert "the hostname (demo; the disk reports no serial)" in proc.stdout and "Disk serial: none" in proc.stdout
    assert args[-1] == disk



# --- round 3: fail closed ----------------------------------------------------------------


@pytest.mark.parametrize(
    "probe,needle",
    [
        ("-dno TYPE", "cannot read the type of"),
        ("-nro MOUNTPOINTS", "cannot read the mounts on"),
        ("-nrpso NAME,TYPE", "cannot read the devices the boot medium"),
        ("-dno SERIAL", "cannot read the serial of"),
    ],
)
def test_script_refuses_when_lsblk_fails(script_env, probe, needle):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_FAIL=probe)
    assert proc.returncode == 1 and needle in proc.stderr and "refusing, nothing was changed" in proc.stderr and args is None


def test_script_finds_a_mount_among_blank_lines(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_MOUNTS="\\n\\n/mnt\\n")
    assert proc.returncode == 1 and "mounted or used as swap; refusing" in proc.stderr and args is None


def test_script_blank_mount_lines_are_not_mounts(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_MOUNTS="\\n\\n\\n")
    assert proc.returncode == 0 and args is not None, proc.stderr


def test_script_refuses_a_swap_child(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_MOUNTS="\\n[SWAP]\\n")
    assert proc.returncode == 1 and "mounted or used as swap; refusing" in proc.stderr and args is None


def test_script_refuses_without_root(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_UID="1000")
    assert proc.returncode == 1 and "run it as root: sudo install-demo" in proc.stderr and args is None


def test_script_refuses_a_missing_disk(script_env):
    run_script, disk, _ = script_env
    os.unlink(disk)
    proc, args = run_script()
    assert proc.returncode == 1 and "not found; nothing was changed" in proc.stderr and args is None


def test_script_refuses_a_by_id_link_to_the_boot_medium(script_env):
    run_script, disk, medium = script_env
    # the by-id path resolves to /dev/sdz, the disk the /iso medium partition sits on
    proc, args = run_script(STUB_LINK_FROM=disk, STUB_LINK_TO="/dev/sdz", STUB_DEV="/dev/sdz", STUB_ANCESTORS=f"{medium} part\\n/dev/sdz disk\\n")
    assert proc.returncode == 1 and "(/dev/sdz) is the USB stick this installer booted from; refusing" in proc.stderr and args is None



# --- round 4: /iso without a block device -------------------------------------------------


def test_script_notes_when_iso_is_not_mounted(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_ISO_MOUNTED="0")
    assert proc.returncode == 0 and args is not None, proc.stderr
    assert "no block device at /iso; the boot medium was not compared" in proc.stderr


def test_script_notes_when_iso_is_not_a_block_device(script_env, tmp_path):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_MEDIUM="tmpfs")  # copytoram: /iso is a tmpfs
    assert proc.returncode == 0 and args is not None, proc.stderr
    assert "no block device at /iso; the boot medium was not compared" in proc.stderr


def test_script_refuses_when_iso_is_mounted_but_unreadable(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_FINDMNT_FAIL="1")
    assert proc.returncode == 1 and "cannot read the source of /iso; refusing" in proc.stderr and args is None


def test_script_refuses_when_id_fails(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_ID_FAIL="1")
    assert proc.returncode == 1 and "cannot read the user id; refusing" in proc.stderr and args is None


@pytest.mark.parametrize("uid", ["", "abc", "1000"])
def test_script_treats_any_non_zero_uid_as_non_root(script_env, uid):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_UID=uid)
    assert proc.returncode == 1 and "run it as root" in proc.stderr and args is None


def test_plan_refuses_a_usb_partition_in_its_dd_line():
    lines = nixos.plan(host(), Context("demo"))
    dd = next(line for line in lines if line.startswith('case "$USB" in'))
    assert '*-part[0-9]*) echo "USB=$USB is a partition; use the whole stick" >&2 ;;' in dd
    assert any("Unmount the stick's partitions first" in line for line in lines)


def test_boot_loader_generation_limit():
    assert "boot.loader.systemd-boot.configurationLimit = 10;" in nixos.render(host(), Context("demo"))["configuration.nix"]
    bios = nixos.render(host(lambda r: r.update(firmware="bios")), Context("demo"))["configuration.nix"]
    assert "boot.loader.grub.configurationLimit = 10;" in bios and 'size = "512M";' in nixos.render(host(), Context("demo"))["disko.nix"]


# --- ext4-luks, volumes, hibernate, esp_size -----------------------------------------------


def _luks_raw(raw):
    raw["nixos-disko"].update(layout="ext4-luks", root_size="64G", hibernate=True)
    raw["nixos-disko"]["volumes"] = [
        {"name": "data", "mountpoint": "/srv/data", "size": "200G", "mount_options": ["noatime", "nodev"], "owner": "ops"},
        {"name": "home", "mountpoint": "/home", "size": "100%FREE"},
    ]


def luks_findings(edit=None):
    return spec_findings(lambda r: (_luks_raw(r), edit and edit(r)))


def luks_host(edit=None):
    return host(lambda r: (_luks_raw(r), edit and edit(r)))


def vol(r, i):
    return r["nixos-disko"]["volumes"][i]


def test_luks_spec_is_valid_without_findings():
    assert luks_findings() == []


def test_new_keys_default_to_the_old_output():
    explicit = host(lambda r: r["nixos-disko"].update(esp_size="512M", hibernate=False, root_size="100%FREE", volumes=[]))
    assert explicit == host()
    assert nixos.render(explicit, Context("demo")) == nixos.render(host(), Context("demo"))
    assert host()["nixos-disko"]["esp_size"] == "512M" and host()["nixos-disko"]["volumes"] == []


@pytest.mark.parametrize(
    "edit,where,needle",
    [
        (lambda r: r.update(firmware="bios"), "firmware", "GRUB unlocking an encrypted /boot (cryptodisk) is not supported"),
        (lambda r: r["nixos-disko"].pop("swap_size"), "nixos-disko.hibernate", "hibernate needs swap_size"),
        (lambda r: r["nixos-disko"].update(esp_size="128M"), "nixos-disko.esp_size", "esp_size 128M is too small; use at least 256M"),
        (lambda r: vol(r, 1).update(size="10G"), "nixos-disko.volumes", "must be 100%FREE (the space left); none does"),
        (lambda r: r["nixos-disko"].update(root_size="100%FREE"), "nixos-disko.volumes", "several do: 'root', 'home'"),
        (lambda r: vol(r, 0).update(name="root"), "nixos-disko.volumes[0].name", "is reserved"),
        (lambda r: vol(r, 0).update(name="swap"), "nixos-disko.volumes[0].name", "is reserved"),
        (lambda r: vol(r, 0).update(name="home"), "nixos-disko.volumes[1].name", "'home' is already used by volumes[0]"),
        (lambda r: vol(r, 0).update(mountpoint="/home"), "nixos-disko.volumes[1].mountpoint", "'/home' is already used by volumes[0]"),
        (lambda r: vol(r, 0).update(mountpoint="/"), "nixos-disko.volumes[0].mountpoint", "is reserved"),
        (lambda r: vol(r, 0).update(mountpoint="/boot"), "nixos-disko.volumes[0].mountpoint", "is reserved"),
        (lambda r: vol(r, 0).update(mountpoint="/boot/efi"), "nixos-disko.volumes[0].mountpoint", "is reserved"),
        (lambda r: vol(r, 0).update(mountpoint="/nix"), "nixos-disko.volumes[0].mountpoint", "is reserved"),
        (lambda r: vol(r, 0).update(mountpoint="/srv//data"), "nixos-disko.volumes[0].mountpoint", "is not normalised"),
        (lambda r: vol(r, 0).update(mountpoint="/srv/data/"), "nixos-disko.volumes[0].mountpoint", "is not normalised"),
        (lambda r: vol(r, 0).update(mountpoint="/srv/../boot"), "nixos-disko.volumes[0].mountpoint", "is not normalised"),
        (lambda r: vol(r, 0).update(mountpoint="/srv/./data"), "nixos-disko.volumes[0].mountpoint", "is not normalised"),
        (lambda r: vol(r, 0).update(owner="mallory"), "nixos-disko.volumes[0].owner", "owner 'mallory' is not a [[users]] name"),
    ],
)
def test_luks_check_rules(edit, where, needle):
    findings = [f for f in luks_findings(edit) if f.level == ERROR]
    assert any(f.where == where and needle in f.message for f in findings), findings


def test_volumes_and_root_size_need_ext4_luks():
    findings = spec_findings(lambda r: r["nixos-disko"].update(volumes=[{"name": "data", "mountpoint": "/srv", "size": "1G"}]))
    assert any("volumes need layout 'ext4-luks'" in m for m in messages(findings))
    findings = spec_findings(lambda r: r["nixos-disko"].update(root_size="20G"))
    assert any("root_size applies to layout 'ext4-luks' only" in m for m in messages(findings))


def test_ext4_hibernate_needs_swap_and_esp_size_is_checked_on_uefi_only():
    assert "hibernate needs swap_size (at least the RAM size) to resume from" in messages(
        spec_findings(lambda r: (r["nixos-disko"].pop("swap_size"), r["nixos-disko"].update(hibernate=True)))
    )
    assert messages(spec_findings(lambda r: (r.update(firmware="bios"), r["nixos-disko"].update(esp_size="100M")))) == [
        "esp_size applies to firmware 'uefi' only; BIOS uses a 1M BIOS boot partition"
    ]
    assert messages(spec_findings(lambda r: (r.update(firmware="bios"), r["nixos-disko"].update(esp_size="512M")))) == []


@pytest.mark.parametrize(
    "edit",
    [
        lambda r: vol(r, 0).update(mount_options=["noatime,exec"]),
        lambda r: vol(r, 0).update(mount_options=["x y"]),
        lambda r: vol(r, 0).update(mount_options=['a"b']),
        lambda r: vol(r, 0).update(mount_options=["${x}"]),
        lambda r: vol(r, 0).update(name="Data"),
        lambda r: vol(r, 0).update(name="da_ta"),
        lambda r: vol(r, 0).update(mountpoint="srv/data"),
        lambda r: vol(r, 0).update(mountpoint="/srv/my data"),
        lambda r: vol(r, 0).update(size="100%"),
        lambda r: vol(r, 0).update(size="0G"),
        lambda r: vol(r, 0).update(extra=1),
        lambda r: vol(r, 0).pop("mountpoint"),
        lambda r: r["nixos-disko"].update(esp_size="512MB"),
        lambda r: r["nixos-disko"].update(root_size="50%FREE"),
        lambda r: r["nixos-disko"].update(layout="luks"),
    ],
)
def test_luks_schema_rejections(edit):
    raw = starter_raw()
    _luks_raw(raw)
    edit(raw)
    spec, findings = check_host(raw)
    assert spec is None and messages(findings)


def test_luks_disko_layout():
    disko = nixos.render(luks_host(), Context("demo"))["disko.nix"]
    for needle in (
        f'device = "{DISK}";',
        'type = "EF00";',
        'size = "512M";',
        'mountOptions = [ "umask=0077" ];',
        "luks = {",
        'type = "luks";',
        'name = "crypted";',
        "settings.allowDiscards = true;",
        'content = { type = "lvm_pv"; vg = "pool"; };',
        "disko.devices.lvm_vg.pool = {",
        'type = "lvm_vg";',
        'content = { type = "swap"; resumeDevice = true; };',
        'size = "64G";',
        'mountpoint = "/";',
        'mountpoint = "/srv/data";',
        'mountOptions = [ "noatime" "nodev" ];',
        'size = "100%FREE";',
        'mountpoint = "/home";',
    ):
        assert needle in disko, needle
    assert disko.count('format = "ext4";') == 3
    assert not nixos._SECRET.search(disko) and "askPassword" not in disko
    # the ESP size follows esp_size; the luks partition takes the rest of the disk
    sized = nixos.render(luks_host(lambda r: r["nixos-disko"].update(esp_size="1G")), Context("demo"))["disko.nix"]
    assert 'size = "1G";' in sized and sized.count('size = "100%";') == 1


def test_resume_device_only_with_hibernate():
    disko = nixos.render(luks_host(lambda r: r["nixos-disko"].update(hibernate=False)), Context("demo"))["disko.nix"]
    assert "resumeDevice" not in disko and 'content = { type = "swap"; };' in disko
    no_swap = nixos.render(luks_host(lambda r: (r["nixos-disko"].pop("swap_size"), r["nixos-disko"].update(hibernate=False))), Context("demo"))
    assert "swap" not in no_swap["disko.nix"]
    ext4 = nixos.render(host(lambda r: r["nixos-disko"].update(hibernate=True)), Context("demo"))["disko.nix"]
    assert 'content = { type = "swap"; resumeDevice = true; };' in ext4 and "luks" not in ext4
    assert "resumeDevice" not in nixos.render(host(), Context("demo"))["disko.nix"]


def test_luks_configuration():
    conf = nixos.render(luks_host(), Context("demo"))["configuration.nix"]
    # nixpkgs' default initrd modules already carry hid_generic/usbhid (checked by nix eval)
    assert 'boot.initrd.availableKernelModules = [ "nvme" "ahci" "xhci_pci" "usb_storage" "sd_mod" "usbhid" ];' in conf
    assert "hid_generic" not in conf
    assert "console.earlySetup = true;" in conf
    assert "boot.initrd.luks.devices.crypted =" not in conf and "boot.resumeDevice =" not in conf  # disko sets both
    plain = nixos.render(host(), Context("demo"))["configuration.nix"]
    assert "hid_generic" not in plain and "earlySetup" not in plain and "tmpfiles" not in plain


def test_tmpfiles_rules_for_owned_volumes_are_sorted():
    def edit(r):
        r["users"].append({"name": "bob", "ssh_authorized_keys": []})
        r["nixos-disko"]["volumes"].insert(0, {"name": "zz", "mountpoint": "/srv/zz", "size": "1G", "owner": "bob"})

    conf = nixos.render(luks_host(edit), Context("demo"))["configuration.nix"]
    block = conf[conf.index("systemd.tmpfiles.rules = [") :].split("];", 1)[0].splitlines()[1:]
    assert [line.strip() for line in block if line.strip()] == ['"d /srv/data 0750 ops users -"', '"d /srv/zz 0750 bob users -"']


def test_luks_installer_announces_the_passphrase_prompt():
    inst = nixos.render(luks_host(), Context("demo"))["installer.nix"]
    echo = inst.index('echo "install-$host: disko will now ask for the new disk passphrase twice"')
    assert inst.index('if [ "$answer" != "$expect" ]; then') < echo < inst.index("exec disko-install")
    assert "passphrase" not in nixos.render(host(), Context("demo"))["installer.nix"]


@pytest.fixture
def luks_rendered(tmp_path):
    for name, text in nixos.render(luks_host(), Context("demo")).items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_clean_luks_render_validates(luks_rendered):
    assert messages(nixos.validate(luks_rendered)) == []


@pytest.mark.parametrize(
    "name,edit,needle",
    [
        ("disko.nix", lambda t: t.replace('type = "luks";', 'type = "filesystem";'), "the luks partition is missing"),
        ("disko.nix", lambda t: t.replace('name = "crypted";', 'name = "other";'), "the LUKS name crypted is missing"),
        ("disko.nix", lambda t: t.replace("disko.devices.lvm_vg.pool = {", "disko.devices.lvm_vg.other = {"), "disko.devices.lvm_vg.pool is missing"),
        ("disko.nix", lambda t: t.replace('vg = "pool";', 'vg = "other";'), "physical volume of volume group pool is missing"),
        ("disko.nix", lambda t: t.replace(DISK, "/dev/disk/by-id/other"), "but disko.nix partitions"),
        ("disko.nix", lambda t: t.replace("settings.allowDiscards = true;", 'passwordFile = "/tmp/secret.key";'), "passwordFile ="),
        ("disko.nix", lambda t: t.replace("settings.allowDiscards = true;", 'settings.keyFile = "/tmp/secret.key";'), "keyFile ="),
        ("configuration.nix", lambda t: t.replace("isNormalUser = true;", 'isNormalUser = true; password = "x";'), "password ="),
        ("installer.nix", lambda t: t.replace("in\n{\n", 'in\n{\n  users.users.nixos.initialPassword = "x";\n'), "initialPassword ="),
    ],
)
def test_broken_luks_files_are_caught(luks_rendered, name, edit, needle):
    path = luks_rendered / name
    text = path.read_text()
    assert edit(text) != text
    path.write_text(edit(text))
    msgs = messages(nixos.validate(luks_rendered))
    assert any(needle in m for m in msgs), msgs


def test_luks_plan_lines():
    lines = nixos.plan(luks_host(), Context("demo"))
    install = lines.index("sudo install-demo")
    warn = [i for i, line in enumerate(lines) if "asks TWICE for a NEW disk passphrase" in line]
    assert warn and warn[0] < install and "cannot be recovered" in lines[warn[0] + 1] and "password manager" in lines[warn[0] + 1]
    post = lines[install:]
    for line in ("findmnt /", "findmnt /boot", "findmnt /srv/data", "findmnt /home", "swapon --show", "sudo cryptsetup status crypted", "sudo lvs", "cat /sys/power/disk"):
        assert line in post, line
    assert any("systemctl hibernate" in line for line in post) and "swap_size should be at least the RAM size" in " ".join(line.lstrip("# ") for line in post)
    plain = nixos.plan(host(), Context("demo"))
    assert not any("passphrase" in line or "cryptsetup" in line or "/sys/power" in line for line in plain)
    no_hib = nixos.plan(luks_host(lambda r: r["nixos-disko"].update(hibernate=False)), Context("demo"))
    assert "sudo lvs" in no_hib and "cat /sys/power/disk" not in no_hib


def test_luks_plan_quotes_hostile_values(tmp_path):
    spec = luks_host()
    spec["nixos-disko"]["volumes"][0]["mountpoint"] = "/srv/x;reboot"
    spec["nixos-disko"]["swap_size"] = "8G;reboot"
    lines = plan_lines(nixos, spec, Context("demo"), "os-install/demo", tmp_path)
    for line in lines:
        if line and not line.startswith("#"):
            assert "reboot" not in shlex.split(line), line
    assert "findmnt '/srv/x;reboot'" in lines and any("'8G;reboot'" in line for line in lines)


def test_luks_hostile_mountpoint_is_escaped_in_nix():
    spec = luks_host()
    spec["nixos-disko"]["volumes"][0]["mountpoint"] = '/srv/"${builtins.abort 1}"'
    files = nixos.render(spec, Context("demo"))
    assert 'mountpoint = "/srv/\\"\\${builtins.abort 1}\\"";' in files["disko.nix"]
    assert '"d /srv/\\"\\${builtins.abort 1}\\" 0750 ops users -"' in files["configuration.nix"]


def test_luks_example_cli_round_trip(capsys):
    from conftest import EXAMPLES

    root = str(EXAMPLES)
    code, out, err = run(capsys, "--root", root, "validate", "os-install/demo-nixos-gnome-luks")
    assert code == 0 and "ok: os-install/demo-nixos-gnome-luks" in out, err
    assert run(capsys, "--root", root, "diff", "os-install/demo-nixos-gnome-luks")[0] == 0
    code, out, _ = run(capsys, "--root", root, "plan", "os-install/demo-nixos-gnome-luks")
    assert code == 0 and "sudo install-demo-nixos-gnome-luks" in out and "sudo cryptsetup status crypted" in out


def test_starter_hints_for_the_new_keys_are_comments():
    text = nixos.starter("demo")
    for key in ("esp_size", "hibernate", "ext4-luks", "root_size", "[[nixos-disko.volumes]]", "mount_options", "owner"):
        assert any(line.startswith("#") and key in line for line in text.splitlines()), key
    assert "volumes" not in starter_raw()["nixos-disko"]


# --- review round 1: reserved names and mount points, owners, secrets, the luks guard -------


@pytest.mark.parametrize("name", ["snapshot", "snapshots", "snapshot-a", "pvmove", "pvmove1"])
def test_lv_names_starting_with_lvm_reserved_prefixes_are_rejected(name):
    findings = luks_findings(lambda r: vol(r, 0).update(name=name))
    assert any(f.where == "nixos-disko.volumes[0].name" and "starting with snapshot or pvmove" in f.message for f in findings), findings


@pytest.mark.parametrize("name", ["snap", "data-snapshot", "pv", "move"])
def test_lv_names_merely_containing_the_prefixes_are_fine(name):
    assert luks_findings(lambda r: vol(r, 0).update(name=name)) == []


@pytest.mark.parametrize(
    "mount", ["/", "/boot", "/boot/efi", "/nix", "/nix/store", "/proc", "/proc/x", "/sys", "/sys/fs", "/dev", "/dev/shm", "/run", "/run/user"]
)
def test_reserved_mount_trees_are_rejected(mount):
    findings = luks_findings(lambda r: (vol(r, 0).pop("owner"), vol(r, 0).update(mountpoint=mount)))
    assert any(f.where == "nixos-disko.volumes[0].mountpoint" and "is reserved" in f.message for f in findings), findings


@pytest.mark.parametrize("mount", ["/bootstrap", "/nixos-data", "/devel", "/running", "/var/lib/data"])
def test_mount_points_merely_sharing_a_prefix_are_fine(mount):
    assert luks_findings(lambda r: (vol(r, 0).pop("owner"), vol(r, 0).update(mountpoint=mount))) == []


@pytest.mark.parametrize("mount", ["/home", "/tmp", "/srv", "/opt", "/mnt", "/media", "/var", "/var/lib/x", "/etc", "/etc/x", "/usr", "/usr/local"])
def test_owner_on_a_shared_system_directory_is_rejected(mount):
    def edit(r):
        vol(r, 1).update(mountpoint="/data")  # free /home for the parametrized volume
        vol(r, 0).update(mountpoint=mount)

    findings = luks_findings(edit)
    assert any(f.where == "nixos-disko.volumes[0].owner" and "shared system directory" in f.message for f in findings), findings


@pytest.mark.parametrize("mount", ["/srv/data", "/home/ops-data", "/data", "/opt/app"])
def test_owner_below_a_shared_directory_is_fine(mount):
    assert luks_findings(lambda r: vol(r, 0).update(mountpoint=mount)) == []


def test_root_size_minimum():
    findings = luks_findings(lambda r: r["nixos-disko"].update(root_size="4G"))
    assert "root_size 4G is too small for a NixOS root; use at least 8G or 100%FREE" in messages(findings)
    assert luks_findings(lambda r: r["nixos-disko"].update(root_size="8G")) == []
    assert luks_findings(lambda r: r["nixos-disko"].update(root_size="8192M")) == []


def test_luks2_is_pinned_and_discards_documented():
    disko = nixos.render(luks_host(), Context("demo"))["disko.nix"]
    assert 'extraFormatArgs = [ "--type" "luks2" ];' in disko
    assert "settings.allowDiscards = true; # TRIM on SSDs; reveals which blocks are unused" in disko


def test_esp_comment_carries_no_spec_value():
    conf = nixos.render(luks_host(lambda r: r["nixos-disko"].update(esp_size="1G")), Context("demo"))["configuration.nix"]
    assert "configurationLimit = 10; # keeps the ESP from filling up" in conf and "1G" not in conf
    assert "# keeps the 512M ESP from filling up" in nixos.render(host(), Context("demo"))["configuration.nix"]


def test_secret_names_inside_strings_and_comments_are_not_secrets(tmp_path):
    spec = luks_host(lambda r: r["users"][0].update(gecos="Ops; password = x { keyFile = y"))
    for name, text in nixos.render(spec, Context("demo")).items():
        (tmp_path / name).write_text(text)
    assert messages(nixos.validate(tmp_path)) == []
    assert nixos._secret('# password = "x";\n  description = "a; passwordFile = b";\n') is None
    assert nixos._secret('  passwordCheck == 1;\n') is None


@pytest.mark.parametrize(
    "text,name",
    [
        ("  additionalKeyFiles = [ ];\n", "additionalKeyFiles"),
        ('  settings = { keyFile = "/tmp/k"; };\n', "keyFile"),
        ('  settings = { allowDiscards = true; keyFile = "/tmp/k"; };\n', "keyFile"),
        ('  luks.content.passwordFile = "/tmp/k";\n', "passwordFile"),
        ('  users.users.x.initialPassword = "x";\n', "initialPassword"),
        ('{ password = "x"; }\n', "password"),
    ],
)
def test_secret_attributes_are_found(text, name):
    assert nixos._secret(text) == name


@pytest.mark.parametrize(
    "name,edit,needle",
    [
        ("disko.nix", lambda t: t.replace('    type = "lvm_vg";', '    type = "other";'), "the lvm_vg type is missing"),
        ("disko.nix", lambda t: t.replace('type = "EF00";', 'type = "EF02";'), "needs an EF00 ESP and no EF02"),
        ("disko.nix", lambda t: t.replace('extraFormatArgs = [ "--type" "luks2" ];', "additionalKeyFiles = [ ];"), "additionalKeyFiles ="),
        ("installer.nix", lambda t: t.replace("vgnames=$(vgs --noheadings -o vg_name) || refuse", "vgnames="), "the existing-VG check (vgs) is missing"),
        ("installer.nix", lambda t: t.replace('if [ "$vg" = pool ]; then', "if false; then"), "the VG pool comparison is missing"),
        ("installer.nix", lambda t: t.replace('if [ -e /dev/mapper/crypted ] || [ "$crypt_rc" = 0 ]; then', "if false; then"), "the open-crypted check is missing"),
        ("installer.nix", lambda t: t.replace('elif [ "$crypt_rc" != 4 ]; then', "elif false; then"), "the fail-closed crypted status check is missing"),
        ("installer.nix", lambda t: t.replace(" pkgs.lvm2 pkgs.cryptsetup", ""), "lvm2 and cryptsetup in runtimeInputs is missing"),
    ],
)
def test_broken_luks_files_are_caught_round_1(luks_rendered, name, edit, needle):
    path = luks_rendered / name
    text = path.read_text()
    assert edit(text) != text
    path.write_text(edit(text))
    msgs = messages(nixos.validate(luks_rendered))
    assert any(needle in m for m in msgs), msgs


def test_luks_guard_comes_before_the_confirmation():
    inst = nixos.render(luks_host(), Context("demo"))["installer.nix"]
    assert inst.index('done <<< "$vgnames"') < inst.index("read -r answer")
    assert inst.index("cryptsetup status crypted") < inst.index("read -r answer")
    assert "runtimeInputs = [ pkgs.coreutils pkgs.util-linux disko pkgs.lvm2 pkgs.cryptsetup ];" in inst
    plain = nixos.render(host(), Context("demo"))["installer.nix"]
    assert "vgs" not in plain and "runtimeInputs = [ pkgs.coreutils pkgs.util-linux disko ];" in plain
    lines = nixos.plan(luks_host(), Context("demo"))
    assert any("or /dev/mapper/crypted is open" in line for line in lines)


def test_luks_script_proceeds_when_clean(script_env):
    run_script, disk, _ = script_env
    proc, args = run_script(layout="ext4-luks", STUB_VGS="  other\\n  data\\n")
    assert proc.returncode == 0, proc.stderr
    assert args == ["--write-efi-boot-entries", "--flake", "/nix/store/fake-install-flake#demo", "--disk", "main", disk]
    assert "disko will now ask for the new disk passphrase twice" in proc.stdout


def test_luks_script_refuses_an_existing_pool(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(layout="ext4-luks", STUB_VGS="  other\\n  pool\\n")
    assert proc.returncode == 1 and "volume group named pool already exists" in proc.stderr and args is None
    assert "Type " not in proc.stdout  # refused before the confirmation prompt


def test_luks_script_refuses_when_vgs_fails(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(layout="ext4-luks", STUB_VGS_FAIL="1")
    assert proc.returncode == 1 and "cannot list the LVM volume groups (vgs); refusing" in proc.stderr and args is None


def test_luks_script_refuses_an_active_crypted_mapping(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(layout="ext4-luks", STUB_CRYPT_OPEN="1")
    assert proc.returncode == 1 and "/dev/mapper/crypted is already open" in proc.stderr and args is None


def test_luks_script_refuses_an_existing_mapper_node(script_env, tmp_path):
    run_script, _, _ = script_env
    mapper = tmp_path / "mapper-crypted"
    mapper.write_text("")
    proc, args = run_script(layout="ext4-luks", STUB_MAPPER=str(mapper))
    assert proc.returncode == 1 and "/dev/mapper/crypted is already open" in proc.stderr and args is None


# --- review round 2: partition labels, cryptsetup exit codes, homes, plan wording ----------


def test_script_refuses_a_disko_partlabel_on_another_disk(script_env, tmp_path):
    run_script, _, _ = script_env
    other = tmp_path / "other-disk"
    proc, args = run_script(STUB_PARTS=f"{other}p1 disk-main-ESP\\n{other}p2 \\n")
    assert proc.returncode == 1 and args is None
    assert f"{other}p1 on disk {other} carries the partition label disk-main-ESP" in proc.stderr
    assert "wipefs -a or sgdisk --zap-all on " + str(other) in proc.stderr and "Type " not in proc.stdout


def test_script_accepts_disko_partlabels_on_the_target_disk(script_env, tmp_path):
    run_script, disk, _ = script_env
    other = tmp_path / "other-disk"
    proc, args = run_script(STUB_PARTS=f"{disk}p1 disk-main-ESP\\n{disk}p2 disk-main-root\\n{other}p1 data\\n{other} \\n")
    assert proc.returncode == 0, proc.stderr
    assert args[-1] == disk


def test_luks_script_refuses_a_foreign_disk_main_luks_label(script_env, tmp_path):
    run_script, _, _ = script_env
    proc, args = run_script(layout="ext4-luks", STUB_PARTS=f"{tmp_path / 'old'}p2 disk-main-luks\\n")
    assert proc.returncode == 1 and "carries the partition label disk-main-luks" in proc.stderr and args is None


def test_script_refuses_when_the_label_scan_fails(script_env):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_FAIL="NAME,PARTLABEL")
    assert proc.returncode == 1 and "cannot list the partition labels (lsblk); refusing" in proc.stderr and args is None


@pytest.mark.parametrize("rc", ["1", "2", "5"])
def test_luks_script_refuses_when_cryptsetup_cannot_answer(script_env, rc):
    run_script, _, _ = script_env
    proc, args = run_script(layout="ext4-luks", STUB_CRYPT_RC=rc)
    assert proc.returncode == 1 and f"cannot query the LUKS mapping crypted (cryptsetup status exited {rc}); load device-mapper: modprobe dm_mod, then retry" in proc.stderr and args is None


def test_partlabel_guard_is_rendered_for_every_layout():
    for spec in (host(), host(lambda r: r.update(firmware="bios")), luks_host()):
        inst = nixos.render(spec, Context("demo"))["installer.nix"]
        scan = inst.index("parts=$(lsblk -nrpo NAME,PARTLABEL) || refuse")
        assert scan < inst.index("read -r answer") < inst.index("exec disko-install")


@pytest.mark.parametrize(
    "spec_edit,edit,needle",
    [
        (None, lambda t: t.replace("parts=$(lsblk -nrpo NAME,PARTLABEL) || refuse", "parts="), "the partition-label scan is missing"),
        (None, lambda t: t.replace("          disk-main-*)", "          nothing-*)"), "the disk-main-* label match is missing"),
        (None, lambda t: t.replace('if [ "$o" != "$dev" ]; then', "if false; then"), "the foreign-disk label refusal is missing"),
        (
            None,
            lambda t: t.replace('      parts=$(lsblk -nrpo NAME,PARTLABEL) || refuse "cannot list the partition labels (lsblk)"\n', "").replace(
                '--disk main "$disk"\n', '--disk main "$disk"\n      parts=$(lsblk -nrpo NAME,PARTLABEL) || refuse "x"\n'
            ),
            "the partition-label scan comes after exec disko-install",
        ),
        (
            _luks_raw,
            lambda t: t.replace('      elif [ "$crypt_rc" != 4 ]; then\n', "").replace('--disk main "$disk"\n', '--disk main "$disk"\n      elif [ "$crypt_rc" != 4 ]; then\n'),
            "layout ext4-luks: the fail-closed crypted status check comes after exec disko-install",
        ),
    ],
)
def test_validate_requires_the_guards_before_disko_install(tmp_path, spec_edit, edit, needle):
    for name, text in nixos.render(host(spec_edit), Context("demo")).items():
        (tmp_path / name).write_text(text)
    path = tmp_path / "installer.nix"
    text = path.read_text()
    assert edit(text) != text
    path.write_text(edit(text))
    msgs = messages(nixos.validate(tmp_path))
    assert any(needle in m for m in msgs), msgs


@pytest.mark.parametrize("mount", ["/var/run", "/var/run/x", "/var/lock", "/var/lock/subsys"])
def test_var_run_and_var_lock_are_reserved(mount):
    findings = luks_findings(lambda r: (vol(r, 0).pop("owner"), vol(r, 0).update(mountpoint=mount)))
    assert any(f.where == "nixos-disko.volumes[0].mountpoint" and "is reserved" in f.message for f in findings), findings


@pytest.mark.parametrize("mount", ["/root", "/root/x", "/bin", "/lib", "/lib64", "/lib64/x", "/sbin"])
def test_owner_on_system_binary_and_root_dirs_is_rejected(mount):
    findings = luks_findings(lambda r: vol(r, 0).update(mountpoint=mount))
    assert any(f.where == "nixos-disko.volumes[0].owner" and "shared system directory" in f.message for f in findings), findings


def test_owner_on_a_users_home_is_rejected():
    def edit(r):
        vol(r, 1).update(mountpoint="/data")
        vol(r, 0).update(mountpoint="/home/ops")

    findings = luks_findings(edit)
    assert any(f.where == "nixos-disko.volumes[0].owner" and "that is the home of user 'ops'" in f.message and "0700" in f.message for f in findings), findings
    assert luks_findings(lambda r: (vol(r, 1).update(mountpoint="/data"), vol(r, 0).update(mountpoint="/home/someone"))) == []


def test_plan_findmnt_one_mount_point_per_line():
    lines = nixos.plan(host(), Context("demo"))
    assert "findmnt /" in lines and "findmnt /boot" in lines and not any(line.startswith("findmnt / ") for line in lines)
    assert "findmnt /boot" not in nixos.plan(host(lambda r: r.update(firmware="bios")), Context("demo"))


def test_plan_wording_for_retries_and_cleanup():
    joined = " ".join(line.lstrip("# ") for line in nixos.plan(luks_host(), Context("demo")))
    assert "disko exits after the disk was already wiped and partitioned: run sudo install-demo again" in joined
    assert "must fit the disk" in joined
    assert "vgchange -an pool   then   cryptsetup close crypted (in that order)" in joined
    assert "vgrename pool" in joined and "try again" not in joined
    plain = " ".join(line.lstrip("# ") for line in nixos.plan(host(), Context("demo")))
    assert "disk-main-*" in plain and "sgdisk --zap-all on THAT disk" in plain


def test_rendered_names_that_spell_secret_keywords_are_not_secrets(tmp_path):
    def edit(r):
        r["users"].append({"name": "password", "ssh_authorized_keys": []})
        r["users"].append({"name": "keyfile", "ssh_authorized_keys": []})
        vol(r, 0).update(name="keyfile", owner="password")
        r["nixos-disko"]["volumes"].append({"name": "password", "mountpoint": "/srv/p", "size": "1G"})

    files = nixos.render(luks_host(edit), Context("demo"))
    assert "users.users.password = {" in files["configuration.nix"] and "      password = {" in files["disko.nix"]
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    assert messages(nixos.validate(tmp_path)) == []
    assert nixos._secret("  users.users.password = {\n") is None and nixos._secret("  keyFile = {\n") is None
    assert nixos._secret('  users.users.password.password = "x";\n') == "password"


# --- review round 3: positive label ownership, inner lsblk failure, derived wording --------


def test_script_refuses_a_label_on_a_loop_device_that_is_not_the_target(script_env, tmp_path):
    run_script, _, _ = script_env
    loop = tmp_path / "loop7"
    proc, args = run_script(STUB_PARTS=f"{loop}p1 disk-main-root\\n", STUB_PART_ANCESTORS=f"{loop}p1 part\\n{loop} loop\\n")
    assert proc.returncode == 1 and f"on disk {loop} carries the partition label disk-main-root" in proc.stderr and args is None


def test_script_refuses_a_label_without_a_disk_or_loop_ancestor(script_env):
    run_script, disk, _ = script_env
    proc, args = run_script(STUB_PARTS="/dev/mapper/odd disk-main-ESP\\n", STUB_PART_ANCESTORS="/dev/mapper/odd crypt\\n")
    assert proc.returncode == 1 and args is None
    assert f"/dev/mapper/odd carries the partition label disk-main-ESP but does not sit on {disk}" in proc.stderr


def test_script_accepts_a_label_whose_ancestor_is_the_target(script_env):
    run_script, disk, _ = script_env
    proc, args = run_script(STUB_PARTS=f"{disk}p1 disk-main-ESP\\n", STUB_PART_ANCESTORS=f"{disk}p1 part\\n{disk} disk\\n")
    assert proc.returncode == 0, proc.stderr
    assert args[-1] == disk


def test_script_refuses_when_the_label_disk_lookup_fails(script_env, tmp_path):
    run_script, _, _ = script_env
    proc, args = run_script(STUB_PARTS=f"{tmp_path / 'x'}p1 disk-main-ESP\\n", STUB_PART_FAIL="1")
    assert proc.returncode == 1 and f"cannot read the disk under {tmp_path / 'x'}p1; refusing" in proc.stderr and args is None


def test_on_target_requirement_is_validated(rendered):
    path = rendered / "installer.nix"
    path.write_text(path.read_text().replace('if [ "$on_target" != 1 ]; then', "if false; then"))
    assert any("the on-target label requirement is missing" in m for m in messages(nixos.validate(rendered)))


def test_reserved_and_shared_wording_follows_the_lists():
    vols = nixos.SCHEMA["properties"]["volumes"]["items"]["properties"]
    for tree in nixos.RESERVED_MOUNT_TREES:
        assert tree in vols["mountpoint"]["description"]
    for path in nixos.SHARED_DIRS + nixos.SHARED_TREES:
        assert path in vols["owner"]["description"]
    assert "/home/<name>" in vols["owner"]["description"]
    findings = luks_findings(lambda r: (vol(r, 0).pop("owner"), vol(r, 0).update(mountpoint="/var/lock")))
    assert any(f"is reserved: not {nixos._RESERVED_TEXT}" in f.message for f in findings)


# --- networking (networkd | networkmanager) and desktop (none | gnome) ----------------------


def _nm(raw):
    raw.pop("network")
    raw["nixos-disko"]["networking"] = "networkmanager"


def _gnome(raw):
    _nm(raw)
    raw["nixos-disko"]["desktop"] = "gnome"
    raw["users"][0]["password_hash"] = "$6$EXAMPLEsalt$EXAMPLEhash"  # GDM logs in with a password


def nm_host(edit=None):
    return host(lambda r: (_nm(r), edit and edit(r)))


def gnome_host(edit=None):
    return host(lambda r: (_gnome(r), edit and edit(r)))


def test_networkmanager_and_gnome_specs_are_valid_without_findings():
    assert spec_findings(_nm) == [] and spec_findings(_gnome) == []
    assert "network" not in gnome_host()


def test_networkd_without_network_is_rejected():
    findings = spec_findings(lambda r: r.pop("network"))
    assert [f.where for f in findings if f.level == ERROR] == ["network"]
    assert messages(findings) == [
        "networking 'networkd' needs [network] with at least one interface; "
        'on a machine that joins networks interactively set networking = "networkmanager"'
    ]


def test_networkmanager_with_network_is_rejected():
    full = spec_findings(lambda r: r["nixos-disko"].update(networking="networkmanager"))
    assert any(m.startswith("[network] is not used with networking 'networkmanager'") and "nmtui or nmcli" in m for m in messages(full))
    # a [network] holding only dns is not even a valid table: interfaces are required once it is present
    dns_only = spec_findings(lambda r: (r["nixos-disko"].update(networking="networkmanager"), r.update(network={"dns": {"nameservers": ["192.0.2.53"]}})))
    assert messages(dns_only) == ["network: missing required key 'interfaces'"]


def test_networkmanager_render_has_no_networkd():
    conf = nixos.render(nm_host(), Context("demo"))["configuration.nix"]
    assert "networking.networkmanager.enable = true;" in conf
    for absent in ("useNetworkd", "systemd.network", "networking.useDHCP", ".link", "nameservers", "matchConfig"):
        assert absent not in conf, absent
    assert "this file holds no network credentials" in conf and "nmtui or nmcli" in conf and "GNOME Settings" not in conf
    networkd = nixos.render(host(), Context("demo"))["configuration.nix"]
    assert "networking.useNetworkd = true;" in networkd and "networkmanager" not in networkd


def _groups(conf, user):
    m = re.search(rf"users\.users\.{user} = {{\n(?:.*\n)*?    extraGroups = (\[[^\]]*\]);", conf)
    return m.group(1) if m else None


def test_networkmanager_group_for_passwordless_sudo_users_only():
    def users(r):
        ssh = r["users"][0]["ssh_authorized_keys"]
        r["users"] = [
            {"name": "ops", "sudo": True, "ssh_authorized_keys": ssh},
            {"name": "alice", "groups": ["wheel"], "password_hash": "$6$EXAMPLEsalt$EXAMPLEhash"},
            {"name": "bob", "groups": ["video"]},
            {"name": "carol", "groups": ["networkmanager", "wheel"], "sudo": True},
            {"name": "dave", "groups": ["video"], "sudo": True},
            {"name": "erin", "groups": ["networkmanager"]},
        ]

    conf = nixos.render(nm_host(users), Context("demo"))["configuration.nix"]
    assert _groups(conf, "ops") == '[ "wheel" "networkmanager" ]'
    assert _groups(conf, "alice") == '[ "wheel" ]'  # password sudo: polkit asks her as an admin
    assert _groups(conf, "bob") == '[ "video" ]'
    assert _groups(conf, "carol") == '[ "networkmanager" "wheel" ]'  # no duplicate
    assert _groups(conf, "dave") == '[ "video" "wheel" "networkmanager" ]'
    assert _groups(conf, "erin") == '[ "networkmanager" ]'  # opted in explicitly
    assert "# Users with sudo = true are also in the networkmanager group" in conf
    nosudo = nixos.render(nm_host(lambda r: r["users"][0].update(sudo=False, groups=["wheel"])), Context("demo"))["configuration.nix"]
    assert "networkmanager group" not in nosudo and _groups(nosudo, "ops") == '[ "wheel" ]'
    networkd = nixos.render(host(users), Context("demo"))["configuration.nix"]
    assert _groups(networkd, "alice") == '[ "wheel" ]' and _groups(networkd, "ops") == '[ "wheel" ]'


def test_gnome_needs_networkmanager():
    findings = spec_findings(lambda r: r["nixos-disko"].update(desktop="gnome"))
    assert any(
        f.where == "nixos-disko.desktop" and f.message == 'desktop \'gnome\' needs networking = "networkmanager" (GNOME manages connections through NetworkManager)'
        for f in findings
    ), findings


@pytest.mark.parametrize("release,ok", [("nixos-24.11", False), ("nixos-25.05", False), ("nixos-25.11", True), ("nixos-26.05", True)])
def test_gnome_needs_release_25_11(release, ok):
    findings = spec_findings(lambda r: (_gnome(r), r["nixos-disko"].update(release=release, state_version="24.11")))
    errors = [f for f in findings if f.level == ERROR]
    if ok:
        assert errors == []
    else:
        assert [f.where for f in errors] == ["nixos-disko.release"] and release in errors[0].message and "nixos-25.11 or newer" in errors[0].message
    # without a desktop the old releases stay allowed
    assert messages(spec_findings(lambda r: (_nm(r), r["nixos-disko"].update(release=release, state_version="24.11")))) == []


def test_gnome_renders_current_option_names():
    conf = nixos.render(gnome_host(), Context("demo"))["configuration.nix"]
    assert "services.displayManager.gdm.enable = true;" in conf and "services.desktopManager.gnome.enable = true;" in conf
    assert "Wayland" in conf and "25.11" in conf
    for absent in ("services.xserver.enable", "services.xserver.desktopManager", "services.xserver.displayManager", "pipewire", "pulseaudio", "sound."):
        assert absent not in conf, absent
    assert "nmtui, nmcli or GNOME Settings" in conf
    assert "services.desktopManager" not in nixos.render(nm_host(), Context("demo"))["configuration.nix"]


def test_explicit_networking_and_desktop_defaults_equal_omitted():
    explicit = host(lambda r: r["nixos-disko"].update(networking="networkd", desktop="none"))
    assert explicit == host() and nixos.render(explicit, Context("demo")) == nixos.render(host(), Context("demo"))
    assert host()["nixos-disko"]["networking"] == "networkd" and host()["nixos-disko"]["desktop"] == "none"


def test_keyboard_comment_follows_the_desktop():
    plain = nixos.render(nm_host(), Context("demo"))["configuration.nix"]
    gnome = nixos.render(gnome_host(), Context("demo"))["configuration.nix"]
    assert "# keyboard is an XKB layout; the console keymap is derived from it (X stays disabled)." in plain
    assert "the console keymap is derived from it and the desktop uses the same setting." in gnome
    assert "X stays disabled" not in gnome


@pytest.mark.parametrize("key,value", [("networking", "wpa_supplicant"), ("networking", "NetworkManager"), ("desktop", "kde"), ("desktop", "")])
def test_bad_networking_and_desktop_values(key, value):
    findings = spec_findings(lambda r: r["nixos-disko"].update({key: value}))
    assert any(f"nixos-disko.{key}: {value!r} is not one of" in m for m in messages(findings)), findings


def test_no_network_credentials_in_any_file():
    files = nixos.render(gnome_host(), Context("demo"))
    for name, text in files.items():
        assert nixos._net_secret(text) is None, name
        for needle in ("psk", "wireless.networks", "ensureProfiles"):
            assert needle not in text, (name, needle)


@pytest.fixture
def gnome_rendered(tmp_path):
    for name, text in nixos.render(gnome_host(), Context("demo")).items():
        (tmp_path / name).write_text(text)
    return tmp_path


def test_clean_gnome_render_validates(gnome_rendered):
    assert messages(nixos.validate(gnome_rendered)) == []


NM_LINE = "  networking.networkmanager.enable = true;\n"
GDM_LINE = "  services.displayManager.gdm.enable = true;\n"


@pytest.mark.parametrize(
    "edit,needle",
    [
        (lambda t: t.replace(NM_LINE, NM_LINE + "  networking.useNetworkd = true;\n"), "both systemd-networkd and NetworkManager"),
        (lambda t: t.replace(NM_LINE, NM_LINE + '  systemd.network.networks."10-x".matchConfig.Name = "x";\n'), "both systemd-networkd and NetworkManager"),
        (lambda t: t.replace(NM_LINE, ""), "neither systemd-networkd nor NetworkManager"),
        (lambda t: t.replace(NM_LINE, "  networking.useNetworkd = true;\n"), "GNOME is enabled without NetworkManager"),
        (lambda t: t.replace(GDM_LINE, ""), "GNOME is enabled without services.displayManager.gdm.enable"),
        (lambda t: t.replace(GDM_LINE, "  services.xserver.displayManager.gdm.enable = true;\n"), "services.xserver.displayManager.gdm is the pre-25.11 name; use services.displayManager.gdm"),
        (lambda t: t.replace(NM_LINE, NM_LINE + "  services.xserver.desktopManager.gnome.enable = true;\n"), "services.xserver.desktopManager.gnome is the pre-25.11 name; use services.desktopManager.gnome"),
        (lambda t: t.replace(NM_LINE, NM_LINE + "  networking.wireless = {\n    enable = true;\n    networks = { };\n  };\n"), "wireless.networks: no network credentials"),
        (lambda t: t.replace(NM_LINE, "  networking.networkmanager = {\n    enable = true;\n    ensureProfiles.profiles = { };\n  };\n" + NM_LINE), "ensureProfiles: no network credentials"),
        (lambda t: t.replace(NM_LINE, NM_LINE + "  networking = { wireless = { secretsFile = /run/x; }; };\n"), "wireless.secretsFile: no network credentials"),
        (lambda t: t.replace(NM_LINE, NM_LINE + '  networking.wireless.networks."EXAMPLE" = { pskRaw = "x"; };\n'), "wireless.networks: no network credentials"),
        (lambda t: t.replace(NM_LINE, NM_LINE + '  networking.wireless.networks."EXAMPLE".psk = "x";\n'), "no network credentials may be rendered"),
        (lambda t: t.replace(NM_LINE, NM_LINE + "  networking.networkmanager.ensureProfiles.profiles = { };\n"), "ensureProfiles: no network credentials"),
        (lambda t: t.replace(NM_LINE, NM_LINE + "  networking.wireless.secretsFile = /run/x;\n"), "wireless.secretsFile: no network credentials"),
    ],
)
def test_broken_desktop_files_are_caught(gnome_rendered, edit, needle):
    path = gnome_rendered / "configuration.nix"
    text = path.read_text()
    assert edit(text) != text
    path.write_text(edit(text))
    msgs = messages(nixos.validate(gnome_rendered))
    assert any(needle in m for m in msgs), msgs


def test_network_words_in_strings_and_comments_are_not_credentials(tmp_path):
    spec = gnome_host(lambda r: r["users"][0].update(gecos="Ops psk wireless.networks ensureProfiles"))
    for name, text in nixos.render(spec, Context("demo")).items():
        (tmp_path / name).write_text(text)
    assert messages(nixos.validate(tmp_path)) == []
    assert nixos._net_secret("  # psk = x; ensureProfiles\n") is None
    assert nixos._net_secret("  x = a.psk == b;\n  y = [ pkgs.ensureProfiles ];\n") is None


def test_users_volumes_and_packages_named_like_credentials_validate(tmp_path):
    def edit(r):
        _luks_raw(r)
        _gnome(r)
        r["users"][0]["name"] = "psk"
        r["users"].append({"name": "ensureprofiles", "password_hash": "$6$EXAMPLEsalt$EXAMPLEhash"})
        r["nixos-disko"]["volumes"][0].update(name="psk", owner="psk")
        r["nixos-disko"]["volumes"].append({"name": "pskraw", "mountpoint": "/srv/secretsfile", "size": "1G"})
        r["nixos-disko"]["packages"] = ["psk", "ensureProfiles", "wireless", "secretsFile"]

    spec = host(edit)
    files = nixos.render(spec, Context("demo"))
    assert "users.users.psk = {" in files["configuration.nix"] and "      psk = {" in files["disko.nix"]
    for name, text in files.items():
        (tmp_path / name).write_text(text)
        assert nixos._net_secret(text) is None, name
    assert messages(nixos.validate(tmp_path)) == []


@pytest.mark.parametrize(
    "text,hit",
    [
        ('  networking.wireless.networks."x".psk = "y";\n', "wireless.networks"),
        ("  networking.wireless = {\n    networks.x.psk = \"y\";\n  };\n", "wireless.networks"),
        ("  networking = {\n    wireless = {\n      networks = { };\n    };\n  };\n", "wireless.networks"),
        ("  networking.networkmanager = {\n    ensureProfiles = { };\n  };\n", "ensureProfiles"),
        ("  networking.networkmanager.ensureProfiles.environmentFiles = [ ];\n", "ensureProfiles"),
        ("  wireless = { secretsFile = /run/x; };\n", "wireless.secretsFile"),
        ("  { config, ... }: { networking.wireless.networks = { }; }\n", "wireless.networks"),
    ],
)
def test_nested_network_credentials_are_found(text, hit):
    assert nixos._net_secret(text) == hit


def test_initrd_networkd_is_not_the_system_network_stack(gnome_rendered):
    path = gnome_rendered / "configuration.nix"
    path.write_text(path.read_text().replace(NM_LINE, NM_LINE + '  boot.initrd.systemd.network.enable = true;\n'))
    assert messages(nixos.validate(gnome_rendered)) == []


def test_other_xserver_display_manager_options_are_not_old_names(gnome_rendered):
    path = gnome_rendered / "configuration.nix"
    path.write_text(path.read_text().replace(NM_LINE, NM_LINE + '  services.xserver.displayManager.sessionCommands = "";\n'))
    assert messages(nixos.validate(gnome_rendered)) == []


def test_gnome_without_a_password_hash_warns():
    findings = spec_findings(lambda r: (_gnome(r), r["users"][0].pop("password_hash")))
    assert messages(findings) == []
    assert [f.where for f in findings if f.level == WARNING] == ["users"]
    assert "nobody can log in at the GDM login screen" in messages(findings, WARNING)[0]
    assert spec_findings(_gnome) == [] and spec_findings(_nm) == []


def test_networkmanager_plan_lines():
    lines = nixos.plan(nm_host(), Context("demo"))
    post = lines[lines.index("sudo install-demo"):]
    assert any("sudo nmtui" in line and "TARGET console" in line for line in post) and "nmcli device status" in post
    assert "target console" in post[post.index("nmcli device status") - 1]
    assert any("never in spec.toml" in line for line in post)
    # no static address without [network]: the ssh hint asks for the address
    assert post.index("nmcli device status") < post.index("HOST_IP=CHANGE-ME")
    assert post[post.index("HOST_IP=CHANGE-ME") + 1] == 'ssh ops@"$HOST_IP"'
    assert "systemctl is-active display-manager" not in lines
    assert not any("GNOME Settings" in line or "desktop closure" in line for line in lines)
    plain = nixos.plan(host(), Context("demo"))
    assert "nmcli device status" not in plain and not any("nmtui" in line for line in plain)


def test_gnome_plan_lines():
    lines = nixos.plan(gnome_host(), Context("demo"))
    iso = next(i for i, line in enumerate(lines) if line.startswith("nix ") and " build " in line)
    size = [i for i, line in enumerate(lines) if "whole desktop closure" in line]
    assert size and size[0] < iso and any("16 GB" in line for line in lines)
    assert any("sudo nmtui   (Ctrl+Alt+F3 for a text console, or use GNOME Settings);" in line for line in lines)
    assert lines[-1] == "systemctl is-active display-manager"


@pytest.mark.parametrize("firmware", ["uefi", "bios"])
def test_secure_boot_and_wsl2_lines(firmware):
    lines = nixos.plan(host(lambda r: r.update(firmware=firmware)), Context("demo"))
    secure = [line for line in lines if "Secure Boot" in line]
    if firmware == "uefi":
        assert secure == ["#    Disable Secure Boot in the firmware setup first: neither the installer stick nor systemd-boot is signed."]
        assert lines.index(secure[0]) < lines.index("sudo install-demo")
    else:
        assert secure == []
    case = next(i for i, line in enumerate(lines) if line.startswith('case "$USB"'))
    wsl = [i for i, line in enumerate(lines) if "WSL2" in line]
    assert wsl and wsl[0] > case and '#        cp -L "$ISO" /mnt/c/Users/CHANGE-ME/Downloads/' in lines
    assert any("Rufus (DD image mode) or balenaEtcher" in line for line in lines)


def test_gnome_plan_executes_nothing(tmp_path, capsys, monkeypatch):
    from conftest import EXAMPLES

    for name in ("run", "Popen", "call", "check_call", "check_output", "getoutput", "getstatusoutput"):
        monkeypatch.setattr(subprocess, name, _forbidden)
    for name in ("system", "popen", "execv", "execve", "execvp", "execvpe", "spawnv", "spawnve", "posix_spawn", "fork"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, _forbidden)
    code, out, _ = run(capsys, "--root", str(EXAMPLES), "plan", "os-install/demo-nixos-gnome-luks")
    assert code == 0 and "nmcli device status" in out and "systemctl is-active display-manager" in out


def test_hostile_values_without_network_are_quoted(tmp_path):
    spec = gnome_host()
    spec["hostname"] = "demo;touch${IFS}/tmp/pwned"
    spec["users"][0]["name"] = "ops$(id)"
    lines = plan_lines(nixos, spec, Context("demo"), "os-install/demo", tmp_path)
    for line in lines:
        if line and not line.startswith("#"):
            # punctuation_chars splits an unquoted ; ( ) off, so a leaked value yields touch.../id tokens
            tokens = list(shlex.shlex(line, posix=True, punctuation_chars=True))
            assert not any(t.startswith("touch") or t == "id" for t in tokens), line
    joined = "\n".join(lines)
    assert shlex.quote("demo;touch${IFS}/tmp/pwned") in joined and shlex.quote("install-demo;touch${IFS}/tmp/pwned") in joined
    assert "ssh 'ops$(id)'@\"$HOST_IP\"" in lines
    files = nixos.render(gnome_host(lambda r: r["users"][0].update(gecos='x"${builtins.abort 1}"')), Context("demo"))
    assert 'description = "x\\"\\${builtins.abort 1}\\"";' in files["configuration.nix"]


def test_gnome_example_cli_round_trip(capsys):
    from conftest import EXAMPLES

    root = str(EXAMPLES)
    code, out, err = run(capsys, "--root", root, "validate", "os-install/demo-nixos-gnome-luks")
    assert code == 0 and "ok: os-install/demo-nixos-gnome-luks" in out, err
    assert run(capsys, "--root", root, "diff", "os-install/demo-nixos-gnome-luks")[0] == 0
    raw = tomllib.loads((EXAMPLES / "os-install/demo-nixos-gnome-luks/spec.toml").read_text())
    assert "network" not in raw and raw["nixos-disko"]["networking"] == "networkmanager" and raw["nixos-disko"]["desktop"] == "gnome"


def test_starter_hints_for_networking_and_desktop_are_comments():
    text = nixos.starter("demo")
    for key in ('networking = "networkd"', '"networkmanager"', 'desktop = "none"', '"gnome"'):
        assert any(line.startswith("#") and key in line for line in text.splitlines()), key
    assert "networking" not in starter_raw()["nixos-disko"] and "desktop" not in starter_raw()["nixos-disko"]
