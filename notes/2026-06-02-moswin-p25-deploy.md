# 2026-06-02 — MOSWIN P25: bring-up → full deployment

Took MOSWIN P25 from "unproven on the discone" to a working, always-on,
web-controlled listening feature that coexists with the radio project. PR #2
(`feat/moswin-p25-listen`). Radio-side companion: radio PR #1.

## Bring-up findings (the discone CAN do 769 MHz)
- `rtl_power` sweep: control channel at 769.16875 MHz sits **+44 dB** over a flat
  noise floor — one of the strongest signals on the band. (Opposite of NOAA APT.)
- Decode lock **~0.1% sync loss**. Live group calls, full system identity.
- **Control-channel modulation is C4FM ("Normal"), NOT CQPSK/LSM.** CQPSK gave
  ~98% sync loss — the production EMS playlist used CQPSK, so it had never
  decoded MOSWIN. This was the single biggest fix.
- **On-air NAC is 0x1CC (460), not 0x1C3 (451)** as previously documented.
  System 0x1CE / WACN 0xBEE00 confirmed; P25 Phase II (TDMA).
- **op25 doesn't fit on disk**: it needs GNU Radio (~1.49 GB / 475 pkgs) and the
  SD card had ~0.8 GB free. Used **SDRTrunk** (already installed) instead.

## What was built/deployed
- **C4FM fix** in the EMS playlist (template + live) + NAC corrected.
- **JMBE codec** built from source (`dsheirer/jmbe` 1.0.9) — built in `/dev/shm`
  (RAM) because the SD card was too full for gradle; needed gradle 8.10.2 (repo's
  7.4 can't run on JDK 21). Installed to `/var/lib/scanner/jmbe/jmbe.jar`, wired
  via SDRTrunk's `path.jmbe.library` preference. P25 voice now decodes.
- **`/listen` page** — source switcher on the one Nooelec: MOSWIN P25 (default,
  always-on via `SCHEDULER_EMS_DEFAULT=true`) + Aviation AM on demand, played
  through Icecast. Scheduler gained `start_moswin()` + `/source/moswin`.
- **Always-on MOSWIN without NOAA**: `SCHEDULER_EMS_DEFAULT` (NOAA stays off —
  the discone can't hear 137 MHz).
- **Call log fixed**: SDRTrunk now records playable per-call `.mp3` (added the
  `record` alias-id; dropped the raw-`.mbe` recorder) and `EMS_RECORDINGS_DIR`
  points at the real `…/SDRTrunk/recordings` (capital S). `/calls` + the
  recent-calls panel populate and play.
- **Talkgroup labels**: `files/opt/scanner/p25/moswin_talkgroups.tsv` is the
  single source of truth; `gen_aliases.py` turns it into SDRTrunk aliases AND
  category streams; the app maps TGID→label for the call log (hot-reloaded).
- **Live-stream silence fix**: the playlist `<stream>` had
  `maximum_recording_age="0"`, so SDRTrunk's broadcaster aged-off (discarded)
  every audio segment → silent stream. Set to 600000 ms.
- **Category sub-streams**: one Icecast mount per talkgroup group
  (`/ems.mp3` All + `/ems-fire.mp3`, `/ems-police.mp3`, …), selectable on
  `/listen`. Required raising Icecast `<sources>` 2→10.

## Coexistence with the radio (separate dongles, but they fought)
SDRTrunk only uses the Nooelec, but on startup it loads `libsdrplay_api.so` and
enumerates the radio's RSPdx-R2 — kicking the radio off its own device
(`rx_fm: "Device has been removed"`). `disabledTuners` doesn't stop the
enumeration, and SDRTrunk has no path override. **Fix (bootstrap.sh):** restrict
`/usr/local/lib/libsdrplay_api.so*` to `root:radio 750` so the `scanner` user
can't load it and SDRTrunk skips the RSP; the radio (group `radio`) keeps it.
**Verified: always-on MOSWIN + the radio's FM stream run simultaneously.**
Re-apply the chmod after any SDRplay API reinstall (resets perms to 644).

## Gotchas / notes for next time
- The SD card is chronically near-full (~0.8 GB free); build heavy things in
  `/dev/shm`. The build-only JDK was apt-removed afterward (~300 MB reclaimed).
- A transient USB re-enumeration during testing dropped both dongles at once;
  worth watching whether sustained dual-SDR load stresses USB power on the Pi.
- The radio's `rx_fm` doesn't self-exit on device loss — fixed on the radio side
  (radio PR #1) so it self-heals from transient losses.

## Still open
- Fill in the busy talkgroups (3402, 4203, 6703, 6721, 6108, 4241) in the TSV
  from a RadioReference account (labels hot-reload; regen aliases for stream
  titles). AIS (Stage 6) / ACARS (Stage 7) still stubbed.
