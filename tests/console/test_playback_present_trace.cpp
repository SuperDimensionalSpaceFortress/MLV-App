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
    trace.noteAdvance(5008.5, 7.0, 7.0, 8);   // granted nothing: a hold
    Row r;
    r.presentMs = 5020.0;
    trace.notePresent(r);
    ASSERT_EQ(static_cast<size_t>(1), trace.rows().size());
    ASSERT_EQ(0, trace.rows()[0].advances);
    ASSERT_NEAR(-1.0, trace.rows()[0].lastAdvanceMs, 1e-9);
    ASSERT_NEAR(0.0, trace.rows()[0].intervalMs, 1e-9);
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
        "        m_playbackPresentTrace.noteAdvance( mlv_stage_timing_now() * 1000.0, tracePositionBefore, m_newPosDropMode, timeDiff );")));
    ASSERT_EQ(1, source.count(QStringLiteral("        if( m_playbackPresentTrace.active() )\n        {\n            playback_present_trace::Row traceRow;")));
    // rows are written after the Play stops, never inside the timed window
    const int stop = source.indexOf(QStringLiteral("    m_playbackSmokeActive = false;\n    m_playbackPresentTrace.end();"));
    const int dump = source.indexOf(QStringLiteral("\"playback_smoke.present_trace session=%1 row=%2\""));
    ASSERT_TRUE(stop >= 0);
    ASSERT_TRUE(dump > stop);
}
