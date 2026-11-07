"""Unit tests: python3 -m unittest discover -s image-bump"""

import json
import os
import unittest

from image_bump import (
    Bump,
    BumpError,
    Pin,
    apply,
    body,
    candidates,
    plan,
    release_notes,
    scan_text,
    slug,
    title,
)

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "testdata", "tags.json"), encoding="utf-8") as f:
    TAGS = json.load(f)


class FakeRegistry:
    """Real tag lists from testdata; digests as the fixture says (fedora:46 is rawhide)."""

    def __init__(self, tags=None, digests=None, labels=None):
        self._tags = tags or TAGS
        self._digests = digests or {
            "registry.fedoraproject.org/fedora": {"latest": "a", "45": "a", "46": "b", "rawhide": "b"},
        }
        self._labels = labels or {}
        self.digest_calls = 0

    def tags(self, image):
        if image not in self._tags:
            raise BumpError(f"no such image {image}")
        return self._tags[image]

    def digest(self, image, tag):
        self.digest_calls += 1
        return self._digests.get(image, {}).get(tag, f"other-{tag}")

    def labels(self, image, tag):
        return self._labels.get(image, {})


def no_release(repo, tag):
    return None


NIX = """\
{ ... }:
let
  image = "ghcr.io/goauthentik/server:2026.4.1"; # image-bump: calver
in
{
  a.image = "agent0ai/agent-zero:v1.20"; # image-bump: semver
  # image-bump: semver
  b.image = "mintplexlabs/anythingllm:1.18.0";
  c.image = "docker.io/yanwk/comfyui-boot:xpu-20261012"; # image-bump: regex:^xpu-\\d{8}$
  d.image = "docker.io/langflowai/langflow:1.13.1"; # image-bump: semver
  e.image = "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z";
}
"""

JUST = """\
create-fedora name="fedora-dev":
    distrobox create --image registry.fedoraproject.org/fedora:44 --name {{name}} # image-bump: alias:latest

create-ubuntu name="ubuntu-dev":
    # image-bump: ubuntu-lts
    distrobox create --image ubuntu:24.04 --name {{name}}

create-arch name="arch-dev":
    distrobox create --image docker.io/library/archlinux:latest --name {{name}}
"""


class Scan(unittest.TestCase):
    def test_nix(self):
        pins = scan_text("x.nix", NIX)
        self.assertEqual(
            [(p.image, p.tag, p.scheme, p.line) for p in pins],
            [
                ("ghcr.io/goauthentik/server", "2026.4.1", "calver", 2),
                ("agent0ai/agent-zero", "v1.20", "semver", 5),
                ("mintplexlabs/anythingllm", "1.18.0", "semver", 7),
                ("docker.io/yanwk/comfyui-boot", "xpu-20261012", r"regex:^xpu-\d{8}$", 8),
                ("docker.io/langflowai/langflow", "1.13.1", "semver", 9),
            ],
        )

    def test_just(self):
        pins = scan_text("dx.just", JUST)
        self.assertEqual(
            [(p.image, p.tag, p.scheme) for p in pins],
            [
                ("registry.fedoraproject.org/fedora", "44", "alias:latest"),
                ("ubuntu", "24.04", "ubuntu-lts"),
            ],
        )

    def test_annotation_needs_one_ref(self):
        with self.assertRaises(BumpError):
            scan_text("x.nix", 'x = "a"; # image-bump: semver\n')
        with self.assertRaises(BumpError):
            scan_text("x.nix", 'x = [ "a:1" "b:2" ]; # image-bump: semver\n')

    def test_dangling_annotation(self):
        with self.assertRaises(BumpError):
            scan_text("x.nix", "# image-bump: semver\n")


class Schemes(unittest.TestCase):
    def test_calver_skips_rc_and_aliases(self):
        found = candidates("calver", "2026.8.0", TAGS["ghcr.io/goauthentik/server"])
        self.assertEqual(found[0], "2026.8.5")
        self.assertFalse([t for t in found if "rc" in t or t.count(".") != 2])

    def test_semver_keeps_v_and_shape(self):
        found = candidates("semver", "v2.1", TAGS["agent0ai/agent-zero"])
        self.assertEqual(found[0], "v2.15")
        self.assertTrue(all(t.startswith("v") for t in found))

    def test_semver_skips_dev_and_arch_tags(self):
        found = candidates("semver", "1.12.0", TAGS["docker.io/langflowai/langflow"])
        self.assertEqual(found[0], "1.13.1")

    def test_regex(self):
        found = candidates(r"regex:^xpu-\d{8}$", "xpu-20261012", TAGS["docker.io/yanwk/comfyui-boot"])
        self.assertEqual(found[0], "xpu-20261102")
        with self.assertRaises(BumpError):
            candidates(r"regex:^cu\d+$", "xpu-20261012", [])

    def test_ubuntu_lts_only(self):
        found = candidates("ubuntu-lts", "24.04", TAGS["ubuntu"])
        self.assertEqual(found[:2], ["26.04", "24.04"])

    def test_unknown_scheme(self):
        with self.assertRaises(BumpError):
            candidates("newest", "1.0", ["1.1"])

    def test_non_version_tag(self):
        with self.assertRaises(BumpError):
            candidates("semver", "latest", ["1.0"])


