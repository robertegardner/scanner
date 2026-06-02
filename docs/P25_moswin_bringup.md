# P25 MOSWIN Reception — Project Design & Bring-Up

Scanner project (`robertegardner/scanner`). Goal: **prove the discone can
receive and decode the Cape Girardeau County MOSWIN P25 system, straight into
the Nooelec, before building the antenna relay.** If the discone can't pull the
769 MHz control channel cleanly on a direct cable, the relay is wasted effort —
this is the go/no-go gate for that build.

## The decision this answers first

Right now the Nooelec is on the VHF dipole (NOAA APT working). MOSWIN lives at
**769–771 MHz**, which the dipole physically can't hear. So the natural order is:

1. **(This task)** Cable the discone *directly* into the Nooelec, confirm MOSWIN
   decodes well enough to be worth listening to. **No relay involved.**
2. **(Only if step 1 is a GO)** Build the antenna relay so the scheduler can
   switch discone (UHF/P25) vs dipole (NOAA/AIS/ACARS) automatically.
3. **(After the relay)** Integrate P25 as the scheduler's `ems_scanner` default
   job with Icecast streaming and call logging.

Don't invert this. Reception quality on the discone is the unknown that gates
everything downstream.

## Decoder choice: op25 (boatbod fork)

op25 over SDRTrunk, for this specific setup:

