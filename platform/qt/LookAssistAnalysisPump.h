#ifndef LOOK_ASSIST_ANALYSIS_PUMP_H
#define LOOK_ASSIST_ANALYSIS_PUMP_H

// LOOK-ASSIST-CLIP-OPEN-GUI-BLOCK-1: Look Assist's analysis stays on the GUI thread, in its order, and each heavy call
// runs on one worker thread while the GUI thread pumps. Every state change happens on the GUI thread, between heavy
// calls, while the worker is idle. So the analysis is the sequential one, and the only new thing is what a pump turn
// can run: MainWindow holds those sites until the analysis lands.

#include <QCoreApplication>
#include <QElapsedTimer>
#include <QEvent>
#include <QEventLoop>
#include <QObject>
#include <QPointer>
#include <QString>
#include <QStringLiteral>
#include <QVector>
#include <QtGlobal>

#include <chrono>
#include <condition_variable>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <thread>

#ifdef _OPENMP
#include <omp.h>
#endif

namespace look_assist_pump {

// A held event comes back as a copy, and only Qt 6 can copy any QEvent. A Qt 5 build (still a documented kit) analyses
// inline as before this card, so no session opens there and it never asks for a copy.
#if QT_VERSION >= QT_VERSION_CHECK( 6, 0, 0 )
constexpr bool kWorkerRouteAvailable = true;
inline QEvent *heldCopy( const QEvent *event ) { return event->clone(); }
#else
constexpr bool kWorkerRouteAvailable = false;
inline QEvent *heldCopy( const QEvent * ) { return nullptr; }
#endif

// One look_assist.apply.timing line: the time the apply spent in each segment. A daylight verdict that falls back to
// master's pass re-enters the apply, and that pass adds to the same line, so one analysis writes one line.
struct ApplyTiming
{
    double rawThumbMs = 0.0;
    double sceneMs = 0.0;
    double processedMs = 0.0;
    double meterMs = 0.0;
    double solveMs = 0.0;
    double windowLitMs = 0.0;
    double walkMs = 0.0;
    double totalMs = 0.0;
    double guiBlockMaxMs = 0.0;
    unsigned long long pumpTicks = 0;
    bool worker = false;

    QString logFields() const
    {
        return QStringLiteral( "raw_thumb_ms=%1 scene_ms=%2 processed_ms=%3 meter_ms=%4 solve_ms=%5 window_lit_ms=%6 "
                               "walk_ms=%7 total_ms=%8 gui_block_max_ms=%9 pump_ticks=%10 route=%11" )
            .arg( rawThumbMs, 0, 'f', 1 )
            .arg( sceneMs, 0, 'f', 1 )
            .arg( processedMs, 0, 'f', 1 )
            .arg( meterMs, 0, 'f', 1 )
            .arg( solveMs, 0, 'f', 1 )
            .arg( windowLitMs, 0, 'f', 1 )
            .arg( walkMs, 0, 'f', 1 )
            .arg( totalMs, 0, 'f', 1 )
            .arg( guiBlockMaxMs, 0, 'f', 1 )
            .arg( pumpTicks )
            .arg( worker ? QStringLiteral( "worker" ) : QStringLiteral( "inline" ) );
    }
};

// Adds the scope's elapsed time to one segment of the line.
class SegmentClock
{
public:
    explicit SegmentClock( double *segmentMs ) : m_segmentMs( segmentMs ) { m_clock.start(); }
    ~SegmentClock() { *m_segmentMs += m_clock.nsecsElapsed() / 1000000.0; }
    SegmentClock( const SegmentClock & ) = delete;
    SegmentClock &operator=( const SegmentClock & ) = delete;

private:
    double *m_segmentMs;
    QElapsedTimer m_clock;
};

// One OS thread that runs posted jobs in order and otherwise sleeps. The same thread serves every call, so the libgomp
// team it creates stays alive (a new thread per call is the team churn of libgomp-team-size-churn-sh-stutter-20261011)
// and the per-thread scratch buffers of src/mlv are reused, as they were on the GUI thread. The app keeps one for the
// whole process and never ends it: those buffers have no destructor, so a thread that ends leaks them, and on this
// MinGW a thread_local destructor can run after its storage is freed (commit 9183c7cf6, tls_exit_order_fixture).
class AnalysisWorker
{
public:
    AnalysisWorker() : m_thread( [this]() { threadMain(); } ) {}

    ~AnalysisWorker()
    {
        {
            std::lock_guard<std::mutex> lock( m_mutex );
            m_stop = true;
        }
        m_wake.notify_all();
        m_thread.join();
    }

    AnalysisWorker( const AnalysisWorker & ) = delete;
    AnalysisWorker &operator=( const AnalysisWorker & ) = delete;

