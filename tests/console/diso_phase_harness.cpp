// DUALISO-PATTERN-FLICKER-CHECK-1: per-frame dual-ISO phase harness.
// Decodes N consecutive frames of a clip through the HQ dual-ISO CPU pipeline (the
// same pattern resolver the GPU prepare uses) and prints one CSV row per frame:
// the camera frame number from the VIDF header, the raw mod-4 green row-group means
// (the decode-level truth), the resolver's cached/resolved pattern and probe flags,
// and a row-banding metric of the processed output.
// Usage: diso_phase_harness <clip path> <receipt path> <start> <count> [out.csv]
//        diso_phase_harness --synthetic <bright_signal> <dark_signal> <steps>
#include "../common/test_runtime.h"

#include "../../src/mlv_include.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../src/mlv/llrawproc/dualiso.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/batch/ReceiptApplier.h"
#include "../../src/batch/ReceiptLoader.h"

#include <QApplication>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>

static const int kPatterns[4][4] = {{1, 1, 0, 0}, {1, 0, 0, 1}, {0, 0, 1, 1}, {0, 1, 1, 0}};

// Truth: green-site means per y%4 over the whole frame, black subtracted. Returns the
// iso_patterns index of the two brightest groups (or -1 when they are not adjacent).
static int raw_truth(const uint16_t * raw, int w, int h, int black, double g[4], double * margin)
{
    double sum[4] = {0, 0, 0, 0};
    double cnt[4] = {0, 0, 0, 0};
    for (int y = 0; y < h / 4 * 4; y++)
    {
        const uint16_t * row = raw + (size_t)y * (size_t)w;
        for (int x = (y % 2) ? 0 : 1; x < w; x += 2)
        {
            sum[y % 4] += row[x];
            cnt[y % 4] += 1;
        }
    }
    int order[4] = {0, 1, 2, 3};
    for (int i = 0; i < 4; i++) g[i] = sum[i] / cnt[i] - black;
    for (int i = 0; i < 4; i++)
        for (int j = i + 1; j < 4; j++)
            if (g[order[j]] > g[order[i]]) { int t = order[i]; order[i] = order[j]; order[j] = t; }
    *margin = std::fmin(g[order[0]], g[order[1]]) - std::fmax(g[order[2]], g[order[3]]);
    int bright[4] = {0, 0, 0, 0};
    bright[order[0]] = 1;
    bright[order[1]] = 1;
    for (int i = 0; i < 4; i++)
        if (bright[0] == kPatterns[i][0] && bright[1] == kPatterns[i][1]
            && bright[2] == kPatterns[i][2] && bright[3] == kPatterns[i][3])
            return i;
    return -1;
}

// Banding: mean |row-mean(y) - row-mean(y+1)| of the green channel, and the mod-4 row
// group spread of the green channel (max - min of the four group means).
static void banding(const std::vector<uint16_t> & rgb, int w, int h, double * row_alt, double * grp_spread, double * mean)
{
    std::vector<double> rm(h, 0.0);
    double total = 0.0;
    for (int y = 0; y < h; y++)
    {
        double s = 0.0;
        const uint16_t * row = rgb.data() + (size_t)y * (size_t)w * 3;
        for (int x = 0; x < w; x++) s += row[x * 3 + 1];
        rm[y] = s / w;
        total += s;
    }
    double alt = 0.0;
    for (int y = 0; y + 1 < h; y++) alt += std::fabs(rm[y] - rm[y + 1]);
    *row_alt = alt / (h - 1);
    double grp[4] = {0, 0, 0, 0};
    double gc[4] = {0, 0, 0, 0};
    for (int y = 0; y < h / 4 * 4; y++) { grp[y % 4] += rm[y]; gc[y % 4] += 1; }
    double lo = 1e30, hi = -1e30;
    for (int i = 0; i < 4; i++) { grp[i] /= gc[i]; lo = std::fmin(lo, grp[i]); hi = std::fmax(hi, grp[i]); }
    *grp_spread = hi - lo;
    *mean = total / ((double)w * h);
}

