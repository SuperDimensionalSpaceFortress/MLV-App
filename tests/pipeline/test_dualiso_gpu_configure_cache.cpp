// DUALISO-CONFIGURE-CACHE-1: the CUDA dual-ISO playback route must not re-run its backend configure (set_clip,
// set_luts, the notch flag) on a frame whose configure inputs the backend already holds, and every input the backend
// reads must still reconfigure when it changes. Counts calls (llrpGpuPlaybackReconSetClipCount / SetLutsCount and the
// fake's notch set() counter), never time, through the real llrawproc_gpu_recon_run_backend on the in-process fake.
//
// Measured origin (UPSTREAM-FEED-ISOLATION-1 r1, 1457 s1 CUDA): 1439 set_clip events, 1438 of them "changed=pattern",
// and 1439 set_luts events (set_clip success forced set_luts): configure_ms p50 5.85 ms of a 12.15 ms llrawproc. The clip
// width/height/levels are constant; only the ISO pattern (is_bright[4]) moves, and the backend re-pushes it on every run.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../platform/qt/PlaybackWarmOutputReusePolicy.h"

#include <QByteArray>
#include <QFile>
#include <QString>

#include <string>
#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" int llrpSetFakeGpuBackendIsoPatternSymbolForTesting(int present);
extern "C" void llrpFakeGpuBackendIsoPatternStateForTesting(uint64_t * set_calls, int * last_run_is_bright4);
extern "C" int llrpFakeGpuBackendLastRunStateForTesting(int * width, int * height, int * black, int * white,
                                                         int * is_bright4, const void ** raw2ev, const void ** ev2raw,
                                                         const void ** mix, const void ** fullres, const void ** randn,
                                                         int * notch);
extern "C" void llrpFakeGpuBackendReducedIsoNotchStateForTesting(int * last_run_flag,
                                                                 uint64_t * runs_with_notch,
                                                                 uint64_t * runs_without_notch,
                                                                 uint64_t * set_calls);
extern "C" int llrpGpuPlaybackReconRunReducedNotchProbeForTesting(const llrpGpuPlaybackReconState_t * state,
                                                                   const uint16_t * raw_input_bayer14,
                                                                   size_t raw_image_size, int * rc_out);
extern "C" void llrpFakeGpuBackendInjectRunFailureForTesting(int rc, int cuda_code, const char * name);
extern "C" void llrpSetFakeGpuBackendAfterConfigureStoreHookForTesting(void (*hook)(int kind));

namespace
{

struct ConfigureLuts
{
    int raw2ev[4] = { 0, 0, 0, 0 };
    int ev2raw[4] = { 0, 0, 0, 0 };
    double mix[4] = { 0.0, 0.0, 0.0, 0.0 };
    double fullres[4] = { 0.0, 0.0, 0.0, 0.0 };
    float randn[4] = { 0.0f, 0.0f, 0.0f, 0.0f };
};

ConfigureLuts g_luts;
ConfigureLuts g_lutsAlt;

llrpGpuPlaybackReconState_t configureState(uint64_t frameId, int width = 4, int height = 4)
{
    llrpGpuPlaybackReconState_t state = {};
    state.valid = 1;
    state.width = width;
    state.height = height;
    state.black_level = 100;
    state.white_level = 65535;
    state.white_darkened = 65535;
    state.interp_method = 1;
    state.use_alias_map = 1;
    state.use_fullres = 1;
    state.is_bright[0] = 1;
    state.is_bright[1] = 1;
    state.is_bright[2] = 0;
    state.is_bright[3] = 0;
    state.raw2ev = g_luts.raw2ev;
    state.ev2raw = g_luts.ev2raw;
    state.mix_curve = g_luts.mix;
    state.fullres_curve = g_luts.fullres;
    state.randn05 = g_luts.randn;
    state.ev_correction = -3.0;
    state.dark_noise = 8.0;
    state.frame_id = frameId;
    return state;
}

int runState(const llrpGpuPlaybackReconState_t & state)
{
    std::vector<uint16_t> input(size_t(state.width) * size_t(state.height), 100);
    std::vector<uint16_t> output(input.size());
    int rc = -99;
    llrpGpuPlaybackReconTiming_t timing = {};
    const int used = llrpGpuPlaybackReconRunCpu16Probe(&state, input.data(), input.size() * sizeof(uint16_t),
                                                       output.data(), &rc, &timing);
    return (used != 0 && rc == 0) ? 1 : 0;
}

struct FakeBackendScope
{
    FakeBackendScope() { llrpInstallFakeGpuPlaybackReconBackendForTesting(1); }
    ~FakeBackendScope()
    {
        llrpInstallFakeGpuPlaybackReconBackendForTesting(0);
        llrpResetGpuExportBackendForTesting();
    }
};

struct Counts
{
    uint64_t setClip;
    uint64_t setLuts;
    uint64_t notchSets;
};

Counts counts()
{
    Counts c = {};
    c.setClip = llrpGpuPlaybackReconSetClipCount();
    c.setLuts = llrpGpuPlaybackReconSetLutsCount();
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(nullptr, nullptr, nullptr, &c.notchSets);
    return c;
}

// One frame run on the state, and what configure work it caused (deltas).
Counts configureWorkOf(const llrpGpuPlaybackReconState_t & state, int * ok)
{
    const Counts before = counts();
    *ok = runState(state);
    const Counts after = counts();
    Counts d = {};
    d.setClip = after.setClip - before.setClip;
    d.setLuts = after.setLuts - before.setLuts;
    d.notchSets = after.notchSets - before.notchSets;
    return d;
}

} // namespace

