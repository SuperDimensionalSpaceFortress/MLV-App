/*!
 * \file FullscreenControlBar.cpp
 * \brief PLAYER-FULLSCREEN-CONTROL-BAR-1: the bar's rasteriser and the CPU viewer's bar widget (see the header).
 */

#include "FullscreenControlBar.h"

#include <QAbstractScrollArea>
#include <QColor>
#include <QCoreApplication>
#include <QFont>
#include <QFontDatabase>
#include <QMouseEvent>
#include <QPainter>
#include <QPainterPath>
#include <QPolygonF>
#include <QResizeEvent>
#include <QWheelEvent>

namespace
{

const QColor kBackground( 22, 22, 22 );
const QColor kIcon( 232, 232, 232 );
const QColor kTrack( 88, 88, 88 );
const QColor kPlayhead( 64, 168, 255 );

void preparePainter( QPainter &p )
{
    p.setRenderHint( QPainter::Antialiasing, true );
    p.setRenderHint( QPainter::TextAntialiasing, true );
}

// The icon box: the button square inset by 30 %, centred.
QRectF iconBox( const QRect &button )
{
    const qreal side = qMin( button.width(), button.height() ) * 0.4;
    const QPointF c = QRectF( button ).center();
    return QRectF( c.x() - side / 2, c.y() - side / 2, side, side );
}

void drawTriangle( QPainter &p, const QRectF &box, bool pointsRight )
{
    QPolygonF tri;
    if( pointsRight ) tri << box.topLeft() << box.bottomLeft() << QPointF( box.right(), box.center().y() );
    else tri << box.topRight() << box.bottomRight() << QPointF( box.left(), box.center().y() );
    p.setPen( Qt::NoPen );
    p.setBrush( kIcon );
    p.drawPolygon( tri );
}

void drawPlayPause( QPainter &p, const QRect &button, bool playing )
{
    const QRectF box = iconBox( button );
    if( !playing )
    {
        drawTriangle( p, box, true );
        return;
    }
    const qreal barW = box.width() * 0.32;
    p.fillRect( QRectF( box.left(), box.top(), barW, box.height() ), kIcon );
    p.fillRect( QRectF( box.right() - barW, box.top(), barW, box.height() ), kIcon );
}

void drawStep( QPainter &p, const QRect &button, bool forward )
{
    const QRectF box = iconBox( button );
    const qreal barW = qMax( 2.0, box.width() * 0.16 );
    if( forward )
    {
        drawTriangle( p, QRectF( box.left(), box.top(), box.width() - barW * 1.5, box.height() ), true );
        p.fillRect( QRectF( box.right() - barW, box.top(), barW, box.height() ), kIcon );
    }
    else
    {
        p.fillRect( QRectF( box.left(), box.top(), barW, box.height() ), kIcon );
        drawTriangle( p, QRectF( box.left() + barW * 1.5, box.top(), box.width() - barW * 1.5, box.height() ), false );
    }
}

// Exit fullscreen: four corner brackets pointing inward.
void drawExit( QPainter &p, const QRect &button )
{
    const QRectF box = iconBox( button );
    const qreal arm = box.width() * 0.38;
    QPen pen( kIcon );
    pen.setWidthF( qMax( 2.0, box.width() * 0.12 ) );
    pen.setCapStyle( Qt::FlatCap );
    pen.setJoinStyle( Qt::MiterJoin );
    p.setPen( pen );
    p.setBrush( Qt::NoBrush );
    const qreal l = box.left(), r = box.right(), t = box.top(), b = box.bottom();
    const qreal ix = l + arm, rx = r - arm, ty = t + arm, by = b - arm;
    QPainterPath path;
    path.moveTo( ix, t ); path.lineTo( ix, ty ); path.lineTo( l, ty );
    path.moveTo( rx, t ); path.lineTo( rx, ty ); path.lineTo( r, ty );
    path.moveTo( ix, b ); path.lineTo( ix, by ); path.lineTo( l, by );
    path.moveTo( rx, b ); path.lineTo( rx, by ); path.lineTo( r, by );
    p.drawPath( path );
}

void paintTimecodeCell( QPainter &p, const QRect &cell, const QString &timecode, int fontPx )
{
    p.fillRect( cell, kBackground );
    QFont font = QFontDatabase::systemFont( QFontDatabase::FixedFont );
    font.setPixelSize( qMax( 1, fontPx ) );
    p.setFont( font );
    p.setPen( kIcon );
    p.drawText( cell, Qt::AlignCenter, timecode );
}

#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
QPointF localPosOf( const QMouseEvent *e ) { return e->position(); }
QPointF localPosOf( const QWheelEvent *e ) { return e->position(); }
QPointF globalPosOf( const QWheelEvent *e ) { return e->globalPosition(); }
#else
QPointF localPosOf( const QMouseEvent *e ) { return e->localPos(); }
QPointF localPosOf( const QWheelEvent *e ) { return e->posF(); }
QPointF globalPosOf( const QWheelEvent *e ) { return e->globalPosF(); }
#endif

// The view's events after which its device-pixel size may differ with no Resize (adversary r1 pass 3 A2): a ratio
// change on the same screen (Qt 6.6 and later) and a move to another screen (every version).
bool viewGeometryChanges( QEvent::Type type )
{
    switch( type )
    {
    case QEvent::Resize:
    case QEvent::ScreenChangeInternal:
#if QT_VERSION >= QT_VERSION_CHECK(6, 6, 0)
    case QEvent::DevicePixelRatioChange:
#endif
        return true;
    default:
        return false;
    }
}

} // namespace

