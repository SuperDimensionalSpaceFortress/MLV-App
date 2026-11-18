// PLAYER-FULLSCREEN-KEEPS-PLAYING-1: MainWindow::resizeEvent unchecked Play on every resize, so toggling full screen,
// maximizing / restoring or dragging the window ended playback. The decision is now one pure function
// (ResizePlaybackPolicy.h): a resize during Play keeps playing, and no Play is stopped, so none is resumed. These pin its
// truth table and, by source scan (MainWindow.cpp needs a full GUI build, so it is read as text, mirroring
// test_resize_still_drawing_wait.cpp), that resizeEvent routes through it and that the KeepPlaying path does none of
// the stopped-frame redraw work.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/ResizePlaybackPolicy.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QTextStream>

#include <string>

using resize_playback_policy::Action;
using resize_playback_policy::actionName;
using resize_playback_policy::decide;

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    if (path.isEmpty()) return QString();
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) return QString();
    QTextStream stream(&file);
    return stream.readAll();
}

// The resizeEvent body with // comments removed (a comment naming a forbidden call is not a call).
QString resizeEventCode()
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int at = source.indexOf(QStringLiteral("void MainWindow::resizeEvent(QResizeEvent *event)"));
    const int next = source.indexOf(QStringLiteral("void MainWindow::changeEvent( QEvent *event )"), at);
    if (at < 0 || next <= at) return QString();
    QString body = source.mid(at, next - at);
    body.remove(QRegularExpression(QStringLiteral("//[^\\n]*")));
    return body;
}

// The text of one case of the resize switch, up to the next case label (or the end of the body).
QString caseBody(const QString & body, const QString & action)
{
    const QString label = QStringLiteral("case resize_playback_policy::Action::%1:").arg(action);
    const int at = body.indexOf(label);
    if (at < 0) return QString();
    const int next = body.indexOf(QStringLiteral("case resize_playback_policy::Action::"), at + label.size());
    return next < 0 ? body.mid(at) : body.mid(at, next - at);
}

std::string name(Action action) { return std::string(actionName(action)); }

} // namespace

TEST(ResizePlaybackPolicy, TheFullTruthTable)
{
    for (int bits = 0; bits < 8; ++bits)
    {
        const bool opening = (bits & 4) != 0;
        const bool lookAssist = (bits & 2) != 0;
        const bool play = (bits & 1) != 0;
        const Action got = decide(opening, lookAssist, play);
        Action want = Action::RedrawStopped;
        if (opening) want = Action::AcceptOnly;                  // an open in progress wins every input
        else if (lookAssist) want = Action::HoldForLookAssist;   // the Look Assist hold wins over Play
        else if (play) want = Action::KeepPlaying;               // Play keeps playing, never a stop
        ASSERT_EQ(name(want), name(got));
    }
}

TEST(ResizePlaybackPolicy, ActionsHaveTheLogNames)
{
    ASSERT_EQ(std::string("accept_only"), name(Action::AcceptOnly));
    ASSERT_EQ(std::string("hold_for_look_assist"), name(Action::HoldForLookAssist));
    ASSERT_EQ(std::string("keep_playing"), name(Action::KeepPlaying));
    ASSERT_EQ(std::string("redraw_stopped"), name(Action::RedrawStopped));
}

TEST(ResizePlaybackPolicyWiring, ResizeEventDecidesOnceAndNeverStopsOrStartsPlay)
{
    const QString body = resizeEventCode();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_EQ(1, static_cast<int>(body.count(QStringLiteral("resize_playback_policy::decide("))));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "resize_playback_policy::decide( m_inOpeningProcess, m_lookAssistPump.sessionOpen(), ui->actionPlay->isChecked() )")));
    // no stop, so no resume (ENFORCE-2: a resume would be a programmatic Play)
    ASSERT_FALSE(body.contains(QStringLiteral("actionPlay->setChecked")));
    ASSERT_FALSE(body.contains(QStringLiteral("actionPlay->trigger")));
    ASSERT_FALSE(body.contains(QStringLiteral("programmaticPlay")));
    ASSERT_FALSE(body.contains(QStringLiteral("on_actionPlay_")));
}

TEST(ResizePlaybackPolicyWiring, TheKeepPlayingCaseDoesNoStoppedFrameWork)
{
    const QString body = resizeEventCode();
    ASSERT_FALSE(body.isEmpty());
    const QString keep = caseBody(body, QStringLiteral("KeepPlaying"));
    ASSERT_FALSE(keep.isEmpty());
    ASSERT_FALSE(keep.contains(QStringLiteral("waitForResizeStillDrawing")));
    ASSERT_FALSE(keep.contains(QStringLiteral("m_pRenderThread->lock")));
    ASSERT_FALSE(keep.contains(QStringLiteral("drawFrame(")));
    ASSERT_FALSE(keep.contains(QStringLiteral("m_pAudioPlayback")));
    // it accepts, logs once, and returns before the stopped-frame redraw
    ASSERT_TRUE(keep.contains(QStringLiteral("event->accept();")));
    ASSERT_TRUE(keep.contains(QStringLiteral("return;")));
    ASSERT_EQ(1, static_cast<int>(keep.count(QStringLiteral("QStringLiteral(\"resize.keep_playing\")"))));
    // the gradient re-anchors on the next landed playback frame (finishPresentedFrame consumes the flag)
    ASSERT_TRUE(keep.contains(QStringLiteral("m_zoomModeChanged = true;")));
}

TEST(ResizePlaybackPolicyWiring, TheStoppedRedrawAndTheLookAssistHoldAreUnchanged)
{
    const QString body = resizeEventCode();
    ASSERT_FALSE(body.isEmpty());
    // the Look Assist hold still re-posts the resize at landing, where decide() runs again
    const QString hold = caseBody(body, QStringLiteral("HoldForLookAssist"));
    ASSERT_TRUE(hold.contains(QStringLiteral("m_lookAssistPump.hold(")));
    ASSERT_TRUE(hold.contains(QStringLiteral("resizeEvent( &heldResize );")));
    // the open-in-progress case only accepts
    const QString accept = caseBody(body, QStringLiteral("AcceptOnly"));
    ASSERT_TRUE(accept.contains(QStringLiteral("event->accept();")));
    ASSERT_FALSE(accept.contains(QStringLiteral("drawFrame(")));
    // Play off: the audio backstop and the redraw block run after the switch
    const int stopped = body.indexOf(QStringLiteral("case resize_playback_policy::Action::RedrawStopped:"));
    const int audio = body.indexOf(QStringLiteral("m_pAudioPlayback->stop();"));
    const int draw = body.indexOf(QStringLiteral("drawFrame();"));
    ASSERT_TRUE(stopped >= 0);
    ASSERT_TRUE(audio > stopped);
    ASSERT_TRUE(draw > audio);
    ASSERT_EQ(1, static_cast<int>(body.count(QStringLiteral("m_pAudioPlayback->stop();"))));
}
