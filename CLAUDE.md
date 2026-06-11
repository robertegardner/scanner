# CLAUDE.md

Project memory for [Claude Code](https://code.claude.com). Read this first when
starting a new session.

## What this project is

Multi-purpose secondary SDR scanner running on a Raspberry Pi 5 in the attic.
Time-slices a single RTL-SDR dongle (the spare Nooelec NESDR SMArt v5) across
several radio reception jobs that don't need continuous receive time. A
scheduler owns the SDR; jobs are plugin-style modules.

A Flask web UI lets the operator see what's running, override the scheduler
to listen to anything manually, and browse captured artifacts (EMS call log,
AIS history, ACARS logs).

This project shares hardware with the FM/AM broadcast radio project at
[robertegardner/radio](https://github.com/robertegardner/radio). They run on
the same Pi but are independent codebases. The radio uses an SDRplay RSPdx-R2;
this project uses the spare Nooelec on a separate USB port.

## V2 (scanner-compute era) — START HERE as of 2026-06-10

P25 decoding moved OFF the Pi to the `scanner-compute` LXC (192.168.6.83):
op25 (boatbod) decodes MOSWIN from the RTL2838 served over SoapyRemote, audio
publishes to the rack Icecast `/ems.mp3`. The platform repo
(`../platform`, see its CLAUDE.md) owns ALL the infrastructure (container,
op25 build/units/config, registry); THIS repo owns scanner app code.

- **`v2/scanner_api.py`** — V1-contract REST bridge on .83:8081 feeding the
  Android app (`~/radio-android`): live talkgroup status from op25's http
  terminal, call EVENTS (no recordings yet), graceful stubs for
  monitor/squelch until the Airspy R2. Deploy: **`v2/deploy.sh`** (ships via
  the thebeast SSH jump + restarts `scanner-api`; the unit/env are
  platform-provisioned — change them in ../platform).
- **The V1 stack below still exists on the Pi** but is RETIRED from live
  duty: `SCHEDULER_EMS_DEFAULT=false`, SDRTrunk off, the dongle is owned by
  `sdr-source@rtl-2838` 24/7. **NEVER start the V1 EMS/monitor jobs or
  re-enable autopilot** — the scheduler's dongle handling (usbreset) yanks
  the RTL out from under the source server and wedges rack op25. The Pi UI
  (:8081) remains useful as the read-only legacy call/recordings archive.
- V2 roadmap: edge-record `/ems.mp3` on tgid boundaries (tappable calls in
  the app), then full scanner v2 on Airspy R2 arrival (multi_rx, category
  mounts, aviation monitor + squelch, transcripts).

---

## Status (V1 — historical; superseded above)

**Stages 0–5 complete and running on the Pi.** As of 2026-05-27 the system is
in daily use for VHF aviation AM listening on the discone; EMS scanning works
when the discone is repositioned for 700 MHz.

**2026-06-02 — MOSWIN P25 fully working + deployed (PR #2 `feat/moswin-p25-listen`):**
The discone decodes MOSWIN at 769 MHz beautifully (+44 dB CC, ~0.1% sync loss).
Key fixes/finds: control channel is **C4FM not CQPSK** (the old EMS playlist
never decoded), on-air **NAC is 0x1CC** (not 0x1C3), op25 doesn't fit on disk so
**SDRTrunk** is the decoder, **JMBE codec built** (`/var/lib/scanner/jmbe/jmbe.jar`)
so P25 voice decodes. New **`/listen`** page = source switcher (MOSWIN default +
aviation on demand) over Icecast, with **category sub-streams** (All/Fire/Law/…
from `files/opt/scanner/p25/moswin_talkgroups.tsv` via `gen_aliases.py`),
**talkgroup labels**, and a working **call log**. `SCHEDULER_EMS_DEFAULT=true`
runs MOSWIN as the always-on background default (NOAA stays off). **Coexistence
with the radio:** see the SDRplay note under "Hardware constraints" — SDRTrunk
would grab the radio's RSPdx-R2 without the libsdrplay_api.so perm restriction
(now in bootstrap.sh). Icecast `<sources>` raised 2→10 for the category mounts.

**2026-06-07 — MOSWIN tuner-wedge fix (commits `dbe3ad9` + `c7f659d`):**
ATC (rtl_fm) and MOSWIN (SDRTrunk) share the one Nooelec, and an rtl_fm↔SDRTrunk
hand-off intermittently wedges the R820T so SDRTrunk's I2C tuner init fails
(`USB error 1: error writing byte buffer` → `No Tuner Available`) — a silent
tunerless zombie with mounts still "Connected". Fix: `lib/sdr.py reset_dongle()`
(`sudo usbreset 0bda:2838` + 3s settle) is now called from the scheduler `_loop`
before *every* job acquires the dongle (covers both directions + NOAA/manual);
needs the `usbreset` sudoers line (colon escaped). The `EMSJob` watchdog now
faults on `No Tuner Available`/`Unable to start`/`auto-start failed` →
requeue-with-reset. **Gotcha:** the scheduler reads SDRTrunk's STDOUT (console
layout, no thread-name column), so watchdog markers must be stdout-visible —
success is keyed off the channelizer line (`ComplexPolyphaseChannelizer …
providing [N] channels`), not the file-only `[sdrtrunk channel …]` tag (that
mistake false-restarted EMS every ~50s; fixed in c7f659d). Full writeup:
`notes/2026-06-07-moswin-tuner-wedge-fix.md`.

**Implemented + live:**
- Scheduler with priority preemption, EMS job (SDRTrunk),
  ManualJob (raw WAV capture), MonitorJob (live ffmpeg → Icecast stream).
- Flask web UI on port 8081: dashboard with status/queue/activity,
  Aviation + Cape County preset banks, live monitor stream player,
  Record button + `/recordings` MP3 library with disk-usage warning,
  separate `/monitor` tuner page, `/calls` (EMS call log, now with a
  transcript column), `/transcript` (transcript log).
- Audio post-processing chain (ffmpeg) for the live monitor: AM uses
  band-limit + **de-ring notches** + agate squelch + compand + dynaudnorm +
  alimiter; FM uses a gentler chain. All filters live in env vars
  (`MONITOR_AUDIO_FILTER_AM`, `MONITOR_AUDIO_FILTER_FM`,
  `MONITOR_AUDIO_SQUELCH`) using a `{squelch}` placeholder so the
  dashboard Squelch toggle restarts the pipeline with/without the gate
  (~2s gap; agate's threshold isn't a runtime command). NOTE: env is read
  once at scheduler **startup** (systemd `EnvironmentFile`), so editing a
  filter needs `systemctl restart scanner-scheduler` + a re-tune, not just a
  toggle.
- **AM de-ring + weak-signal tuning (2026-06-02):** the Nooelec injects a
  fixed digital birdie + harmonics into AM audio at **464 / 898 / 1797 / 2695
  Hz** (898 Hz fundamental ×n). With the gate off, dynaudnorm pumped it ~15×
  in dead air → a "high-pitched ring." Fix: narrow `equalizer` notches
  (−26..−34 dB) **before** the gate, so the gate then judges clean silence and
  its threshold could drop 0.06→0.015 (~−36 dBFS). The old 0.06 (≈−24 dBFS) sat
  above both the birdie *and* quiet KCGI Tower voice, so squelch-on muted the
  tower entirely — that fix made squelch-on pass tower traffic. **KCGI Tower is
  RF/antenna-limited, not DSP-limited:** it's a low-power ground station that
  arrives far weaker than airborne aircraft at the attic discone. Chasing its
  loudness by lifting the compand low end (tried at -35/-22 and -45/-16) and by
  raising RF gain (40→49) both made it *quieter* — boosting the noise floor
  makes dynaudnorm/alimiter ride the whole signal down (same finding as the
  2026-05-27 chain tuning). Compand left at the -22/-18 sweet spot; real fix is
  antenna-side (reposition discone / dedicated airband antenna). **Tower presets
  use RF gain 40** (dashboard `index.html`); `/api/monitor/tune` defaults to 20.
- `SCHEDULER_AUTOPILOT=false` mode: scheduler stays idle, no EMS
  auto-start. The HTTP API stays up for manual
  tuning. Flip via `/etc/scanner/config.env` and restart.
- **Transcription** (`scanner-transcribe.service`, PR #5): Whisper captions +
  EMS call text. The Pi has no GPU, so it **reuses the radio's remote
  faster-whisper host** (`WHISPER_URL=http://gti-ai.srvr:8088`, same token as
  the radio's `captions.env`); degrades gracefully when that box is offline.
  Each SDRTrunk call MP3 is transcribed whole → sidecar under
  `/var/lib/scanner/transcripts/calls/` that `recent_calls()` shows on `/calls`;
  the live monitor stream is window-captioned. All lines append to
  `transcripts/YYYY-MM-DD.jsonl` (→ `/transcript`); the latest live caption is
  mirrored to `/run/scanner/transcribe.json` (→ rolling overlay on `/listen`
  and `/monitor`). The call loop runs **newest-first** so live calls caption
  immediately while older calls backfill. NOTE: EMS recordings actually live at
  `/var/lib/scanner/SDRTrunk/recordings` (per `EMS_RECORDINGS_DIR`), not the
  `/var/lib/scanner/ems/recordings` code default.
- systemd units enabled; services start on boot.

**Hardware:**
- Discone connected and in use for aviation AM (118–137 MHz). It works great
  for aviation.
- Spectrum scan via `rtl_power` confirms a healthy aviation band with
  20+ active ARTCC/approach channels above the noise floor in a
  weekday-morning window. KCGI tower (125.525) traffic confirmed audible.

**NOAA APT REMOVED (2026-06-08).** The APT birds (NOAA-15/18/19) are
end-of-life, and the discone physically can't hear a 137 MHz LEO sat anyway
(confirmed 2026-06-01: two passes flat, an 83.8° near-overhead NOAA-18 Doppler
sweep showed max band-SNR 0.02 dB — its zenith null + vertical-vs-RHCP mismatch).
**Weather-satellite imagery moved to the radio project** (Meteor LRPT on a
V-dipole on the SDRplay RSPdx-R2). All NOAA APT code (`jobs/noaa_apt.py`,
`lib/pass_predictor.py`, the scheduler pass-watcher, `/gallery`, `/passes`,
pyorbital + the noaa-apt binary, TLE handling) was deleted from this repo; this
project no longer does any satellite work.

**Known stubs:**
- `jobs/ais_poll.py` — Stage 6
- `jobs/acars_poll.py` — Stage 7
- P25 trunked decoding integration via SDRTrunk works for EMS but
  requires discone repositioned to 700 MHz.

**Next work:** AIS poll job (Stage 6);
antenna-switching automation (currently manual); MOSWIN listening when discone is
on 700 MHz.

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
    │   │   ├── ais_poll.py            ← marine AIS polling
    │   │   └── acars_poll.py          ← optional, aircraft text messages
    │   ├── lib/
    │   │   ├── sdr.py                 ← SDR ownership / locking primitives
    │   │   └── queue.py               ← job queue with priority preemption
    │   └── templates/
    │       └── index.html             ← dashboard
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
  - Aviation AM: 118–137 MHz
  - ACARS: 131.4-131.7 MHz
  - AIS: 161.975 + 162.025 MHz
  - NOAA WX: 162 MHz (deprioritized — phone alerts handle this)
  - MOSWIN P25 (Cape County): 769–771 MHz (700 MHz band, NOT VHF)
  A discone rated 25–1300 MHz covers all of these. A VHF dipole
  tuned for 140 MHz will NOT receive the 700 MHz MOSWIN signal.
  (NOAA APT weather-sat reception at 137 MHz was removed — moved to the
  radio project; see Status.)

## Design conventions

Match the radio project for consistency:

- **Python:** stdlib + Flask + requests + a few job-specific libraries.
  Keep deps minimal.
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
                │   │ 2. AIS poll (scheduled)      │     │
                │   │ 3. ACARS poll (scheduled)    │     │
                │   │ 4. EMS scanner (default)     │     │
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
job. AIS and ACARS polls are scheduled by cron-like triggers (every N minutes).
Manual overrides from the UI inject into the queue at top priority.

### Why a scheduler instead of just systemd timers

Two reasons:
1. **Exclusive SDR access.** Multiple services trying to open the same device
   would conflict. The scheduler centralizes that.
2. **Preemption logic.** A scheduled poll (or manual tune) should interrupt the
   EMS scanner mid-stream if it's running. systemd's `Conflicts=` directive can
   do this but gets fiddly with many interacting services; one Python process
   with a queue is simpler.

### Job interface (planned)

Each job module exports a class with a standard interface so the scheduler can
dispatch them uniformly:

```python
class Job:
    name: str          # e.g. "ems_scanner"
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
Control channel: 769.16875 MHz (Site 033 primary). NAC: 0x1CC (460, confirmed
on-air 2026-06-01; earlier 0x1C3 was wrong). Modulation: **C4FM** ("Normal") —
confirmed on-air; CQPSK/LSM does NOT decode this CC (see docs/P25_BRINGUP.md).
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

### NOAA APT — REMOVED (2026-06-08)

Weather-satellite imagery is **no longer part of this project.** NOAA-15/18/19
APT was removed because the birds are end-of-life and the discone physically
can't hear a 137 MHz LEO sat (zenith null + vertical-vs-RHCP polarization
mismatch — see Status). Weather-sat work moved to the sibling **radio project**
(Meteor LRPT on a V-dipole on the SDRplay RSPdx-R2). All code, the pass
predictor, the `/gallery` page, pyorbital, and the noaa-apt binary are gone.

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
- `/calls` — EMS call log with playback
- `/ships` — AIS history (with optional map)

**API endpoints:**
- `GET /api/status` — current job, queue, next scheduled item
- `POST /api/override` — preempt scheduler with manual tune
- `POST /api/release` — release manual override
- `GET /api/calls?recent=N` — recent EMS calls

## Common operations

These are planned; will be real once implementation lands.

```bash
# Watch the scheduler
sudo journalctl -u scanner-scheduler -f

# Reload code after editing
sudo /srv/scanner/deploy.sh

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
- **SDRTrunk vs the radio's SDRplay.** SDRTrunk (the EMS/MOSWIN job) only uses
  the Nooelec, but on startup it loads `libsdrplay_api.so` and enumerates the
  radio's RSPdx-R2 — which knocks the radio off its dongle (`rx_fm: "Device has
  been removed"`). `disabledTuners` stops SDRTrunk *streaming* the RSP but not
  this enumeration, and SDRTrunk has no path-override hook. Fix (in bootstrap.sh):
  restrict `/usr/local/lib/libsdrplay_api.so*` to `root:radio 750` so SDRTrunk
  (user `scanner`) can't load it and skips the RSP, while the radio (user `radio`,
  group `radio`) keeps it. With this in place MOSWIN + the radio run
  simultaneously on their separate dongles. **Re-apply after any SDRplay API
  reinstall** (it resets the perms to 644 and the conflict returns). Without it,
  enabling always-on MOSWIN (`SCHEDULER_EMS_DEFAULT=true`) takes the radio down.
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
- Captured artifacts (EMS call recordings, AIS logs, etc.)
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
3. **AIS database vs. feeding service.** Just log NMEA locally, or also
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
