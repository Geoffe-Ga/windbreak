#!/usr/bin/env bash
# scripts/ralph/test_should_retry_review.sh
#
# Offline tests for should-retry-review.sh (issue #152) — the thin decision
# wrapper that lets the Code Review workflow attempt ONE bounded in-job retry
# of the Claude review step when the review agent completes without posting
# its verdict.
#
# should-retry-review.sh has the SAME CLI signature as assert-review-posted.sh:
#   should-retry-review.sh <PR_NUMBER> <STARTED_AT> \
#     [--repo <owner/repo>] [--execution-file <path>]
#
# It resolves and invokes the REAL assert-review-posted.sh (cwd-independent,
# via SCRIPT_DIR — same pattern verdict-regex.sh sourcing uses) and maps its
# exit code to a retry decision:
#
#   assert exit 0 (verdict posted)              -> retry=false reason=verdict-posted
#   assert exit 3 (retryable miss)               -> retry=true  reason=no-verdict-retryable
#   assert exit 1 (hard failure: agent errored,
#                  or workflow-validation guard)  -> retry=false reason=<non-empty>
#   assert exit 2 (usage error)                   -> propagated as exit 2
#
# The decision is APPENDED to the file named by $GITHUB_OUTPUT as
# `retry=true|false` and `reason=<...>` lines. When $GITHUB_OUTPUT is unset,
# the same lines are printed to stdout instead (never a crash). The wrapper
# itself exits 0 for ANY decided outcome — including retry=false — because a
# "no" decision must not fail the calling job; only a USAGE error propagates
# a nonzero (2) exit.
#
# should-retry-review.sh does not exist yet, so every functional case below
# fails RED (the direct absolute-path invocation exits 127, "no such file").
# The static workflow guards at the bottom are RED too: code-review.yml has no
# retry step wired up yet, and ralph-fleet-tests.yml doesn't run this suite.
#
# Run:  bash scripts/ralph/test_should_retry_review.sh
set -euo pipefail

RALPH_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$RALPH_DIR/../.." && pwd)"
SCRIPT="$RALPH_DIR/should-retry-review.sh"
WORKFLOWS_DIR="$ROOT_DIR/.github/workflows"
CODE_REVIEW_YML="$WORKFLOWS_DIR/code-review.yml"
FLEET_TESTS_YML="$WORKFLOWS_DIR/ralph-fleet-tests.yml"

PASS=0
FAIL=0
ok()  { PASS=$((PASS + 1)); printf '  ok  - %s\n' "$1"; }
bad() { FAIL=$((FAIL + 1)); printf 'FAIL  - %s\n' "$1"; }
check() { # check <desc> <expected> <actual>
  if [[ "$2" == "$3" ]]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
BIN="$WORK/bin"
mkdir -p "$BIN"
mkdir -p "$WORK/sub"
STDOUT_FILE="$WORK/stdout.txt"
STDERR_FILE="$WORK/stderr.txt"
GH_OUTPUT_FILE="$WORK/gh_output"

# Arg-aware fake gh — identical stub to test_assert_review_posted.sh, since the
# end-to-end path here composes should-retry-review.sh -> REAL
# assert-review-posted.sh -> fake gh, so decisions are tested against real
# production logic, not a re-implemented mock of it.
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"--json comments"*)
    if [[ -n "${COMMENTS_JSON:-}" ]]; then
      expr="" prev="" has_jq=no
      for a in "$@"; do
        if [[ "$prev" == "--jq" ]]; then expr="$a"; has_jq=yes; fi
        prev="$a"
      done
      if [[ "$has_jq" == yes ]]; then
        printf '%s' "$COMMENTS_JSON" | jq -rc "$expr"
      else
        printf '%s' "$COMMENTS_JSON"
      fi
    else
      printf '{"comments":[]}\n'
    fi
    ;;
  *"--name-only"*)
    if [[ -n "${DIFF_RC:-}" ]]; then
      exit "$DIFF_RC"
    fi
    if [[ -n "${DIFF_FILES:-}" ]]; then
      printf '%s\n' "$DIFF_FILES"
    fi
    ;;
  *) echo '' ;;
