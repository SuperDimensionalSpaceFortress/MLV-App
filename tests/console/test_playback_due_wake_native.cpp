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
#include <functional>
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
    // r5 (pass 10 A3): the first stamp still reaches the gate; the post-return time is the third argument (pass 9 A1)
    ASSERT_EQ( 1, source.count( QStringLiteral( "const qint64 postTicks = static_cast<qint64>( m->lParam );" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "d->deliverGeneration( playbackDueWakeWParamGeneration( wParam ), postTicks, published ? returnTicks : 0 );" ) ) );
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
    // r6 (adversary pass 10 A2): the split is the [lo, hi] bounds of the two stamps, never a point
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral(
        "\" post_stamped=%1 due_to_post_p50_lo_ms=%2 due_to_post_p50_hi_ms=%3 \"" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "\"post_to_receive_p50_lo_ms=%6 post_to_receive_p50_hi_ms=%7 \"" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "playbackDueWakeSplitBounds( m_playbackDueWakeDueAtMs, firstStampMs, postMs, nowMs );" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "m_playbackDueWakeDueToPostLoMs.add( bounds.dueToPostLoMs );" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "m_playbackDueWakePostToReceiveHiMs.add( bounds.postToReceiveHiMs );" ) ) );
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
    bool unsplit = false;
    PlaybackDueWake *self = nullptr;
    PlaybackDueWake wake( nullptr, [&]() { postMs = self->lastDeliveryPostMs(); unsplit = self->lastDeliveryUnsplit(); receivedMs = qpcMs(); } );
    self = &wake;
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    // r5 pass 9 A1: a delivery taken before the wait thread published PostMessageW's return time is unsplit (< 0); retry
    double armedMs = qpcMs();
    for( int attempt = 0; attempt < 20; ++attempt )
    {
        armedMs = qpcMs();
        wake.arm( 1.0 );
        ASSERT_EQ( 1, pumpDueWakeMessages( wake, 200 ) );
        ASSERT_TRUE( ( postMs >= 0.0 ) != unsplit );   // split at the return time, or counted unsplit: never both, never neither
        if( postMs >= 0.0 ) break;
    }
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
    // r6 (pass 10 A2): the lateness, and the split's four bounds and its post-call span
    ASSERT_EQ( 6, header.count( QRegularExpression( QStringLiteral( "PlaybackDueWakeSamples[ ]+m_playbackDueWake[A-Za-z]+Ms;" ) ) ) );
    ASSERT_FALSE( source.contains( QRegularExpression( QStringLiteral( "m_playbackDueWake[A-Za-z]*Ms[.](push_back|emplace_back|clear)[(]" ) ) ) );
    const QString reset = functionBody( source, QStringLiteral( "void MainWindow::resetPlaybackDueWakeCounters( void )" ),
                                        QStringLiteral( "QString MainWindow::playbackDueWakeSummaryLine( void ) const" ) );
    ASSERT_FALSE( reset.isEmpty() );
    ASSERT_EQ( 1, reset.count( QStringLiteral( "m_playbackDueWakeLatenessMs.reset();" ) ) );
    const char *const splitSets[] = { "DueToPostLo", "DueToPostHi", "PostToReceiveLo", "PostToReceiveHi", "PostCall" };
    for( const char *set : splitSets )
        ASSERT_EQ( 1, reset.count( QStringLiteral( "m_playbackDueWake" ) + QLatin1String( set ) + QStringLiteral( "Ms.reset();" ) ) );
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
    // r6 (pass 10 A2): lateness p50 / p95 and the post-call span at p50 / p95 are values; the four split bounds at p50 / p95
    // are bounds, rounded outward (pass 11 A1)
    ASSERT_EQ( 4, line.count( QStringLiteral( "playbackDueWakePercentileText( m_playbackDueWake" ) ) );
    const char *const ranks[] = { "LatenessMs, 0.50 )", "LatenessMs, 0.95 )", "PostCallMs, 0.50 )", "PostCallMs, 0.95 )" };
    for( const char *rank : ranks )
        ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakePercentileText( m_playbackDueWake" ) + QLatin1String( rank ) ) );
    ASSERT_EQ( 8, line.count( QStringLiteral( "playbackDueWakePercentileBoundText( m_playbackDueWake" ) ) );
    const char *const bounds[] = { "DueToPostLoMs, 0.50, PlaybackDueWakeBoundSide::Lower )",
                                   "DueToPostLoMs, 0.95, PlaybackDueWakeBoundSide::Lower )",
                                   "DueToPostHiMs, 0.50, PlaybackDueWakeBoundSide::Upper )",
                                   "DueToPostHiMs, 0.95, PlaybackDueWakeBoundSide::Upper )",
                                   "PostToReceiveLoMs, 0.50, PlaybackDueWakeBoundSide::Lower )",
                                   "PostToReceiveLoMs, 0.95, PlaybackDueWakeBoundSide::Lower )",
                                   "PostToReceiveHiMs, 0.50, PlaybackDueWakeBoundSide::Upper )",
                                   "PostToReceiveHiMs, 0.95, PlaybackDueWakeBoundSide::Upper )" };
    for( const char *bound : bounds )
        ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakePercentileBoundText( m_playbackDueWake" ) + QLatin1String( bound ) ) );
    ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakeLatenessMs, \"late\" )" ) ) );
    ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakeDueToPostLoMs, \"due_to_post_lo\" )" ) ) );
    ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakeDueToPostHiMs, \"due_to_post_hi\" )" ) ) );
    ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakePostToReceiveLoMs, \"post_to_receive_lo\" )" ) ) );
    ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakePostToReceiveHiMs, \"post_to_receive_hi\" )" ) ) );
    ASSERT_EQ( 1, line.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakePostCallMs, \"post_call\" )" ) ) );
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
    // r5 adversary pass 10 A5 input: SIZE_MAX samples, rank( 1.0 ) -- the endpoint rank rounded to 2^64 (out-of-range
    // conversion); clamped, it is the last sample. A p outside [0, 1] clamps the same way.
    PlaybackDueWakeSamples huge;
    huge.addRepeated( 5.0, std::numeric_limits<std::size_t>::max() );
    ASSERT_TRUE( huge.rank( 1.0 ).censor == PlaybackDueWakeSamples::Censor::None );
    ASSERT_NEAR( 5.0, huge.percentile( 1.0 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
    ASSERT_NEAR( 5.0, huge.percentile( -0.5 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
    ASSERT_NEAR( 5.0, huge.percentile( 1.5 ), PlaybackDueWakeSamples::kBinMs / 2.0 + 1e-9 );
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

TEST(PlaybackDueWakeNative, AFailedReArmCancelsTheReplacedNativeTimer)
{
    // r5 adversary pass 9 A2 input: a native arm(50) succeeds; arm(5) takes the SetWaitableTimer failure branch (QTimer). The
    // replaced waitable timer stayed set: at 50 ms its wait thread posted the NEW generation, posted after the new due time,
    // so the abandoned timer fired the replacement arm on the native path. Qt timers are not dispatched here (no
    // processEvents), so only the native path could deliver.
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    wake.arm( 50.0 );
    wake.setWaitableTimerFailsForTest( true );
    wake.arm( 5.0 );
    ASSERT_TRUE( wake.lastArmDelivery() == PlaybackDueWake::Delivery::QtTimer );
    ASSERT_EQ( 0, pumpDueWakeMessages( wake, 150 ) );   // the replaced timer was cancelled: nothing is posted
    ASSERT_EQ( 0, fired );
    // the fallback QTimer is what delivers the replacement arm, once
    const ULONGLONG deadline = GetTickCount64() + 500;
    while( fired < 1 && GetTickCount64() < deadline )
    {
        QCoreApplication::processEvents( QEventLoop::AllEvents, 5 );
        Sleep( 1 );
    }
    ASSERT_EQ( 1, fired );
#endif
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "        CancelWaitableTimer( d->handle );\n"
                                                "        d->postGeneration.store( 0, std::memory_order_release );\n"
                                                "        d->gate.renew();\n"
                                                "        d->lastArmQtTimer = true;" ) ) );
}

TEST(PlaybackDueWakeNative, TheLatenessSplitsAtThePostReturnNotTheFirstStamp)
{
    // r5 adversary pass 9 A1 input: due at 100 ms, the wait thread stamps 100 ms, is descheduled before PostMessageW until
    // 140 ms, and the GUI thread receives at 140.1 ms. Split at the first stamp, due-to-post read 0 and post-to-receive 40.1 ms
    // (the wait thread's pause charged to the GUI queue). The split point is now PostMessageW's return, published by the wait
    // thread through a seqlock keyed by the first stamp; a delivery taken before it is published is unsplit, never split early.
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_FALSE( source.isEmpty() );
    const int post = source.indexOf( QStringLiteral( "PostMessageW( args.window, PlaybackDueWake::kNativeWakeMessage," ) );
    const int invalidate = source.indexOf( QStringLiteral( "args.postReturnFor->store( 0 );" ), post );
    const int write = source.indexOf( QStringLiteral( "args.postReturnQpc->store( returnQpc.QuadPart );" ), post );
    const int validate = source.indexOf( QStringLiteral( "args.postReturnFor->store( postQpc.QuadPart );" ), post );
    ASSERT_TRUE( post > 0 && invalidate > post && write > invalidate && validate > write );
    const int before = source.indexOf( QStringLiteral( "const qint64 forBefore = d->postReturnFor.load();" ) );
    const int value = source.indexOf( QStringLiteral( "const qint64 returnTicks = d->postReturnQpc.load();" ) );
    const int after = source.indexOf( QStringLiteral( "const qint64 forAfter = d->postReturnFor.load();" ) );
    ASSERT_TRUE( before > 0 && value > before && after > value );
    ASSERT_EQ( 1, source.count( QStringLiteral( "const bool published = postTicks > 0 && forBefore == postTicks && forAfter == postTicks;" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "published ? returnTicks : 0 );" ) ) );
    // the fired post time is the return stamp; with none published the delivery is unsplit
    ASSERT_EQ( 1, source.count( QStringLiteral( "firingPostMs = returnNs >= 0 ? static_cast<double>( returnNs ) / 1000000.0 : -1.0;" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "firingUnsplit = postNs >= 0 && returnNs < 0;" ) ) );
    // MainWindow records an unsplit delivery in neither split set, counts it, and puts the count on the line
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "else if( m_playbackDueWake && m_playbackDueWake->lastDeliveryUnsplit() )\n"
                                                    "        ++m_playbackDueWakePostUnsplit;" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "QStringLiteral( \" post_unsplit=%1\" ).arg( m_playbackDueWakePostUnsplit )" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "m_playbackDueWakePostUnsplit = 0;" ) ) );