    void post( std::function<void()> job )
    {
        {
            std::lock_guard<std::mutex> lock( m_mutex );
            m_jobs.push_back( std::move( job ) );
        }
        m_wake.notify_all();
    }

private:
    void threadMain()
    {
        std::unique_lock<std::mutex> lock( m_mutex );
        for( ;; )
        {
            m_wake.wait( lock, [this]() { return m_stop || !m_jobs.empty(); } );
            if( m_jobs.empty() ) return;   // stop is honoured only once the queue has drained
            std::function<void()> job = std::move( m_jobs.front() );
            m_jobs.pop_front();
            lock.unlock();
            job();
            job = nullptr;
            lock.lock();
        }
    }

    std::mutex m_mutex;
    std::condition_variable m_wake;
    std::deque<std::function<void()>> m_jobs;
    bool m_stop = false;
    std::thread m_thread;
};

// The event types that are the user's hand on the app. Focus counts: a spin box commits typed text to the clip when focus
// leaves it, and the window losing activation is not input to ExcludeUserInputEvents. So does an IME commit or a drop into
// a line edit: either changes its text, and the slot behind that text changes the clip.
inline bool isUserInputEvent( QEvent::Type type )
{
    switch( type )
    {
    case QEvent::InputMethod:
    case QEvent::DragEnter:
    case QEvent::DragMove:
    case QEvent::Drop:
    case QEvent::Gesture:
    case QEvent::NativeGesture:
    case QEvent::FocusIn:
    case QEvent::FocusOut:
    case QEvent::MouseButtonPress:
    case QEvent::MouseButtonRelease:
    case QEvent::MouseButtonDblClick:
    case QEvent::MouseMove:
    case QEvent::NonClientAreaMouseButtonPress:
    case QEvent::NonClientAreaMouseButtonRelease:
    case QEvent::NonClientAreaMouseButtonDblClick:
    case QEvent::Wheel:
    case QEvent::KeyPress:
    case QEvent::KeyRelease:
    case QEvent::Shortcut:
    case QEvent::ShortcutOverride:
    case QEvent::ContextMenu:
    case QEvent::TouchBegin:
    case QEvent::TouchUpdate:
    case QEvent::TouchEnd:
    case QEvent::TouchCancel:
    case QEvent::TabletPress:
    case QEvent::TabletMove:
    case QEvent::TabletRelease:
        return true;
    default:
        return false;
    }
}

// What a session holds. A press from just before the analysis has armed its widget's auto-repeat timer, and that timer
// would keep stepping a spin box or slider, so the clip's live settings, while the release waits behind the pump. So a
// widget's timer waits too. QTimer and single-shot timers are not widgets: MainWindow's guarded sites answer those.
inline bool heldDuringSession( QEvent::Type type, bool targetIsWidget )
{
    return isUserInputEvent( type ) || ( type == QEvent::Timer && targetIsWidget );
}

// A drag's data lives only while its event is delivered, so a copy could not be posted at landing: it is refused instead,
// and the user drops again.
inline bool isRefusedDuringSession( QEvent::Type type )
{
    return type == QEvent::DragEnter || type == QEvent::DragMove || type == QEvent::Drop;
}

// The GUI thread's side of one analysis. With no session open every call runs inline on the caller, which is the route
// the MLVAPP_LOOK_ASSIST_ANALYSIS_INLINE=1 rollback and every other caller of the apply take.
class AnalysisPump
{
public:
    // How long one pump turn waits for the worker before it looks at the event queue again.
    static constexpr int kTurnWaitMs = 4;

    // mayTakeInput: true while the user may act anyway. A modal dialog that a held-off path could not stop has to work,
    // and the window behind it takes no input.
    void beginSession( AnalysisWorker *worker, std::function<bool()> mayTakeInput = nullptr )
    {
        m_worker = worker;
        m_lastTurn.start();
        m_guiBlockMaxMs = 0.0;
        m_ticks = 0;
        m_inputHold.mayTakeInput = std::move( mayTakeInput );
        if( QCoreApplication::instance() ) QCoreApplication::instance()->installEventFilter( &m_inputHold );
    }

    // Returns the work to run once the analysis has landed, in the order it was held.
    QVector<std::function<void()>> endSession()
    {
        noteTurn();
        if( QCoreApplication::instance() ) QCoreApplication::instance()->removeEventFilter( &m_inputHold );
        m_inputHold.mayTakeInput = nullptr;
        m_worker = nullptr;
        QVector<std::function<void()>> held;
        held.swap( m_held );
        return held;
    }

    bool sessionOpen() const { return m_worker != nullptr; }
    bool pumping() const { return m_pumpDepth > 0; }
    // True during the wait for a render already running: that frame must still present, or the render never goes idle.
    bool drainingRender() const { return m_drainingRender; }
    bool holdsPresentation() const { return pumping() && !drainingRender(); }

    double guiBlockMaxMs() const { return m_guiBlockMaxMs; }
    unsigned long long pumpTicks() const { return m_ticks; }

    void hold( std::function<void()> work ) { m_held.push_back( std::move( work ) ); }

    // Work that arrives by itself and must not run inside an analysis, such as a queued result. Held work comes back on a
    // later turn, and another analysis may have opened by then, so it asks again.
    void runOrHold( std::function<void()> work )
    {
        if( !sessionOpen() )
        {
            work();
            return;
        }
        hold( [this, work]() { runOrHold( work ); } );
    }

