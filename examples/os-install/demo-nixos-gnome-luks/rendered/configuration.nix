# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render.
# The target system. Static validation only: prov has not installed it on hardware.
{ config, lib, pkgs, modulesPath, ... }:
let
  # A human-owned hardware-configuration.nix next to spec.toml (outside rendered/) is
  # imported when it exists and is git-tracked; render never writes or deletes it.
  # On the USB stick disko-install evaluates a copy of this directory alone in the store,
  # where ../ is the store directory itself: the installer copies the file in as
  # ./hardware-configuration.installer-copy.nix, and ../ is not looked at there.
  storeRoot = dirOf (toString ./.) == builtins.storeDir;
  hardware =
    if builtins.pathExists ./hardware-configuration.installer-copy.nix then [ ./hardware-configuration.installer-copy.nix ]
    else if !storeRoot && builtins.pathExists ../hardware-configuration.nix then [ ../hardware-configuration.nix ]
    else [ ];
in
{
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ] ++ hardware;

  # Fallback for hardware without a hardware-configuration.nix: broad initrd storage/USB drivers.
  boot.initrd.availableKernelModules = [ "nvme" "ahci" "xhci_pci" "usb_storage" "sd_mod" "usbhid" ];
  # ext4-luks: disko.nix declares boot.initrd.luks.devices.crypted (from its luks partition)
  # and the LVM volume group; nixpkgs' default initrd modules carry the USB/HID keyboard
  # drivers. The console keymap is loaded early, before the passphrase prompt.
  console.earlySetup = true;
  hardware.cpu.amd.updateMicrocode = true;

  # UEFI: systemd-boot on the ESP (disko.nix mounts it at /boot).
  boot.loader.systemd-boot.enable = true;
  boot.loader.systemd-boot.configurationLimit = 10; # keeps the ESP from filling up
  boot.loader.efi.canTouchEfiVariables = true;

  networking.hostName = "demo-nixos-gnome-luks";
  networking.domain = "example.com";
  time.timeZone = "Etc/UTC";
  i18n.defaultLocale = "en_US.UTF-8";
  # keyboard is an XKB layout; the console keymap is derived from it and the desktop uses the same setting.
  services.xserver.xkb.layout = "us";
  console.useXkbConfig = true;

  # Network: NetworkManager. Connections, Wi-Fi included, are made on the machine with
  # nmtui, nmcli or GNOME Settings after the first boot; this file holds no network credentials.
  networking.networkmanager.enable = true;

  # Desktop: GNOME on Wayland with the GDM login screen (option names of NixOS 25.11 and later).
  services.displayManager.gdm.enable = true;
  services.desktopManager.gnome.enable = true;

  users.users.alice = {
    isNormalUser = true;
    description = "Alice Example";
    extraGroups = [ "wheel" ];
    hashedPassword = "$6$EXAMPLEsalt$EXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhash0123456789";
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE alice@example.com"
    ];
  };
  # Owned volumes: the mount point belongs to its owner (group users, mode 0750).
  systemd.tmpfiles.rules = [
    "d /srv/data 0750 alice users -"
  ];

  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  nix.settings.experimental-features = [ "nix-command" "flakes" ];
  environment.systemPackages = [
    pkgs.git
    pkgs.vim
  ];

  # The release first installed; never change it after the install.
  system.stateVersion = "26.05";
}
