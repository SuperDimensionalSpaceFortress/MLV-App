/*
 * HFR-NATIVE-M16-1457-GUI-PRESENT-CADENCE-1 r2: the due wake's native-message delivery (platform/qt/PlaybackDueWake.*)
 * and the every-frame "advance only past a presented frame" gate (adversary r1 pass 3 A1).
 *
 * r1 measured the wake 10.1-11.7 ms late (late_p50, Bachelor CUDA, hires=1): the QWinEventNotifier delivery reaches the GUI
 * thread as a Qt posted event. r2 posts kNativeWakeMessage from a wait thread to a message-only window and takes it in a
 * QAbstractNativeEventFilter. These tests pin the message id, the stale-generation no-op, the kill switch and the thread
 * stop/join order, and drive the real wait thread on this host's message queue (no window is shown, no playback runs).
 */

#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackDueWake.h"

#include <QCoreApplication>
#include <QEventLoop>
#include <QFile>
#include <QString>
#include <QTextStream>

#include <string>

#ifdef Q_OS_WIN
#include <windows.h>
#endif

namespace
{
QString dueWakeRepoSource( const QString & relative )
{
    const QString path = repo_file_path( relative );
    QFile file( path );
    if( path.isEmpty() || !file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return QString();
    QTextStream stream( &file );
    return stream.readAll();
}

QString functionBody( const QString & source, const QString & signature, const QString & nextSignature )
{
    const int begin = source.indexOf( signature );
    const int end = source.indexOf( nextSignature, begin + 1 );
    if( begin < 0 || end <= begin ) return QString();
    return source.mid( begin, end - begin );
}

#ifdef Q_OS_WIN
// Take this wake's messages off the queue for up to waitMs, handing each to the wake (what the native filter does).
// Returns how many were taken.
int pumpDueWakeMessages( PlaybackDueWake & wake, DWORD waitMs )
{
    int taken = 0;
    const HWND window = reinterpret_cast<HWND>( wake.nativeWindow() );
    const ULONGLONG deadline = GetTickCount64() + waitMs;
    MSG msg;
    while( GetTickCount64() < deadline )
    {
        while( PeekMessageW( &msg, window, PlaybackDueWake::kNativeWakeMessage, PlaybackDueWake::kNativeWakeMessage,
                             PM_REMOVE ) )
        {
            if( wake.handleNativeMessage( &msg ) ) ++taken;
        }
        Sleep( 1 );
    }
    return taken;
}

// True once a message of this wake sits in the queue (left there), within waitMs.
bool dueWakeMessageQueued( PlaybackDueWake & wake, DWORD waitMs )
{
    const HWND window = reinterpret_cast<HWND>( wake.nativeWindow() );
    const ULONGLONG deadline = GetTickCount64() + waitMs;
    MSG msg;
    while( GetTickCount64() < deadline )
    {
        if( PeekMessageW( &msg, window, PlaybackDueWake::kNativeWakeMessage, PlaybackDueWake::kNativeWakeMessage,
                          PM_NOREMOVE ) )
            return true;
        Sleep( 1 );
    }
    return false;
}
#endif
}

TEST(PlaybackDueWakeNative, TheMessageIsWmAppPlus0x1457AndTheWaitThreadPostsIt)
{
    ASSERT_EQ( 0x9457u, PlaybackDueWake::kNativeWakeMessage );
    ASSERT_TRUE( PlaybackDueWake::kNativeWakeMessage >= 0x8000u && PlaybackDueWake::kNativeWakeMessage <= 0xBFFFu );   // WM_APP range
#ifdef Q_OS_WIN
    ASSERT_EQ( static_cast<unsigned>( WM_APP + 0x1457 ), PlaybackDueWake::kNativeWakeMessage );
#endif
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_FALSE( source.isEmpty() );
    ASSERT_EQ( 1, source.count( QStringLiteral( "PostMessageW( args.window, PlaybackDueWake::kNativeWakeMessage," ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "QCoreApplication::instance()->installNativeEventFilter( d->filter );" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "d->delivery = Delivery::NativeMessage;" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "d->delivery = Delivery::Notifier;" ) ) );   // the named fallback
    ASSERT_EQ( std::string( "native-msg" ),
               std::string( PlaybackDueWake::deliveryName( PlaybackDueWake::Delivery::NativeMessage ) ) );
    ASSERT_EQ( std::string( "notifier" ),
               std::string( PlaybackDueWake::deliveryName( PlaybackDueWake::Delivery::Notifier ) ) );
    ASSERT_EQ( std::string( "qtimer" ),
               std::string( PlaybackDueWake::deliveryName( PlaybackDueWake::Delivery::QtTimer ) ) );
}

TEST(PlaybackDueWakeGate, AStaleGenerationIsANoOpAndTheCurrentOneFiresOnce)
{
    PlaybackDueWakeGate gate;
    const quint32 first = gate.arm( 0, 5000000 );                 // due at 5 ms
    const quint32 second = gate.arm( 1000000, 5000000 );          // re-armed at 1 ms: due at 6 ms
    ASSERT_TRUE( first != second );
    ASSERT_FALSE( gate.accept( first, 7000000 ) );                // the first arm's message, queued before the re-arm
    ASSERT_TRUE( gate.armed() );
    ASSERT_FALSE( gate.accept( second, 2000000 ) );               // the new generation read after the old signal: early
    ASSERT_TRUE( gate.armed() );
    ASSERT_TRUE( gate.accept( second, 6000000 - PlaybackDueWakeGate::kStaleDeliveryNs ) );
    ASSERT_FALSE( gate.armed() );
    ASSERT_FALSE( gate.accept( second, 9000000 ) );               // a duplicate after it fired
    const quint32 third = gate.arm( 9000000, 0 );
    gate.cancel();                                                // stop / pause after the post
    ASSERT_FALSE( gate.accept( third, 9500000 ) );
    ASSERT_FALSE( gate.armed() );
}

TEST(PlaybackDueWakeNative, TheKillSwitchTurnsTheWakeOffAndIsHonouredByTheArm)
{
    const QByteArray saved = qgetenv( "MLVAPP_PLAYBACK_DUE_WAKE" );
    const bool hadSaved = qEnvironmentVariableIsSet( "MLVAPP_PLAYBACK_DUE_WAKE" );
    qputenv( "MLVAPP_PLAYBACK_DUE_WAKE", "0" );
    ASSERT_FALSE( PlaybackDueWake::enabledByEnvironment() );
    qputenv( "MLVAPP_PLAYBACK_DUE_WAKE", "1" );
    ASSERT_TRUE( PlaybackDueWake::enabledByEnvironment() );
    qunsetenv( "MLVAPP_PLAYBACK_DUE_WAKE" );
    ASSERT_TRUE( PlaybackDueWake::enabledByEnvironment() );
    if( hadSaved ) qputenv( "MLVAPP_PLAYBACK_DUE_WAKE", saved );
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "static const bool enabled = PlaybackDueWake::enabledByEnvironment();" ) ) );
    const QString arm = functionBody( source, QStringLiteral( "void MainWindow::armPlaybackDueWake( void )" ),
                                      QStringLiteral( "void MainWindow::onPlaybackDueWake( void )" ) );
    ASSERT_FALSE( arm.isEmpty() );
    const int killSwitch = arm.indexOf( QStringLiteral( "if( !mlvappPlaybackDueWakeEnabled() ) return;" ) );
    const int create = arm.indexOf( QStringLiteral( "new PlaybackDueWake(" ) );
    ASSERT_TRUE( killSwitch > 0 && create > killSwitch );         // off means no wake object, thread or window at all
}