// Synthetic mode: one 1920x804 frame per step whose bright row pair rotates +1 row per
// camera frame (the M16 rig's phase law), fed through diso_prepare_gpu_recon_state with
// the cached pattern carried between frames exactly as a worker carries diso_pattern.
// Frame brightness is (bright_signal, dark_signal) above black; a dim step models a
// night frame. Prints truth vs resolved per step.
static int run_synthetic(int bright_signal, int dark_signal, int steps)
{
    struct raw_info raw = {};
    raw.width = 1920;
    raw.height = 804;
    raw.pitch = raw.width * 2;
    raw.frame_size = raw.pitch * raw.height;
    raw.bits_per_pixel = 14;
    raw.black_level = 2047;
    raw.white_level = 16200;
    raw.active_area.x2 = raw.width;
    raw.active_area.y2 = raw.height;
    raw.cfa_pattern = 0x02010100;
    dualiso_full20bit_scratch_t scratch = {};
    int iso_pattern = 0;
    int mismatches = 0;
    printf("step,truth,initial,resolved,probe_ok,probe_decisive,redetected,match\n");
    for (int step = 0; step < steps; step++)
    {
        const int truth = step & 3;
        std::vector<uint16_t> frame((size_t)raw.width * raw.height);
        unsigned seed = 12345u + (unsigned)step;
        for (int y = 0; y < raw.height; y++)
            for (int x = 0; x < raw.width; x++)
            {
                seed = seed * 1103515245u + 12345u;
                const int noise = (int)((seed >> 16) & 15) - 8;
                /* scene: a 0.1..1.9 luminance ramp plus sparse 12x highlights (street lights) */
                double scene = 0.1 + 1.8 * (double)((x / 16 + y / 16) % 64) / 63.0;
                if (((seed >> 8) % 997u) == 0u) scene *= 12.0;
                const int base = kPatterns[truth][y & 3] ? bright_signal : dark_signal;
                const int value = raw.black_level + (int)(base * scene) + noise;
                frame[(size_t)y * raw.width + x] = (uint16_t)(value > 16383 ? 16383 : value);
            }
        int auto_correction = -1;
        double ev_correction = 1.0;
        int black_delta = -1;
        dualiso_gpu_recon_state_t state = {};
        dualiso_debug_reset_full20bit_timing();
        diso_prepare_gpu_recon_state(raw, frame.data(), 0, 100, 1600, &iso_pattern, &auto_correction,
                                     &ev_correction, &black_delta, 1, 0, 1, 0, 1, 1, &scratch, &state);
        dualiso_full20bit_timing_t t;
        dualiso_debug_get_full20bit_timing(&t);
        const int resolved = (t.pattern_resolved < 0) ? (-t.pattern_resolved - 1) : t.pattern_resolved - 1;
        mismatches += (resolved != truth);
        printf("%d,%d,%d,%d,%d,%d,%d,%d\n", step, truth, t.pattern_initial, t.pattern_resolved,
               t.phase_probe_succeeded, t.phase_probe_decisive, t.phase_probe_redetected, resolved == truth);
    }
    free_dualiso_full20bit_scratch(&scratch);
    printf("mismatches=%d of %d\n", mismatches, steps);
    return 0;
}

