#pragma once

// PLAYER-GL-WINDOW-INPUT-PARITY-1: pure mapping and routing rules for viewer mouse input that
// lands on the GL display window (GpuDisplayWindow) instead of the QGraphicsView. QtCore only,
// so console tests can pin it without a GUI.

#include <QEvent>
#include <QList>
#include <QObject>
#include <QPointF>
#include <QRectF>
#include <QSizeF>
#include <QString>
#include <QStringList>
#include <QUrl>

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
// geometry is empty; *scenePos is still written when the geometry is not empty. The picture
// is HALF-OPEN, [left, right) x [top, bottom): its right and bottom edges map to scene x ==
// width and y == height, one past the last pixel, so they are outside (adversary A2, r1).
inline bool windowToScene( const QPointF &windowPos, const QSizeF &window, const QSizeF &scene, QPointF *scenePos )
{
    const QRectF r = letterboxRect( window, scene );
    if( r.isEmpty() ) return false;
    if( scenePos )
    {
        *scenePos = QPointF( ( windowPos.x() - r.x() ) / r.width() * scene.width(),
                             ( windowPos.y() - r.y() ) / r.height() * scene.height() );
    }
    return windowPos.x() >= r.left() && windowPos.x() < r.right()
        && windowPos.y() >= r.top() && windowPos.y() < r.bottom();
}

inline bool halfOpenContains( const QRectF &r, const QPointF &p )
{
    return !r.isEmpty() && p.x() >= r.left() && p.x() < r.right() && p.y() >= r.top() && p.y() < r.bottom();
}

// Window position -> the scene pixel a picker click selects, as the slots take it.
//
// paintGL letterboxes with the display size it was handed, which is NOT always the scene's
// aspect: the image route hands it the scene-sized image, but the CUDA texture route hands it
// the clip's own stretched size (mainWindowGpuTexturePresentDisplaySize), while the scene is
// computed with integer division before the stretch (computeDisplaySceneGeometry), so under an
// anamorphic stretch the two pictures can differ by several scene pixels (adversary r1 pass 1
// A3, pass 2 A1). GpuDisplayWindow exposes no read of its display size, so the caller passes
// both candidates: primaryDisplay (the route that presented last) and otherDisplay. An empty
// candidate means the scene.
//  - A click counts when it is inside EITHER candidate's letterbox (half-open), so a click on
//    the displayed picture is never dropped whichever route drew it; a click outside both is on
//    the black bars and selects nothing.
//  - It is mapped through the primary candidate's letterbox, then clamped to a real pixel,
//    [0, ceil(width) - 1] x [0, ceil(height) - 1], so no pick names a row or column one past
//    the image (adversary r1 pass 1 A2: the slots' own guard is "x > width").
inline bool pickPixel( const QPointF &windowPos, const QSizeF &window, const QSizeF &scene,
                       const QSizeF &primaryDisplay, const QSizeF &otherDisplay, int *x, int *y )
{
    if( scene.width() <= 0.0 || scene.height() <= 0.0 ) return false;
    const auto orScene = [&scene]( const QSizeF &d ) { return ( d.width() > 0.0 && d.height() > 0.0 ) ? d : scene; };
    const QRectF primary = letterboxRect( window, orScene( primaryDisplay ) );
    const QRectF other = letterboxRect( window, orScene( otherDisplay ) );
    if( primary.isEmpty() ) return false;
    if( !halfOpenContains( primary, windowPos ) && !halfOpenContains( other, windowPos ) ) return false;
    const double sx = ( windowPos.x() - primary.x() ) / primary.width() * scene.width();
    const double sy = ( windowPos.y() - primary.y() ) / primary.height() * scene.height();
    const int lastX = int( std::ceil( scene.width() ) ) - 1;
    const int lastY = int( std::ceil( scene.height() ) ) - 1;
    int px = int( std::floor( sx ) );
    int py = int( std::floor( sy ) );
    px = px < 0 ? 0 : ( px > lastX ? lastX : px );
    py = py < 0 ? 0 : ( py > lastY ? lastY : py );
    if( x ) *x = px;
    if( y ) *y = py;
    return true;
}

// The dropped URLs as the QGraphicsView path hands them to openMlvSet: every URL through
// toLocalFile(), empty results skipped, order kept. Mirrors GraphicsPickerScene::dropEvent, so
// a drop on the GL window opens exactly what the same drop on the scene opens (adversary A1,
// r1: MainWindow::dropEvent classifies by the first path only and is the top-level route).
inline QStringList localPathsFromUrls( const QList<QUrl> &urls )
{
    QStringList list;
    list.reserve( urls.size() );
    for( const QUrl &url : urls )
    {
        const QString localPath = url.toLocalFile();
        if( !localPath.isEmpty() )
            list.append( localPath );
    }
    return list;
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
