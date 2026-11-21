// PLAYBACK-STARTUP-CATCHUP-BURST-1 r7: the opt-in per-present playback trace (MLVAPP_PLAYBACK_PRESENT_TRACE=1). Off by
// default it must be inert -- no state, no allocation, no row -- so a product run pays one flag test per hook. On, it
// gives every presented frame one row with what the playback timer did since the previous present.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackPresentTrace.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <limits>
#include <string>

using playback_present_trace::Recorder;
using playback_present_trace::Row;

namespace
{

// One measured Play: origin 1000 ms, two presents 41.7 ms apart with the 8 ms poll between them.
void driveOnePlay(Recorder &trace)
{
    trace.begin(1000.0, 64);
    trace.noteTick(1001.0, false, false);
    trace.noteAdvance(1001.5, 0.0, 1.0, 42);
    Row first;
    first.frame = 1;
    first.position = 1;
    first.presentMs = 1010.0;
    first.readyMs = 1005.0;
    trace.notePresent(first);
    trace.notePresentEnd(1012.0);
    trace.noteTick(1020.0, true, false);
    trace.noteTick(1028.0, false, true);
    trace.noteAdvance(1028.5, 1.0, 1.4, 42);
    trace.noteTick(1036.0, false, false);
    trace.noteAdvance(1036.5, 1.4, 2.0, 8);
    trace.noteGrab(3.5);
    trace.noteAudioSync();
    Row second;
    second.frame = 2;
    second.position = 2;
    second.presentMs = 1051.7;
    second.readyMs = -1.0;
    trace.notePresent(second);
    trace.end();
}

QString sourceOf(const QString &relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString mainWindowSource()
{
    return sourceOf(QStringLiteral("platform/qt/MainWindow.cpp"));
}

// PLAYBACK-TRACE-LOWOVERHEAD-1 r1: one frame's worker stamps in stage-clock SECONDS, 2 ms apart per stage from request.
// Absolute ms with the Play origin at 1000: request 1001, ... draw begin 1016.
playback_present_trace::StageStamps stagesFrom(double requestS)
{
    playback_present_trace::StageStamps s;
    s.requestS = requestS;
    s.decodeQueueS = requestS + 0.002;
    s.decodeStartS = requestS + 0.004;
    s.decodeEndS = requestS + 0.006;
    s.reconStartS = requestS + 0.008;
    s.reconEndS = requestS + 0.020;
    s.renderStartS = requestS + 0.021;
    s.drawBeginS = requestS + 0.030;
    return s;
}

// RAII: the process-wide stage switch is restored so a test never leaks it into the next.
struct StampGateScope
{
    explicit StampGateScope(bool on) : previous(playback_present_trace::stampsEnabled()) { playback_present_trace::setStampsEnabled(on); }
    ~StampGateScope() { playback_present_trace::setStampsEnabled(previous); }
    bool previous;
};

} // namespace

TEST(PlaybackPresentTrace, OffByDefaultItRecordsAndAllocatesNothing)
{
    Recorder trace;
    ASSERT_FALSE(trace.enabled());
    driveOnePlay(trace);
    ASSERT_FALSE(trace.active());
    ASSERT_TRUE(trace.empty());
    ASSERT_EQ(static_cast<size_t>(0), trace.allocated());   // the backing storage, not the slot count: nothing was reserved
}

TEST(PlaybackPresentTrace, SwitchedOffAgainItStaysInert)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.setEnabled(false);
    driveOnePlay(trace);
    ASSERT_TRUE(trace.empty());
    ASSERT_EQ(static_cast<size_t>(0), trace.allocated());
}

TEST(PlaybackPresentTrace, OnEveryPresentGetsARowWithTheTimerSinceThePreviousPresent)
{
    Recorder trace;
    trace.setEnabled(true);
    driveOnePlay(trace);
    ASSERT_FALSE(trace.active());   // end() stops recording; the rows stay for the log
    ASSERT_EQ(static_cast<size_t>(2), trace.size());
    const Row &a = trace.row(0);
    ASSERT_NEAR(10.0, a.presentMs, 1e-9);
    ASSERT_NEAR(5.0, a.readyMs, 1e-9);
    ASSERT_NEAR(0.0, a.intervalMs, 1e-9);
    ASSERT_EQ(1, a.ticks);
    ASSERT_EQ(1, a.advances);
    ASSERT_NEAR(1.5, a.lastAdvanceMs, 1e-9);
    ASSERT_NEAR(-1.0, a.prevEndMs, 1e-9);
    const Row &b = trace.row(1);
    ASSERT_NEAR(51.7, b.presentMs, 1e-9);
    ASSERT_NEAR(41.7, b.intervalMs, 1e-9);
    ASSERT_NEAR(-1.0, b.readyMs, 1e-9);   // an unknown ready time stays unknown, never shifted
    ASSERT_NEAR(12.0, b.prevEndMs, 1e-9);
    ASSERT_EQ(3, b.ticks);
    ASSERT_EQ(1, b.busyTicks);
    ASSERT_EQ(1, b.predictiveTicks);
    ASSERT_NEAR(10.0, b.maxTickGapMs, 1e-9);   // previous present (10) -> first tick (20)
    ASSERT_EQ(2, b.advances);
    ASSERT_NEAR(1.0, b.advancedFrames, 1e-9);
    ASSERT_NEAR(36.5, b.lastAdvanceMs, 1e-9);
    ASSERT_NEAR(8.0, b.lastTickDiffMs, 1e-9);
    ASSERT_NEAR(3.5, b.grabMs, 1e-9);
    ASSERT_EQ(1, b.audioSyncs);
}