#ifdef Q_OS_WIN
    // a live delivery: the post time it reports is never before the first stamp the message carries
    double postMs = -2.0;
    double firstMs = -2.0;
    bool unsplit = false;
    PlaybackDueWake *self = nullptr;
    PlaybackDueWake wake( nullptr, [&]() { postMs = self->lastDeliveryPostMs(); firstMs = self->lastDeliveryPostFirstMs();
                                           unsplit = self->lastDeliveryUnsplit(); } );
    self = &wake;
    LARGE_INTEGER frequency;
    ASSERT_TRUE( QueryPerformanceFrequency( &frequency ) && frequency.QuadPart > 0 );
    const HWND window = reinterpret_cast<HWND>( wake.nativeWindow() );
    int split = 0;
    for( int attempt = 0; attempt < 20 && split == 0; ++attempt )
    {
        wake.arm( 1.0 );
        MSG msg = {};
        const ULONGLONG deadline = GetTickCount64() + 200;
        bool taken = false;
        while( !taken && GetTickCount64() < deadline )
        {
            if( PeekMessageW( &msg, window, PlaybackDueWake::kNativeWakeMessage, PlaybackDueWake::kNativeWakeMessage, PM_REMOVE ) )
            {
                Sleep( 2 );   // let the wait thread publish the return time first, as a busy GUI thread does
                ASSERT_TRUE( wake.handleNativeMessage( &msg ) );
                taken = true;
            }
            else
                Sleep( 0 );
        }
        ASSERT_TRUE( taken );
        const double firstStampMs = static_cast<double>( msg.lParam ) * 1000.0 / static_cast<double>( frequency.QuadPart );
        ASSERT_TRUE( ( postMs >= 0.0 ) != unsplit );
        if( postMs >= 0.0 )
        {
            ASSERT_TRUE( postMs >= firstStampMs );
            // r6 (pass 10 A2): the lower end of the post interval is the message's own first stamp
            ASSERT_NEAR( firstStampMs, firstMs, 1e-3 );
            ++split;
        }
        else
            ASSERT_TRUE( firstMs < 0.0 );   // unsplit: no interval at all
    }
    ASSERT_EQ( 1, split );
