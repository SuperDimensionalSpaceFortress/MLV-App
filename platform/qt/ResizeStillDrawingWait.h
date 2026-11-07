#ifndef RESIZESTILLDRAWINGWAIT_H
#define RESIZESTILLDRAWINGWAIT_H

// GUI-SMOKE-FOREGROUND-HANG-NOLA-1: the bounded wait MainWindow::resizeEvent runs before it redraws.
// It replaces `while( m_frameStillDrawing ) qApp->processEvents();`, which had no bound: a stale-true flag
// (nothing left in flight to clear it) pumped the GUI thread forever after a showFullScreen() resize.
//
// Pure decision with injected callbacks, no Qt GUI types and no sleeping beyond what pump() does:
//   - flag already clear              -> NotWaiting (no pump, no clock read past the entry check)
//   - pump; flag clears               -> Cleared
//   - flag still set after >= kMinPumpsBeforeStale pumps, while renderIdle() has held continuously for
//     >= kStaleIdleGraceMs             -> clearStale() once, StaleCleared
//   - kDeadlineMs since entry, measured after a pump's renderIdle() sample (it can block on the renderer mutex), i.e.
//     immediately before the next pump -> Timeout (the caller proceeds)
// The idle grace runs from when a renderIdle() sample RETURNS idle: time blocked inside it is never counted as idle.
// A busy render thread is never forged clear: it only ever times out.
// WHAT IS BOUNDED (adversary r1 A1 and its re-pass): the time this helper spends deciding, i.e. the number of pumps
// after the deadline is zero. The deadline is checked after every pump, so a pump that outlasts it ends the wait at
// once. The duration of ONE pump is the caller's: a synchronous pump cannot be pre-empted, so a handler it dispatches
// that runs its own nested event loop (a modal dialog; Updater::getReleaseList's processEvents() loop, which then
// admits user input such as Ctrl+I's file dialog) suspends the wait until that loop returns, exactly as it suspended
// the unbounded spin this replaces. The wait resumes and ends when that loop returns; it no longer spins forever on a
// stale flag. MainWindow's pump passes QEventLoop::ExcludeUserInputEvents so the pump ITSELF dispatches no user input.

#include <cstdint>
#include <functional>

namespace resize_still_drawing_wait
{

enum class Outcome
{
    NotWaiting,
    Cleared,
    StaleCleared,
    Timeout
};

constexpr std::int64_t kStaleIdleGraceMs = 50;
constexpr std::int64_t kDeadlineMs = 2000;
constexpr int kMinPumpsBeforeStale = 2;

struct Callbacks
{
    std::function<bool()> stillDrawing;
    std::function<bool()> renderIdle;
    std::function<void()> pump;
    std::function<void()> clearStale;
    std::function<std::int64_t()> nowMs; // monotonic
};

struct Result
{
    Outcome outcome = Outcome::NotWaiting;
    std::int64_t waitedMs = 0;
    int pumps = 0;
    bool renderIdle = false; // the last renderIdle() sample (false when it was never sampled)
};

inline const char *outcomeName( Outcome outcome )
{
    switch( outcome )
    {
    case Outcome::NotWaiting: return "not_waiting";
    case Outcome::Cleared: return "cleared";
    case Outcome::StaleCleared: return "stale_cleared";
    case Outcome::Timeout: return "timeout";
    }
    return "unknown";
}

inline Result wait( const Callbacks &callbacks )
{
    Result result;
    if( !callbacks.stillDrawing() ) return result;

    const std::int64_t startMs = callbacks.nowMs();
    bool idleRun = false;
    std::int64_t idleSinceMs = 0;
    for( ;; )
    {
        callbacks.pump();
        ++result.pumps;
        const std::int64_t nowMs = callbacks.nowMs();
        result.waitedMs = nowMs - startMs;
        if( !callbacks.stillDrawing() )
        {
            result.outcome = Outcome::Cleared;
            return result;
        }

        result.renderIdle = callbacks.renderIdle();
        // adversary r1 pass 3 A2: renderIdle() reaches the renderer mutex and can block, so both the stale grace and the
        // deadline are judged on a clock sampled after it returns. Nothing runs between this sample and the next pump.
        const std::int64_t sampledMs = callbacks.nowMs();
        result.waitedMs = sampledMs - startMs;
        if( !result.renderIdle )
        {
            idleRun = false;
        }
        else if( !idleRun )
        {
            idleRun = true;
            idleSinceMs = sampledMs;
        }

        if( idleRun
         && result.pumps >= kMinPumpsBeforeStale
         && sampledMs - idleSinceMs >= kStaleIdleGraceMs )
        {
            callbacks.clearStale();
            result.outcome = Outcome::StaleCleared;
            return result;
        }
        if( result.waitedMs >= kDeadlineMs )
        {
            result.outcome = Outcome::Timeout;
            return result;
        }
    }
}

} // namespace resize_still_drawing_wait

#endif // RESIZESTILLDRAWINGWAIT_H
