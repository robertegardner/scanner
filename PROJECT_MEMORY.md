# Homelab Radio / SDR Project — Project Memory

Context document for Claude Projects AND the repo. Any new conversation in
this project should read this first. It captures who I am, what I'm building,
the current state, and the plans — so I don't have to re-establish context
every time.

> **Keep this file updated.** It is a snapshot, not a live feed. At the end
> of any meaningful conversation where a decision was made, a build step
> completed, or hardware changed, refresh this file (edit and re-upload to
> the Claude Project, and commit to the repo). Stale project memory is worse
> than none — it makes Claude confidently wrong. If a conversation ends with
> "we decided X" or "I finished Y", that is the signal to update.

Last meaningful update (2026-06-01): dx-R2 received, installed, and the radio
migrated to it (RTL-SDR → SDRplay); AM long-wire built and live on dx-R2
Antenna C (BNC adapter arrived) — currently troubleshooting high RF
interference on the AM path; FM working on Antenna A; FM stereo / HD Radio
backburnered; scanner project past skeleton (stages 0–5 live); MOSWIN P25
confirmed to be 700 MHz (not VHF); scanner antenna-switch (discone/dipole)
designed with a bench test protocol.

---

## Who I am

- Bob Gardner. Based near Cape Girardeau, Missouri (lat ~37.31, lon ~-89.55).
- Run a homelab: Proxmox cluster, several Pis, network-attached services.
- Comfortable with Linux, Python, through-hole soldering, 3D printing
  (Bambu/Prusa-class printer, 0.4mm nozzle, 250mm+ bed).
- This project is a hobby build, not production. The hardware *is* the test
  environment.

## What I'm building

A multi-part software-defined radio (SDR) setup. Three related projects:

### 1. The radio project — `github.com/robertegardner/radio`

A live-tuning FM/AM broadcast receiver on a Raspberry Pi 5 in my attic.
Streams via Icecast, decodes RDS metadata, looks up synced lyrics, runs
Whisper-based live captions for talk content, and serves a car-stereo-style
web tuner UI. **Now running on the SDRplay RSPdx-R2** (migrated off the
Nooelec RTL-SDR).

- **Live deployment:** `https://radio.rg2.io` (admin UI) and
  `https://radio.rg2.io/radio` (stereo-style listener UI).
- **Stream:** `https://icecast.rg2.io/fm.mp3`, reverse-proxied via NPMplus.
- **Pi:** hostname `radio`, user `rgardner`, in the attic, PoE-powered via a
  UCTRONICS U627803 PoE HAT.
- **Repo layout:** code in `files/opt/sdr-tuner/`, deploys to `/opt/sdr-tuner/`
  via `deploy.sh`. Git checkout lives at `/srv/radio` on the Pi.
- **Project memory for Claude Code:** the repo has its own `CLAUDE.md` and a
  gitignored `CLAUDE.local.md`. Claude Code on the Pi reads those.

### 2. The scanner project — `github.com/robertegardner/scanner`

A multi-purpose secondary SDR scanner on the same attic Pi 5, separate
codebase, running on the spare Nooelec NESDR SMArt v5. Time-slices the one
dongle across EMS/public-safety scanning, NOAA weather-satellite imagery, AIS
marine tracking, and optionally ACARS, via a scheduler that owns the SDR and
dispatches plugin-style jobs.

- **Status: live, not a skeleton.** Stages 0–5 complete and running on the Pi
  (as of 2026-05-23): scheduler, EMS job, NOAA APT job, pass predictor, and
  Flask UI all implemented and deployed; systemd units enabled and start on
  boot.
- **VHF dipole connected; NOAA APT captures working.** First NOAA images
  expected imminently (pass watcher live, TLEs current).
- **Discone arriving soon** — required for MOSWIN P25 (700 MHz); the VHF
  dipole physically can't receive it.
- **Web UI:** `https://scanner.rg2.io` (or `http://<pi-ip>:8081`).

### 3. (Existing, separate) ADS-B flight tracker

