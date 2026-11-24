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
  imports = [ (modulesPath + "/installer/scan/not-detected.nix") ] ++ hardware ++ [ ./prov-state.nix ];

  # Fallback for hardware without a hardware-configuration.nix: broad initrd storage/USB drivers.
  boot.initrd.availableKernelModules = [ "nvme" "ahci" "xhci_pci" "usb_storage" "sd_mod" "usbhid" ];
  hardware.cpu.intel.updateMicrocode = true;

  # UEFI: systemd-boot on the ESP (disko.nix mounts it at /boot).
  boot.loader.systemd-boot.enable = true;
  boot.loader.systemd-boot.configurationLimit = 10; # keeps the 512M ESP from filling up
  boot.loader.efi.canTouchEfiVariables = true;

  networking.hostName = "demo-nixos-state";
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
  systemd.network.links."10-lan0" = {
    matchConfig.PermanentMACAddress = "02:00:00:00:00:01";
    linkConfig.Name = "lan0";
  };
  systemd.network.networks."10-lan0" = {
    matchConfig.Name = "lan0";
    networkConfig.DHCP = "no";
    address = [ "192.0.2.20/24" "2001:db8::20/64" ];
    gateway = [ "192.0.2.1" "2001:db8::1" ];
    linkConfig.MTUBytes = "1500";
  };
  systemd.network.networks."10-lan1" = {
    matchConfig.Name = "lan1";
    networkConfig.DHCP = "ipv4";
  };

  users.users.ops = {
    isNormalUser = true;
    description = "Operator";
    extraGroups = [ "wheel" ];
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE ops@example.com"
    ];
  };
  users.users.alice = {
    isNormalUser = true;
    description = "Alice Example";
    extraGroups = [ "video" ];
    shell = pkgs.zsh;
    hashedPassword = "$6$EXAMPLEsalt$EXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhashEXAMPLEhash0123456789";
    openssh.authorizedKeys.keys = [
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE alice@example.com"
    ];
  };
  programs.zsh.enable = true;
  # Passwordless sudo for the users with sudo = true (they are in wheel too).
  security.sudo.extraRules = [
    {
      users = [ "ops" ];
      commands = [ { command = "ALL"; options = [ "NOPASSWD" ]; } ];
    }
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
    pkgs.htop
  ];

  # The release first installed; never change it after the install.
  system.stateVersion = "26.05";
}
