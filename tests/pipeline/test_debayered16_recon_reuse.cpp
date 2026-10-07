// CPU-DEBAYERED16-REUSE-PHASE3-RECON-1: during CPU playback on the preview-processing
// route (fast processing for playback, subset, with the preview config enabled) the
// render requests OutputDebayered16. The phase-3 recon worker has already decoded the
// frame and run the full llrawproc (dual-ISO recon included) on its own worker state;
// the render used to throw that away and decode + reconstruct again inside
// getMlvRawFrameDebayered. It now debayers the worker's reconstruction
// (getMlvRawFrameDebayeredFromReconnedRaw16) whenever Debayered16ReconReusePolicy
// admits the frame. RenderFrameThread is not linked into the test targets. The render
// side runs the production dispatch itself (renderDebayered16FromSlot, which
// RenderFrameThread::drawFrame calls); the worker side, reconWorkerStep() below, performs
// the recon worker's decode, llrawproc and the two stamps RenderFrameThread records
// around them (decodeFrameForWorker, signalReconDoneFromWorker). The tests pin:
//  (a) byte-exact output (memcmp, zero tolerance) against today's render over >= 8
//      consecutive frames, two fresh objects: HQ dual-ISO with AMaZE and bilinear (bit
//      shift 0), preview dual-ISO (mode 2) and dual-ISO off (both bit shift 16 - bpp),
//      each also checked for real picture content, so a common-mode degenerate output
//      cannot pass;
//  (b) the render runs no second recon (the per-thread llrawproc run counter), and
//      the mutation: forcing today's path (the kill
//      switch production honours) makes that check fail;
//  (c) the reuse never reads or writes the single-frame cache, and a paused frame and
//      the export path afterwards give the same bytes as on a fresh object;
//  (d) a frame cache that could serve the frame makes the entry decline untouched;
//  (e) the policy admits only full-resolution playback frames the worker reconstructed
//      from a successful decode, on the CPU recon, under unchanged llrawproc settings;
//  (f) r2: Raw Fix off, or a dual-ISO mode change, between the recon and its
//      consumption is refused and counted; the frame equals today's path, and
//      consuming the stale recon would not have;
//  (g) r2: a failed worker decode (truncated LJ92 payload) is refused and counted; the
//      frame is today's zero-filled failure output, and consuming would not have been;
//  (h) r3: Raw Fix off after the policy admitted the recon and before its debayer: the
//      bit shift is the recon-time HQ state, so the frame is still byte-exact;
//  (i) r3: what the decode and recon-done stamps catch, and the A -> B -> A limit;
//  (j) r3: the CUDA-recon provenance handoff from the recon-done stamp to the policy.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"
#include "../../platform/qt/Debayered16ReconReusePolicy.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <functional>
#include <set>
#include <string>
#include <vector>

#ifdef _WIN32
#define DEBAYERED16_REUSE_TEST_SETENV(name, value) _putenv_s((name), (value))
#define DEBAYERED16_REUSE_TEST_UNSETENV(name) _putenv_s((name), "")
#else
#define DEBAYERED16_REUSE_TEST_SETENV(name, value) setenv((name), (value), 1)
#define DEBAYERED16_REUSE_TEST_UNSETENV(name) unsetenv((name))
#endif

namespace {

struct KillSwitchGuard
{
    KillSwitchGuard() { DEBAYERED16_REUSE_TEST_UNSETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV); }
    ~KillSwitchGuard() { DEBAYERED16_REUSE_TEST_UNSETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV); }
};

void setKillSwitch(bool on)
{
    if (on) DEBAYERED16_REUSE_TEST_SETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV, "1");
    else DEBAYERED16_REUSE_TEST_UNSETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV);
}

struct WorkerState
{
    WorkerState() { llrpInitWorkerState(&state); }
    ~WorkerState() { llrpFreeWorkerState(&state); }
    llrawprocWorkerState_t state;
};

enum class Debayer { Receipt, Amaze, Bilinear };

const char * kHqReceipt = "tests/fixtures/receipts/large_dual_iso_hq.marxml";
const char * kPreviewReceipt = "tests/fixtures/receipts/large_dual_iso_preview.marxml";

bool openLargeFixture(MlvPipelineFixture & fixture, const char * receipt, Debayer debayer, int cpuCores)
{
    QString error;
    if (!fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error))
        return false;
    if (!fixture.loadReceipt(QString::fromLatin1(receipt), &error)) return false;
    if (!fixture.applyReceipt(&error)) return false;
    if (debayer == Debayer::Amaze) setMlvAlwaysUseAmaze(fixture.video());
    if (debayer == Debayer::Bilinear) setMlvDontAlwaysUseAmaze(fixture.video());
    setMlvCpuCores(fixture.video(), cpuCores);
    return true;
}

