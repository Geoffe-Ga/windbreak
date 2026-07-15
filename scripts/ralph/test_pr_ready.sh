#!/usr/bin/env bash
# scripts/ralph/test_pr_ready.sh
#
# Offline tests for pr-ready.sh — the authoritative CI + review-verdict
# readiness check the orchestrator (ralph-tick.md Step 1) uses before merging a
# lane. CI state is keyed off the `gh pr checks` EXIT CODE (0=green, 8=pending,
# else=failed), never a text grep of its TAB-delimited output, and an LGTM
# verdict only counts when it is fresher than the PR's HEAD commit (stale-verdict
# guard). We put a fake, arg-aware `gh` on PATH and assert every classification.
#
# Issue #129 (RED): pr-ready.sh's token vocabulary is being widened from
# ready|behind|pending|ci-failed|awaiting-review to also emit `comments` (fresh
# COMMENTS verdict + CLEAN), `comments-behind` (fresh COMMENTS + BEHIND), and
# `changes-requested` (a fresh verdict-bearing comment that is neither LGTM nor
# COMMENTS, derived by elimination). The cases below pin that vocabulary; they
# fail against the CURRENT pr-ready.sh, which knows none of the three new
# tokens, until the implementation lands.
#
# Run:  bash scripts/ralph/test_pr_ready.sh
set -euo pipefail

READY="$(cd "$(dirname "$0")" && pwd)/pr-ready.sh"
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

# Arg-aware fake gh. Behaviour is driven by env vars the test sets per case:
#   CHECKS_EC     — exit code `gh pr checks` should return (0 green / 8 pending / other failed)
#   MERGE_STATE   — mergeStateStatus (CLEAN / BEHIND / ...)
#   HEAD_DATE     — RFC3339 committedDate of the PR HEAD commit
#   VERDICT       — the "<createdAt>|<isLGTM>|<isCOMMENTS>" scalar the verdict
#                   jq resolves to (widened to 3 fields for issue #129 — the
#                   third field lets a scalar-stub case pin a fresh COMMENTS
#                   verdict without going through the real regex)
#   COMMENTS_JSON — raw `--json comments` payload; when set, the stub runs the
#                   REAL jq with pr-ready.sh's own `--jq` expression against it,
#                   so the production verdict regex is genuinely exercised
#                   (otherwise a scalar stub would mask a broken regex).
# Real gh applies --jq, so — like test_fleet.sh — the stub emits the already
# extracted scalar and branches on which --json field the caller asked for.
cat > "$BIN/gh" <<'STUB'
#!/usr/bin/env bash
args="$*"
case "$args" in
  *"pr checks"*)            exit "${CHECKS_EC:-0}" ;;
  *"--json mergeStateStatus"*) printf '%s|%s\n' "${MERGE_STATE:-CLEAN}" "${HEAD_DATE:-}" ;;
  *"--json comments"*)
    if [[ -n "${COMMENTS_JSON:-}" ]]; then
      expr="" prev=""
      for a in "$@"; do [[ "$prev" == "--jq" ]] && expr="$a"; prev="$a"; done
      printf '%s' "$COMMENTS_JSON" | jq -rc "$expr"
    else
      printf '%s\n' "${VERDICT:-|false|false}"
    fi ;;
  *)                        echo '' ;;
esac
STUB
chmod +x "$BIN/gh"

run() { PATH="$BIN:$PATH" "$READY" "$@" 2>/dev/null; }

H="2026-07-01T10:00:00Z"          # HEAD commit time baseline
FRESH="2026-07-01T11:00:00Z"      # a verdict posted AFTER HEAD (valid)
STALE="2026-07-01T09:00:00Z"      # a verdict posted BEFORE HEAD (stale)

# --- usage: missing PR number exits 2 --------------------------------------
rc=0
PATH="$BIN:$PATH" "$READY" >/dev/null 2>&1 || rc=$?
check "missing PR number exits 2" "2" "$rc"

# --- pending: gh pr checks exit 8 is NEVER ready (the core bug) -------------
check "exit 8 → pending" "pending" \
  "$(CHECKS_EC=8 run 100)"

# --- CI failure surfaced: non-0/non-8 exit → ci-failed ---------------------
check "exit 1 → ci-failed" "ci-failed" \
  "$(CHECKS_EC=1 run 100)"
check "exit 2 → ci-failed" "ci-failed" \
  "$(CHECKS_EC=2 run 100)"

# --- ready: green + CLEAN + fresh LGTM -------------------------------------
check "green + CLEAN + fresh LGTM → ready" "ready" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true|false" run 100)"

# --- behind: green + fresh LGTM but not up-to-date -------------------------
check "green + BEHIND + fresh LGTM → behind" "behind" \
  "$(CHECKS_EC=0 MERGE_STATE=BEHIND HEAD_DATE=$H VERDICT="$FRESH|true|false" run 100)"

# --- stale-verdict guard: an LGTM older than HEAD does NOT count ------------
check "green + CLEAN + STALE LGTM → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$STALE|true|false" run 100)"

