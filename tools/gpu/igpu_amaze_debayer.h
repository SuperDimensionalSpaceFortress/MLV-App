/* igpu_amaze_debayer.h - C ABI boundary for the CUDA AMaZE debayer backend.
 *
 * MLVApp is built with Qt/MinGW, while CUDA is built with MSVC/nvcc. Keep this
 * boundary plain C so the app can runtime-load the backend DLL with
 * LoadLibrary/GetProcAddress or QLibrary::resolve.
 *
 * DATA CONTRACT
 *   Input  : float[width*height] RGGB raw frame after the same WB/CA prep the
 *            CPU AMaZE core receives.
 *   Output : uint16_t[width*height*3] interleaved RGB, before WB undo, or a
 *            GL_TEXTURE_2D with GL_RGBA16 storage for lower-stack parity
 *            checks. The production playback GL handoff must use the explicit
 *            post-WB-undo texture symbol so displayed pixels match the current
 *            CPU-readback path.
 */
#ifndef IGPU_AMAZE_DEBAYER_H
#define IGPU_AMAZE_DEBAYER_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define IGPU_AMAZE_DEBAYER_ABI_VERSION 1

typedef struct igpu_amaze_debayer_backend igpu_amaze_debayer_backend;

typedef struct {
    double upload_ms;
    double kernel_ms;
    double download_ms;
    double total_ms;
} igpu_amaze_debayer_timing_t;

igpu_amaze_debayer_backend * igpu_amaze_debayer_create(const char * backend_name);
void                         igpu_amaze_debayer_destroy(igpu_amaze_debayer_backend * backend);
int                          igpu_amaze_debayer_abi_version(igpu_amaze_debayer_backend * backend);
const char *                 igpu_amaze_debayer_describe(igpu_amaze_debayer_backend * backend);

int igpu_amaze_debayer_run(igpu_amaze_debayer_backend * backend,
                           const float * in_raw_float,
                           uint16_t * out_rgb16,
                           int width,
                           int height);

int igpu_amaze_debayer_run_gl_texture(igpu_amaze_debayer_backend * backend,
                                      const float * in_raw_float,
                                      unsigned int gl_texture,
                                      int width,
                                      int height);

int igpu_amaze_debayer_run_post_wb_gl_texture(igpu_amaze_debayer_backend * backend,
                                              const float * in_raw_float,
                                              unsigned int gl_texture,
                                              int width,
                                              int height,
                                              int black_level,
                                              double wb_multiplier_r,
                                              double wb_multiplier_g,
                                              double wb_multiplier_b);

/* Optional live-playback extension: consume an already-reconstructed GL_R16
 * Bayer texture and write a post-WB GL_RGBA16 texture without CPU readback.
 * Older DLLs may omit this symbol; callers must fail closed to the existing
 * CPU/readback or shader-side fallback path.
 */
int igpu_amaze_debayer_run_post_wb_gl_texture_from_r16_gl_texture(
    igpu_amaze_debayer_backend * backend,
    unsigned int in_r16_gl_texture,
    unsigned int out_rgba16_gl_texture,
    int width,
    int height,
    int black_level,
    double wb_multiplier_r,
    double wb_multiplier_g,
    double wb_multiplier_b);

/* Optional live-playback direct-device extension: consume a CUDA device pointer
 * to reconstructed Bayer16 from the recon backend and write post-WB GL_RGBA16.
 * Older DLLs may omit this symbol; callers must fail closed to the R16 texture
 * bridge or CPU/readback fallback.
 */
int igpu_amaze_debayer_run_post_wb_gl_texture_from_device_bayer16(
    igpu_amaze_debayer_backend * backend,
    const uint16_t * device_bayer16,
    unsigned int out_rgba16_gl_texture,
    int width,
    int height,
    int black_level,
    double wb_multiplier_r,
    double wb_multiplier_g,
    double wb_multiplier_b);

/* Optional live-playback cache reset. Newer CUDA backends may keep CUDA/GL
 * image registrations alive across frames; callers that own the GL textures
 * should invoke this before deleting or reallocating those textures. Older DLLs
 * may omit this symbol.
 */
