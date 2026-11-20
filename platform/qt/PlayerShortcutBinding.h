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
// PLAYER-STANDARD-SHORTCUT-MAP-1: bindings() is the ONE player key table. bindToWindow() applies
// it (the live shortcuts are exactly the table), FocusGuard keeps navigation keys with the input
// widget that has focus, and shortcutRows()/createShortcutsDialog() build Help > Keyboard
// Shortcuts from the live actions, so there is no second, hand-kept key list.
//
// Header-only, no Q_OBJECT (no moc).
#ifndef PLAYERSHORTCUTBINDING_H
#define PLAYERSHORTCUTBINDING_H

#include <QAbstractButton>
#include <QAbstractItemView>
#include <QAbstractSlider>
#include <QAbstractSpinBox>
#include <QAction>
#include <QApplication>
#include <QComboBox>
#include <QCoreApplication>
#include <QDebug>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDockWidget>
#include <QEvent>
#include <QHeaderView>
#include <QKeyEvent>
#include <QKeySequence>
#include <QLineEdit>
#include <QList>
#include <QMenu>
#include <QPlainTextEdit>
#include <QPointer>
#include <QShortcut>
#include <QStringList>
#include <QTableWidget>
#include <QTextEdit>
#include <QVBoxLayout>
#include <QWidget>

#include <algorithm>
#include <cmath>

