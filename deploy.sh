#!/usr/bin/env bash
# deploy.sh — sync source tree to /opt/scanner and reload services.
# Run as root or with sudo from /srv/scanner.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/files/opt/scanner" && pwd)"
DEST="/opt/scanner"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo ./deploy.sh" >&2
  exit 1
fi

echo "→ Syncing $SRC → $DEST"
rsync -a --delete \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'venv' \
  "$SRC/" "$DEST/"

chown -R scanner:scanner "$DEST"

echo "→ Reloading services"
systemctl restart scanner-scheduler.service
systemctl restart scanner-ui.service

echo "Done. Check status:"
echo "  journalctl -u scanner-scheduler -n 20"
echo "  journalctl -u scanner-ui -n 20"