#endif
}

TEST(PlaybackDueWakeGate, ARenewKeepsTheArmAndItsDueAndStalesEveryEarlierGeneration)
{
    // r6 adversary pass 10 A1: the failed re-arm renews the generation; the due time and the armed state are the arm's own
    const qint64 ms = 1000000;
    PlaybackDueWakeGate gate;
    const quint32 replaced = gate.arm( 0, 50 * ms );
    const quint32 replacement = gate.arm( 60 * ms, 5 * ms );      // due t=65
    const quint32 renewed = gate.renew();
    ASSERT_TRUE( renewed != replacement && renewed != replaced && renewed != 0u );
    ASSERT_TRUE( gate.armed() );
    ASSERT_FALSE( gate.accept( replacement, 67 * ms, 65 * ms + 100000 ) );   // the old wait thread's late post: stale
    ASSERT_FALSE( gate.accept( replaced, 67 * ms, 65 * ms + 100000 ) );
    ASSERT_TRUE( gate.armed() );
    ASSERT_FALSE( gate.accept( renewed, 64 * ms ) );              // the due is kept: an early QTimer is still early
    ASSERT_TRUE( gate.accept( renewed, 67 * ms ) );               // the QTimer (no post time) fires it, once
    ASSERT_FALSE( gate.armed() );
    // the delivery paths: a notifier activation delivers only a waitable-timer arm, a QTimer only a QTimer arm
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_FALSE( source.isEmpty() );
    ASSERT_EQ( 1, source.count( QStringLiteral( "if( fromQtTimer != lastArmQtTimer ) return;" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "[impl]( HANDLE ) { impl->deliverCurrent( false ); } );" ) ) );
    ASSERT_EQ( 2, source.count( QStringLiteral( "impl->deliverCurrent( true ); } );" ) ) );
    ASSERT_EQ( 0, source.count( QStringLiteral( "deliverCurrent(); }" ) ) );
}

TEST(PlaybackDueWakeNative, TheReplacedTimersSignalPostedAfterAFailedReArmNeverFiresIt)
{
    // r6 adversary pass 10 A1 input: at t=0 arm(50) succeeds. At t=50 the wait thread consumes its signal and pauses before
    // stamping. At t=60 arm(5) publishes generation 2 (due t=65) and SetWaitableTimer fails: cancel, generation 0 posted from
    // then on. At t=65.1 the old wait thread resumes, stamps and posts generation 2. The GUI handles it at t=67, before the
    // fallback QTimer. On 933b6558 it matched gate generation 2, passed both time checks, fired and disarmed the replacement
    // while lastArmDelivery() said qtimer. The wait thread's late post is fed directly (its timing cannot be forced).
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    wake.arm( 50.0 );                                              // generation 1, on the waitable timer
    wake.setWaitableTimerFailsForTest( true );
    wake.arm( 5.0 );                                               // generation 2 stored for the wait thread; the set fails
    ASSERT_TRUE( wake.lastArmDelivery() == PlaybackDueWake::Delivery::QtTimer );
    Sleep( 8 );                                                    // past the replacement's due (t=65.1 .. 67)
    LARGE_INTEGER stamp;
    ASSERT_TRUE( QueryPerformanceCounter( &stamp ) );
    MSG late = {};
    late.hwnd = reinterpret_cast<HWND>( wake.nativeWindow() );
    late.message = PlaybackDueWake::kNativeWakeMessage;
    late.lParam = static_cast<LPARAM>( stamp.QuadPart );          // stamped after the due: the time checks pass
    for( const quint32 generation : { 2u, 1u } )                   // the generation it loaded, and the replaced arm's
    {
        late.wParam = static_cast<WPARAM>(
            playbackDueWakePackWParam( generation, static_cast<quint32>( PlaybackDueWake::kNativeWakeTag ) ) );
        ASSERT_TRUE( wake.handleNativeMessage( &late ) );          // this wake's message: consumed...
        ASSERT_EQ( 0, fired );                                     // ...and stale
        ASSERT_TRUE( wake.armed() );                               // the replacement is still armed for its QTimer
    }
    // the fallback QTimer is what delivers it, once
    const ULONGLONG deadline = GetTickCount64() + 500;
    while( fired < 1 && GetTickCount64() < deadline )
    {
        QCoreApplication::processEvents( QEventLoop::AllEvents, 5 );
        Sleep( 1 );
    }
    ASSERT_EQ( 1, fired );
    ASSERT_FALSE( wake.armed() );
    ASSERT_EQ( 0, pumpDueWakeMessages( wake, 20 ) );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeNative, AQueuedNotifierActivationOfTheReplacedTimerNeverFiresAFailedReArm)
{
    // r6 adversary pass 10 A1 sibling (PlaybackDueWake.cpp:258): on the notifier path the replaced arm's timer signalled and
    // its activation was queued before arm(5) failed over to a QTimer. deliverCurrent took "whatever is armed now", so the
    // queued activation fired the replacement on the notifier. Only the notifier's own posted event type (WinEventAct) is sent
    // first: a bare sendPostedEvents() also ran the fallback QTimer's timeout on hosted CI (c625b77a, fired == 1 from the
    // correct QTimer delivery), so it could not tell the two mechanisms apart.
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; }, false );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::Notifier );
    wake.arm( 1.0 );                                               // on the waitable timer
    Sleep( 30 );                                                   // it signals; nobody processes events
    wake.setWaitableTimerFailsForTest( true );
    wake.arm( 5.0 );
    ASSERT_TRUE( wake.lastArmDelivery() == PlaybackDueWake::Delivery::QtTimer );
    Sleep( 8 );                                                    // past the replacement's due
    QCoreApplication::sendPostedEvents( nullptr, QEvent::WinEventAct );   // the queued activation only, if it was queued
    ASSERT_EQ( 0, fired );
    ASSERT_TRUE( wake.armed() );
    const ULONGLONG deadline = GetTickCount64() + 500;
    while( fired < 1 && GetTickCount64() < deadline )
    {
        QCoreApplication::processEvents( QEventLoop::AllEvents, 5 );
        Sleep( 1 );
    }
    ASSERT_EQ( 1, fired );                                         // the QTimer delivers it, once
    ASSERT_FALSE( wake.armed() );