bool openHqFixture(MlvPipelineFixture & fixture, bool large, Debayer debayer, int cpuCores)
{
    if (large) return openLargeFixture(fixture, kHqReceipt, debayer, cpuCores);
    QString error;
    if (!fixture.openTinyDualIso(&error)) return false;
    if (!fixture.loadReceipt(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"), &error)) return false;
    if (!fixture.applyReceipt(&error)) return false;
    if (debayer == Debayer::Amaze) setMlvAlwaysUseAmaze(fixture.video());
    if (debayer == Debayer::Bilinear) setMlvDontAlwaysUseAmaze(fixture.video());
    setMlvCpuCores(fixture.video(), cpuCores);
    return true;
}

size_t pixels(MlvPipelineFixture & fixture)
{
    return static_cast<size_t>(fixture.width()) * static_cast<size_t>(fixture.height());
}

// The render thread's per-slot state for one frame (the FrameSlot fields the dispatch reads).
struct Slot
{
    std::vector<uint16_t> rawImage16;  // W*H*3, like FrameSlot::rawImage16
    std::vector<float> scratch;        // RenderFrameThread::m_debayered16ReconScratch
    Debayered16ReconProvenance provenance;  // FrameSlot::reconProvenance
};

using SettingsChange = std::function<void(mlvObject_t *)>;

// Where a test changes settings around one frame's recon and render (r3 temporal tests).
struct FrameHooks
{
    SettingsChange beforeRecon;     // after the decode stamp and the decode, before llrawproc
    SettingsChange afterRecon;      // after llrawproc, before the recon-done stamp
    SettingsChange afterAdmission;  // inside the production dispatch: admitted, not yet debayered
};

void runAfterAdmission(mlvObject_t * video, void * context)
{
    const SettingsChange * change = static_cast<const SettingsChange *>(context);
    if (change && *change) (*change)(video);
}

struct StepResult
{
    Debayered16ReconRefusal outcome = Debayered16ReconRefusal::NotReconned;
    bool reused = false;
    std::string refusal;
    uint64_t renderLlrawprocRuns = 0;     // full llrawproc runs on the render step
    double renderLlrawprocTotalMs = -1.0;
    double renderLlrawprocMs = -1.0;
    double renderRawUint16Ms = -1.0;
};

// The recon worker for one frame, as RenderFrameThread runs it on this route:
// decodeFrameForWorker (settings stamp; the decode, its status recorded into the slot,
// which keeps whatever it held before when the decode fails), reconFrameForWorker's
// full-res llrawproc on the persistent worker state (flags 0), then
// signalReconDoneFromWorker's stamp (the production debayered16ReconDoneStamp).
void reconWorkerStep(MlvPipelineFixture & fixture, uint64_t frame, llrawprocWorkerState_t * worker, Slot & slot,
                     const FrameHooks & hooks = FrameHooks())
{
    const size_t n = pixels(fixture);
    if (slot.rawImage16.size() < n * 3u) slot.rawImage16.resize(n * 3u);
    slot.provenance.settingsAtDecode = getMlvLlrawprocSettingsFingerprint(fixture.video());
    slot.provenance.acquisitionSucceeded =
        getMlvRawFrameUint16(fixture.video(), frame, slot.rawImage16.data()) == 0;
    if (hooks.beforeRecon) hooks.beforeRecon(fixture.video());
    applyLLRawProcObjectWorker(fixture.video(), slot.rawImage16.data(), n * sizeof(uint16_t), worker, 0);
    if (hooks.afterRecon) hooks.afterRecon(fixture.video());
    slot.provenance.done = debayered16ReconDoneStamp(fixture.video());
}

// The render's OutputDebayered16 branch for one frame: the production handoff
// (applyDebayered16ReconProvenance) and dispatch.
StepResult renderStep(MlvPipelineFixture & fixture, uint64_t frame, Slot & slot, bool playing,
                      Debayered16ReconReuseCounters * counters, const SettingsChange & afterAdmission = nullptr)
{
    const size_t n = pixels(fixture);
    Debayered16ReconReuseInputs reuse;
    reuse.consumeReconnedRaw = true;
    applyDebayered16ReconProvenance(slot.provenance, reuse);
    reuse.playbackActive = playing;
    reuse.reducedReconScale = 1;
    reuse.reconFrameNumber = static_cast<uint32_t>(frame);
    reuse.renderFrameNumber = static_cast<uint32_t>(frame);
    reuse.reconRequestSerial = 7;
    reuse.renderRequestSerial = 7;
    reuse.reconBufferComplete = slot.rawImage16.size() >= n * 3u;

    StepResult result;
    llrpResetDebugRunCount();
    result.outcome = renderDebayered16FromSlot(fixture.video(), static_cast<uint32_t>(frame), reuse,
                                               slot.rawImage16.data(), n, slot.scratch, counters,
                                               afterAdmission ? &runAfterAdmission : nullptr,
                                               const_cast<SettingsChange *>(&afterAdmission));
    result.reused = result.outcome == Debayered16ReconRefusal::None;
    result.refusal = debayered16ReconRefusalReason(result.outcome);
    result.renderLlrawprocRuns = llrpGetDebugRunCount();
    result.renderLlrawprocTotalMs = llrpGetLastTotalMilliseconds();
    result.renderLlrawprocMs = getMlvLastLlrawprocMilliseconds();
    result.renderRawUint16Ms = getMlvLastRawUint16Milliseconds();
    return result;
}

StepResult playbackRenderStep(MlvPipelineFixture & fixture, uint64_t frame, llrawprocWorkerState_t * worker,
                              Slot & slot, bool playing = true, Debayered16ReconReuseCounters * counters = nullptr)
{
    reconWorkerStep(fixture, frame, worker, slot);
    return renderStep(fixture, frame, slot, playing, counters);
}

// The success gate: the render consumed the recon and ran no llrawproc of its own. The
// proof is the run counter, never a duration; the ms values are printed for the record.
bool renderRanNoSecondRecon(const StepResult & r)
{
    return r.reused && r.renderLlrawprocRuns == 0;
}

size_t mismatchedWords(const std::vector<uint16_t> & a, const std::vector<uint16_t> & b, size_t words)
{
    size_t mismatched = 0;
    for (size_t i = 0; i < words; ++i)
        if (a[i] != b[i]) ++mismatched;
    return mismatched;
}

size_t nonzeroWords(const std::vector<uint16_t> & a)
{
    return static_cast<size_t>(std::count_if(a.begin(), a.end(), [](uint16_t v) { return v != 0; }));
}

// Independent of the comparison: the frame is a picture, not a constant or near-empty
// buffer both paths could agree on (sol r1 H1).
void assertRealPicture(const uint16_t * rgb, size_t words, const char * label)
{
    size_t nonzero = 0;
    uint16_t lo = 0xFFFFu, hi = 0;
    std::set<uint16_t> distinct;
    for (size_t i = 0; i < words; ++i)
    {
        const uint16_t v = rgb[i];
        if (v) ++nonzero;
        lo = std::min(lo, v);
        hi = std::max(hi, v);
        if (distinct.size() < 4096 && (i % 7) == 0) distinct.insert(v);
    }
    std::printf("[debayered16-recon-reuse] %s content: nonzero=%zu of %zu min=%u max=%u distinct_sampled=%zu\n",
                label, nonzero, words, static_cast<unsigned>(lo), static_cast<unsigned>(hi), distinct.size());
    ASSERT_TRUE(nonzero > words / 2);
    ASSERT_TRUE(hi > lo);
    ASSERT_TRUE(hi - lo > 1024);
    ASSERT_TRUE(distinct.size() >= 256);
}

// r3 (fable r2 H4): the first frame's refusal cause, asserted. The recon loaded the
// focus pixel map status (0 -> nonzero, version bumped once) and may have published
// the clip's first dual-ISO pattern / EV / black delta; putting exactly those fields
// back gives the decode stamp again, so nothing else changed during that recon.
void assertFirstFrameRefusalIsTheFirstRunLoad(mlvObject_t * video, const llrawprocObject_t & before,
                                              const Debayered16ReconProvenance & provenance, const char * label)
{
    llrawprocObject_t * live = video->llrawproc;
    const llrawprocObject_t now = *live;
    std::printf("[debayered16-recon-reuse] %s frame 0 cause: fpm_status %d->%d fpm_ver %u->%u "
                "diso_pattern %d->%d diso_ev %.6f->%.6f diso_black_delta %d->%d\n",
                label, before.fpm_status, now.fpm_status, before.focus_pixel_map_version,
                now.focus_pixel_map_version, before.diso_pattern, now.diso_pattern, before.diso_ev_correction,
                now.diso_ev_correction, before.diso_black_delta, now.diso_black_delta);
    ASSERT_EQ(0, before.fpm_status);
    ASSERT_TRUE(now.fpm_status != 0);
    ASSERT_EQ(before.focus_pixel_map_version + 1u, now.focus_pixel_map_version);
    ASSERT_TRUE(provenance.settingsAtDecode != provenance.done.settings);
    ASSERT_EQ(provenance.done.settings, getMlvLlrawprocSettingsFingerprint(video));
    live->fpm_status = before.fpm_status;
    live->focus_pixel_map_version = before.focus_pixel_map_version;
    live->diso_pattern = before.diso_pattern;
    live->diso_ev_correction = before.diso_ev_correction;
    live->diso_black_delta = before.diso_black_delta;
    const uint64_t reverted = getMlvLlrawprocSettingsFingerprint(video);
    live->fpm_status = now.fpm_status;
    live->focus_pixel_map_version = now.focus_pixel_map_version;
    live->diso_pattern = now.diso_pattern;
    live->diso_ev_correction = now.diso_ev_correction;
    live->diso_black_delta = now.diso_black_delta;
    ASSERT_EQ(provenance.settingsAtDecode, reverted);
}

struct ExactnessLeg
{
    const char * label;
    const char * receipt;
    Debayer debayer;
    int cpuCores;
    int dualIsoModeOverride;  // -1 keeps the receipt's
    int expectHqDualIso;      // llrpHQDualIso after setup (1: bit shift 0; 0: bit shift 16 - bpp)
};

void runExactnessSequence(const ExactnessLeg & leg)
{
    KillSwitchGuard guard;
    MlvPipelineFixture reference;
    MlvPipelineFixture reused;
    ASSERT_TRUE(openLargeFixture(reference, leg.receipt, leg.debayer, leg.cpuCores));
    ASSERT_TRUE(openLargeFixture(reused, leg.receipt, leg.debayer, leg.cpuCores));
    if (leg.dualIsoModeOverride >= 0)
    {
        llrpSetDualIsoMode(reference.video(), leg.dualIsoModeOverride);
        llrpSetDualIsoMode(reused.video(), leg.dualIsoModeOverride);
    }
    ASSERT_EQ(leg.expectHqDualIso, llrpHQDualIso(reference.video()) != 0 ? 1 : 0);
    ASSERT_TRUE(llrpGetFixRawMode(reference.video()) != 0);
    ASSERT_EQ(reference.width(), reused.width());
    ASSERT_EQ(reference.height(), reused.height());
    const uint64_t frames = getMlvFrames(reference.video());
    ASSERT_TRUE(frames >= 8);
    const size_t words = pixels(reference) * 3u;

    WorkerState worker;
    Slot slot;
    Debayered16ReconReuseCounters counters;
    for (uint64_t f = 0; f < frames; ++f)
    {
        const llrawprocObject_t before = *reused.video()->llrawproc;
        const std::vector<uint16_t> expected = reference.renderDebayeredFrame16(f);
        const StepResult step = playbackRenderStep(reused, f, &worker.state, slot, true, &counters);
        const size_t mismatched = mismatchedWords(expected, slot.rawImage16, words);
        std::printf("[debayered16-recon-reuse] %s frame %d: reused=%d refusal='%s' mismatched_words=%zu of %zu\n",
                    leg.label, static_cast<int>(f), step.reused ? 1 : 0, step.refusal.c_str(), mismatched, words);
        const llrawprocObject_t & after = *reused.video()->llrawproc;
        std::printf("[debayered16-recon-reuse] %s frame %d fields: fpm_status %d->%d bpm_status %d->%d "
                    "fpm_ver %u->%u bpm_ver %u->%u fpm_count %d->%d bpm_count %d->%d auto_corr %d->%d "
                    "compute_stripes %d->%d\n",
                    leg.label, static_cast<int>(f), before.fpm_status, after.fpm_status, before.bpm_status,
                    after.bpm_status, before.focus_pixel_map_version, after.focus_pixel_map_version,
                    before.bad_pixel_map_version, after.bad_pixel_map_version,
                    static_cast<int>(before.focus_pixel_map.count), static_cast<int>(after.focus_pixel_map.count),
                    static_cast<int>(before.bad_pixel_map.count), static_cast<int>(after.bad_pixel_map.count),
                    before.diso_auto_correction, after.diso_auto_correction, before.compute_stripes,
                    after.compute_stripes);
        // The first llrawproc run on an object loads the focus pixel map status (a
        // fingerprinted field), so the first frame may take today's path for exactly
        // that reason; the app's paused render at clip open does that load before
        // playback. Every later frame is consumed.
        if (f == 0 && !step.reused)
        {
            ASSERT_TRUE(step.outcome == Debayered16ReconRefusal::SettingsChangedDuringRecon);
            assertFirstFrameRefusalIsTheFirstRunLoad(reused.video(), before, slot.provenance, leg.label);
        }
        else
        {
            ASSERT_TRUE(step.reused);
        }
        ASSERT_EQ(static_cast<size_t>(0), mismatched);
        ASSERT_EQ(0, std::memcmp(expected.data(), slot.rawImage16.data(), words * sizeof(uint16_t)));
        if (f == 0 || f + 1 == frames) assertRealPicture(expected.data(), words, leg.label);
    }
    ASSERT_EQ(frames, counters.consumed() + counters.count(Debayered16ReconRefusal::SettingsChangedDuringRecon));
    ASSERT_TRUE(counters.consumed() >= frames - 1);
    ASSERT_EQ(counters.count(Debayered16ReconRefusal::SettingsChangedDuringRecon), counters.ownRecon());
}

// The slot's recon debayered as the consumer would: with an explicit HQ flag.
std::vector<uint16_t> debayerRecon(MlvPipelineFixture & fixture, uint64_t frame, const Slot & slot, bool hqDualIso)
{
    const size_t n = pixels(fixture);
    std::vector<float> scratch(n);
    std::copy_n(slot.rawImage16.data(), n, reinterpret_cast<uint16_t *>(scratch.data()));
    std::vector<uint16_t> out(n * 3u, 0u);
    (void)getMlvRawFrameDebayeredFromReconnedRaw16(fixture.video(), frame, scratch.data(), out.data(), hqDualIso ? 1 : 0);
    return out;
}

// Plays frames 0..k-1, then lets the worker reconstruct frame k with `hooks` around
// the recon, applies `beforeRender` after the recon-done stamp, and renders frame k
// through the production dispatch (with hooks.afterAdmission inside it).
struct TransitionRun
{
    bool opened = false;
    std::vector<uint16_t> frameK;
    std::vector<uint16_t> staleReuseK;     // the recon debayered with its stamped HQ flag: what consuming it shows
    std::vector<uint16_t> liveFlagReuseK;  // r3: the recon debayered with the live HQ flag after admission
    bool liveHqAfterAdmission = false;
    Debayered16ReconProvenance provenanceK;
    StepResult stepK;
    uint64_t consumedBeforeK = 0;
    uint64_t ownReconBeforeK = 0;
    uint64_t consumed = 0;
    uint64_t ownRecon = 0;
    uint64_t outcomeKCount = 0;  // attempts counted under stepK's outcome at frame k (the delta)
};

TransitionRun runSettingsTransition(bool killSwitch, uint64_t k, const SettingsChange & beforeRender,
                                    const FrameHooks & hooks = FrameHooks())
{
    setKillSwitch(killSwitch);
    TransitionRun run;
    MlvPipelineFixture fixture;
    run.opened = openHqFixture(fixture, true, Debayer::Amaze, 4);
    if (!run.opened) return run;
    const size_t n = pixels(fixture);
    WorkerState worker;
    Slot slot;
    Debayered16ReconReuseCounters counters;
    for (uint64_t f = 0; f < k; ++f) (void)playbackRenderStep(fixture, f, &worker.state, slot, true, &counters);
    reconWorkerStep(fixture, k, &worker.state, slot, hooks);
    if (beforeRender) beforeRender(fixture.video());
    run.provenanceK = slot.provenance;
    run.staleReuseK = debayerRecon(fixture, k, slot, slot.provenance.done.hqDualIso);
    SettingsChange afterAdmission;
    if (hooks.afterAdmission)
    {
        afterAdmission = [&](mlvObject_t * video) {
            hooks.afterAdmission(video);
            run.liveHqAfterAdmission = llrpHQDualIso(video) != 0;
            run.liveFlagReuseK = debayerRecon(fixture, k, slot, run.liveHqAfterAdmission);
        };
    }
    run.consumedBeforeK = counters.consumed();
    run.ownReconBeforeK = counters.ownRecon();
    std::array<uint64_t, static_cast<size_t>(Debayered16ReconRefusal::Count)> before{};
    for (size_t i = 0; i < before.size(); ++i) before[i] = counters.byOutcome[i].load();
    run.stepK = renderStep(fixture, k, slot, true, &counters, afterAdmission);
    run.frameK.assign(slot.rawImage16.begin(), slot.rawImage16.begin() + static_cast<std::ptrdiff_t>(n * 3u));
    run.consumed = counters.consumed();
    run.ownRecon = counters.ownRecon();
    run.outcomeKCount = counters.count(run.stepK.outcome) - before[static_cast<size_t>(run.stepK.outcome)];
    setKillSwitch(false);
    return run;
}

// Frame k is refused for `expected`, counted, equals the old path (kill switch, same
// changes) byte-for-byte, and (when staleDiffers) consuming the recon would not have.
void runTransitionCase(const char * label, const SettingsChange & beforeRender, const FrameHooks & hooks = FrameHooks(),
                       Debayered16ReconRefusal expected = Debayered16ReconRefusal::SettingsChangedSinceRecon,
                       bool staleDiffers = true)
{
    KillSwitchGuard guard;
    const uint64_t k = 3;
    const TransitionRun oldPath = runSettingsTransition(true, k, beforeRender, hooks);
    const TransitionRun reuse = runSettingsTransition(false, k, beforeRender, hooks);
    ASSERT_TRUE(oldPath.opened);
    ASSERT_TRUE(reuse.opened);
    const size_t words = oldPath.frameK.size();
    ASSERT_TRUE(words > 0);
    ASSERT_EQ(words, reuse.frameK.size());
    const size_t mismatched = mismatchedWords(oldPath.frameK, reuse.frameK, words);
    const size_t staleMismatched = mismatchedWords(oldPath.frameK, reuse.staleReuseK, words);
    std::printf("[debayered16-recon-reuse] %s: outcome=%s consumed=%llu own_recon=%llu "
                "refused_%s=%llu mismatched_vs_old_path=%zu stale_reuse_mismatched=%zu of %zu\n",
                label, debayered16ReconRefusalName(reuse.stepK.outcome),
                static_cast<unsigned long long>(reuse.consumed), static_cast<unsigned long long>(reuse.ownRecon),
                debayered16ReconRefusalName(expected), static_cast<unsigned long long>(reuse.outcomeKCount),
                mismatched, staleMismatched, words);
    // Refused, counted as a fallback, and the frame is today's path byte-for-byte.
    ASSERT_TRUE(reuse.stepK.outcome == expected);
    ASSERT_FALSE(reuse.stepK.reused);
    ASSERT_TRUE(reuse.consumedBeforeK >= k - 1);  // steady play before the change (see runExactnessSequence)
    ASSERT_EQ(reuse.consumedBeforeK, reuse.consumed);
    ASSERT_EQ(reuse.ownReconBeforeK + 1, reuse.ownRecon);
    ASSERT_EQ(static_cast<uint64_t>(1), reuse.outcomeKCount);
    ASSERT_TRUE(oldPath.stepK.outcome == Debayered16ReconRefusal::KillSwitch);
    ASSERT_EQ(static_cast<size_t>(0), mismatched);
    ASSERT_EQ(0, std::memcmp(oldPath.frameK.data(), reuse.frameK.data(), words * sizeof(uint16_t)));
    assertRealPicture(reuse.frameK.data(), words, label);
    // Sensitivity: consuming the stale recon would have shown a different frame.
    if (staleDiffers) ASSERT_TRUE(staleMismatched > words / 100);
}

// Frame k is consumed (counted as consumed, no second recon) and compared with the old
// path run under the settings the recon ran under (kill switch, no change at all).
struct ConsumedCase
{
    TransitionRun oldPath;
    TransitionRun reuse;
    size_t words = 0;
    size_t mismatched = 0;
};

ConsumedCase runConsumedCase(const char * label, const FrameHooks & hooks)
{
    KillSwitchGuard guard;
    const uint64_t k = 3;
    ConsumedCase c;
    c.oldPath = runSettingsTransition(true, k, nullptr);
    c.reuse = runSettingsTransition(false, k, nullptr, hooks);
    ASSERT_TRUE(c.oldPath.opened);
    ASSERT_TRUE(c.reuse.opened);
    c.words = c.oldPath.frameK.size();
    ASSERT_TRUE(c.words > 0);
    ASSERT_EQ(c.words, c.reuse.frameK.size());
    c.mismatched = mismatchedWords(c.oldPath.frameK, c.reuse.frameK, c.words);
    std::printf("[debayered16-recon-reuse] %s: outcome=%s consumed=%llu own_recon=%llu stamped_hq=%d "
                "mismatched_vs_old_path_at_recon_settings=%zu of %zu\n",
                label, debayered16ReconRefusalName(c.reuse.stepK.outcome),
                static_cast<unsigned long long>(c.reuse.consumed), static_cast<unsigned long long>(c.reuse.ownRecon),
                c.reuse.provenanceK.done.hqDualIso ? 1 : 0, c.mismatched, c.words);
    ASSERT_TRUE(c.oldPath.stepK.outcome == Debayered16ReconRefusal::KillSwitch);
    ASSERT_TRUE(c.reuse.stepK.outcome == Debayered16ReconRefusal::None);
    ASSERT_TRUE(c.reuse.stepK.reused);
    ASSERT_EQ(static_cast<uint64_t>(0), c.reuse.stepK.renderLlrawprocRuns);
    ASSERT_EQ(c.reuse.consumedBeforeK + 1, c.reuse.consumed);
    ASSERT_EQ(c.reuse.ownReconBeforeK, c.reuse.ownRecon);
    return c;
}

} // namespace

// (a) Byte-exact over every frame of the 16-frame HQ dual-ISO fixture, AMaZE (the
// debayer the playback route runs) on the multi-threaded path the render thread uses.
TEST(Debayered16ReconReuse, ReuseIsByteExactOverDualIsoSequenceAmaze)
{
    runExactnessSequence({ "hq amaze x4 threads", kHqReceipt, Debayer::Amaze, 4, -1, 1 });
}

// (a) The same with the bilinear (u16) debayer, single-threaded.
TEST(Debayered16ReconReuse, ReuseIsByteExactOverDualIsoSequenceBilinear)
{
    runExactnessSequence({ "hq bilinear x1 thread", kHqReceipt, Debayer::Bilinear, 1, -1, 1 });
}

// (a) r2 (fable H3): the non-HQ branch, bit shift 16 - bpp, with preview dual-ISO
// (mode 2, the worker-carried pattern) from its own receipt.
TEST(Debayered16ReconReuse, ReuseIsByteExactOverPreviewDualIsoSequence)
{
    runExactnessSequence({ "preview dual-iso amaze x4", kPreviewReceipt, Debayer::Amaze, 4, -1, 0 });
}

// (a) r2 (fable H3): dual-ISO processing off with the rest of llrawproc on, the path a
// non-dual-ISO clip takes (the tracked fixtures hold no non-dual-ISO clip).
TEST(Debayered16ReconReuse, ReuseIsByteExactWithDualIsoOff)
{
    runExactnessSequence({ "dual-iso off amaze x4", kHqReceipt, Debayer::Amaze, 4, 0, 0 });
}

// (b) The render consumes the recon and runs no decode and no llrawproc of its own
// (before this card it ran both: one full llrawproc per frame on the render
// thread). Mutation: forcing today's path makes exactly this check fail.
TEST(Debayered16ReconReuse, RenderRunsNoSecondReconAndForcingOldPathFailsIt)
{
    KillSwitchGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, false, Debayer::Amaze, 1));
    WorkerState worker;
    Slot slot;
    // The paused render at clip open, as in the app (see runExactnessSequence); its
    // single-frame cache is dropped so the forced legs below really re-render.
    (void)fixture.renderDebayeredFrame16(0);
    resetMlvCachedFrame(fixture.video());
    for (const uint64_t frame : { 0u, 1u })
    {
        const StepResult step = playbackRenderStep(fixture, frame, &worker.state, slot);
        std::printf("[debayered16-recon-reuse] reuse frame %d: llrawproc_runs=%llu llrawproc_total_ms=%.3f raw_uint16_ms=%.3f\n",
                    static_cast<int>(frame), static_cast<unsigned long long>(step.renderLlrawprocRuns),
                    step.renderLlrawprocTotalMs, step.renderRawUint16Ms);
        ASSERT_TRUE(renderRanNoSecondRecon(step));
    }

    setKillSwitch(true);
    for (const uint64_t frame : { 0u, 1u })
    {
        const StepResult step = playbackRenderStep(fixture, frame, &worker.state, slot);
        std::printf("[debayered16-recon-reuse] forced old path frame %d: refusal='%s' llrawproc_runs=%llu llrawproc_total_ms=%.3f\n",
                    static_cast<int>(frame), step.refusal.c_str(),
                    static_cast<unsigned long long>(step.renderLlrawprocRuns), step.renderLlrawprocTotalMs);
        ASSERT_FALSE(step.reused);
        ASSERT_TRUE(step.renderLlrawprocRuns >= 1);
        ASSERT_FALSE(renderRanNoSecondRecon(step));
    }
}

