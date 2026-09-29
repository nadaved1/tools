# fsdb_activity

Reads an FSDB directly through the Synopsys NPI reader API and emits, per
timestamp, how many signals changed value. `vcd_activity.py` uses it
automatically for `.fsdb` input, so a full-chip FSDB never has to be converted
to a (multi-GB, often unconvertible) VCD first.

## Build

Against a Verdi install (the usual case):

```bash
cd fsdb_activity
make VERDI_HOME=$VERDI_HOME
```

Against the FSDB+ development kit instead: `make FSDB_PLUS_HOME=/path/to/Y-2026.03`.

Running needs a Verdi license (or `FSDB+DevKit` for the SDK build) reachable
through `LM_LICENSE_FILE` / `SNPSLMD_LICENSE_FILE`.

## Use

```bash
vcd_activity.py run.fsdb --html                     # whole design
vcd_activity.py run.fsdb --html --scope top.core    # one sub-tree
```

`vcd_activity.py` finds the helper at `--fsdb-reader PATH`, `$FSDB_ACTIVITY`,
`fsdb_activity/fsdb_activity` next to the script, or on `$PATH`.

Supported with FSDB input: native per-timestamp mode, `--scope`, `--no-xz`,
`--avg`, `--html`, `--max-points`, `--ncores`. Not yet: `--by-clock`, and the
hierarchy heatmap / similarity overlay in the HTML.

`--ncores N` runs N `fsdb_activity --part k/N` processes, each in its own
directory. Every worker walks the scope tree but reads only the signals of
every N-th block of 16 scopes, and the per-timestamp counts are summed. NPI
decoding is single-threaded, so this is where the speed comes from, but **each
worker checks out its own Verdi license and opens the FSDB itself** (about
25 GB and a minute per open on a 557M-signal chip with Verdi 2017), so size N
to the free licenses. `--ncores` is capped to the cores the job may use (CPU
affinity and cgroup quota).

## Memory

NPI frees nothing a process allocated for the signals it has read - neither
the signal handles nor the value changes, even after they are unloaded - until
that process closes the FSDB. A worker therefore only grows, by roughly the
signals it has read times a per-signal cost, and only exiting hands it back.

`vcd_activity.py` handles this without any option: it detects the memory the
job may use (the tightest of the cgroup limit, `RLIMIT_AS` and `MemAvailable`,
less 20%) and gives each concurrent worker an equal share as `--max-rss`. A
worker that would pass its ceiling stops at the next block of 16 scopes,
reports the counts it has, and its share continues in a fresh process from
that block (`--start-block`). Designs that fit never restart. If opening the
FSDB alone takes most of a worker's ceiling, fewer workers run at a time; if
it does not fit even alone, the run stops and says how much memory it needs.

## Semantics

Counting matches `vcd_activity.py` on the VCD that `fsdb2vcd` would produce
from the same FSDB (verified on Verdi U-2023.03):

- a signal's first value is its initial value, not a change;
- a signal counts at most once per timestamp;
- `--no-xz` ignores values containing x/z and keeps the last known value.

Two differences from a VCD dumped directly by the simulator, both because the
FSDB does not store the information:

- a glitch that returns to the old value within one timestamp is not counted;
- timestamps at which nothing changed have no row (they would be 0).

## Standalone output

```
# fsdb_activity 2
timescale 1ps
open_rss 96           # MB resident right after opening the FSDB
signals 200000
<time> <count>        # ascending
resume 1234           # only when --max-rss stopped it early
end
```

Options for use on its own: `--scope a.b.c`, `--no-xz`, `--part K/N`,
`--start-block B`, `--max-rss MB`, `--batch N`, `--no-progress`.

Progress and NPI banners go to stderr. Exit codes: 2 usage, 3 open / license
failure, 4 no signals (e.g. `--scope` matched nothing).
