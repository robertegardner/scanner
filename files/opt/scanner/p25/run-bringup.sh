#!/usr/bin/env bash
#
# MOSWIN P25 discone bring-up launcher (SDRTrunk, isolated, headless, NO Icecast).
#
# Purpose: prove the discone can receive + decode the Cape Girardeau County
# MOSWIN P25 Phase II system on the Nooelec, BEFORE building an antenna relay.
# This is a MANUAL diagnostic. It is NOT a service, NOT wired into scheduler.py
# or jobs/ems_scanner.py, and does NOT stream to Icecast.
#
# Why SDRTrunk and not op25: op25 requires GNU Radio (~1.5 GB installed) which
# does not fit on the Pi's SD card (823 MB free). SDRTrunk is already installed
# and proven for P25 Phase II here. See docs/P25_BRINGUP.md for the full story.
#
# RADIO-PROJECT SAFETY: this copies the *live* tuner_configuration.json into the
# isolated home, so the radio project's RSPdx-R2 stays in disabledTuners and is
# never opened. Stop scanner-scheduler before running so the Nooelec is free.
#
# Usage:  sudo -u scanner files/opt/scanner/p25/run-bringup.sh [runtime_seconds]
#
set -euo pipefail

# ---- knobs (edit these) ---------------------------------------------------
FREQ_HZ=769168750            # MOSWIN Site 033 control channel (769.16875 MHz)
MODULATION="${MOD:-C4FM}"    # PROVEN: MOSWIN's CC is C4FM ("Normal"), NOT CQPSK.
#   CQPSK (LSM) gave ~98% SYNC LOSS; C4FM gives ~0.1%. The production EMS playlist
#   uses CQPSK, which is why that channel never decoded. Override: MOD=CQPSK.
RUNTIME_S="${1:-240}"        # decode duration before auto-stop
MASTER_GAIN="${2:-GAIN_327}" # R820T master gain, tenths of dB (GAIN_327 = 32.7 dB).
#   GAIN_327 + C4FM decodes flawlessly here; gain was never the problem (the
#   sync loss was purely the CQPSK/C4FM mismatch). Range GAIN_0..GAIN_495.
# ---------------------------------------------------------------------------

SDRTRUNK_BIN=/opt/scanner/sdrtrunk/sdr-trunk-latest
PROD_HOME=/var/lib/scanner/SDRTrunk
WORK=/var/lib/scanner/p25bringup
HOME_BASE="$WORK/sthome"          # becomes -Duser.home
ST_HOME="$HOME_BASE/SDRTrunk"     # SDRTrunk resolves <user.home>/SDRTrunk
SELF_DIR="$(cd "$(dirname "$0")" && pwd)"
PLAYLIST_SRC="$SELF_DIR/moswin-bringup-playlist.xml"

mkdir -p "$ST_HOME/configuration" "$ST_HOME/playlist" "$ST_HOME/settings" "$WORK"

# Reuse the live disabledTuners so the RSPdx-R2 (radio project) is never grabbed.
cp "$PROD_HOME/configuration/tuner_configuration.json" "$ST_HOME/configuration/"
# Override the R820T master gain (production GAIN_327 overloads on the strong CC).
sed -i "s/\"masterGain\" : \"[^\"]*\"/\"masterGain\" : \"$MASTER_GAIN\"/" \
  "$ST_HOME/configuration/tuner_configuration.json"
printf 'root.directory=SDRTrunk\n' > "$ST_HOME/SDRTrunk.properties"

# Install the bring-up playlist and apply the freq/modulation knobs.
cp "$PLAYLIST_SRC" "$ST_HOME/playlist/default.xml"
sed -i "s/frequency=\"[0-9]*\"/frequency=\"$FREQ_HZ\"/" "$ST_HOME/playlist/default.xml"
sed -i "s/modulation=\"[^\"]*\"/modulation=\"$MODULATION\"/" "$ST_HOME/playlist/default.xml"

LOG="$WORK/bringup_$(date +%Y%m%d_%H%M%S).log"
echo "SDRTrunk bring-up: CC=$FREQ_HZ Hz  mod=$MODULATION  runtime=${RUNTIME_S}s"
echo "isolated home: $HOME_BASE   stdout/err -> $LOG"
echo "R820T master gain: $MASTER_GAIN (tenths of dB)"

SDR_TRUNK_OPTS="-Xmx384m -Duser.home=$HOME_BASE" \
  timeout "${RUNTIME_S}" "$SDRTRUNK_BIN" --headless >"$LOG" 2>&1 || true

echo "Done. App log: $LOG"
echo "Event logs (talkgroups / decoded messages): $ST_HOME/event_logs/"
