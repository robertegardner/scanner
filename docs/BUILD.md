# Build plan

Staged implementation. Each stage produces something useful on its own,
so we can stop at any point and have a working system at that level.

## Stage 0 — Hardware ready

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

## Stage 1 — Repository skeleton

This is the current state. Repo exists, docs in place, no code yet.

## Stage 2 — Standalone EMS scanner

Get a working P25 trunked decoder running as its own systemd service,
independent of the scheduler infrastructure. This validates the antenna,
the dongle, and the software stack.

**Actions:**
1. Install SDRTrunk (or op25) on the Pi
2. Configure for Cape County MOSWIN (talkgroups from radioreference.com)
3. Test reception: at minimum the MOSWIN control channel should decode
4. Set up Icecast mount for streamed audio
5. Wire it as a systemd service with restart on failure

**Done when:** you can listen to Cape County dispatch via a browser stream.

This is genuinely useful on its own as a standalone scanner.

## Stage 3 — Standalone NOAA APT

Same idea — get satellite imagery capture working as a one-shot before
worrying about scheduling.

**Actions:**
1. Install `noaa-apt` from Cargo or precompiled binary
2. Install `pyorbital` for pass prediction
3. Write a script that captures a single named pass: tune 137.x MHz, record
   for N seconds, save raw WAV
4. Decode WAV to PNG, store in `/var/lib/scanner/noaa/`
5. Test manually: predict the next pass, kill the EMS scanner from Stage 2,
   run the capture script, validate the output

**Done when:** you have at least one decoded image from one real pass.

## Stage 4 — Scheduler MVP

Now wire Stages 2 and 3 together with a scheduler that owns the SDR.

**Actions:**
1. Build the scheduler skeleton with a priority queue
2. Wrap the EMS scanner from Stage 2 as a Job class (priority 1, perpetual)
3. Wrap the NOAA APT from Stage 3 as a Job class (priority 5, scheduled per pass)
4. Pass-predictor service that injects upcoming NOAA passes into the queue
5. systemd unit for the scheduler
6. Update Stage 2's EMS scanner to be preempt-able

**Done when:** the scheduler runs EMS by default, automatically preempts
for NOAA passes, and resumes EMS after.

## Stage 5 — Flask UI

Now we add the dashboard.

**Actions:**
1. Flask app on port 8081
2. Dashboard showing current job and upcoming schedule
3. NOAA gallery page (lists captured images)
4. Manual override form: tune to X for Y seconds
5. NPMplus proxy for `https://scanner.rg2.io` (optional)

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

- NOAA TLE auto-update via cron
- Pass elevation filtering (skip passes below e.g. 20° max elevation)
- False-color NOAA imagery rendering
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
