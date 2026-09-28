/* dll_test.cpp - host harness proving the igpu_recon CUDA backend works
 * THROUGH the C ABI by runtime-loading the DLL.
 *
 * It does NOT link igpu_recon_cuda.dll at build time. Instead it
 * LoadLibrary()s it and GetProcAddress()es every ABI symbol - this is the
 * exact integration mechanism MLVApp (Qt/MinGW) will use, so a green run here
 * proves the DLL is loadable and callable across the pure-C boundary.
 *
 * Flow:
 *   1. LoadLibrary("igpu_recon_cuda.dll") + resolve the 8 ABI functions.
 *   2. Load the oracle in.u16 + the 4 LUTs from the vectors dir.
 *      If scalars.txt is present, use its live app state instead of the
 *      original fixed oracle-vector defaults.
 *   3. create("cuda") / set_clip / set_luts / run(in.u16, CPU16).
 *   4. Compare run() output to oracle out.u16 -> expect max abs diff 0 LSB.
 *   5. Print last_timing (upload / kernel / download-or-interop / total).
 *   6. Optional --gl-texture mode reruns through IGPU_OUT_GL_TEXTURE into a
 *      hidden-WGL GL_R16 texture, then reads that texture back only for harness
 *      parity comparison. The backend call itself performs no D2H copy.
 *
 * Build (MSVC cl, on the host):
 *   cl /EHsc /O2 dll_test.cpp /Fe:dll_test.exe opengl32.lib user32.lib gdi32.lib
 *   (no CUDA link; only Win32 LoadLibrary plus optional WGL test context. See
 *   build-backend-dll.ps1.)
 *
 * Run:
 *   dll_test.exe [vectors_dir] [dll_path]
 *     vectors_dir default: G:\Temp\mlv-gpu-profile\oracle\vectors
 *     dll_path    default: .\igpu_recon_cuda.dll
 */

#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <cstdint>
#include <string>
#include <windows.h>
#include <GL/gl.h>

#include "igpu_recon.h"

extern "C" {
__declspec(dllexport) unsigned long NvOptimusEnablement = 0x00000001;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}

#ifndef GL_CLAMP_TO_EDGE
#define GL_CLAMP_TO_EDGE 0x812F
#endif
#ifndef GL_RED
#define GL_RED 0x1903
#endif
#ifndef GL_R16
#define GL_R16 0x822A
#endif

/* fixed oracle geometry / LUT sizes (README.txt) */
#define W 1808
#define H 2268
#define RAW2EV_COUNT (1u << 20)
#define EV2RAW_COUNT (24u * 65536u)
#define RANDN05_COUNT 1024u

/* ABI function pointer typedefs */
typedef igpu_recon_backend* (*pfn_create)(const char*);
typedef void                (*pfn_destroy)(igpu_recon_backend*);
typedef int                 (*pfn_abi_version)(igpu_recon_backend*);
typedef const char*         (*pfn_describe)(igpu_recon_backend*);
typedef int                 (*pfn_set_clip)(igpu_recon_backend*, const igpu_recon_clip_t*);
typedef int                 (*pfn_set_luts)(igpu_recon_backend*, const igpu_recon_luts_t*);
typedef int                 (*pfn_run)(igpu_recon_backend*, const igpu_recon_frame_t*,
                                       const uint16_t*, igpu_recon_out_kind, uint16_t*, unsigned int);
typedef int                 (*pfn_last_timing)(igpu_recon_backend*, igpu_recon_timing_t*);
typedef int                 (*pfn_preupload_frame)(igpu_recon_backend*, uint64_t,
                                                   const uint16_t*, size_t, int);
typedef int                 (*pfn_run_preuploaded)(igpu_recon_backend*, uint64_t,
                                                   const igpu_recon_frame_t*,
                                                   const uint16_t*, igpu_recon_out_kind,
                                                   uint16_t*, unsigned int);
typedef int                 (*pfn_last_preupload_status)(
    igpu_recon_backend*, igpu_recon_preupload_status_t*);

