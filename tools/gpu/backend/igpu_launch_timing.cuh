/* igpu_launch_timing.cuh - per-launch GPU timing for the CUDA backend DLLs
 * (GPU-EVENT153-ROOTCAUSE-2). Env-gated, default OFF; the gate, the statistics
 * and the line formats live in igpu_launch_timing_format.h.
 *
 *   IGPU_LT_LAUNCH(stage, grid, block, stream, kernel<<<...>>>(...));
 *       records a cudaEvent on `stream` before and after the launch. Adds NO
 *       synchronisation: the pair is resolved later by igpu_lt::poll() with
 *       cudaEventQuery, after a sync the backend already does. A launch error
 *       (cudaPeekAtLastError, which does not clear it) is logged at once with
 *       its stage.
 *   cudaError_t e = IGPU_LT_SYNC_CALL(stage, <synchronising runtime call>);
 *       host time around a synchronising call; the call's own result is
 *       returned unchanged, so CK(IGPU_LT_SYNC_CALL(...)) keeps CK outside.
 *       This header waits on the device only at each DLL's first attach() (backend
 *       create), never per frame: one call site, run kAnchorTries times, for
 *       the device-clock anchor (CUDA-RECON-PRESENT-OVERLAP-TIMELINE-1).
 *   igpu_lt::SpanScope s(stage, stream);   igpu_lt::record_span(stage, issue, t0, m0, m1, t1, ms);
 *       kind=span records: an event pair around non-kernel device work, or four
 *       events the caller already waited on. Resolved pairs carry dev_t0_utc_ms /
 *       dev_t1_utc_ms: device times on the host epoch scale (anchor_utc_ms +
 *       cudaEventElapsedTime from the anchor), comparable across both DLLs within
 *       the sum of their anchor_bracket_ms.
 *   IGPU_LT_SYNC_BLOCK(stage, statement);
 *       host time around a statement (e.g. a free of every clip buffer).
 *   igpu_lt::FrameScope f(width, height);   igpu_lt::TileScope t(top, left);
 *       the frame dims / tile origin stamped on every record on this thread.
 *   igpu_lt::attach();   igpu_lt::teardown("destroy");
 *       backend create / the last step of backend destroy: teardown destroys
 *       every event the recorder created, without waiting on the device, except
 *       the one device-clock anchor, which lives with the (leaked) recorder so a
 *       later backend of the same DLL stamps against the same anchor.
 *
 * OFF: every site above tests one cached bool and does nothing else.
 * Each DLL is one translation unit, so each has its own recorder; define
 * IGPU_LT_DLL_NAME before the first include to name it in the lines.
 */
#ifndef IGPU_LAUNCH_TIMING_CUH
#define IGPU_LAUNCH_TIMING_CUH

#include <cuda_runtime.h>

#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <vector>
#ifdef _WIN32
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

#include "igpu_launch_timing_format.h"

#ifndef IGPU_LT_DLL_NAME
#define IGPU_LT_DLL_NAME "probe"
#endif