class Plan(unittest.TestCase):
    def test_fixture_repo(self):
        pins = scan_text("x.nix", NIX) + scan_text("dx.just", JUST)
        reg = FakeRegistry(labels={"ghcr.io/goauthentik/server": {
            "org.opencontainers.image.source": "https://github.com/goauthentik/authentik"}})
        bumps, current, errors = plan(pins, reg, no_release)
        self.assertEqual(errors, [])
        self.assertEqual(current, ["docker.io/langflowai/langflow"])
        got = {b.image: (b.old, b.new, b.major) for b in bumps}
        self.assertEqual(got, {
            "ghcr.io/goauthentik/server": ("2026.4.1", "2026.8.5", False),
            "agent0ai/agent-zero": ("v1.20", "v2.15", True),
            "mintplexlabs/anythingllm": ("1.18.0", "1.19.0", False),
            "docker.io/yanwk/comfyui-boot": ("xpu-20261012", "xpu-20261102", False),
            # 46 is rawhide: alias:latest stops at 45.
            "registry.fedoraproject.org/fedora": ("44", "45", True),
            "ubuntu": ("24.04", "26.04", True),
        })
        auth = next(b for b in bumps if b.image == "ghcr.io/goauthentik/server")
        self.assertEqual(auth.notes, "https://github.com/goauthentik/authentik/releases")

    def test_current_pins_do_nothing(self):
        current_nix = NIX.replace("2026.4.1", "2026.8.5").replace("v1.20", "v2.15").replace(
            "1.18.0", "1.19.0").replace("xpu-20261012", "xpu-20261102")
        current_just = JUST.replace("fedora:44", "fedora:45").replace("24.04", "26.04")
        pins = scan_text("x.nix", current_nix) + scan_text("dx.just", current_just)
        reg = FakeRegistry()
        bumps, current, errors = plan(pins, reg, no_release)
        self.assertEqual((bumps, errors), ([], []))
        self.assertEqual(len(current), 7)
        # Manifest reads count against Docker Hub's limit: only alias:latest
        # makes them, here for fedora:latest and the rawhide 46.
        self.assertEqual(reg.digest_calls, 2)

    def test_registry_error_is_per_image(self):
        pins = [Pin("a", 0, "nope/nope", "1.0", "semver"), Pin("b", 0, "mintplexlabs/anythingllm", "1.18.0", "semver")]
        bumps, _, errors = plan(pins, FakeRegistry(), no_release)
        self.assertEqual([b.image for b in bumps], ["mintplexlabs/anythingllm"])
        self.assertEqual(len(errors), 1)

    def test_disagreeing_pins(self):
        pins = [Pin("a", 0, "ubuntu", "24.04", "ubuntu-lts"), Pin("b", 0, "ubuntu", "22.04", "ubuntu-lts")]
        bumps, _, errors = plan(pins, FakeRegistry(), no_release)
        self.assertEqual(bumps, [])
        self.assertIn("disagree", errors[0])


class Apply(unittest.TestCase):
    def test_rewrites_only_the_tag(self):
        files = {"x.nix": NIX, "dx.just": JUST}
        pins = scan_text("x.nix", NIX) + scan_text("dx.just", JUST)
        bumps, _, _ = plan(pins, FakeRegistry(), no_release)
        out = {}
        for b in bumps:
            out.update(apply(b, read=lambda p: out.get(p, files[p])))
        self.assertIn('image = "ghcr.io/goauthentik/server:2026.8.5"; # image-bump: calver\n', out["x.nix"])
        self.assertIn('"docker.io/yanwk/comfyui-boot:xpu-20261102"; # image-bump: regex:^xpu-\\d{8}$', out["x.nix"])
        self.assertIn("--image registry.fedoraproject.org/fedora:45 --name", out["dx.just"])
        self.assertIn("--image ubuntu:26.04 --name", out["dx.just"])
        self.assertIn("archlinux:latest", out["dx.just"])
        self.assertIn("minio:RELEASE.2025-09-07T16-13-09Z", out["x.nix"])
        self.assertEqual(len(out["x.nix"].splitlines()), len(NIX.splitlines()))

    def test_pr_text(self):
        b = Bump("agent0ai/agent-zero", "v1.20", "v2.15", "semver",
                 [Pin("x.nix", 5, "agent0ai/agent-zero", "v1.20", "semver")], True,
                 "https://github.com/agent0ai/agent-zero/releases/tag/v2.15")
        self.assertEqual(title(b), "chore: bump agent0ai/agent-zero from v1.20 to v2.15")
        text = body(b, "https://run")
        self.assertIn("Major version change", text)
        self.assertIn("releases/tag/v2.15", text)
        self.assertIn("`x.nix` line 6", text)
        self.assertEqual(slug("ghcr.io/goauthentik/server"), "ghcr-io-goauthentik-server")


class Notes(unittest.TestCase):
    def test_github_source(self):
        labels = {"org.opencontainers.image.source": "https://github.com/langflow-ai/langflow.git"}
        self.assertEqual(release_notes(labels, "1.14.0", lambda r, t: f"https://github.com/{r}/releases/tag/{t}"),
                         "https://github.com/langflow-ai/langflow/releases/tag/1.14.0")
        self.assertEqual(release_notes({}, "1.0", no_release), "")
        self.assertEqual(release_notes({"org.opencontainers.image.source": "https://gitlab.com/a/b"}, "1", no_release), "")


if __name__ == "__main__":
    unittest.main()
