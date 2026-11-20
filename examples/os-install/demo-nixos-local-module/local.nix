# Host-owned NixOS module (local_module = true): what spec.toml cannot express.
# Hand-written and git-tracked next to spec.toml; prov render never touches it.
# Keep it self-contained: the installer copies only this file onto the USB stick.
{ config, lib, pkgs, inputs, ... }:
{
  # Proprietary packages, allowed one by one.
  nixpkgs.config.allowUnfreePredicate = pkg: builtins.elem (lib.getName pkg) [ "vscode" ];

  environment.systemPackages = [
    pkgs.vscode
    # A package from an extra flake input (spec.toml [nixos-disko.inputs]).
    inputs.nh.packages.${pkgs.stdenv.hostPlatform.system}.default
  ];

  # A service with its own group.
  virtualisation.docker.enable = true;
  users.users.alice.extraGroups = [ "docker" ];

  # zsh (the user's shell from spec.toml) with the powerlevel10k prompt and its font.
  programs.zsh.promptInit = "source ${pkgs.zsh-powerlevel10k}/share/zsh-powerlevel10k/powerlevel10k.zsh-theme";
  fonts.packages = [ pkgs.nerd-fonts.meslo-lg ];
}