namespace igpu_lt
{

static const double kSummaryIntervalMs = 5000.0;
static const std::size_t kMaxPending = 200000;
static const unsigned long long kMaxErrorLines = 64;
static const int kAnchorTries = 3;

inline bool enabled()
{
    static const bool on = igpu_lt_parse_enabled(std::getenv(IGPU_LT_ENV_ENABLE)) != 0;
    return on;
}

inline double min_ms()
{
    static const double v = igpu_lt_parse_min_ms(std::getenv(IGPU_LT_ENV_MIN_MS));
    return v;
}

inline double steady_ms()
{
    return std::chrono::duration<double, std::milli>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

inline double utc_ms()
{
#ifdef _WIN32
    FILETIME ft;
    GetSystemTimePreciseAsFileTime(&ft);
    const unsigned long long t = ((unsigned long long)ft.dwHighDateTime << 32) | ft.dwLowDateTime;
    return (double)(t - 116444736000000000ull) / 10000.0;
#else
    return std::chrono::duration<double, std::milli>(
        std::chrono::system_clock::now().time_since_epoch()).count();
#endif
}

inline IgpuLtDim to_dim(const dim3 & d)
{
    IgpuLtDim r = {d.x, d.y, d.z};
    return r;
}

struct Context
{
    int width = 0;
    int height = 0;
    int tile_top = IGPU_LT_NO_TILE;
    int tile_left = IGPU_LT_NO_TILE;
};

inline Context & context()
{
    static thread_local Context c;
    return c;
}

class FrameScope
{
public:
    FrameScope(int width, int height) : active_(enabled())
    {
        if (!active_) return;
        saved_ = context();
        context().width = width;
        context().height = height;
        context().tile_top = IGPU_LT_NO_TILE;
        context().tile_left = IGPU_LT_NO_TILE;
    }
    ~FrameScope() { if (active_) context() = saved_; }
    FrameScope(const FrameScope &) = delete;
    FrameScope & operator=(const FrameScope &) = delete;
private:
    bool active_;
    Context saved_;
};

class TileScope
{
public:
    TileScope(int top, int left) : active_(enabled())
    {
        if (!active_) return;
        saved_top_ = context().tile_top;
        saved_left_ = context().tile_left;
        context().tile_top = top;
        context().tile_left = left;
    }
    ~TileScope()
    {
        if (!active_) return;
        context().tile_top = saved_top_;
        context().tile_left = saved_left_;
    }
    TileScope(const TileScope &) = delete;
    TileScope & operator=(const TileScope &) = delete;
private:
    bool active_;
    int saved_top_ = IGPU_LT_NO_TILE;
    int saved_left_ = IGPU_LT_NO_TILE;
};

struct Pending
{
    IgpuLtRecord rec;
    cudaEvent_t start = nullptr;
    cudaEvent_t stop = nullptr;
};

class Recorder
{
public:
    /* Leaked on purpose: no destructor runs at DLL unload, when the CUDA
     * runtime may already be gone. */
    static Recorder & get()
    {
        static Recorder * r = new Recorder();
        return *r;
    }

    bool begin_kernel(Pending & p, const char * stage, const dim3 & grid, const dim3 & block,
                      cudaStream_t stream, const char * kind = "kernel")
    {
        {
            std::lock_guard<std::mutex> lock(mu_);
            if (!take_event_locked(&p.start)) return false;
            if (!take_event_locked(&p.stop))
            {
                pool_.push_back(p.start);
                p.start = nullptr;
                return false;
            }
            fill_locked(&p.rec, kind, stage);
        }
        p.rec.grid = to_dim(grid);
        p.rec.block = to_dim(block);
        if (cudaEventRecord(p.start, stream) != cudaSuccess)
        {
            std::lock_guard<std::mutex> lock(mu_);
            destroy_events_locked(p);
            return false;
        }
        return true;
    }

    /* peek_launch: a kernel checks its launch with cudaPeekAtLastError; a span (no launch of
     * its own) checks only its stop record, so an earlier unrelated error is not pinned on it. */
    void end_kernel(Pending & p, cudaStream_t stream, bool peek_launch = true)
    {
        const cudaError_t recorded = cudaEventRecord(p.stop, stream);
        const cudaError_t launch = peek_launch ? cudaPeekAtLastError() : cudaSuccess;
        std::lock_guard<std::mutex> lock(mu_);
        if (launch != cudaSuccess || recorded != cudaSuccess)
        {
            const cudaError_t e = launch != cudaSuccess ? launch : recorded;
            p.rec.ms = -1.0;
            p.rec.err = (int)e;
            p.rec.err_name = cudaGetErrorName(e);
            finish_locked(p.rec);
            /* Events touched by a faulted context are not reused. */
            destroy_events_locked(p);
            return;
        }
        if (pending_.size() >= kMaxPending)
        {
            dropped_ += 1;
            discard_events_locked(pending_.front());
            pending_.pop_front();
        }
        pending_.push_back(p);
    }

    void record_sync(const char * stage, double issue_utc_ms, double ms, int err)
    {
        std::lock_guard<std::mutex> lock(mu_);
        IgpuLtRecord rec;
        fill_locked(&rec, "sync", stage);
        rec.utc_ms = issue_utc_ms;
        rec.ms = ms;
        rec.err = err;
        rec.err_name = err != 0 ? cudaGetErrorName((cudaError_t)err) : nullptr;
        finish_locked(rec);
    }

    /* Resolves every launch whose stop event has completed; never blocks. */
    void poll(const char * at)
    {
        std::lock_guard<std::mutex> lock(mu_);
        resolve_locked();
        const double now = steady_ms();
        if (at || now - last_summary_ms_ >= kSummaryIntervalMs)
        {
            write_summaries_locked(at ? at : "periodic");
            last_summary_ms_ = now;
        }
    }

    /* A span the caller bounded with events it already recorded and already waited on (the
     * recon run: ev_start, ev_after_up, ev_after_kernel, ev_after_dl, after the run's own
     * wait on ev_after_dl). Reads elapsed times only; never waits. utc_ms is the caller's host
     * clock at issue (adversary r2 pass 2 A3), not the time this record is written. */
    void record_span(const char * stage, double issue_utc, cudaEvent_t t0, cudaEvent_t mark0,
                     cudaEvent_t mark1, cudaEvent_t t1, double ms)
    {
        std::lock_guard<std::mutex> lock(mu_);
        IgpuLtRecord rec;
        fill_locked(&rec, "span", stage);
        rec.utc_ms = issue_utc;
        rec.ms = ms;
        stamp_locked(&rec, t0, t1);
        if (rec.has_dev && dev_utc_locked(mark0, &rec.dev_mark_utc_ms[0])
            && dev_utc_locked(mark1, &rec.dev_mark_utc_ms[1]))
            rec.dev_marks = 2;
        finish_locked(rec);
    }

    void attach()
    {
        std::lock_guard<std::mutex> lock(mu_);
        live_backends_ += 1;
        if (!anchor_tried_)
        {
            anchor_tried_ = true;
            establish_anchor_locked();
            if (anchor_ || !config_written_) write_config_locked();
        }
    }

    /* Backend teardown, called after the destructor's own completion points:
     * resolves what has completed (never blocks), writes the summaries, and
     * destroys the idle pool. The last live backend also destroys the pairs
     * still pending; cudaEventDestroy on an unfinished event returns at once
     * and the runtime frees it when the device reaches it. */
    void teardown(const char * at)
    {
        std::lock_guard<std::mutex> lock(mu_);
        resolve_locked();
        if (live_backends_ > 0) live_backends_ -= 1;
        for (cudaEvent_t ev : pool_) cudaEventDestroy(ev);
        pool_.clear();
        if (live_backends_ == 0)
        {
            abandoned_ += pending_.size();
            for (Pending & p : pending_) destroy_events_locked(p);
            pending_.clear();
        }
        write_summaries_locked(at);
        last_summary_ms_ = steady_ms();
    }

private:
    void resolve_locked()
    {
        for (auto it = pending_.begin(); it != pending_.end();)
        {
            const cudaError_t q = cudaEventQuery(it->stop);
            if (q == cudaErrorNotReady)
            {
                ++it;
                continue;
            }
            if (q == cudaSuccess)
            {
                float elapsed = 0.0f;
                const cudaError_t e = cudaEventElapsedTime(&elapsed, it->start, it->stop);
                if (e == cudaSuccess)
                {
                    it->rec.ms = (double)elapsed;
                    stamp_locked(&it->rec, it->start, it->stop);
                    pool_.push_back(it->start);
                    pool_.push_back(it->stop);
                    it->start = it->stop = nullptr;
                }
                else
                {
                    it->rec.ms = -1.0;
                    it->rec.err = (int)e;
                    it->rec.err_name = cudaGetErrorName(e);
                }
            }
            else
            {
                it->rec.ms = -1.0;
                it->rec.err = (int)q;
                it->rec.err_name = cudaGetErrorName(q);
            }
            finish_locked(it->rec);
            destroy_events_locked(*it); /* no-op when the pair went back to the pool */
            it = pending_.erase(it);
        }
    }

    /* The config line is written by the first attach() (after the anchor, so it can name it),
     * or before the first other line if one comes first. */
    Recorder() : min_ms_(min_ms()), last_summary_ms_(steady_ms()) {}

    /* Adversary r2 A3: every line is sized from its formatter's return, so a long value (e.g.
     * MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS=1e308) never truncates the fields after it. */
    template <typename F>
    static void write_line(F && format)
    {
        char small[512];
        const int need = format(small, sizeof(small));
        if (need < 0) return;
        if ((std::size_t)need < sizeof(small))
        {
            std::fprintf(stderr, "%s\n", small);
            return;
        }
        std::vector<char> big((std::size_t)need + 1);
        format(big.data(), big.size());
        std::fprintf(stderr, "%s\n", big.data());
    }

    void write_config_locked()
    {
        if (anchor_)
            write_line([&](char * out, std::size_t cap) {
                return igpu_lt_format_config_anchored(out, cap, IGPU_LT_DLL_NAME, min_ms_, anchor_utc_ms_,
                                                      anchor_bracket_ms_, anchor_steady_ms_);
            });
        else
            write_line([&](char * out, std::size_t cap) {
                return igpu_lt_format_config(out, cap, IGPU_LT_DLL_NAME, min_ms_);
            });
        config_written_ = true;
    }

    void ensure_config_locked()
    {
        if (!config_written_) write_config_locked();
    }

    /* CUDA-RECON-PRESENT-OVERLAP-TIMELINE-1: the device-clock anchor. Called once per DLL, from
     * the first attach() (backend create), never on a per-frame path. Each try records an event
     * on stream 0, waits for it, and reads the clocks right after; the event's device time lies
     * inside the monotonic bracket [before the record, after the wait], which bounds the anchor's
     * host/device offset error. A try across which the wall clock stepped is dropped
     * (igpu_lt_anchor_try_bracket); the shortest usable bracket of kAnchorTries is kept. This is
     * the recorder's only synchronising call; with the timer OFF it is never reached. */
    void establish_anchor_locked()
    {
        const cudaError_t prior = cudaPeekAtLastError();
        for (int i = 0; i < kAnchorTries; ++i)
        {
            cudaEvent_t ev = nullptr;
            if (cudaEventCreate(&ev) != cudaSuccess) break;
            const double utc_before = utc_ms();
            const double steady_before = steady_ms();
            const bool ok = cudaEventRecord(ev, 0) == cudaSuccess && cudaEventSynchronize(ev) == cudaSuccess;
            const double steady_after = steady_ms();
            const double utc_after = utc_ms();
            double bracket = 0.0;
            if (ok && igpu_lt_anchor_try_bracket(utc_before, utc_after, steady_before, steady_after, &bracket)
                && (!anchor_ || bracket < anchor_bracket_ms_))
            {
                if (anchor_) cudaEventDestroy(anchor_);
                anchor_ = ev;
                anchor_utc_ms_ = utc_after;
                anchor_steady_ms_ = steady_after;
                anchor_bracket_ms_ = bracket;
            }
            else
            {
                cudaEventDestroy(ev);
            }
        }
        drop_own_error(prior);
    }

    /* Adversary r2 pass 2 A2: a recorder-only CUDA failure (the anchor's create/record/wait, an
     * event-pool create, an elapsed read) must not stay pending for the backend's own
     * CK(cudaGetLastError()). The last error is cleared only when none was pending before the
     * recorder's call (`prior`), so a backend's own error is never lost either. */
    static void drop_own_error(cudaError_t prior)
    {
        if (prior == cudaSuccess) (void)cudaGetLastError();
    }

    /* The device time of a completed event on the host's Unix-epoch ms scale. A failed elapsed
     * read is cleared from the runtime's last error only if no earlier error was pending, so a
     * backend's own CK(cudaGetLastError()) never sees this recorder's failure, nor loses its own. */
    bool dev_utc_locked(cudaEvent_t ev, double * out)
    {
        if (!anchor_ || !ev) return false;
        const cudaError_t prior = cudaPeekAtLastError();
        float elapsed = 0.0f;
        if (cudaEventElapsedTime(&elapsed, anchor_, ev) != cudaSuccess)
        {
            drop_own_error(prior);
            return false;
        }
        /* Adversary r2 A2: no stamp past the float-precision age limit. */
        return igpu_lt_dev_stamp(anchor_utc_ms_, (double)elapsed, out) != 0;
    }

    void stamp_locked(IgpuLtRecord * rec, cudaEvent_t start, cudaEvent_t stop)
    {
        rec->has_dev = dev_utc_locked(start, &rec->dev_t0_utc_ms) && dev_utc_locked(stop, &rec->dev_t1_utc_ms);
    }

    bool take_event_locked(cudaEvent_t * ev)
    {
        if (!pool_.empty())
        {
            *ev = pool_.back();
            pool_.pop_back();
            return true;
        }
        const cudaError_t prior = cudaPeekAtLastError();
        if (cudaEventCreate(ev) == cudaSuccess) return true;
        drop_own_error(prior);
        return false;
    }

    void discard_events_locked(Pending & p)
    {
        if (p.start) pool_.push_back(p.start);
        if (p.stop) pool_.push_back(p.stop);
        p.start = p.stop = nullptr;
    }

    /* Error branches and teardown: the handles are destroyed, never just dropped. */
    void destroy_events_locked(Pending & p)
    {
        if (p.start) cudaEventDestroy(p.start);
        if (p.stop) cudaEventDestroy(p.stop);
        p.start = p.stop = nullptr;
    }

    void fill_locked(IgpuLtRecord * rec, const char * kind, const char * stage)
    {
        std::memset(rec, 0, sizeof(*rec));
        const Context & c = context();
        rec->dll = IGPU_LT_DLL_NAME;
        rec->kind = kind;
        rec->stage = stage;
        rec->seq = ++seq_;
        rec->utc_ms = utc_ms();
        rec->width = c.width;
        rec->height = c.height;
        rec->tile_top = c.tile_top;
        rec->tile_left = c.tile_left;
        rec->ms = -1.0;
    }

    void finish_locked(const IgpuLtRecord & rec)
    {
        ensure_config_locked();
        const std::string key = std::string(rec.kind) + '\x1f' + rec.stage;
        auto found = stats_.find(key);
        const int first = found == stats_.end();
        IgpuLtStats & s = stats_[key];
        igpu_lt_stats_add(&s, rec, min_ms_);
        if (!igpu_lt_should_log_record(rec, min_ms_, first)) return;
        if (rec.err != 0)
        {
            error_lines_ += 1;
            if (error_lines_ > kMaxErrorLines) return;
        }
        write_line([&](char * out, std::size_t cap) { return igpu_lt_format_record(out, cap, rec); });
    }

    void write_summaries_locked(const char * at)
    {
        ensure_config_locked();
        for (const auto & kv : stats_)
        {
            const std::size_t sep = kv.first.find('\x1f');
            const std::string kind = kv.first.substr(0, sep);
            const std::string stage = kv.first.substr(sep + 1);
            write_line([&](char * out, std::size_t cap) {
                return igpu_lt_format_summary(out, cap, IGPU_LT_DLL_NAME, kind.c_str(), stage.c_str(), at,
                                              kv.second);
            });
        }
        if (dropped_ || abandoned_ || error_lines_ > kMaxErrorLines)
        {
            std::fprintf(stderr,
                         "igpu_launch_timing_drops dll=%s at=%s pending=%zu dropped=%llu abandoned=%llu "
                         "error_lines_suppressed=%llu\n",
                         IGPU_LT_DLL_NAME, at, pending_.size(), dropped_, abandoned_,
                         error_lines_ > kMaxErrorLines ? error_lines_ - kMaxErrorLines : 0ull);
        }
    }

    std::mutex mu_;
    std::vector<cudaEvent_t> pool_;
    std::deque<Pending> pending_;
    std::map<std::string, IgpuLtStats> stats_;
    unsigned long long seq_ = 0;
    unsigned long long dropped_ = 0;
    unsigned long long abandoned_ = 0;
    unsigned long long error_lines_ = 0;
    unsigned long long live_backends_ = 0;
    const double min_ms_;
    double last_summary_ms_;
    bool config_written_ = false;
    bool anchor_tried_ = false;
    /* Created once and kept for the recorder's (leaked) lifetime, so every later backend of
     * this DLL stamps against the same anchor: the one event teardown() does not destroy. */
    cudaEvent_t anchor_ = nullptr;
    double anchor_utc_ms_ = 0.0;
    double anchor_steady_ms_ = 0.0;
    double anchor_bracket_ms_ = 0.0;
};

class LaunchScope
{
public:
    LaunchScope(const char * stage, const dim3 & grid, const dim3 & block, cudaStream_t stream)
        : active_(false), stream_(stream)
    {
        if (!enabled()) return;
        active_ = Recorder::get().begin_kernel(pending_, stage, grid, block, stream);
    }
    ~LaunchScope()
    {
        if (active_) Recorder::get().end_kernel(pending_, stream_);
    }
    LaunchScope(const LaunchScope &) = delete;
    LaunchScope & operator=(const LaunchScope &) = delete;
private:
    bool active_;
    cudaStream_t stream_;
    Pending pending_;
};

/* A device span on `stream` with no launch of its own (e.g. the AMaZE GL map/copy/unmap
 * handoff): an event pair like IGPU_LT_LAUNCH, recorded as kind=span, resolved by poll().
 * end() closes it early; the destructor closes it on any other exit. Adds no synchronisation. */
class SpanScope
{
public:
    SpanScope(const char * stage, cudaStream_t stream) : active_(false), stream_(stream)
    {
        if (!enabled()) return;
        active_ = Recorder::get().begin_kernel(pending_, stage, dim3(0, 0, 0), dim3(0, 0, 0), stream, "span");
    }
    ~SpanScope() { end(); }
    void end()
    {
        if (!active_) return;
        active_ = false;
        Recorder::get().end_kernel(pending_, stream_, false);
    }
    SpanScope(const SpanScope &) = delete;
    SpanScope & operator=(const SpanScope &) = delete;
private:
    bool active_;
    cudaStream_t stream_;
    Pending pending_;
};

class SyncScope
{
public:
    explicit SyncScope(const char * stage) : active_(enabled()), stage_(stage)
    {
        if (!active_) return;
        issue_utc_ms_ = utc_ms();
        start_ms_ = steady_ms();
    }
    ~SyncScope()
    {
        if (active_) Recorder::get().record_sync(stage_, issue_utc_ms_, steady_ms() - start_ms_, err_);
    }
    void set_error(cudaError_t e) { err_ = (int)e; }
    SyncScope(const SyncScope &) = delete;
    SyncScope & operator=(const SyncScope &) = delete;
private:
    bool active_;
    const char * stage_;
    double issue_utc_ms_ = 0.0;
    double start_ms_ = 0.0;
    int err_ = 0;
};

template <typename F>
inline cudaError_t timed_call(const char * stage, F && call)
{
    if (!enabled()) return call();
    SyncScope scope(stage);
    const cudaError_t rc = call();
    scope.set_error(rc);
    return rc;
}

/* Resolve completed launches; `at` non-null forces the summaries out
 * (e.g. "destroy"). Cheap and non-blocking; a no-op when OFF. */
inline void poll(const char * at = nullptr)
{
    if (!enabled()) return;
    Recorder::get().poll(at);
}

/* Pair with teardown(): attach() when a backend is created, teardown() as the
 * last step of its destructor, after the destructor's existing completion
 * points. Adds no synchronisation; a no-op when OFF. */
inline void attach()
{
    if (!enabled()) return;
    Recorder::get().attach();
}

inline void teardown(const char * at)
{
    if (!enabled()) return;
    Recorder::get().teardown(at);
}

/* The host wall clock to pass as a later record_span's issue time; 0 when OFF. */
inline double issue_utc_ms()
{
    if (!enabled()) return 0.0;
    return utc_ms();
}

/* One kind=span record over four events the caller has already waited on (t0, two inner
 * marks, t1), with `ms` the caller's own elapsed(t0, t1) and `issue_utc` its issue_utc_ms()
 * from just before t0 was recorded. Never waits; a no-op when OFF. */
inline void record_span(const char * stage, double issue_utc, cudaEvent_t t0, cudaEvent_t mark0,
                        cudaEvent_t mark1, cudaEvent_t t1, double ms)
{
    if (!enabled()) return;
    Recorder::get().record_span(stage, issue_utc, t0, mark0, mark1, t1, ms);
}

} // namespace igpu_lt

#define IGPU_LT_LAUNCH(stage, grid, block, stream, ...)                                      \
    do                                                                                       \
    {                                                                                        \
        ::igpu_lt::LaunchScope igpu_lt_launch_scope_((stage), dim3(grid), dim3(block), (stream)); \
        __VA_ARGS__;                                                                         \
    } while (0)

#define IGPU_LT_SYNC_CALL(stage, ...) ::igpu_lt::timed_call((stage), [&]() -> cudaError_t { return (__VA_ARGS__); })

#define IGPU_LT_SYNC_BLOCK(stage, ...)                          \
    do                                                          \
    {                                                           \
        ::igpu_lt::SyncScope igpu_lt_sync_scope_((stage));      \
        __VA_ARGS__;                                            \
    } while (0)

#endif /* IGPU_LAUNCH_TIMING_CUH */
