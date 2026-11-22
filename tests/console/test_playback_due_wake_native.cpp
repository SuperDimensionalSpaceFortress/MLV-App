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
#include "../../platform/qt/GuiHopWaitSplit.h"
#include "../../platform/qt/GpuPresentTailWindow.h"

#include <QCoreApplication>
#include <QRegularExpression>
#include <QEventLoop>
#include <QFile>
#include <QString>
#include <QTextStream>
#include <QElapsedTimer>
#include <QTimer>

#include <algorithm>
#include <atomic>
#include <cmath>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <string>
#include <thread>
#include <utility>
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

// CI-FLAKE-PLAYBACK-DUE-WAKE-R17A1-TIMING-1 r5 (20x run on a loaded host: the R17A1 sink test read 76.1-79.2 ms for an 80 ms
// sleep in 9 of 20 runs, and R3A3 read under 140 ms for 150 ms once): the sleep's wait is MsgWaitForMultipleObjectsEx with a
// whole-ms timeout, whose expiry is tick-granular, and the product does not re-read the clock on a timeout, so a sleep may end
// up to one timer tick (15.625 ms by default; 16 with margin) before its requested time. The floor a sleep must reach is
// therefore its request less that tick, never less than 1 ms: it still fails a sleep cut short at a dispatch or ended at ~0 ms,
// and what pins the sleep not being cut short mid-way is the probe liveness check in the R17A1 fixture, not the wall floor.
constexpr double kSleepTimerTickSlackMs = 16.0;
double dueWakeSleepFloorMs( double requestedMs )
{
    return std::max( 1.0, requestedMs - kSleepTimerTickSlackMs );
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
    // r6 (pass 10 A2): the lateness, and the split's four bounds and its post-call span; FULLSCREEN-S1S2-PRESENT-GAP-1 r1: and
    // post_to_awake / awake_to_receive (bounded the same way)
    ASSERT_EQ( 8, header.count( QRegularExpression( QStringLiteral( "PlaybackDueWakeSamples[ ]+m_playbackDueWake[A-Za-z]+Ms;" ) ) ) );
    ASSERT_EQ( 1, header.count( QStringLiteral( "PlaybackDueWakeSamples m_playbackDueWakePostToAwakeMs;" ) ) );
    ASSERT_EQ( 1, header.count( QStringLiteral( "PlaybackDueWakeSamples m_playbackDueWakeAwakeToReceiveMs;" ) ) );
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
    // FULLSCREEN-S1S2-PRESENT-GAP-1 r1: and post_to_awake / awake_to_receive at p50 / p95, values
    ASSERT_EQ( 8, line.count( QStringLiteral( "playbackDueWakePercentileText( m_playbackDueWake" ) ) );
    const char *const ranks[] = { "LatenessMs, 0.50 )", "LatenessMs, 0.95 )", "PostCallMs, 0.50 )", "PostCallMs, 0.95 )",
                                  "PostToAwakeMs, 0.50 )", "PostToAwakeMs, 0.95 )", "AwakeToReceiveMs, 0.50 )", "AwakeToReceiveMs, 0.95 )" };
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
// CI-FLAKE-PLAYBACK-DUE-WAKE-R17A1-TIMING-1 r3 (adversary pass 4 A1): a replacement arm's own due time is asserted on EVERY attempt
// that is judged, never skipped. The timer starts BEFORE arm(), so the replacement's due time is inside what it measures whatever
// the scheduler did after the arm (late only lengthens it); the arm's own cost is bracketed. A pause before the arm would let a
// shortened replacement still read long, so an attempt whose arm cost reached kReplacementMaxArmCostNs is not judged (it is
// drained and the observation retried, keyed on that flag); every judged attempt runs the full bound, and no attempt passes by
// skipping it.
// r4 (sol review r3, blocker 1; adversary pass 1 A2/A3/A7): the credit for time spent before the timer started is the arm cost
// itself, so it is capped at 2 ms (was 20 ms: an 18 ms pause before the arm let a replacement shortened to 18 ms read 36 ms and
// pass), and the floor is the 40 ms due less 2 ms of timer slack. A replacement is therefore rejected below 36 ms of due time
// (38 ms floor - 2 ms credit); one between 36 and 40 ms is inside the stated tolerance. The bound has a ceiling too (a delivery 40
// ms late is not the replacement's due time, whatever the lower bound says), judged on the wait thread's RETURN stamp of the post:
// the first stamp only bounds the send from below, and a wait thread paused before its send would read on time there. A delivery
// past the ceiling is a scheduler fact, not a verdict: it is observed again, and only five late ones in a row (a product that is
// consistently late) leave the test without a judged attempt and fail it.
// r7 (sol review r6, blocker 2): two scheduler facts the r4 classifier read as verdicts. (1) The wait thread's first stamp is taken
// when IT wakes: a thread paused 22 ms before that stamp stamps a timer shortened 40 -> 18 ms at 40.2 ms, which no single attempt
// can tell from a healthy one (nothing the test can observe separates "the timer fired late" from "the thread ran late"). A
// shortened timer is a systematic defect, though: it reads early in every attempt whose worker is not paused, so ONE judged attempt
// proves nothing and kReplacementProofsNeeded of them, each a fresh arm, must all clear the floor (any early one fails at once).
// (2) A delivery the GUI thread took before the wait thread published its return stamp is "unsplit" (no first/return stamps at
// all): five healthy wakes in a row can land that way. It is no longer retried away; the GUI thread's own receive time is a lower
// bound on it (a delivery cannot be received before it is posted), so it proves "not early" as the stamps do, and says nothing of
// "not late". The ceiling is judged only where the return stamp exists, and only a split on-time or an unsplit not-early delivery
// counts toward the proofs; a late one is observed again.
constexpr int kReplacementAttempts = 8;
constexpr int kReplacementProofsNeeded = 3;
constexpr qint64 kReplacementMaxArmCostNs = 2000000;
constexpr qint64 kReplacementMinElapsedNs = 38000000;                 // the 40 ms replacement due, less 2 ms of timer slack
constexpr qint64 kReplacementMaxElapsedNs = 70000000;                 // the 40 ms replacement due, plus 30 ms of scheduling
enum class ReplacementAttempt { NotJudgeable, TooEarly, TooLate, OnTime, NotEarlyOnly };
enum class ReplacementStamps { None, Unsplit, Split };
ReplacementStamps replacementStampsOf( bool split, bool unsplit )
{
    return split ? ReplacementStamps::Split : ( unsplit ? ReplacementStamps::Unsplit : ReplacementStamps::None );
}
// One replacement attempt, from the arm's own cost, which stamps the delivery carried, and the time (ns) from before the arm to the
// FIRST stamp (the earliest the post can have been), to the RETURN stamp (the latest) and to the GUI thread's own receive of it.
// Early is a verdict on any delivery from a cheap arm; late and unstamped are not. The comparisons are written so that an
// unordered (NaN) elapsed time fails them: it reads as early, never as on time. A split pair whose return precedes its first
// stamp is not a measurement (r7, sol review r6 hardening) and is not judged.
ReplacementAttempt classifyReplacementAttempt( qint64 armCostNs, ReplacementStamps stamps, double firstElapsedNs, double returnElapsedNs, double receiveElapsedNs )
{
    if( armCostNs >= kReplacementMaxArmCostNs || stamps == ReplacementStamps::None ) return ReplacementAttempt::NotJudgeable;
    if( stamps == ReplacementStamps::Unsplit )
    {
        if( !( receiveElapsedNs >= static_cast<double>( kReplacementMinElapsedNs ) ) ) return ReplacementAttempt::TooEarly;
        return ReplacementAttempt::NotEarlyOnly;
    }
    if( !( firstElapsedNs >= static_cast<double>( kReplacementMinElapsedNs ) ) ) return ReplacementAttempt::TooEarly;
    if( !( returnElapsedNs <= static_cast<double>( kReplacementMaxElapsedNs ) ) ) return ReplacementAttempt::TooLate;
    if( !( firstElapsedNs <= returnElapsedNs ) ) return ReplacementAttempt::NotJudgeable;
    return ReplacementAttempt::OnTime;
}
// An attempt that clears the floor: the replacement was not early (and, for a split one, not late either)
bool replacementAttemptProves( ReplacementAttempt verdict )
{
    return verdict == ReplacementAttempt::OnTime || verdict == ReplacementAttempt::NotEarlyOnly;
}
// r3 (adversary pass 4 A5): a helper window is destroyed on every exit, an ASSERT or a throw included (same thread that created it)
struct HelperWindowGuard
{
    HWND window;
    ~HelperWindowGuard() { if( window ) DestroyWindow( window ); }
};
// r4 (adversary pass 2 A3, and pass 1 A8): a sender thread is joined on every exit. An ASSERT that returns, or a throw, between
// its creation and its join would otherwise destroy a joinable std::thread (std::terminate) before the window guard above runs
struct ThreadJoinGuard
{
    std::thread &thread;
    ~ThreadJoinGuard() { if( thread.joinable() ) thread.join(); }
};
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
    // r7 pass 16 A3: the failure is counted just BEFORE the generation is published; settling waits for the publication
    ASSERT_TRUE( wake.settlePostCounters() );
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
    ASSERT_TRUE( wake.settlePostCounters() );
    drainDueWakeWindow( wake );
    wake.cancel();
    ASSERT_FALSE( wake.deliverLostPost() );
    ASSERT_EQ( 1, fired );
    ASSERT_EQ( 2u, wake.postFailures() );
    // the wiring: the post result is checked and retried, the poll tick rescues, and the line counts all three
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    // FULLSCREEN-S1S2-PRESENT-GAP-1 r1: both deliveries check their result -- SendNotifyMessageW (the default) and PostMessageW
    ASSERT_EQ( 2, source.count( QStringLiteral( "static_cast<LPARAM>( postQpc ) ) != 0;" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "return SendNotifyMessageW( args.window, PlaybackDueWake::kNativeWakeMessage," ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "return PostMessageW( args.window, PlaybackDueWake::kNativeWakeMessage," ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "bool posted = postDueWake( args, postQpc.QuadPart, generation );" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "args.lostGeneration->store( generation, std::memory_order_release );" ) ) );
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString timer = functionBody( mainWindow, QStringLiteral( "void MainWindow::timerEvent(QTimerEvent *t)" ),
                                        QStringLiteral( "//1sec Timer" ) );
    const int rescue = timer.indexOf( QStringLiteral( "if( m_playbackDueWake ) m_playbackDueWake->deliverLostPost();" ) );
    const int tick = timer.indexOf( QStringLiteral( "timerFrameEvent();" ) );
    ASSERT_TRUE( rescue > 0 && tick > rescue );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral(
        "QStringLiteral( \" post_refused=%1 post_failed=%2 post_rescued=%3 post_counts_settled=%4\" )" ) ) );
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

