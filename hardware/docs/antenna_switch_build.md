# Pi-Controlled Antenna Switch — Build Guide & Test Protocol

Scanner project (`robertegardner/scanner`). Selects one of several antennas onto
the single Nooelec NESDR SMArt v5 input under software control, so the scheduler
can pick the right antenna per job.

> **2026-06-08 — NOAA APT removed.** Part of this doc's original rationale was a
> dedicated 137 MHz RHCP (QFH/V-dipole) antenna for NOAA APT. NOAA APT has been
> removed from the project (the discone can't hear a 137 MHz LEO sat; weather-sat
> imagery moved to the sibling radio project as Meteor LRPT). So the switch is no
> longer needed to feed a sat antenna — the only live driver left is routing the
> **discone** for 700 MHz P25 and the **dipole** for the 131–162 MHz VHF cluster
> (AIS/ACARS). The 137 MHz/QFH references below are stale; treat them as removed.

> **Why this exists (and why it didn't for the radio project).** The radio
> project's RSPdx-R2 has three antenna inputs, so antenna selection there is a
> software API call and the old GPIO relay design was scrapped. The scanner's
> Nooelec has **one** SMA input, so a real RF switch is genuinely needed again.

## The problem this solves

The scanner's jobs live in two very different parts of the spectrum, and **no
single antenna serves both well**:

| Job | Frequency | Best antenna | Why |
|-----|-----------|--------------|-----|
| EMS scanner — MOSWIN P25 (**default job**) | **769–771 MHz** | **Discone** | The VHF dipole physically can't receive 700 MHz at all |
| AIS | 162 MHz | Dipole | Better VHF gain than the discone |
| ACARS | 131 MHz | Dipole | Same |

So the switch lets the scheduler route the discone for the 700 MHz P25 work and
the dipole for the 131–162 MHz cluster. That's the core win, and it's why two
inputs are *necessary*, not a nice-to-have.

## Recommendation: how many inputs

**Build an SP3T (two-relay tree, three input jacks). Populate two now
(discone + dipole); reserve the third jack.**

- **2 inputs** covers the two real, non-overlapping needs today.
- **A 3rd reserved input** is cheap insurance against one realistic upgrade
  without another attic trip: a tuned 700 MHz gain antenna (collinear/yagi) to
  lift P25 decode margin above the wideband discone.
- **Stop at 3 in a relay tree.** Every relay in the signal path adds insertion
  loss, and that bites hardest at 769 MHz. A 4th antenna means a 3-deep path
  (~3–6 dB at UHF), which can erase the very gain a tuned UHF antenna was meant
  to add. **If you really want 4+ ports, don't deepen the tree — use a single
  SP4T coaxial relay** (Option B below): one relay in every path, best loss and
  isolation.

The tree is arranged so the **discone sits on the shortest (1-relay) path** and
is also the **fail-safe** position (all relays de-energized → discone). That
single choice satisfies three things at once: the default/most-used job, the
worst-case UHF-loss path, and graceful failure if the Pi or GPIO dies.

---

## Architecture

### Option A (recommended): SPDT-relay tree

```
   Nooelec SDR ──[SMA out]── K1.COM
                              K1.NC ───────────────── ANT1  DISCONE   (K1 off)        [1 relay]
                              K1.NO ── K2.COM
                                        K2.NC ─────── ANT2  DIPOLE    (K1 on, K2 off) [2 relays]
                                        K2.NO ─────── ANT3  AUX/spare (K1 on, K2 on)  [2 relays]

   K1, K2 = SMA coaxial SPDT RF relays (DC–3 GHz, gold contacts)
   Control: ULN2003A Darlington driver, one channel per relay coil
```

**Truth table**

| Selected antenna | K1 (GPIO17) | K2 (GPIO27) | Relays in path |
|------------------|:-----------:|:-----------:|:--------------:|
| ANT1 Discone (default + fail-safe) | OFF | don't care | 1 |
| ANT2 Dipole (VHF 131–162) | ON | OFF | 2 |
| ANT3 Aux / future | ON | ON | 2 |

All relays de-energized (Pi off, GPIO floating, software not running) → **discone**.
That's the antenna the default EMS job needs, so the scanner degrades gracefully.

To later expand to a true SP4T tree, add a third relay K3 fed from `K2.NO`
(needs a third GPIO and pushes two paths to 3 relays). I don't recommend it —
go to Option B instead if you need four.

### Option B (if you insist on 4+ inputs): single SP4T coaxial relay

A one-piece SP4T coaxial relay puts exactly one relay in every path (best loss
and isolation, especially at 769 MHz). Trade-offs:

- Drive it with the ULN2003 in **one-hot** mode (one coil per position). Most
  SP4T relays are *non*-fail-safe: all coils off = no port connected. If you want
  fail-to-discone you need a **fail-safe (spring-return) or latching** SP4T, which
  is pricier/rarer. With a plain one-hot SP4T, accept that a GPIO failure leaves
  you with no antenna until software re-asserts a position (the scheduler sets it
  on every dispatch anyway).
- Cost is higher and the good ones are usually surplus (Mini-Circuits / Ducommun /
  Radiall on eBay).

The rest of this guide assumes **Option A**.

---

## Build sheet (parts list)

### RF path — the parts that matter most

| Item | Qty | Notes |
|------|:---:|-------|
| **SMA coaxial SPDT RF relay**, DC–3 GHz, **5 V coil**, gold contacts | 2 | The single most important spec. Must be a *real* RF relay rated to ≥3 GHz, **not** a general-purpose SRD/Songle relay (those roll off badly above ~100 MHz and will wreck your 769 MHz path). Coaxial SMA-port relays need no RF PCB skill. Examples: Mini-Circuits ZASW-2-50DR+ (premium, ~0.7 dB loss, >75 dB isolation), or generic "SMA SPDT RF relay DC-6GHz 5V" — verify the UHF loss/isolation numbers before buying. If only 12 V relays are available, see the boost-converter note under Power. |
| SMA bulkhead (panel-mount) F-F jack | 4 | 1 output (to SDR) + 3 inputs (discone, dipole, aux). |
| RG316 SMA male–male jumper, ~10 cm | 6 | PTFE coax, good to GHz, low loss at 769 MHz. Use for *all* internal interconnects and jack pigtails. **Do not** use RG174 internally — too lossy at UHF. |
| 50 Ω SMA terminator | 1 | For isolation testing (terminate unused ports). |
| SMA dust caps | 1–2 | Cap the reserved aux jack until used. |

### Driver / control board

| Item | Qty | Notes |
|------|:---:|-------|
| ULN2003A (16-pin DIP) | 1 | 7-channel Darlington array with built-in flyback diodes — far cleaner than discrete transistors for multiple relays. We use 2 of 7 channels (room for K3 free). |
| 16-pin DIP socket | 1 | So you never solder-cook the chip. |
| Protoboard, 5×7 cm | 1 | The driver board. |
| 10 kΩ resistor (¼ W) | 2 | Pull-down on each ULN2003 input → relays stay OFF (discone) during Pi boot when GPIO floats. |
| 0.1 µF ceramic capacitor | 1 | Decouple the relay 5 V rail at the board. |
| 100 µF electrolytic cap | 1 | Bulk reservoir for relay switching transients. |
| Male header pins, 0.1″ | ~10 | Coil outputs + power + GPIO input header. |
| Female–female DuPont jumpers | 4 | Pi GPIO → board (2 signal + 5 V + GND). |
| 22 AWG hookup wire | small spool | Point-to-point. |

### Power

- **5 V relays (recommended):** take 5 V from Pi header **pin 2 or 4**, GND from
  **pin 6**. Pi 5 + PoE HAT supplies this easily (two relay coils ≈ 100 mA).
- **12 V relays (only if that's what you can get):** add an MT3608 5→12 V boost
  module (set to 12.0 V *before* wiring it in), exactly as in the radio project's
  `hardware/docs/BUILD_GUIDE.md`. Tie ULN2003 COM (pin 9) to +12 V instead of +5 V.

### Enclosure

| Item | Qty | Notes |
|------|:---:|-------|
| **PETG** filament | ~80 g | PETG, not PLA — the attic hits 50 °C+ and PLA softens at ~60 °C. |
| 3D-printed box with 4 SMA panel cutouts + board standoffs | 1 | Adapt the case from the radio guide; you need 4 SMA holes (1 out, 3 in) and M3 standoffs for the driver board. |
| M3 × 8 mm screws + nuts | 4 | Board mount. |
| M2.5 × 6 mm screws | 4 | Lid. |

### Tools

Soldering iron, multimeter (continuity + resistance), wire strippers, flush
cutters, heat-shrink — same as the radio build.

**Strongly recommended:** a **NanoVNA** (~$50). It lets you measure insertion
loss and isolation per path at 137 / 162 / 769 MHz on the bench **without needing
the antennas connected** — which means you can fully validate the RF before the
discone even arrives. You're a homelabber; if you don't have one, it's worth it
for this and every future RF project.

---

## Driver circuit (ULN2003A)

```
                          +5V (Pi pin 2/4)  ── 0.1µF ──┐── 100µF ──┐
                            │                           │          │
                            ├───────────── ULN2003 pin 9 (COM, flyback clamp)
                            │
              Relay K1 coil +├──────────────┐
              Relay K2 coil +├───────────┐  │
                                         │  │
   GPIO17 ──[in]── ULN2003 pin 1 ──(out) pin16 ── K1 coil -   (energize K1)
   GPIO27 ──[in]── ULN2003 pin 2 ──(out) pin15 ── K2 coil -   (energize K2)
                            │
   pin1 ──10kΩ── GND        (pull-down: relay OFF while GPIO floats at boot)
   pin2 ──10kΩ── GND
                            │
                 ULN2003 pin 8 ── GND ── Pi pin 6 (shared ground)
```

- ULN2003 inputs are 3.3 V-logic friendly (internal ~2.7 kΩ series base
  resistor) — drive them straight from the Pi GPIO.
- Outputs are open-collector (they *sink* the coil to ground). Coil **+** goes to
  the relay supply; coil **−** goes to the ULN2003 output pin.
- **COM (pin 9) → relay supply +** provides the coil flyback clamping; don't skip
  it.
- The 10 kΩ pull-downs guarantee both relays are de-energized (→ discone) during
  the boot window before software claims the pins.

### GPIO pin choice (Pi 5 + UCTRONICS U627803 PoE HAT)

- The radio project no longer uses any GPIO (its relay plan is dead), so there's
  no conflict from that side. Keep everything under the scanner's `scanner` user
  and `/srv/scanner` per the project's separation rules — **don't touch
  `/srv/radio` or `/opt/sdr-tuner`.**
- The U627803 HAT exposes 36 of 40 pins; the 4 it consumes are typically the fan
  and an I²C OLED (GPIO2/3 + a fan pin). **Verify against the HAT's manual** and
  with `pinout` / `gpioinfo` before committing.
- Proposed (commonly free, contiguous): **GPIO17 = header pin 11** (K1),
  **GPIO27 = header pin 13** (K2). Power from pin 2 (5 V) and pin 6 (GND).
- Pi 5 GPIO needs the lgpio backend:
  ```bash
  sudo apt install -y python3-gpiozero python3-lgpio
  sudo usermod -aG gpio scanner    # let the service user toggle pins
  ```

---

## Build sequence

1. **Socket first.** Solder the 16-pin DIP socket (don't insert the ULN2003 yet).
2. **Pull-downs.** Solder a 10 kΩ from input pin 1 → GND and pin 2 → GND.
3. **Caps.** 0.1 µF and 100 µF across the relay 5 V rail to GND (mind electrolytic
   polarity).
4. **Power/clamp.** Wire +5 V rail to ULN2003 pin 9, GND to pin 8.
5. **Coil headers.** Run output pin 16 → K1 coil(−) header, pin 15 → K2 coil(−)
   header; both coil(+) headers → +5 V rail.
6. **Input header.** Bring GPIO17 → pin 1, GPIO27 → pin 2 out to a 2-pin header;
   add the 5 V and GND DuPont pins.
7. **Insert the ULN2003** into the socket (notch/pin-1 orientation correct).
8. **RF interconnects (no soldering of RF if using coaxial relays):**
   - `K1.COM` → output panel jack (to SDR) via RG316.
   - `K1.NC` → ANT1 (discone) panel jack.
   - `K1.NO` → `K2.COM`.
   - `K2.NC` → ANT2 (dipole) panel jack.
   - `K2.NO` → ANT3 (aux) panel jack — cap it for now.
9. **Continuity audit** with a multimeter before any power (see Phase 0).

---

## Software

Drop this in `files/opt/scanner/lib/antenna_switch.py`, deploy with
`deploy.sh`, and have the scheduler call it before each job dispatch. It is a
safe no-op until the GPIO pins are configured, so you can deploy it before the
hardware is finished.

```python
#!/usr/bin/env python3
"""
antenna_switch.py — Pi-controlled RF antenna selector for the scanner SDR.

Drives an SPDT-relay tree (hardware/docs/ANTENNA_SWITCH_BUILD.md) that selects
one antenna onto the single Nooelec input.

  * No-op if GPIO libs are missing or pins unset (deploy before hardware).
  * Caches last selection; only toggles relays on a real change.
  * Fail-safe to the discone (all relays off) — the only antenna that hears the
    769 MHz MOSWIN control channel the default EMS job needs.

Tree:
    SDR ── K1.COM
            K1.NC ──── DISCONE   (K1 off)          [1 relay]
            K1.NO ── K2.COM
                      K2.NC ──── DIPOLE   (K1 on, K2 off)  [2 relays]
                      K2.NO ──── AUX      (K1 on, K2 on)   [2 relays]

Env (/etc/scanner/config.env):
    ANTENNA_GPIO_K1=17
    ANTENNA_GPIO_K2=27
    ANTENNA_UHF_THRESHOLD_HZ=400000000
"""
from __future__ import annotations
import json, os, tempfile, logging

log = logging.getLogger("antenna_switch")
STATE_PATH = "/run/scanner/antenna.json"

DISCONE, DIPOLE, AUX = "discone", "dipole", "aux"

# (K1, K2) coil states per antenna
_TRUTH = {
    DISCONE: (False, False),
    DIPOLE:  (True,  False),
    AUX:     (True,  True),
}

def _env_int(name, default):
    v = os.environ.get(name)
    try:
        return int(v) if v not in (None, "") else default
    except ValueError:
        return default

_K1_PIN = _env_int("ANTENNA_GPIO_K1", 0)   # 0 = unset -> no-op
_K2_PIN = _env_int("ANTENNA_GPIO_K2", 0)
_UHF_HZ = _env_int("ANTENNA_UHF_THRESHOLD_HZ", 400_000_000)

_k1 = _k2 = None
_gpio_ok = False

def _init_gpio():
    global _k1, _k2, _gpio_ok
    if _gpio_ok or _K1_PIN == 0:
        return
    try:
        from gpiozero import OutputDevice
        _k1 = OutputDevice(_K1_PIN, active_high=True, initial_value=False)
        _k2 = OutputDevice(_K2_PIN, active_high=True, initial_value=False)
        _gpio_ok = True
        log.info("antenna_switch GPIO ready (K1=%d K2=%d)", _K1_PIN, _K2_PIN)
    except Exception as e:                       # missing libs, perms, etc.
        log.warning("antenna_switch GPIO unavailable; no-op mode: %s", e)
        _gpio_ok = False

def _read_state():
    try:
        with open(STATE_PATH) as f:
            return json.load(f).get("antenna")
    except Exception:
        return None

def _write_state(name):
    try:
        d = os.path.dirname(STATE_PATH)
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d)
        with os.fdopen(fd, "w") as f:
            json.dump({"antenna": name}, f)
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        log.warning("could not persist antenna state: %s", e)

def set_antenna(name: str) -> str:
    """Select an antenna by name; returns the antenna selected."""
    if name not in _TRUTH:
        log.warning("unknown antenna %r -> discone", name)
        name = DISCONE
    if _read_state() == name and _gpio_ok:
        return name                       # already there; don't cycle relays
    _init_gpio()
    if _gpio_ok:
        k1v, k2v = _TRUTH[name]
        (_k2.on if k2v else _k2.off)()    # branch first, then root:
        (_k1.on if k1v else _k1.off)()    # never transiently route wrong
    _write_state(name)
    log.info("antenna -> %s", name)
    return name

def set_for_freq(hz: int) -> str:
    """UHF (>= threshold) -> discone, else dipole."""
    return set_antenna(DISCONE if hz >= _UHF_HZ else DIPOLE)

JOB_ANTENNA = {
    "ems_scanner": DISCONE,   # MOSWIN P25 @ 769 MHz
    "ais_poll":    DIPOLE,    # 162 MHz
    "acars_poll":  DIPOLE,    # 131 MHz
}

def set_for_job(job_name: str) -> str:
    return set_antenna(JOB_ANTENNA.get(job_name, DISCONE))

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    print(set_antenna(sys.argv[1]) if len(sys.argv) == 2
          else "usage: antenna_switch.py <discone|dipole|aux>")
```

**Scheduler hook.** Wherever the scheduler is about to hand the SDR to a job, add:

```python
import antenna_switch
antenna_switch.set_for_job(job.name)        # or set_for_freq(job.center_hz)
```

`set_for_job` is the explicit map; `set_for_freq` is the frequency fallback for
manual overrides (anything ≥ 400 MHz → discone, else dipole).

**Config** (`files/etc/scanner/config.env.example`):

```
ANTENNA_GPIO_K1=17
ANTENNA_GPIO_K2=27
ANTENNA_UHF_THRESHOLD_HZ=400000000
```

---

## Test protocol — confirm build quality before the attic

Run these on the bench, in order. **Phases 0–2 and 5 need no antennas** and can
be done before the discone arrives; Phases 3–4 want the antennas (or at least the
dipole you already have, plus the discone once it's in hand). Record results in
the table at the end; the **acceptance gate** is your go/no-go for sealing the box.

### Phase 0 — Visual + continuity (UNPOWERED, multimeter)

1. Inspect every solder joint; reflow anything dull or bridged.
2. Coil resistance: across each relay coil you should read a sane DC resistance
   (tens to low hundreds of Ω, per the relay datasheet), **not** open and **not**
   a short.
3. Driver wiring: confirm ULN2003 pin 1↔GPIO17, pin 2↔GPIO27, pin 8↔GND, pin
   9↔relay-supply-+; pull-downs read ~10 kΩ input-to-GND.
4. **No short** between the RF center path and ground anywhere, and **no short**
   between coil pins and RF ports.

### Phase 1 — Driver logic (POWERED, NO RF, multimeter on contacts)

Apply 5 V to the board's relay rail. With **no GPIO driven** (or Pi off), the
relays must be de-energized = **discone path**.

Toggle from the Pi:

```bash
sudo apt install -y python3-gpiozero python3-lgpio
sudo -u scanner python3 - <<'PY'
from gpiozero import OutputDevice
from time import sleep
k1 = OutputDevice(17, initial_value=False)
k2 = OutputDevice(27, initial_value=False)
for name, a, b in [("DISCONE",0,0),("DIPOLE",1,0),("AUX",1,1),
                   ("DISCONE",0,0),("DIPOLE",1,0),("AUX",1,1)]:
    k1.value = a; k2.value = b
    print(f"{name:8} K1={a} K2={b}"); sleep(2)
k1.off(); k2.off()
PY
```

You should hear crisp, single clicks (no chatter). With a multimeter on continuity
between the **output (SDR) jack center** and each **input jack center**, confirm
the truth table: exactly the selected input is connected to the output, and the
**other inputs are open** (no continuity). Repeat for all three selections.

### Phase 2 — RF insertion loss + isolation (NanoVNA, no antennas needed)

For each path, connect NanoVNA port 1 → the relevant **input** jack, port 2 →
**output** jack, terminate the other input(s) with 50 Ω. Select that path in
software. Measure **S21 (insertion loss)** and, by re-selecting a *different*
path, **isolation** to the now-deselected port.

Targets at **137 / 162 / 769 MHz**:

| Metric | Discone path (1 relay) | Dipole / aux path (2 relays) |
|--------|:----------------------:|:----------------------------:|
| Insertion loss (S21) | ≤ ~1.5 dB | ≤ ~3 dB |
| Off-port isolation | ≥ 30 dB | ≥ 30 dB |

The **769 MHz numbers are the ones that matter** — if a relay is secretly a
general-purpose part, this is where it falls apart (loss > several dB, isolation
< 20 dB). Also glance at S11/return loss; nothing should be wildly mismatched.

### Phase 3 — Live RF through the switch (Nooelec SDR)

The Nooelec is RTL device index 0 on this Pi. Use the two always-on reference
beacons:

- **VHF reference:** NOAA Weather Radio, 162.400 / 162.475 / 162.550 MHz
  (whichever is strong near Cape Girardeau). 24/7.
- **UHF reference:** MOSWIN P25 control channel **769.16875 MHz**. 24/7. (Needs
  the discone.)

Confirm the dongle, then compare relative power with one-shot sweeps:

```bash
rtl_test -t

# VHF reference through the dipole path:
rtl_power -f 162.3M:162.6M:2k -g 30 -i 5 -1 vhf_dipole.csv
# UHF reference through the discone path (after selecting discone):
rtl_power -f 769.0M:769.3M:2k -g 30 -i 5 -1 uhf_discone.csv
```

Checks:

1. **Right antenna, right signal.** With the *dipole* selected you should see the
   162 MHz reference; with the *discone* selected you should see the 769 MHz
   control channel. Selecting the wrong antenna should make the respective signal
   drop sharply — that proves the switch is actually switching.
2. **Switch tax.** Compare the reference-bin level **through the switch** vs.
   **antenna straight into the dongle**. The drop should match Phase 2
   (≈1–3 dB). A bigger drop means a bad joint, wrong relay, or lossy coax.
   (`rtl_power` levels are relative, so use them for *differences*, not absolute
   calibration.)
3. **P25 lock (once discone is in hand).** Point SDRTrunk/op25 at 769.16875 MHz
   through the discone path and confirm control-channel lock and decode. That's
   the real proof the default job will work.

### Phase 4 — Reliability cycling + thermal soak

Mechanical relays and attic heat both deserve a burn-in.

```bash
sudo -u scanner python3 - <<'PY'
from gpiozero import OutputDevice
from time import sleep
k1 = OutputDevice(17); k2 = OutputDevice(27)
for i in range(100):
    for a,b in [(0,0),(1,0),(1,1)]:
        k1.value=a; k2.value=b; sleep(0.3)
k1.off(); k2.off()
print("100 cycles done")
PY
```

- No missed/sticky switches, no chatter during the run.
- Re-run Phase 1 continuity afterward — contacts must still be clean.
- Leave the board powered for ~2 hours in a warm spot (or just verify it's fine
  at room temp and trust PETG for the heat). The ULN2003 and relays should be
  barely warm. Anything hot = a wiring fault or undersized supply.

### Phase 5 — Software integration

```bash
sudo -u scanner python3 /opt/scanner/lib/antenna_switch.py discone
sudo -u scanner python3 /opt/scanner/lib/antenna_switch.py dipole
sudo -u scanner python3 /opt/scanner/lib/antenna_switch.py aux
cat /run/scanner/antenna.json        # reflects last selection
```

- Confirm each call clicks the right relays (audible + Phase 1 continuity).
- Call the same antenna twice — the relays should **not** re-cycle the second
  time (state cache working).
- Temporarily unset `ANTENNA_GPIO_K1` and confirm the module no-ops cleanly
  (logs a warning, returns the name, doesn't crash).
- Trigger a real job from the scheduler and confirm it selects the mapped
  antenna before tuning.

---

## Acceptance gate (go / no-go to the attic)

Seal the box only when **all** of these pass:

- [ ] Phase 0: clean continuity, sane coil resistance, no RF-to-ground or
      RF-to-coil shorts.
- [ ] Phase 1: all relays click crisply (no chatter); truth table verified;
      all-off = discone.
- [ ] Phase 2: insertion loss ≤ ~1.5 dB (discone) / ≤ ~3 dB (2-relay paths) and
      isolation ≥ 30 dB **at 769 MHz** (and at 137/162).
- [ ] Phase 3: correct signal appears on the correct antenna and drops when
      switched away; switch tax matches Phase 2; (discone in hand) P25 control
      channel locks.
- [ ] Phase 4: 100-cycle test clean; continuity holds afterward; nothing
      overheats.
- [ ] Phase 5: `set_antenna` clicks correctly, caches state, no-ops without
      hardware; scheduler hook selects the right antenna per job.

### Results log

| Test | Target | Discone | Dipole | Aux | Pass? |
|------|--------|---------|--------|-----|:-----:|
| Coil resistance (Ω) | per datasheet | | | n/a | |
| Continuity: selected→out | yes | | | | |
| Continuity: deselected→out | open | | | | |
| Insertion loss @137 MHz | ≤1.5 / ≤3 dB | | | | |
| Insertion loss @162 MHz | ≤1.5 / ≤3 dB | | | | |
| Insertion loss @769 MHz | ≤1.5 / ≤3 dB | | | | |
| Isolation @769 MHz | ≥30 dB | | | | |
| Live reference level vs direct | within 1–3 dB | | | | |
| 100-cycle reliability | clean | — | — | — | |
| Software set/cache/no-op | ok | — | — | — | |

---

## Troubleshooting

**Relay doesn't click.** Check the 5 V rail at the coil; confirm the ULN2003 is
seated the right way; verify the GPIO actually toggles (`gpioinfo`); confirm
`scanner` is in the `gpio` group.

**Relay clicks but the wrong antenna is selected.** Re-check `K1.NC`/`K1.NO`
wiring against the truth table — it's easy to swap normally-closed and
normally-open. Discone must be on `K1.NC`.

**769 MHz path is much lossier than 137/162.** The relay isn't a real RF part, or
you used RG174/RG58 internally. Swap to a DC-3 GHz coaxial relay and RG316.

**Random clicking at boot.** A pull-down is missing/open. Both ULN2003 inputs
need ~10 kΩ to GND so relays stay off (discone) before software runs.

---

## Note on project memory

The live `scanner/CLAUDE.md` is ahead of the bundled `PROJECT_MEMORY.md`: the
scanner is past skeleton (stages 0–5 running, 2026-05-23), and MOSWIN P25 is
**700 MHz (769–771)**, not VHF. Worth refreshing `PROJECT_MEMORY.md` so the next
planning session isn't working from the stale "VHF MOSWIN / skeleton" picture —
per your own "stale memory is worse than none" rule at the top of that file.