// (c) The reuse leaves the single-frame cache (rgb_raw_current_frame /
// current_cached_frame, read by export, the WB picker and Look Assist's WB solve)
// exactly as the last paused render left it; afterwards a paused render and the export
// path give the same bytes as a fresh object that never played.
TEST(Debayered16ReconReuse, ReuseLeavesFrameCacheAndPausedAndExportUntouched)
{
    KillSwitchGuard guard;
    MlvPipelineFixture played;
    ASSERT_TRUE(openHqFixture(played, false, Debayer::Amaze, 1));
    const size_t words = pixels(played) * 3u;
    const std::vector<uint16_t> pausedFrame0 = played.renderDebayeredFrame16(0);
    ASSERT_EQ(1, played.video()->current_cached_frame_active);
    ASSERT_EQ(static_cast<uint64_t>(0), static_cast<uint64_t>(played.video()->current_cached_frame));
    const std::vector<uint16_t> cachedBefore(played.video()->rgb_raw_current_frame,
                                             played.video()->rgb_raw_current_frame + words);

    WorkerState worker;
    Slot slot;
    for (const uint64_t frame : { 0u, 1u })
    {
        ASSERT_TRUE(playbackRenderStep(played, frame, &worker.state, slot).reused);
    }
    ASSERT_EQ(1, played.video()->current_cached_frame_active);
    ASSERT_EQ(static_cast<uint64_t>(0), static_cast<uint64_t>(played.video()->current_cached_frame));
    ASSERT_EQ(0, std::memcmp(cachedBefore.data(), played.video()->rgb_raw_current_frame, words * sizeof(uint16_t)));

    // Paused / scrubbed frames take today's path (the policy refuses them).
    const StepResult paused = playbackRenderStep(played, 1, &worker.state, slot, /*playing=*/false);
    ASSERT_FALSE(paused.reused);
    ASSERT_TRUE(paused.refusal.find("not playing") != std::string::npos);
    const std::vector<uint16_t> playedPaused1 = played.renderDebayeredFrame16(1);
    const std::vector<uint16_t> playedExport16 = played.renderFrame16(1);
    const std::vector<uint8_t> playedExport8 = played.renderFrame8(1);

    MlvPipelineFixture fresh;
    ASSERT_TRUE(openHqFixture(fresh, false, Debayer::Amaze, 1));
    ASSERT_TRUE(fresh.renderDebayeredFrame16(0) == pausedFrame0);
    const std::vector<uint16_t> freshPaused1 = fresh.renderDebayeredFrame16(1);
    const std::vector<uint16_t> freshExport16 = fresh.renderFrame16(1);
    const std::vector<uint8_t> freshExport8 = fresh.renderFrame8(1);

    ASSERT_TRUE(playedPaused1 == freshPaused1);
    ASSERT_TRUE(playedExport16 == freshExport16);
    ASSERT_TRUE(playedExport8 == freshExport8);
}

