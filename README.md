# tools

## vcd_activity.py

Per-timestamp signal switching activity (how many signals changed, as a % of
all signals) from a VCD or FSDB, with an optional interactive HTML graph.
Python 3, standard library only.

```bash
python vcd_activity.py trace.vcd                   # -> trace.activity.csv
python vcd_activity.py trace.vcd --html            # + trace.activity.html
python vcd_activity.py trace.vcd --html out.html   # custom HTML path
python vcd_activity.py trace.vcd --scope top.core  # one sub-tree
python vcd_activity.py trace.vcd --by-clock --clock clk --edge rising
python vcd_activity.py trace.fsdb --html           # FSDB, see below
```

Common options:

| Option | Meaning |
| --- | --- |
| `-o FILE` | output CSV (default `<input>.activity.csv`) |
| `--html [PATH]` | also write the HTML graph |
| `--scope PATH` | restrict to a dotted scope, e.g. `top.core.alu` |
| `--by-clock` | one bucket per clock cycle (`--clock`, `--edge`, `--include-clock`) |
| `--avg N` | average every N buckets |
| `--no-xz` | ignore transitions into/out of x/z |
| `--max-points N` | cap points embedded in the HTML (default 100000, 0 = all) |
| `--no-hierarchy`, `--no-similarity` | drop those HTML sections to shrink the file |
| `--ncores N` | parse on N processes (0 = all cores) |

`python vcd_activity.py -h` lists everything.

## fsdb_activity (compiled helper)

An FSDB is read directly through the Synopsys NPI API, so no
`fsdb2vcd` conversion is needed. `vcd_activity.py` calls the helper automatically for
`.fsdb` input.

**Build** (once; needs a Verdi install and a C++11 compiler):

```bash
cd fsdb_activity
make VERDI_HOME=$VERDI_HOME
# or, with the FSDB+ SDK:  make FSDB_PLUS_HOME=/path/to/Y-2026.03
```

Running needs a Verdi license (or FSDB+DevKit) reachable through
`LM_LICENSE_FILE` / `SNPSLMD_LICENSE_FILE`.

**Use through the script** (the usual way):

```bash
python vcd_activity.py run.fsdb --html
python vcd_activity.py run.fsdb --html --scope top.core --ncores 4
```

The script finds the helper via `--fsdb-reader PATH`, `$FSDB_ACTIVITY`,
`fsdb_activity/fsdb_activity` next to the script, or `$PATH`.

`--ncores N` starts N NPI processes (capped to the cores the job may use); each
checks out its own Verdi license and opens the FSDB itself, so keep N within
the free licenses. Memory needs no option: the script detects what the job may
use (cgroup limit, `RLIMIT_AS`, `MemAvailable`) and splits it across workers;
a worker nearing its share stops and a fresh process continues from there.

Not supported for FSDB yet: `--by-clock`, and the hierarchy / similarity
overlays in the HTML.

**Use standalone:**

```bash
fsdb_activity/fsdb_activity run.fsdb [--scope a.b.c] [--no-xz] [--batch N] [--part K/N]
                            [--start-block B] [--max-rss MB] [--no-progress]
```

Output on stdout, progress on stderr:

```
# fsdb_activity 2
timescale 1ps
open_rss 96           # MB resident right after opening
signals 200000
<time> <count>        # ascending, only timestamps with changes
resume 1234           # only when --max-rss stopped it early
end
```

Exit codes: 2 usage, 3 open / license failure, 4 no signals (e.g. `--scope`
matched nothing). More detail in [fsdb_activity/README.md](fsdb_activity/README.md).