TEST(PlaybackDueWakeNative, TheThreadIsJoinedBeforeTheWindowGoesAndMainWindowDropsTheWakeFirst)
{
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    const QString destructor = functionBody( source, QStringLiteral( "PlaybackDueWake::~PlaybackDueWake()" ),
                                             QStringLiteral( "void PlaybackDueWake::stop()" ) );
    ASSERT_FALSE( destructor.isEmpty() );
    const int stop = destructor.indexOf( QStringLiteral( "stop();" ) );
    const int destroyWindow = destructor.indexOf( QStringLiteral( "DestroyWindow( d->window );" ) );
    const int removeFilter = destructor.indexOf( QStringLiteral( "removeNativeEventFilter( d->filter );" ) );
    ASSERT_TRUE( stop > 0 && destroyWindow > stop && removeFilter > stop );
    const QString stopBody = functionBody( source, QStringLiteral( "void PlaybackDueWake::stop()" ),
                                           QStringLiteral( "void PlaybackDueWake::arm( double dueMs )" ) );
    const int setEvent = stopBody.indexOf( QStringLiteral( "SetEvent( d->stopEvent );" ) );
    const int join = stopBody.indexOf( QStringLiteral( "WaitForSingleObject( d->thread, INFINITE );" ) );
    ASSERT_TRUE( setEvent > 0 && join > setEvent );
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString mainDestructor = functionBody( mainWindow, QStringLiteral( "MainWindow::~MainWindow()" ),
                                                 QStringLiteral( "flushPhase3PlaybackTime( true );" ) );
    ASSERT_TRUE( mainDestructor.contains( QStringLiteral( "m_playbackDueWake.reset();" ) ) );
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    ASSERT_TRUE( wake.nativeWindow() != 0 );
    ASSERT_TRUE( wake.waitThreadRunning() );
    wake.stop();
    ASSERT_FALSE( wake.waitThreadRunning() );
    wake.arm( 0.0 );                                              // a no-op once stopped: nothing posts any more
    ASSERT_EQ( 0, pumpDueWakeMessages( wake, 40 ) );
    ASSERT_EQ( 0, fired );
    wake.stop();                                                  // idempotent
#endif
}

