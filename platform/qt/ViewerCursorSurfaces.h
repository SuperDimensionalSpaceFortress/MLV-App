/*!
 * \file ViewerCursorSurfaces.h
 * \brief PLAYER-CURSOR-AUTOHIDE-1: the two viewer surfaces whose cursor the fullscreen idle hide blanks,
 *        and the reading of a Qt input event as mouse activity. The idle decision itself is
 *        ViewerIdlePolicy.h.
 *
 * Only the viewer surfaces get Qt::BlankCursor: the QGraphicsView viewport (the scene display path) and
 * the GL display window (a QWindow, so it needs QWindow::setCursor). No application-wide override
 * cursor is used, so menus and dialogs always keep their cursor.
 *
 * Restore never overwrites a cursor someone else set while hidden: a viewer tool armed while the cursor
 * was hidden has already put its own cursor on the viewport, and that cursor stays.
 */

#ifndef VIEWERCURSORSURFACES_H
#define VIEWERCURSORSURFACES_H

#include "ViewerIdlePolicy.h"

#include <QCursor>
#include <QMouseEvent>
#include <QWheelEvent>
#include <QWidget>
#include <QWindow>

namespace viewer_idle
{

/*! \brief The surfaces under the fullscreen cursor. Either may be null (skipped). */
struct Surfaces
{
    QWidget *viewport = nullptr;
    QWindow *glWindow = nullptr;
};

/*! \brief Blanks and restores the cursor of the viewer surfaces. */
class CursorHider
{
public:
    void hide( const Surfaces &s )
    {
        if( s.viewport )
        {
            m_viewportHadOwnCursor = s.viewport->testAttribute( Qt::WA_SetCursor );
            m_viewportSavedCursor = s.viewport->cursor();
            s.viewport->setCursor( Qt::BlankCursor );
        }
        if( s.glWindow )
        {
            s.glWindow->setCursor( Qt::BlankCursor );
        }
    }

    void restore( const Surfaces &s )
    {
        if( s.viewport && s.viewport->cursor().shape() == Qt::BlankCursor )
        {
            if( m_viewportHadOwnCursor ) s.viewport->setCursor( m_viewportSavedCursor );
            else s.viewport->unsetCursor();
        }
        if( s.glWindow && s.glWindow->cursor().shape() == Qt::BlankCursor )
        {
            s.glWindow->unsetCursor();
        }
    }

private:
    bool m_viewportHadOwnCursor = false;
    QCursor m_viewportSavedCursor;
};

/*! \brief Whether this event is real mouse activity (see isActivity). For mouse button and move events
 *         the global position is compared with, then stored in, *lastGlobal / *haveLast; other events
 *         leave the stored position alone. */
inline bool eventIsActivity( const QEvent *event, QPoint *lastGlobal, bool *haveLast )
{
    if( !event ) return false;
    const QEvent::Type type = event->type();
    switch( type )
    {
    case QEvent::MouseMove:
    case QEvent::MouseButtonPress:
    case QEvent::MouseButtonRelease:
    case QEvent::MouseButtonDblClick:
    {
        const QMouseEvent *mouse = dynamic_cast<const QMouseEvent *>( event );
        if( !mouse ) return false;
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
        const QPoint global = mouse->globalPosition().toPoint();
#else
        const QPoint global = mouse->globalPos();
#endif
        const bool activity = isActivity( type, global, *lastGlobal, *haveLast );
        *lastGlobal = global;
        *haveLast = true;
        return activity;
    }
    case QEvent::Wheel:
        return true;
    default:
        return isActivity( type, QPoint(), QPoint(), false );
    }
}

} // namespace viewer_idle

#endif // VIEWERCURSORSURFACES_H