TEST(PlaybackDueWakeNative, APreviousPlaysLateFailureIsSettledBeforeTheNextPlaysBaseline)
{
    // r7 adversary pass 16 A1 input (MainWindow.cpp:19684 on c687f37c): native Play A, arm(5.0), the GUI queue full through
    // the retry budget; the worker is still retrying (its failure not yet counted) when A stops and B starts, so B's baseline
    // read postFailures() == 0; the worker then counted A's failure inside B, and B printed post_failed=1 with no failure of
    // its own. Here the worker is held inside its retry by a long budget and a queue that stays full; B's reset settles first.
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    DueWakeQueueDrain drainOnExit { wake };
    wake.setPostRetryBudgetMsForTest( 300 );
    DWORD refusedWith = 0;
    fillQueueWithWmNull( reinterpret_cast<HWND>( wake.nativeWindow() ), refusedWith );
    if( refusedWith == 0 ) SKIP_TEST( "this host's posted-message limit is above 1000000: the queue cannot be filled" );
    ASSERT_EQ( static_cast<DWORD>( ERROR_NOT_ENOUGH_QUOTA ), refusedWith );
    wake.arm( 5.0 );                                               // Play A's last arm
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.postRefusals() >= 1; }, 3000 ) );   // the worker is retrying
    wake.cancel();                                                 // A stops (the cancel does not reach the worker)
    // B's reset (resetPlaybackDueWakeCounters): settle, then the baseline. The queue stays full, so the worker gives up.
    ASSERT_TRUE( wake.settlePostCounters() );
    ASSERT_EQ( 1u, wake.postFailures() );                          // A's failure is in before B's baseline...
    const quint64 baseFailed = wake.postFailures();
    const quint64 baseRefused = wake.postRefusals();
    drainDueWakeWindow( wake );
    // ...and B, with no failure of its own, reports none
    wake.arm( 1.0 );
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 200 ) );
    ASSERT_TRUE( wake.settlePostCounters() );
    ASSERT_EQ( 0u, wake.postFailures() - baseFailed );
    ASSERT_EQ( 0u, wake.postRefusals() - baseRefused );
    ASSERT_FALSE( wake.deliverLostPost() );                        // A's lost post was cancelled: the gate drops it
    ASSERT_EQ( 1, fired );
    // the wiring: the settle request is the LAST handle; the reset and the line both settle before reading the counters
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "const HANDLE handles[3] = { args.stopEvent, args.timer, args.settleEvent };" ) ) );
    // r7 adversary pass 16 A2: the elapsed budget is checked again after every retry wait, before any further attempt
    const int retryWait = source.indexOf( QStringLiteral( "if( WaitForSingleObject( args.stopEvent, retryWaitMs ) != WAIT_TIMEOUT )" ) );
    const int recheck = source.indexOf( QStringLiteral( "if( GetTickCount64() - start >= budget ) break;" ), retryWait );
    const int retry = source.indexOf( QStringLiteral( "if( postDueWake( args, postQpc.QuadPart, generation ) )" ), retryWait );
    ASSERT_TRUE( retryWait > 0 && recheck > retryWait && retry > recheck );
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString reset = functionBody( mainWindow, QStringLiteral( "void MainWindow::resetPlaybackDueWakeCounters( void )" ),
                                        QStringLiteral( "QString MainWindow::playbackDueWakeSummaryLine( void ) const" ) );
    const int settle = reset.indexOf( QStringLiteral(
        "m_playbackDueWakePostBaselineSettled = m_playbackDueWake ? m_playbackDueWake->settlePostCounters() : true;" ) );
    const int baseline = reset.indexOf( QStringLiteral( "m_playbackDueWakePostFailedBase =" ) );
    ASSERT_TRUE( settle > 0 && baseline > settle );
    const QString line = functionBody( mainWindow, QStringLiteral( "QString MainWindow::playbackDueWakeSummaryLine( void ) const" ),
                                       QStringLiteral( "//Paint the Audio Track Wave to GUI" ) );
    // r8 (adversary pass 18 A2): the expression now also requires the baseline's settle (pass 17 A1)
    const int settled = line.indexOf( QStringLiteral(
        "const bool postCountsSettled = ( m_playbackDueWake ? m_playbackDueWake->settlePostCounters() : true )" ) );
    const int read = line.indexOf( QStringLiteral( "m_playbackDueWake->postFailures() - m_playbackDueWakePostFailedBase" ) );
    ASSERT_TRUE( settled > 0 && read > settled );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeNative, ABaselineTakenAfterAnUnansweredSettleMarksThePlaysLineUnsettled)
{
    // r8 (adversary pass 17 A1, MainWindow.cpp:19684 on b19a895a) input: native delivery, Play A's arm(5.0) against a full
    // queue; the worker is still inside its retry when A stops and B starts, and stays there through B's settle timeout; it
    // counts A's failure after B recorded its baseline, B has no failure of its own, and B's end-of-Play settle succeeds. On
    // b19a895a the reset dropped the settle's false, so B printed post_failed=1 post_counts_settled=1. Here the worker keeps
    // the 3000 ms budget it loaded when its retry began, and the settle's deadline uses the budget set after that (0 + slack),
    // so the settle times out exactly as it does for a worker descheduled past it.
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    DueWakeQueueDrain drainOnExit { wake };
    wake.setPostRetryBudgetMsForTest( 3000 );
    DWORD refusedWith = 0;
    fillQueueWithWmNull( reinterpret_cast<HWND>( wake.nativeWindow() ), refusedWith );
    if( refusedWith == 0 ) SKIP_TEST( "this host's posted-message limit is above 1000000: the queue cannot be filled" );
    ASSERT_EQ( static_cast<DWORD>( ERROR_NOT_ENOUGH_QUOTA ), refusedWith );
    wake.arm( 5.0 );                                               // Play A's last arm
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.postRefusals() >= 1; }, 3000 ) );   // the worker is retrying
    wake.cancel();                                                 // A stops
    wake.setPostRetryBudgetMsForTest( 0 );                         // the settle below waits 0 + kSettleSlackMs only
    // B's reset: the settle is not answered in time, and the baseline is taken anyway
    const bool baselineSettled = wake.settlePostCounters();
    ASSERT_FALSE( baselineSettled );
    const quint64 baseFailed = wake.postFailures();
    ASSERT_EQ( 0u, baseFailed );                                   // A's failure is not in yet...
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.postFailures() >= 1; }, 6000 ) );   // ...it lands inside B
    drainDueWakeWindow( wake );
    wake.arm( 1.0 );                                               // B's own arm: delivered, no failure
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 200 ) );
    const bool endSettled = wake.settlePostCounters();
    ASSERT_TRUE( endSettled );                                     // the end-of-Play settle alone says "settled"...
    ASSERT_EQ( 1u, wake.postFailures() - baseFailed );             // ...yet B's difference holds A's failure
    ASSERT_FALSE( endSettled && baselineSettled );                 // what B's line must print: post_counts_settled=0
    ASSERT_FALSE( wake.deliverLostPost() );                        // A's lost post was cancelled: the gate drops it
    ASSERT_EQ( 1, fired );
    // the wiring: the reset keeps the settle's answer, and the line's post_counts_settled requires it as well as its own
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 0, mainWindow.count( QStringLiteral( "if( m_playbackDueWake ) m_playbackDueWake->settlePostCounters();" ) ) );
    const QString reset = functionBody( mainWindow, QStringLiteral( "void MainWindow::resetPlaybackDueWakeCounters( void )" ),
                                        QStringLiteral( "QString MainWindow::playbackDueWakeSummaryLine( void ) const" ) );
    const int kept = reset.indexOf( QStringLiteral(
        "m_playbackDueWakePostBaselineSettled = m_playbackDueWake ? m_playbackDueWake->settlePostCounters() : true;" ) );
    const int baseline = reset.indexOf( QStringLiteral( "m_playbackDueWakePostRefusedBase =" ) );
    ASSERT_TRUE( kept > 0 && baseline > kept );
    const QString line = functionBody( mainWindow, QStringLiteral( "QString MainWindow::playbackDueWakeSummaryLine( void ) const" ),
                                       QStringLiteral( "//Paint the Audio Track Wave to GUI" ) );
    const int both = line.indexOf( QStringLiteral( "&& m_playbackDueWakePostBaselineSettled;" ) );
    const int printed = line.indexOf( QStringLiteral( ".arg( postCountsSettled ? 1 : 0 )" ) );
    ASSERT_TRUE( both > 0 && printed > both );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeNative, ASettleWakeThatLoadsANewerRequestHandlesThePendingTimerSignalFirst)
{
    // r8 (adversary pass 18 A1, PlaybackDueWake.cpp:209 on d2fda465) input: the worker is paused after WaitForMultipleObjects
    // picked settle request 1 and before it loads the request number; meanwhile Play B's timer expires (its post will fail:
    // the GUI queue is full) and a newer request (C's baseline) is issued. On d2fda465 the resumed worker loaded the newer
    // number and answered it at once: C's settle returned true with B's failure not yet counted, and C later printed
    // post_failed=1 post_counts_settled=1. Here the pause is the test hook; request 1 is issued from a helper thread.
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    DueWakeQueueDrain drainOnExit { wake };
    wake.setPostRetryBudgetMsForTest( 0 );                         // a refused post fails at once (settle deadline: slack)
    DWORD refusedWith = 0;
    fillQueueWithWmNull( reinterpret_cast<HWND>( wake.nativeWindow() ), refusedWith );
    if( refusedWith == 0 ) SKIP_TEST( "this host's posted-message limit is above 1000000: the queue cannot be filled" );
    ASSERT_EQ( static_cast<DWORD>( ERROR_NOT_ENOUGH_QUOTA ), refusedWith );
    wake.setSettleAnswerDelayMsForTest( 400 );
    std::atomic<bool> firstSettled { false };
    std::thread first( [&wake, &firstSettled]() { firstSettled = wake.settlePostCounters(); } );   // request 1: wakes the worker
    Sleep( 60 );                                                   // the worker is inside its pause
    wake.arm( 5.0 );                                               // B's timer expires while the worker is paused
    Sleep( 60 );
    const bool secondSettled = wake.settlePostCounters();          // C's baseline: request 2, newer than the wake
    const quint64 failedAtBaseline = wake.postFailures();
    first.join();
    wake.setSettleAnswerDelayMsForTest( 0 );
    ASSERT_TRUE( firstSettled.load() );
    ASSERT_TRUE( secondSettled );
    ASSERT_EQ( 1u, failedAtBaseline );                             // B's failure is counted before C's baseline is answered
    ASSERT_TRUE( wake.settlePostCounters() );
    ASSERT_EQ( 1u, wake.postFailures() );                          // nothing moves after it
    drainDueWakeWindow( wake );
    ASSERT_TRUE( wake.deliverLostPost() );                         // B's lost post is still delivered once
    ASSERT_EQ( 1, fired );
    // the wiring: the request is loaded BEFORE the timer probe, and a pending signal defers the answer
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 0, source.count( QStringLiteral( "args.settleAck->store( args.settleRequest->load() );" ) ) );
    const int load = source.indexOf( QStringLiteral( "const quint64 request = args.settleRequest->load();" ) );
    const int probe = source.indexOf( QStringLiteral( "if( WaitForSingleObject( args.timer, 0 ) != WAIT_OBJECT_0 )" ), load );
    const int defer = source.indexOf( QStringLiteral( "pendingAck = request;" ), probe );
    ASSERT_TRUE( load > 0 && probe > load && defer > probe );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeNative, EveryRetryAndSettleWaitIsRecomputedAndClampedImmediatelyBeforeIt)
{
    // r8 (adversary pass 17 A2, PlaybackDueWake.cpp:239 on b19a895a) input: 100 ms retry budget, native arm(5.0), full queue;
    // the last check reads elapsed=90 ms, the worker is descheduled until 150 ms. Sibling (:601): the settle reads now=0 with
    // deadline=1100, is descheduled until 1500, then starts a stale 1100 ms relative wait (blocking to about 2600 ms). Now
    // every wait is computed from the clock in the expression that feeds it, clamped to one slice, and 0 past the deadline.
    // retry: start=0, deadline = start + budget = 100, slice 1 ms
    ASSERT_EQ( 1u, playbackDueWakeSliceWaitMs( 90, 100, 1 ) );
    ASSERT_EQ( 1u, playbackDueWakeSliceWaitMs( 99, 100, 1 ) );
    ASSERT_EQ( 0u, playbackDueWakeSliceWaitMs( 100, 100, 1 ) );    // spent: no wait and no attempt
    ASSERT_EQ( 0u, playbackDueWakeSliceWaitMs( 150, 100, 1 ) );    // the resumed worker starts nothing
    // settle: deadline 1100 (budget 100 + slack 1000), slice kSettleSliceMs
    ASSERT_EQ( PlaybackDueWake::kSettleSliceMs, playbackDueWakeSliceWaitMs( 0, 1100, PlaybackDueWake::kSettleSliceMs ) );
    ASSERT_EQ( 5u, playbackDueWakeSliceWaitMs( 1095, 1100, PlaybackDueWake::kSettleSliceMs ) );
    ASSERT_EQ( 0u, playbackDueWakeSliceWaitMs( 1500, 1100, PlaybackDueWake::kSettleSliceMs ) );   // returns, no 1100 ms wait
    ASSERT_TRUE( PlaybackDueWake::kSettleSliceMs <= 10u );
#ifdef Q_OS_WIN
    // the wiring: no wait takes a relative time computed before a separate check
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 0, source.count( QStringLiteral( "WaitForSingleObject( args.stopEvent, 1 )" ) ) );
    ASSERT_EQ( 0, source.count( QStringLiteral( "static_cast<DWORD>( deadline - now )" ) ) );
    const int retryWait = source.indexOf( QStringLiteral(
        "const DWORD retryWaitMs = playbackDueWakeSliceWaitMs( GetTickCount64(), start + budget, 1 );" ) );
    const int retrySpent = source.indexOf( QStringLiteral( "if( retryWaitMs == 0 ) break;" ), retryWait );
    const int retryUse = source.indexOf( QStringLiteral( "WaitForSingleObject( args.stopEvent, retryWaitMs )" ), retryWait );
    const int recheck = source.indexOf( QStringLiteral( "if( GetTickCount64() - start >= budget ) break;" ), retryUse );
    const int attempt = source.indexOf( QStringLiteral( "if( postDueWake( args, postQpc.QuadPart, generation ) )" ), retryUse );
    ASSERT_TRUE( retryWait > 0 && retrySpent > retryWait && retryUse > retrySpent && recheck > retryUse && attempt > recheck );
    const int settleWait = source.indexOf( QStringLiteral(
        "const DWORD settleWaitMs = playbackDueWakeSliceWaitMs( GetTickCount64(), deadline, kSettleSliceMs );" ) );
    const int settleUse = source.indexOf( QStringLiteral( "WaitForSingleObject( d->settledEvent, settleWaitMs );" ), settleWait );
    ASSERT_TRUE( settleWait > 0 && settleUse > settleWait );
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

// ---------------------------------------------------------------------------------------------------------------------------
// HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1, step 1 (measurement). On Bachelor CUDA (4adc11c9) the fullscreen wake was
// posted on time but taken 7.9-9.2 ms later (post_to_receive p50, s2/s4) and 1162 of 1217 fullscreen s1 arms were replaced
// before the GUI thread took their post. These pin the split that says WHERE the GUI thread was at each post: the dispatcher's
// wait (aboutToBlock), the smoke runner's own sleep, or running.

TEST(PlaybackGuiSleepTrace, TheStateAtAnInstantIsTheLatestTransitionAndASleepKnowsWhenItWasLeft)
{
    typedef PlaybackGuiSleepTrace::State S;
    PlaybackGuiSleepTrace trace;
    ASSERT_TRUE( trace.at( 5.0 ).state == S::Unknown );   // nothing traced yet
    trace.note( S::Running, 10.0 );
    trace.note( S::RunnerSleep, 12.0 );
    trace.note( S::Running, 22.5 );
    trace.note( S::Running, 23.0 );                        // folded into the 22.5 transition (awake at every processEvents)
    trace.note( S::DispatcherWait, 30.0 );
    trace.note( S::Unknown, 31.0 );                        // never a transition
    ASSERT_EQ( static_cast<std::size_t>( 4 ), trace.transitions() );
    ASSERT_TRUE( trace.at( 9.0 ).state == S::Unknown );    // older than anything kept: never guessed
    PlaybackGuiSleepTrace::At at = trace.at( 15.0 );
    ASSERT_TRUE( at.state == S::RunnerSleep );
    ASSERT_TRUE( at.hasLeft );
    ASSERT_NEAR( 22.5, at.leftMs, 1e-9 );                  // left at the FIRST later transition, not the folded one
    at = trace.at( 12.0 );                                 // at the instant of the transition: the state it entered
    ASSERT_TRUE( at.state == S::RunnerSleep );
    at = trace.at( 25.0 );
    ASSERT_TRUE( at.state == S::Running );
    ASSERT_FALSE( at.hasLeft );
    at = trace.at( 31.0 );
    ASSERT_TRUE( at.state == S::DispatcherWait );
    ASSERT_FALSE( at.hasLeft );                            // still waiting: not left yet
    ASSERT_TRUE( PlaybackGuiSleepTrace::sleeping( S::DispatcherWait ) && PlaybackGuiSleepTrace::sleeping( S::RunnerSleep ) );
    ASSERT_FALSE( PlaybackGuiSleepTrace::sleeping( S::Running ) || PlaybackGuiSleepTrace::sleeping( S::Unknown ) );
    trace.reset();
    ASSERT_TRUE( trace.at( 31.0 ).state == S::Unknown );
}

TEST(PlaybackGuiSleepTrace, TheRingIsBoundedAndAnInstantOlderThanItsOldestKeptTransitionIsUnknown)
{
    typedef PlaybackGuiSleepTrace::State S;
    PlaybackGuiSleepTrace trace;
    const std::size_t n = PlaybackGuiSleepTrace::kCapacity * 3;
    for( std::size_t i = 0; i < n; ++i ) trace.note( ( i % 2 ) ? S::Running : S::RunnerSleep, static_cast<double>( i ) );
    ASSERT_EQ( n, trace.transitions() );
    ASSERT_TRUE( trace.at( 1.5 ).state == S::Unknown );    // overwritten: Unknown, not the oldest kept state
    const double oldestKept = static_cast<double>( n - PlaybackGuiSleepTrace::kCapacity );   // even: a RunnerSleep entry
    ASSERT_TRUE( trace.at( oldestKept - 0.5 ).state == S::Unknown );
    const PlaybackGuiSleepTrace::At at = trace.at( oldestKept + 0.5 );
    ASSERT_TRUE( at.state == S::RunnerSleep );
    ASSERT_TRUE( at.hasLeft );
    ASSERT_NEAR( oldestKept + 1.0, at.leftMs, 1e-9 );
    ASSERT_TRUE( trace.at( static_cast<double>( n ) ).state == S::Running );   // the newest (n - 1 is odd)
    ASSERT_TRUE( sizeof( PlaybackGuiSleepTrace ) <= PlaybackGuiSleepTrace::kCapacity * 16 + 16 );   // fixed, whatever the Play's length
}

TEST(PlaybackDueWakeClassify, APostIsAsleepAwakeOrUnknownAndTheAsleepCaseSplitsAtTheWake)
{
    typedef PlaybackGuiSleepTrace::State S;
    typedef PlaybackDueWakePostClass::Kind K;
    // the 4adc11c9 fullscreen shape: posted 1 ms into a 10 ms runner sleep, taken right after it
    PlaybackDueWakePostClass c = playbackDueWakeClassifyPost( { S::RunnerSleep, true, 21.0 }, 12.0, 21.4 );
    ASSERT_TRUE( c.kind == K::Asleep );
    ASSERT_TRUE( c.runnerSleep );
    ASSERT_NEAR( 9.0, c.postToAwakeMs, 1e-9 );
    ASSERT_NEAR( 0.4, c.awakeToReceiveMs, 1e-9 );
    // posted into the dispatcher's wait, which returned at once
    c = playbackDueWakeClassifyPost( { S::DispatcherWait, true, 12.2 }, 12.0, 12.3 );
    ASSERT_TRUE( c.kind == K::Asleep );
    ASSERT_FALSE( c.runnerSleep );
    ASSERT_NEAR( 0.2, c.postToAwakeMs, 1e-9 );
    ASSERT_NEAR( 0.1, c.awakeToReceiveMs, 1e-9 );
    // posted while the GUI thread was running: all of it is occupancy
    c = playbackDueWakeClassifyPost( { S::Running, false, 0.0 }, 12.0, 20.0 );
    ASSERT_TRUE( c.kind == K::Awake );
    ASSERT_NEAR( 8.0, c.awakeToReceiveMs, 1e-9 );
    // a sleep that was not left (or left after the receive, or before the post) is inconsistent: Unknown, never a split
    ASSERT_TRUE( playbackDueWakeClassifyPost( { S::RunnerSleep, false, 0.0 }, 12.0, 20.0 ).kind == K::Unknown );
    ASSERT_TRUE( playbackDueWakeClassifyPost( { S::RunnerSleep, true, 25.0 }, 12.0, 20.0 ).kind == K::Unknown );
    ASSERT_TRUE( playbackDueWakeClassifyPost( { S::DispatcherWait, true, 11.0 }, 12.0, 20.0 ).kind == K::Unknown );
    ASSERT_TRUE( playbackDueWakeClassifyPost( { S::Unknown, false, 0.0 }, 12.0, 20.0 ).kind == K::Unknown );
}

