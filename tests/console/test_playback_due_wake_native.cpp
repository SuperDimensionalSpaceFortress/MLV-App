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
#include "../../platform/qt/PlaybackDueWakeSamples.h"

#include <QCoreApplication>
#include <QRegularExpression>
#include <QEventLoop>
#include <QFile>
#include <QString>
#include <QTextStream>

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <string>
#include <vector>

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

TEST(PlaybackDueWakeGate, AnEarlierArmsSignalPostedBeforeTheDueTimeNeverTakesTheReplacement)
{
    // r3 adversary pass 7 A1 input: at t=0 arm(2 ms), generation 1. At t=2 the wait thread consumes its signal and samples the
    // post time, then pauses before it loads the generation. At t=3 arm(10 ms), generation 2, due t=13. At t=4 the thread resumes,
    // loads generation 2 and posts the OLD signal stamped t=2. The GUI is busy until t=15; the genuine generation-2 message
    // (posted t=13) follows it. Before r3's fix the old signal was accepted as generation 2 at t=15 (the post time was not
    // consulted: the two-argument accept below), disarmed the replacement and recorded due-to-post -11 ms; the genuine message
    // was then dropped.
    const qint64 ms = 1000000;
    PlaybackDueWakeGate before;
    before.arm( 0, 2 * ms );
    const quint32 replacedOnly = before.arm( 3 * ms, 10 * ms );
    ASSERT_TRUE( before.accept( replacedOnly, 15 * ms ) );        // the bb06bf2b / 89c345d2 shape: no post time, accepted
    PlaybackDueWakeGate gate;
    const quint32 first = gate.arm( 0, 2 * ms );
    const quint32 second = gate.arm( 3 * ms, 10 * ms );           // due t=13
    ASSERT_TRUE( first != second );
    ASSERT_FALSE( gate.accept( second, 15 * ms, 2 * ms ) );       // the old signal, posted t=2 < due 13: stale, dropped
    ASSERT_TRUE( gate.armed() );                                  // the replacement is still armed
    ASSERT_TRUE( gate.accept( second, 15 * ms, 13 * ms ) );       // the genuine message, posted at its due time, fires
    ASSERT_FALSE( gate.armed() );
    // the tolerance only absorbs clock-read skew; the notifier / qtimer paths (no post time) keep the r2 rule
    PlaybackDueWakeGate skew;
    const quint32 g = skew.arm( 0, 10 * ms );
    ASSERT_TRUE( skew.accept( g, 10 * ms, 10 * ms - PlaybackDueWakeGate::kStaleDeliveryNs ) );
    PlaybackDueWakeGate none;
    const quint32 n = none.arm( 0, 10 * ms );
    ASSERT_TRUE( none.accept( n, 11 * ms, -1 ) );
    // the wiring: the native path hands the gate the message's post time, on the gate's (QPC) clock
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "if( !gate.accept( generation, nowNs(), postNs ) || !onFire ) return;" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "d->deliverGeneration( playbackDueWakeWParamGeneration( wParam ), static_cast<qint64>( m->lParam ) );" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "const quint32 generation = d->gate.arm( d->nowNs(), static_cast<qint64>( dueMs * 1000000.0 ) );" ) ) );
    ASSERT_FALSE( source.contains( QStringLiteral( "gate.accept( gate.generation(), clock." ) ) );   // no gate call on the other clock
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
    const int gate = wake.indexOf( QStringLiteral( "playbackDueWakeOutcome( m_frameStillDrawing, ui->actionDropFrameMode->isChecked()," ) );
    const int advance = wake.indexOf( QStringLiteral( "timerFrameEvent( true );" ) );
    ASSERT_TRUE( gate > 0 && advance > gate );
    const QString held = wake.mid( gate, advance - gate );
    ASSERT_TRUE( held.contains( QStringLiteral( "m_playbackDueWakePresentedFrame, ui->horizontalSliderPosition->value() );" ) ) );
    ASSERT_TRUE( held.contains( QStringLiteral( "++m_playbackDueWakeUnpresentedDeferred;" ) ) );
    // r2 adversary pass 6 A1: the hold leaves no pending advance (see EveryFrameAHeldWakeLeavesNoPendingAdvance...)
    ASSERT_TRUE( playbackDueWakeOutcome( false, false, presented, slider ) == PlaybackDueWakeOutcome::HoldUnpresented );
    ASSERT_TRUE( playbackDueWakeOutcome( false, false, 10, slider ) == PlaybackDueWakeOutcome::Advance );
    ASSERT_TRUE( playbackDueWakeOutcome( false, true, presented, slider ) == PlaybackDueWakeOutcome::Advance );
}

