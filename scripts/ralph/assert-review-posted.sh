#!/usr/bin/env bash
# scripts/ralph/assert-review-posted.sh
#
# The LOUD post-gate counterpart to pr-ready.sh's silent freshness guard. The
# orchestrator runs this AFTER dispatching claude-code-action's review step, to
# prove a verdict comment actually landed on the PR during THIS run.
#
# WHY THIS EXISTS:
#   #135 (silent-success stall): the review action can finish "successfully"
#     without ever posting a verdict — the workflow step goes green, pr-ready.sh
#     then reports `awaiting-review` forever (no fresh verdict), and the lane
#     stalls with nothing loud to point at. This script turns that silent hole
#     into a failing step with an explicit "rerun" message.
#   #140 (instant-error incident): the review action can fail INSTANTLY (e.g. an
#     expired OAuth token / credit-balance error) while the step STILL reports
#     success — starving the PR of a verdict with zero diagnostics. When handed
#     the action's --execution-file we read its own is_error flag and fail fast
#     with the agent's error text, independent of whatever is on the PR.
#
# Two invariants, in order:
#   STEP A (defensive, best-effort): if the execution file says is_error → fail
#     LOUD immediately with the agent's error text. Never crashes on a
#     missing/unreadable/malformed file or absent jq — it just falls through.
#   STEP B (authoritative): assert a verdict-bearing comment (same VERDICT_RE
#     pr-ready.sh selects on) exists with createdAt >= STARTED_AT, so a stale
#     verdict from a PREVIOUS run cannot paper over a broken current one.
#
# Usage:  assert-review-posted.sh <PR_NUMBER> <STARTED_AT> \
#           [--repo <owner/repo>] [--execution-file <path>]
set -euo pipefail

# Shared verdict regex — the single source of truth also sourced by pr-ready.sh
# (the merge gate). Resolve relative to THIS script so the check is
# cwd-independent. Provides VERDICT_RE (the one we need here).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/ralph/verdict-regex.sh
# shellcheck disable=SC1091  # sourced at runtime; not followed without -x
source "$SCRIPT_DIR/verdict-regex.sh"

die() { echo "assert-review-posted: $1" >&2; exit 2; }

pr=""
started_at=""
execution_file=""
repo_args=()
positional_seen=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)           [[ $# -ge 2 ]] || die "--repo needs a value"; repo_args+=(--repo "$2"); shift 2 ;;
    --execution-file) [[ $# -ge 2 ]] || die "--execution-file needs a value"; execution_file="$2"; shift 2 ;;
    -*)               die "unknown option: $1" ;;
    *)
      if [[ "$positional_seen" -eq 0 ]]; then
        pr="$1"; positional_seen=1
      elif [[ "$positional_seen" -eq 1 ]]; then
        started_at="$1"; positional_seen=2
      else
        die "unexpected extra argument: $1"
      fi
      shift ;;
  esac
done
[[ "$pr" =~ ^[0-9]+$ ]] || die "usage: assert-review-posted.sh <PR_NUMBER> <STARTED_AT> [--repo <owner/repo>] [--execution-file <path>]"
[[ -n "$started_at" ]]  || die "usage: assert-review-posted.sh <PR_NUMBER> <STARTED_AT> [--repo <owner/repo>] [--execution-file <path>]"

# `${arr[@]+"${arr[@]}"}` expands to nothing when the array is empty instead of
# tripping `set -u` on bash 3.2 (stock /bin/bash on macOS).
gh_args=("$pr" ${repo_args[@]+"${repo_args[@]}"})

# --- STEP A: execution-file is_error fast-fail (#140 diagnosability) ----------
# Best-effort ONLY. An empty --execution-file value means "not provided". If the
# file is missing/unreadable/malformed or jq is absent we say nothing and let
# Step B stay authoritative — this path can only ADD a loud failure, never
# suppress the comment check. Every jq call is guarded so a garbage file can
# never crash the script.
if [[ -n "$execution_file" && -r "$execution_file" ]] && command -v jq >/dev/null 2>&1; then
  is_err="$(jq -r '[.[]? | select(.type == "result")] | last | .is_error // empty' "$execution_file" 2>/dev/null || true)"
  if [[ "$is_err" == "true" ]]; then
    detail="$(jq -r '[.[]? | select(.type == "result")] | last | (.result // .subtype // "unknown error")' "$execution_file" 2>/dev/null || true)"
    [[ -n "$detail" ]] || detail="unknown error"
    echo "assert-review-posted: review agent errored: $detail" >&2
    exit 1
  fi
fi

# --- STEP B: authoritative fresh-verdict-comment assertion --------------------
# Count PR comments whose body matches the shared verdict regex AND whose
# createdAt is at-or-after STARTED_AT. RFC3339 UTC timestamps are fixed-width,
# so the `>=` compare is done as a LEXICAL string compare INSIDE jq (portable,
# no date arithmetic). The compare is `>=` (inclusive — a comment posted in the
# same second as the run start counts), deliberately LOOSER than pr-ready.sh's
# strict `>` against HEAD: different invariant ("posted during THIS run" vs
# "postdates HEAD"), same lexical-RFC3339 technique.
#
# STARTED_AT is spliced into the jq program as a quoted JSON string literal, the
# same way VERDICT_RE is interpolated in pr-ready.sh's verdict query: `gh --jq`
# runs its expression server-side and (unlike standalone jq) exposes NO `--arg`
# flag, so
# interpolation is the only channel. Safe here — STARTED_AT is a fixed-width
# RFC3339 UTC timestamp (`date -u +%Y-%m-%dT%H:%M:%SZ`) with no quote characters
# to break out of the literal.
count="$(gh pr view "${gh_args[@]}" \
  --json comments \
  --jq "[.comments[] | select(.body != null and (.body | test(\"$VERDICT_RE\")) and (.createdAt != null) and (.createdAt >= \"$started_at\"))] | length")"

if [[ "$count" =~ ^[0-9]+$ ]] && [[ "$count" -ge 1 ]]; then
  exit 0
fi

echo "assert-review-posted: no verdict-bearing comment created at/after ${started_at} — the review agent posted no verdict; rerun the Code Review workflow" >&2
exit 1