QImage renderFullscreenControlBar( const fullscreen_bar::Layout &l, const fullscreen_bar::Feed &feed )
{
    if( l.bar.isEmpty() ) return QImage();
    QImage image( l.bar.size(), QImage::Format_RGBA8888_Premultiplied );
    image.fill( kBackground );
    QPainter p( &image );
    preparePainter( p );
    const QPoint o = -l.bar.topLeft();
    if( !l.play.isEmpty() ) drawPlayPause( p, l.play.translated( o ), feed.playing );
    if( !l.prev.isEmpty() ) drawStep( p, l.prev.translated( o ), false );
    if( !l.next.isEmpty() ) drawStep( p, l.next.translated( o ), true );
    if( !l.exit.isEmpty() ) drawExit( p, l.exit.translated( o ) );
    if( !l.track.isEmpty() ) p.fillRect( l.track.translated( o ), kTrack );
    if( !l.timecode.isEmpty() ) paintTimecodeCell( p, l.timecode.translated( o ), feed.timecode, l.fontPx );
    return image;
}

QImage renderFullscreenControlBarTimecode( const fullscreen_bar::Layout &l, const QString &timecode )
{
    if( l.timecode.isEmpty() ) return QImage();
    QImage image( l.timecode.size(), QImage::Format_RGBA8888_Premultiplied );
    image.fill( kBackground );
    QPainter p( &image );
    preparePainter( p );
    paintTimecodeCell( p, QRect( QPoint( 0, 0 ), l.timecode.size() ), timecode, l.fontPx );
    return image;
}

QColor fullscreenControlBarPlayheadColor( void )
{
    return kPlayhead;
}

FullscreenControlBarWidget::FullscreenControlBarWidget( QWidget *view )
    : QWidget( view )
    , m_view( view )
{
    setAttribute( Qt::WA_OpaquePaintEvent );
    setAttribute( Qt::WA_NoSystemBackground );
    setAttribute( Qt::WA_NoMousePropagation );
    setAutoFillBackground( false );
    setFocusPolicy( Qt::NoFocus );
    setMouseTracking( true );   // hover moves reach the idle clock (the application filter) and the hold
    QWidget::hide();
    if( m_view ) m_view->installEventFilter( this );
}

fullscreen_bar::Layout FullscreenControlBarWidget::viewLayout( void ) const
{
    if( !m_view ) return fullscreen_bar::Layout();
    const qreal dpr = devicePixelRatioF();
    return fullscreen_bar::layoutFor( QSize( qRound( m_view->width() * dpr ), qRound( m_view->height() * dpr ) ) );
}

bool FullscreenControlBarWidget::containsGlobal( const QPoint &globalPos ) const
{
    return isVisible() && rect().contains( mapFromGlobal( globalPos ) );
}

void FullscreenControlBarWidget::followView( void )
{
    if( !m_view ) return;
    const fullscreen_bar::Layout l = viewLayout();
    if( l.bar.isEmpty() )
    {
        setGeometry( QRect() );
        return;
    }
    const qreal dpr = devicePixelRatioF();
    const int h = qMin( m_view->height(), qCeil( l.bar.height() / dpr ) );
    setGeometry( 0, m_view->height() - h, m_view->width(), h );
}

void FullscreenControlBarWidget::setBarShown( bool shown, qint64 nowMs )
{
    if( shown )
    {
        if( !m_presenter.show( nowMs ) ) return;
        followView();
        raise();   // above the viewport, also one set after this widget was made
        QWidget::show();
    }
    else if( m_presenter.hide( nowMs ) )
    {
        QWidget::hide();
    }
}

void FullscreenControlBarWidget::setFeed( const fullscreen_bar::Feed &feed )
{
    if( m_presenter.noteFeed( feed ) ) update();
}

