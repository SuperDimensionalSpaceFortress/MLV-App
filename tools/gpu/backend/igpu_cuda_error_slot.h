/* igpu_cuda_error_slot.h - the DLL-global "last CUDA error" slot behind
 * igpu_cuda_error.h (GPU-FIELD-TELEMETRY-1). Host-only C++: no CUDA call, no
 * synchronisation. Each backend DLL is one translation unit, so the
 * function-local static below is one slot per DLL.
 *
 * igpu_cuda_error_note() runs only inside failure branches (cold path); the
 * success path never touches the slot. The CUDA side passes the name with
 * IGPU_CUDA_ERROR_NOTE(e, site), which calls cudaGetErrorName on the failing
 * code only.
 */
#ifndef IGPU_CUDA_ERROR_SLOT_H
#define IGPU_CUDA_ERROR_SLOT_H

#include "../igpu_cuda_error.h"

#include <chrono>
#include <cstring>
#include <mutex>

namespace igpu_cuda_error_slot
{
struct Slot
{
    std::mutex mutex;
    igpu_cuda_error_t last;
};

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