#else
    SKIP_TEST( "the notifier delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeSplit, ADescheduleAfterPostMessageReturnsIsBoundedNotChargedToDueToPost)
{
    // r6 adversary pass 10 A2 input: due and first stamp at 100 ms, PostMessageW returns at 100 ms, the wait thread is
    // descheduled before the return stamp until 140 ms and publishes 140, the GUI receives at 150 ms. 9c673870 reported
    // due-to-post 40 / post-to-receive 10 against the true 0 / 50. Any single stamp has a window; the split is the bounds.
    const PlaybackDueWakeSplitBounds late = playbackDueWakeSplitBounds( 100.0, 100.0, 140.0, 150.0 );
    ASSERT_EQ( 0.0, late.dueToPostLoMs );
    ASSERT_EQ( 40.0, late.dueToPostHiMs );
    ASSERT_EQ( 10.0, late.postToReceiveLoMs );
    ASSERT_EQ( 50.0, late.postToReceiveHiMs );
    ASSERT_EQ( 40.0, late.postCallMs );                            // the attribution uncertainty, disclosed
    ASSERT_TRUE( late.dueToPostLoMs <= 0.0 && 0.0 <= late.dueToPostHiMs );           // the true 0 / 50 is inside
    ASSERT_TRUE( late.postToReceiveLoMs <= 50.0 && 50.0 <= late.postToReceiveHiMs );
    // the pass 9 A1 mirror (deschedule BEFORE PostMessageW until 140, receive 140.1): the true 40 / 0.1 is inside too
    const PlaybackDueWakeSplitBounds early = playbackDueWakeSplitBounds( 100.0, 100.0, 140.0, 140.1 );
    ASSERT_TRUE( early.dueToPostLoMs <= 40.0 && 40.0 <= early.dueToPostHiMs );
    ASSERT_TRUE( early.postToReceiveLoMs <= 0.1 + 1e-9 && 0.1 <= early.postToReceiveHiMs + 1e-9 );
    // per-sample bounds bound every percentile: a true post anywhere in [first, return] stays inside the printed pair
    PlaybackDueWakeSamples lo, hi, truth;
    LatenessSource source;
    for( int i = 0; i < 4000; ++i )
    {
        const double due = 1000.0 * i;
        const double first = due + source.next( 0.0, 3.0 );
        const double ret = first + source.next( 0.0, 40.0 );
        const double post = first + ( ret - first ) * source.next( 0.0, 1.0 );
        const PlaybackDueWakeSplitBounds b = playbackDueWakeSplitBounds( due, first, ret, ret + source.next( 0.0, 20.0 ) );
        lo.add( b.dueToPostLoMs );
        hi.add( b.dueToPostHiMs );
        truth.add( post - due );
    }
    for( const double p : { 0.0, 0.50, 0.95, 1.0 } )
        ASSERT_TRUE( lo.percentile( p ) <= truth.percentile( p ) && truth.percentile( p ) <= hi.percentile( p ) );
    // the summary line claims no point split any more
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_FALSE( mainWindow.isEmpty() );
    for( const char *point : { "due_to_post_p50_ms=", "due_to_post_p95_ms=", "post_to_receive_p50_ms=", "post_to_receive_p95_ms=" } )
        ASSERT_EQ( 0, mainWindow.count( QLatin1String( point ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "\"post_call_p50_ms=%10 post_call_p95_ms=%11\"" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "const double firstStampMs = m_playbackDueWake ? m_playbackDueWake->lastDeliveryPostFirstMs() : -1.0;" ) ) );
    const QString wake = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 1, wake.count( QStringLiteral( "firingFirstMs = returnNs >= 0 ? static_cast<double>( postNs ) / 1000000.0 : -1.0;" ) ) );
}

