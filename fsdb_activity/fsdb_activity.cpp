// fsdb_activity.cpp - per-timestamp switching activity straight from an FSDB.
//
// Companion to vcd_activity.py: instead of converting FSDB -> VCD (which for a
// full chip produces a header alone of many GB), walk the FSDB through the
// Synopsys NPI reader API and emit only the aggregate the script needs:
// how many signals changed value at each timestamp.
//
// Counting rules match vcd_activity.py's native mode:
//   * a signal's first value is its initial value and is not a change;
//   * a signal counts at most once per timestamp, if any value it takes there
//     differs from its running last value;
//   * with --no-xz, values containing x/z are ignored (last known value kept).
//
// Output (stdout), line oriented, consumed by vcd_activity.py:
//   # fsdb_activity 2
//   timescale <unit>
//   open_rss <MB>           resident memory right after opening the FSDB
//   signals <N>
//   <time> <count>          one line per timestamp, ascending
//   resume <block>          only if --max-rss stopped the walk early
//   end
// Progress goes to stderr.
//
// Memory: NPI keeps what it allocates for every signal handle and value-change
// load until the FSDB is closed, so a process only grows.  With --max-rss the
// walk stops at the first scope-block boundary where it would pass that
// ceiling, reports the counts for the blocks it finished, and names the block
// to resume from; vcd_activity.py then continues in a fresh process
// (--start-block), which is the only way to hand that memory back.
//
// Build: see Makefile (Verdi install or FSDB+ SDK).

#include <algorithm>
#include <climits>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <vector>

#include <unistd.h>

#include "npi.h"
#include "npi_fsdb.h"

#ifdef NPI_FPLUS
#include "npi_util.h"
extern int npi_fplus_init();          // in libNPI.so, not in public headers
#endif

namespace {

struct Options {
    const char* path = nullptr;
    std::vector<std::string> scope;   // dotted --scope split into components
    bool no_xz = false;
    size_t batch = 4000000;           // cap on signals per load_vc_by_range
    unsigned part = 0, nparts = 1;    // --part K/N: this process's share
    unsigned long long start_block = 0;   // --start-block: skip earlier blocks
    long max_rss_mb = 0;              // --max-rss: stop before passing this
    bool progress = true;
};

struct Leaf {
    npiFsdbSigHandle sig;
    bool is_real;
};

// Per-timestamp change counts, keyed by FSDB time.  A full chip has a row for
// every timestamp, so this is an open-addressing table (16 bytes a slot)
// rather than a node-based map (~60 bytes a row, plus a copy to sort it).
class CountTable {
public:
    struct Slot {
        npiFsdbTime t;
        unsigned long long n;
    };

    CountTable() { slots_.assign(size_t(1) << bits_, Slot{kEmpty, 0}); }

    // The count for time t, inserting a zero row if t is new.  The reference
    // is valid until the next call.
    unsigned long long& at(npiFsdbTime t)
    {
        if ((size_ + 1) * 10 > slots_.size() * 7) grow();
        Slot& s = find(t);
        if (s.t == kEmpty) {
            s.t = t;
            ++size_;
        }
        return s.n;
    }

    size_t size() const { return size_; }

    // The rows in ascending time order; consumes the table.
    std::vector<Slot>& sorted()
    {
        size_t out = 0;
        for (size_t i = 0; i < slots_.size(); ++i)
            if (slots_[i].t != kEmpty) slots_[out++] = slots_[i];
        slots_.resize(out);
        slots_.shrink_to_fit();
        std::sort(slots_.begin(), slots_.end(),
                  [](const Slot& a, const Slot& b) { return a.t < b.t; });
        return slots_;
    }

private:
    static const npiFsdbTime kEmpty = ~npiFsdbTime(0);

