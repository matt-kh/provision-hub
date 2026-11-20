# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render.
# USB installer: the NixOS minimal ISO plus an offline copy of the target system and an
# install-demo-nixos-local-module command. Nothing runs at boot; a human starts the install.
#
# Offline closure: the pattern of disko's docs/disko-install.md ("Using disko-install in an
# offline installer") at tag v1.13.0 (commit de5708739256238fb912c62f03988815db89ec9a),
# Only outputs of the target system are copied, never its build closure: disko-install
# re-evaluates the same flake with the same disk, which yields the same derivations, so
# nothing of the system is rebuilt on the stick.
{ config, lib, pkgs, modulesPath, self, ... }:
let
  hostName = "demo-nixos-local-module";
  disk = "/dev/disk/by-id/nvme-EXAMPLE_SSD_SERIAL_0000000000";
  target = self.nixosConfigurations.demo-nixos-local-module;
  disko = self.inputs.disko.packages.${pkgs.stdenv.hostPlatform.system}.disko;
  # As in configuration.nix: no ../ when this file sits directly in the store.
  storeRoot = dirOf (toString ./.) == builtins.storeDir;

  # The flake disko-install evaluates on the stick. disko-install copies only the flake
  # directory (a ?dir= flake ref loses its dir there), so ../hardware-configuration.nix
  # is copied in as ./hardware-configuration.installer-copy.nix,
  # which configuration.nix checks first.
  # local_module: ../local.nix is copied in as ./local.installer-copy.nix the same way.
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
      + lib.optionalString (!storeRoot) (
        assert lib.assertMsg (builtins.pathExists ../local.nix)
          "local_module = true: create ../local.nix next to spec.toml and git add it";
        ''
          cp ${../local.nix} "$out/local.installer-copy.nix"
        ''
      )
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
      # Every probe is captured on its own line with an explicit refusal: a failing lsblk
      # (or a pipe cut short) must stop the install, never let it through.
      refuse() { echo "install-$host: $*; refusing, nothing was changed" >&2; exit 1; }
      if [ ! -t 0 ]; then
        echo "install-$host: run it on a terminal; the confirmation must be typed" >&2
        exit 1
      fi
      uid=$(id -u) || refuse "cannot read the user id"
      if [ "$uid" != "0" ]; then
        echo "install-$host: run it as root: sudo install-$host" >&2
        exit 1
      fi
      if [ ! -b "$disk" ]; then
        echo "install-$host: disk $disk not found; nothing was changed. Disks present:" >&2
        ls -l /dev/disk/by-id/ >&2 || true
        exit 1
      fi
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
      # Best effort: the installer medium is mounted at /iso (iso-image.nix); every disk it
      # sits on (through partitions, dm or md) must differ from the target. Without a block
      # device there (copytoram, netboot) nothing is compared; the mounted-disk check above
      # and the typed serial still apply.
      medium=""
      if mountpoint -q /iso; then
        medium=$(findmnt -no SOURCE /iso) || refuse "cannot read the source of /iso"
      fi
      if [ -n "$medium" ] && [ -b "$medium" ]; then
        ancestors=$(lsblk -nrpso NAME,TYPE "$medium") || refuse "cannot read the devices the boot medium $medium sits on"
        while read -r name kind; do
          if [ "$kind" = "disk" ]; then
            r=$(readlink -f "$name") || refuse "cannot resolve $name"
            if [ "$r" = "$dev" ]; then
              refuse "$disk ($dev) is the USB stick this installer booted from"
            fi
          fi
        done <<< "$ancestors"
      else
        echo "install-$host: no block device at /iso; the boot medium was not compared" >&2
      fi
      # Partition labels disko uses (disk-main-*) must not exist on any other disk.
      parts=$(lsblk -nrpo NAME,PARTLABEL) || refuse "cannot list the partition labels (lsblk)"
      while read -r pname plabel; do
        case "$plabel" in
          disk-main-*)
            owners=$(lsblk -nrpso NAME,TYPE "$pname") || refuse "cannot read the disk under $pname"
            on_target=0
            while read -r oname okind; do
              if [ "$okind" = "disk" ] || [ "$okind" = "loop" ]; then
                o=$(readlink -f "$oname") || refuse "cannot resolve $oname"
                if [ "$o" != "$dev" ]; then
                  refuse "$pname on disk $o carries the partition label $plabel that disko uses for this install; it could capture a mount at install or boot time. Unplug that disk, or wipe its partition table yourself (wipefs -a or sgdisk --zap-all on $o, NOT on the target disk)"
                fi
                on_target=1
              fi
            done <<< "$owners"
            if [ "$on_target" != 1 ]; then
              refuse "$pname carries the partition label $plabel but does not sit on $dev; disko could pick it up. Unplug or wipe the device it is on"
            fi
            ;;
        esac
      done <<< "$parts"
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