esac
STUB
chmod +x "$BIN/gh"

# get_output <file> <key> — last `key=value` line's value, or empty. Several
# call sites use this as a plain `var=$(get_output ...)` assignment; under
# `set -euo pipefail` a bare no-match (grep exits 1) would otherwise abort the
# whole suite mid-run instead of cleanly recording a FAIL, so the trailing
# `|| true` pins this pipeline's own exit status at 0 unconditionally — a
# missing key still resolves to an empty string, never a crash.
get_output() {
  grep -E "^$2=" "$1" 2>/dev/null | tail -1 | cut -d= -f2- || true
}

# run_capture <args...> — invokes the script under test with the fake gh on
# PATH and a per-invocation GITHUB_OUTPUT file (truncated first), discards
# nothing (stdout/stderr both captured), and prints the exit code. Uses the
# `rc=0; ... || rc=$?` idiom so `set -e` never trips on a nonzero (including a
# missing-script 127) exit.
run_capture() {
  local rc=0
  : > "$GH_OUTPUT_FILE"
  : > "$STDOUT_FILE"
  : > "$STDERR_FILE"
  PATH="$BIN:$PATH" GITHUB_OUTPUT="$GH_OUTPUT_FILE" \
    "$SCRIPT" "$@" >"$STDOUT_FILE" 2>"$STDERR_FILE" || rc=$?
  printf '%s' "$rc"
}

# run_capture_sub <args...> — identical, but invoked from a subdirectory, to
# prove the script derives no cwd-dependent state.
run_capture_sub() {
  local rc=0
  : > "$GH_OUTPUT_FILE"
  : > "$STDOUT_FILE"
  : > "$STDERR_FILE"
  (cd "$WORK/sub" && PATH="$BIN:$PATH" GITHUB_OUTPUT="$GH_OUTPUT_FILE" \
    "$SCRIPT" "$@" >"$STDOUT_FILE" 2>"$STDERR_FILE") || rc=$?
  printf '%s' "$rc"
}

# run_capture_no_output <args...> — GITHUB_OUTPUT deliberately UNSET (even if
# the ambient environment set one, e.g. when this suite itself runs inside a
# real GitHub Actions step), to exercise the stdout-fallback path.
run_capture_no_output() {
  local rc=0
  : > "$STDOUT_FILE"
  : > "$STDERR_FILE"
  PATH="$BIN:$PATH" env -u GITHUB_OUTPUT "$SCRIPT" "$@" \
    >"$STDOUT_FILE" 2>"$STDERR_FILE" || rc=$?
  printf '%s' "$rc"
}

PR=100
STARTED="2026-07-01T10:00:00Z"     # the review step's recorded start time
FRESH="2026-07-01T11:00:00Z"       # a verdict posted AFTER STARTED (valid)
STALE="2026-07-01T09:00:00Z"       # a verdict posted BEFORE STARTED (stale)

cj() { printf '{"comments":[%s]}' "$1"; }   # wrap comment object(s) as a payload

EMPTY_COMMENTS='{"comments":[]}'
FRESH_LGTM_COMMENTS="$(cj '{"createdAt":"'"$FRESH"'","body":"## Verdict: LGTM\n"}')"

WORKFLOW_TOUCHING_DIFF=$'scripts/ralph/assert-review-posted.sh\n.github/workflows/code-review.yml'

if command -v jq >/dev/null 2>&1; then

  # --- 1) verdict posted -> retry=false reason=verdict-posted, exit 0 ---------
  rc="$(COMMENTS_JSON="$FRESH_LGTM_COMMENTS" run_capture "$PR" "$STARTED")"
  check "fresh verdict posted -> wrapper exit 0" "0" "$rc"
  check "fresh verdict posted -> retry=false" "false" "$(get_output "$GH_OUTPUT_FILE" retry)"
  check "fresh verdict posted -> reason=verdict-posted" "verdict-posted" \
    "$(get_output "$GH_OUTPUT_FILE" reason)"

  # --- 2) no verdict + execution-file is_error:true -> hard failure, not retryable
  ERROR_EXEC="$WORK/exec-error.json"
  cat > "$ERROR_EXEC" <<'JSON'
