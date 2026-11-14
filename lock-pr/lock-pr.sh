#!/usr/bin/env bash
# Puts $LOCK from the working tree on $BRANCH and makes sure an open PR with
# auto-merge carries it. Env: GH_TOKEN REPO LOCK BRANCH BASE_SHA RESET_TO TITLE BODY.
set -euo pipefail

if [ -n "$BASE_SHA" ]; then
  base=$BASE_SHA
  echo "extending $BRANCH at $base"
else
  # Not main's head now: a lock change merged meanwhile must not be reverted.
  base=$RESET_TO
fi

open_pr() {
  gh pr list --repo "$REPO" --head "$BRANCH" --base main --state open --json number --jq '.[0].number // empty'
}

# The blob sha of the lock at the base; empty when it has none yet. Any other
# lookup error stops here, before the branch is touched.
if ! current=$(gh api "repos/$REPO/contents/$LOCK?ref=$base" --jq .sha 2>"$RUNNER_TEMP/lookup.err"); then
  grep -q 'HTTP 404' "$RUNNER_TEMP/lookup.err" || { cat "$RUNNER_TEMP/lookup.err" >&2; exit 1; }
  current=""
fi
if [ "$current" = "$(git hash-object "$LOCK")" ]; then
  echo "$LOCK unchanged, nothing to do."
  # Main has this lock already: an open PR would land an older one. Checked
  # on main itself, which can be ahead of the base.
  pr=$(open_pr)
  if [ -z "$BASE_SHA" ] && [ -n "$pr" ]; then
    if ! on_main=$(gh api "repos/$REPO/contents/$LOCK?ref=main" --jq .sha 2>"$RUNNER_TEMP/lookup.err"); then
      grep -q 'HTTP 404' "$RUNNER_TEMP/lookup.err" || { cat "$RUNNER_TEMP/lookup.err" >&2; exit 1; }
      on_main=""
    fi
    if [ "$on_main" = "$(git hash-object "$LOCK")" ]; then
      gh pr close --repo "$REPO" "$pr" --comment "main has this lock already."
    fi
  fi
  echo "pr=" >> "$GITHUB_OUTPUT"
  exit 0
fi

commit_branch=$BRANCH
tmp_branch=""
cleanup() {
  [ -z "$tmp_branch" ] || gh api -X DELETE "repos/$REPO/git/refs/heads/$tmp_branch" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if [ -z "$BASE_SHA" ]; then
  # Stage the signed commit on a scratch ref so $BRANCH never points at $base:
  # resetting $BRANCH to main's ancestor queues an async zero-diff PR close.
  # no-link: GitHub closes zero-diff PRs by design and createCommitOnBranch requires a branch ref.
  tmp_branch="${BRANCH}-stage-${GITHUB_RUN_ID:-$$}-${GITHUB_RUN_ATTEMPT:-0}"
  commit_branch=$tmp_branch
  if ! gh api "repos/$REPO/git/refs" -f ref="refs/heads/$tmp_branch" -f sha="$base" >/dev/null 2>"$RUNNER_TEMP/ref.err"; then
    grep -q 'Reference already exists' "$RUNNER_TEMP/ref.err" || { cat "$RUNNER_TEMP/ref.err" >&2; exit 1; }
    gh api -X PATCH "repos/$REPO/git/refs/heads/$tmp_branch" -f sha="$base" -F force=true >/dev/null
  fi
fi

# createCommitOnBranch: GitHub signs the commit, which the rulesets require.
# The content goes through a file: a big lock in base64 can pass the 128 KiB
# limit for one argument.
base64 <"$LOCK" | tr -d '\n' >"$RUNNER_TEMP/lock.b64" # GNU and BSD base64 alike
commit_oid=$(jq -n \
  --arg repo "$REPO" --arg branch "$commit_branch" --arg sha "$base" \
  --arg path "$LOCK" --rawfile content "$RUNNER_TEMP/lock.b64" --arg headline "$TITLE" \
  --arg query 'mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }' \
  '{query: $query, variables: {input: {
      branch: {repositoryNameWithOwner: $repo, branchName: $branch},
      message: {headline: $headline},
      expectedHeadOid: $sha,
      fileChanges: {additions: [{path: $path, contents: $content}]}
  }}}' | gh api graphql --input - --jq '.data.createCommitOnBranch.commit.oid')
if [ -z "$commit_oid" ] || [ "$commit_oid" = "null" ]; then
  echo "::error::createCommitOnBranch returned no commit oid" >&2
  exit 1
fi

if [ -z "$BASE_SHA" ]; then
  if gh api "repos/$REPO/git/ref/heads/$BRANCH" >/dev/null 2>&1; then
    if ! gh api -X PATCH "repos/$REPO/git/refs/heads/$BRANCH" -f sha="$commit_oid" -F force=true >/dev/null 2>"$RUNNER_TEMP/ref.err"; then
      grep -q 'Reference does not exist' "$RUNNER_TEMP/ref.err" || { cat "$RUNNER_TEMP/ref.err" >&2; exit 1; }
      gh api "repos/$REPO/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$commit_oid" >/dev/null
    fi
  elif ! gh api "repos/$REPO/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$commit_oid" >/dev/null 2>"$RUNNER_TEMP/ref.err"; then
    grep -q 'Reference already exists' "$RUNNER_TEMP/ref.err" || { cat "$RUNNER_TEMP/ref.err" >&2; exit 1; }
    gh api -X PATCH "repos/$REPO/git/refs/heads/$BRANCH" -f sha="$commit_oid" -F force=true >/dev/null
  fi
  cleanup
  tmp_branch=""
fi

ensure_open_pr() {
  pr=$(open_pr)
  if [ -z "$pr" ]; then
    # A concurrent run can open it first; then use that one.
    gh pr create --repo "$REPO" --head "$BRANCH" --base main --title "$TITLE" --body "$BODY" || true
    pr=$(open_pr)
    [ -n "$pr" ] || { echo "::error::no open PR for $BRANCH" >&2; exit 1; }
  fi
}

# Re-open once if an existing PR closes concurrently before recording GITHUB_OUTPUT.
# no-link: concurrent PR merge/close during branch update is normal GitHub state, not an upstream bug.
for attempt in 1 2; do
  ensure_open_pr
  gh pr merge --repo "$REPO" "$pr" --auto --squash || true
  status=$(gh pr view --repo "$REPO" "$pr" --json number,state,autoMergeRequest \
    --jq 'if .state == "MERGED" or (.state == "OPEN" and .autoMergeRequest != null) then "ok:lock PR #\(.number): \(.state), auto-merge armed" elif .state == "CLOSED" then "closed" else "err:lock PR #\(.number) has no auto-merge (state: \(.state))" end')
  case "$status" in
    ok:*)
      echo "${status#ok:}"
      echo "pr=$pr" >> "$GITHUB_OUTPUT"
      exit 0
      ;;
    closed)
      [ "$attempt" -eq 1 ] || { echo "::error::lock PR #$pr closed repeatedly" >&2; exit 1; }
      echo "lock PR #$pr closed concurrently, opening a new one"
      ;;
    err:*)
      echo "::error::${status#err:}" >&2
      exit 1
      ;;
  esac
done
