/* igpu_cuda_error_slot.h - the DLL-global "last CUDA error" slot behind
 * igpu_cuda_error.h (GPU-FIELD-TELEMETRY-1). Host-only C++: no CUDA call, no
 * synchronisation. Each backend DLL is one translation unit, so the
 * function-local static below is one slot per DLL.
 *
 * igpu_cuda_error_note() runs only inside failure branches (cold path); the
 * success path never touches the slot. The CUDA side passes the name with
 * IGPU_CUDA_ERROR_NOTE(e, site), which calls cudaGetErrorName on the failing
 * code only.
 *
 * r4: each note is also queued on its site's channel (igpu_cuda_error.h) and
 * take() hands it out once, under the same mutex, so a preupload fault and an
 * overlapping render fault both survive with their own stage, and a later
 * failure never re-reads a note already taken.
 *
 * r5: drain() hands out every pending note of a channel at once, so one failing
 * call that notes two codes (a map error, then its cleanup's) reports both and
 * leaves nothing for a later call. A full ring still drops its oldest note, but
 * counts it; drain() reports the drops since the previous drain. pending_word()
 * mirrors each ring's size so a caller can see an empty channel with one relaxed
 * load and no lock.
 */
#ifndef IGPU_CUDA_ERROR_SLOT_H
#define IGPU_CUDA_ERROR_SLOT_H

#include "../igpu_cuda_error.h"

#include <atomic>
#include <chrono>
#include <cstring>
#include <mutex>

namespace igpu_cuda_error_slot
{
/* r4: notes not yet taken, per channel (igpu_recon_take_cuda_error). A full
 * ring drops its oldest note (r5: and counts it). */
constexpr int kPending = IGPU_CUDA_ERROR_PENDING_MAX;
constexpr int kChannels = 2;

struct Ring
{
    igpu_cuda_error_t note[kPending];
    int oldest;
    int size;
    unsigned long long dropped;          /* monotonic: notes this ring dropped */
    unsigned long long dropped_reported; /* the part drain() already reported */
};

/* The pending word is read from the app through a const volatile long *
 * (C++11 checks: the DLL build passes no -std, and MSVC defaults to C++14). */
static_assert(sizeof(std::atomic<long>) == sizeof(long), "pending word layout");
static_assert(ATOMIC_LONG_LOCK_FREE == 2, "pending word must be lock-free");

struct Slot
{
    std::mutex mutex;
    igpu_cuda_error_t last;
    Ring pending[kChannels];
    std::atomic<long> pending_size[kChannels]; /* == pending[c].size, stored under mutex */
};

inline int channel_of(const char * site)
{
    return site && std::strncmp(site, "preupload:", 10) == 0
        ? IGPU_CUDA_ERROR_CHANNEL_PREUPLOAD
        : IGPU_CUDA_ERROR_CHANNEL_RENDER;
}

inline Slot & slot()
{
    static Slot s;
    return s;
}

inline void copy_text(char * out, std::size_t cap, const char * text)
{
    if (!text) text = "";
    std::size_t n = std::strlen(text);
    if (n >= cap) n = cap - 1;
    std::memcpy(out, text, n);
    out[n] = '\0';
}

inline void note(int code, const char * name, const char * site)
{
    if (code == 0) return;
    const double utc_ms = std::chrono::duration<double, std::milli>(
        std::chrono::system_clock::now().time_since_epoch()).count();
    Slot & s = slot();
    std::lock_guard<std::mutex> lock(s.mutex);
    s.last.code = code;
    copy_text(s.last.name, sizeof(s.last.name), name);
    copy_text(s.last.site, sizeof(s.last.site), site);
    s.last.utc_ms = utc_ms;
    s.last.count += 1;
    const int channel = channel_of(site);
    Ring & r = s.pending[channel];
    if (r.size == kPending)
    {
        r.oldest = (r.oldest + 1) % kPending;
        --r.size;
        ++r.dropped;
    }
    r.note[(r.oldest + r.size) % kPending] = s.last;
    ++r.size;
    s.pending_size[channel].store(r.size, std::memory_order_relaxed);
}

inline int take(int channel, igpu_cuda_error_t * out)
{
    if (!out || channel < 0 || channel >= kChannels) return -1;
    Slot & s = slot();
    std::lock_guard<std::mutex> lock(s.mutex);
    Ring & r = s.pending[channel];
    if (r.size == 0)
    {
        *out = igpu_cuda_error_t();
        return 0;
    }
    *out = r.note[r.oldest];
    r.oldest = (r.oldest + 1) % kPending;
    --r.size;
    s.pending_size[channel].store(r.size, std::memory_order_relaxed);
    return 0;
}

/* r5: copies every pending note of `channel`, oldest first, into out[0..n) and
 * empties the channel, in one step under the slot mutex. capacity must hold
 * kPending. *dropped (optional) gets the notes the ring dropped since the
 * previous drain. Returns n (0 = none pending), or -1 for a bad argument. */
inline int drain(int channel, igpu_cuda_error_t * out, int capacity, unsigned long long * dropped)
{
    if (!out || capacity < kPending || channel < 0 || channel >= kChannels) return -1;
    Slot & s = slot();
    std::lock_guard<std::mutex> lock(s.mutex);
    Ring & r = s.pending[channel];
    const int n = r.size;
    for (int i = 0; i < n; ++i) out[i] = r.note[(r.oldest + i) % kPending];
    r.oldest = 0;
    r.size = 0;
    s.pending_size[channel].store(0, std::memory_order_relaxed);
    if (dropped) *dropped = r.dropped - r.dropped_reported;
    r.dropped_reported = r.dropped;
    return n;
}

/* r5: the channel's pending note count, for a lock-free relaxed load (0 =
 * nothing to drain). NULL for an unknown channel. The address is stable for
 * the DLL's lifetime. */
inline const volatile long * pending_word(int channel)
{
    if (channel < 0 || channel >= kChannels) return nullptr;
    return reinterpret_cast<const volatile long *>(&slot().pending_size[channel]);
}

/* r5: notes the channel's ring dropped since the DLL loaded (monotonic). */
inline unsigned long long dropped_total(int channel)
{
    if (channel < 0 || channel >= kChannels) return 0;
    Slot & s = slot();
    std::lock_guard<std::mutex> lock(s.mutex);
    return s.pending[channel].dropped;
}

inline int copy_last(igpu_cuda_error_t * out)
{
    if (!out) return -1;
    Slot & s = slot();
    std::lock_guard<std::mutex> lock(s.mutex);
    *out = s.last;
    return 0;
}
} // namespace igpu_cuda_error_slot

#define IGPU_CUDA_ERROR_NOTE(e, site) \
    igpu_cuda_error_slot::note((int)(e), cudaGetErrorName(e), (site))

#endif /* IGPU_CUDA_ERROR_SLOT_H */
