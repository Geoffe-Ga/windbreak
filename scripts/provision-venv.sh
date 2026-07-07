#!/usr/bin/env bash
# scripts/provision-venv.sh - Provision/refresh the SHARED repo-local .venv
# Usage: ./scripts/provision-venv.sh [--print-venv | --check | --help]
#
# Provisions a SINGLE shared virtual environment at the MAIN repo root and
# reuses it from every fleet worktree, so check-all.sh runs against the pinned
# quality toolchain (constraints-quality.txt) regardless of the operator's
# global Python -- ending fleet-wide pip-audit drift noise (issue #133).
#
# The shared venv lives at "<main-worktree-root>/.venv". A call from inside a
# linked worktree (.ralph/worktrees/issue-N/) still resolves to the SAME main
# root .venv, so the toolchain is installed once and shared by all lanes.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

MODE="provision"

# Parse command line arguments (mirrors check-all.sh / lint.sh conventions).
while [[ $# -gt 0 ]]; do
    case $1 in
        --print-venv)
            MODE="print-venv"
            shift
            ;;
        --check)
            MODE="check"
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Provision/refresh the SINGLE shared repo-local .venv at the main repo root and
reuse it from every fleet worktree so quality checks run against the pinned
toolchain (constraints-quality.txt).

OPTIONS:
    --print-venv   Print the resolved shared .venv absolute path and exit.
    --check        Drift check only (never creates/installs): exit 0 when the
                   installed toolchain matches constraints-quality.txt, nonzero
                   (with a provision-venv.sh hint) when drifted or missing.
    --help         Display this help message.
    (no option)    Provision: create the shared .venv if absent, then install
                   the pinned toolchain only when fresh or drifted (idempotent).

EXIT CODES:
    0           Success (in sync, provisioned, or informational)
    1           Drift detected / provisioning failed
    2           Error running the script (e.g. unknown option)

EXAMPLES:
    $(basename "$0")                # Provision or refresh the shared .venv
    $(basename "$0") --print-venv   # Print the shared .venv path
    $(basename "$0") --check        # Fail loudly on toolchain drift
EOF
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

