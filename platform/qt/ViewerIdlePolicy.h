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
 * it on entering fullscreen, and onTimeout() says whether to hide now or rearm for how long.
 */

#ifndef VIEWERIDLEPOLICY_H
#define VIEWERIDLEPOLICY_H

#include <QEvent>
#include <QPoint>
#include <QtGlobal>

namespace viewer_idle
{

/*! \brief Idle time with no mouse activity before the fullscreen cursor hides. */
inline constexpr int kIdleHideMs() { return 2000; }

/*! \brief What the window looks like at the moment the hide timer fires. */
struct Inputs
{
    bool fullscreen = false;        // actionFullscreen checked AND the window really is fullscreen
    bool windowActive = false;      // the main window is the active window
    bool toolArmed = false;         // WB picker, bad-pixel edit or gradient paint armed
    bool popupOrModalOpen = false;  // a popup (menu) or a modal dialog is open
};

/*! \brief The cursor may hide only in real fullscreen, while active, with no tool and no popup/modal. */
inline bool mayHide( const Inputs &i )
{
    return i.fullscreen && i.windowActive && !i.toolArmed && !i.popupOrModalOpen;
}

/*! \brief Whether an event of this type is real mouse activity. A move to the same global position
 *         (Windows and Qt synthesize those) is not activity; press, release, double-click and wheel are. */
inline bool isActivity( QEvent::Type type, const QPoint &globalNow, const QPoint &globalLast, bool haveLast )
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
 *         window or a deactivation (no mouse event reaches a blocked window), and a context menu. */
inline bool forcesRestore( QEvent::Type type )
{
    return type == QEvent::WindowBlocked
        || type == QEvent::WindowDeactivate
        || type == QEvent::ContextMenu;
}

/*! \brief What the caller does when its single-shot idle timer fires. */
enum class Tick { None, Hide, Rearm };

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

    /*! \brief The timer fired. Hide, or rearm the timer for *rearmMs, or do nothing. */
    Tick onTimeout( const Inputs &in, qint64 nowMs, int idleMs, int *rearmMs )
    {
        if( !m_fullscreen ) return Tick::None;
        if( m_hidden ) return Tick::None;
        if( !mayHide( in ) )
        {
            if( rearmMs ) *rearmMs = idleMs;
            return Tick::Rearm;
        }
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

    bool hidden() const { return m_hidden; }
    bool fullscreen() const { return m_fullscreen; }

private:
    bool m_fullscreen = false;
    bool m_hidden = false;
    qint64 m_lastActivity = 0;
};

} // namespace viewer_idle

#endif // VIEWERIDLEPOLICY_H