TEST(PlaybackDueWakeSplit, ThePrintedBoundsEncloseTheTrueSplitPastTheExactStoreAndAtThePrintedPrecision)
{
    // r6 adversary pass 11 A1 input: one looping Play of 16,385 native deliveries, each with due D, first stamp D + 0.002 ms,
    // actual post D + 0.003, return stamp D + 0.004, receive D + 0.020. Binned, d608cd40 printed both due-to-post ends as the
    // bin centre 0.005 ([0.005, 0.005] excludes the true 0.003) and both post-to-receive ends as 0.015 (excludes 0.017).
    const PlaybackDueWakeSplitBounds b = playbackDueWakeSplitBounds( 0.0, 0.002, 0.004, 0.020 );
    PlaybackDueWakeSamples dueLo, dueHi, receiveLo, receiveHi;
    dueLo.addRepeated( b.dueToPostLoMs, PlaybackDueWakeSamples::kExactCapacity + 1 );
    dueHi.addRepeated( b.dueToPostHiMs, PlaybackDueWakeSamples::kExactCapacity + 1 );
    receiveLo.addRepeated( b.postToReceiveLoMs, PlaybackDueWakeSamples::kExactCapacity + 1 );
    receiveHi.addRepeated( b.postToReceiveHiMs, PlaybackDueWakeSamples::kExactCapacity + 1 );
    ASSERT_FALSE( dueLo.exact() );
    const auto lower = []( const PlaybackDueWakeSamples &s, double p )
    { return playbackDueWakePercentileBoundText( s, p, PlaybackDueWakeBoundSide::Lower ).toDouble(); };
    const auto upper = []( const PlaybackDueWakeSamples &s, double p )
    { return playbackDueWakePercentileBoundText( s, p, PlaybackDueWakeBoundSide::Upper ).toDouble(); };
    for( const double p : { 0.50, 0.95 } )
    {
        ASSERT_TRUE( lower( dueLo, p ) <= 0.003 && 0.003 <= upper( dueHi, p ) );
        ASSERT_TRUE( lower( receiveLo, p ) <= 0.017 && 0.017 <= upper( receiveHi, p ) );
    }
    ASSERT_EQ( std::string( "0.000" ), playbackDueWakePercentileBoundText( dueLo, 0.50, PlaybackDueWakeBoundSide::Lower ).toStdString() );
    ASSERT_EQ( std::string( "0.010" ), playbackDueWakePercentileBoundText( dueHi, 0.50, PlaybackDueWakeBoundSide::Upper ).toStdString() );
    // the exact store: rounded outward at the printed precision, not to the nearest (0.0026 is not printed as 0.003 when
    // it is a lower bound, nor 0.0024 as 0.002 when it is an upper bound)
    PlaybackDueWakeSamples exactLo, exactHi;
    exactLo.add( 0.0026 );
    exactHi.add( 0.0024 );
    ASSERT_TRUE( exactLo.exact() );
    ASSERT_EQ( std::string( "0.002" ), playbackDueWakePercentileBoundText( exactLo, 0.50, PlaybackDueWakeBoundSide::Lower ).toStdString() );
    ASSERT_EQ( std::string( "0.003" ), playbackDueWakePercentileBoundText( exactHi, 0.50, PlaybackDueWakeBoundSide::Upper ).toStdString() );
    // a negative lower bound floors away from zero; a value on the printed grid is printed as itself
    PlaybackDueWakeSamples negative, onGrid;
    negative.add( -0.0004 );
    onGrid.add( 0.25 );
    ASSERT_EQ( std::string( "-0.001" ), playbackDueWakePercentileBoundText( negative, 0.50, PlaybackDueWakeBoundSide::Lower ).toStdString() );
    ASSERT_EQ( std::string( "0.000" ), playbackDueWakePercentileBoundText( negative, 0.50, PlaybackDueWakeBoundSide::Upper ).toStdString() );
    ASSERT_EQ( std::string( "0.250" ), playbackDueWakePercentileBoundText( onGrid, 0.50, PlaybackDueWakeBoundSide::Lower ).toStdString() );
    // a censored rank keeps its censored text on either side
    PlaybackDueWakeSamples late;
    late.addRepeated( 500.0, PlaybackDueWakeSamples::kExactCapacity + 1 );
    ASSERT_EQ( std::string( ">=80.000" ), playbackDueWakePercentileBoundText( late, 0.50, PlaybackDueWakeBoundSide::Lower ).toStdString() );
    ASSERT_EQ( std::string( ">=80.000" ), playbackDueWakePercentileBoundText( late, 0.50, PlaybackDueWakeBoundSide::Upper ).toStdString() );
    // in range, binned: every rank's bin edges enclose its exact value, over a wide spread of samples
    PlaybackDueWakeSamples spread;
    std::vector<double> reference;
    LatenessSource source;
    for( int i = 0; i < 20000; ++i ) { const double v = source.next( -19.0, 79.0 ); spread.add( v ); reference.push_back( v ); }
    ASSERT_FALSE( spread.exact() );
    for( const double p : { 0.0, 0.05, 0.50, 0.95, 1.0 } )
    {
        const double truth = unboundedPercentile( reference, p );
        ASSERT_TRUE( lower( spread, p ) <= truth && truth <= upper( spread, p ) );
    }
}

