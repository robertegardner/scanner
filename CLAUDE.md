# CLAUDE.md

Project memory for [Claude Code](https://code.claude.com). Read this first when
starting a new session.

## What this project is

Multi-purpose secondary SDR scanner running on a Raspberry Pi 5 in the attic.
Time-slices a single RTL-SDR dongle (the spare Nooelec NESDR SMArt v5) across
several radio reception jobs that don't need continuous receive time. A
scheduler owns the SDR; jobs are plugin-style modules.

A Flask web UI lets the operator see what's running, override the scheduler
to listen to anything manually, and browse captured artifacts (NOAA images,
AIS history, ACARS logs).

This project shares hardware with the FM/AM broadcast radio project at
[robertegardner/radio](https://github.com/robertegardner/radio). They run on
the same Pi but are independent codebases. The radio uses an SDRplay RSPdx-R2;
this project uses the spare Nooelec on a separate USB port.

## Status

**Stages 0–5 complete and running on the Pi** (as of 2026-05-23).

- Scheduler, EMS job, NOAA APT job, pass predictor, Flask UI — all implemented and deployed.
- systemd units enabled; services start on boot.
- VHF dipole connected: NOAA APT captures working. **Discone arriving soon** for MOSWIN 700 MHz coverage.
- First NOAA satellite images expected imminently (pass watcher is live, TLEs current).

Next work: verify NOAA image quality once first pass completes, then MOSWIN P25 decoding once discone arrives, then Stage 6 (AIS).

## Where things will go

```
/srv/scanner/                          ← git checkout (clone target on Pi)
├── README.md
├── CLAUDE.md                          ← this file
├── LICENSE
├── .gitignore
├── deploy.sh                          ← syncs files/opt/scanner → /opt/scanner
├── bootstrap.sh                       ← first-install: apt deps, systemd units, etc.
├── docs/
│   ├── ARCHITECTURE.md
│   ├── BUILD.md
│   └── JOBS.md
└── files/
    ├── opt/scanner/                   ← deploys to /opt/scanner/ on Pi
    │   ├── app.py                     ← Flask UI + JSON API on a TBD port (8081?)
    │   ├── scheduler.py               ← owns the SDR, dispatches jobs
    │   ├── jobs/                      ← plugin-style job implementations
    │   │   ├── __init__.py
    │   │   ├── ems_scanner.py         ← P25 trunked decoder, MOSWIN talkgroups
    │   │   ├── noaa_apt.py            ← satellite pass capture + image decode
    │   │   ├── ais_poll.py            ← marine AIS polling
    │   │   └── acars_poll.py          ← optional, aircraft text messages
    │   ├── lib/
    │   │   ├── sdr.py                 ← SDR ownership / locking primitives
    │   │   ├── pass_predictor.py      ← NOAA orbital ephemeris (pyorbital wrapper)
    │   │   └── queue.py               ← job queue with priority preemption
    │   └── templates/
    │       ├── index.html             ← dashboard
    │       └── gallery.html           ← NOAA image gallery
    └── etc/
        ├── systemd/system/
        │   ├── scanner-scheduler.service
        │   └── scanner-ui.service
        ├── sudoers.d/
        │   └── scanner
        └── scanner/
            ├── config.env.example
            └── talkgroups.json.example
```

Deploy model is the same as the radio project: source lives in `/srv/scanner`,
installed copy in `/opt/scanner/`, push via `deploy.sh`. Not symlinked.

## Hardware

- **Pi 5** in the attic (shared with the radio project). PoE-powered. Uses the
  last free PoE port up there.
- **Spare Nooelec NESDR SMArt v5** plugged into one of the Pi's USB 3.0 ports.
  The radio project's RSPdx-R2 uses the other USB 3.0 port.
- **One wideband VHF antenna** (discone or dipole kit) for this project. The
  dx-R2's antennas (Shakespeare 5120 + Cat 5 long-wire) are for radio only,
  no sharing.
- One wideband antenna covers all the jobs, but frequency range is wider
  than originally assumed:
  - NOAA APT: 137 MHz
  - ACARS: 131.4-131.7 MHz
  - AIS: 161.975 + 162.025 MHz
  - NOAA WX: 162 MHz (deprioritized — phone alerts handle this)
  - MOSWIN P25 (Cape County): 769–771 MHz (700 MHz band, NOT VHF)
  A discone rated 25–1300 MHz covers all of these. A VHF dipole
  tuned for 140 MHz will NOT receive the 700 MHz MOSWIN signal.

## Design conventions

Match the radio project for consistency:

- **Python:** stdlib + Flask + requests + a few job-specific libraries
  (`pyorbital`, `noaa-apt-decoder`, etc.). Keep deps minimal.
- **State files use atomic writes** (write `.tmp`, then `os.replace()`).
- **`/run/scanner/*`** is transient (tmpfs); persistent state in `/var/lib/scanner/`.
- **The Flask app never owns the SDR directly.** It talks to the scheduler
  via a Unix socket or a tiny REST-on-localhost API. The scheduler owns the
  SDR exclusively.
- **The `scanner` user runs all services.** Passwordless sudo only for the
  specific systemctl operations needed (start/stop/restart of scanner units).
- **HTML templates over SPAs.** Server-rendered + vanilla JS polling JSON
  APIs. No build step.

## Architecture (planned)

The scheduler is the heart of the system:

```
                ┌────────────────────────────────────────┐
                │           scanner-scheduler            │
                │                                        │
                │  owns the SDR exclusively              │
                │  maintains job queue                   │
                │  dispatches based on priority + time   │
                │                                        │
                │   ┌──────────────────────────────┐     │
                │   │  Job queue (priority sorted) │     │
                │   ├──────────────────────────────┤     │
                │   │ 1. manual override (highest) │     │
                │   │ 2. NOAA pass (scheduled)     │     │
                │   │ 3. AIS poll (scheduled)      │     │
                │   │ 4. ACARS poll (scheduled)    │     │
                │   │ 5. EMS scanner (default)     │     │
                │   └──────────────────────────────┘     │
                └────────────────┬───────────────────────┘
                                 │ owns
                                 ▼
                          ┌───────────────┐
                          │  Nooelec SDR  │
                          └───────────────┘

         ┌──────────────────────┐
         │   scanner-ui (Flask) │
         │   port 8081          │
         └──────────┬───────────┘
                    │ HTTP/Unix socket
                    ▼
         to scheduler: "tune to X for Y" or "show me status"
```

### Default behavior

When nothing higher-priority is scheduled, the scheduler runs the EMS scanner
job. NOAA passes are predicted via orbital ephemeris and queued automatically.
AIS and ACARS polls are scheduled by cron-like triggers (every N minutes).
Manual overrides from the UI inject into the queue at top priority.

### Why a scheduler instead of just systemd timers

Two reasons:
1. **Exclusive SDR access.** Multiple services trying to open the same device
   would conflict. The scheduler centralizes that.
2. **Preemption logic.** A NOAA pass should interrupt the EMS scanner mid-stream
   if it's running. systemd's `Conflicts=` directive can do this but gets fiddly
   with many interacting services; one Python process with a queue is simpler.

### Job interface (planned)

Each job module exports a class with a standard interface so the scheduler can
dispatch them uniformly:

```python
class Job:
    name: str          # e.g. "noaa_apt"
    priority: int      # higher = preempts lower
    duration_s: int    # how long it'll hold the SDR

    def run(self, sdr_handle) -> JobResult:
        """Called by scheduler when it's our turn. Owns the SDR for
        duration_s seconds. Returns artifacts (file paths, log entries)."""
```

This is sketched; actual interface evolves with the first implementation.

## Jobs

### EMS scanner (default)

**Frequency band:** Cape Girardeau County MOSWIN — **700 MHz, P25 Phase II**.
NOT VHF as originally assumed. Sites 033/055/060 all operate in the
769–771 MHz band (standard public safety 700 MHz).
Control channel: 769.16875 MHz (Site 033 primary). NAC: 0x1C3 (451).
System ID: 1CE, WACN: BEE00. RadioReference: https://www.radioreference.com/db/sid/6847

Cape County Private Ambulance (CCPA) still uses conventional 155.205 MHz
simplex (non-trunked).

**Software:** `SDRTrunk` is the modern P25 decoder; `op25` is the historical
alternative. Both work on Pi 5. SDRTrunk is JVM-based, easier to configure;
op25 is C/Python, lighter on CPU.

**Talkgroups to monitor (Cape Girardeau County):**
- Cape CO ALL
- Cape CO Fire Disp 1, Fire Disp 2
- Cape CO EMS
- E Fire 1
- Plus MO regional interop and statewide channels as desired

**Output:** stream audio of currently-active talkgroup to Icecast (mount
TBD), log all calls to disk with timestamps and talkgroup labels.

**Encryption status:** Cape Girardeau County's listed talkgroups appear to
be in the clear as of project planning (Nov 2025). Verify on RadioReference
before deployment.

### NOAA APT (scheduled)

**Satellites:** NOAA-15 (137.620 MHz), NOAA-18 (137.9125 MHz), NOAA-19
(137.100 MHz). All polar-orbiting, ~6 passes per day over Cape Girardeau
combined. Each pass is 10-15 minutes.

**Software:** `noaa-apt` (Rust) for image decoding. `pyorbital` for pass
prediction from TLE data. TLEs auto-update from celestrak.org or similar.

**Pipeline:**
1. Predictor maintains a list of upcoming passes
2. Scheduler queues a pass ~5 min before AOS (acquisition of signal)
3. Job tunes 137.x MHz at LOS-zenith time, records raw audio for the pass
   duration to a WAV file
4. After LOS, job decodes WAV to PNG image, optionally applies false-color
   maps, saves to `/var/lib/scanner/noaa/`

**Output:** dated/numbered PNG image per pass. Browse via the gallery page.

**Caveats:** NOAA-15/18/19 are well past design life. Could be decommissioned
any time. Probably years away but worth tracking. MetOp-B/C transmit a
similar mode (LRPT) but at 1.7 GHz and require a different antenna.

### AIS poll (scheduled, low priority)

**Frequency:** 161.975 + 162.025 MHz (channels 87B + 88B).

**Software:** `rtl_ais` for AIS message decode. Outputs NMEA which can be
piped to OpenCPN, a local database, or a service like MarineTraffic.com.

**Schedule:** every 30 min, 3-5 min polls. Mississippi River traffic is
slow enough that even sparse coverage captures useful data.

**Optional:** feed MarineTraffic.com (similar to feeding FlightAware) for
account perks. Set this up only if poll is enabled and a decent fraction
of vessels are being caught.

### ACARS poll (scheduled, optional)

**Frequency:** 131.55 + 131.725 + 131.725 + 131.45 MHz (US primary ACARS).

**Software:** `acarsdec` for message decoding. Logs to disk; optionally
forwards to ACARSdrama or airframes.io.

**Notes:** less interesting after the first day. Try for a week. Most traffic
is automated position/weather/fuel reports.

### NOAA Weather Radio (deprioritized)

Phone alerts handle SAME alerts well. Decoding weather radio adds little.
**Likely don't implement** unless there's a specific use case.

## Web UI

**URL:** `https://scanner.rg2.io` (proxy via NPMplus when ready, or just
`http://<pi-ip>:8081` for now).

**Pages (planned):**
- `/` — dashboard: current job, schedule, manual override controls
- `/gallery` — NOAA image gallery
- `/calls` — EMS call log with playback
- `/ships` — AIS history (with optional map)

**API endpoints:**
- `GET /api/status` — current job, queue, next scheduled item
- `POST /api/override` — preempt scheduler with manual tune
- `POST /api/release` — release manual override
- `GET /api/passes` — upcoming NOAA passes
- `GET /api/calls?recent=N` — recent EMS calls

## Common operations

These are planned; will be real once implementation lands.

```bash
# Watch the scheduler
sudo journalctl -u scanner-scheduler -f

# Reload code after editing
sudo /srv/scanner/deploy.sh

# Update NOAA TLEs (cron job, but manual run):
sudo -u scanner /opt/scanner/scripts/update_tles.sh

# Force a manual tune from CLI
curl -X POST http://localhost:8081/api/override \
  -d '{"freq": "155.205M", "mode": "fm", "duration_s": 120}'
```

## Things to know about the dongle

Same gotchas as the radio project:

1. **DVB kernel driver auto-claims the device.** Already blacklisted at the
   OS level for the radio project; that blacklist covers this dongle too.
2. **Device identification.** Two RTL-SDR-class dongles on the Pi if you
   count the radio Pi's dx-R2 as non-RTL — but the dx-R2 uses SoapySDR
   API not librtlsdr, so they don't collide. The Nooelec is RTL device
   index 0 on this Pi.

## Hardware constraints to remember

- **The Pi runs the radio project too.** Don't break that. The scanner
  must not crash, hang, or consume so much CPU that the radio stutters.
- **USB bandwidth.** The Pi 5 has two USB 3.0 ports. dx-R2 on one,
  Nooelec on the other. Pi 5's USB 3.0 is 5 Gbps; either dongle alone
  is well under 30 Mbps. No contention.
- **Antenna positions.** All antennas live in the attic. The discone for
  this project should be separated from the FM whip (Shakespeare 5120)
  and the AM long-wire by at least 3-4 feet to avoid mutual coupling.
- **Cooling.** The Pi is in an attic. Summer temperatures may hit 50°C+.
  The UCTRONICS PoE HAT has a fan; verify it's running when ambient gets
  high.

## What's not in git (will be gitignored)

- `*.env` files in `files/etc/scanner/`
- `talkgroups.json` (the actual one, not `.example`)
- NOAA TLE cache files
- Captured artifacts (`/var/lib/scanner/noaa/*.png`, AIS logs, etc.)
- The `.claude/` directory if Claude Code creates one

## Open design questions for first build session

These should be resolved when implementation actually begins:

1. **SDR ownership primitive.** Python `multiprocessing.Lock`? File lock?
   Just run all jobs in the scheduler process so contention is impossible?
2. **EMS scanner integration.** SDRTrunk is JVM-heavy. Does it play
   nicely as a child process of the scheduler, or do we run it as its
   own systemd unit and use it as an external consumer of the SDR
   (in which case the scheduler shuts it down before other jobs and
   restarts it after)?
3. **NOAA pass scheduling resolution.** Predict at 1-second granularity?
   1-minute? Some passes are very low elevation and not worth capturing.
   Configurable minimum elevation threshold (probably ~20°).
4. **AIS database vs. feeding service.** Just log NMEA locally, or also
   send to MarineTraffic? Latter requires their API and an account.
5. **Talkgroup discovery.** Hard-code the list from RadioReference, or
   auto-detect from the control channel and let user label them in the UI?

## Companion: the radio project

This project's sibling is at [robertegardner/radio](https://github.com/robertegardner/radio).
They share:
- The same Pi 5 in the attic
- The DVB driver blacklist (only needs to be set once for the system)
- The deployment conventions (see radio's `CLAUDE.md` for patterns)
- The Cape Girardeau location and antenna placement constraints

They do NOT share:
- Code (separate repos, separate `/srv` directories)
- The `radio` vs. `scanner` system users
- SDR hardware (different dongles)
- Antennas (separate feedlines)
- Systemd units
- Flask UIs (different ports, different domains)

When working on this project, **don't modify anything in `/srv/radio` or
`/opt/sdr-tuner`**. They belong to the radio project.
