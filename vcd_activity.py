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
import gzip
import os
import sys
import csv
import json
import time
import html
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


# --------------------------------------------------------------------------- #
# Input helpers: transparent gzip + x/z filtering
# --------------------------------------------------------------------------- #
def _is_gzip(path):
    """True if path is gzip-compressed (sniffed by magic bytes, not extension)."""
    try:
        with open(path, 'rb') as fh:
            return fh.read(2) == b'\x1f\x8b'
    except OSError:
        return False


def _open(path):
    """Open a VCD for *sequential* reading, decompressing gzip transparently.
    Yields raw bytes lines.  Note: the returned object is not seekable when the
    input is gzip, so callers that need random access must guard on _is_gzip."""
    return gzip.open(path, 'rb') if _is_gzip(path) else open(path, 'rb')


def _gzip_isize(path):
    """Uncompressed size from the gzip footer (ISIZE, modulo 2**32).  Used only
    as a progress denominator: exact for streams < 4 GiB, approximate beyond."""
    try:
        with open(path, 'rb') as fh:
            fh.seek(-4, os.SEEK_END)
            return int.from_bytes(fh.read(4), 'little') or 1
    except OSError:
        return 1


def _has_xz(val):                                    # str value (serial parsing)
    return 'x' in val or 'X' in val or 'z' in val or 'Z' in val


def _has_xz_b(val):                                  # bytes value (worker parsing)
    return b'x' in val or b'X' in val or b'z' in val or b'Z' in val


def human_bar(frac, width=30):
    frac = max(0.0, min(1.0, frac))
    filled = int(frac * width)
    return '#' * filled + '-' * (width - filled)


def human_size(nbytes):
    n = float(nbytes)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if n < 1024 or unit == 'TiB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.2f %s' % (n, unit))
        n /= 1024


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
    path, start, end, no_xz = task
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
            if no_xz and _has_xz_b(val):             # ignore x/z, keep last known
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
                        ncores, writer, prog, avg, no_xz):
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
    tasks = [(path, st, en, no_xz) for st, en in zip(starts, ends) if st < en]

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
    """Read the VCD header.  Returns (all_ids, id_names, meta, hier, bytes_read,
    file_handle) with the handle positioned right after $enddefinitions.
    meta has 'date', 'version' and 'timescale' (each '' if absent).
    hier has 'modules' (list of scope-path strings) and 'id2mod'
    (identifier -> index into modules), built from $scope/$upscope."""
    id_names = {}
    all_ids = set()
    meta = {'date': '', 'version': '', 'timescale': ''}
    modules = []
    mod_index = {}                            # scope path -> index in modules
    id2mod = {}                               # identifier -> module index
    scope = []                                # current $scope stack
    bytes_read = 0
    nvars = 0
    hdr_tick = 0
    pending = None                            # multi-line field being collected

    f = _open(path)
    for raw in f:
        bytes_read += len(raw)
        line = raw.decode('ascii', 'replace').strip()
        if not line:
            continue
        if pending is not None:               # inside $date/$version/$timescale
            if '$end' in line:
                tok = line.replace('$end', '').strip()
                if tok:
                    meta[pending] = (meta[pending] + ' ' + tok).strip()
                pending = None
            else:
                meta[pending] = (meta[pending] + ' ' + line).strip()
            continue
        if line.startswith('$var'):
            parts = line.split()             # $var <type> <size> <id> <name> ...
            ident, name = parts[3], parts[4]
            all_ids.add(ident)
            id_names.setdefault(ident, name)
            if ident not in id2mod:
                path_str = '.'.join(scope) if scope else '(top)'
                mi = mod_index.get(path_str)
                if mi is None:
                    mi = len(modules)
                    mod_index[path_str] = mi
                    modules.append(path_str)
                id2mod[ident] = mi
            nvars += 1
            if prog is not None and (nvars & 0x3F) == 0:
                prog.marquee('reading header: %d signals (%d vars)'
                             % (len(all_ids), nvars), hdr_tick)
                hdr_tick += 1
        elif line.startswith('$scope'):
            parts = line.split()             # $scope <type> <name> $end
            if len(parts) >= 3:
                scope.append(parts[2])
        elif line.startswith('$upscope'):
            if scope:
                scope.pop()
        elif (line.startswith('$timescale') or line.startswith('$date')
              or line.startswith('$version')):
            kw = line.split(None, 1)[0]       # $timescale | $date | $version
            name = kw[1:]
            rest = line[len(kw):]
            if '$end' in rest:
                meta[name] = rest.replace('$end', '').strip()
            else:
                meta[name] = rest.strip()
                pending = name
        elif line.startswith('$enddefinitions'):
            break
    if prog is not None:
        prog.marquee_done('reading header: %d signals (%d vars)'
                          % (len(all_ids), nvars))
    hier = {'modules': modules, 'id2mod': id2mod}
    return all_ids, id_names, meta, hier, bytes_read, f


