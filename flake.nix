{
  description = "RomGoGetter personal fork — see SECURITY.md";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        # python.withPackages gives us both the interpreter and the script-importable
        # packages. tkinter ships with the python interpreter in nixpkgs via
        # pkgs.tkinter (a passthrough), but it's easier to just include
        # python3Packages.tkinter so `import tkinter` resolves on every distro.
        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          tkinter        # required by RomGoGetter_v0.18.pyw
          rapidfuzz      # optional but strongly recommended by upstream README
          cloudscraper   # required for Eden (Switch) / MobyGames compatibility
        ]);
      in {
        # `nix develop` — drops you into a shell with everything needed.
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            pythonEnv
            aria2          # required for the Minerva/Myrient torrent feature
            git            # not strictly required but useful
          ];

          # Don't need LD_LIBRARY_PATH munging or shellHook tweaks; the env-var
          # approach to IGDB creds is documented in SECURITY.md.
          shellHook = ''
            echo "RomGoGetter fork dev shell (NixOS)"
            echo "  python:  $(python --version)"
            echo "  aria2c:  $(aria2c --version | head -1)"
            echo "  pip:     $(pip --version 2>/dev/null || echo 'bundled')"
            echo ""
            echo "Run:  python RomGoGetter_v0.18.pyw"
            echo "Optional IGDB creds (Top-N):"
            echo "  export IGDB_CLIENT_ID=...; export IGDB_TWITCH_SECRET=..."
          '';
        };

        # `nix run` shortcut. Equivalent to `nix shell` + running python.
        apps.default = {
          type = "app";
          program = toString (pkgs.writeShellScript "romgogetter" ''
            exec ${pythonEnv}/bin/python ${./RomGoGetter_v0.18.pyw} "$@"
          '');
        };
      }
    );
}
