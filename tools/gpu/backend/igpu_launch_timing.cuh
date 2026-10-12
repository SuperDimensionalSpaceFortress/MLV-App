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
 *       This header itself never synchronises.
 *   IGPU_LT_SYNC_BLOCK(stage, statement);
 *       host time around a statement (e.g. a free of every clip buffer).
 *   igpu_lt::FrameScope f(width, height);   igpu_lt::TileScope t(top, left);
 *       the frame dims / tile origin stamped on every record on this thread.
 *   igpu_lt::attach();   igpu_lt::teardown("destroy");
 *       backend create / the last step of backend destroy: teardown destroys
 *       every event the recorder created, without waiting on the device.
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
                      cudaStream_t stream)
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
            fill_locked(&p.rec, "kernel", stage);
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

    void end_kernel(Pending & p, cudaStream_t stream)
    {
        const cudaError_t recorded = cudaEventRecord(p.stop, stream);
        const cudaError_t launch = cudaPeekAtLastError();
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

    void attach()
    {
        std::lock_guard<std::mutex> lock(mu_);
        live_backends_ += 1;
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

    Recorder() : min_ms_(min_ms()), last_summary_ms_(steady_ms())
    {
        char line[160];
        igpu_lt_format_config(line, sizeof(line), IGPU_LT_DLL_NAME, min_ms_);
        std::fprintf(stderr, "%s\n", line);
    }

    bool take_event_locked(cudaEvent_t * ev)
    {
        if (!pool_.empty())
        {
            *ev = pool_.back();
            pool_.pop_back();
            return true;
        }
        return cudaEventCreate(ev) == cudaSuccess;
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
        char line[512];
        igpu_lt_format_record(line, sizeof(line), rec);
        std::fprintf(stderr, "%s\n", line);
    }

    void write_summaries_locked(const char * at)
    {
        char line[640];
        for (const auto & kv : stats_)
        {
            const std::size_t sep = kv.first.find('\x1f');
            const std::string kind = kv.first.substr(0, sep);
            const std::string stage = kv.first.substr(sep + 1);
            igpu_lt_format_summary(line, sizeof(line), IGPU_LT_DLL_NAME, kind.c_str(), stage.c_str(), at,
                                   kv.second);
            std::fprintf(stderr, "%s\n", line);
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
