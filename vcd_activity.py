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
from datetime import datetime


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


def parse_header(path):
    """Read the VCD header.  Returns (all_ids, id_names, timescale, bytes_read,
    file_handle) with the handle positioned right after $enddefinitions."""
    id_names = {}
    all_ids = set()
    timescale = None
    bytes_read = 0
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
    return all_ids, id_names, timescale, bytes_read, f


# --------------------------------------------------------------------------- #
# Progress
# --------------------------------------------------------------------------- #
class Progress:
    def __init__(self, total_size, enabled):
        self.total = total_size or 1
        self.enabled = enabled
        self.last = -1

    def update(self, bytes_read, note):
        if not self.enabled:
            return
        pct = int(bytes_read * 100 / self.total)
        if pct != self.last:
            sys.stderr.write('\r[%s] %3d%%  %s'
                             % (human_bar(pct / 100.0), pct, note))
            sys.stderr.flush()
            self.last = pct

    def done(self, note):
        if self.enabled:
            sys.stderr.write('\r[%s] 100%%  %s\n' % (human_bar(1.0), note))


# --------------------------------------------------------------------------- #
# Mode 1: native timestamps (default)
# --------------------------------------------------------------------------- #
def run_native(f, bytes_read, total_size, all_ids, writer, prog):
    total_signals = len(all_ids)
    last_val = {}
    block_time = None
    block_changes = set()
    rows = 0

    def flush():
        nonlocal rows
        if block_time is None:
            return
        n = len(block_changes)
        pct = 100.0 * n / total_signals
        writer.writerow([block_time, n, total_signals, '%.4f' % pct])
        rows += 1

    for raw in f:
        bytes_read += len(raw)
        prog.update(bytes_read, 'rows: %d' % rows)
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
    prog.done('rows: %d' % rows)
    return rows, total_signals


# --------------------------------------------------------------------------- #
# Mode 2: per clock cycle (--by-clock)
# --------------------------------------------------------------------------- #
def run_by_clock(f, bytes_read, total_size, all_ids, id_names,
                 clock_name, edge, include_clock, writer, prog):
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
        n = len(cur_changes)
        pct = 100.0 * n / total_signals
        writer.writerow([cycle_idx, start, end, end - start,
                         n, total_signals, '%.4f' % pct])
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
    prog.done('cycles: %d' % cycle_idx)
    return cycle_idx, total_signals


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
    ap.add_argument('--no-progress', action='store_true',
                    help='disable the progress indicator')
    args = ap.parse_args()

    total_size = os.path.getsize(args.vcd) or 1
    base = os.path.splitext(args.vcd)[0]
    out_path = args.output or (base + '.activity.csv')

    all_ids, id_names, timescale, bytes_read, f = parse_header(args.vcd)
    prog = Progress(total_size, not args.no_progress)
    unit = timescale or 'time units'

    out = open(out_path, 'w', newline='')
    writer = csv.writer(out)

    if args.by_clock:
        writer.writerow(['cycle', 'start_time', 'end_time', 'duration',
                         'signals_changed', 'total_signals', 'percent_changed'])
        rows, total_signals = run_by_clock(
            f, bytes_read, total_size, all_ids, id_names,
            args.clock, args.edge, args.include_clock, writer, prog)
        xlabel = 'time (%s) - cycle start' % unit
        subtitle = ('%d signals (clock %r excluded) | %d %s-edge cycles'
                    % (total_signals, args.clock, rows, args.edge))
    else:
        writer.writerow(['time', 'signals_changed',
                         'total_signals', 'percent_changed'])
        rows, total_signals = run_native(
            f, bytes_read, total_size, all_ids, writer, prog)
        xlabel = 'time (%s)' % unit
        subtitle = '%d signals | %d native timestamps' % (total_signals, rows)

    f.close()
    out.close()

    generated = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')

    if args.html is not None:
        html_path = (base + '.activity.html') if args.html == '__AUTO__' \
            else args.html
        title = 'VCD switching activity - %s' % os.path.basename(args.vcd)
        render_html(out_path, html_path, title, subtitle, xlabel, generated)
        sys.stderr.write('html: %s\n' % html_path)

    sys.stderr.write('done: signals=%d  rows=%d  -> %s\n'
                     % (total_signals, rows, out_path))


if __name__ == '__main__':
    main()
