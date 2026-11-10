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
| `extra-conf` | | Extra `nix.conf` lines after the defaults: `trusted-users = root runner` and `fallback = true` (a failed cache download builds the path instead of failing). |
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

## lock-pr

Commits an updated `flake.lock` to a bot branch (`update-flake-lock`) as a GitHub-signed commit, then makes sure an open PR with auto-merge carries it. If the old PR was closed, it opens a new one rather than reopening, because GitHub refuses to reopen a PR whose branch moved. It fails the step when the PR ends up without auto-merge.

```yaml
- uses: kleinbem/actions/lock-pr@<sha> # v1.5.0
  with:
    token: ${{ steps.app-token.outputs.token }} # contents: write, pull-requests: write
```

| Input | Default | What it does |
|---|---|---|
| `token` | | GitHub App token. A PR from `GITHUB_TOKEN` would not trigger the checks. |
| `working-directory` | `.` | Checkout holding the updated lock. |
| `lock-file` | `flake.lock` | Path of the lock in the repo. |
| `branch` | `update-flake-lock` | Bot branch. |
| `base-sha` | | Commit to extend (an open lock PR's head). Empty resets the branch to `reset-to`. |
| `reset-to` | `github.sha` | Commit the branch is reset to: the one the lock was updated from. If main's lock changed since, the PR conflicts instead of reverting that change. |
| `title`, `body` | | PR title (also the commit headline) and body. |

Output `pr` is the PR number, empty when the lock did not change.

## image-bump

Keeps container image tags current where Dependabot can't parse them: Nix strings, `just` recipes. A pin opts in with an annotation naming its scheme, at the end of its line or on a comment line right above it:

```nix
image = "ghcr.io/goauthentik/server:2026.8.5"; # image-bump: calver
```

| Scheme | Picks the newest tag that |
|---|---|
| `semver`, `calver` | has the current tag's shape: same `v` prefix, same number of numeric parts. Pre-releases (`-rc1`, `.dev3`) and moving aliases (`2026.8` next to `2026.8.5`) never match |
| `ubuntu-lts` | is an LTS release (`YY.04`, even year) |
| `alias:<tag>` | has the current shape and is the same image as `<tag>` (`alias:latest` for Fedora, where `46` is rawhide) |
| `regex:<re>` | fully matches `<re>`, compared by number runs (`regex:^xpu-\d{8}$`) |

It lists tags with skopeo, anonymously (`list-tags` doesn't count against Docker Hub's pull limit; label and `alias:` lookups read a manifest and do). For each image with a newer tag it commits the change as a GitHub-signed commit to `image-bump/<image>` and opens a PR with auto-merge, one per image. Majors are proposed too, flagged in the PR body; when the image has an `org.opencontainers.image.source` label on GitHub, the body links the release notes. A PR already proposing the same tag is left alone; one for a newer tag is replaced; one for an image that is current on main is closed. A run on a branch other than the default opens PRs against that branch without auto-merge, for tests. Unannotated pins are ignored, so leaving the annotation off excludes one.

```yaml
- uses: kleinbem/actions/image-bump@<sha> # v1.7.0
  with:
    token: ${{ steps.app-token.outputs.token }} # contents: write, pull-requests: write
```

| Input | Default | What it does |
|---|---|---|
| `token` | | GitHub App token. A PR from `GITHUB_TOKEN` would not trigger the checks. |
| `working-directory` | `.` | Checkout to scan (a git work tree). |
| `pathspec` | `*.nix *.just *justfile *Justfile` | Files to scan, as git pathspecs. |
| `dry-run` | `false` | Report only, open no PRs. |
| `default-branch` | the repo's | Branch whose PRs get auto-merge. |

Output `bumps` is a JSON list of the newer tags found (`image`, `from`, `to`, `major`). Tests: `python3 -m unittest discover -s image-bump`.

## image-bump (reusable workflow)

Runs `image-bump` on the caller's repo with its GitHub App token, so the PRs' checks run.

```yaml
# .github/workflows/image-bump.yaml
name: Image bump
on:
  workflow_dispatch:
    inputs:
      dry-run:
        type: boolean
        default: false
  schedule:
    - cron: '45 4 * * *'
permissions: {}
jobs:
  image-bump:
    uses: kleinbem/actions/.github/workflows/image-bump.yaml@<sha> # v1.7.1
    with:
      dry-run: ${{ inputs.dry-run || false }}
    secrets:
      app-id: ${{ secrets.APP_ID }}
      app-private-key: ${{ secrets.APP_PRIVATE_KEY }}
```

| Input | Default | What it does |
|---|---|---|
| `pathspec` | `*.nix *.just *justfile *Justfile` | Files to scan. |
| `dry-run` | `false` | Report only. |

## actionlint

Lints GitHub Actions workflow files with `actionlint` and `shellcheck`. Requires `shellcheck` and `python3` on `PATH`. Both are preinstalled on Ubuntu GitHub-hosted runners; on macOS runners, install `shellcheck` first (e.g. `brew install shellcheck`).

```yaml
- uses: kleinbem/actions/actionlint@<sha> # v1.0.0
  with:
    # Optional flags and custom workflow file targets:
    args: -color
    files: ""
```

| Input | Default | What it does |
|---|---|---|
| `args` | `-color` | Flags passed to actionlint. |
| `files` | `""` | Workflow files to check (empty checks all workflows under `.github/workflows`). |

## lint

One required check for every repo: each tool runs only when the repo has its file types, every finding is reported, and the job fails if any tool failed. Tools come from `lint/flake.nix` (nixpkgs, locked), so run the action after `nix-setup`. It needs no secrets.

```yaml
# .github/workflows/fleet-lint.yaml
name: fleet-lint
on:
  push:
    branches: [main]
  pull_request:
permissions: {}
jobs:
  fleet-lint:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@<sha> # v7.0.1
        with:
          fetch-depth: 0 # gitleaks scans every commit of the PR
          persist-credentials: false
      - uses: kleinbem/actions/nix-setup@<sha> # v1.3.0
      - uses: kleinbem/actions/lint@<sha> # v1.3.0
```

| Check | Runs when the repo has | Notes |
|---|---|---|
| gitleaks | always | PR and push: the new commits; otherwise the tree |
| actionlint | workflows | with shellcheck on `run:` blocks |
| action-shellcheck | composite `action.yaml` | actionlint doesn't read them |
| zizmor | workflows, actions, `dependabot.yml` | fails on medium and up |
| shellcheck | `*.sh`, `*.bash`, shell shebangs | |
| yamllint | YAML | correctness rules only |
| ruff | Python | `E4,E7,E9,F,S` minus `S101,S603,S607`, ignoring the repo's own rule set |
| markdownlint | Markdown | broken links, anchors and tables only |
| hadolint | Dockerfiles | |
| tofu, tflint, trivy | `*.tf` | fmt + validate per directory; trivy HIGH and CRITICAL |
| dependabot-coverage | always | each workflow dir, composite action using `uses:`, manifest (npm, cargo, gomod, pip/uv, docker, compose, OpenTofu root, …) needs a `dependabot.yml` entry for its directory |
| osv-scanner | language lockfiles | not the Nix closure |

| Input | Default | What it does |
|---|---|---|
| `github-token` | `github.token` | Read token for zizmor's online audits. |
| `skip` | | Space-separated checks to skip. Give the reason next to it. |

A repo's own `.gitleaks.toml`, `.yamllint*`, `.markdownlint*`, `.hadolint.yaml`, `.trivyignore*` or `<dir>/.tflint.hcl` replaces the default in `lint/config/`. Silence one finding inline where the tool allows it (`gitleaks:allow`, `# noqa: S...`, `# zizmor: ignore[...]`, `# shellcheck disable=...`), with the reason. Checkov isn't included: nixpkgs' build depends on python ecdsa, marked insecure for CVE-2024-23342, which upstream won't fix (tlsfuzzer/python-ecdsa#330).

## dependabot-auto-merge (reusable workflow)

Arms auto-merge on Dependabot PRs where every update is a patch or minor bump; majors and anything unclassified stay open for review. The repo needs auto-merge and squash merges allowed (github-config sets both). The merge runs with the repo's GitHub App token (`APP_ID`/`APP_PRIVATE_KEY` Dependabot secrets from `nix/infra/github-secrets.tf`), so main's push workflows still run.

```yaml
# .github/workflows/dependabot-auto-merge.yaml
name: Dependabot auto-merge
on: pull_request
permissions: {}
jobs:
  auto-merge:
    permissions:
      pull-requests: read
    uses: kleinbem/actions/.github/workflows/dependabot-auto-merge.yaml@<sha> # v1.2.1
    secrets:
      app-id: ${{ secrets.APP_ID }}
      app-private-key: ${{ secrets.APP_PRIVATE_KEY }}
```

Pair it with a `cooldown` in the repo's `dependabot.yml`, so version updates don't auto-merge on the day they are published. Security updates skip the cooldown by design.

## maintain-flake (reusable workflow)

The nightly `flake.lock` update: `nix flake update`, a validation of the new lock, then a PR with auto-merge through `lock-pr`. The repo's own checks run on that PR and decide whether it lands. The PR is opened with the repo's GitHub App token (`APP_ID`/`APP_PRIVATE_KEY` from `nix/infra/github-secrets.tf`), so its checks run.

```yaml
# .github/workflows/maintain.yaml
name: Maintain Flake
on:
  workflow_dispatch:
  schedule:
    - cron: '15 2 * * *'
permissions: {}
jobs:
  maintain:
    uses: kleinbem/actions/.github/workflows/maintain-flake.yaml@<sha> # v1.6.0
    with:
      validate: nix flake check --no-build # the default
    secrets:
      app-id: ${{ secrets.APP_ID }}
      app-private-key: ${{ secrets.APP_PRIVATE_KEY }}
```

| Input | Default | What it does |
|---|---|---|
| `validate` | `nix flake check --no-build` | Shell (bash, `-euo pipefail`) run on the updated lock. A failure stops before the PR. |
| `nix-path` | | `NIX_PATH` for the update and validation, e.g. `nixpkgs=channel:nixos-unstable`. |

## dispatch-lock-bump (reusable workflow)

Asks `nix-config`'s Maintain Flake to bump this repo's flake input right after its main branch turns green:

```yaml
# .github/workflows/dispatch-lock-bump.yaml
name: Dispatch nix-config lock bump
on:
  workflow_run:
    workflows: ["Check Flake"]
    branches: [main]
    types: [completed]
permissions: {}
jobs:
  dispatch:
    if: >-
      github.event.workflow_run.conclusion == 'success' &&
      github.event.workflow_run.event == 'push' &&
      github.event.workflow_run.head_repository.full_name == github.repository
    uses: kleinbem/actions/.github/workflows/dispatch-lock-bump.yaml@<sha> # v1.8.0
    secrets:
      app-id: ${{ secrets.APP_ID }}
      app-private-key: ${{ secrets.APP_PRIVATE_KEY }}
```

| Input | Default | What it does |
|---|---|---|
| `target-owner` | `kleinbem` | Owner of the target repository. |
| `target-repo` | `nix-config` | Target repository to receive the `repository_dispatch`. |
| `input` | caller repo name | Flake input name to bump in nix-config. |
| `sha` | caller commit SHA | Commit SHA recorded in client_payload. |

## Versioning

Consumers pin a full commit sha with the release tag as a comment; Dependabot bumps it. Releases are `vMAJOR.MINOR.PATCH` tags, and `vMAJOR` moves with them. Breaking input changes bump the major.

Refresh the lint tools first (`nix flake update --flake ./lint`, in its own PR), so each release ships current tools.

```bash
git tag -a v1.1.0 -m "nix-setup: ..." && git tag -f v1 v1.1.0
git push origin v1.1.0 && git push -f origin v1
```

## Bundled CA

`nix-setup/caddy-root.crt` is a copy of `nix-config/pki/caddy-root.crt` (Caddy Local Authority - 2026 ECC Root, valid to 2036). If the root rotates, update both; nix-config passes its own copy through `ca-cert`.
