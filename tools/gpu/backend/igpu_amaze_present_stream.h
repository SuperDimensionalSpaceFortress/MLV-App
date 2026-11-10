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

namespace igpu_amaze_present
{

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
