/* igpu_launch_timing_format.h - host-only half of the CUDA launch timer
 * (GPU-EVENT153-ROOTCAUSE-2). igpu_launch_timing.cuh records cudaEvents around
 * every kernel launch and host time around every synchronising call in the two
 * CUDA backend DLLs; this header owns everything that needs no CUDA: the env
 * gate, the per-stage statistics and the exact text of every log line. It has
 * no CUDA and no Qt dependency so console_tests compiles it directly
 * (tests/console/test_cuda_launch_timing.cpp).
 *
 * Gate: MLVAPP_CUDA_LAUNCH_TIMING=1|on|true|yes (case-insensitive, trimmed)
 * turns the timer on; anything else, or unset, leaves it OFF and every timed
 * site costs one cached-bool test. MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS=<ms> (default
 * 0 = every launch) suppresses per-launch lines faster than <ms>; the first
 * launch of each stage, every error and the periodic per-stage summaries are
 * always written.
 *
 * Lines (one per record, key=value, written to stderr, which the GUI smoke
 * captures into the run's result.json process.stderr):
 *   igpu_launch_timing_config dll=recon enabled=1 min_ms=2.000
 *   igpu_launch_timing dll=recon kind=kernel stage=k_promote_match seq=12
 *       utc_ms=1791774588083.123 grid=120x51x1 block=16x16x1 w=1920 h=804 ms=0.412
 *   igpu_launch_timing ... kind=sync stage=run:ev_after_dl ... ms=3.100
 *   igpu_launch_timing ... ms=na err=700 err_name=cudaErrorIllegalAddress
 *   igpu_launch_timing_summary dll=amaze kind=kernel stage=k_gradients at=periodic
 *       count=6000 over_min=0 errors=0 sum_ms=1210.000 max_ms=0.811 max_seq=4410
 *       max_utc_ms=... max_grid=32x32x1 max_block=16x16x1 max_w=1920 max_h=804 max_tile=464:944
 * (each shown wrapped here; every real line is a single line). utc_ms is the
 * wall clock at issue, in Unix epoch milliseconds, so a line can be matched
 * against a System-log event time. tile= / max_tile= (top:left) appear only on
 * tiled AMaZE launches.
 */
#ifndef IGPU_LAUNCH_TIMING_FORMAT_H
#define IGPU_LAUNCH_TIMING_FORMAT_H

#include <cctype>
#include <cmath>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>

#define IGPU_LT_ENV_ENABLE "MLVAPP_CUDA_LAUNCH_TIMING"
#define IGPU_LT_ENV_MIN_MS "MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS"
#define IGPU_LT_NO_TILE (-2147483647 - 1)

struct IgpuLtDim
{
    unsigned int x;
    unsigned int y;
    unsigned int z;
};

struct IgpuLtRecord
{
    const char * dll;      /* "recon" | "amaze" | "probe" */
    const char * kind;     /* "kernel" | "sync" */
    const char * stage;    /* kernel name, or "<call site>:<what>" for a sync */
    unsigned long long seq;
    double utc_ms;         /* wall clock at issue, Unix epoch ms */
    IgpuLtDim grid;        /* 0x0x0 for a sync */
    IgpuLtDim block;
    int width;             /* frame dims; 0 when the call site has none */
    int height;
    int tile_top;          /* IGPU_LT_NO_TILE when not a tiled launch */
    int tile_left;
    double ms;             /* < 0: no duration (an error before it resolved) */
    int err;               /* cudaError_t value; 0 = success */
    const char * err_name; /* NULL when err == 0 */
    /* CUDA-RECON-PRESENT-OVERLAP-TIMELINE-1: device-clock stamps, Unix epoch ms, set only when
     * the pair resolved against the DLL's anchor event (anchor_utc_ms + cudaEventElapsedTime
     * from the anchor). has_dev == 0 leaves the line byte-identical to the format without them. */
    int has_dev;
    double dev_t0_utc_ms;  /* the start event */
    double dev_t1_utc_ms;  /* the stop event */
    int dev_marks;         /* 0, or 2: inner marks (recon kind=span stage=run: ev_after_up, ev_after_kernel) */
    double dev_mark_utc_ms[2];
};

struct IgpuLtStats
{
    unsigned long long count;
    unsigned long long over_min;
    unsigned long long errors;
    double sum_ms;
    double max_ms;
    int has_max;
    IgpuLtRecord max_rec;
};

