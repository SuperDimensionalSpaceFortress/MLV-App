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

/*! \brief The WH_MOUSE procedure. Records, never consumes: the message always goes on to the next hook. */
inline LRESULT CALLBACK proc( int code, WPARAM wParam, LPARAM lParam )
{
    if( code >= 0 && lParam != 0 )
    {
        RetrievedKind kind;
        if( kindOfMessage( UINT( wParam ), &kind ) )
        {
            const MOUSEHOOKSTRUCT *mouse = reinterpret_cast<const MOUSEHOOKSTRUCT *>( lParam );
            witness().note( kind, QPoint( mouse->pt.x, mouse->pt.y ), code == HC_ACTION );
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
}

inline bool installed() { return hookHandle() != nullptr; }

} // namespace win_hook
} // namespace viewer_idle

#endif // Q_OS_WIN

#endif // VIEWERMOUSERETRIEVALHOOK_H
