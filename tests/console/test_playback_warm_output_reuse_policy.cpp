// PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-WARM-OUTPUT-REUSE-1 R2/R3: Play's first recon present keeps the paused warm's
// AMaZE output only when preGate() passes on every non-content AMaZE input and the exact device compare of the input
// bytes says equal. The collision cases come first (fleet TRAPS.md:26538-26545: key a reuse cache on structured
// identity, never on presentation-only fields, and write the collision tests before review): WB one ulp apart, a frame
// off by one, the dims swapped, each must NOT reuse. Then one case per skip reason, the compare outcomes, and the line
// formats.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackWarmOutputReusePolicy.h"
#include "../../platform/qt/PlaybackStartupSplit.h"

#include <cmath>
#include <functional>
#include <string>

using playback_warm_output_reuse::Key;
using playback_warm_output_reuse::Reason;
using playback_warm_output_reuse::afterCompare;
using playback_warm_output_reuse::preGate;
using playback_warm_output_reuse::reasonName;
using playback_warm_output_reuse::reuse;

namespace {

// PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 F-key: the recon's per-run constants (the M16-1243 playback shape)
playback_warm_output_reuse::PreparedState preparedState()
{
    playback_warm_output_reuse::PreparedState s;
    s.valid = true;
    s.whiteDarkened = 8191;
    s.blackDelta = 23;
    s.evCorrection = -3.0000000000000004;
    s.darkNoise = 4.5;
    s.interpMethod = 1;
    s.useAliasMap = 0;
    s.useFullres = 1;
    s.chromaSmoothMethod = 1;
    s.applyDither = 1;
    s.isBright[0] = 1;
    s.isBright[1] = 1;
    s.isBright[2] = 0;
    s.isBright[3] = 0;
    s.frameId = 1243;
    return s;
}

Key warmKey()
{
    Key k;
    k.valid = true;
    k.frame = 1243;
    k.width = 1808;
    k.height = 2268;
    k.blackLevel = 2047;
    k.wb[0] = 2.0176366843033511;
    k.wb[1] = 1.0;
    k.wb[2] = 1.5503875968992249;
    k.reducedHnyquist = false;
    k.setClipCountAfterWarm = 3;
    k.setLutsCountAfterWarm = 5;
    k.setPatternCountAfterWarm = 11;
    k.captureId = 7;
    k.state = preparedState();
    return k;
}

// the present's own AMaZE inputs: equal to the warm's (its counts and id are not read from it)
Key presentKey()
{
    Key k = warmKey();
    k.valid = false;
    k.setClipCountAfterWarm = 0;
    k.setLutsCountAfterWarm = 0;
    k.setPatternCountAfterWarm = 0;
    k.captureId = 0;
    return k;
}

// enabled, adopted, not suppressed, not a validation probe, no set_clip / set_luts / set_iso_pattern since the warm
Reason gate(const Key &warm, const Key &present)
{
    return preGate(true, true, warm, present, false, false, warm.setClipCountAfterWarm, warm.setLutsCountAfterWarm,
                   warm.setPatternCountAfterWarm);
}

std::string name(Reason reason) { return std::string(reasonName(reason)); }

} // namespace

// ---- collision cases first ----

TEST(PlaybackWarmOutputReusePolicy, WbOneUlpApartIsNeverReused)
{
    for (int channel = 0; channel < 3; ++channel)
    {
        for (const double toward : {INFINITY, -INFINITY})
        {
            Key present = presentKey();
            present.wb[channel] = std::nextafter(present.wb[channel], toward);
            ASSERT_TRUE(present.wb[channel] != warmKey().wb[channel]);
            const Reason r = gate(warmKey(), present);
            ASSERT_EQ(std::string("wb_mismatch"), name(r));
            ASSERT_FALSE(reuse(r));
            // even a compare that says equal never turns a refused gate into a hit: the caller compares only on None
            ASSERT_TRUE(r != Reason::None);
        }
    }
}