# --------------------------------------------------------------------------- #
# Region analysis: which logic toggles, and how the active set shifts in time.
#
# Time is split into NBINS equal columns over [t_min, t_max].  We accumulate a
# matrix M[bin][module] = number of toggles by signals of that module in that
# time column.  From it we derive both views:
#   * the heatmap (modules x time)               -> WHICH logic toggles
#   * per-bin active-module set + Jaccard         -> SAME vs DIFFERENT regions
# Boundary effect: a signal's first appearance inside a parse chunk is skipped
# (its previous value lives in an earlier chunk); that is at most one toggle per
# signal per chunk - negligible for a coarse heatmap.
# --------------------------------------------------------------------------- #
def time_range(path, body_start, file_size):
    """Cheaply find (t_min, t_max) without a full pass (head read + tail read)."""
    tmin = tmax = None
    with open(path, 'rb') as f:
        f.seek(body_start)
        for _ in range(200000):
            line = f.readline()
            if not line:
                break
            if line[:1] == b'#' and line[1:2].isdigit():
                tmin = int(line.strip()[1:])
                break
        tail = min(file_size, 1 << 20)
        f.seek(file_size - tail)
        for ln in reversed(f.read().split(b'\n')):
            s = ln.strip()
            if s[:1] == b'#' and s[1:2].isdigit():
                tmax = int(s[1:])
                break
    return tmin, tmax


_HM = {}


def _hm_init(id2mod, nbins, tmin, tspan, nmods, noxz):
    _HM.update(id2mod=id2mod, nbins=nbins, tmin=tmin, tspan=tspan, nmods=nmods,
               noxz=noxz)


def _module_worker(task):
    """Accumulate {bin: [per-module toggle counts]} for one '#'-aligned range."""
    path, start, end = task
    id2mod = _HM['id2mod']
    nbins, tmin, tspan, nmods = (_HM['nbins'], _HM['tmin'],
                                 _HM['tspan'], _HM['nmods'])
    noxz = _HM['noxz']
    counts = {}
    last = {}
    cur_bin = 0
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
                t = int(s[1:])
                b = (t - tmin) * nbins // tspan
                cur_bin = 0 if b < 0 else (nbins - 1 if b >= nbins else b)
                continue
            if c == b'$':
                continue
            ident, val = _parse_change_bytes(s)
            if ident is None:
                continue
            if noxz and _has_xz_b(val):              # ignore x/z, keep last known
                continue
            prev = last.get(ident)
            if prev == val:
                continue
            first = ident not in last
            last[ident] = val
            if first:
                continue                          # boundary: prev lives elsewhere
            m = id2mod.get(ident)                  # id2mod is keyed by bytes
            if m is None:
                continue
            row = counts.get(cur_bin)
            if row is None:
                row = counts[cur_bin] = [0] * nmods
            row[m] += 1
    return counts


