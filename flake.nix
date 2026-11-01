{
  description = "SuperBot — a simulated Unitree G1 plays Job Simulator VR with no headset";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [
        "x86_64-linux"
        "aarch64-linux"
      ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in
    {
      # The app itself is standard-library-only Python; what this shell pins is
      # the host tooling the launch path needs (nested Weston VNC, websockify
      # and noVNC, plus optional magick and mpv for --preview-smoke).
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            packages = [
              pkgs.python3
              pkgs.weston
              pkgs.imagemagick
              pkgs.mpv
              pkgs.novnc
              pkgs.python3Packages.websockify
              # OpenVR -> OpenXR shim. Proton's C:\vrclient bridge loads its
              # bin/linux64/vrclient.so when VR_OVERRIDE points here. xrizer panics
              # on Job Simulator's action manifest; OpenComposite does not.
              pkgs.opencomposite
            ];
            shellHook = ''
              # Hand the shim path to the app through its documented env override,
              # so config.toml need not hard-code a /nix/store path.
              export SUPERBOT_PATHS_VR_OVERRIDE="''${SUPERBOT_PATHS_VR_OVERRIDE:-${pkgs.opencomposite}/lib/opencomposite}"
            '';
          };
        }
      );

      # Only superbot/, tests/ and spike/c0/ are copied in: the rest of spike/
      # holds gigabytes of vendored upstreams and weights the tests never touch.
      checks = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default =
            pkgs.runCommand "superbot-tests"
              {
                nativeBuildInputs = [ pkgs.python3 ];
              }
              ''
                cp -r ${./superbot} superbot
                cp -r ${./tests} tests
                # The manual-mode tests load the real pose client from here.
                mkdir -p spike
                cp -r ${./spike/c0} spike/c0
                chmod -R u+w superbot tests spike
                # The orchestrator puts its Unix sockets under XDG_RUNTIME_DIR and
                # the sandbox has none, so give it one that exists.
                export XDG_RUNTIME_DIR=$PWD/.xdg-runtime
                mkdir -p "$XDG_RUNTIME_DIR"
                export PYTHONDONTWRITEBYTECODE=1
                python3 -m unittest discover -s tests -t .
                touch $out
              '';
        }
      );
    };
}