# --- no verdict yet → awaiting-review --------------------------------------
check "green + CLEAN + no verdict → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="|false|false" run 100)"

# --- RED (#129): fresh CHANGES_REQUESTED (neither LGTM nor COMMENTS) is now
# derived by elimination → changes-requested, not the old awaiting-review ----
check "green + CLEAN + fresh CHANGES_REQUESTED → changes-requested" "changes-requested" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|false|false" run 100)"

# --- RED (#129): fresh COMMENTS verdict gets its own token, distinct from
# both LGTM and changes-requested --------------------------------------------
check "green + CLEAN + fresh COMMENTS → comments" "comments" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|false|true" run 100)"

# --- RED (#129): fresh COMMENTS + BEHIND → comments-behind ------------------
check "green + BEHIND + fresh COMMENTS → comments-behind" "comments-behind" \
  "$(CHECKS_EC=0 MERGE_STATE=BEHIND HEAD_DATE=$H VERDICT="$FRESH|false|true" run 100)"

# --- stale-verdict guard applies to COMMENTS too ----------------------------
check "green + CLEAN + STALE COMMENTS → awaiting-review" "awaiting-review" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$STALE|false|true" run 100)"

# --- CI precedence: pending short-circuits BEFORE any verdict parsing, even
# with a fresh COMMENTS verdict sitting on the PR ----------------------------
check "exit 8 → pending (even with a fresh COMMENTS verdict)" "pending" \
  "$(CHECKS_EC=8 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|false|true" run 100)"

