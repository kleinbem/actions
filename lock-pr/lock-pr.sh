#!/usr/bin/env bash
# Puts $LOCK from the working tree on $BRANCH and makes sure an open PR with
# auto-merge carries it. Env: GH_TOKEN REPO LOCK BRANCH BASE_SHA TITLE BODY.
set -euo pipefail

if [ -n "$BASE_SHA" ]; then
  base=$BASE_SHA
  echo "extending $BRANCH at $base"
else
  base=$(gh api "repos/$REPO/git/ref/heads/main" --jq .object.sha)
fi

# Same content as the base: nothing to commit.
current=$(gh api "repos/$REPO/contents/$LOCK?ref=$base" --jq .sha 2>/dev/null || true)
if [ "$current" = "$(git hash-object "$LOCK")" ]; then
  echo "$LOCK unchanged, nothing to do."
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
jq -n \
  --arg repo "$REPO" --arg branch "$BRANCH" --arg sha "$base" \
  --arg path "$LOCK" --arg content "$(base64 -w0 "$LOCK")" --arg headline "$TITLE" \
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
pr=$(gh pr list --repo "$REPO" --head "$BRANCH" --base main --state open --json number --jq '.[0].number // empty')
if [ -z "$pr" ]; then
  url=$(gh pr create --repo "$REPO" --head "$BRANCH" --base main --title "$TITLE" --body "$BODY")
  pr=${url##*/}
fi
echo "pr=$pr" >> "$GITHUB_OUTPUT"

# Fails when auto-merge is already armed; the check below covers the rest.
gh pr merge --repo "$REPO" "$pr" --auto --squash || true

gh pr view --repo "$REPO" "$pr" --json number,state,autoMergeRequest \
  --jq 'if .state == "MERGED" or (.state == "OPEN" and .autoMergeRequest != null) then "lock PR #\(.number): \(.state), auto-merge armed" else error("lock PR #\(.number) has no auto-merge (state: \(.state))") end'
