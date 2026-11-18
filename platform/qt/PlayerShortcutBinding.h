// PLAYER-FULLSCREEN-SHORTCUTS-LIVE-1: keep the player shortcuts live while the menu bar and
// toolbar are hidden (fullscreen hides both, see MainWindow::on_actionFullscreen_triggered()).
//
// Qt rule: a Qt::WindowShortcut QAction fires only through a VISIBLE, enabled widget the action
// was added to, inside the active window. A QMenu counts only through its own menu action (so
// through the menu bar that shows it). With the menu bar and toolbar hidden, the action has no
// visible carrier and its keys are dead. Adding the action to the top-level window itself gives
// it a carrier that is visible whenever the window is. (A macOS native menu bar counts as
// visible even when hidden, so there the keys were never dead; binding is harmless there.)
//
// The shortcut context is deliberately left as it is (Qt::WindowShortcut, the QAction default).
// An application-wide context would fire while a non-modal dialog (for example StatusDialog)
// has focus; tests/console/test_playback_fullscreen_ui_wiring.cpp pins that this file never
// changes the context. An action the window already carries is left where it is
// (QWidget::addAction() would remove and re-append it, reordering actions()). One action is one
// shortcut-map entry however many widgets carry it, so windowed behaviour does not change and
// no key becomes ambiguous.
//
// Header-only, no Q_OBJECT (no moc).
#ifndef PLAYERSHORTCUTBINDING_H
#define PLAYERSHORTCUTBINDING_H

#include <QAction>
#include <QDebug>
#include <QStringList>
#include <QWidget>

namespace PlayerShortcutBinding
{

// Object names from MainWindow.ui, in the order the card lists them.
inline QStringList liveActionNames()
{
    return QStringList()
        << QStringLiteral( "actionPlay" )               // Space
        << QStringLiteral( "actionGoto_First_Frame" )   // X
        << QStringLiteral( "actionPreviousFrame" )      // ,
        << QStringLiteral( "actionNextFrame" )          // .
        << QStringLiteral( "actionZoomFit" )            // F
        << QStringLiteral( "actionZoom100" )            // Ctrl+0
        << QStringLiteral( "actionFullscreen" );        // Ctrl+F (+ F11 on Windows)
}

// Adds each live action to `window`. uic parents every QAction directly to the main window,
// so the lookup is limited to direct children (a dialog's same-named action is never bound).
// Returns the number of live actions the window carries afterwards (already-carried ones count);
// warns for each name that is missing.
inline int bindToWindow( QWidget *window )
{
    if( !window ) return 0;
    int bound = 0;
    const QStringList names = liveActionNames();
    for( const QString &name : names )
    {
        QAction *action = window->findChild<QAction*>( name, Qt::FindDirectChildrenOnly );
        if( !action )
        {
            qWarning() << "PlayerShortcutBinding: action not found:" << name;
            continue;
        }
        if( !window->actions().contains( action ) ) window->addAction( action );
        ++bound;
    }
    return bound;
}

} // namespace PlayerShortcutBinding

#endif // PLAYERSHORTCUTBINDING_H