TEST(PlaybackDueWakeSplit, APrintedBoundNeverCrossesItsSampleAtDoublePrecision)
{
    // r6 adversary pass 12 A2 input: 44.029999999999994 x 16,385 (p50 Lower) printed 44.030, above its sample; 44.160000000000004
    // x 16,385 (p50 Upper) printed 44.160, below it. The division in binOf rounded the sample into the next / previous bin.
    const double lowSample = 44.029999999999994;
    const double highSample = 44.160000000000004;
    PlaybackDueWakeSamples low, high;
    low.addRepeated( lowSample, PlaybackDueWakeSamples::kExactCapacity + 1 );
    high.addRepeated( highSample, PlaybackDueWakeSamples::kExactCapacity + 1 );
    ASSERT_FALSE( low.exact() );
    ASSERT_TRUE( low.rank( 0.50 ).loMs <= lowSample && lowSample < low.rank( 0.50 ).hiMs );
    ASSERT_TRUE( high.rank( 0.50 ).loMs <= highSample && highSample < high.rank( 0.50 ).hiMs );
    ASSERT_TRUE( playbackDueWakePercentileBoundText( low, 0.50, PlaybackDueWakeBoundSide::Lower ).toDouble() <= lowSample );
    ASSERT_TRUE( playbackDueWakePercentileBoundText( high, 0.50, PlaybackDueWakeBoundSide::Upper ).toDouble() >= highSample );
    // every sample sits inside its own bin's reported edges, across the range, at and next to an edge every 0.4 ms (250 edges,
    // 4 values each: each one is a spilled set, so the count is kept small)
    LatenessSource source;
    for( int i = 0; i < 250; ++i )
    {
        const double edge = -20.0 + static_cast<double>( i * 40 ) * 0.01;
        for( const double v : { edge, std::nextafter( edge, -100.0 ), std::nextafter( edge, 100.0 ), source.next( -20.0, 80.0 ) } )
        {
            if( !( v >= PlaybackDueWakeSamples::kMinMs ) || v >= PlaybackDueWakeSamples::kMaxMs ) continue;
            PlaybackDueWakeSamples one;
            one.addRepeated( v, PlaybackDueWakeSamples::kExactCapacity + 1 );
            const PlaybackDueWakeSamples::Rank r = one.rank( 0.50 );
            ASSERT_TRUE( r.loMs <= v && v < r.hiMs );
            ASSERT_TRUE( playbackDueWakePercentileBoundText( one, 0.50, PlaybackDueWakeBoundSide::Lower ).toDouble() <= v );
            ASSERT_TRUE( playbackDueWakePercentileBoundText( one, 0.50, PlaybackDueWakeBoundSide::Upper ).toDouble() >= v );
        }
    }
    // the exact store: a value just below a printed step is not printed as that step when it is a lower bound
    PlaybackDueWakeSamples exact;
    const double justBelow = std::nextafter( 0.003, 0.0 );
    exact.add( justBelow );
    ASSERT_TRUE( playbackDueWakePercentileBoundText( exact, 0.50, PlaybackDueWakeBoundSide::Lower ).toDouble() <= justBelow );
    ASSERT_TRUE( playbackDueWakePercentileBoundText( exact, 0.50, PlaybackDueWakeBoundSide::Upper ).toDouble() >= justBelow );
}

TEST(PlaybackDueWakeGate, AnAbsurdWaitSaturatesTheDueInsteadOfOverflowing)
{
    // r6 adversary pass 12 A1 gate sibling input: nowNs = 1,000,000,000,000 and dueInNs = 9,223,372,036,000,000,000. The sum
    // overflowed qint64 (undefined; in practice a negative due, so any delivery was "on time").
    PlaybackDueWakeGate gate;
    const quint32 g = gate.arm( 1000000000000LL, 9223372036000000000LL );
    ASSERT_TRUE( gate.armed() );
    ASSERT_FALSE( gate.accept( g, 2000000000000LL ) );            // far before the (saturated) due: early, still armed
    ASSERT_TRUE( gate.armed() );
    ASSERT_TRUE( gate.accept( g, std::numeric_limits<qint64>::max() ) );
    // a negative wait is "now"
    PlaybackDueWakeGate now;
    const quint32 n = now.arm( 5000000, -3000000 );
    ASSERT_TRUE( now.accept( n, 5000000 ) );
}

TEST(PlaybackDueWakeNative, AnAbsurdDueIsClampedBeforeEveryConversion)
{
    // r6 adversary pass 12 A1 input: fpsOverride with frameRate 1e-12 makes msUntilCredit ~1e15 ms; 1e-7 gives ~1e10 ms on the
    // QTimer path; +infinity is pass 10 A4. dueMs * 1e6 left qint64, the 100 ns units LONGLONG, and the QTimer delay int.
    ASSERT_EQ( 2147483647.0, PlaybackDueWake::kMaxDueMs );
    ASSERT_TRUE( PlaybackDueWake::kMaxDueMs * 1000000.0 < 9.2e18 );                        // the gate's ns
    ASSERT_TRUE( std::ceil( PlaybackDueWake::kMaxDueMs * 10000.0 ) < 9.2e18 );             // SetWaitableTimer's 100 ns units
    ASSERT_TRUE( std::ceil( PlaybackDueWake::kMaxDueMs ) <= 2147483647.0 );               // QTimer::start( int )
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_FALSE( source.isEmpty() );
    const int clamp = source.indexOf( QStringLiteral( "if( dueMs > kMaxDueMs ) dueMs = kMaxDueMs;" ) );
    const int convert = source.indexOf( QStringLiteral( "const quint32 generation = d->gate.arm( d->nowNs(), static_cast<qint64>( dueMs * 1000000.0 ) );" ) );
    ASSERT_TRUE( clamp > 0 && convert > clamp );
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    for( const double due : { 1e15, std::numeric_limits<double>::infinity() } )
    {
        wake.arm( due );
        ASSERT_TRUE( wake.armed() );
        ASSERT_TRUE( wake.lastArmDelivery() == PlaybackDueWake::Delivery::NativeMessage );   // the set succeeded
        ASSERT_EQ( 0, pumpDueWakeMessages( wake, 30 ) );                                     // and it is not "now"
        ASSERT_EQ( 0, fired );
    }
    wake.setWaitableTimerFailsForTest( true );
    wake.arm( 10000000000.25 );                                                              // the QTimer path
    ASSERT_TRUE( wake.lastArmDelivery() == PlaybackDueWake::Delivery::QtTimer );
    const ULONGLONG deadline = GetTickCount64() + 30;
    while( GetTickCount64() < deadline ) { QCoreApplication::processEvents( QEventLoop::AllEvents, 5 ); Sleep( 1 ); }
    ASSERT_EQ( 0, fired );
    ASSERT_TRUE( wake.armed() );
    wake.cancel();
#endif
}

