#!/usr/bin/env bash
# scripts/ralph/test_queue_depth.sh
#
# Offline tests for queue-depth.sh — the hopper's pickable-backlog counter.
# queue-depth.sh prints a single integer: the count of ELIGIBLE open issues
# (per the shared require/exclude label rules in eligibility.sh) MINUS those
# already covered by an in-flight open PR (`Closes|Fixes|Resolves #N`).
#
# IMPORTANT: queue-depth.sh runs on a BARE GitHub Actions runner (the hopper
# workflow), which never has a checkout of `.ralph/worktrees` from any other
# job/runner. So "in-flight" here is approximated ONLY via open-PR bodies —
# there is deliberately no worktree/git-repo dependency, unlike pick-next.sh's
# active-lane detection. This suite therefore never `git init`s a fixture repo
# and always invokes the helper from a plain, non-repo scratch directory, to
# lock in "queue-depth.sh must not require being inside a git repository".
#
# Like pick-next.sh, queue-depth.sh talks to GitHub only through
# `gh ... --jq ...`, so we put a fake `gh` on PATH that emits the values a
# scenario needs. Two calls are stubbed:
#   gh issue list --json number,labels --jq "<shared filter>"  (JSON mode)
#   gh pr list    --json body          --jq '.[].body'
#
# Run:  bash scripts/ralph/test_queue_depth.sh
set -euo pipefail

RALPH_DIR="$(cd "$(dirname "$0")" && pwd)"
QD="$RALPH_DIR/queue-depth.sh"
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

# A plain, NON-git scratch directory. Every invocation of queue-depth.sh below
# runs from here — never from inside a git repository — to prove the helper
# needs no `git rev-parse` / worktree lookup at all (see header note).
PLAIN="$WORK/plain"; mkdir -p "$PLAIN"

# --- fake gh: reads scenario inputs from $STUBDIR (exported per scenario) ------
# Copied verbatim (case-statement logic) from test_pick_next.sh's stub so the
# REAL --jq filter queue-depth.sh embeds is exercised end-to-end, not bypassed.
BIN="$WORK/bin"; mkdir -p "$BIN"
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
# Emit the value real gh would produce *after* applying --jq for each call
# queue-depth.sh makes. Scenario data lives under $STUBDIR.
args="$*"
case "$args" in
  *"issue list"*)
    # Two modes. Default: emit the pre-jq'd TSV a scenario supplied (tests the
    # bash walk logic). Opt-in JSON mode ($STUBDIR/issue_json present): apply the
    # REAL --jq filter queue-depth.sh passes to a JSON fixture, so the embedded
    # require/exclude filter is exercised end-to-end.
    if [[ -f "$STUBDIR/issue_json" ]]; then
      filter=""; prev=""
      for a in "$@"; do
        [[ "$prev" == "--jq" ]] && { filter="$a"; break; }
        prev="$a"
      done
      jq -r "$filter" "$STUBDIR/issue_json"
    else
      cat "$STUBDIR/issue_list.tsv" 2>/dev/null || true
    fi ;;
  *"pr list"*)
    cat "$STUBDIR/pr_bodies" 2>/dev/null || true ;;
  *"issue view"*)
    for tok in "$@"; do
      if [[ "$tok" =~ ^[0-9]+$ ]]; then
        cat "$STUBDIR/labels/$tok" 2>/dev/null || true
        break
      fi
    done ;;
  *) : ;;
esac
STUB
chmod +x "$BIN/gh"

# --- scenario harness ---------------------------------------------------------
new_scenario() {
  STUBDIR="$WORK/scn.$1"; export STUBDIR
  rm -rf "$STUBDIR"
  mkdir -p "$STUBDIR/labels"
  : > "$STUBDIR/issue_list.tsv"
  : > "$STUBDIR/issue_json.lines"
  : > "$STUBDIR/pr_bodies"
}
pr_line() { printf '%s\n' "$1" >> "$STUBDIR/pr_bodies"; }   # <raw PR body line>

# JSON-mode helpers: build the fixture the stub feeds to queue-depth's REAL
# --jq filter (mirrors `gh issue list --json number,labels`).
ij_add() { # <num> <labels-csv>  — append one issue object
  local names
  names=$(jq -cn --arg s "$2" '$s | split(",") | map(select(length>0) | {name: .})')
  jq -cn --argjson n "$1" --argjson l "$names" '{number:$n, labels:$l}' \
    >> "$STUBDIR/issue_json.lines"
}
ij_finalize() { jq -s . "$STUBDIR/issue_json.lines" > "$STUBDIR/issue_json"; }

