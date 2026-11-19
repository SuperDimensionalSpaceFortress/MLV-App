// PLAYER-DOUBLECLICK-FULLSCREEN-1: the decision behind "a left double-click on the viewer toggles
// fullscreen". Plain values only (QtCore), so the console tests exercise it without a window.
//
// The caller (MainWindow::eventFilter, through ViewerDoubleClickFilter.h) feeds it the viewer's
// tool state; this header never reads MainWindow and never touches a Qt event.
#ifndef VIEWERDOUBLECLICKPOLICY_H
#define VIEWERDOUBLECLICKPOLICY_H

#include <QObject>
#include <Qt>
#include <QtGlobal>

namespace viewer_dblclick
{

/*! \brief The persisted "Double-Click Viewer Toggles Fullscreen" setting. On by default. */
inline constexpr const char *kSettingsKey() { return "Playback/DoubleClickViewerTogglesFullscreen"; }
inline constexpr bool kSettingsDefault() { return true; }

/*! \brief What the viewer looks like at the moment of a press or a double-click. */
struct ViewerState
{
    bool enabled = true;                 // the setting
    bool clipLoaded = false;             // a clip is open
    bool wbArmed = false;                // white-balance picker (sticky across picks)
    bool bpArmed = false;                // bad-pixel edit (sticky)
    bool gradientPaintArmed = false;     // gradient paint (one-shot: off after the first release)
    bool gradientHandleHovered = false;  // the cursor is over the visible gradient handle
};

/*! \brief True while any viewer tool would claim the click for itself. */
inline bool anyToolArmed( const ViewerState &s )
{
    return s.wbArmed || s.bpArmed || s.gradientPaintArmed || s.gradientHandleHovered;
}

/*! \brief The gradient handle claims a click only while it is drawn on the active viewer: the
 *  QGraphicsView is the visible display, the handle item is visible, and the cursor is over it.
 *  On the GL path the view is hidden (GpuDisplayWindow::enable) but its scene item still reads
 *  isVisible() and tests the hidden view's geometry, so the view's own visibility must gate it
 *  (adversary r1 pass 3 A1). */
inline bool gradientHandleClaims( bool viewDisplayed, bool handleVisible, bool handleUnderMouse )
{
    return viewDisplayed && handleVisible && handleUnderMouse;
}

/*! \brief The object a mouse event was delivered to is one of the two display surfaces: the
 *  QGraphicsView viewport or the GL display window. Keyed on identity, never on underMouse(),
 *  so the double-click is handled once even though Qt also hands it to the top-level window. */
inline bool isViewerTarget( const QObject *watched, const QObject *viewport, const QObject *glWindow )
{
    if( !watched ) return false;
    return watched == viewport || ( glWindow && watched == glWindow );
}

/*! \brief The toggle rule: a plain left double-click, the setting on, a clip loaded, no tool armed
 *  now, and none armed at the first press of this double-click. */
inline bool shouldToggle( Qt::MouseButton button, Qt::KeyboardModifiers modifiers, const ViewerState &s, bool armedAtFirstPress )
{
    return button == Qt::LeftButton
        && modifiers == Qt::NoModifier
        && s.enabled
        && s.clipLoaded
        && !anyToolArmed( s )
        && !armedAtFirstPress;
}

/*! \brief Remembers whether a tool was armed at the press that started the current double-click.
 *
 *  Why: gradient paint switches itself off on the first click's release (gradientFinalPosPicked
 *  unchecks it), so by the time the double-click arrives the tool reads as off. Without this
 *  record, finishing a gradient with a quick second click would also toggle fullscreen.
 *
 *  Which press started it is Qt's decision, not ours: Qt pairs two presses into a double-click
 *  (interval, distance, same button, no movement or other button in between, on its own native
 *  timestamps) and marks the second press with Qt::MouseEventCreatedDoubleClick. A widget is never
 *  handed that second press (only the double-click); a QWindow gets it, flagged. So the first press
 *  of any double-click is the last left press on the viewer that Qt did NOT flag. The gate records
 *  every unflagged press and skips flagged ones; it re-derives no interval, distance or timestamp
 *  rule of its own (adversary r1 A1/A2/A3: each re-derived rule drifted from Qt's).
 *
 *  Qt's first press need not land on the viewer: it can land just outside (a layout gap in the
 *  same native window) and still pair with a second press on the viewer. So an unflagged left press
 *  to ANY other object also starts a new record, unarmed (adversary r1 pass 3 A2). One physical
 *  press reaches several objects (the top-level QWidgetWindow before the viewport, the QGraphicsView
 *  and its parents after it when the viewport ignores it); QApplication copies the press's
 *  timestamp onto each propagated copy, so a press elsewhere that carries the recorded press's
 *  timestamp is that same press and leaves the record alone. The timestamp is only compared for
 *  identity, never measured. */
class Gate
{
public:
    /*! \brief Record a left press on the viewer.
     *  \param armedNow           anyToolArmed() at this press
     *  \param createdDoubleClick Qt flagged this press as the second press of a double-click
     *  \param pressId            the press's event timestamp, used only as its identity */
    void notePress( bool armedNow, bool createdDoubleClick, quint64 pressId = 0 )
    {
        if( createdDoubleClick ) return; // the second press of Qt's pair: keep the first press's record
        m_armed = armedNow;
        m_pressId = pressId;
    }

    /*! \brief Record a left press delivered to any object other than the two display surfaces.
     *  An unflagged press there is a new first press with no viewer tool armed, unless it is a copy
     *  of the press already recorded (same \a pressId). */
    void noteOtherPress( bool createdDoubleClick, quint64 pressId )
    {
        if( createdDoubleClick ) return;
        if( pressId == m_pressId ) return; // the recorded press reaching another object
        m_armed = false;
        m_pressId = pressId;
    }

    bool armedAtFirstPress() const { return m_armed; }

    bool decide( Qt::MouseButton button, Qt::KeyboardModifiers modifiers, const ViewerState &s ) const
    {
        return shouldToggle( button, modifiers, s, armedAtFirstPress() );
    }

private:
    bool m_armed = false;
    quint64 m_pressId = 0;
};

} // namespace viewer_dblclick

#endif // VIEWERDOUBLECLICKPOLICY_H
