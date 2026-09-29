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
    // the idle branch zeroes the meter only when paused, before the first draw, or stalled
    const int idleAt = body.indexOf(QStringLiteral("timer_frame.idle"));
    ASSERT_TRUE(idleAt >= 0);
    const QString idle = body.mid(idleAt);
    const int guardAt = idle.indexOf(QStringLiteral("playback_fps_meter::fpsMeterStalled("));
    const int resetAt = idle.indexOf(QStringLiteral("m_playbackFpsEmaFrameMs = 0.0;"));
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(resetAt > guardAt); // the reset sits inside the guarded block, not before it
    ASSERT_TRUE(idle.contains(QStringLiteral("!ui->actionPlay->isChecked()")));
}