TEST(PlaybackDueWakeNative, TheWaitThreadDeliversOnceAndAMessageQueuedBeforeACancelIsANoOp)
{
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    wake.arm( 1.0 );
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 200 ) );
    ASSERT_EQ( 1, fired );
    ASSERT_FALSE( wake.armed() );
    // posted while nobody pumps, then cancelled (Play stopped) before the GUI thread reads it: consumed, does nothing.
    // (adversary pass 4 A2: wait until the message is really queued, never a fixed sleep, before cancelling or re-arming --
    // a re-arm before the wait thread consumed the old expiry resets the timer and only one message would ever post)
    wake.arm( 0.5 );
    ASSERT_TRUE( dueWakeMessageQueued( wake, 500 ) );
    wake.cancel();
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 60 ) );
    ASSERT_EQ( 1, fired );
    // re-armed while the old message is queued: the old one is dropped, the new one fires once
    wake.arm( 0.5 );
    ASSERT_TRUE( dueWakeMessageQueued( wake, 500 ) );
    wake.arm( 2.0 );
    ASSERT_EQ( 2, pumpDueWakeMessages( wake, 200 ) );
    ASSERT_EQ( 2, fired );
    // a message that is not this wake's (another window, another tag) is left alone
    MSG foreign = {};
    foreign.hwnd = reinterpret_cast<HWND>( wake.nativeWindow() );
    foreign.message = PlaybackDueWake::kNativeWakeMessage;
    foreign.lParam = 0;
    ASSERT_FALSE( wake.handleNativeMessage( &foreign ) );
    foreign.lParam = static_cast<LPARAM>( PlaybackDueWake::kNativeWakeTag );
    foreign.hwnd = nullptr;
    ASSERT_FALSE( wake.handleNativeMessage( &foreign ) );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeNative, TheNotifierFallbackIsNamedWhenTheNativePathIsNotTaken)
{
#ifdef Q_OS_WIN
    PlaybackDueWake wake( nullptr, []() {}, false );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::Notifier );
    ASSERT_TRUE( wake.nativeWindow() == 0 );
    ASSERT_FALSE( wake.waitThreadRunning() );
#endif
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 1, source.count( QStringLiteral(
        "\"late_p50_ms=%6 late_p95_ms=%7 hires=%8 disabled=%9 delivery=%10 unpresented_deferred=%11\"" ) ) );
    ASSERT_EQ( 0, source.count( QStringLiteral(
        "m_playbackDueWakeDelivery = PlaybackDueWake::deliveryName( m_playbackDueWake->delivery() );" ) ) );
}