static void* load_blob(const std::string& path, size_t expect_bytes)
{
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) { fprintf(stderr, "cannot open %s\n", path.c_str()); exit(3); }
    fseek(f, 0, SEEK_END); long sz = ftell(f); fseek(f, 0, SEEK_SET);
    if (expect_bytes && (size_t)sz != expect_bytes) {
        fprintf(stderr, "size mismatch %s: got %ld want %zu\n", path.c_str(), sz, expect_bytes);
        exit(3);
    }
    void* p = malloc(sz);
    if (fread(p, 1, sz, f) != (size_t)sz) { fprintf(stderr, "short read %s\n", path.c_str()); exit(3); }
    fclose(f);
    return p;
}

static bool file_exists(const std::string& path)
{
    FILE* f = fopen(path.c_str(), "rb");
    if (!f) return false;
    fclose(f);
    return true;
}

struct VectorScalars
{
    int width = W;
    int height = H;
    int black_level = 131008;
    int white_level = 960000;
    int white_darkened = 174632;
    int black_delta = 0;
    double ev_correction = 3.0;
    double dark_noise = 512.0;
    int interp_method = 1;
    int use_alias_map = 1;
    int use_fullres = 1;
    int chroma_smooth_method = 0;
    int apply_dither = 0;
    int is_bright[4] = {1, 1, 0, 0};
};

static bool parse_scalars_file(const std::string& path, VectorScalars* scalars)
{
    if (!scalars || !file_exists(path)) return false;
    FILE* f = fopen(path.c_str(), "r");
    if (!f) return false;

    char line[512];
    while (fgets(line, sizeof(line), f)) {
        char key[128] = {0};
        char value[256] = {0};
        if (sscanf(line, " %127[^=]=%255s", key, value) != 2) continue;
        if (strcmp(key, "width") == 0) scalars->width = atoi(value);
        else if (strcmp(key, "height") == 0) scalars->height = atoi(value);
        else if (strcmp(key, "black_level") == 0) scalars->black_level = atoi(value);
        else if (strcmp(key, "white_level") == 0) scalars->white_level = atoi(value);
        else if (strcmp(key, "black20") == 0) scalars->black_level = atoi(value);
        else if (strcmp(key, "white20") == 0) scalars->white_level = atoi(value);
        else if (strcmp(key, "white_darkened") == 0) scalars->white_darkened = atoi(value);
        else if (strcmp(key, "black_delta") == 0) scalars->black_delta = atoi(value);
        else if (strcmp(key, "black_delta20") == 0) scalars->black_delta = atoi(value);
        else if (strcmp(key, "ev_correction") == 0) scalars->ev_correction = atof(value);
        else if (strcmp(key, "dark_noise") == 0) scalars->dark_noise = atof(value);
        else if (strcmp(key, "dark_noise20") == 0) scalars->dark_noise = atof(value);
        else if (strcmp(key, "dark_noise_20bit") == 0) scalars->dark_noise = atof(value);
        else if (strcmp(key, "interp_method") == 0) scalars->interp_method = atoi(value);
        else if (strcmp(key, "use_alias_map") == 0) scalars->use_alias_map = atoi(value);
        else if (strcmp(key, "use_fullres") == 0) scalars->use_fullres = atoi(value);
        else if (strcmp(key, "chroma_smooth_method") == 0) scalars->chroma_smooth_method = atoi(value);
        else if (strcmp(key, "apply_dither") == 0) scalars->apply_dither = atoi(value);
        else if (strcmp(key, "is_bright") == 0) {
            int b0 = 0, b1 = 0, b2 = 0, b3 = 0;
            if (sscanf(value, "%d,%d,%d,%d", &b0, &b1, &b2, &b3) == 4) {
                scalars->is_bright[0] = b0;
                scalars->is_bright[1] = b1;
                scalars->is_bright[2] = b2;
                scalars->is_bright[3] = b3;
            }
        }
        else if (strcmp(key, "is_bright0") == 0) scalars->is_bright[0] = atoi(value);
        else if (strcmp(key, "is_bright1") == 0) scalars->is_bright[1] = atoi(value);
        else if (strcmp(key, "is_bright2") == 0) scalars->is_bright[2] = atoi(value);
        else if (strcmp(key, "is_bright3") == 0) scalars->is_bright[3] = atoi(value);
    }
    fclose(f);
    return true;
}