TEST(PlaybackPresentTrace, AHoldIsNotAnAdvanceAndANewPlayStartsClean)
{
    Recorder trace;
    trace.setEnabled(true);
    driveOnePlay(trace);
    trace.begin(5000.0, 8);
    trace.noteTick(5008.0, false, false);
    trace.noteAdvance(5008.5, 7.0, 7.0, 8);   // granted nothing: a hold
    Row r;
    r.presentMs = 5020.0;
    trace.notePresent(r);
    ASSERT_EQ(static_cast<size_t>(1), trace.size());
    ASSERT_EQ(0, trace.row(0).advances);
    ASSERT_NEAR(-1.0, trace.row(0).lastAdvanceMs, 1e-9);
    ASSERT_NEAR(0.0, trace.row(0).intervalMs, 1e-9);
}

TEST(PlaybackPresentTrace, ARowHasOneFieldPerHeaderColumn)
{
    Recorder trace;
    trace.setEnabled(true);
    driveOnePlay(trace);
    const std::string header = Recorder::header();
    const std::string row = Recorder::formatRow(1, trace.row(1));
    const auto commas = [](const std::string &s) { size_t n = 0; for (char c : s) n += c == ',' ? 1 : 0; return n; };
    ASSERT_EQ(commas(header), commas(row));
    ASSERT_EQ(0u, row.find("1,2,2,51.700,41.700,-1.000,"));
}

// ---- PLAYBACK-TRACE-LOWOVERHEAD-1 r1: per-frame stage stamps at LIGHT cost ----

TEST(PlaybackPresentTraceStages, AFrameStoresItsStageStampsOrderedAndRebasedToThePlayOrigin)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 16);
    Row r;
    r.frame = 7;
    r.setStages(stagesFrom(1.001));        // request at 1001 ms absolute
    r.readyMs = 1022.0;
    r.presentMs = 1035.0;
    trace.notePresent(r);
    ASSERT_EQ(static_cast<size_t>(1), trace.size());
    const Row &a = trace.row(0);
    ASSERT_NEAR(1.0, a.reqMs, 1e-6);
    ASSERT_NEAR(3.0, a.dqMs, 1e-6);
    ASSERT_NEAR(5.0, a.dsMs, 1e-6);
    ASSERT_NEAR(7.0, a.deMs, 1e-6);
    ASSERT_NEAR(9.0, a.rsMs, 1e-6);
    ASSERT_NEAR(21.0, a.reMs, 1e-6);
    ASSERT_NEAR(22.0, a.rndMs, 1e-6);
    ASSERT_NEAR(31.0, a.drawBeginMs, 1e-6);
    ASSERT_NEAR(22.0, a.readyMs, 1e-9);
    ASSERT_NEAR(35.0, a.presentMs, 1e-9);
    ASSERT_TRUE(a.stagesOrdered());
    // out of order is reported, not hidden: the verdict is judged in notePresent on the absolute inputs and stored in the row
    Row bad;
    bad.setStages(stagesFrom(1.001));
    bad.reMs = 1004.0;                     // recon ended (absolute) before the decode ended at 1007
    bad.readyMs = 1022.0;
    bad.presentMs = 1035.0;
    trace.notePresent(bad);
    ASSERT_FALSE(trace.row(1).stagesOrdered());
}

TEST(PlaybackPresentTraceStages, AStageThatWasNeverStampedStaysUnknownAndAStageBeforeThePlayStaysNegative)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 4);
    playback_present_trace::StageStamps s = stagesFrom(0.990);   // requested 10 ms before the Play's origin
    s.reconEndS = 0.0;                                           // this one was never stamped
    Row r;
    r.setStages(s);
    r.presentMs = 1040.0;
    trace.notePresent(r);
    const Row &a = trace.row(0);
    ASSERT_NEAR(-10.0, a.reqMs, 1e-6);
    ASSERT_TRUE(a.reMs <= playback_present_trace::kUnknownMs / 2.0);
    ASSERT_TRUE(a.stagesOrdered());
}

TEST(PlaybackPresentTraceStages, TheRingKeepsTheNewestRowsWithTheirStagesAndNeverReallocates)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(0.0, 4);
    const Row *const ring = trace.storage();
    ASSERT_EQ(static_cast<size_t>(4), trace.capacity());
    const size_t allocatedAtBegin = trace.allocated();
    ASSERT_TRUE(allocatedAtBegin >= 4);
    for (int i = 0; i < 10; ++i)
    {
        Row r;
        r.frame = 100 + i;
        r.setStages(stagesFrom(1.0 + i));   // each present's stages are 1 s apart
        r.readyMs = 1000.0 * (1.0 + i) + 22.0;
        r.presentMs = 1000.0 * (1.0 + i) + 35.0;
        trace.notePresent(r);
    }
    ASSERT_EQ(static_cast<size_t>(4), trace.size());
    ASSERT_EQ(static_cast<size_t>(10), trace.total());
    ASSERT_EQ(static_cast<size_t>(6), trace.dropped());
    ASSERT_EQ(static_cast<size_t>(6), trace.firstIndex());
    ASSERT_EQ(static_cast<size_t>(4), trace.capacity());
    ASSERT_EQ(allocatedAtBegin, trace.allocated());   // the backing storage's own capacity never grew
    ASSERT_TRUE(ring == trace.storage());   // a present never moved or grew the storage
    for (size_t k = 0; k < trace.size(); ++k)
    {
        const Row &a = trace.row(k);
        const int i = 6 + static_cast<int>(k);
        ASSERT_EQ(100 + i, a.frame);                                   // oldest first
        ASSERT_NEAR(1000.0 * (1.0 + i), a.reqMs, 1e-6);                // its own stamps, not a neighbour's
        ASSERT_NEAR(1000.0 * (1.0 + i) + 20.0, a.reMs, 1e-6);
        ASSERT_TRUE(a.stagesOrdered());
    }
}

