// UPSTREAM-SYNC-20260920-1 r1 fix 1 (adversary A1 on 1cc7a52d): librtprocess's bayerborder_demosaic, the border pass every
// Bayer kernel runs (RCD border 9, IGV 8, AHD 5, HPHD 4, AMaZE 3, VNG4 3, DCB 1, bayerfast's own), assumed the frame is
// wider and taller than the border. On an 8-wide frame RCD's border 9 wrote column 8 (past the row) and, in the
// last-columns loop, column -1, and read columns -2 and 9; on the production contiguous buffer the last row's writes run
// past the plane. Porting e25c1fa0 lets a receipt select RCD for a rendered batch export, so it became reachable from
// the headless export too. These tests feed the finding's own input (one 8x32 RGGB frame, samples 4096+x+8*y) and the
// border of every kernel, through rows that each carry a guard band on both sides:
//   - no output write lands outside [0, width) of a row (the guards of red/green/blue stay intact);
//   - no in-row output reads outside the frame (the raw guards hold a huge value, so an out-of-frame read pulls an output
//     pixel out of the range of the raw samples, which every 3x3 average of in-frame samples stays inside).
#include "../common/minitest.h"

#include "librtprocess.h"

#include <algorithm>
#include <string>
#include <vector>

namespace
{

constexpr int kGuard = 16;                 // wider than every kernel's border + 1
constexpr float kOutputGuard = -7777.0f;
constexpr float kRawGuard = 1.0e9f;        // an out-of-frame read drags an average far out of the sample range

struct GuardedPlane
{
    GuardedPlane(int width, int height, float fill)
        : width(width), storage(static_cast<size_t>(height) * (width + 2 * kGuard), fill), rows(height)
    {
        for (int y = 0; y < height; ++y) rows[y] = storage.data() + static_cast<size_t>(y) * (width + 2 * kGuard) + kGuard;
    }

    bool guardsIntact(float fill) const
    {
        for (float * row : rows) {
            for (int x = -kGuard; x < 0; ++x) if (row[x] != fill) return false;
            for (int x = width; x < width + kGuard; ++x) if (row[x] != fill) return false;
        }
        return true;
    }

    int width;
    std::vector<float> storage;
    std::vector<float *> rows;
};

struct BorderRun
{
    bool outputGuardsIntact = false;
    bool outputsInSampleRange = false;
    bool ringFilled = true;
    int rc = -1;
};

BorderRun runBorder(int width, int height, int border)
{
    GuardedPlane raw(width, height, kRawGuard);
    float minSample = 1.0e30f, maxSample = -1.0e30f;
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const float sample = static_cast<float>(4096 + x + 8 * y);
            raw.rows[y][x] = sample;
            minSample = std::min(minSample, sample);
            maxSample = std::max(maxSample, sample);
        }
    }
    GuardedPlane red(width, height, kOutputGuard), green(width, height, kOutputGuard), blue(width, height, kOutputGuard);
    const unsigned rggb[2][2] = { { 0, 1 }, { 1, 2 } };

    BorderRun run;
    run.rc = bayerborder_demosaic(width, height, border, raw.rows.data(), red.rows.data(), green.rows.data(),
                                  blue.rows.data(), rggb);
    run.outputGuardsIntact = red.guardsIntact(kOutputGuard) && green.guardsIntact(kOutputGuard)
                             && blue.guardsIntact(kOutputGuard);
    run.outputsInSampleRange = true;
    for (const GuardedPlane * plane : { &red, &green, &blue }) {
        for (int y = 0; y < height; ++y) {
            for (int x = 0; x < width; ++x) {
                const float v = plane->rows[y][x];
                const bool ring = x < border || x >= width - border || y < border || y >= height - border;
                if (v == kOutputGuard) {
                    if (ring) run.ringFilled = false;   // advisory A2 on eb01ef00: every channel's ring pixel is written
                    continue;                           // an interior pixel the border pass does not own
                }
                if (!(v >= minSample && v <= maxSample)) run.outputsInSampleRange = false;
            }
        }
    }
    return run;
}

