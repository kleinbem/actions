# actions

Shared GitHub Actions for the kleinbem repos. An action lives here once two or more repos use it; until then it stays in the repo that needs it.

## nix-setup

Installs Determinate Nix and, when the job has the secrets, joins NetBird and configures the mesh-only Attic cache as a substituter.

```yaml
- uses: kleinbem/actions/nix-setup@<sha> # v1.0.0
  with:
    # Optional: read from the Attic cache. Both secrets or nothing; without
    # them (fork PRs, Dependabot) the job just builds without the cache.
    netbird-setup-key: ${{ secrets.NETBIRD_SETUP_KEY_EPHEMERAL }}
    attic-token: ${{ secrets.ATTIC_PULL_TOKEN }}
```

| Input | Default | What it does |
|---|---|---|
| `github-token` | `github.token` | Token for github.com fetches. Pass an App token when the job reads other repos or needs a higher rate limit. |
| `extra-conf` | | Extra `nix.conf` lines after `trusted-users = root runner`. |
| `nix-path` | | Sets `NIX_PATH` for the rest of the job. |
| `free-disk-space` | `false` | Reclaims ~20 GiB. For NixOS systems and VM tests. |
| `configure-auto-gc` | `false` | GC when free space drops below 1 GiB. For big closures. |
| `netbird-setup-key` | | Ephemeral NetBird key. Runner peers land in `ci-runners` and may reach only the cache. |
| `attic-token` | | `ATTIC_PULL_TOKEN` for reading, `ATTIC_PUSH_TOKEN` only for jobs that push. |
| `attic-endpoint`, `attic-cache` | `https://cache.kleinbem.dev/`, `system` | The cache. |
| `attic-mesh-ip` | `100.117.146.201` | Fallback `/etc/hosts` pin if NetBird DNS does not answer. |
| `ca-cert` | bundled `caddy-root.crt` | Root CA the cache is served with. |

Output `attic` is `'true'` when the cache was configured. The action never pushes: a job that should push does it itself, for example with `nix-fast-build --attic-cache system`, so only paths it really built land in the cache.

The secrets come from `nix/infra/github-secrets.tf` (sops-backed), per repo.

## Versioning

Consumers pin a full commit sha with the release tag as a comment; Dependabot bumps it. Releases are `vMAJOR.MINOR.PATCH` tags, and `vMAJOR` moves with them. Breaking input changes bump the major.

```bash
git tag -a v1.1.0 -m "nix-setup: ..." && git tag -f v1 v1.1.0
git push origin v1.1.0 && git push -f origin v1
```

## Bundled CA

`nix-setup/caddy-root.crt` is a copy of `nix-config/pki/caddy-root.crt` (Caddy Local Authority - 2026 ECC Root, valid to 2036). If the root rotates, update both; nix-config passes its own copy through `ca-cert`.