/* 1 for 1|on|true|yes (case-insensitive, surrounding whitespace ignored), else 0. */
static inline int igpu_lt_parse_enabled(const char * value)
{
    if (!value) return 0;
    while (*value && std::isspace((unsigned char)*value)) ++value;
    std::size_t n = std::strlen(value);
    while (n > 0 && std::isspace((unsigned char)value[n - 1])) --n;
    if (n == 0 || n > 4) return 0;
    char lower[5] = {0, 0, 0, 0, 0};
    for (std::size_t i = 0; i < n; ++i) lower[i] = (char)std::tolower((unsigned char)value[i]);
    return std::strcmp(lower, "1") == 0 || std::strcmp(lower, "on") == 0
        || std::strcmp(lower, "true") == 0 || std::strcmp(lower, "yes") == 0;
}

/* The per-launch line threshold in ms. Unset, empty, unparseable, negative,
 * NaN or infinite -> 0 (every launch). */
static inline double igpu_lt_parse_min_ms(const char * value)
{
    if (!value || !*value) return 0.0;
    char * end = NULL;
    const double parsed = std::strtod(value, &end);
    if (end == value) return 0.0;
    while (*end && std::isspace((unsigned char)*end)) ++end;
    if (*end) return 0.0;
    if (!(parsed > 0.0) || std::isinf(parsed)) return 0.0;
    return parsed;
}

/* Whether a resolved record gets its own line: every error, the first launch
 * of its stage, and anything at or over the threshold. */
static inline int igpu_lt_should_log_record(const IgpuLtRecord & r, double min_ms, int first_of_stage)
{
    if (r.err != 0) return 1;
    if (first_of_stage) return 1;
    return r.ms >= min_ms;
}

static inline void igpu_lt_stats_add(IgpuLtStats * s, const IgpuLtRecord & r, double min_ms)
{
    if (!s) return;
    s->count += 1;
    if (r.err != 0) s->errors += 1;
    if (r.ms < 0.0) return;
    s->sum_ms += r.ms;
    if (r.ms >= min_ms && min_ms > 0.0) s->over_min += 1;
    if (!s->has_max || r.ms > s->max_ms)
    {
        s->has_max = 1;
        s->max_ms = r.ms;
        s->max_rec = r;
    }
}

/* GPU-FIELD-TELEMETRY-1: a fixed log-bucket histogram for p95, host-only.
 * Bucket 0 holds ms < 0.01; bucket k (1..IGPU_LT_HIST_BUCKETS-2) holds
 * [edge(k-1), edge(k)) with edge(k) = 0.01 * 2^(k/4) ms; the last bucket holds
 * everything from edge(IGPU_LT_HIST_BUCKETS-2) (~119 s) up. Each bucket spans a
 * factor of 2^(1/4) (~1.19), so a quantile read as its bucket's upper edge is
 * never below the true nearest-rank value and less than 1.19x above it. */
#define IGPU_LT_HIST_BUCKETS 96
#define IGPU_LT_HIST_FIRST_EDGE_MS 0.01

struct IgpuLtHistogram
{
    unsigned long long counts[IGPU_LT_HIST_BUCKETS];
    unsigned long long total;
};

/* Upper edge of bucket k in ms; the overflow bucket has none (returns +inf). */
static inline double igpu_lt_hist_upper_ms(int k)
{
    if (k >= IGPU_LT_HIST_BUCKETS - 1) return HUGE_VAL;
    return IGPU_LT_HIST_FIRST_EDGE_MS * std::pow(2.0, (double)k / 4.0);
}

static inline int igpu_lt_hist_bucket(double ms)
{
    if (!(ms >= IGPU_LT_HIST_FIRST_EDGE_MS)) return 0;
    int k = (int)std::floor(4.0 * std::log2(ms / IGPU_LT_HIST_FIRST_EDGE_MS)) + 1;
    if (k < 1) k = 1;
    /* log2 rounding: keep the bucket's half-open range exact at its edges. */
    while (k > 1 && ms < igpu_lt_hist_upper_ms(k - 1)) --k;
    while (k < IGPU_LT_HIST_BUCKETS - 1 && ms >= igpu_lt_hist_upper_ms(k)) ++k;
    return k;
}

/* Negative ms (an error with no duration) is not a sample. */
static inline void igpu_lt_hist_add(IgpuLtHistogram * h, double ms)
{
    if (!h || ms < 0.0) return;
    h->counts[igpu_lt_hist_bucket(ms)] += 1;
    h->total += 1;
}

/* Nearest-rank quantile q in (0, 1]: the upper edge of the bucket holding the
 * ceil(q * total)-th sample, capped at max_ms (the exact maximum the caller
 * tracked, so the top bucket reads exactly). 0 with no samples. */