TEST(PlaybackWarmOutputReusePolicy, AFrameOffByOneIsNeverReused)
{
    for (const int delta : {-1, 1})
    {
        Key present = presentKey();
        present.frame = static_cast<uint64_t>(static_cast<long long>(present.frame) + delta);
        const Reason r = gate(warmKey(), present);
        ASSERT_EQ(std::string("frame_mismatch"), name(r));
        ASSERT_FALSE(reuse(r));
    }
    // the unarmed frame id (UINT64_MAX, a compare-and-reject mismatch) never equals a warm frame
    Key present = presentKey();
    present.frame = UINT64_MAX;
    ASSERT_EQ(std::string("frame_mismatch"), name(gate(warmKey(), present)));
}

TEST(PlaybackWarmOutputReusePolicy, DimsSwappedOrOffAreNeverReused)
{
    Key present = presentKey();
    present.width = warmKey().height;
    present.height = warmKey().width;
    ASSERT_EQ(std::string("size_mismatch"), name(gate(warmKey(), present)));
    // same pixel count, other shape
    Key half = presentKey();
    half.width = warmKey().width * 2;
    half.height = warmKey().height / 2;
    ASSERT_EQ(warmKey().width * warmKey().height, half.width * half.height);
    ASSERT_EQ(std::string("size_mismatch"), name(gate(warmKey(), half)));
    Key oneRow = presentKey();
    oneRow.height -= 1;
    ASSERT_EQ(std::string("size_mismatch"), name(gate(warmKey(), oneRow)));
}

TEST(PlaybackWarmOutputReusePolicy, BlackAndHnyquistCollisionsAreNeverReused)
{
    Key black = presentKey();
    black.blackLevel += 1;
    ASSERT_EQ(std::string("black_mismatch"), name(gate(warmKey(), black)));
    Key hny = presentKey();
    hny.reducedHnyquist = true;
    ASSERT_EQ(std::string("hnyquist_mismatch"), name(gate(warmKey(), hny)));
}

// ---- every skip reason ----

TEST(PlaybackWarmOutputReusePolicy, OnlyAnAllMatchingGateGoesToTheCompare)
{
    const Reason r = gate(warmKey(), presentKey());
    ASSERT_TRUE(Reason::None == r);
    ASSERT_EQ(std::string("none"), name(r));
    ASSERT_FALSE(reuse(r));   // None is "go compare", never a reuse by itself
}

TEST(PlaybackWarmOutputReusePolicy, EachSkipReasonRefusesBeforeAnyCompare)
{
    const Key w = warmKey();
    const Key p = presentKey();
    const uint64_t c = w.setClipCountAfterWarm;
    const uint64_t l = w.setLutsCountAfterWarm;
    const uint64_t t = w.setPatternCountAfterWarm;
    ASSERT_EQ(std::string("disabled"), name(preGate(false, true, w, p, false, false, c, l, t)));
    ASSERT_EQ(std::string("not_adopted"), name(preGate(true, false, w, p, false, false, c, l, t)));
    Key invalid = w;
    invalid.valid = false;
    ASSERT_EQ(std::string("no_warm_key"), name(preGate(true, true, invalid, p, false, false, c, l, t)));
    Key noCapture = w;
    noCapture.captureId = 0;
    ASSERT_EQ(std::string("no_warm_key"), name(preGate(true, true, noCapture, p, false, false, c, l, t)));
    ASSERT_EQ(std::string("validation_probe"), name(preGate(true, true, w, p, false, true, c, l, t)));
    ASSERT_EQ(std::string("cuda_suppressed"), name(preGate(true, true, w, p, true, false, c, l, t)));
    ASSERT_EQ(std::string("recon_configured_since_warm"), name(preGate(true, true, w, p, false, false, c + 1, l, t)));
    ASSERT_EQ(std::string("recon_configured_since_warm"), name(preGate(true, true, w, p, false, false, c, l + 1, t)));
    // adversary r17 A1: a pattern-only configure (set_iso_pattern) advances neither of the two counts above
    ASSERT_EQ(std::string("recon_configured_since_warm"), name(preGate(true, true, w, p, false, false, c, l, t + 1)));
    // a default (never set) key, as the window holds before any warm
    ASSERT_EQ(std::string("no_warm_key"), name(preGate(true, true, Key(), p, false, false, 0, 0, 0)));
    for (const Reason r : {Reason::Disabled, Reason::NotAdopted, Reason::NoWarmKey, Reason::ValidationProbe,
                           Reason::CudaSuppressed, Reason::FrameMismatch, Reason::SizeMismatch, Reason::BlackMismatch,
                           Reason::WbMismatch, Reason::HnyquistMismatch, Reason::ReconConfiguredSinceWarm})
        ASSERT_FALSE(reuse(r));
}

