# Jobs

Detailed notes on each scheduled job. Implementation notes, references,
edge cases.

## EMS scanner (priority 1, perpetual default)

**Frequency band:** Cape Girardeau County MOSWIN — primarily VHF
~150-159 MHz region. The MOSWIN system uses dynamically assigned voice
channels; the scanner follows the control channel (typically the lowest
frequency in the site's allocation) and decodes call assignments.

**Software options:**

| Tool | Pros | Cons |
|---|---|---|
| **SDRTrunk** | Modern, well-maintained, good UI for testing, P25 Phase II support | Java-based, heavier on CPU than op25 |
| **op25** | Lightweight, Python, hackable | Less polished, older docs, sometimes fiddly setup |

Recommendation for first build: **SDRTrunk** for ease. Switch to op25
if Pi 5 CPU becomes a problem (unlikely with one SDR).

**RadioReference.com is the canonical source** for talkgroup lists and
system config. Cape Girardeau County wiki page lists current state.
URL: https://wiki.radioreference.com/index.php/Cape_Girardeau_County_(MO)

**Important talkgroups (incomplete list, verify before building):**
- Zone 1 Cape County All
- Cape CO Travel
- Cape CO Fire Disp 1, Fire Disp 2
- Cape CO EMS
- E Fire 1 (East-side fire)

Plus MO statewide interop, regional channels.

**Conventional channels (non-MOSWIN, also worth monitoring):**
- 155.205 MHz — Cape County Private Ambulance simplex
- 155.340 MHz — EMS Cap Hospital interop
- 153.830 / 153.890 / 153.950 MHz — Cape Co Fire frequencies (note: most
  fire departments have migrated to MOSWIN; these may be deprecated)

**Encryption:** as of project planning (Nov 2025), Cape County's listed
talkgroups appear unencrypted. Always verify in RadioReference before
deployment; this can change.

**Preempt behavior:** when preempted by a higher-priority job, the scanner
should:
1. Stop the active decode session
2. Log the timestamp + currently-active talkgroup (if any) for later review
3. Release the SDR
4. On resume, reacquire the control channel and continue

The "we missed N minutes of EMS" gap should be visible in the UI.

## NOAA APT — removed 2026-06-08

NOAA APT weather-satellite imagery was removed from this project. The aging
NOAA-15/18/19 birds are end-of-life and, more fundamentally, the discone
physically can't hear a 137 MHz LEO satellite (zenith null + vertical-vs-RHCP
polarization mismatch). Weather-sat imagery moved to the sibling **radio**
project, which decodes **Meteor LRPT** on a V-dipole on the SDRplay RSPdx-R2.

## AIS poll (priority 3, scheduled)

**Frequency:** 161.975 MHz (channel 87B) + 162.025 MHz (channel 88B).
Both are used simultaneously by AIS; software typically scans both.

**Software:** `rtl_ais` — open-source AIS decoder for RTL-SDR. Outputs
NMEA strings on stdout.

**Schedule:** every 30 minutes, run for 3-5 minutes. Mississippi River
traffic is slow enough that this sample rate catches most vessels.

**Storage:** append NMEA to a daily log file. Periodically (nightly?)
parse log and update a SQLite database of vessel positions for the UI.

**Optional: feed MarineTraffic.com.** Their feeder program is small and
runs alongside `rtl_ais`. Earns "supporter" status on the MarineTraffic
account, slight perks. Sign up at marinetraffic.com first if interested.

**Expected reception range:** with a wideband VHF antenna in the attic,
20-40 km. That covers approximately Cape Rock to Commerce, MO on the
Mississippi. Whether it catches enough barge traffic to be interesting
is empirical.

## ACARS poll (priority 2, scheduled, optional)

**US ACARS frequencies:**
- 131.55 MHz (primary, most common)
- 131.725 MHz
- 131.45 MHz
- 130.025 MHz, 130.45 MHz, 131.125 MHz (less common but worth scanning)

**Software:** `acarsdec` — decodes ACARS messages from rtl_sdr.

**Schedule:** every 30 minutes, run for 3-5 minutes on the primary 131.55.

**Storage:** append decoded messages to a daily log with timestamps.

**What you'll see:** mostly automated traffic — gate changes, position
reports, weather requests, OOOI (Out/Off/On/In) events, fuel reports.
Occasionally interesting things like medical emergencies, diversions,
or maintenance issues. Most messages are addressed to specific aircraft
by ICAO 24-bit address.

**Reality check:** ACARS is fun for about 30 minutes, then loses charm
unless you're specifically interested in airline operations. Try for a
week, decide.

## NOAA Weather Radio (priority 4, deprioritized — likely won't implement)

The case for it: hyperlocal weather alerts via SAME decoding, automatic
phone notifications when severe weather hits Cape Girardeau County.

The case against: your phone already does this via the National Weather
Service and WEA (Wireless Emergency Alerts). Adding it here is duplicate
infrastructure for no real benefit.

**Decision:** skip unless a specific use case emerges.

## Manual override (priority 10)

This is technically a "job" too. When the user hits the dashboard's
override form:

1. UI POSTs `{freq, mode, duration_s}` to scheduler
2. Scheduler creates a `ManualJob` instance and pushes it with priority 10
3. Current job receives preempt signal
4. ManualJob runs: tunes the SDR, streams audio to Icecast, sleeps for
   `duration_s` seconds
5. After expiry (or user clicks "release" in UI), control returns to the
   scheduler

This lets you experiment with arbitrary frequencies without restarting
services. Useful for tuning AM amateur band, NOAA WX out of curiosity,
or anything else interesting in the moment.