TEST(PlaybackDueWakeNative, AnArmThatFellBackToAQtTimerIsReportedAsQtimerAndNotHighResolution)
{
    // r2 adversary pass 5 A1 input: Windows, the high-resolution timer and the native-message path constructed, Play active,
    // arm(4.25); SetWaitableTimer returns FALSE, so the arm runs on a fallback QTimer. The line printed delivery=native-msg
    // hires=1 for that wake.
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    wake.arm( 0.5 );
    ASSERT_TRUE( wake.lastArmDelivery() == PlaybackDueWake::Delivery::NativeMessage );
    const bool hiResOnTheWaitableTimer = wake.hiResActive();
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 200 ) );
    wake.setWaitableTimerFailsForTest( true );
    wake.arm( 4.25 );
    ASSERT_TRUE( wake.lastArmDelivery() == PlaybackDueWake::Delivery::QtTimer );
    ASSERT_FALSE( wake.hiResActive() );
    // the fallback QTimer still delivers the wake, once
    const ULONGLONG deadline = GetTickCount64() + 500;
    while( fired < 2 && GetTickCount64() < deadline )
    {
        QCoreApplication::processEvents( QEventLoop::AllEvents, 5 );
        Sleep( 1 );
    }
    ASSERT_EQ( 2, fired );
    ASSERT_EQ( 0, pumpDueWakeMessages( wake, 20 ) );   // nothing on the native path for that arm
    // the next arm that sets the waitable timer is native again
    wake.setWaitableTimerFailsForTest( false );
    wake.arm( 0.5 );
    ASSERT_TRUE( wake.lastArmDelivery() == PlaybackDueWake::Delivery::NativeMessage );
    ASSERT_TRUE( wake.hiResActive() == hiResOnTheWaitableTimer );
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 200 ) );
    ASSERT_EQ( 3, fired );
#endif
    // MainWindow records the mechanism after EVERY arm (first and re-arm) and never keeps a "hires" a later arm lost
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString arm = functionBody( source, QStringLiteral( "void MainWindow::armPlaybackDueWake( void )" ),
                                      QStringLiteral( "void MainWindow::notePlaybackDueWakeArmMechanism( void )" ) );
    const QString wakeBody = functionBody( source, QStringLiteral( "void MainWindow::onPlaybackDueWake( void )" ),
                                           QStringLiteral( "void MainWindow::cancelPlaybackDueWake( void )" ) );
    const QString note = functionBody( source, QStringLiteral( "void MainWindow::notePlaybackDueWakeArmMechanism( void )" ),
                                       QStringLiteral( "void MainWindow::onPlaybackDueWake( void )" ) );
    ASSERT_TRUE( arm.indexOf( QStringLiteral( "notePlaybackDueWakeArmMechanism();" ) ) >
                 arm.indexOf( QStringLiteral( "m_playbackDueWake->arm( dueMs + kPlaybackDueWakeSlackMs );" ) ) );
    ASSERT_TRUE( wakeBody.lastIndexOf( QStringLiteral( "notePlaybackDueWakeArmMechanism();" ) ) >
                 wakeBody.lastIndexOf( QStringLiteral( "m_playbackDueWake->arm( dueMs + kPlaybackDueWakeSlackMs );" ) ) );
    ASSERT_TRUE( wakeBody.lastIndexOf( QStringLiteral( "m_playbackDueWake->arm( dueMs + kPlaybackDueWakeSlackMs );" ) ) > 0 );
    ASSERT_TRUE( note.contains( QStringLiteral( "m_playbackDueWake->lastArmDelivery()" ) ) );
    ASSERT_TRUE( note.contains( QStringLiteral( "m_playbackDueWakeHiRes = m_playbackDueWakeHiRes && hiResSoFar;" ) ) );
}

