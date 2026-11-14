import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

SCRIPT = Path(__file__).resolve().parent / "lock-pr.sh"

FAKE_GH = textwrap.dedent("""\
    #!/usr/bin/env python3
    import json
    import os
    from pathlib import Path
    import subprocess
    import sys

    state_file = Path(os.environ["FAKE_GH_STATE"])
    state = json.loads(state_file.read_text())
    args = sys.argv[1:]
    state["calls"].append(args)


    def save() -> None:
        state_file.write_text(json.dumps(state))


    def emit(payload: object, jq_expr: str | None) -> None:
        raw = json.dumps(payload)
        if jq_expr is None:
            print(raw)
            return
        proc = subprocess.run(
            ["jq", "-r", jq_expr],
            input=raw,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.stdout:
            sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            save()
            sys.exit(proc.returncode)


    jq_expr = None
    method = None
    fields: dict[str, str] = {}
    clean: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--jq":
            jq_expr = args[i + 1]
            i += 2
        elif a == "-X":
            method = args[i + 1]
            i += 2
        elif a in ("-f", "-F"):
            k, v = args[i + 1].split("=", 1)
            fields[k] = v
            i += 2
        elif a == "--input":
            i += 2
        else:
            clean.append(a)
            i += 1

    if clean[:2] == ["api", "graphql"]:
        body = json.loads(sys.stdin.read())
        inp = body["variables"]["input"]
        branch = inp["branch"]["branchName"]
        expected = inp["expectedHeadOid"]
        if state.get("fail_graphql"):
            save()
            sys.stderr.write("GraphQL mutation failed\\n")
            sys.exit(1)
        actual = state["refs"].get(f"refs/heads/{branch}")
        if actual != expected:
            save()
            sys.stderr.write(f"expected {expected}, got {actual}\\n")
            sys.exit(1)
        oid = state["next_commit_oid"]
        state["refs"][f"refs/heads/{branch}"] = oid
        state["ref_history"].setdefault(branch, []).append(oid)
        save()
        emit({"data": {"createCommitOnBranch": {"commit": {"oid": oid}}}}, jq_expr)
        sys.exit(0)

    if clean[0] == "api":
        endpoint = clean[1]
        if "/contents/" in endpoint:
            ref = endpoint.split("?ref=", 1)[1]
            sha = state["contents"].get(ref)
            save()
            if sha is None:
                sys.stderr.write("gh: Not Found (HTTP 404)\\n")
                sys.exit(1)
            emit({"sha": sha}, jq_expr)
            sys.exit(0)
        if "/git/ref/heads/" in endpoint:
            branch = endpoint.split("/git/ref/heads/", 1)[1]
            sha = state["refs"].get(f"refs/heads/{branch}")
            save()
            if sha is None:
                sys.stderr.write("gh: Not Found (HTTP 404)\\n")
                sys.exit(1)
            emit({"object": {"sha": sha}}, jq_expr)
            sys.exit(0)
        if endpoint.endswith("/git/refs") and method is None:
            ref = fields["ref"]
            if ref in state["refs"]:
                save()
                sys.stderr.write("gh: Reference already exists (HTTP 422)\\n")
                sys.exit(1)
            state["refs"][ref] = fields["sha"]
            if ref.startswith("refs/heads/"):
                state["ref_history"].setdefault(ref.removeprefix("refs/heads/"), []).append(fields["sha"])
            save()
            emit({"ref": ref}, jq_expr)
            sys.exit(0)
        if "/git/refs/heads/" in endpoint and method == "PATCH":
            branch = endpoint.split("/git/refs/heads/", 1)[1]
            ref = f"refs/heads/{branch}"
            if ref not in state["refs"]:
                save()
                sys.stderr.write("gh: Reference does not exist (HTTP 422)\\n")
                sys.exit(1)
            state["refs"][ref] = fields["sha"]
            state["ref_history"].setdefault(branch, []).append(fields["sha"])
            save()
            emit({"ref": ref}, jq_expr)
            sys.exit(0)
        if "/git/refs/heads/" in endpoint and method == "DELETE":
            branch = endpoint.split("/git/refs/heads/", 1)[1]
            state["refs"].pop(f"refs/heads/{branch}", None)
            save()
            sys.exit(0)

    if clean[:2] == ["pr", "list"]:
        open_prs = [p for p in state["prs"] if p["state"] == "OPEN"]
        save()
        emit(open_prs, jq_expr)
        sys.exit(0)

    if clean[:2] == ["pr", "create"]:
        num = state["next_pr"]
        state["next_pr"] += 1
        state["prs"].append({"number": num, "state": "OPEN", "autoMergeRequest": None})
        save()
        print(f"https://github.com/kleinbem/test/pull/{num}")
        sys.exit(0)

    if clean[:2] == ["pr", "close"]:
        pr_num = int(clean[4])
        for p in state["prs"]:
            if p["number"] == pr_num:
                p["state"] = "CLOSED"
        save()
        sys.exit(0)

    if clean[:2] == ["pr", "merge"]:
        pr_num = int(clean[4])
        if state.get("close_on_first_merge") == pr_num:
            state["close_on_first_merge"] = None
            for p in state["prs"]:
                if p["number"] == pr_num:
                    p["state"] = "CLOSED"
                    p["autoMergeRequest"] = None
            save()
            sys.stderr.write(f"Pull request #{pr_num} is closed\\n")
            sys.exit(1)
        for p in state["prs"]:
            if p["number"] == pr_num and p["state"] == "OPEN":
                p["autoMergeRequest"] = {"enabledAt": "2026-11-14T00:00:00Z"}
                save()
                sys.exit(0)
        save()
        sys.stderr.write("cannot arm auto-merge\\n")
        sys.exit(1)

    if clean[:2] == ["pr", "view"]:
        pr_num = int(clean[4])
        for p in state["prs"]:
            if p["number"] == pr_num:
                save()
                emit(p, jq_expr)
                sys.exit(0)

    save()
    sys.stderr.write(f"unhandled gh args: {args!r}\\n")
    sys.exit(2)
""")


