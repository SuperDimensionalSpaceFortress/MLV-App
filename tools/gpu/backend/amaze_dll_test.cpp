/* amaze_dll_test.cpp - LoadLibrary harness for the CUDA AMaZE DLL.
 *
 * Default mode verifies the existing host RGB16 output path is callable through
 * the C ABI. --gl-texture additionally creates a hidden WGL GL_RGBA16 texture,
 * runs the additive texture-output symbol, then reads the texture back only for
 * harness comparison against the host RGB16 output. The backend call itself
 * performs no device-to-host copy in GL mode.
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>
#include <windows.h>
#include <GL/gl.h>

#include "igpu_amaze_debayer.h"

extern "C" {
__declspec(dllexport) unsigned long NvOptimusEnablement = 0x00000001;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}

#ifndef GL_CLAMP_TO_EDGE
#define GL_CLAMP_TO_EDGE 0x812F
#endif
#ifndef GL_RGBA16
#define GL_RGBA16 0x805B
#endif

typedef igpu_amaze_debayer_backend * (*pfn_create)(const char *);
typedef void (*pfn_destroy)(igpu_amaze_debayer_backend *);
typedef int (*pfn_abi_version)(igpu_amaze_debayer_backend *);
typedef const char * (*pfn_describe)(igpu_amaze_debayer_backend *);
typedef int (*pfn_run)(igpu_amaze_debayer_backend *,
                       const float *,
                       uint16_t *,
                       int,
                       int);
typedef int (*pfn_run_gl_texture)(igpu_amaze_debayer_backend *,
                                  const float *,
                                  unsigned int,
                                  int,
                                  int);
typedef int (*pfn_run_post_wb_gl_texture)(igpu_amaze_debayer_backend *,
                                          const float *,
                                          unsigned int,
                                          int,
                                          int,
                                          int,
                                          double,
                                          double,
                                          double);
typedef int (*pfn_last_timing)(igpu_amaze_debayer_backend *,
                               igpu_amaze_debayer_timing_t *);
typedef int (*pfn_set_reduced_hnyquist)(igpu_amaze_debayer_backend *, int);
/* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 (--clock-kick) */
typedef int (*pfn_run_post_wb_from_r16)(igpu_amaze_debayer_backend *, unsigned int, unsigned int,
                                        int, int, int, double, double, double);
typedef int (*pfn_arm_probe)(igpu_amaze_debayer_backend *, int);
typedef int (*pfn_last_probe)(igpu_amaze_debayer_backend *, igpu_amaze_probe_t *);
typedef int (*pfn_clock_kick_begin)(igpu_amaze_debayer_backend *, int);
typedef int (*pfn_clock_kick_end)(igpu_amaze_debayer_backend *, igpu_clock_kick_report_t *);

/* PLAYBACK-CUDA-HONOUR-SCALE-1 r3 c1: a verbatim copy of
 * debayer_reduced_hnyquist121_rgb16 (src/debayer/debayer.c), which this MSVC
 * harness cannot link. --dump-dir writes both DLL outputs so the opt-in pipeline
 * test GpuDualIsoPreviewScale.C1HnyquistDllParity re-checks them against the real
 * C function. */
static void hnyquist121_reference(uint16_t * out, const uint16_t * in, int w, int h)
{
    for (int y = 0; y < h; ++y)
    {
        const uint16_t * src = in + (size_t)y * (size_t)w * 3u;
        uint16_t * dst = out + (size_t)y * (size_t)w * 3u;
        for (int x = 0; x < w; ++x)
        {
            const int xl = (x > 0) ? x - 1 : ((w > 1) ? x + 1 : x);
            const int xr = (x < w - 1) ? x + 1 : ((w > 1) ? x - 1 : x);
            for (int c = 0; c < 3; ++c)
            {
                const uint32_t l = src[(size_t)xl * 3u + (size_t)c];
                const uint32_t m = src[(size_t)x * 3u + (size_t)c];
                const uint32_t r = src[(size_t)xr * 3u + (size_t)c];
                dst[(size_t)x * 3u + (size_t)c] = (uint16_t)((l + 2u * m + r + 2u) >> 2);
            }
        }
    }
}

static bool write_file(const std::string & path, const void * data, size_t bytes)
{
    FILE * f = std::fopen(path.c_str(), "wb");
    if (!f) return false;
    const bool ok = std::fwrite(data, 1, bytes, f) == bytes;
    std::fclose(f);
    return ok;
}

static bool read_u16_file(const std::string & path, std::vector<uint16_t> * out)
{
    FILE * f = std::fopen(path.c_str(), "rb");
    if (!f) return false;
    const size_t got = std::fread(out->data(), sizeof(uint16_t), out->size(), f);
    std::fclose(f);
    return got == out->size();
}

struct HiddenGlContext
{
    HWND hwnd;
    HDC hdc;
    HGLRC hglrc;
};

