// PLAYER-DOUBLECLICK-FULLSCREEN-1: the event side of the viewer double-click. MainWindow's
// application event filter calls filterEvent() for every event; it acts only on mouse presses and
// double-clicks delivered to one of the two display surfaces (the QGraphicsView viewport or the
// GL display window), so each double-click is decided exactly once.
#ifndef VIEWERDOUBLECLICKFILTER_H
#define VIEWERDOUBLECLICKFILTER_H

#include "ViewerDoubleClickPolicy.h"

#include <QEvent>
#include <QGuiApplication>
#include <QMouseEvent>
#include <QStyleHints>

#include <functional>

namespace viewer_dblclick
{

/*! \brief Returns true when the caller should toggle fullscreen and swallow the event.
 *  A press is never swallowed: it only updates the gate's first-press record. \a state is called
 *  lazily, only for events on a display surface. */
inline bool filterEvent( Gate &gate, const QObject *watched, QEvent *event, const QObject *viewport,
                         const QObject *glWindow, const std::function<ViewerState()> &state )
{
    if( !event ) return false;
    const QEvent::Type type = event->type();
    if( type != QEvent::MouseButtonPress && type != QEvent::MouseButtonDblClick ) return false;
    if( !isViewerTarget( watched, viewport, glWindow ) ) return false;

    const QMouseEvent *mouse = static_cast<const QMouseEvent *>( event );
    const Qt::MouseButton button = mouse->button();
    const Qt::KeyboardModifiers modifiers = mouse->modifiers();

    if( type == QEvent::MouseButtonPress )
    {
        if( button == Qt::LeftButton )
        {
            // Pair presses by time AND distance, the rule Qt itself uses to make a double-click.
            // Global position: the viewport and the GL window have different local coordinates.
            const QStyleHints *hints = QGuiApplication::styleHints();
            const int intervalMs = hints ? hints->mouseDoubleClickInterval() : 400;
#if QT_VERSION >= QT_VERSION_CHECK( 5, 14, 0 )
            const int distancePx = hints ? hints->mouseDoubleClickDistance() : 5;
#else
            const int distancePx = 5; // Qt's default platform-theme double-click distance
#endif
#if QT_VERSION >= QT_VERSION_CHECK( 6, 0, 0 )
            const QPointF globalPos = mouse->globalPosition();
#else
            const QPointF globalPos = mouse->screenPos();
#endif
            gate.notePress( state ? anyToolArmed( state() ) : false,
                            static_cast<quint64>( mouse->timestamp() ), intervalMs, globalPos, distancePx );
        }
        return false;
    }

    if( !state ) return false;
    return gate.decide( button, modifiers, state() );
}

} // namespace viewer_dblclick

#endif // VIEWERDOUBLECLICKFILTER_H
