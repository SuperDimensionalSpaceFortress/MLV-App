// PLAYBACK-STARTUP-WAIT-PREWARM-1 Row A: the audio start moves off the GUI thread. While paused and at Play the audio
// worker holds the sink open, running on silence from a closed gate (prepare); at the first present it hands the
// content over where the video will be when the content is audible, after a resume or a re-open when one is needed.
// r2 (both keys on 8f64fe85): r1 resumed a suspended sink at the prepared frame, and a 0.3-0.63 s resume left the
// audio that far behind; the handover re-anchors it (behaviour: test_playback_audio_start_engine.cpp).
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

TEST(PlaybackAudioStartPolicy, AnOpenHealthySinkIsUsedAtAnyFrameAndLag)
{
    ASSERT_NEAR(42.0, kAudioResumeSlackMs, 1e-9); // one frame at 23.976: the slow-resume line
    ASSERT_TRUE(Mode::Prepared == chooseMode(false, true, true));
}

TEST(PlaybackAudioStartPolicy, AMissingOrUnhealthySinkIsReopened)
{
    ASSERT_TRUE(Mode::Reopen == chooseMode(false, true, false));   // device change or stream error since the prepare
    ASSERT_TRUE(Mode::Reopen == chooseMode(false, false, true));   // never prepared, or closed by a clip change
    ASSERT_TRUE(Mode::Reopen == chooseMode(false, false, false));
}

TEST(PlaybackAudioStartPolicy, TheContentStartsWhereTheVideoIsWhenItIsAudible)
{
    // presented frame 120, handed over 600 ms later (a slow resume), 46 ms queued ahead of it
    ASSERT_NEAR(120 * 1000.0 / kFps + 600.0 + 46.0, playback_audio_start::handoverContentMs(120, 600.0, 46.0, kFps), 1e-9);
    // never before the presented frame, never negative
    ASSERT_NEAR(0.0, playback_audio_start::handoverContentMs(-5, -10.0, -1.0, kFps), 1e-9);
    ASSERT_NEAR(0.0, playback_audio_start::frameMs(30, 0.0), 1e-9); // an unknown fps leaves frame time 0
    // what plays first: the ring's queue (served minus processed), then the host buffer
    ASSERT_NEAR(54.0 + 46.0, playback_audio_start::outputLatencyMs(100.0, 46.0, 46.0), 1e-9);
    ASSERT_NEAR(46.0, playback_audio_start::outputLatencyMs(100.0, 100.0, 46.0), 1e-9);
    ASSERT_NEAR(46.0, playback_audio_start::outputLatencyMs(100.0, 140.0, 46.0), 1e-9); // never a negative queue
}

TEST(PlaybackAudioStartPolicy, TheLegacyFlagSelectsTheLegacyPath)
{
    ASSERT_TRUE(Mode::Legacy == chooseMode(true, true, true));
    ASSERT_TRUE(Mode::Legacy == chooseMode(true, false, false));
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
    // read once, from the exact value set (PlaybackEnvFlags.h)
    ASSERT_TRUE(source.contains(QStringLiteral(
        "static const bool requested = playbackEnvSwitch( \"MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY\", false );")));
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
    // r1 venue leg a702dab3: a sink opened only at Play (0.61 s cold) was still opening at the first present (0.135 s).
    // A settled paused frame keeps it held.
    const int pausedPrepare = finish.indexOf(QStringLiteral(
        "        m_pAudioPlayback->prepare( static_cast<int>( displayFrame ) );"));
    ASSERT_TRUE(pausedPrepare > async);
    const QString guard = finish.mid(pausedPrepare - 400, 400);
    ASSERT_TRUE(guard.contains(QStringLiteral("if( !ui->actionPlay->isChecked()")));
    ASSERT_TRUE(guard.contains(QStringLiteral("&& !mlvappPlaybackAudioSyncLegacyRequested()")));
    // only the newest queued prepare runs
    const QString audio = sourceFile("platform/qt/AudioPlayback.cpp");
    ASSERT_TRUE(audio.contains(QStringLiteral("if( ticket != m_prepareTicket.load() ) return;")));
    // r1 venue leg d1426fe1: a window resize (resizeEvent stops the audio) just before Play closed the prepared sink.
    // A public stop() silences a held sink and keeps it; only a clip change or shutdown closes it.
    const QString stopBody = bodyBetween(audio, QStringLiteral("void AudioPlayback::stop()"),
                                         QStringLiteral("void AudioPlayback::stopHeldNow()"));
    ASSERT_FALSE(stopBody.isEmpty());
    const int invalidate = stopBody.indexOf(QStringLiteral("m_session.invalidate();"));
    const int post = stopBody.indexOf(QStringLiteral("post( [this]()"));
    const int keep = stopBody.indexOf(QStringLiteral("stopHeldNow();"));
    const int close = stopBody.indexOf(QStringLiteral("stopNow();"));
    ASSERT_TRUE(invalidate >= 0);
    ASSERT_TRUE(post > invalidate); // r2 row c: on the GUI thread, before the stop is queued
    ASSERT_TRUE(keep > post);
    ASSERT_TRUE(close > keep);
    // a sync carries the session it was posted in and is dropped when a stop came after it
    const QString sync = bodyBetween(audio, QStringLiteral("void AudioPlayback::syncAndPlayNow("),
                                     QStringLiteral("//The legacy synchronous start"));
    ASSERT_TRUE(sync.contains(QStringLiteral("if( !m_session.isCurrent( session ) ) return;")));
    ASSERT_TRUE(audio.contains(QStringLiteral("const quint64 session = m_session.current();")));
}

