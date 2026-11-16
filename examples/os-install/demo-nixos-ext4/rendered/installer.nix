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

  # The flake disko-install evaluates on the stick. disko-install copies only the flake
  # directory, so ../hardware-configuration.nix would be lost: it is copied in as
  # ./hardware-configuration.nix, which configuration.nix checks first.
  installFlake = pkgs.runCommand "install-flake-${hostName}" { } (
    ''
      mkdir -p "$out"
      cp ${./flake.nix} "$out/flake.nix"
      cp ${./configuration.nix} "$out/configuration.nix"
      cp ${./disko.nix} "$out/disko.nix"
      cp ${./installer.nix} "$out/installer.nix"
    ''
    + lib.optionalString (builtins.pathExists ./flake.lock) ''
      cp ${./flake.lock} "$out/flake.lock"
    ''
    + lib.optionalString (builtins.pathExists ../hardware-configuration.nix) ''
      cp ${../hardware-configuration.nix} "$out/hardware-configuration.nix"
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
      if [ "$(id -u)" -ne 0 ]; then
        echo "install-$host: run it as root: sudo install-$host" >&2
        exit 1
      fi
      if [ ! -b "$disk" ]; then
        echo "install-$host: disk $disk not found; nothing was changed. Disks present:" >&2
        ls -l /dev/disk/by-id/ >&2 || true
        exit 1
      fi
      echo "WARNING: this ERASES ALL DATA on $disk ($(readlink -f "$disk")):"
      lsblk -o NAME,MODEL,SERIAL,SIZE,TYPE,FSTYPE,LABEL,MOUNTPOINTS "$disk"
      printf "Type the hostname (%s) to wipe this disk and install NixOS: " "$host"
      answer=""
      read -r answer || true
      if [ "$answer" != "$host" ]; then
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
