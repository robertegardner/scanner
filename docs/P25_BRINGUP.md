# MOSWIN P25 — Discone Bring-up Report

**Date:** 2026-06-01
**Question:** Is the discone good enough at 769 MHz to justify building the
antenna relay, by proving it can receive **and decode** the Cape Girardeau
County MOSWIN P25 Phase II system on the Nooelec?

**Answer: GO.** The discone receives the MOSWIN control channel at +44 dB over
noise and decodes it essentially perfectly (~0.1% sync loss), with live group
calls, full system identity, and clear (unencrypted) voice grants. See evidence
below.

This was a manual, interactive bring-up. It is **not** wired into the scheduler,
the `ems_scanner` job, or systemd, and does **not** stream to Icecast. The radio
project (RSPdx-R2) was never touched.

---

## TL;DR results

| Item | Result |
|------|--------|
| Control-channel carrier (rtl_power) | **+44.3 dB** over noise floor at 769.16875 MHz |
| Decode lock quality (C4FM) | 3401 msgs, **4 sync losses (~0.1%)** |
| NAC | **0x1CC** (460) — *not* 0x1C3 as documented; see note |
| System ID | **0x1CE** (462) ✓ matches MOSWIN |
| WACN | **0xBEE00** (781824) ✓ matches MOSWIN |
| System type | **P25 Phase II** (TDMA sync + TDMA IDEN updates present) |
| Talkgroups heard | 4229, 4241, 4244 (live, ~3 min window) |
| Encryption | **None** — service options `[VOICE, REGISTRATION]`, calls in the clear |
| Decoded audio (WAV) | **Not produced** — JMBE IMBE codec not installed (see below) |
| Correct modulation | **C4FM**, *not* CQPSK |

---

## Two surprises worth recording

1. **op25 could not be used — it does not fit on disk.** op25 (boatbod) must be
   compiled against GNU Radio + dev headers. On this Debian 13 (trixie) box,
   `gnuradio-dev` pulls the full `gnuradio` metapackage (Qt5, gnome-terminal,
   companion) — **~1.49 GB installed, 475 packages** — and `/` had only **823 MB
   free** (88% full) on the single SD card, with no external storage and no room
   to grow the partition. There is no headless GNU Radio dev package in apt.
   Installing it would have pushed root toward ~99% on the card that also runs
   the live radio project, risking the "don't break the radio" constraint.
   **No GNU Radio install or op25 clone was attempted.** The decode was done
   with **SDRTrunk**, which is already installed and proven for P25 here.

