#!/usr/bin/env bash
# scripts/ralph/test_deslop_workflow.sh
#
# Pure static-analysis tests over .github/workflows/deslop.yml and its area
# registry .github/deslop-areas.json — no `gh` stub needed.
#
# hedgekit is a root-only Python repo: there is no frontend/ tree and no
# backend/requirements* layout (requirements live at the repo ROOT, and there
# is no Node/npm step anywhere). deslop.yml was bootstrapped from a donor
# project that DOES have a frontend/backend split; issue #120 retargeted it to
# this repo's real layout. This suite is the regression guard that fails if
# any of that donor layout creeps back in:
#
#  1. deslop.yml must NOT reference the nonexistent frontend/ tree (frontend/
#     .nvmrc, `cd frontend`, "frontend-core" area text) or the nonexistent
#     backend/requirements* layout, must NOT run `npm ci` or set up Node, and
#     must NOT gate its install/setup steps behind `matrix.area.stack` (which
#     made the broken steps merely look "skippable"). The correct shape is a
#     single unconditional Python setup + root `-r requirements.txt` install,
#     no Node/npm anywhere, and area text that matches this repo's real layout.
#
#  2. .github/deslop-areas.json — the area registry deslop.yml's matrix reads
#     from — must ship only paths that exist on disk (no "backend/src",
#     "frontend/src"). A registry entry whose `paths` don't exist is a silent
#     no-op: the scan "runs" but reads nothing.
#
#  3. ralph-fleet-tests.yml (the CI job that runs THIS suite) must trigger on
#     deslop.yml and deslop-areas.json and must invoke this file, so the guard
#     is actually wired into CI.
#
# Run:  bash scripts/ralph/test_deslop_workflow.sh
set -euo pipefail

RALPH_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$RALPH_DIR/../.." && pwd)"
WORKFLOWS_DIR="$ROOT_DIR/.github/workflows"
DESLOP="$WORKFLOWS_DIR/deslop.yml"
AREAS="$ROOT_DIR/.github/deslop-areas.json"
FLEET_TESTS_WF="$WORKFLOWS_DIR/ralph-fleet-tests.yml"

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

# --- 1) deslop.yml must purge the donor project's frontend/backend layout ----
frontend_hits=$(grep -c -i -F -- 'frontend' "$DESLOP" 2>/dev/null) || true
check "deslop.yml does not reference the nonexistent frontend/ tree or area" \
  "0" "$frontend_hits"

backend_dir_hits=$(grep -cF -- 'backend/' "$DESLOP" 2>/dev/null) || true
check "deslop.yml does not reference the nonexistent backend/ path prefix" \
  "0" "$backend_dir_hits"

matrix_stack_hits=$(grep -cF -- 'matrix.area.stack' "$DESLOP" 2>/dev/null) || true
check "deslop.yml has no matrix.area.stack conditional (no step is skippable)" \
  "0" "$matrix_stack_hits"

npm_ci_hits=$(grep -cF -- 'npm ci' "$DESLOP" 2>/dev/null) || true
check "deslop.yml has no npm ci step" "0" "$npm_ci_hits"

setup_node_hits=$(grep -c -i -F -- 'setup-node' "$DESLOP" 2>/dev/null) || true
check "deslop.yml has no Set up Node / setup-node step" "0" "$setup_node_hits"

# --- 2) deslop.yml must install from the ROOT requirements files, unconditionally
root_txt_hits=$(grep -cF -- '-r requirements.txt' "$DESLOP" 2>/dev/null) || true
check "deslop.yml installs from root requirements.txt" "1" "$root_txt_hits"

root_dev_hits=$(grep -cF -- '-r requirements-dev.txt' "$DESLOP" 2>/dev/null) || true
check "deslop.yml installs from root requirements-dev.txt" "1" "$root_dev_hits"

setup_python_hits=$(grep -c -i -F -- 'setup-python' "$DESLOP" 2>/dev/null) || true
if [[ "$setup_python_hits" -ge 1 ]]; then setup_python_present=yes; else setup_python_present=no; fi
check "deslop.yml sets up Python (unconditionally, root-only repo)" \
  "yes" "$setup_python_present"

# --- 3) permissions regression pin — this one is ALREADY correct; keep it so --
# Extract a workflow's top-level `permissions:` block: the line itself plus
# every immediately-following indented line, stopping at the next line that
# starts at column 0 (the next top-level key). Deliberately ignores any
# job-level `jobs.<job>.permissions:` block, which is always indented.
# (Verbatim from test_scan_workflows.sh.)
extract_top_level_permissions() { # <file>
  awk '
    /^permissions:/ { found=1; print; next }
    found && /^[[:space:]]/ { print; next }
    { found=0 }
  ' "$1"
}

