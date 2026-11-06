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
  # Main has this lock already: an open PR would land an older one.
  pr=$(open_pr)
  if [ -z "$BASE_SHA" ] && [ -n "$pr" ]; then
    gh pr close --repo "$REPO" "$pr" --comment "main has this lock already."
  fi
  echo "pr=" >> "$GITHUB_OUTPUT"
  exit 0
fi

if [ -z "$BASE_SHA" ]; then
  if gh api "repos/$REPO/git/ref/heads/$BRANCH" >/dev/null 2>&1; then
    gh api -X PATCH "repos/$REPO/git/refs/heads/$BRANCH" -f sha="$base" -F force=true >/dev/null
  else
    gh api "repos/$REPO/git/refs" -f ref="refs/heads/$BRANCH" -f sha="$base" >/dev/null
  fi
fi

# createCommitOnBranch: GitHub signs the commit, which the rulesets require.
# The content goes through a file: a big lock in base64 can pass the 128 KiB
# limit for one argument.
base64 <"$LOCK" | tr -d '\n' >"$RUNNER_TEMP/lock.b64" # GNU and BSD base64 alike
jq -n \
  --arg repo "$REPO" --arg branch "$BRANCH" --arg sha "$base" \
  --arg path "$LOCK" --rawfile content "$RUNNER_TEMP/lock.b64" --arg headline "$TITLE" \
  --arg query 'mutation($input: CreateCommitOnBranchInput!) { createCommitOnBranch(input: $input) { commit { oid } } }' \
  '{query: $query, variables: {input: {
      branch: {repositoryNameWithOwner: $repo, branchName: $branch},
      message: {headline: $headline},
      expectedHeadOid: $sha,
      fileChanges: {additions: [{path: $path, contents: $content}]}
  }}}' | gh api graphql --input - >/dev/null

# Only an open PR counts. A reset can close the old one (zero diff for an
# instant), and a closed PR can't always be reopened once its branch moved,
# so open a new one instead.
pr=$(open_pr)
if [ -z "$pr" ]; then
  # A concurrent run can open it first; then use that one.
  gh pr create --repo "$REPO" --head "$BRANCH" --base main --title "$TITLE" --body "$BODY" || true
  pr=$(open_pr)
  [ -n "$pr" ] || { echo "::error::no open PR for $BRANCH" >&2; exit 1; }
fi
echo "pr=$pr" >> "$GITHUB_OUTPUT"

# Fails when auto-merge is already armed; the check below covers the rest.
gh pr merge --repo "$REPO" "$pr" --auto --squash || true

gh pr view --repo "$REPO" "$pr" --json number,state,autoMergeRequest \
  --jq 'if .state == "MERGED" or (.state == "OPEN" and .autoMergeRequest != null) then "lock PR #\(.number): \(.state), auto-merge armed" else error("lock PR #\(.number) has no auto-merge (state: \(.state))") end'
