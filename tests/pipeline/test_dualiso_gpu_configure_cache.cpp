// DUALISO-CONFIGURE-CACHE-1: the CUDA dual-ISO playback route must not re-run its backend configure (set_clip,
// set_luts, the notch flag) on a frame whose configure inputs the backend already holds, and every input the backend
// reads must still reconfigure when it changes. Counts calls (llrpGpuPlaybackReconSetClipCount / SetLutsCount and the
// fake's notch set() counter), never time, through the real llrawproc_gpu_recon_run_backend on the in-process fake.
//
// Measured origin (UPSTREAM-FEED-ISOLATION-1 r1, 1457 s1 CUDA): 1439 set_clip events, 1438 of them "changed=pattern",
// and 1439 set_luts events (set_clip success forced set_luts): configure_ms p50 5.85 ms of a 12.15 ms llrawproc. The clip
// width/height/levels are constant; only the ISO pattern (is_bright[4]) moves, and the backend re-pushes it on every run.
#include "../common/minitest.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <windows.h>

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>

extern "C" int llrpInstallFakeGpuPlaybackReconBackendForTesting(int install);
extern "C" int llrpResetGpuExportBackendForTesting(void);
extern "C" void llrpFakeGpuBackendReducedIsoNotchStateForTesting(int * last_run_flag,
                                                                 uint64_t * runs_with_notch,
                                                                 uint64_t * runs_without_notch,
                                                                 uint64_t * set_calls);

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
    ASSERT_EQ(0u, configureWorkOf(configureState(1), &ok).setClip - 1u);
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
