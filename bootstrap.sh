#!/usr/bin/env bash
# bootstrap.sh — first-time setup for the scanner project on a Raspberry Pi 5.
# Run once as root after cloning the repo to /srv/scanner.
set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
JAVA_HEAP="-Xmx512m"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo ./bootstrap.sh" >&2
  exit 1
fi

log() { echo "[bootstrap] $*"; }

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
log "Installing system packages"
apt-get update -qq
apt-get install -y --no-install-recommends \
  python3 python3-venv python3-pip \
  rtl-sdr \
  sox \
  libsox-fmt-mp3 \
  openjdk-21-jre-headless \
  rsync \
  curl \
  git

# ---------------------------------------------------------------------------
# 2. Blacklist DVB-T kernel driver (if not already done by the radio project)
# ---------------------------------------------------------------------------
BLACKLIST=/etc/modprobe.d/rtlsdr-blacklist.conf
if [[ ! -f "$BLACKLIST" ]]; then
  log "Blacklisting DVB-T kernel driver"
  cat > "$BLACKLIST" <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF
  update-initramfs -u
else
  log "DVB-T blacklist already in place — skipping"
fi

# ---------------------------------------------------------------------------
# 3. scanner user and directories
# ---------------------------------------------------------------------------
if ! id scanner &>/dev/null; then
  log "Creating scanner user"
  useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/scanner scanner
fi

# Add scanner to plugdev so it can open the RTL-SDR device
usermod -aG plugdev scanner

log "Creating directories"
install -d -o scanner -g scanner -m 755 \
  /opt/scanner \
  /var/lib/scanner \
  /var/lib/scanner/sdrtrunk \
  /var/lib/scanner/sdrtrunk/recordings \
  /var/lib/scanner/sdrtrunk/playlists \
  /var/lib/scanner/manual \
  /etc/scanner

# ---------------------------------------------------------------------------
# 4. Python virtual environment
# ---------------------------------------------------------------------------
log "Creating Python virtual environment"
python3 -m venv /opt/scanner/venv
/opt/scanner/venv/bin/pip install --quiet --upgrade pip
/opt/scanner/venv/bin/pip install --quiet flask requests

# ---------------------------------------------------------------------------
# 5. SDRTrunk
# ---------------------------------------------------------------------------
SDRTRUNK_DIR=/opt/scanner/sdrtrunk
SDRTRUNK_VERSION="0.6.1"
SDRTRUNK_INSTALL="$SDRTRUNK_DIR/sdr-trunk-linux-aarch64-v${SDRTRUNK_VERSION}"
SDRTRUNK_BIN="$SDRTRUNK_INSTALL/bin/sdr-trunk"

if [[ ! -f "$SDRTRUNK_BIN" ]]; then
  log "Downloading SDRTrunk v${SDRTRUNK_VERSION}"
  install -d -o scanner -g scanner "$SDRTRUNK_DIR"
  SDRTRUNK_URL="https://github.com/DSheirer/sdrtrunk/releases/download/v${SDRTRUNK_VERSION}/sdr-trunk-linux-aarch64-v${SDRTRUNK_VERSION}.zip"
  TMP=$(mktemp -d)
  curl -L --progress-bar "$SDRTRUNK_URL" -o "$TMP/sdrtrunk.zip"
  unzip -q "$TMP/sdrtrunk.zip" -d "$SDRTRUNK_DIR"
  rm -rf "$TMP"
  chown -R scanner:scanner "$SDRTRUNK_INSTALL"
  chmod +x "$SDRTRUNK_BIN"
  # Stable symlink so config.env doesn't need updating on version bumps
  ln -sf "$SDRTRUNK_BIN" "$SDRTRUNK_DIR/sdr-trunk-latest"
  log "SDRTrunk installed at $SDRTRUNK_BIN"
else
  log "SDRTrunk already installed — skipping"
fi

# ---------------------------------------------------------------------------
# 5b. Keep SDRTrunk off the radio's SDRplay
# ---------------------------------------------------------------------------
# SDRTrunk only needs the Nooelec (RTL2832), but on startup it loads
# libsdrplay_api.so and enumerates the RSPdx-R2 — which yanks the device out
# from under the radio project (rx_fm: "Device has been removed"). disabledTuners
# stops SDRTrunk *streaming* the RSP but not this enumeration. SDRTrunk loads the
# lib by name/path with no override hook, but skips the RSP gracefully if it
# can't read the lib. So we restrict the lib to the radio's group: radio (group
# radio) keeps it, scanner (SDRTrunk) is denied -> the two coexist on their
# separate dongles. Re-run this after any SDRplay API reinstall (it resets perms).
SDRPLAY_LIB=$(readlink -f /usr/local/lib/libsdrplay_api.so 2>/dev/null || true)
if [[ -n "$SDRPLAY_LIB" && -f "$SDRPLAY_LIB" ]] && getent group radio >/dev/null; then
  chown root:radio "$SDRPLAY_LIB"
  chmod 750 "$SDRPLAY_LIB"
  log "Restricted $SDRPLAY_LIB to root:radio 750 (SDRTrunk won't grab the SDRplay)"
