#!/usr/bin/env python3
"""Bumps container image tags pinned in text files (Nix, just, ...).

A pin opts in with an annotation on its line, or on a comment line right above:

    image = "ghcr.io/goauthentik/server:2026.8.5"; # image-bump: calver

Schemes (see README): semver, calver, ubuntu-lts, alias:<tag>, regex:<re>.
One PR per image, committed through createCommitOnBranch so GitHub signs it.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
from typing import Callable

ANNOTATION = re.compile(r"#\s*image-bump:\s*(\S+)\s*$")
# registry/repo:tag; the registry part needs a dot or a port, as in docker.
REF = re.compile(
    r"(?<![\w./:@-])"
    r"((?:[a-z0-9-]+(?:\.[a-z0-9-]+)+(?::\d+)?/|localhost(?::\d+)?/)?"
    r"[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*(?:/[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*)*)"
    r":([A-Za-z0-9_][A-Za-z0-9._-]{0,127})"
    r"(?![\w.:@-])"
)
VERSION = re.compile(r"^(v?)(\d+(?:\.\d+)*)$")
SOURCE_LABEL = "org.opencontainers.image.source"
# Digest lookups count against Docker Hub's anonymous pull limit.
MAX_ALIAS_LOOKUPS = 5


class BumpError(Exception):
    pass


@dataclasses.dataclass
class Pin:
    path: str
    line: int  # 0-based
    image: str
    tag: str
    scheme: str


@dataclasses.dataclass
class Bump:
    image: str
    old: str
    new: str
    scheme: str
    pins: list[Pin]
    major: bool = False
    notes: str = ""


# --- scanning -------------------------------------------------------------


def scan_text(path: str, text: str) -> list[Pin]:
    pins = []
    pending = None  # scheme from a comment line, for the next code line
    for n, line in enumerate(text.splitlines()):
        m = ANNOTATION.search(line)
        code = line[: m.start()] if m else line
        if m and not code.strip():
            pending = (m.group(1), n)
            continue
        if m:
            scheme = m.group(1)
        elif pending and not line.strip().startswith("#"):
            scheme = pending[0]
        else:
            continue
        pending = None
        refs = REF.findall(code)
        if len(refs) != 1:
            raise BumpError(f"{path}:{n + 1}: expected one image:tag next to the annotation, found {len(refs)}")
        image, tag = refs[0]
        pins.append(Pin(path, n, image, tag, scheme))
    if pending:
        raise BumpError(f"{path}:{pending[1] + 1}: annotation without an image line after it")
    return pins


def scan_repo(pathspec: list[str]) -> list[Pin]:
    files = subprocess.run(
        ["git", "ls-files", "-z", "--", *pathspec], check=True, capture_output=True, text=True
    ).stdout.split("\0")
    pins = []
    for path in filter(None, files):
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            text = f.read()
        if "image-bump:" in text:
            pins += scan_text(path, text)
    return pins


# --- schemes --------------------------------------------------------------


def natural_key(tag: str) -> tuple:
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p) for p in re.findall(r"\d+|\D+", tag))


def version_shape(tag: str) -> tuple[str, int]:
    m = VERSION.match(tag)
    if not m:
        raise BumpError(f"tag {tag} is not a plain version (digits and dots, optional v)")
    return m.group(1), m.group(2).count(".") + 1


def same_shape(current: str, tags: list[str]) -> list[str]:
    """Release tags shaped like the current one: pre-releases (-rc1, .dev3)
    and moving aliases (2026.8 next to 2026.8.5) don't match."""
    prefix, parts = version_shape(current)
    shape = re.compile(rf"^{re.escape(prefix)}\d+(?:\.\d+){{{parts - 1}}}$")
    return [t for t in tags if shape.match(t)]