A Pi 4 outside the house, two SDRs, 1090 MHz + 978 MHz UAT antenna, feeding
FlightAware / Flightradar24 / ADSBexchange. 700+ day uptime streak — do NOT
suggest changes that risk it. Future interest: anomaly alerting (read-only
analysis of its data). Not part of the radio or scanner repos.

## The PRIMARY use case

**Listening to St. Louis Cardinals baseball games over the radio.** This is
the main reason the project exists. Everything else (music, captions, lyrics,
HD) is secondary. Key Cardinals stations:

- **KMOX 1120 AM** — St. Louis, 50 kW clear-channel flagship. Designed to
  cover most of the central US at night; receivable in Cape Girardeau.
  **Verified working on the dx-R2.**
- **KZYM 1230 AM** — Cape Girardeau local Cardinals affiliate.
- **95.7 FM** and other FM affiliates also carry games.

Because the Cardinals network is fundamentally AM-based, **good AM reception
matters more than premium FM** for the core goal.

## Hardware — current

### Radio project
- **Raspberry Pi 5** in the attic, UCTRONICS U627803 PoE HAT (the HAT has a
  cutout exposing 36 GPIO pins). Shared with the scanner project.
- **SDRplay RSPdx-R2** — the active radio SDR. 14-bit ADC, three
  software-selectable antenna inputs (A/B SMA, C BNC), HDR mode for strong AM
  dynamic range, hardware notch filters. Handles AM natively (no
  direct-sampling hack). Migration off the Nooelec is complete: SDRplay RSP
  API 3.15 installed manually, SoapySDR + SoapySDRPlay3 built from source,
  stream/scan scripts updated to `rx_fm --driver sdrplay` with antenna
  selection.
- **Shakespeare 5120** — 5-foot marine FM whip (88–108 MHz), in the attic, on
  **dx-R2 Antenna A (SMA)**.
- **Cat 5 long-wire AM antenna** — built, with a Nooelec 9:1 unun balun
  (B08HGSYB7R), live on **dx-R2 Antenna C (BNC)**. **Currently troubleshooting
  high RF interference on this path** (see open work).
- **GPU host** — separate homelab machine running a Whisper FastAPI service
  in Docker, used for live captions. Token-authenticated.

