#pragma once

// PLAYER-GL-WINDOW-INPUT-PARITY-1: pure mapping and routing rules for viewer mouse input that
// lands on the GL display window (GpuDisplayWindow) instead of the QGraphicsView. QtCore only,
// so console tests can pin it without a GUI.

#include <QEvent>
#include <QObject>
#include <QPointF>
#include <QRectF>
#include <QSizeF>

#include <cmath>

namespace gpu_window_input
{

// Input mirror of GpuDisplayWindow::paintGL's fit block (the letterbox fit):
//     if ( imgAspect > winAspect ) sy = winAspect / imgAspect;
//     else                         sx = imgAspect / winAspect;
// The console drift pin (test_gpu_window_viewer_input.cpp C6) fails if paintGL changes.
// Returns the centred rect, in window coordinates, that the frame occupies.
inline QRectF letterboxRect( const QSizeF &window, const QSizeF &display )
{
    if( window.width() <= 0.0 || window.height() <= 0.0
     || display.width() <= 0.0 || display.height() <= 0.0 )
        return QRectF();
    const double winAspect = window.width() / window.height();
    const double imgAspect = display.width() / display.height();
    double sx = 1.0;
    double sy = 1.0;
    if( imgAspect > winAspect ) sy = winAspect / imgAspect;
    else                        sx = imgAspect / winAspect;
    const double w = window.width() * sx;
    const double h = window.height() * sy;
    return QRectF( ( window.width() - w ) / 2.0, ( window.height() - h ) / 2.0, w, h );
}

// Window position -> scene position. Logical pixels in and out: the device pixel ratio
// cancels. Returns false when the point is outside the picture (on a black bar) or the
// geometry is empty; *scenePos is still written when the geometry is not empty.
inline bool windowToScene( const QPointF &windowPos, const QSizeF &window, const QSizeF &scene, QPointF *scenePos )
{
    const QRectF r = letterboxRect( window, scene );
    if( r.isEmpty() ) return false;
    if( scenePos )
    {
        *scenePos = QPointF( ( windowPos.x() - r.x() ) / r.width() * scene.width(),
                             ( windowPos.y() - r.y() ) / r.height() * scene.height() );
    }
    return r.contains( windowPos );
}

// Inverse of windowToScene (used by the round-trip test).
inline QPointF sceneToWindow( const QPointF &scenePos, const QSizeF &window, const QSizeF &scene )
{
    const QRectF r = letterboxRect( window, scene );
    if( r.isEmpty() || scene.width() <= 0.0 || scene.height() <= 0.0 ) return QPointF();
    return QPointF( r.x() + scenePos.x() / scene.width() * r.width(),
                    r.y() + scenePos.y() / scene.height() * r.height() );
}

enum class Route { None, PickWhiteBalance, PickBadPixel, ContextMenu, AcceptDrag, Drop };

// Qt's own per-platform context-menu trigger: release on Windows, press elsewhere.
inline constexpr bool contextMenuOnReleaseDefault()
{
#ifdef Q_OS_WIN
    return true;
#else
    return false;
#endif
}

inline Route route( QEvent::Type type, Qt::MouseButton button, bool wbArmed, bool bpArmed, bool contextMenuOnRelease )
{
    switch( type )
    {
    case QEvent::MouseButtonPress:
        if( button == Qt::LeftButton )
            return wbArmed ? Route::PickWhiteBalance : ( bpArmed ? Route::PickBadPixel : Route::None );
        if( button == Qt::RightButton && !contextMenuOnRelease ) return Route::ContextMenu;
        return Route::None;
    case QEvent::MouseButtonRelease:
        if( button == Qt::RightButton && contextMenuOnRelease ) return Route::ContextMenu;
        return Route::None;
    case QEvent::DragEnter:
    case QEvent::DragMove:
        return Route::AcceptDrag;
    case QEvent::Drop:
        return Route::Drop;
    default:
        return Route::None;
    }
}

// The app-wide filter's "is this press on the viewer?" test: the QGraphicsView, or the GL
// display window that replaces it (the hidden view is never under the mouse).
inline bool pressIsOnViewer( bool graphicsViewUnderMouse, const QObject *watched, const QObject *glWindow )
{
    return graphicsViewUnderMouse || ( glWindow && watched == glWindow );
}

} // namespace gpu_window_input
