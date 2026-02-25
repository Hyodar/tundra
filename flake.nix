{
  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = {
    self,
    nixpkgs,
  }: let
    mkosi = system: let
      pkgs = import nixpkgs {inherit system;};
      mkosi-unwrapped = pkgs.mkosi.override {
        extraDeps = with pkgs; [
          apt
          dpkg
          gnupg
          debootstrap
          squashfsTools
          dosfstools
          e2fsprogs
          mtools
          cryptsetup
          gptfdisk
          util-linux
          zstd
          which
          qemu-utils
          parted
          unzip
          jq
        ];
      };
    in
      pkgs.writeShellScriptBin "mkosi" ''
        exec ${pkgs.util-linux}/bin/unshare \
          --map-auto --map-current-user \
          --setuid=0 --setgid=0 \
          -- \
          env PATH="$PATH" \
          ${mkosi-unwrapped}/bin/mkosi "$@"
      '';
  in {
    devShells = builtins.listToAttrs (map (system: {
      name = system;
      value.default = (import nixpkgs {inherit system;}).mkShell {
        nativeBuildInputs = [(mkosi system)];
        shellHook = ''
          mkdir -p mkosi.cache mkosi.builddir
        '';
      };
    }) ["x86_64-linux" "aarch64-linux"]);
  };
}