static inline double igpu_lt_hist_quantile(const IgpuLtHistogram & h, double q, double max_ms)
{
    if (h.total == 0) return 0.0;
    if (q > 1.0) q = 1.0;
    unsigned long long rank = (unsigned long long)std::ceil(q * (double)h.total);
    if (rank < 1) rank = 1;
    unsigned long long seen = 0;
    for (int k = 0; k < IGPU_LT_HIST_BUCKETS; ++k)
    {
        seen += h.counts[k];
        if (seen >= rank)
        {
            const double upper = igpu_lt_hist_upper_ms(k);
            return upper < max_ms ? upper : max_ms;
        }
    }
    return max_ms;
}

/* snprintf-style: returns the length the full line needs (excluding the NUL);
 * a return >= cap means the line was truncated. No trailing newline. */
static inline int igpu_lt_format_record(char * out, std::size_t cap, const IgpuLtRecord & r)
{
    char tile[40] = "";
    if (r.tile_top != IGPU_LT_NO_TILE && r.tile_left != IGPU_LT_NO_TILE)
        std::snprintf(tile, sizeof(tile), " tile=%d:%d", r.tile_top, r.tile_left);
    char ms[40];
    if (r.ms >= 0.0) std::snprintf(ms, sizeof(ms), "%.3f", r.ms);
    else std::snprintf(ms, sizeof(ms), "na");
    char err[96] = "";
    if (r.err != 0)
        std::snprintf(err, sizeof(err), " err=%d err_name=%s", r.err, r.err_name ? r.err_name : "unknown");
    int n = std::snprintf(out, cap,
                          "igpu_launch_timing dll=%s kind=%s stage=%s seq=%llu utc_ms=%.3f "
                          "grid=%ux%ux%u block=%ux%ux%u w=%d h=%d%s ms=%s%s",
                          r.dll ? r.dll : "?", r.kind ? r.kind : "?", r.stage ? r.stage : "?",
                          r.seq, r.utc_ms,
                          r.grid.x, r.grid.y, r.grid.z, r.block.x, r.block.y, r.block.z,
                          r.width, r.height, tile, ms, err);
    if (n < 0 || !r.has_dev) return n;
    /* Adversary r2 pass 2 A4: the device stamps are appended straight into `out` (no fixed
     * intermediate buffer), and the return is the FULL length, so a caller that sizes from it
     * gets every field and a short buffer is reported as truncated, never cut silently. */
    const auto append = [&](const char * fmt, double a, double b) {
        const std::size_t at = (std::size_t)n < cap ? (std::size_t)n : (cap ? cap - 1 : 0);
        const std::size_t room = cap > at ? cap - at : 0;
        const int m = std::snprintf(room ? out + at : nullptr, room, fmt, a, b);
        if (m > 0) n += m;
    };
    append(" dev_t0_utc_ms=%.3f dev_t1_utc_ms=%.3f", r.dev_t0_utc_ms, r.dev_t1_utc_ms);
    if (r.dev_marks == 2) append(" dev_marks_utc_ms=%.3f,%.3f", r.dev_mark_utc_ms[0], r.dev_mark_utc_ms[1]);
    return n;
}

static inline int igpu_lt_format_summary(char * out, std::size_t cap,
                                         const char * dll, const char * kind, const char * stage,
                                         const char * at, const IgpuLtStats & s)
{
    char max[256] = " max_ms=na";
    if (s.has_max)
    {
        const IgpuLtRecord & m = s.max_rec;
        char tile[40] = "";
        if (m.tile_top != IGPU_LT_NO_TILE && m.tile_left != IGPU_LT_NO_TILE)
            std::snprintf(tile, sizeof(tile), " max_tile=%d:%d", m.tile_top, m.tile_left);
        std::snprintf(max, sizeof(max),
                      " max_ms=%.3f max_seq=%llu max_utc_ms=%.3f max_grid=%ux%ux%u max_block=%ux%ux%u "
                      "max_w=%d max_h=%d%s",
                      s.max_ms, m.seq, m.utc_ms, m.grid.x, m.grid.y, m.grid.z,
                      m.block.x, m.block.y, m.block.z, m.width, m.height, tile);
    }
    return std::snprintf(out, cap,
                         "igpu_launch_timing_summary dll=%s kind=%s stage=%s at=%s count=%llu "
                         "over_min=%llu errors=%llu sum_ms=%.3f%s",
                         dll ? dll : "?", kind ? kind : "?", stage ? stage : "?", at ? at : "?",
                         s.count, s.over_min, s.errors, s.sum_ms, max);
}

