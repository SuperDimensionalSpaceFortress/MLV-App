/* oracle_main.c - standalone CPU "oracle" for the MLV-App Dual ISO full-20-bit
 * reconstruction (diso_get_full20bit, src/mlv/llrawproc/dualiso.c:4735).
 *
 * Purpose
 *   Produce a bit-exact scalar reference output + test vectors for CUDA parity
 *   testing of the GPU recon backend (tools/gpu/igpu_recon.h). It generates a
 *   DETERMINISTIC synthetic dual-ISO Bayer frame from a fixed closed-form
 *   formula (so the CUDA side can regenerate byte-identical input), runs the
 *   exact documented pre-call sequence + diso_get_full20bit on the scalar path
 *   (MLVAPP_DISABLE_AVX2=1), then dumps:
 *
 *     in.u16          input 14-bit Bayer            uint16 LE [w*h]
 *     out.u16         output 16-bit Bayer           uint16 LE [w*h]
 *     raw2ev.i32      raw(20b) -> EV*65536 LUT       int32  LE [1<<20]
 *     ev2raw.i32      EV -> raw(20b) LUT (base)      int32  LE [24*65536]
 *     mix_curve.f64   mix blend factor              double LE [1<<20]
 *     fullres_curve.f64  fullres confidence (float curve, cast f64) double LE [1<<20]
 *     scalars.txt     captured out-params / context scalars
 *     README.txt      format spec + synthetic formula + scalars
 *
 *   raw2ev / ev2raw / mix_curve are read back from the scratch struct after the
 *   run (the EXACT LUTs the recon used). fullres_curve is rebuilt here with the
 *   report's exact formula because the recon stores it in a file-scope static
 *   inside dualiso.c that is not reachable from the scratch struct; the rebuild
 *   uses the identical constants (fullres_start=4, fullres_transition=4) and the
 *   production-default FLOAT curve (build_fullres_curve_float) cast to double, to
 *   match what final_blend actually gathers.
 *
 * Build: see build-oracle.ps1 (mingw gcc, -O2 -fopenmp -lpthread -lm
 *        -DSTDOUT_SILENT, run with MLVAPP_DISABLE_AVX2=1).
 *
 * This file lives entirely under tools/gpu/oracle/ and does NOT modify any
 * src/ files. It #includes dualiso.c + hist.c directly into this TU.
 *
 * AMaZE (interp_method=0, the PRODUCTION DEFAULT) support
 * ------------------------------------------------------
 * When built WITH the real demosaic (-DORACLE_REAL_DEMOSAIC, which the
 * build script sets by default and which compiles src/debayer/amaze_demosaic.c
 * into this TU), the "--case amaze*" cases run interp_method=0 SINGLE-THREAD
 * (threads=1, so demosaic processes the whole image as one chunk -> fully
 * deterministic, no pthread fan-out / chunk-boundary effects). In that mode the
 * oracle additionally dumps the AMaZE-specific intermediate planes the CUDA
 * edge-directed reconstruction port consumes / is validated against:
 *
 *   amaze_squeezed.i32          int32 LE [h]      squeezed[y] row remap
 *   amaze_rawData.f32           f32   LE [h*wx]   pre-demosaic prescaled cfa
 *   amaze_red.f32               f32   LE [h*wx]   demosaiced + clamped red plane
 *   amaze_green.f32             f32   LE [h*wx]   demosaiced + green-undo plane
 *   amaze_blue.f32              f32   LE [h*wx]   demosaiced + clamped blue plane
 *   amaze_gray.u32              u32   LE [w*h]    grayscale = g/2+r/4+b/4 (trunc)
 *   amaze_edge_direction.u8     u8    LE [w*h]    selected edge direction index
 *   amaze_row_width.txt (in scalars: amaze_row_width=wx) -- wx = w+16
 *
 * The demosaiced red/green/blue planes are the validated handoff point between
 * the generic RawTherapee AMaZE demosaic (an upstream component) and the
 * dualiso-owned edge-directed reconstruction. The CUDA port reproduces every
 * dualiso-owned AMaZE stage (gray, edge_direction, edge_interp -> dark/bright)
 * starting from these planes; the demosaic itself is the one upstream stage that
 * is NOT re-ported (its SSE2 float core is the source of the engine's own
 * internal AVX2/scalar +/-1 LSB tolerance and is validated separately).
 *
 * The legacy mean23 cases keep working unchanged. When built WITHOUT
 * -DORACLE_REAL_DEMOSAIC a one-line demosaic() stub satisfies the link symbol
 * and the AMaZE cases are unavailable.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <math.h>
#include <sys/stat.h>
#if defined(_OPENMP)
#include <omp.h>   /* omp_set_num_threads: force scalar dither single-thread */
#endif
#if defined(_WIN32)
#include <direct.h>
#define ORACLE_MKDIR(p) _mkdir(p)
#else
#define ORACLE_MKDIR(p) mkdir((p), 0755)
#endif

/* ---- bring in the recon as a single TU --------------------------------- *
 * dualiso.c inline-#includes dualiso_avx2.inc, dualiso_amaze_avx2.inc,
 * dualiso_mix_chroma_avx2.inc and chroma_smooth.c (x3). hist.c is compiled into the same TU. The only external
 * symbol diso_get_full20bit's reachable code references that is not defined in
 * dualiso.c is demosaic() (debayer.h) - referenced only in the AMaZE worker
 * (dualiso.c:2809), never entered for interp_method=1. Provide a stub before
 * the include so the symbol resolves.                                       */
#include "../../../src/debayer/debayer.h"

#ifndef ORACLE_REAL_DEMOSAIC
/* mean23-only build: the AMaZE branch is never entered, so a one-line stub
 * satisfies the link-time demosaic() symbol reference. */
int
#ifdef __MINGW32__
__attribute__ ((force_align_arg_pointer))
#endif
demosaic(amazeinfo_t * inputdata)
{
    (void)inputdata;
    return 1;
}
#endif /* !ORACLE_REAL_DEMOSAIC */

#include "../../../src/mlv/llrawproc/hist.c"
#include "../../../src/mlv/llrawproc/dualiso.c"

#ifdef ORACLE_REAL_DEMOSAIC
/* Real AMaZE demosaic, compiled INTO this TU so interp_method=0 runs the
 * engine's actual edge-directed reconstruction. amaze_demosaic.c #includes
 * sleefsseavx.c (SSE helpers) and debayer.h; it has no other src deps for the
 * single demosaic() entry. NOTE: amaze_demosaic.c redefines MIN/MAX/COERCE/SQR
 * as its own (RawTherapee) macros, which collide with dualiso.c's. Undef the
 * dualiso ones first so the demosaic TU sees its intended definitions. The
 * demosaic() body is fully self-contained after that point.
 *
 * ORACLE_SCALAR_DEMOSAIC (diagnostic): undef __SSE2__ around the demosaic
 * include so amaze_demosaic.c + sleefsseavx.c take their scalar `#else`
 * branches. This builds a SECOND reference whose only difference from the
 * default SSE2 oracle is the demosaic float path. Diffing the two oracles'
 * out.u16 quantifies how much the demosaic's float-path choice perturbs the
 * FINAL output -- i.e. the upper bound a GPU demosaic port (a third float path)
 * would contribute to the end-to-end gap. The rest of the TU (dualiso.c) keeps
 * SSE2 (it has no intrinsics; it is scalar C either way). */
#undef MIN
#undef MAX
#undef COERCE
#undef ABS
#ifdef ORACLE_SCALAR_DEMOSAIC
# pragma push_macro("__SSE2__")
# undef __SSE2__
#endif
#include "../../../src/debayer/amaze_demosaic.c"
#ifdef ORACLE_SCALAR_DEMOSAIC
# pragma pop_macro("__SSE2__")
#endif
#endif /* ORACLE_REAL_DEMOSAIC */

/* ====================================================================== *
 *  DETERMINISTIC SYNTHETIC DUAL-ISO BAYER GENERATOR
 *
 *  Closed-form, integer, reproducible byte-for-byte on the CUDA side.
 *  See README.txt for the prose spec. All arithmetic below is exact:
 *  integer ops + a single double-precision sin() per pixel (rounded with
 *  floor(.. + 0.5)). To stay byte-identical the CUDA generator must use the
 *  same IEEE-754 double sin() and the same rounding.
 *
 *  Constants (FIXED - do not change without regenerating the CUDA side):     */
#define ORACLE_BLACK_14   2047    /* raw_info.black_level (14-bit)            */
#define ORACLE_WHITE_14   15000   /* raw_info.white_level (14-bit)            */

/* Bright-field pattern per (y & 3): default {1,1,0,0}. Bright rows
 * (is_bright==1) simulate the LOW-iso-100 exposure that holds ~3 EV more
 * highlight signal; dark rows (is_bright==0) simulate iso-800 (darker, noisier
 * conceptually). The default {1,1,0,0} matches iso_patterns[0] in dualiso.c so
 * auto-detect resolves to pattern index 0.
 *
 * NON-BASE PARAMETERIZATION (added for the stage-diff localization work):
 *   This array is NO LONGER const. It is seeded to {1,1,0,0} at startup so the
 *   DEFAULT (env unset) behavior is byte-identical to before, and may be
 *   overridden by ORACLE_IS_BRIGHT="abcd" (four 0/1 chars) to generate non-base
 *   diagnostic vectors. See oracle_apply_env_overrides() in main().            */
static int ORACLE_IS_BRIGHT[4] = {1, 1, 0, 0};

