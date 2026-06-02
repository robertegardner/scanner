#!/usr/bin/env bash
# usb_oc_watch.sh — detect *new* USB over-current events and log them.
#
# Background: the Pi 5 / UCTRONICS PoE-HAT USB rail was tripping its
# over-current limiter and dropping the Nooelec (and sometimes the radio's
# SDRplay) off the bus, killing MOSWIN. The standing fix is
# `usb_max_current_enable=1` in /boot/firmware/config.txt (lifts the 600mA
# per-port cap to 1.6A). This watcher confirms whether over-current still
# recurs after that change — especially through a hot attic midday.
#
# Uses a persistent journald cursor so it only ever reports events it hasn't
# seen, with no double-counting and no gaps across reboots. Run from a
# systemd timer (scanner-usb-oc-watch.timer).
set -euo pipefail

LOG=/var/lib/scanner/usb_overcurrent.log
CURSOR=/var/lib/scanner/usb_oc.cursor
PATTERN='over-current|overcurrent'

# First run (no cursor yet): seed the cursor at the tail of the kernel journal
# so we capture events going forward, not the 139 historical ones already known.
if [[ ! -s "$CURSOR" ]]; then
  journalctl -k -n1 --show-cursor -q 2>/dev/null \
    | sed -n 's/^-- cursor: //p' > "${CURSOR}.tmp" \
    && mv "${CURSOR}.tmp" "$CURSOR"
  exit 0
fi

CUR="$(cat "$CURSOR")"

# One read: new kernel entries after the saved cursor, plus the latest cursor
# at the end (a "-- cursor: ..." line). Reading entries and the advance-point in
# one call avoids racing past events that arrive mid-run.
out="$(journalctl -k --after-cursor="$CUR" -o short-iso --show-cursor -q 2>/dev/null || true)"

# Advance the saved cursor to the newest entry we just read.
newcur="$(printf '%s\n' "$out" | sed -n 's/^-- cursor: //p' | tail -1)"
if [[ -n "$newcur" ]]; then
  printf '%s\n' "$newcur" > "${CURSOR}.tmp" && mv "${CURSOR}.tmp" "$CURSOR"
fi

# Filter out the trailing cursor line, keep only over-current matches.
new="$(printf '%s\n' "$out" | grep -v '^-- cursor:' | grep -iE "$PATTERN" || true)"

if [[ -n "$new" ]]; then
  cnt="$(printf '%s\n' "$new" | grep -c .)"
  {
    echo "=== $(date -Is) — ${cnt} new USB over-current event(s) ==="
    printf '%s\n' "$new"
  } >> "$LOG"
  # Surface in the journal too, at warning priority, so it's greppable and
  # shows up alongside the scanner units.
  printf '%s new USB over-current event(s) detected (see %s)\n' "$cnt" "$LOG" \
    | systemd-cat -t scanner-usb-oc-watch -p warning
fi