int main(int argc, char ** argv)
{
    if (argc >= 2 && strcmp(argv[1], "--synthetic") == 0)
    {
        if (argc < 5) return 2;
        return run_synthetic(atoi(argv[2]), atoi(argv[3]), atoi(argv[4]));
    }
    if (argc < 5)
    {
        fprintf(stderr, "usage: %s <clip path> <receipt path> <start> <count> [out.csv]\n"
                        "       %s --synthetic <bright_signal> <dark_signal> <steps>\n", argv[0], argv[0]);
        return 2;
    }
    test_runtime::force_single_threaded_pipeline();
    QApplication app(argc, argv);

    const uint64_t start = strtoull(argv[3], nullptr, 10);
    const uint64_t count = strtoull(argv[4], nullptr, 10);
    FILE * out = (argc > 5) ? fopen(argv[5], "w") : stdout;
    if (!out) return 3;

    int open_code = 0;
    char open_error[256] = {0};
    mlvObject_t * video = initMlvObjectWithClip(argv[1], MLV_OPEN_FULL, &open_code, open_error);
    if (!video || open_code)
    {
        fprintf(stderr, "open failed %d %s\n", open_code, open_error);
        return 4;
    }
    processingObject_t * processing = initProcessingObject();
    setMlvProcessing(video, processing);

    ReceiptSettings receipt;
    QString receipt_error;
    if (!ReceiptLoader::loadFromFile(QString::fromLocal8Bit(argv[2]), &receipt, &receipt_error))
    {
        fprintf(stderr, "receipt load failed: %s\n", qPrintable(receipt_error));
        return 5;
    }
    ReceiptApplier::applyToMlv(&receipt, video, processing);
    setMlvAlwaysUseAmaze(video);
    disableMlvCaching(video);
    setMlvCpuCores(video, 1);
    video->cache_next = 0;

    const int w = getMlvWidth(video);
    const int h = getMlvHeight(video);
    const int black = getMlvBlackLevel(video);
    const uint64_t frames = getMlvFrames(video);
    fprintf(stderr, "%dx%d black=%d white=%d frames=%llu validity=%d\n",
            w, h, black, getMlvWhiteLevel(video), (unsigned long long)frames,
            llrpGetDualIsoValidity(video));

    std::vector<uint16_t> raw((size_t)w * h);
    std::vector<uint16_t> rgb((size_t)w * h * 3);
    fprintf(out, "idx,cam_frame,g0,g1,g2,g3,truth,truth_margin,initial,resolved,source,probe_ok,probe_decisive,implied,redetected,resolved_matches_truth,out_row_alt,out_group_spread,out_mean,out_fnv1a\n");
    for (uint64_t i = start; i < start + count && i < frames; i++)
    {
        getMlvRawFrameUint16(video, i, raw.data());
        double g[4];
        double margin = 0.0;
        const int truth = raw_truth(raw.data(), w, h, black, g, &margin);

        dualiso_debug_reset_full20bit_timing();
        getMlvProcessedFrame16(video, i, rgb.data(), 1);
        dualiso_full20bit_timing_t t;
        dualiso_debug_get_full20bit_timing(&t);
        const int resolved_idx = (t.pattern_resolved < 0) ? (-t.pattern_resolved - 1)
                               : (t.pattern_resolved > 0 && t.pattern_resolved <= 4) ? t.pattern_resolved - 1 : -1;

        double row_alt = 0.0, spread = 0.0, mean = 0.0;
        banding(rgb, w, h, &row_alt, &spread, &mean);
        uint64_t fnv = 1469598103934665603ull;
        for (uint16_t v : rgb) { fnv = (fnv ^ v) * 1099511628211ull; }
        fprintf(out, "%llu,%u,%.1f,%.1f,%.1f,%.1f,%d,%.1f,%d,%d,%d,%d,%d,%d,%d,%d,%.1f,%.1f,%.1f,%016llx\n",
                (unsigned long long)i, (unsigned)video->video_index[i].frame_number,
                g[0], g[1], g[2], g[3], truth, margin,
                t.pattern_initial, t.pattern_resolved, t.pattern_source,
                t.phase_probe_succeeded, t.phase_probe_decisive, t.phase_implied_pattern - 1,
                t.phase_probe_redetected, resolved_idx == truth ? 1 : 0,
                row_alt, spread, mean, (unsigned long long)fnv);
        fflush(out);
    }
    if (out != stdout) fclose(out);
    freeMlvObject(video);
    freeProcessingObject(processing);
    return 0;
}
