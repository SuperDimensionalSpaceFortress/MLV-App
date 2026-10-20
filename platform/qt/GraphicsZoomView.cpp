/*!
 * \file GraphicsZoomView.cpp
 * \author masc4ii
 * \copyright 2017
 * \brief A QGraphicsView without scrolling but with zoom on mousewheel or y-axis on trackpad
 */

#include "GraphicsZoomView.h"
#include "LookLabelOverlay.h"
#include "LookLabelPolicy.h"
#include <QCursor>
#include <QPainter>
#include <QPixmap>

//Constructor
GraphicsZoomView::GraphicsZoomView(QWidget *parent) :
    QGraphicsView(parent)
{
    setTransformationAnchor(QGraphicsView::AnchorUnderMouse);
    setAcceptDrops( true );
    m_isZoomEnabled = false;
    m_pickerState = NoPicker;
    m_isMousePressed = false;

    m_pZoomInSc = new QShortcut( QKeySequence::ZoomIn, this, SLOT( shortCutZoomIn() ) );
    m_pZoomOutSc = new QShortcut( QKeySequence::ZoomOut, this, SLOT( shortCutZoomOut() ) );
}

//Destructor
GraphicsZoomView::~GraphicsZoomView()
{
    delete m_pZoomInSc;
    delete m_pZoomOutSc;
}

//En-/disable zoom on mouse wheel
void GraphicsZoomView::setZoomEnabled(bool on)
{
    m_isZoomEnabled = on;
}

//return if zoom on mouse wheel is en-/disabled
bool GraphicsZoomView::isZoomEnabled()
{
    return m_isZoomEnabled;
}

//LOOK-FULLSCREEN-PARITY-1: set the corner look label ("" hides it); repaints once
void GraphicsZoomView::setLookLabel(const QString &text)
{
    if( m_lookLabelText == text ) return;
    m_lookLabelText = text;
    viewport()->update();
}

//Blit the cached look label in the viewport's bottom-right corner. The frame's own repaints
//touch only the picture, so the label is redrawn when its region is exposed, not per frame.
void GraphicsZoomView::drawForeground(QPainter *painter, const QRectF &rect)
{
    QGraphicsView::drawForeground( painter, rect );
    m_lookLabelDrawnText.clear();
    if( m_lookLabelText.isEmpty() ) return;

    const qreal dpr = viewport()->devicePixelRatioF();
    const QSize deviceSize( qRound( viewport()->width() * dpr ), qRound( viewport()->height() * dpr ) );
    const int fontPx = look_label::fontPixels( deviceSize.height() );
    if( m_lookLabelPixmapText != m_lookLabelText || m_lookLabelPixmapFontPx != fontPx || m_lookLabelPixmapDpr != dpr )
    {
        m_lookLabelPixmap = QPixmap::fromImage( renderLookLabelOverlayImage( m_lookLabelText, fontPx ) );
        m_lookLabelPixmap.setDevicePixelRatio( dpr );
        m_lookLabelPixmapText = m_lookLabelText;
        m_lookLabelPixmapFontPx = fontPx;
        m_lookLabelPixmapDpr = dpr;
    }
    if( m_lookLabelPixmap.isNull() ) return;
    const QRect deviceRect = look_label::overlayRect( deviceSize, m_lookLabelPixmap.size(),
                                                      look_label::marginPixels( fontPx ) );
    if( deviceRect.isEmpty() ) return;
    painter->save();
    painter->resetTransform();   // viewport coordinates, not scene coordinates
    painter->drawPixmap( QPointF( deviceRect.x() / dpr, deviceRect.y() / dpr ), m_lookLabelPixmap );
    painter->restore();
    m_lookLabelDrawnText = m_lookLabelText;
}

//A scroll moves the viewport's pixels, label included: repaint so it stays in the corner
void GraphicsZoomView::scrollContentsBy(int dx, int dy)
{
    QGraphicsView::scrollContentsBy( dx, dy );
    if( !m_lookLabelText.isEmpty() ) viewport()->update();
}

//Reset the zoom to exact 100%
void GraphicsZoomView::resetZoom()
{
    //resetTransform(); //->kills anchor
    //Calculating this does not kill anchor -> latest anchor will be used
    qreal percentZoom = 100.0;
    qreal targetScale = (qreal)percentZoom / 100.0;
    qreal scaleFactor = targetScale / transform().m11();
    scale( scaleFactor, scaleFactor );
}

//Set white balance picker active
void GraphicsZoomView::setWbPickerActive(bool on)
{
    if( on )
    {
        m_pickerState = WbPicker;
        setPipetteCursor();
    }
    else
    {
        m_pickerState = NoPicker;
        viewport()->setCursor( Qt::OpenHandCursor );
    }
}