### Scanner project
- **Spare Nooelec NESDR SMArt v5** (RTL2832U + R820T2, 8-bit ADC) — the
  scanner's SDR, on the Pi's other USB 3.0 port. RTL device index 0 on this
  Pi. (The dx-R2 uses SoapySDR, not librtlsdr, so they don't collide.)
- **VHF dipole** — connected to the Nooelec; good for the 131–162 MHz cluster
  (NOAA APT 137, AIS 162, ACARS 131). Cannot receive 700 MHz.
- **Discone (25–1300 MHz)** — incoming; needed for MOSWIN P25 at 769–771 MHz.

### Incoming / awaited
- **Discone antenna** for the scanner (700 MHz P25).

## Antenna / SDR assignment (current state)

| SDR | Antenna(s) | Purpose |
|-----|-----------|---------|
| **RSPdx-R2** | Shakespeare 5120 → Antenna A (SMA); Cat 5 AM long-wire → Antenna C (BNC) | Radio project: FM + AM broadcast |
| **Nooelec NESDR SMArt v5** | VHF dipole now; discone soon (via the planned antenna switch) | Scanner project: EMS / NOAA APT / AIS / ACARS |

- dx-R2 **Antenna A (SMA)** → Shakespeare 5120 (FM) — working
- dx-R2 **Antenna B (SMA)** → spare / experimental
- dx-R2 **Antenna C (BNC, HF-optimized)** → Cat 5 AM long-wire — live, but the
  AM path is currently fighting high RF interference (troubleshooting)
- Antenna selection on the dx-R2 is a software API call — no relay hardware.
- The Nooelec has a **single** input, so the scanner *does* need a real RF
  switch to alternate discone/dipole — see the scanner antenna-switch decision
  below. This is the one place the relay idea is alive (it's dead for the
  radio).

## Key decisions made (so we don't re-litigate them)

1. **RSPdx-R2 chosen over RSP1B, RSPduo, and Airspy HF+ Discovery.** Reasons:
   three software-selectable antenna inputs (eliminates the radio's
   antenna-switching relay), HDR mode genuinely improves AM dynamic range,
   wide bandwidth, and the cost delta buys real capability. The RSPduo was
   ruled out — its dual tuners don't help a single-stream use case. **Done:
   installed and migrated.**

2. **The GPIO relay antenna-switching plan is DEAD *for the radio*.** The
   dx-R2's three antenna ports make radio antenna selection a software API
   call — no relay, driver board, boost converter, or relay case. (The old
   relay build guide in the radio repo's `hardware/` directory: its
   *antenna-assembly* sections are still valid; ignore its relay/case
   sections.) **But the relay idea is reborn for the scanner** (single-input
   Nooelec) — see decision 6.

3. **FM was overloading the Nooelec.** With the Shakespeare 5120's strong
   signal, the Nooelec's 8-bit front end saturated — KGMO 100.7 disappeared
   from scans at GAIN=30, needed GAIN=5 to listen cleanly. This overload is
   *why* the SDR upgrade happened; the dx-R2's 14-bit ADC is expected to fix
   it.

4. **FM stereo + HD Radio: nrsc5 is OUT, and the whole effort is backburnered
   (reversed + deferred).** Earlier plan was to use nrsc5 for both HD Radio and
   an analog FM-stereo decode. **nrsc5 is hardcoded to librtlsdr**, so on the
   dx-R2 both HD Radio and the nrsc5 stereo path are dead ends. FM is mono
   today and that's accepted for now — **stereo and HD work is on the back
   burner.** When/if it's picked back up:
   - **FM stereo** would go via a **csdr-based pipeline using SoapySDR Python
     bindings for raw IQ capture**, replacing the mono `rx_fm -M fm` path for
     `wbfm` mode (bitrate to bump to 192k for stereo). A Claude Code prompt is
     drafted with an explicit bail-out: **don't ship if audio quality is worse
     than the current mono pipeline.**
   - **HD Radio** would require separate RTL-SDR hardware; it's not happening
     on the dx-R2. (Moot in-market anyway — no HD stations near Cape
     Girardeau; nearest is St. Louis ~115 mi.)

5. **Two separate repos, not one.** Radio and scanner are independent
   codebases sharing the attic Pi. Different `/srv` directories, system users
   (`radio` vs `scanner`), SDRs, antennas, systemd units, and Flask
   ports/domains. **When working on one, don't touch the other's `/srv` or
   `/opt`.**

6. **Scanner antenna switch: SP3T relay tree, fail-safe to discone.** The
   single-input Nooelec needs to alternate antennas because the jobs span two
   incompatible bands: MOSWIN P25 (the default job) is **769–771 MHz** and
   only the discone can hear it, while NOAA APT (137 MHz) and the rest of the
   VHF cluster want the dipole. Design: two SMA coaxial SPDT RF relays (DC–3
   GHz) in a tree driven by a ULN2003, with the discone on the shortest
   1-relay path and as the all-relays-off fail-safe (default EMS job survives
   a GPIO/software failure). Three input jacks — populate discone + dipole
   now, reserve the third for a future polarization-matched NOAA antenna
   (V-dipole/QFH) or a tuned 700 MHz gain antenna. Do **not** deepen the tree
   to 4; if 4+ inputs are ever wanted, use a single SP4T coaxial relay
   instead. Relay must be a true RF coaxial part — a general-purpose
   SRD/Songle relay will wreck the 769 MHz path. Build sheet + bench test
   protocol written this session (`ANTENNA_SWITCH_BUILD.md`, for the scanner
   repo's `hardware/docs/`).

7. **MOSWIN P25 is 700 MHz, not VHF (correction).** Cape Girardeau County
   MOSWIN is P25 Phase II at **769–771 MHz** (sites 033/055/060). Control
   channel **769.16875 MHz** (Site 033 primary), NAC 0x1C3 (451), System ID
   1CE, WACN BEE00 (RadioReference sid/6847). Cape County Private Ambulance
   (CCPA) still uses conventional **155.205 MHz** simplex. A VHF dipole tuned
   for ~140 MHz cannot receive the 700 MHz signal — hence the discone and the
   antenna switch.

## The AM antenna — built and live (troubleshooting interference)

Cat 5 long-wire AM antenna, complete and connected to dx-R2 Antenna C (BNC).
Working as a signal path, but the AM project is currently fighting high RF
interference — see open work for the troubleshooting plan.

- **Wire:** ~100 ft of Cat 5. Far end: all 8 conductors twisted into one
  bundle. Near end: split into two 4-conductor leads — antenna + counterpoise.
- **Color convention (the balun terminals are unlabeled):**
  **orange pair + green pair = antenna lead** (→ ANTENNA terminal),
  **blue pair + brown pair = counterpoise lead** (→ GROUND terminal). Reuse
  this mapping for any future work on this antenna.
- **Balun:** Nooelec 9:1 unun (B08HGSYB7R). Radio side feeds coax to the dx-R2
  Antenna C (BNC).

**Settled antenna facts (still authoritative):**
- Linear run is essential — a coiled wire is electrically near-useless for AM
  (adjacent turns cancel; it becomes an inductor in the noise). Use the length
  *as length*; cut excess rather than coil it. (Coiling the *coax* feedline is
  fine — coax is shielded.)
- The counterpoise sharing the Cat 5 jacket with the radiator is a mild
  compromise but worth connecting — better than an empty GROUND terminal. No
  separate counterpoise wire is being run.
- The attic is RF-favorable: pine/plywood/asphalt-shingle, no foil radiant
  barrier, no metal roof, blown-cellulose insulation (RF-transparent). Network
  noise sources are in the basement/main level; only the attic PoE camera
  switches are nearby — give them a few feet of berth.
- Outdoor installation isn't possible at this location; the attic is the
  chosen compromise and a good one given the construction.

## Project status snapshot

- **Radio project:** live on the **dx-R2** + Shakespeare 5120 (FM, Antenna A)
  and Cat 5 long-wire (AM, Antenna C). KGMO 100.7 FM and KMOX 1120 AM verified.
  FCC CDBS station database in use. Admin UI + stereo `/radio` UI both built.
  FM works on Antenna A. **The AM path is up but fighting high RF interference
  — active troubleshooting.** FM is **mono** today (stereo/HD backburnered);
  distant-station FM quality is degraded (mono demod, hardcoded GAIN=30 possibly
  non-optimal for the dx-R2, possible attic multipath).
- **AM antenna:** built and live on Antenna C; interference troubleshooting in
  progress.
- **Scanner project:** stages 0–5 running on the Pi. NOAA APT capturing on the
  dipole; first images imminent. Awaiting discone for MOSWIN P25. Next: verify
  NOAA image quality, then P25 decode once the discone is in, then Stage 6
  (AIS).
- **Scanner antenna switch:** designed (build sheet + test protocol written),
  not yet built. Bench-validate before the attic trip.

## Open / planned work

### Radio project
- **AM RF interference (active).** AM long-wire is live on Antenna C but the
  band is noisy. Troubleshooting directions worth working through: identify the
  noise (wideband hash vs. discrete carriers/birdies) by sweeping the AM band
  in the admin UI / `rtl_power`-equivalent; test with suspected local noise
  sources powered off (PoE camera switches nearby, Pi/HAT supplies, LED/SMPS
  wall warts) to isolate the culprit; try the dx-R2's HDR mode and hardware
  notch filters; confirm the 9:1 unun GROUND/counterpoise connection and that
  the coax shield is properly grounded; consider common-mode choking (clip-on
  ferrites / a few coax turns through a ferrite) at the balun and at the dx-R2;
  re-check antenna routing relative to AC lines and the camera switches.
- **FM stereo via csdr/SoapySDR IQ — BACKBURNERED.** When resumed: replace the
  mono `rx_fm -M fm` `wbfm` path with a csdr pipeline fed by SoapySDR Python
  bindings; bump bitrate to 192k. Bail-out: don't ship if worse than current
  mono. (nrsc5 is not an option — see decision 4.)
- **Gain tuning for the dx-R2** — the hardcoded GAIN=30 was inherited from the
  RTL era and may not suit the dx-R2's gain structure; revisit for distant FM
  (and re-evaluate alongside the AM interference work).
- **NPMplus auth on admin endpoints** — keep `/radio` and read-only APIs
  public; require auth on `/`, `/tune`, `/scan-*`, `/settings`.
- **Favorites sync/export** — URL-based export/import for cross-device sync
  (presets are per-browser localStorage today).
- **Stream recording** — capture current stream to timestamped MP3.
- **Weekly cron** for the FCC station database refresh.
- **Scan-and-listen mode**, **wake-up alarm** — minor UI/timer features.

### Scanner project
- **Verify NOAA image quality** once the first pass completes; if noisy,
  suspect dipole polarization (vertical dipole is a poor match for RHCP
  satellites) and consider a V-dipole/QFH on the switch's reserved input.
- **MOSWIN P25 decode** once the discone arrives — SDRTrunk or op25 on
  769.16875 MHz control channel; confirm clear (unencrypted) talkgroups.
- **Build + bench-test the antenna switch** (see `ANTENNA_SWITCH_BUILD.md`);
  pass the acceptance gate before the attic.
- **Attic cabling** for the discone (deferred until the switch is built).
- **Stage 6: AIS poll**, then optional ACARS.

## How to work with me on this

- The **repos are the source of truth.** `CLAUDE.md` in each repo is the
  authoritative project memory for Claude Code sessions. This document is the
  higher-level memory for design/planning conversations.
- For **implementation work**, Claude Code on the Pi is the right tool (reads
  files, runs commands directly). For **design, troubleshooting, planning**, a
  chat conversation is right.
- To see current repo state, ask me to fetch a specific GitHub URL (paste it),
  or paste `cat`/`git log` output from the Pi. The main repo page returns
  stale cached data; **specific commit URLs are reliable** for code review;
  raw file URLs need me to paste content. I can't autonomously poll the repo
  between messages.
- I prefer complete rewritten files over diffs when handing over code.
- I test on the live deployment; there is no CI.

## Things that have bitten this project before

- **DVB kernel driver** auto-claims RTL-SDR dongles — blacklisted, needs a
  reboot to take effect. Symptom: `usb_claim_interface error -6`. (Applies to
  the scanner's Nooelec; the blacklist is system-wide so it's set once.)
- **nrsc5 is hardcoded to librtlsdr** — it cannot use the dx-R2. Any HD Radio
  or nrsc5-based stereo path needs separate RTL-SDR hardware. (This killed the
  old "nrsc5 for FM stereo" plan.)
- **`active.env` must be writable by `radio:radio`** or tuning returns 500.
- **Trixie uses `/usr/bin/systemctl`**, not `/bin/systemctl` — matters for the
  sudoers rules.
- **The Pi is headless** — `aplay` fails (no audio device). Test audio by
  publishing to Icecast and listening via the web UI.
- **FM front-end overload** on the 8-bit Nooelec with a strong antenna — the
  reason for the dx-R2 upgrade.
- **(Historical) AM on RTL-SDR** needed direct sampling (`-E direct2`); the
  R820T can't go below ~24 MHz. Moot for the radio now — the dx-R2 does AM
  natively. Still relevant if the scanner's Nooelec is ever used for HF/AM.

---

## Maintenance reminder

This file drifts out of date the moment something changes. Treat updating it
as part of finishing any session. Triggers to update:

- A hardware change (received, installed, swapped, retired)
- A design decision made or reversed
- A build milestone completed (antenna strung, dx-R2 integrated, etc.)
- A new planned feature added or an existing one finished
- Anything in "Project status snapshot" no longer being true

When updating: revise the relevant section, bump the "Last meaningful update"
line near the top, re-upload to the Claude Project, and commit to the repo.
