# 2026-06-08 — NOAA APT removed (moved to the radio project)

Fully removed NOAA APT weather-satellite reception from the scanner project.
Weather-sat imagery now lives in the sibling **radio** project, decoding
**Meteor LRPT** on a **V-dipole on the SDRplay RSPdx-R2**.

## Why
Two independent reasons, both decisive:

1. **The birds are end-of-life.** NOAA-15/18/19 APT are all well past design
   life and being decommissioned.
2. **This project's antenna physically can't hear them** (confirmed 2026-06-01).
   The discone is the wrong antenna for a 137 MHz LEO sat:
   - A 47° NOAA-15 capture (corrected recipe `-s 60000 -F 9 -A fast -g 40`) was
     pure noise — flat spectrogram, RMS dead flat, no TCA bulge.
   - An `rtl_power` Doppler sweep over an **83.8° near-overhead NOAA-18 pass**
     (best possible geometry, max gain 49.6) detected **no carrier at all** —
     max in-band band-SNR **0.02 dB**, flat the whole pass, zero Doppler drift.

   The receive chain is healthy (aviation AM at 132 MHz is solid daily), so the
   discone's **zenith null + vertical-vs-RHCP polarization mismatch** is the
   cause. Getting APT working here would have required a dedicated 137 MHz RHCP
   antenna (QFH/turnstile) — and with the birds dying and the radio project's
   SDRplay/V-dipole already positioned for Meteor LRPT, that work moved there
   instead.

## What was removed
**Code (deleted):**
- `files/opt/scanner/jobs/noaa_apt.py` — the APT capture/decode job
- `files/opt/scanner/lib/pass_predictor.py` — pyorbital pass prediction + TLE handling
- `files/opt/scanner/templates/gallery.html` — the NOAA image gallery page

**Code (excised):**
- `scheduler.py`: the `from jobs.noaa_apt`/`from lib.pass_predictor` imports, the
  `_pass_watcher` thread (and its `start()` launch), NOAA auto-queueing, the
  `noaa_data_dir` Config field, the `NOAAJob` checks in `start_moswin()`,
  `upcoming_passes` in `status()`, the `/passes` route, and the now-unused
  `timedelta`/`timezone`/`update_tles` imports.
- `app.py`: `NOAA_DATA_DIR`, the `/gallery` route, `/api/passes`, and the
  `/noaa/<date>/<filename>` image-serving route.
- All 6 templates: the **Gallery** nav link (`index`, `listen`, `calls`,
  `transcript`, `monitor`, `recordings`). Two stale NOAA comments generalized.
- `config.env.example`: `NOAA_DATA_DIR` + autopilot comment cleanup.
- `bootstrap.sh`: the `/var/lib/scanner/noaa*` dir creation, the `pyorbital` pip
  dependency, and the entire `noaa-apt` binary download/install block.

**Docs:** `CLAUDE.md` + `README.md` + `docs/{JOBS,ARCHITECTURE,CONTEXT,BUILD,
P25_BRINGUP}.md` + `hardware/docs/antenna_switch_build.md` updated to record the
removal rather than silently drop it. **`NOAA WX` (162.550 MHz weather-radio FM
monitor preset) was intentionally KEPT** — that is a different thing from APT
satellite imagery.

Net diff: ~18 files, −357/+122, 3 files deleted. `py_compile` + import smoke
test (`Config.from_env()` has no `noaa_data_dir`) both clean.

## Runtime leftovers on the Pi (NOT cleaned by this change)
The new `bootstrap.sh` no longer creates/installs these, but the existing Pi
still has them — safe to delete by hand whenever:
- `/var/lib/scanner/noaa/` (noise test images + `weather.tle` cache)
- `/var/lib/scanner/aptpower/` (the 2026-06-01 Doppler-sweep diagnostic)
- `/usr/local/bin/noaa-apt` (the decoder binary)
- `pyorbital` in `/opt/scanner/venv`

## Not in scope
The dongle was wedged off the USB bus (unenumerated, `error -110`) during this
work — unrelated hardware issue needing a physical replug / PoE cycle. This
change deploys fine regardless; deploy + restart after the dongle is back so you
can confirm EMS returns on the new code.