TEST(PlaybackAudioStartWiring, PlayPostsThePrepareAndTheAudioObjectLivesOnItsOwnThread)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    const QString triggered = bodyBetween(source, QStringLiteral("void MainWindow::on_actionPlay_triggered(bool checked)"),
                                          QStringLiteral("void MainWindow::applyEffectiveDualIsoPlaybackSettings( void )"));
    ASSERT_FALSE(triggered.isEmpty());
    // MERGE-MASTER r5 (CI-2, DISTINGUISH; Dot 6541731407): PLAYBACK-HFR-CONFORM-DEFAULT-1 raises the sync request through
    // ONE conform-gated helper, requestPlaybackAudioSync(), which is the only writer of "m_tryToSyncAudio = true;"
    // (PlaybackConformWiring.AudioSyncFlagIsRaisedInExactlyOneGatedPlace pins a count of 1, so the literal can no longer also
    // sit in this handler). The order guarded here is unchanged: the request comes before the prepare, the prepare is gated
    // on !legacy, and the helper raises the flag the first present's consumer reads.
    const int sync = triggered.indexOf(QStringLiteral("requestPlaybackAudioSync();"));
    const int prepare = triggered.indexOf(QStringLiteral(
        "m_pAudioPlayback->prepare( ui->horizontalSliderPosition->value() );"));
    ASSERT_TRUE(sync >= 0);
    ASSERT_TRUE(prepare > sync);
    ASSERT_TRUE(triggered.mid(sync, prepare - sync).contains(QStringLiteral("!mlvappPlaybackAudioSyncLegacyRequested()")));
    const QString request = bodyBetween(source, QStringLiteral("void MainWindow::requestPlaybackAudioSync( void )"),
                                        QStringLiteral("void MainWindow::refreshPlaybackFpsStatus( void )"));
    ASSERT_FALSE(request.isEmpty());
    ASSERT_TRUE(request.contains(QStringLiteral("m_tryToSyncAudio = true;")));
    // constructed without a parent and moved to its own thread unless legacy; joined (bounded) in the destructor
    // before the mlvObject is freed, and never deleted when the join timed out
    ASSERT_TRUE(source.contains(QStringLiteral(
        "    m_pAudioPlayback = new AudioPlayback( mlvappPlaybackAudioSyncLegacyRequested() ? this : nullptr );\n"
        "    if( !mlvappPlaybackAudioSyncLegacyRequested() ) m_pAudioPlayback->startWorkerThread();\n")));
    const QString dtor = bodyBetween(source, QStringLiteral("MainWindow::~MainWindow()"), QStringLiteral("delete m_pAudioPlayback;"));
    ASSERT_FALSE(dtor.isEmpty());
    const int join = dtor.indexOf(QStringLiteral("if( m_pAudioPlayback && !m_pAudioPlayback->shutdownWorkerThread() )"));
    const int leak = dtor.indexOf(QStringLiteral("m_pAudioPlayback = nullptr;"));
    const int freeMlv = dtor.indexOf(QStringLiteral("freeActiveMlvObjectAfterLifecycleBarrier( \"destructor\" );"));
    ASSERT_TRUE(join >= 0);
    ASSERT_TRUE(leak > join);
    ASSERT_TRUE(freeMlv > leak);

    const QString audio = sourceFile("platform/qt/AudioPlayback.cpp");
    ASSERT_TRUE(audio.contains(QStringLiteral("moveToThread( m_pWorkerThread );")));
    ASSERT_TRUE(audio.contains(QStringLiteral("CoInitializeEx( nullptr, COINIT_APARTMENTTHREADED )")));
    ASSERT_FALSE(audio.contains(QStringLiteral("Qt::BlockingQueuedConnection")));
    ASSERT_TRUE(audio.contains(QStringLiteral("playback_worker_join::runBounded( this,")));
    ASSERT_TRUE(audio.contains(QStringLiteral("playback_worker_join::joinBounded( m_pWorkerThread,")));
    // a fresh gate per open (Qt 6.10 leaves a stream's readyRead connection on its device)
    const QString open = bodyBetween(audio, QStringLiteral("    bool open( double primeMs ) override"),
                                     QStringLiteral("    void close() override"));
    ASSERT_TRUE(open.contains(QStringLiteral("m_gate.reset( new PlaybackAudioGate(")));
}
