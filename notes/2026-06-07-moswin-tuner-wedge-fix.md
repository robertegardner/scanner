# 2026-06-07 — MOSWIN silent: RTL tuner wedge on the rtl_fm↔SDRTrunk hand-off

MOSWIN feed showed "streaming" but played no traffic while ATC still worked.
Root-caused to a shared-dongle hand-off bug and shipped a durable fix.
Commits: `dbe3ad9` (first cut) + `c7f659d` (centralize + regression fix).

## Symptom
- `/listen` MOSWIN mounts (All/Police/Fire/Interop) all "Connected" but silent.
- ATC (aviation AM) worked fine.
- Two distinct causes got conflated at first:
  - The ~24h of total silence was the **SD-card restore** to the new larger
    card (unrelated).
  - The recurring silence-after-tuning-ATC-and-back is the **tuner wedge** below.

## Root cause
ATC (rtl_fm `MonitorJob`) and MOSWIN (`EMSJob` → SDRTrunk) share the one Nooelec
(0bda:2838). After an rtl_fm→SDRTrunk hand-off, SDRTrunk's R820T I2C register
init fails reliably:
```
org.usb4java.LibUsbException: USB error 1: error writing byte buffer: Input/Output Error
  (writeI2CRegister → R8xEmbeddedTuner.initTuner)
→ Channel: Cape County MOSWIN auto-start failed: No Tuner Available
```
SDRTrunk stays up as a **tunerless zombie** — mounts "Connected", decoding
nothing. ATC is unaffected because rtl_fm tolerates the wedged device state;
SDRTrunk's register write does not. (So "ATC works, MOSWIN doesn't" actually
proves the dongle/antenna are healthy and isolates the fault to SDRTrunk
acquisition.)

**Why it didn't self-heal:** the `EMSJob` watchdog keyed "tuner ok" off the
log strings `ADDED / STARTING` / `TUNER PLUG-IN DETECTED`, which SDRTrunk emits
on *attempt*, not success — and a register I/O error logs no `TUNER UNPLUGGED`.
So `_tuner_fault()` returned None forever. This is the "~2.5h silent dead air"
case the `ems_scanner.py` header comment already warned about.

## Immediate recovery (verified)
`sudo systemctl stop scanner-scheduler && sleep 8 && sudo systemctl start
scanner-scheduler`. The clean SIGTERM teardown + idle settle gap recovers the
dongle. NOTE: tuning ATC↔MOSWIN does **not** fix it — each MOSWIN resume re-opens
the same wedged device.

## Durable fix (deployed + verified)
- **`lib/sdr.py` `reset_dongle()`** — `sudo usbreset 0bda:2838` + 3s settle.
  Called from the scheduler `_loop` right after `_sdr.acquire()`, before *every*
  job opens the dongle, so it clears the wedge in **both** directions
  (rtl_fm↔SDRTrunk) and for NOAA/manual. ~3s, negligible vs. job durations.
- **`sudoers.d/scanner`** — `scanner ALL=(ALL) NOPASSWD: /usr/bin/usbreset 0bda\:2838`
  (the scanner user only had systemctl sudo before). Colon MUST be escaped or
  `visudo -c` rejects it.
- **Watchdog** — `NO TUNER AVAILABLE` / `UNABLE TO START` / `AUTO-START FAILED`
  set `_tuner_start_failed` → immediate fault → requeue → relaunch-with-reset.
  Success keyed off the channelizer line
  (`ComplexPolyphaseChannelizer ... providing [N] channels`).

## Gotchas for next time
- **The scheduler reads SDRTrunk's STDOUT, not its log file.** The console
  layout has **no thread-name column**. My first watchdog cut keyed success off
  `sdrtrunk channel [` — that tag only exists in the log FILE — so it never
  matched in stdout and EMS **false-restarted every ~50s** (shipped in dbe3ad9,
  fixed in c7f659d). Any new SDRTrunk log marker must appear in **stdout**
  (logger-name + message), and verify it's present on success / absent on
  failure before relying on it. The channelizer line satisfies both.
- `usbreset` takes `VVVV:PPPP` or `BBB/DDD`, **not** a `/dev/bus/usb/...` path
  ("No such device found" if you pass a path).
- Device-number changes after a reset are transparent: SDRTrunk discovers by USB
  bus/port, rtl_fm by device index.

## Verify it's healthy
```bash
# 0 expected over a 40s+ window:
sudo journalctl -u scanner-scheduler --since "2 min ago" | grep -c "tuner unhealthy"
# reset fires before each job:
sudo journalctl -u scanner-scheduler | grep "Reset RTL dongle"
# decoding + calls:
curl -s http://127.0.0.1:8082/calls?limit=3
```

## Still open
- Same reset belt isn't strictly needed for the rtl_fm side (it tolerates the
  wedge), but the centralized loop-level reset now covers it anyway.