# --- REAL jq: exercise the production verdict regex against real bodies ----
# The verdict `claude-code-review.yml` posts is `## Verdict: <X>` at the END of a
# long `## Summary …` body. These cases feed raw comment JSON through pr-ready.sh's
# own `--jq`, so a regex that fails to match `## Verdict:` — or that reads "LGTM"
# from prose instead of the verdict line — is caught here (a scalar stub can't).
if command -v jq >/dev/null 2>&1; then
  cj() { printf '{"comments":[%s]}' "$1"; }   # wrap comment object(s) as a payload

  # Canonical `## Verdict: LGTM`, fresh + CLEAN → ready.
  check "real ## Verdict: LGTM (fresh) → ready" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Summary\ngood\n\n## Verdict: LGTM\n"}')" \
       run 100)"

  # `**Verdict:** CHANGES_REQUESTED` whose prose mentions "LGTM" must NOT count as
  # LGTM — the exact false-positive a whole-body match would cause. RED (#129):
  # a fresh non-LGTM/non-COMMENTS verdict is now changes-requested by
  # elimination, not the old awaiting-review.
  check "real CHANGES_REQUESTED w/ 'LGTM' in prose → changes-requested" "changes-requested" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"Not ready for LGTM yet.\n\n**Verdict:** CHANGES_REQUESTED\n"}')" \
       run 100)"

  # RED (#129): `**Verdict:** COMMENTS` must classify as comments, not the old
  # awaiting-review / a false LGTM.
  check "real **Verdict:** COMMENTS (fresh) → comments" "comments" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"Some notes here.\n\n**Verdict:** COMMENTS\n"}')" \
       run 100)"

  # RED (#129): canonical single-line `## Verdict: COMMENTS` → comments.
  check "real ## Verdict: COMMENTS (fresh, single line) → comments" "comments" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Summary\nfeedback\n\n## Verdict: COMMENTS\n"}')" \
       run 100)"

  # No verdict-bearing comment at all → awaiting-review.
  check "real no-verdict comment → awaiting-review" "awaiting-review" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"just a chat comment"}')" \
       run 100)"

  # Latest verdict wins: an LGTM posted after an earlier CHANGES_REQUESTED → ready.
  check "real latest-verdict-wins (LGTM after CR) → ready" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$STALE"'","body":"## Verdict: CHANGES_REQUESTED\n"},{"createdAt":"'"$FRESH"'","body":"## Verdict: LGTM\n"}')" \
       run 100)"

  # A real, fresh `## Verdict: LGTM` that predates HEAD is still stale → awaiting.
  check "real ## Verdict: LGTM but stale → awaiting-review" "awaiting-review" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$STALE"'","body":"## Verdict: LGTM\n"}')" \
       run 100)"

  # The real reviewer format: a heading with the token on the NEXT line,
  # emoji-prefixed. The separator class `[:*\s]+` cannot cross the emoji, so
  # this currently misclassifies as awaiting-review — the exact observed bug.
  check "real ## Verdict\\n✅ LGTM (fresh) → ready" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Summary\ngood\n\n## Verdict\n✅ LGTM\n"}')" \
       run 100)"

  # Same-line emoji variant: `## Verdict: ✅ LGTM` — the emoji sits between the
  # colon/space separator and the token itself.
  check "real ## Verdict: ✅ LGTM (fresh, same line) → ready" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Verdict: ✅ LGTM\n"}')" \
       run 100)"

  # RED (#129): same-line emoji COMMENTS variant → comments.
  check "real ## Verdict: 💬 COMMENTS (fresh, same line) → comments" "comments" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Verdict: 💬 COMMENTS\n"}')" \
       run 100)"

  # Latest-verdict-wins across the emoji-prefixed format: a stale
  # CHANGES_REQUESTED followed by a fresh LGTM must still resolve to ready.
  check "real latest-verdict-wins across emoji format (LGTM after CR) → ready" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$STALE"'","body":"## Verdict\n🔴 CHANGES_REQUESTED\n"},{"createdAt":"'"$FRESH"'","body":"## Verdict\n✅ LGTM\n"}')" \
       run 100)"

  # Guard: emoji-prefixed CHANGES_REQUESTED must never read as LGTM. RED (#129):
  # retokened from awaiting-review — a fresh non-LGTM/non-COMMENTS verdict is
  # now changes-requested by elimination.
  check "real ## Verdict\\n🔴 CHANGES_REQUESTED (fresh) → changes-requested" "changes-requested" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Verdict\n🔴 CHANGES_REQUESTED\n"}')" \
       run 100)"

  # Guard: emoji-prefixed COMMENTS must never read as LGTM. RED (#129): this is
  # the real-world PR #225/#230 posting format — retokened from awaiting-review
  # to the new dedicated comments token.
  check "real ## Verdict\\n💬 COMMENTS (fresh) → comments" "comments" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Verdict\n💬 COMMENTS\n"}')" \
       run 100)"

  # Prose guard: the widened separator class must still stop at the first
  # alphanumeric character after the emoji, so a stray "LGTM" later in the
  # body's prose never reactivates the match. RED (#129): retokened from
  # awaiting-review — this is a fresh CHANGES_REQUESTED, resolved by
  # elimination.
  check "real prose 'LGTM' after emoji CHANGES_REQUESTED → changes-requested" "changes-requested" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Verdict\n🔴 CHANGES_REQUESTED\n\nAn LGTM will come after fixes.\n"}')" \
       run 100)"

  # RED (#129): prose guard the other direction — a stray "comments" word after
  # an LGTM verdict line must not flip the classification to comments.
  check "real prose 'comments' word after LGTM → ready (not comments)" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Verdict\n✅ LGTM\n\nSee comments below.\n"}')" \
       run 100)"

  # RED (#129): same prose guard for CHANGES_REQUESTED — a stray "comments"
  # word in prose must not flip changes-requested to comments.
  check "real prose 'comments' word after CHANGES_REQUESTED → changes-requested (not comments)" "changes-requested" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$FRESH"'","body":"## Verdict\n🔴 CHANGES_REQUESTED\n\nAddressed in comments.\n"}')" \
       run 100)"

  # RED (#129): latest-verdict-wins — a fresh COMMENTS posted after a stale
  # LGTM must resolve to comments, not ready.
  check "real latest-verdict-wins (COMMENTS after stale LGTM) → comments" "comments" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$STALE"'","body":"## Verdict: LGTM\n"},{"createdAt":"'"$FRESH"'","body":"## Verdict: COMMENTS\n"}')" \
       run 100)"

  # RED (#129): latest-verdict-wins the other direction — a fresh LGTM posted
  # after a stale COMMENTS must resolve to ready, not comments.
  check "real latest-verdict-wins (LGTM after stale COMMENTS) → ready" "ready" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$STALE"'","body":"## Verdict: COMMENTS\n"},{"createdAt":"'"$FRESH"'","body":"## Verdict: LGTM\n"}')" \
       run 100)"

  # Freshness guard intact under the new emoji-prefixed format: a stale LGTM
  # must still be rejected even once the separator class is widened.
  check "real ## Verdict\\n✅ LGTM but stale → awaiting-review" "awaiting-review" \
    "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H \
       COMMENTS_JSON="$(cj '{"createdAt":"'"$STALE"'","body":"## Verdict\n✅ LGTM\n"}')" \
       run 100)"
else
  echo "  skip - real-jq verdict-regex cases (jq not installed)"
fi

# --- cwd-independence guard ------------------------------------------------
# pr-ready.sh takes the PR number explicitly and never resolves a repo root,
# so its classification must be identical no matter the caller's cwd. This
# passes TODAY — it's a regression guard (not a RED case) protecting the
# "no cwd-derived state" design so a future refactor doesn't quietly
# introduce one, unlike fleet.sh / pick-next.sh which infer the root from
# `git rev-parse --show-toplevel` and are being fixed for #83.
mkdir -p "$WORK/sub"
run_sub() { (cd "$WORK/sub" && PATH="$BIN:$PATH" "$READY" "$@" 2>/dev/null); }

check "pending verdict identical from \$WORK vs a subdirectory (cwd guard)" \
  "$(CHECKS_EC=8 run 100)" "$(CHECKS_EC=8 run_sub 100)"

check "ready verdict identical from \$WORK vs a subdirectory (cwd guard)" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true|false" run 100)" \
  "$(CHECKS_EC=0 MERGE_STATE=CLEAN HEAD_DATE=$H VERDICT="$FRESH|true|false" run_sub 100)"

# --- summary ---------------------------------------------------------------
echo
echo "pr-ready tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
