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

#include <algorithm>
#include <cmath>

namespace gpu_window_input
{

// Input mirror of GpuDisplayWindow::paintGL's fit block (the letterbox fit):
//     if ( imgAspect > winAspect ) sy = winAspect / imgAspect;
//     else                         sx = imgAspect / winAspect;
// Returns the centred rect, in window coordinates, that a frame of that display size occupies.
// Picks do not use it (they map through drawnPictureRect, what paintGL recorded); it is the
// reference the tests compare drawnPictureRect and windowToScene against.
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

// The picture the last paintGL drew, in window logical pixels (mouse coordinates), from what
// GpuDisplayWindow::lastPaintedPicture() recorded: the framebuffer size, the device pixel ratio
// and the quad's half-extents sx/sy. paintGL draws the quad over NDC [-sx, sx] x [-sy, sy] in a
// framebuffer-sized viewport, so the picture spans device x (1 - sx) / 2 * fbw .. (1 + sx) / 2 * fbw
// (y likewise, from the top); dividing by the device pixel ratio gives logical pixels. Empty
// (no pick) when nothing was drawn or any input is not a positive finite number.
inline QRectF drawnPictureRect( bool valid, int framebufferWidth, int framebufferHeight,
                                double devicePixelRatio, double scaleX, double scaleY )
{
    if( !valid || framebufferWidth <= 0 || framebufferHeight <= 0 ) return QRectF();
    if( !std::isfinite( devicePixelRatio ) || !std::isfinite( scaleX ) || !std::isfinite( scaleY ) ) return QRectF();
    if( devicePixelRatio <= 0.0 || scaleX <= 0.0 || scaleY <= 0.0 ) return QRectF();
    const double w = framebufferWidth / devicePixelRatio;
    const double h = framebufferHeight / devicePixelRatio;
    return QRectF( ( 1.0 - scaleX ) * 0.5 * w, ( 1.0 - scaleY ) * 0.5 * h, scaleX * w, scaleY * h );
}

// Largest clip, scene or stretched extent the mapper accepts, so every int conversion below is
// defined (adversary r2 pass 2 A2: INT_MAX-sized geometry overflowed before the clamp).
inline constexpr double kMaxPickExtent = 16777216.0;

inline bool pickExtentUsable( double v )
{
    return std::isfinite( v ) && v > 0.0 && v <= kMaxPickExtent;
}

// Where a click lands inside the drawn picture, as fractions of its width and height in [0, 1).
// drawn is the picture rect the last paintGL actually drew (drawnPictureRect). paintGL letterboxes
// with whatever display size the route that presented handed it (the scene-sized image, the CUDA
// texture route's stretched clip size, or the RGB fallback), and the stretch controls can change
// before the next frame lands, so only paintGL knows the rect: r1's reconstructions from the
// requested route and the live stretch were wrong after a route fallback, a stretch change or
// integer truncation (adversary r1 pass 3 A1-A3). An empty rect (nothing drawn yet) is no pick, and
// a click counts only inside drawn (half-open): the black bars select nothing. The drawn picture is
// the whole frame, as the scene is, so the fractions locate the frame position the picture shows.
inline bool pictureFraction( const QPointF &windowPos, const QRectF &drawn, double *fx, double *fy )
{
    if( !halfOpenContains( drawn, windowPos ) ) return false;
    const double ax = ( windowPos.x() - drawn.x() ) / drawn.width();
    const double ay = ( windowPos.y() - drawn.y() ) / drawn.height();
    if( !std::isfinite( ax ) || !std::isfinite( ay ) ) return false;
    const double belowOne = std::nextafter( 1.0, 0.0 );
    *fx = ax < 0.0 ? 0.0 : ( ax > belowOne ? belowOne : ax );
    *fy = ay < 0.0 ? 0.0 : ( ay > belowOne ? belowOne : ay );
    return true;
}

// Window position -> scene pixel, proportionally through the drawn picture, clamped to a real pixel
// [0, ceil(width) - 1] x [0, ceil(height) - 1] (adversary r1 pass 1 A2: the slots' own guard is
// "x > width"). The scene-space mapping; the forwarder hands the slots pickSlotPixel's integers.
inline bool pickPixel( const QPointF &windowPos, const QRectF &drawn, const QSizeF &scene, int *x, int *y )
{
    if( !pickExtentUsable( scene.width() ) || !pickExtentUsable( scene.height() ) ) return false;
    double fx = 0.0;
    double fy = 0.0;
    if( !pictureFraction( windowPos, drawn, &fx, &fy ) ) return false;
    const int lastX = int( std::ceil( scene.width() ) ) - 1;
    const int lastY = int( std::ceil( scene.height() ) ) - 1;
    int px = int( std::floor( fx * scene.width() ) );
    int py = int( std::floor( fy * scene.height() ) );
    px = px < 0 ? 0 : ( px > lastX ? lastX : px );
    py = py < 0 ? 0 : ( py > lastY ? lastY : py );
    if( x ) *x = px;
    if( y ) *y = py;
    return true;
}

// What the picker slots (MainWindow::whiteBalancePicked / badPixelPicked) convert the scene integers
// they are handed with, read when the click lands. Per axis, exactly as they compute it:
//   fit mode: raw = int( c * ( raw / sceneRect ) );   100%: raw = int( c / the CURRENT stretch ).
struct PickBasis
{
    bool zoomFit = true;
    QSizeF sceneRect;
    int rawWidth = 0;
    int rawHeight = 0;
    double stretchX = 1.0;
    double stretchY = 1.0;
};

// Every input positive, finite and bounded, in either mode (adversary r2 pass 2 A3: fit mode used to
// skip the clip and stretch checks).
inline bool pickBasisUsable( const PickBasis &b )
{
    if( !pickExtentUsable( b.rawWidth ) || !pickExtentUsable( b.rawHeight ) ) return false;
    if( !pickExtentUsable( b.stretchX ) || !pickExtentUsable( b.stretchY ) ) return false;
    if( !pickExtentUsable( b.rawWidth * b.stretchX ) || !pickExtentUsable( b.rawHeight * b.stretchY ) ) return false;
    if( b.zoomFit && ( !pickExtentUsable( b.sceneRect.width() ) || !pickExtentUsable( b.sceneRect.height() ) ) ) return false;
    return true;
}

// One axis of the slots' scene -> raw conversion, in their own arithmetic (double, then int truncation).
inline int slotRawFromScene( int c, bool zoomFit, int raw, double sceneExtent, double stretch )
{
    return zoomFit ? static_cast<int>( c * ( raw / sceneExtent ) ) : static_cast<int>( c / stretch );
}

// The scene integer for one axis whose slot conversion names raw pixel floor( fraction * raw ): the
// pixel the picture shows. Flooring in scene space and letting the slot truncate again misses it by
// one when the stretch is not 1 (adversary r2 pass 2 A1: displayed raw (23.04, 101.2) at stretch
// 1.25 x 1.6667 became (22, 100)). The conversion is non-decreasing in c, so a binary search finds
// the smallest c reaching the wanted pixel. At 100% every raw pixel is reachable (the stretches are
// >= 1); in fit mode with a scene smaller than the clip one scene pixel spans several raw pixels, and
// the reachable one nearest the wanted pixel is taken (ties: the lower), as fine as the slots allow.
inline int slotSceneForFraction( double fraction, bool zoomFit, int raw, double sceneExtent, double stretch )
{
    const double slotExtent = zoomFit ? sceneExtent : raw * stretch;
    const int lastC = std::max( 0, int( std::ceil( slotExtent ) ) - 1 );
    int want = int( std::floor( fraction * raw ) );
    want = want < 0 ? 0 : ( want > raw - 1 ? raw - 1 : want );
    const auto conv = [&]( int c ) { return slotRawFromScene( c, zoomFit, raw, sceneExtent, stretch ); };
    int lo = 0;
    int hi = lastC + 1;
    while( lo < hi )
    {
        const int mid = lo + ( hi - lo ) / 2;
        if( conv( mid ) >= want ) hi = mid;
        else lo = mid + 1;
    }
    if( lo > lastC ) return lastC;
    if( lo == 0 || conv( lo ) == want ) return lo;
    return ( conv( lo ) - want < want - conv( lo - 1 ) ) ? lo : lo - 1;
}

// Window position -> the scene integers the picker slots must be handed so that their own
// conversion selects the raw pixel the drawn picture shows at that position, whatever the stretch
// controls say now (adversary r2 pass 1 A1, pass 2 A1). False (no pick) on a black bar, before any
// paint, or with an unusable basis.
inline bool pickSlotPixel( const QPointF &windowPos, const QRectF &drawn, const PickBasis &basis, int *x, int *y )
{
    if( !pickBasisUsable( basis ) ) return false;
    double fx = 0.0;
    double fy = 0.0;
    if( !pictureFraction( windowPos, drawn, &fx, &fy ) ) return false;
    const int px = slotSceneForFraction( fx, basis.zoomFit, basis.rawWidth, basis.sceneRect.width(), basis.stretchX );
    const int py = slotSceneForFraction( fy, basis.zoomFit, basis.rawHeight, basis.sceneRect.height(), basis.stretchY );
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
