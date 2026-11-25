// PLAYBACK-STARTUP-CATCHUP-BURST-1 r7: the opt-in per-present playback trace (MLVAPP_PLAYBACK_PRESENT_TRACE=1). Off by
// default it must be inert -- no state, no allocation, no row -- so a product run pays one flag test per hook. On, it
// gives every presented frame one row with what the playback timer did since the previous present.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackPresentTrace.h"
#include "../../platform/qt/PlaybackFpsMeterPolicy.h"
#include "../../platform/qt/PlaybackFrameRange.h"

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
    trace.noteAdvance(1001.5, 1.0, 42);   // r7: the applied grant (0 -> 1)
    Row first;
    first.frame = 1;
    first.position = 1;
    first.presentMs = 1010.0;
    first.readyMs = 1005.0;
    trace.notePresent(first);
    trace.notePresentEnd(1012.0);
    trace.noteTick(1020.0, true, false);
    trace.noteTick(1028.0, false, true);
    trace.noteAdvance(1028.5, 0.4, 42);   // 1.0 -> 1.4
    trace.noteTick(1036.0, false, false);
    trace.noteAdvance(1036.5, 0.6, 8);    // 1.4 -> 2.0
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

QString mainWindowSource()
{
    const QString path = repo_file_path(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

} // namespace

TEST(PlaybackPresentTrace, OffByDefaultItRecordsAndAllocatesNothing)
{
    Recorder trace;
    ASSERT_FALSE(trace.enabled());
    driveOnePlay(trace);
    ASSERT_FALSE(trace.active());
    ASSERT_TRUE(trace.rows().empty());
    ASSERT_EQ(static_cast<size_t>(0), trace.rows().capacity());
}

TEST(PlaybackPresentTrace, SwitchedOffAgainItStaysInert)
{
    Recorder trace;
    trace.setEnabled(true);
    trace.setEnabled(false);
    driveOnePlay(trace);
    ASSERT_TRUE(trace.rows().empty());
    ASSERT_EQ(static_cast<size_t>(0), trace.rows().capacity());
}

TEST(PlaybackPresentTrace, OnEveryPresentGetsARowWithTheTimerSinceThePreviousPresent)
{
    Recorder trace;
    trace.setEnabled(true);
    driveOnePlay(trace);
    ASSERT_FALSE(trace.active());   // end() stops recording; the rows stay for the log
    ASSERT_EQ(static_cast<size_t>(2), trace.rows().size());
    const Row &a = trace.rows()[0];
    ASSERT_NEAR(10.0, a.presentMs, 1e-9);
    ASSERT_NEAR(5.0, a.readyMs, 1e-9);
    ASSERT_NEAR(0.0, a.intervalMs, 1e-9);
    ASSERT_EQ(1, a.ticks);
    ASSERT_EQ(1, a.advances);
    ASSERT_NEAR(1.5, a.lastAdvanceMs, 1e-9);
    ASSERT_NEAR(-1.0, a.prevEndMs, 1e-9);
    const Row &b = trace.rows()[1];
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
    trace.noteAdvance(5008.5, 0.0, 8);   // granted nothing (7.0 -> 7.0): a hold
    Row r;
    r.presentMs = 5020.0;
    trace.notePresent(r);
    ASSERT_EQ(static_cast<size_t>(1), trace.rows().size());
    ASSERT_EQ(0, trace.rows()[0].advances);
    ASSERT_NEAR(-1.0, trace.rows()[0].lastAdvanceMs, 1e-9);
    ASSERT_NEAR(0.0, trace.rows()[0].intervalMs, 1e-9);
}

// PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1 r7 (sol r6 blocker on 9f3f5ebd): the trace decided "advanced" from equal endpoints,
// so a capped Drop Frame tick whose whole laps land back on its start lost its advance and its shaped interval. Sol's
// literal repro: 24 fps, Drop Frame + Loop, cut 1..616, start 0, no pending advance, an unpaced (or fully credited) grant,
// a 41666300 ms shaped tick. The cap asks for 41666250 ms = 999990 frames, which is 1626 laps of 615: position 0 -> 0.
TEST(PlaybackPresentTrace, AWholeLapDropFrameTickIsAnAdvanceWithItsShapedInterval)
{
    const int shapedTickMs = 41666300;
    const int cappedMs = playback_fps_meter::frameBoundedTickTimeDiffMs(shapedTickMs, 24.0);
    ASSERT_EQ(41666250, cappedMs);
    const double request = playback_fps_meter::dropFrameRequestFrames(24.0, cappedMs);
    ASSERT_NEAR(999990.0, request, 0.0);
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(0.0, request, 1, 616, true, false);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_EQ(615, tick.wrapWidth);
    ASSERT_EQ(1626, tick.wrapLaps);
    ASSERT_NEAR(0.0, tick.position, 0.0);   // the endpoints are equal
    // MainWindow's grantedDropFrames: the advance the tick applied
    const double granted = tick.position - 0.0 + tick.foldedFrames + static_cast<double>(tick.wrapWidth) * tick.wrapLaps;
    ASSERT_NEAR(999990.0, granted, 0.0);

    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 4);
    trace.noteTick(1008.0, false, false);
    trace.noteAdvance(1008.5, granted, shapedTickMs);   // 0 -> tick.position (0), 999990 granted
    Row r;
    r.presentMs = 1020.0;
    trace.notePresent(r);
    ASSERT_EQ(static_cast<size_t>(1), trace.rows().size());
    ASSERT_EQ(1, trace.rows()[0].advances);
    ASSERT_NEAR(8.5, trace.rows()[0].lastAdvanceMs, 1e-9);
    ASSERT_NEAR(41666300.0, trace.rows()[0].lastTickDiffMs, 0.0);   // the shaped interval, never the capped request
    ASSERT_NEAR(999990.0, trace.rows()[0].advancedFrames, 0.0);     // "the source frames those advances granted"
}

