#!/usr/bin/env bash
# scripts/ralph/test_provision_venv.sh
#
# Offline tests for scripts/provision-venv.sh (issue #133) — the script that
# provisions/refreshes a SINGLE SHARED repo-local .venv at the MAIN repo root,
# reused by every fleet worktree, so check-all.sh doesn't reinstall the
# quality toolchain in every lane.
#
# Contract under test:
#   --print-venv   print the resolved shared venv absolute path, exit 0.
#   --check        drift check only (no install); exit 0 if the installed
#                  toolchain matches constraints-quality.txt; nonzero (with a
#                  hint containing the literal "provision-venv.sh") if
#                  drifted/missing.
#   --help         usage, exit 0.
#   <unknown flag> error to stderr, exit 2 (matches check-all.sh/lint.sh).
#   (no flag)      provision: create .venv via `python3 -m venv` if absent,
#                  drift-check, and ONLY pip-install when fresh/drifted
#                  (idempotent — a clean venv exits 0 without touching pip).
#                  Install NEVER passes `-e` (shared venv must not bind to one
#                  lane's checkout).
#   venv location  resolves to the MAIN repo root .venv even from inside a
#                  worktree, via `git rev-parse --path-format=absolute
#                  --git-common-dir`; falls back to "<script's own
#                  PROJECT_ROOT>/.venv" outside a git repo.
#
# OFFLINE: no real pip, no network. We stub an ambient `python3` on PATH whose
# `-m venv <dir>` creates `<dir>/bin/python` — itself a stub whose `pip list`
# / `pip install` behavior is driven by env vars a scenario exports
# (FREEZE_FILE = the venv's simulated `pip list --format=freeze` output;
# PIP_LOG = where "pip install" invocations get appended), so tests can assert
# exactly what was/wasn't passed to pip without ever touching the network.
#
# Run:  bash scripts/ralph/test_provision_venv.sh
set -euo pipefail

RALPH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPTS_DIR="$(dirname "$RALPH_DIR")"
SELF="$RALPH_DIR/$(basename "${BASH_SOURCE[0]}")"
PV="$SCRIPTS_DIR/provision-venv.sh"
CHECK_ALL="$SCRIPTS_DIR/check-all.sh"
PROMPT_MD="$RALPH_DIR/PROMPT.md"

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
BIN="$WORK/bin"; mkdir -p "$BIN"

# --- shared fake venv-python template -----------------------------------------
# Written once, then either copied straight into a pre-fabricated ".venv/bin/"
# (idempotent/drift scenarios) or copied by the fake ambient python3's
# "-m venv" handler (fresh-provision scenarios) — one definition, two callers.
VENV_PY_TEMPLATE="$WORK/venv_python_template.sh"
cat > "$VENV_PY_TEMPLATE" <<'INNER'
#!/usr/bin/env bash
# Fake venv python: drives pip behavior from env vars the scenario exported.
if [[ "$1" == "-m" && "$2" == "pip" ]]; then
  shift 2
  case "$1" in
    list)
      cat "${FREEZE_FILE:-/dev/null}" 2>/dev/null || true
      ;;
    install)
      shift
      printf '%s\n' "$*" >> "${PIP_LOG:?PIP_LOG not set}"
      # Optional concurrency instrumentation (set only by lock/race scenarios):
      # BEGIN/END markers bracket a (deliberately slow) install so a test can
      # detect whether two concurrent installs overlapped or were serialized.
      if [[ -n "${INSTALL_TRACE:-}" ]]; then
        printf 'BEGIN %s\n' "$$" >> "$INSTALL_TRACE"
        sleep "${INSTALL_SLEEP:-0}"
        printf 'END %s\n' "$$" >> "$INSTALL_TRACE"
      fi
      # Simulate pip resolving the env to a post-install freeze state, so a
      # sibling lane's re-check-under-lock sees the drift as already healed.
      if [[ -n "${INSTALL_RESULT_FREEZE:-}" ]]; then
        cat "$INSTALL_RESULT_FREEZE" > "${FREEZE_FILE:?FREEZE_FILE not set}"
      fi
      # Simulate an install failure (exercises lock release on error).
      if [[ -n "${INSTALL_FAIL:-}" ]]; then
        exit 1
      fi
      ;;
    *) : ;;
  esac
