/*!
 * \file ViewerMouseRetrievalHook.h
 * \brief PLAYER-CURSOR-AUTOHIDE-1 r2 (adversary A1): the Windows witness for mouse input that an
 *        input-excluded wait takes away before any Qt event exists for it.
 *
 * Qt's Windows event dispatcher, in a wait with QEventLoop::ExcludeUserInputEvents, removes each mouse
 * message from the thread's queue with PeekMessage(PM_REMOVE) and appends it to its private
 * queuedUserInputEvents without translating or dispatching it (qeventdispatcher_win.cpp, Qt 6.10.2). No
 * Qt event exists for it until the wait returns, GetQueueStatus no longer reports it, and a move away and
 * back leaves the sampled pointer position unchanged.
 *
 * A thread-local WH_MOUSE hook is called by the system inside that PeekMessage, for every mouse message
 * the thread takes, before Qt defers it. It is installed for the GUI thread only (no DLL, no other
 * thread or process), and only while the viewer is fullscreen. Both viewer surfaces belong to that
 * thread: the main window's QGraphicsView viewport and the GL display window (GpuDisplayWindow, its own
 * HWND), so one hook covers both display paths. The hook only records into a RetrievalWitness
 * (ViewerIdlePolicy.h); the idle tick reads it.
 *
 * r6 (key B1): Windows coalesces queued moves, so an away-and-back before the thread takes its queue
 * arrives as one WM_MOUSEMOVE at the starting pixel, and by position it is the same as a synthesized move.
 * In fullscreen, shown or hidden (install() and then every idle tick call markHistory()), such a same-position
 * move reads the system move history (GetMouseMovePointsEx) and counts as movement iff the pointer was at
 * another position after the mark (MoveHistoryMark). So a coalesced excursion both restores a hidden cursor
 * and restarts the idle period of a shown one (pass 8 A1). The read happens only for a same-position move
 * while marked; the mark is one read per tick.
 *
 * r7 (key B1): input still on the queue at a tick is judged by lookAtQueue() (QueueLook grammar), not by the
 * queue status alone, so a move Windows synthesizes at the cursor's position does not show a hidden cursor.
 *
 * GetLastInputInfo was rejected because a key press counts as input there, and a key press must not
 * bring the cursor back.
 */

#ifndef VIEWERMOUSERETRIEVALHOOK_H
#define VIEWERMOUSERETRIEVALHOOK_H

#include "ViewerIdlePolicy.h"

#include <QPoint>
#include <QtGlobal>

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>

