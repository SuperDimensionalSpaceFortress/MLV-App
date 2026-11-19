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
 * asks for a look every poll period, shown or hidden. Off Windows that is the whole witness: a move away
 * and back that lies entirely between two consecutive samples is seen only when the wait delivers its
 * events (see kPointerPollMs for the exact bound). On Windows the thread mouse hook sees it when the GUI
 * thread takes the move from its queue; if Windows has coalesced the excursion into one move back at the
 * starting pixel, the hook reads the system move history to tell it from a synthesized one (r6, key B1,
 * MoveHistoryMark, with its stated limits). The
 * sample is in logical pixels, so off Windows a persistent move of less than one logical pixel is not seen
 * by it either (see PointerWatch for that bound); any real mouse event still restores through the event path.
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

/*! \brief The requested gap between two looks at the OS pointer while fullscreen (r3, adversary r1 pass 4 A1).
 *         Off Windows the sampled position is the only witness for movement an input-excluded wait does not
 *         deliver, so it is sampled every poll period rather than once per idle period. The exact bound
 *         (r3, pass 5 A1): a move is seen at the first tick that samples the pointer one logical pixel or
 *         more away from the last sample (a smaller move is PointerWatch's r4 bound). A move away and back
 *         that lies entirely between two consecutive samples is NOT seen by the sample; the samples are at
 *         least this far apart and later when the GUI thread is busy (a Qt timer may fire late), so no poll
 *         period makes that a guarantee. Off Windows such an excursion is seen when the wait returns and Qt
 *         delivers its deferred mouse events, if the move it leaves differs from the last one (the event
 *         path). On Windows the thread mouse hook sees it when the move is taken from the queue, and in
 *         fullscreen a move that Windows coalesced back to the starting pixel is checked against the
 *         system move history (MoveHistoryMark, r6 key B1). The poll does not change when the cursor
 *         hides (the activity timestamp decides that). */
inline constexpr int kPointerPollMs() { return 50; }

/*! \brief What the window looks like at the moment the hide timer fires. */
struct Inputs
{
    bool fullscreen = false;        // actionFullscreen checked AND the window really is fullscreen
    bool windowActive = false;      // the main window is the active window
    bool toolArmed = false;         // WB picker, bad-pixel edit or gradient paint armed
    bool popupOrModalOpen = false;  // a popup (menu) or a modal dialog is open
    bool pointerMoved = false;      // the OS pointer moved since the last look (PointerWatch); not a hide gate
    bool witnessMissing = false;    // Windows: the thread mouse hook is not installed (r6 pass 9 A1)
};

/*! \brief The cursor may hide only in real fullscreen, while active, with no tool and no popup/modal, and (on
 *         Windows) only while the thread mouse hook is installed: without it input that an excluded wait defers
 *         has no witness, so a hidden cursor could stay blank for the whole wait (r6 pass 9 A1, fail-safe). */