- **Headless-native.** op25 is CLI with a built-in HTTP dashboard; SDRTrunk is a
  JavaFX GUI app that needs a display (Xvfb hacks on a headless Pi). The Pi is
  headless (and `aplay` doesn't work — no audio device), so op25's
  browser-dashboard-instead-of-soundcard model is exactly right.
- **Lighter on the Pi 5** and proven on far weaker Pis for continuous P25
  trunked streaming.
- **Clean service integration later** — as a child process the scheduler
  start/stops, or a dedicated unit it gates. SDRTrunk-as-a-daemon is awkward.
- **boatbod is actively maintained** (current activity into 2026, builds against
  GNU Radio 3.10 which is what Trixie ships) and handles P25 Phase II TDMA voice
  and trunk-following on a single RTL-SDR.

SDRTrunk stays the fallback if op25 won't decode for some reason — but start
with op25.

## MOSWIN system parameters (from the scanner CLAUDE.md / RadioReference)

| Field | Value |
|-------|-------|
| System | MOSWIN, Cape Girardeau County, **P25 Phase II** |
| Control channel | **769.16875 MHz** (Site 033 primary) |
| NAC | **0x1C3** (451) |
| System ID | **1CE** |
| WACN | **BEE00** |
| Sites | 033 / 055 / 060, all in **769–771 MHz** |
| RadioReference | sid/6847 |
| (Separate, VHF) CCPA | 155.205 MHz conventional simplex — **out of scope** here; it needs the dipole, not the discone |

Encryption: Cape County's listed talkgroups looked clear as of late 2025 —
**verify live**; op25's dashboard flags encrypted (ENC) calls, so this task will
produce real data on what's actually listenable.

## Architecture

### Phase 1 — Proof of reception (this task: manual, interactive)

```
   DISCONE ──(direct coax)──► Nooelec NESDR (RTL idx 0)
                                   │  librtlsdr
                                   ▼
                              op25 rx.py  (control channel 769.16875,
                                   │       NAC 0x1C3, -2 Phase II TDMA)
                       ┌───────────┼────────────────┐
                       ▼           ▼                ▼
                http dashboard   decoded audio    stderr log
                (browser, :8090)  (wav sample)    (TSBKs, errors)
```

Preconditions, both mandatory:
- **Discone cabled directly to the Nooelec**, bypassing the (unbuilt) relay.
  This means temporarily unplugging the dipole — NOAA captures pause for the
  session. Fine.
- **Scanner scheduler stopped** so it doesn't grab the dongle out from under
  op25. (The radio project is unaffected — it's on the dx-R2 via SoapySDR, a
  different driver and device; op25's `rtl` args only see the Nooelec.)

What "success" looks like, in order:
1. `rtl_power` sweep of 769–771 MHz shows the control-channel carrier present and
   strong on the discone (first hard signal-quality data point).
2. op25 locks the control channel: dashboard shows **NAC 0x1C3** and the System
   ID / WACN matching MOSWIN, TSBKs decoding, low error rate.
3. Talkgroups appear on the dashboard as calls happen; ENC flag reveals clear vs
   encrypted.
4. A captured **audio sample of a clear call** confirms end-to-end voice.

### Phase 2 — Integration (later, NOT this task)

After a GO and after the relay exists: wrap op25 as the scheduler's
`ems_scanner` default job (op25 as a managed child process is the clean answer to
the CLAUDE.md "SDRTrunk-as-child vs unit" open question — op25 holds the SDR as
the low-priority default and yields to NOAA passes). Pipe op25 audio → ffmpeg →
Icecast (house pattern), log calls with talkgroup labels to `/var/lib/scanner`,
surface them on the Flask `/calls` page.

## op25 invocation (starting point — verify against op25's own README)

Single dongle, headless, Phase II two-slot TDMA, web dashboard on **:8090**
(avoids the radio's 8080 and the scanner UI's 8081):

```bash
./rx.py --args 'rtl' -N 'LNA:36' -S 1000000 -q 0 \
        -T moswin.tsv -V -2 \
        -l http:0.0.0.0:8090 2> stderr.log
```

`moswin.tsv` (tab-separated; columns are the boatbod standard):

```
"Sysname"  "Control Channel List"  "Offset"  "NAC"   "Modulation"  "TGID Tags File"  "Whitelist"  "Blacklist"  "Center Frequency"
"MOSWIN"   "769.16875"             "0"       "0x1C3" "cqpsk"       "moswin_tags.tsv" ""           ""           ""
```

Tuning decisions, in priority order (the dashboard's error/symbol metrics guide
all of these):

1. **Modulation `cqpsk` first.** 700 MHz P25 systems are usually simulcast,
   where `cqpsk` (LSM) decodes far better than `fsk4`. If the CC won't lock and
   the carrier is clearly present, try `fsk4`.
2. **Control channel type.** Start *without* `--tdma-cc` (assume a Phase I C4FM
   control channel, the common case even on Phase II voice systems). Only add
   `--tdma-cc` if the CC genuinely won't decode — and note it forces the symbol
   rate to 6000 and disables FDMA-CC decode, so it's a real either/or.
3. **ppm offset (`-q`)** — RTL-SDR dongles drift; set the correction once
   measured. A wrong ppm is a common "carrier present but no decode" cause.
4. **Gain (`-N LNA:nn`)** — tune for lowest error rate, not maximum gain;
   overload at 700 MHz degrades decode.
5. **Sample rate** — `-S 1000000` is a stable single-dongle starting point;
   op25 retunes the one dongle to follow voice grants, so you don't need to
   capture the whole 769–771 band at once.

---

## Claude Code prompt (Phase 1 bring-up)

Run Claude Code from `/srv/scanner` on the Pi (so the scanner `CLAUDE.md` loads).
Paste everything in the box below.

```
GOAL
Prove the discone can receive and decode the Cape Girardeau County MOSWIN P25
Phase II system using the Nooelec, BEFORE we build any antenna relay. This is a
manual, interactive bring-up — NOT a service, NOT scheduler-integrated, NOT
Icecast-streamed. The single deliverable that matters is a clear answer to:
"is the discone good enough at 769 MHz to justify building the relay?"

HARD CONSTRAINTS
- Do NOT modify anything in /srv/radio or /opt/sdr-tuner. That's the radio
  project on the dx-R2; it must keep running untouched.
- Do NOT integrate this into scheduler.py or the ems_scanner job, do NOT add a
  systemd unit, do NOT stream to Icecast, do NOT touch relay/GPIO anything.
  Those are later phases.
- op25 is a large external dependency (GNU Radio). Build it OUTSIDE the deploy
  tree — do NOT vendor it into files/opt/scanner. Put it under the scanner
  user's space (e.g. /opt/op25, owned appropriately) and reference it.
- Keep everything under the scanner user / scanner conventions.

PRECONDITIONS (I will confirm before you run anything live)
- The discone is cabled DIRECTLY into the Nooelec (dipole temporarily
  unplugged). I'll confirm when done.
- The scanner scheduler service is STOPPED so op25 can own the dongle. Stop it
  and verify nothing else holds the Nooelec before opening the device.

MOSWIN PARAMETERS (authoritative — from this repo's CLAUDE.md)
- System: MOSWIN, P25 Phase II
- Control channel: 769.16875 MHz (Site 033 primary)
- NAC: 0x1C3 (451), System ID: 1CE, WACN: BEE00
- Sites 033/055/060, all 769-771 MHz
- RadioReference sid/6847 (full channel + talkgroup list lives there; the
  control channel alone is enough to start)

DECODER
Use op25 (boatbod fork, https://github.com/boatbod/op25), default branch
(GNU Radio 3.10). Headless, with its HTTP dashboard. Do NOT use SDRTrunk.

STEPS
1. Confirm the dongle and the antenna feed:
   - rtl_test -t (confirm the Nooelec is present, note its index/serial).
   - Sweep the band to see the control-channel carrier and gauge discone signal
     level: rtl_power -f 769.0M:771.0M:2k -g 30 -i 5 -1 to a CSV, and summarize
     whether a strong carrier sits at/near 769.16875. Record the level — this is
     our first discone-quality number.
2. Build op25 from the boatbod fork per its install instructions. Capture any
   build issues and how you resolved them. Verify the build runs (rx.py --help).
3. Create the MOSWIN config in a tracked location under files/opt/scanner/p25/
   (this part IS small and belongs in the repo):
   - moswin.tsv (boatbod trunk.tsv format) with control channel 769.16875,
     NAC 0x1C3, Modulation cqpsk to start, tags file moswin_tags.tsv.
   - moswin_tags.tsv — seed with whatever Cape Girardeau County talkgroup
     labels you can derive (Cape CO ALL, Cape CO Fire Disp 1/2, Cape CO EMS,
     E Fire 1, plus MO interop). Leave it easily extensible; full data needs a
     RadioReference account, so don't block on it.
   - run-bringup.sh — a wrapper that launches op25 against moswin.tsv with the
     HTTP dashboard on port 8090 (NOT 8080/8081 — those are the radio and
     scanner UIs). Make the gain/ppm/modulation easy to edit at the top.
4. Run op25 interactively and tune for decode, using the dashboard metrics:
   - Start with Modulation cqpsk and WITHOUT --tdma-cc.
   - If the carrier is clearly present but the CC won't lock: try fsk4; if still
     nothing, try --tdma-cc (and note it forces 6000 symbol rate / disables FDMA
     CC). Adjust ppm (-q) and gain (-N LNA:nn) for lowest error rate.
   - Confirm on the dashboard: NAC reads 0x1C3, System/WACN match MOSWIN, TSBKs
     decode, error rate is low.
5. Observe live for a bit: list the talkgroups that appear, flag which are clear
   vs ENC (encrypted), and capture ONE short audio sample of a clear call to a
   wav file to prove end-to-end voice (use op25's wav/file output or a
   udp->ffmpeg capture — your call, keep it simple, no Icecast).

DELIVERABLES
- The op25 build (outside the deploy tree) + the tracked config in
  files/opt/scanner/p25/ (moswin.tsv, moswin_tags.tsv, run-bringup.sh).
- docs/P25_BRINGUP.md: a short report with (a) the rtl_power signal level at the
  control channel, (b) the final working op25 command line (gain/ppm/modulation/
  tdma-cc settings that worked), (c) NAC/System/WACN confirmation, (d) the list
  of talkgroups seen and their clear/ENC status, (e) whether a clear call
  decoded to intelligible audio, and (f) your GO / NO-GO recommendation on
  whether discone reception justifies building the relay, with the evidence.
- Leave the dipole reconnect + scheduler restart to me; just tell me when you're
  done so I can put the antenna back.

BAIL-OUT / STOP CONDITIONS
- If op25 won't build after a reasonable effort, STOP and report the blocker
  with what you tried — don't thrash for hours or start hacking GNU Radio.
- If the control channel carrier is absent or buried in the rtl_power sweep,
  that itself is the answer (discone/placement problem) — report it as a
  NO-GO with the data rather than chasing decode settings that can't work.
- Don't expand scope. No scheduler, no streaming, no relay, no radio project.
```

---

## After this task

- **GO** → proceed to build the antenna relay (`ANTENNA_SWITCH_BUILD.md`), then
  Phase 2 integration (op25 as the scheduler's default `ems_scanner` job, Icecast
  + call logging).
- **NO-GO** → the problem is reception, not software: revisit discone placement,
  feedline, and 700 MHz coverage, or consider a tuned 700 MHz antenna on the
  switch's reserved input. No point building the relay until a direct-cable test
  passes.
- Either way, update `PROJECT_MEMORY.md` and the scanner `CLAUDE.md` with the
  result (the bring-up report's GO/NO-GO and the working op25 settings).
