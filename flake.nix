{
  description = "SuperBot — a simulated Unitree G1 plays Job Simulator VR with no headset";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  # OpenComposite is a C++ .so that Proton's vrclient bridge dlopen()s inside the
  # Steam runtime. A build from a nixpkgs whose glibc is newer than the runtime's
  # references GLIBC_2.43 symbols the runtime cannot satisfy, so the bridge falls
  # back to wineopenxr and every OpenVR title fails with VRInitError 105 (as Job
  # Simulator did). Build just that package from a nixpkgs whose glibc is at or
  # below the runtime's (this rev is 2.42, matching Steam Linux Runtime 4).
  inputs.nixpkgs-compat.url = "github:NixOS/nixpkgs/0954f7ee2f6bb3dc7d4e3d0d8bcb8fd4bde4cfc5";

  outputs =
    { nixpkgs, nixpkgs-compat, ... }:
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
          # Only opencomposite comes from the older pin; everything else stays current.
          pkgsCompat = nixpkgs-compat.legacyPackages.${system};
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
              # Sends the fullscreen client message weston's xwm needs; the game's
              # own _NET_WM_STATE_FULLSCREEN is ignored, so it would stay windowed.
              pkgs.xdotool
              # OpenVR -> OpenXR shim. Proton's C:\vrclient bridge loads its
              # bin/linux64/vrclient.so when VR_OVERRIDE points here. xrizer panics
              # on Job Simulator's action manifest; OpenComposite does not.
              pkgsCompat.opencomposite
            ];
            shellHook = ''
              # Hand the shim path to the app through its documented env override,
              # so config.toml need not hard-code a /nix/store path.
              export SUPERBOT_PATHS_VR_OVERRIDE="''${SUPERBOT_PATHS_VR_OVERRIDE:-${pkgsCompat.opencomposite}/lib/opencomposite}"
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
