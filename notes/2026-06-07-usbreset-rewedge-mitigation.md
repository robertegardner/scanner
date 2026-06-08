# 2026-06-07 (PM) — the tuner-wedge `usbreset` is re-killing a degrading dongle

Follow-up to `2026-06-07-moswin-tuner-wedge-fix.md`. That fix added a
`reset_dongle()` (`sudo usbreset 0bda:2838`) before *every* job acquire. On this
now-degrading dongle, that reset is itself the thing knocking it off the USB bus.

## What happened
The Nooelec dropped fully off the bus (the hard wedge from
`project_usb_overcurrent`: `error -110/-71`, `unable to enumerate`). User
**PoE-cycled the Pi to recover it** — and that worked: dmesg shows the dongle
enumerate **clean** at +1.0s into boot:
```
[ 1.013] usb 1-1: New USB device found, idVendor=0bda, idProduct=2838
[ 1.013] usb 1-1: Product: NESDR SMArt v5  SerialNumber: 22012952
```
It stayed healthy for ~45s. Then it died — and the cause was **our own software**:
```
21:58:57 scheduler: Starting job: ems_scanner
21:58:57 sudo … COMMAND=/usr/bin/usbreset 0bda:2838      ← reset_dongle()
21:58:59 lib.sdr: Reset RTL dongle 0bda:2838
```
matches the kernel exactly:
```
[44.831] usb 1-1: reset high-speed USB device number 2
[44.952] usb 1-1: Device not responding to setup address
[45.359] usb 1-1: device not accepting address 2, error -71
         → device descriptor read/64, error -71 → unable to enumerate
```

## Diagnosis
**The PoE cycle did its job — the hardware came back clean.** What re-killed it
was the pre-acquire `usbreset`. This dongle now survives a *cold power-on* but
**not a warm `usbreset`** — it's degrading (prior over-current history + attic
heat; SoC was 67.5°C, `get_throttled` had shown `0xe0000` earlier today). The
reset that was added to clear the R820T **I2C tuner wedge** is now triggering a
deeper **USB enumeration wedge**. A healthy dongle shrugs off `usbreset` all day;
this one doesn't.

## Mitigation (implemented + deployed via deploy.sh; scheduler PID 3900)
Gate `reset_dongle()` so it only fires on a genuine job→job hand-off, never on a
freshly-enumerated dongle:
- `scheduler.py __init__`: `self._dongle_held_by: Optional[str] = None`.
- `_loop`: only `reset_dongle()` when `_dongle_held_by is not None`; else log
  `"Skipping pre-acquire reset … dongle freshly enumerated, no prior owner"`.
- After a job releases: `self._dongle_held_by = job.name` (next acquire is a real
  hand-off → reset as before).
- In the dongle-absent backoff branch: `self._dongle_held_by = None` (when it
  returns it's freshly enumerated → skip reset).

Preserves the original `dbe3ad9` hand-off fix AND the EMS watchdog
requeue-with-reset (a fault leaves `_dongle_held_by` set → reset fires). **Side
effect: replug-while-scheduler-runs is now safe** — it's sitting in the 60s
backoff loop and skips the reset when the dongle reappears, so no need to stop
the scheduler first.

Still **uncommitted on main**, alongside the `dongle_present` guard. Deployed to
`/opt` (`grep -c _dongle_held_by /opt/scanner/scheduler.py` = 4).

## PICK UP HERE after the PoE cycle
User is PoE-cycling again to reset the dongle. The deployed code is the safe
version, so on reboot the first EMS acquire should skip the reset and come up
clean. When back, verify in order:
```bash
lsusb | grep 2838                        # NESDR SMArt v5 present
sudo journalctl -u scanner-scheduler --since "2 min ago" \
  | grep -E "Skipping pre-acquire reset|Starting job|error -71|providing \[|No Tuner"
#   want: "Skipping pre-acquire reset for ems_scanner — dongle freshly enumerated"
#         "Starting job: ems_scanner"  +  channelizer "providing [N] channels"
#         NO "error -71", NO "No Tuner Available"
curl -s http://127.0.0.1:8082/calls?limit=3   # decoding traffic
```
If clean and decoding → **commit the whole uncommitted set** (this gating +
`dongle_present` guard) in one commit; update `project_usb_overcurrent` +
`project_status`. If it wedges *again* on a clean boot with the reset skipped →
the dongle hardware is failing outright; escalate to powered-hub / replacement
(the long-standing hardware fix).