TEST(PlaybackDueWakeNative, APostWhoseArmIsReplacedBeforeTheGuiTakesItIsStrandedAndSplitByWhereTheGuiWas)
{
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; } );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    ASSERT_EQ( std::string( "post" ), std::string( wake.deliveryKindName() ) );   // the bool constructor keeps PostMessageW
    // trace off: the stranded post is counted, its GUI state Unknown
    wake.arm( 0.5 );
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.armPostPublished(); }, 1000 ) );
    wake.arm( 50.0 );                                             // replaced before anyone took the post
    PlaybackDueWake::StrandedCounts s = wake.strandedCounts();
    ASSERT_EQ( 1u, s.total );
    ASSERT_EQ( 1u, s.unknown );
    wake.cancel();                                                // that arm never posted: nothing more is stranded
    ASSERT_EQ( 1u, wake.strandedCounts().total );
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 60 ) );              // the stranded message is taken late and dropped
    ASSERT_EQ( 0, fired );
    // trace on, posted inside the smoke runner's sleep
    wake.setGuiTraceActive( true );
    ASSERT_TRUE( wake.guiTraceActive() );
    wake.noteRunnerSleep( true );
    wake.arm( 0.5 );
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.armPostPublished(); }, 1000 ) );
    wake.noteRunnerSleep( false );
    wake.cancel();
    s = wake.strandedCounts();
    ASSERT_EQ( 2u, s.total );
    ASSERT_EQ( 1u, s.runnerSleep );
    // posted while running
    wake.arm( 0.5 );
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.armPostPublished(); }, 1000 ) );
    wake.arm( 50.0 );
    s = wake.strandedCounts();
    ASSERT_EQ( 3u, s.total );
    ASSERT_EQ( 1u, s.running );
    ASSERT_EQ( 0u, s.dispatcherWait );
    wake.cancel();
    ASSERT_EQ( 2, pumpDueWakeMessages( wake, 60 ) );
    ASSERT_EQ( 0, fired );
    // a post the GUI thread took is never stranded
    wake.arm( 0.5 );
    ASSERT_EQ( 1, pumpDueWakeMessages( wake, 200 ) );
    ASSERT_EQ( 1, fired );
    wake.arm( 50.0 );
    wake.cancel();
    ASSERT_EQ( 3u, wake.strandedCounts().total );
    wake.resetStrandedCounts();
    ASSERT_EQ( 0u, wake.strandedCounts().total );
    wake.setGuiTraceActive( false );
    ASSERT_FALSE( wake.guiTraceActive() );
    ASSERT_TRUE( wake.guiStateAt( 1.0e12 ).state == PlaybackGuiSleepTrace::State::Unknown );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeNative, QtsWindowsDispatcherSignalsAboutToBlockSoAWakePostedIntoAnIdleLoopReadsAsleep)
{
    // K0 for FULLSCREEN-S1S2-PRESENT-GAP-1: the split needs QAbstractEventDispatcher::aboutToBlock / awake around the Windows
    // dispatcher's wait (Qt 6.10.2 ships no source in the install, so this proves it on the real dispatcher): a wake posted
    // while a QEventLoop idles is classified Asleep in the dispatcher's wait, and its receive follows the wait's end.
#ifdef Q_OS_WIN
    LARGE_INTEGER frequency;
    ASSERT_TRUE( QueryPerformanceFrequency( &frequency ) && frequency.QuadPart > 0 );
    const auto qpcMs = [&frequency]() -> double
    {
        LARGE_INTEGER now;
        QueryPerformanceCounter( &now );
        return static_cast<double>( now.QuadPart ) * 1000.0 / static_cast<double>( frequency.QuadPart );
    };
    int fired = 0;
    double postMs = -2.0;
    double receivedMs = -2.0;
    PlaybackGuiSleepTrace::At at { PlaybackGuiSleepTrace::State::Unknown, false, 0.0 };
    PlaybackDueWake *self = nullptr;
    QEventLoop loop;
    PlaybackDueWake wake( nullptr, [&]() {
        ++fired;
        postMs = self->lastDeliveryPostMs();
        receivedMs = qpcMs();
        at = self->guiStateAt( postMs );
        loop.quit();
    } );
    self = &wake;
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    wake.setGuiTraceActive( true );
    QTimer guard;
    guard.setSingleShot( true );
    QObject::connect( &guard, &QTimer::timeout, &loop, &QEventLoop::quit );
    for( int attempt = 0; attempt < 10 && at.state != PlaybackGuiSleepTrace::State::DispatcherWait; ++attempt )
    {
        wake.arm( 20.0 );
        guard.start( 1000 );
        loop.exec();
        guard.stop();
    }
    ASSERT_TRUE( fired >= 1 );
    ASSERT_TRUE( at.state == PlaybackGuiSleepTrace::State::DispatcherWait );
    ASSERT_TRUE( at.hasLeft );
    const PlaybackDueWakePostClass c = playbackDueWakeClassifyPost( at, postMs, receivedMs );
    ASSERT_TRUE( c.kind == PlaybackDueWakePostClass::Kind::Asleep );
    ASSERT_FALSE( c.runnerSleep );
    ASSERT_TRUE( c.postToAwakeMs < 50.0 );                        // the wait returned for the posted message
    wake.setGuiTraceActive( false );
#else
    SKIP_TEST( "the Windows event dispatcher" );
#endif
}

TEST(PlaybackDueWakeNative, TheSummaryLineCarriesTheGuiStateSplitAndTheRunnerSleepIsTraced)
{
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_FALSE( mainWindow.isEmpty() );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral(
        "\" stranded=%1 stranded_asleep=%2 stranded_runner_sleep=%3 stranded_awake=%4 \"" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral(
        "\"stranded_unknown=%5 post_asleep=%6 post_runner_sleep=%7 post_awake=%8 \"" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral(
        "\"awake_to_receive_p95_ms=%4 reentrant_deferred=%5 delivery_kind=%6\"" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( ".arg( m_playbackDueWakeUnpresentedDeferred ) + split + bound + gui;" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral(
        "playbackDueWakeClassifyPost( m_playbackDueWake->guiStateAt( postMs ), postMs, nowMs );" ) ) );
    // the trace runs only inside a smoke session, and the runner's sleep is bracketed
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "if( m_playbackDueWake ) m_playbackDueWake->setGuiTraceActive( true );" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "m_playbackDueWake->setGuiTraceActive( m_playbackSmokeActive );" ) ) );
    const QString sleepBody = functionBody( mainWindow, QStringLiteral( "void MainWindow::sleepGuiThreadForSmokeRunner( unsigned long ms )" ),
                                            QStringLiteral( "void MainWindow::presentPlaybackPreparedFrame(" ) );
    const int begin = sleepBody.indexOf( QStringLiteral( "m_playbackDueWake->noteRunnerSleep( true );" ) );
    const int end = sleepBody.indexOf( QStringLiteral( "m_playbackDueWake->noteRunnerSleep( false );" ) );
    ASSERT_TRUE( begin > 0 && end > begin );
    // the line's sets stay bounded (PlaybackDueWakeSamples), and both disclose their range
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "playbackDueWakeRangeFields( m_playbackDueWakePostToAwakeMs, \"post_to_awake\" )" ) ) );
    const QString header = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.h" ) );
    ASSERT_EQ( 1, header.count( QStringLiteral( "PlaybackDueWakeSamples m_playbackDueWakeAwakeToReceiveMs;" ) ) );
}
// ---------------------------------------------------------------------------------------------------------------------------
// HFR-NATIVE-M16-1457-FULLSCREEN-S1S2-PRESENT-GAP-1, step 2 (the lever): the wake is SENT (SendNotifyMessageW) by default, and the
// --gui-smoke-playback runner's sleep is a QS_SENDMESSAGE wait the sent wake ends. MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY=post is r2.

#ifdef Q_OS_WIN
namespace
{
double dueWakeQpcNowMs()
{
    LARGE_INTEGER frequency;
    LARGE_INTEGER now;
    QueryPerformanceFrequency( &frequency );
    QueryPerformanceCounter( &now );
    return static_cast<double>( now.QuadPart ) * 1000.0 / static_cast<double>( frequency.QuadPart );
}

// CI-FLAKE-PLAYBACK-DUE-WAKE-R17A1-TIMING-1 r7 (sol review r6, blocker 3): the sibling floors (G1, R3A3, R2A1) allow one timer tick
// (dueWakeSleepFloorMs), and a floor read on the TEST's clock after the sleep returned can be moved by a pause of this thread
// between the return and the read. So a sleep is measured on the PRODUCT's own reads of the clock it is handed (its entry read and
// its final read, taken at the loop exit), and a sample judges a dispatch-triggered early exit only when the sleep's last dispatch
// ended at least kSleepExitMarginMs before that floor: a sleep cut short at that dispatch then reads below the floor, whatever
// the tick slack. A sample whose last dispatch ended too late (a GUI thread paused until the floor) cannot tell the two apart; it
// is observed again, never passed (the callers retry, then fail when no sample was judged).
constexpr double kSleepExitMarginMs = 4.0;
constexpr int kSleepObservationAttempts = 3;
double g_dueWakeSleepFirstS = 0.0;
double g_dueWakeSleepLastS = 0.0;
int g_dueWakeSleepReads = 0;
double dueWakeQpcNowS()
{
    return dueWakeQpcNowMs() / 1000.0;
}
void dueWakeRecordedClockReset()
{
    g_dueWakeSleepFirstS = 0.0;
    g_dueWakeSleepLastS = 0.0;
    g_dueWakeSleepReads = 0;
}
// the clock a sleep under observation is handed: it records its first read (the entry) and its last (the loop exit's own read)
double dueWakeRecordedNowS()
{
    const double s = dueWakeQpcNowS();
    if( g_dueWakeSleepReads == 0 ) g_dueWakeSleepFirstS = s;
    ++g_dueWakeSleepReads;
    g_dueWakeSleepLastS = s;
    return s;
}
struct DueWakeSleepObservation
{
    double sleptMs;      // the sleep's own entry read to its own final read
    bool exposed;        // its last dispatch ended early enough that an exit at it would read below the floor
};
DueWakeSleepObservation dueWakeObserveSleep( const std::vector<std::pair<double, double>> & dispatchSpansS, double requestedMs )
{
    DueWakeSleepObservation observation { ( g_dueWakeSleepLastS - g_dueWakeSleepFirstS ) * 1000.0, false };
    if( dispatchSpansS.empty() || g_dueWakeSleepReads < 2 ) return observation;
    const double lastDispatchEndMs = ( dispatchSpansS.back().second - g_dueWakeSleepFirstS ) * 1000.0;
    observation.exposed = lastDispatchEndMs + kSleepExitMarginMs <= dueWakeSleepFloorMs( requestedMs );
    return observation;
}
}
#endif

TEST(PlaybackDueWakeSend, A_ARunnerSleepEndsForASentWakeButNotForAPostedOne)
{
#ifdef Q_OS_WIN
    // RED (the 4adc11c9 fullscreen mechanism): a POSTED wake does not end a wait that names only QS_SENDMESSAGE, nor a
    // QThread::msleep; it waits for the sleep's end. The post arm's runner sleep is the unchanged msleep (NotHandled).
    int postFired = 0;
    PlaybackDueWake postWake( nullptr, [&postFired]() { ++postFired; }, PlaybackDueWakeDeliveryMode::Post );
    ASSERT_TRUE( postWake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    ASSERT_FALSE( postWake.sendsNotify() );
    ASSERT_EQ( std::string( "post" ), std::string( postWake.deliveryKindName() ) );
    ASSERT_TRUE( postWake.sleepForSmokeRunner( 1 ) == PlaybackDueWake::RunnerSleep::NotHandled );
    postWake.arm( 2.0 );
    ASSERT_TRUE( waitWithoutPumping( [&postWake]() { return postWake.armPostPublished(); }, 1000 ) );
    ASSERT_EQ( static_cast<DWORD>( WAIT_TIMEOUT ),
               MsgWaitForMultipleObjectsEx( 0, nullptr, 60, QS_SENDMESSAGE, MWMO_INPUTAVAILABLE ) );
    ASSERT_EQ( 0, postFired );
    ASSERT_EQ( 1, pumpDueWakeMessages( postWake, 60 ) );          // only a pump takes it
    ASSERT_EQ( 1, postFired );
    // GREEN: the sent wake ends the runner's sleep long before its 500 ms, and runs only when dispatched
    int fired = 0;
    double firedMs = -2.0;
    PlaybackDueWake wake( nullptr, [&]() { ++fired; firedMs = dueWakeQpcNowMs(); }, PlaybackDueWakeDeliveryMode::SendNotify );
    ASSERT_TRUE( wake.delivery() == PlaybackDueWake::Delivery::NativeMessage );
    ASSERT_TRUE( wake.sendsNotify() );
    ASSERT_EQ( std::string( "send" ), std::string( wake.deliveryKindName() ) );
    const double armedMs = dueWakeQpcNowMs();
    wake.arm( 2.0 );
    const PlaybackDueWake::RunnerSleep slept = wake.sleepForSmokeRunner( 500 );
    const double wokeMs = dueWakeQpcNowMs();
    ASSERT_TRUE( slept == PlaybackDueWake::RunnerSleep::WokenBySent );
    ASSERT_TRUE( wokeMs - armedMs < 250.0 );                      // woken by the wake, not the 500 ms timeout
    ASSERT_EQ( 0, fired );                                        // nothing ran inside the sleep
    ASSERT_EQ( 1u, wake.runnerSleepsWokenBySent() );
    wake.dispatchSentMessages();
    ASSERT_EQ( 1, fired );
    // r3 (r2 adversary pass 6 advisory A4): dispatched at once, no queue in between -- bounded well below the 500 ms sleep the
    // wake cut short, not at 5 ms, so a test thread descheduled for a few ms between the two stamps does not fail it
    ASSERT_TRUE( firedMs - wokeMs < 50.0 );
    ASSERT_FALSE( wake.armed() );
    // the design's shape: a filtered PM_NOREMOVE peek of an unrelated range (which marks the queue seen), then a plain
    // QS_ALLINPUT wait without MWMO_INPUTAVAILABLE -- a sent wake that arrives during it still ends it, and the next
    // PeekMessage, whatever its filter, dispatches it
    MSG unrelated;
    PeekMessageW( &unrelated, nullptr, WM_USER + 1, WM_USER + 1, PM_NOREMOVE );
    wake.arm( 2.0 );
    const ULONGLONG allInputDeadline = GetTickCount64() + 1000;
    while( fired < 2 && GetTickCount64() < allInputDeadline )
    {
        // other input of this thread can end the wait first; the sent wake always ends one, and is dispatched by the peek
        MsgWaitForMultipleObjectsEx( 0, nullptr, 500, QS_ALLINPUT, 0 );
        PeekMessageW( &unrelated, nullptr, WM_USER + 1, WM_USER + 1, PM_NOREMOVE );
    }
    ASSERT_EQ( 2, fired );
    // a sleep no sent message interrupts runs its whole length
    const double before = dueWakeQpcNowMs();
    ASSERT_TRUE( wake.sleepForSmokeRunner( 30 ) == PlaybackDueWake::RunnerSleep::Slept );
    ASSERT_TRUE( dueWakeQpcNowMs() - before >= dueWakeSleepFloorMs( 30.0 ) );   // 30 ms less one timer tick (a dispatch-free sleep; r7: was 25.0)
    ASSERT_EQ( 2, fired );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeSend, B_TheDeliveryEnvironmentParsesPostNotifierAndDefaultsToSend)
{
    typedef PlaybackDueWakeDeliveryMode M;
    ASSERT_TRUE( playbackDueWakeDeliveryModeFromText( QStringLiteral( "post" ) ) == M::Post );
    ASSERT_TRUE( playbackDueWakeDeliveryModeFromText( QStringLiteral( "  POST \n" ) ) == M::Post );
    ASSERT_TRUE( playbackDueWakeDeliveryModeFromText( QStringLiteral( "notifier" ) ) == M::Notifier );
    ASSERT_TRUE( playbackDueWakeDeliveryModeFromText( QStringLiteral( "Notifier" ) ) == M::Notifier );
    ASSERT_TRUE( playbackDueWakeDeliveryModeFromText( QString() ) == M::SendNotify );
    ASSERT_TRUE( playbackDueWakeDeliveryModeFromText( QStringLiteral( "send" ) ) == M::SendNotify );
    ASSERT_TRUE( playbackDueWakeDeliveryModeFromText( QStringLiteral( "posted" ) ) == M::SendNotify );
    ASSERT_TRUE( playbackDueWakeDeliveryModeFromText( QStringLiteral( "native-msg" ) ) == M::SendNotify );
#ifdef Q_OS_WIN
    PlaybackDueWake notifier( nullptr, []() {}, M::Notifier );
    ASSERT_TRUE( notifier.delivery() == PlaybackDueWake::Delivery::Notifier );
    ASSERT_EQ( std::string( "notifier" ), std::string( notifier.deliveryKindName() ) );
    ASSERT_FALSE( notifier.sendsNotify() );
    ASSERT_TRUE( notifier.sleepForSmokeRunner( 1 ) == PlaybackDueWake::RunnerSleep::NotHandled );
#endif
    // read once, through the pure parse, and handed to the constructor; the r2 bool reader is gone
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 1, source.count( QStringLiteral(
        "playbackDueWakeDeliveryModeFromText( qEnvironmentVariable( \"MLVAPP_PLAYBACK_DUE_WAKE_DELIVERY\" ) );" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "static const PlaybackDueWakeDeliveryMode mode =" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "mlvappPlaybackDueWakeDeliveryMode() ) );" ) ) );
    ASSERT_EQ( 0, source.count( QStringLiteral( "mlvappPlaybackDueWakePreferNativeMessage" ) ) );
    // post keeps r2's PostMessageW call byte for byte; send is the other branch of the same function
    const QString wakeSource = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral( "PostMessageW( args.window, PlaybackDueWake::kNativeWakeMessage," ) ) );
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral( "return SendNotifyMessageW( args.window, PlaybackDueWake::kNativeWakeMessage," ) ) );
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral( "d->mode == PlaybackDueWakeDeliveryMode::SendNotify };" ) ) );
}

TEST(PlaybackDueWakeSend, C_AWakeInsideAPresentOrAnAdvanceIsDeferredCountedAndReArmedNeverAdvanced)
{
#ifdef Q_OS_WIN
    // why the guard exists: a SENT wake is dispatched inside ANY PeekMessage on the GUI thread, a non-Qt one nested in a
    // present included (a posted one is taken only by the dispatcher)
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; }, PlaybackDueWakeDeliveryMode::SendNotify );
    wake.arm( 1.0 );
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.armPostPublished(); }, 1000 ) );
    MSG nested;
    PeekMessageW( &nested, nullptr, WM_USER + 7, WM_USER + 7, PM_NOREMOVE | PM_NOYIELD );   // e.g. a driver's own peek
    ASSERT_EQ( 1, fired );