/* ---- non-base diagnostic axes (env-controlled, default == base) ----------- *
 * All three default to the current base values so existing base/iso1600/clipped
 * vectors regenerate byte-identically when the env vars are unset.
 *
 *   ORACLE_IS_BRIGHT   four 0/1 chars, e.g. "0110" or "1100" (default 1100)
 *   ORACLE_BLACK_DELTA integer in 14-bit units, default 0. This is the value
 *                      passed to diso_get_full20bit's *black_delta in/out param.
 *                      match_exposures (dualiso.c:2529-2538) does:
 *                          if (*black_delta != -1) _black_delta = *black_delta*64;
 *                          _black_delta = COERCE(_black_delta, 0, 100*64);
 *                          *black_delta = _black_delta / 64;   // 14-bit readback
 *                      so the APPLY value used internally (and recorded as
 *                      black_delta20 in scalars.txt) is (ORACLE_BLACK_DELTA*64),
 *                      clamped to [0, 6400]. The 14-bit value is what the API
 *                      exposes; the 20-bit value in scalars is x64.
 *   ORACLE_APPLY_DITHER 0/1, default 0. When 1, the randn05 LUT is initialized
 *                      deterministically (see oracle_build_randn05) and the
 *                      scalar convert_20_to_16bit path's fast_randn05() applies
 *                      it, exercising the anti-posterization dither the base
 *                      vectors never touch. The LUT is dumped to randn05.f32 so
 *                      a consumer can load the EXACT table used. See the dither
 *                      fidelity caveat in README.txt.
 *   ORACLE_USE_ALIAS_MAP 0/1, default 1. Exercises the final-blend alias gate.
 *   ORACLE_CHROMA_SMOOTH_METHOD 0/1, default 0. Exercises optional 2x2 chroma. */
static int    g_oracle_apply_dither = 0;
static int    g_oracle_black_delta  = 0;   /* 14-bit units, default 0 */
static int    g_oracle_use_alias_map = 1;
static int    g_oracle_chroma_smooth_method = 0;

/* ---- auto_correction (match-by) diagnostic axis (env-controlled) ----------- *
 * ORACLE_AUTO_CORRECTION selects which match_exposures branch diso_get_full20bit
 * takes (dualiso.c:2499-2522):
 *   unset / -1  -> ISO-ratio path (the historical oracle default; corr_ev =
 *                  log2(iso2/iso1), black_delta from the ISO pair). BYTE-IDENTICAL
 *                  to before when unset, so every existing vector regenerates
 *                  unchanged.
 *   -2          -> match_by_histogram path (dualiso.c:2329). This is the EXACT
 *                  runtime configuration the GUI uses for a DISO_FORCED clip
 *                  (MainWindow.cpp:11015-11021): diso_auto_correction=-2,
 *                  diso_ev_correction=1 (sentinel), diso_black_delta=-1 (sentinel).
 *                  Those two sentinels are what make match_exposures KEEP the
 *                  histogram-derived _ev_correction/_black_delta instead of
 *                  overwriting them with caller values (the override at
 *                  dualiso.c:2524 / :2529 only fires for ev!=1 / bd!=-1).
 *
 * When -2 is selected the oracle therefore passes ev_correction=1 and
 * black_delta=-1 as the in/out sentinels (overriding the LOAD-mode bd=-1 default,
 * which already coincides), so diso_get_full20bit runs match_by_histogram and the
 * APPLIED a/b20 reach the pixels. The captured ev_correction/black_delta read
 * back the histogram values (dualiso.c:2537-2538). Default (-1) leaves the whole
 * param block untouched -> base/iso1600/clipped/live(ISO-ratio) vectors are
 * byte-identical. */
static int    g_oracle_auto_correction      = -1;  /* -1 = ISO-ratio (default), -2 = histogram */
static int    g_oracle_auto_correction_set  = 0;   /* 1 if ORACLE_AUTO_CORRECTION was set */

/* Parse an env var "0110"/"1100" (exactly 4 chars, each '0' or '1') into the
 * mutable ORACLE_IS_BRIGHT[4]. Leaves it untouched (default {1,1,0,0}) when the
 * var is unset or malformed, so unset == base. Returns 1 if an override was
 * applied, 0 otherwise. */
static int oracle_parse_is_bright_env(void)
{
    const char * s = getenv("ORACLE_IS_BRIGHT");
    if (!s || !*s) return 0;
    /* accept exactly 4 binary digits */
    int v[4];
    for (int i = 0; i < 4; i++) {
        if (s[i] != '0' && s[i] != '1') {
            fprintf(stderr, "[oracle] ignoring malformed ORACLE_IS_BRIGHT='%s' "
                            "(need 4 chars of 0/1, e.g. 0110)\n", s);
            return 0;
        }
        v[i] = s[i] - '0';
    }
    if (s[4] != '\0') {
        fprintf(stderr, "[oracle] ignoring malformed ORACLE_IS_BRIGHT='%s' "
                        "(need exactly 4 chars)\n", s);
        return 0;
    }
    for (int i = 0; i < 4; i++) ORACLE_IS_BRIGHT[i] = v[i];
    return 1;
}

/* Read ORACLE_BLACK_DELTA / ORACLE_APPLY_DITHER. Unset -> defaults (0/0). */
static void oracle_parse_scalar_env(void)
{
    const char * bd = getenv("ORACLE_BLACK_DELTA");
    if (bd && *bd) g_oracle_black_delta = atoi(bd);
    const char * dt = getenv("ORACLE_APPLY_DITHER");
    if (dt && *dt) g_oracle_apply_dither = (atoi(dt) != 0);
    const char * am = getenv("ORACLE_USE_ALIAS_MAP");
    if (am && *am) g_oracle_use_alias_map = (atoi(am) != 0);
    const char * cs = getenv("ORACLE_CHROMA_SMOOTH_METHOD");
    if (cs && *cs) {
        const int v = atoi(cs);
        if (v >= 0 && v <= 1) {
            g_oracle_chroma_smooth_method = v;
        } else {
            fprintf(stderr, "[oracle] ignoring unsupported ORACLE_CHROMA_SMOOTH_METHOD=%d "
                            "(supported for CUDA parity: 0/1)\n", v);
        }
    }
    const char * ac = getenv("ORACLE_AUTO_CORRECTION");
    if (ac && *ac) {
        int v = atoi(ac);
        if (v == -1 || v == -2) {
            g_oracle_auto_correction     = v;
            g_oracle_auto_correction_set = 1;
        } else {
            fprintf(stderr, "[oracle] ignoring ORACLE_AUTO_CORRECTION='%s' "
                            "(only -1 or -2 supported)\n", ac);
        }
    }
}

/* ---- LOAD-EXTERNAL-INPUT mode (env-controlled, default OFF) ---------------- *
 * When ORACLE_LOAD_INPUT names an existing raw 14-bit Bayer file (uint16 LE,
 * exactly W*H elements), oracle_main loads THAT buffer as image_data instead of
 * synthesizing the deterministic test frame. This lets the oracle reproduce a
 * REAL live capture's Dual ISO state (e.g. M16-1327) so the parity harness can
 * localize which backend stage first diverges from the CPU oracle.
 *
 * Geometry/levels/iso come from companion env vars (defaults preserve the base
 * synthetic-case behavior when ORACLE_LOAD_INPUT is unset):
 *   ORACLE_LOAD_INPUT       path to in.u16 (raw 14-bit Bayer uint16 LE, W*H)
 *   ORACLE_WIDTH/HEIGHT     geometry (must match the file element count)
 *   ORACLE_BLACK_LEVEL_14   raw_info.black_level (14-bit; diso_get_full20bit
 *                           *64's it internally, see dualiso.c:5249-5250)
 *   ORACLE_WHITE_LEVEL_14   raw_info.white_level (14-bit; *64'd internally)
 *   ORACLE_ISO1/ORACLE_ISO2 iso pair driving match_exposures' ISO-ratio path
 *                           (corr_ev = log2(iso2/iso1); for the live M16 state
 *                           100/1600 -> corr_ev 4.0, black_delta20 960)
 *   ORACLE_IS_BRIGHT        four 0/1 chars (already parsed above); ALSO forces
 *                           *iso_pattern so diso_get_full20bit uses that exact
 *                           pattern instead of auto-detecting (see main()).
 *
 * All vars default to unset; with ORACLE_LOAD_INPUT unset every existing code
 * path (synthetic generation, base/iso1600/clipped/amaze cases) is untouched
 * and byte-identical to before. */
static const char * g_oracle_load_input = NULL;  /* NULL -> synthesize (default) */
static int          g_oracle_iso1_ovr   = -1;    /* <0 -> use case default */
static int          g_oracle_iso2_ovr   = -1;
static int          g_oracle_black14_ovr = -1;   /* <0 -> use ORACLE_BLACK_14 */
static int          g_oracle_white14_ovr = -1;   /* <0 -> use ORACLE_WHITE_14 */

static void oracle_parse_load_env(void)
{
    const char * p = getenv("ORACLE_LOAD_INPUT");
    if (p && *p) g_oracle_load_input = p;
    const char * i1 = getenv("ORACLE_ISO1");
    if (i1 && *i1) g_oracle_iso1_ovr = atoi(i1);
    const char * i2 = getenv("ORACLE_ISO2");
    if (i2 && *i2) g_oracle_iso2_ovr = atoi(i2);
    const char * b = getenv("ORACLE_BLACK_LEVEL_14");
    if (b && *b) g_oracle_black14_ovr = atoi(b);
    const char * w = getenv("ORACLE_WHITE_LEVEL_14");
    if (w && *w) g_oracle_white14_ovr = atoi(w);
}

