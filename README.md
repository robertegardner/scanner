# Scanner

Multi-purpose secondary SDR receiver for the Cape Girardeau attic homelab Pi.
Time-slices a single RTL-SDR dongle across several jobs:

- **EMS / public safety scanning** — Cape Girardeau County MOSWIN P25 system on VHF
- **NOAA APT weather satellite imagery** — passes captured automatically on schedule
- **AIS marine traffic** — Mississippi River vessel tracking
- **ACARS** — aircraft text message decoding (optional)

Lives on the same Pi as the radio project at `/srv/radio`. Separate repo, separate
codebase, separate deployment — they only share the hardware. The radio uses the
SDRplay RSPdx-R2 for AM/FM broadcast; this project uses the spare Nooelec NESDR
SMArt v5 with a wideband VHF antenna.

A Flask web UI dashboards what's running and lets the operator override the
scheduler to listen to anything live.

**Status:** Skeleton only. Architecture documented in `CLAUDE.md`. Implementation
pending hardware (antenna ordering) and a build session with Claude Code on the Pi.

## Quick links

- [`CLAUDE.md`](CLAUDE.md) — Full project context for Claude Code. Read first.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — System design and rationale
- [`docs/BUILD.md`](docs/BUILD.md) — Step-by-step build plan
- [`docs/JOBS.md`](docs/JOBS.md) — Description of each scheduled job

## Companion projects

- **`robertegardner/radio`** — primary FM/AM broadcast receiver on the same Pi
- *(future)* ADS-B Pi anomaly alerting

## License

MIT
