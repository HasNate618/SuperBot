{
  description = "SuperBot — a simulated Unitree G1 plays Superhot VR with no headset";

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
            ];
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
