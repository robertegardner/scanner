#!/usr/bin/env python3
"""Generate SDRTrunk talkgroup-label aliases from moswin_talkgroups.tsv.

Rewrites the alias block of a v4 SDRTrunk playlist, between the marker comments
  <!-- TALKGROUPS:BEGIN ... -->  ...  <!-- TALKGROUPS:END -->
so the call log / stream show talkgroup names instead of bare numbers.

The block always contains a catch-all "Cape County All" alias (so unlabeled
talkgroups still record + stream + show as their number), followed by one named
alias per row in the TSV. Each alias carries the broadcastChannel + record ids so
labeled talkgroups keep streaming to Icecast and recording per-call audio.

Usage:
  python3 gen_aliases.py <playlist.xml> [moswin_talkgroups.tsv]

If the markers are absent they're inserted right after <playlist ...>. Any
pre-existing <alias>…</alias> elements outside the markers are removed (the TSV
is the single source of truth for aliases). Channel/stream are left untouched.
"""
import os
import re
import sys
from xml.sax.saxutils import quoteattr

BEGIN = '<!-- TALKGROUPS:BEGIN (generated from p25/moswin_talkgroups.tsv by gen_aliases.py — edit the TSV, not here) -->'
END = '<!-- TALKGROUPS:END -->'
LIST = "MOSWIN"
STREAM = "MOSWIN Live"  # broadcastChannel name; must match the <stream> name

# ARGB ints SDRTrunk uses for alias color, by group.
COLORS = {
    "fire": -65536,       # red
    "ems": -23296,        # orange
    "police": -16776961,  # blue
    "interop": -16711936, # green
    "local": -6710887,    # grey
}
DEFAULT_COLOR = -1        # white


def alias(name, color, ids):
    lines = [f'  <alias color="{color}" group="" name={quoteattr(name)} list="{LIST}">']
    lines += [f'    {i}' for i in ids]
    lines.append('  </alias>')
    return "\n".join(lines)


def build_block(tsv_path):
    out = [BEGIN]
    # Catch-all fallback: unlabeled talkgroups still record + stream, shown as #.
    out.append(alias("Cape County All", -16711936, [
        '<id type="talkgroupRange" protocol="APCO25" min="0" max="65535"/>',
        f'<id type="broadcastChannel" channel="{STREAM}"/>',
        '<id type="record"/>',
    ]))
    n = 0
    with open(tsv_path) as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 2 or not parts[0].strip().isdigit():
                continue
            tgid = parts[0].strip()
            label = parts[1].strip()
            group = parts[2].strip().lower() if len(parts) > 2 else ""
            color = COLORS.get(group, DEFAULT_COLOR)
            out.append(alias(label, color, [
                f'<id type="talkgroup" value="{tgid}" protocol="APCO25"/>',
                f'<id type="broadcastChannel" channel="{STREAM}"/>',
                '<id type="record"/>',
            ]))
            n += 1
    out.append(END)
    return "\n".join(out), n


def main():
    if len(sys.argv) < 2:
        sys.exit(f"usage: {sys.argv[0]} <playlist.xml> [moswin_talkgroups.tsv]")
    playlist = sys.argv[1]
    tsv = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "moswin_talkgroups.tsv")

    block, n = build_block(tsv)
    s = open(playlist).read()

    if BEGIN in s and END in s:
        s = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END), block, s, flags=re.DOTALL)
    else:
        # Drop any hand-written aliases, then insert the block after <playlist>.
        s = re.sub(r"[ \t]*<alias\b.*?</alias>\s*", "", s, flags=re.DOTALL)
        s = re.sub(r"(<playlist[^>]*>\s*)", r"\1" + block + "\n", s, count=1)

    tmp = playlist + ".tmp"
    with open(tmp, "w") as f:
        f.write(s)
    os.replace(tmp, playlist)
    print(f"wrote {n} talkgroup aliases (+ catch-all) into {playlist}")


if __name__ == "__main__":
    main()