static bool create_hidden_gl_context(HiddenGlContext * gl)
{
    if (!gl) return false;
    std::memset(gl, 0, sizeof(*gl));

    WNDCLASSA wc;
    std::memset(&wc, 0, sizeof(wc));
    wc.style = CS_OWNDC;
    wc.lpfnWndProc = DefWindowProcA;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "MLVAppAmazeDllTestHiddenGl";
    RegisterClassA(&wc);

    gl->hwnd = CreateWindowA(wc.lpszClassName,
                             "MLVApp AMaZE DLL GL texture test",
                             WS_OVERLAPPEDWINDOW,
                             0,
                             0,
                             64,
                             64,
                             NULL,
                             NULL,
                             wc.hInstance,
                             NULL);
    if (!gl->hwnd)
    {
        std::fprintf(stderr,
                     "[amaze_dll_test] CreateWindowA failed (err=%lu)\n",
                     (unsigned long)GetLastError());
        return false;
    }

    gl->hdc = GetDC(gl->hwnd);
    if (!gl->hdc)
    {
        std::fprintf(stderr, "[amaze_dll_test] GetDC failed\n");
        DestroyWindow(gl->hwnd);
        std::memset(gl, 0, sizeof(*gl));
        return false;
    }

    PIXELFORMATDESCRIPTOR pfd;
    std::memset(&pfd, 0, sizeof(pfd));
    pfd.nSize = sizeof(pfd);
    pfd.nVersion = 1;
    pfd.dwFlags = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER;
    pfd.iPixelType = PFD_TYPE_RGBA;
    pfd.cColorBits = 32;
    pfd.cDepthBits = 24;
    pfd.iLayerType = PFD_MAIN_PLANE;

    const int pixelFormat = ChoosePixelFormat(gl->hdc, &pfd);
    if (pixelFormat == 0 || !SetPixelFormat(gl->hdc, pixelFormat, &pfd))
    {
        std::fprintf(stderr,
                     "[amaze_dll_test] ChoosePixelFormat/SetPixelFormat failed (err=%lu)\n",
                     (unsigned long)GetLastError());
        ReleaseDC(gl->hwnd, gl->hdc);
        DestroyWindow(gl->hwnd);
        std::memset(gl, 0, sizeof(*gl));
        return false;
    }

    gl->hglrc = wglCreateContext(gl->hdc);
    if (!gl->hglrc || !wglMakeCurrent(gl->hdc, gl->hglrc))
    {
        std::fprintf(stderr,
                     "[amaze_dll_test] wglCreateContext/wglMakeCurrent failed (err=%lu)\n",
                     (unsigned long)GetLastError());
        if (gl->hglrc) wglDeleteContext(gl->hglrc);
        ReleaseDC(gl->hwnd, gl->hdc);
        DestroyWindow(gl->hwnd);
        std::memset(gl, 0, sizeof(*gl));
        return false;
    }

    std::printf("[amaze_dll_test] hidden GL context OK renderer=\"%s\"\n",
                (const char *)glGetString(GL_RENDERER));
    return true;
}

static void destroy_hidden_gl_context(HiddenGlContext * gl)
{
    if (!gl) return;
    wglMakeCurrent(NULL, NULL);
    if (gl->hglrc) wglDeleteContext(gl->hglrc);
    if (gl->hwnd && gl->hdc) ReleaseDC(gl->hwnd, gl->hdc);
    if (gl->hwnd) DestroyWindow(gl->hwnd);
    std::memset(gl, 0, sizeof(*gl));
}

static std::vector<float> make_raw_frame(int width, int height)
{
    std::vector<float> raw((size_t)width * (size_t)height);
    for (int y = 0; y < height; ++y)
    {
        for (int x = 0; x < width; ++x)
        {
            const uint32_t base =
                (uint32_t)((x * 73 + y * 151 + ((x ^ y) & 255) * 29) % 60000);
            raw[(size_t)y * (size_t)width + (size_t)x] =
                (float)(512u + base);
        }
    }
    return raw;
}

static uint64_t fnv1a64(const void * data, size_t bytes)
{
    const unsigned char * p = (const unsigned char *)data;
    uint64_t hash = 1469598103934665603ull;
    for (size_t i = 0; i < bytes; ++i)
    {
        hash ^= (uint64_t)p[i];
        hash *= 1099511628211ull;
    }
    return hash;
}

static long long compare_gl_rgba_to_rgb(const char * label,
                                        const uint16_t * rgb,
                                        const uint16_t * rgba,
                                        size_t pixelCount)
{
    long long maxAbs = 0;
    size_t mismatches = 0;
    size_t alphaBad = 0;
    for (size_t i = 0; i < pixelCount; ++i)
    {
        for (size_t c = 0; c < 3; ++c)
        {
            const long long diff =
                (long long)rgba[i * 4u + c] - (long long)rgb[i * 3u + c];
            const long long absDiff = diff < 0 ? -diff : diff;
            if (absDiff > maxAbs) maxAbs = absDiff;
            if (absDiff != 0) ++mismatches;
        }
        if (rgba[i * 4u + 3u] != 65535u) ++alphaBad;
    }

    std::printf("\n[amaze_dll_test] %s:\n", label);
    std::printf("  max abs diff = %lld LSB\n", maxAbs);
    std::printf("  rgb mismatches = %zu / %zu samples\n", mismatches, pixelCount * 3u);
    std::printf("  alpha mismatches = %zu / %zu pixels\n", alphaBad, pixelCount);
    return maxAbs + (long long)mismatches + (long long)alphaBad;
}

