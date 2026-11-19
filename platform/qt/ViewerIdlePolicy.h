/*!
 * \file ViewerIdlePolicy.h
 * \brief PLAYER-CURSOR-AUTOHIDE-1: one idle clock for the fullscreen viewer. In fullscreen the mouse
 *        cursor hides after kIdleHideMs() with no mouse activity and comes back at once on any real
 *        movement, press or wheel. Pure (QtCore only) so console_tests pins it; the cursor itself is
 *        set by ViewerCursorSurfaces.h.
 *
 * This is meant to be shared: PLAYER-FULLSCREEN-CONTROL-BAR-1 reuses viewer_idle::Controller for the
 * control bar's own show/hide instead of adding a second timer.
 *
 * The Controller holds plain values only. Time is milliseconds from a monotonic clock the caller
 * supplies, so a test drives it with a fake clock. The caller owns one single-shot timer: it starts
 * it on entering fullscreen, and onTimeout() says whether to hide, show or just wait, and for how long
 * to restart the timer.
 *
 * Mouse events are not the only witness of movement. A wait that excludes user input (a nested
 * processEvents / QEventLoop with ExcludeUserInputEvents, e.g. SyncDownloadWaiter) still runs timers but
 * delivers no mouse event, so each timer tick also compares the OS pointer position (PointerWatch):
 * movement keeps the cursor shown, and while hidden the tick keeps watching so it comes back within one
 * idle period even inside such a wait. The caller passes kPointerPollMs() as the poll cap, so the tick
 * looks at least every poll period, shown or hidden (r3: a move away and back within one idle period
 * is then seen on every platform unless the pointer is away for less than one poll period).
 */

#ifndef VIEWERIDLEPOLICY_H
#define VIEWERIDLEPOLICY_H

#include <QEvent>
#include <QPoint>
#include <QPointF>
#include <QtGlobal>

namespace viewer_idle
{

/*! \brief Idle time with no mouse activity before the fullscreen cursor hides. */
inline constexpr int kIdleHideMs() { return 2000; }

/*! \brief The longest gap between two looks at the OS pointer while fullscreen (r3, adversary r1 pass 4 A1).
 *         Off Windows the sampled position is the only witness for movement an input-excluded wait does not
 *         deliver, so it is sampled every poll period rather than once per idle period: a move away and back
 *         is missed only if the pointer is away for less than one poll period. It does not change when the
 *         cursor hides (the activity timestamp decides that). */
inline constexpr int kPointerPollMs() { return 50; }

/*! \brief What the window looks like at the moment the hide timer fires. */
struct Inputs
{
    bool fullscreen = false;        // actionFullscreen checked AND the window really is fullscreen
    bool windowActive = false;      // the main window is the active window
    bool toolArmed = false;         // WB picker, bad-pixel edit or gradient paint armed
    bool popupOrModalOpen = false;  // a popup (menu) or a modal dialog is open
    bool pointerMoved = false;      // the OS pointer moved since the last look (PointerWatch); not a hide gate
};

/*! \brief The cursor may hide only in real fullscreen, while active, with no tool and no popup/modal. */
inline bool mayHide( const Inputs &i )
{
    return i.fullscreen && i.windowActive && !i.toolArmed && !i.popupOrModalOpen;
}

/*! \brief Whether an event of this type is real mouse activity. A move to the same global position
 *         (Windows and Qt synthesize those) is not activity; press, release, double-click and wheel are.
 *         Positions are compared unrounded: at a fractional device-pixel ratio one physical pixel is less
 *         than one logical pixel, and it is still real movement. */
inline bool isActivity( QEvent::Type type, const QPointF &globalNow, const QPointF &globalLast, bool haveLast )
{
    switch( type )
    {
    case QEvent::MouseMove:
        return !haveLast || globalNow != globalLast;
    case QEvent::MouseButtonPress:
    case QEvent::MouseButtonRelease:
    case QEvent::MouseButtonDblClick:
    case QEvent::Wheel:
        return true;
    default:
        return false;
    }
}

/*! \brief Events that show the cursor at once whatever the mouse did: a modal dialog blocking the
 *         window or a deactivation (no mouse event reaches a blocked window), a context menu, and a
 *         window state change (real fullscreen can end without actionFullscreen toggling). */
inline bool forcesRestore( QEvent::Type type )
{
    return type == QEvent::WindowBlocked
        || type == QEvent::WindowDeactivate
        || type == QEvent::ContextMenu
        || type == QEvent::WindowStateChange;
}

/*! \brief The OS pointer position seen at the last look. moved() reports a change since then. Integer
 *         positions are enough here: it is the backstop for undelivered input, not the event path. */
class PointerWatch
{
public:
    void seed( const QPoint &pos )
    {
        m_last = pos;
        m_have = true;
    }

