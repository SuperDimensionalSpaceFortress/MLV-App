// Wiring test: the status fps is a draw-to-draw meter. The idle poll ticks between two draws
// (8 ms poll vs a ~42 ms frame period at 24 fps) must not restart it: they used to re-arm
// lastTime, zero the average and reset the update throttle, so the label read the poll period
// (~90 fps) at any rate. MainWindow.cpp needs a full GUI build (not linked into console_tests),
// so this reads the source as text (mirrors test_playback_display_wake_wiring.cpp).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QTextStream>

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString functionBody(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

QString timerFrameEventBody()
{
    return functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
        QStringLiteral("void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )"),
        QStringLiteral("void MainWindow::timerEvent(QTimerEvent *t)"));
}

} // namespace

TEST(PlaybackFpsMeterWiring, FpsMeterMeasuresDrawToDrawAndIdleTicksDoNotRestartIt)
{
    const QString body = timerFrameEventBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("static QElapsedTimer lastDrawTime;")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "const int measuredFrameMs = playback_fps_meter::stampAgeMs( lastDrawTime, nowTime );")));
    ASSERT_TRUE(body.contains(QStringLiteral("playback_fps_meter::smoothedFrameMs(")));
    // the old measurement off the idle-re-armed lastTime is gone
    ASSERT_FALSE(body.contains(QStringLiteral("const int measuredFrameMs = lastTime.msecsTo( nowTime );")));
    // the reset policy lives in one pure function, called with the real play state and draw age
    // (exact text: a `true ||` or dropped operand cannot slip through)
    const int lambdaAt = body.indexOf(QStringLiteral("const auto resetFpsMeterIfIdle = [this]( const QElapsedTimer & now )"));
    ASSERT_TRUE(lambdaAt >= 0);
    const QString lambda = body.mid(lambdaAt);
    const int policyAt = lambda.indexOf(QStringLiteral(
        "playback_fps_meter::fpsMeterShouldReset( ui->actionPlay->isChecked(), hasDraw,\n"
        "                                                     hasDraw ? playback_fps_meter::stampAgeMs( lastDrawTime, now ) : 0 )"));
    const int zeroAt = lambda.indexOf(QStringLiteral("m_playbackFpsEmaFrameMs = 0.0;"));
    ASSERT_TRUE(policyAt >= 0);
    ASSERT_TRUE(zeroAt > policyAt); // the reset sits inside the guarded block, not before it
    // the lambda must not touch lastTime (drop-frame pacing)
    const int lambdaEnd = lambda.indexOf(QStringLiteral("if( m_frameStillDrawing )"));
    ASSERT_TRUE(lambdaEnd > zeroAt);
    ASSERT_FALSE(lambda.left(lambdaEnd).contains(QStringLiteral("lastTime =")));
}

// sol r1 BLOCKER: each 8 ms tick used to return through the m_frameStillDrawing branch before the
// idle reset, so a stalled render or a pause left the old fps on screen. The reset must run on the
// still-drawing early-return path too, BEFORE its returns.
TEST(PlaybackFpsMeterWiring, FpsMeterResetRunsOnTheStillDrawingEarlyReturnPath)
{
    const QString body = timerFrameEventBody();
    ASSERT_FALSE(body.isEmpty());
    const int busyAt = body.indexOf(QStringLiteral("if( m_frameStillDrawing )"));
    const int busyEnd = body.indexOf(QStringLiteral("const bool hadPendingAdvance"));
    ASSERT_TRUE(busyAt >= 0);
    ASSERT_TRUE(busyEnd > busyAt);
    const QString busy = body.mid(busyAt, busyEnd - busyAt);
    const int callAt = busy.indexOf(QStringLiteral("resetFpsMeterIfIdle( mlvappMonotonicNow() );"));
    const int firstReturnAt = busy.indexOf(QStringLiteral("return;"));
    ASSERT_TRUE(callAt >= 0);
    ASSERT_TRUE(firstReturnAt > callAt); // evaluated before either early return
    // ...and the idle branch still evaluates it with the tick's own time
    const int idleAt = body.indexOf(QStringLiteral("timer_frame.idle"));
    ASSERT_TRUE(idleAt >= 0);
    ASSERT_TRUE(body.mid(idleAt).contains(QStringLiteral("resetFpsMeterIfIdle( nowTime );")));
}

// PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1: the meter, stall and drop-frame tick ages were taken off the wall
// clock (time of day, msecsTo kept inside +-86,400,000 ms). Across midnight an age read about -86,399,970,
// across a DST end about -3,600,000: the status fps froze, a stall never zeroed it, and the unpaced
// automation path asked for about -2 M frames. Every age now comes off a monotonic QElapsedTimer stamp
// through playback_fps_meter::stampAgeMs (0 for a never-started stamp, saturated to int).
TEST(PlaybackFpsMeterWiring, MeterAndTickClocksAreMonotonic)
{
    const QString body = timerFrameEventBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_FALSE(body.contains(QRegularExpression(QStringLiteral("\\bQTime\\b"))));
    ASSERT_TRUE(body.contains(QStringLiteral("static QElapsedTimer lastTime;")));
    ASSERT_TRUE(body.contains(QStringLiteral("static QElapsedTimer lastDrawTime;")));
    ASSERT_FALSE(body.contains(QStringLiteral(".msecsTo(")));
    ASSERT_TRUE(body.contains(QStringLiteral("playback_fps_meter::stampAgeMs( lastTime, nowTime )")));
    ASSERT_TRUE(body.contains(QStringLiteral("playback_fps_meter::stampAgeMs( lastDrawTime, nowTime )")));
    ASSERT_TRUE(body.contains(QStringLiteral("playback_fps_meter::stampAgeMs( lastDrawTime, now )")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "playback_fps_meter::stampAgeMs( m_lastPlaybackFpsStatusUpdateTime, nowTime ) >= 250")));
    // the tick and the busy-path reset read the monotonic clock, never the time of day
    ASSERT_TRUE(body.contains(QStringLiteral("QElapsedTimer nowTime = mlvappMonotonicNow();")));
    ASSERT_TRUE(body.contains(QStringLiteral("lastTime.start();")));

    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("QElapsedTimer m_lastPlaybackFpsStatusUpdateTime;")));
    ASSERT_FALSE(header.contains(QStringLiteral("QTime m_lastPlaybackFpsStatusUpdateTime;")));
}

// adversary r1 A6 (c7c88bf8): the pins above passed a mlvappMonotonicNow() that never starts its timer (every
// stamp invalid, every age 0), a timeDiff overwritten after its stampAgeMs assignment with a difference of
// QDateTime::currentMSecsSinceEpoch() samples, an unconditional (`true ||`) reset and a dead busy-path reset call.
TEST(PlaybackFpsMeterWiring, MonotonicClockAndResetWiringRejectTheirMutations)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_FALSE(source.isEmpty());
    // the clock helper, exactly: a started QElapsedTimer and nothing else
    ASSERT_TRUE(source.contains(QStringLiteral(
        "static QElapsedTimer mlvappMonotonicNow()\n"
        "{\n"
        "    QElapsedTimer now;\n"
        "    now.start();\n"
        "    return now;\n"
        "}\n")));
    ASSERT_EQ(1, source.count(QStringLiteral("static QElapsedTimer mlvappMonotonicNow()")));

    const QString body = timerFrameEventBody();
    ASSERT_FALSE(body.isEmpty());
    // no other clock reaches the tick
    ASSERT_FALSE(body.contains(QStringLiteral("QDateTime")));
    ASSERT_FALSE(body.contains(QStringLiteral("currentMSecsSinceEpoch")));
    ASSERT_FALSE(body.contains(QStringLiteral("currentTime")));
    // the age reaches the shaping call untouched: no other timeDiff write between the two
    const QString ageLine = QStringLiteral("timeDiff = playback_fps_meter::stampAgeMs( lastTime, nowTime );");
    const int ageAt = body.indexOf(ageLine);
    const int shapedAt = body.indexOf(QStringLiteral("timeDiff = playback_native_pace::shapedTickTimeDiffMs( timeDiff,"));
    ASSERT_TRUE(ageAt >= 0);
    ASSERT_EQ(1, body.count(ageLine));
    ASSERT_TRUE(shapedAt > ageAt);
    ASSERT_FALSE(body.mid(ageAt + ageLine.size(), shapedAt - (ageAt + ageLine.size())).contains(QStringLiteral("timeDiff")));
    // the tick stamp is taken just before the age, from the helper
    ASSERT_TRUE(body.contains(QStringLiteral(
        "    QElapsedTimer nowTime = mlvappMonotonicNow();\n"
        "    timeDiff = playback_fps_meter::stampAgeMs( lastTime, nowTime );\n")));
    // the reset is guarded by the policy itself (no `true ||` in front of it)
    ASSERT_TRUE(body.contains(QStringLiteral(
        "        if( playback_fps_meter::fpsMeterShouldReset( ui->actionPlay->isChecked(), hasDraw,\n")));
    // the busy-path reset is a plain statement right after the trace block, not behind a dead condition
    ASSERT_TRUE(body.contains(QStringLiteral(
        "                    true );\n"
        "            }\n"
        "            resetFpsMeterIfIdle( mlvappMonotonicNow() );\n"
        "            //On setup slider priority\n")));
    // adversary r2 A1 (894b938d): the shaped tick is bounded in frames before playbackHandling reads it, exactly once.
    // r4 (pass 5 A2 on b70f718d): only the Drop Frame request reads the tick as frames, so Normal mode passes no rate and
    // keeps its whole tick (the fps meter's `timeDiff != 0` gate stays open at any source rate, as on the base).
    const QString boundLine = QStringLiteral(
        "    timeDiff = playback_fps_meter::frameBoundedTickTimeDiffMs( timeDiff, ui->actionDropFrameMode->isChecked() ? getPlaybackFramerate() : 0.0 );\n");
    const int boundAt = body.indexOf(boundLine);
    const int handlingAt = body.indexOf(QStringLiteral("    playbackHandling( timeDiff );"));
    ASSERT_TRUE(boundAt > shapedAt);
    ASSERT_EQ(1, body.count(boundLine));
    ASSERT_TRUE(handlingAt > boundAt);
    ASSERT_FALSE(body.mid(boundAt + boundLine.size(), handlingAt - (boundAt + boundLine.size())).contains(QStringLiteral("timeDiff")));
    // ...and the idle-branch reset likewise runs on every idle tick, right before the re-arm
    ASSERT_TRUE(body.contains(QStringLiteral(
        "        resetFpsMeterIfIdle( nowTime );\n"
        "        lastTime.start(); //do that for calculation of timeDiff for DropFrameMode;\n")));
}