deslop_grants_required_perms() { # <file> — 0 if id-token+issues+contents all granted
  local blob
  blob="$(extract_top_level_permissions "$1")"
  grep -qE 'id-token:[[:space:]]*write' <<<"$blob" || return 1
  grep -qE 'issues:[[:space:]]*write' <<<"$blob" || return 1
  grep -qE 'contents:[[:space:]]*read' <<<"$blob" || return 1
}

if deslop_grants_required_perms "$DESLOP"; then perms_status=granted; else perms_status=missing; fi
check "deslop.yml top-level permissions grant id-token:write + issues:write + contents:read" \
  "granted" "$perms_status"

# --- 4) deslop-areas.json must be valid, non-empty, well-formed, and REAL ------
jq_rc=0
jq empty "$AREAS" >/dev/null 2>&1 || jq_rc=$?
if [[ "$jq_rc" -eq 0 ]]; then json_valid=valid; else json_valid=invalid; fi
check "deslop-areas.json is valid JSON" "valid" "$json_valid"

area_count=$(jq 'length' "$AREAS" 2>/dev/null) || area_count=0
if [[ "$area_count" -gt 0 ]]; then has_areas=yes; else has_areas=no; fi
check "deslop-areas.json has at least one area entry" "yes" "$has_areas"

invalid_entries=$(jq '[
    .[] | select(
      (.id // "" | length == 0) or
      ((.paths // []) | length == 0) or
      (.charter // "" | length == 0)
    )
  ] | length' "$AREAS" 2>/dev/null) || invalid_entries=-1
check "every deslop-areas.json entry has a non-empty id, paths[], and charter" \
  "0" "$invalid_entries"

# Every path an entry claims to scan must actually exist — a registry entry
# pointing at a nonexistent path silently scans nothing.
missing_paths=0
while IFS= read -r p; do
  [[ -z "$p" ]] && continue
  if [[ ! -e "$ROOT_DIR/$p" ]]; then
    missing_paths=$((missing_paths + 1))
    echo "  missing path referenced in deslop-areas.json: $p"
  fi
done < <(jq -r '.[].paths[]' "$AREAS" 2>/dev/null)
check "every path referenced in deslop-areas.json exists on disk" \
  "0" "$missing_paths"

stale_layout_hits=$(jq -r '.[].paths[]' "$AREAS" 2>/dev/null | grep -Ec -- '^(backend|frontend)/') || true
check "no deslop-areas.json path references the stale backend/ or frontend/ layout" \
  "0" "$stale_layout_hits"

# --- 5) ralph-fleet-tests.yml must actually wire this guard into CI -----------
deslop_yml_trigger_hits=$(grep -cF -- 'deslop.yml' "$FLEET_TESTS_WF" 2>/dev/null) || true
if [[ "$deslop_yml_trigger_hits" -ge 1 ]]; then deslop_yml_wired=yes; else deslop_yml_wired=no; fi
check "ralph-fleet-tests.yml re-triggers on deslop.yml changes" "yes" "$deslop_yml_wired"

deslop_areas_trigger_hits=$(grep -cF -- 'deslop-areas.json' "$FLEET_TESTS_WF" 2>/dev/null) || true
if [[ "$deslop_areas_trigger_hits" -ge 1 ]]; then deslop_areas_wired=yes; else deslop_areas_wired=no; fi
check "ralph-fleet-tests.yml re-triggers on deslop-areas.json changes" "yes" "$deslop_areas_wired"

test_invocation_hits=$(grep -cF -- 'test_deslop_workflow.sh' "$FLEET_TESTS_WF" 2>/dev/null) || true
if [[ "$test_invocation_hits" -ge 1 ]]; then test_invoked=yes; else test_invoked=no; fi
check "ralph-fleet-tests.yml invokes test_deslop_workflow.sh" "yes" "$test_invoked"

# --- 6) actionlint, when available, must pass over deslop.yml -----------------
# Bounded/graceful: this repo's CI may not have actionlint on PATH, and that
# must never fail the suite — only its ABSENCE is skipped, never its findings.
if command -v actionlint >/dev/null 2>&1; then
  rc=0
  actionlint "$DESLOP" > "$WORK/actionlint.out" 2>&1 || rc=$?
  if [[ "$rc" -eq 0 ]]; then al_status=clean; else al_status=dirty; fi
  check "actionlint passes over deslop.yml" "clean" "$al_status"
  if [[ "$rc" -ne 0 ]]; then
    echo "  actionlint findings:"
    sed 's/^/    /' "$WORK/actionlint.out"
  fi
else
  echo "  skip - actionlint not on PATH; static workflow lint skipped"
fi

echo
echo "deslop-workflow tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
