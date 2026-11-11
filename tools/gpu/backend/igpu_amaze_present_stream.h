/* igpu_amaze_present_stream.h - HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r3 (the stream-0 clause).
 *
 * The GUI present path of the CUDA AMaZE backend (the two live GL entries,
 * igpu_amaze_debayer_run_post_wb_gl_texture_from_device_bayer16 and ..._from_r16_gl_texture) runs its GL
 * handoff -- the reduced H-Nyquist filter, the WB-undo pack, cudaGraphicsMapResources, the
 * device-to-array copy and cudaGraphicsUnmapResources -- on a cudaStreamNonBlocking stream the backend
 * owns, instead of the legacy default stream (stream 0), where the worker's recon kernels also run.
 *
 * Kill switch: MLVAPP_CUDA_AMAZE_PRESENT_STREAM. Unset or empty = ON (the new path). A value starting
 * with 0, f, F, n or N, or with "of" in either case ("off", "OFF"), = OFF: every call goes back to
 * stream 0 exactly as before r3. Any other value = ON. Same spelling rules as
 * MLVAPP_CUDA_AMAZE_LIVE_DIRECT_RGBA_STORE.
 *
 * Pure C++ (no CUDA types), so tests/console checks the parse without a GPU.
 */
#ifndef IGPU_AMAZE_PRESENT_STREAM_H
#define IGPU_AMAZE_PRESENT_STREAM_H

#define IGPU_AMAZE_PRESENT_STREAM_ENV "MLVAPP_CUDA_AMAZE_PRESENT_STREAM"

#include <cstddef>

namespace igpu_amaze_present
{

/* r4 (795-R3-I1): the best-effort tile drain at the end of run_frame_to_device_rgb16_bayer16_post_wb_batched.
 * Calls sync(streams[slot]) for EVERY slot in [0, active), whatever an earlier call returned, and returns the
 * first code that is not `success`: `firstRc` itself when it already failed (the present-ordering code), else
 * the first failing sync. Nothing here throws, so one failed drain cannot skip the later ones; the caller
 * raises the returned code once, after the loop. A template over the error and stream types (no CUDA types),
 * so tests/console drives it with a host-side stub sync that fails on a chosen stream. */
template <typename Rc, typename Streams, typename Sync>
Rc drain_every_stream(const Streams & streams, std::size_t active, Rc firstRc, Rc success, Sync && sync)
{
    for (std::size_t slot = 0; slot < active; ++slot)
    {
        const Rc rc = sync(streams[slot]);
        if (firstRc == success && rc != success) firstRc = rc;
    }
    return firstRc;
}

/* true = the GUI present path uses its own non-blocking stream; false = legacy stream 0. */
inline bool present_stream_enabled(const char * value)
{
    if (!value || !*value) return true;

    const char first = value[0];
    if (first == '0' || first == 'f' || first == 'F' || first == 'n' || first == 'N') return false;
    if ((first == 'o' || first == 'O') && (value[1] == 'f' || value[1] == 'F')) return false;
    return true;
}

} // namespace igpu_amaze_present

#endif /* IGPU_AMAZE_PRESENT_STREAM_H */
