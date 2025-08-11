#!/usr/bin/env bash
set -euo pipefail

if [ -f "modules/autonomous_agent.py" ]; then
  echo "❌ Legacy file 'modules/autonomous_agent.py' must not exist. Delete it and use the package layout."
  exit 1
fi

# Ensure package entrypoint is tiny (no heavy imports). This is a simple heuristic:
LINES=$(wc -l < modules/autonomous_agent/__init__.py || echo 0)
if [ "$LINES" -gt 250 ]; then
  echo "⚠️  __init__.py seems large ($LINES lines). Keep it minimal & side-effect free."
  exit 2
fi

echo "✅ Package purity checks passed."
