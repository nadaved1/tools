#!/usr/bin/env python3
"""
vcd_activity.py - per-time-unit signal switching activity from a VCD file.

By default the script reports, for every native VCD timestamp (the lines that
start with '#'), how many of the design's signals changed value at that time,
as a percentage of all signals.  Optionally it can instead bucket activity per
clock cycle (--by-clock) and/or render a dark-themed interactive HTML graph
(--html).

The VCD is streamed line by line (never loaded whole), so it scales to very
large traces.  The only state kept in memory is the last value of each signal,
which is bounded by the number of signals in the design - not by the length of
the simulation.

Usage:
    python vcd_activity.py trace.vcd                         # -> trace.activity.csv
    python vcd_activity.py trace.vcd --html                  # + trace.activity.html
    python vcd_activity.py trace.vcd --by-clock --clock clk
"""

import argparse
import os
import sys
import csv
import json
import time
from datetime import datetime
from multiprocessing import Pool


# --------------------------------------------------------------------------- #
# VCD parsing helpers
# --------------------------------------------------------------------------- #
def parse_change(line):
    """Return (identifier, value) for a value-change line, or (None, None).

    Scalar:  '1!'            -> ('!', '1')
    Scalar:  '0"!'           -> ('"!', '0')   (multi-char identifiers exist)
    Vector:  'b1010 "p'      -> ('"p', '1010')
    Real:    'r3.14 xy'      -> ('xy', '3.14')
    """
    c = line[0]
    if c in 'bBrR':
        sp = line.find(' ')
        if sp == -1:
            return None, None
        return line[sp + 1:], line[1:sp]
    if c in '01xXzZ-':
        return line[1:], c
    return None, None


def human_bar(frac, width=30):
    frac = max(0.0, min(1.0, frac))
    filled = int(frac * width)
    return '#' * filled + '-' * (width - filled)


def human_time(seconds):
    if seconds < 60:
        return '%.2fs' % seconds
    m, s = divmod(seconds, 60)
    if m < 60:
        return '%dm %04.1fs' % (m, s)
    h, m = divmod(int(m), 60)
    return '%dh %dm %04.1fs' % (h, m, s)


# --------------------------------------------------------------------------- #
# Parallel native parsing (--ncores)
#
# The file is split into byte ranges that always begin on a '#' timestamp line,
# so every timestamp block lives wholly inside one chunk.  A worker counts the
# changes it can decide on its own - i.e. every change except the FIRST time it
# sees a given signal, whose previous value lives in an earlier chunk.  Those
# first appearances are resolved later by a cheap serial pass that walks the
# chunks in order and carries each signal's last value across the boundaries.
# --------------------------------------------------------------------------- #
def _parse_change_bytes(s):
    """Bytes version of parse_change: returns (identifier, value) or (None, None)."""
    c = s[0:1]
    if c in b'bBrR':
        sp = s.find(b' ')
        if sp == -1:
            return None, None
        return s[sp + 1:], s[1:sp]
    if c in b'01xXzZ-':
        return s[1:], c
    return None, None


def _native_worker(task):
    """Parse one '#'-aligned byte range [start, end).

    Returns (ts_order, counts, first_occ, carry_out):
      ts_order  - timestamps seen, in order (one CSV row each)
      counts    - {timestamp: changes decided within this chunk}
      first_occ - {identifier: (timestamp, value)} first appearance in the chunk
      carry_out - {identifier: last value in the chunk}
    """
    path, start, end = task
    counts = {}
    ts_order = []
    last = {}
    first_occ = {}
    cur = None
    block = set()                                    # distinct signals this block
    pos = start
    with open(path, 'rb') as f:
        f.seek(start)
        while pos < end:
            line = f.readline()
            if not line:
                break
            pos += len(line)
            s = line.strip()
            if not s:
                continue
            c = s[0:1]
            if c == b'#':
                if cur is not None:
                    counts[cur] = len(block)
                cur = int(s[1:])
                ts_order.append(cur)
                block = set()
                continue
            if c == b'$':
                continue
            ident, val = _parse_change_bytes(s)
            if ident is None:
                continue
            prev = last.get(ident)
            if prev is None and ident not in last:
                last[ident] = val
                first_occ[ident] = (cur, val)        # decided in the merge pass
            elif prev != val:
                last[ident] = val
                block.add(ident)                     # set => one count per signal
        if cur is not None:
            counts[cur] = len(block)
    return ts_order, counts, first_occ, last