run_qd() { (cd "$PLAIN" && PATH="$BIN:$PATH" bash "$QD" 2>/dev/null); }

# 1) All-eligible backlog of 3 distinct open issues, no exclusions, no PRs.
new_scenario all_eligible
ij_add 10 ""; ij_add 11 ""; ij_add 12 ""
ij_finalize
check "3 distinct eligible issues, no PRs => count 3" "3" "$(run_qd)"

# 2) Excluded-label issues are not counted; only the eligible remainder is.
new_scenario exclude_labels
ij_add 10 ""              # eligible
ij_add 11 "epic"          # excluded
ij_add 12 "in-progress"   # excluded
ij_add 13 "blocked"       # excluded
ij_add 14 "bug"           # eligible
ij_finalize
check "excluded-label issues are not counted (2 eligible remain)" "2" "$(run_qd)"

# 3) In-flight PRs subtract eligible issues; markers are case-insensitive and
#    span all three synonyms (Closes/Fixes/Resolves).
new_scenario inflight_case_insensitive
ij_add 30 ""; ij_add 31 ""; ij_add 32 ""
ij_finalize
pr_line "Closes #30"
pr_line "fixes #31"
pr_line "Resolves #32"
check "in-flight PRs (mixed-case markers) subtract all 3 eligible issues" "0" "$(run_qd)"

# 4) CRITICAL set-difference correctness: a PR closing an issue OUTSIDE the
#    eligible set (already excluded / nonexistent) must not decrement count.
new_scenario set_difference_unrelated_pr
ij_add 40 ""; ij_add 41 ""; ij_add 42 ""
ij_finalize
pr_line "Closes #999"
check "PR closing an issue outside the eligible set does not decrement count" \
  "3" "$(run_qd)"

# 5) RALPH_REQUIRE_LABELS=agent-ready env override restricts the count to
#    agent-ready-labelled issues only.
new_scenario require_labels_override
ij_add 50 "agent-ready"; ij_add 51 ""; ij_add 52 "agent-ready,bug"
ij_finalize
check "RALPH_REQUIRE_LABELS=agent-ready restricts count to agent-ready issues" \
  "2" \
  "$(cd "$PLAIN" && PATH="$BIN:$PATH" RALPH_REQUIRE_LABELS=agent-ready bash "$QD" 2>/dev/null)"

# 6) Empty backlog prints the literal character "0" (hopper does numeric
#    comparison on the output — an empty string would break `[ "$COUNT" -ge ]`).
new_scenario empty_backlog
ij_finalize
check "empty backlog prints literal 0" "0" "$(run_qd)"

# 7) Single-source guard (no gh needed): the default exclude-label list string
#    must be defined in exactly ONE place — eligibility.sh — never duplicated,
#    and both pick-next.sh and queue-depth.sh must source it. Production
#    scripts only: test_*.sh fixtures legitimately reference the literal as an
#    expected value, which would otherwise self-inflate the count.
EXCLUDE_STR='epic wontfix duplicate invalid question blocked needs-spec future-work do-not-auto-merge in-progress'

prod_files=()
while IFS= read -r f; do
  prod_files+=("$f")
done < <(find "$RALPH_DIR" -maxdepth 1 -name '*.sh' ! -name 'test_*.sh' | sort)

total_hits=$(grep -c -- "$EXCLUDE_STR" "${prod_files[@]}" 2>/dev/null \
  | awk -F: '{sum+=$NF} END{print sum+0}') || true
check "shared exclude-label list string appears exactly once across scripts/ralph/*.sh" \
  "1" "$total_hits"

hit_files=$(grep -l -- "$EXCLUDE_STR" "${prod_files[@]}" 2>/dev/null) || true
check "shared exclude-label list lives in eligibility.sh" \
  "$RALPH_DIR/eligibility.sh" "$hit_files"

sources_eligibility() { # <file> — non-comment "source"/"." line naming eligibility.sh
  grep -Eq '(^|[^#])[[:space:]]*(source|\.)[[:space:]]+.*eligibility\.sh' "$1" 2>/dev/null
}
if sources_eligibility "$RALPH_DIR/pick-next.sh"; then pn_src=yes; else pn_src=no; fi
check "pick-next.sh sources the shared eligibility.sh" "yes" "$pn_src"

if sources_eligibility "$QD"; then qd_src=yes; else qd_src=no; fi
check "queue-depth.sh sources the shared eligibility.sh" "yes" "$qd_src"

echo
echo "queue-depth tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
