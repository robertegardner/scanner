# MOSWIN P25 — Design, Bring-up & Deployment

Scanner project (`robertegardner/scanner`). This is the single source of truth
for MOSWIN P25 reception: the original design intent, the bring-up evidence, and
what is now deployed. (Supersedes the earlier separate plan + report docs.)

**Goal of the bring-up:** prove the discone can receive *and decode* the Cape
Girardeau County MOSWIN P25 Phase II system straight into the Nooelec, as the
go/no-go gate before investing in any antenna relay.

**Outcome: GO — and now live.** The discone hears the 769 MHz control channel at
**+44 dB** over noise and decodes it at **~0.1% sync loss**, with live clear
(unencrypted) group calls. P25 voice now decodes to audio (JMBE built) and streams
via Icecast, selectable from a new `/listen` web page. The radio project
(RSPdx-R2) was never touched throughout.

---

## TL;DR results

| Item | Result |
|------|--------|
| Control-channel carrier (rtl_power) | **+44.3 dB** over a flat noise floor at 769.16875 MHz |
| Decode lock quality | 3401 msgs, **4 sync losses (~0.1%)** |
| **Modulation** | **C4FM** ("Normal") — *not* CQPSK/LSM (CQPSK ≈ 98% sync loss) |
| NAC | **0x1CC** (460) — corrected from the previously-documented 0x1C3 |
| System ID | **0x1CE** (462) ✓ · WACN **0xBEE00** (781824) ✓ |
| System type | **P25 Phase II** (TDMA sync + TDMA IDEN updates present) |
| Talkgroups heard | 4229, 4241, 4244 (live, ~3 min window), all **in the clear** |
| Voice channels | 769.66875 / 769.91875 / 770.16875 MHz |
| Audio | **Works** — JMBE codec built; SDRTrunk: `IMBE CODEC successfully loaded` |
| Decoder | **SDRTrunk** (op25 was the plan but doesn't fit on disk — see below) |

---

## How it's wired now (deployed)

A `/listen` page (Flask UI, :8081) is a **source switcher on the one shared
Nooelec** — no relay, no antenna change, because the discone covers both bands:

- **MOSWIN P25** (default, auto-starts) → SDRTrunk → Icecast `/ems.mp3`, with live
  talkgroup + recent-calls display.
- **Aviation AM** (118–137 MHz, on demand) → rtl_fm monitor → Icecast `/monitor.mp3`,
  preset bank + direct tune.

Switching sources preempts on the single SDR via the scheduler
(`Scheduler.start_moswin()` / `/source/moswin`; EMS is the lowest-priority job so
switching back to MOSWIN stops the monitor and queues EMS). Works with
`SCHEDULER_AUTOPILOT=false`.

---

## System parameters (corrected, from on-air decode + RadioReference sid/6847)

| Field | Value |
|-------|-------|
| System | MOSWIN, Cape Girardeau County, **P25 Phase II** |
| Control channel | **769.16875 MHz** (Site 033 primary) |
| **Modulation** | **C4FM** ("Normal") |
| **NAC** | **0x1CC** (460) — confirmed on air; *not* 0x1C3 |
| System ID | **0x1CE** (462) · WACN **0xBEE00** |
| Sites | 033 / 055 / 060, all in **769–771 MHz** |
| Encryption | none observed — listed talkgroups in the clear |

(Cape County Private Ambulance on 155.205 MHz conventional is VHF and out of
scope here — it needs the dipole, not the discone.)

---

## Bring-up evidence

### (a) Signal level — rtl_power sweep

```
rtl_power -f 769.0M:771.0M:2k -g 30 -i 5 -1 sweep_769_771.csv
```

| Metric | Value |
|--------|-------|
| Noise floor (median, DC bin excluded) | −44.7 dB (very flat, MAD 0.57 dB) |
| Peak bin | 769.16797 MHz = −0.4 dB |
| **Carrier vs floor at the CC** | **+44.3 dB** |
| Other strong carriers | 770.16875 MHz (+41.5 dB), 769.01875 MHz (+38.8 dB) |

The 770.16875 MHz carrier later showed up as an active FDMA **voice channel** in
the decode — independent confirmation the sweep was seeing real MOSWIN traffic.
Dongle: `0: Nooelec NESDR SMArt v5, SN 22012952` (RTL2832U + R820T).

### (b) Decoder configuration

| Parameter | Value |
|-----------|-------|
| Decoder | SDRTrunk 0.6.1, headless |
| Control channel | 769.16875 MHz (769168750 Hz) |
| **Modulation** | **C4FM** — CQPSK/LSM does **not** work here |
| Decode type | `decodeConfigP25Phase1`, `traffic_channel_pool_size=10` |
| R820T master gain | `GAIN_327` (32.7 dB) — flawless; gain was never the issue |
| PPM | auto (Nooelec has a TCXO) |

Reproduce the isolated bring-up (Nooelec free / scheduler stopped):
```bash
sudo systemctl stop scanner-scheduler
sudo -u scanner /srv/scanner/files/opt/scanner/p25/run-bringup.sh 240
#   args: runtime_s [GAIN_xxx];  MOD=CQPSK env var to override modulation
```
`files/opt/scanner/p25/run-bringup.sh` copies the live `tuner_configuration.json`
(so the RSPdx-R2 stays in `disabledTuners`) and runs SDRTrunk in an isolated
`-Duser.home` with no Icecast stream — it never disturbs the production config.

### (c) System identity — confirmed

```
NAC:460/x1CC  NET_STATUS_BCAST WACN:781824/xBEE00
              RFSS_STATUS_BCST SYSTEM:462/x1CE  RFSS:3  SITE:7
```
Numerous `TDMA_SYNC_BCST` / `IDEN_UPDATE_TDMA` confirm **Phase II**. The on-air
**NAC is 0x1CC**, not the 0x1C3 previously recorded — now corrected everywhere.

### (d) Talkgroups + encryption

| Talkgroup | Grants | Voice channel | Encryption |
|-----------|-------:|---------------|------------|
| 4229 | 43 | 770.16875 MHz | clear |
| 4241 | 23 | 769.66875 MHz | clear |
| 4244 |  2 | 769.91875 MHz | clear |

Source radios heard: 87208, 88072, 91986. **No `ENCRYPTED` markers** anywhere;
grant service options read `[VOICE, REGISTRATION]`. (Labels show "Cape County All"
only because the bring-up alias is a catch-all 0–65535 range; real labels need the
RadioReference list. The numeric TGIDs are real.)

### (e) Decoded audio — works (JMBE built)

P25 voice needs the JMBE IMBE/AMBE codec, which is patent-encumbered and not in
apt. It was built from source (`dsheirer/jmbe` v1.0.9) and installed to
`/var/lib/scanner/jmbe/jmbe.jar`, with SDRTrunk's library-path preference set
(`path.jmbe.library.1.0.0` under `/var/lib/scanner/.java/.userPrefs`). SDRTrunk now
logs `IMBE CODEC successfully loaded - P25-1 audio will be available`, so clear
calls decode to audio and stream to Icecast.

Build notes (disk was the recurring obstacle on the ~93%-full SD card): built
entirely in `/dev/shm` (RAM) because gradle wouldn't fit on disk, using gradle
8.10.2 (the repo's 7.4 can't run on the only installable JDK, 21). The build-only
JDK was removed afterward — SDRTrunk uses its own bundled JRE.

---

## What was changed / deployed in the repo

- **EMS playlist** modulation `CQPSK → C4FM` (repo template + live
  `/var/lib/scanner/SDRTrunk/playlist/default.xml`).
- **NAC** corrected to `0x1CC` in `CLAUDE.md` and the playlist template.
- **scheduler.py**: `start_moswin()` + `/source/moswin` route.
- **app.py**: `/listen` page + `/api/source/moswin` proxy.
- **templates/listen.html**: the source switcher.
- **files/opt/scanner/p25/**: tracked SDRTrunk bring-up playlist + runner.
- **JMBE** built out-of-tree (not vendored) and wired into SDRTrunk.

---

## On the antenna relay (revised conclusion)

The bring-up was framed as the gate for building a discone↔dipole relay. Two
findings change that calculus:

1. **The discone covers both jobs in use.** It decodes MOSWIN at 769 MHz *and*
   aviation AM at 118–137 MHz. So aviation + MOSWIN coexist via **software
   source-switching on the one antenna** (the `/listen` page) — **no relay
   required** for them.
2. The relay's remaining value is only to add a *band-optimized* antenna later
   (e.g. a tuned 700 MHz gain antenna to lift P25 decode margin). It is **not** a
   prerequisite for listening to MOSWIN or aviation today.

**Net: GO on MOSWIN reception (excellent), but the relay is deferred** — it's a
future enhancement for adding dedicated antennas, not a blocker.

---

## Decoder choice — op25 (planned) vs SDRTrunk (used)

The original plan was **op25** (boatbod fork): headless-native with an HTTP
dashboard, light on the Pi, clean to integrate as a managed child process, and
actively maintained against GNU Radio 3.10. Sound reasoning — but op25 must be
compiled against GNU Radio + dev headers, and on this Debian 13 box
`gnuradio-dev` pulls the full `gnuradio` metapackage (Qt5, companion):
**~1.49 GB installed / 475 packages** vs **~0.8 GB free** on the single SD card,
with no headless GR dev package and no room to grow. Installing it risked pushing
root to ~99% on the card the radio also runs from. So op25 was **not** installed;
SDRTrunk (already present and proven for P25 here) did the decode. Revisit op25
only if storage is added or a lighter GNU Radio path appears.

---

## Artifacts (`/var/lib/scanner/p25bringup/`)

- `sweep_769_771.csv`, `rtlpower.log` — rtl_power signal sweep
- `c4fm_decoded_messages.log` — winning C4FM decode (NAC/System/WACN/IDEN)
- `c4fm_call_events.log` — live group calls / talkgroups / registrations
- `bringup_*.log` — SDRTrunk app logs (incl. `RSPdxR2 ... Added / Disabled`,
  confirming the radio's tuner was never opened)
