# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render.
# Commit this directory, flake.lock and ../hardware-configuration.nix (if any) to git:
# Nix copies only git-tracked files of a flake.
{
  description = "NixOS host demo-nixos-gnome-luks (prov os-install, backend nixos-disko)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    disko.url = "github:nix-community/disko/v1.13.0";
    disko.inputs.nixpkgs.follows = "nixpkgs";
  };

  outputs = { self, nixpkgs, disko, ... }: {
    # The target system.
    nixosConfigurations.demo-nixos-gnome-luks = nixpkgs.lib.nixosSystem {
      modules = [
        { nixpkgs.hostPlatform = "x86_64-linux"; }
        disko.nixosModules.disko
        ./configuration.nix
        ./disko.nix
      ];
    };

    # The USB installer: nix build .#nixosConfigurations.<host>-installer.config.system.build.isoImage
    nixosConfigurations.demo-nixos-gnome-luks-installer = nixpkgs.lib.nixosSystem {
      specialArgs = { inherit self; };
      modules = [
        { nixpkgs.hostPlatform = "x86_64-linux"; }
        ./installer.nix
      ];
    };
  };
}