TEST(PlaybackPresentTraceStages, ANewPlayStartsTheRingClean)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(0.0, 3);
    for (int i = 0; i < 5; ++i)
    {
        Row r;
        r.frame = i;
        r.setStages(stagesFrom(1.0 + i));
        trace.notePresent(r);
    }
    trace.begin(0.0, 3);
    ASSERT_TRUE(trace.empty());
    ASSERT_EQ(static_cast<size_t>(0), trace.total());
    ASSERT_EQ(static_cast<size_t>(0), trace.dropped());
}

TEST(PlaybackPresentTraceStages, TheDumpCarriesTheStagesAndTheHeaderStillMatchesTheRow)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 8);
    Row r;
    r.frame = 7;
    r.position = 7;
    r.setStages(stagesFrom(1.001));
    r.readyMs = 1022.0;
    r.presentMs = 1035.0;
    trace.notePresent(r);
    const std::string header = Recorder::header();
    const std::string row = Recorder::formatRow(trace.firstIndex(), trace.row(0));
    const auto commas = [](const std::string &s) { size_t n = 0; for (char c : s) n += c == ',' ? 1 : 0; return n; };
    ASSERT_EQ(commas(header), commas(row));
    // the original columns keep their place; the stage columns are appended
    ASSERT_TRUE(header.find("audio_syncs,req,dq,ds,de,rs,re,rnd,draw_begin,stage_ok") != std::string::npos);
    ASSERT_TRUE(row.find(",1.000,3.000,5.000,7.000,9.000,21.000,22.000,31.000,1") != std::string::npos);
    // r3 (adversary 81c5805c A1/A2): no per-frame duration column derived from the backend's last-run status
    ASSERT_TRUE(header.find("configure") == std::string::npos);
    ASSERT_TRUE(header.find("h2d") == std::string::npos);
}

TEST(PlaybackPresentTraceStages, WithTheSwitchOffTheStageHooksAreNoOps)
{
    {
        StampGateScope off(false);
        double slotField = 0.0;
        playback_present_trace::stamp(slotField, 12.5);
        ASSERT_NEAR(0.0, slotField, 0.0);
        ASSERT_FALSE(playback_present_trace::stampsEnabled());
    }
    {
        StampGateScope on(true);
        double slotField = 0.0;
        playback_present_trace::stamp(slotField, 12.5);
        ASSERT_NEAR(12.5, slotField, 0.0);
    }
    // a disabled recorder takes no stages either, whatever the stamp switch says
    StampGateScope on(true);
    Recorder trace;
    Row r;
    r.setStages(stagesFrom(1.0));
    trace.begin(0.0, 8);
    trace.notePresent(r);
    ASSERT_TRUE(trace.empty());
    ASSERT_EQ(static_cast<size_t>(0), trace.allocated());
}

// adversary a7085920 A2: a request stamped 0.5 ms and a queue stamped exactly 1 ms BEFORE the Play's start (-0.5, -1.0) run
// backwards; the old shared "t == -1.0 is unknown" skip hid the second one and reported stage_ok=1.
TEST(PlaybackPresentTraceStages, ARealMinusOneMsStampStillTakesPartInTheOrderCheck)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 4);
    playback_present_trace::StageStamps s;
    s.requestS = 0.9995;      // -0.5 ms
    s.decodeQueueS = 0.999;   // -1.0 ms: before the request
    Row r;
    r.setStages(s);
    r.presentMs = 1001.0;
    trace.notePresent(r);
    ASSERT_FALSE(trace.row(0).stagesOrdered());
    // an unknown ready time (-1) is still skipped, not mistaken for a stamp
    Row ok;
    ok.setStages(stagesFrom(1.0));
    ok.readyMs = -1.0;
    ok.presentMs = 1040.0;
    trace.notePresent(ok);
    ASSERT_TRUE(trace.row(1).stagesOrdered());
}

// r4 (sol r3 B1, HUB RULING 2026-11-20T16:00Z): "unknown" is never a value a real stamp can take. A known ready time that
// rebases to exactly -1.0 ms is a stamp like any other and takes part in the order check.
TEST(PlaybackPresentTraceStages, AKnownReadyTimeThatRebasesToMinusOneMsStillTakesPartInTheOrderCheck)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 4);
    playback_present_trace::StageStamps s;
    s.renderStartS = 0.9995;   // rnd = -0.5 ms
    s.drawBeginS = 1.001;      // draw_begin = +1 ms
    Row r;
    r.setStages(s);
    r.readyMs = 999.0;         // known, absolute: rebases to exactly -1.0 ms, BEFORE rnd
    r.presentMs = 1002.0;
    trace.notePresent(r);
    ASSERT_NEAR(-1.0, trace.row(0).readyMs, 1e-9);
    ASSERT_FALSE(trace.row(0).stagesOrdered());
    const std::string line = Recorder::formatRow(0, trace.row(0));
    ASSERT_TRUE(line.size() > 2 && line.compare(line.size() - 2, 2, ",0") == 0);   // stage_ok=0
    // the same -1.0 ms ready stamp in order (rnd before it) is ordered, and an unknown ready is still skipped
    Row ordered;
    playback_present_trace::StageStamps s2;
    s2.renderStartS = 0.998;   // rnd = -2.0 ms
    s2.drawBeginS = 1.001;
    ordered.setStages(s2);
    ordered.readyMs = 999.0;
    ordered.presentMs = 1002.0;
    trace.notePresent(ordered);
    ASSERT_TRUE(trace.row(1).stagesOrdered());
    Row unknownReady;
    unknownReady.setStages(s);
    unknownReady.readyMs = -1.0;
    unknownReady.presentMs = 1002.0;
    trace.notePresent(unknownReady);
    ASSERT_TRUE(trace.row(2).stagesOrdered());
}