def collect_module_activity(path, body_start, file_size, hier, nbins,
                            tmin, tmax, ncores, prog, no_xz):
    """Build M[bin][module] = toggle count, used to derive set-similarity."""
    # key the map by bytes so workers skip per-toggle decoding
    id2mod = {k.encode('ascii', 'replace'): v
              for k, v in hier['id2mod'].items()}
    nmods = len(hier['modules'])
    tspan = max(1, tmax - tmin + 1)
    nchunks = max(1, ncores * 4)
    with open(path, 'rb') as f:
        starts = [_align_to_hash(f, 0, False)]
        span = file_size - body_start
        for i in range(1, nchunks):
            off = _align_to_hash(f, body_start + span * i // nchunks, True)
            if off is not None and off > starts[-1]:
                starts.append(off)
    starts = [s for s in starts if s is not None]
    ends = starts[1:] + [file_size]
    tasks = [(path, st, en) for st, en in zip(starts, ends) if st < en]

    M = [[0] * nmods for _ in range(nbins)]
    ntasks = len(tasks)
    with Pool(ncores, initializer=_hm_init,
              initargs=(id2mod, nbins, tmin, tspan, nmods, no_xz)) as pool:
        for done, counts in enumerate(
                pool.imap_unordered(_module_worker, tasks), 1):
            for b, arr in counts.items():
                row = M[b]
                for m in range(nmods):
                    if arr[m]:
                        row[m] += arr[m]
            prog.update_frac(done / ntasks, 'regions: chunk %d/%d' % (done, ntasks))
    prog.done('regions: %d modules x %d time bins' % (nmods, nbins))
    return M


def similarity_regimes(M, nbins, threshold):
    """Per-bin set-similarity to the previous bin, and a regime id that bumps
    whenever similarity drops below threshold (i.e. the active set changed)."""
    active = [frozenset(m for m, v in enumerate(M[b]) if v) for b in range(nbins)]
    sim = [1.0] * nbins
    regime = [0] * nbins
    rid = 0
    for b in range(1, nbins):
        a, c = active[b - 1], active[b]
        uni = len(a | c)
        j = (len(a & c) / uni) if uni else 1.0
        sim[b] = j
        if j < threshold:
            rid += 1
        regime[b] = rid
    if nbins > 1:
        sim[0] = sim[1]
    return sim, regime


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
def run_native(f, bytes_read, total_size, all_ids, writer, prog, avg, no_xz):
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
        if no_xz and _has_xz(val):           # ignore x/z, keep last known value
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
                 clock_name, edge, include_clock, writer, prog, avg, no_xz):
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
        if no_xz and _has_xz(val):           # ignore x/z, keep last known value
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
  .sub {{ font-size: 12px; color: #8b949e; margin: 0 0 12px; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 10px 26px; background: #161b22;
           border: 1px solid #21262d; border-radius: 8px; padding: 11px 16px;
           margin: 0 0 16px; font-size: 12px; }}
  .meta .k {{ color: #8b949e; margin-right: 7px;
              text-transform: uppercase; letter-spacing: .04em; font-size: 11px; }}
  .meta .v {{ color: #e6edf3; font-weight: 600; }}
  #chart {{ width: 100%; height: 74vh; }}
  h2 {{ font-size: 14px; font-weight: 600; margin: 24px 0 10px; color: #e6edf3; }}
  h2 .hint {{ font-size: 11px; font-weight: 400; color: #8b949e; }}
  .tree {{ font-size: 12px; }}
  details.mod {{ border: 1px solid #30363d; border-radius: 6px;
                 margin: 5px 0; background: #161b22; }}
  details.mod > summary {{ cursor: pointer; padding: 6px 10px; list-style: none;
                           display: flex; align-items: center; gap: 9px;
                           user-select: none; }}
  details.mod > summary::-webkit-details-marker {{ display: none; }}
  details.mod > summary::before {{ content: "\\25B8"; color: #8b949e;
                                   font-size: 10px; transition: transform .12s; }}
  details.mod[open] > summary::before {{ transform: rotate(90deg); }}
  .kids {{ padding: 2px 10px 8px 24px; }}
  .mod.leaf {{ border: 1px dashed #30363d; border-radius: 6px; margin: 4px 0;
               padding: 5px 10px 5px 28px; background: #0d1117;
               display: flex; align-items: center; gap: 9px; }}
  .nm {{ color: inherit; font-weight: 600;
         font-family: ui-monospace, "Cascadia Code", Consolas, monospace; }}
  .cnt {{ color: inherit; background: rgba(127,127,127,0.22);
          border-radius: 10px; padding: 1px 8px; font-size: 11px; }}
  .agg {{ color: inherit; opacity: .72; font-size: 11px; }}
  .legend {{ display: inline-flex; align-items: center; gap: 6px;
             margin-left: 10px; font-weight: 400; vertical-align: middle; }}
  .legend .bar {{ width: 84px; height: 10px; border-radius: 3px;
                  border: 1px solid #30363d;
                  background: linear-gradient(90deg, #ffffff, #ff0000); }}
  .footer {{ margin-top: 14px; padding-top: 10px; border-top: 1px solid #21262d;
             font-size: 12px; color: #8b949e; }}
  .footer b {{ color: #c9d1d9; font-weight: 600; }}
</style>
</head>
<body>
<div class="wrap">
  <h1>{title}</h1>
  <p class="sub">{subtitle}</p>
  <div class="meta">
    <div><span class="k">Dumped by</span><span class="v">{m_tool}</span></div>
    <div><span class="k">Dump date</span><span class="v">{m_date}</span></div>
    <div><span class="k">Timescale</span><span class="v">{m_scale}</span></div>
    <div><span class="k">VCD size</span><span class="v">{m_size}</span></div>
  </div>
  <div id="chart"></div>
  {hierarchy}
  <div class="footer">Generated: <b>{generated}</b></div>
</div>
<script>
{script}
</script>
</body>
</html>
"""

# palette cycled across regimes (background bands)
_REGIME_COLORS = ['#1f6feb', '#8957e5', '#238636', '#9e6a03',
                  '#bc4c00', '#1b7c83', '#a371f7', '#57606a']


def _decimate(xs, ys, max_points):
    """Down-sample (xs, ys) for display, keeping the min AND max of each bucket.

    Inlining millions of points makes the HTML huge and the browser choke.
    Bucketing into ~max_points/2 ranges and emitting each bucket's lowest and
    highest sample (in time order) preserves the visual envelope - activity
    spikes are never dropped - while bounding the embedded data.
    """
    n = len(xs)
    if max_points <= 0 or n <= max_points:
        return xs, ys, n
    nb = max(1, max_points // 2)
    ox, oy = [], []
    for b in range(nb):
        lo = (b * n) // nb
        hi = ((b + 1) * n) // nb
        if hi <= lo:
            continue
        imin = imax = lo
        for i in range(lo + 1, hi):
            if ys[i] < ys[imin]:
                imin = i
            elif ys[i] > ys[imax]:
                imax = i
        a, c = (imin, imax) if imin <= imax else (imax, imin)
        ox.append(xs[a]); oy.append(ys[a])
        if c != a:
            ox.append(xs[c]); oy.append(ys[c])
    return ox, oy, n


def _regime_bands(bin_x, regime, bin_w):
    """Contiguous runs of equal regime id -> background rectangle shapes."""
    shapes = []
    if not regime:
        return shapes
    b0 = 0
    for b in range(1, len(regime) + 1):
        if b == len(regime) or regime[b] != regime[b0]:
            col = _REGIME_COLORS[regime[b0] % len(_REGIME_COLORS)]
            shapes.append({
                'type': 'rect', 'xref': 'x', 'yref': 'paper', 'layer': 'below',
                'x0': bin_x[b0] - bin_w / 2.0, 'x1': bin_x[b - 1] + bin_w / 2.0,
                'y0': 0, 'y1': 1, 'line': {'width': 0},
                'fillcolor': col, 'opacity': 0.13})
            b0 = b
    return shapes


# --------------------------------------------------------------------------- #
# Scope hierarchy -> nested "block diagram"
#
# parse_header already flattened $scope/$upscope into hier['modules'] (dotted
# scope paths, in first-appearance order) and hier['id2mod'] (identifier ->
# module index).  Here we fold the dotted paths back into a tree, tag each node
# with the number of signals declared directly in that scope, and emit nested,
# collapsible boxes - the containment view of the design that the scope section
# encodes losslessly.
# --------------------------------------------------------------------------- #
def _build_module_tree(hier):
    modules = hier.get('modules', [])
    id2mod = hier.get('id2mod', {})
    direct = [0] * len(modules)                    # signals declared in each scope
    for mi in id2mod.values():
        if 0 <= mi < len(direct):
            direct[mi] += 1
    root = {'name': '', 'children': {}, 'direct': 0, 'idx': None}
    for idx, path in enumerate(modules):
        parts = [path] if path == '(top)' else path.split('.')
        node = root
        for p in parts:                            # create intermediate scopes too
            node = node['children'].setdefault(
                p, {'name': p, 'children': {}, 'direct': 0, 'idx': None})
        node['direct'] = direct[idx]
        node['idx'] = idx                          # links node back to mod_tot[idx]

    def agg(n):                                    # subtree signal totals
        t = n['direct']
        for c in n['children'].values():
            t += agg(c)
        n['total'] = t
        return t
    agg(root)
    return root


def _heat_style(t):
    """Inline 'background/color' for a white(0)->red(1) box, with text color
    flipped to stay legible against the chosen background luminance."""
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    g = int(round(255 * (1 - t)))                  # white #ffffff -> red #ff0000
    lum = 76.245 + 0.701 * g                       # 0.299*R + (0.587+0.114)*g
    fg = '#0d1117' if lum >= 150 else '#f0f6fc'
    return 'background:#%02x%02x%02x;color:%s' % (255, g, g, fg)


def _render_module_node(node, depth, esc, mod_tot, amax):
    name = esc(node['name'])
    kids = node['children']
    act = mod_tot[node['idx']] if (mod_tot and node['idx'] is not None) else 0
    style = (' style="%s"' % _heat_style(act / amax if amax else 0.0)
             if mod_tot is not None else '')
    tip = ' title="%d toggles (own signals)"' % act if mod_tot is not None else ''
    if not kids:                                   # leaf scope
        return ('<div class="mod leaf"%s%s><span class="nm">%s</span>'
                '<span class="cnt">%d sig</span></div>'
                % (style, tip, name, node['direct']))
    badge = ('<span class="cnt">%d sig</span>' % node['direct']
             if node['direct'] else '')
    agg_b = '<span class="agg">%d total</span>' % node.get('total', 0)
    inner = ''.join(_render_module_node(c, depth + 1, esc, mod_tot, amax)
                    for c in kids.values())
    open_attr = ' open' if depth < 2 else ''       # top levels expanded by default
    return ('<details class="mod"%s><summary%s%s><span class="nm">%s</span>%s%s'
            '</summary><div class="kids">%s</div></details>'
            % (open_attr, style, tip, name, badge, agg_b, inner))


def render_hierarchy_html(hier, esc, mod_tot=None):
    root = _build_module_tree(hier)
    if not root['children']:
        return ''
    nmods = len(hier.get('modules', []))
    nsig = len(hier.get('id2mod', {}))
    amax = max(mod_tot) if mod_tot else 0
    body = ''.join(_render_module_node(c, 0, esc, mod_tot, amax)
                   for c in root['children'].values())
    legend = ''
    if mod_tot is not None:
        legend = ('<span class="legend">low<span class="bar"></span>high '
                  '(max %d toggles)</span>' % amax)
    return ('<div class="hier"><h2>Module hierarchy '
            '<span class="hint">(scope block diagram &mdash; %d scopes, '
            '%d signals; color = own switching activity, not sub-scopes; '
            'click a box to collapse)</span>%s</h2>'
            '<div class="tree">%s</div></div>'
            % (nmods, nsig, legend, body))


def render_html(csv_path, html_path, title, subtitle, xlabel, generated,
                max_points, meta, vcd_size, region=None, hier=None):
    xs, ys = [], []
    with open(csv_path, newline='') as fh:
        r = csv.DictReader(fh)
        xcol = 'time' if 'time' in r.fieldnames else 'start_time'
        for row in r:
            xs.append(int(row[xcol]))
            ys.append(float(row['percent_changed']))
    xs, ys, total = _decimate(xs, ys, max_points)
    if len(xs) < total:
        subtitle += (' | plotted %s of %s points (min/max decimated)'
                     % (f'{len(xs):,}', f'{total:,}'))

    region = region or {}
    nbins = region.get('nbins', 0)
    tmin = region.get('tmin', 0)
    tspan = region.get('tspan', 1)
    sim = region.get('sim')
    regime = region.get('regime')
    bin_w = tspan / nbins if nbins else 1
    bin_x = [tmin + (b + 0.5) * tspan / nbins for b in range(nbins)] if nbins else []

    # ---- activity line (+ optional similarity + regime bands) --------------
    traces = [{
        'x': xs, 'y': ys, 'type': 'scattergl', 'mode': 'lines',
        'name': '% changed', 'line': {'color': '#58a6ff', 'width': 1},
        'fill': 'tozeroy', 'fillcolor': 'rgba(88,166,255,0.12)',
        'hovertemplate': xlabel + ' = %{x}<br>%{y:.2f} %<extra></extra>'}]
    layout = {
        'paper_bgcolor': '#0d1117', 'plot_bgcolor': '#0d1117',
        'font': {'color': '#c9d1d9'}, 'showlegend': bool(sim),
        'legend': {'orientation': 'h', 'y': 1.08, 'x': 0},
        'margin': {'l': 64, 'r': 60, 't': 28, 'b': 46},
        'xaxis': {'title': xlabel, 'gridcolor': '#21262d',
                  'zerolinecolor': '#30363d', 'showspikes': True,
                  'spikecolor': '#484f58', 'spikethickness': 1,
                  'spikemode': 'across', 'spikedash': 'dot'},
        'yaxis': {'title': '% of signals changed', 'gridcolor': '#21262d',
                  'zerolinecolor': '#30363d', 'rangemode': 'tozero',
                  'ticksuffix': ' %'}}
    if sim:
        traces.append({
            'x': bin_x, 'y': [round(100 * s, 2) for s in sim], 'yaxis': 'y2',
            'type': 'scattergl', 'mode': 'lines', 'name': 'set similarity',
            'line': {'color': '#3fb950', 'width': 1.5},
            'hovertemplate': 'similarity to prev bin = %{y:.0f} %<extra></extra>'})
        layout['yaxis2'] = {'title': 'set similarity', 'overlaying': 'y',
                            'side': 'right', 'range': [0, 100],
                            'ticksuffix': ' %', 'showgrid': False}
    if regime:
        layout['shapes'] = _regime_bands(bin_x, regime, bin_w)

    config = {'responsive': True, 'scrollZoom': True, 'displaylogo': False,
              'modeBarButtonsToRemove': ['select2d', 'lasso2d'],
              'toImageButtonOptions': {'format': 'png',
                                       'filename': 'vcd_activity', 'scale': 2}}

    js = "Plotly.newPlot('chart', %s, %s, %s);" % (
        json.dumps(traces), json.dumps(layout), json.dumps(config))

    if sim:
        subtitle += (' | green = active-module-set similarity to previous bin'
                     ' (dips/color bands mark where different logic toggles)')

    esc = lambda s: html.escape(s) if s else 'n/a'
    mod_tot = (region or {}).get('mod_tot')
    hierarchy = render_hierarchy_html(hier, html.escape, mod_tot) if hier else ''
    page = HTML_TEMPLATE.format(
        title=title, subtitle=subtitle, generated=generated, script=js,
        m_tool=esc(meta.get('version')), m_date=esc(meta.get('date')),
        m_scale=esc(meta.get('timescale')), m_size=esc(human_size(vcd_size)),
        hierarchy=hierarchy)
    with open(html_path, 'w', encoding='utf-8') as out:
        out.write(page)


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
    ap.add_argument('--max-points', type=int, default=100000, metavar='N',
                    help='cap points embedded in the HTML via min/max '
                         'decimation, keeping spikes (default: 100000; '
                         '0 = embed every point)')
    ap.add_argument('--no-similarity', action='store_true',
                    help='do not render the active-set similarity / regime '
                         'overlay')
    ap.add_argument('--no-hierarchy', action='store_true',
                    help='do not render the module hierarchy / scope block '
                         'diagram (one nested box per scope). For designs with '
                         'huge scope trees this section dominates the HTML size, '
                         'so omitting it can shrink the file dramatically')
    ap.add_argument('--time-bins', type=int, default=600, metavar='N',
                    help='time columns for the similarity overlay '
                         '(default: 600)')
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
    ap.add_argument('--no-xz', action='store_true',
                    help='ignore x/z (unknown / high-impedance) values: a '
                         'transition into or out of x/z is not counted as '
                         'activity (the last defined value is retained)')
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

    is_gz = _is_gzip(args.vcd)
    if is_gz and args.ncores > 1:
        # parallel parsing seeks to byte offsets; a gzip stream is not seekable.
        sys.stderr.write('note: gzip input is read sequentially; ignoring '
                         '--ncores (running serial).\n')
        args.ncores = 1

    t0 = time.perf_counter()
    disk_size = os.path.getsize(args.vcd) or 1          # bytes on disk (shown)
    total_size = _gzip_isize(args.vcd) if is_gz else disk_size  # progress basis
    base = os.path.splitext(args.vcd)[0]
    if is_gz and base.endswith('.vcd'):                 # foo.vcd.gz -> foo
        base = base[:-4]
    out_path = args.output or (base + '.activity.csv')

    prog = Progress(total_size, not args.no_progress)
    all_ids, id_names, meta, hier, bytes_read, f = parse_header(args.vcd, prog)
    unit = meta['timescale'] or 'time units'

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
            args.clock, args.edge, args.include_clock, writer, prog, args.avg,
            args.no_xz)
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
                args.ncores, writer, prog, args.avg, args.no_xz)
        else:
            rows, total_signals = run_native(
                f, bytes_read, total_size, all_ids, writer, prog, args.avg,
                args.no_xz)
        xlabel = 'time (%s)' % unit
        if args.avg > 1:
            subtitle = ('%d signals | %d points (mean of %d timestamps each)'
                        % (total_signals, rows, args.avg))
        else:
            subtitle = ('%d signals | %d native timestamps'
                        % (total_signals, rows))

    if args.no_xz:
        subtitle += ' | x/z transitions ignored'
    if is_gz:
        subtitle += ' | gzip input'

    if not f.closed:
        f.close()
    out.close()

    # ---- region analysis (active-set similarity overlay), HTML only --------
    # This pass feeds the optional similarity overlay AND the hierarchy heatmap,
    # but neither the activity chart nor the (uncolored) hierarchy depend on it.
    # Any failure here must therefore degrade to region=None rather than abort
    # the whole HTML render, which happens later.
    region = None
    if args.html is not None and not args.no_similarity:
        try:
            if is_gz:
                # time_range / collect_module_activity seek into the file, which
                # a gzip stream cannot do; the (uncolored) hierarchy still renders.
                sys.stderr.write('note: similarity overlay / heatmap need random '
                                 'access; skipping for gzip input (use an '
                                 'uncompressed VCD to enable them).\n')
            elif not hier['modules']:
                sys.stderr.write('note: no module hierarchy in VCD; '
                                 'skipping similarity overlay.\n')
            else:
                tmin, tmax = time_range(args.vcd, bytes_read, total_size)
                if tmin is None or tmax is None:
                    sys.stderr.write('note: could not determine time range; '
                                     'skipping similarity overlay.\n')
                else:
                    nbins = max(1, min(args.time_bins, tmax - tmin + 1))
                    prog.new_bar()
                    M = collect_module_activity(args.vcd, bytes_read, total_size,
                                                hier, nbins, tmin, tmax,
                                                args.ncores, prog, args.no_xz)
                    sim, regime = similarity_regimes(M, nbins, 0.5)
                    nmods = len(hier['modules'])   # per-scope total toggles
                    mod_tot = [0] * nmods
                    for b in range(nbins):
                        row = M[b]
                        for m in range(nmods):
                            if row[m]:
                                mod_tot[m] += row[m]
                    region = {'nbins': nbins, 'tmin': tmin,
                              'tspan': max(1, tmax - tmin + 1),
                              'sim': sim, 'regime': regime, 'mod_tot': mod_tot}
        except Exception as e:
            sys.stderr.write('note: region analysis failed (%s); rendering '
                             'without overlay/heatmap.\n' % e)
            region = None

    generated = datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')

    if args.html is not None:
        html_path = (base + '.activity.html') if args.html == '__AUTO__' \
            else args.html
        title = 'VCD switching activity - %s' % os.path.basename(args.vcd)
        render_html(out_path, html_path, title, subtitle, xlabel, generated,
                    args.max_points, meta, disk_size, region,
                    None if args.no_hierarchy else hier)
        sys.stderr.write('html: %s\n' % html_path)

    sys.stderr.write('done: signals=%d  rows=%d  cores=%d  time=%s  -> %s\n'
                     % (total_signals, rows, args.ncores,
                        human_time(time.perf_counter() - t0), out_path))


if __name__ == '__main__':
    main()
