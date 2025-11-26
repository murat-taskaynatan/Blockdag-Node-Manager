#!/usr/bin/env bash
set -euo pipefail

# Safe rsync helper that preserves the virtualenv in /opt.
SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
DEST="${2:-/opt/blockdag-node-manager}"

echo "Syncing from $SRC to $DEST (preserving .venv)..."
rsync -a --delete \
  --filter='protect .venv/' \
  --exclude='.venv/' \
  --exclude='.git/' \
  "$SRC/" "$DEST/"

echo "Done."