TEST(PlaybackDueWakeA1, EveryFrameAHeldWakeLeavesNoPendingAdvanceForTheEmptyFrameReadyPath)
{
    // r2 adversary pass 6 A1 input: Play, every-frame mode (dropFrame=false), 60 fps, cut 1..100, scale 1, admitted CUDA texture
    // route with a supported readback fallback, lookahead 2. Frame 8's present ends at 1001 ms (the tail requested 9 at 1000 ms).
    // Frame 9 / serial 109 is acquired and queued for background preparation at 1010 ms and its worker is descheduled until
    // 1020 ms. The due wake at 1016.917 ms sees drawing=false, slider=9, presented=8: it is held. Lookahead frame 10 / serial
    // 110 emits frameReady at 1017 ms, which takes drawFrameReady's empty path (no ready frame for target 9; the renderer idle).
    // On bb06bf2b the held wake set m_playbackFrameAdvancePending, the empty path took it without the gate, advanced 9 -> 10,
    // and preparation 110 replaced 109: frame 9 never presented.
    const long long presented = 8;
    const int slider = 9;
    const bool drawingAtWake = false;
    const PlaybackDueWakeOutcome atWake = playbackDueWakeOutcome( drawingAtWake, false, presented, slider );
    ASSERT_TRUE( atWake == PlaybackDueWakeOutcome::HoldUnpresented );
    // the held wake leaves no pending advance ...
    const bool pendingAfterWake = playbackDueWakeOutcomeLeavesPendingAdvance( atWake );
    ASSERT_FALSE( pendingAfterWake );
    // ... so frame 10's frameReady at 1017 ms (empty path: renderer idle) does not advance
    const bool drawingAtEmptyFrameReady = false;
    ASSERT_FALSE( playbackPendingAdvanceMayBeTaken( true, true, pendingAfterWake, drawingAtEmptyFrameReady, false, presented, slider ) );
    // and the class: whoever left the flag (a poll tick that found the renderer busy before 1010 ms, a busy wake), the empty
    // and lookahead-drop paths never advance over the unpresented 9 in every-frame mode; 9's present takes the flag
    ASSERT_FALSE( playbackPendingAdvanceMayBeTaken( true, true, true, drawingAtEmptyFrameReady, false, presented, slider ) );
    ASSERT_TRUE( playbackPendingAdvanceMayBeTaken( true, true, true, false, false, 9, slider ) );     // once 9's present ended
    ASSERT_TRUE( playbackPendingAdvanceMayBeTaken( true, true, true, false, true, presented, slider ) );   // drop-frame: unchanged
    ASSERT_TRUE( playbackPendingAdvanceMayBeTaken( true, true, true, false, false, -1, slider ) );    // nothing presented this Play
    ASSERT_FALSE( playbackPendingAdvanceMayBeTaken( true, true, true, true, true, 9, slider ) );      // busy never takes it
    ASSERT_FALSE( playbackPendingAdvanceMayBeTaken( true, false, true, false, true, 9, slider ) );    // not playing
    ASSERT_FALSE( playbackPendingAdvanceMayBeTaken( true, true, false, false, true, 9, slider ) );    // nothing pending

    // the wiring, in MainWindow.cpp
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_FALSE( source.isEmpty() );
    const QString wake = functionBody( source, QStringLiteral( "void MainWindow::onPlaybackDueWake( void )" ),
                                       QStringLiteral( "void MainWindow::cancelPlaybackDueWake( void )" ) );
    ASSERT_FALSE( wake.isEmpty() );
    // the wake sets the flag in exactly one place, and only for a busy renderer
    ASSERT_EQ( 1, wake.count( QStringLiteral( "m_playbackFrameAdvancePending = true;" ) ) );
    ASSERT_EQ( 1, wake.count( QStringLiteral(
        "if( playbackDueWakeOutcomeLeavesPendingAdvance( outcome ) ) m_playbackFrameAdvancePending = true;" ) ) );
    // the empty-frameReady advance (drawFrameReady, no ready frame) is gated
    const int empty = source.indexOf( QStringLiteral( "QStringLiteral(\"draw_frame_ready.empty\")" ) );
    const int emptyAdvance = source.indexOf( QStringLiteral( "\"drawFrameReady.empty_advance\"" ), empty );
    ASSERT_TRUE( empty > 0 && emptyAdvance > empty );
    const QString emptyPath = source.mid( empty, emptyAdvance - empty );
    ASSERT_TRUE( emptyPath.contains( QStringLiteral( "if( playbackPendingAdvanceMayBeTaken( mlvappPlaybackDueWakeEnabled(), ui->actionPlay->isChecked()," ) ) );
    ASSERT_TRUE( emptyPath.contains( QStringLiteral( "m_playbackDueWakePresentedFrame, ui->horizontalSliderPosition->value() ) )" ) ) );
    ASSERT_FALSE( emptyPath.contains( QStringLiteral( "&& m_playbackFrameAdvancePending )" ) ) );
    // the lookahead-drop advance (its deferred lambda) is gated the same way, after it re-reads the renderer
    const int drop = source.indexOf( QStringLiteral( "QTimer::singleShot( 0, this, [this, display_frame]()" ) );
    const int dropAdvance = source.indexOf( QStringLiteral( "\"drawFrameReady.lookahead_drop_advance\"" ), drop );
    ASSERT_TRUE( drop > 0 && dropAdvance > drop );
    const QString dropPath = source.mid( drop, dropAdvance - drop );
    const int reread = dropPath.indexOf( QStringLiteral( "renderThreadBusyForPlaybackAdvance( currentPlaybackAdvanceTarget() );" ) );
    const int dropGate = dropPath.indexOf( QStringLiteral( "if( !playbackPendingAdvanceMayBeTaken( mlvappPlaybackDueWakeEnabled(), ui->actionPlay->isChecked()," ) );
    const int dropTimer = dropPath.indexOf( QStringLiteral( "timerFrameEvent();" ) );
    ASSERT_TRUE( reread > 0 && dropGate > reread && dropTimer > dropGate );
    // no other site takes the pending flag outside the poll tick (timerFrameEvent) and these two
    ASSERT_EQ( 2, source.count( QStringLiteral( "playbackPendingAdvanceMayBeTaken(" ) ) );
}

