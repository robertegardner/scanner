#!/usr/bin/env bash
# deploy.sh — sync source tree to /opt/scanner and reload services.
# Run as root or with sudo from /srv/scanner.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
SRC="$REPO/files/opt/scanner"
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
  --exclude 'sdrtrunk' \
  "$SRC/" "$DEST/"

chown -R scanner:scanner "$DEST"

echo "→ Installing SDRTrunk playlist"
SDRTRUNK_HOME="/var/lib/scanner/SDRTrunk"
PLAYLIST_DIR="$SDRTRUNK_HOME/playlist"
PLAYLIST_SRC="$REPO/files/etc/scanner/sdrtrunk-playlist.xml.example"
install -d -o scanner -g scanner "$PLAYLIST_DIR"
if [[ -f "$PLAYLIST_SRC" ]]; then
  install -o scanner -g scanner -m 644 "$PLAYLIST_SRC" "$PLAYLIST_DIR/default.xml"
fi

echo "→ Disabling RSPdxR2 in SDRTrunk tuner config"
python3 - <<EOF
import json, os, sys
path = "$SDRTRUNK_HOME/configuration/tuner_configuration.json"
if not os.path.exists(path):
    print("tuner_configuration.json not yet created — will take effect after first SDRTrunk run")
    sys.exit(0)
with open(path) as f:
    config = json.load(f)
disabled = config.setdefault("disabledTuners", [])
entry = {"tunerClass": "RSP", "id": "RSPdxR2 SER#24051FAF70"}
if entry not in disabled:
    disabled.append(entry)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp, path)
    print("RSPdxR2 added to disabled tuners")
else:
    print("RSPdxR2 already disabled — no change")
EOF
chown scanner:scanner "$SDRTRUNK_HOME/configuration/tuner_configuration.json" 2>/dev/null || true

echo "→ Reloading services"
systemctl restart scanner-scheduler.service
systemctl restart scanner-ui.service

echo "Done. Check status:"
echo "  journalctl -u scanner-scheduler -n 20"
echo "  journalctl -u scanner-ui -n 20"
