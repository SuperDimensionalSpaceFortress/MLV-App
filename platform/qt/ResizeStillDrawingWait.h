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
//   - kDeadlineMs since entry          -> Timeout (the caller proceeds; this never blocks)
// A busy render thread is never forged clear: it only ever times out.
// The deadline is checked after every pump, so a pump that outlasts it ends the wait with no further pump; a pump
// that never returns is outside any bound here, so the caller's pump must not dispatch user input (a shortcut's modal
// dialog runs a nested loop): MainWindow pumps with QEventLoop::ExcludeUserInputEvents (adversary r1 A1).

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
        if( !result.renderIdle )
        {
            idleRun = false;
        }
        else if( !idleRun )
        {
            idleRun = true;
            idleSinceMs = nowMs;
        }

        if( idleRun
         && result.pumps >= kMinPumpsBeforeStale
         && nowMs - idleSinceMs >= kStaleIdleGraceMs )
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