// r4: the recorder's own "have a previous ..." state is a flag, not a value a real time can take. A tick and a present that
// land exactly 1 ms before the Play's origin (-1.0) are real previous events.
TEST(PlaybackPresentTraceStages, APreviousTickOrPresentAtMinusOneMsIsStillAPreviousEvent)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 8);
    trace.noteTick(999.0, false, false);    // -1.0
    trace.noteTick(1009.0, false, false);   // +9.0: the gap to the previous tick is 10 ms
    Row first;
    first.presentMs = 999.0;                // rebases to -1.0
    trace.notePresent(first);
    Row second;
    second.presentMs = 1010.0;
    trace.notePresent(second);
    ASSERT_NEAR(10.0, trace.row(0).maxTickGapMs, 1e-9);
    ASSERT_NEAR(11.0, trace.row(1).intervalMs, 1e-9);   // since the present at -1.0, not 0
    // a present end at -1.0 is carried to the next row as a value
    trace.begin(1000.0, 8);
    trace.notePresentEnd(999.0);
    Row third;
    third.presentMs = 1010.0;
    trace.notePresent(third);
    ASSERT_NEAR(-1.0, trace.row(0).prevEndMs, 1e-9);
}

namespace
{

// The ten chain fields in request .. present order (the order stagesOrdered walks): 0 req, 1 dq, 2 ds, 3 de, 4 rs, 5 re,
// 6 rnd, 7 ready, 8 draw_begin, 9 present.
constexpr int kChainLength = 10;
double *chainField(Row &r, int index)
{
    double *const fields[kChainLength] = { &r.reqMs, &r.dqMs, &r.dsMs, &r.deMs, &r.rsMs,
                                           &r.reMs, &r.rndMs, &r.readyMs, &r.drawBeginMs, &r.presentMs };
    return fields[index];
}

} // namespace

// r5 (sol r4 B1, HUB RULING 2026-11-20T18:55Z): presence is explicit, never a magnitude test. A KNOWN stamp that rebases to
// -5e8 ms or lower (a Play origin far past the stage clock's zero) is a stamp like any other. Sol's literal input, then
// every one of the nine adjacent edges of the chain. r6 (fable key3 r5 B1): the order is judged on the ABSOLUTE inputs in
// notePresent and the verdict is stored, so the test is named for that and not for the rebase.
TEST(PlaybackPresentTraceStages, AKnownStampIsOrderedOnItsAbsoluteValueNoMatterWhatTheRebaseDoesToIt)
{
    {
        Recorder trace;
        trace.setEnabled(true);
        trace.begin(500002000.0, 1);
        playback_present_trace::StageStamps s;
        s.requestS = 2.0;       // 2000 ms absolute: rebases to exactly -500000000
        s.decodeQueueS = 1.0;   // 1000 ms absolute: rebases to -500001000, BEFORE the request
        Row r;
        r.setStages(s);
        r.presentMs = 500002001.0;
        trace.notePresent(r);
        ASSERT_FALSE(trace.row(0).stagesOrdered());
    }
    const double origin = 500002000.0;
    for (int edge = 0; edge + 1 < kChainLength; ++edge)
    {
        for (const bool reversed : { true, false })
        {
            Recorder trace;
            trace.setEnabled(true);
            trace.begin(origin, 1);
            Row r;
            r.presentMs = origin + 10.0;   // a later present, so only this edge decides the verdict
            *chainField(r, edge) = reversed ? 2000.0 : 1000.0;
            *chainField(r, edge + 1) = reversed ? 1000.0 : 2000.0;
            trace.notePresent(r);
            ASSERT_EQ(!reversed, trace.row(0).stagesOrdered());
        }
    }
}

// r5 (sol r4 B2): a non-finite value in a Row that enters notePresent is "unknown" and can never turn the ordering
// accumulator into NaN. Sol's literal input (req=20, dq=NaN, ds=10) and every direct stage field.
TEST(PlaybackPresentTraceStages, ANonFiniteStageInAnAcceptedRowIsUnknownAndNeverHidesALaterReversal)
{
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    {
        Recorder trace;
        trace.setEnabled(true);
        trace.begin(0.0, 1);
        Row r;
        r.reqMs = 20.0;
        r.dqMs = nan;
        r.dsMs = 10.0;
        r.presentMs = 30.0;
        trace.notePresent(r);
        ASSERT_FALSE(trace.row(0).stagesOrdered());
        ASSERT_TRUE(trace.row(0).dqMs <= playback_present_trace::kUnknownMs / 2.0);   // stored as unknown, not NaN
    }
    for (const double bad : { nan, inf, -inf })
    {
        // every field the key names: dq ds de rs re rnd draw_begin, plus ready
        for (const int field : { 1, 2, 3, 4, 5, 6, 7, 8 })
        {
            Recorder trace;
            trace.setEnabled(true);
            trace.begin(0.0, 1);
            Row r;
            r.presentMs = 30.0;
            *chainField(r, field - 1) = 20.0;
            *chainField(r, field) = bad;
            *chainField(r, field + 1) = 10.0;   // 20 then 10: a reversal the bad value must not hide
            trace.notePresent(r);
            ASSERT_FALSE(trace.row(0).stagesOrdered());
        }
    }
}

// r6 (fable key3 r5 B1, HUB RULING 2026-11-20T22:58Z): begin()'s origin is an accepted input too. A non-finite origin is
// REFUSED (the Play records nothing), and a huge finite one cannot hide a reversal, because the order is judged on the
// absolute inputs and not on values that were rebased (and rounded) against that origin.
TEST(PlaybackPresentTraceStages, ANonFiniteOriginIsRefusedAndAHugeOneCannotHideAReversedChain)
{
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    for (const double origin : { nan, inf, -inf })
    {
        Recorder trace;
        trace.setEnabled(true);
        trace.begin(origin, 4);
        ASSERT_FALSE(trace.active());              // refused: the Play is not recorded
        Row r;
        r.reqMs = 20.0;
        r.dqMs = 10.0;                             // reversed
        r.presentMs = 30.0;
        trace.notePresent(r);
        ASSERT_TRUE(trace.empty());                // so no row can say stage_ok=1 for it
        ASSERT_EQ(static_cast<size_t>(0), trace.total());
        trace.begin(1000.0, 4);                    // a later, valid Play records again
        ASSERT_TRUE(trace.active());
        trace.notePresent(r);
        ASSERT_EQ(static_cast<size_t>(1), trace.size());
        ASSERT_FALSE(trace.row(0).stagesOrdered());
    }
}