fi
INNER
chmod +x "$VENV_PY_TEMPLATE"

make_fixture_venv() { # <venv-dir> — a pre-existing, already-provisioned venv
  mkdir -p "$1/bin"
  cp "$VENV_PY_TEMPLATE" "$1/bin/python"
}

# --- fake ambient python3: only its "-m venv" path is exercised (fresh -------
# provision scenarios); a stray "-m pip" against the ambient interpreter would
# violate the contract (must use "$VENV/bin/python -m pip"), so it is recorded
# too, distinctly, so a violation would be visible if ever asserted.
cat > "$BIN/python3" <<STUB
#!/usr/bin/env bash
if [[ "\$1" == "-m" && "\$2" == "venv" ]]; then
  dir="\$3"
  mkdir -p "\$dir/bin"
  cp "$VENV_PY_TEMPLATE" "\$dir/bin/python"
  chmod +x "\$dir/bin/python"
  exit 0
elif [[ "\$1" == "-m" && "\$2" == "pip" ]]; then
  shift 2
  printf 'AMBIENT_PIP_CALL %s\n' "\$*" >> "\${PIP_LOG:-/dev/null}"
fi
STUB
chmod +x "$BIN/python3"

# --- fixture project builder (behavior scenarios 3-11; no git involved, so ---
# the fallback "<PROJECT_ROOT>/.venv" resolution is what's exercised) --------
new_fixture_proj() { # <name> -> sets FIXTURE, FPV (copy of $PV if it exists)
  FIXTURE="$WORK/proj.$1"
  rm -rf "$FIXTURE"
  mkdir -p "$FIXTURE/scripts"
  FPV="$FIXTURE/scripts/provision-venv.sh"
  if [[ -f "$PV" ]]; then
    cp "$PV" "$FPV"
    chmod +x "$FPV"
  fi
  : > "$FIXTURE/requirements.txt"
  : > "$FIXTURE/requirements-dev.txt"
}

pip_log_has() { # <substring> <pip-args>
  [[ "$2" == *"$1"* ]] && echo yes || echo no
}
pip_log_has_flag() { # <bare-flag e.g. -e> <pip-args>
  printf '%s' "$2" | grep -Eq "(^| )$1( |\$)" && echo yes || echo no
}

# ===============================================================================
# 1-2) git resolution: --print-venv resolves to the MAIN repo root .venv, both
#      from the main tree AND from a `git worktree add`ed worktree.
# ===============================================================================
MAINREPO="$WORK/mainrepo"
mkdir -p "$MAINREPO/scripts"
if [[ -f "$PV" ]]; then
  cp "$PV" "$MAINREPO/scripts/provision-venv.sh"
  chmod +x "$MAINREPO/scripts/provision-venv.sh"
fi
git -C "$MAINREPO" init -q -b main
git -C "$MAINREPO" config user.email t@example.com
git -C "$MAINREPO" config user.name t
: > "$MAINREPO/README.md"
git -C "$MAINREPO" add -A
git -C "$MAINREPO" commit -q -m init
MAINREPO_REAL="$(cd "$MAINREPO" && pwd -P)"

out=$( (cd "$MAINREPO" && PATH="$BIN:$PATH" bash "$MAINREPO/scripts/provision-venv.sh" --print-venv) 2>&1 ) && rc=0 || rc=$?
check "--print-venv from the main repo resolves to <mainroot>/.venv" \
  "$MAINREPO_REAL/.venv" "$out"
check "--print-venv from the main repo exits 0" "0" "$rc"

WT="$WORK/worktree1"
git -C "$MAINREPO" worktree add -q -b wt/fixture-1 "$WT"
out2=$( (cd "$WT" && PATH="$BIN:$PATH" bash "$WT/scripts/provision-venv.sh" --print-venv) 2>&1 ) && rc2=0 || rc2=$?
check "--print-venv from a worktree ALSO resolves to <mainroot>/.venv (shared)" \
  "$MAINREPO_REAL/.venv" "$out2"
check "--print-venv from a worktree exits 0" "0" "$rc2"