std::string describe(int width, int height, int border, const BorderRun & run)
{
    return std::to_string(width) + "x" + std::to_string(height) + " border=" + std::to_string(border)
           + " rc=" + std::to_string(run.rc) + " guards=" + (run.outputGuardsIntact ? "intact" : "WRITTEN")
           + " range=" + (run.outputsInSampleRange ? "ok" : "OUT_OF_FRAME_READ")
           + " ring=" + (run.ringFilled ? "filled" : "UNWRITTEN");
}

// every border a Bayer kernel passes: DCB 1, AMaZE/VNG4 3, HPHD 4, AHD 5, IGV 8, RCD 9
const int kKernelBorders[] = { 1, 3, 4, 5, 8, 9 };

} // namespace

// The finding's input: RCD (border 9) on one 8x32 RGGB frame.
TEST(BayerBorderBounds, RcdBorderOnAnEightWideFrameStaysInsideEveryRow)
{
    const BorderRun run = runBorder(8, 32, 9);
    ASSERT_EQ(0, run.rc);
    ASSERT_EQ(std::string("8x32 border=9 rc=0 guards=intact range=ok ring=filled"), describe(8, 32, 9, run));
}

// The class: every kernel's border on frames narrower, shorter, or both, than the border (and the boundary sizes).
TEST(BayerBorderBounds, EveryKernelBorderStaysInsideSmallFrames)
{
    const int sizes[][2] = { { 8, 32 }, { 32, 8 }, { 8, 8 }, { 2, 2 }, { 4, 18 }, { 18, 4 }, { 9, 9 }, { 10, 10 },
                             { 17, 19 }, { 18, 18 }, { 19, 17 } };
    std::string failures;
    for (const int border : kKernelBorders) {
        for (const auto & size : sizes) {
            const BorderRun run = runBorder(size[0], size[1], border);
            if (run.rc != 0 || !run.outputGuardsIntact || !run.outputsInSampleRange || !run.ringFilled) {
                failures += describe(size[0], size[1], border, run) + "; ";
            }
        }
    }
    ASSERT_EQ(std::string(), failures);
}

// A frame larger than twice the border is the production shape: the pass still fills the whole border ring and
// leaves the interior to the kernel.
TEST(BayerBorderBounds, LargeFrameBorderRingIsFilledAndInteriorUntouched)
{
    for (const int border : kKernelBorders) {
        const int width = 64, height = 48;
        GuardedPlane raw(width, height, kRawGuard);
        for (int y = 0; y < height; ++y) for (int x = 0; x < width; ++x) raw.rows[y][x] = static_cast<float>(4096 + x + 8 * y);
        GuardedPlane red(width, height, kOutputGuard), green(width, height, kOutputGuard), blue(width, height, kOutputGuard);
        const unsigned rggb[2][2] = { { 0, 1 }, { 1, 2 } };
        ASSERT_EQ(0, static_cast<int>(bayerborder_demosaic(width, height, border, raw.rows.data(), red.rows.data(),
                                                           green.rows.data(), blue.rows.data(), rggb)));
        ASSERT_TRUE(red.guardsIntact(kOutputGuard) && green.guardsIntact(kOutputGuard) && blue.guardsIntact(kOutputGuard));
        for (int y = 0; y < height; ++y) {
            for (int x = 0; x < width; ++x) {
                const bool ring = x < border || x >= width - border || y < border || y >= height - border;
                // adversary advisory A2 on eb01ef00: all three channels, not green alone
                ASSERT_EQ(ring, red.rows[y][x] != kOutputGuard);
                ASSERT_EQ(ring, green.rows[y][x] != kOutputGuard);
                ASSERT_EQ(ring, blue.rows[y][x] != kOutputGuard);
            }
        }
    }
}
