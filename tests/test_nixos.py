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
    assert "medium=$(findmnt -no SOURCE /iso || true)" in inst
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
        'if [ -b "$USB" ]; then sudo dd if="$ISO" of="$USB" bs=4M conv=fsync status=progress; else echo "USB=$USB is not a block device; set USB first" >&2; fi',
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
    "id": 'echo "${STUB_UID:-0}"\n',
    "readlink": 'p="${@: -1}"; if [ "$p" = "${STUB_LINK_FROM:-}" ]; then echo "$STUB_LINK_TO"; else echo "$p"; fi\n',
    "findmnt": 'printf "%s\\n" "$STUB_MEDIUM"\n',
    "lsblk": r'''case "$*" in *"${STUB_FAIL:-@@}"*) echo "lsblk: failed" >&2; exit 32 ;; esac
case "$*" in
  "-dno TYPE "*) echo "$STUB_TYPE" ;;
  "-nro MOUNTPOINTS "*) printf "%b" "$STUB_MOUNTS" ;;
  "-nrpso NAME,TYPE "*) printf "%b" "$STUB_ANCESTORS" ;;
  "-dno SERIAL "*) echo "$STUB_SERIAL" ;;
  *) echo "lsblk $*" ;;
esac
''',
    "disko-install": 'printf "%s\\n" "$@" > "$STUB_LOG"\n',
}


def _script(firmware, disk, tmp_path):
    """The install-<host> script as writeShellApplication would run it.

    Nix interpolations are substituted as Nix would; the three file-type tests that cannot
    be stubbed ([ -b ] on the disk and the medium, [ -t 0 ]) become [ -e ] / always-true.
    """
    inst = nixos.render(host(lambda r: r.update(firmware=firmware)), Context("demo"))["installer.nix"]
    body = inst.split("    text = ''\n", 1)[1].split("\n    '';\n", 1)[0]
    lines = [line[6:] if line.startswith("      ") else line for line in body.split("\n")]
    text = "\n".join(lines)
    text = text.replace("${lib.escapeShellArg disk}", shlex.quote(disk))
    text = text.replace("${lib.escapeShellArg hostName}", "demo")
    text = text.replace('${lib.escapeShellArg "${installFlake}#${hostName}"}', "/nix/store/fake-install-flake#demo")
    assert "${" not in text, "an unsubstituted Nix interpolation is left"
    text = text.replace('[ ! -b "$disk" ]', '[ ! -e "$disk" ]').replace('[ -b "$medium" ]', '[ -e "$medium" ]')
    text = text.replace("[ ! -t 0 ]", '[ -n "${STUB_NOTTY:-}" ]')
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
    }

    def run_script(firmware="uefi", answer="9876\n", **overrides):
        script = _script(firmware, str(disk), tmp_path)
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
    proc, args = run_script(STUB_LINK_FROM=disk, STUB_LINK_TO="/dev/sdz", STUB_ANCESTORS=f"{medium} part\\n/dev/sdz disk\\n")
    assert proc.returncode == 1 and "(/dev/sdz) is the USB stick this installer booted from; refusing" in proc.stderr and args is None