# ===============================================================================
# 3-4) Fresh provision: venv created via `python3 -m venv`; install args are
#      exactly -c/-r/-r, NEVER -e.
# ===============================================================================
new_fixture_proj fresh
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
bar==2.0
EOF
PIP_LOG="$WORK/pip.fresh.log"; : > "$PIP_LOG"
FREEZE_FILE="$WORK/freeze.fresh.txt"; : > "$FREEZE_FILE"   # brand-new venv: nothing installed
export PIP_LOG FREEZE_FILE

out=$(PATH="$BIN:$PATH" bash "$FPV" 2>&1) && rc=0 || rc=$?
check "fresh provision exits 0" "0" "$rc"
check "fresh provision creates .venv/bin/python" "yes" \
  "$( [[ -x "$FIXTURE/.venv/bin/python" ]] && echo yes || echo no )"

pip_args="$(cat "$PIP_LOG" 2>/dev/null)" || true
check "fresh provision passes -c constraints-quality.txt to pip install" \
  "yes" "$(pip_log_has '-c constraints-quality.txt' "$pip_args")"
check "fresh provision passes -r requirements.txt to pip install" \
  "yes" "$(pip_log_has '-r requirements.txt' "$pip_args")"
check "fresh provision passes -r requirements-dev.txt to pip install" \
  "yes" "$(pip_log_has '-r requirements-dev.txt' "$pip_args")"
check "fresh provision NEVER installs editable (-e absent)" \
  "no" "$(pip_log_has_flag -e "$pip_args")"

# ===============================================================================
# 5) Idempotent reuse: existing venv whose freeze matches ALL pins -> plain run
#    exits 0 and never calls pip install (log stays empty).
# ===============================================================================
new_fixture_proj idempotent
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
bar==2.0
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.idempotent.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo==1.0
bar==2.0
EOF
PIP_LOG="$WORK/pip.idempotent.log"; : > "$PIP_LOG"
export PIP_LOG FREEZE_FILE

out=$(PATH="$BIN:$PATH" bash "$FPV" 2>&1) && rc=0 || rc=$?
check "idempotent reuse: plain run exits 0 when freeze matches all pins" "0" "$rc"
check "idempotent reuse: no reinstall (pip-install log stays empty)" \
  "" "$(cat "$PIP_LOG" 2>/dev/null)"

# ===============================================================================
# 6) Drift by version mismatch: --check exits nonzero, names the offender, and
#    hints at provision-venv.sh.
# ===============================================================================
new_fixture_proj drift_version
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
bar==2.0
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.drift_version.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo==1.5
bar==2.0
EOF
PIP_LOG="$WORK/pip.drift_version.log"; : > "$PIP_LOG"
export PIP_LOG FREEZE_FILE

out=$(PATH="$BIN:$PATH" bash "$FPV" --check 2>&1) && rc=0 || rc=$?
check "version-mismatch drift: --check exits nonzero" \
  "nonzero" "$( [[ "$rc" -ne 0 ]] && echo nonzero || echo zero )"
check "version-mismatch drift: output names the offending package (foo)" \
  "yes" "$( [[ "$out" == *foo* ]] && echo yes || echo no )"
check "version-mismatch drift: output hints at provision-venv.sh" \
  "yes" "$( [[ "$out" == *provision-venv.sh* ]] && echo yes || echo no )"

# ===============================================================================
# 7) Drift by missing package: --check exits nonzero and names the missing pin.
# ===============================================================================
new_fixture_proj drift_missing
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
bar==2.0
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.drift_missing.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo==1.0
EOF
PIP_LOG="$WORK/pip.drift_missing.log"; : > "$PIP_LOG"
export PIP_LOG FREEZE_FILE

out=$(PATH="$BIN:$PATH" bash "$FPV" --check 2>&1) && rc=0 || rc=$?
check "missing-package drift: --check exits nonzero" \
  "nonzero" "$( [[ "$rc" -ne 0 ]] && echo nonzero || echo zero )"
check "missing-package drift: output names the missing pin (bar)" \
  "yes" "$( [[ "$out" == *bar* ]] && echo yes || echo no )"

# ===============================================================================
# 8) Constraints parsing: blank/comment lines ignored; PEP-503 name
#    normalization means "Foo_Bar==1.0" (pin) matches "foo-bar==1.0" (freeze) —
#    no drift.
# ===============================================================================
new_fixture_proj normalize
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
# a leading comment