TEST(PlaybackWarmOutputReusePolicy, TheKillSwitchAndAdoptionOutrankEveryOtherField)
{
    Key p = presentKey();
    p.frame += 1;
    p.wb[0] += 1.0;
    // disabled first, then not adopted, whatever else differs
    ASSERT_EQ(std::string("disabled"), name(preGate(false, false, Key(), p, true, true, 9, 9, 9)));
    ASSERT_EQ(std::string("not_adopted"), name(preGate(true, false, warmKey(), p, true, true, 9, 9, 9)));
}

// adversary r17 A1: the pattern-only configure (llrpGpuPlaybackReconSetIsoPatternCount) is a recon configure like the clip
// and LUT ones. Warm frame 0 with pattern 1100, finish frame 1 with 0011 and identical geometry, levels and LUTs: only the
// pattern count moved, and the warm output must not be compared (let alone reused) as if nothing had changed.
TEST(PlaybackWarmOutputReusePolicy, APatternOnlyConfigureSinceTheWarmRefusesTheCompare)
{
    const Key w = warmKey();
    const Key p = presentKey();
    ASSERT_EQ(std::string("none"), name(gate(w, p)));
    ASSERT_EQ(std::string("recon_configured_since_warm"),
              name(preGate(true, true, w, p, false, false, w.setClipCountAfterWarm, w.setLutsCountAfterWarm,
                           w.setPatternCountAfterWarm + 1)));
    // each of the three counts alone refuses; none of them moving does not
    ASSERT_EQ(std::string("recon_configured_since_warm"),
              name(preGate(true, true, w, p, false, false, w.setClipCountAfterWarm + 1, w.setLutsCountAfterWarm,
                           w.setPatternCountAfterWarm)));
    ASSERT_EQ(std::string("recon_configured_since_warm"),
              name(preGate(true, true, w, p, false, false, w.setClipCountAfterWarm, w.setLutsCountAfterWarm + 1,
                           w.setPatternCountAfterWarm)));
}

// sol r17 #2: the present's decision (decide(), GpuDisplayWindow's gate and compare) with the interleaving injected, no
// threads. The process-wide counts live in `live`; readLiveCounts() is the present reading them; the compare callback is
// the window in which a lookahead frame's configure can land (llrawproc.c runs on the recon worker while the GUI thread
// presents).
namespace {

struct InjectedPresent
{
    playback_warm_output_reuse::LiveCounts live;
    int reads = 0;
    int compares = 0;
    bool equal = true;                                   // what the exact compare reports
    std::function<void()> duringCompare;                 // a configure landing after the gate's read
    // adversary r18 A1: the three counters are read one after another (GpuDisplayWindow's readLiveConfigureCounts);
    // midRead(read, field) runs after field 0 (clip), 1 (luts) or 2 (pattern) of read number `read` was copied
    std::function<void(int, int)> midRead;

    playback_warm_output_reuse::LiveCounts readSequentially()
    {
        ++reads;
        playback_warm_output_reuse::LiveCounts now;
        now.setClip = live.setClip;
        if (midRead) midRead(reads, 0);
        now.setLuts = live.setLuts;
        if (midRead) midRead(reads, 1);
        now.setPattern = live.setPattern;
        if (midRead) midRead(reads, 2);
        now.generation = live.generation;   // r22 adversary A1: loaded last, as GpuDisplayWindow does
        return now;
    }

    Reason run(const Key &warm, const Key &present, bool *compareAttempted)
    {
        return playback_warm_output_reuse::decide(
            true, true, warm, present, false, false,
            [this]() { return readSequentially(); },
            [this](bool *sameInput, QString *why) {
                ++compares;
                if (duringCompare) duringCompare();
                *sameInput = equal;
                why->clear();
                return true;
            },
            compareAttempted);
    }
};

InjectedPresent atWarmCounts(const Key &w)
{
    InjectedPresent p;
    p.live.setClip = w.setClipCountAfterWarm;
    p.live.setLuts = w.setLutsCountAfterWarm;
    p.live.setPattern = w.setPatternCountAfterWarm;
    p.live.generation = w.configureGenerationAfterWarm;
    return p;
}

} // namespace