namespace PlayerShortcutBinding
{

// Primary: the key the menu shows. Secondary: another permanent key. Alias: an old key kept
// until PLAYER-JKL-SHUTTLE-1 takes it over.
enum class KeyKind { Primary, Secondary, Alias };

struct KeyBinding
{
    QKeySequence sequence;
    KeyKind kind;
};

struct ActionBinding
{
    QString objectName;      // MainWindow.ui action name
    QList<KeyBinding> keys;  // in order; keys.first() is the Primary
};

inline QString aliasRetireNote()
{
    return QStringLiteral( "old key, retires with PLAYER-JKL-SHUTTLE-1" );
}

// THE player key table (standard NLE/player keys). Not bound here on purpose: Esc and Q
// (MainWindow QShortcuts), M (bad-pixel edit), I (Clip Info), Up/Down (PLAYER-MUTE-VOLUME-1),
// L, and K as play/pause (PLAYER-JKL-SHUTTLE-1).
inline QList<ActionBinding> bindings()
{
    QList<ActionBinding> table;
    table << ActionBinding{ QStringLiteral( "actionPlay" ),
                            { { QKeySequence( Qt::Key_Space ), KeyKind::Primary } } };
    QList<KeyBinding> fullscreen{ { QKeySequence( Qt::CTRL | Qt::Key_F ), KeyKind::Primary } };
#ifdef Q_OS_WIN
    fullscreen << KeyBinding{ QKeySequence( Qt::Key_F11 ), KeyKind::Secondary };
#endif
    // Qt::Key_Enter also matches numpad Enter (Qt::KeypadModifier): QShortcutMap retries a
    // keypad key without the modifier.
    fullscreen << KeyBinding{ QKeySequence( Qt::Key_Return ), KeyKind::Secondary }
               << KeyBinding{ QKeySequence( Qt::Key_Enter ), KeyKind::Secondary };
    table << ActionBinding{ QStringLiteral( "actionFullscreen" ), fullscreen };
    table << ActionBinding{ QStringLiteral( "actionPreviousFrame" ),
                            { { QKeySequence( Qt::Key_Left ), KeyKind::Primary },
                              { QKeySequence( Qt::Key_Comma ), KeyKind::Secondary } } };
    table << ActionBinding{ QStringLiteral( "actionNextFrame" ),
                            { { QKeySequence( Qt::Key_Right ), KeyKind::Primary },
                              { QKeySequence( Qt::Key_Period ), KeyKind::Secondary } } };
    table << ActionBinding{ QStringLiteral( "actionSeekBackwardOneSecond" ),
                            { { QKeySequence( Qt::SHIFT | Qt::Key_Left ), KeyKind::Primary } } };
    table << ActionBinding{ QStringLiteral( "actionSeekForwardOneSecond" ),
                            { { QKeySequence( Qt::SHIFT | Qt::Key_Right ), KeyKind::Primary } } };
    table << ActionBinding{ QStringLiteral( "actionGoto_First_Frame" ),
                            { { QKeySequence( Qt::Key_Home ), KeyKind::Primary },
                              { QKeySequence( Qt::Key_X ), KeyKind::Alias } } };
    table << ActionBinding{ QStringLiteral( "actionGoto_Last_Frame" ),
                            { { QKeySequence( Qt::Key_End ), KeyKind::Primary } } };
    table << ActionBinding{ QStringLiteral( "actionLoop" ),
                            { { QKeySequence( Qt::CTRL | Qt::Key_L ), KeyKind::Primary } } };
    table << ActionBinding{ QStringLiteral( "actionPrevious_Clip" ),
                            { { QKeySequence( Qt::Key_PageUp ), KeyKind::Primary },
                              { QKeySequence( Qt::Key_J ), KeyKind::Alias } } };
    table << ActionBinding{ QStringLiteral( "actionNext_Clip" ),
                            { { QKeySequence( Qt::Key_PageDown ), KeyKind::Primary },
                              { QKeySequence( Qt::Key_K ), KeyKind::Alias } } };
    // F stays Zoom: fit (the grading-inspection key wins over a player-only meaning).
    table << ActionBinding{ QStringLiteral( "actionZoomFit" ),
                            { { QKeySequence( Qt::Key_F ), KeyKind::Primary } } };
    table << ActionBinding{ QStringLiteral( "actionZoom100" ),
                            { { QKeySequence( Qt::CTRL | Qt::Key_0 ), KeyKind::Primary } } };
    return table;
}

inline QList<QKeySequence> sequencesOf( const ActionBinding &binding )
{
    QList<QKeySequence> sequences;
    for( const KeyBinding &key : binding.keys ) sequences << key.sequence;
    return sequences;
}

// Object names of the table actions, in table order (derived, never listed by hand).
inline QStringList liveActionNames()
{
    QStringList names;
    for( const ActionBinding &binding : bindings() ) names << binding.objectName;
    return names;
}

// Adds each table action to `window` and sets its shortcuts to exactly the table's sequences.
// uic parents every QAction directly to the main window, so the lookup is limited to direct
// children (a dialog's same-named action is never bound). Returns the number of table actions
// the window carries afterwards (already-carried ones count); warns for each name that is missing.
inline int bindToWindow( QWidget *window )
{
    if( !window ) return 0;
    int bound = 0;
    const QList<ActionBinding> table = bindings();
    for( const ActionBinding &binding : table )
    {
        QAction *action = window->findChild<QAction*>( binding.objectName, Qt::FindDirectChildrenOnly );
        if( !action )
        {
            qWarning() << "PlayerShortcutBinding: action not found:" << binding.objectName;
            continue;
        }
        const QList<QKeySequence> sequences = sequencesOf( binding );
        if( action->shortcuts() != sequences ) action->setShortcuts( sequences );
        if( !window->actions().contains( action ) ) window->addAction( action );
        ++bound;
    }
    return bound;
}

// Target frame of a one-second seek (Shift+Left/Right): `direction` (-1 or +1) times the clip's
// frame rate rounded to whole frames (at least 1), from `position`, clamped to [minimum, maximum].
// All in double, so a huge or non-finite rate from clip metadata never overflows an int
// (adversary pass 2 A1: an accepted 2147483648 fps clip).
inline int oneSecondSeekTarget( int position, double fps, int direction, int minimum, int maximum )
{
    double step = ( std::isfinite( fps ) && fps > 0.0 ) ? std::round( fps ) : 1.0;
    if( step < 1.0 ) step = 1.0;
    if( maximum < minimum ) maximum = minimum;
    const double target = static_cast<double>( position ) + ( direction < 0 ? -step : step );
    const double clamped = std::min( std::max( target, static_cast<double>( minimum ) ), static_cast<double>( maximum ) );
    return static_cast<int>( clamped );
}

// Keeps navigation keys with the focused input widget: the player table binds Left/Right/Home/
// End/PgUp/PgDown/Return/Enter, which spin boxes, sliders, lists, combo boxes and text fields
// need for themselves. Installed on qApp; acts only on QEvent::ShortcutOverride (accepting it
// makes Qt deliver the key to the widget instead of trying the shortcut map) and never filters
// the event out. The exempt widget (the seek bar) and the viewer get the player keys.
class FocusGuard : public QObject
{
public:
    explicit FocusGuard( QWidget *exempt, QObject *parent = nullptr )
        : QObject( parent ), m_exempt( exempt ) {}

    static bool guardedKey( int key, Qt::KeyboardModifiers modifiers )
    {
        if( modifiers & ~( Qt::ShiftModifier | Qt::KeypadModifier ) ) return false;
        switch( key )
        {
        case Qt::Key_Left: case Qt::Key_Right: case Qt::Key_Up: case Qt::Key_Down:
        case Qt::Key_Home: case Qt::Key_End: case Qt::Key_PageUp: case Qt::Key_PageDown:
        case Qt::Key_Return: case Qt::Key_Enter:
            return true;
        default:
            return false;
        }
    }

