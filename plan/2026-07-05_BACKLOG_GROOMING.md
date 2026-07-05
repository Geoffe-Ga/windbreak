# Backlog Grooming — 2026-07-05 (automated, Ralph groom gate)

## Scope
Triggered by groom gate (13 completions since last groom). Reviewed the 13 merged Ralph PRs (#63–#87) and all open issues.

## Findings & actions
- **Issue↔PR hygiene: clean.** Every merged PR carried `Closes #N`; all 13 issues verified closed at merge time by the orchestrator (10, 11, 12, 13, 14, 15, 16, 17, 22, 23, 62, 66, 83). #61 closed via PR #63's second commit (`Fixes #61`).
- **Epic #2 (Foundations M0) closed** — all six children merged.
- **Blocked labels verified accurate:** #19 (needs #18, in flight), #20 (needs #19), #21 (needs #20). Will be lifted by the orchestrator as predecessors merge.
- **No missing issues found**: every gap surfaced during the run was filed at the time (#64, #65, #68, #74–#81, #83).
- **Duplicate check:** #64 (lockfile/reproducibility) vs #80 (dev-dep single source of truth) overlap but are distinct and cross-referenced; both kept.

## Stats
- PRs analyzed: 13 (Ralph) + 1 pre-existing (#1)
- Issues closed this grooming: 1 (epic #2; the 13 work issues were closed at merge time)
- Issues created: 0 (all gaps already tracked)
- Backlog health: 45 open issues — 3 blocked (accurate), 8 epics → 7 after closing #2, remainder are sequenced spec-decomposition work.

---

# Second pass — 2026-07-05 (evening)

Grooming pass per `.claude/skills/backlog-grooming/SKILL.md`. Scope: last 20
merged PRs (#85–#118), open-issue hygiene (stale blocks, duplicates), gap
detection. gh-only; no code changes.

## PRs Analyzed (20)

#85→#17, #86→#22, #87→#23, #88→#25, #89→#18, #90→#24, #92→#26, #94→#28,
#95→#19, #96→#29, #97→#27, #99→#30, #102→#20, #103→#31, #105→#21, #108→#104,
#111→#32, #112→#107, #116→#113, #118→#33.

GitHub auto-close resolved every referenced issue — zero stragglers to close
manually.

## Actions Taken

1. **Closed #117 as duplicate of #122.** Same defect (quality scripts never
   implement the `--metrics` JSON contract; dashboard tiles degrade to
   null/N/A). #122 is canonical: verified root cause, per-script deliverables
   table, labels (`bug,polish,P3`), and the #107 mutation-tile constraint.
   #117 was the earlier, thinner note from PR #116's verification. No open PR
   was working #117.
2. **Progress comment on epic #4 (Forecast Engine, M2).** All 7 child issues
   (#22–#28) are closed, but the epic's done-gate exceeds the child list
   (>=50 auditable forecasts end-to-end, epic-surface smoke tests) and
   follow-ups #91, #93, #98, #101 remain open — left open for owner decision.

## Checks With No Action Needed

- **Blocked issues:** only #36 carries `blocked` (on #34 and #35). Both
  blockers are still OPEN — label retained.
- **Gaps (merged work without tracking issues):** none. Every merged PR closed
  a tracked issue, and follow-up work identified in PR bodies already has
  issues (#110, #114, #106, #109, #120, #121, #122, #123, #64, #68, #80).
- **Other duplicate candidates reviewed and kept distinct:** #64 vs #80
  (runtime lockfile vs dev-manifest single-sourcing — PR #108 explicitly left
  both open); #120 vs #121 (deslop path config vs shellcheck/actionlint);
  #75 vs #76 (ledger hash-chain anchoring vs append rollback).
- **Epic #5 (Risk Kernel):** children #29–#33 closed via PRs
  #96/#99/#103/#111/#118, but #34, #35, #36 still open — epic correctly open.
- **Epic #3 (Connector M1):** already closed; children #17–#21 all merged.
- **Untouched per instruction:** issues #34, #35, #37, #115 (active lanes) and
  PR #119.

## Stats (second pass)

- PRs analyzed: 20; issues closed: 1 (#117, duplicate); issues created: 0;
  labels changed: 0; comments posted: 2 (close rationale on #117, epic-status
  on #4).
- Open issues: 59 → 58. Auto-close discipline is working; follow-ups are
  consistently filed at PR time.