// r22 adversary A1, the row as written: warm and gate counts (3,5,11), adopted valid keys, an equal completed compare;
// the worker publishes and stores set_luts 5 -> 6 after the luts load of read 3 (the second post-compare read) and
// before its pattern load. Every native configure also publishes the generation before its call (llrawproc.c) and the
// present loads the generation last, so the hit is refused.
TEST(PlaybackWarmOutputReusePolicy, ALutsConfigureInsideTheFinalReadIsNeverAHitBecauseTheGenerationIsReadLast)
{
    Key w = warmKey();
    w.configureGenerationAfterWarm = 19;
    InjectedPresent present = atWarmCounts(w);
    present.midRead = [&present](int read, int field)
    {
        if (read != 3 || field != 1) return;
        ++present.live.setLuts;      // published before the native call ...
        ++present.live.generation;   // ... with the generation
    };
    bool attempted = false;
    const Reason r = present.run(w, presentKey(), &attempted);
    ASSERT_EQ(3, present.reads);
    ASSERT_EQ(std::string("recon_configured_since_warm"), name(r));
    ASSERT_FALSE(reuse(r));
    // and a generation that moved before the gate skips the compare, like any per-kind count
    InjectedPresent early = atWarmCounts(w);
    early.live.generation = 20;
    bool earlyAttempted = true;
    ASSERT_EQ(std::string("recon_configured_since_warm"), name(early.run(w, presentKey(), &earlyAttempted)));
    ASSERT_FALSE(earlyAttempted);
    ASSERT_EQ(0, early.compares);
}

// sol r17 #2, the row as written: reuse on, the present samples pattern count 11, then lookahead configures 1100 -> 0011
// (live count 12) before the present's compare; equal prepared state, equal Bayer16 input. Expected
// recon_configured_since_warm, never a hit.
TEST(PlaybackWarmOutputReusePolicy, APatternConfigureAfterTheGateReadAndBeforeTheCompareIsNeverAHit)
{
    const Key w = warmKey();
    ASSERT_EQ((uint64_t)11, w.setPatternCountAfterWarm);
    InjectedPresent present = atWarmCounts(w);
    present.duringCompare = [&present]() { present.live.setPattern = 12; };
    bool attempted = false;
    const Reason r = present.run(w, presentKey(), &attempted);
    ASSERT_EQ(std::string("recon_configured_since_warm"), name(r));
    ASSERT_FALSE(reuse(r));
    ASSERT_TRUE(attempted);
    ASSERT_EQ(1, present.compares);
    ASSERT_EQ(std::string("miss"), std::string(playback_warm_output_reuse::outcomeName(r, attempted)));
}

// the class, not the instance: a late set_clip or set_luts is refused the same way
TEST(PlaybackWarmOutputReusePolicy, AClipOrLutsConfigureAfterTheGateReadIsNeverAHitEither)
{
    const Key w = warmKey();
    for (int which = 0; which < 3; ++which)
    {
        InjectedPresent present = atWarmCounts(w);
        present.duringCompare = [&present, which]() {
            if (which == 0) ++present.live.setClip;
            else if (which == 1) ++present.live.setLuts;
            else ++present.live.setPattern;
        };
        bool attempted = false;
        const Reason r = present.run(w, presentKey(), &attempted);
        ASSERT_EQ(std::string("recon_configured_since_warm"), name(r));
        ASSERT_FALSE(reuse(r));
    }
}

// nothing configures: still a hit, and the counts were read once before the compare and twice after it
TEST(PlaybackWarmOutputReusePolicy, WithNoConfigureTheDecisionIsAHitAndReadsTheCountsAfterTheCompare)
{
    const Key w = warmKey();
    InjectedPresent present = atWarmCounts(w);
    bool attempted = false;
    const Reason r = present.run(w, presentKey(), &attempted);
    ASSERT_EQ(std::string("hit"), name(r));
    ASSERT_TRUE(reuse(r));
    ASSERT_TRUE(attempted);
    ASSERT_EQ(3, present.reads);
    ASSERT_EQ(1, present.compares);
}

