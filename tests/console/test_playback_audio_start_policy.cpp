// PLAYBACK-STARTUP-WAIT-PREWARM-1 Row A: the audio start moves off the GUI thread. At Play the audio worker opens the
// sink at the start frame, silent and suspended (prepare); at the first present it resumes that sink when it was
// prepared at the presented frame and the present is at most one frame (kAudioResumeSlackMs) old, and otherwise
// re-opens once, seeking ahead by the lag plus the expected open time so the audio lands on the video timeline.
// MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY=1 keeps today's synchronous stop / jumpToPos / play on the GUI thread.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackAudioStartPolicy.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <string>

using playback_audio_start::Mode;
using playback_audio_start::chooseMode;
using playback_audio_start::kAudioResumeSlackMs;
using playback_audio_start::reopenSeekFrame;

namespace
{

constexpr double kFps = 24000.0 / 1001.0; // 23.976

QString sourceFile(const char *relative)
{
    const QString path = repo_file_path(QString::fromLatin1(relative));
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString bodyBetween(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(PlaybackAudioStartPolicy, ThePreparedSinkResumesInsideTheSlack)
{
    ASSERT_NEAR(42.0, kAudioResumeSlackMs, 1e-9); // one frame at 23.976
    ASSERT_TRUE(Mode::Prepared == chooseMode(false, true, 120, 120, 0.0));
    ASSERT_TRUE(Mode::Prepared == chooseMode(false, true, 120, 120, 41.9));
    ASSERT_TRUE(Mode::Prepared == chooseMode(false, true, 120, 120, kAudioResumeSlackMs));
}

TEST(PlaybackAudioStartPolicy, ItReopensOutsideTheSlackOrAtAnotherFrame)
{
    ASSERT_TRUE(Mode::Reopen == chooseMode(false, true, 120, 120, 42.1));   // too late: the audio would trail
    ASSERT_TRUE(Mode::Reopen == chooseMode(false, true, 120, 121, 0.0));    // prepared at another frame
    ASSERT_TRUE(Mode::Reopen == chooseMode(false, false, 120, 120, 0.0));   // never prepared (or already spent)
    ASSERT_TRUE(Mode::Reopen == chooseMode(false, false, -1, 0, 0.0));
}

TEST(PlaybackAudioStartPolicy, TheReopenSeeksAheadByTheLagPlusTheOpen)
{
    // 220 ms late + a 600 ms open = 820 ms = 19.66 frames -> 20 frames ahead (rounded up: never behind the video)
    ASSERT_EQ(120 + 20, reopenSeekFrame(120, 220.0, 600.0, kFps));
    // nothing to cover: the presented frame itself
    ASSERT_EQ(120, reopenSeekFrame(120, 0.0, 0.0, kFps));
    // exactly one frame period is one frame, not two
    ASSERT_EQ(121, reopenSeekFrame(120, 1000.0 / kFps, 0.0, kFps));
    // a partial frame rounds up
    ASSERT_EQ(121, reopenSeekFrame(120, 1.0, 0.0, kFps));
}

TEST(PlaybackAudioStartPolicy, TheSeekFrameIsNeverNegative)
{
    ASSERT_EQ(0, reopenSeekFrame(-5, 0.0, 0.0, kFps));
    ASSERT_EQ(0, reopenSeekFrame(0, -500.0, -100.0, kFps)); // a negative lag or open never seeks backward
    ASSERT_EQ(30, reopenSeekFrame(30, -10.0, 0.0, kFps));
    ASSERT_EQ(30, reopenSeekFrame(30, 500.0, 0.0, 0.0));    // an unknown fps leaves the frame alone
}

TEST(PlaybackAudioStartPolicy, TheLegacyFlagSelectsTheLegacyPath)
{
    ASSERT_TRUE(Mode::Legacy == chooseMode(true, true, 120, 120, 0.0));
    ASSERT_TRUE(Mode::Legacy == chooseMode(true, false, -1, 120, 900.0));
    ASSERT_EQ(std::string("legacy"), std::string(playback_audio_start::modeName(Mode::Legacy)));
    ASSERT_EQ(std::string("prepared"), std::string(playback_audio_start::modeName(Mode::Prepared)));
    ASSERT_EQ(std::string("reopen"), std::string(playback_audio_start::modeName(Mode::Reopen)));
}

TEST(PlaybackAudioStartPolicy, TheOffsetIsAudioContentMinusVideoTimelineAndAudioLateIsNegative)
{
    // prepared at the presented frame, resumed 30 ms after the present, 5 ms played silently while prepared
    ASSERT_NEAR(5.0 - 30.0, playback_audio_start::avOffsetEstMs(120, 5.0, 120, 30.0, kFps), 1e-9);
    // re-opened 20 frames ahead, audible 800 ms after the present: 834.2 ms of content for 800 ms of video
    ASSERT_NEAR(20 * 1000.0 / kFps - 800.0, playback_audio_start::avOffsetEstMs(140, 0.0, 120, 800.0, kFps), 1e-9);
}

TEST(PlaybackAudioStartWiring, TheGuiThreadNeverStartsTheAudioDeviceUnlessLegacy)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    // read once
    ASSERT_TRUE(source.contains(QStringLiteral(
        "static const bool requested = environmentFlagEnabled( \"MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY\" );")));
    const QString finish = bodyBetween(source, QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"),
                                       QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_FALSE(finish.isEmpty());
    // exactly one play() in finishPresentedFrame, and it is inside the legacy branch
    ASSERT_EQ(1, finish.count(QStringLiteral("m_pAudioPlayback->play()")));
    ASSERT_EQ(0, finish.count(QStringLiteral("m_pAudioPlayback->start(")));
    const int legacy = finish.indexOf(QStringLiteral("if( mlvappPlaybackAudioSyncLegacyRequested() )"));
    const int stop = finish.indexOf(QStringLiteral("                m_pAudioPlayback->stop();\n"));
    const int jump = finish.indexOf(QStringLiteral("                m_pAudioPlayback->jumpToPos( syncFrame );\n"));
    const int play = finish.indexOf(QStringLiteral("                m_pAudioPlayback->play();\n"));
    const int async = finish.indexOf(QStringLiteral("m_pAudioPlayback->syncAndPlay( static_cast<int>( displayFrame ),"));
    ASSERT_TRUE(legacy >= 0);
    ASSERT_TRUE(stop > legacy);
    ASSERT_TRUE(jump > stop);
    ASSERT_TRUE(play > jump);
    ASSERT_TRUE(async > play); // the else branch
    ASSERT_TRUE(finish.mid(play, async - play).contains(QStringLiteral("else")));
    // r1 venue leg a702dab3: a sink opened only at Play (0.61 s cold) was still opening at the first present (0.135 s),
    // so the sync re-opened 27 frames ahead and the audio ran 321 ms early. A settled paused frame keeps it prepared.
    const int pausedPrepare = finish.indexOf(QStringLiteral(
        "        m_pAudioPlayback->prepare( static_cast<int>( displayFrame ) );"));
    ASSERT_TRUE(pausedPrepare > async);
    const QString guard = finish.mid(pausedPrepare - 400, 400);
    ASSERT_TRUE(guard.contains(QStringLiteral("if( !ui->actionPlay->isChecked()")));
    ASSERT_TRUE(guard.contains(QStringLiteral("&& !mlvappPlaybackAudioSyncLegacyRequested()")));
    // only the newest queued prepare runs, and a sink already prepared at that frame is kept
    const QString audio = sourceFile("platform/qt/AudioPlayback.cpp");
    ASSERT_TRUE(audio.contains(QStringLiteral("if( ticket != m_prepareTicket.load() ) return;")));
    ASSERT_TRUE(audio.contains(QStringLiteral(
        "if( m_prepared && m_audioEngineRunning && m_preparedFrame == startFrame ) return;")));
    // r1 venue leg d1426fe1: a window resize (resizeEvent stops the audio) just before Play closed the prepared sink.
    // A public stop() keeps a prepared (silent, suspended) sink; only the engine's own re-open and reset close it.
    const QString stopBody = bodyBetween(audio, QStringLiteral("void AudioPlayback::stop()"),
                                         QStringLiteral("void AudioPlayback::stopNow()"));
    ASSERT_FALSE(stopBody.isEmpty());
    const int keep = stopBody.indexOf(QStringLiteral("if( m_prepared ) return;"));
    const int close = stopBody.indexOf(QStringLiteral("stopNow();"));
    ASSERT_TRUE(keep >= 0);
    ASSERT_TRUE(close > keep);
}

TEST(PlaybackAudioStartWiring, PlayPostsThePrepareAndTheAudioObjectLivesOnItsOwnThread)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    const QString triggered = bodyBetween(source, QStringLiteral("void MainWindow::on_actionPlay_triggered(bool checked)"),
                                          QStringLiteral("void MainWindow::applyEffectiveDualIsoPlaybackSettings( void )"));
    ASSERT_FALSE(triggered.isEmpty());
    const int sync = triggered.indexOf(QStringLiteral("m_tryToSyncAudio = true;"));
    const int prepare = triggered.indexOf(QStringLiteral(
        "m_pAudioPlayback->prepare( ui->horizontalSliderPosition->value() );"));
    ASSERT_TRUE(sync >= 0);
    ASSERT_TRUE(prepare > sync);
    ASSERT_TRUE(triggered.mid(sync, prepare - sync).contains(QStringLiteral("!mlvappPlaybackAudioSyncLegacyRequested()")));
    // constructed without a parent and moved to its own thread unless legacy; joined in the destructor before the
    // mlvObject is freed
    ASSERT_TRUE(source.contains(QStringLiteral(
        "    m_pAudioPlayback = new AudioPlayback( mlvappPlaybackAudioSyncLegacyRequested() ? this : nullptr );\n"
        "    if( !mlvappPlaybackAudioSyncLegacyRequested() ) m_pAudioPlayback->startWorkerThread();\n")));
    const QString dtor = bodyBetween(source, QStringLiteral("MainWindow::~MainWindow()"), QStringLiteral("delete m_pAudioPlayback;"));
    ASSERT_FALSE(dtor.isEmpty());
    const int join = dtor.indexOf(QStringLiteral("m_pAudioPlayback->shutdownWorkerThread();"));
    const int freeMlv = dtor.indexOf(QStringLiteral("freeActiveMlvObjectAfterLifecycleBarrier( \"destructor\" );"));
    ASSERT_TRUE(join >= 0);
    ASSERT_TRUE(freeMlv > join);

    const QString audio = sourceFile("platform/qt/AudioPlayback.cpp");
    ASSERT_TRUE(audio.contains(QStringLiteral("moveToThread( m_pWorkerThread );")));
    ASSERT_TRUE(audio.contains(QStringLiteral("CoInitializeEx( nullptr, COINIT_APARTMENTTHREADED )")));
}