def _align_to_hash(f, target, skip_partial):
    """Offset of the first line starting with '#' at or after target (or None)."""
    f.seek(target)
    if skip_partial:
        f.readline()                              # drop the partial leading line
    while True:
        pos = f.tell()
        line = f.readline()
        if not line:
            return None
        if line[:1] == b'#':
            return pos


def run_native_parallel(path, body_start, file_size, all_ids,
                        ncores, writer, prog, avg):
    total_signals = len(all_ids)
    sink = RowSink(writer, total_signals, avg, 'native')
    nchunks = ncores * 4                           # finer chunks: load balance + progress

    # Build '#'-aligned chunk boundaries.
    with open(path, 'rb') as f:
        starts = [_align_to_hash(f, 0, False)]     # first chunk starts at '#0'
        span = file_size - body_start
        for i in range(1, nchunks):
            off = _align_to_hash(f, body_start + span * i // nchunks, True)
            if off is not None and off > starts[-1]:
                starts.append(off)
    starts = [s for s in starts if s is not None]
    ends = starts[1:] + [file_size]
    tasks = [(path, st, en) for st, en in zip(starts, ends) if st < en]

    global_state = {}
    ntasks = len(tasks)
    with Pool(ncores) as pool:
        for done, (ts_order, counts, first_occ, carry_out) in enumerate(
                pool.imap(_native_worker, tasks), 1):
            # Resolve each signal's first appearance against the carry-in state.
            for ident, (ts, val) in first_occ.items():
                prev = global_state.get(ident)
                if prev is not None and prev != val:
                    counts[ts] = counts.get(ts, 0) + 1
            global_state.update(carry_out)         # carry-out becomes next carry-in
            for ts in ts_order:
                sink.add_native(ts, counts.get(ts, 0))
            prog.update_frac(done / ntasks, 'rows: %d (chunk %d/%d)'
                             % (sink.rows, done, ntasks))
    sink.flush()
    prog.done('rows: %d' % sink.rows)
    return sink.rows, total_signals


def parse_header(path, prog=None):
    """Read the VCD header.  Returns (all_ids, id_names, timescale, bytes_read,
    file_handle) with the handle positioned right after $enddefinitions."""
    id_names = {}
    all_ids = set()
    timescale = None
    bytes_read = 0
    nvars = 0
    hdr_tick = 0
    in_timescale = False

    f = open(path, 'rb')
    for raw in f:
        bytes_read += len(raw)
        line = raw.decode('ascii', 'replace').strip()
        if not line:
            continue
        if line.startswith('$var'):
            parts = line.split()             # $var <type> <size> <id> <name> ...
            ident, name = parts[3], parts[4]
            all_ids.add(ident)
            id_names.setdefault(ident, name)
            nvars += 1
            if prog is not None and (nvars & 0x3F) == 0:
                prog.marquee('reading header: %d signals (%d vars)'
                             % (len(all_ids), nvars), hdr_tick)
                hdr_tick += 1
        elif line.startswith('$timescale'):
            rest = line[len('$timescale'):].replace('$end', '').strip()
            if rest:
                timescale = rest
            else:
                in_timescale = True
        elif in_timescale:
            if '$end' in line:
                tok = line.replace('$end', '').strip()
                if tok:
                    timescale = tok
                in_timescale = False
            elif line:
                timescale = line
        elif line.startswith('$enddefinitions'):
            break
    if prog is not None:
        prog.marquee_done('reading header: %d signals (%d vars)'
                          % (len(all_ids), nvars))
    return all_ids, id_names, timescale, bytes_read, f


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #
class Progress:
    def __init__(self, total_size, enabled):
        self.total = total_size or 1
        self.enabled = enabled
        self.last = -1
        self.base = 0            # byte offset that counts as 0% for this phase
        self.active = False      # is a progress line currently on screen?

    def update(self, bytes_read, note, force=False):
        span = self.total - self.base
        self.update_frac((bytes_read - self.base) / span if span > 0 else 1.0,
                         note, force)

    def update_frac(self, frac, note, force=False):
        if not self.enabled:
            return
        pct = int(frac * 100)
        if force or pct != self.last:
            sys.stderr.write('\r[%s] %3d%%  %s'
                             % (human_bar(frac), pct, note))
            sys.stderr.flush()
            self.last = pct
            self.active = True

    def new_bar(self, base=0):
        """Finish the current progress line and start a fresh bar on a new one.

        base is the byte offset treated as 0% for the next (byte-driven) phase.
        """
        if self.enabled and self.active:
            sys.stderr.write('\n')
            sys.stderr.flush()
        self.last = -1
        self.base = base
        self.active = False

    def marquee(self, note, tick, width=30, block=6):
        """Indeterminate bar: a block that bounces across the track.

        Used when the total is unknown (header parsing), so a percentage
        would be meaningless.  `tick` advances the block's position.
        """
        if not self.enabled:
            return
        span = max(1, width - block)
        p = tick % (2 * span)
        pos = p if p <= span else 2 * span - p
        bar = '-' * pos + '#' * block + '-' * (width - block - pos)
        sys.stderr.write('\r[%s]  %s' % (bar, note))
        sys.stderr.flush()
        self.active = True

    def marquee_done(self, note, width=30):
        if not self.enabled:
            return
        sys.stderr.write('\r[%s] done  %s' % ('#' * width, note))
        sys.stderr.flush()
        self.active = True

    def done(self, note):
        if self.enabled:
            sys.stderr.write('\r[%s] 100%%  %s\n' % (human_bar(1.0), note))
            self.active = False


# --------------------------------------------------------------------------- #
# Row output, with optional averaging over a window of `avg` buckets.
#
# With avg == 1 the rows are written through unchanged (byte-identical to the
# un-averaged output).  With avg > 1 each group of `avg` buckets is collapsed
# into one row: the value is the mean activity and the time is the LAST bucket's
# timestamp (for the trailing group, whatever buckets remain are averaged).
# --------------------------------------------------------------------------- #
class RowSink:
    def __init__(self, writer, total_signals, avg, kind):
        self.w = writer
        self.total = total_signals
        self.avg = max(1, avg)
        self.kind = kind                       # 'native' | 'clock'
        self.rows = 0
        self._bin = 0
        self._reset()

    def _reset(self):
        self.cnt = 0
        self.sum_n = 0.0
        self.first_start = None
        self.last_x = None                     # last time (native) / last end (clock)

    def add_native(self, time, n):
        if self.avg == 1:
            self.w.writerow([time, n, self.total,
                             '%.4f' % (100.0 * n / self.total)])
            self.rows += 1
            return
        self.sum_n += n
        self.cnt += 1
        self.last_x = time
        if self.cnt >= self.avg:
            self._emit_native()

    def _emit_native(self):
        a = self.sum_n / self.cnt
        self.w.writerow([self.last_x, '%.4f' % a, self.total,
                         '%.4f' % (100.0 * a / self.total)])
        self.rows += 1
        self._reset()

    def add_clock(self, cycle, start, end, n):
        if self.avg == 1:
            self.w.writerow([cycle, start, end, end - start, n, self.total,
                             '%.4f' % (100.0 * n / self.total)])
            self.rows += 1
            return
        if self.first_start is None:
            self.first_start = start
        self.sum_n += n
        self.cnt += 1
        self.last_x = end
        if self.cnt >= self.avg:
            self._emit_clock()

    def _emit_clock(self):
        a = self.sum_n / self.cnt
        self.w.writerow([self._bin, self.first_start, self.last_x,
                         self.last_x - self.first_start, '%.4f' % a, self.total,
                         '%.4f' % (100.0 * a / self.total)])
        self.rows += 1
        self._bin += 1
        self._reset()

    def flush(self):
        if self.avg == 1 or self.cnt == 0:
            return
        self._emit_native() if self.kind == 'native' else self._emit_clock()


# --------------------------------------------------------------------------- #
# Mode 1: native timestamps (default)
# --------------------------------------------------------------------------- #
def run_native(f, bytes_read, total_size, all_ids, writer, prog, avg):
    total_signals = len(all_ids)
    sink = RowSink(writer, total_signals, avg, 'native')
    last_val = {}
    block_time = None
    block_changes = set()

    def flush():
        if block_time is not None:
            sink.add_native(block_time, len(block_changes))

    for raw in f:
        bytes_read += len(raw)
        prog.update(bytes_read, 'rows: %d' % sink.rows)
        line = raw.decode('ascii', 'replace').strip()
        if not line:
            continue
        c = line[0]
        if c == '#':
            flush()
            block_time = int(line[1:])
            block_changes = set()
            continue
        if c == '$':
            continue
        ident, val = parse_change(line)
        if ident is None:
            continue
        prev = last_val.get(ident)
        if prev == val:
            continue                         # redundant re-dump, not a change
        last_val[ident] = val
        if prev is None:
            continue                         # initial value, not a transition
        if block_time is not None:
            block_changes.add(ident)
    flush()
    sink.flush()
    prog.done('rows: %d' % sink.rows)
    return sink.rows, total_signals


# --------------------------------------------------------------------------- #
# Mode 2: per clock cycle (--by-clock)
# --------------------------------------------------------------------------- #
def run_by_clock(f, bytes_read, total_size, all_ids, id_names,
                 clock_name, edge, include_clock, writer, prog, avg):
    clock_id = None
    for ident, name in id_names.items():
        if name == clock_name:
            clock_id = ident
            break
    if clock_id is None:
        names = '\n  '.join(sorted(set(id_names.values())))
        sys.stderr.write("error: clock %r not found. Available signals:\n  %s\n"
                         % (clock_name, names))
        sys.exit(1)

    total_signals = len(all_ids) - (0 if include_clock else 1)
    if total_signals <= 0:
        sys.stderr.write("error: no non-clock signals to measure.\n")
        sys.exit(1)

    sink = RowSink(writer, total_signals, avg, 'clock')
    last_val = {}
    cur_changes = set()
    cur_start = None
    cycle_idx = 0
    last_time = 0
    block_time = None
    block_changes = set()
    block_edge = False

    def write_cycle(start, end):
        nonlocal cycle_idx
        sink.add_clock(cycle_idx, start, end, len(cur_changes))
        cycle_idx += 1

    def flush_block():
        nonlocal cur_start, cur_changes
        if block_time is None:
            return
        if block_edge:
            if cur_start is not None:
                write_cycle(cur_start, block_time)
                cur_changes = set()
            cur_start = block_time
        if cur_start is not None:
            cur_changes |= block_changes

    for raw in f:
        bytes_read += len(raw)
        prog.update(bytes_read, 'cycles: %d' % cycle_idx)
        line = raw.decode('ascii', 'replace').strip()
        if not line:
            continue
        c = line[0]
        if c == '#':
            flush_block()
            block_time = int(line[1:])
            last_time = block_time
            block_changes = set()
            block_edge = False
            continue
        if c == '$':
            continue
        ident, val = parse_change(line)
        if ident is None:
            continue
        prev = last_val.get(ident)
        if prev == val:
            continue
        last_val[ident] = val
        if ident == clock_id:
            if prev is not None:
                if edge == 'rising' and prev == '0' and val == '1':
                    block_edge = True
                elif edge == 'falling' and prev == '1' and val == '0':
                    block_edge = True
                elif edge == 'both':
                    block_edge = True
            if not include_clock:
                continue
        if prev is not None:
            block_changes.add(ident)

    flush_block()
    if cur_start is not None:
        write_cycle(cur_start, last_time)
    sink.flush()
    prog.done('cycles: %d' % cycle_idx)
    return sink.rows, total_signals


# --------------------------------------------------------------------------- #
# HTML rendering
# --------------------------------------------------------------------------- #
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
<style>
  html, body {{ margin: 0; padding: 0; background: #0d1117; color: #c9d1d9;
                font-family: "Segoe UI", system-ui, Arial, sans-serif; }}
  .wrap {{ padding: 18px 20px; }}
  h1 {{ font-size: 17px; font-weight: 600; margin: 0 0 4px; color: #e6edf3; }}
  .sub {{ font-size: 12px; color: #8b949e; margin: 0 0 14px; }}
  #chart {{ width: 100%; height: 78vh; }}
  .footer {{ margin-top: 14px; padding-top: 10px; border-top: 1px solid #21262d;
             font-size: 12px; color: #8b949e; }}
  .footer b {{ color: #c9d1d9; font-weight: 600; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  <p class="sub">{subtitle}</p>
  <div id="chart"></div>
  <div class="footer">Generated: <b>{generated}</b></div>
</div>
<script>
  const x = {x_json};
  const y = {y_json};
  const trace = {{
    x: x, y: y, type: 'scattergl', mode: 'lines',
    line: {{ color: '#58a6ff', width: 1 }},
    fill: 'tozeroy', fillcolor: 'rgba(88,166,255,0.12)',
    hovertemplate: '{xlabel} = %{{x}}<br>%{{y:.2f}} %<extra></extra>'
  }};
  const layout = {{
    paper_bgcolor: '#0d1117', plot_bgcolor: '#0d1117',
    font: {{ color: '#c9d1d9' }},
    margin: {{ l: 64, r: 24, t: 12, b: 56 }},
    xaxis: {{ title: '{xlabel}', gridcolor: '#21262d', zerolinecolor: '#30363d',
              showspikes: true, spikecolor: '#484f58', spikethickness: 1,
              spikemode: 'across', spikedash: 'dot' }},
    yaxis: {{ title: '% of signals changed', gridcolor: '#21262d',
              zerolinecolor: '#30363d', rangemode: 'tozero', ticksuffix: ' %' }}
  }};
  const config = {{
    responsive: true, scrollZoom: true, displaylogo: false,
    modeBarButtonsToRemove: ['select2d', 'lasso2d'],
    toImageButtonOptions: {{ format: 'png', filename: 'vcd_activity',
                             scale: 2 }}
  }};
  Plotly.newPlot('chart', [trace], layout, config);
</script>
</body>
</html>
"""


def render_html(csv_path, html_path, title, subtitle, xlabel, generated):
    xs, ys = [], []
    with open(csv_path, newline='') as fh:
        r = csv.DictReader(fh)
        xcol = 'time' if 'time' in r.fieldnames else 'start_time'
        for row in r:
            xs.append(int(row[xcol]))
            ys.append(float(row['percent_changed']))
    html = HTML_TEMPLATE.format(
        title=title, subtitle=subtitle, xlabel=xlabel, generated=generated,
        x_json=json.dumps(xs), y_json=json.dumps(ys))
    with open(html_path, 'w', encoding='utf-8') as out:
        out.write(html)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Per-time-unit (or per-clock) signal switching activity "
                    "from a VCD file.")
    ap.add_argument('vcd', help='input VCD file')
    ap.add_argument('-o', '--output',
                    help='output CSV (default: <input>.activity.csv)')
    ap.add_argument('--html', nargs='?', const='__AUTO__', default=None,
                    metavar='PATH',
                    help='also render a dark-themed interactive HTML graph '
                         '(optional output path)')
    ap.add_argument('--by-clock', action='store_true',
                    help='bucket per clock cycle instead of per native timestamp')
    ap.add_argument('--clock', default='clk',
                    help='clock signal name for --by-clock (default: clk)')
    ap.add_argument('--edge', choices=['rising', 'falling', 'both'],
                    default='rising',
                    help='clock edge delimiting a cycle (default: rising)')
    ap.add_argument('--include-clock', action='store_true',
                    help='count the clock among the signals (--by-clock)')
    ap.add_argument('--avg', type=int, default=1, metavar='N',
                    help='average over every N buckets (timestamps, or cycles '
                         'with --by-clock); the row time is the last bucket in '
                         'the group (default: 1 = no averaging)')
    ap.add_argument('--ncores', type=int, default=1, metavar='N',
                    help='parse on N worker processes (native mode only); '
                         '0 = all available cores')
    ap.add_argument('--no-progress', action='store_true',
                    help='disable the progress indicator')
    args = ap.parse_args()

    if args.ncores == 0:
        args.ncores = os.cpu_count() or 1
    if args.avg < 1:
        args.avg = 1

    t0 = time.perf_counter()
    total_size = os.path.getsize(args.vcd) or 1
    base = os.path.splitext(args.vcd)[0]
    out_path = args.output or (base + '.activity.csv')

    prog = Progress(total_size, not args.no_progress)
    all_ids, id_names, timescale, bytes_read, f = parse_header(args.vcd, prog)
    unit = timescale or 'time units'

    out = open(out_path, 'w', newline='')
    writer = csv.writer(out)

    prog.new_bar(bytes_read)        # header done -> body gets its own bar/line

    # When averaging, the change-count column holds a (fractional) mean.
    ncol = 'avg_signals_changed' if args.avg > 1 else 'signals_changed'

    if args.by_clock:
        if args.ncores > 1:
            sys.stderr.write('note: --ncores applies to native mode only; '
                             'running --by-clock serially.\n')
        writer.writerow(['bin' if args.avg > 1 else 'cycle',
                         'start_time', 'end_time', 'duration',
                         ncol, 'total_signals', 'percent_changed'])
        rows, total_signals = run_by_clock(
            f, bytes_read, total_size, all_ids, id_names,
            args.clock, args.edge, args.include_clock, writer, prog, args.avg)
        xlabel = 'time (%s) - cycle start' % unit
        if args.avg > 1:
            subtitle = ('%d signals (clock %r excluded) | %d points '
                        '(mean of %d %s-edge cycles each)'
                        % (total_signals, args.clock, rows, args.avg, args.edge))
        else:
            subtitle = ('%d signals (clock %r excluded) | %d %s-edge cycles'
                        % (total_signals, args.clock, rows, args.edge))
    else:
        writer.writerow(['time', ncol, 'total_signals', 'percent_changed'])
        if args.ncores > 1:
            f.close()
            rows, total_signals = run_native_parallel(
                args.vcd, bytes_read, total_size, all_ids,
                args.ncores, writer, prog, args.avg)
        else:
            rows, total_signals = run_native(
                f, bytes_read, total_size, all_ids, writer, prog, args.avg)
        xlabel = 'time (%s)' % unit
        if args.avg > 1:
            subtitle = ('%d signals | %d points (mean of %d timestamps each)'
                        % (total_signals, rows, args.avg))
        else:
            subtitle = ('%d signals | %d native timestamps'
                        % (total_signals, rows))

    if not f.closed:
        f.close()
    out.close()

    generated = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')

    if args.html is not None:
        html_path = (base + '.activity.html') if args.html == '__AUTO__' \
            else args.html
        title = 'VCD switching activity - %s' % os.path.basename(args.vcd)
        render_html(out_path, html_path, title, subtitle, xlabel, generated)
        sys.stderr.write('html: %s\n' % html_path)

    sys.stderr.write('done: signals=%d  rows=%d  cores=%d  time=%s  -> %s\n'
                     % (total_signals, rows, args.ncores,
                        human_time(time.perf_counter() - t0), out_path))


if __name__ == '__main__':
    main()
