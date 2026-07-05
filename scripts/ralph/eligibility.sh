#!/usr/bin/env bash
# scripts/ralph/eligibility.sh
#
# SINGLE SOURCE OF TRUTH for the Ralph picker's label vocabulary AND the shared
# jq eligibility filter. This file is SOURCED (never executed) by both
# pick-next.sh (the picker) and queue-depth.sh (the hopper's backlog counter),
# so the two agree byte-for-byte on what "an eligible open issue" means: an issue
# carrying ALL required labels and NONE of the excluded ones.
#
# Because it is sourced, it must be side-effect-free: it defines variables and a
# function only, runs no top-level command, and deliberately sets no shell
# options (no `set -e`) so it cannot alter the sourcing script's shell state.
#
# The default exclude-list string below is defined HERE and nowhere else in
# scripts/ralph (a single-source test enforces that); consumers read it via the
# RALPH_DEFAULT_* variables rather than re-embedding the literal.

# Labels an issue MUST all carry to be eligible. Empty = no required label.
# Exported (not merely assigned) so shellcheck sees them as consumed by the
# sourcing scripts, which it cannot otherwise infer for a sourced library.
export RALPH_DEFAULT_REQUIRE_LABELS=""

# Labels that DISQUALIFY an issue: housekeeping / deferred / in-flight markers.
export RALPH_DEFAULT_EXCLUDE_LABELS="epic wontfix duplicate invalid question blocked needs-spec future-work do-not-auto-merge in-progress"

# ralph_eligibility_filter <require-jq-array> <exclude-jq-array>
#
# Emit a jq program that filters an array of `gh issue list --json number,labels`
# objects down to the eligible ones, returning the raw `{number,labels}` objects
# (callers append their own projection/sort/format). Pass the two jq array
# literals (as produced by `printf '%s\n' $VAR | jq -R . | jq -s .`) for the
# require and exclude label sets; they are substituted verbatim into the program.
ralph_eligibility_filter() {
  cat <<EOF
( $1 | map(select(length>0)) ) as \$req
| ( $2 | map(select(length>0)) ) as \$exc
| map(. as \$i | (\$i.labels | map(.name)) as \$names
    | select(
        ( \$req | all(. as \$r | \$names | index(\$r)) )
        and ( \$exc | any(. as \$x | \$names | index(\$x)) | not )
      ))
EOF
}