TEST(PlaybackDueWakeA1, EveryFrameModeAdvancesOnlyPastAPresentedFrame)
{
    // adversary r1 pass 3 A1 input: CPU, 60 fps, every-frame mode, Play checked, lookahead 0. Frame 9 is on screen; frame 9's
    // early advance already requested frame 10 (slider 10). Frame 10 is Ready but its render thread was descheduled before
    // frameReady, so the renderer reads idle (stillDrawing false). The wake at 1006.9 ms has 1.015 credit.
    const long long presented = 9;
    const int slider = 10;
    ASSERT_FALSE( playbackDueWakeMayAdvance( false, false, presented, slider ) );   // would request 11 over unpresented 10
    ASSERT_TRUE( playbackDueWakeMayAdvance( false, false, 10, slider ) );           // once 10 is presented
    ASSERT_TRUE( playbackDueWakeMayAdvance( false, true, presented, slider ) );     // drop-frame mode may step over
    ASSERT_FALSE( playbackDueWakeMayAdvance( true, true, 10, slider ) );            // busy always holds
    ASSERT_TRUE( playbackDueWakeMayAdvance( false, false, -1, slider ) );           // nothing presented in this Play yet
    ASSERT_FALSE( playbackDueWakeMayAdvance( false, false, 99, 0 ) );               // the wrap to cut-in not presented yet
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString wake = functionBody( source, QStringLiteral( "void MainWindow::onPlaybackDueWake( void )" ),
                                       QStringLiteral( "void MainWindow::cancelPlaybackDueWake( void )" ) );
    ASSERT_FALSE( wake.isEmpty() );
    const int gate = wake.indexOf( QStringLiteral( "if( !playbackDueWakeMayAdvance( m_frameStillDrawing, ui->actionDropFrameMode->isChecked()," ) );
    const int advance = wake.indexOf( QStringLiteral( "timerFrameEvent( true );" ) );
    ASSERT_TRUE( gate > 0 && advance > gate );
    const QString held = wake.mid( gate, advance - gate );
    ASSERT_TRUE( held.contains( QStringLiteral( "m_playbackDueWakePresentedFrame, ui->horizontalSliderPosition->value() ) )" ) ) );
    ASSERT_TRUE( held.contains( QStringLiteral( "m_playbackFrameAdvancePending = true;" ) ) );
    ASSERT_TRUE( held.contains( QStringLiteral( "++m_playbackDueWakeUnpresentedDeferred;" ) ) );
}

TEST(PlaybackDueWakeA1, TheGateReadsTheFrameWhosePresentEndedNotTheOneAcceptedForPreparation)
{
    // r2 adversary pass 4 A1 input: CPU, 60 fps, every-frame mode, lookahead 0, zebras on. Frame 8 on screen; the tail requests 9
    // at 1000 ms; 8's present ends at 1001 ms and arms the wake for 1016.9 ms. drawFrameReady accepts 9 at 1010 ms and records it
    // as m_lastPresentedPlaybackFrame BEFORE its background preparation, whose worker is descheduled until 1020 ms. At the wake the
    // accepted frame equals the slider (9 == 9), so a gate on it advanced to 10, whose enqueued preparation replaced 9's: 9 was
    // never shown. The frame whose present ENDED is still 8, so the wake must hold.
    const long long presentEnded = 8;
    const long long acceptedForPreparation = 9;
    const int slider = 9;
    ASSERT_TRUE( playbackDueWakeMayAdvance( false, false, acceptedForPreparation, slider ) );   // the pass 4 shape on dc7b5c43
    ASSERT_FALSE( playbackDueWakeMayAdvance( false, false, presentEnded, slider ) );            // held until 9's present ends
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString wake = functionBody( source, QStringLiteral( "void MainWindow::onPlaybackDueWake( void )" ),
                                       QStringLiteral( "void MainWindow::cancelPlaybackDueWake( void )" ) );
    ASSERT_FALSE( wake.contains( QStringLiteral( "m_lastPresentedPlaybackFrame" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "m_playbackDueWakePresentedFrame = static_cast<long long>( display_frame );" ) ) );
    // set at the END of presentPlaybackPreparedFrame, right before the wake is armed from that present
    const int set = source.indexOf( QStringLiteral( "m_playbackDueWakePresentedFrame = static_cast<long long>( display_frame );" ) );
    const int armed = source.indexOf( QStringLiteral( "    armPlaybackDueWake();\n" ), set );
    const int present = source.lastIndexOf( QStringLiteral( "void MainWindow::presentPlaybackPreparedFrame( const PlaybackPrepResult &result )" ), set );
    const int finish = source.lastIndexOf( QStringLiteral( "    finishPresentedFrame( display_frame," ), set );
    ASSERT_TRUE( present > 0 && finish > present && set > finish && armed > set && armed - set < 400 );
    ASSERT_EQ( 1, source.count( QStringLiteral( "m_playbackDueWakePresentedFrame = -1;" ) ) );   // reset with the counters at Play
}