class LockPrTest(unittest.TestCase):
    def run_lock_pr(
        self,
        initial_state: dict,
        *,
        lock_text: str = '{"nodes":{}}\n',
        base_sha: str = "",
        reset_to: str = "base000",
    ) -> tuple[subprocess.CompletedProcess[str], dict, str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "work"
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            (repo / "flake.lock").write_text(lock_text)

            bin_dir = root / "bin"
            bin_dir.mkdir()
            gh_bin = bin_dir / "gh"
            gh_bin.write_text(FAKE_GH.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
            gh_bin.chmod(0o755)

            state_path = root / "state.json"
            state = {
                "contents": {},
                "refs": {},
                "ref_history": {},
                "prs": [],
                "next_pr": 900,
                "next_commit_oid": "signed999",
                "calls": [],
                **initial_state,
            }
            state_path.write_text(json.dumps(state))
            out_path = root / "github_output"
            out_path.write_text("")
            runner_temp = root / "runner_temp"
            runner_temp.mkdir()

            env = {
                **os.environ,
                "PATH": f"{bin_dir}:{os.environ['PATH']}",
                "FAKE_GH_STATE": str(state_path),
                "RUNNER_TEMP": str(runner_temp),
                "GITHUB_OUTPUT": str(out_path),
                "GITHUB_RUN_ID": "42",
                "GITHUB_RUN_ATTEMPT": "1",
                "GH_TOKEN": "test-token",
                "REPO": "kleinbem/test",
                "LOCK": "flake.lock",
                "BRANCH": "update-flake-lock",
                "BASE_SHA": base_sha,
                "RESET_TO": reset_to,
                "TITLE": "chore: update flake.lock",
                "BODY": "Automated lockfile update.",
            }
            proc = subprocess.run(
                ["bash", str(SCRIPT)],
                cwd=repo,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )
            return proc, json.loads(state_path.read_text()), out_path.read_text()

    def test_existing_branch_never_touches_zero_diff_base(self) -> None:
        proc, state, gh_out = self.run_lock_pr(
            {
                "contents": {"base000": "oldblob"},
                "refs": {"refs/heads/update-flake-lock": "oldlock111"},
                "prs": [{"number": 800, "state": "OPEN", "autoMergeRequest": None}],
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # $BRANCH moves directly from oldlock111 -> signed999 without ever hitting base000.
        self.assertEqual(state["ref_history"]["update-flake-lock"], ["signed999"])
        self.assertEqual(state["refs"], {"refs/heads/update-flake-lock": "signed999"})
        self.assertEqual(gh_out, "pr=800\n")

    def test_creates_branch_and_pr_when_absent(self) -> None:
        proc, state, gh_out = self.run_lock_pr({"contents": {}})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(state["refs"], {"refs/heads/update-flake-lock": "signed999"})
        self.assertEqual(gh_out, "pr=900\n")

    def test_recovers_when_existing_pr_closes_concurrently(self) -> None:
        proc, state, gh_out = self.run_lock_pr(
            {
                "contents": {"base000": "oldblob"},
                "refs": {"refs/heads/update-flake-lock": "oldlock111"},
                "prs": [{"number": 800, "state": "OPEN", "autoMergeRequest": None}],
                "close_on_first_merge": 800,
                "next_pr": 802,
            }
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        # Only the newly opened PR is emitted to GITHUB_OUTPUT.
        self.assertEqual(gh_out, "pr=802\n")
        pr802 = next(p for p in state["prs"] if p["number"] == 802)
        self.assertIsNotNone(pr802["autoMergeRequest"])

    def test_extends_existing_branch_when_base_sha_set(self) -> None:
        proc, state, gh_out = self.run_lock_pr(
            {
                "contents": {"head777": "oldblob"},
                "refs": {"refs/heads/update-flake-lock": "head777"},
                "prs": [{"number": 800, "state": "OPEN", "autoMergeRequest": {"enabledAt": "now"}}],
            },
            base_sha="head777",
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(list(state["ref_history"].keys()), ["update-flake-lock"])
        self.assertEqual(gh_out, "pr=800\n")

    def test_cleans_stage_branch_when_graphql_fails(self) -> None:
        proc, state, gh_out = self.run_lock_pr(
            {
                "contents": {"base000": "oldblob"},
                "refs": {"refs/heads/update-flake-lock": "oldlock111"},
                "fail_graphql": True,
            }
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(state["refs"], {"refs/heads/update-flake-lock": "oldlock111"})
        self.assertEqual(gh_out, "")


if __name__ == "__main__":
    unittest.main()