// Two consecutive frames with identical inputs: the second causes no configure work at all.
TEST(DualIsoGpuConfigureCache, AnIdenticalSecondFrameDoesNoConfigureWork)
{
    FakeBackendScope scope;
    int ok = 0;
    const Counts first = configureWorkOf(configureState(1), &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(1u, first.setClip);
    ASSERT_EQ(1u, first.setLuts);
    const Counts second = configureWorkOf(configureState(2), &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(0u, second.setClip);
    ASSERT_EQ(0u, second.setLuts);
    ASSERT_EQ(0u, second.notchSets);
    const Counts third = configureWorkOf(configureState(3), &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(0u, third.setClip);
    ASSERT_EQ(0u, third.setLuts);
    ASSERT_EQ(0u, third.notchSets);
}

// The 1457 shape: geometry and levels constant, the ISO pattern moves every frame. The LUT upload is not repeated
// (set_clip never touches the LUT buffers), whichever way the pattern reaches the backend.
TEST(DualIsoGpuConfigureCache, AMovingIsoPatternDoesNotReuploadTheLuts)
{
    FakeBackendScope scope;
    int ok = 0;
    (void)configureWorkOf(configureState(1), &ok);
    ASSERT_EQ(1, ok);
    for (int i = 0; i < 6; ++i)
    {
        llrpGpuPlaybackReconState_t s = configureState(uint64_t(2 + i));
        const int flip = (i & 1) ? 0 : 1;   // {1,0,0,1} then {1,1,0,0} again
        s.is_bright[0] = 1;
        s.is_bright[1] = flip;
        s.is_bright[2] = 0;
        s.is_bright[3] = flip ? 0 : 1;
        const Counts d = configureWorkOf(s, &ok);
        ASSERT_EQ(1, ok);
        ASSERT_EQ(0u, d.setLuts);
    }
}

// Every LUT-key input forces exactly one set_luts and no set_clip.
TEST(DualIsoGpuConfigureCache, EachLutKeyInputForcesOneSetLutsAndNoSetClip)
{
    FakeBackendScope scope;
    int ok = 0;
    (void)configureWorkOf(configureState(1), &ok);
    ASSERT_EQ(1, ok);
    uint64_t frame = 2;
    const char * names[] = { "raw2ev", "ev2raw", "mix_curve", "fullres_curve", "randn05", "apply_dither",
                             "ev_correction", "dark_noise" };
    for (int which = 0; which < 8; ++which)
    {
        llrpGpuPlaybackReconState_t s = configureState(frame++);
        switch (which)
        {
        case 0: s.raw2ev = g_lutsAlt.raw2ev; break;
        case 1: s.ev2raw = g_lutsAlt.ev2raw; break;
        case 2: s.mix_curve = g_lutsAlt.mix; break;
        case 3: s.fullres_curve = g_lutsAlt.fullres; break;
        case 4: s.randn05 = g_lutsAlt.randn; break;
        case 5: s.apply_dither = 1; break;
        case 6: s.ev_correction = -2.0; break;
        case 7: s.dark_noise = 9.0; break;
        }
        const Counts d = configureWorkOf(s, &ok);
        std::printf("[dualiso-configure-cache] lut input %s: set_clip=%llu set_luts=%llu\n", names[which],
                    (unsigned long long)d.setClip, (unsigned long long)d.setLuts);
        ASSERT_EQ(1, ok);
        ASSERT_EQ(0u, d.setClip);
        ASSERT_EQ(1u, d.setLuts);
        // and the next identical frame is quiet again
        const Counts again = configureWorkOf(s, &ok);
        ASSERT_EQ(1, ok);
        ASSERT_EQ(0u, again.setClip);
        ASSERT_EQ(0u, again.setLuts);
    }
}

// A clip-geometry input (black, white, dims) forces set_clip and the LUT upload with it.
TEST(DualIsoGpuConfigureCache, ClipGeometryInputsForceSetClipAndSetLuts)
{
    FakeBackendScope scope;
    int ok = 0;
    (void)configureWorkOf(configureState(1), &ok);
    ASSERT_EQ(1, ok);

    llrpGpuPlaybackReconState_t black = configureState(2);
    black.black_level = 64;
    Counts d = configureWorkOf(black, &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(1u, d.setClip);
    ASSERT_EQ(1u, d.setLuts);

    llrpGpuPlaybackReconState_t white = configureState(3);
    white.black_level = 64;
    white.white_level = 60000;
    d = configureWorkOf(white, &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(1u, d.setClip);
    ASSERT_EQ(1u, d.setLuts);

    llrpGpuPlaybackReconState_t dims = configureState(4, 8, 4);
    dims.black_level = 64;
    dims.white_level = 60000;
    d = configureWorkOf(dims, &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(1u, d.setClip);
    ASSERT_EQ(1u, d.setLuts);
}

// A recreated backend (device-loss recovery, DLL reload, a new session) holds nothing: it is configured in full again.
TEST(DualIsoGpuConfigureCache, ARecreatedBackendIsConfiguredInFullAgain)
{
    int ok = 0;
    {
        FakeBackendScope scope;
        (void)configureWorkOf(configureState(1), &ok);
        ASSERT_EQ(1, ok);
        const Counts quiet = configureWorkOf(configureState(2), &ok);
        ASSERT_EQ(0u, quiet.setClip);
        ASSERT_EQ(0u, quiet.setLuts);
    }
    FakeBackendScope again;
    const Counts d = configureWorkOf(configureState(3), &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(1u, d.setClip);
    ASSERT_EQ(1u, d.setLuts);
}

// ---- the pattern-only configure (igpu_recon_set_iso_pattern) --------------------------------------------------

namespace
{

void setPattern(llrpGpuPlaybackReconState_t & s, int a, int b, int c, int d)
{
    s.is_bright[0] = a;
    s.is_bright[1] = b;
    s.is_bright[2] = c;
    s.is_bright[3] = d;
}

struct PatternState
{
    uint64_t setCalls;
    int lastRun[4];
};

PatternState patternState()
{
    PatternState p = {};
    llrpFakeGpuBackendIsoPatternStateForTesting(&p.setCalls, p.lastRun);
    return p;
}

} // namespace

// With the export present, a pattern-only change costs one set_iso_pattern and no set_clip or set_luts, and the
// backend's next run sees exactly the pattern that frame asked for (the pixels depend on it).
TEST(DualIsoGpuConfigureCache, APatternOnlyChangeIsOneSetIsoPatternAndTheRunSeesThatPattern)
{
    FakeBackendScope scope;
    int ok = 0;
    llrpGpuPlaybackReconState_t s = configureState(1);
    setPattern(s, 1, 1, 0, 0);
    (void)configureWorkOf(s, &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(1, patternState().lastRun[0]);
    ASSERT_EQ(0, patternState().lastRun[3]);
    const int patterns[3][4] = { { 1, 0, 0, 1 }, { 0, 0, 1, 1 }, { 1, 1, 0, 0 } };
    for (int i = 0; i < 3; ++i)
    {
        llrpGpuPlaybackReconState_t t = configureState(uint64_t(2 + i));
        setPattern(t, patterns[i][0], patterns[i][1], patterns[i][2], patterns[i][3]);
        const uint64_t patternBefore = llrpGpuPlaybackReconSetIsoPatternCount();
        const Counts d = configureWorkOf(t, &ok);
        ASSERT_EQ(1, ok);
        ASSERT_EQ(0u, d.setClip);
        ASSERT_EQ(0u, d.setLuts);
        ASSERT_EQ(patternBefore + 1, llrpGpuPlaybackReconSetIsoPatternCount());
        const PatternState p = patternState();
        for (int k = 0; k < 4; ++k) ASSERT_EQ(patterns[i][k], p.lastRun[k]);
        // the same pattern again is quiet
        const uint64_t patternAfter = llrpGpuPlaybackReconSetIsoPatternCount();
        const Counts again = configureWorkOf(t, &ok);
        ASSERT_EQ(1, ok);
        ASSERT_EQ(0u, again.setClip);
        ASSERT_EQ(0u, again.setLuts);
        ASSERT_EQ(patternAfter, llrpGpuPlaybackReconSetIsoPatternCount());
    }
}

// A DLL without the export (an older build) takes the old set_clip for a pattern change, keeps the pattern right,
// and still does not re-upload the LUTs.
TEST(DualIsoGpuConfigureCache, ADllWithoutSetIsoPatternFallsBackToSetClipAndKeepsTheLuts)
{
    FakeBackendScope scope;
    int ok = 0;
    ASSERT_EQ(1, llrpSetFakeGpuBackendIsoPatternSymbolForTesting(0));
    llrpGpuPlaybackReconState_t s = configureState(1);
    setPattern(s, 1, 1, 0, 0);
    (void)configureWorkOf(s, &ok);
    ASSERT_EQ(1, ok);
    llrpGpuPlaybackReconState_t t = configureState(2);
    setPattern(t, 0, 1, 1, 0);
    const uint64_t patternBefore = llrpGpuPlaybackReconSetIsoPatternCount();
    const Counts d = configureWorkOf(t, &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(1u, d.setClip);
    ASSERT_EQ(0u, d.setLuts);
    ASSERT_EQ(patternBefore, llrpGpuPlaybackReconSetIsoPatternCount());
    const PatternState p = patternState();
    ASSERT_EQ(0, p.lastRun[0]);
    ASSERT_EQ(1, p.lastRun[1]);
    ASSERT_EQ(1, p.lastRun[2]);
    ASSERT_EQ(0, p.lastRun[3]);
}

// A pattern change that arrives together with a geometry change is a set_clip (+ set_luts), never the pattern-only
// call, and leaves the backend holding the new pattern.
TEST(DualIsoGpuConfigureCache, AGeometryChangeWithANewPatternIsAFullSetClip)
{
    FakeBackendScope scope;
    int ok = 0;
    llrpGpuPlaybackReconState_t s = configureState(1);
    setPattern(s, 1, 1, 0, 0);
    (void)configureWorkOf(s, &ok);
    ASSERT_EQ(1, ok);
    llrpGpuPlaybackReconState_t t = configureState(2);
    t.black_level = 64;
    setPattern(t, 0, 0, 1, 1);
    const uint64_t patternBefore = llrpGpuPlaybackReconSetIsoPatternCount();
    const Counts d = configureWorkOf(t, &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(1u, d.setClip);
    ASSERT_EQ(1u, d.setLuts);
    ASSERT_EQ(patternBefore, llrpGpuPlaybackReconSetIsoPatternCount());
    ASSERT_EQ(1, patternState().lastRun[2]);
    ASSERT_EQ(1, patternState().lastRun[3]);
}

// The pattern the key holds is the one last SENT: alternating A, B, A, B sends on every change and never otherwise.
TEST(DualIsoGpuConfigureCache, AnAlternatingPatternIsSentWheneverItChangesAndNeverOtherwise)
{
    FakeBackendScope scope;
    int ok = 0;
    uint64_t sends = 0;
    for (int i = 0; i < 8; ++i)
    {
        llrpGpuPlaybackReconState_t s = configureState(uint64_t(1 + i));
        if (i & 1) setPattern(s, 1, 0, 0, 1);
        else setPattern(s, 1, 1, 0, 0);
        const uint64_t before = llrpGpuPlaybackReconSetIsoPatternCount();
        const Counts d = configureWorkOf(s, &ok);
        ASSERT_EQ(1, ok);
        sends += llrpGpuPlaybackReconSetIsoPatternCount() - before;
        ASSERT_EQ(i == 0 ? 1u : 0u, d.setClip);
        ASSERT_EQ(i == 0 ? 1u : 0u, d.setLuts);
        ASSERT_EQ((i & 1) ? 0 : 1, patternState().lastRun[1]);
    }
    ASSERT_EQ(7u, sends);   // frames 1..7 each differ from the frame before
}

// The model check behind "pixels identical to MASTER": whatever the cache skips, the state the backend RUNS under
// (dims, levels, ISO pattern, LUT pointers, notch) is, on every one of 600 frames, exactly the state that frame's
// inputs describe. A random walk over the four ISO patterns with occasional level, LUT-set and dims changes, run both
// with the export present and with it absent (an older DLL).
namespace
{

struct Lcg
{
    uint32_t s;
    uint32_t next()
    {
        s = s * 1664525u + 1013904223u;
        return s >> 8;
    }
};

void checkEveryRunSawItsFramesState(bool exportPresent)
{
    FakeBackendScope scope;
    if (!exportPresent) ASSERT_EQ(1, llrpSetFakeGpuBackendIsoPatternSymbolForTesting(0));
    const int patterns[4][4] = { { 1, 1, 0, 0 }, { 1, 0, 0, 1 }, { 0, 0, 1, 1 }, { 0, 1, 1, 0 } };
    Lcg rng = { 12345u };
    int black = 100;
    int width = 4;
    const ConfigureLuts * luts = &g_luts;
    uint64_t quietFrames = 0;
    for (int frame = 0; frame < 600; ++frame)
    {
        const uint32_t r = rng.next();
        const int pattern = int(r & 3u);
        if ((r >> 4) % 23u == 0u) black = (black == 100) ? 64 : 100;
        if ((r >> 9) % 31u == 0u) luts = (luts == &g_luts) ? &g_lutsAlt : &g_luts;
        if ((r >> 14) % 47u == 0u) width = (width == 4) ? 8 : 4;
        llrpGpuPlaybackReconState_t s = configureState(uint64_t(frame), width, 4);
        s.black_level = black;
        setPattern(s, patterns[pattern][0], patterns[pattern][1], patterns[pattern][2], patterns[pattern][3]);
        s.raw2ev = luts->raw2ev;
        s.ev2raw = luts->ev2raw;
        s.mix_curve = luts->mix;
        s.fullres_curve = luts->fullres;
        s.randn05 = luts->randn;
        int ok = 0;
        const Counts d = configureWorkOf(s, &ok);
        ASSERT_EQ(1, ok);
        if (d.setClip == 0u && d.setLuts == 0u && d.notchSets == 0u) ++quietFrames;
        int w = 0, h = 0, bl = 0, wh = 0, notch = -1;
        int seen[4] = { -1, -1, -1, -1 };
        const void * raw2ev = nullptr;
        const void * ev2raw = nullptr;
        const void * mix = nullptr;
        const void * fullres = nullptr;
        const void * randn = nullptr;
        ASSERT_EQ(1, llrpFakeGpuBackendLastRunStateForTesting(&w, &h, &bl, &wh, seen, &raw2ev, &ev2raw, &mix,
                                                               &fullres, &randn, &notch));
        ASSERT_EQ(width, w);
        ASSERT_EQ(4, h);
        ASSERT_EQ(black, bl);
        ASSERT_EQ(65535, wh);
        for (int k = 0; k < 4; ++k) ASSERT_EQ(patterns[pattern][k], seen[k]);
        ASSERT_TRUE(raw2ev == luts->raw2ev);
        ASSERT_TRUE(ev2raw == luts->ev2raw);
        ASSERT_TRUE(mix == luts->mix);
        ASSERT_TRUE(fullres == luts->fullres);
        ASSERT_TRUE(randn == luts->randn);
        ASSERT_EQ(0, notch);
    }
    ASSERT_TRUE(quietFrames > 0u);   // the walk repeats a pattern often enough that skipping really happened
}

} // namespace

TEST(DualIsoGpuConfigureCache, EveryRunExecutesUnderExactlyItsFramesStateWithTheExportPresent)
{
    checkEveryRunSawItsFramesState(true);
}

TEST(DualIsoGpuConfigureCache, EveryRunExecutesUnderExactlyItsFramesStateWithTheExportAbsent)
{
    checkEveryRunSawItsFramesState(false);
}

// ---- r3: the three coverage gaps r2 listed ----------------------------------------------------------------------

// SEEK. Frame ids jump forward past frames never played and back to an earlier one. The backend holds the pattern
// last SENT, not the one of the previous frame id, so revisiting a pattern held earlier is a send, a far jump onto
// the pattern it already holds is not, and no seek ever costs a set_clip or a set_luts.
TEST(DualIsoGpuConfigureCache, ASeekToANonAdjacentFrameAndBackToAnEarlierPatternSendsExactlyTheChanges)
{
    FakeBackendScope scope;
    int ok = 0;
    const int A[4] = { 1, 1, 0, 0 };
    const int B[4] = { 1, 0, 0, 1 };
    const int C[4] = { 0, 0, 1, 1 };
    struct Step
    {
        uint64_t frameId;
        const int * pattern;
        unsigned expectSend;
    };
    const Step steps[] = {
        { 1, A, 0 },      // the first run is the full configure (set_clip + set_luts), not a pattern send
        { 2, B, 1 },
        { 3, A, 1 },
        { 500, C, 1 },    // seek forward over 496 frames never run
        { 501, C, 0 },
        { 2, B, 1 },      // seek back: a pattern held earlier (frame 2's) is sent again
        { 3, A, 1 },
        { 3, A, 0 },      // the same frame replayed (pause, step): nothing
        { 1200, A, 0 },   // far jump onto the pattern the backend already holds: nothing
        { 1199, B, 1 },   // a step backwards
        { 7, B, 0 },
    };
    uint64_t sends = 0;
    for (size_t i = 0; i < sizeof(steps) / sizeof(steps[0]); ++i)
    {
        llrpGpuPlaybackReconState_t s = configureState(steps[i].frameId);
        setPattern(s, steps[i].pattern[0], steps[i].pattern[1], steps[i].pattern[2], steps[i].pattern[3]);
        const uint64_t before = llrpGpuPlaybackReconSetIsoPatternCount();
        const Counts d = configureWorkOf(s, &ok);
        ASSERT_EQ(1, ok);
        const uint64_t sent = llrpGpuPlaybackReconSetIsoPatternCount() - before;
        sends += sent;
        ASSERT_EQ(i == 0 ? 1u : 0u, d.setClip);
        ASSERT_EQ(i == 0 ? 1u : 0u, d.setLuts);
        ASSERT_EQ(uint64_t(steps[i].expectSend), sent);
        // whatever the seek did, the run executed under that frame's own pattern
        const PatternState p = patternState();
        for (int k = 0; k < 4; ++k) ASSERT_EQ(steps[i].pattern[k], p.lastRun[k]);
    }
    ASSERT_EQ(6u, sends);
}

// NOTCH = 1 (the reduced route: reduced_iso_notch passed to the backend run). The sticky flag is told once per
// change of the flag and never per run, a moving pattern does not tell it again, and a return to the full-dims route
// (flag 0) and back are one set each.
TEST(DualIsoGpuConfigureCache, TheReducedIsoNotchIsToldOncePerChangeOfTheFlagNotOncePerRun)
{
    FakeBackendScope scope;
    std::vector<uint16_t> input(16, 100);
    int rc = -99;
    int lastFlag = -1;
    uint64_t withNotch = 0, withoutNotch = 0, notchSets = 0;
    uint64_t frame = 1;
    int badRuns = 0;   // a run that was refused or returned non-zero (minitest has no non-fatal check in a lambda)
    auto reduced = [&](int a, int b, int c, int d) -> Counts {
        llrpGpuPlaybackReconState_t s = configureState(frame++);
        setPattern(s, a, b, c, d);
        const Counts before = counts();
        rc = -99;
        const int used = llrpGpuPlaybackReconRunReducedNotchProbeForTesting(&s, input.data(),
                                                                             input.size() * sizeof(uint16_t), &rc);
        Counts delta = counts();
        delta.setClip -= before.setClip;
        delta.setLuts -= before.setLuts;
        delta.notchSets -= before.notchSets;
        if (used != 1 || rc != 0) ++badRuns;
        return delta;
    };
    auto full = [&](int a, int b, int c, int d) -> Counts {
        llrpGpuPlaybackReconState_t s = configureState(frame++);
        setPattern(s, a, b, c, d);
        int ok = 0;
        const Counts delta = configureWorkOf(s, &ok);
        if (ok != 1) ++badRuns;
        return delta;
    };

    Counts d = reduced(1, 1, 0, 0);   // the first reduced run: the full configure and the notch told as 1
    ASSERT_EQ(0, badRuns);
    ASSERT_EQ(1u, d.setClip);
    ASSERT_EQ(1u, d.setLuts);
    ASSERT_EQ(1u, d.notchSets);
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, &withNotch, &withoutNotch, &notchSets);
    ASSERT_EQ(1, lastFlag);
    ASSERT_EQ(1u, withNotch);

    for (int i = 0; i < 6; ++i)       // five more reduced runs, the pattern moving: no notch set, no clip, no LUTs
    {
        d = (i & 1) ? reduced(1, 1, 0, 0) : reduced(1, 0, 0, 1);
        ASSERT_EQ(0u, d.setClip);
        ASSERT_EQ(0u, d.setLuts);
        ASSERT_EQ(0u, d.notchSets);
    }
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, &withNotch, &withoutNotch, &notchSets);
    ASSERT_EQ(1, lastFlag);
    ASSERT_EQ(7u, withNotch);
    ASSERT_EQ(1u, notchSets);

    d = full(1, 0, 0, 1);             // back to full dims: the flag changes to 0, one set
    ASSERT_EQ(0u, d.setClip);
    ASSERT_EQ(0u, d.setLuts);
    ASSERT_EQ(1u, d.notchSets);
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, nullptr, &withoutNotch, &notchSets);
    ASSERT_EQ(0, lastFlag);
    ASSERT_EQ(1u, withoutNotch);
    d = full(1, 1, 0, 0);             // another full-dims run: no set
    ASSERT_EQ(0u, d.notchSets);

    d = reduced(1, 1, 0, 0);          // and back to reduced: the flag changes to 1, one set
    ASSERT_EQ(1u, d.notchSets);
    ASSERT_EQ(0u, d.setClip);
    ASSERT_EQ(0u, d.setLuts);
    d = reduced(1, 1, 0, 0);
    ASSERT_EQ(0u, d.notchSets);
    llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, nullptr, nullptr, &notchSets);
    ASSERT_EQ(1, lastFlag);
    ASSERT_EQ(3u, notchSets);         // 1 -> told once, 0 -> told, 1 -> told: three sets in 12 runs
    ASSERT_EQ(0, badRuns);
}

// DEVICE LOSS. A run fails (the injected failure a device-loss fault leaves behind), the device is gone, and the
// backend is recreated (the product tears the loaded backend down and a new one is created on the next frame).
// The first frame on the new backend is a full set_clip + set_luts + the notch told again: nothing the dead backend
// held is assumed, on the reduced route (notch 1) and on the full route (notch 0 is the new backend's default, still
// told once because the cache does not know it).
TEST(DualIsoGpuConfigureCache, TheFirstFrameAfterADeviceLossIsAFullSetClipSetLutsAndNotch)
{
    std::vector<uint16_t> input(16, 100);
    int rc = -99;
    int ok = 0;
    {
        FakeBackendScope scope;
        for (uint64_t f = 1; f <= 3; ++f)
        {
            llrpGpuPlaybackReconState_t s = configureState(f);
            rc = -99;
            ASSERT_EQ(1, llrpGpuPlaybackReconRunReducedNotchProbeForTesting(&s, input.data(),
                                                                             input.size() * sizeof(uint16_t), &rc));
            ASSERT_EQ(0, rc);
        }
        // the loss: the next run fails and the device is gone
        llrpFakeGpuBackendInjectRunFailureForTesting(-1, 0, "device-loss");
        llrpGpuPlaybackReconState_t failing = configureState(4);
        rc = 0;
        (void)llrpGpuPlaybackReconRunReducedNotchProbeForTesting(&failing, input.data(),
                                                                 input.size() * sizeof(uint16_t), &rc);
        ASSERT_NE(0, rc);
    }
    {
        FakeBackendScope recreated;
        llrpGpuPlaybackReconState_t s = configureState(5);
        const Counts before = counts();
        rc = -99;
        ASSERT_EQ(1, llrpGpuPlaybackReconRunReducedNotchProbeForTesting(&s, input.data(),
                                                                         input.size() * sizeof(uint16_t), &rc));
        ASSERT_EQ(0, rc);
        const Counts after = counts();
        ASSERT_EQ(1u, after.setClip - before.setClip);
        ASSERT_EQ(1u, after.setLuts - before.setLuts);
        ASSERT_EQ(1u, after.notchSets - before.notchSets);
        int lastFlag = -1;
        llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, nullptr, nullptr, nullptr);
        ASSERT_EQ(1, lastFlag);
        // and the frame after it is quiet again
        llrpGpuPlaybackReconState_t t = configureState(6);
        const Counts b = counts();
        ASSERT_EQ(1, llrpGpuPlaybackReconRunReducedNotchProbeForTesting(&t, input.data(),
                                                                         input.size() * sizeof(uint16_t), &rc));
        const Counts a = counts();
        ASSERT_EQ(0u, a.setClip - b.setClip);
        ASSERT_EQ(0u, a.setLuts - b.setLuts);
        ASSERT_EQ(0u, a.notchSets - b.notchSets);
    }
    {
        // the same loss followed by a full-dims first frame: full configure, notch told as 0
        FakeBackendScope fullRoute;
        const Counts first = configureWorkOf(configureState(7), &ok);
        ASSERT_EQ(1, ok);
        ASSERT_EQ(1u, first.setClip);
        ASSERT_EQ(1u, first.setLuts);
        ASSERT_EQ(1u, first.notchSets);
        int lastFlag = -1;
        llrpFakeGpuBackendReducedIsoNotchStateForTesting(&lastFlag, nullptr, nullptr, nullptr);
        ASSERT_EQ(0, lastFlag);
    }
}

// ---- LUT contents that change in place (sol r6 B3) -------------------------------------------------------------
// The cache key holds the LUT POINTERS, so a table rewritten at the same address was invisible to it, and a frame
// that also moved the ISO pattern skipped an upload the pre-cache code made. The state now carries lut_generation,
// which every writer of those tables changes; the backend uploads again when it moves.

extern "C" int llrpFakeGpuBackendDeviceLutEntryForTesting(int which, double * value);

#include "../../tools/gpu/backend/iso_pattern_alternate.h"

namespace
{

const char * const kLutNames[5] = { "raw2ev", "ev2raw", "mix_curve", "fullres_curve", "randn05" };

llrpGpuPlaybackReconState_t stateOnLive(ConfigureLuts & live, uint64_t frame, uint64_t generation)
{
    llrpGpuPlaybackReconState_t s = configureState(frame);
    s.raw2ev = live.raw2ev;
    s.ev2raw = live.ev2raw;
    s.mix_curve = live.mix;
    s.fullres_curve = live.fullres;
    s.randn05 = live.randn;
    s.lut_generation = generation;
    return s;
}

void rewriteInPlace(ConfigureLuts & live, int which)
{
    switch (which)
    {
    case 0: live.raw2ev[0] = 11; break;
    case 1: live.ev2raw[0] = 12; break;
    case 2: live.mix[0] = 0.5; break;
    case 3: live.fullres[0] = 0.25; break;
    case 4: live.randn[0] = 0.125f; break;
    }
}

double expectedEntry(int which)
{
    static const double v[5] = { 11.0, 12.0, 0.5, 0.25, 0.125 };
    return v[which];
}

// One key row: table `which` rewritten at the same address while the ISO pattern moves.
void inPlaceLutChangeRow(int which)
{
    std::printf("[dualiso-configure-cache] in-place rewrite of %s with a pattern change\n", kLutNames[which]);
    FakeBackendScope scope;
    ConfigureLuts live;
    int ok = 0;
    (void)configureWorkOf(stateOnLive(live, 1, 7), &ok);
    ASSERT_EQ(1, ok);
    double before = -1.0;
    ASSERT_EQ(1, llrpFakeGpuBackendDeviceLutEntryForTesting(which, &before));
    ASSERT_EQ(0.0, before);

    // the control: the pattern moves, nothing was rewritten -> still no upload (INV-1 stays)
    llrpGpuPlaybackReconState_t moved = stateOnLive(live, 2, 7);
    setPattern(moved, 1, 0, 0, 1);
    Counts d = configureWorkOf(moved, &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(0u, d.setClip);
    ASSERT_EQ(0u, d.setLuts);

    // the writer rewrites the table in place and stamps the state; the pattern moves back in the same frame
    rewriteInPlace(live, which);
    llrpGpuPlaybackReconState_t s = stateOnLive(live, 3, 8);
    setPattern(s, 1, 1, 0, 0);
    d = configureWorkOf(s, &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(0u, d.setClip);
    ASSERT_EQ(1u, d.setLuts);
    double after = -1.0;
    ASSERT_EQ(1, llrpFakeGpuBackendDeviceLutEntryForTesting(which, &after));
    ASSERT_EQ(expectedEntry(which), after);

    // and the next frame under the same stamp is quiet again
    d = configureWorkOf(stateOnLive(live, 4, 8), &ok);
    ASSERT_EQ(1, ok);
    ASSERT_EQ(0u, d.setClip);
    ASSERT_EQ(0u, d.setLuts);
}

} // namespace

TEST(DualIsoGpuConfigureCache, AnInPlaceRewriteOfRaw2evWithAPatternChangeUploadsTheNewContents)
{
    inPlaceLutChangeRow(0);
}

TEST(DualIsoGpuConfigureCache, AnInPlaceRewriteOfEv2rawWithAPatternChangeUploadsTheNewContents)
{
    inPlaceLutChangeRow(1);
}

TEST(DualIsoGpuConfigureCache, AnInPlaceRewriteOfTheMixCurveWithAPatternChangeUploadsTheNewContents)
{
    inPlaceLutChangeRow(2);
}

TEST(DualIsoGpuConfigureCache, AnInPlaceRewriteOfTheFullresCurveWithAPatternChangeUploadsTheNewContents)
{
    inPlaceLutChangeRow(3);
}

TEST(DualIsoGpuConfigureCache, AnInPlaceRewriteOfRandn05WithAPatternChangeUploadsTheNewContents)
{
    inPlaceLutChangeRow(4);
}

// A rewrite with no pattern change is the same key row: the stamp alone forces one set_luts and no set_clip.
TEST(DualIsoGpuConfigureCache, AnInPlaceRewriteWithoutAPatternChangeAlsoUploadsTheNewContents)
{
    FakeBackendScope scope;
    ConfigureLuts live;
    int ok = 0;
    (void)configureWorkOf(stateOnLive(live, 1, 7), &ok);
    ASSERT_EQ(1, ok);
    for (int which = 0; which < 5; ++which)
    {
        rewriteInPlace(live, which);
        const Counts d = configureWorkOf(stateOnLive(live, uint64_t(2 + which), uint64_t(8 + which)), &ok);
        ASSERT_EQ(1, ok);
        ASSERT_EQ(0u, d.setClip);
        ASSERT_EQ(1u, d.setLuts);
        double after = -1.0;
        ASSERT_EQ(1, llrpFakeGpuBackendDeviceLutEntryForTesting(which, &after));
        ASSERT_EQ(expectedEntry(which), after);
    }
}

// ---- the stamp must survive the Qt presentation path (adversary r7 A1) -----------------------------------------
// RenderFrameThread copies the prepared state's tables into an immutable snapshot (its own cache, keyed by the SOURCE
// table pointers), and MainWindow rebuilds the public state from that snapshot. A key that ignored the stamp there
// would serve a stale snapshot for tables rewritten in place, and a fresh snapshot allocated at a freed one's address
// would look unchanged to the backend. Those two files are not linked into the test binaries, so the wiring is pinned
// on their text (whitespace-insensitive), the way the console wiring tests pin theirs.
namespace
{

std::string squeezedSource(const char * relativePath)
{
    QFile file(repo_file_path(QString::fromLatin1(relativePath)));
    if (!file.open(QIODevice::ReadOnly)) return std::string();
    const QByteArray bytes = file.readAll();
    std::string out;
    for (int i = 0; i < bytes.size(); ++i)
    {
        const char c = bytes.at(i);
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n') out.push_back(c);
    }
    return out;
}

bool has(const std::string & text, const char * needle)
{
    return text.find(needle) != std::string::npos;
}

} // namespace

TEST(DualIsoGpuConfigureCache, TheTextureSnapshotKeyAndStateCarryTheLutGeneration)
{
    const std::string header = squeezedSource("platform/qt/RenderFrameThread.h");
    ASSERT_TRUE(!header.empty());
    // the snapshot cache key holds it and compares it (a stale snapshot is never served for rewritten tables)
    ASSERT_TRUE(has(header, "structLutSnapshotKey{"));
    ASSERT_TRUE(has(header, "&&lutGeneration==other.lutGeneration"));
    // one member in the snapshot cache key, one in the texture state the presenter reads
    size_t members = 0;
    for (size_t at = header.find("uint64_tlutGeneration=0;"); at != std::string::npos;
         at = header.find("uint64_tlutGeneration=0;", at + 1))
        ++members;
    ASSERT_EQ(size_t(2), members);
}

TEST(DualIsoGpuConfigureCache, TheSnapshotBuilderAndThePresenterCopyTheLutGenerationFromTheSourceState)
{
    const std::string source = squeezedSource("platform/qt/RenderFrameThread.cpp");
    ASSERT_TRUE(!source.empty());
    ASSERT_TRUE(has(source, "key.lutGeneration=source.lut_generation;"));
    ASSERT_TRUE(has(source, "destination.lutGeneration=source.lut_generation;"));
    const std::string mainWindow = squeezedSource("platform/qt/MainWindow.cpp");
    ASSERT_TRUE(!mainWindow.empty());
    ASSERT_TRUE(has(mainWindow, "destination->lut_generation=source.lutGeneration;"));
}

// adversary r13 A1: the snapshot cache key must NOT hold the ISO pattern. The snapshot is a memcpy of the source LUT
// tables, which do not depend on the pattern, and the texture state carries the pattern separately. A pattern in the key
// gives every pattern its own snapshot, so a pattern-only change hands the backend new table addresses and its LUT key
// re-uploads the unchanged tables (the very work this card removes), on the Qt texture-present route.
TEST(DualIsoGpuConfigureCache, ThePatternIsNotPartOfTheSnapshotCacheKeyButStaysOnTheTextureState)
{
    const std::string header = squeezedSource("platform/qt/RenderFrameThread.h");
    ASSERT_TRUE(!header.empty());
    ASSERT_TRUE(!has(header, "&&isBright==other.isBright"));
    // exactly one isBright member is left: the texture state's, which the presenter reads
    size_t members = 0;
    for (size_t at = header.find("std::array<int,4>isBright{{0,0,0,0}};"); at != std::string::npos;
         at = header.find("std::array<int,4>isBright{{0,0,0,0}};", at + 1))
        ++members;
    ASSERT_EQ(size_t(1), members);
    const std::string source = squeezedSource("platform/qt/RenderFrameThread.cpp");
    ASSERT_TRUE(!source.empty());
    ASSERT_TRUE(!has(source, "key.isBright[i]=source.is_bright[i];"));
    // the per-frame pattern still reaches the texture state on every call, hit or miss
    ASSERT_TRUE(has(source, "destination.isBright[i]=source.is_bright[i];"));
}

// ---- the count on a configure event belongs to its own call (adversary r13 A1, second pass) -------------------------
// The event logger runs after the backend mutex is released. A count read there is the global cumulative value at THAT
// moment, so a frame that configures in the gap between another frame's unlock and its log line makes the first frame
// report the second one's ordinal and the sequence loses a number. The ordinal is taken under the lock, at the call.
// The after-unlock hook runs a second frame's configure inside that gap, on the same thread (the mutex is free there).
extern "C" void llrpSetGpuReconAfterUnlockHookForTesting(void (*hook)(void));

namespace
{

std::vector<std::string> g_eventLines;
bool g_gapFrameRan = false;

void captureEventLine(const char * line)
{
    g_eventLines.push_back(std::string(line));
}

void runTheSecondFrameInTheGap()
{
    llrpSetGpuReconAfterUnlockHookForTesting(nullptr);
    llrpGpuPlaybackReconState_t second = configureState(12); // pattern 1100 again: its own set_iso_pattern
    g_gapFrameRan = runState(second) != 0;
}

// the count= of the set_iso_pattern line a given frame logged, or -1
long long patternCountLoggedBy(unsigned long long frame)
{
    for (size_t i = 0; i < g_eventLines.size(); ++i)
    {
        unsigned long long f = 0;
        double wall = 0.0;
        double dur = 0.0;
        int w = 0;
        int h = 0;
        int rc = 0;
        long long count = -1;
        if (std::sscanf(g_eventLines[i].c_str(),
                        "gpu_recon_event kind=set_iso_pattern frame=%llu wall_ms=%lf dur_ms=%lf width=%d height=%d "
                        "rc=%d count=%lld",
                        &f, &wall, &dur, &w, &h, &rc, &count) == 7
            && f == frame)
            return count;
    }
    return -1;
}

} // namespace

TEST(DualIsoGpuConfigureCache, ASetIsoPatternEventCarriesTheOrdinalOfItsOwnCallNotTheCountAtLogTime)
{
    FakeBackendScope scope;
    ASSERT_EQ(1, llrpSetFakeGpuBackendIsoPatternSymbolForTesting(1));
    int ok = 0;
    (void)configureWorkOf(configureState(10), &ok); // 1100: the first configure
    ASSERT_EQ(1, ok);
    const long long before = (long long)llrpGpuPlaybackReconSetIsoPatternCount();

    g_eventLines.clear();
    g_gapFrameRan = false;
    llrpSetGpuReconEventLogger(&captureEventLine);
    llrpGpuPlaybackReconState_t first = configureState(11);
    first.is_bright[0] = 1; first.is_bright[1] = 0; first.is_bright[2] = 0; first.is_bright[3] = 1; // 1001
    llrpSetGpuReconAfterUnlockHookForTesting(&runTheSecondFrameInTheGap);
    const int firstOk = runState(first);
    llrpSetGpuReconAfterUnlockHookForTesting(nullptr);
    llrpSetGpuReconEventLogger(nullptr);

    ASSERT_EQ(1, firstOk);
    ASSERT_TRUE(g_gapFrameRan);
    ASSERT_EQ(before + 2, (long long)llrpGpuPlaybackReconSetIsoPatternCount());
    // frame 11 made the first call, frame 12 the second: each line names its own ordinal
    ASSERT_EQ(before + 1, patternCountLoggedBy(11));
    ASSERT_EQ(before + 2, patternCountLoggedBy(12));
}

// ---- adversary r14 A1: a logger that uninstalls itself from inside its own call ---------------------------------------
// One configure can log up to three events (set_iso_pattern, set_clip, set_luts). The code checked the global logger once
// and then called it for each event; a logger that calls llrpSetGpuReconEventLogger(NULL) in its set_iso_pattern event
// left the global NULL, and the next event called it. Each event must read the global once and call only a non-NULL value.
namespace
{

void captureThenUninstallOnPattern(const char * line)
{
    g_eventLines.push_back(std::string(line));
    if (std::strstr(line, "kind=set_iso_pattern") != nullptr) llrpSetGpuReconEventLogger(nullptr);
}

} // namespace

TEST(DualIsoGpuConfigureCache, ALoggerThatUninstallsItselfInItsPatternEventIsNotCalledAgainAndNothingCallsNull)
{
    FakeBackendScope scope;
    ASSERT_EQ(1, llrpSetFakeGpuBackendIsoPatternSymbolForTesting(1));
    int ok = 0;
    (void)configureWorkOf(configureState(10), &ok); // 1100: the first configure
    ASSERT_EQ(1, ok);
    g_eventLines.clear();
    llrpSetGpuReconEventLogger(&captureThenUninstallOnPattern);
    llrpGpuPlaybackReconState_t s = configureState(11);
    s.is_bright[0] = 0; s.is_bright[1] = 0; s.is_bright[2] = 1; s.is_bright[3] = 1; // 0011: a set_iso_pattern
    s.dark_noise = 9.0;                                                              // and a set_luts
    const int ran = runState(s); // on the unfixed code this calls the NULL logger after the pattern event
    llrpSetGpuReconEventLogger(nullptr);
    ASSERT_EQ(1, ran);
    ASSERT_EQ(1u, g_eventLines.size()); // only the pattern line; events after the uninstall are not delivered
    ASSERT_TRUE(g_eventLines[0].find("kind=set_iso_pattern") != std::string::npos);
}

// ---- the DLL harness's alternate pattern (sol r6 B1) ------------------------------------------------------------
// dll_test.cpp feeds igpu_recon_set_iso_pattern an alternate pattern and expects the output to change. For a truth
// pattern that equals its own half-swap (1,0,1,0 / 0,1,0,1 / 0,0,0,0 / 1,1,1,1) the alternate was the truth itself,
// the output could not differ, and a correct backend was failed. Every binary pattern must get an alternate that
// differs from it.
namespace
{

int alternateDiffers(int a, int b, int c, int d)
{
    const int truth[4] = { a, b, c, d };
    int other[4] = { -1, -1, -1, -1 };
    const int differs = dll_test_pick_alternate_iso_pattern(truth, other);
    const int reallyDiffers = (other[0] != truth[0] || other[1] != truth[1] || other[2] != truth[2]
                               || other[3] != truth[3]) ? 1 : 0;
    std::printf("[dualiso-configure-cache] harness alternate for %d,%d,%d,%d is %d,%d,%d,%d (reports differs=%d)\n",
                a, b, c, d, other[0], other[1], other[2], other[3], differs);
    // the helper must tell the truth about what it picked, and what it picked must be a binary pattern
    for (int k = 0; k < 4; ++k) ASSERT_TRUE(other[k] == 0 || other[k] == 1);
    ASSERT_EQ(reallyDiffers, differs);
    return reallyDiffers;
}

} // namespace

TEST(DualIsoGpuConfigureCache, TheHarnessAlternateDiffersFromTheTruthForTheAlternatingPattern1010)
{
    ASSERT_EQ(1, alternateDiffers(1, 0, 1, 0));
}

TEST(DualIsoGpuConfigureCache, TheHarnessAlternateDiffersFromTheTruthForTheAlternatingPattern0101)
{
    ASSERT_EQ(1, alternateDiffers(0, 1, 0, 1));
}

TEST(DualIsoGpuConfigureCache, TheHarnessAlternateDiffersFromTheTruthForTheUniformPattern1111)
{
    ASSERT_EQ(1, alternateDiffers(1, 1, 1, 1));
}

TEST(DualIsoGpuConfigureCache, TheHarnessAlternateDiffersFromTheTruthForTheUniformPattern0000)
{
    ASSERT_EQ(1, alternateDiffers(0, 0, 0, 0));
}

TEST(DualIsoGpuConfigureCache, TheHarnessAlternateDiffersFromTheTruthForEveryBinaryPattern)
{
    for (int m = 0; m < 16; ++m)
        ASSERT_EQ(1, alternateDiffers(m & 1, (m >> 1) & 1, (m >> 2) & 1, (m >> 3) & 1));
}

// ---- the DLL harness's buffer allocations (sol r16 #2) --------------------------------------------------------------
// The checked-in 8192x4320 vector needs three more 70,778,880-byte buffers in the pattern step, and the harness passed
// each malloc result straight to memset. A failed allocation must be a typed diagnostic and a non-zero exit, never a
// null dereference. The helper is header-only so an allocator that fails can be injected here; dll_test.cpp is not
// linked into the test binaries, so its use of the helper is pinned on its text, as the Qt wiring above is.
#include "../../tools/gpu/backend/dll_test_alloc.h"

namespace
{

size_t g_allocCalls = 0;
size_t g_allocBytes = 0;

void * failingAlloc(size_t bytes)
{
    ++g_allocCalls;
    g_allocBytes = bytes;
    return nullptr;
}

void * dirtyAlloc(size_t bytes)
{
    ++g_allocCalls;
    g_allocBytes = bytes;
    void * p = std::malloc(bytes);
    if (p) std::memset(p, 0xAB, bytes);
    return p;
}

} // namespace

TEST(DualIsoGpuConfigureCache, TheHarnessAllocationHelperReturnsNullWhenTheAllocatorFails)
{
    g_allocCalls = 0;
    g_allocBytes = 0;
    const size_t n = size_t(8192) * 4320; // the res8k vector
    uint16_t * p = dll_test_alloc_u16_zeroed(n, "alt", failingAlloc);
    ASSERT_TRUE(p == nullptr);
    ASSERT_EQ(size_t(1), g_allocCalls);
    ASSERT_EQ(size_t(70778880), g_allocBytes);
}

TEST(DualIsoGpuConfigureCache, TheHarnessAllocationHelperZeroFillsWhatTheAllocatorReturns)
{
    g_allocCalls = 0;
    uint16_t * p = dll_test_alloc_u16_zeroed(1024, "back", dirtyAlloc);
    ASSERT_TRUE(p != nullptr);
    ASSERT_EQ(size_t(1), g_allocCalls);
    ASSERT_EQ(size_t(2048), g_allocBytes);
    size_t nonZero = 0;
    for (size_t i = 0; i < 1024; ++i) nonZero += p[i] != 0u;
    std::free(p);
    ASSERT_EQ(size_t(0), nonZero);
}

TEST(DualIsoGpuConfigureCache, TheHarnessAllocationHelperRefusesASizeThatOverflowsWithoutCallingTheAllocator)
{
    g_allocCalls = 0;
    ASSERT_TRUE(dll_test_alloc_u16_zeroed((size_t)-1, "alt_ref", failingAlloc) == nullptr);
    ASSERT_TRUE(dll_test_alloc_u16_zeroed(0, "alt_ref", failingAlloc) == nullptr);
    ASSERT_EQ(size_t(0), g_allocCalls);
}

TEST(DualIsoGpuConfigureCache, TheDllHarnessChecksEveryBufferAllocationBeforeUsingIt)
{
    const std::string src = squeezedSource("tools/gpu/backend/dll_test.cpp");
    ASSERT_TRUE(!src.empty());
    ASSERT_TRUE(has(src, "#include\"dll_test_alloc.h\""));
    // no raw uint16 plane allocation is left: each goes through the helper and is tested before use
    ASSERT_TRUE(!has(src, "(uint16_t*)malloc("));
    // the three pattern-step buffers, tested together before the first memcmp, run or use
    ASSERT_TRUE(has(src, "uint16_t*alt=dll_test_alloc_u16_zeroed(n,\"alt\",malloc);"));
    ASSERT_TRUE(has(src, "uint16_t*alt_ref=dll_test_alloc_u16_zeroed(n,\"alt_ref\",malloc);"));
    ASSERT_TRUE(has(src, "uint16_t*back=dll_test_alloc_u16_zeroed(n,\"back\",malloc);"));
    ASSERT_TRUE(has(src, "if(!alt||!alt_ref||!back){free(alt);free(alt_ref);free(back);return4;}"));
}

// ---- r22 (sol r21 #1): the warm-output reuse gate after a native configure whose count is not yet published -------
//
// Sol's schedule: with reuse on, the recon worker's set_iso_pattern stores 0011 in the backend while the pattern count
// still reads 11; if both of the present's post-compare reads land in that interval, decide() returns Hit and AMaZE is
// skipped although the backend state already changed. The fake's after-store hook runs exactly there: the native state
// has changed and llrawproc_gpu_recon_run_backend has not yet run its next statement. decide() is run from inside it,
// reading the three process-wide counts the way GpuDisplayWindow's readLiveConfigureCounts does, with a compare that
// says "equal". Any outcome but recon_configured_since_warm is the race. Clip (set_clip) and LUT (set_luts) alike.

namespace
{

playback_warm_output_reuse::LiveCounts liveConfigureCountsLikeThePresent()
{
    playback_warm_output_reuse::LiveCounts now;
    now.setClip = llrpGpuPlaybackReconSetClipCount();
    now.setLuts = llrpGpuPlaybackReconSetLutsCount();
    now.setPattern = llrpGpuPlaybackReconSetIsoPatternCount();
    return now;
}

struct RaceProbe
{
    int kind = -1;                      // which fake store runs the decision (0 pattern, 1 clip, 2 luts)
    int probes = 0;
    playback_warm_output_reuse::Key warmKey;
    playback_warm_output_reuse::Reason reason = playback_warm_output_reuse::Reason::None;
};

RaceProbe g_race;

playback_warm_output_reuse::Key raceWarmKeyFromLiveCounts()
{
    playback_warm_output_reuse::Key k;
    k.valid = true;
    k.frame = 1;
    k.width = 4;
    k.height = 4;
    k.blackLevel = 100;
    k.wb[0] = 2.0;
    k.wb[1] = 1.0;
    k.wb[2] = 1.5;
    k.captureId = 7;
    k.state.valid = true;
    const playback_warm_output_reuse::LiveCounts c = liveConfigureCountsLikeThePresent();
    k.setClipCountAfterWarm = c.setClip;
    k.setLutsCountAfterWarm = c.setLuts;
    k.setPatternCountAfterWarm = c.setPattern;
    return k;
}

playback_warm_output_reuse::Reason decideWithAnEqualCompare(const playback_warm_output_reuse::Key & warmKey)
{
    bool attempted = false;
    return playback_warm_output_reuse::decide(
        true, true, warmKey, warmKey, false, false, liveConfigureCountsLikeThePresent,
        [](bool * equal, QString * why)
        {
            *equal = true;
            why->clear();
            return true;
        },
        &attempted);
}

void raceProbeAfterNativeStore(int kind)
{
    if (kind != g_race.kind) return;
    ++g_race.probes;
    g_race.reason = decideWithAnEqualCompare(g_race.warmKey);
}

struct AfterStoreHookScope
{
    AfterStoreHookScope() { llrpSetFakeGpuBackendAfterConfigureStoreHookForTesting(raceProbeAfterNativeStore); }
    ~AfterStoreHookScope()
    {
        llrpSetFakeGpuBackendAfterConfigureStoreHookForTesting(nullptr);
        g_race = RaceProbe();
    }
};

// One configure kind: warm on `warmState`, then run `changed` with the decision taken inside the fake's store of `kind`.
void expectTheDecisionInsideTheNativeStoreIsAMiss(const char * label, int kind, const llrpGpuPlaybackReconState_t & warmState,
                                                  const llrpGpuPlaybackReconState_t & changed)
{
    int ok = 0;
    (void)configureWorkOf(warmState, &ok);
    ASSERT_EQ(1, ok);
    g_race = RaceProbe();
    g_race.warmKey = raceWarmKeyFromLiveCounts();
    // not vacuous: with no configure since the warm the same decision is a hit
    ASSERT_EQ(std::string("hit"), std::string(playback_warm_output_reuse::reasonName(decideWithAnEqualCompare(g_race.warmKey))));
    g_race.kind = kind;
    (void)configureWorkOf(changed, &ok);
    ASSERT_EQ(1, ok);
    std::printf("[dualiso-configure-cache] r22 race %s: probes=%d reason_inside_native_store=%s\n", label, g_race.probes,
                playback_warm_output_reuse::reasonName(g_race.reason));
    ASSERT_EQ(1, g_race.probes);
    ASSERT_EQ(std::string("recon_configured_since_warm"),
              std::string(playback_warm_output_reuse::reasonName(g_race.reason)));
    g_race.kind = -1;
}

} // namespace

TEST(DualIsoGpuConfigureCache, AReuseDecisionInsideANativePatternStoreIsNeverAHit)
{
    FakeBackendScope scope;
    AfterStoreHookScope hook;
    llrpGpuPlaybackReconState_t warm = configureState(1);
    setPattern(warm, 1, 1, 0, 0);
    llrpGpuPlaybackReconState_t changed = configureState(2);
    setPattern(changed, 0, 0, 1, 1);   // sol's 1100 -> 0011: a pattern-only configure (set_iso_pattern)
    expectTheDecisionInsideTheNativeStoreIsAMiss("set_iso_pattern", 0, warm, changed);
}

TEST(DualIsoGpuConfigureCache, AReuseDecisionInsideANativeClipStoreIsNeverAHit)
{
    FakeBackendScope scope;
    AfterStoreHookScope hook;
    llrpGpuPlaybackReconState_t changed = configureState(2);
    changed.black_level = 64;   // a level change: set_clip (then set_luts)
    expectTheDecisionInsideTheNativeStoreIsAMiss("set_clip", 1, configureState(1), changed);
}

TEST(DualIsoGpuConfigureCache, AReuseDecisionInsideANativeLutStoreIsNeverAHit)
{
    FakeBackendScope scope;
    AfterStoreHookScope hook;
    llrpGpuPlaybackReconState_t changed = configureState(2);
    changed.ev_correction = -2.0;   // a LUT-key-only change: set_luts alone
    expectTheDecisionInsideTheNativeStoreIsAMiss("set_luts", 2, configureState(1), changed);
}
