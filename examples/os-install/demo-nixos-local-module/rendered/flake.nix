# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render.
# Commit this directory, flake.lock, ../local.nix and ../hardware-configuration.nix (if any) to git:
# Nix copies only git-tracked files of a flake.
{
  description = "NixOS host demo-nixos-local-module (prov os-install, backend nixos-disko)";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";
    disko.url = "github:nix-community/disko/v1.13.0";
    disko.inputs.nixpkgs.follows = "nixpkgs";
    # Extra inputs from spec.toml (inputs), each with its own locked dependencies.
    nh.url = "github:nix-community/nh/v4.5.0";
  };

  outputs = { self, nixpkgs, disko, ... }@inputs: {
    # The target system.
    # Its modules (../local.nix among them) get every flake input as `inputs`.
    nixosConfigurations.demo-nixos-local-module = nixpkgs.lib.nixosSystem {
      specialArgs = { inherit inputs; };
      modules = [
        { nixpkgs.hostPlatform = "x86_64-linux"; }
        disko.nixosModules.disko
        ./configuration.nix
        ./disko.nix
      ];
    };

    # The USB installer: nix build .#nixosConfigurations.<host>-installer.config.system.build.isoImage
    nixosConfigurations.demo-nixos-local-module-installer = nixpkgs.lib.nixosSystem {
      specialArgs = { inherit self; };
      modules = [
        { nixpkgs.hostPlatform = "x86_64-linux"; }
        ./installer.nix
      ];
    };
  };
}
