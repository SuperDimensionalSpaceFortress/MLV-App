#pragma once

// PLAYER-GL-WINDOW-INPUT-PARITY-1: an event filter installed on the GL display window
// (GpuDisplayWindow, a QOpenGLWindow inside a window container) that routes the viewer's
// mouse input to the same MainWindow slots the QGraphicsView path reaches through its scene:
// white-balance pick, bad-pixel pick, the right-click viewer menu, and drop-to-open.
// Picks are mapped with gpu_window_input::pickSlotPixel, through the picture rect the window's
// last paintGL drew (the drawnPicture hook), into the scene integers whose conversion in the
// unchanged slots (the pickBasis hook) selects the raw pixel the picture shows. A drop is handed over as the scene's filesDropped list (openMlvSet),
// not through MainWindow::dropEvent, so it opens what the same drop on the scene opens.
// Header-only, no Q_OBJECT (no moc). The filter is a child of the window, so it lives as
// long as the window does.

#include "GpuWindowViewerInput.h"

#include <QDragEnterEvent>
#include <QDragMoveEvent>
#include <QDropEvent>
#include <QMimeData>
#include <QMouseEvent>
#include <QWindow>

#include <functional>
#include <utility>

namespace gpu_window_input
{

class Forwarder : public QObject
{
public:
    struct Hooks
    {
        // How the picker slots convert the scene integers they are handed (fit flag, scene rect, clip
        // size, current stretch), read at the click; see pickSlotPixel. Unset = no usable basis: no pick.
        std::function<PickBasis()> pickBasis;
        // The picture rect the window's last paintGL drew, in window logical pixels
        // (drawnPictureRect of GpuDisplayWindow::lastPaintedPicture()); see pickPixel. Unset or
        // empty = nothing drawn yet: a pick is refused, never guessed.
        std::function<QRectF()> drawnPicture;
        std::function<bool()> wbArmed;
        std::function<bool()> bpArmed;
        std::function<void(int, int)> pickWhiteBalance;
        std::function<void(int, int)> pickBadPixel;
        std::function<void(const QPoint &)> contextMenuAtGlobal;
        std::function<void(QDragEnterEvent *)> dragEnter;
        // Receives the drop as the scene's filesDropped(QStringList) does (localPathsFromUrls).
        std::function<void(const QStringList &)> openFiles;
    };

    Forwarder( QWindow *window, Hooks hooks )
        : QObject( window )
        , m_window( window )
        , m_hooks( std::move( hooks ) )
    {
    }

    bool eventFilter( QObject *watched, QEvent *event ) override
    {
        if( !m_window || watched != m_window || !event ) return false;

        const QEvent::Type type = event->type();
        Qt::MouseButton button = Qt::NoButton;
        QPointF pos;
        QPoint global;
        const bool isMouse = type == QEvent::MouseButtonPress || type == QEvent::MouseButtonRelease;
        if( isMouse )
        {
            const QMouseEvent *mouse = static_cast<const QMouseEvent *>( event );
            button = mouse->button();
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
            pos = mouse->position();
            global = mouse->globalPosition().toPoint();
#else
            pos = mouse->localPos();
            global = mouse->globalPos();
#endif
        }
        else if( type != QEvent::DragEnter && type != QEvent::DragMove && type != QEvent::Drop )
        {
            return false;
        }

        const bool wb = m_hooks.wbArmed ? m_hooks.wbArmed() : false;
        const bool bp = m_hooks.bpArmed ? m_hooks.bpArmed() : false;
        switch( route( type, button, wb, bp, contextMenuOnReleaseDefault() ) )
        {
        case Route::PickWhiteBalance:
        case Route::PickBadPixel:
        {
            const PickBasis basis = m_hooks.pickBasis ? m_hooks.pickBasis() : PickBasis();
            const QRectF drawn = m_hooks.drawnPicture ? m_hooks.drawnPicture() : QRectF();
            int x = 0;
            int y = 0;
            if( pickSlotPixel( pos, drawn, basis, &x, &y ) )
            {
                const bool white = wb;
                if( white && m_hooks.pickWhiteBalance ) m_hooks.pickWhiteBalance( x, y );
                else if( !white && m_hooks.pickBadPixel ) m_hooks.pickBadPixel( x, y );
            }
            return true;
        }
        case Route::ContextMenu:
            // Consuming the button event stops Qt from synthesising a second context-menu event.
            if( m_hooks.contextMenuAtGlobal ) m_hooks.contextMenuAtGlobal( global );
            return true;
        case Route::AcceptDrag:
            if( type == QEvent::DragEnter )
            {
                if( m_hooks.dragEnter ) m_hooks.dragEnter( static_cast<QDragEnterEvent *>( event ) );
            }
            else
            {
                QDragMoveEvent *move = static_cast<QDragMoveEvent *>( event );
                if( move->mimeData() && move->mimeData()->hasUrls() ) move->acceptProposedAction();
            }
            return true;
        case Route::Drop:
        {
            // As GraphicsPickerScene::dropEvent: every local path, then accept.
            QDropEvent *drop = static_cast<QDropEvent *>( event );
            const QStringList paths = drop->mimeData() ? localPathsFromUrls( drop->mimeData()->urls() ) : QStringList();
            if( m_hooks.openFiles ) m_hooks.openFiles( paths );
            drop->acceptProposedAction();
            return true;
        }
        case Route::None:
        default:
            return false;
        }
    }

private:
    QWindow *m_window;
    Hooks m_hooks;
};

} // namespace gpu_window_input
