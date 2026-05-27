# Scanner Project — Context for claude.ai Conversations

A standalone snapshot you can paste/attach when starting a new claude.ai
conversation about this project. Self-contained enough that the AI doesn't
need to read the rest of the repo first.

For a richer technical reference see `ARCHITECTURE.md`, `BUILD.md`,
`JOBS.md`. For day-to-day Claude Code sessions see `../CLAUDE.md`.

Last refreshed: 2026-05-27

---

## What this project is

A multi-purpose secondary SDR scanner on a Raspberry Pi 5 in an attic. One
RTL-SDR dongle (Nooelec NESDR SMArt v5) is time-sliced across several
reception jobs that don't need continuous receive time:

- **EMS scanner** — Cape County MOSWIN P25 Phase II trunked, 769.16875 MHz
  control channel. Runs via SDRTrunk headless. (Needs discone on 700 MHz.)
- **NOAA APT** — 137 MHz satellite imagery during predicted passes.
- **Aviation AM** — 118–137 MHz manual listening. Currently the primary use.
- **AIS, ACARS** — planned, stubbed.

A scheduler owns the SDR exclusively and dispatches jobs by priority with
preemption. A Flask UI lets the operator see status, tune presets, listen
live via Icecast, and browse recordings/captures.

The Pi runs a sibling **radio project** at `/srv/radio` (FM/AM broadcast
with the SDRplay RSPdxR2). These two projects are independent codebases
sharing hardware. Don't touch `/srv/radio` from this repo.

Owner: Robert Gardner. Cape Girardeau, MO. Linux/Python/systemd
comfortable; prefers minimal deps, server-rendered HTML, atomic file
writes, deploy-from-source (no symlinks).

## Architecture in one diagram

```
                    ┌──────────────────────────────┐
                    │   scanner-scheduler          │  Python, port 8082
                    │   ─────────────────          │  (HTTP API only on localhost)
                    │   - owns the SDR             │
                    │   - priority queue           │
                    │   - dispatch + preemption    │
                    │   - NOAA pass watcher        │
                    │   - RecordingManager         │
                    │   - audio_squelch state      │
                    └────────────┬─────────────────┘
                                 │ exclusive
                                 ▼
                          ┌───────────────┐
                          │  Nooelec SDR  │  (index 0, serial 22012952)
                          └───────────────┘

       ┌──────────────────────────────┐
       │  scanner-ui (Flask)          │  port 8081
       │  ────────────────            │
       │  - dashboard, presets        │
       │  - /recordings, /gallery     │
       │  - proxies to scheduler API  │
       └──────────────────────────────┘

       Icecast on :8000
       - /ems.mp3      ← SDRTrunk publishes P25 audio
       - /monitor.mp3  ← MonitorJob (rtl_fm → ffmpeg → Icecast)
```

**Job priorities:** ManualJob 10 (highest) > MonitorJob 3 > NOAAJob 5
(wait, NOAA is 5 — preempts monitor) > EMSJob 1 (lowest, requeues itself).

## Repo layout

```
/srv/scanner/                       ← git checkout on Pi
├── CLAUDE.md                       ← project memory for Claude Code
├── README.md
├── deploy.sh                       ← rsync files/opt/scanner → /opt/scanner + restart units
├── bootstrap.sh                    ← first-install: apt, java, sdrtrunk, systemd
├── docs/
│   ├── ARCHITECTURE.md             ← deeper architecture details
│   ├── BUILD.md                    ← bootstrap walkthrough
│   ├── JOBS.md                     ← job interface
│   └── CONTEXT.md                  ← THIS FILE — claude.ai bootstrap doc
└── files/
    ├── opt/scanner/                ← deploys to /opt/scanner/
    │   ├── app.py                  ← Flask UI on :8081
    │   ├── scheduler.py            ← scheduler + HTTP API on :8082
    │   ├── jobs/
    │   │   ├── __init__.py         ← Job ABC, JobResult dataclass
    │   │   ├── ems_scanner.py      ← SDRTrunk subprocess wrapper
    │   │   ├── noaa_apt.py         ← rtl_fm → sox WAV → noaa-apt PNG decode
    │   │   ├── ais_poll.py         ← STUB (Stage 6)
    │   │   └── acars_poll.py       ← STUB (Stage 7)
    │   ├── lib/
    │   │   ├── pass_predictor.py   ← pyorbital wrapper, TLE refresh
    │   │   ├── queue.py            ← priority heap
    │   │   └── sdr.py              ← SDRToken ownership marker
    │   └── templates/
    │       ├── index.html          ← dashboard with presets, record, squelch
    │       ├── monitor.html        ← dedicated tuner page
    │       ├── recordings.html     ← MP3 list + disk usage
    │       ├── gallery.html        ← NOAA images
    │       └── calls.html          ← EMS call log
    └── etc/
        ├── scanner/
        │   ├── config.env.example  ← all env vars documented
        │   └── sdrtrunk-playlist.xml.example
        ├── systemd/system/
        │   ├── scanner-scheduler.service
        │   └── scanner-ui.service
        └── sudoers.d/scanner
```