TEST(PlaybackDueWakeNative, TheMessageCarriesItsPostTimeAndTheLatenessIsSplitAtThePost)
{
    // r3: the wake's lateness is split into due-to-post and post-to-receive, so the fullscreen gap can be read as delivery
    // (the timer / wait thread) or GUI-thread occupancy (successor HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1)
    const quint64 packed = playbackDueWakePackWParam( 0xFFFFFFF0u, static_cast<quint32>( PlaybackDueWake::kNativeWakeTag ) );
    ASSERT_EQ( 0xFFFFFFF0u, playbackDueWakeWParamGeneration( packed ) );
    ASSERT_EQ( static_cast<quint32>( PlaybackDueWake::kNativeWakeTag ), playbackDueWakeWParamTag( packed ) );
    ASSERT_EQ( 0u, playbackDueWakeWParamTag( 7u ) );   // a bare generation carries no tag
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral(
        "\" post_stamped=%1 due_to_post_p50_ms=%2 due_to_post_p95_ms=%3 \"" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "\"post_to_receive_p50_ms=%4 post_to_receive_p95_ms=%5\"" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "m_playbackDueWakeDueToPostMs.add( postMs - m_playbackDueWakeDueAtMs );" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "m_playbackDueWakePostToReceiveMs.add( nowMs - postMs );" ) ) );
#ifdef Q_OS_WIN
    LARGE_INTEGER frequency;
    ASSERT_TRUE( QueryPerformanceFrequency( &frequency ) && frequency.QuadPart > 0 );
    const auto qpcMs = [&frequency]() -> double
    {
        LARGE_INTEGER now;
        QueryPerformanceCounter( &now );
        return static_cast<double>( now.QuadPart ) * 1000.0 / static_cast<double>( frequency.QuadPart );
    };
    double postMs = -2.0;
    double receivedMs = -2.0;
    PlaybackDueWake *self = nullptr;
    PlaybackDueWake wake( nullptr, [&]() { postMs = self->lastDeliveryPostMs(); receivedMs = qpcMs(); } );
    self = &wake;
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    const double armedMs = qpcMs();
    wake.arm( 1.0 );
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 200 ) );
    ASSERT_TRUE( postMs >= armedMs );            // posted after the arm, on the same QPC clock
    ASSERT_TRUE( receivedMs >= postMs );         // received after the post
    ASSERT_TRUE( postMs - armedMs < 1000.0 );
    ASSERT_TRUE( wake.lastDeliveryPostMs() < 0.0 );   // only meaningful inside the fire callback
    // the notifier path carries no post time
    PlaybackDueWake notifierWake( nullptr, []() {}, false );
    ASSERT_TRUE( notifierWake.delivery() == PlaybackDueWake::Delivery::Notifier );
    ASSERT_TRUE( notifierWake.lastDeliveryPostMs() < 0.0 );
#endif
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

namespace
{
// the r3 summary line's percentile over its unbounded std::vector (MainWindow.cpp at 9ca4c36b), kept as the reference
double unboundedPercentile( std::vector<double> values, double p )
{
    if( values.empty() ) return 0.0;
    const size_t index = static_cast<size_t>( std::floor( p * static_cast<double>( values.size() - 1 ) + 0.5 ) );
    std::nth_element( values.begin(), values.begin() + static_cast<std::ptrdiff_t>( index ), values.end() );
    return values[index];
}

// deterministic wake-lateness-shaped samples in [lo, hi)
struct LatenessSource
{
    std::uint64_t state = 0x9E3779B97F4A7C15ull;
    double next( double lo, double hi )
    {
        state = state * 6364136223846793005ull + 1442695040888963407ull;
        const double unit = static_cast<double>( state >> 11 ) / 9007199254740992.0;
        return lo + unit * ( hi - lo );
    }
};
}