def candidates(scheme: str, current: str, tags: list[str]) -> list[str]:
    """Tags the scheme allows, newest first."""
    if scheme in ("semver", "calver"):
        found = same_shape(current, tags)
    elif scheme.startswith("alias:"):
        found = same_shape(current, tags)
    elif scheme == "ubuntu-lts":
        found = [t for t in tags if re.fullmatch(r"\d\d\.04", t) and int(t[:2]) % 2 == 0]
    elif scheme.startswith("regex:"):
        try:
            pattern = re.compile(scheme[len("regex:") :])
        except re.error as e:
            raise BumpError(f"bad regex in scheme {scheme}: {e}") from e
        if not pattern.fullmatch(current):
            raise BumpError(f"current tag {current} does not match {scheme}")
        found = [t for t in tags if pattern.fullmatch(t)]
    else:
        raise BumpError(f"unknown scheme {scheme}")
    return sorted(found, key=natural_key, reverse=True)


class Registry:
    """skopeo against the registry, anonymous. Tag lists don't count against
    Docker Hub's pull limit; manifest reads (digest, labels) do."""

    def __init__(self, skopeo: str = "skopeo"):
        self.skopeo = skopeo
        self._tags: dict[str, list[str]] = {}

    def _run(self, *args: str) -> str:
        cmd = [self.skopeo, args[0], "--retry-times", "3", *args[1:]]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode:
            raise BumpError(f"skopeo {' '.join(args)}: {r.stderr.strip()}")
        return r.stdout

    def tags(self, image: str) -> list[str]:
        if image not in self._tags:
            self._tags[image] = json.loads(self._run("list-tags", f"docker://{image}"))["Tags"]
        return self._tags[image]

    def digest(self, image: str, tag: str) -> str:
        raw = self._run("inspect", "--raw", f"docker://{image}:{tag}")
        return hashlib.sha256(raw.encode()).hexdigest()

    def labels(self, image: str, tag: str) -> dict:
        out = json.loads(self._run("inspect", "--no-tags", f"docker://{image}:{tag}"))
        return out.get("Labels") or {}


def newest(scheme: str, image: str, current: str, registry) -> str | None:
    tags = registry.tags(image)
    found = candidates(scheme, current, tags)
    newer = [t for t in found if natural_key(t) > natural_key(current)]
    if not newer:
        return None
    if scheme.startswith("alias:"):
        alias = scheme[len("alias:") :]
        if alias not in tags:
            raise BumpError(f"{image} has no tag {alias}")
        want = registry.digest(image, alias)
        for t in newer[:MAX_ALIAS_LOOKUPS]:
            if registry.digest(image, t) == want:
                return t
        return None
    return newer[0]


def is_major(old: str, new: str) -> bool:
    """First version component changed; dated or other tags never count."""
    a, b = VERSION.match(old), VERSION.match(new)
    return bool(a and b) and a.group(2).split(".")[0] != b.group(2).split(".")[0]


# --- release notes --------------------------------------------------------


def gh_release_url(repo: str, tag: str) -> str | None:
    """The release page for tag, under the names projects commonly use."""
    bare = tag[1:] if tag.startswith("v") else tag
    for name in dict.fromkeys([tag, f"v{bare}", bare, f"version/{bare}"]):
        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/releases/tags/{name}", "--jq", ".html_url"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def release_notes(labels: dict, tag: str, lookup: Callable[[str, str], str | None]) -> str:
    source = labels.get(SOURCE_LABEL, "")
    m = re.match(r"https?://github\.com/([\w.-]+/[\w.-]+?)(?:\.git)?/?$", source)
    if not m:
        return ""
    repo = m.group(1)
    return lookup(repo, tag) or f"https://github.com/{repo}/releases"


# --- planning -------------------------------------------------------------


def plan(pins: list[Pin], registry, lookup=gh_release_url) -> tuple[list[Bump], list[str], list[str]]:
    """Returns (bumps, current images, errors)."""
    by_image: dict[str, list[Pin]] = {}
    for p in pins:
        by_image.setdefault(p.image, []).append(p)
    bumps, current, errors = [], [], []
    for image, group in sorted(by_image.items()):
        schemes = {p.scheme for p in group}
        tags = {p.tag for p in group}
        if len(schemes) > 1 or len(tags) > 1:
            where = ", ".join(f"{p.path}:{p.line + 1} {p.tag} {p.scheme}" for p in group)
            errors.append(f"{image}: pins disagree on tag or scheme ({where})")
            continue
        scheme, old = schemes.pop(), tags.pop()
        try:
            new = newest(scheme, image, old, registry)
        except BumpError as e:
            errors.append(f"{image}: {e}")
            continue
        if not new:
            current.append(image)
            continue
        bump = Bump(image, old, new, scheme, group, is_major(old, new))
        try:
            bump.notes = release_notes(registry.labels(image, new), new, lookup)
        except BumpError as e:
            print(f"::warning::{image}: no labels for {new}: {e}", file=sys.stderr)
        bumps.append(bump)
    return bumps, current, errors