// r7 (sol r6 INV-5 row 2): equal endpoints with no grant stay a hold, the same repro shape with a zero request.
TEST(PlaybackPresentTrace, AZeroGrantOnEqualEndpointsIsStillAHold)
{
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(0.0, 0.0, 1, 616, true, false);
    ASSERT_FALSE(tick.wrapped);
    ASSERT_NEAR(0.0, tick.position, 0.0);
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 4);
    trace.noteTick(1008.0, false, false);
    trace.noteAdvance(1008.5, 0.0, 8);   // 0 -> tick.position (0), nothing granted
    Row r;
    r.presentMs = 1020.0;
    trace.notePresent(r);
    ASSERT_EQ(static_cast<size_t>(1), trace.rows().size());
    ASSERT_EQ(0, trace.rows()[0].advances);
    ASSERT_NEAR(-1.0, trace.rows()[0].lastAdvanceMs, 1e-9);
    ASSERT_EQ(1, trace.rows()[0].ticks);   // the tick ran; it only granted nothing
    ASSERT_NEAR(0.0, trace.rows()[0].advancedFrames, 0.0);
}

// r7 iteration 2 (adversary pass 13 A1 on 80079007): a stale start past the lap limit is folded into the cut with zero
// laps (r6), so a zero-grant tick moves the drop position 1100000 -> 0 without travelling a frame. The trace fell back to
// the endpoint displacement and logged adv=1, adv_frames=-1100000. The fold's stale distance is never an advance.
TEST(PlaybackPresentTrace, AFoldedStaleStartWithNoGrantIsNotAnAdvance)
{
    const playback_frame_range::DropFrameTickResult tick =
        playback_frame_range::advanceDropFrameTick(1100000.0, 0.0, 1, 2, true, false);
    ASSERT_TRUE(tick.wrapped);
    ASSERT_EQ(0, tick.wrapLaps);
    ASSERT_NEAR(0.0, tick.position, 0.0);
    ASSERT_NEAR(1100000.0, tick.foldedFrames, 0.0);
    const double granted = tick.position - 1100000.0 + tick.foldedFrames + static_cast<double>(tick.wrapWidth) * tick.wrapLaps;
    ASSERT_NEAR(0.0, granted, 0.0);
    Recorder trace;
    trace.setEnabled(true);
    trace.begin(1000.0, 4);
    trace.noteTick(1008.0, false, false);
    trace.noteAdvance(1008.5, granted, 1);   // 1100000 -> tick.position (0), nothing granted
    Row r;
    r.presentMs = 1020.0;
    trace.notePresent(r);
    ASSERT_EQ(static_cast<size_t>(1), trace.rows().size());
    ASSERT_EQ(0, trace.rows()[0].advances);
    ASSERT_NEAR(-1.0, trace.rows()[0].lastAdvanceMs, 1e-9);
    ASSERT_NEAR(0.0, trace.rows()[0].advancedFrames, 0.0);
}

// PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1 r8 (sol r7 blocker on 75ed5094, DC-06 catalogue row "a non-Loop clamp correction is
// not an advance"): during Play, Drop Frame off, seek 100 -> 10, cut 1..50, Drop Frame back on before the next tick, Loop
// off. The slider handler leaves the Drop Frame position at 100 and the normalizer keeps it (it is inside the clip); the
// slider at 10 bypasses the outer last-frame branch, and the non-Loop clamp returns 49 with no fold and no lap. 75ed5094's
// grantedDropFrames (position - before + foldedFrames + wrapWidth * wrapLaps) read that correction as -51 frames of travel
// and the trace logged adv=1, adv_frames=-51 and moved last_adv / last_tick_diff. A correction with zero grant is no
// advance: the advance fields keep the previous real advance, for a zero request and for an ordinary 8 ms request.
TEST(PlaybackPresentTrace, AClampedStaleNonLoopStartWithNoGrantIsNotAnAdvance)
{
    ASSERT_FALSE(playback_frame_range::outerLastFrameBranchHandlesTick(10, 50, true, false, false));
    playback_frame_range::CutRange cut;
    cut.cutIn = 1;
    cut.cutOut = 50;
    cut.valid = true;
    ASSERT_NEAR(100.0, playback_frame_range::normalizeDropFramePosition(100.0, cut, 200, false), 0.0);
    const double eightMsRequest = playback_fps_meter::dropFrameRequestFrames(24.0, 8);
    ASSERT_NEAR(0.192, eightMsRequest, 1e-12);
    const double requests[] = { 0.0, eightMsRequest };
    for (const double request : requests)
    {
        const playback_frame_range::DropFrameTickResult tick =
            playback_frame_range::advanceDropFrameTick(100.0, request, 1, 50, false, false);
        ASSERT_FALSE(tick.wrapped);
        ASSERT_EQ(0, tick.wrapLaps);
        ASSERT_NEAR(0.0, tick.foldedFrames, 0.0);
        ASSERT_NEAR(49.0, tick.position, 0.0);
        // the value 75ed5094 handed the trace (its grantedDropFrames had no clamp term)
        const double reconstructed = tick.position - 100.0 + tick.foldedFrames
            + static_cast<double>(tick.wrapWidth) * tick.wrapLaps;
        ASSERT_NEAR(-51.0, reconstructed, 0.0);
        Recorder trace;
        trace.setEnabled(true);
        trace.begin(1000.0, 4);
        trace.noteTick(1008.0, false, false);
        trace.noteAdvance(1008.5, 1.0, 42);            // a real advance first
        trace.noteTick(1016.0, false, false);
        trace.noteAdvance(1016.5, reconstructed, 8);   // 100 -> 49: the clamp's correction, nothing granted
        Row r;
        r.presentMs = 1020.0;
        trace.notePresent(r);
        ASSERT_EQ(static_cast<size_t>(1), trace.rows().size());
        ASSERT_EQ(2, trace.rows()[0].ticks);
        ASSERT_EQ(1, trace.rows()[0].advances);
        ASSERT_NEAR(1.0, trace.rows()[0].advancedFrames, 0.0);
        ASSERT_NEAR(8.5, trace.rows()[0].lastAdvanceMs, 1e-9);
        ASSERT_NEAR(42.0, trace.rows()[0].lastTickDiffMs, 0.0);
    }
}

TEST(PlaybackPresentTrace, ARowHasOneFieldPerHeaderColumn)
{
    Recorder trace;
    trace.setEnabled(true);
    driveOnePlay(trace);
    const std::string header = Recorder::header();
    const std::string row = Recorder::formatRow(1, trace.rows()[1]);
    const auto commas = [](const std::string &s) { size_t n = 0; for (char c : s) n += c == ',' ? 1 : 0; return n; };
    ASSERT_EQ(commas(header), commas(row));
    ASSERT_EQ(0u, row.find("1,2,2,51.700,41.700,-1.000,"));
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
        "        m_playbackPresentTrace.noteAdvance( mlv_stage_timing_now() * 1000.0, m_playbackTickAppliedFrames, shapedTickMs );")));   // r7
    ASSERT_EQ(1, source.count(QStringLiteral("        if( m_playbackPresentTrace.active() )\n        {\n            playback_present_trace::Row traceRow;")));
    // rows are written after the Play stops, never inside the timed window
    const int stop = source.indexOf(QStringLiteral("    m_playbackSmokeActive = false;\n    m_playbackPresentTrace.end();"));
    const int dump = source.indexOf(QStringLiteral("\"playback_smoke.present_trace session=%1 row=%2\""));
    ASSERT_TRUE(stop >= 0);
    ASSERT_TRUE(dump > stop);
}

