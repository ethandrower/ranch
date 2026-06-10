#!/usr/bin/env bash
# Launch the DEV ranch console (this worktree) with full isolation from the
# live ranch instance running out of ~/code/citemed/ranch.
#
# Run from the worktree root:   ./scripts/dev.sh
#
# What this script guarantees:
#   - Vite renderer binds to 5174 (live uses 5173) and fails loudly on collision
#   - The Electron app's userData is ~/Library/Application Support/ranch-console-dev/
#     (different package name → different default path, set in console/package.json)
#   - RANCH_DATABASE_URL points at this worktree's .ranch-dev/ranch.db
#   - RANCH_HOME points at this worktree's .ranch-dev/ for any code that respects it
#   - RANCH_API_PORT pins the FastAPI sidecar (once it exists) to 8421
#
# To stop the dev instance later: kill ONLY by the PID this script reports.
# NEVER run `pkill electron` or `pkill -f ranch` — that will take down your
# live sessions too.

set -euo pipefail

WORKTREE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEV_HOME="$WORKTREE_ROOT/.ranch-dev"

mkdir -p "$DEV_HOME"

# ─── Pre-flight ─────────────────────────────────────────────────────
# 5174 (Vite) is ours — must be free.
if lsof -nP -iTCP:5174 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "✗ Port 5174 (Vite renderer) is in use:"
  lsof -nP -iTCP:5174 -sTCP:LISTEN
  echo
  echo "  Investigate before launching — do NOT kill blindly."
  exit 1
fi
# 8421 (sidecar) — coexist with a healthy one, refuse if it's something else.
if lsof -nP -iTCP:8421 -sTCP:LISTEN >/dev/null 2>&1; then
  if curl -fs http://127.0.0.1:8421/api/health >/dev/null 2>&1; then
    echo "  (note: a healthy ranch sidecar is already running on 8421 — coexisting)"
  else
    echo "✗ Port 8421 is held by an unidentified process:"
    lsof -nP -iTCP:8421 -sTCP:LISTEN
    echo "  Investigate before launching — do NOT kill blindly."
    exit 1
  fi
fi

# ─── Isolation env ─────────────────────────────────────────────────
export RANCH_HOME="$DEV_HOME"
export RANCH_DATABASE_URL="sqlite:///$DEV_HOME/ranch.db"
export RANCH_API_PORT=8421
# Launch the rebuilt HandsConsole UI by default in dev. To boot into the
# legacy pty terminal app instead, unset this before running.
export VITE_RANCH_VIEW=hands
# Belt-and-suspenders: even though package.json's name change already
# redirects Electron's userData, set ELECTRON_USER_DATA in case any code
# path opts into reading it.
export ELECTRON_USER_DATA="$HOME/Library/Application Support/ranch-console-dev"

echo "─── ranch DEV console ──────────────────────────────────────────"
echo "  worktree:           $WORKTREE_ROOT"
echo "  RANCH_HOME:         $RANCH_HOME"
echo "  RANCH_DATABASE_URL: $RANCH_DATABASE_URL"
echo "  RANCH_API_PORT:     $RANCH_API_PORT"
echo "  vite renderer:      127.0.0.1:5174 (strictPort)"
echo "  userData:           ~/Library/Application Support/ranch-console-dev/"
echo "─────────────────────────────────────────────────────────────────"

cd "$WORKTREE_ROOT/console"

# First-time setup — node_modules won't exist in a fresh worktree.
if [ ! -d node_modules ]; then
  echo "→ Installing console deps (first run in this worktree)…"
  pnpm install
fi

exec pnpm dev