#endif
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString onWake = functionBody( source, QStringLiteral( "void MainWindow::onPlaybackDueWake( void )" ),
                                         QStringLiteral( "void MainWindow::cancelPlaybackDueWake( void )" ) );
    ASSERT_FALSE( onWake.isEmpty() );
    const int classify = onWake.indexOf( QStringLiteral( "playbackDueWakeClassifyPost(" ) );
    const int guard = onWake.indexOf( QStringLiteral( "if( m_playbackDueWakeDeferDepth > 0 )" ) );
    const int counted = onWake.indexOf( QStringLiteral( "++m_playbackDueWakeReentrantDeferred;" ), guard );
    const int pending = onWake.indexOf( QStringLiteral( "m_playbackDueWakeDeferredSinceArm = true;" ), guard );
    const int returned = onWake.indexOf( QStringLiteral( "return;" ), guard );
    const int outcome = onWake.indexOf( QStringLiteral( "const PlaybackDueWakeOutcome outcome =" ) );
    const int advance = onWake.indexOf( QStringLiteral( "timerFrameEvent( true );" ) );
    // measured first, then deferred before the outcome (no pending-advance flag is touched) and before any advance
    ASSERT_TRUE( classify > 0 && guard > classify && counted > guard && pending > guard && returned > pending );
    ASSERT_TRUE( outcome > returned && advance > outcome );
    // r1 adversary pass 1 A4: a deferred delivery is still measured in full -- an unsplit one is counted post_unsplit BEFORE
    // the deferral returns, and that count stays the else-branch of the split (never bound to the deferral's if)
    const int unsplitCounted = onWake.indexOf( QStringLiteral( "++m_playbackDueWakePostUnsplit;" ) );
    ASSERT_TRUE( unsplitCounted > classify && unsplitCounted < guard );
    ASSERT_EQ( 1, onWake.count( QStringLiteral( "            break;\n        }\n    }\n"
                                                "    // r5 adversary pass 9 A1: postMs is PostMessageW's return time; a native delivery taken before the wait thread published\n"
                                                "    // it is in neither split set (post_unsplit), never split at the first stamp\n"
                                                "    else if( m_playbackDueWake && m_playbackDueWake->lastDeliveryUnsplit() )\n" ) ) );
    // the scopes: a present (both entry points) and every advance
    ASSERT_EQ( 1, source.count( QStringLiteral(
        "    const GuiOtherBusyScope otherBusySpan( this, gui_present_gap::OtherBusySpan::DrawFrameReady );\n"
        "    const PlaybackDueWakeDeferScope dueWakeDeferScope( this );" ) ) );
    const QString present = functionBody( source, QStringLiteral( "void MainWindow::presentPlaybackPreparedFrame( const PlaybackPrepResult &result )" ),
                                          QStringLiteral( "m_lastPresentTailTiming = GpuPresentTailTiming();" ) );
    ASSERT_TRUE( present.contains( QStringLiteral( "const PlaybackDueWakeDeferScope dueWakeDeferScope( this );" ) ) );
    const QString timer = functionBody( source, QStringLiteral( "void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )" ),
                                        QStringLiteral( "if( m_lookAssistPump.sessionOpen() ) return;" ) );
    ASSERT_TRUE( timer.contains( QStringLiteral( "const PlaybackDueWakeDeferScope dueWakeDeferScope( this );" ) ) );
    // every arm clears the pending deferral; only the outermost scope end re-arms, and only when none did
    // armPlaybackDueWake, the wake's own re-arm, and the scope end (before it re-arms)
    ASSERT_EQ( 3, source.count( QStringLiteral( "m_playbackDueWakeDeferredSinceArm = false;" ) ) );
    const QString scopeEnd = functionBody( source, QStringLiteral( "MainWindow::PlaybackDueWakeDeferScope::~PlaybackDueWakeDeferScope()" ),
                                           QStringLiteral( "void MainWindow::cancelPlaybackDueWake( void )" ) );
    const int depth = scopeEnd.indexOf( QStringLiteral( "if( --m_window->m_playbackDueWakeDeferDepth > 0 ) return;" ) );
    const int none = scopeEnd.indexOf( QStringLiteral( "if( !m_window->m_playbackDueWakeDeferredSinceArm ) return;" ) );
    const int rearm = scopeEnd.indexOf( QStringLiteral( "m_window->armPlaybackDueWake();" ) );
    ASSERT_TRUE( depth > 0 && none > depth && rearm > none );
}

TEST(PlaybackDueWakeSend, D_AStaleSentWakeThroughTheWindowProcedureIsDroppedByTheGateAndCountedStranded)
{
#ifdef Q_OS_WIN
    int fired = 0;
    double firedPostFirstMs = -1.0;                                   // the wait thread's own stamp of the delivery that fired
    double firedPostReturn = -1.0;                                    // ...and its stamp of when the post returned (the latest the post can have been)
    bool firedStamped = false;                                        // ...and whether that delivery carried them
    bool firedUnsplit = false;                                        // ...or was taken before the return stamp was published
    double firedReceiveMs = -1.0;                                     // the GUI thread's own receive time (QPC ms)
    PlaybackDueWake *self = nullptr;
    PlaybackDueWake wake( nullptr, [&]() { ++fired; firedReceiveMs = dueWakeQpcNowMs(); const double stamp = self->lastDeliveryPostFirstMs(); firedPostReturn = self->lastDeliveryPostMs(); firedStamped = stamp >= 0.0 && firedPostReturn >= 0.0; firedUnsplit = self->lastDeliveryUnsplit(); firedPostFirstMs = stamp; }, PlaybackDueWakeDeliveryMode::SendNotify );
    self = &wake;
    // r3 (adversary pass 4 A1 sibling): the replacement's own due time is asserted on every judged attempt (see kReplacementMax*)
    // r7 (sol review r6, blocker 2): kReplacementProofsNeeded attempts must clear the floor (see kReplacementProofsNeeded)
    int proofs = 0;
    for( int attempt = 0; attempt < kReplacementAttempts && proofs < kReplacementProofsNeeded; ++attempt )
    {
        fired = 0;
        firedPostFirstMs = -1.0;
        firedPostReturn = -1.0;
        firedStamped = false;
        firedUnsplit = false;
        firedReceiveMs = -1.0;
        wake.resetStrandedCounts();
        wake.arm( 0.5 );
        ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.armPostPublished(); }, 1000 ) );
        QElapsedTimer t;
        t.start();
        const double preArmMs = dueWakeQpcNowMs();
        wake.arm( 40.0 );                                         // re-armed before the GUI thread took the sent wake
        const qint64 armCostNs = t.nsecsElapsed();
        if( armCostNs >= kReplacementMaxArmCostNs )
        {
            // not judgeable (a pause before the arm could hide a shortened replacement): drain it, observe again
            wake.sleepForSmokeRunner( 1000 );
            wake.dispatchSentMessages();
            continue;
        }
        ASSERT_EQ( 1u, wake.strandedCounts().total );
        wake.dispatchSentMessages();                              // dueWakeWindowProc -> handleNativeMessage -> the gate
        ASSERT_EQ( 0, fired );                                    // the old generation: dropped
        ASSERT_TRUE( wake.armed() );                              // ...and the replacement is still armed
        ASSERT_TRUE( wake.sleepForSmokeRunner( 1000 ) == PlaybackDueWake::RunnerSleep::WokenBySent );
        wake.dispatchSentMessages();
        ASSERT_EQ( 1, fired );                                    // the replacement fires once, at its own due time
        ASSERT_FALSE( wake.armed() );
        // the wait thread stamped the replacement's delivery on its own clock, so a pause of THIS thread after the wake cannot
        // lengthen what is measured: from before the arm to that stamp is >= the 40 ms due (less slack), never the stale wake's ~0.5 ms
        // r4 (sol review r3, blocker 1): early is a failure, late (past kReplacementMaxElapsedNs) or unstamped is observed again
        // r7: a delivery the GUI took before the wait thread published its return stamp carries no split (PlaybackDueWake.h
        // lastDeliveryPostFirstMs, "unsplit"); it is judged on the GUI thread's receive time (not early) and counts as a proof,
        // never retried away, so five healthy wakes landing that way no longer fail the test
        const ReplacementAttempt verdict = classifyReplacementAttempt( armCostNs, replacementStampsOf( firedStamped, firedUnsplit ),
            ( firedPostFirstMs - preArmMs ) * 1000000.0, ( firedPostReturn - preArmMs ) * 1000000.0, ( firedReceiveMs - preArmMs ) * 1000000.0 );
        ASSERT_TRUE( verdict != ReplacementAttempt::TooEarly );
        if( replacementAttemptProves( verdict ) ) ++proofs;
    }
    ASSERT_TRUE( proofs >= kReplacementProofsNeeded );            // never passes by skipping the bound
    // cancelled after the send: dropped too
    wake.arm( 0.5 );
    ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.armPostPublished(); }, 1000 ) );
    wake.cancel();
    wake.dispatchSentMessages();
    ASSERT_EQ( 1, fired );
    ASSERT_EQ( 2u, wake.strandedCounts().total );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

// r4 (sol review r3, blocker 1), its inputs verbatim: pre-arm pause 18 ms with the replacement mapped 40 -> 18 ms (arm returns at
// 18.2 ms, delivery stamped at 36.2 ms) passed both bounds at r3; so did the replacement mapped 40 -> 80 ms (stamped at 80.2 ms).
TEST(PlaybackDueWakeSend, R4A1_TheReplacementBoundRejectsAnEarlyDeliveryAndNeverCreditsAPauseBeforeTheArm)
{
#ifdef Q_OS_WIN
    // a delivery that carried the wait thread's first and return stamps (split), received by the GUI thread at the return stamp
    const auto classifySplit = []( qint64 armCostNs, bool stamped, double firstElapsedNs, double returnElapsedNs ) {
        return classifyReplacementAttempt( armCostNs, replacementStampsOf( stamped, false ), firstElapsedNs, returnElapsedNs, returnElapsedNs );
    };
    // a healthy replacement: 0.2 ms arm, first stamp 40.2 ms and return stamp 40.3 ms after the pre-arm read
    ASSERT_TRUE( classifySplit(200000, true, 40200000.0, 40300000.0 ) == ReplacementAttempt::OnTime );
    // 40 -> 18 ms with an 18 ms pause before the arm: the arm cost is 18.2 ms, so the attempt is not judged (never credited 18 ms)
    ASSERT_TRUE( classifySplit(18200000, true, 36200000.0, 36300000.0 ) == ReplacementAttempt::NotJudgeable );
    // the same shortened replacement with no pause is judged, and is early
    ASSERT_TRUE( classifySplit(200000, true, 18200000.0, 18300000.0 ) == ReplacementAttempt::TooEarly );
    // 40 -> 8 ms: early with no pause, not judged with a 6 ms pause (6.2 ms of arm cost)
    ASSERT_TRUE( classifySplit(200000, true, 8200000.0, 8300000.0 ) == ReplacementAttempt::TooEarly );
    ASSERT_TRUE( classifySplit(6200000, true, 14200000.0, 14300000.0 ) == ReplacementAttempt::NotJudgeable );
    // adversary pass 1 A2: 40 -> 31 ms with a 4.3 ms pause inside the arm (4.4 ms of arm cost, first stamp 35.4 ms) passed at the
    // 5 ms cap and the 35 ms floor; the 2 ms cap leaves it unjudged, and the 38 ms floor rejects the same shortening without a pause
    ASSERT_TRUE( classifySplit(4400000, true, 35400000.0, 35500000.0 ) == ReplacementAttempt::NotJudgeable );
    ASSERT_TRUE( classifySplit(200000, true, 31200000.0, 31300000.0 ) == ReplacementAttempt::TooEarly );
    // 40 -> 80 ms: a delivery 40 ms late is not the replacement's due time (never OnTime, so the test cannot pass on it)
    ASSERT_TRUE( classifySplit(200000, true, 80200000.0, 80300000.0 ) == ReplacementAttempt::TooLate );
    // adversary pass 1 A3: a wait thread paused after its first stamp (40.2 ms) and before its send read on time by the first
    // stamp alone; the return stamp (120.3 ms) is the latest the post can have been
    ASSERT_TRUE( classifySplit(200000, true, 40200000.0, 120300000.0 ) == ReplacementAttempt::TooLate );
    // a delivery with no wait-thread stamp has nothing to measure
    ASSERT_TRUE( classifySplit(200000, false, 40200000.0, 40300000.0 ) == ReplacementAttempt::NotJudgeable );
    // the edges: 38 ms (first stamp) and 70 ms (return stamp) are in, a cheap arm just under the cap is judged
    ASSERT_TRUE( classifySplit(1900000, true, 38000000.0, 38100000.0 ) == ReplacementAttempt::OnTime );
    ASSERT_TRUE( classifySplit(1900000, true, 69900000.0, 70000000.0 ) == ReplacementAttempt::OnTime );
    ASSERT_TRUE( classifySplit(2000000, true, 40000000.0, 40100000.0 ) == ReplacementAttempt::NotJudgeable );
    // adversary pass 1 A7: an unordered (NaN) elapsed time is never on time
    ASSERT_TRUE( classifySplit(200000, true, std::nan( "" ), 40300000.0 ) == ReplacementAttempt::TooEarly );
    ASSERT_TRUE( classifySplit(200000, true, 40200000.0, std::nan( "" ) ) == ReplacementAttempt::TooLate );
    // r7 (sol review r6, hardening): a split pair whose return stamp precedes its first stamp is no measurement
    ASSERT_TRUE( classifySplit(200000, true, 60000000.0, 50000000.0 ) == ReplacementAttempt::NotJudgeable );
    ASSERT_TRUE( classifySplit(200000, true, 40200000.0, 40100000.0 ) == ReplacementAttempt::NotJudgeable );
    // r7 (sol review r6, blocker 2): a delivery the GUI thread took before the return stamp was published (unsplit) is judged on
    // the GUI thread's own receive time: not early is a proof (never retried away), early is a verdict, a pause before the arm
    // is still not judged, and an unordered receive time is early
    ASSERT_TRUE( classifyReplacementAttempt( 200000, ReplacementStamps::Unsplit, -1.0, -1.0, 40400000.0 ) == ReplacementAttempt::NotEarlyOnly );
    ASSERT_TRUE( classifyReplacementAttempt( 200000, ReplacementStamps::Unsplit, -1.0, -1.0, 150000000.0 ) == ReplacementAttempt::NotEarlyOnly );
    ASSERT_TRUE( classifyReplacementAttempt( 200000, ReplacementStamps::Unsplit, -1.0, -1.0, 18400000.0 ) == ReplacementAttempt::TooEarly );
    ASSERT_TRUE( classifyReplacementAttempt( 18200000, ReplacementStamps::Unsplit, -1.0, -1.0, 40400000.0 ) == ReplacementAttempt::NotJudgeable );
    ASSERT_TRUE( classifyReplacementAttempt( 200000, ReplacementStamps::Unsplit, -1.0, -1.0, std::nan( "" ) ) == ReplacementAttempt::TooEarly );
    ASSERT_TRUE( classifyReplacementAttempt( 200000, ReplacementStamps::None, -1.0, -1.0, 40400000.0 ) == ReplacementAttempt::NotJudgeable );
    // only a split on-time or an unsplit not-early delivery proves; a late, unjudged or early one does not
    ASSERT_TRUE( replacementAttemptProves( ReplacementAttempt::OnTime ) );
    ASSERT_TRUE( replacementAttemptProves( ReplacementAttempt::NotEarlyOnly ) );
    ASSERT_FALSE( replacementAttemptProves( ReplacementAttempt::TooLate ) );
    ASSERT_FALSE( replacementAttemptProves( ReplacementAttempt::TooEarly ) );
    ASSERT_FALSE( replacementAttemptProves( ReplacementAttempt::NotJudgeable ) );
    // the property a single attempt cannot have (a worker paused 22 ms before its first stamp reads a timer shortened 40 -> 18 ms
    // as 40.2 ms): the attempts that are not paused read it early, and one early attempt fails the test (the callers assert
    // every attempt's verdict is not TooEarly, and need kReplacementProofsNeeded proofs from fresh arms)
    ASSERT_TRUE( classifySplit(200000, true, 40200000.0, 40300000.0 ) == ReplacementAttempt::OnTime );
    ASSERT_TRUE( classifySplit(200000, true, 18200000.0, 18300000.0 ) == ReplacementAttempt::TooEarly );
    ASSERT_TRUE( kReplacementProofsNeeded >= 3 && kReplacementAttempts > kReplacementProofsNeeded );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeSend, E_NothingIsSentAfterStopAndTheThreadIsJoinedBeforeTheWindowGoes)
{
#ifdef Q_OS_WIN
    int fired = 0;
    {
        PlaybackDueWake wake( nullptr, [&fired]() { ++fired; }, PlaybackDueWakeDeliveryMode::SendNotify );
        ASSERT_TRUE( wake.waitThreadRunning() );
        wake.stop();
        ASSERT_FALSE( wake.waitThreadRunning() );
        wake.arm( 0.0 );                                          // a no-op once stopped
        ASSERT_TRUE( wake.sleepForSmokeRunner( 40 ) == PlaybackDueWake::RunnerSleep::Slept );   // nothing was sent
        wake.dispatchSentMessages();
        ASSERT_EQ( 0, fired );
        // a wake armed and destroyed before its due time never reaches a destroyed window
        PlaybackDueWake second( nullptr, [&fired]() { ++fired; }, PlaybackDueWakeDeliveryMode::SendNotify );
        second.arm( 5.0 );
    }
    Sleep( 20 );
    MSG msg;
    PeekMessageW( &msg, nullptr, 0, 0, PM_NOREMOVE | PM_QS_SENDMESSAGE );
    ASSERT_EQ( 0, fired );
#endif
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    const QString destructor = functionBody( source, QStringLiteral( "PlaybackDueWake::~PlaybackDueWake()" ),
                                             QStringLiteral( "void PlaybackDueWake::stop()" ) );
    const int stop = destructor.indexOf( QStringLiteral( "stop();" ) );
    const int clearUser = destructor.indexOf( QStringLiteral( "SetWindowLongPtrW( d->window, GWLP_USERDATA, 0 );" ) );
    const int destroyWindow = destructor.indexOf( QStringLiteral( "DestroyWindow( d->window );" ) );
    ASSERT_TRUE( stop > 0 && clearUser > stop && destroyWindow > clearUser );
}