// (d) When the AMaZE frame cache could serve the frame, today's path may copy from it,
// so the reconned-raw entry declines and writes nothing.
TEST(Debayered16ReconReuse, FrameCacheThatMayServeMakesEntryDecline)
{
    KillSwitchGuard guard;
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, false, Debayer::Amaze, 1));
    const size_t n = pixels(fixture);
    ASSERT_EQ(0, mlvRawDebayerCacheMayServeFrame(fixture.video(), 1));

    setMlvRawCacheLimitMegaBytes(fixture.video(), 512);
    mlvCacheSetStop(fixture.video(), 0);
    ASSERT_TRUE(getMlvRawCacheLimitFrames(fixture.video()) > 0);
    ASSERT_EQ(1, mlvRawDebayerCacheMayServeFrame(fixture.video(), 1));

    std::vector<float> scratch(n, 0.0f);
    std::vector<uint16_t> out(n * 3u, 0xABCDu);
    ASSERT_EQ(0, getMlvRawFrameDebayeredFromReconnedRaw16(fixture.video(), 1, scratch.data(), out.data(), 1));
    ASSERT_TRUE(std::all_of(out.begin(), out.end(), [](uint16_t v) { return v == 0xABCDu; }));

    mlvCacheSetStop(fixture.video(), 1);
    disableMlvCaching(fixture.video());
    ASSERT_EQ(0, mlvRawDebayerCacheMayServeFrame(fixture.video(), 1));
}