//Set bad pixel picker active
void GraphicsZoomView::setBpPickerActive(bool on)
{
    if( on )
    {
        m_pickerState = BpPicker;
        viewport()->setCursor( Qt::CrossCursor );
    }
    else
    {
        m_pickerState = NoPicker;
        viewport()->setCursor( Qt::OpenHandCursor );
    }
}

//Set cross cursor active
void GraphicsZoomView::setCrossCursorActive(bool on)
{
    if( on ) viewport()->setCursor( Qt::CrossCursor );
    else viewport()->setCursor( Qt::OpenHandCursor );
}

//Shortcut Zoom In
void GraphicsZoomView::shortCutZoomIn()
{
    //If disabled, do nothing
    if( !m_isZoomEnabled ) return;

    //else zoom
    setTransformationAnchor( QGraphicsView::AnchorUnderMouse) ;
    // Scale the view / do the zoom
    double scaleFactor = 1.5;
    // Zoom in
    scale( scaleFactor, scaleFactor );
}

//Shortcut Zoom Out
void GraphicsZoomView::shortCutZoomOut()
{
    //If disabled, do nothing
    if( !m_isZoomEnabled ) return;

    //else zoom
    setTransformationAnchor( QGraphicsView::AnchorUnderMouse );
    // Scale the view / do the zoom
    double scaleFactor = 1.5;
    // Zoom in
    scale( 1.0 / scaleFactor, 1.0 / scaleFactor );
}

//Methods for changing the cursor
#if QT_VERSION < QT_VERSION_CHECK(6, 0, 0)
void GraphicsZoomView::enterEvent(QEvent *event)
#else
void GraphicsZoomView::enterEvent(QEnterEvent *event)
#endif
{
    QGraphicsView::enterEvent(event);
    //viewport()->setCursor(QCursor(m_cursorPixmap,0,31));
}

//Mouse was pressed
void GraphicsZoomView::mousePressEvent(QMouseEvent *event)
{
    QGraphicsView::mousePressEvent(event);
    m_isMousePressed = true;
    //viewport()->setCursor(QCursor(m_cursorPixmap,0,31));
    if( m_pickerState == WbPicker )
    {
        //m_pickerState = false; //Needed for single click wb picking
        setPipetteCursor(); //Needed for wb picking until deactivation
        emit wbPicked( event->pos().x(), event->pos().y() );
    }
    else if( m_pickerState == BpPicker )
    {
        viewport()->setCursor( Qt::CrossCursor ); //Needed for bad pixel picking until deactivation
        emit bpPicked( event->pos().x(), event->pos().y() );
    }
}

//Mouse was released
void GraphicsZoomView::mouseReleaseEvent(QMouseEvent *event)
{
    m_isMousePressed = false;
    QGraphicsView::mouseReleaseEvent(event);
    //viewport()->setCursor(QCursor(m_cursorPixmap,0,31));

    //Needed for wb picking until deactivation
    if( m_pickerState == WbPicker )
    {
        setPipetteCursor();
    }
    //Needed for bad pixel picking until deactivation
    else if( m_pickerState == BpPicker )
    {
        viewport()->setCursor( Qt::CrossCursor );
    }
}

//The mousewheel event
void GraphicsZoomView::wheelEvent(QWheelEvent *event)
{
    //If disabled, do nothing
    if( !m_isZoomEnabled ) return;

    //else zoom
    setTransformationAnchor(QGraphicsView::AnchorUnderMouse);
    // Scale the view / do the zoom
    double scaleFactor = 1.05;
    if(event->angleDelta().y() > 0)
    {
        // Zoom in
        scale(scaleFactor, scaleFactor);
    }
    else if(event->angleDelta().y() < 0)
    {
        // Zooming out
        scale(1.0 / scaleFactor, 1.0 / scaleFactor);
    }
    else
    {
        //do nothing
    }
}

//Show the wb picker icon as cursor
void GraphicsZoomView::setPipetteCursor()
{
    //Calc cursor here,to make it work when monitor changed on runtime to retina
    QPixmap cursorPixmap = QPixmap( ":/RetinaIMG/RetinaIMG/Actions-color-picker-icon.png" )
                                   .scaled( 32 * devicePixelRatio(),
                                            32 * devicePixelRatio(),
                                            Qt::KeepAspectRatio, Qt::SmoothTransformation);
    cursorPixmap.setDevicePixelRatio( devicePixelRatio() );
    viewport()->setCursor(QCursor(cursorPixmap,4,27));
}
