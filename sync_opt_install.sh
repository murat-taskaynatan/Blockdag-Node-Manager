#!/usr/bin/env bash
set -euo pipefail

# Helper to keep /opt/blockdag-node-manager in sync after pushing to main.
# Usage: run this from the repository clone after a `git push origin main`.

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER="$REPO_DIR/install_node_manager.sh"

if [[ ! -x "$INSTALLER" ]]; then
  echo "error: installer not found or not executable at $INSTALLER" >&2
  exit 1
fi

cd "$REPO_DIR"
git fetch origin main
git checkout main
git pull origin main

echo "Running installer to sync /opt/blockdag-node-manager…"
sudo "$INSTALLER"

echo "Sync complete; the service should now run the latest main build."