    /*! \brief True iff pos differs from the last look (false on the first look). Remembers pos. */
    bool moved( const QPoint &pos )
    {
        const bool changed = m_have && pos != m_last;
        seed( pos );
        return changed;
    }

private:
    QPoint m_last;
    bool m_have = false;
};

/*! \brief The kind of a mouse message taken from the thread's input queue. */
enum class RetrievedKind { Move, Button, Wheel };

/*! \brief Mouse input seen at the moment the thread takes it from its input queue, before any Qt event
 *         exists for it (on Windows a thread-local WH_MOUSE hook, ViewerMouseRetrievalHook.h).
 *
 * An input-excluded wait takes WM_MOUSEMOVE off the Win32 queue into Qt's own deferred queue, so a move
 * away and back between two ticks reaches neither the sampled position (PointerWatch) nor the Win32 queue
 * status, and no Qt event is delivered until the wait returns. The retrieval is the one point Qt cannot
 * defer, so it is the witness that survives (PLAYER-CURSOR-AUTOHIDE-1 r2 A1).
 *
 * A move counts only when its position differs from the last retrieved move. That is how Qt itself reads
 * a move: it discards a button-less WM_MOUSEMOVE at the position of the last one (Windows synthesizes
 * those when a window appears under the cursor), so this never counts input the event path would not.
 * A button or wheel message counts when it is removed from the queue (a peek that leaves it there is seen
 * again when it is removed). The event path clears the witness when it notes activity itself, so input
 * that was delivered is not counted a second time at the next tick. Positions are the hook's own
 * coordinates (physical pixels); they are compared only with each other. */
class RetrievalWitness
{
public:
    /*! \brief Starts a fresh look: pos is where the pointer is now; nothing is pending. */
    void seed( const QPoint &pos )
    {
        m_last = pos;
        m_have = true;
        m_pending = false;
    }

    /*! \brief Forgets the position too (no pointer position was available). */
    void reset()
    {
        m_have = false;
        m_pending = false;
    }

    /*! \brief One mouse message taken from (removed true) or peeked in (removed false) the queue. */
    void note( RetrievedKind kind, const QPoint &pos, bool removed )
    {
        if( kind == RetrievedKind::Move )
        {
            if( !m_have || pos != m_last ) m_pending = true;
            m_last = pos;
            m_have = true;
            return;
        }
        if( removed ) m_pending = true;
    }

    /*! \brief The event path saw the input itself: nothing is pending (the last position is kept). */
    void clear() { m_pending = false; }

    /*! \brief True iff input was seen since the last take/clear/seed. Clears it. */
    bool take()
    {
        const bool was = m_pending;
        m_pending = false;
        return was;
    }

    bool pending() const { return m_pending; }

private:
    QPoint m_last;
    bool m_have = false;
    bool m_pending = false;
};

/*! \brief What the caller does when its single-shot idle timer fires. For Hide, Show and Rearm the caller
 *         also restarts the timer for *rearmMs; None (not fullscreen) leaves it stopped. */
enum class Tick { None, Hide, Show, Rearm };

/*! \brief The idle state machine. No Qt objects; the caller supplies the clock and owns the timer. */
class Controller
{
public:
    /*! \brief Entering fullscreen starts the idle clock; leaving it clears the hidden state. */
    void setFullscreen( bool on, qint64 nowMs )
    {
        if( on )
        {
            m_fullscreen = true;
            m_lastActivity = nowMs;
        }
        else
        {
            m_fullscreen = false;
            m_hidden = false;
        }
    }

    /*! \brief Records activity. Returns true iff the cursor was hidden (the caller restores it). */
    bool noteActivity( qint64 nowMs )
    {
        m_lastActivity = nowMs;
        const bool wasHidden = m_hidden;
        m_hidden = false;
        return wasHidden;
    }

    /*! \brief Forces the shown state. Returns true iff the cursor was hidden (the caller restores it). */
    bool forceShown()
    {
        const bool wasHidden = m_hidden;
        m_hidden = false;
        return wasHidden;
    }

    /*! \brief The timer fired. Hide, show (the pointer moved while hidden), or keep waiting; for all three
     *         *rearmMs is the next timer interval. None only when not fullscreen. A positive pollMs caps
     *         every *rearmMs (shown or hidden), so the caller looks at the pointer at least that often;
     *         0 or less is no cap. */
    Tick onTimeout( const Inputs &in, qint64 nowMs, int idleMs, int *rearmMs, int pollMs = 0 )
    {
        const Tick tick = decide( in, nowMs, idleMs, rearmMs );
        if( tick != Tick::None && rearmMs && pollMs > 0 && *rearmMs > pollMs ) *rearmMs = pollMs;
        return tick;
    }

    bool hidden() const { return m_hidden; }
    bool fullscreen() const { return m_fullscreen; }

private:
    Tick decide( const Inputs &in, qint64 nowMs, int idleMs, int *rearmMs )
    {
        if( !m_fullscreen ) return Tick::None;
        if( idleMs < 1 ) idleMs = 1;
        if( rearmMs ) *rearmMs = idleMs;
        if( in.pointerMoved )
        {
            // Movement the event path did not see (input excluded by a wait): it is activity.
            m_lastActivity = nowMs;
            if( m_hidden )
            {
                m_hidden = false;
                return Tick::Show;
            }
            return Tick::Rearm;
        }
        if( !mayHide( in ) )
        {
            // A gate fails (fullscreen really left, inactive, tool, popup/modal): never stay hidden.
            if( m_hidden )
            {
                m_hidden = false;
                return Tick::Show;
            }
            return Tick::Rearm;
        }
        if( m_hidden ) return Tick::Rearm;          // keep watching the pointer while hidden
        const qint64 elapsed = nowMs < m_lastActivity ? 0 : nowMs - m_lastActivity;
        if( elapsed < idleMs )
        {
            const qint64 left = idleMs - elapsed;
            if( rearmMs ) *rearmMs = left < 1 ? 1 : int( left );
            return Tick::Rearm;
        }
        m_hidden = true;
        return Tick::Hide;
    }

    bool m_fullscreen = false;
    bool m_hidden = false;
    qint64 m_lastActivity = 0;
};

} // namespace viewer_idle

#endif // VIEWERIDLEPOLICY_H
