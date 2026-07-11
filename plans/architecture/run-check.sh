#!/usr/bin/env bash
set -euo pipefail

echo "🏛️  Checking Python architecture with import-linter..."

if ! command -v lint-imports &> /dev/null; then
    echo "❌ import-linter not found. Install with: pip install -r requirements-dev.txt"
    exit 1
fi

lint-imports --config plans/architecture/.importlinter

echo "✅ Architecture checks passed!"