// adversary r18 A1, the row as written: warm (3,5,11), equal compare; during the first read after the compare, after
// its LUT read of 5 and before its pattern read, lookahead frame 1244 runs set_luts (5 -> 6). That read is the torn
// tuple (3,5,11). Expected recon_configured_since_warm, never a hit.
TEST(PlaybackWarmOutputReusePolicy, AConfigureBetweenTwoCounterReadsAfterTheCompareIsNeverAHit)
{
    const Key w = warmKey();
    InjectedPresent present = atWarmCounts(w);
    present.midRead = [&present](int read, int field) {
        if (read == 2 && field == 1) present.live.setLuts = 6;
    };
    bool attempted = false;
    const Reason r = present.run(w, presentKey(), &attempted);
    ASSERT_EQ(std::string("recon_configured_since_warm"), name(r));
    ASSERT_FALSE(reuse(r));
    ASSERT_TRUE(attempted);
}

// the class: every tear position after the compare (a counter advanced after this read copied it), for each counter
// already copied in that read
TEST(PlaybackWarmOutputReusePolicy, EveryTornReadAfterTheCompareIsRefused)
{
    const Key w = warmKey();
    for (int field = 0; field < 3; ++field)
    {
        for (int counter = 0; counter <= field; ++counter)
        {
            InjectedPresent present = atWarmCounts(w);
            present.midRead = [&present, field, counter](int read, int at) {
                if (read != 2 || at != field) return;
                if (counter == 0) ++present.live.setClip;
                else if (counter == 1) ++present.live.setLuts;
                else ++present.live.setPattern;
            };
            bool attempted = false;
            const Reason r = present.run(w, presentKey(), &attempted);
            ASSERT_EQ(std::string("recon_configured_since_warm"), name(r));
            ASSERT_FALSE(reuse(r));
        }
    }
}

// a refusal before the compare does not compare and reads once; a content mismatch stays content_mismatch (the dump's
// trigger) even when a configure also landed
TEST(PlaybackWarmOutputReusePolicy, TheRecheckOnlyEverTurnsAHitIntoARefusal)
{
    const Key w = warmKey();
    InjectedPresent early = atWarmCounts(w);
    early.live.setPattern = 12;
    bool attempted = true;
    ASSERT_EQ(std::string("recon_configured_since_warm"), name(early.run(w, presentKey(), &attempted)));
    ASSERT_FALSE(attempted);
    ASSERT_EQ(0, early.compares);
    ASSERT_EQ(1, early.reads);

    InjectedPresent differs = atWarmCounts(w);
    differs.equal = false;
    differs.duringCompare = [&differs]() { differs.live.setPattern = 12; };
    ASSERT_EQ(std::string("content_mismatch"), name(differs.run(w, presentKey(), &attempted)));
    ASSERT_TRUE(attempted);
}

// ---- after the compare ----

TEST(PlaybackWarmOutputReusePolicy, OnlyACompletedEqualCompareIsAHit)
{
    ASSERT_TRUE(reuse(afterCompare(true, true, QString())));
    ASSERT_EQ(std::string("hit"), name(afterCompare(true, true, QString())));
    ASSERT_EQ(std::string("content_mismatch"), name(afterCompare(true, false, QString())));
    ASSERT_FALSE(reuse(afterCompare(true, false, QString())));
    // a compare that did not complete never reuses, whatever *equal says
    ASSERT_EQ(std::string("symbol_missing"), name(afterCompare(false, true, QStringLiteral("symbol_missing"))));
    ASSERT_EQ(std::string("capture_missing"), name(afterCompare(false, true, QStringLiteral("capture_missing"))));
    ASSERT_EQ(std::string("cuda_suppressed"), name(afterCompare(false, true, QStringLiteral("cuda_suppressed"))));
    ASSERT_EQ(std::string("compare_failed"), name(afterCompare(false, true, QStringLiteral("compare_failed"))));
    ASSERT_EQ(std::string("compare_failed"), name(afterCompare(false, true, QStringLiteral("anything else"))));
    for (const char *why : {"symbol_missing", "capture_missing", "cuda_suppressed", "compare_failed", ""})
        ASSERT_FALSE(reuse(afterCompare(false, true, QString::fromLatin1(why))));
}