**Deploy model:** `/srv/scanner` is the git clone, `/opt/scanner` is the
deployed copy. `sudo /srv/scanner/deploy.sh` rsyncs files → /opt and
restarts the systemd units. NOT symlinked — explicit deploy step.

**Persistent state** (NOT in repo):
- `/var/lib/scanner/SDRTrunk/` — SDRTrunk playlist + recordings
- `/var/lib/scanner/noaa/` — captured WAVs and decoded PNGs
- `/var/lib/scanner/manual/` — ManualJob raw WAV captures
- `/var/lib/scanner/recordings/` — Record-button MP3 captures
- `/etc/scanner/config.env` — copied from example, edited in place

## Audio chain (the part that gets tweaked most)

The MonitorJob runs `rtl_fm` (raw IF demod) → `ffmpeg` (filter + encode) →
Icecast `/monitor.mp3`. The filter chain lives in env vars so it can be
tuned without code changes:

```
MONITOR_AUDIO_FILTER_AM=highpass=f=200, lowpass=f=3400, {squelch}compand=...:gain=0, dynaudnorm=p=0.95:m=15:s=10:g=15, alimiter=level_in=1:level_out=0.95:limit=0.95:attack=5:release=50
MONITOR_AUDIO_FILTER_FM=highpass=f=80, lowpass=f=5000, {squelch}dynaudnorm=p=0.85:m=15:s=10:g=11
MONITOR_AUDIO_SQUELCH=agate=threshold=0.06:ratio=8:attack=20:release=150:detection=rms:link=average
```

`{squelch}` is a placeholder substituted at runtime:
- Squelch ON → agate filter inserted at that position
- Squelch OFF → placeholder removed, gate-less chain runs

Why each stage in the AM chain:
- `highpass=200, lowpass=3400` — strip rumble + hiss, keep ATC voice band
- `agate` (when ON) — silence dead air below ~-24 dBFS
- `compand` — static transfer curve: dead air mapped 1:1 (no upward noise
  expansion), mid-voice boosted, peaks gently attenuated
- `dynaudnorm p=0.95 g=15` — final loudness leveling (g=15 = 3.75s
  lookahead; must stay ≤15 to avoid Icecast source-timeout)
- `alimiter limit=0.95` — brick wall safety net at -0.4 dBFS

**Critical gotchas (saved in local memory; documenting here for claude.ai):**

1. **Whitespace around `,` in filter chain silently breaks ffmpeg.** The
   parser interprets " lowpass" (with leading space) as an unknown filter
   name and emits NO error. `audio_filter_for()` in scheduler.py runs
   `re.sub(r"\s*,\s*", ",", chain)` to normalize.

2. **dynaudnorm `gausssize` default (31) kills Icecast.** g=31 = 7.5s
   lookahead > Icecast's ~10s source-timeout once you add ffmpeg's startup
   overhead. The mount appears for ~11s then drops with "Broken pipe."
   Keep g≤15.

3. **agate has no runtime command interface.** zmq/sendcmd doesn't work
   for `threshold`. Squelch toggle restarts the rtl_fm→ffmpeg pipeline
   (~1-2s stream gap). Documented in `Scheduler.set_squelch()`.

4. **Never use `stderr=DEVNULL` on streaming subprocesses.** Bugs 1, 2, 3
   were all invisible until `MonitorJob` started piping stderr through a
   `_drain_named` thread that logs with `monitor-ffmpeg:` /
   `monitor-rtl_fm:` prefixes to journald.