    static bool isInputWidget( const QWidget *widget )
    {
        return qobject_cast<const QLineEdit*>( widget )
            || qobject_cast<const QTextEdit*>( widget )
            || qobject_cast<const QPlainTextEdit*>( widget )
            || qobject_cast<const QAbstractSpinBox*>( widget )
            || qobject_cast<const QAbstractSlider*>( widget )
            || qobject_cast<const QAbstractItemView*>( widget )
            || qobject_cast<const QComboBox*>( widget );
    }

    // True when `focus` (or an ancestor up to its window) is an input widget other than
    // `exempt` and the key is one the guard keeps for it.
    static bool focusOwnsKey( QWidget *focus, int key, Qt::KeyboardModifiers modifiers, const QWidget *exempt )
    {
        if( !focus || !guardedKey( key, modifiers ) ) return false;
        for( QWidget *w = focus; w; w = w->parentWidget() )
        {
            if( w != exempt && isInputWidget( w ) ) return true;
            if( w->isWindow() ) break;
        }
        return false;
    }

    bool eventFilter( QObject *watched, QEvent *event ) override
    {
        Q_UNUSED( watched );
        if( event->type() != QEvent::ShortcutOverride ) return false;
        QKeyEvent *keyEvent = static_cast<QKeyEvent*>( event );
        if( focusOwnsKey( QApplication::focusWidget(), keyEvent->key(), keyEvent->modifiers(), m_exempt.data() ) )
            keyEvent->accept();
        return false;
    }

private:
    QPointer<QWidget> m_exempt;
};

// One row of Help > Keyboard Shortcuts.
struct ShortcutRow
{
    QString action;  // action text without mnemonics
    QString keys;    // live keys, NativeText
    QString note;
};

inline QString plainActionText( const QString &text )
{
    QString out;
    for( int i = 0; i < text.size(); ++i )
    {
        if( text.at( i ) == QLatin1Char( '&' ) )
        {
            if( i + 1 < text.size() && text.at( i + 1 ) == QLatin1Char( '&' ) )
            {
                out += QLatin1Char( '&' );
                ++i;
            }
            continue;
        }
        out += text.at( i );
    }
    return out;
}

inline QString keysText( const QList<QKeySequence> &sequences )
{
    QStringList parts;
    for( const QKeySequence &sequence : sequences ) parts << sequence.toString( QKeySequence::NativeText );
    return parts.join( QStringLiteral( " or " ) );
}

// True when `object` lives in `window` itself: walking up its parents reaches `window` without
// passing another top-level window (a dialog has its own shortcuts). Menus (popup windows) and
// floating dock widgets still belong to the window that owns them.
inline bool ownedByWindow( const QObject *object, const QWidget *window )
{
    for( const QObject *o = object; o; o = o->parent() )
    {
        if( o == window ) return true;
        const QWidget *w = qobject_cast<const QWidget*>( o );
        if( w && w->isWindow() && !qobject_cast<const QMenu*>( w ) && !qobject_cast<const QDockWidget*>( w ) )
            return false;
    }
    return false;
}

// Name for a key that has no action text: its description, else its owner's.
inline QString describedName( const QString &primary, const QString &secondary, const QObject *fallback )
{
    if( !primary.isEmpty() ) return primary;
    if( !secondary.isEmpty() ) return secondary;
    if( fallback && !fallback->objectName().isEmpty() ) return fallback->objectName();
    return fallback ? QString::fromLatin1( fallback->metaObject()->className() ) : QString();
}

// Rows from the window's LIVE key carriers, so nothing is kept by hand: section 1 is the table
// actions in table order; section 2 every other visible QAction (menus and submenus included)
// and every button with a shortcut, by name; section 3 every QShortcut in the window (its
// whatsThis(), else its owner's name), by name. Objects inside a dialog are not listed.
inline QList<ShortcutRow> shortcutRows( QWidget *window )
{
    QList<ShortcutRow> rows;
    if( !window ) return rows;

    QStringList tableNames;
    for( const ActionBinding &binding : bindings() )
    {
        tableNames << binding.objectName;
        QAction *action = window->findChild<QAction*>( binding.objectName, Qt::FindDirectChildrenOnly );
        if( !action ) continue;
        QStringList aliases;
        for( const KeyBinding &key : binding.keys )
            if( key.kind == KeyKind::Alias && action->shortcuts().contains( key.sequence ) )
                aliases << key.sequence.toString( QKeySequence::NativeText );
        rows << ShortcutRow{ plainActionText( action->text() ), keysText( action->shortcuts() ),
                             aliases.isEmpty() ? QString()
                                               : QStringLiteral( "%1: %2" ).arg( aliases.join( QStringLiteral( ", " ) ),
                                                                                aliasRetireNote() ) };
    }

    QList<ShortcutRow> others;
    const QList<QAction*> actions = window->findChildren<QAction*>();
    for( QAction *action : actions )
    {
        if( tableNames.contains( action->objectName() ) && action->parent() == window ) continue;
        if( action->shortcuts().isEmpty() || !action->isVisible() || !ownedByWindow( action, window ) ) continue;
        others << ShortcutRow{ describedName( plainActionText( action->text() ), action->toolTip(), action ),
                               keysText( action->shortcuts() ), QString() };
    }
    const QList<QAbstractButton*> buttons = window->findChildren<QAbstractButton*>();
    for( QAbstractButton *button : buttons )
    {
        if( button->shortcut().isEmpty() || !ownedByWindow( button, window ) ) continue;
        others << ShortcutRow{ describedName( button->toolTip(), plainActionText( button->text() ), button ),
                               keysText( { button->shortcut() } ), QString() };
    }
    std::stable_sort( others.begin(), others.end(), []( const ShortcutRow &a, const ShortcutRow &b )
                      { return a.action.compare( b.action, Qt::CaseInsensitive ) < 0; } );
    rows << others;

    QList<ShortcutRow> described;
    const QList<QShortcut*> shortcuts = window->findChildren<QShortcut*>();
    for( QShortcut *shortcut : shortcuts )
    {
        if( shortcut->keys().isEmpty() || !ownedByWindow( shortcut, window ) ) continue;
        described << ShortcutRow{ describedName( shortcut->whatsThis(), QString(), shortcut->parent() ),
                                  keysText( shortcut->keys() ), QString() };
    }
    std::stable_sort( described.begin(), described.end(), []( const ShortcutRow &a, const ShortcutRow &b )
                      { return a.action.compare( b.action, Qt::CaseInsensitive ) < 0; } );
    rows << described;
    return rows;
}

inline QDialog *createShortcutsDialog( QWidget *window )
{
    const QList<ShortcutRow> rows = shortcutRows( window );
    QDialog *dialog = new QDialog( window );
    dialog->setObjectName( QStringLiteral( "keyboardShortcutsDialog" ) );
    dialog->setWindowTitle( QCoreApplication::translate( "PlayerShortcutBinding", "Keyboard Shortcuts" ) );

    QTableWidget *table = new QTableWidget( rows.size(), 3, dialog );
    table->setObjectName( QStringLiteral( "keyboardShortcutsTable" ) );
    table->setHorizontalHeaderLabels( QStringList()
        << QCoreApplication::translate( "PlayerShortcutBinding", "Action" )
        << QCoreApplication::translate( "PlayerShortcutBinding", "Keys" )
        << QCoreApplication::translate( "PlayerShortcutBinding", "Note" ) );
    table->setEditTriggers( QAbstractItemView::NoEditTriggers );
    table->setSelectionBehavior( QAbstractItemView::SelectRows );
    table->verticalHeader()->hide();
    for( int r = 0; r < rows.size(); ++r )
    {
        const QString cells[3] = { rows.at( r ).action, rows.at( r ).keys, rows.at( r ).note };
        for( int c = 0; c < 3; ++c )
        {
            QTableWidgetItem *item = new QTableWidgetItem( cells[c] );
            item->setFlags( Qt::ItemIsEnabled | Qt::ItemIsSelectable );
            table->setItem( r, c, item );
        }
    }
    table->horizontalHeader()->setSectionResizeMode( QHeaderView::ResizeToContents );
    table->horizontalHeader()->setStretchLastSection( true );

    QDialogButtonBox *buttons = new QDialogButtonBox( QDialogButtonBox::Close, dialog );
    QObject::connect( buttons, &QDialogButtonBox::rejected, dialog, &QDialog::reject );

    QVBoxLayout *layout = new QVBoxLayout( dialog );
    layout->addWidget( table );
    layout->addWidget( buttons );
    dialog->resize( 620, 520 );
    return dialog;
}

} // namespace PlayerShortcutBinding

#endif // PLAYERSHORTCUTBINDING_H