TEST(PlaybackPresentTraceStages, AHugeFiniteOriginCannotRoundTwoReversedStampsIntoAnOrderedChain)
{
    {
        // fable's literal: both stamps round to exactly -1e20 once rebased against 1e20
        Recorder trace;
        trace.setEnabled(true);
        trace.begin(1.0e20, 2);
        playback_present_trace::StageStamps s;
        s.requestS = 2.0;
        s.decodeQueueS = 1.0;                      // before the request
        Row r;
        r.setStages(s);
        r.presentMs = 1.0e20;
        trace.notePresent(r);
        ASSERT_FALSE(trace.row(0).stagesOrdered());
        playback_present_trace::StageStamps good;
        good.requestS = 1.0;
        good.decodeQueueS = 2.0;                   // in order, and still ordered
        Row g;
        g.setStages(good);
        g.presentMs = 1.0e20;
        trace.notePresent(g);
        ASSERT_TRUE(trace.row(1).stagesOrdered());
    }
}

TEST(PlaybackPresentTraceStages, ARebaseThatOverflowsIsUnknownAndNeverAnInfiniteStoredValue)
{
    {
        // 1e308 - (-1e308) overflows to +inf
        Recorder trace;
        trace.setEnabled(true);
        trace.begin(-1.0e308, 1);
        Row r;
        r.reqMs = 1.0e308;
        r.dqMs = 1.0e308;
        r.presentMs = 1.0e308;
        trace.notePresent(r);
        ASSERT_TRUE(std::isfinite(trace.row(0).reqMs));
        ASSERT_TRUE(std::isfinite(trace.row(0).dqMs));
        ASSERT_TRUE(std::isfinite(trace.row(0).presentMs));
    }
}

// r6 (fable key3 r5 B2): formatRow must never cut a row. Two 1e308 stamps print 313 characters each; the row still has one
// field per header column and still ends with stage_ok.
TEST(PlaybackPresentTraceStages, ARowWithHugeFiniteStampsIsNeverCutShort)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(0.0, 1);
    Row r;
    r.reqMs = 1.0e308;
    r.dqMs = 1.0e308;
    r.dsMs = 1.0e308;
    r.deMs = 1.0e308;
    r.rsMs = 1.0e308;
    r.reMs = 1.0e308;
    r.rndMs = 1.0e308;
    r.drawBeginMs = 1.0e308;
    r.presentMs = 30.0;                            // before the stamps: stage_ok=0
    trace.notePresent(r);
    const std::string header = Recorder::header();
    const std::string row = Recorder::formatRow(0, trace.row(0));
    const auto commas = [](const std::string &s) { size_t n = 0; for (char c : s) n += c == ',' ? 1 : 0; return n; };
    ASSERT_EQ(commas(header), commas(row));
    ASSERT_TRUE(row.size() > 2 && row.compare(row.size() - 2, 2, ",0") == 0);   // stage_ok is present and 0
}

// r6 (fable key3 r5 H1): a non-finite presentMs is unknown at the entry. It is stored as the marker, never as NaN, and the
// next row keeps its interval and its present-to-first-tick gap, measured from the last KNOWN present.
TEST(PlaybackPresentTraceStages, ANonFinitePresentIsUnknownAndTheNextRowKeepsItsIntervalAndTickGap)
{
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    for (const double bad : { nan, inf, -inf })
    {
        Recorder trace;
        trace.setEnabled(true);
        trace.begin(0.0, 8);
        Row first;
        first.presentMs = 10.0;
        trace.notePresent(first);
        Row unknown;
        unknown.presentMs = bad;
        trace.notePresent(unknown);
        trace.noteTick(30.0, false, false);        // 20 ms after the last known present
        Row next;
        next.presentMs = 50.0;
        trace.notePresent(next);
        ASSERT_NEAR(playback_present_trace::kUnknownMs, trace.row(1).presentMs, 0.0);
        ASSERT_TRUE(trace.row(1).stagesOrdered());
        ASSERT_TRUE(std::isfinite(trace.row(2).intervalMs));
        ASSERT_NEAR(40.0, trace.row(2).intervalMs, 1e-9);
        ASSERT_NEAR(20.0, trace.row(2).maxTickGapMs, 1e-9);
    }
    // fable's literal: nothing known before the NaN present
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(0.0, 4);
    Row unknown;
    unknown.presentMs = nan;
    trace.notePresent(unknown);
    Row next;
    next.presentMs = 50.0;
    trace.notePresent(next);
    ASSERT_TRUE(std::isfinite(trace.row(1).intervalMs));
}