#ifdef Q_OS_WIN
namespace
{
// r7 (adversary passes 13/14 A1): fill this thread's posted-message queue with harmless WM_NULLs to the wake's window until
// PostMessageW refuses one (10000 by default). Returns how many went in; refusedWith is GetLastError() of the refusal, 0 when
// the cap was reached first (a raised USERPostMessageLimit).
int fillQueueWithWmNull( HWND window, DWORD & refusedWith )
{
    refusedWith = 0;
    int posted = 0;
    for( ; posted < 1000000; ++posted )
    {
        if( !PostMessageW( window, WM_NULL, 0, 0 ) )
        {
            refusedWith = GetLastError();
            break;
        }
    }
    return posted;
}

// Take every message queued for the wake's window: WM_NULLs are dropped, the wake's own handed to it. Returns the wake's.
int drainDueWakeWindow( PlaybackDueWake & wake )
{
    const HWND window = reinterpret_cast<HWND>( wake.nativeWindow() );
    int taken = 0;
    MSG msg;
    while( PeekMessageW( &msg, window, 0, 0, PM_REMOVE ) )
    {
        if( msg.message == PlaybackDueWake::kNativeWakeMessage && wake.handleNativeMessage( &msg ) ) ++taken;
    }
    return taken;
}

// a failed assert throws: never leave the queue full for the next test
struct DueWakeQueueDrain
{
    PlaybackDueWake & wake;
    ~DueWakeQueueDrain()
    {
        drainDueWakeWindow( wake );
        QCoreApplication::processEvents( QEventLoop::AllEvents, 5 );   // a Qt wake-up refused meanwhile is re-sent here
    }
};

bool waitWithoutPumping( const std::function<bool()> & done, DWORD waitMs )
{
    const ULONGLONG deadline = GetTickCount64() + waitMs;
    while( !done() && GetTickCount64() < deadline ) Sleep( 2 );
    return done();
}
}
#endif