// adversary r2 A7 (3bb01140): the cap pin accepted a dead `if( false )` line in front of the cap, and the reset pin
// accepted `|| true` after the policy call (both pins stopped short of the statement's end). Each pin now holds the
// whole statement and its neighbours contiguously, and each rejects its mutation applied in memory to the real source.
namespace
{
bool frameCapIsLive(const QString & body)
{
    return body.contains(QStringLiteral(
        "ui->actionPlay->isChecked(), hadPendingAdvance );\n"
        "    // PLAYBACK-FPS-METER-MONOTONIC-CLOCK-1 (adversary r2 A1): one tick never asks the drop-frame loop for more frames than\n"
        "    // it can count (a long suspension at a high source rate); every ordinary tick is unchanged.\n"
        "    // r4 (pass 5 A2): only the Drop Frame request reads the tick as frames, so Normal mode keeps its whole tick (fps meter).\n"
        "    timeDiff = playback_fps_meter::frameBoundedTickTimeDiffMs( timeDiff, ui->actionDropFrameMode->isChecked() ? getPlaybackFramerate() : 0.0 );\n"
        "\n"
        "    //Playback\n"));
}

bool fpsResetIsConditional(const QString & body)
{
    return body.contains(QStringLiteral(
        "        const bool hasDraw = lastDrawTime.isValid();\n"
        "        if( playback_fps_meter::fpsMeterShouldReset( ui->actionPlay->isChecked(), hasDraw,\n")
        + QString(53, QLatin1Char(' '))
        + QStringLiteral(
        "hasDraw ? playback_fps_meter::stampAgeMs( lastDrawTime, now ) : 0 ) )\n"
        "        {\n"
        "            const QString playbackFpsText = playback_conform::playbackFpsStatusText(\n"));
}
} // namespace

TEST(PlaybackFpsMeterWiring, CapAndResetPinsRejectADeadCapAndAnUnconditionalReset)
{
    const QString body = timerFrameEventBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(frameCapIsLive(body));
    ASSERT_TRUE(fpsResetIsConditional(body));

    const QString capLine = QStringLiteral(
        "    timeDiff = playback_fps_meter::frameBoundedTickTimeDiffMs( timeDiff, ui->actionDropFrameMode->isChecked() ? getPlaybackFramerate() : 0.0 );\n");
    ASSERT_EQ(1, body.count(capLine));
    QString deadCap = body;
    deadCap.replace(capLine, QStringLiteral("    if( false )\n") + capLine);
    ASSERT_FALSE(frameCapIsLive(deadCap));

    const QString resetTail = QStringLiteral("stampAgeMs( lastDrawTime, now ) : 0 ) )\n");
    ASSERT_EQ(1, body.count(resetTail));
    QString alwaysReset = body;
    alwaysReset.replace(resetTail, QStringLiteral("stampAgeMs( lastDrawTime, now ) : 0 ) || true )\n"));
    ASSERT_FALSE(fpsResetIsConditional(alwaysReset));
    QString alwaysResetFront = body;
    alwaysResetFront.replace(QStringLiteral("if( playback_fps_meter::fpsMeterShouldReset("),
                             QStringLiteral("if( true || playback_fps_meter::fpsMeterShouldReset("));
    ASSERT_FALSE(fpsResetIsConditional(alwaysResetFront));
}