// (e) The eligibility policy: full-resolution playback frames the recon worker
// reconstructed from a successful decode on the CPU recon, CPU debayer, no GPU recon
// texture route, no frame cache, matching frame identity, unchanged llrawproc settings.
// Everything else keeps today's path with a named reason.
TEST(Debayered16ReconReuse, PolicyAdmitsOnlyFullResPlaybackRecon)
{
    KillSwitchGuard guard;
    Debayered16ReconReuseInputs ok;
    ok.consumeReconnedRaw = true;
    ok.reconAcquisitionSucceeded = true;
    ok.playbackActive = true;
    ok.reducedReconScale = 1;
    ok.reconFrameNumber = ok.renderFrameNumber = 42;
    ok.reconRequestSerial = ok.renderRequestSerial = 9;
    ok.reconSettingsAtDecode = ok.reconSettingsAtReconDone = ok.renderSettings = 0x1234u;
    ok.reconBufferComplete = true;
    ASSERT_TRUE(Debayered16ReconReusePolicy::ineligibleReason(ok) == nullptr);
    ASSERT_TRUE(Debayered16ReconReusePolicy::refusal(ok) == Debayered16ReconRefusal::None);

    const auto why = [](Debayered16ReconReuseInputs in) { return Debayered16ReconReusePolicy::refusal(in); };
    Debayered16ReconReuseInputs in = ok; in.consumeReconnedRaw = false;
    ASSERT_TRUE(why(in) == Debayered16ReconRefusal::NotReconned);
    in = ok; in.reconAcquisitionSucceeded = false; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::AcquisitionFailed);
    in = ok; in.playbackActive = false; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::NotPlaying);
    in = ok; in.useGpuAmazeDebayer = true; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::GpuAmazeDebayer);
    in = ok; in.useGpuBilinearDebayer = true; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::GpuBilinearDebayer);
    in = ok; in.gpuPlaybackReconTexturePresentRequested = true;
    ASSERT_TRUE(why(in) == Debayered16ReconRefusal::GpuReconTextureRoute);
    in = ok; in.reconUsedGpuPlaybackRecon = true; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::GpuPlaybackRecon);
    in = ok; in.frameCacheMayServe = true; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::FrameCacheMayServe);
    in = ok; in.reducedReconScale = 2; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::ReducedScale);
    in = ok; in.reducedReconScale = 4; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::ReducedScale);
    in = ok; in.reconFrameNumber = 41; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::FrameIdentity);
    in = ok; in.reconRequestSerial = 8; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::FrameIdentity);
    in = ok; in.reconSettingsAtDecode = 0x9999u;
    ASSERT_TRUE(why(in) == Debayered16ReconRefusal::SettingsChangedDuringRecon);
    in = ok; in.renderSettings = 0x9999u; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::SettingsChangedSinceRecon);
    in = ok; in.reconBufferComplete = false; ASSERT_TRUE(why(in) == Debayered16ReconRefusal::BufferIncomplete);
    for (int i = 0; i < static_cast<int>(Debayered16ReconRefusal::Count); ++i)
    {
        const std::string name = debayered16ReconRefusalName(static_cast<Debayered16ReconRefusal>(i));
        ASSERT_TRUE(name != "unknown");
    }

    setKillSwitch(true);
    ASSERT_TRUE(why(ok) == Debayered16ReconRefusal::KillSwitch);
    DEBAYERED16_REUSE_TEST_SETENV(MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV, "0");
    ASSERT_TRUE(why(ok) == Debayered16ReconRefusal::None);
}