Foo_Bar==1.0

# a trailing comment
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.normalize.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo-bar==1.0
EOF
PIP_LOG="$WORK/pip.normalize.log"; : > "$PIP_LOG"
export PIP_LOG FREEZE_FILE

out=$(PATH="$BIN:$PATH" bash "$FPV" --check 2>&1) && rc=0 || rc=$?
check "name-normalized pin (Foo_Bar) matches freeze (foo-bar): --check exits 0" \
  "0" "$rc"

# ===============================================================================
# 9) Unknown flag -> exit 2, error on stderr. --help -> exit 0.
# ===============================================================================
new_fixture_proj flags
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
EOF
ERR_FILE="$WORK/unknown_flag.stderr"
out=$( (PATH="$BIN:$PATH" bash "$FPV" --bogus-flag 2>"$ERR_FILE") ) && rc=0 || rc=$?
check "unknown flag exits 2" "2" "$rc"
check "unknown flag prints an error to stderr" \
  "yes" "$( [[ -s "$ERR_FILE" ]] && echo yes || echo no )"

out=$(PATH="$BIN:$PATH" bash "$FPV" --help 2>&1) && rc=0 || rc=$?
check "--help exits 0" "0" "$rc"

# ===============================================================================
# 10) Constraints parsing: a TRAILING inline "# comment" after a version pin
#     (mirrors real constraints-quality.txt entries like
#     "pytest==9.0.3  # CVE-2025-71176 ...") must be stripped, not treated as
#     part of the version/name — otherwise --check reports perpetual false
#     drift for every pin that carries a security-advisory annotation.
# ===============================================================================
new_fixture_proj inline_comment
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0  # some note
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.inline_comment.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo==1.0
EOF
PIP_LOG="$WORK/pip.inline_comment.log"; : > "$PIP_LOG"
export PIP_LOG FREEZE_FILE

out=$(PATH="$BIN:$PATH" bash "$FPV" --check 2>&1) && rc=0 || rc=$?
check "inline-comment pin (foo==1.0  # note) matches freeze (foo==1.0): --check exits 0" \
  "0" "$rc"

# ===============================================================================
# 11) --check with NO venv present: a missing .venv counts as drift (nonzero,
#     hints at provision-venv.sh), and --check must NEVER provision — the
#     .venv directory must still be absent afterward.
# ===============================================================================
new_fixture_proj no_venv
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
EOF
PIP_LOG="$WORK/pip.no_venv.log"; : > "$PIP_LOG"
FREEZE_FILE="$WORK/freeze.no_venv.txt"; : > "$FREEZE_FILE"
export PIP_LOG FREEZE_FILE

out=$(PATH="$BIN:$PATH" bash "$FPV" --check 2>&1) && rc=0 || rc=$?
check "--check with no .venv present exits nonzero" \
  "nonzero" "$( [[ "$rc" -ne 0 ]] && echo nonzero || echo zero )"
check "--check with no .venv present hints at provision-venv.sh" \
  "yes" "$( [[ "$out" == *provision-venv.sh* ]] && echo yes || echo no )"
check "--check with no .venv present names the missing .venv itself (not just a bash file-not-found error whose path happens to contain the script's own name)" \
  "yes" "$( [[ "$out" == *.venv* ]] && echo yes || echo no )"
check "--check with no .venv present never creates .venv (check must not provision)" \
  "no" "$( [[ -d "$FIXTURE/.venv" ]] && echo yes || echo no )"

# ===============================================================================
# 12) Static guards on the REAL check-all.sh and PROMPT.md (grep only, never
#     executed): must invoke provision-venv.sh --check, must prepend
#     .venv/bin to PATH, must keep the venv OPTIONAL (absent-venv
#     fall-through — checks still run when there's no shared venv yet), and
#     PROMPT.md must tell a Ralph worker the shared-venv contract exists.
# ===============================================================================
ca_content="$(cat "$CHECK_ALL" 2>/dev/null)" || true

check "check-all.sh invokes provision-venv.sh --check" "yes" \
  "$( printf '%s' "$ca_content" \
      | grep -Eq -- 'provision-venv\.sh.*--check|--check.*provision-venv\.sh' \
      && echo yes || echo no )"

