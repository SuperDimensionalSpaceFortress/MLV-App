/*!
 * \file GraphicsZoomView.h
 * \author masc4ii
 * \copyright 2017
 * \brief A QGraphicsView without scrolling but with zoom on mousewheel or y-axis on trackpad
 */

#ifndef GRAPHICSZOOMVIEW_H
#define GRAPHICSZOOMVIEW_H

#include <QGraphicsView>
#include <QWheelEvent>
#include <QWidget>
#include <Qt>
#include <QDebug>
#include <QShortcut>

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

    //Wall time of each viewport paint (playback_smoke.gui_present_summary)
    struct PaintTiming
    {
        quint64 paints = 0;
        double sumMs = 0.0;
        double maxMs = 0.0;
    };
    PaintTiming paintTiming(void) const;
    void resetPaintTiming(void);

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
    PaintTiming m_paintTiming;
    bool m_isZoomEnabled;
    PickerState m_pickerState;
    bool m_isMousePressed;
    QShortcut *m_pZoomInSc;
    QShortcut *m_pZoomOutSc;
};

#endif // GRAPHICSZOOMVIEW_H