struct HiddenGlContext
{
    HWND hwnd;
    HDC hdc;
    HGLRC hglrc;
};

static bool create_hidden_gl_context(HiddenGlContext* gl)
{
    if (!gl) return false;
    memset(gl, 0, sizeof(*gl));

    WNDCLASSA wc;
    memset(&wc, 0, sizeof(wc));
    wc.style = CS_OWNDC;
    wc.lpfnWndProc = DefWindowProcA;
    wc.hInstance = GetModuleHandleA(NULL);
    wc.lpszClassName = "MLVAppReconDllTestHiddenGl";
    RegisterClassA(&wc);

    gl->hwnd = CreateWindowA(wc.lpszClassName,
                             "MLVApp recon dll GL texture test",
                             WS_OVERLAPPEDWINDOW,
                             0,
                             0,
                             64,
                             64,
                             NULL,
                             NULL,
                             wc.hInstance,
                             NULL);
    if (!gl->hwnd) {
        fprintf(stderr, "[dll_test] CreateWindowA for hidden GL context failed (err=%lu)\n",
                (unsigned long)GetLastError());
        return false;
    }

    gl->hdc = GetDC(gl->hwnd);
    if (!gl->hdc) {
        fprintf(stderr, "[dll_test] GetDC for hidden GL context failed\n");
        DestroyWindow(gl->hwnd);
        memset(gl, 0, sizeof(*gl));
        return false;
    }

    PIXELFORMATDESCRIPTOR pfd;
    memset(&pfd, 0, sizeof(pfd));
    pfd.nSize = sizeof(pfd);
    pfd.nVersion = 1;
    pfd.dwFlags = PFD_DRAW_TO_WINDOW | PFD_SUPPORT_OPENGL | PFD_DOUBLEBUFFER;
    pfd.iPixelType = PFD_TYPE_RGBA;
    pfd.cColorBits = 32;
    pfd.cDepthBits = 24;
    pfd.iLayerType = PFD_MAIN_PLANE;

    const int pixel_format = ChoosePixelFormat(gl->hdc, &pfd);
    if (pixel_format == 0 || !SetPixelFormat(gl->hdc, pixel_format, &pfd)) {
        fprintf(stderr, "[dll_test] ChoosePixelFormat/SetPixelFormat failed (err=%lu)\n",
                (unsigned long)GetLastError());
        ReleaseDC(gl->hwnd, gl->hdc);
        DestroyWindow(gl->hwnd);
        memset(gl, 0, sizeof(*gl));
        return false;
    }

    gl->hglrc = wglCreateContext(gl->hdc);
    if (!gl->hglrc || !wglMakeCurrent(gl->hdc, gl->hglrc)) {
        fprintf(stderr, "[dll_test] wglCreateContext/wglMakeCurrent failed (err=%lu)\n",
                (unsigned long)GetLastError());
        if (gl->hglrc) wglDeleteContext(gl->hglrc);
        ReleaseDC(gl->hwnd, gl->hdc);
        DestroyWindow(gl->hwnd);
        memset(gl, 0, sizeof(*gl));
        return false;
    }

    printf("[dll_test] hidden GL context OK renderer=\"%s\"\n",
           (const char*)glGetString(GL_RENDERER));
    return true;
}

static void destroy_hidden_gl_context(HiddenGlContext* gl)
{
    if (!gl) return;
    wglMakeCurrent(NULL, NULL);
    if (gl->hglrc) wglDeleteContext(gl->hglrc);
    if (gl->hwnd && gl->hdc) ReleaseDC(gl->hwnd, gl->hdc);
    if (gl->hwnd) DestroyWindow(gl->hwnd);
    memset(gl, 0, sizeof(*gl));
}