[{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Credit balance is too low."}]
JSON
  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" run_capture "$PR" "$STARTED" --execution-file "$ERROR_EXEC")"
  check "agent errored (is_error:true) -> wrapper exit 0" "0" "$rc"
  check "agent errored -> retry=false" "false" "$(get_output "$GH_OUTPUT_FILE" retry)"
  reason_val="$(get_output "$GH_OUTPUT_FILE" reason)"
  check "agent errored -> reason is non-empty" "yes" \
    "$([[ -n "$reason_val" ]] && echo yes || echo no)"

  # --- 3) no verdict + diff touches code-review.yml -> workflow-validation guard
  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" DIFF_FILES="$WORKFLOW_TOUCHING_DIFF" \
        run_capture "$PR" "$STARTED")"
  check "workflow-validation guard fires -> wrapper exit 0" "0" "$rc"
  check "workflow-validation guard fires -> retry=false" "false" "$(get_output "$GH_OUTPUT_FILE" retry)"
  reason_val="$(get_output "$GH_OUTPUT_FILE" reason)"
  check "workflow-validation guard fires -> reason is non-empty" "yes" \
    "$([[ -n "$reason_val" ]] && echo yes || echo no)"

  # --- 4) no verdict + clean/unrelated diff -> THE true branch ----------------
  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" DIFF_FILES='scripts/ralph/pr-ready.sh' \
        run_capture "$PR" "$STARTED")"
  check "clean unrelated diff, no verdict -> wrapper exit 0" "0" "$rc"
  check "clean unrelated diff, no verdict -> retry=true" "true" "$(get_output "$GH_OUTPUT_FILE" retry)"
  check "clean unrelated diff, no verdict -> reason=no-verdict-retryable" "no-verdict-retryable" \
    "$(get_output "$GH_OUTPUT_FILE" reason)"

  # --- 5) no verdict + missing execution-file path -> falls through -> retryable
  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" run_capture "$PR" "$STARTED" \
        --execution-file "$WORK/does-not-exist.json")"
  check "missing execution-file path -> wrapper exit 0" "0" "$rc"
  check "missing execution-file path -> retry=true" "true" "$(get_output "$GH_OUTPUT_FILE" retry)"

  # --- 6) no verdict + empty --execution-file "" value -> retryable -----------
  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" run_capture "$PR" "$STARTED" --execution-file "")"
  check "empty --execution-file value -> wrapper exit 0" "0" "$rc"
  check "empty --execution-file value -> retry=true" "true" "$(get_output "$GH_OUTPUT_FILE" retry)"

  # --- 7) no verdict + malformed execution file -> retryable ------------------
  MALFORMED_EXEC="$WORK/exec-malformed.json"
  printf 'not valid json {' > "$MALFORMED_EXEC"
  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" run_capture "$PR" "$STARTED" --execution-file "$MALFORMED_EXEC")"
  check "malformed execution file -> wrapper exit 0" "0" "$rc"
  check "malformed execution file -> retry=true" "true" "$(get_output "$GH_OUTPUT_FILE" retry)"

  # --- 8) no verdict + DIFF_RC=1 (diff query hard-fails) -> retryable, no crash
  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" DIFF_RC=1 run_capture "$PR" "$STARTED")"
  check "diff query hard-fails (DIFF_RC=1) -> wrapper exit 0, no crash" "0" "$rc"
  check "diff query hard-fails -> retry=true" "true" "$(get_output "$GH_OUTPUT_FILE" retry)"

  # --- 9) near-miss diff paths must NOT trigger the guard ----------------------
  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" DIFF_FILES='docs/code-review.yml' \
        run_capture "$PR" "$STARTED")"
  check "near-miss 'docs/code-review.yml' -> retry=true (not a guard)" "true" \
    "$(get_output "$GH_OUTPUT_FILE" retry)"

  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" DIFF_FILES='.github/workflows/code-review.yml.bak' \
        run_capture "$PR" "$STARTED")"
  check "near-miss '.github/workflows/code-review.yml.bak' -> retry=true (not a guard)" "true" \
    "$(get_output "$GH_OUTPUT_FILE" retry)"

  # --- 10) stale verdict also counts as "no fresh verdict" -> retryable -------
  STALE_LGTM_COMMENTS="$(cj '{"createdAt":"'"$STALE"'","body":"## Verdict: LGTM\n"}')"
  rc="$(COMMENTS_JSON="$STALE_LGTM_COMMENTS" run_capture "$PR" "$STARTED")"
  check "stale verdict (createdAt < STARTED_AT) -> retry=true" "true" \
    "$(get_output "$GH_OUTPUT_FILE" retry)"

  # --- 11) GITHUB_OUTPUT unset -> stdout fallback, parseable, no crash --------
  rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" run_capture_no_output "$PR" "$STARTED")"
  check "GITHUB_OUTPUT unset -> wrapper exit 0" "0" "$rc"
  check "GITHUB_OUTPUT unset -> stdout carries a parseable 'retry=true' line" "yes" \
    "$(grep -qx 'retry=true' "$STDOUT_FILE" && echo yes || echo no)"
  check "GITHUB_OUTPUT unset -> stdout carries a 'reason=' line" "yes" \
    "$(grep -qE '^reason=' "$STDOUT_FILE" && echo yes || echo no)"

  # --- 12) cwd-independence guard ----------------------------------------------
  a_rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" run_capture "$PR" "$STARTED")"
  a_retry="$(get_output "$GH_OUTPUT_FILE" retry)"
  b_rc="$(COMMENTS_JSON="$EMPTY_COMMENTS" run_capture_sub "$PR" "$STARTED")"
  b_retry="$(get_output "$GH_OUTPUT_FILE" retry)"
  check "cwd-independence: exit code identical from \$WORK vs a subdirectory" "$a_rc" "$b_rc"
  check "cwd-independence: retry decision identical from \$WORK vs a subdirectory" "$a_retry" "$b_retry"

else
  echo "  skip - real-jq decision cases (jq not installed)"
fi

# --- 13) usage errors: no args / missing STARTED_AT ---------------------------
rc="$(run_capture)"
check "no args at all -> exit 2 (usage)" "2" "$rc"
check "no args -> nothing written to GITHUB_OUTPUT" "" "$(cat "$GH_OUTPUT_FILE" 2>/dev/null || true)"

rc="$(run_capture "$PR")"
check "PR given but STARTED_AT missing -> exit 2 (usage)" "2" "$rc"
check "STARTED_AT missing -> nothing written to GITHUB_OUTPUT" "" "$(cat "$GH_OUTPUT_FILE" 2>/dev/null || true)"

# --- 14) static guard: code-review.yml wires the bounded retry ----------------
# The decision step must run AFTER the first review attempt (id: claude-review)
# — the same first-match line-number-compare idiom
# test_assert_review_posted.sh uses for REVIEW_STARTED_AT ordering.
retry_decision_line=$(grep -n -- 'should-retry-review.sh' "$CODE_REVIEW_YML" 2>/dev/null | head -1 | cut -d: -f1) || true
claude_review_line=$(grep -n -- 'id: claude-review' "$CODE_REVIEW_YML" 2>/dev/null | head -1 | cut -d: -f1) || true
if [[ -n "$retry_decision_line" && -n "$claude_review_line" ]] \
   && [[ "$claude_review_line" -lt "$retry_decision_line" ]]; then
  retry_order_status=after
else
  retry_order_status=not-after-or-missing
fi
check "should-retry-review.sh runs AFTER the id: claude-review step" \
  "after" "$retry_order_status"

claude_review_retry_hits=$(grep -cF -- 'id: claude-review-retry' "$CODE_REVIEW_YML" 2>/dev/null) || true
if [[ "$claude_review_retry_hits" -ge 1 ]]; then retry_step_present=yes; else retry_step_present=no; fi
check "code-review.yml has an 'id: claude-review-retry' step" "yes" "$retry_step_present"

action_hits=$(grep -cF -- 'uses: anthropics/claude-code-action' "$CODE_REVIEW_YML" 2>/dev/null) || true
check "exactly 2 claude-code-action invocations (bounded to ONE retry)" "2" "$action_hits"

retry_gate_hits=$(grep -cF -- "steps.retry-decision.outputs.retry == 'true'" "$CODE_REVIEW_YML" 2>/dev/null) || true
if [[ "$retry_gate_hits" -ge 2 ]]; then retry_gate_status=ge2; else retry_gate_status=lt2; fi
check "at least 2 steps gated on steps.retry-decision.outputs.retry == 'true'" "ge2" "$retry_gate_status"

started_at_reexport_hits=$(grep -cF -- 'REVIEW_STARTED_AT=$(date' "$CODE_REVIEW_YML" 2>/dev/null) || true
if [[ "$started_at_reexport_hits" -ge 2 ]]; then reexport_status=ge2; else reexport_status=lt2; fi
check "REVIEW_STARTED_AT is re-exported for the retried window (>= 2 occurrences)" \
  "ge2" "$reexport_status"

execution_file_fallback_hits=$(grep -cF -- 'claude-review-retry.outputs.execution_file ||' "$CODE_REVIEW_YML" 2>/dev/null) || true
if [[ "$execution_file_fallback_hits" -ge 1 ]]; then fallback_status=yes; else fallback_status=no; fi
check "final assert's EXECUTION_FILE falls back with 'claude-review-retry.outputs.execution_file ||'" \
  "yes" "$fallback_status"

review_prompt_ref_hits=$(grep -cF -- '${{ env.REVIEW_PROMPT }}' "$CODE_REVIEW_YML" 2>/dev/null) || true
check "exactly 2 references to \${{ env.REVIEW_PROMPT }} (both attempts share one prompt)" \
  "2" "$review_prompt_ref_hits"

review_prompt_def_hits=$(grep -cF -- 'REVIEW_PROMPT:' "$CODE_REVIEW_YML" 2>/dev/null) || true
check "exactly 1 REVIEW_PROMPT: definition (single-sourced)" "1" "$review_prompt_def_hits"

has_gh_pr_comment=$(grep -qF -- 'gh pr comment' "$CODE_REVIEW_YML" 2>/dev/null && echo yes || echo no)
# Exclude YAML comment lines (leading '#') so an unrelated `# ... required for`
# explanatory comment elsewhere in the file (e.g. documenting allowed-tools)
# can never false-positive this guard — the vocabulary must appear in the
# workflow's actual instructional text (prompt/run bodies), not its comments.
has_mandatory_vocab=$(grep -vE -- '^[[:space:]]*#' "$CODE_REVIEW_YML" 2>/dev/null \
  | grep -qiE -- '\b(must|required|mandatory)\b' && echo yes || echo no)
check "prompt still instructs gh pr comment" "yes" "$has_gh_pr_comment"
check "prompt is strengthened with MUST/required/mandatory vocabulary (outside comments)" \
  "yes" "$has_mandatory_vocab"

# --- 15) static guard: self-wiring into ralph-fleet-tests.yml -----------------
run_list_hits=$(grep -cF -- 'test_should_retry_review.sh' "$FLEET_TESTS_YML" 2>/dev/null) || true
if [[ "$run_list_hits" -ge 1 ]]; then run_list_wired=yes; else run_list_wired=no; fi
check "ralph-fleet-tests.yml run: list invokes test_should_retry_review.sh" \
  "yes" "$run_list_wired"

# --- 16) actionlint, when available, must pass over code-review.yml -----------
# Bounded/graceful: this repo's CI may not have actionlint on PATH, and that
# must never fail the suite — only its ABSENCE is skipped, never its findings.
if command -v actionlint >/dev/null 2>&1; then
  rc=0
  actionlint "$CODE_REVIEW_YML" > "$WORK/actionlint.out" 2>&1 || rc=$?
  if [[ "$rc" -eq 0 ]]; then al_status=clean; else al_status=dirty; fi
  check "actionlint passes over code-review.yml" "clean" "$al_status"
  if [[ "$rc" -ne 0 ]]; then
    echo "  actionlint findings:"
    sed 's/^/    /' "$WORK/actionlint.out"
  fi
else
  echo "  skip - actionlint not on PATH; static workflow lint skipped"
fi

# --- summary --------------------------------------------------------------
echo
echo "should-retry-review tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
