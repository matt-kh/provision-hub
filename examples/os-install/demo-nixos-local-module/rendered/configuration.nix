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
  # local_module: the host-owned ../local.nix next to spec.toml (outside rendered/), for
  # what spec.toml cannot express. Required: git add it. The installer copies it in as ./local.installer-copy.nix.
  local =
    if builtins.pathExists ./local.installer-copy.nix then [ ./local.installer-copy.nix ]
    else assert lib.assertMsg (!storeRoot && builtins.pathExists ../local.nix)
      "local_module = true: create ../local.nix next to spec.toml and git add it";
    [ ../local.nix ];
in
{
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ] ++ hardware ++ local;

  # Fallback for hardware without a hardware-configuration.nix: broad initrd storage/USB drivers.
  boot.initrd.availableKernelModules = [ "nvme" "ahci" "xhci_pci" "usb_storage" "sd_mod" "usbhid" ];

  # UEFI: systemd-boot on the ESP (disko.nix mounts it at /boot).
  boot.loader.systemd-boot.enable = true;
  boot.loader.systemd-boot.configurationLimit = 10; # keeps the 512M ESP from filling up
  boot.loader.efi.canTouchEfiVariables = true;

  networking.hostName = "demo-nixos-local-module";
  networking.domain = "example.com";
  time.timeZone = "Etc/UTC";
  i18n.defaultLocale = "en_US.UTF-8";
  # keyboard is an XKB layout; the console keymap is derived from it (X stays disabled).
  services.xserver.xkb.layout = "us";
  console.useXkbConfig = true;

  # Network: systemd-networkd, one unit per interface; NICs with a MAC are renamed by .link files.
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.enable = true;
  # Boot waits for one configured interface, not all (an unplugged NIC would stall it).
  systemd.network.wait-online.anyInterface = true;
  networking.nameservers = [ "192.0.2.53" "2001:db8::53" ];
  networking.search = [ "example.com" ];
  systemd.network.networks."10-lan0" = {
    matchConfig.Name = "lan0";
    networkConfig.DHCP = "ipv4";
  };

  users.users.alice = {
    isNormalUser = true;
    description = "Alice Example";
    extraGroups = [ "wheel" ];
    shell = pkgs.zsh;
    hashedPassword = "$6$EXAMPLEsalt$EXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhash0123456789";
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE alice@example.com"
    ];
  };
  programs.zsh.enable = true;

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
    pkgs.htop
    pkgs.tmux
  ];

  # The release first installed; never change it after the install.
  system.stateVersion = "26.05";
}
