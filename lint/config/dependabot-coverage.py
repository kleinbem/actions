"""Fail when a tracked manifest has no .github/dependabot.yml entry for its directory.

Dependabot has no catch-all: every ecosystem and directory must be listed, so a
new package.json or Dockerfile is silently never updated. File list on stdin.
"""
import fnmatch
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
    if name == "devcontainer.json":
        return {"devcontainers"}, ""
    if path == ".gitmodules":
        return {"gitsubmodule"}, ""
    return None


def norm(d):
    return "/" + d.strip("/") if d.strip("/") else "/"


def covered(eco, d, entries):
    # `directories` takes globs; fnmatch's * also crosses /, like Dependabot's **.
    return any(e in eco and fnmatch.fnmatchcase(d, pattern) for e, pattern in entries)


files = [f for f in sys.stdin.read().splitlines() if f]
want = {}
for f in files:
    n = needs(f)
    if n:
        eco, d = n
        want.setdefault((frozenset(eco), norm(d)), []).append(f)

entries = []
cfg = next((p for p in (".github/dependabot.yml", ".github/dependabot.yaml") if os.path.exists(p)), None)
if cfg:
    for u in (yaml.safe_load(open(cfg)) or {}).get("updates") or []:
        dirs = u.get("directories") or [u.get("directory", "/")]
        entries += [(u.get("package-ecosystem"), norm(x)) for x in dirs]

missing = [(eco, d, fs) for (eco, d), fs in sorted(want.items(), key=lambda x: x[0][1])
           if not covered(eco, d, entries)]
for eco, d, fs in missing:
    print(f"{cfg or '.github/dependabot.yml'}: no {'/'.join(sorted(eco))} entry for {d} ({fs[0]})")
sys.exit(1 if missing else 0)