// r8 (sol r7 B1): an unknown present is not an event the timer can measure from, so it never clears the tick history. sol's
// catalogue row: present(10) tick(20) present(unknown) tick(30) present(50) -> the last row's max_tick_gap is 10 ms, the gap
// between the two consecutive VALID ticks; and the same row without the first known present -> 10 ms too (not 0).
TEST(PlaybackPresentTraceStages, AnUnknownPresentKeepsTheTickHistorySoTheGapIsBetweenTheValidTicks)
{
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    for (const bool knownFirst : { true, false })
    {
        for (const double bad : { nan, inf, -inf })
        {
            Recorder trace;
            trace.setEnabled(true);
            trace.begin(0.0, 8);
            if (knownFirst)
            {
                Row first;
                first.presentMs = 10.0;
                trace.notePresent(first);
            }
            trace.noteTick(20.0, false, false);
            Row unknown;
            unknown.presentMs = bad;
            trace.notePresent(unknown);
            trace.noteTick(30.0, false, false);
            Row next;
            next.presentMs = 50.0;
            trace.notePresent(next);
            const size_t last = trace.size() - 1;
            ASSERT_NEAR(10.0, trace.row(last).maxTickGapMs, 1e-9);
            ASSERT_EQ(1, trace.row(last).ticks);
            // the unknown present's own row: present 10 -> tick 20 when that present is known, else no earlier event
            ASSERT_NEAR(knownFirst ? 10.0 : 0.0, trace.row(last - 1).maxTickGapMs, 1e-9);
        }
    }
    // A KNOWN present still restarts the gap from itself: present(10) tick(20) present(25) tick(30) present(50) -> 5 ms.
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(0.0, 8);
    Row a;
    a.presentMs = 10.0;
    trace.notePresent(a);
    trace.noteTick(20.0, false, false);
    Row b;
    b.presentMs = 25.0;
    trace.notePresent(b);
    trace.noteTick(30.0, false, false);
    Row c;
    c.presentMs = 50.0;
    trace.notePresent(c);
    ASSERT_NEAR(5.0, trace.row(2).maxTickGapMs, 1e-9);
}

// r8 (sol r7 B2): a frame no worker queued (the inline render of Fast quality with Phase3Mode Disabled, and runSerial) still
// carries its own request stamp: drawFrame takes it from the active request's clock value, the same value
// queueDecodeRequestLocked stamps for a queued frame. Driven through the helper drawFrame calls, then through the recorder.
TEST(PlaybackPresentTraceStages, AFrameNoWorkerQueuedStillCarriesItsOwnRequestStamp)
{
    StampGateScope on(true);
    // the slot was last queued for request 41, so its stamps are that frame's; this frame is request 42, requested at 1.0015 s
    const playback_present_trace::StageStamps stale = stagesFrom(0.500);
    playback_present_trace::StageStamps s = playback_present_trace::stampsForRender(stale, 41, 42, 1.0015);
    ASSERT_NEAR(1.0015, s.requestS, 1e-12);
    // the slot-serial guard: none of request 41's stamps rides with request 42
    ASSERT_NEAR(0.0, s.decodeQueueS, 0.0);
    ASSERT_NEAR(0.0, s.decodeStartS, 0.0);
    ASSERT_NEAR(0.0, s.decodeEndS, 0.0);
    ASSERT_NEAR(0.0, s.reconStartS, 0.0);
    ASSERT_NEAR(0.0, s.reconEndS, 0.0);
    ASSERT_NEAR(0.0, s.renderStartS, 0.0);
    ASSERT_NEAR(0.0, s.drawBeginS, 0.0);
    // the render thread stamps its start, the frame is presented, and the row's req is a real time, not the unknown marker
    playback_present_trace::stamp(s.renderStartS, 1.0100);
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 4);
    Row row;
    row.setStages(s);
    row.readyMs = 1012.0;
    row.presentMs = 1020.0;
    trace.notePresent(row);
    ASSERT_NEAR(1.5, trace.row(0).reqMs, 1e-6);
    ASSERT_NEAR(playback_present_trace::kUnknownMs, trace.row(0).dqMs, 0.0);
    ASSERT_NEAR(10.0, trace.row(0).rndMs, 1e-6);
    ASSERT_TRUE(trace.row(0).stagesOrdered());
    // a slot never used before (serial 0) on the inline route: same answer
    const playback_present_trace::StageStamps fresh =
        playback_present_trace::stampsForRender(playback_present_trace::StageStamps(), 0, 7, 2.25);
    ASSERT_NEAR(2.25, fresh.requestS, 1e-12);
}

// r8: a queued frame keeps every stamp its workers took, including the request stamp queueDecodeRequestLocked took.
TEST(PlaybackPresentTraceStages, AQueuedFrameKeepsItsOwnWorkerStamps)
{
    StampGateScope on(true);
    const playback_present_trace::StageStamps mine = stagesFrom(1.001);
    const playback_present_trace::StageStamps s = playback_present_trace::stampsForRender(mine, 42, 42, 1.001);
    ASSERT_NEAR(mine.requestS, s.requestS, 0.0);
    ASSERT_NEAR(mine.decodeQueueS, s.decodeQueueS, 0.0);
    ASSERT_NEAR(mine.decodeStartS, s.decodeStartS, 0.0);
    ASSERT_NEAR(mine.decodeEndS, s.decodeEndS, 0.0);
    ASSERT_NEAR(mine.reconStartS, s.reconStartS, 0.0);
    ASSERT_NEAR(mine.reconEndS, s.reconEndS, 0.0);
    ASSERT_NEAR(mine.renderStartS, s.renderStartS, 0.0);
    ASSERT_NEAR(mine.drawBeginS, s.drawBeginS, 0.0);
}

// r8: with the trace off the render-side helper takes no request stamp (the hook stays a no-op).
TEST(PlaybackPresentTraceStages, WithTheSwitchOffTheRenderSideTakesNoRequestStamp)
{
    StampGateScope off(false);
    const playback_present_trace::StageStamps s =
        playback_present_trace::stampsForRender(playback_present_trace::StageStamps(), 41, 42, 1.0015);
    ASSERT_NEAR(0.0, s.requestS, 0.0);
}