# Trim leading/trailing whitespace (and CR) from a string.
trim() {
    local s="$1"
    s="${s#"${s%%[![:space:]]*}"}"
    s="${s%"${s##*[![:space:]]}"}"
    printf '%s' "$s"
}

# PEP-503 normalize a distribution name: lowercase and collapse any run of
# -, _ or . into a single "-". So "Foo_Bar" and "foo-bar" compare equal.
normalize_name() {
    printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | sed -E 's/[._-]+/-/g'
}

# Resolve the shared venv path: the MAIN worktree root .venv, even from inside
# a linked worktree, via git's common .git dir; fall back to this script's own
# PROJECT_ROOT .venv when outside a git repository. Anchored to PROJECT_ROOT
# (the checkout that owns this script), NOT the ambient CWD, so a caller's
# working directory can never redirect the shared venv to the wrong repo.
resolve_venv() {
    local common_dir main_root
    if common_dir="$(git -C "$PROJECT_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"; then
        main_root="${common_dir%/.git}"
        if [[ "$main_root" == "$common_dir" ]]; then
            main_root="$(dirname "$common_dir")"
        fi
        main_root="$(cd "$main_root" 2>/dev/null && pwd -P)" || main_root="$PROJECT_ROOT"
    else
        main_root="$PROJECT_ROOT"
    fi
    printf '%s/.venv\n' "$main_root"
}

VENV="$(resolve_venv)"

# Drift check: compare the pins in constraints-quality.txt against what is
# installed in the shared venv. Prints offenders + a provision-venv.sh hint to
# stderr. Returns 0 when in sync, 1 when drifted or the venv is missing.
# NEVER creates or mutates the venv.
drift_check() {
    local venv_python="$VENV/bin/python"
    if [[ ! -x "$venv_python" ]]; then
        printf 'Shared virtual environment missing at %s\n' "$VENV" >&2
        printf 'Run provision-venv.sh to create it.\n' >&2
        return 1
    fi

    local constraints="$PROJECT_ROOT/constraints-quality.txt"
    [[ -f "$constraints" ]] || return 0

    # Snapshot the installed toolchain as normalized "name==version" lines.
    local freeze_raw freeze_norm=""
    freeze_raw="$("$venv_python" -m pip list --format=freeze 2>/dev/null || true)"
    local fline fname fver fnorm
    while IFS= read -r fline; do
        fline="$(trim "$fline")"
        [[ -z "$fline" ]] && continue
        # Skip freeze lines that are not exact name==version pins (warnings,
        # "pkg @ file://...", "-e ...").
        [[ "$fline" != *"=="* ]] && continue
        fname="$(trim "${fline%%==*}")"
        fver="$(trim "${fline#*==}")"
        [[ "$fname" =~ ^[A-Za-z0-9._-]+$ ]] || continue
        fnorm="$(normalize_name "$fname")"
        freeze_norm+="${fnorm}==${fver}"$'\n'
    done <<< "$freeze_raw"

    # Compare every pin against the installed set.
    local offenders="" rline pname pver pnorm installed
    while IFS= read -r rline; do
        # Strip inline "# comment" (e.g. "pytest==9.0.3  # CVE-...").
        rline="${rline%%#*}"
        rline="$(trim "$rline")"
        [[ -z "$rline" ]] && continue
        [[ "$rline" != *"=="* ]] && continue
        pname="$(trim "${rline%%==*}")"
        pver="$(trim "${rline#*==}")"
        [[ "$pname" =~ ^[A-Za-z0-9._-]+$ ]] || continue
        pnorm="$(normalize_name "$pname")"
        installed="$(printf '%s' "$freeze_norm" | grep -E "^${pnorm}==" | head -n1 || true)"
        if [[ -z "$installed" ]]; then
            offenders+="  - ${pname} (pinned ${pver}, not installed)"$'\n'
        else
            installed="${installed#*==}"
            if [[ "$installed" != "$pver" ]]; then
                offenders+="  - ${pname} (pinned ${pver}, installed ${installed})"$'\n'
            fi
        fi
    done < "$constraints"

    if [[ -n "$offenders" ]]; then
        printf 'Toolchain drift from constraints-quality.txt:\n%s' "$offenders" >&2
        printf 'Run provision-venv.sh to refresh the shared .venv.\n' >&2
        return 1
    fi
    return 0
}

# Provision the shared venv: create it when absent, then install the pinned
# toolchain only when the venv was just created or has drifted (idempotent --
# a clean, matching venv exits 0 without invoking pip install).
provision() {
    local fresh=false drifted=false
    if [[ ! -x "$VENV/bin/python" ]]; then
        python3 -m venv "$VENV"
        fresh=true
    fi
    if ! drift_check 2>/dev/null; then
        drifted=true
    fi
    if [[ "$fresh" == true || "$drifted" == true ]]; then
        # Install from the invoking checkout's project root so the constraints
        # and requirements are exactly those relative paths. NEVER install
        # editable (-e): the shared venv must not bind to one lane's checkout.
        (
            cd "$PROJECT_ROOT" || exit 1
            # Mirror ci.yml's `--upgrade pip setuptools wheel` so pip-audit sees
            # the same env as CI (venv-bundled pip can carry flagged CVEs).
            "$VENV/bin/python" -m pip install --upgrade pip setuptools wheel
            "$VENV/bin/python" -m pip install \
                -c constraints-quality.txt \
                -r requirements.txt \
                -r requirements-dev.txt
        )
    fi
}

case "$MODE" in
    print-venv)
        printf '%s\n' "$VENV"
        exit 0
        ;;
    check)
        if drift_check; then
            exit 0
        fi
        exit 1
        ;;
    provision)
        provision
        exit 0
        ;;
esac
