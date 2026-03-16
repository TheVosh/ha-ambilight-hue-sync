{
  description = "Hue Entertainment Bridge for Home Assistant";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python313;
        pythonPkgs = python.pkgs;
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            python
            pythonPkgs.cffi
            pythonPkgs.pytest
            pythonPkgs.pytest-asyncio
            pythonPkgs.cryptography
            pythonPkgs.aiohttp
            pythonPkgs.zeroconf
            pythonPkgs.mypy
            pkgs.openssl
            pkgs.ruff
            pkgs.pre-commit
          ];

          shellHook = ''
            export LD_LIBRARY_PATH="${pkgs.openssl.out}/lib:$LD_LIBRARY_PATH"
            export DYLD_LIBRARY_PATH="${pkgs.openssl.out}/lib:$DYLD_LIBRARY_PATH"
            echo "Hue Entertainment dev shell"
            echo "  Python: $(python3 --version)"
            echo "  OpenSSL: $(openssl version)"
            echo "  libssl: ${pkgs.openssl.out}/lib"
          '';
        };
      }
    );
}
