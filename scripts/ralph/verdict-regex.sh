#!/usr/bin/env bash
# scripts/ralph/verdict-regex.sh
#
# Single source of truth for the review-verdict regex constants. Sourced by
# BOTH pr-ready.sh (the merge gate — "is there a FRESH LGTM?") and
# assert-review-posted.sh (the post gate — "did the review agent post a verdict
# at all during this run?"). Keeping the two checks byte-identical is the whole
# point: a drift between the posting check and the merging check is exactly the
# class of silent-stall bug #135 fixes — a verdict the poster considers valid
# but the merger silently ignores (or vice versa) starves the lane forever.
#
# NOTE: this is a sourced fragment, not a standalone script. It deliberately
# does NOT set `set -euo pipefail` — it is sourced into callers that already do,
# and re-arming shell options in a fragment would clobber the caller's state.
# It only declares constants.
#
# Idempotent: if these constants are already defined in this shell, return early.
# The `readonly` declarations below would otherwise abort (and, under a caller's
# `set -e`, kill the whole script) if this fragment were sourced twice into one
# process — e.g. a future harness that sources both pr-ready.sh and
# assert-review-posted.sh. Today each consumer is its own process, so this is
# pure future-proofing.
[[ -n "${VERDICT_PREFIX_RE:-}" ]] && return 0
#
# The canonical verdict line `claude-code-review.yml` posts is
# `## Verdict: <LGTM|CHANGES_REQUESTED|COMMENTS>` (also tolerated: `**Verdict:**`
# and a bare `Verdict:`), sitting at the END of a longer `## Summary …` body — so
# the match must be case-insensitive AND multiline (`m`, so `^` anchors to the
# verdict line — which sits at the END of a multi-line `## Summary …` body, not
# at string start), prefix-tolerant, and keyed to the verdict LINE (a stray
# "LGTM" in prose must not count). Reviewers are also seen posting `## Verdict`
# as a bare heading with an emoji-prefixed token on the NEXT line (e.g.
# `## Verdict\n✅ LGTM`), so the LGTM separator tolerates any non-alphanumeric
# decoration (emoji/whitespace/newline) between `verdict` and `lgtm`. That stays
# safe: a stray "LGTM" in prose never matches (it is keyed to the verdict line),
# and a non-LGTM token like COMMENTS/CHANGES_REQUESTED puts a letter right after
# the emoji, breaking the non-alphanumeric run before any later "LGTM".
# `VERDICT_COMMENTS_RE` is the exact mirror of `VERDICT_LGTM_RE` for the
# `COMMENTS` token: same emoji/newline-tolerant `[^a-zA-Z0-9]+` separator, so
# `## Verdict\n💬 COMMENTS`, `## Verdict: 💬 COMMENTS`, `## Verdict: COMMENTS`,
# and `**Verdict:** COMMENTS` all match, and the same prose-guard property holds
# — the run stops at the first alphanumeric char, so a stray "comments" word in
# later prose (after an LGTM/CHANGES_REQUESTED line) never matches. The two
# token matchers widen; `VERDICT_RE` (comment selection) keeps its strict
# `[:*\s]` class unchanged. Backslashes are doubled because this text is spliced
# into a jq string literal, where `\s` is an invalid escape and must reach the
# regex engine as `\\s` (the negated class `[^a-zA-Z0-9]` has no backslash, so
# it is spelled literally — the explicit form self-documents and sidesteps the
# Oniguruma subtlety of case-folding a negated class). The per-branch fragments
# are SINGLE-quoted (not folded into the surrounding double quotes) so their
# `\\s` survives verbatim: inside double quotes bash would collapse `\\s` → `\s`,
# which jq then rejects as an invalid escape — the class must stay `[:*\\s]`.
# VERDICT_RE, VERDICT_LGTM_RE, and VERDICT_COMMENTS_RE are consumed by the
# scripts that source this fragment (pr-ready.sh uses all three;
# assert-review-posted.sh uses VERDICT_RE), so a standalone shellcheck of this
# file cannot see their use — silence SC2034.
readonly VERDICT_PREFIX_RE='(?im)^\\s*(?:#{1,6}\\s+|\\*\\*)?verdict'
# shellcheck disable=SC2034
readonly VERDICT_RE="${VERDICT_PREFIX_RE}"'[:*\\s]'
# shellcheck disable=SC2034
readonly VERDICT_LGTM_RE="${VERDICT_PREFIX_RE}"'[^a-zA-Z0-9]+lgtm'
# shellcheck disable=SC2034
readonly VERDICT_COMMENTS_RE="${VERDICT_PREFIX_RE}"'[^a-zA-Z0-9]+comments'