// ---- the line formats ----

TEST(PlaybackWarmOutputReusePolicy, TheFieldsAndTheOutcomeNames)
{
    using playback_warm_output_reuse::fields;
    using playback_warm_output_reuse::outcomeName;
    ASSERT_EQ(std::string("hit"), std::string(outcomeName(Reason::Hit, true)));
    ASSERT_EQ(std::string("miss"), std::string(outcomeName(Reason::ContentMismatch, true)));
    ASSERT_EQ(std::string("miss"), std::string(outcomeName(Reason::CompareFailed, true)));
    ASSERT_EQ(std::string("skip"), std::string(outcomeName(Reason::WbMismatch, false)));
    ASSERT_EQ(std::string("skip"), std::string(outcomeName(Reason::Disabled, false)));
    ASSERT_EQ(std::string(" warm_output_reuse=hit reuse_reason=hit reuse_compare_ms=0.250"),
              fields(QStringLiteral("hit"), QStringLiteral("hit"), 0.25).toStdString());
    ASSERT_EQ(std::string(" warm_output_reuse=skip reuse_reason=none reuse_compare_ms=0.000"),
              fields(QString(), QString(), 0.0).toStdString());
    ASSERT_EQ(std::string(" reuse_capture=ok"), playback_warm_output_reuse::captureField(QStringLiteral("ok")).toStdString());
    ASSERT_EQ(std::string(" reuse_capture=none"), playback_warm_output_reuse::captureField(QString()).toStdString());
    // every reason has its own name
    const Reason all[] = {Reason::None, Reason::Disabled, Reason::NotAdopted, Reason::NoWarmKey, Reason::ValidationProbe,
                          Reason::CudaSuppressed, Reason::FrameMismatch, Reason::SizeMismatch, Reason::BlackMismatch,
                          Reason::WbMismatch, Reason::HnyquistMismatch, Reason::ReconConfiguredSinceWarm,
                          Reason::StateMismatch, Reason::Hit, Reason::ContentMismatch, Reason::CompareFailed,
                          Reason::SymbolMissing, Reason::CaptureMissing};
    for (const Reason a : all)
        for (const Reason b : all)
            if (a != b) ASSERT_TRUE(name(a) != name(b));
}

TEST(PlaybackWarmOutputReusePolicy, TheFirstPresentSplitEndsWithTheReuseFields)
{
    playback_startup_split::FirstPresentSplit fp;
    fp.warmOutputReuse = QStringLiteral("hit");
    fp.reuseReason = QStringLiteral("hit");
    fp.reuseCompareMs = 0.5;
    const std::string text = fp.line(4).toStdString();
    const std::string tail = " kick_lead_ms=NA warm_output_reuse=hit reuse_reason=hit reuse_compare_ms=0.500";
    ASSERT_EQ(text.size() - tail.size(), text.rfind(tail));
    ASSERT_TRUE(fp.frame0Reused());
    // the CPU route (no GPU window present): a skip that names it
    playback_startup_split::FirstPresentSplit cpu;
    ASSERT_FALSE(cpu.frame0Reused());
    ASSERT_TRUE(cpu.line(4).endsWith(QStringLiteral(" warm_output_reuse=skip reuse_reason=none reuse_compare_ms=0.000")));
}

TEST(PlaybackWarmOutputReusePolicy, AReusedFrameZeroIsNeverLabelledByFrameOnesRecord)
{
    playback_startup_split::AmazeFirstFrames frames;
    frames.frame0Reused = true;
    frames.count = 2;   // the session's first probed runs: frames 1 and 2
    frames.probeAvailable = true;
    frames.kernelMs[0] = 31.5;
    frames.deviceMs[0] = 30.25;
    frames.smMhzBefore[0] = 405.0;
    frames.valid[0] = true;
    frames.kernelMs[1] = 18.0;
    frames.deviceMs[1] = 17.5;
    frames.smMhzBefore[1] = 1755.0;
    frames.valid[1] = true;
    ASSERT_EQ(std::string("playback_smoke.amaze_first_frames session=3 f0=reused f1=31.500/30.250/405.0 "
                          "f2=18.000/17.500/1755.0 f3=NA/NA/NA"),
              frames.line(3).toStdString());
    // without a reuse, record 0 is frame 0 as before
    frames.frame0Reused = false;
    ASSERT_EQ(std::string("playback_smoke.amaze_first_frames session=3 f0=31.500/30.250/405.0 "
                          "f1=18.000/17.500/1755.0 f2=NA/NA/NA f3=NA/NA/NA"),
              frames.line(3).toStdString());
}

