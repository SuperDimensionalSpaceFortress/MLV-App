// PLAYER-DOUBLECLICK-FULLSCREEN-1: the event side of the viewer double-click. MainWindow's
// application event filter calls filterEvent() for every event; it decides only double-clicks
// delivered to one of the two display surfaces (the QGraphicsView viewport or the GL display
// window), so each double-click is decided exactly once. Left presses anywhere update the gate's
// first-press record (a press elsewhere records "nothing armed").
#ifndef VIEWERDOUBLECLICKFILTER_H
#define VIEWERDOUBLECLICKFILTER_H

#include "ViewerDoubleClickPolicy.h"

#include <QEvent>
#include <QMouseEvent>

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

    const QMouseEvent *mouse = static_cast<const QMouseEvent *>( event );
    const Qt::MouseButton button = mouse->button();
    const Qt::KeyboardModifiers modifiers = mouse->modifiers();
    const bool onViewer = isViewerTarget( watched, viewport, glWindow );

    if( type == QEvent::MouseButtonPress )
    {
        if( button == Qt::LeftButton )
        {
            // Qt has already decided whether this press is the second press of a double-click;
            // the gate keeps the first press's record across it (see Gate).
            const bool createdDoubleClick = mouse->flags().testFlag( Qt::MouseEventCreatedDoubleClick );
            if( onViewer )
            {
                const bool armedNow = ( !createdDoubleClick && state ) ? anyToolArmed( state() ) : false;
                gate.notePress( armedNow, createdDoubleClick, mouse->timestamp() );
            }
            else
            {
                // A first press Qt may pair from outside the viewer: a new, unarmed record
                // (adversary r1 pass 3 A2). No state() read for presses elsewhere.
                gate.noteOtherPress( createdDoubleClick, mouse->timestamp() );
            }
        }
        return false;
    }

    if( !onViewer ) return false;

    if( !state ) return false;
    return gate.decide( button, modifiers, state() );
}

} // namespace viewer_dblclick

#endif // VIEWERDOUBLECLICKFILTER_H