TEST(PlaybackDueWakeSend, TheRunnerSleepIsTheSentWaitWithTheMsleepKeptForEveryOtherDelivery)
{
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString helper = functionBody( source, QStringLiteral( "void MainWindow::sleepGuiThreadForSmokeRunner( unsigned long ms )" ),
                                         QStringLiteral( "void MainWindow::presentPlaybackPreparedFrame(" ) );
    ASSERT_FALSE( helper.isEmpty() );
    const int sent = helper.indexOf( QStringLiteral(
        "m_playbackDueWake->sleepForSmokeRunner( ms, []() { return mlv_stage_timing_now(); }, &MainWindow::chargeSmokeRunnerSleepSegmentSink, this )" ) );
    const int notHandled = helper.indexOf( QStringLiteral( "if( runnerSleep == PlaybackDueWake::RunnerSleep::NotHandled )" ) );
    const int msleep = helper.indexOf( QStringLiteral( "QThread::msleep( ms );" ) );
    // r17 (r16 adversary A1): the msleep's one segment is charged before the woken wake runs (the sent wait charged its own)
    const int accounted = helper.indexOf( QStringLiteral( "chargeSmokeRunnerSleepSegment( sleepStartS, mlv_stage_timing_now() );" ) );
    const int dispatch = helper.indexOf( QStringLiteral( "m_playbackDueWake->dispatchSentMessages();" ) );
    ASSERT_TRUE( sent > 0 && notHandled > sent && msleep > notHandled && accounted > msleep && dispatch > accounted );
    ASSERT_EQ( 1, source.count( QStringLiteral( "\" runner_sleep_woken=%1\"" ) ) );
    const QString wakeSource = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral(
        "MsgWaitForMultipleObjectsEx( 0, nullptr, restMs, QS_SENDMESSAGE, MWMO_INPUTAVAILABLE );" ) ) );
    // r1 adversary pass 1 A5: never INFINITE, whatever the unsigned long
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral(
        "const DWORD waitMs = ms >= static_cast<unsigned long>( INFINITE ) ? INFINITE - 1 : static_cast<DWORD>( ms );" ) ) );
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral( "PeekMessageW( &msg, nullptr, 0, 0, PM_NOREMOVE | PM_QS_SENDMESSAGE );" ) ) );
}

TEST(PlaybackDueWakeSend, DeliveryKindIsTheSelectionAndDeliveryIsWhatTheArmsUsed)
{
    // r1 adversary passes 1 A2 / 2 A1: delivery_kind is the SELECTED delivery, never rewritten by a fallback arm (an inherited
    // selection must always show), and the arm that fell back to a QTimer shows in the Play's delivery= field, which the venue
    // gate requires to be exactly native-msg before send / post counts
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 1, source.count( QStringLiteral(
        ".arg( m_playbackDueWake ? QString::fromLatin1( m_playbackDueWake->deliveryKindName() ) : QStringLiteral( \"none\" ) )" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral(
        "const QString used = QString::fromLatin1( PlaybackDueWake::deliveryName( m_playbackDueWake->lastArmDelivery() ) );" ) ) );
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; }, PlaybackDueWakeDeliveryMode::Post );
    wake.setWaitableTimerFailsForTest( true );
    wake.arm( 1.0 );
    ASSERT_TRUE( wake.lastArmDelivery() == PlaybackDueWake::Delivery::QtTimer );
    ASSERT_EQ( std::string( "qtimer" ), std::string( PlaybackDueWake::deliveryName( wake.lastArmDelivery() ) ) );
    ASSERT_EQ( std::string( "post" ), std::string( wake.deliveryKindName() ) );   // the selection stays visible
    wake.cancel();
#endif
}

TEST(PlaybackDueWakeSend, OnlyAQueuedSentMessageEndsTheRunnerSleepAndAConsumedPostIsNeverStranded)
{
    const QString wakeSource = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    // r1 adversary pass 2 A2: a wait that returns with no sent message queued sleeps on; only a queued one is counted
    const int wait = wakeSource.indexOf( QStringLiteral( "MsgWaitForMultipleObjectsEx( 0, nullptr, restMs, QS_SENDMESSAGE, MWMO_INPUTAVAILABLE );" ) );
    const int queued = wakeSource.indexOf( QStringLiteral( "if( HIWORD( GetQueueStatus( QS_SENDMESSAGE ) ) & QS_SENDMESSAGE )" ), wait );
    const int counted = wakeSource.indexOf( QStringLiteral( "++d->runnerSleepsWokenBySent;" ), queued );
    ASSERT_TRUE( wait > 0 && queued > wait && counted > queued );
    // r1 adversary pass 2 A3: the GUI thread notes every native message it takes, and a published post it took is not stranded
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral( "d->consumedPostFor = postTicks;" ) ) );
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral( "if( forBefore == consumedPostFor ) return;" ) ) );
#ifdef Q_OS_WIN
    // live: an unarmed sent-wake sleep with nothing sent sleeps its whole time and counts nothing
    PlaybackDueWake wake( nullptr, []() {}, PlaybackDueWakeDeliveryMode::SendNotify );
    QElapsedTimer t;
    t.start();
    ASSERT_TRUE( wake.sleepForSmokeRunner( 20 ) == PlaybackDueWake::RunnerSleep::Slept );
    ASSERT_TRUE( static_cast<double>( t.elapsed() ) >= dueWakeSleepFloorMs( 20.0 ) );   // a 20 ms sleep, less one timer tick
    ASSERT_EQ( static_cast<quint64>( 0 ), wake.runnerSleepsWokenBySent() );
#endif
}

TEST(PlaybackDueWakeSend, G1_AForeignSentMessageInTheRunnerSleepIsNotAWakeAndTheSleepRunsItsFullInterval)
{
    // r2 G1 (CoS PR 811): the sleep ended, and counted runner_sleep_woken, for ANY pending sent message. Only this wake's own
    // message (held by its window procedure inside the sleep's dispatch) ends it now; another sent message is dispatched,
    // counted runner_sleep_woken_other, and the sleep goes on to its full interval.
    const QString source = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "\" runner_sleep_woken_other=%1 runner_sleep_wait_failed=%2\"" ) ) );
    ASSERT_EQ( 1, source.count( QStringLiteral( "m_playbackDueWake->runnerSleepsWokenOther()" ) ) );
    const QString wakeSource = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral( "if( !wake->holdWindowProcWake( &msg ) ) wake->handleNativeMessage( &msg );" ) ) );
    ASSERT_EQ( 1, wakeSource.count( QStringLiteral( "++d->runnerSleepsWokenOther;" ) ) );
#ifdef Q_OS_WIN
    int fired = 0;
    PlaybackDueWake wake( nullptr, [&fired]() { ++fired; }, PlaybackDueWakeDeliveryMode::SendNotify );
    const HWND window = reinterpret_cast<HWND>( wake.nativeWindow() );
    ASSERT_TRUE( window != nullptr );
    // a foreign sent message (not kNativeWakeMessage) to the GUI-thread window, 15 ms into a 150 ms runner sleep
    // r7 (sol review r6, blocker 3): the full-interval floor is read on the sleep's own entry and exit reads, and judged only on a
    // sample whose foreign dispatch ended early enough that a sleep cut short at it reads below the floor (dueWakeObserveSleep);
    // otherwise the observation is repeated, and a card with no judged sample fails
    DueWakeSleepObservation foreignObservation { 0.0, false };
    for( int attempt = 0; attempt < kSleepObservationAttempts && !foreignObservation.exposed; ++attempt )
    {
        wake.resetStrandedCounts();
        std::atomic<bool> sent { false };
        std::thread sender( [window, &sent]() {
            Sleep( 15 );
            sent = SendNotifyMessageW( window, WM_USER + 0x51, 0, 0 ) != 0;
        } );
        const ThreadJoinGuard senderGuard { sender };
        dueWakeRecordedClockReset();
        const PlaybackDueWake::RunnerSleep slept = wake.sleepForSmokeRunner( 150, &dueWakeRecordedNowS );
        sender.join();
        ASSERT_TRUE( sent.load() );
        ASSERT_TRUE( slept == PlaybackDueWake::RunnerSleep::Slept );      // not WokenBySent
        ASSERT_EQ( static_cast<quint64>( 0 ), wake.runnerSleepsWokenBySent() );
        ASSERT_EQ( static_cast<quint64>( 1 ), wake.runnerSleepsWokenOther() );
        ASSERT_EQ( 0, fired );
        foreignObservation = dueWakeObserveSleep( wake.lastRunnerSleepDispatchSpansS(), 150.0 );
    }
    ASSERT_TRUE( foreignObservation.exposed );
    ASSERT_TRUE( foreignObservation.sleptMs >= dueWakeSleepFloorMs( 150.0 ) );   // the full interval (less one timer tick), never cut short at ~15 ms
    // the wake itself still ends a sleep at once, is held inside it, and fires only when dispatched after it
    QElapsedTimer t;
    wake.arm( 2.0 );
    t.start();
    ASSERT_TRUE( wake.sleepForSmokeRunner( 500 ) == PlaybackDueWake::RunnerSleep::WokenBySent );
    ASSERT_TRUE( t.elapsed() < 250 );
    ASSERT_EQ( 0, fired );
    ASSERT_EQ( static_cast<quint64>( 1 ), wake.runnerSleepsWokenBySent() );
    ASSERT_EQ( static_cast<quint64>( 1 ), wake.runnerSleepsWokenOther() );
    wake.dispatchSentMessages();
    ASSERT_EQ( 1, fired );
    ASSERT_FALSE( wake.armed() );
    wake.resetStrandedCounts();
    ASSERT_EQ( static_cast<quint64>( 0 ), wake.runnerSleepsWokenOther() );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

#ifdef Q_OS_WIN
namespace {
// r3 A3: a GUI-thread window whose handler does real work, so the runner sleep's dispatch of its sent message takes time
struct R3A3Foreign
{
    PlaybackDueWake *wake = nullptr;
    int calls = 0;
    PlaybackGuiSleepTrace::At at { PlaybackGuiSleepTrace::State::Unknown, false, 0.0 };
    double handlerStartMs = -1.0;
};
R3A3Foreign *g_r3a3Foreign = nullptr;
constexpr UINT kR3A3ForeignMessage = WM_USER + 0x52;
LRESULT CALLBACK r3a3ForeignProc( HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam )
{
    if( message == kR3A3ForeignMessage && g_r3a3Foreign )
    {
        ++g_r3a3Foreign->calls;
        g_r3a3Foreign->handlerStartMs = dueWakeQpcNowMs();
        // what the GUI trace says while the handler runs -- the state a wake posted from here is classified by
        g_r3a3Foreign->at = g_r3a3Foreign->wake->guiStateAt( g_r3a3Foreign->handlerStartMs );
        QElapsedTimer busy;
        busy.start();
        volatile quint64 spin = 0;
        while( busy.elapsed() < 30 ) ++spin;                      // >= 20 ms of real handler work
        return 0;
    }
    return DefWindowProcW( hwnd, message, wParam, lParam );
}
}
#endif