def apply(bump: Bump, read=lambda p: open(p, encoding="utf-8").read()) -> dict[str, str]:
    """New content per file, with every pin of the image moved to the new tag."""
    changed: dict[str, str] = {}
    for p in bump.pins:
        text = changed.get(p.path) or read(p.path)
        lines = text.splitlines(keepends=True)
        line = lines[p.line]
        m = ANNOTATION.search(line)
        code_end = m.start() if m else len(line)
        for r in REF.finditer(line, 0, code_end):
            if r.group(1) == p.image and r.group(2) == p.tag:
                lines[p.line] = line[: r.start(2)] + bump.new + line[r.end(2) :]
                break
        else:
            raise BumpError(f"{p.path}:{p.line + 1}: {p.image}:{p.tag} not found")
        changed[p.path] = "".join(lines)
    return changed


def slug(image: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", image.lower()).strip("-")


def title(bump: Bump) -> str:
    return f"chore: bump {bump.image} from {bump.old} to {bump.new}"


def body(bump: Bump, run_url: str) -> str:
    where = "\n".join(f"- `{p.path}` line {p.line + 1}" for p in bump.pins)
    parts = [
        f"Bumps `{bump.image}` from `{bump.old}` to `{bump.new}` (scheme `{bump.scheme}`).",
        where,
    ]
    if bump.major:
        parts.append("**Major version change**: check the release notes for breaking changes before this lands.")
    if bump.notes:
        parts.append(f"Release notes: {bump.notes}")
    parts.append(f"Opened by image-bump ({run_url}). The repo checks run on this PR; auto-merge lands it once they pass.")
    return "\n\n".join(parts)


# --- GitHub ---------------------------------------------------------------


class GitHub:
    def __init__(self, repo: str, base: str, base_sha: str, auto_merge: bool):
        self.repo, self.base, self.base_sha, self.auto_merge = repo, base, base_sha, auto_merge

    def gh(self, *args: str, input: str | None = None, check: bool = True) -> str:
        r = subprocess.run(["gh", *args], input=input, capture_output=True, text=True)
        if check and r.returncode:
            raise BumpError(f"gh {' '.join(args[:3])}: {r.stderr.strip()}")
        return r.stdout

    def open_prs(self, prefix: str) -> dict[str, dict]:
        out = self.gh(
            "pr", "list", "--repo", self.repo, "--base", self.base, "--state", "open",
            "--limit", "200", "--json", "number,headRefName,title",
        )
        return {p["headRefName"]: p for p in json.loads(out) if p["headRefName"].startswith(prefix)}

    def reset_branch(self, branch: str) -> None:
        r = subprocess.run(["gh", "api", f"repos/{self.repo}/git/ref/heads/{branch}"], capture_output=True, text=True)
        if r.returncode and "HTTP 404" not in r.stderr:
            raise BumpError(f"looking up {branch}: {r.stderr.strip()}")
        if r.returncode == 0:
            self.gh("api", "-X", "PATCH", f"repos/{self.repo}/git/refs/heads/{branch}",
                    "-f", f"sha={self.base_sha}", "-F", "force=true")
        else:
            self.gh("api", f"repos/{self.repo}/git/refs", "-f", f"ref=refs/heads/{branch}", "-f", f"sha={self.base_sha}")

    def commit(self, branch: str, headline: str, files: dict[str, str]) -> None:
        query = "mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }"
        additions = [
            {"path": path, "contents": base64.b64encode(text.encode("utf-8", "surrogateescape")).decode()}
            for path, text in files.items()
        ]
        payload = {"query": query, "variables": {"input": {
            "branch": {"repositoryNameWithOwner": self.repo, "branchName": branch},
            "message": {"headline": headline},
            "expectedHeadOid": self.base_sha,
            "fileChanges": {"additions": additions},
        }}}
        self.gh("api", "graphql", "--input", "-", input=json.dumps(payload))

    def propose(self, branch: str, pr: dict | None, bump: Bump, files: dict[str, str], run_url: str) -> str:
        self.reset_branch(branch)
        self.commit(branch, title(bump), files)
        if pr:
            self.gh("pr", "edit", "--repo", self.repo, str(pr["number"]), "--title", title(bump), "--body", body(bump, run_url))
            number = str(pr["number"])
        else:
            url = self.gh("pr", "create", "--repo", self.repo, "--base", self.base, "--head", branch,
                          "--title", title(bump), "--body", body(bump, run_url)).strip()
            number = url.rsplit("/", 1)[-1]
        if self.auto_merge:
            # Fails when auto-merge is already armed.
            self.gh("pr", "merge", "--repo", self.repo, number, "--auto", "--squash", check=False)
        return number

    def close(self, pr: dict, reason: str) -> None:
        self.gh("pr", "close", "--repo", self.repo, str(pr["number"]), "--comment", reason, "--delete-branch")


# --- main -----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pathspec", nargs="*", default=["*.nix", "*.just", "*justfile", "*Justfile"])
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing on GitHub")
    ap.add_argument("--skopeo", default=os.environ.get("SKOPEO", "skopeo"))
    args = ap.parse_args(argv)

    errors: list[str] = []
    try:
        pins = scan_repo(args.pathspec)
    except BumpError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 1
    print(f"{len(pins)} annotated pins")
    bumps, current, errors = plan(pins, Registry(args.skopeo))
    for image in current:
        print(f"current: {image}")
    for b in bumps:
        print(f"bump: {b.image} {b.old} -> {b.new}{' (major)' if b.major else ''} {b.notes}")

    summary = ["| Image | Pinned | Proposed |", "|---|---|---|"]
    summary += [f"| `{b.image}` | `{b.old}` | `{b.new}`{' (major)' if b.major else ''} |" for b in bumps]
    summary += [f"| `{i}` | current | |" for i in current]

    if not args.dry_run:
        env = os.environ
        repo, base = env["GITHUB_REPOSITORY"], env["GITHUB_REF_NAME"]
        base_sha = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
        default = env.get("DEFAULT_BRANCH", "main")
        run_url = f"{env['GITHUB_SERVER_URL']}/{repo}/actions/runs/{env['GITHUB_RUN_ID']}"
        # Branches off main are image-bump/<image>; a test run elsewhere keeps its own.
        prefix = "image-bump/" if base == default else f"image-bump/{base}/"
        hub = GitHub(repo, base, base_sha, auto_merge=base == default)
        try:
            open_prs = hub.open_prs(prefix)
        except BumpError as e:
            print(f"::error::{e}", file=sys.stderr)
            return 1
        for b in bumps:
            branch = prefix + slug(b.image)
            pr = open_prs.get(branch)
            if pr and pr["title"] == title(b):
                print(f"{b.image}: PR #{pr['number']} already proposes {b.new}")
                continue
            try:
                number = hub.propose(branch, pr, b, apply(b), run_url)
                print(f"{b.image}: PR #{number}")
            except BumpError as e:
                errors.append(f"{b.image}: {e}")
        # A PR whose image main already has at the newest tag would only roll it back.
        for image in current:
            pr = open_prs.get(prefix + slug(image))
            if pr:
                hub.close(pr, f"`{image}` on {base} is current already.")
                print(f"{image}: closed stale PR #{pr['number']}")

    for e in errors:
        print(f"::error::{e}", file=sys.stderr)
    summary += [f"\nError: {e}" for e in errors]
    if path := os.environ.get("GITHUB_OUTPUT"):
        found = [{"image": b.image, "from": b.old, "to": b.new, "major": b.major} for b in bumps]
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"bumps={json.dumps(found)}\n")
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a", encoding="utf-8") as f:
            f.write("## image-bump\n\n" + "\n".join(summary) + "\n")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