5. **rtl_fm auto-offsets the tuner ~252 kHz high for narrow AM** (it sees
   the `-s 24k` and dodges the R820T2's DC spike). The stderr will show
   `Tuned to <freq+252000> Hz` — that's the offset trick, not a bug.

## Aviation AM presets (dashboard)

Frequencies confirmed active by a 30-min `rtl_power` scan in May 2026
(★ = bursty voice signature, others are FAA Chart Supplement guesses):

| Freq | Use | Note |
|---|---|---|
| 131.36 | Memphis Center | ★ strongest in scan |
| 132.5363 | Memphis Center | ★ audibly verified |
| 135.23 / 135.50 | ARTCC sectors | ★ |
| 124.71 / 124.0117 | Approach/Departure | ★ |
| 127.49 / 127.99 / 128.32 / 130.03 / 132.89 / 136.11 | ARTCC | ★ |
| 125.525 | KCGI Tower (per LiveATC) | quiet, listen during arrivals |
| 119.0 / 121.6 / 120.55 / 133.65 | KCGI per FAA Chart | mostly silent in scans |
| 121.5 | Emergency Guard | monitor-only |

**Reality check:** Small uncontrolled airports may have NO traffic during
a given 30-min scan window. KCGI tower (125.525) sat 0.2 dB *below* the
noise floor over a Wednesday-morning scan despite LiveATC confirming the
channel is active. Silence doesn't prove the receive chain is broken.

## Useful one-liners

```bash
# Live scheduler log
sudo journalctl -u scanner-scheduler -f

# Push code changes
sudo /srv/scanner/deploy.sh

# Toggle scheduler autopilot (EMS auto-start + NOAA pass queue)
sudo sed -i 's|^SCHEDULER_AUTOPILOT=.*|SCHEDULER_AUTOPILOT=false|' /etc/scanner/config.env
sudo systemctl restart scanner-scheduler

# Manual tune from CLI (live stream)
curl -X POST http://localhost:8081/api/monitor/tune \
  -H "Content-Type: application/json" \
  -d '{"freq":"131.36M","mode":"am","gain":40,"label":"test","duration_s":600}'

# Toggle squelch
curl -X POST http://localhost:8081/api/monitor/squelch \
  -H "Content-Type: application/json" -d '{"enabled":true}'

# Inspect what ffmpeg is actually running
ps -ef | grep "ffmpeg.*monitor.mp3" | grep -v grep

# Icecast mount status
curl -s http://localhost:8000/status-json.xsl | python3 -m json.tool

# Spectrum scan (stop scheduler first; ~30 min)
sudo systemctl stop scanner-scheduler
rtl_power -f 118M:137M:25k -i 60 -e 30m -g 40 /tmp/airband.csv
sudo systemctl start scanner-scheduler
```

## Things NOT to change without checking

- The `/srv/radio` directory — sibling project, different repo, different
  user. DON'T touch it.
- SDRTrunk's runtime state at `/var/lib/scanner/SDRTrunk/`. deploy.sh
  installs the playlist only if missing; it doesn't overwrite. The
  RSPdxR2 disabled-tuners entry is patched on every deploy though
  (must keep SDRTrunk off the radio project's dongle).
- The DVB kernel-driver blacklist (one for the whole system; the radio
  project owns it).
- Anything in `/etc/icecast2/`. Icecast is shared infrastructure with
  the radio project — its source-timeout setting in particular has
  cascading consequences for our dynaudnorm `g` value.

## How to start a productive conversation

For a tuning/code question:
> "I'm working on the scanner project (see CONTEXT.md). Issue: <symptom>.
> What I've checked: <…>. What I think might be wrong: <…>."

For a feature/design question:
> "In the scanner project (see CONTEXT.md), I want to add <feature>.
> Constraints: <relevant ones from the architecture diagram>.
> Show me a plan with 3-5 bullet points before any code."

For a debugging question:
> Paste relevant output from `journalctl -u scanner-scheduler -n 100` AND
> the matching ffmpeg cmdline from `ps -ef | grep ffmpeg`. Both are
> usually needed.