    Slot& find(npiFsdbTime t)
    {
        size_t mask = slots_.size() - 1;
        size_t i = size_t((t * 0x9E3779B97F4A7C15ull) >> (64 - bits_));
        while (slots_[i].t != kEmpty && slots_[i].t != t) i = (i + 1) & mask;
        return slots_[i];
    }

    void grow()
    {
        std::vector<Slot> old;
        old.swap(slots_);
        ++bits_;
        slots_.assign(size_t(1) << bits_, Slot{kEmpty, 0});
        for (const Slot& s : old)
            if (s.t != kEmpty) find(s.t) = s;
    }

    unsigned bits_ = 16;
    size_t size_ = 0;
    std::vector<Slot> slots_;
};

CountTable g_counts;
unsigned long long g_signals = 0;
unsigned long long g_batches_done = 0;
size_t g_batch_size = 20000;          // current batch; doubles up to the cap
const unsigned long long kPartBlock = 16;     // --part granularity (scopes)
unsigned long long g_scopes = 0;              // scopes seen in the selection
unsigned long long g_cur_block = ULLONG_MAX;  // block being read, if any
unsigned long long g_blocks_here = 0;         // blocks read by this process
bool g_stopped = false;                       // --max-rss ended the walk
unsigned long long g_resume = 0;              //   ... at this block
double g_mb_per_sig = 0;                      // RSS growth per signal read
time_t g_t0;

long rss_mb()
{
    FILE* f = fopen("/proc/self/status", "r");
    if (!f) return 0;
    char line[256];
    long kb = 0;
    while (fgets(line, sizeof line, f))
        if (sscanf(line, "VmRSS: %ld", &kb) == 1) break;
    fclose(f);
    return kb / 1024;
}

bool has_xz(const char* s)
{
    for (; *s; ++s) {
        char c = *s;
        if (c == 'x' || c == 'X' || c == 'z' || c == 'Z') return true;
    }
    return false;
}

void progress(bool force)
{
    static time_t last = 0;
    time_t now = time(nullptr);
    if (!force && now == last) return;
    last = now;
    fprintf(stderr, "\rfsdb_activity: %llu signals, %zu timestamps, %lds",
            g_signals, g_counts.size(), (long)(now - g_t0));
    fflush(stderr);
}

// Read one signal's value at the vct's current position into `out`.
// Returns false if the value could not be read.
bool read_value(npiFsdbVctHandle vct, bool is_real, std::string& out)
{
    npiFsdbValue v;
    if (is_real) {
        v.format = npiFsdbRealVal;
        if (!npi_fsdb_vct_value(vct, &v)) return false;
        out.assign(reinterpret_cast<const char*>(&v.value.real), sizeof(double));
        return true;
    }
    v.format = npiFsdbBinStrVal;
    if (!npi_fsdb_vct_value(vct, &v) || !v.value.str) return false;
    out.assign(v.value.str);
    return true;
}

void process_batch(npiFsdbFileHandle file, std::vector<Leaf>& batch,
                   npiFsdbTime tmin, npiFsdbTime tmax, const Options& opt)
{
    if (batch.empty()) return;
    long rss_before = opt.max_rss_mb ? rss_mb() : 0;
    for (const Leaf& l : batch) npi_fsdb_add_to_sig_list(file, l.sig);
    npi_fsdb_load_vc_by_range(file, tmin, tmax);

    std::string last, val;
    for (const Leaf& l : batch) {
        npiFsdbVctHandle vct = npi_fsdb_create_vct(l.sig);
        if (!vct) continue;
        bool have_last = false;
        bool counted = false;         // already counted at timestamp `cur`
        npiFsdbTime cur = 0;
        for (int ok = npi_fsdb_goto_first(vct); ok; ok = npi_fsdb_goto_next(vct)) {
            npiFsdbTime t;
            npi_fsdb_vct_time(vct, &t);
            if (t != cur) {
                cur = t;
                counted = false;
            }
            unsigned long long& n = g_counts.at(t);  // every change time is a row
            if (!read_value(vct, l.is_real, val)) continue;
            if (opt.no_xz && !l.is_real && has_xz(val.c_str())) continue;
            if (!have_last) {         // initial value: not a change
                last = val;
                have_last = true;
            } else if (val != last) {
                last = val;
                if (!counted) {
                    ++n;
                    counted = true;
                }
            }
        }
        npi_fsdb_release_vct(vct);
    }
    npi_fsdb_unload_vc(file);
    npi_fsdb_reset_sig_list(file);
    ++g_batches_done;
    // Every load_vc_by_range walks all variables in the file (NPI resets its
    // view window), so the number of loads must stay small on huge designs:
    // grow the batch geometrically up to the --batch cap.
    g_batch_size = std::min(g_batch_size * 2, opt.batch);
    if (opt.max_rss_mb) {
        // What this batch kept (NPI frees none of it before close) predicts
        // the next one; keep the next batch within the remaining headroom.
        long rss = rss_mb();
        g_mb_per_sig = std::max(g_mb_per_sig,
                                double(rss - rss_before) / batch.size());
        if (g_mb_per_sig > 0) {
            double room = double(opt.max_rss_mb - rss) / g_mb_per_sig;
            g_batch_size = std::min(g_batch_size,
                                    size_t(std::max(1000.0, room)));
        }
    }
    batch.clear();
}

// Add a signal (recursing into struct/array members) to the batch.
void add_signal(npiFsdbFileHandle file, npiFsdbSigHandle sig,
                std::vector<Leaf>& batch, npiFsdbTime tmin, npiFsdbTime tmax,
                const Options& opt)
{
    NPI_INT32 has_member = 0;
    if (npi_fsdb_sig_property(npiFsdbSigHasMember, sig, &has_member)
        && has_member == 1) {
        npiFsdbSigIter it = npi_fsdb_iter_member(sig);
        if (it) {
            while (npiFsdbSigHandle m = npi_fsdb_iter_sig_next(it))
                add_signal(file, m, batch, tmin, tmax, opt);
            npi_fsdb_iter_sig_stop(it);
        }
        return;
    }
    NPI_INT32 is_real = 0;
    npi_fsdb_sig_property(npiFsdbSigIsReal, sig, &is_real);
    batch.push_back(Leaf{sig, is_real == 1});
    ++g_signals;
    if (batch.size() >= g_batch_size) {
        process_batch(file, batch, tmin, tmax, opt);
        if (opt.progress) progress(false);
    }
}

// depth = number of path components matched so far (scope filter).
void walk_scope(npiFsdbFileHandle file, npiFsdbScopeHandle scope, size_t depth,
                std::vector<Leaf>& batch, npiFsdbTime tmin, npiFsdbTime tmax,
                const Options& opt)
{
    if (g_stopped) return;
    if (depth < opt.scope.size()) {
        const char* name = npi_fsdb_scope_property_str(npiFsdbScopeName, scope);
        if (!name || opt.scope[depth] != name) return;
        ++depth;
    }
    // With --part, every process walks the whole *scope* tree (cheap next to
    // the signals, and identical order everywhere) but takes the signals of
    // only its share of fixed-size scope blocks; per-timestamp counts and
    // signal totals then simply add up across processes.  The same blocks
    // are the --max-rss checkpoints: a block is always read whole.
    bool mine = true;
    if (depth >= opt.scope.size()) {
        unsigned long long block = g_scopes++ / kPartBlock;
        mine = block % opt.nparts == opt.part && block >= opt.start_block;
        if (mine && block != g_cur_block) {    // first scope of a new block
            // Stop if this process's memory plus the pending batch would pass
            // the ceiling; read at least one block so every process advances.
            if (opt.max_rss_mb && g_blocks_here > 0
                && rss_mb() + batch.size() * g_mb_per_sig > opt.max_rss_mb) {
                g_stopped = true;
                g_resume = block;
                return;
            }
            g_cur_block = block;
            ++g_blocks_here;
        }
    }
    if (depth >= opt.scope.size() && mine) {   // inside the selected sub-tree
        npiFsdbSigIter si = npi_fsdb_iter_sig(scope);
        if (si) {
            while (npiFsdbSigHandle s = npi_fsdb_iter_sig_next(si))
                add_signal(file, s, batch, tmin, tmax, opt);
            npi_fsdb_iter_sig_stop(si);
        }
    }
    npiFsdbScopeIter ci = npi_fsdb_iter_child_scope(scope);
    if (ci) {
        while (!g_stopped) {
            npiFsdbScopeHandle c = npi_fsdb_iter_scope_next(ci);
            if (!c) break;
            walk_scope(file, c, depth, batch, tmin, tmax, opt);
        }
        npi_fsdb_iter_scope_stop(ci);
    }
}

void usage(const char* argv0)
{
    fprintf(stderr,
            "usage: %s <file.fsdb> [--scope a.b.c] [--no-xz] [--batch N] "
            "[--part K/N] [--start-block B] [--max-rss MB] [--no-progress]\n",
            argv0);
}

bool parse_args(int argc, char** argv, Options& opt)
{
    for (int i = 1; i < argc; ++i) {
        const char* a = argv[i];
        if (!strcmp(a, "--scope") && i + 1 < argc) {
            std::string s = argv[++i];
            size_t p = 0, q;
            while ((q = s.find('.', p)) != std::string::npos) {
                opt.scope.push_back(s.substr(p, q - p));
                p = q + 1;
            }
            opt.scope.push_back(s.substr(p));
        } else if (!strcmp(a, "--no-xz")) {
            opt.no_xz = true;
        } else if (!strcmp(a, "--batch") && i + 1 < argc) {
            opt.batch = std::max(1L, atol(argv[++i]));
        } else if (!strcmp(a, "--part") && i + 1 < argc) {
            if (sscanf(argv[++i], "%u/%u", &opt.part, &opt.nparts) != 2
                || opt.nparts == 0 || opt.part >= opt.nparts)
                return false;
        } else if (!strcmp(a, "--start-block") && i + 1 < argc) {
            opt.start_block = strtoull(argv[++i], nullptr, 10);
        } else if (!strcmp(a, "--max-rss") && i + 1 < argc) {
            opt.max_rss_mb = std::max(0L, atol(argv[++i]));
        } else if (!strcmp(a, "--no-progress")) {
            opt.progress = false;
        } else if (a[0] == '-') {
            return false;
        } else if (!opt.path) {
            opt.path = a;
        } else {
            return false;
        }
    }
    return opt.path != nullptr;
}

}  // namespace

