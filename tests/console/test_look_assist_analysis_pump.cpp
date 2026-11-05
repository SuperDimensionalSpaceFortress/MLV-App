// LOOK-ASSIST-CLIP-OPEN-GUI-BLOCK-1: the helper that runs Look Assist's heavy calls on a worker while the GUI thread
// pumps. MainWindow cannot be built headless, so the helper is tested directly and the sites a pump turn could reach are
// pinned in MainWindow.cpp. The pixels are tested by tests/pipeline/test_look_assist_analysis_thread_identity.cpp.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/LookAssistAnalysisPump.h"

#include <QCoreApplication>
#include <QEvent>
#include <QFile>
#include <QString>
#include <QTextStream>
#include <QThread>
#include <QTimer>

#include <atomic>
#include <chrono>
#include <cstdio>
#include <thread>
#include <vector>

#ifdef Q_OS_WIN
#include <windows.h>
#endif

using look_assist_pump::AnalysisPump;
using look_assist_pump::AnalysisWorker;

namespace
{

QString mainWindowSource()
{
    const QString path = repo_file_path( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    QFile file( path );
    if( path.isEmpty() || !file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return QString();
    QTextStream in( &file );
    return in.readAll();
}

// The function whose definition starts at the signature, to its closing brace at column 0.
QString functionBody( const QString &source, const QString &signature )
{
    const int at = source.indexOf( signature );
    if( at < 0 ) return QString();
    const int end = source.indexOf( QStringLiteral( "\n}\n" ), at );
    return end > at ? source.mid( at, end - at ) : QString();
}

void sleepMs( int ms )
{
    std::this_thread::sleep_for( std::chrono::milliseconds( ms ) );
}

} // namespace

TEST(LookAssistAnalysisPump, WithNoSessionACallRunsOnTheCallerAndNothingPumps)
{
    AnalysisPump pump;
    std::thread::id ranOn;
    pump.call( [&]() { ranOn = std::this_thread::get_id(); } );
    ASSERT_TRUE( ranOn == std::this_thread::get_id() );
    ASSERT_TRUE( !pump.sessionOpen() );
    ASSERT_TRUE( pump.pumpTicks() == 0ULL );
}

TEST(LookAssistAnalysisPump, EveryCallOfASessionRunsOnOneOtherThreadInOrder)
{
    AnalysisWorker worker;
    AnalysisPump pump;
    pump.beginSession( &worker );
    std::vector<std::thread::id> threads;
    std::vector<int> order;
    for( int i = 0; i < 5; ++i )
    {
        pump.call( [&, i]()
        {
            threads.push_back( std::this_thread::get_id() );
            order.push_back( i );
        } );
        // The GUI side runs between the calls, and the worker is idle: the call has returned.
        ASSERT_EQ( i + 1, static_cast<int>( order.size() ) );
    }
    ASSERT_TRUE( pump.endSession().isEmpty() );
    ASSERT_EQ( 5, static_cast<int>( threads.size() ) );
    for( int i = 0; i < 5; ++i )
    {
        ASSERT_TRUE( threads[i] != std::this_thread::get_id() );
        ASSERT_TRUE( threads[i] == threads[0] );
        ASSERT_EQ( i, order[i] );
    }
}

TEST(LookAssistAnalysisPump, TheWorkerSurvivesASessionSoItsThreadIsReused)
{
    AnalysisWorker worker;
    std::thread::id first;
    std::thread::id second;
    for( std::thread::id *ranOn : { &first, &second } )
    {
        AnalysisPump pump;
        pump.beginSession( &worker );
        pump.call( [ranOn]() { *ranOn = std::this_thread::get_id(); } );
        pump.endSession();
    }
    ASSERT_TRUE( first == second );
}

TEST(LookAssistAnalysisPump, TimersFireDuringAHeavyCallAndTheBlockIsFarBelowIt)
{
    AnalysisWorker worker;
    AnalysisPump pump;
    pump.beginSession( &worker );
    int timerTicks = 0;
    bool sawPumping = false;
    QTimer timer;
    QObject::connect( &timer, &QTimer::timeout, [&]()
    {
        ++timerTicks;
        sawPumping = sawPumping || pump.holdsPresentation();
    } );
    timer.start( 10 );
    constexpr int kHeavyMs = 1000;
    pump.call( []() { sleepMs( kHeavyMs ); } );
    timer.stop();
    pump.endSession();
    ASSERT_TRUE( timerTicks >= 5 );
    ASSERT_TRUE( sawPumping );
    ASSERT_TRUE( pump.pumpTicks() >= 5ULL );
    // A GUI thread that waited on the call would show the whole call here.
    ASSERT_TRUE( pump.guiBlockMaxMs() < 0.8 * kHeavyMs );
}

TEST(LookAssistAnalysisPump, ASlotThatRunsLongInATurnIsWhatTheBlockMeasureReports)
{
    AnalysisWorker worker;
    AnalysisPump pump;
    pump.beginSession( &worker );
    QTimer::singleShot( 0, []() { sleepMs( 150 ); } );
    pump.call( []() { sleepMs( 300 ); } );
    pump.endSession();
    ASSERT_TRUE( pump.guiBlockMaxMs() >= 140.0 );
}

TEST(LookAssistAnalysisPump, WorkHeldInATurnComesBackAtLandingInOrder)
{
    AnalysisWorker worker;
    AnalysisPump pump;
    pump.beginSession( &worker );
    std::vector<int> ran;
    QTimer::singleShot( 0, [&]()
    {
        pump.hold( [&]() { ran.push_back( 1 ); } );
        pump.hold( [&]() { ran.push_back( 2 ); } );
    } );
    pump.call( []() { sleepMs( 100 ); } );
    ASSERT_TRUE( ran.empty() );   // held, not run
    ASSERT_TRUE( pump.sessionOpen() );
    const auto held = pump.endSession();
    ASSERT_TRUE( !pump.sessionOpen() );
    ASSERT_EQ( 2, static_cast<int>( held.size() ) );
    for( const auto &work : held ) work();
    ASSERT_EQ( 1, ran[0] );
    ASSERT_EQ( 2, ran[1] );
    ASSERT_TRUE( pump.endSession().isEmpty() );
}

TEST(LookAssistAnalysisPump, TheRenderDrainPresentsAndIsBounded)
{
    AnalysisWorker worker;
    AnalysisPump pump;
    pump.beginSession( &worker );

    // Already idle: nothing pumps and nothing waits.
    int waited = -1;
    ASSERT_TRUE( pump.drainUntil( []() { return true; }, 2000, &waited ) );
    ASSERT_TRUE( pump.pumpTicks() == 0ULL );

    // A frame that finishes during the drain still presents: drawFrameReady is not held.
    bool presentationHeld = true;
    bool idle = false;
    QTimer::singleShot( 20, [&]()
    {
        presentationHeld = pump.holdsPresentation();
        idle = true;
    } );
    ASSERT_TRUE( pump.drainUntil( [&]() { return idle; }, 2000, &waited ) );
    ASSERT_TRUE( !presentationHeld );
    ASSERT_TRUE( pump.drainingRender() == false );

    // A render that never ends: the bound passes and the analysis goes on.
    ASSERT_TRUE( !pump.drainUntil( []() { return false; }, 50, &waited ) );
    ASSERT_TRUE( waited >= 50 );
    pump.endSession();
}

namespace
{

class EventCounter : public QObject
{
public:
    int inputEvents = 0;

protected:
    bool event( QEvent *e ) override
    {
        if( look_assist_pump::isUserInputEvent( e->type() ) ) ++inputEvents;
        return QObject::event( e );
    }
};

} // namespace

// Adversary A1: a slot in a pump turn ran an unrestricted loop (Updater::getReleaseList), a click reached Next Clip, and
// the clip replacement waited forever on the barrier the suspended analysis holds.
TEST(LookAssistAnalysisPump, AnUnrestrictedLoopInATurnCannotDeliverInputUntilLanding)
{
    EventCounter target;

    // Control: with no session the same loop delivers the event, so what the session does below is the session's doing.
    QCoreApplication::postEvent( &target, new QEvent( QEvent::MouseButtonPress ) );
    QCoreApplication::processEvents();
    ASSERT_EQ( 1, target.inputEvents );
    target.inputEvents = 0;

    AnalysisWorker worker;
    AnalysisPump pump;
    pump.beginSession( &worker );
    int deliveredInsideTheLoop = -1;
    QTimer::singleShot( 0, [&]()
    {
        QCoreApplication::postEvent( &target, new QEvent( QEvent::MouseButtonPress ) );
        QCoreApplication::postEvent( &target, new QEvent( QEvent::KeyPress ) );
        QCoreApplication::processEvents();   // what the update check does
        deliveredInsideTheLoop = target.inputEvents;
    } );
    pump.call( []() { sleepMs( 100 ); } );
    const auto held = pump.endSession();
    ASSERT_EQ( 0, deliveredInsideTheLoop );
    ASSERT_EQ( 0, target.inputEvents );
    ASSERT_EQ( 2, static_cast<int>( held.size() ) );

    // Landing: the click takes effect, and later input is untouched.
    for( const auto &work : held ) work();
    QCoreApplication::processEvents();
    ASSERT_EQ( 2, target.inputEvents );
    QCoreApplication::postEvent( &target, new QEvent( QEvent::MouseButtonPress ) );
    QCoreApplication::processEvents();
    ASSERT_EQ( 3, target.inputEvents );
}

// Adversary A1 on 9a8868b2: a press just before the analysis armed a spin box's auto-repeat timer, the release waited behind
// the pump, and the timer kept stepping the clip's settings under the worker. A widget's timer waits; a focus change, which
// commits typed text, waits. QTimer and single-shot timers are not widgets, or the pump itself could not turn.
TEST(LookAssistAnalysisPump, AWidgetTimerAndAFocusChangeWaitAndOtherTimersRun)
{
    using look_assist_pump::heldDuringSession;
    ASSERT_TRUE( heldDuringSession( QEvent::Timer, true ) );
    ASSERT_TRUE( !heldDuringSession( QEvent::Timer, false ) );
    ASSERT_TRUE( heldDuringSession( QEvent::FocusOut, true ) );
    ASSERT_TRUE( heldDuringSession( QEvent::FocusIn, true ) );
    ASSERT_TRUE( heldDuringSession( QEvent::MouseButtonRelease, true ) );
    ASSERT_TRUE( heldDuringSession( QEvent::KeyPress, false ) );
    ASSERT_TRUE( !heldDuringSession( QEvent::Paint, true ) );
    ASSERT_TRUE( !heldDuringSession( QEvent::MetaCall, true ) );
    ASSERT_TRUE( !heldDuringSession( QEvent::Close, true ) );   // MainWindow::closeEvent holds it

    // The filter asks about the real target. Console tests have no QWidget, so this is pinned.
    const QString path = repo_file_path( QStringLiteral( "platform/qt/LookAssistAnalysisPump.h" ) );
    QFile file( path );
    ASSERT_TRUE( !path.isEmpty() && file.open( QIODevice::ReadOnly | QIODevice::Text ) );
    QTextStream in( &file );
    ASSERT_TRUE( in.readAll().contains( QStringLiteral( "heldDuringSession( event->type(), watched->isWidgetType() )" ) ) );

    // A plain object's timer still runs during a session.
    AnalysisWorker worker;
    AnalysisPump pump;
    pump.beginSession( &worker );
    int fired = 0;
    QTimer timer;
    QObject::connect( &timer, &QTimer::timeout, [&fired]() { ++fired; } );
    timer.start( 5 );
    pump.call( []() { sleepMs( 100 ); } );
    timer.stop();
    pump.endSession();
    ASSERT_TRUE( fired > 0 );
}

// Adversary A2 on ff4e96ed: the request survives the analysis. The filter does not touch it: MainWindow::event holds it.
TEST(LookAssistAnalysisPump, ADialogThatIsUpKeepsItsInputAndOtherEventsPass)
{
    EventCounter target;
    AnalysisWorker worker;
    AnalysisPump pump;
    bool dialogUp = true;
    pump.beginSession( &worker, [&dialogUp]() { return dialogUp; } );
    QCoreApplication::postEvent( &target, new QEvent( QEvent::MouseButtonPress ) );
    QCoreApplication::processEvents();
    ASSERT_EQ( 1, target.inputEvents );
    dialogUp = false;
    bool userEventDelivered = false;
    QTimer::singleShot( 0, [&userEventDelivered]() { userEventDelivered = true; } );
    QCoreApplication::processEvents();
    ASSERT_TRUE( userEventDelivered );
    ASSERT_TRUE( pump.endSession().isEmpty() );
}

#ifdef Q_OS_WIN
namespace
{

std::atomic<int> g_closeMessages{ 0 };

LRESULT CALLBACK closeCountingProc( HWND hwnd, UINT message, WPARAM wParam, LPARAM lParam )
{
    if( message == WM_CLOSE )
    {
        g_closeMessages.fetch_add( 1 );
        return 0;
    }
    return DefWindowProcW( hwnd, message, wParam, lParam );
}

HWND createHiddenWindow()
{
    static const wchar_t kClassName[] = L"LookAssistAnalysisPumpCloseProbe";
    WNDCLASSW wc = {};
    wc.lpfnWndProc = closeCountingProc;
    wc.hInstance = GetModuleHandleW( nullptr );
    wc.lpszClassName = kClassName;
    RegisterClassW( &wc );   // a second registration fails harmlessly
    return CreateWindowExW( 0, kClassName, L"", WS_OVERLAPPED, 0, 0, 10, 10, nullptr, nullptr, wc.hInstance, nullptr );
}

} // namespace

// The close the title bar X, Alt+F4 and a posted WM_CLOSE deliver waits for the landing, which is the promise
// ExcludeUserInputEvents makes: Qt's dispatcher counts a posted WM_CLOSE as user input.
TEST(LookAssistAnalysisPump, APostedCloseIsDeferredByThePumpAndArrivesAfterLanding)
{
    HWND window = createHiddenWindow();
    ASSERT_TRUE( window != nullptr );
    g_closeMessages = 0;
    AnalysisWorker worker;
    AnalysisPump pump;
    pump.beginSession( &worker );
    ASSERT_TRUE( PostMessageW( window, WM_CLOSE, 0, 0 ) != 0 );
    pump.call( []() { sleepMs( 200 ); } );
    pump.endSession();
    ASSERT_TRUE( pump.pumpTicks() >= 5ULL );
    std::printf( "[close-probe] posted WM_CLOSE during the pump: delivered %d time(s)\n", g_closeMessages.load() );
    ASSERT_EQ( 0, g_closeMessages.load() );

    QCoreApplication::processEvents();
    std::printf( "[close-probe] posted WM_CLOSE after landing: delivered %d time(s)\n", g_closeMessages.load() );
    ASSERT_EQ( 1, g_closeMessages.load() );
    DestroyWindow( window );
}

// A SENT close is not deferred: Windows delivers it inside the pump's message peek, whatever the flag says. So the flag
// alone cannot be the guard, and MainWindow::closeEvent holds the close itself (pinned below).
TEST(LookAssistAnalysisPump, ASentCloseIsNotDeferredByThePumpSoCloseEventMustHoldIt)
{
    HWND window = createHiddenWindow();
    ASSERT_TRUE( window != nullptr );
    g_closeMessages = 0;
    AnalysisWorker worker;
    AnalysisPump pump;
    pump.beginSession( &worker );
    std::thread sender( [window]()
    {
        DWORD_PTR ignored = 0;
        SendMessageTimeoutW( window, WM_CLOSE, 0, 0, SMTO_NORMAL, 5000, &ignored );
    } );
    pump.call( []() { sleepMs( 200 ); } );
    const int deliveredDuringPump = g_closeMessages.load();
    pump.endSession();
    sender.join();
    std::printf( "[close-probe] sent WM_CLOSE during the pump: delivered %d time(s)\n", deliveredDuringPump );
    ASSERT_EQ( 1, deliveredDuringPump );
    DestroyWindow( window );
}
#endif // Q_OS_WIN

// Hazard 1: an analysis in flight reads as already applied at the three checks, with no side effect.
TEST(LookAssistAnalysisPumpWiring, AnAnalysisInFlightReadsAsAlreadyApplied)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE( !source.isEmpty() );
    ASSERT_EQ( 2, static_cast<int>( source.count(
        QStringLiteral( "m_lookAssistApplied.isApplied( ACTIVE_RECEIPT ) || m_lookAssistPump.sessionOpen()" ) ) ) );
    ASSERT_TRUE( source.contains( QStringLiteral( "|| m_lookAssistPump.sessionOpen()\n" ) ) );
}

// The frame-ready callers take the pump route, and the route carries the kill switch, the status message and the drain.
TEST(LookAssistAnalysisPumpWiring, TheFrameReadyCallersPumpAndTheRouteHasItsRollback)
{
    const QString source = mainWindowSource();
    ASSERT_EQ( 2, static_cast<int>( source.count(
        QStringLiteral( "applyLookAssistFromFrameReady( ACTIVE_RECEIPT, analysisFrame );" ) ) ) );
    const QString route = functionBody( source, QStringLiteral( "void MainWindow::applyLookAssistFromFrameReady(" ) );
    ASSERT_TRUE( route.contains( QStringLiteral( "\"MLVAPP_LOOK_ASSIST_ANALYSIS_INLINE\"" ) ) );
    ASSERT_TRUE( route.contains( QStringLiteral( "tr( \"Analyzing Look Assist\" )" ) ) );
    ASSERT_TRUE( route.contains( QStringLiteral( "statusBar()->clearMessage()" ) ) );
    ASSERT_TRUE( route.contains( QStringLiteral( "kRenderDrainBoundMs = 2000" ) ) );
    ASSERT_TRUE( route.contains( QStringLiteral( "isIdle()" ) ) );
    ASSERT_TRUE( route.contains( QStringLiteral( "QApplication::activeModalWidget() != nullptr" ) ) );
    ASSERT_TRUE( route.contains( QStringLiteral( "look_assist.apply.render_wait" ) ) );
    // The worker is never deleted: process exit ends it.
    ASSERT_TRUE( route.contains( QStringLiteral( "static look_assist_pump::AnalysisWorker *const s_analysisWorker = new" ) ) );
}

// Hazards 2 and 4: a site that would render, touch the clip object or wait on the clip barrier holds itself. Each row is
// the function and the guard its first statement carries.
TEST(LookAssistAnalysisPumpWiring, EverySiteAPumpTurnCanReachHoldsItself)
{
    const QString source = mainWindowSource();
    struct Site
    {
        const char *signature;
        const char *guard;
    };
    const Site sites[] = {
        { "void MainWindow::timerFrameEvent(", "if( m_lookAssistPump.sessionOpen() ) return;" },
        { "void MainWindow::timerEvent(", "m_fileLoaded && !m_lookAssistPump.sessionOpen()" },
        { "void MainWindow::resizeEvent(", "m_lookAssistPump.sessionOpen()" },
        { "void MainWindow::updateCheck(", "m_lookAssistPump.pumping()" },
        // Adversary A2: the request comes back at landing, it is not dropped.
        { "bool MainWindow::event(", "QEvent *const heldOpen = event->clone();" },
        { "void MainWindow::dropEvent(", "m_lookAssistPump.pumping()" },
        { "void MainWindow::openMlvSet(", "m_lookAssistPump.pumping()" },
        { "void MainWindow::onPlaybackPrepResultReady(", "m_lookAssistPump.holdsPresentation()" },
        { "void MainWindow::drawFrameReady(", "m_lookAssistPump.holdsPresentation()" },
        { "void MainWindow::requestFrameRefresh(", "m_lookAssistPump.pumping()" },
        { "void MainWindow::scheduleClipReopenRetry(", "m_lookAssistPump.sessionOpen()" },
        { "void MainWindow::exportHandler(", "m_lookAssistPump.pumping()" },
        { "void MainWindow::runPlaybackRoutePrewarm(", "m_lookAssistPump.pumping()" },
        { "void MainWindow::postPlaybackReconWarm(", "m_lookAssistPump.pumping()" },
        { "void MainWindow::onPlaybackReconWarmDone(", "m_lookAssistPump.pumping()" },
    };
    for( const Site &site : sites )
    {
        const QString body = functionBody( source, QString::fromLatin1( site.signature ) );
        std::printf( "[pump-wiring] %s\n", site.signature );
        ASSERT_TRUE( !body.isEmpty() );
        ASSERT_TRUE( body.contains( QString::fromLatin1( site.guard ) ) );
    }
}

// Hazard 5: a close that reaches the window during the analysis waits for the landing and does not wait on the barrier.
TEST(LookAssistAnalysisPumpWiring, CloseEventHoldsTheCloseUntilLanding)
{
    const QString body = functionBody( mainWindowSource(), QStringLiteral( "void MainWindow::closeEvent(" ) );
    const int guard = body.indexOf( QStringLiteral( "m_lookAssistPump.sessionOpen()" ) );
    ASSERT_TRUE( guard >= 0 );
    const int held = body.indexOf( QStringLiteral( "m_lookAssistPump.hold( [this]() { close(); } );" ), guard );
    ASSERT_TRUE( held > guard );
    const int ignored = body.indexOf( QStringLiteral( "event->ignore();" ), guard );
    ASSERT_TRUE( ignored > guard );
    // Nothing of the teardown comes before the guard.
    ASSERT_TRUE( !body.left( guard ).contains( QStringLiteral( "freeActiveMlvObjectAfterLifecycleBarrier" ) ) );
}

// The design: the render inside each wrapped callback runs on the worker, and the dormant async route is not touched.
TEST(LookAssistAnalysisPumpWiring, EachRendererOfTheAnalysisIsWrapped)
{
    const QString source = mainWindowSource();
    ASSERT_TRUE( source.contains( QStringLiteral( "wbRequest.renderBalance = renderOnAnalysisWorker(" ) ) );
    ASSERT_TRUE( source.contains( QStringLiteral( "windowLitRequest.renderBalance = renderOnAnalysisWorker(" ) ) );
    ASSERT_TRUE( source.contains( QStringLiteral( "windowLitRequest.renderDisplayBalance = renderOnAnalysisWorker(" ) ) );
    const QString apply = functionBody( source, QStringLiteral( "void MainWindow::applyLookAssistToReceipt(" ) );
    ASSERT_TRUE( !apply.isEmpty() );
    // Every heavy primitive named in the apply sits inside a pump call: its line follows an opening of one.
    const char *const primitives[] = {
        "get_area_average_downscale_raw_thumnail( m_pMlvObject,",
        "ReceiptApplier::lookAssistDisplayMeter(",
        "findMlvWhiteBalanceAtAnalysisLevels( m_pMlvObject,",
    };
    for( const char *primitive : primitives )
    {
        const int at = apply.indexOf( QString::fromLatin1( primitive ) );
        ASSERT_TRUE( at > 0 );
        const int pumpCall = apply.lastIndexOf( QStringLiteral( "m_lookAssistPump.call(" ), at );
        ASSERT_TRUE( pumpCall >= 0 );
        ASSERT_TRUE( at - pumpCall < 160 );
    }
}