TEST(PlaybackDueWakeSend, R3A3_AForeignHandlerInTheRunnerSleepIsGuiWorkNotRunnerSleep)
{
    // r2 adversary pass 7 A3, its input: SendNotify delivery, trace on, a runner sleep; another thread sends a message to a
    // GUI-thread window whose handler does real work. The handler ran while the trace still said RunnerSleep, so its time was
    // charged to the runner sleep and a wake posted in it was classed asleep. Now the dispatch is traced Running and its time
    // is returned (lastRunnerSleepDispatchNs). r17 (r16 adversary A1): the caller's accounts take only the slept segments the
    // sleep hands its sink, so the dispatch is outside every one of them (R17A1_* below); no account subtracts it afterwards.
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_EQ( 0, mainWindow.count( QStringLiteral( "? m_playbackDueWake->lastRunnerSleepDispatchNs() : 0;" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "&MainWindow::chargeSmokeRunnerSleepSegmentSink, this )" ) ) );
    ASSERT_EQ( 1, mainWindow.count( QStringLiteral( "if( m_guiPresentGapStartNs >= 0 ) m_guiPresentGapRunnerSleepNs += segmentNs;" ) ) );
#ifdef Q_OS_WIN
    PlaybackDueWake wake( nullptr, []() {}, PlaybackDueWakeDeliveryMode::SendNotify );
    ASSERT_TRUE( wake.sendsNotify() );
    R3A3Foreign foreign;
    foreign.wake = &wake;
    g_r3a3Foreign = &foreign;
    static const wchar_t *kClass = L"MlvR3A3ForeignHandler";
    WNDCLASSW wc {};
    wc.lpfnWndProc = r3a3ForeignProc;
    wc.hInstance = GetModuleHandleW( nullptr );
    wc.lpszClassName = kClass;
    RegisterClassW( &wc );
    const HWND window = CreateWindowExW( 0, kClass, L"", 0, 0, 0, 0, 0, HWND_MESSAGE, nullptr, wc.hInstance, nullptr );
    ASSERT_TRUE( window != nullptr );
    const HelperWindowGuard windowGuard { window };
    // r7 (sol review r6, blocker 3): the cadence floor is read on the sleep's own entry and exit reads and judged only on a sample
    // whose foreign dispatch ended early enough to expose an early exit; otherwise the observation is repeated (dueWakeObserveSleep)
    DueWakeSleepObservation foreignObservation { 0.0, false };
    PlaybackGuiSleepTrace::At afterHandler { PlaybackGuiSleepTrace::State::Unknown, false, 0.0 };
    PlaybackDueWake::RunnerSleep slept = PlaybackDueWake::RunnerSleep::NotHandled;
    qint64 sleptMs = 0;
    bool sentOk = false;
    for( int attempt = 0; attempt < kSleepObservationAttempts && !foreignObservation.exposed; ++attempt )
    {
        foreign = R3A3Foreign();
        foreign.wake = &wake;
        wake.setGuiTraceActive( true );
        std::atomic<bool> sent { false };
        std::thread sender( [window, &sent]() {
            Sleep( 5 );
            sent = SendNotifyMessageW( window, kR3A3ForeignMessage, 0, 0 ) != 0;
        } );
        const ThreadJoinGuard senderGuard { sender };
        QElapsedTimer t;
        t.start();
        dueWakeRecordedClockReset();
        slept = wake.sleepForSmokeRunner( 150, &dueWakeRecordedNowS );
        sleptMs = t.elapsed();
        sender.join();
        // after the sleep: the trace is back in RunnerSleep only while the runner really slept
        afterHandler = wake.guiStateAt( foreign.handlerStartMs + 60.0 );
        wake.setGuiTraceActive( false );
        sentOk = sent.load();
        foreignObservation = dueWakeObserveSleep( wake.lastRunnerSleepDispatchSpansS(), 150.0 );
    }
    g_r3a3Foreign = nullptr;
    ASSERT_TRUE( sentOk );
    ASSERT_EQ( 1, foreign.calls );
    ASSERT_TRUE( slept == PlaybackDueWake::RunnerSleep::Slept );
    ASSERT_TRUE( foreignObservation.exposed );
    ASSERT_TRUE( foreignObservation.sleptMs >= dueWakeSleepFloorMs( 150.0 ) );   // the cadence is unchanged: the full interval (less one timer tick)
    // the handler ran as GUI work: a wake posted from it is not classed asleep, nor as a runner-sleep post
    ASSERT_TRUE( foreign.at.state == PlaybackGuiSleepTrace::State::Running );
    const PlaybackDueWakePostClass c = playbackDueWakeClassifyPost( foreign.at, foreign.handlerStartMs, foreign.handlerStartMs + 1.0 );
    ASSERT_TRUE( c.kind != PlaybackDueWakePostClass::Kind::Asleep );
    ASSERT_FALSE( c.runnerSleep );
    // ...and the sleep went back to RunnerSleep for the rest of its interval
    ASSERT_TRUE( afterHandler.state == PlaybackGuiSleepTrace::State::RunnerSleep );
    // the handler's time is returned, so the caller's runner-sleep account excludes it
    const qint64 dispatchMs = wake.lastRunnerSleepDispatchNs() / 1000000;
    ASSERT_TRUE( dispatchMs >= 28 );
    ASSERT_TRUE( dispatchMs <= sleptMs );
    ASSERT_TRUE( sleptMs - dispatchMs <= 150 );
    // a sleep that dispatched nothing returns 0
    ASSERT_TRUE( wake.sleepForSmokeRunner( 5 ) == PlaybackDueWake::RunnerSleep::Slept );
    ASSERT_EQ( static_cast<qint64>( 0 ), wake.lastRunnerSleepDispatchNs() );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeSend, R15A1_ADescheduleBeforeTheSleepStartsNeverShiftsTheDispatchSpan)
{
    // r14 adversary A1 on a27f3d88, its input: SendNotify delivery, a 150 ms runner sleep, the GUI thread descheduled 20 ms
    // between the caller's origin stamp and the sleep's own timer start, a foreign handler dispatched inside the sleep. The
    // caller placed the excluded span at origin + offset, 20 ms before the handler really ran.
#ifdef Q_OS_WIN
    PlaybackDueWake wake( nullptr, []() {}, PlaybackDueWakeDeliveryMode::SendNotify );
    ASSERT_TRUE( wake.sendsNotify() );
    R3A3Foreign foreign;
    foreign.wake = &wake;
    g_r3a3Foreign = &foreign;
    static const wchar_t *kClass = L"MlvR15A1ForeignHandler";
    WNDCLASSW wc {};
    wc.lpfnWndProc = r3a3ForeignProc;
    wc.hInstance = GetModuleHandleW( nullptr );
    wc.lpszClassName = kClass;
    RegisterClassW( &wc );
    const HWND window = CreateWindowExW( 0, kClass, L"", 0, 0, 0, 0, 0, HWND_MESSAGE, nullptr, wc.hInstance, nullptr );
    ASSERT_TRUE( window != nullptr );
    const HelperWindowGuard windowGuard { window };
    wake.setGuiTraceActive( true );
    wake.setRunnerSleepStartDelayMsForTest( 20 );
    std::atomic<bool> sent { false };
    std::thread sender( [window, &sent]() {
        Sleep( 50 );
        sent = SendNotifyMessageW( window, kR3A3ForeignMessage, 0, 0 ) != 0;
    } );
    const ThreadJoinGuard senderGuard { sender };
    // r15: the caller hands the sleep its own clock (seconds); the spans come back on it, with no origin of the caller's
    const PlaybackDueWake::RunnerSleep slept = wake.sleepForSmokeRunner( 150, []() { return dueWakeQpcNowMs() / 1000.0; } );
    sender.join();
    wake.setRunnerSleepStartDelayMsForTest( 0 );
    wake.setGuiTraceActive( false );
    g_r3a3Foreign = nullptr;
    ASSERT_TRUE( sent.load() );
    ASSERT_EQ( 1, foreign.calls );
    ASSERT_TRUE( slept == PlaybackDueWake::RunnerSleep::Slept );
    const std::vector<std::pair<double, double>> spans = wake.lastRunnerSleepDispatchSpansS();
    ASSERT_EQ( static_cast<size_t>( 1 ), spans.size() );
    const double spanStartMs = spans[0].first * 1000.0;
    const double spanEndMs = spans[0].second * 1000.0;
    // the excluded span is where the handler ran: it starts at (just before) the handler and covers its >= 30 ms of work
    ASSERT_TRUE( spanStartMs <= foreign.handlerStartMs + 0.5 );
    ASSERT_TRUE( foreign.handlerStartMs - spanStartMs < 5.0 );
    ASSERT_TRUE( spanEndMs >= foreign.handlerStartMs + 29.0 );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

#ifdef Q_OS_WIN
namespace {
// r15 adversary A3: the caller's clock jumps 20 ms once the handler ends (a GUI deschedule between the dispatch's end and the
// span's end read)
std::atomic<int> g_r15A3SkewMs { 0 };
int g_r15A3Calls = 0;
constexpr UINT kR15A3ForeignMessage = WM_USER + 0x53;
LRESULT CALLBACK r15A3ForeignProc( HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam )
{
    if( message == kR15A3ForeignMessage )
    {
        ++g_r15A3Calls;
        QElapsedTimer busy;
        busy.start();
        volatile quint64 spin = 0;
        while( busy.elapsed() < 30 ) ++spin;
        g_r15A3SkewMs = 20;
        return 0;
    }
    return DefWindowProcW( hwnd, message, wParam, lParam );
}
double r15A3CallerNowS()
{
    return ( dueWakeQpcNowMs() + static_cast<double>( g_r15A3SkewMs.load() ) ) / 1000.0;
}
}
#endif

TEST(PlaybackDueWakeSend, R15A3_TheDurationAccountIsTheDispatchSpansLength)
{
    // r15 adversary A3 on d991b0af, its input: the dispatch's duration came from its own timer while its span came from the
    // caller's clock read after it, so a deschedule before that read made the span 20 ms longer than the time the scalar and
    // present-gap accounts took out. The duration account is now the span's own length, so all three exclude one interval.
#ifdef Q_OS_WIN
    PlaybackDueWake wake( nullptr, []() {}, PlaybackDueWakeDeliveryMode::SendNotify );
    ASSERT_TRUE( wake.sendsNotify() );
    static const wchar_t *kClass = L"MlvR15A3ForeignHandler";
    WNDCLASSW wc {};
    wc.lpfnWndProc = r15A3ForeignProc;
    wc.hInstance = GetModuleHandleW( nullptr );
    wc.lpszClassName = kClass;
    RegisterClassW( &wc );
    const HWND window = CreateWindowExW( 0, kClass, L"", 0, 0, 0, 0, 0, HWND_MESSAGE, nullptr, wc.hInstance, nullptr );
    ASSERT_TRUE( window != nullptr );
    const HelperWindowGuard windowGuard { window };
    g_r15A3SkewMs = 0;
    g_r15A3Calls = 0;
    std::atomic<bool> sent { false };
    std::thread sender( [window, &sent]() {
        Sleep( 10 );
        sent = SendNotifyMessageW( window, kR15A3ForeignMessage, 0, 0 ) != 0;
    } );
    const ThreadJoinGuard senderGuard { sender };
    const PlaybackDueWake::RunnerSleep slept = wake.sleepForSmokeRunner( 120, &r15A3CallerNowS );
    sender.join();
    ASSERT_TRUE( sent.load() );
    ASSERT_EQ( 1, g_r15A3Calls );
    ASSERT_TRUE( slept == PlaybackDueWake::RunnerSleep::Slept );
    const std::vector<std::pair<double, double>> spans = wake.lastRunnerSleepDispatchSpansS();
    ASSERT_EQ( static_cast<size_t>( 1 ), spans.size() );
    const double spanNs = ( spans[0].second - spans[0].first ) * 1000000000.0;
    ASSERT_TRUE( spanNs >= 49000000.0 );                               // the handler's 30 ms plus the 20 ms jump
    ASSERT_TRUE( std::fabs( spanNs - static_cast<double>( wake.lastRunnerSleepDispatchNs() ) ) < 1000.0 );
    g_r15A3SkewMs = 0;
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeSend, R2A1_AStaleSentWakeInTheRunnerSleepIsNotHeldCountedOrAllowedToEndTheSleep)
{
    // r2 adversary pass 5 A1, its input verbatim: SendNotify; arm(0.5); the sent message published without pumping; cancel();
    // runner sleep; dispatch. Variant: arm(40) instead of cancel(). The stale wake must not end the sleep, count as a wake,
    // or fire; the replacement (variant) still ends it at its own due time. (The live part runs first, so RED shows the behaviour.)
#ifdef Q_OS_WIN
    int fired = 0;
    double firedPostFirstMs = -1.0;                                   // the wait thread's own stamp of the delivery that fired
    double firedPostReturn = -1.0;                                    // ...and its stamp of when the post returned (the latest the post can have been)
    bool firedStamped = false;                                        // ...and whether that delivery carried them
    bool firedUnsplit = false;                                        // ...or was taken before the return stamp was published
    double firedReceiveMs = -1.0;                                     // the GUI thread's own receive time (QPC ms)
    PlaybackDueWake *self = nullptr;
    PlaybackDueWake wake( nullptr, [&]() { ++fired; firedReceiveMs = dueWakeQpcNowMs(); const double stamp = self->lastDeliveryPostFirstMs(); firedPostReturn = self->lastDeliveryPostMs(); firedStamped = stamp >= 0.0 && firedPostReturn >= 0.0; firedUnsplit = self->lastDeliveryUnsplit(); firedPostFirstMs = stamp; }, PlaybackDueWakeDeliveryMode::SendNotify );
    self = &wake;
    // r7 (sol review r6, blocker 3): the stale wake is dispatched at the sleep's entry; a sleep cut short at that dispatch ends at
    // ~0 ms on its OWN reads, so the floor is read on the sleep's own entry and exit reads (dueWakeObserveSleep), not on this
    // thread's clock after the return, and a sample whose last dispatch ended too late to expose an early exit is observed again
    DueWakeSleepObservation stale { 0.0, false };
    for( int attempt = 0; attempt < kSleepObservationAttempts && !stale.exposed; ++attempt )
    {
        fired = 0;
        wake.resetStrandedCounts();
        wake.arm( 0.5 );
        ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.armPostPublished(); }, 1000 ) );
        wake.cancel();
        dueWakeRecordedClockReset();
        ASSERT_TRUE( wake.sleepForSmokeRunner( 60, &dueWakeRecordedNowS ) == PlaybackDueWake::RunnerSleep::Slept );
        stale = dueWakeObserveSleep( wake.lastRunnerSleepDispatchSpansS(), 60.0 );
        ASSERT_EQ( static_cast<quint64>( 0 ), wake.runnerSleepsWokenBySent() );
        ASSERT_EQ( static_cast<quint64>( 1 ), wake.runnerSleepsWokenOther() );
        wake.dispatchSentMessages();
        ASSERT_EQ( 0, fired );
    }
    ASSERT_TRUE( stale.exposed );                                     // a sleep no sample of which could expose an early exit is not a pass
    ASSERT_TRUE( stale.sleptMs >= dueWakeSleepFloorMs( 60.0 ) );      // the stale wake did not cut it short (60 ms less one timer tick)
    QElapsedTimer t;
    t.start();
    // variant: replaced by arm(40) -- the old generation's wake goes on to sleep, the replacement ends it and fires once
    // r3 (adversary pass 4 A1): the replacement's own due time is asserted on EVERY judged attempt, never skipped. It is measured
    // from before the arm to the wait thread's OWN stamp of the delivery that fired (not to when this thread looks), so a pause of
    // this thread after the wake cannot lengthen it; an attempt whose arm cost reached kReplacementMaxArmCostNs is not judgeable
    // (a pause before the arm could hide a shortened replacement): it is drained and observed again, keyed on that flag, and the
    // test fails if no attempt could be judged
    // r7 (sol review r6, blocker 2): kReplacementProofsNeeded attempts must clear the floor (see kReplacementProofsNeeded)
    int proofs = 0;
    for( int attempt = 0; attempt < kReplacementAttempts && proofs < kReplacementProofsNeeded; ++attempt )
    {
        fired = 0;
        firedPostFirstMs = -1.0;
        firedPostReturn = -1.0;
        firedStamped = false;
        firedUnsplit = false;
        firedReceiveMs = -1.0;
        wake.resetStrandedCounts();
        wake.arm( 0.5 );
        ASSERT_TRUE( waitWithoutPumping( [&wake]() { return wake.armPostPublished(); }, 1000 ) );
        t.restart();
        const double preArmMs = dueWakeQpcNowMs();
        wake.arm( 40.0 );
        const qint64 armCostNs = t.nsecsElapsed();
        if( armCostNs >= kReplacementMaxArmCostNs )
        {
            wake.sleepForSmokeRunner( 500 );
            wake.dispatchSentMessages();
            continue;
        }
        ASSERT_TRUE( wake.sleepForSmokeRunner( 500 ) == PlaybackDueWake::RunnerSleep::WokenBySent );
        ASSERT_EQ( static_cast<quint64>( 1 ), wake.runnerSleepsWokenBySent() );
        ASSERT_EQ( static_cast<quint64>( 1 ), wake.runnerSleepsWokenOther() );
        ASSERT_EQ( 0, fired );
        wake.dispatchSentMessages();
        ASSERT_EQ( 1, fired );
        ASSERT_FALSE( wake.armed() );
        // not at the stale wake (~0.5 ms), at the replacement's due time: early is a failure, late (past
        // kReplacementMaxElapsedNs) or unstamped is observed again (r4, sol review r3, blocker 1)
        // r7: a delivery the GUI took before the wait thread published its return stamp carries no split (PlaybackDueWake.h
        // lastDeliveryPostFirstMs, "unsplit"); it is judged on the GUI thread's receive time (not early) and counts as a proof
        const ReplacementAttempt verdict = classifyReplacementAttempt( armCostNs, replacementStampsOf( firedStamped, firedUnsplit ),
            ( firedPostFirstMs - preArmMs ) * 1000000.0, ( firedPostReturn - preArmMs ) * 1000000.0, ( firedReceiveMs - preArmMs ) * 1000000.0 );
        ASSERT_TRUE( verdict != ReplacementAttempt::TooEarly );
        if( replacementAttemptProves( verdict ) ) ++proofs;
    }
    ASSERT_TRUE( proofs >= kReplacementProofsNeeded );                // never passes by skipping the bound
#endif
    const QString wakeSource = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    const QString hold = functionBody( wakeSource, QStringLiteral( "bool PlaybackDueWake::holdWindowProcWake( const void *message )" ),
                                       QStringLiteral( "quint64 PlaybackDueWake::runnerSleepsWokenBySent() const" ) );
    const int current = hold.indexOf( QStringLiteral( "if( !d->deliveryCurrent( playbackDueWakeWParamGeneration( wParam )" ) );
    const int pushed = hold.indexOf( QStringLiteral( "d->heldWakes.push_back( *m );" ) );
    ASSERT_TRUE( current > 0 && pushed > current );
}