static inline int igpu_lt_format_config(char * out, std::size_t cap, const char * dll, double min_ms)
{
    return std::snprintf(out, cap, "igpu_launch_timing_config dll=%s enabled=1 min_ms=%.3f",
                         dll ? dll : "?", min_ms);
}

/* Adversary r2 A1. The tolerance for one monotonic interval to stand in for another: a wall read's
 * own pin (the monotonic time around it), and the wall-vs-monotonic agreement of one anchor try. */
#define IGPU_LT_ANCHOR_STEP_TOL_MS 0.010

/* Adversary r2 A2. cudaEventElapsedTime returns a float (24-bit mantissa), so an elapsed time far
 * from the anchor loses precision. A device stamp is written only for an elapsed time in
 * [0, IGPU_LT_ANCHOR_MAX_AGE_MS); there its rounding is at most IGPU_LT_ANCHOR_ROUNDING_MS
 * (half of 2^(19-24) ms). Past that age a record carries no stamp (has_dev=0), never a coarse one. */
#define IGPU_LT_ANCHOR_MAX_AGE_MS 524288.0
#define IGPU_LT_ANCHOR_ROUNDING_MS 0.015625

/* Adversary r2 pass 3 A3. A bound printed with %.3f rounds to nearest, so a bracket of
 * 0.012451171875 ms would print as 0.012, below the bound it states. igpu_lt_ceil_ms3(v) is the
 * smallest multiple of 0.001 ms whose double is >= v, so %.3f prints it exactly and the text read
 * back is never below v (v itself when it is negative, NaN or too large to scale). */
static inline double igpu_lt_ceil_ms3(double v)
{
    if (!(v >= 0.0) || !(v * 1000.0 < 9.0e15)) return v;
    double k = std::ceil(v * 1000.0);
    while (k / 1000.0 < v) k += 1.0;
    while (k >= 1.0 && (k - 1.0) / 1000.0 >= v) k -= 1.0;
    return k / 1000.0;
}

/* The config line of a DLL whose recorder holds a device-clock anchor: the event recorded and
 * waited on once at backend create, the wall clock read right after (anchor_utc_ms), the
 * shortest usable bracket of the tries (anchor_bracket_ms, printed rounded UP to 0.001 ms), which
 * bounds the anchor's host/device offset error, the monotonic clock at the anchor
 * (anchor_steady_ms), so a reader can check two DLLs' anchors with igpu_lt_anchors_comparable(),
 * and the float rounding each stamp may carry (stamp_rounding_ms), which
 * igpu_lt_cross_dll_bound_ms() adds. snprintf-style: size the buffer from the return. */
static inline int igpu_lt_format_config_anchored(char * out, std::size_t cap, const char * dll, double min_ms,
                                                 double anchor_utc_ms, double anchor_bracket_ms,
                                                 double anchor_steady_ms)
{
    return std::snprintf(out, cap,
                         "igpu_launch_timing_config dll=%s enabled=1 min_ms=%.3f device_clock=anchored "
                         "anchor_utc_ms=%.3f anchor_bracket_ms=%.3f anchor_steady_ms=%.3f stamp_rounding_ms=%.6f",
                         dll ? dll : "?", min_ms, anchor_utc_ms, igpu_lt_ceil_ms3(anchor_bracket_ms),
                         anchor_steady_ms, IGPU_LT_ANCHOR_ROUNDING_MS);
}

/* Adversary r2 A1 (pass 3 A1: each wall read pinned). One anchor try reads, in order: the
 * monotonic clock (steady_b0), the wall clock (utc_before), the monotonic clock (steady_b1); records
 * the event and waits for it; then the monotonic clock (steady_a0), the wall clock (utc_after), the
 * monotonic clock (steady_a1). Each wall read's pin (steady_b1 - steady_b0, steady_a1 - steady_a0)
 * must be within IGPU_LT_ANCHOR_STEP_TOL_MS, so a scheduling gap next to a wall read (where a
 * wall-clock step could hide) makes the try unusable; and the wall interval must lie within the
 * monotonic time between the two wall reads ([a0 - b1, a1 - b0], widened by the tolerance), so a step
 * between them makes it unusable too. The event ran in [b1, a0] and the anchor is utc_after, read in
 * [a0, a1], so anchor_utc_ms - event time <= a1 - b1 <= a1 - b0; the bracket is the longer of
 * a1 - b0 and the wall interval. Returns 1 and writes the bracket for a usable try, else 0. */