/* Map an ORACLE_IS_BRIGHT pattern to the FORCED *iso_pattern value (1..4) that
 * makes diso_get_full20bit pick iso_patterns[idx] verbatim (dualiso.c:5187,
 * 5215-5218):
 *   iso_patterns[4][4] = {{1,1,0,0},{1,0,0,1},{0,0,1,1},{0,1,1,0}}
 * so pattern value v in 1..4 selects iso_patterns[v-1]. For the live M16 state
 * is_bright={0,1,1,0} == iso_patterns[3] -> forced *iso_pattern = 4. Returns the
 * 1-based forced value, or 0 if the current ORACLE_IS_BRIGHT does not match any
 * known pattern (caller then falls back to auto-detect, *iso_pattern=0). */
static int oracle_forced_iso_pattern(void)
{
    static const int iso_patterns[4][4] =
        {{1,1,0,0}, {1,0,0,1}, {0,0,1,1}, {0,1,1,0}};
    for (int i = 0; i < 4; i++) {
        if (ORACLE_IS_BRIGHT[0] == iso_patterns[i][0] &&
            ORACLE_IS_BRIGHT[1] == iso_patterns[i][1] &&
            ORACLE_IS_BRIGHT[2] == iso_patterns[i][2] &&
            ORACLE_IS_BRIGHT[3] == iso_patterns[i][3])
            return i + 1;
    }
    return 0;
}

/* Load a raw uint16-LE file of exactly `count` elements into `dst`. Host is LE
 * x86-64 so the on-disk bytes ARE the in-memory layout (matches dump_blob). */
static int oracle_load_u16(const char * path, uint16_t * dst, size_t count)
{
    FILE * f = fopen(path, "rb");
    if (!f) { fprintf(stderr, "[oracle] cannot open ORACLE_LOAD_INPUT '%s'\n", path); return 0; }
    /* size check so a width/height mismatch is caught loudly, not silently mis-strided */
    if (fseek(f, 0, SEEK_END) == 0) {
        long sz = ftell(f);
        if (sz >= 0 && (size_t)sz != count * sizeof(uint16_t)) {
            fprintf(stderr, "[oracle] ORACLE_LOAD_INPUT '%s' is %ld bytes, expected %zu "
                            "(W*H*2). Check ORACLE_WIDTH/ORACLE_HEIGHT.\n",
                    path, sz, count * sizeof(uint16_t));
            fclose(f); return 0;
        }
        rewind(f);
    }
    size_t r = fread(dst, sizeof(uint16_t), count, f);
    fclose(f);
    if (r != count) {
        fprintf(stderr, "[oracle] short read from '%s' (%zu/%zu elements)\n", path, r, count);
        return 0;
    }
    fprintf(stderr, "[oracle] loaded external input '%s' (%zu elements)\n", path, count);
    return 1;
}

/* Build the randn05 anti-posterization LUT EXACTLY as the engine's
 * fast_randn_init() does (dualiso.c:1912-1919):
 *     for i in [0,1024): randn05_cache[i] = RANDN / 2;
 *     RANDN = sqrt(-2*log(RAND)) * cos(TWOPI*RAND);  RAND = rand()/RAND_MAX
 * fast_randn_init() consumes rand() in pairs (two RAND draws per entry) in the
 * SAME loop order, so seeding the CRT PRNG identically and replaying the same
 * call sequence reproduces the table byte-for-byte. We seed with srand(0) for a
 * fixed, reproducible table (the engine never seeds, and never calls
 * fast_randn_init at all, so there is no "true" engine seed to match -- see the
 * dither fidelity caveat). We call the engine's own fast_randn_init() so the
 * arithmetic is literally the engine code, not a re-derivation, then copy the
 * resulting randn05_cache out. TWOPI/RANDN/RAND are dualiso.c macros, in scope
 * here because dualiso.c is #included into this TU. */
static void oracle_build_randn05(float out[1024])
{
    srand(0);                 /* fixed, reproducible draw sequence */
    fast_randn_init();        /* engine code path -> fills randn05_cache[1024] */
    memcpy(out, randn05_cache, 1024 * sizeof(float));
}

/* ---------------------------------------------------------------------------
 * CASE SELECTOR
 *
 * Each case is a (name, W, H, iso1, iso2, pattern_variant) tuple. The CUDA
 * probe regenerates in.u16 byte-identically from the same (W,H,iso pair,
 * variant) + the same closed-form, so every case is fully reproducible on the
 * device side and the scalars.txt sidecar carries everything the probe needs.
 *
 * pattern_variant:
 *   0  = "smooth": diagonal gradient + mid-freq sinusoid (original base case).
 *        Bright rows already touch WHITE near the corner, so the clip branch is
 *        lightly exercised, but the bright field is mostly below white.
 *   1  = "clipped": a boosted/offset field designed so a LARGE fraction of the
 *        BRIGHT rows reach or exceed WHITE (saturates), forcing the recon's
 *        fullres clip branch (f >= white_darkened -> MAX(f, dark)), the
 *        overexposed map (bright >= white_darkened, dark >= white), and
 *        final_blend's overexposed + dark-area cap paths to fire over a wide
 *        region. Dark rows stay informative (1/8 exposure, clamped at white).
 *------------------------------------------------------------------------- */
typedef struct {
    const char * name;
    int W, H;
    int iso1, iso2;
    int variant;
    int interp_method;   /* 1 = mean23 (default), 0 = AMaZE (production default) */
} oracle_case_t;

static const oracle_case_t ORACLE_CASES[] = {
    /* name         W      H      iso1  iso2   variant  interp */
    { "base",       1808,  2268,  100,  800,   0,       1 },  /* original mean23 parity case   */
    { "res8k",      8192,  4320,  100,  800,   0,       1 },  /* Case A: 8K, mean23            */
    { "clipped",    1808,  2268,  100,  800,   1,       1 },  /* Case B: saturated, mean23     */
    { "iso1600",    1808,  2268,  100,  1600,  0,       1 },  /* Case C: corr_ev=4, mean23     */
    /* --- AMaZE (interp_method=0, the PRODUCTION DEFAULT) --- */
    { "amaze",      1808,  2268,  100,  800,   0,       0 },  /* AMaZE base (smooth)           */
    { "amaze_clip", 1808,  2268,  100,  800,   1,       0 },  /* AMaZE saturated highlights    */
    { "amaze_iso1600",1808,2268,  100,  1600,  0,       0 },  /* AMaZE corr_ev=4               */
};
#define ORACLE_NUM_CASES ((int)(sizeof(ORACLE_CASES)/sizeof(ORACLE_CASES[0])))

/* Base scene "irradiance" field S(x,y) in [0, 1]: a smooth diagonal gradient
 * plus a mid-frequency sinusoidal texture, fully deterministic. Returns a
 * value in [0,1]. */
static double oracle_scene(int x, int y, int W, int H)
{
    /* diagonal gradient 0..1 */
    double gx = (double)x / (double)(W - 1);
    double gy = (double)y / (double)(H - 1);
    double grad = 0.5 * (gx + gy);                 /* [0,1] */
    /* mid-frequency texture, amplitude 0.18, period ~64px in x and ~48px in y */
    double tex = 0.18 * sin((x * (2.0 * M_PI / 64.0)) + (y * (2.0 * M_PI / 48.0)));
    double s = grad + tex;
    if (s < 0.0) s = 0.0;
    if (s > 1.0) s = 1.0;
    return s;
}

/* Clipped variant scene S'(x,y): same diagonal gradient + texture, then boosted
 * by gain 1.8 and DC offset 0.35 so a large contiguous region maps above 1.0
 * (which the bright-row formula below clamps to WHITE). The pre-clamp value is
 * returned UNclamped (can exceed 1.0); the caller clamps after the iso scale so
 * BRIGHT rows saturate while DARK rows (1/ratio gain) stay well below white. */
static double oracle_scene_clipped(int x, int y, int W, int H)
{
    double gx = (double)x / (double)(W - 1);
    double gy = (double)y / (double)(H - 1);
    double grad = 0.5 * (gx + gy);
    double tex = 0.18 * sin((x * (2.0 * M_PI / 64.0)) + (y * (2.0 * M_PI / 48.0)));
    double s = grad + tex;
    if (s < 0.0) s = 0.0;
    /* boost + offset, intentionally NOT clamped to 1.0 here */
    double s2 = s * 1.8 + 0.35;
    if (s2 < 0.0) s2 = 0.0;
    return s2;   /* may exceed 1.0 -> bright rows clip to WHITE */
}

/* Produce the 14-bit Bayer pixel for (x,y) for a given case.
 *
 * The bright/dark exposure ratio is the ISO ratio (iso2/iso1), so
 * match_exposures on the ISO-ratio path computes corr_ev = log2(iso2/iso1) and
 * matches without aborting:
 *   variant 0/iso 100/800  -> ratio 8  -> corr_ev 3
 *   variant 0/iso 100/1600 -> ratio 16 -> corr_ev 4
 *
 *   signal_span = WHITE - BLACK
 *   dark rows : value = BLACK + S * (signal_span / ratio)   (high iso, /ratio)
 *   bright rows: value = BLACK + S * (signal_span)          (low iso)
 *
 * Both clamp to [BLACK, WHITE]. For variant 1 the bright-row S exceeds 1.0 over
 * a wide region so bright values saturate at WHITE (clip-branch coverage). */