int igpu_amaze_debayer_reset_live_gl_texture_resources(
    igpu_amaze_debayer_backend * backend);

/* Optional reduced-texture extension (PLAYBACK-CUDA-HONOUR-SCALE-1 r3). While enable
 * is 1 (sticky), every run applies a horizontal [1,2,1]/4 Nyquist zero to the AMaZE
 * RGB16 output, per channel with mirrored edge columns, before the WB-undo pack; the
 * C reference is debayer_reduced_hnyquist121_rgb16 (src/debayer/debayer.h). Callers
 * set 1 only for a reduced (x2/x4) recon texture and 0 otherwise. Returns 0, or -1
 * for a null backend or an enable other than 0/1. Older DLLs omit this symbol; a
 * caller must then refuse a reduced present rather than show it unfiltered.
 */
int igpu_amaze_debayer_set_reduced_hnyquist(igpu_amaze_debayer_backend * backend,
                                            int enable);

int igpu_amaze_debayer_last_timing(igpu_amaze_debayer_backend * backend,
                                   igpu_amaze_debayer_timing_t * timing);

/* Optional probe extension (PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0). arm_probe(calls) arms the next
 * `calls` live runs (the two *_from_* symbols above). An armed run reads the SM clock in-process right before its
 * tile batch and right after its drain (a one-thread kernel timing clock64 against %globaltimer for ~50 us; never
 * NVML), both outside kernel_ms, and times the batch on the device (a start event on tile stream 0, every active
 * tile stream joined into stream 0, an end event). last_probe returns the last live run's result; valid is 0 when
 * that run was not armed or a probe step failed. Both return 0, or -1 for a null argument (or calls < 0). Older
 * DLLs omit both symbols; callers then print NA. */
typedef struct {
    int valid;
    double sm_mhz_before;
    double sm_mhz_after;
    double device_ms;
} igpu_amaze_probe_t;

int igpu_amaze_debayer_arm_probe(igpu_amaze_debayer_backend * backend, int calls);
int igpu_amaze_debayer_last_probe(igpu_amaze_debayer_backend * backend, igpu_amaze_probe_t * out);

/* Optional clock-kick extension (Row K). begin launches ONE <<<1, 32>>> kernel on a backend-owned lowest-priority
 * non-blocking stream that spins on %globaltimer until max_ms or a host-mapped stop flag, writing a (%globaltimer,
 * clock64) sample about every 5 ms plus a closing one to mapped pinned memory. It never touches the live buffers or
 * the tile streams. A live run raises the stop flag right after its tile drain, so the kick never overlaps a CUDA-GL
 * handoff. begin returns 0 (launched), 1 (a kick is still in flight), -1 (bad argument: max_ms outside 1..2000) or
 * -2 (a CUDA call failed; its code is noted). end raises the stop flag and collects the samples once the kernel
 * says it is done, waiting at most ~2 ms; it makes no CUDA call, so it is safe after a device loss. end returns 0
 * (collected, state 0), 1 (still running: state 1, collect at a later end) or 2 (no kick: state 2); -1 for a null
 * argument. Older DLLs omit both symbols. */
#define IGPU_CLOCK_KICK_MAX_SAMPLES 96

typedef struct {
    int state;          /* 0 collected, 1 pending, 2 none */
    int samples;        /* valid entries in sample_ns / sample_clk, the closing sample last */
    int stopped_by;     /* 0 deadline, 1 end(), 2 a live run's drain */
    double ran_ms;      /* the kernel's own %globaltimer start -> end */
    unsigned long long sample_ns[IGPU_CLOCK_KICK_MAX_SAMPLES];
    long long sample_clk[IGPU_CLOCK_KICK_MAX_SAMPLES];
} igpu_clock_kick_report_t;

int igpu_amaze_debayer_clock_kick_begin(igpu_amaze_debayer_backend * backend, int max_ms);
int igpu_amaze_debayer_clock_kick_end(igpu_amaze_debayer_backend * backend, igpu_clock_kick_report_t * out);

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* IGPU_AMAZE_DEBAYER_H */
