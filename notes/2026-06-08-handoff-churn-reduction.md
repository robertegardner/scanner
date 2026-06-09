# 2026-06-08 — minimize hand-off `usbreset` churn in the scheduler

Follow-up to `2026-06-07-usbreset-rewedge-mitigation.md`. That fix established
that the pre-acquire `reset_dongle()` (`sudo usbreset 0bda:2838`) is itself what
re-wedges this aging Nooelec (clean cold-power-on, but a *warm* usbreset →
`error -71` → unenumerated). It gated the reset to skip the first acquire after
fresh enumeration. This change goes further: it issues the reset **only when it
can clear a real wedge**, instead of on every job→job hand-off.

## Why the old gating still over-reset

The R820T wedge the reset exists to clear is specific to the **rtl_fm↔SDRTrunk
hand-off** (different USB access patterns across the one Nooelec). But the loop
reset on *every* hand-off where a prior job had held the dongle — including
hand-offs where no wedge is possible:

- **Monitor → Monitor** (aviation retune: change freq/gain) — rtl_fm closes and
  reopens the device cleanly.
- **Squelch toggle** — restarts the same Monitor (rtl_fm) pipeline in place.
- **Monitor ↔ Manual** — rtl_fm ↔ rtl_fm.
- **Clean EMS restart** (rc=0) — SDRTrunk → SDRTrunk with no fault.

During an aviation listening session where the operator tweaks freq/gain/squelch,
that was a `usbreset` *per tweak* — each one a chance to knock the degrading
dongle off the bus, with nothing to gain.

## The fix

Reset before a job acquires the dongle only when, since the last fresh
enumeration, **either**:

1. the incoming job's SDR tool differs from the previous holder's
   (rtl_fm↔SDRTrunk — the documented wedge trigger), **or**
2. the previous holder's run ended in failure / raised (it may have faulted on a
   tuner wedge that must be cleared before the retry).

Otherwise skip: same tool + clean prior run = a clean close/reopen, no wedge.

### Mechanics
- New `Job.sdr_tool` class attribute (`jobs/__init__.py`), default `"rtl_fm"`;
  `EMSJob` overrides to `"sdrtrunk"`. Monitor/Manual/NOAA inherit `"rtl_fm"`.
- `scheduler.py` tracks `_last_tool` + `_last_result` alongside the existing
  `_dongle_held_by` sentinel (`None` = freshly enumerated → skip).
- `_run_job` nulls `_last_result` before calling `job.run()`, so a job that
  *raises* leaves it `None` and is treated as a fault (reset on retry — fail-safe).
  `t.join()` guarantees the loop sees the stored result.
- The dongle-absent defer branch clears `_last_tool` too, so a reappear after a
  drop is treated as fresh enumeration (no reset on the comeback).

### Behaviour matrix

| Hand-off                          | Before | After  | Reason                         |
|-----------------------------------|--------|--------|--------------------------------|
| Fresh enumeration (boot/replug)   | skip   | skip   | nothing wedged                 |
| rtl_fm → SDRTrunk (or reverse)    | reset  | reset  | documented R820T wedge trigger |
| Previous job faulted              | reset  | reset  | may have left a wedge          |
| Monitor → Monitor (retune)        | reset  | **skip** | rtl_fm clean close/reopen    |
| Squelch toggle (monitor restart)  | reset  | **skip** | same                         |
| Monitor ↔ Manual                  | reset  | **skip** | rtl_fm ↔ rtl_fm              |
| Clean EMS restart (rc=0)          | reset  | **skip** | no wedge on clean exit       |

The wedge-clearing resets that matter (rtl_fm↔SDRTrunk swap; EMS watchdog
fault→requeue) are fully preserved.

## Verified live (2026-06-08, deployed + restarted)

First acquire after restart logged the fresh-enumeration skip and EMS came up
clean — no reset, no `error -71`, healthy channelizer:

```
Scheduler started (EMS default ON, NOAA off ...)
Starting job: ems_scanner
Skipping pre-acquire reset for ems_scanner — dongle freshly enumerated, no prior owner to clear
ComplexPolyphaseChannelizerM2 - Sample Rate [2400000.0] providing [96] channels
Auto-starting channel Cape County MOSWIN
```

The tool-change and same-tool paths log on the next real hand-off:
- enter aviation → `Pre-acquire USB reset ... tool change sdrtrunk->rtl_fm`
- retune / squelch toggle → `Skipping ... same tool (rtl_fm), prior run clean`

## Relation to the bigger picture

This reduces the *rate* of an operation that's risky on a degrading dongle; it
does not fix the dongle. The durable fix remains the powered USB hub (clean VBUS,
removes the under-volt sensitivity). See `project_usb_overcurrent`,
`project_temp_watch`. Asymmetry note: the radio's RSPdx-R2 never wedges partly
because it's opened once and never reset — minimizing our reset churn moves the
Nooelec's treatment in that same direction.