check "check-all.sh prepends .venv/bin to PATH" "yes" \
  "$( printf '%s' "$ca_content" \
      | grep -Eq '\.venv/bin.*PATH|PATH=.*\.venv/bin' \
      && echo yes || echo no )"

pv_line="$(grep -n 'provision-venv\.sh' "$CHECK_ALL" 2>/dev/null | head -1 | cut -d: -f1)" || true
if [[ -n "$pv_line" ]]; then
  start=$(( pv_line > 6 ? pv_line - 6 : 1 ))
  ctx="$(sed -n "${start},${pv_line}p" "$CHECK_ALL")"
else
  ctx=""
fi
check "check-all.sh guards the provision-venv.sh call conditionally (absent-.venv fall-through, not a hard requirement)" \
  "yes" "$( printf '%s' "$ctx" | grep -Eq '(^|[^#])[[:space:]]*if[[:space:]]|&&|\|\|' \
      && echo yes || echo no )"

check "scripts/ralph/PROMPT.md mentions provision-venv.sh" "yes" \
  "$( grep -q 'provision-venv\.sh' "$PROMPT_MD" 2>/dev/null && echo yes || echo no )"

# ===============================================================================
# 14) Concurrency lock — released on success. A fresh provision must grab the
#     advisory lock around create+install and release it on the way out, so no
#     "<venv>.lock" directory is left behind to wedge later lanes.
# ===============================================================================
new_fixture_proj lock_release_ok
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
EOF
PIP_LOG="$WORK/pip.lock_release_ok.log"; : > "$PIP_LOG"
FREEZE_FILE="$WORK/freeze.lock_release_ok.txt"; : > "$FREEZE_FILE"
export PIP_LOG FREEZE_FILE

out=$(PATH="$BIN:$PATH" bash "$FPV" 2>&1) && rc=0 || rc=$?
check "lock: fresh provision exits 0" "0" "$rc"
check "lock: released after success (no <venv>.lock dir remains)" \
  "no" "$( [[ -d "$FIXTURE/.venv.lock" ]] && echo yes || echo no )"

# ===============================================================================
# 15) Concurrency lock — released on FAILURE. When pip install fails mid-way,
#     the lock must still be released (trap on EXIT) so a crashed/aborted lane
#     can't permanently wedge the shared venv for the rest of the fleet.
# ===============================================================================
new_fixture_proj lock_release_fail
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
EOF
PIP_LOG="$WORK/pip.lock_release_fail.log"; : > "$PIP_LOG"
FREEZE_FILE="$WORK/freeze.lock_release_fail.txt"; : > "$FREEZE_FILE"
INSTALL_FAIL=1
export PIP_LOG FREEZE_FILE INSTALL_FAIL

out=$(PATH="$BIN:$PATH" bash "$FPV" 2>&1) && rc=0 || rc=$?
unset INSTALL_FAIL
check "lock: provision surfaces the pip install failure (nonzero exit)" \
  "nonzero" "$( [[ "$rc" -ne 0 ]] && echo nonzero || echo zero )"
check "lock: released even after a failed install (no <venv>.lock dir remains)" \
  "no" "$( [[ -d "$FIXTURE/.venv.lock" ]] && echo yes || echo no )"

# ===============================================================================
# 16) Concurrency lock — serialization. Two lanes that BOTH detect drift and
#     provision the shared venv at the same time must NOT run their pip installs
#     concurrently (interleaved site-packages writes corrupt the venv). With the
#     install deliberately slowed, the BEGIN/END trace must never show two
#     installs in flight at once (max nesting depth == 1).
# ===============================================================================
new_fixture_proj lock_serialize
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.lock_serialize.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo==0.9
EOF
PIP_LOG="$WORK/pip.lock_serialize.log"; : > "$PIP_LOG"
INSTALL_TRACE="$WORK/trace.lock_serialize.log"; : > "$INSTALL_TRACE"
INSTALL_SLEEP=0.5
export PIP_LOG FREEZE_FILE INSTALL_TRACE INSTALL_SLEEP
# NB: INSTALL_RESULT_FREEZE intentionally UNSET here, so the freeze stays
# drifted and BOTH lanes install — that is what we want to prove is serialized.

