#!/usr/bin/env bash
# scripts/architecture.sh - Enforce import boundaries with import-linter
# Usage: ./scripts/architecture.sh [--check] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --check)
            # Check-only is the only behaviour; accept the flag as a no-op so
            # callers (e.g. check-all.sh) can pass it explicitly.
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Enforce the research-sandbox / signing-key / order-submission import
boundaries with import-linter, as CI-enforced defense-in-depth for the
pure-stdlib AST boundary tests.

OPTIONS:
    --check     Check only, fail if a contract breaks (default mode)
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           All import-linter contracts KEPT
    1           A contract is BROKEN or lint-imports is not installed
    2           Error running checks

EXAMPLES:
    $(basename "$0")              # Run the boundary checks
    $(basename "$0") --verbose     # Show detailed output
EOF
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

cd "$PROJECT_ROOT"

# Set verbosity
if $VERBOSE; then
    set -x
fi

# Delegate to the canonical runner, which invokes lint-imports against
# plans/architecture/.importlinter and fails loudly with a clear "not found"
# message when lint-imports is missing.
bash plans/architecture/run-check.sh