static uint16_t oracle_pixel(int x, int y, const oracle_case_t * c)
{
    int span  = ORACLE_WHITE_14 - ORACLE_BLACK_14;   /* 12953 */
    int ratio = c->iso2 / c->iso1;                   /* 8 or 16 */
    double s = (c->variant == 1)
                 ? oracle_scene_clipped(x, y, c->W, c->H)
                 : oracle_scene(x, y, c->W, c->H);
    int is_bright = ORACLE_IS_BRIGHT[y & 3];
    double v;
    if (is_bright)
        v = (double)ORACLE_BLACK_14 + s * (double)span;
    else
        v = (double)ORACLE_BLACK_14 + s * ((double)span / (double)ratio);
    int iv = (int)floor(v + 0.5);
    if (iv < ORACLE_BLACK_14) iv = ORACLE_BLACK_14;   /* no sub-black for this synthetic */
    if (iv > ORACLE_WHITE_14) iv = ORACLE_WHITE_14;
    return (uint16_t)iv;
}

/* ---- little-endian dump helpers (host is LE x86; write raw bytes) ------- */
static int dump_blob(const char * dir, const char * name, const void * data, size_t bytes)
{
    char path[1024];
    snprintf(path, sizeof(path), "%s/%s", dir, name);
    FILE * f = fopen(path, "wb");
    if (!f) { fprintf(stderr, "[oracle] cannot open %s for write\n", path); return 0; }
    size_t w = fwrite(data, 1, bytes, f);
    fclose(f);
    if (w != bytes) { fprintf(stderr, "[oracle] short write to %s (%zu/%zu)\n", path, w, bytes); return 0; }
    fprintf(stderr, "[oracle] wrote %s (%zu bytes)\n", name, bytes);
    return 1;
}