    // gui_block_max_ms: the longest the event queue went unserved, whatever held it (GUI-side code between heavy calls,
    // or a slot that ran long inside a turn).
    void noteTurn()
    {
        const double gapMs = m_lastTurn.nsecsElapsed() / 1000000.0;
        if( gapMs > m_guiBlockMaxMs ) m_guiBlockMaxMs = gapMs;
        m_lastTurn.start();
    }

    // Runs the call on the worker and pumps until it returns; inline when no session is open.
    void call( const std::function<void()> &heavy )
    {
        if( !m_worker )
        {
            heavy();
            return;
        }
#ifdef _OPENMP
        // omp_set_num_threads and omp_set_dynamic bind to the calling thread, so a new thread does not inherit them.
        // The regions in the heavy call must be the size they were on the caller.
        const int callerMaxThreads = omp_get_max_threads();
        const int callerDynamic = omp_get_dynamic();
#endif
        struct Completion
        {
            std::mutex mutex;
            std::condition_variable done;
            bool finished = false;
        };
        // Shared: the worker may still be leaving the mutex when the caller wakes and returns.
        const auto completion = std::make_shared<Completion>();
        m_worker->post( [&heavy, completion
#ifdef _OPENMP
                         , callerMaxThreads, callerDynamic
#endif
                        ]()
        {
#ifdef _OPENMP
            if( omp_get_dynamic() != callerDynamic ) omp_set_dynamic( callerDynamic );
            if( omp_get_max_threads() != callerMaxThreads ) omp_set_num_threads( callerMaxThreads );
#endif
            heavy();
            std::lock_guard<std::mutex> lock( completion->mutex );
            completion->finished = true;
            completion->done.notify_all();
        } );
        pumpUntil( [&completion]( int waitMs )
        {
            std::unique_lock<std::mutex> lock( completion->mutex );
            return completion->done.wait_for( lock, std::chrono::milliseconds( waitMs ),
                                              [&completion]() { return completion->finished; } );
        } );
    }

    // Pumps until the render is idle or the bound passes. Returns whether it went idle.
    bool drainUntil( const std::function<bool()> &idle, int boundMs, int *waitedMs )
    {
        QElapsedTimer clock;
        clock.start();
        m_drainingRender = true;
        bool wentIdle = idle();
        while( !wentIdle && clock.elapsed() < boundMs )
        {
            pumpTurn();
            std::this_thread::sleep_for( std::chrono::milliseconds( kTurnWaitMs ) );
            wentIdle = idle();
        }
        m_drainingRender = false;
        if( waitedMs ) *waitedMs = static_cast<int>( clock.elapsed() );
        return wentIdle;
    }

private:
    // ExcludeUserInputEvents only binds this pump's own turn. A slot in the turn can run an unrestricted loop of its own
    // (Updater::getReleaseList does), and that loop delivers a click into whatever it reaches: Next Clip would wait on the
    // clip barrier that the suspended analysis holds, forever. So input stops here for the whole session, whichever loop
    // carries it, and each event is posted again at landing, as ExcludeUserInputEvents would have kept it.
    class InputHold : public QObject
    {
    public:
        explicit InputHold( AnalysisPump *pump ) : m_pump( pump ) {}
        std::function<bool()> mayTakeInput;

    protected:
        bool eventFilter( QObject *watched, QEvent *event ) override
        {
            if( !heldDuringSession( event->type(), watched->isWidgetType() ) || ( mayTakeInput && mayTakeInput() ) ) return false;
            // A running timer fires again by itself after landing; only one shot of it is skipped.
            if( event->type() == QEvent::Timer ) return true;
            if( isRefusedDuringSession( event->type() ) )
            {
                event->ignore();
                return true;
            }
            const QPointer<QObject> target( watched );
            QEvent *const held = heldCopy( event );
            if( !held ) return true;
            m_pump->hold( [target, held]()
            {
                if( target ) QCoreApplication::postEvent( target.data(), held );
                else delete held;
            } );
            return true;
        }

    private:
        AnalysisPump *m_pump;
    };

    template <typename WaitForFinish>
    void pumpUntil( WaitForFinish waitForFinish )
    {
        while( !waitForFinish( 0 ) )
        {
            pumpTurn();
            if( waitForFinish( kTurnWaitMs ) ) break;
        }
        noteTurn();
    }

    void pumpTurn()
    {
        noteTurn();
        ++m_ticks;
        ++m_pumpDepth;
        QCoreApplication::processEvents( QEventLoop::ExcludeUserInputEvents );
        --m_pumpDepth;
    }

    AnalysisWorker *m_worker = nullptr;
    QElapsedTimer m_lastTurn;
    double m_guiBlockMaxMs = 0.0;
    unsigned long long m_ticks = 0;
    int m_pumpDepth = 0;
    bool m_drainingRender = false;
    QVector<std::function<void()>> m_held;
    InputHold m_inputHold{ this };
};

} // namespace look_assist_pump

#endif // LOOK_ASSIST_ANALYSIS_PUMP_H