TEST(PlaybackDueWakeSend, G3_AFailedRunnerSleepWaitSleepsTheRestOfTheIntervalAndIsCounted)
{
    // r2 G3 (CoS PR 811): WAIT_FAILED used to break out and return Slept after ~0 ms. It now sleeps the rest of the interval
    // with QThread::msleep and counts runner_sleep_wait_failed. The OS failure cannot be injected, so a test hook forces it.
    const QString wakeSource = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    const int failed = wakeSource.indexOf( QStringLiteral( "if( result == WAIT_FAILED )" ) );
    const int counted = wakeSource.indexOf( QStringLiteral( "++d->runnerSleepWaitFailed;" ), failed );
    const int msleep = wakeSource.indexOf( QStringLiteral( "QThread::msleep( static_cast<unsigned long>( leftMs ) );" ), counted );
    const int timeout = wakeSource.indexOf( QStringLiteral( "if( result != WAIT_OBJECT_0 ) break;   // the timeout" ), msleep );
    ASSERT_TRUE( failed > 0 && counted > failed && msleep > counted && timeout > msleep );
#ifdef Q_OS_WIN
    PlaybackDueWake wake( nullptr, []() {}, PlaybackDueWakeDeliveryMode::SendNotify );
    wake.setRunnerSleepWaitFailsForTest( true );
    QElapsedTimer t;
    t.start();
    ASSERT_TRUE( wake.sleepForSmokeRunner( 60 ) == PlaybackDueWake::RunnerSleep::Slept );
    ASSERT_TRUE( static_cast<double>( t.elapsed() ) >= dueWakeSleepFloorMs( 60.0 ) );   // never ~0 ms (60 ms less one timer tick)
    ASSERT_EQ( static_cast<quint64>( 1 ), wake.runnerSleepWaitFailures() );
    ASSERT_EQ( static_cast<quint64>( 0 ), wake.runnerSleepsWokenBySent() );
    wake.setRunnerSleepWaitFailsForTest( false );
    ASSERT_TRUE( wake.sleepForSmokeRunner( 10 ) == PlaybackDueWake::RunnerSleep::Slept );
    ASSERT_EQ( static_cast<quint64>( 1 ), wake.runnerSleepWaitFailures() );
    wake.resetStrandedCounts();
    ASSERT_EQ( static_cast<quint64>( 0 ), wake.runnerSleepWaitFailures() );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

// ---------------------------------------------------------------------------------------------------------------------------
// r17 (r16 adversary A1 on 522f72c6), its input verbatim: "SendNotify mode, smoke active, gap/tail already open; valid prep
// result ready at 1.001s; sleepGuiThreadForSmokeRunner(10) enters at 1.002s; at 1.006s SendNotifyMessageW(helperHwnd,
// WM_USER+0x53, 0, 0) invokes a GUI-thread handler calling QCoreApplication::processEvents(AllEvents); queued prep result
// presents and opens the next gap/tail at 1.008s; sleep returns at 1.012s." The whole sleep was charged after it returned, so
// the old gap closed in the dispatch without its 4 ms, the prep result's hop wait missed them, and the new gap/tail got all 8.

namespace
{
// The windows a sleep segment can be charged to: [0] the gap/tail open when the sleep began, [1] the one the in-sleep present
// opened. The hop recorder is MainWindow's m_guiHopWait.
struct R17A1Windows
{
    int open = 0;
    double chargedS[2] = { 0.0, 0.0 };
    int segments[2] = { 0, 0 };
    double firstStartS[2] = { 0.0, 0.0 };                              // the endpoints the sink was handed, not only their difference
    double lastEndS[2] = { 0.0, 0.0 };
    // r4 (adversary pass 1 A5): every segment the sink was handed, in order, with the window it was charged to -- so a segment
    // shifted at a SECOND dispatch is seen even when the totals and the outer endpoints still match. Reserved up front: the
    // sink runs inside the sleep and must not allocate (a push past the capacity is dropped, so the count assertion fails)
    // r7 (sol review r6, blocker 4): chargedAtMs is the time (QPC ms, the clock the handlers stamp with) the sink was called, so the
    // test can prove each segment was charged BEFORE the handlers of the dispatch that closes it ran, for every dispatch
    struct Segment { int window; double startS; double endS; double chargedAtMs; };
    std::vector<Segment> segs;
    R17A1Windows() { segs.reserve( 64 ); }
    gui_hop_wait::Recorder hop;
};
void r17A1Charge( void *context, double startS, double endS )
{
    R17A1Windows *w = static_cast<R17A1Windows *>( context );
#ifdef Q_OS_WIN
    const double chargedAtMs = dueWakeQpcNowMs();                      // no allocation: the sink runs inside the sleep
#else
    const double chargedAtMs = 0.0;
#endif
    if( w->segs.size() < w->segs.capacity() ) w->segs.push_back( { w->open, startS, endS, chargedAtMs } );
    w->chargedS[w->open] += endS - startS;
    if( w->segments[w->open] == 0 ) w->firstStartS[w->open] = startS;
    w->lastEndS[w->open] = endS;
    ++w->segments[w->open];
    w->hop.push( gui_hop_wait::Kind::RunnerSleep, startS, endS );
}
}

TEST(PlaybackDueWakeSend, R17A1_EachRunnerSleepSegmentIsChargedToTheWindowItRanInBeforeAnInSleepDispatchClosesIt)
{
    R17A1Windows w;
    PlaybackRunnerSleepSegments segments( 1.002 );                     // sleepGuiThreadForSmokeRunner(10) enters
    double startS = 0.0;
    double endS = 0.0;
    // 1.006: the sent message's dispatch begins -- the segment it ends is charged now, while the old gap/tail is open
    ASSERT_TRUE( segments.dispatchBegins( 1.006, &startS, &endS ) );
    r17A1Charge( &w, startS, endS );
    // inside the dispatch: the queued prep result (ready at 1.001) presents; its hop wait [1.001, 1.006] is split now
    // (presentPlaybackPreparedFrame), then the old gap closes and the next gap/tail opens at 1.008
    const gui_hop_wait::Split prepWait = w.hop.split( 1.001, 1.006 );
    w.open = 1;
    segments.dispatchEnded( 1.008 );
    ASSERT_TRUE( segments.sleepEnds( 1.012, &startS, &endS ) );         // the sleep returns
    r17A1Charge( &w, startS, endS );
    ASSERT_TRUE( std::fabs( w.chargedS[0] - 0.004 ) < 1e-9 );          // the old gap: the 4 ms before the dispatch
    ASSERT_TRUE( std::fabs( w.chargedS[1] - 0.004 ) < 1e-9 );          // the new gap/tail: only the 4 ms after it
    ASSERT_EQ( 1, w.segments[0] );
    ASSERT_EQ( 1, w.segments[1] );
    ASSERT_TRUE( prepWait.valid );
    ASSERT_TRUE( std::fabs( prepWait.kindMs[static_cast<int>( gui_hop_wait::Kind::RunnerSleep )] - 4.0 ) < 1e-6 );
    // the hop recorder holds the whole non-dispatch sleep, 8 ms, and none of the 2 ms dispatch
    const gui_hop_wait::Split whole = w.hop.split( 1.002, 1.012 );
    ASSERT_TRUE( std::fabs( whole.kindMs[static_cast<int>( gui_hop_wait::Kind::RunnerSleep )] - 8.0 ) < 1e-6 );
    // empty segments are never handed out: a dispatch at the sleep's entry, and a sleep that ends with its dispatch
    PlaybackRunnerSleepSegments atEntry( 2.0 );
    ASSERT_FALSE( atEntry.dispatchBegins( 2.0, &startS, &endS ) );
    atEntry.dispatchEnded( 2.003 );
    ASSERT_FALSE( atEntry.sleepEnds( 2.003, &startS, &endS ) );
    // a GPU present-tail window takes the same segments: the one open at each charge (GpuPresentTailWindow::runnerSlept)
    GpuPresentTailWindow oldTail;
    GpuPresentTailWindow newTail;
    oldTail.open( 1000, 0.0, 0.0 );
    oldTail.runnerSlept( 4000000 );
    GpuPresentTailTiming oldTiming;
    oldTail.close( 1008000000, false, oldTiming );
    newTail.open( 1008000000, 0.0, 0.0 );
    newTail.runnerSlept( 4000000 );
    GpuPresentTailTiming newTiming;
    newTail.close( 1012000000, false, newTiming );
    ASSERT_TRUE( std::fabs( oldTiming.runner_sleep_ms - 4.0 ) < 1e-9 );
    ASSERT_TRUE( std::fabs( newTiming.runner_sleep_ms - 4.0 ) < 1e-9 );
}

#ifdef Q_OS_WIN
namespace {
R17A1Windows *g_r17A1Windows = nullptr;
double g_r17A1HandlerStartMs = 0.0;
double g_r17A1HandlerEndMs = 0.0;
bool g_r17A1HandlerRan = false;                                        // set by the handler itself: the observation landed
constexpr UINT kR17A1ForeignMessage = WM_USER + 0x53;
// r4 (sol review r3, blocker 2; adversary pass 1 A1/A4): liveness probes, one every ~10 ms after the first send, each on the
// SENDER's clock. Only a sleep that is STILL WAITING dispatches a probe; a sleep that returned after any dispatch (cut short)
// leaves the later ones queued, however long the GUI thread was paused before its final clock read -- the pause cannot be read as
// the sleep having run on. The handler records which probe (its wParam) ran, nothing else.
constexpr UINT kR17A1ProbeMessage = WM_USER + 0x54;
constexpr int kR17A1MaxProbes = 8;
// when the sender queues each probe, from its own entry read. r7 (sol review r6, blocker 1): a healthy sleep may legitimately end
// up to one timer tick before its 80 ms and a GUI thread paused across that expiry leaves later messages queued, so only a probe
// queued by kR17A1ProbeLatestMs (the wall floor less kSleepExitMarginMs, 60 ms) MUST have been dispatched; the former probes at
// 70 and 74 ms sat inside the tick the sleep may end early in and failed a healthy wait (expiry resumed at 81.2 ms, last probe
// queued, not dispatched). The offsets cover the interval up to it.
constexpr double kR17A1ProbeOffsetsMs[] = { 30.0, 36.0, 42.0, 48.0, 54.0 };
// r7: a sample whose latest must-run probe was queued less than this after the sleep's entry stamp cannot expose a sleep cut short
// after it (a sender paused past the probes leaves only the first one to judge by, and a sleep that returned at the first
// dispatch and was paused before its final read then reads healthy), so it is observed again, never judged
constexpr double kR17A1ProbeCoverMs = 40.0;
unsigned g_r17A1ProbeRanMask = 0;
double g_r17A1ProbeRunMs[kR17A1MaxProbes] = {};                        // when each probe's handler ran (QPC ms); read after the sleep returned
// what the sender did, on its own clock: read by the test only after the sender is joined
struct R17A1Probes
{
    double beforeMs[kR17A1MaxProbes] = {};
    double afterMs[kR17A1MaxProbes] = {};                              // taken after the send returned: the probe is queued by then; infinity when the send was refused
    int sent = 0;
};
LRESULT CALLBACK r17A1ForeignProc( HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam )
{
    if( message == kR17A1ProbeMessage )
    {
        // r7 (sol review r6, hardening): a direct message with an index past the probe table is no probe (and a shift by >= the
        // mask's width is undefined behaviour)
        if( wParam < static_cast<WPARAM>( kR17A1MaxProbes ) )
        {
            g_r17A1ProbeRunMs[static_cast<unsigned>( wParam )] = dueWakeQpcNowMs();
            g_r17A1ProbeRanMask |= 1u << static_cast<unsigned>( wParam );
        }
        return 0;
    }
    if( message == kR17A1ForeignMessage )
    {
        // the handler presents: the gap/tail the sleep began in closes and the next one opens
        g_r17A1HandlerStartMs = dueWakeQpcNowMs();
        Sleep( 2 );
        if( g_r17A1Windows ) g_r17A1Windows->open = 1;
        g_r17A1HandlerEndMs = dueWakeQpcNowMs();
        g_r17A1HandlerRan = true;
        return 0;
    }
    return DefWindowProcW( hwnd, message, wParam, lParam );
}
double r17A1NowS()
{
    return dueWakeQpcNowMs() / 1000.0;
}
// CI-FLAKE-PLAYBACK-DUE-WAKE-R17A1-TIMING-1: the caller clock the sleep is handed, recording every read it makes. Whatever the
// scheduler did, the sleep's segments are differences of these reads, so the test measures the times it then compares against.
std::vector<double> g_r17A1Stamps;
std::atomic<bool> g_r17A1Entered { false };
double r17A1RecordingNowS()
{
    const double s = dueWakeQpcNowMs() / 1000.0;
    // never grows: capacity is reserved up front, and a read past it is dropped so the stamp-count assertion fails instead
    if( g_r17A1Stamps.size() < g_r17A1Stamps.capacity() ) g_r17A1Stamps.push_back( s );
    g_r17A1Entered = true;
    return s;
}
}
#endif

TEST(PlaybackDueWakeSend, R17A1_TheSentWaitHandsEachSegmentToTheSinkBeforeItsDispatchRuns)
{
#ifdef Q_OS_WIN
    PlaybackDueWake wake( nullptr, []() {}, PlaybackDueWakeDeliveryMode::SendNotify );
    ASSERT_TRUE( wake.sendsNotify() );
    static const wchar_t *kClass = L"MlvR17A1ForeignHandler";
    WNDCLASSW wc {};
    wc.lpfnWndProc = r17A1ForeignProc;
    wc.hInstance = GetModuleHandleW( nullptr );
    wc.lpszClassName = kClass;
    RegisterClassW( &wc );
    const HWND window = CreateWindowExW( 0, kClass, L"", 0, 0, 0, 0, 0, HWND_MESSAGE, nullptr, wc.hInstance, nullptr );
    ASSERT_TRUE( window != nullptr );
    const HelperWindowGuard windowGuard { window };
    // CI-FLAKE-PLAYBACK-DUE-WAKE-R17A1-TIMING-1 (hosted run 44998377370: `w.chargedS[1] * 1000.0 >= 30.0` failed, the sender
    // thread woke at ~55 ms of the 80). Every bound below follows from a clock read the test or the sleep made, never from the
    // scheduler running a thread on time. The sender starts its 20 ms only once the sleep has stamped its entry, so a main
    // thread descheduled before the sleep cannot eat the segment before the send. The send itself must land inside the sleep
    // for the property to be observable at all; when the sender is so late that it misses the whole 80 ms (the handler never
    // ran, so there is nothing to measure), the observation is retried. The retry keys on the HANDLER's own stamps, never on
    // the sleep's span record: an attempt whose handler ran is judged in full below, so a product that dropped the span (or a
    // stamp, or a segment) fails here instead of being retried away. A pass can never come from a missed send.
    // r4 (sol review r3, blocker 2; adversary pass 1 A1/A4, pass 2 A1/A2): after the first send the sender puts out liveness
    // probes at fixed offsets (kR17A1ProbeOffsetsMs: every 6 ms from 30 to 54 ms; r7: they end inside the 60 ms a healthy sleep must
    // still be waiting for, see kProbeLatestMs), each stamped on ITS clock once the send
    // returned (queued by then). The caller clock cannot tell a sleep that
    // ran its 80 ms from one that returned after a dispatch and was then paused before its final read (both read ~80 ms); a
    // message queued while the sleep should still be waiting can: the sleep dispatches it, or it was cut short. A probe that was
    // queued by kProbeLatestMs after the sleep's entry stamp (r7: the wall floor less the exit margin, so the sleep is still
    // waiting even when its wait ends one timer tick early) MUST have been
    // dispatched inside the sleep; a probe queued later proves nothing and is not held against it. An attempt with no such
    // probe is observed again, never passed. Two sends taken in one dispatch (a GUI pause) are no failure: the assertions below
    // follow the dispatches the sleep recorded, however many.
    constexpr int kAttempts = 5;
    // r7 (sol review r6, blocker 1): the sleep's wait may expire one timer tick (16 ms) before its 80 ms, so a probe is only held
    // against the sleep when it was queued with that whole tick still to run (the wall floor less kSleepExitMarginMs of slack)
    const double kProbeLatestMs = dueWakeSleepFloorMs( 80.0 ) - kSleepExitMarginMs;
    // a healthy sleep whose GUI thread was paused across its expiry check legitimately leaves queued probes behind; that pause
    // reads as a return far past the requested 80 ms, so such an attempt is observed again rather than judged (the wait's own
    // timer granularity is inside the allowance)
    constexpr double kOverrunMs = 15.0;
    g_r17A1Stamps.reserve( 256 );                                     // the clock callback must never allocate (a throw there would std::terminate the sender's join)
    std::unique_ptr<R17A1Windows> wp;
    std::vector<std::pair<double, double>> spans;
    std::vector<double> stamps;
    double handlerStartMs = 0.0;
    double handlerEndMs = 0.0;
    double enteredMs = 0.0;
    double returnedMs = 0.0;
    unsigned timelyProbes = 0;                                        // bit i: probe i was queued with a full sleep still to run
    unsigned ranProbes = 0;                                           // bit i: probe i's handler ran inside the sleep
    double probeRunMs[kR17A1MaxProbes] = {};                          // when probe i's handler ran (valid where ranProbes has bit i)
    PlaybackDueWake::RunnerSleep slept = PlaybackDueWake::RunnerSleep::NotHandled;
    bool sentOk = false;
    bool landed = false;
    for( int attempt = 0; attempt < kAttempts && !landed; ++attempt )
    {
        wp.reset( new R17A1Windows );
        g_r17A1Windows = wp.get();
        g_r17A1Stamps.clear();
        g_r17A1Entered = false;
        g_r17A1HandlerStartMs = 0.0;
        g_r17A1HandlerEndMs = 0.0;
        g_r17A1HandlerRan = false;
        g_r17A1ProbeRanMask = 0;
        std::fill( g_r17A1ProbeRunMs, g_r17A1ProbeRunMs + kR17A1MaxProbes, 0.0 );
        R17A1Probes probes;
        std::atomic<bool> sent { false };
        std::thread sender( [window, &sent, &probes]() {
            const ULONGLONG giveUp = GetTickCount64() + 5000;
            bool entered = false;
            while( !entered && GetTickCount64() < giveUp )
            {
                entered = g_r17A1Entered.load();
                if( !entered ) Sleep( 0 );
            }
            // r4 (sol review r3, hardening): the observed entry is checked against the cutoff at admission, so a flag that turned
            // true after it (read false at 4,999 ms, true at 5,005 ms) is not admitted either
            if( !entered || GetTickCount64() >= giveUp ) return;       // the sleep never started: no send, so the attempt reads as missed
            const double seenMs = dueWakeQpcNowMs();
            // each send at an offset from the sender's own entry read, waited out on its clock (not a Sleep, whose granularity
            // could leave no probe near the end of the sleep)
            const auto waitUntil = []( double targetMs ) { while( dueWakeQpcNowMs() < targetMs ) Sleep( 0 ); };
            waitUntil( seenMs + 20.0 );
            sent = SendNotifyMessageW( window, kR17A1ForeignMessage, 0, 0 ) != 0;
            for( const double offsetMs : kR17A1ProbeOffsetsMs )
            {
                waitUntil( seenMs + offsetMs );
                const int i = probes.sent;
                probes.beforeMs[i] = dueWakeQpcNowMs();
                // r7 (sol review r6, hardening): a refused send queued nothing, so it is never counted as a probe queued in time
                const BOOL queued = SendNotifyMessageW( window, kR17A1ProbeMessage, static_cast<WPARAM>( i ), 0 );
                probes.afterMs[i] = queued ? dueWakeQpcNowMs() : std::numeric_limits<double>::infinity();   // this thread's clock: a pause of the GUI thread cannot move it
                probes.sent = i + 1;
            }
        } );
        const ThreadJoinGuard senderGuard { sender };               // pass 1 A8: an exception inside the sleep unwinds through a joined thread
        enteredMs = dueWakeQpcNowMs();
        slept = wake.sleepForSmokeRunner( 80, &r17A1RecordingNowS, &r17A1Charge, wp.get() );
        returnedMs = dueWakeQpcNowMs();
        sender.join();
        g_r17A1Windows = nullptr;
        // the handler's own stamps, taken BEFORE the drain below (which runs the handler too, when the send missed the sleep)
        handlerStartMs = g_r17A1HandlerStartMs;
        handlerEndMs = g_r17A1HandlerEndMs;
        const bool handlerRan = g_r17A1HandlerRan;                     // also before the drain, which runs the handler too when a send missed
        ranProbes = g_r17A1ProbeRanMask;
        std::copy( g_r17A1ProbeRunMs, g_r17A1ProbeRunMs + kR17A1MaxProbes, probeRunMs );   // also before the drain
        // a send that missed the sleep is still queued: take it now (its handler sees no windows), so it cannot cut the next try
        MSG drain;
        PeekMessageW( &drain, nullptr, 0, 0, PM_NOREMOVE | PM_QS_SENDMESSAGE );
        sentOk = sent.load();
        spans = wake.lastRunnerSleepDispatchSpansS();
        stamps = g_r17A1Stamps;
        timelyProbes = 0;
        double coverMs = 0.0;                                          // how long after the entry stamp the latest must-run probe was queued
        for( int i = 0; i < probes.sent && !stamps.empty(); ++i )
        {
            const double queuedAfterEntryMs = probes.afterMs[i] - stamps[0] * 1000.0;   // infinity for a refused send: never timely
            if( queuedAfterEntryMs <= kProbeLatestMs )
            {
                timelyProbes |= 1u << static_cast<unsigned>( i );
                coverMs = std::max( coverMs, queuedAfterEntryMs );
            }
        }
        // judged only when the first handler ran in the sleep, the must-run probes reach kR17A1ProbeCoverMs into the sleep (so a
        // sleep cut short at the first dispatch has probes left queued to show it), and the sleep did not overrun its 80 ms by
        // more than the allowance; otherwise the sender's or the GUI thread's timing made this attempt unobservable and it is
        // retried -- never passed (r7, sol review r6, blocker 1: one timely probe no longer makes a sample judgeable)
        landed = handlerRan && !stamps.empty() && timelyProbes != 0u && coverMs >= kR17A1ProbeCoverMs
            && ( stamps.back() - stamps.front() ) * 1000.0 <= 80.0 + kOverrunMs;
    }
    ASSERT_TRUE( sentOk );
    ASSERT_TRUE( landed );                                             // the send fell inside one of the sleeps, with a probe judged
    // every probe queued while the sleep still had time to run was dispatched by it: a sleep that returned after a dispatch left
    // the later ones queued (and a GUI pause before its final read cannot stand in for the 80 ms)
    ASSERT_TRUE( ( timelyProbes & ~ranProbes ) == 0u );
    ASSERT_TRUE( slept == PlaybackDueWake::RunnerSleep::Slept );
    R17A1Windows &w = *wp;
    // the dispatches the sleep recorded: the first send's, then each probe dispatch that was not taken in the same batch
    const size_t dispatches = spans.size();
    ASSERT_TRUE( dispatches >= 1 );
    // the reads the sleep made on the caller clock: its entry, each dispatch's begin and end, and its return
    ASSERT_EQ( 2 + 2 * dispatches, stamps.size() );
    const double entryS = stamps[0];
    const double dispatchStartS = stamps[1];
    const double returnS = stamps[stamps.size() - 1];
    ASSERT_TRUE( entryS * 1000.0 >= enteredMs - 1e-6 );                // the sleep ran between the test's own two reads (1e-6: the /1000 then *1000 round trip)
    ASSERT_TRUE( returnS * 1000.0 <= returnedMs + 1e-6 );
    ASSERT_TRUE( entryS < dispatchStartS );
    for( size_t i = 1; i < stamps.size(); ++i ) ASSERT_TRUE( stamps[i - 1] <= stamps[i] );
    // r4 (adversary pass 1 A5): EVERY segment is exactly [previous dispatch end, next dispatch begin] on the caller clock (the
    // last one ends at the return), in order and in the right window, so a segment shifted at the second dispatch is caught even
    // when the totals and the outer endpoints still match; a late sender moves the dispatches, never the identity. The one in the
    // old window is charged before the first handler closed it, every other one in the new window. r3 (adversary pass 4 A2): the
    // sink was handed those very endpoints, so a sink call shifted in time is caught too.
    // r7 (sol review r6, hardening): an EMPTY segment (a dispatch ending at the read the next begins at, or at the return) is
    // never handed out (PlaybackRunnerSleepSegments::take), so the expectation is built from the reads, not from dispatches + 1
    const size_t kNoSegment = static_cast<size_t>( -1 );
    std::vector<size_t> segmentOfDispatch( dispatches, kNoSegment );   // the segment the sink was handed when dispatch k began
    double chargedOld = 0.0;
    double chargedNew = 0.0;
    size_t handed = 0;
    for( size_t i = 0; i <= dispatches; ++i )
    {
        const double segmentStartS = stamps[2 * i];
        const double segmentEndS = stamps[2 * i + 1];
        if( !( segmentEndS > segmentStartS ) ) continue;               // empty: never handed out
        ASSERT_TRUE( handed < w.segs.size() );
        const R17A1Windows::Segment &seg = w.segs[handed];
        ASSERT_EQ( i == 0 ? 0 : 1, seg.window );
        ASSERT_TRUE( std::fabs( seg.startS - segmentStartS ) < 1e-9 );
        ASSERT_TRUE( std::fabs( seg.endS - segmentEndS ) < 1e-9 );
        if( i < dispatches ) segmentOfDispatch[i] = handed;
        if( i == 0 ) chargedOld += segmentEndS - segmentStartS;
        else chargedNew += segmentEndS - segmentStartS;
        ++handed;
    }
    ASSERT_EQ( handed, w.segs.size() );
    ASSERT_TRUE( std::fabs( w.chargedS[0] - chargedOld ) < 1e-9 );
    ASSERT_TRUE( std::fabs( w.chargedS[1] - chargedNew ) < 1e-9 );
    // r7 (sol review r6, blocker 4): every segment was charged BEFORE the handlers of the dispatch that closes it ran -- for EVERY
    // dispatch, not the first alone. A probe handler leaves the windows as they are, so the windows prove the order for the first
    // dispatch only; a charge moved to after a later dispatch kept every value observed. The proof is the clock: a handler's own
    // QPC read (taken before the drain) lies inside exactly one recorded dispatch span, and the segment the sink was handed when
    // that dispatch began must have been handed at or before that read. A charge after its dispatch is later than every handler in it.
    const auto assertChargedBeforeHandler = [&]( double handlerMs ) {
        size_t k = 0;
        for( ; k < dispatches; ++k )
            if( spans[k].first * 1000.0 <= handlerMs + 0.001 && handlerMs <= spans[k].second * 1000.0 + 0.001 ) break;   // 1 us: the /1000 round trip
        ASSERT_TRUE( k < dispatches );                                 // the handler ran inside a dispatch the sleep recorded
        if( segmentOfDispatch[k] == kNoSegment ) return;               // nothing was slept before this dispatch: nothing to charge
        ASSERT_TRUE( w.segs[segmentOfDispatch[k]].chargedAtMs <= handlerMs );
    };
    assertChargedBeforeHandler( handlerStartMs );
    for( int i = 0; i < kR17A1MaxProbes; ++i )
        if( ( ranProbes & ( 1u << static_cast<unsigned>( i ) ) ) != 0u ) assertChargedBeforeHandler( probeRunMs[i] );
    ASSERT_TRUE( w.chargedS[0] * 1000.0 <= handlerStartMs - enteredMs + 0.5 );
    ASSERT_TRUE( w.chargedS[1] * 1000.0 <= returnedMs - handlerEndMs + 0.5 );
    ASSERT_TRUE( w.chargedS[0] * 1000.0 >= 10.0 );                     // the sender waits 20 ms after the entry stamp: only later
    // the sleep is never cut short at a dispatch: it runs the requested 80 ms (late only lengthens it)
    // (r5: the floor is the request less one timer tick; a 20x run read 76.1-79.2 ms here, so the former 79.0 failed 9 of 20)
    ASSERT_TRUE( ( returnS - entryS ) * 1000.0 >= dueWakeSleepFloorMs( 80.0 ) );
    // the segments and the dispatches tile the sleep: the dispatch account is exactly what the segments leave out (1e-8: the
    // sum is taken in another order than the reads, which costs a few ulps of a seconds-since-boot value)
    double dispatchSpanS = 0.0;
    for( size_t i = 0; i < dispatches; ++i )
    {
        ASSERT_TRUE( std::fabs( spans[i].first - stamps[2 * i + 1] ) < 1e-9 );
        ASSERT_TRUE( std::fabs( spans[i].second - stamps[2 * i + 2] ) < 1e-9 );
        dispatchSpanS += spans[i].second - spans[i].first;
    }
    ASSERT_TRUE( std::fabs( w.chargedS[0] + w.chargedS[1] + dispatchSpanS - ( returnS - entryS ) ) < 1e-8 );
    // the first dispatch span sits inside the handler's own reads' bracket: it began before the handler and ended after it
    ASSERT_TRUE( spans[0].first * 1000.0 <= handlerStartMs + 0.5 );
    ASSERT_TRUE( spans[0].second * 1000.0 >= handlerEndMs - 0.5 );
    // a sleep no dispatch cuts is one segment, charged to the window open at its end; NotHandled hands out none
    R17A1Windows plain;
    g_r17A1Stamps.clear();
    ASSERT_TRUE( wake.sleepForSmokeRunner( 15, &r17A1RecordingNowS, &r17A1Charge, &plain ) == PlaybackDueWake::RunnerSleep::Slept );
    ASSERT_EQ( 1, plain.segments[0] );
    ASSERT_EQ( static_cast<size_t>( 2 ), g_r17A1Stamps.size() );
    ASSERT_TRUE( std::fabs( plain.chargedS[0] - ( g_r17A1Stamps[1] - g_r17A1Stamps[0] ) ) < 1e-9 );   // exactly entry to return
    ASSERT_TRUE( std::fabs( plain.firstStartS[0] - g_r17A1Stamps[0] ) < 1e-9 );                        // ...with the clock's own endpoints
    ASSERT_TRUE( std::fabs( plain.lastEndS[0] - g_r17A1Stamps[1] ) < 1e-9 );
    ASSERT_TRUE( plain.chargedS[0] * 1000.0 >= dueWakeSleepFloorMs( 15.0 ) );   // the requested 15 ms less one timer tick: never ~0 ms
    PlaybackDueWake postWake( nullptr, []() {}, PlaybackDueWakeDeliveryMode::Post );
    R17A1Windows none;
    ASSERT_TRUE( postWake.sleepForSmokeRunner( 1, &r17A1NowS, &r17A1Charge, &none ) == PlaybackDueWake::RunnerSleep::NotHandled );
    ASSERT_EQ( 0, none.segments[0] );
#else
    SKIP_TEST( "native-message delivery is Windows-only" );
#endif
}

TEST(PlaybackDueWakeSend, R17A1_TheRunnerSleepChargesEverySegmentThroughOneSinkAndNothingAfterTheSleep)
{
    // the wiring: the sent wait hands its sink the segment BEFORE the dispatch, and MainWindow charges every account (present
    // gap, hop recorder, GPU present tail) in that one sink -- never once more after the sleep returned
    const QString wakeSource = dueWakeRepoSource( QStringLiteral( "platform/qt/PlaybackDueWake.cpp" ) );
    const QString sleep = functionBody( wakeSource, QStringLiteral( "PlaybackDueWake::RunnerSleep PlaybackDueWake::sleepForSmokeRunner(" ),
                                        QStringLiteral( "qint64 PlaybackDueWake::lastRunnerSleepDispatchNs() const" ) );
    ASSERT_FALSE( sleep.isEmpty() );
    const int cut = sleep.indexOf( QStringLiteral( "segmentEnded = segments.dispatchBegins( dispatchStartS, &segmentStartS, &segmentEndS );" ) );
    const int charged = sleep.indexOf( QStringLiteral( "chargeSegment( segmentEnded, segmentStartS, segmentEndS );" ), cut );
    const int dispatch = sleep.indexOf( QStringLiteral( "dispatchPendingSentMessages();" ), charged );
    const int resumed = sleep.indexOf( QStringLiteral( "segments.dispatchEnded( dispatchEndS );" ), dispatch );
    const int last = sleep.indexOf( QStringLiteral( "segmentEnded = segments.sleepEnds( spanNowS(), &segmentStartS, &segmentEndS );" ), resumed );
    ASSERT_TRUE( cut > 0 && charged > cut && dispatch > charged && resumed > dispatch && last > resumed );
    // r7 (sol review r6, blocker 4): the order above is the FIRST textual occurrence of each; it holds for every dispatch only when
    // there is exactly one dispatch site, one cut and one final segment, and no charge anywhere between the dispatch and the
    // sleep's last segment (a charge of later segments moved after their dispatch is a second chargeSegment( call there). The live
    // fixture proves the same order on the clock for every dispatch it observes (R17A1_TheSentWait...).
    ASSERT_EQ( 1, sleep.count( QStringLiteral( "segments.dispatchBegins(" ) ) );
    ASSERT_EQ( 1, sleep.count( QStringLiteral( "dispatchPendingSentMessages();" ) ) );
    ASSERT_EQ( 1, sleep.count( QStringLiteral( "segments.sleepEnds(" ) ) );
    ASSERT_EQ( 2, sleep.count( QStringLiteral( "chargeSegment( segmentEnded, segmentStartS, segmentEndS );" ) ) );
    ASSERT_EQ( 2, sleep.count( QStringLiteral( "chargeSegment(" ) ) );                 // no other call shape of it (the lambda is defined as `chargeSegment = [`)
    ASSERT_EQ( 0, sleep.mid( dispatch, last - dispatch ).count( QStringLiteral( "chargeSegment(" ) ) );
    const QString mainWindow = dueWakeRepoSource( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    const QString helper = functionBody( mainWindow, QStringLiteral( "void MainWindow::sleepGuiThreadForSmokeRunner( unsigned long ms )" ),
                                         QStringLiteral( "void MainWindow::waitGuiThreadForSmokeEvents( int maxMs )" ) );
    ASSERT_FALSE( helper.isEmpty() );
    ASSERT_TRUE( helper.contains( QStringLiteral(
        "m_playbackDueWake->sleepForSmokeRunner( ms, []() { return mlv_stage_timing_now(); }, &MainWindow::chargeSmokeRunnerSleepSegmentSink, this )" ) ) );
    ASSERT_EQ( 0, helper.count( QStringLiteral( "lastRunnerSleepDispatchSpansS()" ) ) );
    ASSERT_EQ( 0, helper.count( QStringLiteral( "lastRunnerSleepDispatchNs()" ) ) );
    ASSERT_EQ( 0, helper.count( QStringLiteral( "m_guiPresentGapRunnerSleepNs +=" ) ) );
    ASSERT_EQ( 0, helper.count( QStringLiteral( "m_guiHopWait.push(" ) ) );
    ASSERT_EQ( 0, helper.count( QStringLiteral( "noteSmokeRunnerSleepIfActive(" ) ) );
    const QString segment = functionBody( mainWindow, QStringLiteral( "void MainWindow::chargeSmokeRunnerSleepSegment( double startS, double endS )" ),
                                          QStringLiteral( "void MainWindow::sleepGuiThreadForSmokeRunner( unsigned long ms )" ) );
    ASSERT_FALSE( segment.isEmpty() );
    ASSERT_EQ( 1, segment.count( QStringLiteral( "m_guiHopWait.push( gui_hop_wait::Kind::RunnerSleep, startS, endS );" ) ) );
    ASSERT_EQ( 1, segment.count( QStringLiteral( "if( m_guiPresentGapStartNs >= 0 ) m_guiPresentGapRunnerSleepNs += segmentNs;" ) ) );
    ASSERT_EQ( 1, segment.count( QStringLiteral( "GpuDisplayWindow::noteSmokeRunnerSleepIfActive( segmentNs );" ) ) );
}