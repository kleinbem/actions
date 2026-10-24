# actions

Shared composite GitHub Actions for the kleinbem repos. `CLAUDE.md` is a symlink to this file.

- One directory per action (`nix-setup/action.yaml`). Add one only when two or more repos need it.
- Pin every third-party action by full sha with the version as a comment. Dependabot (`.github/dependabot.yml`) must list each action directory.
- Inputs are a public interface. Renaming or removing one, or changing a default in a way that changes behaviour, is a major release.
- Secrets never get interpolated into `run:` scripts; pass them through `env:`.
- Release: annotated `vX.Y.Z` tag on a green main, then move `vX`. Consumers pin the sha, never `@main` or a moving tag.
- CI (`.github/workflows/ci.yaml`) runs the action itself without secrets on x86 and ARM; keep that path working, it is what fork PRs and Dependabot runs take.