// ---- PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 T-D: the prepared recon state is part of the key ----
// The GPU recon takes white_darkened, black_delta, ev_correction, dark_noise, is_bright and the method flags as per-run
// constants (cudaMemcpyToSymbol in igpu_recon_cuda.cu), not through set_clip / set_luts, so the counters cannot see a
// change in them. A warm whose state differs from Play's is refused before any device compare.

TEST(PlaybackWarmOutputReusePolicy, EachPreparedStateFieldThatDiffersIsAStateMismatchBeforeAnyCompare)
{
    using playback_warm_output_reuse::PreparedState;
    struct Case
    {
        const char *field;
        void (*mutate)(PreparedState &);
    };
    const Case cases[] = {
        {"white_darkened", [](PreparedState &s) { s.whiteDarkened += 1; }},
        {"black_delta", [](PreparedState &s) { s.blackDelta += 1; }},
        {"ev_correction", [](PreparedState &s) { s.evCorrection = std::nextafter(s.evCorrection, 0.0); }},
        {"dark_noise", [](PreparedState &s) { s.darkNoise = std::nextafter(s.darkNoise, INFINITY); }},
        {"interp_method", [](PreparedState &s) { s.interpMethod = 0; }},
        {"use_alias_map", [](PreparedState &s) { s.useAliasMap = 1; }},
        {"use_fullres", [](PreparedState &s) { s.useFullres = 0; }},
        {"chroma_smooth_method", [](PreparedState &s) { s.chromaSmoothMethod = 0; }},
        {"apply_dither", [](PreparedState &s) { s.applyDither = 0; }},
        {"is_bright", [](PreparedState &s) { s.isBright[0] = 0; }},
        {"is_bright", [](PreparedState &s) { s.isBright[3] = 1; }},
        {"frame_id", [](PreparedState &s) { s.frameId += 1; }},
    };
    for (const Case &c : cases)
    {
        Key present = presentKey();
        c.mutate(present.state);
        const Reason r = gate(warmKey(), present);
        ASSERT_EQ(std::string("state_mismatch"), name(r));
        ASSERT_FALSE(reuse(r));
        ASSERT_TRUE(r != Reason::None);   // the caller compares only on None
        ASSERT_EQ(std::string("skip"), std::string(playback_warm_output_reuse::outcomeName(r, false)));
        ASSERT_EQ(std::string(c.field),
                  playback_warm_output_reuse::preparedStateDiff(warmKey().state, present.state).toStdString());
        // the same field changed on the warm's side instead
        Key warm = warmKey();
        c.mutate(warm.state);
        ASSERT_EQ(std::string("state_mismatch"), name(gate(warm, presentKey())));
    }
    // equal states (and everything else equal) still go to the compare
    ASSERT_TRUE(Reason::None == gate(warmKey(), presentKey()));
    ASSERT_EQ(std::string(), playback_warm_output_reuse::preparedStateDiff(warmKey().state, presentKey().state).toStdString());
}

TEST(PlaybackWarmOutputReusePolicy, AMissingPreparedStateOnEitherSideIsAStateMismatch)
{
    Key warm = warmKey();
    warm.state.valid = false;   // the warm could not read its prepared state
    ASSERT_EQ(std::string("state_mismatch"), name(gate(warm, presentKey())));
    Key present = presentKey();
    present.state = playback_warm_output_reuse::PreparedState();
    ASSERT_EQ(std::string("state_mismatch"), name(gate(warmKey(), present)));
    ASSERT_EQ(std::string("valid"),
              playback_warm_output_reuse::preparedStateDiff(warmKey().state, present.state).toStdString());
    // the earlier skip reasons still outrank it
    Key frame = presentKey();
    frame.frame += 1;
    frame.state = playback_warm_output_reuse::PreparedState();
    ASSERT_EQ(std::string("frame_mismatch"), name(gate(warmKey(), frame)));
    ASSERT_EQ(std::string("recon_configured_since_warm"),
              name(preGate(true, true, warmKey(), present, false, false, warmKey().setClipCountAfterWarm + 1,
                           warmKey().setLutsCountAfterWarm, warmKey().setPatternCountAfterWarm)));
}