inline bool mayHide( const Inputs &i )
{
    return i.fullscreen && i.windowActive && !i.toolArmed && !i.popupOrModalOpen && !i.witnessMissing;
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

/*! \brief The OS pointer position seen at the last look, in logical pixels (the caller passes QCursor::pos()).
 *         moved() reports a change since then. It is the backstop for input a wait does not deliver, not the
 *         event path.
 *
 * Sub-logical-pixel bound (r4, adversary r1 pass 6 A1), a non-promise like the between-samples bound in
 * kPointerPollMs: QCursor::pos() is the native position divided by the scale factor and rounded to a QPoint,
 * and Qt has no public sub-logical-pixel cursor position (the native one is QPA-private; platform natives such as
 * xcb or Cocoa are not used). At a device-pixel ratio above 1 (X11 at scale 3, a Retina Mac) a persistent move of
 * less than one logical pixel can round to the same QPoint as the last look, so the sample does NOT see it, at
 * any tick. A displacement of one logical pixel or more on either axis always changes the rounded point and is
 * seen at the next sample. The smaller move is seen when the wait returns and Qt delivers its events (isActivity
 * compares unrounded QPointF positions), and on Windows by the thread mouse hook when the move is taken from the
 * queue (physical pixels). */
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
 * By position, a move counts only when it differs from the last retrieved move. That is how Qt itself reads
 * a move: it discards a button-less WM_MOUSEMOVE at the position of the last one (Windows synthesizes
 * those when a window appears under the cursor). A move at the same position can still be real: Windows
 * coalesces queued moves, so an away-and-back before the thread takes the queue leaves one WM_MOUSEMOVE at
 * the starting pixel (r6, key B1). The hook tells it apart with MoveHistoryMark (marked each tick) and calls
 * noteExcursion(); this class stays the position rule.
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

    /*! \brief True iff a move at pos is at the position of the last retrieved move (not new by position). */
    bool samePosition( const QPoint &pos ) const { return m_have && pos == m_last; }

    /*! \brief Real movement seen another way (r6: the system move history showed an excursion): pending. */
    void noteExcursion() { m_pending = true; }

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

/*! \brief One point of the system mouse-move history, newest first (on Windows GetMouseMovePointsEx in
 *         display points, the hook's physical pixels). time is the system's input time stamp. */
struct HistoryPoint
{
    int x = 0;
    int y = 0;
    quint32 time = 0;
};

inline bool operator==( const HistoryPoint &a, const HistoryPoint &b )
{
    return a.x == b.x && a.y == b.y && a.time == b.time;
}

/*! \brief PLAYER-CURSOR-AUTOHIDE-1 r6 (key B1): did the pointer really leave its pixel since the last look?
 *
 * Windows coalesces queued mouse moves: a move away and back before the GUI thread takes its queue leaves ONE
 * WM_MOUSEMOVE at the starting pixel. By position that message cannot be told from the synthesized moves
 * Windows sends at an unchanged position (a window shown or hidden under the cursor, a cursor change).
 * What tells them apart is the system's own record of where the pointer went: the system keeps the last 64
 * pointer positions with their time stamps (GetMouseMovePointsEx). A synthesized move adds no point at
 * another position; a real excursion does.
 *
 * Grammar: on entering fullscreen, and at every idle tick after it read the witness, the history is marked (a
 * copy of it, newest first), shown or hidden alike (pass 8 A1: a coalesced excursion while shown restarts the
 * idle period, so the cursor never hides right after real movement). While marked, a move retrieved at the
 * position of the last retrieved move reads the history again. The points
 * newer than the mark are the ones in front of the first place where the marked copy lines up with the new
 * list (newest first, every overlapping point equal, x, y and time). The move is real movement iff one of
 * those newer points is at another position than the move. If the marked copy no longer lines up anywhere
 * (more new points than the history holds), any point at another position counts.
 *
 * r7 (key B1, opus H1): the tick's mark is also its look at the queue (QueueLook): ONE history read is judged
 * against the previous mark and then becomes the new mark, and the queue status is read after it. The hook runs
 * only inside this thread's message retrieval, and the tick retrieves nothing between its witness read, that
 * history read and the status read, so an excursion is judged against the previous mark (already taken: by the
 * hook; still queued: by the tick) or is newer than the new one; none falls between them.
 *
 * Non-promises: an unmarked history (no point at the cursor position, or the read failed) never counts, so the
 * r5 position rule stands until the next mark. */
class MoveHistoryMark
{
public:
    static constexpr int kMaxPoints = 64;

    /*! \brief Copies the history (n points, newest first). n <= 0 (no history, read failed) unmarks. */
    void mark( const HistoryPoint *newestFirst, int n )
    {
        if( !newestFirst || n <= 0 )
        {
            m_count = 0;
            return;
        }
        if( n > kMaxPoints ) n = kMaxPoints;
        for( int i = 0; i < n; ++i ) m_points[i] = newestFirst[i];
        m_count = n;
    }

    void unmark() { m_count = 0; }

    bool marked() const { return m_count > 0; }

    /*! \brief True iff a point newer than the mark lies at another position than (x, y). False when unmarked. */
    bool leftSinceMark( const HistoryPoint *newestFirst, int n, int x, int y ) const
    {
        if( !marked() || !newestFirst || n <= 0 ) return false;
        if( n > kMaxPoints ) n = kMaxPoints;
        int newer = -1;
        for( int k = 0; k < n && newer < 0; ++k )
        {
            const int overlap = qMin( n - k, m_count );
            bool lines = true;
            for( int j = 0; j < overlap && lines; ++j ) lines = newestFirst[k + j] == m_points[j];
            if( lines ) newer = k;
        }
        if( newer < 0 ) newer = n;   // the marked copy scrolled out: every point is newer
        for( int i = 0; i < newer; ++i )
        {
            if( newestFirst[i].x != x || newestFirst[i].y != y ) return true;
        }
        return false;
    }

private:
    HistoryPoint m_points[kMaxPoints];
    int m_count = 0;
};

/*! \brief PLAYER-CURSOR-AUTOHIDE-1 r7 (key B1): one idle tick's look at the mouse input still on the GUI thread's
 *         queue, the input the hook has not seen yet (on Windows GetQueueStatus HIWORD, read without removing it).
 *
 * Windows synthesizes a WM_MOUSEMOVE at an unchanged position (a window shown or hidden under the cursor, a
 * cursor change, activation), so "a move is queued" alone is not movement (round 1: same position is not
 * activity). Queued moves are coalesced into one, made at retrieval with the newest position, so the queued
 * message says nothing about where the pointer went in between; the pointer's own record does.
 *
 * Grammar: a queued button or wheel message is activity. A queued move is activity iff the cursor (physical
 * pixels, the hook's coordinates) is not where it was at the last look, or the system move history has a point at
 * another position newer than the last look's mark (a coalesced away-and-back still on the queue). A queued move
 * whose cursor or last look cannot be read counts (unknown never strands a hidden cursor). Nothing queued is not
 * activity. So a synthesized same-position move leaves a hidden cursor hidden and the idle clock untouched. */
struct QueueLook
{
    bool buttonQueued = false;   // a button or wheel message is still queued (QS_MOUSEBUTTON)
    bool moveQueued = false;     // a move is still queued (QS_MOUSEMOVE)
    bool haveCursor = false;     // the cursor position was read at this look
    QPoint cursor;
    bool haveLastLook = false;   // the cursor position was read at the last look (install or the last tick)
    QPoint lastLook;
    bool leftSinceMark = false;  // MoveHistoryMark::leftSinceMark of this look's history against the last mark
};

/*! \brief The QueueLook grammar: true iff the queued mouse input is activity. */
inline bool queuedMouseIsActivity( const QueueLook &q )
{
    if( q.buttonQueued ) return true;
    if( !q.moveQueued ) return false;
    if( !q.haveCursor || !q.haveLastLook ) return true;
    if( q.cursor != q.lastLook ) return true;
    return q.leftSinceMark;
}

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
        m_sawWindowFullscreen = false;
        if( on )
        {
            m_fullscreen = true;
            m_lastActivity = nowMs;
            m_enteredAt = nowMs;
        }
        else
        {
            m_fullscreen = false;
            m_hidden = false;
        }
    }

    /*! \brief r7 (adversary pass 11 A1): the window's real fullscreen state at a tick. True iff the caller must
     *         leave the idle machinery (stop the timer, remove the hook, show the cursor) because fullscreen was left
     *         without the action (the window manager restored the window): the window was seen really fullscreen at
     *         a tick and is not now, or it was never seen fullscreen within graceMs of entering (showFullScreen() is
     *         asynchronous, so the first looks may precede it). False when not fullscreen. */
    bool windowLeftFullscreen( bool reallyFullscreen, qint64 nowMs, qint64 graceMs )
    {
        if( !m_fullscreen ) return false;
        if( reallyFullscreen )
        {
            m_sawWindowFullscreen = true;
            return false;
        }
        if( m_sawWindowFullscreen ) return true;
        return nowMs - m_enteredAt >= graceMs || nowMs < m_enteredAt;
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
    bool m_sawWindowFullscreen = false;
    qint64 m_enteredAt = 0;
    qint64 m_lastActivity = 0;
};

} // namespace viewer_idle

#endif // VIEWERIDLEPOLICY_H
