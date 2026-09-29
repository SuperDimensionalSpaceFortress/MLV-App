// Wiring test: the status fps is a draw-to-draw meter. The idle poll ticks between two draws
// (8 ms poll vs a ~42 ms frame period at 24 fps) must not restart it: they used to re-arm
// lastTime, zero the average and reset the update throttle, so the label read the poll period
// (~90 fps) at any rate. MainWindow.cpp needs a full GUI build (not linked into console_tests),
// so this reads the source as text (mirrors test_playback_display_wake_wiring.cpp).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
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
    ASSERT_TRUE(body.contains(QStringLiteral("static QTime lastDrawTime;")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "const int measuredFrameMs = lastDrawTime.isValid() ? lastDrawTime.msecsTo( nowTime ) : 0;")));
    ASSERT_TRUE(body.contains(QStringLiteral("playback_fps_meter::smoothedFrameMs(")));
    // the old measurement off the idle-re-armed lastTime is gone
    ASSERT_FALSE(body.contains(QStringLiteral("const int measuredFrameMs = lastTime.msecsTo( nowTime );")));
    // the reset policy lives in one pure function, called with the real play state and draw age
    // (exact text: a `true ||` or dropped operand cannot slip through)
    const int lambdaAt = body.indexOf(QStringLiteral("const auto resetFpsMeterIfIdle = [this]( const QTime & now )"));
    ASSERT_TRUE(lambdaAt >= 0);
    const QString lambda = body.mid(lambdaAt);
    const int policyAt = lambda.indexOf(QStringLiteral(
        "playback_fps_meter::fpsMeterShouldReset( ui->actionPlay->isChecked(), hasDraw,\n"
        "                                                     hasDraw ? lastDrawTime.msecsTo( now ) : 0 )"));
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
    const int callAt = busy.indexOf(QStringLiteral("resetFpsMeterIfIdle( QTime::currentTime() );"));
    const int firstReturnAt = busy.indexOf(QStringLiteral("return;"));
    ASSERT_TRUE(callAt >= 0);
    ASSERT_TRUE(firstReturnAt > callAt); // evaluated before either early return
    // ...and the idle branch still evaluates it with the tick's own time
    const int idleAt = body.indexOf(QStringLiteral("timer_frame.idle"));
    ASSERT_TRUE(idleAt >= 0);
    ASSERT_TRUE(body.mid(idleAt).contains(QStringLiteral("resetFpsMeterIfIdle( nowTime );")));
}