// (e) The settings fingerprint moves with every setting the r2 blockers name and (r3,
// fable r2 H1) with the dual-ISO pattern, EV correction and black delta, which are recon
// inputs, but not with the DNG levels recon publishes.
TEST(Debayered16ReconReuse, SettingsFingerprintTracksRawFixDualIsoAndChromaSmooth)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openHqFixture(fixture, true, Debayer::Amaze, 1));
    mlvObject_t * video = fixture.video();
    const uint64_t base = getMlvLlrawprocSettingsFingerprint(video);
    ASSERT_EQ(base, getMlvLlrawprocSettingsFingerprint(video));

    llrpSetFixRawMode(video, 0);
    ASSERT_TRUE(getMlvLlrawprocSettingsFingerprint(video) != base);
    llrpSetFixRawMode(video, 1);
    ASSERT_EQ(base, getMlvLlrawprocSettingsFingerprint(video));

    llrpSetDualIsoMode(video, 2);
    ASSERT_TRUE(getMlvLlrawprocSettingsFingerprint(video) != base);
    llrpSetDualIsoMode(video, 1);
    ASSERT_EQ(base, getMlvLlrawprocSettingsFingerprint(video));

    const int chroma = video->llrawproc->chroma_smooth;
    llrpSetChromaSmoothMode(video, chroma == 3 ? 5 : 3);
    ASSERT_TRUE(getMlvLlrawprocSettingsFingerprint(video) != base);
    llrpSetChromaSmoothMode(video, chroma);
    ASSERT_EQ(base, getMlvLlrawprocSettingsFingerprint(video));

    // Manual dual-ISO exposure matching and pattern (what the GUI handlers write).
    llrawprocObject_t * llrawproc = video->llrawproc;
    const int pattern = llrawproc->diso_pattern;
    const double ev = llrawproc->diso_ev_correction;
    const int blackDelta = llrawproc->diso_black_delta;
    llrawproc->diso_pattern = pattern == -4 ? 3 : -4;
    ASSERT_TRUE(getMlvLlrawprocSettingsFingerprint(video) != base);
    llrawproc->diso_pattern = pattern;
    llrawproc->diso_ev_correction = ev == -2.5 ? -3.0 : -2.5;
    ASSERT_TRUE(getMlvLlrawprocSettingsFingerprint(video) != base);
    llrawproc->diso_ev_correction = ev;
    llrawproc->diso_black_delta = blackDelta == 7 ? 8 : 7;
    ASSERT_TRUE(getMlvLlrawprocSettingsFingerprint(video) != base);
    llrawproc->diso_black_delta = blackDelta;
    ASSERT_EQ(base, getMlvLlrawprocSettingsFingerprint(video));

    // The DNG levels recon publishes do not move it.
    llrawproc->dng_white_level = 12345;
    llrawproc->dng_black_level = 321;
    ASSERT_EQ(base, getMlvLlrawprocSettingsFingerprint(video));
}

