#pragma once

// PLAYER-GL-WINDOW-INPUT-PARITY-1: an event filter installed on the GL display window
// (GpuDisplayWindow, a QOpenGLWindow inside a window container) that routes the viewer's
// mouse input to the same MainWindow slots the QGraphicsView path reaches through its scene:
// white-balance pick, bad-pixel pick, the right-click viewer menu, and drop-to-open.
// Picks are mapped from window to scene coordinates with gpu_window_input::windowToScene,
// the input mirror of paintGL's letterbox fit; the slots convert scene coordinates as before.
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
        std::function<QSizeF()> sceneSize;
        std::function<bool()> wbArmed;
        std::function<bool()> bpArmed;
        std::function<void(int, int)> pickWhiteBalance;
        std::function<void(int, int)> pickBadPixel;
        std::function<void(const QPoint &)> contextMenuAtGlobal;
        std::function<void(QDragEnterEvent *)> dragEnter;
        std::function<void(QDropEvent *)> drop;
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
            const QSizeF scene = m_hooks.sceneSize ? m_hooks.sceneSize() : QSizeF();
            QPointF p;
            if( windowToScene( pos, QSizeF( m_window->size() ), scene, &p ) )
            {
                const int x = int( std::floor( p.x() ) );
                const int y = int( std::floor( p.y() ) );
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
            if( m_hooks.drop ) m_hooks.drop( static_cast<QDropEvent *>( event ) );
            return true;
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
