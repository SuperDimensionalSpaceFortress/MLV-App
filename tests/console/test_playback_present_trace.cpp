// PLAYBACK-STARTUP-CATCHUP-BURST-1 r7: the opt-in per-present playback trace (MLVAPP_PLAYBACK_PRESENT_TRACE=1). Off by
// default it must be inert -- no state, no allocation, no row -- so a product run pays one flag test per hook. On, it
// gives every presented frame one row with what the playback timer did since the previous present.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackPresentTrace.h"

#include <QFile>
#include <QString>
#include <QTextStream>

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
    ASSERT_EQ(static_cast<size_t>(0), trace.capacity());
}

TEST(PlaybackPresentTrace, SwitchedOffAgainItStaysInert)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.setEnabled(false);
    driveOnePlay(trace);
    ASSERT_TRUE(trace.empty());
    ASSERT_EQ(static_cast<size_t>(0), trace.capacity());
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
    // out of order is reported, not hidden
    Row bad = a;
    bad.reMs = 4.0;
    ASSERT_FALSE(bad.stagesOrdered());
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
    ASSERT_EQ(static_cast<size_t>(0), trace.capacity());
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
    ASSERT_EQ(1, render.count(QStringLiteral(
        "        reconQueuedRequestSerial == m_activeFrameRequestSerial\n"
        "            ? slot.traceStamps\n"
        "            : playback_present_trace::StageStamps();\n")));
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