TEST(PlaybackDueWakeNative, APostAFullQueueRefusesIsDeliveredOnceByThePollTickAndCounted)
{
    // r7 (adversary pass 13 A1, confirmed by pass 14; PlaybackDueWake.cpp:168 on ecdc3aa7) input: default native-message
    // delivery; the GUI queue holds 10000 harmless WM_NULL messages; arm(5.0); nothing is drained until after the expiry.
    // PostMessageW returns FALSE with ERROR_NOT_ENOUGH_QUOTA. On ecdc3aa7 the result was discarded: the auto-reset signal was
    // consumed, nothing was posted, the arm stayed armed and undelivered, and the line showed nothing. Now the wait thread
    // retries for its budget, then hands the generation to the GUI thread; the poll tick delivers it once, and it is counted.
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    DueWakeQueueDrain drainOnExit { wake };
    wake.setPostRetryBudgetMsForTest( 20 );
    DWORD refusedWith = 0;
    const int filled = fillQueueWithWmNull( reinterpret_cast<HWND>( wake.nativeWindow() ), refusedWith );
    if( refusedWith == 0 ) SKIP_TEST( "this host's posted-message limit is above 1000000: the queue cannot be filled" );
    ASSERT_TRUE( filled > 0 );
    ASSERT_EQ( static_cast<DWORD>( ERROR_NOT_ENOUGH_QUOTA ), refusedWith );
    wake.arm( 5.0 );
    // not drained until the wait thread gave up: refused, retried for 20 ms, refused
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.postFailures() >= 1; }, 3000 ) );
    ASSERT_EQ( 1u, wake.postFailures() );
    ASSERT_TRUE( wake.postRefusals() >= 1u );                      // the first post at least (the retries depend on scheduling)
    ASSERT_EQ( 0, drainDueWakeWindow( wake ) );                    // the queue drains: no wake message was ever posted
    ASSERT_EQ( 0, fired );
    ASSERT_TRUE( wake.armed() );                                   // still the arm's, for the poll tick
    ASSERT_TRUE( wake.deliverLostPost() );                         // the poll tick delivers it...
    ASSERT_EQ( 1, fired );
    ASSERT_FALSE( wake.armed() );
    ASSERT_FALSE( wake.deliverLostPost() );                        // ...exactly once
    ASSERT_EQ( 1, fired );
    ASSERT_EQ( 0, pumpDueWakeMessages( wake, 20 ) );
    // a lost post of an arm cancelled before the tick is dropped by the gate (counted as failed, never fired)
    fillQueueWithWmNull( reinterpret_cast<HWND>( wake.nativeWindow() ), refusedWith );
    ASSERT_EQ( static_cast<DWORD>( ERROR_NOT_ENOUGH_QUOTA ), refusedWith );
    wake.arm( 1.0 );
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.postFailures() >= 2; }, 3000 ) );
    drainDueWakeWindow( wake );
    wake.cancel();
    ASSERT_FALSE( wake.deliverLostPost() );
    ASSERT_EQ( 1, fired );
    ASSERT_EQ( 2u, wake.postFailures() );
    // the wiring: the post result is checked and retried, the poll tick rescues, and the line counts all three
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "static_cast<LPARAM>( postQpc ) ) != 0;" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "bool posted = postDueWake( args, postQpc.QuadPart, generation );" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "args.lostGeneration->store( generation, std::memory_order_release );" ) ) );
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString timer = functionBody( mainWindow, QStringLiteral( "void MainWindow::timerEvent(QTimerEvent *t)" ),
                                        QStringLiteral( "//1sec Timer" ) );
    const int rescue = timer.indexOf( QStringLiteral( "if( m_playbackDueWake ) m_playbackDueWake->deliverLostPost();" ) );
    const int tick = timer.indexOf( QStringLiteral( "timerFrameEvent();" ) );
    ASSERT_TRUE( rescue > 0 && tick > rescue );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "QStringLiteral( \" post_refused=%1 post_failed=%2 post_rescued=%3\" )" ) ) );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeNative, ARescuedWakeThatStopsPlayPrintsItsOwnRescueAndFailure)
{
    // r7 adversary pass 15 A1 input (MainWindow.cpp:3095 on 4c5e8289): the current due wake is armed, cut-out moved to the
    // next frame, the GUI queue filled with 10000 posted messages and held past the 100 ms retry budget; the worker publishes
    // the lost generation, the queue drains and the poll tick rescues it. The rescue's callback (onPlaybackDueWake ->
    // timerFrameEvent) stops Play at the cut-out, and the stop handler prints the summary line BEFORE deliverLostPost returns.
    // On 4c5e8289 MainWindow counted the rescue after deliverLostPost returned (post_failed=1 post_rescued=0 on that line), and
    // the worker published the generation before counting the failure. The callback below reads the counters exactly where
    // the summary line does: from inside the fire.
#ifdef Q_OS_WIN
    quint64 failedSeen = 0;
    quint64 rescuedSeen = 0;
    int fired = 0;
    PlaybackDueWake *self = nullptr;
    PlaybackDueWake wake( nullptr, [&]() { ++fired; failedSeen = self->postFailures(); rescuedSeen = self->postRescued(); } );
    self = &wake;
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    DueWakeQueueDrain drainOnExit { wake };
    wake.setPostRetryBudgetMsForTest( 20 );
    DWORD refusedWith = 0;
    fillQueueWithWmNull( reinterpret_cast<HWND>( wake.nativeWindow() ), refusedWith );
    if( refusedWith == 0 ) SKIP_TEST( "this host's posted-message limit is above 1000000: the queue cannot be filled" );
    ASSERT_EQ( static_cast<DWORD>( ERROR_NOT_ENOUGH_QUOTA ), refusedWith );
    wake.arm( 5.0 );
    // the GUI thread polls the rescue the way the poll tick does, as soon as a generation is published (no other wait)
    const ULONGLONG deadline = GetTickCount64() + 3000;
    bool rescued = false;
    while( !rescued && GetTickCount64() < deadline )
    {
        rescued = wake.deliverLostPost();
        if( !rescued ) Sleep( 0 );
    }
    ASSERT_TRUE( rescued );
    ASSERT_EQ( 1, fired );
    ASSERT_EQ( 1u, failedSeen );                                   // the failure was counted before it was published
    ASSERT_EQ( 1u, rescuedSeen );                                  // the rescue was counted before the callback ran
    ASSERT_EQ( 1u, wake.postRescued() );
    ASSERT_FALSE( wake.deliverLostPost() );
    ASSERT_EQ( 1u, wake.postRescued() );
    // a native delivery is never counted as a rescue
    drainDueWakeWindow( wake );
    wake.arm( 1.0 );
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 200 ) );
    ASSERT_EQ( 2, fired );
    ASSERT_EQ( 1u, rescuedSeen );
    // the wiring: the wake counts both ahead of their publication; MainWindow reads the wake's counter (no own increment)
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    const int count = source.indexOf( QStringLiteral( "args.postFailures->fetch_add( 1 );" ) );
    const int publish = source.indexOf( QStringLiteral( "args.lostGeneration->store( generation, std::memory_order_release );" ) );
    ASSERT_TRUE( count > 0 && publish > count );
    const QString deliver = functionBody( source, QStringLiteral( "void deliverGeneration( quint32 generation" ),
                                          QStringLiteral( "#ifdef Q_OS_WIN\nnamespace" ) );
    const int rescue = deliver.indexOf( QStringLiteral( "++postRescued;" ) );
    const int fire = deliver.indexOf( QStringLiteral( "onFire();" ) );
    ASSERT_TRUE( rescue > 0 && fire > rescue );
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 0, mainWindow.count( QStringLiteral( "++m_playbackDueWakePostRescued" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral(
        "m_playbackDueWake ? m_playbackDueWake->postRescued() - m_playbackDueWakePostRescuedBase : 0" ) ) );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeNative, APostAFullQueueRefusesIsRetriedAndDeliveredOnceWhenTheQueueDrains)
{
    // r7 (adversary pass 13 A1, the retry): the same full queue, drained while the wait thread is still inside its budget. The
    // retry goes through: one message, one fire, nothing lost and nothing for the poll tick.
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    DueWakeQueueDrain drainOnExit { wake };
    wake.setPostRetryBudgetMsForTest( 10000 );
    DWORD refusedWith = 0;
    fillQueueWithWmNull( reinterpret_cast<HWND>( wake.nativeWindow() ), refusedWith );
    if( refusedWith == 0 ) SKIP_TEST( "this host's posted-message limit is above 1000000: the queue cannot be filled" );
    ASSERT_EQ( static_cast<DWORD>( ERROR_NOT_ENOUGH_QUOTA ), refusedWith );
    wake.arm( 5.0 );
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.postRefusals() >= 1; }, 3000 ) );   // refused at least once
    int taken = drainDueWakeWindow( wake );
    taken += pumpDueWakeMessages( wake, 300 );
    ASSERT_EQ( 1, taken );
    ASSERT_EQ( 1, fired );
    ASSERT_FALSE( wake.armed() );
    ASSERT_EQ( 0u, wake.postFailures() );
    ASSERT_FALSE( wake.deliverLostPost() );
    ASSERT_EQ( 1, fired );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}
