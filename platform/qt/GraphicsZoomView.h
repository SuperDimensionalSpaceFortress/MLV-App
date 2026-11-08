/*!
 * \file GraphicsZoomView.h
 * \author masc4ii
 * \copyright 2017
 * \brief A QGraphicsView without scrolling but with zoom on mousewheel or y-axis on trackpad
 */

#ifndef GRAPHICSZOOMVIEW_H
#define GRAPHICSZOOMVIEW_H

#include <QGraphicsView>
#include <QPixmap>
#include <QString>
#include <QWheelEvent>
#include <QWidget>
#include <Qt>
#include <QDebug>
#include <QShortcut>
#include "PreviewPaintTiming.h"

class GraphicsZoomView : public QGraphicsView
{
    Q_OBJECT
public:
    explicit GraphicsZoomView(QWidget *parent = 0);
    ~GraphicsZoomView();
    void setZoomEnabled(bool on);
    bool isZoomEnabled(void);
    void resetZoom(void);
    void setWbPickerActive(bool on);
    void setBpPickerActive(bool on);
    void setCrossCursorActive(bool on);
    // LOOK-FULLSCREEN-PARITY-1: the "current look" label in the viewport's bottom-right corner
    // (empty = none). Rasterised once per change of text or size, blitted in drawForeground().
    void setLookLabel(const QString &text);
    // The label text the most recent viewport paint drew ("" when it drew none).
    QString lookLabelDrawnText(void) const { return m_lookLabelDrawnText; }

    //Wall time of every preview paint (playback_smoke.gui_present_summary)
    using PaintTiming = preview_paint_timing::Totals;
    PaintTiming paintTiming(void) const;
    void resetPaintTiming(void);

    // HFR-NATIVE-60FPS-FULLRES-1 r28 fix iteration 2 (adversary A1 on 4201a437): the CPU pixmap route's deferred paint. The playback
    // present notes the request serial it handed the pixmap item; the next viewport paint draws it. Whether a frame with that serial
    // (or a later one) was handed over and not yet painted.
    void notePixmapPresentSerial(quint64 serial) { m_pixmapPendingSerial = serial; }
    bool pixmapPaintPendingForSerial(quint64 serial) const
    {
        return serial != 0 && m_pixmapPendingSerial >= serial && m_pixmapPaintedSerial < serial;
    }

public slots:
    void shortCutZoomIn(void);
    void shortCutZoomOut(void);

signals:
    void wbPicked( int x, int y );
    void bpPicked( int x, int y );

protected:
    enum PickerState{ NoPicker, WbPicker, BpPicker };
#if QT_VERSION < QT_VERSION_CHECK(6, 0, 0)
    void enterEvent(QEvent *event);
#else
    void enterEvent(QEnterEvent *event);
#endif
    void mousePressEvent(QMouseEvent *event);
    void mouseReleaseEvent(QMouseEvent *event);
    void wheelEvent(QWheelEvent *event);
    void paintEvent(QPaintEvent *event) override;
    void setPipetteCursor();
    void drawForeground(QPainter *painter, const QRectF &rect) override;
    void scrollContentsBy(int dx, int dy) override;
    QString m_lookLabelText;
    QString m_lookLabelPixmapText;
    int m_lookLabelPixmapFontPx = 0;
    qreal m_lookLabelPixmapDpr = 0.0;
    QPixmap m_lookLabelPixmap;
    QString m_lookLabelDrawnText;
    quint64 m_pixmapPendingSerial = 0;
    quint64 m_pixmapPaintedSerial = 0;
    bool m_isZoomEnabled;
    PickerState m_pickerState;
    bool m_isMousePressed;
    QShortcut *m_pZoomInSc;
    QShortcut *m_pZoomOutSc;
};

#endif // GRAPHICSZOOMVIEW_H