( PATH="$BIN:$PATH" bash "$FPV" >/dev/null 2>&1 ) & p1=$!
( PATH="$BIN:$PATH" bash "$FPV" >/dev/null 2>&1 ) & p2=$!
wait "$p1" && sr1=0 || sr1=$?
wait "$p2" && sr2=0 || sr2=$?
serialize_trace="$INSTALL_TRACE"
unset INSTALL_TRACE INSTALL_SLEEP
maxdepth=$(awk '/^BEGIN/{d++; if (d>m) m=d} /^END/{d--} END{print m+0}' "$serialize_trace")
check "lock: two concurrent provisions both exit 0" \
  "yes" "$( [[ "$sr1" -eq 0 && "$sr2" -eq 0 ]] && echo yes || echo no )"
check "lock: concurrent installs are serialized (trace nesting depth never > 1)" \
  "1" "$maxdepth"

# ===============================================================================
# 17) Concurrency lock — convergence (anti-thrash). When one lane finishes
#     provisioning to matching constraints, a second lane that was waiting on
#     the lock must RE-CHECK drift after acquiring it and skip the redundant
#     reinstall — so the shared venv converges instead of thrashing. Here the
#     install heals the drift (INSTALL_RESULT_FREEZE), so exactly ONE lane's
#     constraint install should land.
# ===============================================================================
new_fixture_proj lock_converge
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.lock_converge.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo==0.9
EOF
HEALED="$WORK/freeze.lock_converge.healed.txt"
cat > "$HEALED" <<'EOF'
foo==1.0
EOF
PIP_LOG="$WORK/pip.lock_converge.log"; : > "$PIP_LOG"
INSTALL_RESULT_FREEZE="$HEALED"
INSTALL_SLEEP=0.3
export PIP_LOG FREEZE_FILE INSTALL_RESULT_FREEZE INSTALL_SLEEP

( PATH="$BIN:$PATH" bash "$FPV" >/dev/null 2>&1 ) & q1=$!
( PATH="$BIN:$PATH" bash "$FPV" >/dev/null 2>&1 ) & q2=$!
wait "$q1" || true
wait "$q2" || true
unset INSTALL_RESULT_FREEZE INSTALL_SLEEP
constraint_installs=$(grep -c -- '-c constraints-quality.txt' "$PIP_LOG" 2>/dev/null || true)
check "lock: convergence — a healed drift triggers exactly ONE constraint install" \
  "1" "$constraint_installs"

# ===============================================================================
# 18) Concurrency lock — stale lock reclaimed. A leftover "<venv>.lock" whose
#     recorded holder PID is dead (e.g. a lane that crashed mid-provision) must
#     be treated as stale and broken, so provisioning still proceeds rather than
#     hanging forever.
# ===============================================================================
new_fixture_proj lock_stale
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.lock_stale.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo==0.9
EOF
PIP_LOG="$WORK/pip.lock_stale.log"; : > "$PIP_LOG"
export PIP_LOG FREEZE_FILE
mkdir -p "$FIXTURE/.venv.lock"
printf '%s\n' "99999999" > "$FIXTURE/.venv.lock/pid"   # a PID that is not alive

out=$(PATH="$BIN:$PATH" bash "$FPV" 2>&1) && rc=0 || rc=$?
check "lock: a stale lock (dead holder PID) is reclaimed and provisioning succeeds" \
  "0" "$rc"
check "lock: stale-lock provision actually installs (drift healed)" \
  "yes" "$(pip_log_has '-c constraints-quality.txt' "$(cat "$PIP_LOG" 2>/dev/null)")"
check "lock: reclaimed lock is released on the way out" \
  "no" "$( [[ -d "$FIXTURE/.venv.lock" ]] && echo yes || echo no )"