// r3 (adversary 81c5805c A1/A2, HUB RULING 2026-11-20T11:30Z): the LIGHT trace has no configure or H2D column, and no code
// path derives a per-frame span from the backend's last-run preupload status. Pinned at the three places that did.
TEST(PlaybackPresentTraceWiring, NoConfigureOrH2dSpanIsCapturedOrWrittenForTheLightTrace)
{
    const QString render = sourceOf(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    const QString window = mainWindowSource();
    const QString traceHeader = sourceOf(QStringLiteral("platform/qt/PlaybackPresentTrace.h"));
    for (const QString &banned : { QStringLiteral("traceStamps.configureMs"), QStringLiteral("traceStamps.h2dMs"),
                                   QStringLiteral("traceStamps.workerSplit"), QStringLiteral("traceRow.h2dMs"),
                                   QStringLiteral("traceRow.configureMs") })
    {
        ASSERT_FALSE(render.contains(banned));
        ASSERT_FALSE(window.contains(banned));
    }
    ASSERT_FALSE(traceHeader.contains(QStringLiteral("configureMs")));
    ASSERT_FALSE(traceHeader.contains(QStringLiteral("h2dMs")));
    ASSERT_FALSE(traceHeader.contains(QStringLiteral("workerSplit")));
}

TEST(PlaybackPresentTraceWiring, EveryStageHookIsOneGuardedStoreOnAKnownThread)
{
    const QString render = sourceOf(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    // request and decode-queue: queueDecodeRequestLocked (before the slot is handed to a worker)
    ASSERT_EQ(1, render.count(QStringLiteral("    playback_present_trace::stamp( slot.traceStamps.requestS, request.requestStageTime );\n")));
    ASSERT_EQ(1, render.count(QStringLiteral("    playback_present_trace::stamp( slot.traceStamps.decodeQueueS, decodeQueueStageTime );\n")));
    // decode worker, recon worker, render thread: each reuses a clock value the LIGHT path already holds
    ASSERT_EQ(1, render.count(QStringLiteral("    playback_present_trace::stamp( slot.traceStamps.decodeStartS, decodeStartStageTime );\n")));
    ASSERT_EQ(1, render.count(QStringLiteral("    playback_present_trace::stamp( slot.traceStamps.decodeEndS, decodeEndStageTime );\n")));
    ASSERT_EQ(1, render.count(QStringLiteral("    playback_present_trace::stamp( slot.traceStamps.reconStartS, reconStartStageTime );\n")));
    ASSERT_EQ(1, render.count(QStringLiteral("    playback_present_trace::stamp( slot.traceStamps.renderStartS, render_start );\n")));
    // the process stage's resetMetadata() must not wipe them, and only the frame they belong to may keep them
    ASSERT_EQ(1, render.count(QStringLiteral(
        "    slot.resetMetadata();\n"
        "    slot.traceStamps = preservedTraceStamps;\n")));
    // r8: through the one helper (behaviour pinned by AFrameNoWorkerQueuedStillCarriesItsOwnRequestStamp), fed this frame's
    // serial and its request clock value
    ASSERT_EQ(1, render.count(QStringLiteral(
        "        playback_present_trace::stampsForRender( slot.traceStamps,\n"
        "                                                 reconQueuedRequestSerial,\n"
        "                                                 m_activeFrameRequestSerial,\n"
        "                                                 m_activeFrameRequestStageTime );\n")));
    // the one clock read the LIGHT path did not have is taken only when the trace is on
    ASSERT_EQ(1, render.count(QStringLiteral(
        "    if( playback_present_trace::stampsEnabled() )\n"
        "        slot.traceStamps.reconEndS = mlv_stage_timing_now();\n")));
    // the slot's stamps ride to the GUI on the ReadyFrame, and a slot reuse clears them
    ASSERT_EQ(1, render.count(QStringLiteral("        frame->traceStamps = slot.traceStamps;\n")));
    const QString header = sourceOf(QStringLiteral("platform/qt/RenderFrameThread.h"));
    ASSERT_EQ(2, header.count(QStringLiteral("playback_present_trace::StageStamps traceStamps;")));
    ASSERT_EQ(1, header.count(QStringLiteral("            traceStamps = playback_present_trace::StageStamps();")));
    // no JSON insert, string or lock was added for the stamps
    ASSERT_FALSE(render.contains(QStringLiteral("traceStamps.requestS = ")));
}

TEST(PlaybackPresentTraceWiring, TheWindowSwitchesTheStageHooksWithTheRecorderAndReadsThemAtThePresent)
{
    const QString source = mainWindowSource();
    ASSERT_EQ(1, source.count(QStringLiteral(
        "    m_playbackPresentTrace.setEnabled( mlvappPlaybackPresentTraceRequested() );\n"
        "    playback_present_trace::setStampsEnabled( m_playbackPresentTrace.enabled() );\n")));
    // the GUI thread's draw-begin reuses display_start (already read), stored on the ReadyFrame
    ASSERT_EQ(1, source.count(QStringLiteral("    playback_present_trace::stamp( readyFrame.traceStamps.drawBeginS, display_start );\n")));
    // the present row takes the stamps and nothing else from the frame's telemetry (no configure/H2D: see the test above)
    ASSERT_EQ(1, source.count(QStringLiteral("            traceRow.setStages( readyFrame.traceStamps );\n")));
    // the dump walks the ring oldest-first and says how many rows the ring overwrote
    ASSERT_TRUE(source.contains(QStringLiteral("m_playbackPresentTrace.row( i )")));
    ASSERT_TRUE(source.contains(QStringLiteral("m_playbackPresentTrace.dropped()")));
}

// r10 (sol r9 A1, INV-1 "stage timestamps name the sampled event"): the rnd column is render_start, the clock value the
// render thread takes AFTER drawFrame's prologue and the same one HEAVY logs as render_thread_start_stage_time. The render
// column is (ready - render_start), so ready - rnd == render on EVERY row. A row that is not that is a mislabelled stamp.
TEST(PlaybackPresentTraceStages, ReadyMinusRndIsTheRenderWorkOnEveryRowOfAFixture)
{
    StampGateScope on(true);
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 16);
    // five frames: render_start (absolute stage-clock seconds), the render work in ms, then ready = render_start + work
    const double renderStartS[5] = { 1.012, 1.062, 1.101, 1.3333, 1.5 };
    const double renderWorkMs[5] = { 4.0, 4.0, 11.25, 0.5, 33.125 };
    for (int i = 0; i < 5; ++i)
    {
        playback_present_trace::StageStamps stamps = stagesFrom(renderStartS[i] - 0.021);
        stamps.renderStartS = renderStartS[i];
        Row row;
        row.frame = i + 1;
        row.presentMs = renderStartS[i] * 1000.0 + renderWorkMs[i] + 8.0;
        row.readyMs = renderStartS[i] * 1000.0 + renderWorkMs[i];
        row.renderMs = renderWorkMs[i];
        row.setStages(stamps);
        trace.notePresent(row);
    }
    ASSERT_EQ(5u, trace.size());
    for (size_t i = 0; i < trace.size(); ++i)
    {
        const Row &r = trace.row(i);
        ASSERT_NEAR(r.renderMs, r.readyMs - r.rndMs, 1.0e-6);
    }
    // the identity comes from these two reads and nothing else: render_start is stored where it is read, and the render work
    // is (ready emit - render_start), so neither clock read moved
    const QString render = sourceOf(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    ASSERT_EQ(1, render.count(QStringLiteral(
        "    const double render_start = mlv_stage_timing_now();\n"
        "    playback_present_trace::stamp( slot.traceStamps.renderStartS, render_start );\n")));
    ASSERT_EQ(1, render.count(QStringLiteral("    const double renderThreadWorkMs = (renderThreadEndStageTime - render_start) * 1000.0;\n")));
    ASSERT_EQ(1, render.count(QStringLiteral("    const double decodeQueueStageTime = mlv_stage_timing_now();\n    FrameSlot &slot = m_frameSlots[slotIndex];\n")));
    ASSERT_EQ(1, render.count(QStringLiteral("    request.requestStageTime = mlv_stage_timing_now();\n")));
}

// r10 (sol r9 A1): each column's documentation names the point actually stamped, so a reader never takes it for another
// event. The StageStamps field and the Row column of a stage carry the same statement. Nearest mechanical pin the header allows.
TEST(PlaybackPresentTraceWiring, EachStampColumnsDocNamesTheSiteThatStampsIt)
{
    const QString header = sourceOf(QStringLiteral("platform/qt/PlaybackPresentTrace.h"));
    const auto lineWith = [&header](const QString &needle) -> QString
    {
        const QStringList lines = header.split(QLatin1Char('\n'));
        QString found;
        int hits = 0;
        for (const QString &line : lines)
        {
            if (line.contains(needle)) { found = line; ++hits; }
        }
        return hits == 1 ? found : QString();
    };
    const QString reqField = lineWith(QStringLiteral("double requestS = 0.0;"));
    const QString dqField = lineWith(QStringLiteral("double decodeQueueS = 0.0;"));
    const QString rndField = lineWith(QStringLiteral("double renderStartS = 0.0;"));
    const QString reqColumn = lineWith(QStringLiteral("double reqMs = kUnknownMs;"));
    const QString dqColumn = lineWith(QStringLiteral("double dqMs = kUnknownMs;"));
    const QString rndColumn = lineWith(QStringLiteral("double rndMs = kUnknownMs;"));
    for (const QString &line : { reqField, dqField, rndField, reqColumn, dqColumn, rndColumn })
        ASSERT_FALSE(line.isEmpty());
    // req: after the m_mutex wait in renderFrame, where the request clock value is taken
    for (const QString &line : { reqField, reqColumn })
        ASSERT_TRUE(line.contains(QStringLiteral("after the m_mutex wait")));
    // dq: the decode-queue function-entry sample, taken before the slot is set up
    for (const QString &line : { dqField, dqColumn })
        ASSERT_TRUE(line.contains(QStringLiteral("decode-queue function-entry sample")));
    // rnd: render_start, after drawFrame's prologue; the value HEAVY logs as render_thread_start_stage_time
    for (const QString &line : { rndField, rndColumn })
    {
        ASSERT_TRUE(line.contains(QStringLiteral("render_start")));
        ASSERT_TRUE(line.contains(QStringLiteral("after drawFrame's prologue")));
    }
    ASSERT_TRUE(header.contains(QStringLiteral("render_thread_start_stage_time")));
    ASSERT_TRUE(header.contains(QStringLiteral("ready - rnd == render")));
}

TEST(PlaybackPresentTraceWiring, ReadOnceFromTheEnvironmentAndEveryHookIsGuarded)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE(source.contains(QStringLiteral(
        "static const bool requested = environmentFlagEnabled( \"MLVAPP_PLAYBACK_PRESENT_TRACE\" );")));
    ASSERT_EQ(1, source.count(QStringLiteral(
        "m_playbackPresentTrace.setEnabled( mlvappPlaybackPresentTraceRequested() );")));
    // the per-tick and per-present hooks test the cheap flag before reading the clock
    ASSERT_EQ(1, source.count(QStringLiteral(
        "    if( m_playbackPresentTrace.active() )\n"
        "        m_playbackPresentTrace.noteTick( mlv_stage_timing_now() * 1000.0, m_frameStillDrawing, predictivePlaybackAdvance );")));
    ASSERT_EQ(1, source.count(QStringLiteral(
        "    if( m_playbackPresentTrace.active() )\n"
        "        m_playbackPresentTrace.noteAdvance( mlv_stage_timing_now() * 1000.0, tracePositionBefore, m_newPosDropMode, timeDiff );")));
    ASSERT_EQ(1, source.count(QStringLiteral("        if( m_playbackPresentTrace.active() )\n        {\n            playback_present_trace::Row traceRow;")));
    // rows are written after the Play stops, never inside the timed window
    const int stop = source.indexOf(QStringLiteral("    m_playbackSmokeActive = false;\n    m_playbackPresentTrace.end();"));
    const int dump = source.indexOf(QStringLiteral("\"playback_smoke.present_trace session=%1 row=%2\""));
    ASSERT_TRUE(stop >= 0);
    ASSERT_TRUE(dump > stop);
}