static long long compare_to_oracle(const char* label,
                                   const uint16_t* result,
                                   const uint16_t* h_out,
                                   size_t n,
                                   size_t width)
{
    long long maxabs = 0;
    double sumabs = 0;
    size_t le0 = 0, le1 = 0, le2 = 0;
    size_t fx = 0, fy = 0;
    long long fd = 0;
    int found = 0;
    for (size_t i = 0; i < n; i++) {
        long long d = (long long)result[i] - (long long)h_out[i];
        long long a = d < 0 ? -d : d;
        if (a > maxabs) maxabs = a;
        sumabs += (double)a;
        if (a <= 0) le0++;
        if (a <= 1) le1++;
        if (a <= 2) le2++;
        if (a > 0 && !found) { found = 1; fx = i % width; fy = i / width; fd = d; }
    }

    printf("\n[dll_test] PARITY THROUGH THE ABI vs oracle out.u16 (%s):\n", label);
    printf("  max abs diff   = %lld LSB\n", maxabs);
    printf("  mean abs diff  = %.6f LSB\n", sumabs / (double)n);
    printf("  within 0 LSB   = %.4f%% (%zu/%zu)\n", 100.0 * le0 / n, le0, n);
    printf("  within 1 LSB   = %.4f%% (%zu/%zu)\n", 100.0 * le1 / n, le1, n);
    printf("  within 2 LSB   = %.4f%% (%zu/%zu)\n", 100.0 * le2 / n, le2, n);
    if (found) printf("  first mismatch @(%zu,%zu) dll-oracle=%lld\n", fx, fy, fd);
    return maxabs;
}

#define RESOLVE(var, type, name) \
    var = (type)GetProcAddress(dll, name); \
    if (!var) { fprintf(stderr, "GetProcAddress failed for %s\n", name); return 4; }

