#!/usr/bin/env bash
# scripts/ralph/should-retry-review.sh
#
# WHY THIS EXISTS (issue #152):
#   claude-code-action's review step intermittently finishes "successfully"
#   without ever posting its verdict comment (the #135/#140 flake class). A
#   single bounded in-job retry self-heals that transient miss before
#   assert-review-posted.sh — the authoritative backstop — fails the job red on
#   a SECOND miss. This script is the decision wrapper the workflow consults
#   between the two attempts: it reuses the REAL assert backstop (no
#   re-implemented verdict logic) and translates its exit-code contract into a
#   machine-readable `retry=true|false` + `reason=<...>` decision.
#
#   It is purely ADVISORY: it exits 0 for ANY decided outcome (retry=true OR
#   retry=false) so it can never fail the calling job. Job success/failure is
#   owned by the retry gating and the FINAL assert step, not by this step. Only
#   a usage error propagates a nonzero (2) exit.
#
# Usage:  should-retry-review.sh <PR_NUMBER> <STARTED_AT> \
#           [--repo <owner/repo>] [--execution-file <path>]
#
# The CLI signature is IDENTICAL to assert-review-posted.sh: every arg is passed
# straight through, and the assert's own arg validation (its exit 2) is the
# single source of truth for what a usage error is — no duplicated parsing here.
# The assert's stderr flows through untouched, so the workflow logs stay
# diagnostic (a second miss still surfaces its specific message via the final
# assert step).
#
# The decision is APPENDED to the file named by $GITHUB_OUTPUT (the runner's
# step-output channel) as two lines:
#     retry=true|false
#     reason=<verdict-posted|no-verdict-retryable|hard-failure|unknown>
# When $GITHUB_OUTPUT is unset/empty (offline tests), the same two lines are
# printed to stdout instead. On a usage error NOTHING is written to either.
set -euo pipefail

# Resolve the sibling assert script cwd-independently (the verdict-regex.sh
# sourcing pattern), so the decision reflects the REAL production backstop no
# matter where the workflow invokes us from.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ASSERT="$SCRIPT_DIR/assert-review-posted.sh"

# Invoke the REAL backstop with the SAME args; capture its exit code without
# tripping `set -e`. Its stderr is intentionally NOT swallowed.
rc=0
bash "$ASSERT" "$@" || rc=$?

# Map the assert's exit-code contract (documented in its header) to a decision.
# A usage error (2) is propagated as-is BEFORE writing any output, so a
# malformed call leaves $GITHUB_OUTPUT untouched.
case "$rc" in
  2) exit 2 ;;                                       # usage error — propagate, write nothing
  0) retry=false; reason=verdict-posted ;;           # fresh verdict already posted
  3) retry=true;  reason=no-verdict-retryable ;;     # transient miss — one retry may help
  1) retry=false; reason=hard-failure ;;             # is_error or workflow-validation guard
  *) retry=false; reason=unknown ;;                  # defensive: never retry the unknown
esac

# Emit the decision: append to $GITHUB_OUTPUT when set, else stdout fallback.
if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    printf 'retry=%s\n' "$retry"
    printf 'reason=%s\n' "$reason"
  } >> "$GITHUB_OUTPUT"
else
  printf 'retry=%s\n' "$retry"
  printf 'reason=%s\n' "$reason"
fi

# Advisory only — a decided outcome never fails the job.
exit 0