// r7 (sol r6 blocker on 9f3f5ebd): the trace is handed the tick's APPLIED grant, which playbackHandling clears on entry and
// sets on each of its three grant paths (the outer Loop wrap, the Normal-mode whole frame, the Drop Frame tick), so an
// advance is never inferred from equal endpoints alone.
TEST(PlaybackPresentTraceWiring, TheAdvanceIsTheTicksAppliedGrantNotItsEndpoints)
{
    const QString source = mainWindowSource();
    ASSERT_EQ(1, source.count(QStringLiteral(
        "    if( m_playbackPresentTrace.active() )\n"
        "        m_playbackPresentTrace.noteAdvance( mlv_stage_timing_now() * 1000.0, m_playbackTickAppliedFrames, shapedTickMs );")));
    // r7 iteration 2 (pass 13 A1 on 80079007): the endpoints are not passed at all, so no fallback can infer an advance
    ASSERT_FALSE(source.contains(QStringLiteral("tracePositionBefore")));
    const int handlingAt = source.indexOf(QStringLiteral("void MainWindow::playbackHandling(int timeDiff)\n{\n"));
    ASSERT_TRUE(handlingAt >= 0);
    const int handlingEnd = source.indexOf(QStringLiteral("void MainWindow::showPerformanceProfilingDialog"), handlingAt);
    ASSERT_TRUE(handlingEnd > handlingAt);
    const QString handling = source.mid(handlingAt, handlingEnd - handlingAt);
    // cleared first, before any early return
    ASSERT_TRUE(handling.contains(QStringLiteral(
        "gui_present_gap::OtherBusySpan::PlaybackHandling );\n    m_playbackTickAppliedFrames = 0.0;   // r7 (sol r6)")));
    ASSERT_EQ(4, handling.count(QStringLiteral("m_playbackTickAppliedFrames = ")));
    const int wrapGrantAt = handling.indexOf(QStringLiteral(
        "if( !m_playbackPaceGuard.grantLoopWrap( paceNowMs, playbackPaceGuardFps() ) ) return;"));
    const int wrapSetAt = handling.indexOf(QStringLiteral("m_playbackTickAppliedFrames = 1.0;"));
    const int wholeGrantAt = handling.indexOf(QStringLiteral(
        "if( !m_playbackPaceGuard.grantWholeFrame( paceNowMs, playbackPaceGuardFps() ) ) return;"));
    const int wholeSetAt = handling.indexOf(QStringLiteral(
        "m_playbackTickAppliedFrames = ui->horizontalSliderPosition->value() - sourcePositionBeforeTick;"));
    const int dropGrantedAt = handling.indexOf(QStringLiteral("const double grantedDropFrames = "));
    const int dropSetAt = handling.indexOf(QStringLiteral("m_playbackTickAppliedFrames = grantedDropFrames;"));
    ASSERT_TRUE(wrapGrantAt >= 0);
    ASSERT_TRUE(wrapSetAt > wrapGrantAt);
    ASSERT_TRUE(wholeGrantAt > wrapSetAt);
    ASSERT_TRUE(wholeSetAt > wholeGrantAt);
    ASSERT_TRUE(dropGrantedAt > wholeSetAt);
    ASSERT_TRUE(dropSetAt > dropGrantedAt);
    // r7 iteration 2 (pass 13 A3 on 80079007): each store is pinned with the line above it and its own indentation, so an
    // `if( false )` in front of it (on its line or the line before) breaks the pin. An enclosing dead block is the open
    // dead-block hardening row (sol r6).
    ASSERT_EQ(1, handling.count(QStringLiteral(
        "or leaves m_newPosDropMode where it was (no audio output).\n"
        "                m_playbackTickAppliedFrames = 1.0;\n")));
    ASSERT_EQ(1, handling.count(QStringLiteral(
        "whatever m_newPosDropMode held before\n"
        "                    m_playbackTickAppliedFrames = ui->horizontalSliderPosition->value() - sourcePositionBeforeTick;\n")));
    ASSERT_EQ(1, handling.count(QStringLiteral(
        "including whole laps that land back on the start\n"
        "                m_playbackTickAppliedFrames = grantedDropFrames;\n")));
}

// r8 (sol r7 blocker on 75ed5094): the Drop Frame grant (the trace's advance and the slip histogram's granted share) adds
// back the distance the non-Loop clamp moved a stale start past the cut's last frame onto it, as it adds back a Loop fold,
// so a clamp correction with no grant is 0 frames, never a negative advance.
TEST(PlaybackPresentTraceWiring, TheDropFrameGrantAddsBackTheNonLoopClampCorrection)
{
    const QString source = mainWindowSource();
    ASSERT_EQ(1, source.count(QStringLiteral(
        "                const double grantedDropFrames = dropFrameTick.position - sourcePositionBeforeDropTick + dropFrameTick.foldedFrames\n"
        "                    + dropFrameTick.clampedFrames + static_cast<double>( dropFrameTick.wrapWidth ) * dropFrameTick.wrapLaps;\n")));
}