int main(int argc, char ** argv)
{
    /* 0) Force the scalar parity path BEFORE the first recon call. The env is
     *    also expected to be set by the runner; set it here too for safety. */
#if defined(_WIN32)
    _putenv("MLVAPP_DISABLE_AVX2=1");
#else
    setenv("MLVAPP_DISABLE_AVX2", "1", 1);
#endif

    /* Argument forms (backward compatible):
     *   oracle.exe                               -> base case, outdir "."
     *   oracle.exe <W> <H> [outdir]              -> legacy: explicit geometry,
     *                                               base-case iso/variant
     *   oracle.exe --case <name> [outdir]        -> named case from ORACLE_CASES
     *   oracle.exe --list                        -> print case table and exit
     *
     * The named-case form is the one the parity-breadth runner uses; it carries
     * W/H + iso pair + pattern variant so each case is self-describing. */
    int W = 1808, H = 2268;
    int sel_iso1 = 100, sel_iso2 = 800, sel_variant = 0;
    int sel_interp = 1;                       /* 1 = mean23 (legacy default) */
    const char * case_name = "base";
    const char * outdir = ".";

    /* Non-base diagnostic overrides. All three default to the base values, so
     * with the env vars UNSET this produces byte-identical output to before.
     * Parsed up front so the startup banner reflects the active axes.          */
    int isbright_overridden = oracle_parse_is_bright_env();
    oracle_parse_scalar_env();
    oracle_parse_load_env();   /* ORACLE_LOAD_INPUT + geometry/levels/iso overrides */
    if (g_oracle_auto_correction_set) {
        fprintf(stderr, "[oracle] NON-BASE axis: auto_correction=%d (%s)\n",
                g_oracle_auto_correction,
                g_oracle_auto_correction == -2 ? "match_by_histogram" : "ISO-ratio");
    }
    if (isbright_overridden || g_oracle_black_delta != 0 || g_oracle_apply_dither) {
        fprintf(stderr, "[oracle] NON-BASE axes: is_bright={%d,%d,%d,%d}%s "
                        "black_delta=%d(14b) apply_dither=%d\n",
                ORACLE_IS_BRIGHT[0], ORACLE_IS_BRIGHT[1],
                ORACLE_IS_BRIGHT[2], ORACLE_IS_BRIGHT[3],
                isbright_overridden ? " (overridden)" : "",
                g_oracle_black_delta, g_oracle_apply_dither);
    }

    if (argc >= 2 && strcmp(argv[1], "--list") == 0) {
        printf("oracle cases:\n");
        for (int i = 0; i < ORACLE_NUM_CASES; i++) {
            const oracle_case_t * c = &ORACLE_CASES[i];
            printf("  %-14s %5dx%-5d  iso %d/%-5d  variant=%d  interp=%s\n",
                   c->name, c->W, c->H, c->iso1, c->iso2, c->variant,
                   c->interp_method == 0 ? "AMaZE" : "mean23");
        }
        return 0;
    }

    if (argc >= 3 && strcmp(argv[1], "--case") == 0) {
        const oracle_case_t * found = NULL;
        for (int i = 0; i < ORACLE_NUM_CASES; i++) {
            if (strcmp(argv[2], ORACLE_CASES[i].name) == 0) { found = &ORACLE_CASES[i]; break; }
        }
        if (!found) {
            fprintf(stderr, "[oracle] unknown case '%s' (use --list)\n", argv[2]);
            return 2;
        }
        W = found->W; H = found->H;
        sel_iso1 = found->iso1; sel_iso2 = found->iso2; sel_variant = found->variant;
        sel_interp = found->interp_method;
        case_name = found->name;
        if (argc >= 4) outdir = argv[3];
#ifndef ORACLE_REAL_DEMOSAIC
        if (sel_interp == 0) {
            fprintf(stderr, "[oracle] case '%s' needs interp_method=0 (AMaZE) but this "
                            "oracle was built WITHOUT -DORACLE_REAL_DEMOSAIC.\n", case_name);
            return 2;
        }
#endif
    } else {
        /* legacy positional form: <W> <H> [outdir] */
        if (argc >= 3) { W = atoi(argv[1]); H = atoi(argv[2]); }
        if (argc >= 4) outdir = argv[3];
    }
    /* LOAD-EXTERNAL-INPUT geometry/level/iso overrides (env, default OFF).
     * Applied AFTER case/positional resolution so they win, but ONLY when set.
     * When ORACLE_LOAD_INPUT is unset every override below is a no-op (the
     * *_ovr defaults are <0) so synthetic-case behavior is byte-identical. */
    {
        const char * we = getenv("ORACLE_WIDTH");
        const char * he = getenv("ORACLE_HEIGHT");
        if (we && *we) W = atoi(we);
        if (he && *he) H = atoi(he);
        if (g_oracle_iso1_ovr > 0) sel_iso1 = g_oracle_iso1_ovr;
        if (g_oracle_iso2_ovr > 0) sel_iso2 = g_oracle_iso2_ovr;
        if (g_oracle_load_input) case_name = "live";  /* label for scalars/README */
    }
    if (W <= 16 || H <= 16) { fprintf(stderr, "[oracle] bad geometry %dx%d\n", W, H); return 2; }

    /* the resolved case used by the synthetic generator + scalars sidecar */
    oracle_case_t the_case;
    the_case.name = case_name;
    the_case.W = W; the_case.H = H;
    the_case.iso1 = sel_iso1; the_case.iso2 = sel_iso2;
    the_case.variant = sel_variant;
    the_case.interp_method = sel_interp;
    fprintf(stderr, "[oracle] case=%s  geometry=%dx%d  iso=%d/%d  variant=%d  interp=%s\n",
            case_name, W, H, sel_iso1, sel_iso2, sel_variant,
            sel_interp == 0 ? "AMaZE" : "mean23");

    /* create output dir if missing (ignore "already exists") */
    if (ORACLE_MKDIR(outdir) != 0) {
        struct stat st;
        if (stat(outdir, &st) != 0) {
            fprintf(stderr, "[oracle] cannot create outdir %s\n", outdir);
            return 4;
        }
    }

    fprintf(stderr, "[oracle] geometry %dx%d  outdir=%s\n", W, H, outdir);

    size_t n = (size_t)W * (size_t)H;

    /* 1) obtain the 14-bit Bayer input.
     *    DEFAULT (ORACLE_LOAD_INPUT unset): synthesize the deterministic frame
     *    exactly as before. LOAD mode: read the live capture's in.u16 verbatim
     *    instead of synthesizing -- the oracle then reproduces the REAL Dual ISO
     *    state. The loaded buffer is used as image_data; in_copy preserves it for
     *    the in.u16 dump (the recon overwrites img in place). */
    uint16_t * img = (uint16_t *)malloc(n * sizeof(uint16_t));
    if (!img) { fprintf(stderr, "[oracle] OOM input\n"); return 4; }
    if (g_oracle_load_input) {
        if (!oracle_load_u16(g_oracle_load_input, img, n)) { free(img); return 3; }
    } else {
        for (int y = 0; y < H; y++)
            for (int x = 0; x < W; x++)
                img[(size_t)x + (size_t)y * W] = oracle_pixel(x, y, &the_case);
    }

    /* keep a pristine copy of the input for the in.u16 dump (recon overwrites
     * img in place with the 16-bit output). */
    uint16_t * in_copy = (uint16_t *)malloc(n * sizeof(uint16_t));
    if (!in_copy) { fprintf(stderr, "[oracle] OOM input copy\n"); return 4; }
    memcpy(in_copy, img, n * sizeof(uint16_t));

    /* 2) raw_info (14-bit input; the recon promotes to 20-bit internally).
     *    active_area = {0,0,W,H} exactly mirrors the production pre-call setup
     *    (llrawproc.c:1188-1194). With this active_area compute_black_noise
     *    samples an empty strip -> num==0 -> dark_noise falls back to the
     *    deterministic default 8 (14-bit) -> *64 = 512 (20-bit). This matches
     *    the app's real behavior on this geometry. NB raw.h active_area layout
     *    is {y1,x1,y2,x2}. */
    struct raw_info ri;
    memset(&ri, 0, sizeof ri);
    ri.width = W; ri.height = H; ri.pitch = W;
    ri.bits_per_pixel = 14;
    /* 14-bit levels. ORACLE_BLACK_LEVEL_14 / ORACLE_WHITE_LEVEL_14 override for
     * LOAD mode (default <0 -> the synthetic constants, byte-identical when
     * unset). diso_get_full20bit multiplies these by 64 internally (dualiso.c:
     * 5249-5250), so pass the 14-bit values (live M16: black14=2047, white14=
     * 15812 -> 131008 / 1011968 in 20-bit). */
    ri.black_level = (g_oracle_black14_ovr > 0) ? g_oracle_black14_ovr : ORACLE_BLACK_14;
    ri.white_level = (g_oracle_white14_ovr > 0) ? g_oracle_white14_ovr : ORACLE_WHITE_14;
    ri.cfa_pattern = 0x02010100;            /* RGGB -> no 1-line skip */
    ri.active_area.x1 = 0; ri.active_area.y1 = 0;
    ri.active_area.x2 = W; ri.active_area.y2 = H;

    /* 3) scratch + diso params.
     *    auto_correction = -1  (ISO-ratio path inside match_exposures).
     *    ev_correction   =  1  (the sentinel that tells match_exposures to KEEP
     *                           the ISO-ratio result instead of overwriting it
     *                           with -*ev_correction; see dualiso.c:2498 and the
     *                           caller's has_explicit_auto_match==false branch
     *                           in llrawproc.c:1221-1234). With iso 100/800 this
     *                           yields _ev_correction = log2(8) = 3.0 EV >= 0.5,
     *                           so match_exposures returns 1 (matched, no abort)
     *                           and darkens the bright rows in place.
     *    black_delta = 0. */
    dualiso_full20bit_scratch_t sc;
    memset(&sc, 0, sizeof sc);

    /* iso_pattern: 0 = auto-detect (resolves to {1,1,0,0} on synthetic frames).
     * When ORACLE_IS_BRIGHT was overridden we FORCE the matching pattern value
     * so diso_get_full20bit uses iso_patterns[value-1] verbatim (dualiso.c:5215-
     * 5218) instead of auto-detecting -- essential in LOAD mode where the real
     * frame's field parity must be honored exactly (live M16 is_bright={0,1,1,0}
     * == iso_patterns[3] -> forced value 4). Unset/base -> stays 0 (auto). */
    int    diso_pattern         = 0;     /* 0 = auto-detect (resolves to {1,1,0,0}) */
    if (isbright_overridden) {
        int fp = oracle_forced_iso_pattern();
        if (fp) {
            diso_pattern = fp;
            fprintf(stderr, "[oracle] forcing iso_pattern=%d for is_bright={%d,%d,%d,%d} "
                            "(iso_patterns[%d])\n", fp,
                    ORACLE_IS_BRIGHT[0], ORACLE_IS_BRIGHT[1],
                    ORACLE_IS_BRIGHT[2], ORACLE_IS_BRIGHT[3], fp - 1);
        } else {
            fprintf(stderr, "[oracle] WARNING: is_bright={%d,%d,%d,%d} matches no "
                            "iso_patterns[] entry; leaving auto-detect (pattern=0)\n",
                    ORACLE_IS_BRIGHT[0], ORACLE_IS_BRIGHT[1],
                    ORACLE_IS_BRIGHT[2], ORACLE_IS_BRIGHT[3]);
        }
    }
    int    diso_auto_correction = g_oracle_auto_correction;  /* -1 ISO-ratio (default), -2 histogram */
    double diso_ev_correction   = 1.0;   /* sentinel: use ISO-ratio (see above)      */
    /* black_delta param semantics (match_exposures, dualiso.c:2529-2538):
     *   diso_black_delta != -1 -> _black_delta OVERWRITTEN with diso_black_delta*64
     *   diso_black_delta == -1 -> KEEP the ISO-ratio-derived _black_delta
     *                             (= (high/100)*64 - (low/100)*64; dualiso.c:2516)
     * SYNTHETIC default stays g_oracle_black_delta (0 base / ORACLE_BLACK_DELTA),
     * so existing base/iso1600/clipped vectors are byte-identical.
     * LOAD mode defaults to -1 (sentinel) so the ISO-ratio black_delta survives:
     * this is what reproduces the live M16 state (iso 100/1600 -> black_delta20
     * = 16*64 - 1*64 = 960, matching the live dump). ORACLE_BLACK_DELTA still
     * overrides if the caller explicitly wants a fixed value. */
    int    diso_black_delta     = g_oracle_black_delta;
    if (g_oracle_load_input && getenv("ORACLE_BLACK_DELTA") == NULL) {
        diso_black_delta = -1;   /* keep ISO-ratio black_delta (live reproduction) */
        fprintf(stderr, "[oracle] LOAD mode: diso_black_delta=-1 (keep ISO-ratio "
                        "black_delta from match_exposures)\n");
    }
    /* AUTO=-2 (match_by_histogram) diagnostic mode. Mirror the GUI DISO_FORCED
     * runtime exactly (MainWindow.cpp:11015-11021): auto_correction=-2 selects
     * the histogram branch in match_exposures (dualiso.c:2519-2522), and the
     * ev=1 / bd=-1 SENTINELS are what stop the override at dualiso.c:2524/:2529
     * from clobbering the histogram-derived _ev_correction/_black_delta. Without
     * those sentinels match_exposures would discard the histogram result and
     * re-apply the iso-ratio (the desync this diagnostic is meant to reproduce).
     * Forcing bd=-1 here also overrides any non-LOAD g_oracle_black_delta so the
     * histogram value is the one APPLIED. */
    if (g_oracle_auto_correction_set && g_oracle_auto_correction == -2) {
        diso_auto_correction = -2;
        diso_ev_correction   = 1.0;   /* sentinel: keep histogram ev_correction  */
        diso_black_delta     = -1;    /* sentinel: keep histogram black_delta     */
        fprintf(stderr, "[oracle] AUTO=-2 mode: match_by_histogram "
                        "(auto_correction=-2, ev_correction=1 sentinel, "
                        "black_delta=-1 sentinel) -- mirrors GUI DISO_FORCED\n");
    }
    int    interp_method        = sel_interp; /* 0=AMaZE (production default), 1=mean23 */
    int    use_alias_map        = g_oracle_use_alias_map;
    int    use_fullres          = 1;     /* fullres reconstruction on                */
    int    chroma_smooth_method = g_oracle_chroma_smooth_method;
    int    threads              = 1;     /* single-thread for determinism            */
    int    dark_frame           = 0;

    /* DITHER: initialize the randn05 LUT BEFORE the recon so the SCALAR
     * convert_20_to_16bit path (raw_set_pixel_20to16_rand -> fast_randn05())
     * applies it. When apply_dither is OFF (default) we leave randn05_cache
     * untouched: the engine never calls fast_randn_init(), so the cache stays
     * all-zero and fast_randn05() returns 0.0 -- byte-identical to base.
     *
     * The table we build is captured for the randn05.f32 dump below; we build it
     * here (not after the run) because fast_randn_init() writes the same global
     * randn05_cache the recon reads, so the dumped table is exactly what the
     * recon used. */
    float oracle_randn05[1024];
    memset(oracle_randn05, 0, sizeof oracle_randn05);
    if (g_oracle_apply_dither) {
        /* fast_randn05() advances a PROCESS-WIDE static counter once per pixel.
         * convert_20_to_16bit's scalar loop is `#pragma omp parallel for
         * collapse(2)` and is NOT gated by the `threads` API param, so under
         * default OMP it would draw table entries in a schedule-dependent order
         * -> non-deterministic out.u16. Force OMP to 1 thread so the scalar
         * dither order is exactly index (x + y*W) & 1023, making the dithered
         * out.u16 reproducible. (Base/dither-off runs leave OMP at its default;
         * the recon is bit-exact regardless of thread count when dither is off,
         * since no per-pixel global counter is consumed.) */
#if defined(_OPENMP)
        omp_set_num_threads(1);
        fprintf(stderr, "[oracle] dither ON: forcing omp_set_num_threads(1) "
                        "for deterministic scalar fast_randn05() order\n");
#endif
        oracle_build_randn05(oracle_randn05);   /* seeds + fills randn05_cache */
        fprintf(stderr, "[oracle] dither ON: randn05 LUT initialized "
                        "(randn05_cache[0]=%.9g [1]=%.9g [1023]=%.9g)\n",
                randn05_cache[0], randn05_cache[1], randn05_cache[1023]);
    }

    fprintf(stderr, "[oracle] running diso_get_full20bit (%s scalar)...\n",
            interp_method == 0 ? "AMaZE" : "mean23");
    int ok = diso_get_full20bit(ri, img, dark_frame, sel_iso1, sel_iso2,
                                &diso_pattern, &diso_auto_correction,
                                &diso_ev_correction, &diso_black_delta,
                                interp_method, use_alias_map, use_fullres,
                                chroma_smooth_method, 0, threads, &sc);
    if (!ok) {
        fprintf(stderr, "[oracle] recon FAILED (pattern/overlap). pattern=%d ev_corr=%f\n",
                diso_pattern, diso_ev_correction);
        return 4;
    }

    /* out-params captured AFTER the run:
     *   diso_ev_correction : match_exposures sets *ev_correction = -_ev_correction
     *                        (negative); corr_ev = ABS(it) is what the recon used.
     *   diso_black_delta   : *black_delta = _black_delta/64 (14-bit-ish units).
     *   diso_pattern       : resolved ISO pattern (negative encoding -(i+1)).
     * white_darkened is computed internally and not returned through the API;
     * we recompute it from the documented formula for the sidecar (see below). */
    double corr_ev = fabs(diso_ev_correction);

    /* Recompute white_darkened exactly as match_exposures does (dualiso.c:2548).
     * Critical subtleties that an earlier version of this sidecar got WRONG:
     *   - match_exposures uses white = MIN(raw_info.white_level, *white_darkened
     *     seed) where the seed is white_bright = white/2 (driver:4924). With the
     *     driver's level math (raw_info.white_level = white = 960000,
     *     white_bright = 480000) that MIN is 480000, NOT 960000.
     *   - The APPLY _black_delta is 0, NOT the ISO-ratio 448: the driver passes
     *     diso_black_delta = 0, and since 0 != -1, match_exposures overwrites
     *     _black_delta with 0*64 = 0 (dualiso.c:2503-2506) before darkening.
     *   - The cast truncates the WHOLE rhs: (int)((white-black+bd)*factor + black),
     *     i.e. +black is in double then truncated (dualiso.c:2548 assigns to an
     *     int *white_darkened target via int arithmetic on a double expression).
     * Driver level math: black20 = 2047*64, white20 = 15000*64,
     * white_bright20 = (15000/2)*64. Result here: 174632 (matches stage_bright). */
    /* Effective 14-bit levels (overridden in LOAD mode, else synthetic consts). */
    int eff_black14 = (g_oracle_black14_ovr > 0) ? g_oracle_black14_ovr : ORACLE_BLACK_14;
    int eff_white14 = (g_oracle_white14_ovr > 0) ? g_oracle_white14_ovr : ORACLE_WHITE_14;
    int black20 = eff_black14 * 64;
    int white20 = eff_white14 * 64;
    int white_bright20 = (eff_white14 / 2) * 64;
    int wd_white = (white20 < white_bright20) ? white20 : white_bright20; /* MIN */
    /* black_delta APPLY value (20-bit). The recon reads back the APPLIED value/64
     * into diso_black_delta (dualiso.c:2538), so the faithful APPLY 20-bit value
     * is the readback * 64 regardless of how it was derived:
     *   SYNTHETIC: g_oracle_black_delta (passed != -1) -> readback == that value
     *   LOAD/live: diso_black_delta passed -1 -> match_exposures keeps the ISO-
     *              ratio value, reads it back (e.g. 15 for iso 100/1600 -> *64=960)
     * Using the readback keeps base vectors byte-identical (readback == passed). */
    int _black_delta_20 = diso_black_delta * 64;
    if (_black_delta_20 < 0)        _black_delta_20 = 0;
    if (_black_delta_20 > 100 * 64) _black_delta_20 = 100 * 64;
    double wd_factor = pow(2.0, -corr_ev);
    int white_darkened = (int)((double)(wd_white - black20 + _black_delta_20) * wd_factor + (double)black20);

    /* black_delta seen back from the recon */
    int black_delta_out = diso_black_delta;

    fprintf(stderr, "[oracle] recon OK. corr_ev=%f black_delta=%d pattern=%d white_darkened=%d\n",
            corr_ev, black_delta_out, diso_pattern, white_darkened);

    /* ---- gather the LUTs the recon actually used (from scratch) --------- */
    /* raw2ev: scratch->ev_raw2ev [1<<20] int */
    /* ev2raw: scratch->ev2raw_0  [24*EV_RESOLUTION] int (base array; the recon
     *         uses ev2raw_0 + 10*EV_RESOLUTION as the usable origin). We dump
     *         the FULL base array to match igpu_recon_luts_t.ev2raw sizing. */
    if (!sc.ev_raw2ev || !sc.ev2raw_0) {
        fprintf(stderr, "[oracle] LUTs missing from scratch (ev_raw2ev=%p ev2raw_0=%p)\n",
                (void*)sc.ev_raw2ev, (void*)sc.ev2raw_0);
        return 4;
    }

    /* mix_curve: find the valid slot the recon built (fresh scratch -> slot 0). */
    double * mix_curve = NULL;
    for (int slot = 0; slot < DUALISO_MIX_CURVE_CACHE_SLOTS; slot++) {
        if (sc.mix_curve_valid[slot] && sc.mix_curve[slot]) { mix_curve = sc.mix_curve[slot]; break; }
    }
    if (!mix_curve) {
        fprintf(stderr, "[oracle] no valid mix_curve slot found\n");
        return 4;
    }

    /* fullres_curve: rebuild with the exact report formula (float curve =
     * production default in final_blend), cast to double for the dump. black is
     * the 20-bit black (black20). */
    size_t lut20 = (size_t)1 << 20;
    double * fullres_curve = (double *)malloc(lut20 * sizeof(double));
    if (!fullres_curve) { fprintf(stderr, "[oracle] OOM fullres_curve\n"); return 4; }
    {
        const double fullres_start = 4.0;
        const double fullres_transition = 4.0;
        for (size_t i = 0; i < lut20; i++) {
            double ev2 = log2(MAX((double)i / 64.0 - (double)black20 / 64.0, 1.0));
            double c2 = -cos(COERCE(ev2 - fullres_start, 0.0, fullres_transition) * M_PI / fullres_transition);
            double f = (c2 + 1.0) / 2.0;
            fullres_curve[i] = (double)(float)f;   /* match build_fullres_curve_float */
        }
    }

    /* fullres_curve_double: the DOUBLE curve (build_fullres_curve, dualiso.c:2698)
     * used ONLY by build_alias_map's fullres-skip predicate
     * (fullres_curve[bright[..]] > fullres_thr). This differs from the float
     * curve cast above by the float rounding, which can flip the > 0.8 predicate
     * at a handful of pixels - so the CUDA alias map needs this exact double
     * table for its skip test. final_blend still uses the float curve. */
    double * fullres_curve_double = (double *)malloc(lut20 * sizeof(double));
    if (!fullres_curve_double) { fprintf(stderr, "[oracle] OOM fullres_curve_double\n"); return 4; }
    {
        const double fullres_start = 4.0;
        const double fullres_transition = 4.0;
        for (size_t i = 0; i < lut20; i++) {
            double ev2 = log2(MAX((double)i / 64.0 - (double)black20 / 64.0, 1.0));
            double c2 = -cos(COERCE(ev2 - fullres_start, 0.0, fullres_transition) * M_PI / fullres_transition);
            double f = (c2 + 1.0) / 2.0;
            fullres_curve_double[i] = f;           /* match build_fullres_curve (double) */
        }
    }

    /* ---- write all dumps ---------------------------------------------- */
    int all_ok = 1;
    all_ok &= dump_blob(outdir, "in.u16",  in_copy, n * sizeof(uint16_t));
    all_ok &= dump_blob(outdir, "out.u16", img,     n * sizeof(uint16_t));
    all_ok &= dump_blob(outdir, "raw2ev.i32", sc.ev_raw2ev, lut20 * sizeof(int));
    all_ok &= dump_blob(outdir, "ev2raw.i32", sc.ev2raw_0, (size_t)DUALISO_EV2RAW_LUT_COUNT * sizeof(int));
    all_ok &= dump_blob(outdir, "mix_curve.f64", mix_curve, lut20 * sizeof(double));
    all_ok &= dump_blob(outdir, "fullres_curve.f64", fullres_curve, lut20 * sizeof(double));
    all_ok &= dump_blob(outdir, "fullres_curve_double.f64", fullres_curve_double, lut20 * sizeof(double));

    /* randn05 anti-posterization LUT: dumped ONLY when dither was applied, so
     * base/iso1600/clipped vectors are unchanged (no new file appears). The 1024
     * float32 LE entries are the EXACT table fast_randn05() drew from this run.
     * A consumer that wants to reproduce the dithered out.u16 must load this and
     * mirror the engine's draw order (see README.txt dither caveat).           */
    if (g_oracle_apply_dither) {
        all_ok &= dump_blob(outdir, "randn05.f32", oracle_randn05, 1024 * sizeof(float));
    }

    /* ---- PER-STAGE INTERMEDIATE DUMPS (for CUDA stage-by-stage parity) ----
     * Read back the recon's scratch planes AFTER diso_get_full20bit returned.
     * The buffers persist (the recon reuses them across calls and never frees
     * until free_dualiso_full20bit_scratch). Each plane is w*h.
     *
     * IMPORTANT smooth-plane aliasing: with chroma_smooth=0 the driver sets
     *   fullres_smooth = fullres ; halfres_smooth = halfres
     * (dualiso.c:4872/4876) and only points them at scratch->{fullres,halfres}
     * _smooth when chroma_smooth_method != 0. With chroma_smooth_method=0 the
     * scratch->fullres_smooth / scratch->halfres_smooth planes are NEVER
     * written, so we dump scratch->fullres for "fullres_smooth" and
     * scratch->halfres for "halfres_smooth" - exactly the buffers the kernel
     * chain consumes. The CUDA side must mirror this aliasing.
     *
     * In the source-smoothed chroma path used for CUDA playback parity,
     * final_blend consumes scratch->fullres_smooth (the secondary full-res
     * reconstruction from smoothed source planes), but halfres_smooth remains
     * scratch->halfres (the mixed output from those smoothed source planes).
     *
     * NOTE raw_buffer_32 holds the FINAL 20-bit blend output here (final_blend
     * does raw_set_pixel32(x,y,ev2raw[output]) into raw_buffer_32), NOT the
     * post-convert_to_20bit promote. That early stage is reproduced on the CUDA
     * side directly from in.u16 and checked against the documented formula, so
     * we instead dump:
     *   - stage_match.u32   raw_buffer_32 right after match_exposures... but the
     *     recon has already overwritten it with the final blend. We cannot
     *     recover the post-promote/post-match buffer without instrumenting
     *     src/, which is out of scope. So stage_match/promote parity is checked
     *     on the CUDA side against the closed-form (v<<6)&0xFFFFF + match cast.
     * The plane dumps we CAN recover post-return are the persistent stage
     * outputs: dark, bright, fullres, halfres, alias_map, overexposed, and the
     * final raw_buffer_32 (== final_blend 20-bit output). */
    if (sc.dark)        all_ok &= dump_blob(outdir, "stage_dark.u32",           sc.dark,        n * sizeof(uint32_t));
    if (sc.bright)      all_ok &= dump_blob(outdir, "stage_bright.u32",         sc.bright,      n * sizeof(uint32_t));
    if (sc.fullres)     all_ok &= dump_blob(outdir, "stage_fullres.u32",        sc.fullres,     n * sizeof(uint32_t));
    if (sc.halfres)     all_ok &= dump_blob(outdir, "stage_halfres.u32",        sc.halfres,     n * sizeof(uint32_t));
    const uint32_t * stage_fullres_smooth =
        (chroma_smooth_method && sc.fullres_smooth) ? sc.fullres_smooth : sc.fullres;
    const uint32_t * stage_halfres_smooth = sc.halfres;
    if (stage_fullres_smooth) all_ok &= dump_blob(outdir, "stage_fullres_smooth.u32", stage_fullres_smooth, n * sizeof(uint32_t));
    if (stage_halfres_smooth) all_ok &= dump_blob(outdir, "stage_halfres_smooth.u32", stage_halfres_smooth, n * sizeof(uint32_t));
    if (sc.alias_map)   all_ok &= dump_blob(outdir, "stage_alias_map.u16",      sc.alias_map,   n * sizeof(uint16_t));
    if (sc.overexposed) all_ok &= dump_blob(outdir, "stage_overexposed.u16",    sc.overexposed, n * sizeof(uint16_t));
    if (sc.raw_buffer_32) all_ok &= dump_blob(outdir, "stage_final20.u32",      sc.raw_buffer_32, n * sizeof(uint32_t));

    /* ---- AMaZE-SPECIFIC INTERMEDIATE DUMPS (interp_method==0 only) --------
     * The edge-directed reconstruction the CUDA port reproduces is the chain
     *   demosaiced red/green/blue planes (post green-undo + clamp)
     *     -> gray = g/2 + r/4 + b/4 (float, truncated to u32)
     *     -> edge_direction[x+y*w]  (integer EV-LUT edge search + diag penalty)
     *     -> edge_interp -> dark/bright[x+y*w] (integer EV-LUT)
     * We dump the planes the CUDA edge stages consume (red/green/blue float),
     * plus gray + edge_direction + squeezed so each dualiso-owned AMaZE stage
     * can be diffed independently against the CUDA kernels. dark/bright are
     * already dumped above (stage_dark/stage_bright). The red/green/blue/rawData
     * planes are stored as h rows of stride wx (=w+16); we dump the full flat
     * h*wx storage so the CUDA side can index [squeezed[y]*wx + x] exactly.
     * raw_buffer_32 (== stage_final20) is the final blend, NOT the post-interp
     * raw promote, so AMaZE dark/bright after edge_interp are the parity-critical
     * interpolation-stage outputs the task calls out (stage_dark / stage_bright).*/
    if (interp_method == 0) {
        size_t wx = (size_t)W + 16;
        size_t plane = (size_t)H * wx;        /* row_count * row_width */
        if (sc.amaze_squeezed)
            all_ok &= dump_blob(outdir, "amaze_squeezed.i32",       sc.amaze_squeezed,       (size_t)H * sizeof(int));
        if (sc.amaze_rawData_storage)
            all_ok &= dump_blob(outdir, "amaze_rawData.f32",        sc.amaze_rawData_storage, plane * sizeof(float));
        if (sc.amaze_red_storage)
            all_ok &= dump_blob(outdir, "amaze_red.f32",            sc.amaze_red_storage,    plane * sizeof(float));
        if (sc.amaze_green_storage)
            all_ok &= dump_blob(outdir, "amaze_green.f32",          sc.amaze_green_storage,  plane * sizeof(float));
        if (sc.amaze_blue_storage)
            all_ok &= dump_blob(outdir, "amaze_blue.f32",           sc.amaze_blue_storage,   plane * sizeof(float));
        if (sc.amaze_gray)
            all_ok &= dump_blob(outdir, "amaze_gray.u32",           sc.amaze_gray,           n * sizeof(uint32_t));
        if (sc.amaze_edge_direction)
            all_ok &= dump_blob(outdir, "amaze_edge_direction.u8",  sc.amaze_edge_direction, n * sizeof(uint8_t));
        fprintf(stderr, "[oracle] AMaZE plane dumps written (wx=%zu, plane=%zu cells)\n", wx, plane);
    }

    /* ---- scalars sidecar ---------------------------------------------- */
    {
        char path[1024];
        snprintf(path, sizeof(path), "%s/scalars.txt", outdir);
        FILE * f = fopen(path, "w");
        if (f) {
            fprintf(f, "# Dual ISO full-20-bit recon oracle - captured scalars\n");
            fprintf(f, "case=%s\n", case_name);
            fprintf(f, "pattern_variant=%d\n", sel_variant);
            fprintf(f, "width=%d\n", W);
            fprintf(f, "height=%d\n", H);
            fprintf(f, "black_level_14=%d\n", eff_black14);
            fprintf(f, "white_level_14=%d\n", eff_white14);
            fprintf(f, "iso1=%d\n", sel_iso1);
            fprintf(f, "iso2=%d\n", sel_iso2);
            fprintf(f, "cfa_pattern=0x02010100\n");
            fprintf(f, "interp_method=%d\n", interp_method);   /* 0=AMaZE, 1=mean23 */
            fprintf(f, "amaze_row_width=%d\n", W + 16);        /* wx = w+16 (AMaZE plane stride) */
            fprintf(f, "use_alias_map=%d\n", use_alias_map);
            fprintf(f, "use_fullres=%d\n", use_fullres);
            fprintf(f, "chroma_smooth_method=%d\n", chroma_smooth_method);
            fprintf(f, "threads=%d\n", threads);
            fprintf(f, "is_bright=%d,%d,%d,%d\n",
                    ORACLE_IS_BRIGHT[0], ORACLE_IS_BRIGHT[1], ORACLE_IS_BRIGHT[2], ORACLE_IS_BRIGHT[3]);
            fprintf(f, "# --- captured out-params (post-recon) ---\n");
            /* auto_correction line emitted ONLY in the diagnostic mode so base/
             * iso1600/clipped/live(ISO-ratio) scalars.txt stay byte-identical. */
            if (g_oracle_auto_correction_set) {
                fprintf(f, "auto_correction=%d        # %s (match_exposures branch)\n",
                        diso_auto_correction,
                        diso_auto_correction == -2 ? "match_by_histogram" : "ISO-ratio");
            }
            fprintf(f, "ev_correction=%.17g\n", diso_ev_correction);   /* negative as stored */
            fprintf(f, "corr_ev=%.17g\n", corr_ev);                    /* ABS(ev_correction) */
            fprintf(f, "black_delta=%d\n", black_delta_out);
            fprintf(f, "iso_pattern=%d\n", diso_pattern);
            fprintf(f, "# --- 20-bit derived levels (probe reads these) ---\n");
            fprintf(f, "black20=%d\n", black20);
            fprintf(f, "white20=%d\n", white20);
            fprintf(f, "white_bright20=%d\n", white_bright20);
            fprintf(f, "white_darkened=%d\n", white_darkened);
            fprintf(f, "factor=%.17g            # pow(2,-corr_ev), the match-exposures darken factor\n", wd_factor);
            /* Preserve the EXACT base-case line (byte-identity) when black_delta
             * is 0; emit the non-base APPLY value with a clarifying comment when
             * overridden. The 20-bit APPLY value is the 14-bit ORACLE_BLACK_DELTA
             * times 64, clamped to [0,6400]. */
            if (_black_delta_20 == 0) {
                fprintf(f, "black_delta20=%d        # APPLY value (diso_black_delta=0 != -1 -> 0)\n", _black_delta_20);
            } else if (g_oracle_load_input && getenv("ORACLE_BLACK_DELTA") == NULL) {
                /* LOAD mode kept the ISO-ratio black_delta (sentinel -1 passed);
                 * the APPLY value is the readback*64 (e.g. iso 100/1600 -> 960). */
                fprintf(f, "black_delta20=%d        # APPLY value (LOAD: sentinel -1 -> ISO-ratio readback %d *64)\n",
                        _black_delta_20, black_delta_out);
            } else {
                fprintf(f, "black_delta20=%d        # APPLY value (ORACLE_BLACK_DELTA=%d 14b -> *64 clamped[0,6400])\n",
                        _black_delta_20, g_oracle_black_delta);
            }
            fprintf(f, "dark_noise20=512        # default-8 (14-bit) fallback *64 for active_area={0,0,W,H}\n");
            fprintf(f, "dark_noise_20bit=512   # legacy alias of dark_noise20\n");
            /* apply_dither line appended ONLY when dither is on, so base/iso1600/
             * clipped scalars.txt stay byte-identical (the line never appears). */
            if (g_oracle_apply_dither) {
                fprintf(f, "apply_dither=1          # randn05.f32 dumped; scalar fast_randn05() applied in convert_20_to_16bit\n");
            }
            fclose(f);
            fprintf(stderr, "[oracle] wrote scalars.txt\n");
        } else {
            fprintf(stderr, "[oracle] cannot write scalars.txt\n");
            all_ok = 0;
        }
    }

    /* ---- README sidecar ----------------------------------------------- */
    {
        char path[1024];
        snprintf(path, sizeof(path), "%s/README.txt", outdir);
        FILE * f = fopen(path, "w");
        if (f) {
            fprintf(f,
"MLV-App Dual ISO full-20-bit recon CPU ORACLE - test vectors\n"
"============================================================\n"
"\n"
"Generated by tools/gpu/oracle/oracle_main.c (mean23 scalar reference path,\n"
"MLVAPP_DISABLE_AVX2=1). Mirrors diso_get_full20bit (src/mlv/llrawproc/\n"
"dualiso.c:4735) with interp_method=1, use_alias_map=1, use_fullres=1,\n"
"chroma_smooth_method=0, iso1=100, iso2=800, threads=1.\n"
"\n"
"All binary files are raw little-endian, no header. Host is x86-64 LE.\n"
"\n"
"FILES\n"
"-----\n"
"  in.u16             uint16 LE, %d elements (W*H). Synthetic 14-bit Bayer\n"
"                     input (values in [black,white]). See FORMULA below.\n"
"  out.u16            uint16 LE, %d elements (W*H). 16-bit Bayer recon output\n"
"                     (the bit-exact scalar reference; CUDA must match this).\n"
"  raw2ev.i32         int32  LE, %u elements (1<<20). raw(20-bit) -> EV*65536.\n"
"                     The EXACT LUT the recon used (read back from scratch).\n"
"  ev2raw.i32         int32  LE, %u elements (24*65536). EV -> raw(20-bit),\n"
"                     BASE array. Usable origin is element 10*65536 (=655360);\n"
"                     valid EV index range [-10*65536, 14*65536). Read back\n"
"                     from scratch. Matches igpu_recon_luts_t.ev2raw sizing.\n"
"  mix_curve.f64      double LE, %u elements (1<<20). Halfres mix blend factor\n"
"                     k(raw20). The EXACT curve the recon built (read back from\n"
"                     scratch slot). Keyed on (black,white,corr_ev,overlap).\n"
"  fullres_curve.f64  double LE, %u elements (1<<20). Fullres confidence curve.\n"
"                     Rebuilt here with dualiso.c's exact formula (FLOAT curve,\n"
"                     production default in final_blend, cast to double):\n"
"                       fullres_start=4, fullres_transition=4, black=black20\n"
"                       ev2 = log2(max(i/64 - black/64, 1))\n"
"                       c2  = -cos(coerce(ev2-4, 0, 4) * PI / 4)\n"
"                       f   = (double)(float)((c2+1)/2)\n"
"                     (stored static in dualiso.c, not reachable via scratch.)\n"
"  scalars.txt        key=value sidecar of geometry, params, captured out-\n"
"                     params and derived 20-bit levels.\n"
"  README.txt         this file.\n"
"\n"
"GEOMETRY\n"
"--------\n"
"  width  W = %d\n"
"  height H = %d\n"
"  Bayer phase: RGGB (cfa_pattern 0x02010100). Element (x,y) at index x + y*W.\n"
"\n"
"SYNTHETIC DUAL-ISO BAYER FORMULA (fixed, closed-form, byte-reproducible)\n"
"-----------------------------------------------------------------------\n"
"  Constants: BLACK=%d, WHITE=%d (14-bit), span = WHITE-BLACK = %d.\n"
"  Bright-field pattern per (y&3): is_bright = {1,1,0,0}\n"
"     (matches iso_patterns[0]; auto-detect resolves to this).\n"
"\n"
"  Scene irradiance S(x,y) in [0,1] (double precision):\n"
"     gx   = x / (W-1)\n"
"     gy   = y / (H-1)\n"
"     grad = 0.5 * (gx + gy)\n"
"     tex  = 0.18 * sin( x*(2*PI/64) + y*(2*PI/48) )\n"
"     S    = clamp(grad + tex, 0, 1)\n"
"\n"
"  Pixel value (14-bit), is_bright = is_bright[y&3]:\n"
"     if is_bright:  v = BLACK + S * span            (low ISO 100, ~3 EV more)\n"
"     else        :  v = BLACK + S * (span / 8)      (high ISO 800, 2^-3)\n"
"     pixel = clamp( floor(v + 0.5), BLACK, WHITE )    (uint16)\n"
"\n"
"  The 8x bright/dark ratio == 3 EV == log2(800/100), so match_exposures on\n"
"  the ISO-ratio path (auto_correction=-1, ev_correction sentinel=1) computes\n"
"  corr_ev = 3.0 (>= 0.5) and matches without aborting.\n"
"\n"
"  CUDA REPRODUCTION: use IEEE-754 double sin()/floor and the same rounding\n"
"  (floor(v+0.5)) to regenerate in.u16 byte-identically.\n"
"\n"
"CAPTURED SCALARS (also in scalars.txt)\n"
"--------------------------------------\n"
"  ev_correction  = %.17g   (as stored: -corr_ev)\n"
"  corr_ev        = %.17g   (= ABS(ev_correction))\n"
"  black_delta    = %d\n"
"  iso_pattern    = %d   (negative -(i+1) auto-discovered encoding)\n"
"  black20        = %d\n"
"  white20        = %d\n"
"  white_bright20 = %d\n"
"  white_darkened = %d   (recomputed from match_exposures formula)\n"
"  dark_noise(20) = 512  (default 8 14-bit fallback *64; active_area={0,0,W,H})\n"
"\n"
"PARITY NOTES\n"
"------------\n"
"  - Output dither is OFF: randn05_cache is never initialized in the codebase\n"
"    (fast_randn_init has no caller), so convert_20_to_16bit reduces to\n"
"    (int)(value/16.0 + 0.5) clamped [0,65535]. Deterministic.\n"
"  - This is the SCALAR path (MLVAPP_DISABLE_AVX2=1). The AVX2 path is ~1 LSB\n"
"    drift and is NOT the parity reference.\n"
"  - For GPU parity, upload raw2ev/ev2raw/mix_curve/fullres_curve as-is and\n"
"    compile with --fmad=false to avoid FMA contraction in the float blends.\n",
                (int)n, (int)n,
                (unsigned)lut20, (unsigned)DUALISO_EV2RAW_LUT_COUNT,
                (unsigned)lut20, (unsigned)lut20,
                W, H,
                ORACLE_BLACK_14, ORACLE_WHITE_14, (ORACLE_WHITE_14 - ORACLE_BLACK_14),
                diso_ev_correction, corr_ev, black_delta_out, diso_pattern,
                black20, white20, white_bright20, white_darkened);
            /* NON-BASE addendum: appended ONLY when a diagnostic axis is active,
             * so base/iso1600/clipped README.txt stay byte-identical. */
            if (g_oracle_apply_dither || g_oracle_black_delta != 0 || isbright_overridden) {
                fprintf(f,
"\n"
"NON-BASE DIAGNOSTIC AXES (this vector)\n"
"--------------------------------------\n"
"  is_bright      = {%d,%d,%d,%d}%s\n"
"  black_delta    = %d (14-bit) -> %d (20-bit APPLY, *64 clamped [0,6400])\n"
"  apply_dither   = %d\n",
                    ORACLE_IS_BRIGHT[0], ORACLE_IS_BRIGHT[1],
                    ORACLE_IS_BRIGHT[2], ORACLE_IS_BRIGHT[3],
                    isbright_overridden ? " (ORACLE_IS_BRIGHT override)" : "",
                    g_oracle_black_delta, _black_delta_20, g_oracle_apply_dither);
                if (g_oracle_apply_dither) {
                    fprintf(f,
"\n"
"  DITHER FIDELITY CAVEAT\n"
"  ----------------------\n"
"  randn05.f32 (1024 float32 LE) is the EXACT anti-posterization LUT this run\n"
"  used. It is built by the engine's own fast_randn_init() (dualiso.c:1912)\n"
"  after srand(0), so the table is reproducible. HOWEVER two distinct, NOT\n"
"  bit-identical dither application orders exist in the engine:\n"
"    1. SCALAR path (this oracle, MLVAPP_DISABLE_AVX2=1): convert_20_to_16bit\n"
"       calls fast_randn05(), which uses a PROCESS-WIDE static counter k that\n"
"       increments once per pixel in row-major collapse(2) order. So pixel\n"
"       (x,y) draws randn05_cache[(x + y*W) & 1023] under single-thread; with\n"
"       OMP threads the global counter order is schedule-dependent (this oracle\n"
"       runs threads=1, so it is deterministic = index (x+y*W)&1023).\n"
"    2. AVX2 fused path (the SHIPPING default when DUALISO_AVX2_AVAILABLE) and\n"
"       the CUDA backend (igpu_recon_cuda.cu:550) index randn05[(y*7+x)&1023].\n"
"  These two orderings select DIFFERENT table entries per pixel, so the SCALAR\n"
"  out.u16 here is NOT bit-identical to an AVX2/CUDA dithered output even with\n"
"  the same LUT. For stage-localization this is fine: the dither only enters at\n"
"  the FINAL convert_20_to_16bit step, so stage_final20.u32 (pre-dither 20-bit)\n"
"  is order-independent and remains the bit-exact handoff to compare. The\n"
"  out.u16 dither diff should be read as ~|N(0,0.5)| LSB noise, not a bug,\n"
"  UNLESS the consumer mirrors THIS oracle's (x+y*W)&1023 scalar order.\n");
                }
            }
            fclose(f);
            fprintf(stderr, "[oracle] wrote README.txt\n");
        } else {
            fprintf(stderr, "[oracle] cannot write README.txt\n");
            all_ok = 0;
        }
    }

    free(fullres_curve);
    free(fullres_curve_double);
    free_dualiso_full20bit_scratch(&sc);
    free(in_copy);
    free(img);

    if (!all_ok) { fprintf(stderr, "[oracle] one or more dumps failed\n"); return 4; }
    fprintf(stderr, "[oracle] DONE\n");
    return 0;
}