TEST(PlaybackDueWakeSamplesB1, ALongLoopingPlayKeepsTheSampleStorageBounded)
{
    // Dot B1 (CoS 6609540072 on 21fc60dd): every due wake appended a double, and every native delivery two more, for the whole
    // Play with no bound (about 4.9 MiB/h at 60 wakes/s); clear() at the next Play start kept the capacity. Every ordinary Play
    // is a playback_smoke session, so the bound must be in the storage. 1e6 wakes = 4.6 h of looping Play at 60 fps.
    PlaybackDueWakeSamples samples;
    std::vector<double> unbounded;   // the r3 collector
    LatenessSource source;
    for( int i = 0; i < 1000000; ++i )
    {
        const double lateness = source.next( -0.5, 16.0 );
        samples.add( lateness );
        unbounded.push_back( lateness );
        if( i % 4096 == 0 ) ASSERT_TRUE( samples.storageBytes() <= PlaybackDueWakeSamples::maxStorageBytes() );
    }
    ASSERT_TRUE( samples.storageBytes() <= PlaybackDueWakeSamples::maxStorageBytes() );
    ASSERT_TRUE( PlaybackDueWakeSamples::maxStorageBytes() <= 256u * 1024u );
    ASSERT_EQ( static_cast<std::size_t>( 1000000 ), samples.size() );
    ASSERT_FALSE( samples.exact() );
    // past the exact store the percentiles stay within half a bin of the unbounded vector's
    ASSERT_NEAR( unboundedPercentile( unbounded, 0.50 ), samples.percentile( 0.50 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
    ASSERT_NEAR( unboundedPercentile( unbounded, 0.95 ), samples.percentile( 0.95 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
    // the r3 vector held 7.6 MiB or more for the same Play, and kept it after clear()
    ASSERT_TRUE( unbounded.capacity() * sizeof( double ) > 30u * PlaybackDueWakeSamples::maxStorageBytes() );
    unbounded.clear();
    ASSERT_TRUE( unbounded.capacity() * sizeof( double ) > 30u * PlaybackDueWakeSamples::maxStorageBytes() );
    // the Play-start reset releases the storage, and the next (measured) Play is exact again
    samples.reset();
    ASSERT_EQ( static_cast<std::size_t>( 0 ), samples.storageBytes() );
    ASSERT_EQ( static_cast<std::size_t>( 0 ), samples.size() );
    ASSERT_TRUE( samples.exact() );
    for( int i = 0; i < 1000; ++i ) samples.add( source.next( 0.0, 4.0 ) );
    ASSERT_TRUE( samples.exact() );
    ASSERT_TRUE( samples.storageBytes() <= PlaybackDueWakeSamples::kExactCapacity * sizeof( double ) );
}

TEST(PlaybackDueWakeSamplesB1, MeasuredPlayPercentilesAreTheUnboundedCollectorsOnAKnownSampleSet)
{
    const double ps[] = { 0.0, 0.05, 0.50, 0.95, 1.0 };
    ASSERT_EQ( 0.0, PlaybackDueWakeSamples().percentile( 0.50 ) );   // no samples: 0.000, as before
    // up to the exact capacity (a measured Play is seconds long): identical, bit for bit
    const std::size_t sizes[] = { 1, 2, 7, 1000, 9999, PlaybackDueWakeSamples::kExactCapacity };
    LatenessSource source;
    for( const std::size_t n : sizes )
    {
        PlaybackDueWakeSamples samples;
        std::vector<double> reference;
        for( std::size_t i = 0; i < n; ++i )
        {
            const double v = source.next( -3.0, 25.0 );
            samples.add( v );
            reference.push_back( v );
        }
        ASSERT_TRUE( samples.exact() );
        for( const double p : ps ) ASSERT_EQ( unboundedPercentile( reference, p ), samples.percentile( p ) );
    }
    // a known set: 0..99 ms in reverse
    PlaybackDueWakeSamples known;
    std::vector<double> knownReference;
    for( int i = 99; i >= 0; --i ) { known.add( i ); knownReference.push_back( i ); }
    ASSERT_EQ( 50.0, known.percentile( 0.50 ) );   // floor( 0.5 * 99 + 0.5 ) = 50
    ASSERT_EQ( 94.0, known.percentile( 0.95 ) );   // floor( 0.95 * 99 + 0.5 ) = 94
    ASSERT_EQ( unboundedPercentile( knownReference, 0.95 ), known.percentile( 0.95 ) );
    // past the exact capacity: the same rank, within half a bin, inside the histogram range
    PlaybackDueWakeSamples binned;
    std::vector<double> binnedReference;
    for( int i = 0; i < 200000; ++i )
    {
        const double v = source.next( -19.0, 79.0 );
        binned.add( v );
        binnedReference.push_back( v );
    }
    ASSERT_FALSE( binned.exact() );
    for( const double p : ps )
        ASSERT_NEAR( unboundedPercentile( binnedReference, p ), binned.percentile( p ),
                     PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
}

TEST(PlaybackDueWakeSamplesB1, MainWindowKeepsNoUnboundedDueWakeSampleVector)
{
    // RED on 9ca4c36b: MainWindow.h declared three std::vector<double> sample members, onPlaybackDueWake push_back'ed into
    // them, and resetPlaybackDueWakeCounters clear()ed them (capacity kept)
    const QString header = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.h" ) );
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_FALSE( header.isEmpty() );
    ASSERT_FALSE( source.isEmpty() );
    ASSERT_FALSE( header.contains( QRegularExpression( QStringLiteral( "std::vector<double>[ ]+m_playbackDueWake" ) ) ) );
    ASSERT_EQ( 3, header.count( QRegularExpression( QStringLiteral( "PlaybackDueWakeSamples[ ]+m_playbackDueWake[A-Za-z]+Ms;" ) ) ) );
    ASSERT_FALSE( source.contains( QRegularExpression( QStringLiteral( "m_playbackDueWake[A-Za-z]*Ms[.](push_back|emplace_back|clear)[(]" ) ) ) );
    const QString reset = functionBody( source, QStringLiteral( "void MainWindow::resetPlaybackDueWakeCounters( void )" ),
                                        QStringLiteral( "QString MainWindow::playbackDueWakeSummaryLine( void ) const" ) );
    ASSERT_FALSE( reset.isEmpty() );
    ASSERT_EQ( 1, reset.count( QStringLiteral( "m_playbackDueWakeLatenessMs.reset();" ) ) );
    ASSERT_EQ( 1, reset.count( QStringLiteral( "m_playbackDueWakeDueToPostMs.reset();" ) ) );
    ASSERT_EQ( 1, reset.count( QStringLiteral( "m_playbackDueWakePostToReceiveMs.reset();" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "m_playbackDueWakeLatenessMs.add( nowMs - m_playbackDueWakeDueAtMs );" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "QStringLiteral( \" samples_binned=%1\" )" ) ) );
}

TEST(PlaybackDueWakeI1, WithTheWakeOffThePendingAdvanceGateIsTheBaseCondition)
{
    // Dot I1: MLVAPP_PLAYBACK_DUE_WAKE=0 stopped the arming but kept the every-frame "only past the presented frame" gate on the
    // empty-frameReady and lookahead-drop advances, so wake-off was not the base build. Off, the gate is the base condition:
    // playing && pending && !drawing (MainWindow.cpp at 03b9ea28, both sites).
    const long long presentedFrames[] = { -1, 8, 9 };
    int differs = 0;
    for( int bits = 0; bits < 16; ++bits )
    {
        const bool play = ( bits & 1 ) != 0, pending = ( bits & 2 ) != 0, drawing = ( bits & 4 ) != 0, dropFrame = ( bits & 8 ) != 0;
        for( const long long presented : presentedFrames )
        {
            const bool base = play && pending && !drawing;
            ASSERT_EQ( base, playbackPendingAdvanceMayBeTaken( false, play, pending, drawing, dropFrame, presented, 9 ) );
            if( base != playbackPendingAdvanceMayBeTaken( true, play, pending, drawing, dropFrame, presented, 9 ) ) ++differs;
        }
    }
    ASSERT_TRUE( differs > 0 );   // on, the every-frame hold (presented 8, slider 9) still applies
    ASSERT_TRUE( playbackPendingAdvanceMayBeTaken( false, true, true, false, false, 8, 9 ) );   // the held shape, wake off
    ASSERT_FALSE( playbackPendingAdvanceMayBeTaken( true, true, true, false, false, 8, 9 ) );   // the held shape, wake on
    // both MainWindow sites pass the kill switch
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 2, source.count( QStringLiteral( "playbackPendingAdvanceMayBeTaken( mlvappPlaybackDueWakeEnabled(), ui->actionPlay->isChecked()," ) ) );
}

// r5 (Dot B2, CoS on a5c8dc46): past the 16,384-value exact store the r4 histogram clamped an out-of-range sample into its
// edge bin, and the summary line printed that bin's centre as a measured percentile. Every assertion below on percentile()
// also compiles against the r4 header, and is RED there (79.995 / -19.995 where the rank is censored).
namespace
{
PlaybackDueWakeSamples dueWakeSamplesOf( std::size_t n, double ms )
{
    PlaybackDueWakeSamples samples;
    for( std::size_t i = 0; i < n; ++i ) samples.add( ms );
    return samples;
}
}

TEST(PlaybackDueWakeSamplesB2, AnUpperTailPastTheExactStoreIsCensoredNotTheEdgeBin)
{
    // the Dot B2 repro: 16,385 wakes of 500 ms gave p50 = p95 = 79.995 ms
    const PlaybackDueWakeSamples late = dueWakeSamplesOf( PlaybackDueWakeSamples::kExactCapacity + 1, 500.0 );
    ASSERT_FALSE( late.exact() );
    ASSERT_TRUE( std::isnan( late.percentile( 0.50 ) ) );
    ASSERT_TRUE( std::isnan( late.percentile( 0.95 ) ) );
    ASSERT_TRUE( std::isnan( late.percentile( 0.0 ) ) );
    ASSERT_TRUE( late.rank( 0.50 ).censor == PlaybackDueWakeSamples::Censor::Overflow );
    ASSERT_EQ( std::string( ">=80.000" ), playbackDueWakePercentileText( late, 0.50 ).toStdString() );
    ASSERT_EQ( std::string( ">=80.000" ), playbackDueWakePercentileText( late, 0.95 ).toStdString() );
    ASSERT_EQ( PlaybackDueWakeSamples::kExactCapacity + 1, late.overflow() );
    ASSERT_EQ( static_cast<std::size_t>( 0 ), late.underflow() );
    // the upper bound itself is out of range ([kMinMs, kMaxMs)); one ulp below it is the last bin
    const PlaybackDueWakeSamples atMax = dueWakeSamplesOf( PlaybackDueWakeSamples::kExactCapacity + 1, PlaybackDueWakeSamples::kMaxMs );
    ASSERT_TRUE( std::isnan( atMax.percentile( 0.50 ) ) );
    const PlaybackDueWakeSamples belowMax =
        dueWakeSamplesOf( PlaybackDueWakeSamples::kExactCapacity + 1, std::nextafter( PlaybackDueWakeSamples::kMaxMs, 0.0 ) );
    ASSERT_NEAR( PlaybackDueWakeSamples::kMaxMs, belowMax.percentile( 0.50 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
    ASSERT_EQ( static_cast<std::size_t>( 0 ), belowMax.overflow() );
}

TEST(PlaybackDueWakeSamplesB2, ALowerTailPastTheExactStoreIsCensoredNotTheEdgeBin)
{
    // below -20 ms saturated the same way (-19.995)
    const PlaybackDueWakeSamples early = dueWakeSamplesOf( PlaybackDueWakeSamples::kExactCapacity + 1, -50.0 );
    ASSERT_FALSE( early.exact() );
    ASSERT_TRUE( std::isnan( early.percentile( 0.50 ) ) );
    ASSERT_TRUE( std::isnan( early.percentile( 1.0 ) ) );
    ASSERT_TRUE( early.rank( 0.95 ).censor == PlaybackDueWakeSamples::Censor::Underflow );
    ASSERT_EQ( std::string( "<=-20.000" ), playbackDueWakePercentileText( early, 0.50 ).toStdString() );
    ASSERT_EQ( PlaybackDueWakeSamples::kExactCapacity + 1, early.underflow() );
    ASSERT_EQ( static_cast<std::size_t>( 0 ), early.overflow() );
    // the lower bound itself is in range (bin 0); NaN is never in range and counts as underflow, not as bin 0
    const PlaybackDueWakeSamples atMin = dueWakeSamplesOf( PlaybackDueWakeSamples::kExactCapacity + 1, PlaybackDueWakeSamples::kMinMs );
    ASSERT_NEAR( PlaybackDueWakeSamples::kMinMs, atMin.percentile( 0.50 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
    ASSERT_EQ( static_cast<std::size_t>( 0 ), atMin.underflow() );
    PlaybackDueWakeSamples withNan = dueWakeSamplesOf( PlaybackDueWakeSamples::kExactCapacity, 5.0 );
    withNan.add( std::numeric_limits<double>::quiet_NaN() );
    ASSERT_EQ( static_cast<std::size_t>( 1 ), withNan.underflow() );
    ASSERT_TRUE( std::isnan( withNan.percentile( 0.0 ) ) );
    ASSERT_NEAR( 5.0, withNan.percentile( 0.50 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
}

TEST(PlaybackDueWakeSamplesB2, TheSpillTransitionMeasuresExactlyUpToTheCapacityAndCensorsPastIt)
{
    // exactly kExactCapacity samples: still the exact store, so even out-of-range values are measured values
    PlaybackDueWakeSamples late = dueWakeSamplesOf( PlaybackDueWakeSamples::kExactCapacity, 500.0 );
    ASSERT_TRUE( late.exact() );
    ASSERT_EQ( 500.0, late.percentile( 0.50 ) );
    ASSERT_EQ( 500.0, late.percentile( 0.95 ) );
    ASSERT_EQ( std::string( "500.000" ), playbackDueWakePercentileText( late, 0.95 ).toStdString() );
    ASSERT_EQ( PlaybackDueWakeSamples::kExactCapacity, late.overflow() );   // disclosed even while exact
    // one more: the spill; the ranks are now in the overflow, so they are censored
    late.add( 500.0 );
    ASSERT_FALSE( late.exact() );
    ASSERT_TRUE( std::isnan( late.percentile( 0.50 ) ) );
    ASSERT_EQ( std::string( ">=80.000" ), playbackDueWakePercentileText( late, 0.50 ).toStdString() );
    ASSERT_EQ( PlaybackDueWakeSamples::kExactCapacity + 1, late.overflow() );
    // the Play-start reset clears the disclosure counts with the storage
    late.reset();
    ASSERT_EQ( static_cast<std::size_t>( 0 ), late.overflow() );
    ASSERT_EQ( static_cast<std::size_t>( 0 ), late.underflow() );
    ASSERT_EQ( std::string( "0.000" ), playbackDueWakePercentileText( late, 0.50 ).toStdString() );   // no samples, as before
}

TEST(PlaybackDueWakeSamplesB2, AMixedSetKeepsInRangeRanksExactToTheBinAndCensorsTheTails)
{
    // 500 below the range, 12,000 in it, 4,000 above: 16,500 samples, past the exact store
    PlaybackDueWakeSamples mixed;
    std::vector<double> reference;
    LatenessSource source;
    for( int i = 0; i < 500; ++i ) { const double v = source.next( -60.0, -20.5 ); mixed.add( v ); reference.push_back( v ); }
    for( int i = 0; i < 12000; ++i ) { const double v = source.next( -19.0, 79.0 ); mixed.add( v ); reference.push_back( v ); }
    for( int i = 0; i < 4000; ++i ) { const double v = source.next( 80.5, 900.0 ); mixed.add( v ); reference.push_back( v ); }
    ASSERT_FALSE( mixed.exact() );
    ASSERT_EQ( static_cast<std::size_t>( 500 ), mixed.underflow() );
    ASSERT_EQ( static_cast<std::size_t>( 4000 ), mixed.overflow() );
    // nearest rank over 16,500: index = floor( p * 16499 + 0.5 ); [0, 500) underflow, [500, 12500) in range, the rest overflow
    const double underflowPs[] = { 0.0, 0.01, 0.03 };                    // indices 0, 165, 495
    const double inRangePs[] = { 0.0304, 0.05, 0.50, 0.70, 0.7575 };     // indices 502, 825, 8250, 11549, 12498
    const double overflowPs[] = { 0.7577, 0.80, 0.95, 1.0 };             // indices 12501, 13199, 15674, 16499
    for( const double p : underflowPs )
    {
        ASSERT_TRUE( std::isnan( mixed.percentile( p ) ) );
        ASSERT_EQ( std::string( "<=-20.000" ), playbackDueWakePercentileText( mixed, p ).toStdString() );
    }
    for( const double p : inRangePs )
    {
        ASSERT_TRUE( mixed.rank( p ).censor == PlaybackDueWakeSamples::Censor::None );
        ASSERT_NEAR( unboundedPercentile( reference, p ), mixed.percentile( p ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
    }
    for( const double p : overflowPs )
    {
        ASSERT_TRUE( std::isnan( mixed.percentile( p ) ) );
        ASSERT_EQ( std::string( ">=80.000" ), playbackDueWakePercentileText( mixed, p ).toStdString() );
    }
}

TEST(PlaybackDueWakeSamplesB2, TheLongPlaySummaryLineDisclosesEachSetsRangeAndCensoring)
{
    // the disclosure fields one set adds to the playback_due_wake line, after a long Play past the exact store
    PlaybackDueWakeSamples late = dueWakeSamplesOf( PlaybackDueWakeSamples::kExactCapacity + 1, 500.0 );
    late.add( -50.0 );
    ASSERT_EQ( std::string( " late_exact=0 late_underflow=1 late_overflow=16385 late_range_ms=-20.000:80.000" ),
               playbackDueWakeRangeFields( late, "late" ).toStdString() );
    const PlaybackDueWakeSamples measured = dueWakeSamplesOf( 3, 4.25 );
    ASSERT_EQ( std::string( " post_to_receive_exact=1 post_to_receive_underflow=0 post_to_receive_overflow=0 "
                            "post_to_receive_range_ms=-20.000:80.000" ),
               playbackDueWakeRangeFields( measured, "post_to_receive" ).toStdString() );
    ASSERT_EQ( std::string( "4.250" ), playbackDueWakePercentileText( measured, 0.50 ).toStdString() );
    // MainWindow emits every percentile of all three sets through the censoring text, and each set's disclosure fields
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString line = functionBody( source, QStringLiteral( "QString MainWindow::playbackDueWakeSummaryLine( void ) const" ),
                                       QStringLiteral( "//Paint the Audio Track Wave to GUI" ) );
    ASSERT_FALSE( line.isEmpty() );
    ASSERT_EQ( 6, line.count( QStringLiteral( "playbackDueWakePercentileText( m_playbackDueWake" ) ) );
    const char *const ranks[] = { "LatenessMs, 0.50 )", "LatenessMs, 0.95 )", "DueToPostMs, 0.50 )", "DueToPostMs, 0.95 )",
                                  "PostToReceiveMs, 0.50 )", "PostToReceiveMs, 0.95 )" };
    for( const char *rank : ranks )
        ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakePercentileText( m_playbackDueWake" ) + QLatin1String( rank ) ) );
    ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakeLatenessMs, \"late\" )" ) ) );
    ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakeDueToPostMs, \"due_to_post\" )" ) ) );
    ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakePostToReceiveMs, \"post_to_receive\" )" ) ) );
    // no raw double percentile is formatted on the line any more (that was the clamped value's path)
    ASSERT_EQ( 0, line.count( QStringLiteral( ".percentile(" ) ) );
    ASSERT_EQ( 0, line.count( QStringLiteral( "0, 'f', 3 )" ) ) );
}

TEST(PlaybackDueWakeSamplesB2, ABinNeverWrapsBeforeThePlaysSampleCount)
{
    // r5 adversary pass 8 A1 input: add( 5.0 ) exactly 4,294,967,296 times without a reset. A uint32_t bin wrapped to 0 while
    // the size_t count did not, so the rank scan fell through to a false Overflow (NaN, ">=80.000", overflow() == 0).
    // addRepeated is that sequence of add() calls in O(1) once past the exact store.
    ASSERT_TRUE( std::numeric_limits<PlaybackDueWakeSamples::BinCount>::max() >= std::numeric_limits<std::size_t>::max() );
    const std::size_t n = static_cast<std::size_t>( 4294967296ull );
    PlaybackDueWakeSamples samples;
    samples.addRepeated( 5.0, n );
    ASSERT_EQ( n, samples.size() );
    ASSERT_FALSE( samples.exact() );
    ASSERT_EQ( static_cast<std::size_t>( 0 ), samples.overflow() );
    ASSERT_EQ( static_cast<std::size_t>( 0 ), samples.underflow() );
    ASSERT_TRUE( samples.rank( 0.50 ).censor == PlaybackDueWakeSamples::Censor::None );
    ASSERT_NEAR( 5.0, samples.percentile( 0.50 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
    ASSERT_NEAR( 5.0, samples.percentile( 0.95 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
    ASSERT_FALSE( playbackDueWakePercentileText( samples, 0.95 ).startsWith( QStringLiteral( ">=" ) ) );   // measured, not censored
    // addRepeated is the same state as the add() loop, across the spill and in all three ranges
    PlaybackDueWakeSamples looped, bulk;
    for( int i = 0; i < 20000; ++i ) looped.add( 3.0 );
    for( int i = 0; i < 300; ++i ) looped.add( -40.0 );
    for( int i = 0; i < 700; ++i ) looped.add( 120.0 );
    bulk.addRepeated( 3.0, 20000 );
    bulk.addRepeated( -40.0, 300 );
    bulk.addRepeated( 120.0, 700 );
    ASSERT_EQ( looped.size(), bulk.size() );
    ASSERT_EQ( looped.underflow(), bulk.underflow() );
    ASSERT_EQ( looped.overflow(), bulk.overflow() );
    for( const double p : { 0.0, 0.01, 0.5, 0.95, 0.99, 1.0 } )
        ASSERT_EQ( playbackDueWakePercentileText( looped, p ).toStdString(), playbackDueWakePercentileText( bulk, p ).toStdString() );
    // the storage bound still holds with 64-bit bins
    ASSERT_TRUE( samples.storageBytes() <= PlaybackDueWakeSamples::maxStorageBytes() );
    ASSERT_TRUE( PlaybackDueWakeSamples::maxStorageBytes() <= 256u * 1024u );
}

TEST(PlaybackDueWakeSamplesB2, ThePerPlayDueWakeCountersAreUnsigned64Bit)
{
    // r5 adversary pass 8 A2 input: 2^31 accepted due-wake callbacks in one looping Play. The counters were signed int, so the
    // increment past INT_MAX was undefined behaviour. MainWindow cannot be built in the console suite: pin the declarations.
    const QString header = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.h" ) );
    ASSERT_FALSE( header.isEmpty() );
    const char *const counters[] = { "Armed", "Fired", "Advanced", "Rearmed", "BusyDeferred", "UnpresentedDeferred" };
    for( const char *counter : counters )
    {
        const QString name = QStringLiteral( "m_playbackDueWake" ) + QLatin1String( counter ) + QStringLiteral( " = 0;" );
        ASSERT_EQ( 1, header.count( QStringLiteral( "quint64 " ) + name ) );
        ASSERT_EQ( 0, header.count( QRegularExpression( QStringLiteral( "\\bint[ ]+" ) + QRegularExpression::escape( name ) ) ) );
    }
    // the only int one is the per-present re-arm count, which is reset every present and capped
    ASSERT_EQ( 1, header.count( QStringLiteral( "int m_playbackDueWakeRearmsThisPresent = 0;" ) ) );
    ASSERT_TRUE( std::numeric_limits<quint64>::max() >= 18446744073709551615ull );
}
