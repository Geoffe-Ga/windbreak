#!/usr/bin/env bash
# scripts/ralph/pr-ready.sh
#
# Authoritative "is this lane safe to merge?" check for the Ralph orchestrator
# (ralph-tick.md Step 1). Prints exactly one status token and exits 0 (it is a
# query — a non-zero exit means a usage/tooling error, never a PR verdict):
#
#   ready             LGTM (fresh) + CI green + up-to-date with main  → merge now
#   behind            LGTM (fresh) + CI green but mergeStateStatus BEHIND → sync first
#   comments          COMMENTS (fresh) + CI green + CLEAN → address feedback, then merge
#   comments-behind   COMMENTS (fresh) + CI green but BEHIND → address feedback + sync
#   changes-requested a FRESH verdict-bearing comment that is neither LGTM nor
#                     COMMENTS (CHANGES_REQUESTED / unknown, by elimination) → Step 2
#   pending           CI still running → wait for a later wake
#   ci-failed         CI has a failing/errored check → Step 2 (ci-debugging)
#   awaiting-review   no fresh verdict at all (missing verdict, or stale — posted
#                     at-or-before HEAD) → wait for the reviewer
#
# WHY THIS EXISTS: the previous all-lanes Monitor grepped `gh pr checks` output
# for ': pending'. That output is TAB-delimited (name<TAB>pending<TAB>...), so the
# grep never matched and a still-running CI was read as settled — a false READY
# that could merge a PR with pending/failing checks. CI state here is keyed off
# the `gh pr checks` EXIT CODE, which is authoritative: 0 = all passed, 8 = some
# pending, anything else = failure. No text parsing of the checks table at all.
#
# Stale-verdict guard: a review verdict only counts when it was posted AFTER the
# PR's HEAD commit. An LGTM from before the latest push is stale (it reviewed
# older code) and must not gate a merge.
#
# Usage:  pr-ready.sh <PR_NUMBER> [--repo <owner/repo>]
set -euo pipefail

# `gh pr checks` exit code that means "checks still pending" (gh's documented
# contract: 0 = pass, 8 = pending, other = failure).
readonly CHECKS_PENDING_EC=8

die() { echo "pr-ready: $1" >&2; exit 2; }

pr=""
repo_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) [[ $# -ge 2 ]] || die "--repo needs a value"; repo_args+=(--repo "$2"); shift 2 ;;
    -*)     die "unknown option: $1" ;;
    *)      [[ -z "$pr" ]] || die "unexpected extra argument: $1"; pr="$1"; shift ;;
  esac
done
[[ "$pr" =~ ^[0-9]+$ ]] || die "usage: pr-ready.sh <PR_NUMBER> [--repo <owner/repo>]"

# The verdict-regex constants (VERDICT_PREFIX_RE/VERDICT_RE/VERDICT_LGTM_RE/
# VERDICT_COMMENTS_RE) live in verdict-regex.sh — the single source of truth
# shared with the post gate
# (assert-review-posted.sh). Resolve it relative to THIS script (not cwd) so the
# check stays cwd-independent. See that file for the full escaping commentary.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ralph/verdict-regex.sh
# shellcheck disable=SC1091  # sourced at runtime; not followed without -x
source "$SCRIPT_DIR/verdict-regex.sh"

# `${arr[@]+"${arr[@]}"}` expands to nothing when the array is empty instead of
# tripping `set -u` on bash 3.2 (stock /bin/bash on macOS).
gh_args=("$pr" ${repo_args[@]+"${repo_args[@]}"})

# --- CI state from the exit code, not the text table -----------------------
ci_ec=0
gh pr checks "${gh_args[@]}" >/dev/null 2>&1 || ci_ec=$?
if [[ "$ci_ec" -eq "$CHECKS_PENDING_EC" ]]; then
  echo "pending"; exit 0
elif [[ "$ci_ec" -ne 0 ]]; then
  echo "ci-failed"; exit 0
fi

# --- CI is green: check mergeability + a FRESH verdict ----------------------
# One call yields "<mergeStateStatus>|<HEAD committedDate>", another the latest
# top-level verdict as "<createdAt>|<isLGTM>|<isCOMMENTS>". gh applies --jq
# server-side. The verdict scalar carries three fields so the classifier can
# distinguish LGTM, COMMENTS, and (by elimination) CHANGES_REQUESTED/unknown
# from a single query — the same selected verdict comment is tested against both
# the LGTM and COMMENTS matchers.
merge_line="$(gh pr view "${gh_args[@]}" \
  --json mergeStateStatus,commits \
  --jq '(.mergeStateStatus // "") + "|" + (.commits[-1].committedDate // "")')"
merge_state="${merge_line%%|*}"
head_date="${merge_line#*|}"

verdict_line="$(gh pr view "${gh_args[@]}" \
  --json comments \
  --jq "([.comments[] | select(.body != null and (.body | test(\"$VERDICT_RE\")))] | last) as \$v
        | ((\$v.createdAt // \"\")
           + \"|\" + ((\$v.body // \"\" | test(\"$VERDICT_LGTM_RE\")) | tostring)
           + \"|\" + ((\$v.body // \"\" | test(\"$VERDICT_COMMENTS_RE\")) | tostring))")"
# Split the 3-field "<createdAt>|<isLGTM>|<isCOMMENTS>" scalar (bash-3.2 safe —
# parameter expansion only). The middle field needs the two-step peel below.
verdict_date="${verdict_line%%|*}"
rest="${verdict_line#*|}"
verdict_lgtm="${rest%%|*}"
verdict_comments="${rest##*|}"

# Without a HEAD commit time we cannot prove the verdict is fresh — fail closed.
if [[ -z "$head_date" ]]; then
  echo "awaiting-review"; exit 0
fi

# A verdict is FRESH ⇔ a verdict comment was selected (verdict_date non-empty)
# AND its createdAt is strictly newer than the HEAD commit. RFC3339 UTC
# timestamps are fixed-width, so a lexical string compare is a correct
# chronological compare (portable — no date arithmetic).
#
# When there is no fresh verdict we split on WHY, because the two cases mean
# different things but share the awaiting-review outcome:
#   - verdict_date empty        → no verdict-bearing comment at all → wait.
#   - stale (≤ HEAD) LGTM/COMMENTS → reviewed older code → wait for a re-review.
# Either way: awaiting-review. A fresh verdict that is neither LGTM nor COMMENTS
# is a CHANGES_REQUESTED/unknown verdict, derived by elimination, and is NOT the
# same as "no verdict" — it gets its own changes-requested token.
if [[ -z "$verdict_date" ]] || ! [[ "$verdict_date" > "$head_date" ]]; then
  echo "awaiting-review"; exit 0
fi

# Fresh verdict present. Classify by matched token; mergeStateStatus splits the
# mergeable (CLEAN) from the sync-first (BEHIND/other) outcome. LGTM takes
# precedence over COMMENTS, and anything else is CHANGES_REQUESTED/unknown.
clean=false
[[ "$merge_state" == "CLEAN" ]] && clean=true
if [[ "$verdict_lgtm" == "true" ]]; then
  if [[ "$clean" == "true" ]]; then echo "ready"; else echo "behind"; fi
elif [[ "$verdict_comments" == "true" ]]; then
  if [[ "$clean" == "true" ]]; then echo "comments"; else echo "comments-behind"; fi
else
  echo "changes-requested"
fi
