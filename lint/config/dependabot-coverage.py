"""Fail when a tracked manifest has no .github/dependabot.yml entry for its directory.

Dependabot has no catch-all: every ecosystem and directory must be listed, so a
new package.json or Dockerfile is silently never updated. File list on stdin.
"""
import os
import re
import sys

import yaml

# Ecosystems a directory may be covered by, per kind of manifest.
PIP = {"pip", "uv"}
TOFU = {"opentofu", "terraform"}


def needs(path):
    """(accepted ecosystems, directory) this file needs covered, or None."""
    d, name = os.path.split(path)
    try:
        text = open(path, errors="replace").read()
    except OSError:
        return None
    if re.fullmatch(r"\.github/workflows/[^/]+\.ya?ml", path):
        return {"github-actions"}, ""
    if name in ("action.yml", "action.yaml"):
        # Composite actions are scanned only when their directory is listed.
        return ({"github-actions"}, d) if re.search(r"^\s*-?\s*uses:", text, re.M) else None
    if name == "package.json":
        deps = re.search(r'"(dev|peer|optional)?[dD]ependencies"\s*:\s*\{\s*"', text)
        return ({"npm", "bun"}, d) if deps else None
    if name == "Cargo.lock":
        return {"cargo"}, d
    if name == "go.mod":
        return ({"gomod"}, d) if re.search(r"^require\b", text, re.M) else None
    if name in ("pyproject.toml", "uv.lock", "Pipfile") or re.fullmatch(r"requirements[^/]*\.txt", name):
        return PIP, d
    if re.fullmatch(r"(Dockerfile|Containerfile)[^/]*", name):
        return {"docker"}, d
    if re.fullmatch(r"(docker-)?compose[^/]*\.ya?ml", name):
        return {"docker-compose"}, d
    if name == ".terraform.lock.hcl" or (name.endswith(".tf") and re.search(r'^\s*backend\s+"', text, re.M)):
        return TOFU, d  # root modules only; child modules follow their root
    if name == "Gemfile":
        return {"bundler"}, d
    if name == "composer.json":
        return {"composer"}, d
    if name == "pom.xml":
        return {"maven"}, d
    if re.fullmatch(r"(build|settings)\.gradle(\.kts)?", name):
        return {"gradle"}, d
    if name == "devcontainer.json" or path == ".devcontainer.json":
        return {"devcontainers"}, ""
    if path == ".gitmodules":
        return {"gitsubmodule"}, ""
    return None


def norm(d):
    return "/" + d.strip("/") if d.strip("/") else "/"


def glob_re(pattern):
    # Dependabot globs: * stays within one directory, /** spans zero or more.
    out = re.escape(pattern).replace(r"/\*\*", "(/.*)?").replace(r"\*", "[^/]*")
    return re.compile(out)


def covered(eco, d, entries):
    return any(e in eco and glob_re(pattern).fullmatch(d) for e, pattern in entries)


files = [f for f in sys.stdin.read().splitlines() if f]
want = {}
for f in files:
    n = needs(f)
    if n:
        eco, d = n
        want.setdefault((frozenset(eco), norm(d)), []).append(f)

entries, errors = [], []
cfg = next((p for p in (".github/dependabot.yml", ".github/dependabot.yaml") if os.path.exists(p)), None)
if cfg:
    updates = (yaml.safe_load(open(cfg)) or {}).get("updates") or []
    for i, u in enumerate(updates if isinstance(updates, list) else [updates]):
        dirs = u.get("directories", [u.get("directory")]) if isinstance(u, dict) else None
        if not isinstance(dirs, list) or not all(isinstance(x, str) for x in dirs):
            errors.append(f"{cfg}: updates[{i}] needs a directory string or a directories list")
            continue
        entries += [(u.get("package-ecosystem"), norm(x)) for x in dirs]

missing = [(eco, d, fs) for (eco, d), fs in sorted(want.items(), key=lambda x: x[0][1])
           if not covered(eco, d, entries)]
for eco, d, fs in missing:
    print(f"{cfg or '.github/dependabot.yml'}: no {'/'.join(sorted(eco))} entry for {d} ({fs[0]})")
for e in errors:
    print(e)
sys.exit(1 if missing or errors else 0)