// (f) r2 (sol B1): HQ dual-ISO recon completed, then Raw Fix unchecked before the render
// consumes it (the handler sets fix_raw before its cache reset and idle wait). The
// stale HQ recon would be shifted by 16 - bpp bits; the policy refuses it instead.
TEST(Debayered16ReconReuse, RawFixOffAfterReconIsRefusedAndMatchesOldPath)
{
    runTransitionCase("raw fix off after recon", [](mlvObject_t * video) { llrpSetFixRawMode(video, 0); });
}

// (f) r2 (fable DEBAYERED16-REUSE-SETTINGS-GENERATION-1): dual-ISO HQ -> preview mode
// between the recon and its consumption.
TEST(Debayered16ReconReuse, DualIsoModeChangeAfterReconIsRefusedAndMatchesOldPath)
{
    runTransitionCase("dual-iso hq->preview after recon", [](mlvObject_t * video) { llrpSetDualIsoMode(video, 2); });
}

// (g) r2 (sol B2): the recon worker reuses a slot that held an earlier frame, and this
// frame's indexed LJ92 payload is truncated to 16 bytes (a damaged frame: lj92_open
// fails on its header before writing a pixel), so the slot keeps the earlier bytes and
// the worker still reconstructs them. The render refuses (acquisition failed, counted)
// and today's path zero-fills the frame, byte-for-byte what the kill switch gives;
// consuming the retained bytes would not have been zero. The damage is made in the
// object's in-memory frame index; the tracked fixture file is only read.
TEST(Debayered16ReconReuse, FailedWorkerDecodeIsRefusedAndZeroFilledLikeOldPath)
{
    KillSwitchGuard guard;
    const uint64_t damaged = 3;
    struct Run
    {
        bool opened = false;
        std::vector<uint16_t> frame;
        std::vector<uint16_t> retainedReuse;  // the retained bytes' recon, debayered
        StepResult step;
        bool acquisitionSucceeded = true;
        uint64_t consumedBefore = 0;
        uint64_t ownReconBefore = 0;
        uint64_t consumed = 0;
        uint64_t ownRecon = 0;
        uint64_t refusedAcquisition = 0;
    };
    const auto play = [&](bool killSwitch) {
        setKillSwitch(killSwitch);
        Run run;
        MlvPipelineFixture fixture;
        run.opened = openHqFixture(fixture, true, Debayer::Amaze, 4)
            && (fixture.video()->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92) != 0
            && getMlvFrames(fixture.video()) > damaged + 1;
        if (!run.opened) return run;
        // Damaged before anything is read, so no read-ahead holds a good copy of it.
        fixture.video()->video_index[damaged].frame_size = 16;
        const size_t n = pixels(fixture);
        WorkerState worker;
        Slot slot;  // one slot reused for every frame, like a FrameSlot in steady play
        Debayered16ReconReuseCounters counters;
        for (uint64_t f = 0; f < damaged; ++f) (void)playbackRenderStep(fixture, f, &worker.state, slot, true, &counters);
        reconWorkerStep(fixture, damaged, &worker.state, slot);
        run.acquisitionSucceeded = slot.provenance.acquisitionSucceeded;
        run.retainedReuse = debayerRecon(fixture, damaged, slot, slot.provenance.done.hqDualIso);
        run.consumedBefore = counters.consumed();
        run.ownReconBefore = counters.ownRecon();
        run.step = renderStep(fixture, damaged, slot, true, &counters);
        run.frame.assign(slot.rawImage16.begin(), slot.rawImage16.begin() + static_cast<std::ptrdiff_t>(n * 3u));
        run.consumed = counters.consumed();
        run.ownRecon = counters.ownRecon();
        run.refusedAcquisition = counters.count(Debayered16ReconRefusal::AcquisitionFailed);
        setKillSwitch(false);
        return run;
    };
    const Run oldPath = play(true);
    const Run reuse = play(false);
    ASSERT_TRUE(oldPath.opened);
    ASSERT_TRUE(reuse.opened);
    const size_t words = reuse.frame.size();
    const size_t nonzero = nonzeroWords(reuse.frame);
    const size_t retainedNonzero = nonzeroWords(reuse.retainedReuse);
    std::printf("[debayered16-recon-reuse] damaged lj92 frame %d: acquisition_succeeded=%d outcome=%s consumed=%llu "
                "refused_acquisition_failed=%llu nonzero=%zu retained_reuse_nonzero=%zu of %zu\n",
                static_cast<int>(damaged), reuse.acquisitionSucceeded ? 1 : 0,
                debayered16ReconRefusalName(reuse.step.outcome), static_cast<unsigned long long>(reuse.consumed),
                static_cast<unsigned long long>(reuse.refusedAcquisition), nonzero, retainedNonzero, words);
    ASSERT_FALSE(reuse.acquisitionSucceeded);
    ASSERT_FALSE(oldPath.acquisitionSucceeded);
    ASSERT_TRUE(reuse.step.outcome == Debayered16ReconRefusal::AcquisitionFailed);
    ASSERT_FALSE(reuse.step.reused);
    ASSERT_TRUE(reuse.consumedBefore >= damaged - 1);  // steady play before the damaged frame
    ASSERT_EQ(reuse.consumedBefore, reuse.consumed);
    ASSERT_EQ(reuse.ownReconBefore + 1, reuse.ownRecon);
    ASSERT_EQ(static_cast<uint64_t>(1), reuse.refusedAcquisition);
    // Today's failure handling: the whole frame zero-filled, on both runs.
    ASSERT_TRUE(words > 0);
    ASSERT_EQ(static_cast<size_t>(0), nonzero);
    ASSERT_EQ(oldPath.frame.size(), words);
    ASSERT_EQ(0, std::memcmp(oldPath.frame.data(), reuse.frame.data(), words * sizeof(uint16_t)));
    // Sensitivity: the retained bytes debayer to a real, nonzero frame.
    ASSERT_TRUE(retainedNonzero > words / 2);
}

