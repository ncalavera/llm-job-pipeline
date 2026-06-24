#!/usr/bin/env bash
# Activate the repo's tracked git hooks (private-data guardrail).
# Run once after cloning: ./scripts/install-hooks.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath hooks
chmod +x hooks/* 2>/dev/null || true
echo "✓ Git hooks active (core.hooksPath=hooks). Pre-commit guards against committing private data."
