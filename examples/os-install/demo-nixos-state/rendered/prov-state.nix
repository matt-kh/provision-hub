# Rendered by prov (os-install, backend nixos-disko) from spec.toml; edit spec.toml and re-render.
# Report-only state agent ([state.report], docs/state.md): a systemd timer runs
# prov-state-agent.sh, which writes this host's state record (os-install/demo-nixos-state) to the state
# repository. It never changes the system. A human places the deploy key on the host
# (prov plan prints the step); prov never renders a key.
{ lib, pkgs, ... }:
let
  # The render digest (prov digest) of this directory, computed at evaluation time:
  # sha256 over the sha256sum-style manifest of every regular file, sorted by name. The
  # installer's *.installer-copy.nix files are not part of rendered/ and are skipped, so
  # the USB stick and a checkout give the same value and the same system. Nix sees only
  # git-tracked files: commit rendered/ (and flake.lock) for the value to match prov digest.
  names = builtins.attrNames (lib.filterAttrs
    (name: type: type == "regular" && !(lib.hasSuffix ".installer-copy.nix" name))
    (builtins.readDir ./.));
  manifest = lib.concatMapStrings (name: "${builtins.hashFile "sha256" (./. + "/${name}")}  ${name}\n") names;
  renderDigest = "sha256:${builtins.hashString "sha256" manifest}";

  agent = pkgs.writeShellApplication {
    name = "prov-state-agent";
    runtimeInputs = [ pkgs.coreutils pkgs.git pkgs.openssh pkgs.jq pkgs.iproute2 ];
    text = builtins.readFile ./prov-state-agent.sh;
  };
in
{
  environment.etc."prov/render-digest".text = renderDigest + "\n";
  # The state repository's ssh host keys: the agent connects with StrictHostKeyChecking=yes.
  environment.etc."prov/state-known-hosts".text = lib.concatMapStrings (line: line + "\n") [
    "git.example.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLEEXAMPLE"
  ];

  systemd.services.prov-state-agent = {
    description = "prov state agent: report this host's state (report-only)";
    wants = [ "network-online.target" ];
    after = [ "network-online.target" ];
    environment = {
      PROV_STATE_REF = "os-install/demo-nixos-state";
      PROV_STATE_REPO = "git@git.example.com:example/hosts-state.git";
      PROV_STATE_BRANCH = "main";
      PROV_STATE_HEARTBEAT = "6h";
      PROV_STATE_IDENTITY = "/var/lib/prov-state/id_ed25519";
      PROV_STATE_KNOWN_HOSTS = "/etc/prov/state-known-hosts";
      PROV_STATE_DIR = "/var/lib/prov-state";
    };
    serviceConfig = {
      Type = "oneshot";
      ExecStart = "${agent}/bin/prov-state-agent";
      StateDirectory = "prov-state";
      StateDirectoryMode = "0700";
      UMask = "0077";
      NoNewPrivileges = true;
      PrivateTmp = true;
      ProtectSystem = "strict";
      ProtectHome = "read-only";
    };
  };
  systemd.timers.prov-state-agent = {
    wantedBy = [ "timers.target" ];
    timerConfig = {
      OnBootSec = "2min";
      OnUnitActiveSec = "15m";
      RandomizedDelaySec = "1min";
    };
  };
}