int main(int argc, char** argv)
{
    std::string vdir = "G:\\Temp\\mlv-gpu-profile\\oracle\\vectors";
    std::string dllpath = "igpu_recon_cuda.dll";
    bool run_gl_texture = false;
    int positional = 0;
    for (int i = 1; i < argc; ++i) {
        if (strcmp(argv[i], "--gl-texture") == 0) {
            run_gl_texture = true;
        } else if (positional == 0) {
            vdir = argv[i];
            positional++;
        } else if (positional == 1) {
            dllpath = argv[i];
            positional++;
        } else {
            fprintf(stderr,
                    "usage: dll_test.exe [--gl-texture] [vectors_dir] [dll_path]\n");
            return 2;
        }
    }
    auto P = [&](const char* n) { return vdir + "\\" + n; };
    VectorScalars scalars;
    const bool loaded_scalars = parse_scalars_file(P("scalars.txt"), &scalars);
    if (scalars.width <= 0 || scalars.height <= 0) {
        fprintf(stderr, "[dll_test] invalid vector dimensions %dx%d\n",
                scalars.width, scalars.height);
        return 3;
    }
    const size_t n = (size_t)scalars.width * (size_t)scalars.height;

    printf("[dll_test] vectors dir : %s\n", vdir.c_str());
    printf("[dll_test] dll path    : %s\n", dllpath.c_str());
    printf("[dll_test] gl texture  : %s\n", run_gl_texture ? "enabled" : "disabled");
    printf("[dll_test] scalars     : %s\n", loaded_scalars ? "scalars.txt" : "built-in defaults");

    /* ---- 1. runtime-load the DLL + resolve the ABI ---- */
    HMODULE dll = LoadLibraryA(dllpath.c_str());
    if (!dll) {
        fprintf(stderr, "[dll_test] LoadLibrary('%s') failed (err=%lu)\n",
                dllpath.c_str(), (unsigned long)GetLastError());
        return 4;
    }
    printf("[dll_test] LoadLibrary OK\n");

    pfn_create      f_create;
    pfn_destroy     f_destroy;
    pfn_abi_version f_abi;
    pfn_describe    f_describe;
    pfn_set_clip    f_set_clip;
    pfn_set_luts    f_set_luts;
    pfn_run         f_run;
    pfn_last_timing f_timing;
    pfn_preupload_frame f_preupload;
    pfn_run_preuploaded f_run_preuploaded;
    pfn_last_preupload_status f_preupload_status;
    RESOLVE(f_create,   pfn_create,      "igpu_recon_create");
    RESOLVE(f_destroy,  pfn_destroy,     "igpu_recon_destroy");
    RESOLVE(f_abi,      pfn_abi_version, "igpu_recon_abi_version");
    RESOLVE(f_describe, pfn_describe,    "igpu_recon_describe");
    RESOLVE(f_set_clip, pfn_set_clip,    "igpu_recon_set_clip");
    RESOLVE(f_set_luts, pfn_set_luts,    "igpu_recon_set_luts");
    RESOLVE(f_run,      pfn_run,         "igpu_recon_run");
    RESOLVE(f_timing,   pfn_last_timing, "igpu_recon_last_timing");
    RESOLVE(f_preupload, pfn_preupload_frame, "igpu_recon_preupload_frame");
    RESOLVE(f_run_preuploaded, pfn_run_preuploaded, "igpu_recon_run_preuploaded");
    RESOLVE(f_preupload_status, pfn_last_preupload_status,
            "igpu_recon_last_preupload_status");
    printf("[dll_test] resolved required ABI + async-H2D extension symbols via GetProcAddress\n");

    /* ---- 2. load oracle in.u16 + out.u16 + the 4 LUTs ---- */
    uint16_t* h_in  = (uint16_t*)load_blob(P("in.u16"),  n * sizeof(uint16_t));
    uint16_t* h_out = (uint16_t*)load_blob(P("out.u16"), n * sizeof(uint16_t));
    int*    h_raw2ev = (int*)   load_blob(P("raw2ev.i32"),     (size_t)RAW2EV_COUNT * sizeof(int));
    int*    h_ev2raw = (int*)   load_blob(P("ev2raw.i32"),     (size_t)EV2RAW_COUNT * sizeof(int));
    double* h_mix    = (double*)load_blob(P("mix_curve.f64"),  (size_t)RAW2EV_COUNT * sizeof(double));
    double* h_frc    = (double*)load_blob(P("fullres_curve.f64"), (size_t)RAW2EV_COUNT * sizeof(double));
    float*  h_randn  = NULL;
    if (scalars.apply_dither || file_exists(P("randn05.f32"))) {
        h_randn = (float*)load_blob(P("randn05.f32"), (size_t)RANDN05_COUNT * sizeof(float));
    }
    printf("[dll_test] loaded in.u16 + out.u16 + 4 LUTs%s\n",
           h_randn ? " + randn05" : "");

    /* ---- 3. create / configure / run THROUGH the ABI ---- */
    igpu_recon_backend* b = f_create("cuda");
    if (!b) { fprintf(stderr, "[dll_test] create('cuda') returned NULL\n"); return 4; }
    printf("[dll_test] create OK  abi=%d  describe=\"%s\"\n", f_abi(b), f_describe(b));

    igpu_recon_clip_t clip;
    memset(&clip, 0, sizeof(clip));
    clip.width  = scalars.width;
    clip.height = scalars.height;
    clip.black_level = scalars.black_level;
    clip.white_level = scalars.white_level;
    clip.is_bright[0] = scalars.is_bright[0];
    clip.is_bright[1] = scalars.is_bright[1];
    clip.is_bright[2] = scalars.is_bright[2];
    clip.is_bright[3] = scalars.is_bright[3];
    if (f_set_clip(b, &clip) != 0) { fprintf(stderr, "[dll_test] set_clip failed\n"); return 4; }
    printf("[dll_test] set_clip OK  (%dx%d  black=%d white=%d is_bright=%d%d%d%d)\n",
           clip.width, clip.height, clip.black_level, clip.white_level,
           clip.is_bright[0], clip.is_bright[1], clip.is_bright[2], clip.is_bright[3]);

    igpu_recon_luts_t luts;
    memset(&luts, 0, sizeof(luts));
    luts.raw2ev        = h_raw2ev;
    luts.ev2raw        = h_ev2raw;
    luts.mix_curve     = h_mix;
    luts.fullres_curve = h_frc;
    luts.randn05       = h_randn;
    if (f_set_luts(b, &luts) != 0) { fprintf(stderr, "[dll_test] set_luts failed\n"); return 4; }
    printf("[dll_test] set_luts OK  (uploaded 4 LUTs)\n");

    igpu_recon_frame_t frame;
    memset(&frame, 0, sizeof(frame));
    frame.ev_correction       = scalars.ev_correction;
    frame.black_delta         = scalars.black_delta;
    frame.white_darkened      = scalars.white_darkened;
    frame.dark_noise          = scalars.dark_noise;
    frame.interp_method       = scalars.interp_method;
    frame.use_alias_map       = scalars.use_alias_map;
    frame.use_fullres         = scalars.use_fullres;
    frame.chroma_smooth_method= scalars.chroma_smooth_method;
    frame.apply_dither        = scalars.apply_dither;
    printf("[dll_test] frame state ev=%.6f black_delta=%d white_darkened=%d "
           "dark_noise=%.6f interp=%d alias=%d fullres=%d chroma=%d dither=%d\n",
           frame.ev_correction,
           frame.black_delta,
           frame.white_darkened,
           frame.dark_noise,
           frame.interp_method,
           frame.use_alias_map,
           frame.use_fullres,
           frame.chroma_smooth_method,
           frame.apply_dither);

    uint16_t* result = (uint16_t*)malloc(n * sizeof(uint16_t));
    memset(result, 0, n * sizeof(uint16_t));

    int rc = f_run(b, &frame, h_in, IGPU_OUT_CPU16, result, 0);
    if (rc != 0) { fprintf(stderr, "[dll_test] run() returned %d\n", rc); return 4; }
    printf("[dll_test] run OK\n");

    /* ---- 4. compare to oracle out.u16 ---- */
    long long maxabs = compare_to_oracle("CPU16", result, h_out, n, (size_t)clip.width);

    /* ---- 5. timing ---- */
    igpu_recon_timing_t t;
    if (f_timing(b, &t) == 0) {
        printf("\n[dll_test] last_timing (ms):\n");
        printf("  upload   = %.3f\n", t.upload_ms);
        printf("  kernel   = %.3f\n", t.kernel_ms);
        printf("  download = %.3f\n", t.download_ms);
        printf("  total    = %.3f\n", t.total_ms);
    }

    /* ---- 6. async-H2D exact-match use + mismatch fail-closed fallback ---- */
    uint16_t* preuploaded_result = (uint16_t*)malloc(n * sizeof(uint16_t));
    memset(preuploaded_result, 0, n * sizeof(uint16_t));
    if (f_preupload(b, 41, h_in, n * sizeof(uint16_t), 1) != 0) {
        fprintf(stderr, "[dll_test] exact preupload rejected\n");
        return 4;
    }
    rc = f_run_preuploaded(b, 41, &frame, h_in,
                           IGPU_OUT_CPU16, preuploaded_result, 0);
    if (rc != 0) {
        fprintf(stderr, "[dll_test] run_preuploaded(exact) returned %d\n", rc);
        return 4;
    }
    igpu_recon_preupload_status_t preupload_status;
    memset(&preupload_status, 0, sizeof(preupload_status));
    if (f_preupload_status(b, &preupload_status) != 0 ||
        !preupload_status.accepted ||
        !preupload_status.used ||
        !preupload_status.exact_match) {
        fprintf(stderr,
                "[dll_test] exact preupload status invalid accepted=%d used=%d exact=%d\n",
                preupload_status.accepted,
                preupload_status.used,
                preupload_status.exact_match);
        return 4;
    }
    const long long preupload_maxabs =
        compare_to_oracle("ASYNC_H2D exact", preuploaded_result,
                          h_out, n, (size_t)clip.width);

    /* ---- 6a. cross-frame token mismatch: preupload 41, run 42, SAME bytes.
     * The staged upload must not be consumed for a different frame even when
     * the bytes are byte-identical to what was staged -- the frame token is
     * the sole admission gate, not a content hash. Fallback output must
     * match the plain synchronous run on the same bytes exactly.
     *
     * The frame-41 slot staged for step 6 above was already consumed by that
     * step's own run_preuploaded(41) call -- take_exact_preupload() sets
     * state=0 on acceptance (igpu_recon_cuda.cu), so without restaging here
     * this call would find no live slot at all and could pass on that
     * technicality alone, never exercising the frame-id compare against a
     * slot that IS live. Restage frame 41 immediately before the frame-42
     * call so a live, wrong-token slot is actually on the table to reject. */
    if (f_preupload(b, 41, h_in, n * sizeof(uint16_t), 0) != 0) {
        fprintf(stderr, "[dll_test] cross-frame restage of frame 41 rejected\n");
        return 4;
    }
    uint16_t* cross_frame_result = (uint16_t*)malloc(n * sizeof(uint16_t));
    memset(cross_frame_result, 0, n * sizeof(uint16_t));
    rc = f_run_preuploaded(b, 42, &frame, h_in,
                          IGPU_OUT_CPU16, cross_frame_result, 0);
    if (rc != 0) {
        fprintf(stderr, "[dll_test] run_preuploaded(cross-frame) returned %d\n", rc);
        return 4;
    }
    igpu_recon_preupload_status_t cross_frame_status;
    memset(&cross_frame_status, 0, sizeof(cross_frame_status));
    if (f_preupload_status(b, &cross_frame_status) != 0 ||
        cross_frame_status.accepted ||
        cross_frame_status.used ||
        cross_frame_status.exact_match ||
        memcmp(result, cross_frame_result, n * sizeof(uint16_t)) != 0) {
        fprintf(stderr,
                "[dll_test] cross-frame preupload was not rejected accepted=%d used=%d exact=%d\n",
                cross_frame_status.accepted,
                cross_frame_status.used,
                cross_frame_status.exact_match);
        return 4;
    }
    printf("[dll_test] async-H2D cross-frame (41 staged, 42 run) rejected + fell back correctly PASS\n");

    uint16_t* mutated = (uint16_t*)malloc(n * sizeof(uint16_t));
    uint16_t* sync_mutated = (uint16_t*)malloc(n * sizeof(uint16_t));
    uint16_t* fallback_mutated = (uint16_t*)malloc(n * sizeof(uint16_t));
    memcpy(mutated, h_in, n * sizeof(uint16_t));
    mutated[n / 2] ^= 1u;
    memset(sync_mutated, 0, n * sizeof(uint16_t));
    memset(fallback_mutated, 0, n * sizeof(uint16_t));
    rc = f_run(b, &frame, mutated, IGPU_OUT_CPU16, sync_mutated, 0);
    if (rc != 0) {
        fprintf(stderr, "[dll_test] synchronous mutated oracle returned %d\n", rc);
        return 4;
    }
    if (f_preupload(b, 42, h_in, n * sizeof(uint16_t), 0) != 0) {
        fprintf(stderr, "[dll_test] mismatch preupload rejected\n");
        return 4;
    }
    rc = f_run_preuploaded(b, 42, &frame, mutated,
                           IGPU_OUT_CPU16, fallback_mutated, 0);
    if (rc != 0) {
        fprintf(stderr, "[dll_test] run_preuploaded(mismatch) returned %d\n", rc);
        return 4;
    }
    memset(&preupload_status, 0, sizeof(preupload_status));
    if (f_preupload_status(b, &preupload_status) != 0 ||
        !preupload_status.accepted ||
        preupload_status.used ||
        preupload_status.exact_match ||
        memcmp(sync_mutated, fallback_mutated, n * sizeof(uint16_t)) != 0) {
        fprintf(stderr,
                "[dll_test] mismatch did not fail closed accepted=%d used=%d exact=%d\n",
                preupload_status.accepted,
                preupload_status.used,
                preupload_status.exact_match);
        return 4;
    }
    printf("[dll_test] async-H2D exact path + byte-exact mismatch fallback PASS\n");

    long long gl_maxabs = 0;
    if (run_gl_texture) {
        HiddenGlContext gl;
        if (!create_hidden_gl_context(&gl)) {
            fprintf(stderr, "[dll_test] GL texture mode cannot create a hardware context\n");
            return 5;
        }

        GLuint tex = 0;
        glGenTextures(1, &tex);
        glBindTexture(GL_TEXTURE_2D, tex);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
        glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
        glTexImage2D(GL_TEXTURE_2D,
                     0,
                     GL_R16,
                     clip.width,
                     clip.height,
                     0,
                     GL_RED,
                     GL_UNSIGNED_SHORT,
                     NULL);
        GLenum gerr = glGetError();
        if (gerr != GL_NO_ERROR) {
            fprintf(stderr, "[dll_test] glTexImage2D(GL_R16) failed GL error 0x%04x\n", gerr);
            destroy_hidden_gl_context(&gl);
            return 5;
        }

        int gl_rc = f_run(b, &frame, h_in, IGPU_OUT_GL_TEXTURE, NULL, (unsigned int)tex);
        if (gl_rc != 0) {
            fprintf(stderr, "[dll_test] run(GL_TEXTURE) returned %d\n", gl_rc);
            glDeleteTextures(1, &tex);
            destroy_hidden_gl_context(&gl);
            return 5;
        }
        printf("[dll_test] run(GL_TEXTURE) OK\n");

        if (f_timing(b, &t) == 0) {
            printf("\n[dll_test] last_timing GL_TEXTURE (ms):\n");
            printf("  upload   = %.3f\n", t.upload_ms);
            printf("  kernel   = %.3f\n", t.kernel_ms);
            printf("  interop  = %.3f\n", t.download_ms);
            printf("  total    = %.3f\n", t.total_ms);
        }

        uint16_t* gl_result = (uint16_t*)malloc(n * sizeof(uint16_t));
        memset(gl_result, 0, n * sizeof(uint16_t));
        glBindTexture(GL_TEXTURE_2D, tex);
        glFinish();
        glGetTexImage(GL_TEXTURE_2D, 0, GL_RED, GL_UNSIGNED_SHORT, gl_result);
        gerr = glGetError();
        if (gerr != GL_NO_ERROR) {
            fprintf(stderr, "[dll_test] glGetTexImage(GL_R16) failed GL error 0x%04x\n", gerr);
            free(gl_result);
            glDeleteTextures(1, &tex);
            destroy_hidden_gl_context(&gl);
            return 5;
        }

        gl_maxabs = compare_to_oracle(
            "GL_TEXTURE/R16 readback for test",
            gl_result,
            h_out,
            n,
            (size_t)clip.width);
        free(gl_result);
        glDeleteTextures(1, &tex);
        destroy_hidden_gl_context(&gl);
    }

    f_destroy(b);
    FreeLibrary(dll);

    const bool pass = (maxabs == 0) &&
                      (preupload_maxabs == 0) &&
                      (!run_gl_texture || gl_maxabs == 0);
    printf("\n[dll_test] RESULT: %s (0 LSB target, through the ABI%s)\n",
           pass ? "PASS" : "FAIL",
           run_gl_texture ? " + GL_TEXTURE" : "");
    return pass ? 0 : 1;
}
