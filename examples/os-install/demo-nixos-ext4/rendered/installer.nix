# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render.
# USB installer: the NixOS minimal ISO plus an offline copy of the target system and an
# install-demo-nixos-ext4 command. Nothing runs at boot; a human starts the install.
#
# Offline closure: the pattern of disko's docs/disko-install.md ("Using disko-install in an
# offline installer") at tag v1.13.0 (commit de5708739256238fb912c62f03988815db89ec9a),
# plus toplevel.drvPath from its tests/disko-install/default.nix.
{ config, lib, pkgs, modulesPath, self, ... }:
let
  hostName = "demo-nixos-ext4";
  disk = "/dev/disk/by-id/nvme-EXAMPLE_SSD_SERIAL_0000000000";
  target = self.nixosConfigurations.demo-nixos-ext4;
  disko = self.inputs.disko.packages.${pkgs.stdenv.hostPlatform.system}.disko;
  # As in configuration.nix: no ../ when this file sits directly in the store.
  storeRoot = dirOf (toString ./.) == builtins.storeDir;

  # The flake disko-install evaluates on the stick. disko-install copies only the flake
  # directory (a ?dir= flake ref loses its dir there), so ../hardware-configuration.nix
  # is copied in as ./hardware-configuration.installer-copy.nix,
  # which configuration.nix checks first.
  # Without a git-tracked flake.lock, disko-install would try to lock the inputs offline,
  # after the wipe is confirmed: refuse to build the installer instead.
  installFlake =
    assert lib.assertMsg (builtins.pathExists ./flake.lock)
      "commit rendered/flake.lock (nix flake lock && git add flake.lock) before building the installer";
    pkgs.runCommand "install-flake-${hostName}" { } (
      ''
        mkdir -p "$out"
        cp ${./flake.nix} "$out/flake.nix"
        cp ${./configuration.nix} "$out/configuration.nix"
        cp ${./disko.nix} "$out/disko.nix"
        cp ${./installer.nix} "$out/installer.nix"
        cp ${./flake.lock} "$out/flake.lock"
      ''
      + lib.optionalString (!storeRoot && builtins.pathExists ../hardware-configuration.nix) ''
        cp ${../hardware-configuration.nix} "$out/hardware-configuration.installer-copy.nix"
      ''
    );

  # Every flake input (recursively), so evaluating the flake needs no network.
  flakeOutPaths =
    let
      collector = parent:
        map (child: [ child.outPath ] ++ (if child ? inputs && child.inputs != { } then collector child else [ ]))
          (lib.attrValues parent.inputs);
    in
    lib.unique (lib.flatten (collector self));

  dependencies = [
    target.config.system.build.toplevel
    target.config.system.build.toplevel.drvPath
    target.config.system.build.diskoScript
    target.config.system.build.diskoScript.drvPath
    target.pkgs.stdenv.drvPath
    target.pkgs.perlPackages.ConfigIniFiles
    target.pkgs.perlPackages.FileSlurp
    (target.pkgs.closureInfo { rootPaths = [ ]; }).drvPath
    installFlake
  ] ++ flakeOutPaths;

  closureInfo = pkgs.closureInfo { rootPaths = dependencies; };

  install = pkgs.writeShellApplication {
    name = "install-${hostName}";
    runtimeInputs = [ pkgs.coreutils pkgs.util-linux disko ];
    text = ''
      disk=${lib.escapeShellArg disk}
      host=${lib.escapeShellArg hostName}
      if [ ! -t 0 ]; then
        echo "install-$host: run it on a terminal; the confirmation must be typed" >&2
        exit 1
      fi
      if [ "$(id -u)" -ne 0 ]; then
        echo "install-$host: run it as root: sudo install-$host" >&2
        exit 1
      fi
      if [ ! -b "$disk" ]; then
        echo "install-$host: disk $disk not found; nothing was changed. Disks present:" >&2
        ls -l /dev/disk/by-id/ >&2 || true
        exit 1
      fi
      # Every probe is captured on its own line with an explicit refusal: a failing lsblk
      # (or a pipe cut short) must stop the install, never let it through.
      refuse() { echo "install-$host: $*; refusing, nothing was changed" >&2; exit 1; }
      dev=$(readlink -f "$disk") || refuse "cannot resolve $disk"
      type=$(lsblk -dno TYPE "$dev") || refuse "cannot read the type of $dev"
      if [ "$type" != "disk" ]; then
        refuse "$disk ($dev) is a $type, not a whole disk"
      fi
      # Anything in the disk's tree mounted or used as swap (partitions, dm, loop, md).
      mounts=$(lsblk -nro MOUNTPOINTS "$dev") || refuse "cannot read the mounts on $dev"
      mounts=$(printf %s "$mounts" | tr -d "[:space:]")
      if [ -n "$mounts" ]; then
        lsblk -o NAME,TYPE,MOUNTPOINTS "$dev" >&2 || true
        refuse "$disk ($dev) or a device on it is mounted or used as swap"
      fi
      # The installer medium: the NixOS ISO mounts it at /iso (iso-image.nix); every disk
      # it sits on (through partitions, dm or md) must differ from the target. No block
      # device at /iso (copytoram) leaves nothing to compare; a medium on the target disk
      # is mounted and was refused above anyway.
      medium=$(findmnt -no SOURCE /iso || true)
      if [ -n "$medium" ] && [ -b "$medium" ]; then
        ancestors=$(lsblk -nrpso NAME,TYPE "$medium") || refuse "cannot read the devices the boot medium $medium sits on"
        while read -r name kind; do
          if [ "$kind" = "disk" ] && [ "$(readlink -f "$name")" = "$dev" ]; then
            refuse "$disk ($dev) is the USB stick this installer booted from"
          fi
        done <<< "$ancestors"
      fi
      serial=$(lsblk -dno SERIAL "$dev") || refuse "cannot read the serial of $dev"
      serial=$(printf %s "$serial" | tr -d "[:space:]")
      if [ "$(printf %s "$serial" | wc -c)" -ge 4 ]; then
        expect=$(printf %s "$serial" | tail -c 4)
        what="the last 4 characters of the disk serial ($serial)"
      else
        expect="$host"
        what="the hostname ($host; the disk reports no serial)"
      fi
      echo "WARNING: this ERASES ALL DATA on $disk ($dev) to install NixOS host $host:"
      lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS "$dev"
      shown="$serial"
      if [ -z "$shown" ]; then shown="none"; fi
      printf "Host: %s  Disk serial: %s\nType %s to wipe this disk: " "$host" "$shown" "$what"
      answer=""
      read -r answer || true
      if [ "$answer" != "$expect" ]; then
        echo "install-$host: confirmation did not match; aborted, nothing was changed" >&2
        exit 1
      fi
      exec disko-install --write-efi-boot-entries --flake ${lib.escapeShellArg "${installFlake}#${hostName}"} --disk main "$disk"
    '';
  };
in
{
  imports = [ (modulesPath + "/installer/cd-dvd/installation-cd-minimal.nix") ];

  # Pulls the whole offline closure onto the ISO.
  environment.etc."install-closure".source = "${closureInfo}/store-paths";
  environment.systemPackages = [ install ];
  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  # The live ISO never imports a ZFS root; the 26.11 default, set early to silence the warning.
  boot.zfs.forceImportRoot = false;
}
