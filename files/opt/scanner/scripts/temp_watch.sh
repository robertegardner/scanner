#!/usr/bin/env bash
# temp_watch.sh — sample SoC temperature + throttle state and log a trend.
#
# Background: on 2026-06-07 the Pi's SoC was seen at 84-86°C and actively
# throttling (`get_throttled=0xe0006`) right after a dongle-wedge recovery.
# That throttling is worth tracking on its own, but it is NOT assumed to be the
# cause of the Nooelec dongle wedges — the radio project shares this exact Pi 5
# / SoC and is not having dongle problems, so generic SoC heat can't explain a
# fault specific to one dongle's USB port/power rail. This watcher just gathers
# the data: how hot does this Pi actually get, how often does it throttle, and
# does that correlate (or not) with the dongle drops in usb_overcurrent.log?
#
# Unlike usb_oc_watch.sh (which tracks discrete kernel *events* via a journald
# cursor), temperature is a point-in-time reading, so this samples on every run
# and appends a compact CSV line for trend analysis. A journal warning is only
# emitted when a sample crosses the soft threshold or shows active throttle bits,
# so the normal case stays quiet. Run from scanner-temp-watch.timer.
set -euo pipefail

LOG=/var/lib/scanner/soc_temp.log
# Pi 5 soft temp limit is 85°C (throttling begins there). Warn a little early so
# we see the climb, not just the cliff.
WARN_C=80

# measure_temp -> "temp=86.2'C"; get_throttled -> "throttled=0xe0006".
temp_raw="$(vcgencmd measure_temp 2>/dev/null || echo "temp=NA")"
thr_raw="$(vcgencmd get_throttled 2>/dev/null || echo "throttled=NA")"
temp_c="${temp_raw#temp=}"; temp_c="${temp_c%\'C}"
thr_hex="${thr_raw#throttled=}"

ts="$(date -Is)"

# Decode the throttle word. Lower 4 bits = conditions active *right now*:
#   bit0 under-voltage, bit1 arm-freq-capped, bit2 throttled, bit3 soft-temp-limit.
# (Bits 16-19 are the latched "has occurred since boot" flags — informative but
# not an active alarm.)
active=""
if [[ "$thr_hex" == 0x* ]]; then
  cur=$(( thr_hex & 0xF ))
  (( cur & 0x1 )) && active+="undervolt "
  (( cur & 0x2 )) && active+="freq-capped "
  (( cur & 0x4 )) && active+="throttled "
  (( cur & 0x8 )) && active+="soft-temp-limit "
fi

# CSV: timestamp,temp_c,throttled_hex,active-flags(space-joined, '-' if none).
printf '%s,%s,%s,%s\n' "$ts" "$temp_c" "$thr_hex" "${active:-- }" \
  | sed 's/ $//' >> "$LOG"

# Warn only on a genuine condition, so the journal isn't spammed every 5 min.
warn=""
if [[ "$temp_c" != "NA" ]]; then
  # bash has no float compare; strip the decimal and compare whole degrees.
  if (( ${temp_c%.*} >= WARN_C )); then
    warn="SoC ${temp_c}°C >= ${WARN_C}°C threshold"
  fi
fi
if [[ -n "$active" ]]; then
  warn="${warn:+$warn; }active throttle: ${active% }"
fi

if [[ -n "$warn" ]]; then
  printf 'SoC temp/throttle alert: %s (throttled=%s)\n' "$warn" "$thr_hex" \
    | systemd-cat -t scanner-temp-watch -p warning
fi
