#!/usr/bin/env bash
# scripts/ralph/queue-depth.sh
#
# Prints the true pickable-issue count the Ralph picker sees: the number of
# ELIGIBLE open issues (per the shared require/exclude label rules in
# eligibility.sh) that are NOT already covered by an in-flight open PR.
#
# This runs on a BARE GitHub Actions runner (the hopper workflow), which never
# has a checkout of another job's `.ralph/worktrees` lanes. So — unlike
# pick-next.sh, which also excludes live local worktrees — "in-flight" here is
# approximated by open-PR `Closes|Fixes|Resolves #N` markers ONLY. This is a
# deliberate, documented approximation: the helper intentionally does no
# worktree/git-repo detection and must run fine outside any git repository.
#
# Output: a single integer on stdout (0 for an empty/drained backlog), so the
# hopper can do numeric comparisons on it directly.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ralph/eligibility.sh
# shellcheck disable=SC1091  # sourced at runtime; not followed without -x
source "$SCRIPT_DIR/eligibility.sh"

REQUIRE_LABELS="${RALPH_REQUIRE_LABELS:-$RALPH_DEFAULT_REQUIRE_LABELS}"
EXCLUDE_LABELS="${RALPH_EXCLUDE_LABELS:-$RALPH_DEFAULT_EXCLUDE_LABELS}"

# jq array literals from the space-separated env vars. Intentionally unquoted:
# word-splitting on IFS whitespace turns "a b c" into three printf lines, one
# label per jq array element (identical idiom to pick-next.sh).
# shellcheck disable=SC2086
require_json=$(printf '%s\n' $REQUIRE_LABELS | jq -R . | jq -s .)
# shellcheck disable=SC2086
exclude_json=$(printf '%s\n' $EXCLUDE_LABELS | jq -R . | jq -s .)

# Eligible open issue numbers, via the SHARED eligibility filter (identical
# require/exclude math to the picker's open_tsv).
eligible=$(
  gh issue list \
    --state open \
    --limit 300 \
    --json number,labels \
    --jq "$(ralph_eligibility_filter "$require_json" "$exclude_json") | .[].number"
)

# Issue numbers already in flight via an open PR (case-insensitive markers).
inflight=$(
  gh pr list \
    --state open \
    --limit 300 \
    --json body \
    --jq '.[].body' \
  | grep -oiE '(closes|fixes|resolves)[[:space:]]+#[0-9]+' \
  | grep -oE '[0-9]+' \
  | sort -u || true
)

# Set difference: count eligible numbers NOT in the in-flight set. A PR closing
# an issue OUTSIDE the eligible set must not decrement the count, so we walk the
# eligible list and test membership rather than subtracting cardinalities.
count=0
while IFS= read -r num; do
  [[ -n "$num" ]] || continue
  if [[ -n "$inflight" ]] && grep -qx "$num" <<<"$inflight"; then
    continue
  fi
  count=$((count + 1))
done <<<"$eligible"

echo "$count"
