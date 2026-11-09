{
  description = "Tools for the kleinbem fleet-lint action";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { nixpkgs, ... }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" "aarch64-darwin" ];
      forAll = f: nixpkgs.lib.genAttrs systems (s: f nixpkgs.legacyPackages.${s});
    in
    {
      packages = forAll (pkgs: {
        default = pkgs.buildEnv {
          name = "fleet-lint-tools";
          paths = with pkgs; [
            actionlint
            deadnix
            git
            gitleaks
            hadolint
            markdownlint-cli2
            opentofu
            osv-scanner
            (python3.withPackages (ps: [ ps.pyyaml ]))
            ruff
            shellcheck
            statix
            tflint
            trivy
            yamllint
            zizmor
          ];
        };
      });
    };
}
