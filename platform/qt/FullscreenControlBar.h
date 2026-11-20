/*!
 * \file FullscreenControlBar.h
 * \brief PLAYER-FULLSCREEN-CONTROL-BAR-1: the bar's rasteriser (shared by the GL window and the CPU viewer) and the
 *        CPU viewer's bar widget. The decisions (layout, hit test, raster cache, input) are FullscreenControlBarPolicy.h.
 *
 * The CPU bar is a child of the QGraphicsView itself, never of its viewport: GraphicsZoomView::scrollContentsBy
 * scrolls viewport children. It is opaque (WA_OpaquePaintEvent, WA_NoSystemBackground) and paints one cached
 * QPixmap, so its repaint never makes the viewport repaint under it (a partial GraphicsZoomView::paintEvent would
 * credit a pending frame). It takes every mouse press, release, double-click and move (WA_NoMousePropagation), so a
 * double-click on it never reaches the viewer's fullscreen toggle, and every wheel except one during a gesture begun
 * elsewhere, which it hands to the viewport. Its geometry follows the view on a resize and on a device-pixel-ratio
 * or screen change, and a position on it maps to the viewer through the layout it paints. Hidden is hide(): no paint
 * at all. No Q_OBJECT (no moc).
 */

#ifndef FULLSCREENCONTROLBAR_H
#define FULLSCREENCONTROLBAR_H

#include "FullscreenControlBarPolicy.h"

#include <QImage>
#include <QPixmap>
#include <QWidget>

#include <functional>

/*! \brief The whole bar (chrome, icons for feed.playing, and feed.timecode) at l.bar's size, device pixels.
 *         Opaque, Format_RGBA8888_Premultiplied. The playhead is not in it. */
QImage renderFullscreenControlBar( const fullscreen_bar::Layout &l, const fullscreen_bar::Feed &feed );

/*! \brief The timecode cell alone (l.timecode's size), identical to the same cell of renderFullscreenControlBar. */
QImage renderFullscreenControlBarTimecode( const fullscreen_bar::Layout &l, const QString &timecode );

/*! \brief The playhead colour (drawn as one rectangle by each surface). */
QColor fullscreenControlBarPlayheadColor( void );

class FullscreenControlBarWidget : public QWidget
{
public:
    /*! \brief A mouse event on the bar, with the layout and the device-pixel position on the whole viewer. Returns
     *         whether the bar takes it. A wheel it does not take (a button pressed elsewhere is held) is handed to the
     *         view's viewport, as it would reach it with no bar; every other event stays the bar's. */
    using InputHook = std::function<bool( const fullscreen_bar::Layout &layout, QEvent *event, const QPointF &devicePos )>;

    explicit FullscreenControlBarWidget( QWidget *view );

    void setInputHook( InputHook hook ) { m_input = std::move( hook ); }

    /*! \brief Shows (raised above the viewport, at the bottom of the view) or hides the bar. */
    void setBarShown( bool shown, qint64 nowMs );
    /*! \brief The latest values; repaints only when shown and something changed. */
    void setFeed( const fullscreen_bar::Feed &feed );

    const fullscreen_bar::Presenter &presenter( void ) const { return m_presenter; }
    /*! \brief Starts a fullscreen session on the bar's counters (Presenter::beginSession). */
    void beginSession( qint64 nowMs ) { m_presenter.beginSession( nowMs ); }
    /*! \brief The layout on the whole view, device pixels. */
    fullscreen_bar::Layout viewLayout( void ) const;
    /*! \brief True iff the bar is shown and the global position lies on it. */
    bool containsGlobal( const QPoint &globalPos ) const;

protected:
    bool eventFilter( QObject *watched, QEvent *event ) override;
    void paintEvent( QPaintEvent *event ) override;
    void mousePressEvent( QMouseEvent *event ) override;
    void mouseReleaseEvent( QMouseEvent *event ) override;
    void mouseDoubleClickEvent( QMouseEvent *event ) override;
    void mouseMoveEvent( QMouseEvent *event ) override;
    void wheelEvent( QWheelEvent *event ) override;

private:
    void followView( void );
    bool route( QEvent *event, const QPointF &localPos );
    void forwardWheelToView( QWheelEvent *event );

    QWidget *m_view;
    fullscreen_bar::Presenter m_presenter;
    QPixmap m_pixmap;
    InputHook m_input;
};

#endif // FULLSCREENCONTROLBAR_H
