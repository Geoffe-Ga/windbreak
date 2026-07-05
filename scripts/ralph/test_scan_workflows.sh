#!/usr/bin/env bash
# scripts/ralph/test_scan_workflows.sh
#
# Pure static-analysis tests over .github/workflows/ — no `gh` stub needed.
# These pin two fixes the maintenance-scan pipeline needs:
#
#  1. Every producer wrapper (a scan-*.yml that calls the reusable
#     _claude-scan.yml) must itself grant `id-token: write`, `issues: write`,
#     and `contents: read` at the top level. A reusable workflow can never
#     receive MORE GITHUB_TOKEN permission than its caller grants — the
#     reusable core requests `id-token: write` (never present in the default
#     token), so a caller that omits it fails the run at startup with NO logs
#     (the observed `startup_failure`). _claude-scan.yml already declares the
#     permissions it needs; the bug is that its CALLERS don't re-grant them.
#
#  2. _claude-scan.yml itself must stop referencing this repo's nonexistent
#     frontend/ tree and backend/requirements* layout (hedgekit keeps
#     requirements at the ROOT and is Python-only — no npm/Node step at all).
#
# Both fixes don't exist yet, so this suite fails RED now.
#
# Run:  bash scripts/ralph/test_scan_workflows.sh
set -euo pipefail

RALPH_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$RALPH_DIR/../.." && pwd)"
WORKFLOWS_DIR="$ROOT_DIR/.github/workflows"
CLAUDE_SCAN="$WORKFLOWS_DIR/_claude-scan.yml"

PASS=0
FAIL=0
check() { # check <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then
    PASS=$((PASS + 1)); printf '  ok  - %s\n' "$1"
  else
    FAIL=$((FAIL + 1)); printf 'FAIL  - %s (expected [%s], got [%s])\n' "$1" "$2" "$3"
  fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# --- 1) Every producer wrapper must re-grant the three permissions ------------
# Discovered dynamically (never hardcoded) via the literal `uses:` line every
# thin wrapper has in common.
mapfile -t WRAPPERS < <(
  grep -lF 'uses: ./.github/workflows/_claude-scan.yml' "$WORKFLOWS_DIR"/scan-*.yml 2>/dev/null | sort
)
if [[ ${#WRAPPERS[@]} -gt 0 ]]; then wrappers_found=yes; else wrappers_found=no; fi
check "at least one producer wrapper discovered (sanity)" "yes" "$wrappers_found"

# Extract a workflow's top-level `permissions:` block: the line itself plus
# every immediately-following indented line, stopping at the next line that
# starts at column 0 (the next top-level key). Deliberately ignores any
# job-level `jobs.<job>.permissions:` block, which is always indented.
extract_top_level_permissions() { # <file>
  awk '
    /^permissions:/ { found=1; print; next }
    found && /^[[:space:]]/ { print; next }
    { found=0 }
  ' "$1"
}

wrapper_grants_required_perms() { # <file> — 0 if id-token+issues+contents all granted
  local blob
  blob="$(extract_top_level_permissions "$1")"
  grep -qE 'id-token:[[:space:]]*write' <<<"$blob" || return 1
  grep -qE 'issues:[[:space:]]*write' <<<"$blob" || return 1
  grep -qE 'contents:[[:space:]]*read' <<<"$blob" || return 1
}

for wf in "${WRAPPERS[@]}"; do
  wf_name="$(basename "$wf")"
  if wrapper_grants_required_perms "$wf"; then status=granted; else status=missing; fi
  check "producer wrapper $wf_name grants id-token:write + issues:write + contents:read" \
    "granted" "$status"
done

# --- 2) _claude-scan.yml must not reference this repo's nonexistent layout ----
frontend_hits=$(grep -cF -- 'frontend/' "$CLAUDE_SCAN" 2>/dev/null) || true
check "_claude-scan.yml does not reference the nonexistent frontend/ path" \
  "0" "$frontend_hits"

backend_req_hits=$(grep -cF -- 'backend/requirements' "$CLAUDE_SCAN" 2>/dev/null) || true
check "_claude-scan.yml does not reference backend/requirements (root-only repo)" \
  "0" "$backend_req_hits"

# --- 3) _claude-scan.yml must install from the ROOT requirements files --------
root_txt_hits=$(grep -cF -- '-r requirements.txt' "$CLAUDE_SCAN" 2>/dev/null) || true
check "_claude-scan.yml installs from root requirements.txt" "1" "$root_txt_hits"

root_dev_hits=$(grep -cF -- '-r requirements-dev.txt' "$CLAUDE_SCAN" 2>/dev/null) || true
check "_claude-scan.yml installs from root requirements-dev.txt" "1" "$root_dev_hits"

backend_req_hits_again=$(grep -cF -- 'backend/requirements' "$CLAUDE_SCAN" 2>/dev/null) || true
check "_claude-scan.yml install step never references backend/requirements" \
  "0" "$backend_req_hits_again"

# --- 4) _claude-scan.yml must have no npm ci / no Node setup (Python-only) ----
npm_hits=$(grep -cF -- 'npm ci' "$CLAUDE_SCAN" 2>/dev/null) || true
check "_claude-scan.yml has no npm ci step" "0" "$npm_hits"

setup_node_hits=$(grep -c -i -F -- 'setup-node' "$CLAUDE_SCAN" 2>/dev/null) || true
check "_claude-scan.yml has no Set up Node / setup-node step" "0" "$setup_node_hits"

# --- 5) actionlint, when available, must pass over THIS change's blast radius --
# Bounded/graceful: this repo's CI may not have actionlint on PATH, and that
# must never fail the suite — only its ABSENCE is skipped, never its findings.
#
# Scoped deliberately to the workflows this change touches — hopper.yml, the
# reusable core _claude-scan.yml, and the scan-*.yml wrappers. ci.yml and
# metrics.yml carry PRE-EXISTING, out-of-scope actionlint/shellcheck findings
# that are being tracked separately, so linting all of workflows/ here would
# fail the suite on unrelated debt rather than on this change.
if command -v actionlint >/dev/null 2>&1; then
  rc=0
  actionlint \
    "$WORKFLOWS_DIR/hopper.yml" \
    "$WORKFLOWS_DIR/_claude-scan.yml" \
    "$WORKFLOWS_DIR"/scan-*.yml \
    > "$WORK/actionlint.out" 2>&1 || rc=$?
  if [[ "$rc" -eq 0 ]]; then al_status=clean; else al_status=dirty; fi
  check "actionlint passes over this change's workflow blast radius (hopper + _claude-scan + scan-*)" \
    "clean" "$al_status"
  if [[ "$rc" -ne 0 ]]; then
    echo "  actionlint findings:"
    sed 's/^/    /' "$WORK/actionlint.out"
  fi
else
  echo "  skip - actionlint not on PATH; static workflow lint skipped"
fi

echo
echo "scan-workflows tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
