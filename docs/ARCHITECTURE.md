# Architecture

Detailed design rationale for the scanner system. Companion to `CLAUDE.md`.

## Why a single SDR with a scheduler

The cheapest viable alternative is "give each job its own SDR." That works
hardware-wise — a Pi can host 4+ USB SDRs — but creates real cost ($30/dongle)
and operational complexity (USB power budgeting, identifying which dongle is
which, multiple antenna runs).

Most jobs are either bursty or polling-friendly (AIS, ACARS), so time-slicing
one SDR is the right design.

The exception is EMS — that one wants 24/7 receive time. But the data is
intermittent (most talkgroups are idle most of the time), so missing a few
minutes during a manual override or a poll is acceptable. The EMS scanner logs
everything locally to disk, so the missed window is gap, not loss; the scanner
picks back up where it left off.

## Why a Python scheduler instead of systemd timers

systemd timers can dispatch jobs on schedule, and `Conflicts=` directives
can stop one service when another starts. So why not use them?

Two reasons:

1. **Priority queue with preemption.** systemd doesn't natively model "this
   job is more important than that one." `Conflicts=` only handles pairs;
   adding more jobs means N² edge cases. A single Python process with a
   priority queue handles arbitrary preemption with no special cases.

2. **State preservation across preemptions.** When the EMS scanner is preempted
   by a manual override, we want to: (a) log what we were doing, (b) save audio
   buffer to disk so the call doesn't vanish mid-recording, (c) restart
   exactly where we left off afterward. systemd can't model that;
   a process holding state can.

## Why HTTP-on-localhost between UI and scheduler

The Flask UI doesn't own the SDR. It needs to talk to the scheduler:
"what are you doing right now?", "preempt your queue with this manual
override", "release the override."

Three implementation options were considered:

- **Direct calls within one Python process.** Simplest, but it means Flask
  and the scheduler run together, and a Flask bug can kill the scheduler.
- **Unix domain socket with a custom protocol.** Lightweight but adds
  serialization code.
- **HTTP-on-localhost (chosen).** Two systemd services, no protocol design,
  Flask can talk to the scheduler via the same `requests` library it uses
  for everything else, scheduler can scale to multiple consumers if useful
  later.

The localhost HTTP cost is negligible (~0.1 ms per call) and the operational
simplicity wins.

## Job interface

```python
@dataclass
class JobResult:
    success: bool
    artifacts: list[Path]   # files created during the job
    log: str                # what happened, for the UI to display

class Job(ABC):
    name: str
    priority: int       # 1 (lowest) to 10 (highest)
    duration_s: int     # how long we'll need the SDR

    @abstractmethod
    def run(self, sdr: SDRHandle, preempt_signal: Event) -> JobResult:
        """Run with exclusive SDR access. Return when done.

        preempt_signal: poll periodically. If set, save state and return
        ASAP; we'll be replaced by a higher-priority job.
        """
        ...
```

Priorities:
- 10 — manual override from UI
- 3 — AIS poll
- 2 — ACARS poll
- 1 — EMS scanner (default filler)

A higher-priority job in the queue preempts a lower-priority running job.
The running job receives `preempt_signal.set()` and is expected to wind
down within a few seconds.

## SDR ownership

The scheduler holds the only reference to the SDR. Jobs receive an `SDRHandle`
that exposes the operations they need (`tune(freq, mode)`, `read_samples(n)`,
etc.) but cannot acquire the underlying device themselves. This prevents
two jobs from racing for the dongle.

If a job needs to spawn a subprocess that touches the SDR (e.g., `rtl_ais`
or `SDRTrunk` as external tools), the scheduler hands off the device by
releasing its handle, the subprocess does its thing, then the scheduler
reacquires when the subprocess exits. The scheduler ensures only one
subprocess is alive at a time.

## Antenna design

A single discone or wideband dipole serves all jobs because they all fit
in roughly the same VHF window. No antenna switching needed.

Trade-off: a discone's gain in any particular band is modest (1-3 dBi),
compared to a band-specific antenna (5-7 dBi for a tuned dipole). For
this project that's the right trade — we lose ~3 dB of sensitivity
vs. ideal, but skip the complexity of switching feeds for different jobs.

## Future considerations

These are speculative; document them so they don't get lost.

**Multiple users of the Flask UI.** If two people open the dashboard
simultaneously, both see the same status (good, that's what we want).
Manual overrides should be first-come-first-served and visible to all
viewers ("override active from user X, ends at HH:MM").

**Geographic AIS coverage.** With a good wideband antenna in the attic,
expect ~20-40 km range for AIS. That's a section of the Mississippi River
from roughly Cape Rock to Commerce. Whether that catches enough traffic
to be useful is an empirical question.

**Trunked-system encryption increase.** Cape County MOSWIN is in the clear
today. If that changes, the EMS scanner's value drops dramatically. Track
this and pivot if needed.

**Adding the ADS-B Pi to this UI.** Future possibility: the scanner UI
becomes a "homelab radio dashboard" that aggregates data from the radio
Pi, this scanner Pi, and the ADS-B Pi. Out of scope for v1, worth keeping
the dashboard design extensible to multi-source data.