2. **MOSWIN's control channel is C4FM, not CQPSK.** The production EMS playlist
   (`/var/lib/scanner/SDRTrunk/playlist/default.xml`) decodes the channel as
   `modulation="CQPSK"`. With CQPSK the decoder showed **~98% SYNC LOSS** and
   captured only a handful of valid frames. Switching to **C4FM** dropped sync
   loss to ~0.1% and produced continuous clean decode. This strongly suggests
   the production EMS channel has effectively never decoded MOSWIN — worth fixing
   separately (outside this bring-up's scope).

---

## (a) Signal level — rtl_power sweep

Command (discone direct to Nooelec, scheduler stopped):

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
Artifacts: `/var/lib/scanner/p25bringup/sweep_769_771.csv`, `rtlpower.log`.

Dongle: `0: Nooelec NESDR SMArt v5, SN 22012952` (RTL2832U + R820T), device 0.

## (b) Final working decoder configuration

Decoder: **SDRTrunk 0.6.1**, headless, run in a fully isolated home so the live
EMS config is untouched and the radio's RSPdx-R2 stays disabled.

| Parameter | Value |
|-----------|-------|
| Control channel | 769.16875 MHz (769168750 Hz) |
| **Modulation** | **C4FM** (`Normal`) — CQPSK/LSM does **not** work |
| Decode type | `decodeConfigP25Phase1`, `traffic_channel_pool_size=10` |
| R820T master gain | `GAIN_327` (32.7 dB) — flawless; gain was not the issue |
| PPM | auto (Nooelec has a TCXO) |

Reproduce (Nooelec free / scheduler stopped):

```bash
sudo systemctl stop scanner-scheduler
sudo -u scanner /srv/scanner/files/opt/scanner/p25/run-bringup.sh 240
#                                       runtime_s ^      gain ^ (default GAIN_327)
# override modulation: MOD=CQPSK sudo -u scanner ... run-bringup.sh 240 GAIN_327
```

Tracked config: `files/opt/scanner/p25/{moswin-bringup-playlist.xml,
run-bringup.sh}`. The script copies the live `tuner_configuration.json` (so the
RSPdx-R2 stays in `disabledTuners`), overrides modulation/gain, and launches
SDRTrunk with `-Duser.home` pointed at an isolated home —
`/var/lib/scanner/p25bringup/sthome/` — with no Icecast stream.

## (c) System identity — confirmed

From `NET_STATUS_BCAST` / `RFSS_STATUS_BCST` (decoded cleanly, repeatedly):

```
NAC:460/x1CC  NET_STATUS_BCAST WACN:781824/xBEE00
              RFSS_STATUS_BCST SYSTEM:462/x1CE  RFSS:3  SITE:7
```

- **System ID 0x1CE** and **WACN 0xBEE00** match the documented MOSWIN values
  in `CLAUDE.md` exactly.
- **NAC reads 0x1CC (460)**, but `CLAUDE.md` records `NAC 0x1C3 (451)`. The
  on-air value is 0x1CC. Either the doc has a transcription error or 769.16875
  is a different site than "Site 033"; the decode is unambiguous. **Action: fix
  the NAC in CLAUDE.md / RadioReference notes.**
- Numerous `TDMA_SYNC_BCST` and `IDEN_UPDATE_TDMA` messages confirm **Phase II**.

## (d) Talkgroups + encryption

Live group calls captured in a ~3-minute window (`c4fm_call_events.log`):

| Talkgroup | Grants | Voice channel(s) | Encryption |
|-----------|-------:|------------------|------------|
| 4229 | 43 | 770.16875 MHz | clear |
| 4241 | 23 | 769.66875 MHz | clear |
| 4244 | 2 | 769.91875 MHz | clear |

Source radio IDs heard: 87208, 88072, 91986. Location registrations to groups
6708/6711/3981 also decoded.

**Encryption: none observed.** Zero `ENCRYPTED` markers across all call events and
decoded messages; grant service options read `[VOICE, REGISTRATION]` with no
encryption flag. Consistent with `CLAUDE.md`'s note that Cape County talkgroups
are in the clear. (Talkgroup *labels* show as "Cape County All" only because the
bring-up alias is a catch-all 0–65535 range; real labels need the RadioReference
list. The numeric TGIDs above are real.)

## (e) Decoded audio — not produced (codec gap, not an antenna issue)

No WAV was produced. SDRTrunk logs:

```
JMBE audio conversion library, IMBE CODEC not loaded - P25-1 audio will NOT be available
```

Only `jmbe-api-1.0.0.jar` (the interface) is present; the **JMBE IMBE/AMBE codec
implementation is not installed** (it is patent-encumbered and must be built from
source). This is a pure software gap, **independent of the discone** — and it
means the production EMS Icecast stream has no voice either. op25 ships its own
software IMBE decoder (would have given audio with no JMBE), but op25 doesn't fit
on disk (see above).

To get an actual voice WAV later: build JMBE once, point SDRTrunk's
`jmbe library path` preference at it, re-run the bring-up — clear calls on the
TGIDs above will then decode to audio. ~15-minute follow-up; flagged, not done.

## (f) GO / NO-GO recommendation

**GO — build the relay.** Evidence:

- The discone hears the 769 MHz control channel at **+44 dB** over a flat noise
  floor — not marginal, one of the strongest signals on the band.
- It decodes that channel at **~0.1% sync loss** with the correct C4FM setting —
  full system identity (NAC/System/WACN), continuous TSBKs, and **live group
  calls with real talkgroup IDs on real voice channels**.
- Calls are **in the clear**, so the system is genuinely listenable once the
  JMBE codec is added.

This is the opposite of the NOAA APT situation (where the discone heard *nothing*
at 137 MHz). At 769 MHz the discone is an excellent antenna, so dedicating it to
the 700 MHz P25 job via the relay — and using a separate VHF antenna for the
131–162 MHz cluster — is well justified.

### Follow-ups (separate from the relay decision)
1. **Fix the production EMS playlist**: change `modulation` CQPSK → **C4FM**, or
   it will keep failing to decode MOSWIN.
2. **Correct the NAC** in `CLAUDE.md`: on-air is **0x1CC**, not 0x1C3.
3. **Install the JMBE codec** if voice audio / Icecast streaming is wanted.
4. The op25 path needs more disk than the SD card has — revisit only if op25 is
   specifically required (e.g. add storage or a lighter GNU Radio).

---

### Artifacts (`/var/lib/scanner/p25bringup/`)
- `sweep_769_771.csv`, `rtlpower.log` — rtl_power signal sweep
- `c4fm_decoded_messages.log` — winning C4FM decode (NAC/System/WACN/IDEN)
- `c4fm_call_events.log` — live group calls / talkgroups / registrations
- `bringup_*.log` — SDRTrunk app logs (incl. `RSPdxR2 ... Added / Disabled`,
  confirming the radio's tuner was never opened)
