# Build plan

Staged implementation. Each stage produces something useful on its own,
so we can stop at any point and have a working system at that level.

**Current state (2026-05-23):** Stages 0–5 complete and running.
Discone connected for MOSWIN 700 MHz + aviation AM.
Next: Stage 6 (AIS).

**NOAA APT removed (2026-06-08):** the original Stage 3 / Stage 4 NOAA APT work
(below) is no longer part of this project — the discone can't hear a 137 MHz LEO
sat, so weather-sat imagery moved to the sibling radio project (Meteor LRPT on a
V-dipole). The historical stages are left in place for narrative continuity but
marked removed; the scheduler runs EMS by default with no NOAA pass watcher.

## Stage 0 — Hardware ready ✓

**Prereqs:**
- Pi 5 already running the radio project at `/srv/radio` and `/opt/sdr-tuner`
- Spare Nooelec NESDR SMArt v5 (the original radio dongle)
- One wideband VHF antenna (see [BUILD.md "Antenna" section](#antenna))

**Actions:**
1. Mount antenna in attic, at least 3-4 feet from the FM whip and AM long-wire
2. Run coax to the Pi
3. Plug spare dongle into Pi's USB 3.0 port (the one not used by RSPdx-R2)
4. Confirm both dongles appear: `rtl_test -t` (Nooelec) and `SoapySDRUtil --find` (RSPdx-R2)
5. Confirm the radio project still works normally — sanity check

**Done when:** both SDRs visible, radio project unaffected.

## Stage 1 — Repository skeleton ✓

This is the current state. Repo exists, docs in place, no code yet.

## Stage 2 — Standalone EMS scanner ✓

Get a working P25 trunked decoder running under the scheduler. This
validates the antenna, the dongle, and the software stack.

### 2a — Look up Cape County MOSWIN on RadioReference

Go to: https://www.radioreference.com/db/browse/ctid/1265

Find the MOSWIN entry (Missouri Statewide Wireless Interoperable Network,
P25 Phase 1, VHF). Open the Cape Girardeau site. You need:

- **Control Channel frequency** — labeled "CC" in the site frequencies table.
  Write it down in Hz (e.g. 154.6250 MHz → `154625000`).
- **NAC** — Network Access Code, shown as hex (e.g. `0x293` → decimal `659`).
  This is optional but prevents the scanner from latching onto a neighboring
  system if the control channel is shared or busy.

### 2b — Fill in the playlist

Edit `/var/lib/scanner/sdrtrunk/playlists/cape-county.xml` (installed by
bootstrap.sh from the template in `files/etc/scanner/sdrtrunk-playlist.xml.example`):

```bash
sudo -u scanner nano /var/lib/scanner/sdrtrunk/playlists/cape-county.xml
```

Replace `TODO_CONTROL_CHANNEL_HZ` with the control channel in Hz.
Optionally set the `<nac>` element to the decimal NAC value.

### 2c — Test SDRTrunk headless

SDRTrunk 0.6.1 is a jlink distribution (not a fat jar). Run via the launcher:

```bash
sudo -u scanner env SDR_TRUNK_OPTS="-Xmx512m" \
  /opt/scanner/sdrtrunk/sdr-trunk-latest \
  --headless \
  --home /var/lib/scanner/sdrtrunk
```

Watch the log output. Within 30–60 seconds you should see:
- `P25 Phase 1 Control Channel ... decoded` — the system is decoded
- Talkgroup activity lines — traffic is being received

If it starts but logs nothing about P25: the frequency is wrong, or the
antenna isn't picking up signal. Try scanning ±50 kHz around the expected
control channel frequency.

If SDRTrunk crashes on startup: the Java heap may be too small. Try `-Xmx768m`.

### 2d — Verify recordings

After a voice call is received, a recording appears under:
```
/var/lib/scanner/sdrtrunk/recordings/
```

Play one to confirm it decoded correctly:
```bash
aplay -r 8000 /var/lib/scanner/sdrtrunk/recordings/*/*.mp3 2>/dev/null || \
  mpg123 /var/lib/scanner/sdrtrunk/recordings/*/*.mp3
```

### 2e — Start the scheduler

```bash
sudo systemctl start scanner-scheduler scanner-ui
sudo journalctl -u scanner-scheduler -f
```

The dashboard is at `http://<pi-ip>:8081/`.

**Done when:** you see P25 control channel decoding in the logs and at least
one EMS call recording on disk.

## Stage 3 — Standalone NOAA APT ✓ (REMOVED 2026-06-08)

This stage built a one-shot NOAA APT satellite-imagery capture (`pyorbital`
pass prediction + `noaa-apt` WAV→PNG decode). It has since been **removed** —
the discone can't physically receive a 137 MHz LEO sat, so weather-sat imagery
moved to the sibling radio project (Meteor LRPT on a V-dipole). The job module,
pass predictor, gallery, and TLE plumbing are all gone.

## Stage 4 — Scheduler MVP ✓

Wire the EMS scanner together with a scheduler that owns the SDR.

**Actions:**
1. Build the scheduler skeleton with a priority queue
2. Wrap the EMS scanner from Stage 2 as a Job class (priority 1, perpetual)
3. systemd unit for the scheduler
4. Update Stage 2's EMS scanner to be preempt-able

(Originally this stage also wrapped a NOAA APT job + pass-predictor service that
injected upcoming passes into the queue; both were removed with Stage 3 — see
the 2026-06-08 note above.)

**Done when:** the scheduler runs EMS by default and yields to manual overrides,
then resumes EMS after.

## Stage 5 — Flask UI ✓

Now we add the dashboard.

**Actions:**
1. Flask app on port 8081
2. Dashboard showing current job and upcoming schedule
3. Manual override form: tune to X for Y seconds
4. NPMplus proxy for `https://scanner.rg2.io` (optional)

**Done when:** the dashboard accurately reflects what's running and
manual overrides work.

## Stage 6 — AIS plugin

Once the scheduler is solid, AIS is just another Job class.

**Actions:**
1. Install `rtl_ais` (or `aisdec`)
2. Write the AIS Job class: tune 161.975/162.025 MHz, run rtl_ais for N
   minutes, write NMEA log to disk
3. Schedule it via cron-like config in the scheduler
4. Dashboard page showing recent ships
5. Optional: feed MarineTraffic.com

**Done when:** AIS data is being captured periodically and visible in UI.

## Stage 7 — ACARS plugin (optional)

Try this for a week. If interesting, keep it; if not, disable.

**Actions:**
1. Install `acarsdec`
2. ACARS Job class
3. Log viewer page

**Done when:** ACARS messages are being logged. Decide whether to keep enabled.

## Stage 8 — Polish

Things that improve the system but aren't strictly necessary.

- Calls-log search and filtering
- AIS map overlay (Leaflet + tile server)
- Backup/archive script for captured artifacts

## Antenna

For Stage 0:

**Option A (cheapest):** RTL-SDR Blog Multipurpose Dipole Antenna Kit
- ~$18-25 from Amazon or rtl-sdr.com
- Telescopic elements, adjustable for any band 30-1500 MHz
- Set elements to ~50 cm each for quarter-wave at ~140 MHz
- Performance: good for single-band tuning, not as wideband as a discone

**Option B (better):** Generic scanner discone
- Search Amazon for "discone antenna scanner" — pick one ~$70-90
- Tram 1410 or Workman D-1000 are well-regarded options
- Skip Diamond D-3000N at $150+ unless you also want transmit capability
- Better all-bands performance, mounts on a mast

**Option C (DIY):** 3D-printed discone (or coat hangers + plywood disk)
- ~$15 in materials
- An afternoon's work
- Performs the same as a $90 commercial discone for receive-only
- Worth doing if you're 3D printer-curious

Recommendation: start with Option A if you're impatient, go Option B if
you can wait for shipping and want better wideband performance.
