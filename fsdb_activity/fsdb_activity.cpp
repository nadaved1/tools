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
//   # fsdb_activity 1
//   timescale <unit>
//   signals <N>
//   <time> <count>          one line per timestamp, ascending
//   end
// Progress goes to stderr.
//
// Build: see Makefile (Verdi install or FSDB+ SDK).

#include <algorithm>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <ctime>
#include <string>
#include <unordered_map>
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
    bool progress = true;
};

struct Leaf {
    npiFsdbSigHandle sig;
    bool is_real;
};

// Per-timestamp change counts, keyed by FSDB time.
std::unordered_map<npiFsdbTime, unsigned long long> g_counts;
unsigned long long g_signals = 0;
unsigned long long g_batches_done = 0;
size_t g_batch_size = 20000;          // current batch; doubles up to the cap
const unsigned long long kPartBlock = 16;     // --part granularity (scopes)
unsigned long long g_scopes = 0;              // scopes seen in the selection
time_t g_t0;

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
            g_counts.emplace(t, 0);   // every value-change time is a row
            if (!read_value(vct, l.is_real, val)) continue;
            if (opt.no_xz && !l.is_real && has_xz(val.c_str())) continue;
            if (!have_last) {         // initial value: not a change
                last = val;
                have_last = true;
            } else if (val != last) {
                last = val;
                if (!counted) {
                    ++g_counts[t];
                    counted = true;
                }
            }
        }
        npi_fsdb_release_vct(vct);
    }
    npi_fsdb_unload_vc(file);
    npi_fsdb_reset_sig_list(file);
    batch.clear();
    ++g_batches_done;
    // Every load_vc_by_range walks all variables in the file (NPI resets its
    // view window), so the number of loads must stay small on huge designs:
    // grow the batch geometrically up to the --batch cap.
    g_batch_size = std::min(g_batch_size * 2, opt.batch);
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
    if (depth < opt.scope.size()) {
        const char* name = npi_fsdb_scope_property_str(npiFsdbScopeName, scope);
        if (!name || opt.scope[depth] != name) return;
        ++depth;
    }
    // With --part, every process walks the whole *scope* tree (cheap next to
    // the signals, and identical order everywhere) but takes the signals of
    // only its share of fixed-size scope blocks; per-timestamp counts and
    // signal totals then simply add up across processes.
    bool mine = true;
    if (depth >= opt.scope.size())
        mine = (g_scopes++ / kPartBlock) % opt.nparts == opt.part;
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
        while (npiFsdbScopeHandle c = npi_fsdb_iter_scope_next(ci))
            walk_scope(file, c, depth, batch, tmin, tmax, opt);
        npi_fsdb_iter_scope_stop(ci);
    }
}

void usage(const char* argv0)
{
    fprintf(stderr,
            "usage: %s <file.fsdb> [--scope a.b.c] [--no-xz] [--batch N] "
            "[--part K/N] [--no-progress]\n", argv0);
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

    npiFsdbTime tmin = 0, tmax = 0;
    npi_fsdb_min_time(file, &tmin);
    npi_fsdb_max_time(file, &tmax);
    const char* unit = npi_fsdb_file_property_str(npiFsdbFileScaleUnit, file);
    g_t0 = time(nullptr);

    g_batch_size = std::min(g_batch_size, opt.batch);
    std::vector<Leaf> batch;
    npiFsdbScopeIter ti = npi_fsdb_iter_top_scope(file);
    if (ti) {
        while (npiFsdbScopeHandle s = npi_fsdb_iter_scope_next(ti))
            walk_scope(file, s, 0, batch, tmin, tmax, opt);
        npi_fsdb_iter_scope_stop(ti);
    }
    if (opt.scope.empty() && opt.part == 0) {   // signals directly at file top
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

    if (g_signals == 0 && opt.nparts == 1) {    // a --part share may be empty
        fprintf(stderr, "error: no signals found%s\n",
                opt.scope.empty() ? "" : " under --scope");
        npi_fsdb_close(file);
        return 4;
    }

    std::vector<std::pair<npiFsdbTime, unsigned long long>> rows(
        g_counts.begin(), g_counts.end());
    std::sort(rows.begin(), rows.end());

    fprintf(data, "# fsdb_activity 1\n");
    fprintf(data, "timescale %s\n", (unit && *unit) ? unit : "");
    fprintf(data, "signals %llu\n", g_signals);
    for (const auto& r : rows)
        fprintf(data, "%llu %llu\n", (unsigned long long)r.first, r.second);
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