static inline int igpu_lt_anchor_try_bracket(double steady_b0, double utc_before, double steady_b1,
                                             double steady_a0, double utc_after, double steady_a1,
                                             double * bracket_ms)
{
    const double pin_before = steady_b1 - steady_b0;
    const double pin_after = steady_a1 - steady_a0;
    if (!(pin_before >= 0.0) || !(pin_before <= IGPU_LT_ANCHOR_STEP_TOL_MS)) return 0;
    if (!(pin_after >= 0.0) || !(pin_after <= IGPU_LT_ANCHOR_STEP_TOL_MS)) return 0;
    const double mono_lo = steady_a0 - steady_b1;
    const double mono_hi = steady_a1 - steady_b0;
    const double wall = utc_after - utc_before;
    if (!(mono_lo >= 0.0) || !(wall >= 0.0)) return 0;
    if (!(wall >= mono_lo - IGPU_LT_ANCHOR_STEP_TOL_MS) || !(wall <= mono_hi + IGPU_LT_ANCHOR_STEP_TOL_MS)) return 0;
    if (bracket_ms) *bracket_ms = wall > mono_hi ? wall : mono_hi;
    return 1;
}

/* Adversary r2 pass 3 A2. The two anchors' wall-clock offset minus their monotonic offset: zero
 * unless the wall clock stepped (or slewed) between them. */
static inline double igpu_lt_anchor_skew_ms(double utc_a, double steady_a, double utc_b, double steady_b)
{
    return (utc_a - utc_b) - (steady_a - steady_b);
}

/* Adversary r2 A1. Stamps from two DLLs share one time axis only if no wall-clock step fell between
 * their anchors: the anchors' skew must be within the summed brackets (plus twice the per-read
 * tolerance). 1 when comparable; a reader drops cross-DLL comparisons otherwise. */
static inline int igpu_lt_anchors_comparable(double utc_a, double steady_a, double bracket_a,
                                             double utc_b, double steady_b, double bracket_b)
{
    const double skew = igpu_lt_anchor_skew_ms(utc_a, steady_a, utc_b, steady_b);
    return std::fabs(skew) <= bracket_a + bracket_b + 2.0 * IGPU_LT_ANCHOR_STEP_TOL_MS;
}

/* Adversary r2 pass 3 A2. Two comparable DLLs' stamps of one device instant differ by at most this:
 * each stamp's anchor offset error lies in [0, its bracket]; each stamp's float elapsed time is
 * rounded by at most IGPU_LT_ANCHOR_ROUNDING_MS; and a wall-clock disagreement between the anchors
 * shifts one DLL's axis by at most |skew| plus the two anchors' read pins
 * (2 x IGPU_LT_ANCHOR_STEP_TOL_MS). It takes the device and host clocks to tick at one rate between
 * the two anchors (both are taken at backend create); the bench's stream-0 calibration
 * (CUDA-RECON-PRESENT-OVERLAP-TIMELINE-1 overlap_b.py) checks that on the data. */
static inline double igpu_lt_cross_dll_bound_ms(double bracket_a, double bracket_b, double skew_ms)
{
    return bracket_a + bracket_b + 2.0 * IGPU_LT_ANCHOR_ROUNDING_MS + std::fabs(skew_ms)
           + 2.0 * IGPU_LT_ANCHOR_STEP_TOL_MS;
}

/* Adversary r3 pass 4 A1. The CUDA error codes a recorder-only call (an event create, record or
 * destroy, an elapsed read) can produce from its own arguments or resources, and so the only ones
 * the recorder may clear from the runtime's last-error slot: cudaErrorInvalidValue (1),
 * cudaErrorMemoryAllocation (2), cudaErrorInvalidResourceHandle (400) and cudaErrorNotReady (600).
 * Any other code (an asynchronous launch fault such as cudaErrorLaunchFailure, 719, observed by the
 * recorder's call, or a context error) belongs to the backend and is never cleared. */
static inline int igpu_lt_recorder_clearable(int err)
{
    return err == 1 || err == 2 || err == 400 || err == 600;
}

static inline int igpu_lt_dev_stamp(double anchor_utc_ms, double elapsed_ms, double * out)
{
    if (!(elapsed_ms >= 0.0) || !(elapsed_ms < IGPU_LT_ANCHOR_MAX_AGE_MS)) return 0;
    if (out) *out = anchor_utc_ms + elapsed_ms;
    return 1;
}

#endif /* IGPU_LAUNCH_TIMING_FORMAT_H */
