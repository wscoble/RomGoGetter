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
          fastapi        # for the indexer app
          uvicorn        # ASGI server
        ]);

        # The fork's source files, packaged so the indexer can import rgg as a module.
        # We strip the .pyw to .py because importlib.util.spec_from_file_location
        # chokes on .pyw extensions (NoneType.loader error).
        rggSource = pkgs.stdenv.mkDerivation {
          name = "romgogetter-source";
          src = ./.;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          buildPhase = ''
            mkdir -p $out
            cp RomGoGetter_v0.18.pyw $out/rgg.py
            cp RomGoGetter_groups.json RomGoGetter_dat_groups.json $out/
            cp SECURITY.md README.md $out/
            cp -r indexer $out/indexer
          '';
        };

        # The indexer server — a standalone binary that imports rgg from
        # rggSource. Used both as a nix run target and inside the OCI image.
        indexerServer = pkgs.writeShellScriptBin "romgogetter-indexer" ''
          export PYTHONPATH="${rggSource}:${pythonEnv}/${pythonEnv.sitePackages}"
          exec ${pythonEnv}/bin/python -m indexer.server "$@"
        '';
      in {

        # `nix develop` — drops you into a shell with everything needed.
        devShells.default = pkgs.mkShell {
          buildInputs = with pkgs; [
            pythonEnv
            aria2          # required for the Minerva/Myrient torrent feature
            git            # not strictly required but useful
          ];

          shellHook = ''
            echo "RomGoGetter fork dev shell (NixOS)"
            echo "  python:  $(python --version)"
            echo "  aria2c:  $(aria2c --version | head -1)"
            echo "  pip:     $(pip --version 2>/dev/null || echo 'bundled')"
            echo ""
            echo "Run:  python RomGoGetter_v0.18.pyw"
            echo "Run:  romgogetter-indexer  (Torznab + Transmission RPC on :9696)"
            echo "Optional IGDB creds (Top-N):"
            echo "  export IGDB_CLIENT_ID=...; export IGDB_TWITCH_SECRET=..."
          '';
        };

        # `nix run` — launches the GUI.
        apps.default = {
          type = "app";
          program = toString (pkgs.writeShellScript "romgogetter" ''
            exec ${pythonEnv}/bin/python ${./RomGoGetter_v0.18.pyw} "$@"
          '');
        };

        # `nix run .#indexer` — runs the FastAPI indexer service.
        apps.indexer = {
          type = "app";
          program = toString indexerServer;
        };

        # `nix build .#indexer-image` — OCI-compatible container image suitable
        # for `podman load` and k3s ctr image import.
        #
        # We don't pull in pkgs.dockerTools or pkgs.buildah as flake inputs
        # because buildLayeredImage would force every k3s node to have the
        # same nix store closure, which isn't true for our multi-host setup.
        # Instead we expose a `dockerfile`-style build script that the
        # private nixos-config invokes at deploy time.
        packages.indexer-image = pkgs.stdenv.mkDerivation {
          name = "romgogetter-indexer-image";
          src = ./.;
          nativeBuildInputs = [ pkgs.makeWrapper ];
          buildPhase = ''
            mkdir -p $out/bin $out/share/romgogetter
            cp RomGoGetter_v0.18.pyw $out/share/romgogetter/rgg.py
            cp RomGoGetter_groups.json RomGoGetter_dat_groups.json $out/share/romgogetter/
            cp -r indexer $out/share/romgogetter/indexer
            cat > $out/bin/build-image.sh <<EOF
            #!/bin/sh
            set -e
            BUILDER=\''${BUILDER:-${pkgs.buildah}/bin/buildah}
            CTX=$out/share/romgogetter
            \$BUILDER from --name rgg-base ${pythonEnv}
            \$BUILDER copy rgg-base $CTX /app
            \$BUILDER run rgg-base /bin/sh -c "ln -s ${pkgs.aria2}/bin/aria2c /usr/local/bin/aria2c"
            \$BUILDER run rgg-base /bin/sh -c "mkdir -p /app/data && chmod 777 /app/data"
            \$BUILDER config --cmd '/usr/bin/python -m indexer.server' rgg-base
            \$BUILDER config --port 9696 rgg-base
            \$BUILDER config --env PYTHONPATH=/app rgg-base
            \$BUILDER commit rgg-base romgogetter-indexer:latest
            \$BUILDER tag romgogetter-indexer:latest romgogetter-indexer:$(cat .git/HEAD 2>/dev/null | tr -d '\n' | head -c 8 || echo local)
            EOF
            chmod +x $out/bin/build-image.sh
            cat > $out/bin/save-image.sh <<EOF
            #!/bin/sh
            set -e
            BUILDER=\''${BUILDER:-${pkgs.buildah}/bin/buildah}
            \$BUILDER push romgogetter-indexer:latest docker-archive:/tmp/romgogetter-indexer.tar:romgogetter-indexer:latest
            EOF
            chmod +x $out/bin/save-image.sh
          '';
        };
      }
    );
}