TEST(PlaybackWarmOutputReusePolicy, ThePreparedStateIsReadFromTheReconStateByName)
{
    // the shape of llrpGpuPlaybackReconState_t (llrawproc.h): the template reads the same member names
    struct ReconState
    {
        int valid;
        int width;
        int height;
        int black_level;
        int white_level;
        int white_darkened;
        int black_delta;
        double ev_correction;
        double dark_noise;
        int interp_method;
        int use_alias_map;
        int use_fullres;
        int chroma_smooth_method;
        int playback_preview_scale_factor;
        int is_bright[4];
        int apply_dither;
        uint64_t frame_id;
    };
    ReconState r = {1, 1808, 2268, 2047, 16383, 8191, 23, -3.0000000000000004, 4.5, 1, 0, 1, 1, 0, {1, 1, 0, 0}, 1, 1243};
    const playback_warm_output_reuse::PreparedState s = playback_warm_output_reuse::preparedStateFrom(r);
    ASSERT_EQ(std::string(), playback_warm_output_reuse::preparedStateDiff(preparedState(), s).toStdString());
    r.valid = 0;
    ASSERT_FALSE(playback_warm_output_reuse::preparedStateFrom(r).valid);
}

TEST(PlaybackWarmOutputReusePolicy, AMissCarriesBothStatesAndAHitOrSkipCarriesNone)
{
    using playback_warm_output_reuse::stateFields;
    Key present = presentKey();
    present.state.whiteDarkened = 8190;
    const std::string state = stateFields(Reason::StateMismatch, warmKey(), present).toStdString();
    ASSERT_EQ(std::string(" warm_state=wd=8191,bd=23,ev=-3.0000000000000004,dn=4.5,interp=1,alias=0,fullres=1,cs=1,"
                          "dither=1,bright=1/1/0/0,fid=1243"
                          " play_state=wd=8190,bd=23,ev=-3.0000000000000004,dn=4.5,interp=1,alias=0,fullres=1,cs=1,"
                          "dither=1,bright=1/1/0/0,fid=1243 state_diff=white_darkened"),
              state);
    // a content miss with equal states says so: the difference is in the input, not in the constants
    const std::string content = stateFields(Reason::ContentMismatch, warmKey(), presentKey()).toStdString();
    ASSERT_TRUE(content.find(" warm_state=wd=8191,") == 0);
    ASSERT_EQ(content.size() - std::string(" state_diff=none").size(), content.rfind(" state_diff=none"));
    // a missing state prints none
    Key none = presentKey();
    none.state = playback_warm_output_reuse::PreparedState();
    ASSERT_TRUE(stateFields(Reason::StateMismatch, warmKey(), none).contains(QStringLiteral(" play_state=none ")));
    // nothing on a hit or a skip for any other reason
    for (const Reason r : {Reason::Hit, Reason::Disabled, Reason::NotAdopted, Reason::NoWarmKey, Reason::FrameMismatch,
                           Reason::WbMismatch, Reason::ReconConfiguredSinceWarm, Reason::CompareFailed})
        ASSERT_TRUE(stateFields(r, warmKey(), present).isEmpty());
    // the first-present split line ends with them on a miss
    playback_startup_split::FirstPresentSplit fp;
    fp.warmOutputReuse = QStringLiteral("skip");
    fp.reuseReason = QStringLiteral("state_mismatch");
    fp.reuseStates = QString::fromStdString(state);
    const std::string line = fp.line(4).toStdString();
    const std::string tail = " warm_output_reuse=skip reuse_reason=state_mismatch reuse_compare_ms=0.000" + state;
    ASSERT_EQ(line.size() - tail.size(), line.rfind(tail));
}