namespace viewer_idle
{
namespace win_hook
{

/*! \brief Maps a mouse message id to its kind. False for a message that is not mouse input. */
inline bool kindOfMessage( UINT message, RetrievedKind *kind )
{
    switch( message )
    {
    case WM_MOUSEMOVE:
    case WM_NCMOUSEMOVE:
        *kind = RetrievedKind::Move;
        return true;
    case WM_MOUSEWHEEL:
    case WM_MOUSEHWHEEL:
        *kind = RetrievedKind::Wheel;
        return true;
    case WM_LBUTTONDOWN: case WM_LBUTTONUP: case WM_LBUTTONDBLCLK:
    case WM_RBUTTONDOWN: case WM_RBUTTONUP: case WM_RBUTTONDBLCLK:
    case WM_MBUTTONDOWN: case WM_MBUTTONUP: case WM_MBUTTONDBLCLK:
    case WM_XBUTTONDOWN: case WM_XBUTTONUP: case WM_XBUTTONDBLCLK:
    case WM_NCLBUTTONDOWN: case WM_NCLBUTTONUP: case WM_NCLBUTTONDBLCLK:
    case WM_NCRBUTTONDOWN: case WM_NCRBUTTONUP: case WM_NCRBUTTONDBLCLK:
    case WM_NCMBUTTONDOWN: case WM_NCMBUTTONUP: case WM_NCMBUTTONDBLCLK:
    case WM_NCXBUTTONDOWN: case WM_NCXBUTTONUP: case WM_NCXBUTTONDBLCLK:
        *kind = RetrievedKind::Button;
        return true;
    default:
        return false;
    }
}

/*! \brief The one witness the hook writes. GUI thread only: the hook runs on the thread that installed it. */
inline RetrievalWitness &witness()
{
    static RetrievalWitness w;
    return w;
}

inline HHOOK &hookHandle()
{
    static HHOOK h = nullptr;
    return h;
}

/*! \brief Reads the system move history from the newest point at `at`, newest first, into out (at most max).
 *         Returns the point count, 0 for none, -1 when the point is not in the history or the read failed.
 *         Coordinates as GetMouseMovePointsEx documents them: the query point is masked to 16 bits and a
 *         returned coordinate above 32767 is a negative one (a monitor left of or above the primary one). */
inline int readSystemHistory( const QPoint &at, HistoryPoint *out, int max )
{
    if( !out || max <= 0 ) return 0;
    if( max > MoveHistoryMark::kMaxPoints ) max = MoveHistoryMark::kMaxPoints;
    MOUSEMOVEPOINT query = {};
    query.x = at.x() & 0x0000FFFF;
    query.y = at.y() & 0x0000FFFF;
    MOUSEMOVEPOINT points[MoveHistoryMark::kMaxPoints];
    const int n = GetMouseMovePointsEx( sizeof( MOUSEMOVEPOINT ), &query, points, max, GMMP_USE_DISPLAY_POINTS );
    if( n <= 0 ) return n < 0 ? -1 : 0;
    for( int i = 0; i < n; ++i )
    {
        out[i].x = points[i].x > 32767 ? points[i].x - 65536 : points[i].x;
        out[i].y = points[i].y > 32767 ? points[i].y - 65536 : points[i].y;
        out[i].time = quint32( points[i].time );
    }
    return n;
}

/*! \brief The history source (readSystemHistory; a test supplies a scripted one). */
using HistoryReader = int (*)( const QPoint &at, HistoryPoint *out, int max );
inline HistoryReader &historyReader()
{
    static HistoryReader r = readSystemHistory;
    return r;
}

/*! \brief The history as it was at the last mark (entering fullscreen, then each tick). GUI thread only. */
inline MoveHistoryMark &historyMark()
{
    static MoveHistoryMark m;
    return m;
}

/*! \brief Reads the cursor position (GetCursorPos, physical pixels; a test supplies a scripted one). */
inline bool readSystemCursor( QPoint *at )
{
    POINT pt;
    if( !at || !GetCursorPos( &pt ) ) return false;
    *at = QPoint( pt.x, pt.y );
    return true;
}
using CursorReader = bool (*)( QPoint *at );
inline CursorReader &cursorReader()
{
    static CursorReader r = readSystemCursor;
    return r;
}

/*! \brief The thread's queue status (GetQueueStatus; a test supplies a scripted one). */
inline DWORD readSystemQueueStatus( UINT flags ) { return GetQueueStatus( flags ); }
using QueueStatusReader = DWORD (*)( UINT flags );
inline QueueStatusReader &queueStatusReader()
{
    static QueueStatusReader r = readSystemQueueStatus;
    return r;
}

/*! \brief Where the cursor was at the last mark (install or the last tick); have is false when it was not read. */
struct LastLook
{
    QPoint pos;
    bool have = false;
};
inline LastLook &lastLook()
{
    static LastLook l;
    return l;
}

/*! \brief Reads the cursor and the history ONCE: *leftSinceMark (optional) is that history judged against the
 *         previous mark (MoveHistoryMark::leftSinceMark), then the same read becomes the mark and the cursor the
 *         last look. If the position cannot be read, the mark and the last look are cleared and false is returned;
 *         a history that cannot be read clears the mark (the r5 position rule stands until the next mark). */
inline bool markHistoryAt( QPoint *cursor, bool *leftSinceMark )
{
    if( leftSinceMark ) *leftSinceMark = false;
    QPoint at;
    if( !cursorReader()( &at ) )
    {
        historyMark().unmark();
        lastLook().have = false;
        return false;
    }
    HistoryPoint points[MoveHistoryMark::kMaxPoints];
    const int n = historyReader()( at, points, MoveHistoryMark::kMaxPoints );
    if( leftSinceMark ) *leftSinceMark = historyMark().leftSinceMark( points, n, at.x(), at.y() );
    historyMark().mark( points, n );
    lastLook().pos = at;
    lastLook().have = true;
    if( cursor ) *cursor = at;
    return true;
}

/*! \brief Called by install(): marks the history at the cursor position (markHistoryAt). */
inline void markHistory()
{
    markHistoryAt( nullptr, nullptr );
}

/*! \brief r11 (adversary pass 15 A1): the event path saw the input itself (a delivered activity event, or a forced
 *         restore), so nothing is pending: witness().clear(). A move the hook saw also unmarked the history (proc:
 *         "the next tick marks afresh"), but with the witness cleared that tick takes nothing, so until it marks, a
 *         coalesced away-and-back retrieved at the delivered position is judged by position alone and lost (the idle
 *         period is not restarted and the cursor hides less than one idle period after real movement). So the history
 *         is marked again here, at the moment the activity is noted: every point the mark absorbs is no newer than that
 *         activity, and every later one is judged against it. A mark still standing is kept (no read). */
inline void noteDelivered()
{
    witness().clear();
    if( !historyMark().marked() ) markHistory();
}

/*! \brief r7 (key B1, opus H1): every idle tick, after it read the witness. Marks the history first (one read,
 *         judged against the previous mark), then reads the queue status, and returns true iff the mouse input
 *         still queued is activity by the QueueLook grammar (ViewerIdlePolicy.h). Removes nothing from the queue
 *         and retrieves nothing, so the hook does not run inside it. */
inline bool lookAtQueue()
{
    QueueLook q;
    q.haveLastLook = lastLook().have;
    q.lastLook = lastLook().pos;
    q.haveCursor = markHistoryAt( &q.cursor, &q.leftSinceMark );
    const DWORD queued = HIWORD( queueStatusReader()( QS_MOUSE ) );
    q.buttonQueued = ( queued & QS_MOUSEBUTTON ) != 0;
    q.moveQueued = ( queued & QS_MOUSEMOVE ) != 0;
    return queuedMouseIsActivity( q );
}

/*! \brief The WH_MOUSE procedure. Records, never consumes: the message always goes on to the next hook. */
inline LRESULT CALLBACK proc( int code, WPARAM wParam, LPARAM lParam )
{
    if( code >= 0 && lParam != 0 )
    {
        RetrievedKind kind;
        if( kindOfMessage( UINT( wParam ), &kind ) )
        {
            const MOUSEHOOKSTRUCT *mouse = reinterpret_cast<const MOUSEHOOKSTRUCT *>( lParam );
            const QPoint pos( mouse->pt.x, mouse->pt.y );
            // r6 (key B1): a move at the last position may be a coalesced away-and-back; the history decides.
            if( kind == RetrievedKind::Move && historyMark().marked() && witness().samePosition( pos ) )
            {
                HistoryPoint points[MoveHistoryMark::kMaxPoints];
                const int n = historyReader()( pos, points, MoveHistoryMark::kMaxPoints );
                if( historyMark().leftSinceMark( points, n, pos.x(), pos.y() ) ) witness().noteExcursion();
            }
            witness().note( kind, pos, code == HC_ACTION );
            // Movement is pending, so the next tick counts it (and shows a hidden cursor) and marks afresh.
            if( witness().pending() ) historyMark().unmark();
        }
    }
    return CallNextHookEx( hookHandle(), code, wParam, lParam );
}

/*! \brief Installs the hook for the calling thread (idempotent) and starts a fresh look at the pointer. */
inline bool install()
{
    POINT pt;
    if( GetCursorPos( &pt ) ) witness().seed( QPoint( pt.x, pt.y ) );
    else witness().reset();
    markHistory();   // r6 pass 8 A1: from the first moment in fullscreen, shown or hidden
    if( !hookHandle() ) hookHandle() = SetWindowsHookExW( WH_MOUSE, proc, nullptr, GetCurrentThreadId() );
    return hookHandle() != nullptr;
}

/*! \brief Removes the hook (idempotent); nothing stays pending. */
inline void remove()
{
    if( hookHandle() )
    {
        UnhookWindowsHookEx( hookHandle() );
        hookHandle() = nullptr;
    }
    witness().reset();
    historyMark().unmark();
    lastLook().have = false;
}

inline bool installed() { return hookHandle() != nullptr; }

} // namespace win_hook
} // namespace viewer_idle

#endif // Q_OS_WIN

#endif // VIEWERMOUSERETRIEVALHOOK_H