static bool create_rgba16_texture(GLuint * tex,
                                  int width,
                                  int height,
                                  const char * label)
{
    if (!tex) return false;
    *tex = 0;

    glGenTextures(1, tex);
    glBindTexture(GL_TEXTURE_2D, *tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    glTexImage2D(GL_TEXTURE_2D,
                 0,
                 GL_RGBA16,
                 width,
                 height,
                 0,
                 GL_RGBA,
                 GL_UNSIGNED_SHORT,
                 NULL);
    const GLenum glError = glGetError();
    if (glError != GL_NO_ERROR)
    {
        std::fprintf(stderr,
                     "[amaze_dll_test] glTexImage2D(GL_RGBA16 %s) failed GL error 0x%04x\n",
                     label ? label : "texture",
                     glError);
        if (*tex) glDeleteTextures(1, tex);
        *tex = 0;
        return false;
    }

    return true;
}

static bool read_rgba16_texture(GLuint tex,
                                std::vector<uint16_t> * rgba,
                                const char * label)
{
    if (!rgba) return false;

    glBindTexture(GL_TEXTURE_2D, tex);
    glFinish();
    glGetTexImage(GL_TEXTURE_2D, 0, GL_RGBA, GL_UNSIGNED_SHORT, rgba->data());
    const GLenum glError = glGetError();
    if (glError != GL_NO_ERROR)
    {
        std::fprintf(stderr,
                     "[amaze_dll_test] glGetTexImage(GL_RGBA16 %s) failed GL error 0x%04x\n",
                     label ? label : "texture",
                     glError);
        return false;
    }

    return true;
}

static uint16_t clamp_double_to_u16_ref(double value)
{
    if (value <= 0.0) return 0u;
    if (value >= 65535.0) return 65535u;
    return (uint16_t)value;
}

static std::vector<uint16_t> apply_wb_undo_reference(const uint16_t * rgb,
                                                     size_t pixelCount,
                                                     int blackLevel,
                                                     double wbR,
                                                     double wbG,
                                                     double wbB)
{
    if (blackLevel < 1000) blackLevel = -1000;

    std::vector<uint16_t> out(pixelCount * 3u, 0u);
    for (size_t i = 0; i < pixelCount; ++i)
    {
        out[i * 3u + 0u] =
            clamp_double_to_u16_ref(((double)rgb[i * 3u + 0u] / wbR)
                                    + (double)blackLevel);
        out[i * 3u + 1u] =
            clamp_double_to_u16_ref(((double)rgb[i * 3u + 1u] / wbG)
                                    + (double)blackLevel);
        out[i * 3u + 2u] =
            clamp_double_to_u16_ref(((double)rgb[i * 3u + 2u] / wbB)
                                    + (double)blackLevel);
    }
    return out;
}

#define RESOLVE(var, type, name) \
    var = (type)GetProcAddress(dll, name); \
    if (!var) { std::fprintf(stderr, "GetProcAddress failed for %s\n", name); return 4; }

/* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 --clock-kick: the Row K kick (begin / in-flight refusal / end with
 * samples and a clock above 0, bounded by its deadline, stopped by a live run's drain) and the Row 0 probe (an armed
 * live R16 -> RGBA16 run is valid with both clocks and a device time above 0; the next, unarmed run is not). */
#ifndef GL_R16
#define GL_R16 0x822A
#endif

static double kick_max_interval_mhz(const igpu_clock_kick_report_t & r)
{
    double best = 0.0;
    for (int i = 1; i < r.samples; ++i)
    {
        if (r.sample_sm[i] != r.sample_sm[i - 1]) continue;   // clock64 is per SM
        const double dns = (double)(r.sample_ns[i] - r.sample_ns[i - 1]);
        const double dclk = (double)(r.sample_clk[i] - r.sample_clk[i - 1]);
        if (dns > 0.0 && dclk * 1000.0 / dns > best) best = dclk * 1000.0 / dns;
    }
    return best;
}

static int collect_kick(pfn_clock_kick_end f_end, igpu_amaze_debayer_backend * backend, igpu_clock_kick_report_t * r)
{
    int rc = f_end(backend, r);
    for (int tries = 0; rc == 1 && tries < 50; ++tries)
    {
        Sleep(10);
        rc = f_end(backend, r);
    }
    return rc;
}

static int run_clock_kick_mode(HMODULE dll, igpu_amaze_debayer_backend * backend, const std::vector<float> & raw,
                               int width, int height)
{
    pfn_arm_probe f_arm;
    pfn_last_probe f_probe;
    pfn_clock_kick_begin f_begin;
    pfn_clock_kick_end f_end;
    pfn_run_post_wb_from_r16 f_r16;
    RESOLVE(f_arm, pfn_arm_probe, "igpu_amaze_debayer_arm_probe");
    RESOLVE(f_probe, pfn_last_probe, "igpu_amaze_debayer_last_probe");
    RESOLVE(f_begin, pfn_clock_kick_begin, "igpu_amaze_debayer_clock_kick_begin");
    RESOLVE(f_end, pfn_clock_kick_end, "igpu_amaze_debayer_clock_kick_end");
    RESOLVE(f_r16, pfn_run_post_wb_from_r16, "igpu_amaze_debayer_run_post_wb_gl_texture_from_r16_gl_texture");
    bool pass = true;
    auto check = [&pass](bool ok, const char * what)
    {
        std::printf("[amaze_dll_test] clock-kick %-58s %s\n", what, ok ? "ok" : "FAIL");
        if (!ok) pass = false;
    };

    // bad arguments; nothing to collect yet
    igpu_clock_kick_report_t r;
    check(f_begin(backend, 0) == -1 && f_begin(backend, 2001) == -1 && f_begin(NULL, 100) == -1, "rejects bad max_ms");
    check(f_end(backend, &r) == 2 && r.state == 2, "end with no kick is state 2");

    // 1) ended by the host: samples, a clock above 0, at most one in flight
    const int maxMs = 200;
    const ULONGLONG t0 = GetTickCount64();
    check(f_begin(backend, maxMs) == 0, "begin(200) launches");
    check(f_begin(backend, maxMs) == 1, "a second begin is in flight");
    Sleep(60);
    int rc = collect_kick(f_end, backend, &r);
    const ULONGLONG hostMs = GetTickCount64() - t0;
    std::printf("[amaze_dll_test] clock-kick end rc=%d state=%d samples=%d stopped_by=%d ran_ms=%.3f max_mhz=%.1f host_ms=%llu\n",
                rc, r.state, r.samples, r.stopped_by, r.ran_ms, kick_max_interval_mhz(r), (unsigned long long)hostMs);
    check(rc == 0 && r.state == 0, "end collects");
    check(r.samples >= 2, ">= 2 samples (periodic + closing)");
    check(kick_max_interval_mhz(r) > 0.0, "SM MHz > 0");
    check(r.stopped_by == 1, "stopped by end()");
    check(r.ran_ms > 0.0 && r.ran_ms <= maxMs + 50.0, "ran_ms within max_ms + 50");

    // 2) the deadline stops it on its own
    check(f_begin(backend, 50) == 0, "begin(50) launches");
    Sleep(200);
    rc = collect_kick(f_end, backend, &r);
    std::printf("[amaze_dll_test] clock-kick deadline rc=%d stopped_by=%d ran_ms=%.3f samples=%d\n",
                rc, r.stopped_by, r.ran_ms, r.samples);
    check(rc == 0 && r.stopped_by == 0, "stopped by its deadline");
    check(r.ran_ms >= 49.0 && r.ran_ms <= 100.0, "deadline ran_ms in [49, 100]");

    // 3) the probe on a live run, and a live run's drain stops a running kick
    HiddenGlContext gl;
    if (!create_hidden_gl_context(&gl)) return 5;
    const size_t pixelCount = (size_t)width * (size_t)height;
    std::vector<uint16_t> bayer(pixelCount);
    for (size_t i = 0; i < pixelCount; ++i) bayer[i] = (uint16_t)(raw[i] > 65535.0f ? 65535.0f : raw[i]);
    GLuint in = 0;
    glGenTextures(1, &in);
    glBindTexture(GL_TEXTURE_2D, in);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glPixelStorei(GL_UNPACK_ALIGNMENT, 2);
    glTexImage2D(GL_TEXTURE_2D, 0, GL_R16, width, height, 0, GL_RED, GL_UNSIGNED_SHORT, bayer.data());
    GLuint out = 0;
    if (glGetError() != GL_NO_ERROR || !create_rgba16_texture(&out, width, height, "probe out"))
    {
        std::fprintf(stderr, "[amaze_dll_test] clock-kick: GL texture setup failed\n");
        destroy_hidden_gl_context(&gl);
        return 5;
    }
    igpu_amaze_probe_t probe;
    std::memset(&probe, 0, sizeof(probe));
    check(f_arm(backend, -1) == -1 && f_probe(NULL, &probe) == -1, "probe rejects bad arguments");
    check(f_arm(backend, 1) == 0, "arm_probe(1)");
    check(f_begin(backend, 400) == 0, "begin(400) before a live run");
    rc = f_r16(backend, (unsigned int)in, (unsigned int)out, width, height, 2048, 1.0, 1.0, 1.0);
    check(rc == 0, "armed live R16 run");
    check(f_probe(backend, &probe) == 0, "last_probe");
    std::printf("[amaze_dll_test] probe valid=%d mhz_before=%.1f mhz_after=%.1f device_ms=%.3f\n",
                probe.valid, probe.sm_mhz_before, probe.sm_mhz_after, probe.device_ms);
    check(probe.valid == 1 && probe.sm_mhz_before > 0.0 && probe.sm_mhz_after > 0.0 && probe.device_ms > 0.0,
          "armed run: valid, both clocks and device_ms > 0");
    rc = collect_kick(f_end, backend, &r);
    std::printf("[amaze_dll_test] clock-kick after live run rc=%d stopped_by=%d ran_ms=%.3f\n", rc, r.stopped_by, r.ran_ms);
    check(rc == 0 && r.stopped_by == 2, "the live run's drain stopped the kick");
    rc = f_r16(backend, (unsigned int)in, (unsigned int)out, width, height, 2048, 1.0, 1.0, 1.0);
    check(rc == 0 && f_probe(backend, &probe) == 0 && probe.valid == 0, "the next, unarmed run is not probed");
    glDeleteTextures(1, &out);
    glDeleteTextures(1, &in);
    destroy_hidden_gl_context(&gl);
    std::printf("\n[amaze_dll_test] RESULT: %s (clock kick + probe)\n", pass ? "PASS" : "FAIL");
    return pass ? 0 : 1;
}

/* PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-WARM-OUTPUT-REUSE-1 T3 --input-capture: the R1 capture / exact compare / release
 * (capture then match on the same buffer gives 1; one changed pixel, other dims, a stale id, and after release give 0)
 * and AMaZE determinism (the device Bayer16 -> RGBA16 run twice on the same input is bit-identical, both H-Nyquist
 * settings). This MSVC harness links no CUDA runtime: device buffers come from the cudart DLL that ships beside the
 * backend (cudart64_*.dll), loaded by name; the runtime API shares the process's primary context with the backend. */
typedef int (*pfn_capture_input)(igpu_amaze_debayer_backend *, const uint16_t *, int, int, unsigned long long *);
typedef int (*pfn_matches_capture)(igpu_amaze_debayer_backend *, const uint16_t *, int, int, unsigned long long, int *);
typedef int (*pfn_release_capture)(igpu_amaze_debayer_backend *);
typedef int (*pfn_run_post_wb_from_device)(igpu_amaze_debayer_backend *, const uint16_t *, unsigned int,
                                           int, int, int, double, double, double);
typedef int (*pfn_cuda_malloc)(void **, size_t);
typedef int (*pfn_cuda_memcpy)(void *, const void *, size_t, int);
typedef int (*pfn_cuda_free)(void *);

static HMODULE load_cudart(const std::string & dllPath)
{
    std::string dir = dllPath;
    const size_t slash = dir.find_last_of("\\/");
    dir = slash == std::string::npos ? std::string() : dir.substr(0, slash + 1);
    for (const char * name : {"cudart64_12.dll", "cudart64_13.dll", "cudart64_110.dll"})
    {
        HMODULE m = LoadLibraryA((dir + name).c_str());
        if (!m) m = LoadLibraryA(name);
        if (m) return m;
    }
    return NULL;
}

static int run_input_capture_mode(HMODULE dll, igpu_amaze_debayer_backend * backend, const std::string & dllPath,
                                  const std::vector<float> & raw, int width, int height)
{
    pfn_capture_input f_capture;
    pfn_matches_capture f_matches;
    pfn_release_capture f_release;
    pfn_run_post_wb_from_device f_device;
    pfn_set_reduced_hnyquist f_hny;
    RESOLVE(f_capture, pfn_capture_input, "igpu_amaze_debayer_capture_input_bayer16");
    RESOLVE(f_matches, pfn_matches_capture, "igpu_amaze_debayer_input_bayer16_matches_capture");
    RESOLVE(f_release, pfn_release_capture, "igpu_amaze_debayer_release_input_capture");
    RESOLVE(f_device, pfn_run_post_wb_from_device, "igpu_amaze_debayer_run_post_wb_gl_texture_from_device_bayer16");
    RESOLVE(f_hny, pfn_set_reduced_hnyquist, "igpu_amaze_debayer_set_reduced_hnyquist");
    HMODULE cudart = load_cudart(dllPath);
    if (!cudart)
    {
        std::fprintf(stderr, "[amaze_dll_test] input-capture: no cudart64_*.dll beside %s or on PATH\n", dllPath.c_str());
        return 5;
    }
    pfn_cuda_malloc c_malloc = (pfn_cuda_malloc)GetProcAddress(cudart, "cudaMalloc");
    pfn_cuda_memcpy c_memcpy = (pfn_cuda_memcpy)GetProcAddress(cudart, "cudaMemcpy");
    pfn_cuda_free c_free = (pfn_cuda_free)GetProcAddress(cudart, "cudaFree");
    if (!c_malloc || !c_memcpy || !c_free)
    {
        std::fprintf(stderr, "[amaze_dll_test] input-capture: cudart lacks cudaMalloc/cudaMemcpy/cudaFree\n");
        return 5;
    }
    bool pass = true;
    auto check = [&pass](bool ok, const char * what)
    {
        std::printf("[amaze_dll_test] input-capture %-62s %s\n", what, ok ? "ok" : "FAIL");
        if (!ok) pass = false;
    };
    const size_t pixelCount = (size_t)width * (size_t)height;
    const size_t bytes = pixelCount * sizeof(uint16_t);
    std::vector<uint16_t> bayer(pixelCount);
    for (size_t i = 0; i < pixelCount; ++i) bayer[i] = (uint16_t)(raw[i] > 65535.0f ? 65535.0f : raw[i]);
    std::vector<uint16_t> changed = bayer;
    changed[pixelCount / 2 + 3] = (uint16_t)(changed[pixelCount / 2 + 3] ^ 1u);   // one pixel, one LSB
    void * dA = NULL;
    void * dB = NULL;
    const int cudaHostToDevice = 1;
    if (c_malloc(&dA, bytes) != 0 || c_malloc(&dB, bytes) != 0
        || c_memcpy(dA, bayer.data(), bytes, cudaHostToDevice) != 0
        || c_memcpy(dB, changed.data(), bytes, cudaHostToDevice) != 0)
    {
        std::fprintf(stderr, "[amaze_dll_test] input-capture: device buffer setup failed\n");
        return 5;
    }
    const uint16_t * a = (const uint16_t *)dA;
    const uint16_t * b = (const uint16_t *)dB;

    // capture / compare / release
    unsigned long long id = 0;
    int equal = -1;
    check(f_capture(NULL, a, width, height, &id) == -1 && f_matches(backend, a, width, height, 1, NULL) == -1,
          "rejects bad arguments");
    check(f_matches(backend, a, width, height, 1, &equal) == 0 && equal == 0, "no capture yet: not equal");
    check(f_capture(backend, a, width, height, &id) == 0 && id > 0, "capture issues an id > 0");
    check(f_matches(backend, a, width, height, id, &equal) == 0 && equal == 1, "same buffer, same id: equal");
    check(f_matches(backend, b, width, height, id, &equal) == 0 && equal == 0, "one pixel one LSB changed: not equal");
    check(f_matches(backend, a, width, height - 2, id, &equal) == 0 && equal == 0, "other dims: not equal");
    check(f_matches(backend, a, height, width, id, &equal) == 0 && equal == 0, "dims swapped: not equal");
    check(f_matches(backend, a, width, height, id + 1, &equal) == 0 && equal == 0, "a stale or foreign id: not equal");
    check(f_matches(backend, a, width, height, 0, &equal) == 0 && equal == 0, "id 0: not equal");
    unsigned long long id2 = 0;
    check(f_capture(backend, b, width, height, &id2) == 0 && id2 > id, "a second capture issues a newer id");
    check(f_matches(backend, b, width, height, id, &equal) == 0 && equal == 0, "the first id is stale after it");
    check(f_matches(backend, b, width, height, id2, &equal) == 0 && equal == 1, "the second capture matches its buffer");
    check(f_matches(backend, a, width, height, id2, &equal) == 0 && equal == 0, "and not the other buffer");
    check(f_release(backend) == 0 && f_release(NULL) == -1, "release");
    check(f_matches(backend, b, width, height, id2, &equal) == 0 && equal == 0, "after release: not equal");
    unsigned long long id3 = 0;
    check(f_capture(backend, a, width, height, &id3) == 0 && id3 > id2, "a capture after release (buffer reused)");
    check(f_matches(backend, a, width, height, id3, &equal) == 0 && equal == 1, "matches again");

    // AMaZE determinism on the device Bayer16 route the warm and Play both use
    HiddenGlContext gl;
    if (!create_hidden_gl_context(&gl)) return 5;
    GLuint out1 = 0;
    GLuint out2 = 0;
    if (!create_rgba16_texture(&out1, width, height, "determinism 1") || !create_rgba16_texture(&out2, width, height, "determinism 2"))
    {
        destroy_hidden_gl_context(&gl);
        return 5;
    }
    std::vector<uint16_t> rgba1(pixelCount * 4u, 0u);
    std::vector<uint16_t> rgba2(pixelCount * 4u, 0u);
    for (int hny = 0; hny <= 1; ++hny)
    {
        check(f_hny(backend, hny) == 0, hny ? "reduced H-Nyquist on" : "reduced H-Nyquist off");
        const int rc1 = f_device(backend, a, (unsigned int)out1, width, height, 2048, 2.0176366843033511, 1.0, 1.5503875968992249);
        const int rc2 = f_device(backend, a, (unsigned int)out2, width, height, 2048, 2.0176366843033511, 1.0, 1.5503875968992249);
        check(rc1 == 0 && rc2 == 0, "two device-Bayer16 AMaZE runs");
        const bool read = read_rgba16_texture(out1, &rgba1, "determinism 1") && read_rgba16_texture(out2, &rgba2, "determinism 2");
        size_t differing = 0;
        for (size_t i = 0; read && i < rgba1.size(); ++i) if (rgba1[i] != rgba2[i]) ++differing;
        std::printf("[amaze_dll_test] input-capture determinism hnyquist=%d differing_words=%zu fnv1=%016llx fnv2=%016llx\n",
                    hny, differing, (unsigned long long)fnv1a64(rgba1.data(), rgba1.size() * 2u),
                    (unsigned long long)fnv1a64(rgba2.data(), rgba2.size() * 2u));
        check(read && differing == 0, "the same input gives bit-identical RGBA16");
    }
    (void)f_hny(backend, 0);
    glDeleteTextures(1, &out2);
    glDeleteTextures(1, &out1);
    destroy_hidden_gl_context(&gl);
    c_free(dB);
    c_free(dA);
    std::printf("\n[amaze_dll_test] RESULT: %s (input capture + determinism)\n", pass ? "PASS" : "FAIL");
    return pass ? 0 : 1;
}

int main(int argc, char ** argv)
{
    std::string dllPath = "igpu_amaze_debayer_cuda.dll";
    bool runGlTexture = false;
    int width = 192;
    int height = 160;
    /* r3 c1: --reduced-hnyquist runs run() with the flag at 0 and at 1 and requires
     * flag 1 == hnyquist121_reference(flag 0) at 0 LSB. --input-u16 FILE (with
     * --size) feeds a Bayer16 vector as float(max(0, v - black)); --dump-dir DIR
     * writes flag0.rgb16 (and flag1.rgb16 when the DLL has the symbol). */
    bool reducedHnyquist = false;
    std::string inputU16Path;
    std::string dumpDir;
    int inputBlack = 0;
    bool clockKick = false;   // PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1: run_clock_kick_mode
    bool inputCapture = false;   // PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-WARM-OUTPUT-REUSE-1 T3: run_input_capture_mode

    for (int i = 1; i < argc; ++i)
    {
        if (std::strcmp(argv[i], "--gl-texture") == 0)
        {
            runGlTexture = true;
        }
        else if (std::strcmp(argv[i], "--input-capture") == 0)
        {
            inputCapture = true;
        }
        else if (std::strcmp(argv[i], "--clock-kick") == 0)
        {
            clockKick = true;
        }
        else if (std::strcmp(argv[i], "--reduced-hnyquist") == 0)
        {
            reducedHnyquist = true;
        }
        else if (std::strcmp(argv[i], "--input-u16") == 0 && i + 1 < argc)
        {
            inputU16Path = argv[++i];
        }
        else if (std::strcmp(argv[i], "--black") == 0 && i + 1 < argc)
        {
            inputBlack = std::atoi(argv[++i]);
        }
        else if (std::strcmp(argv[i], "--dump-dir") == 0 && i + 1 < argc)
        {
            dumpDir = argv[++i];
        }
        else if (std::strcmp(argv[i], "--size") == 0 && i + 2 < argc)
        {
            width = std::atoi(argv[++i]);
            height = std::atoi(argv[++i]);
        }
        else
        {
            dllPath = argv[i];
        }
    }

    if (width <= 32 || height <= 32)
    {
        std::fprintf(stderr, "width/height must be > 32\n");
        return 2;
    }

    std::printf("[amaze_dll_test] dll path   : %s\n", dllPath.c_str());
    std::printf("[amaze_dll_test] size       : %dx%d\n", width, height);
    std::printf("[amaze_dll_test] gl texture : %s\n", runGlTexture ? "enabled" : "disabled");

    HMODULE dll = LoadLibraryA(dllPath.c_str());
    if (!dll)
    {
        std::fprintf(stderr,
                     "[amaze_dll_test] LoadLibrary('%s') failed (err=%lu)\n",
                     dllPath.c_str(),
                     (unsigned long)GetLastError());
        return 4;
    }
    std::printf("[amaze_dll_test] LoadLibrary OK\n");

    pfn_create f_create;
    pfn_destroy f_destroy;
    pfn_abi_version f_abi;
    pfn_describe f_describe;
    pfn_run f_run;
    pfn_run_gl_texture f_run_gl_texture = NULL;
    pfn_run_post_wb_gl_texture f_run_post_wb_gl_texture = NULL;
    pfn_last_timing f_timing;
    RESOLVE(f_create, pfn_create, "igpu_amaze_debayer_create");
    RESOLVE(f_destroy, pfn_destroy, "igpu_amaze_debayer_destroy");
    RESOLVE(f_abi, pfn_abi_version, "igpu_amaze_debayer_abi_version");
    RESOLVE(f_describe, pfn_describe, "igpu_amaze_debayer_describe");
    RESOLVE(f_run, pfn_run, "igpu_amaze_debayer_run");
    RESOLVE(f_timing, pfn_last_timing, "igpu_amaze_debayer_last_timing");
    if (runGlTexture)
    {
        RESOLVE(f_run_gl_texture,
                pfn_run_gl_texture,
                "igpu_amaze_debayer_run_gl_texture");
        RESOLVE(f_run_post_wb_gl_texture,
                pfn_run_post_wb_gl_texture,
                "igpu_amaze_debayer_run_post_wb_gl_texture");
    }
    std::printf("[amaze_dll_test] resolved ABI symbols\n");

    igpu_amaze_debayer_backend * backend = f_create("cuda");
    if (!backend)
    {
        std::fprintf(stderr, "[amaze_dll_test] create('cuda') returned NULL\n");
        return 4;
    }
    std::printf("[amaze_dll_test] create OK abi=%d describe=\"%s\"\n",
                f_abi(backend),
                f_describe(backend));

    std::vector<float> raw = make_raw_frame(width, height);
    const size_t pixelCount = (size_t)width * (size_t)height;
    if (!inputU16Path.empty())
    {
        std::vector<uint16_t> bayer(pixelCount, 0u);
        if (!read_u16_file(inputU16Path, &bayer))
        {
            std::fprintf(stderr, "[amaze_dll_test] cannot read %zu words from %s\n",
                         pixelCount, inputU16Path.c_str());
            return 2;
        }
        for (size_t i = 0; i < pixelCount; ++i)
        {
            const int v = (int)bayer[i] - inputBlack;
            raw[i] = (float)(v > 0 ? v : 0);
        }
        std::printf("[amaze_dll_test] input u16  : %s black=%d\n", inputU16Path.c_str(), inputBlack);
    }
    if (clockKick)
    {
        const int kickRc = run_clock_kick_mode(dll, backend, raw, width, height);
        f_destroy(backend);
        FreeLibrary(dll);
        return kickRc;
    }
    if (inputCapture)
    {
        const int captureRc = run_input_capture_mode(dll, backend, dllPath, raw, width, height);
        f_destroy(backend);
        FreeLibrary(dll);
        return captureRc;
    }
    if (reducedHnyquist)
    {
        pfn_set_reduced_hnyquist f_set = (pfn_set_reduced_hnyquist)
            GetProcAddress(dll, "igpu_amaze_debayer_set_reduced_hnyquist");
        std::vector<uint16_t> flag0(pixelCount * 3u, 0u);
        std::vector<uint16_t> flag1(pixelCount * 3u, 0u);
        if (f_set && f_set(backend, 0) != 0)
        {
            std::fprintf(stderr, "[amaze_dll_test] set_reduced_hnyquist(0) failed\n");
            return 4;
        }
        if (f_run(backend, raw.data(), flag0.data(), width, height) != 0)
        {
            std::fprintf(stderr, "[amaze_dll_test] run(flag 0) failed\n");
            return 4;
        }
        std::printf("[amaze_dll_test] flag0 RGB16 FNV1A64 = %016llx\n",
                    (unsigned long long)fnv1a64(flag0.data(), flag0.size() * sizeof(uint16_t)));
        if (!dumpDir.empty()
            && !write_file(dumpDir + "\\flag0.rgb16", flag0.data(), flag0.size() * sizeof(uint16_t)))
        {
            std::fprintf(stderr, "[amaze_dll_test] dump flag0 failed\n");
            return 4;
        }
        if (!f_set)
        {
            std::printf("[amaze_dll_test] DLL lacks igpu_amaze_debayer_set_reduced_hnyquist (flag-0 output only)\n");
            std::printf("\n[amaze_dll_test] RESULT: PASS (flag 0 only)\n");
            f_destroy(backend);
            FreeLibrary(dll);
            return 0;
        }
        if (f_set(backend, 2) != -1 || f_set(NULL, 1) != -1)
        {
            std::fprintf(stderr, "[amaze_dll_test] set_reduced_hnyquist accepted an invalid call\n");
            return 4;
        }
        if (f_set(backend, 1) != 0
            || f_run(backend, raw.data(), flag1.data(), width, height) != 0)
        {
            std::fprintf(stderr, "[amaze_dll_test] run(flag 1) failed\n");
            return 4;
        }
        std::vector<uint16_t> reference(pixelCount * 3u, 0u);
        hnyquist121_reference(reference.data(), flag0.data(), width, height);
        long long maxAbs = 0;
        size_t mismatches = 0;
        for (size_t i = 0; i < reference.size(); ++i)
        {
            const long long d = (long long)flag1[i] - (long long)reference[i];
            const long long a = d < 0 ? -d : d;
            if (a > maxAbs) maxAbs = a;
            if (a) ++mismatches;
        }
        /* The flag is sticky: back at 0 the output is flag 0's again. */
        std::vector<uint16_t> again(pixelCount * 3u, 0u);
        const bool againOk = f_set(backend, 0) == 0
            && f_run(backend, raw.data(), again.data(), width, height) == 0
            && again == flag0;
        std::printf("[amaze_dll_test] flag1 RGB16 FNV1A64 = %016llx\n",
                    (unsigned long long)fnv1a64(flag1.data(), flag1.size() * sizeof(uint16_t)));
        std::printf("[amaze_dll_test] reduced H-Nyquist flag1 vs C ref(flag0): max abs diff = %lld LSB, mismatches = %zu / %zu; flag0 again identical=%d\n",
                    maxAbs, mismatches, reference.size(), againOk ? 1 : 0);
        if (!dumpDir.empty()
            && !write_file(dumpDir + "\\flag1.rgb16", flag1.data(), flag1.size() * sizeof(uint16_t)))
        {
            std::fprintf(stderr, "[amaze_dll_test] dump flag1 failed\n");
            return 4;
        }
        const bool hnyquistPass = maxAbs == 0 && mismatches == 0 && againOk;
        std::printf("\n[amaze_dll_test] RESULT: %s (reduced H-Nyquist)\n", hnyquistPass ? "PASS" : "FAIL");
        f_destroy(backend);
        FreeLibrary(dll);
        return hnyquistPass ? 0 : 1;
    }
    std::vector<uint16_t> hostRgb(pixelCount * 3u, 0u);
    int rc = f_run(backend, raw.data(), hostRgb.data(), width, height);
    if (rc != 0)
    {
        std::fprintf(stderr, "[amaze_dll_test] run(host RGB16) returned %d\n", rc);
        return 4;
    }
    std::printf("[amaze_dll_test] run(host RGB16) OK\n");
    const uint64_t hostHash =
        fnv1a64(hostRgb.data(), hostRgb.size() * sizeof(uint16_t));
    std::printf("[amaze_dll_test] host RGB16 FNV1A64 = %016llx\n",
                (unsigned long long)hostHash);

    igpu_amaze_debayer_timing_t timing;
    if (f_timing(backend, &timing) == 0)
    {
        std::printf("[amaze_dll_test] host timing ms: upload=%.3f kernel=%.3f download=%.3f total=%.3f\n",
                    timing.upload_ms,
                    timing.kernel_ms,
                    timing.download_ms,
                    timing.total_ms);
    }

    long long glDiff = 0;
    long long postWbGlDiff = 0;
    if (runGlTexture)
    {
        HiddenGlContext gl;
        if (!create_hidden_gl_context(&gl))
        {
            return 5;
        }

        GLuint tex = 0;
        if (!create_rgba16_texture(&tex, width, height, "pre-WB"))
        {
            destroy_hidden_gl_context(&gl);
            return 5;
        }

        rc = f_run_gl_texture(backend, raw.data(), (unsigned int)tex, width, height);
        if (rc != 0)
        {
            std::fprintf(stderr, "[amaze_dll_test] run(GL_TEXTURE) returned %d\n", rc);
            glDeleteTextures(1, &tex);
            destroy_hidden_gl_context(&gl);
            return 5;
        }
        std::printf("[amaze_dll_test] run(GL_TEXTURE) OK\n");

        if (f_timing(backend, &timing) == 0)
        {
            std::printf("[amaze_dll_test] GL timing ms: upload=%.3f kernel=%.3f interop=%.3f total=%.3f\n",
                        timing.upload_ms,
                        timing.kernel_ms,
                        timing.download_ms,
                        timing.total_ms);
        }

        std::vector<uint16_t> rgba(pixelCount * 4u, 0u);
        if (!read_rgba16_texture(tex, &rgba, "pre-WB"))
        {
            glDeleteTextures(1, &tex);
            destroy_hidden_gl_context(&gl);
            return 5;
        }

        glDiff = compare_gl_rgba_to_rgb("pre-WB GL_RGBA16 texture vs host RGB16",
                                        hostRgb.data(),
                                        rgba.data(),
                                        pixelCount);
        const uint64_t rgbaHash =
            fnv1a64(rgba.data(), rgba.size() * sizeof(uint16_t));
        std::printf("[amaze_dll_test] GL RGBA16 FNV1A64 = %016llx\n",
                    (unsigned long long)rgbaHash);
        glDeleteTextures(1, &tex);

        const int postBlackLevel = 2048;
        const double postWbR = 0.75;
        const double postWbG = 1.0;
        const double postWbB = 0.50;
        const std::vector<uint16_t> hostPostRgb =
            apply_wb_undo_reference(hostRgb.data(),
                                    pixelCount,
                                    postBlackLevel,
                                    postWbR,
                                    postWbG,
                                    postWbB);
        const uint64_t hostPostHash =
            fnv1a64(hostPostRgb.data(), hostPostRgb.size() * sizeof(uint16_t));
        std::printf("[amaze_dll_test] post-WB CPU reference black=%d wb=(%.6f, %.6f, %.6f)\n",
                    postBlackLevel,
                    postWbR,
                    postWbG,
                    postWbB);
        std::printf("[amaze_dll_test] post-WB CPU RGB16 FNV1A64 = %016llx\n",
                    (unsigned long long)hostPostHash);

        GLuint postTex = 0;
        if (!create_rgba16_texture(&postTex, width, height, "post-WB"))
        {
            destroy_hidden_gl_context(&gl);
            return 5;
        }

        rc = f_run_post_wb_gl_texture(backend,
                                      raw.data(),
                                      (unsigned int)postTex,
                                      width,
                                      height,
                                      postBlackLevel,
                                      postWbR,
                                      postWbG,
                                      postWbB);
        if (rc != 0)
        {
            std::fprintf(stderr,
                         "[amaze_dll_test] run(post-WB GL_TEXTURE) returned %d\n",
                         rc);
            glDeleteTextures(1, &postTex);
            destroy_hidden_gl_context(&gl);
            return 5;
        }
        std::printf("[amaze_dll_test] run(post-WB GL_TEXTURE) OK\n");

        if (f_timing(backend, &timing) == 0)
        {
            std::printf("[amaze_dll_test] post-WB GL timing ms: upload=%.3f kernel=%.3f interop=%.3f total=%.3f\n",
                        timing.upload_ms,
                        timing.kernel_ms,
                        timing.download_ms,
                        timing.total_ms);
        }

        std::vector<uint16_t> postRgba(pixelCount * 4u, 0u);
        if (!read_rgba16_texture(postTex, &postRgba, "post-WB"))
        {
            glDeleteTextures(1, &postTex);
            destroy_hidden_gl_context(&gl);
            return 5;
        }

        postWbGlDiff =
            compare_gl_rgba_to_rgb("post-WB GL_RGBA16 texture vs CPU post-WB reference",
                                   hostPostRgb.data(),
                                   postRgba.data(),
                                   pixelCount);
        const uint64_t postRgbaHash =
            fnv1a64(postRgba.data(), postRgba.size() * sizeof(uint16_t));
        std::printf("[amaze_dll_test] post-WB GL RGBA16 FNV1A64 = %016llx\n",
                    (unsigned long long)postRgbaHash);
        glDeleteTextures(1, &postTex);
        destroy_hidden_gl_context(&gl);
    }

    f_destroy(backend);
    FreeLibrary(dll);

    const bool pass = !runGlTexture || (glDiff == 0 && postWbGlDiff == 0);
    std::printf("\n[amaze_dll_test] RESULT: %s%s\n",
                pass ? "PASS" : "FAIL",
                runGlTexture ? " (pre-WB and post-WB GL_RGBA16 RGB bit-exact, alpha=65535)" : "");
    return pass ? 0 : 1;
}
