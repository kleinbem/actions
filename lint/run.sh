#!/usr/bin/env bash
# Fleet lint: runs each check whose file types exist in the repo, reports all
# of them, and fails if any failed. Tools come from the caller's PATH.
#   LINT_CONFIG  directory with the default configs (yamllint.yaml, ...)
#   LINT_BASE / LINT_HEAD  commit range for gitleaks; empty scans the tree
#   LINT_SKIP    space-separated check names to skip (each needs a reason in the caller)
set -uo pipefail

cfg=${LINT_CONFIG:?LINT_CONFIG must point at the default configs}
skip=" ${LINT_SKIP:-} "
failed=()
ran=()

# Tracked files only: build outputs and vendored checkouts stay out.
mapfile -t files < <(git ls-files)
match() { printf '%s\n' "${files[@]}" | grep -E "$1" || true; }

check() {
  local name=$1; shift
  if [[ $skip == *" $name "* ]]; then echo "::notice::$name skipped by LINT_SKIP"; return; fi
  echo "::group::$name"
  if "$@"; then echo "::endgroup::"; ran+=("$name"); else
    echo "::endgroup::"; echo "::error::$name failed"; failed+=("$name"); fi
}

# --- secrets ---
gitleaks_run() {
  local conf=()
  [ -f .gitleaks.toml ] || conf=(--config "$cfg/gitleaks.toml")
  if [ -n "${LINT_BASE:-}" ] && [ -n "${LINT_HEAD:-}" ]; then
    gitleaks git --redact --no-banner "${conf[@]}" --log-opts="${LINT_BASE}..${LINT_HEAD}" .
  else
    gitleaks dir --redact --no-banner "${conf[@]}" .
  fi
}
check gitleaks gitleaks_run

# --- GitHub Actions ---
mapfile -t wfs < <(match '^\.github/workflows/[^/]+\.ya?ml$')
mapfile -t acts < <(match '(^|/)action\.ya?ml$')
if [ ${#wfs[@]} -gt 0 ]; then
  check actionlint actionlint -shellcheck=shellcheck "${wfs[@]}"
fi
if [ ${#acts[@]} -gt 0 ]; then
  check action-shellcheck python3 "$cfg/action-shellcheck.py" "${acts[@]}"
fi
if [ ${#wfs[@]} -gt 0 ] || [ ${#acts[@]} -gt 0 ] || [ -f .github/dependabot.yml ]; then
  check zizmor zizmor --min-severity medium --format plain .
fi

# --- shell ---
mapfile -t shs < <(
  for f in "${files[@]}"; do
    [ -f "$f" ] || continue
    case $f in
      *.sh|*.bash) echo "$f" ;;
      *.*) ;;
      *) head -c 64 "$f" 2>/dev/null | head -1 | grep -qE '^#!.*\b(ba)?sh\b' && echo "$f" ;;
    esac
  done)
if [ ${#shs[@]} -gt 0 ]; then
  # SC1091: sourced files are often generated or live outside the repo.
  check shellcheck shellcheck -e SC1091 "${shs[@]}"
fi

# --- YAML ---
mapfile -t ymls < <(match '\.ya?ml$')
if [ ${#ymls[@]} -gt 0 ]; then
  ycfg=()
  [ -f .yamllint ] || [ -f .yamllint.yaml ] || [ -f .yamllint.yml ] || ycfg=(-c "$cfg/yamllint.yaml")
  check yamllint yamllint -s "${ycfg[@]}" "${ymls[@]}"
fi

# --- Python ---
mapfile -t pys < <(match '\.py$')
if [ ${#pys[@]} -gt 0 ]; then
  # Fixed rule set, not the repo's own (that stays the repo's pre-commit).
  # S101 assert, S603/S607 subprocess with an argument list: not injection.
  check ruff ruff check --no-cache --isolated --select E4,E7,E9,F,S \
    --ignore S101,S603,S607 "${pys[@]}"
fi

# --- Markdown ---
mapfile -t mds < <(match '\.md$')
if [ ${#mds[@]} -gt 0 ]; then
  mcfg=()
  ls .markdownlint* >/dev/null 2>&1 || mcfg=(--config "$cfg/markdownlint.yaml")
  check markdownlint markdownlint-cli2 "${mcfg[@]}" "${mds[@]}"
fi

# --- Dockerfiles ---
mapfile -t dockers < <(match '(^|/)(Dockerfile|Containerfile)[^/]*$|\.dockerfile$')
if [ ${#dockers[@]} -gt 0 ]; then
  hc=()
  [ -f .hadolint.yaml ] || hc=(--config "$cfg/hadolint.yaml")
  check hadolint hadolint "${hc[@]}" "${dockers[@]}"
fi

# --- OpenTofu ---
mapfile -t tfdirs < <(match '\.tf$' | xargs -r -n1 dirname | sort -u)
tofu_run() {
  local d rc=0
  tofu fmt -check -recursive -diff . || rc=1
  for d in "${tfdirs[@]}"; do
    (cd "$d" && tofu init -backend=false -input=false -no-color >/dev/null \
      && tofu validate -no-color) || { echo "tofu validate failed in $d"; rc=1; }
  done
  return $rc
}
tflint_run() {
  local d rc=0
  for d in "${tfdirs[@]}"; do
    local c=()
    [ -f "$d/.tflint.hcl" ] || c=(--config "$cfg/tflint.hcl")
    (cd "$d" && tflint --init "${c[@]}" >/dev/null && tflint --no-color "${c[@]}") || rc=1
  done
  return $rc
}
if [ ${#tfdirs[@]} -gt 0 ]; then
  check tofu tofu_run
  check tflint tflint_run
  tic=()
  [ -f .trivyignore ] || [ -f .trivyignore.yaml ] || tic=(--ignorefile "$cfg/trivyignore.yaml")
  check trivy trivy config --quiet --severity HIGH,CRITICAL --exit-code 1 "${tic[@]}" .
fi

# --- dependencies ---
if [ -n "$(match '(^|/)(Cargo\.lock|package-lock\.json|pnpm-lock\.yaml|yarn\.lock|uv\.lock|poetry\.lock|requirements[^/]*\.txt|go\.mod)$')" ]; then
  check osv-scanner osv-scanner scan source -r .
fi

echo
echo "passed: ${ran[*]:-none}"
if [ ${#failed[@]} -gt 0 ]; then
  echo "failed: ${failed[*]}"
  exit 1
fi