# ===============================================================================
# 18b) Stale-lock reclaim must itself be race-safe. TWO lanes that both find the
#      SAME stale lock (dead holder PID) must not both "reclaim" it and end up
#      both owning it — reclaim has to be atomic (rename-then-remove), or the
#      two lanes would run concurrent installs, re-opening the very window this
#      lock closes. With the install slowed, the serialization trace must still
#      never show two installs in flight at once (max nesting depth == 1).
# ===============================================================================
new_fixture_proj lock_stale_race
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.lock_stale_race.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo==0.9
EOF
PIP_LOG="$WORK/pip.lock_stale_race.log"; : > "$PIP_LOG"
INSTALL_TRACE="$WORK/trace.lock_stale_race.log"; : > "$INSTALL_TRACE"
INSTALL_SLEEP=0.5
export PIP_LOG FREEZE_FILE INSTALL_TRACE INSTALL_SLEEP
mkdir -p "$FIXTURE/.venv.lock"
printf '%s\n' "99999999" > "$FIXTURE/.venv.lock/pid"   # dead holder -> stale

( PATH="$BIN:$PATH" bash "$FPV" >/dev/null 2>&1 ) & s1=$!
( PATH="$BIN:$PATH" bash "$FPV" >/dev/null 2>&1 ) & s2=$!
wait "$s1" && sc1=0 || sc1=$?
wait "$s2" && sc2=0 || sc2=$?
stale_race_trace="$INSTALL_TRACE"
unset INSTALL_TRACE INSTALL_SLEEP
stale_maxdepth=$(awk '/^BEGIN/{d++; if (d>m) m=d} /^END/{d--} END{print m+0}' "$stale_race_trace")
check "lock: two lanes racing to reclaim ONE stale lock both exit 0" \
  "yes" "$( [[ "$sc1" -eq 0 && "$sc2" -eq 0 ]] && echo yes || echo no )"
check "lock: stale-lock reclaim is atomic (racing installs stay serialized, depth <= 1)" \
  "1" "$stale_maxdepth"

# ===============================================================================
# 19) Concurrency lock — a live holder is respected (blocks, then times out).
#     A lock held by a LIVE process must NOT be stolen; the waiter honors
#     PROVISION_LOCK_TIMEOUT and exits nonzero WITHOUT installing (never
#     corrupting the venv another lane is actively provisioning).
# ===============================================================================
new_fixture_proj lock_live
cat > "$FIXTURE/constraints-quality.txt" <<'EOF'
foo==1.0
EOF
make_fixture_venv "$FIXTURE/.venv"
FREEZE_FILE="$WORK/freeze.lock_live.txt"
cat > "$FREEZE_FILE" <<'EOF'
foo==0.9
EOF
PIP_LOG="$WORK/pip.lock_live.log"; : > "$PIP_LOG"
export PIP_LOG FREEZE_FILE
mkdir -p "$FIXTURE/.venv.lock"
printf '%s\n' "$$" > "$FIXTURE/.venv.lock/pid"   # THIS test process — very much alive

out=$( (PATH="$BIN:$PATH" PROVISION_LOCK_TIMEOUT=1 bash "$FPV" 2>&1) ) && rc=0 || rc=$?
check "lock: a live-held lock is respected — waiter times out nonzero" \
  "nonzero" "$( [[ "$rc" -ne 0 ]] && echo nonzero || echo zero )"
check "lock: a live-held lock blocks the install (pip-install log stays empty)" \
  "" "$(cat "$PIP_LOG" 2>/dev/null)"
check "lock: the live holder's lock is left intact (not stolen)" \
  "yes" "$( [[ -d "$FIXTURE/.venv.lock" ]] && echo yes || echo no )"
rm -rf "$FIXTURE/.venv.lock"

# ===============================================================================
# 13) shellcheck (skip-with-note if not on PATH — CI's ubuntu runner has it).
#     Only lint files that currently exist, so a missing provision-venv.sh
#     (the expected RED state) doesn't turn this into a spurious file-not-found
#     failure; this test file itself must always be clean.
# ===============================================================================
if command -v shellcheck >/dev/null 2>&1; then
  sc_targets=("$SELF")
  [[ -f "$PV" ]] && sc_targets+=("$PV")
  if shellcheck "${sc_targets[@]}"; then sc_result=clean; else sc_result=dirty; fi
  check "shellcheck clean (this test file, plus provision-venv.sh once it exists)" \
    "clean" "$sc_result"
else
  echo "  (skip) shellcheck not on PATH"
fi

echo
echo "provision-venv tests: $PASS passed, $FAIL failed"
[[ "$FAIL" -eq 0 ]]