else
  log "SDRplay lib or 'radio' group not present — skipping SDRplay restriction"
fi

# ---------------------------------------------------------------------------
# 6. Configuration files
# ---------------------------------------------------------------------------
if [[ ! -f /etc/scanner/config.env ]]; then
  log "Installing config.env.example → /etc/scanner/config.env"
  cp "$REPO/files/etc/scanner/config.env.example" /etc/scanner/config.env
  chmod 640 /etc/scanner/config.env
  chown root:scanner /etc/scanner/config.env
  log "Edit /etc/scanner/config.env before starting services"
else
  log "/etc/scanner/config.env already exists — not overwriting"
fi

if [[ ! -f /etc/scanner/talkgroups.json ]]; then
  log "Installing talkgroups.json.example → /etc/scanner/talkgroups.json"
  cp "$REPO/files/etc/scanner/talkgroups.json.example" /etc/scanner/talkgroups.json
  chown root:scanner /etc/scanner/talkgroups.json
  log "Edit /etc/scanner/talkgroups.json with real TGIDs from radioreference.com"
fi

# ---------------------------------------------------------------------------
# SDRTrunk playlist
# ---------------------------------------------------------------------------
PLAYLIST=/var/lib/scanner/sdrtrunk/playlists/cape-county.xml
if [[ ! -f "$PLAYLIST" ]]; then
  log "Installing SDRTrunk playlist template → $PLAYLIST"
  cp "$REPO/files/etc/scanner/sdrtrunk-playlist.xml.example" "$PLAYLIST"
  chown scanner:scanner "$PLAYLIST"
  log "IMPORTANT: Edit $PLAYLIST — replace TODO_CONTROL_CHANNEL_HZ with the"
  log "  Cape County MOSWIN control channel frequency in Hz (from radioreference.com)"
else
  log "SDRTrunk playlist already exists — not overwriting"
fi

# ---------------------------------------------------------------------------
# 9. sudoers
# ---------------------------------------------------------------------------
log "Installing sudoers"
install -m 440 "$REPO/files/etc/sudoers.d/scanner" /etc/sudoers.d/scanner
visudo -c -f /etc/sudoers.d/scanner

# ---------------------------------------------------------------------------
# 10. systemd units
# ---------------------------------------------------------------------------
log "Installing systemd units"
cp "$REPO/files/etc/systemd/system/scanner-scheduler.service" /etc/systemd/system/
cp "$REPO/files/etc/systemd/system/scanner-ui.service" /etc/systemd/system/
cp "$REPO/files/etc/systemd/system/scanner-usb-oc-watch.service" /etc/systemd/system/
cp "$REPO/files/etc/systemd/system/scanner-usb-oc-watch.timer" /etc/systemd/system/
cp "$REPO/files/etc/systemd/system/scanner-temp-watch.service" /etc/systemd/system/
cp "$REPO/files/etc/systemd/system/scanner-temp-watch.timer" /etc/systemd/system/
cp "$REPO/files/etc/systemd/system/scanner-transcribe.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable scanner-scheduler.service scanner-ui.service
# USB over-current watcher (see CLAUDE.md "Hardware constraints"): logs new kernel
# over-current events so we can tell whether usb_max_current_enable=1 holds.
systemctl enable scanner-usb-oc-watch.timer
# SoC temperature/throttle watcher: logs a CSV trend (/var/lib/scanner/soc_temp.log)
# and warns when the Pi crosses 80°C or actively throttles. Data-gathering only —
# the dongle wedges are NOT assumed thermal (the radio shares this Pi and is fine).
systemctl enable scanner-temp-watch.timer
# Transcription orchestrator (Whisper captions + EMS call text). Reuses the
# radio's GPU host; degrades gracefully when it's offline. Set WHISPER_URL/
# WHISPER_TOKEN in /etc/scanner/config.env first.
systemctl enable scanner-transcribe.service

# ---------------------------------------------------------------------------
# 11. Initial deploy
# ---------------------------------------------------------------------------
log "Running initial deploy"
"$REPO/deploy.sh"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
cat <<'EOF'

Bootstrap complete. Next steps:

1. Verify the RTL-SDR is visible:
     rtl_test -t

2. Edit talkgroups:
     sudo nano /etc/scanner/talkgroups.json
   (Fill in real TGIDs from radioreference.com for Cape Girardeau County MOSWIN)

3. Configure SDRTrunk aliases and system in /var/lib/scanner/sdrtrunk/
   (Start SDRTrunk once manually to generate its config, then stop it.)

4. Start services:
     sudo systemctl start scanner-scheduler scanner-ui

5. Watch logs:
     sudo journalctl -u scanner-scheduler -f

6. Open the dashboard:
     http://<pi-ip>:8081/
EOF