// (h) r3 (sol r2 B1): Raw Fix unchecked AFTER the policy admitted the recon and before
// the reconned debayer (the handler sets fix_raw before its idle wait). The bit shift
// comes from the HQ state the recon ran under (the recon-done stamp), never the live
// object, so the frame is the old path run under the recon's settings byte-for-byte and
// is counted as consumed. Mutation M6 (the consumer reads the live HQ flag again) shifts
// the HQ recon by 16 - bpp bits and fails here.
TEST(Debayered16ReconReuse, RawFixOffAfterAdmissionUsesReconTimeHqState)
{
    FrameHooks hooks;
    hooks.afterAdmission = [](mlvObject_t * video) { llrpSetFixRawMode(video, 0); };
    const ConsumedCase c = runConsumedCase("raw fix off after admission", hooks);
    const size_t liveMismatched = mismatchedWords(c.oldPath.frameK, c.reuse.liveFlagReuseK, c.words);
    std::printf("[debayered16-recon-reuse] raw fix off after admission: live_hq=%d live_flag_mismatched=%zu of %zu\n",
                c.reuse.liveHqAfterAdmission ? 1 : 0, liveMismatched, c.words);
    ASSERT_TRUE(c.reuse.provenanceK.done.hqDualIso);
    ASSERT_FALSE(c.reuse.liveHqAfterAdmission);  // the change landed inside the window
    ASSERT_EQ(static_cast<size_t>(0), c.mismatched);
    ASSERT_EQ(0, std::memcmp(c.oldPath.frameK.data(), c.reuse.frameK.data(), c.words * sizeof(uint16_t)));
    assertRealPicture(c.reuse.frameK.data(), c.words, "raw fix off after admission");
    // Sensitivity: the live flag would have shifted the HQ recon.
    ASSERT_TRUE(liveMismatched > c.words / 100);
}

// (i) r3 (sol r2 H4): what the decode and recon-done stamps catch. Settings changed
// away and back BEFORE the recon reads them: the recon ran under the original settings,
// the stamps agree, and consuming it is byte-exact (there is nothing to catch).
TEST(Debayered16ReconReuse, SettingsAwayAndBackBeforeReconIsConsumedByteExact)
{
    FrameHooks hooks;
    hooks.beforeRecon = [](mlvObject_t * video) {
        llrpSetFixRawMode(video, 0);
        llrpSetFixRawMode(video, 1);
    };
    const ConsumedCase c = runConsumedCase("raw fix away and back before recon", hooks);
    ASSERT_EQ(c.reuse.provenanceK.settingsAtDecode, c.reuse.provenanceK.done.settings);
    ASSERT_EQ(static_cast<size_t>(0), c.mismatched);
    ASSERT_EQ(0, std::memcmp(c.oldPath.frameK.data(), c.reuse.frameK.data(), c.words * sizeof(uint16_t)));
}

// (i) A change during the recon that is still in place at the recon-done stamp: the
// stamps differ, the frame is refused as changed during the recon and equals the old
// path; the HQ recon under the new (non-HQ) bit shift would not have.
TEST(Debayered16ReconReuse, SettingsChangedDuringReconIsRefusedAndMatchesOldPath)
{
    FrameHooks hooks;
    hooks.afterRecon = [](mlvObject_t * video) { llrpSetFixRawMode(video, 0); };
    runTransitionCase("raw fix off during recon", nullptr, hooks, Debayered16ReconRefusal::SettingsChangedDuringRecon);
}

// (i) The documented limit: a change made before the recon reads the settings and undone
// before the recon-done stamp (A -> B -> A spanning the recon). Both stamps read A, so the
// recon made under B is admitted and consumed; the two stamps cannot see it. The old path
// has the same exposure (its own llrawproc run reads the settings once, its bit shift
// later). Pinned so a change to this behaviour is a visible decision.
TEST(Debayered16ReconReuse, SettingsAwayAndBackSpanningReconIsTheDocumentedLimit)
{
    FrameHooks hooks;
    hooks.beforeRecon = [](mlvObject_t * video) { llrpSetFixRawMode(video, 0); };
    hooks.afterRecon = [](mlvObject_t * video) { llrpSetFixRawMode(video, 1); };
    const ConsumedCase c = runConsumedCase("raw fix away and back spanning recon (limit)", hooks);
    ASSERT_EQ(c.reuse.provenanceK.settingsAtDecode, c.reuse.provenanceK.done.settings);
    ASSERT_TRUE(c.mismatched > 0);
}

// (j) r3 (DEBAYERED16-REUSE-GPU-RECON-GATE-TEST-1, sol r2 H2 / fable r2 H3): the CUDA
// recon provenance travels from the recon thread's stamp (debayered16ReconDoneStamp,
// which signalReconDoneFromWorker calls) through the slot to the policy inputs
// (applyDebayered16ReconProvenance, which drawFrame calls), and the frame is refused,
// counted, and equals the old path. No CUDA device is needed: after the CPU recon the
// test sets the thread's "last llrawproc run used the CUDA playback recon" flag, which a
// real CUDA recon sets. Mutation M5 (the handoff dropped at either end) consumes the
// frame and fails here; the call sites in RenderFrameThread.cpp are pinned by the
// console test Debayered16ReconProvenanceWiring.
TEST(Debayered16ReconReuse, CudaReconProvenanceIsCarriedToThePolicyAndRefused)
{
    FrameHooks hooks;
    hooks.afterRecon = [](mlvObject_t *) { llrpSetGpuPlaybackReconLastUsedForTesting(1); };
    // The consumed recon is the correct CPU recon, so it would not differ: staleDiffers=false.
    runTransitionCase("cuda recon provenance", nullptr, hooks, Debayered16ReconRefusal::GpuPlaybackRecon, false);
    llrpSetGpuPlaybackReconLastUsedForTesting(0);

    // Control: the same frame without the flag is consumed.
    const ConsumedCase c = runConsumedCase("cuda recon provenance control", FrameHooks());
    ASSERT_FALSE(c.reuse.provenanceK.done.usedGpuPlaybackRecon);
    ASSERT_EQ(static_cast<size_t>(0), c.mismatched);
}