bool FullscreenControlBarWidget::eventFilter( QObject *watched, QEvent *event )
{
    if( watched == m_view && m_presenter.shown() && viewGeometryChanges( event->type() ) )
    {
        followView();
        update();   // the raster is keyed on the device-pixel size; paintEvent drops one made at another ratio
    }
    return QWidget::eventFilter( watched, event );
}

void FullscreenControlBarWidget::paintEvent( QPaintEvent *event )
{
    Q_UNUSED( event );
    QPainter p( this );
    p.fillRect( rect(), kBackground );   // opaque: every pixel is painted, also a rounding sliver
    const fullscreen_bar::Layout l = viewLayout();
    const fullscreen_bar::Feed &feed = m_presenter.feed();
    const qreal dpr = devicePixelRatioF();
    // The cache is keyed on the device-pixel bar size, which a ratio change can leave equal: re-raster at the new one.
    if( !m_pixmap.isNull() && !qFuzzyCompare( m_pixmap.devicePixelRatio(), dpr ) ) m_presenter.dropRaster();
    const fullscreen_bar::Raster need = m_presenter.rasterFor( l.bar.size() );
    if( need == fullscreen_bar::Raster::Full || ( need == fullscreen_bar::Raster::Timecode && m_pixmap.isNull() ) )
    {
        QImage image = renderFullscreenControlBar( l, feed );
        m_presenter.noteRaster( fullscreen_bar::Raster::Full, l.bar.size() );
        m_pixmap = QPixmap::fromImage( image );
        m_pixmap.setDevicePixelRatio( dpr );
        m_presenter.noteUpload();
    }
    else if( need == fullscreen_bar::Raster::Timecode )
    {
        const QImage cell = renderFullscreenControlBarTimecode( l, feed.timecode );
        m_presenter.noteRaster( need, l.bar.size() );
        // Into the cached pixmap at device pixels (its own ratio set aside while drawing).
        m_pixmap.setDevicePixelRatio( 1.0 );
        {
            QPainter cp( &m_pixmap );
            cp.drawImage( l.timecode.topLeft() - l.bar.topLeft(), cell );
        }
        m_pixmap.setDevicePixelRatio( dpr );
        m_presenter.noteUpload();
    }
    if( !m_pixmap.isNull() ) p.drawPixmap( 0, 0, m_pixmap );
    const QRect head = fullscreen_bar::playheadRect( l, feed.frame, feed.totalFrames ).translated( -l.bar.topLeft() );
    if( !head.isEmpty() )
    {
        p.fillRect( QRectF( head.x() / dpr, head.y() / dpr, head.width() / dpr, head.height() / dpr ),
                    fullscreenControlBarPlayheadColor() );
    }
}

// A position on the widget maps to the viewer through the layout paintEvent drew: the bar's pixmap sits at the widget's
// origin, so widget-local p is device l.bar.topLeft() + p * dpr. Not through pos(): the widget's geometry can lag the
// layout (a ratio change the view saw no Resize for), and the clicks must hit what is on screen (adversary r1 pass 3 A2).
bool FullscreenControlBarWidget::route( QEvent *event, const QPointF &localPos )
{
    event->accept();
    if( !m_input ) return true;
    const qreal dpr = devicePixelRatioF();
    const fullscreen_bar::Layout l = viewLayout();
    return m_input( l, event, QPointF( l.bar.topLeft() ) + localPos * dpr );
}

// A wheel over the bar during a gesture begun elsewhere is that gesture's (adversary r1 pass 3 A3, the CPU sibling of
// the GL claim): the viewport gets it, at the same global position, as it would with no bar.
void FullscreenControlBarWidget::forwardWheelToView( QWheelEvent *event )
{
    QWidget *target = m_view;
    if( QAbstractScrollArea *area = qobject_cast<QAbstractScrollArea *>( m_view ) ) target = area->viewport();
    if( !target ) return;
    const QPointF global = globalPosOf( event );
    QWheelEvent copy( QPointF( target->mapFromGlobal( global.toPoint() ) ), global, event->pixelDelta(), event->angleDelta(),
                      event->buttons(), event->modifiers(), event->phase(), event->inverted() );
    QCoreApplication::sendEvent( target, &copy );
}

void FullscreenControlBarWidget::mousePressEvent( QMouseEvent *event ) { route( event, localPosOf( event ) ); }
void FullscreenControlBarWidget::mouseReleaseEvent( QMouseEvent *event ) { route( event, localPosOf( event ) ); }
void FullscreenControlBarWidget::mouseDoubleClickEvent( QMouseEvent *event ) { route( event, localPosOf( event ) ); }
void FullscreenControlBarWidget::mouseMoveEvent( QMouseEvent *event ) { route( event, localPosOf( event ) ); }
void FullscreenControlBarWidget::wheelEvent( QWheelEvent *event )
{
    if( !route( event, localPosOf( event ) ) ) forwardWheelToView( event );
}