int main(int argc, char** argv)
{
    Options opt;
    if (!parse_args(argc, argv, opt)) {
        usage(argv[0]);
        return 2;
    }

    // NPI prints banners/log paths on stdout.  Keep the real stdout for our
    // data on a private fd and send fd 1 to stderr for everyone else.
    fflush(stdout);
    int data_fd = dup(1);
    FILE* data = data_fd >= 0 ? fdopen(data_fd, "w") : nullptr;
    if (!data || dup2(2, 1) < 0) {
        fprintf(stderr, "error: cannot set up output stream\n");
        return 3;
    }

#ifdef NPI_LIBDIR
    // Older NPI (e.g. Verdi 2017) locates its resource directory through
    // LD_LIBRARY_PATH rather than the loaded library, so make sure the NPI
    // lib dir we were built against is on it.
    {
        const char* old = getenv("LD_LIBRARY_PATH");
        std::string lp = NPI_LIBDIR;
        if (old && *old) lp += std::string(":") + old;
        setenv("LD_LIBRARY_PATH", lp.c_str(), 1);
    }
#endif

    // NPI may consume its own options from argv; hand it just the program.
    int nargc = 1;
    char* nargv_arr[] = {argv[0], nullptr};
    char** nargv = nargv_arr;
#ifdef NPI_FPLUS
    npi_arg_construct(nargc, nargv);
    if (!npi_fplus_init()) {
        fprintf(stderr, "error: NPI (FSDB+) init failed - license?\n");
        return 3;
    }
    npiFsdbFileHandle file = npi_waveform_open(opt.path);
#else
    int init_rc = npi_init(nargc, nargv);
    if (getenv("FSDB_ACTIVITY_DEBUG"))
        fprintf(stderr, "fsdb_activity: npi_init rc=%d\n", init_rc);
    if (!init_rc) {                         // 0 == failure (1 on success)
        fprintf(stderr, "error: NPI init failed - check that a Verdi license "
                        "is reachable (LM_LICENSE_FILE / SNPSLMD_LICENSE_FILE)"
                        " and that this build matches the Verdi install "
                        "(%s)\n",
#ifdef NPI_LIBDIR
                NPI_LIBDIR
#else
                "unknown"
#endif
                );
        return 3;
    }
    npiFsdbFileHandle file = npi_fsdb_open(opt.path);
#endif
    if (!file) {
        fprintf(stderr, "error: cannot open FSDB '%s' (not an FSDB, "
                        "unreadable, or no license)\n", opt.path);
        return 3;
    }

    long open_rss = rss_mb();
    npiFsdbTime tmin = 0, tmax = 0;
    npi_fsdb_min_time(file, &tmin);
    npi_fsdb_max_time(file, &tmax);
    const char* unit = npi_fsdb_file_property_str(npiFsdbFileScaleUnit, file);
    g_t0 = time(nullptr);

    g_batch_size = std::min(g_batch_size, opt.batch);
    std::vector<Leaf> batch;
    npiFsdbScopeIter ti = npi_fsdb_iter_top_scope(file);
    if (ti) {
        while (!g_stopped) {
            npiFsdbScopeHandle s = npi_fsdb_iter_scope_next(ti);
            if (!s) break;
            walk_scope(file, s, 0, batch, tmin, tmax, opt);
        }
        npi_fsdb_iter_scope_stop(ti);
    }
    // Signals directly at file top: read once, by part 0's last process.
    if (opt.scope.empty() && opt.part == 0 && !g_stopped) {
        npiFsdbSigIter si = npi_fsdb_iter_top_sig(file);
        if (si) {
            while (npiFsdbSigHandle s = npi_fsdb_iter_sig_next(si))
                add_signal(file, s, batch, tmin, tmax, opt);
            npi_fsdb_iter_sig_stop(si);
        }
    }
    process_batch(file, batch, tmin, tmax, opt);
    if (opt.progress) {
        progress(true);
        fputc('\n', stderr);
    }

    // A --part share, or a resumed process, may legitimately have none.
    if (g_signals == 0 && opt.nparts == 1 && opt.start_block == 0) {
        fprintf(stderr, "error: no signals found%s\n",
                opt.scope.empty() ? "" : " under --scope");
        npi_fsdb_close(file);
        return 4;
    }

    fprintf(data, "# fsdb_activity 2\n");
    fprintf(data, "timescale %s\n", (unit && *unit) ? unit : "");
    fprintf(data, "open_rss %ld\n", open_rss);
    fprintf(data, "signals %llu\n", g_signals);
    for (const auto& r : g_counts.sorted())
        fprintf(data, "%llu %llu\n", (unsigned long long)r.t, r.n);
    if (g_stopped) fprintf(data, "resume %llu\n", g_resume);
    fprintf(data, "end\n");
    if (fclose(data) != 0) {
        fprintf(stderr, "error: writing output failed\n");
        return 3;
    }

    npi_fsdb_close(file);
#ifndef NPI_FPLUS
    npi_end();
#endif
    return 0;
}
