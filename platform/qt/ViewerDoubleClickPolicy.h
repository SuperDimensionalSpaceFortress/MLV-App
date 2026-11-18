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

/*! \brief Remembers whether a tool was armed at the press that started the current click sequence.
 *
 *  Why: gradient paint switches itself off on the first click's release (gradientFinalPosPicked
 *  unchecks it), so by the time the double-click arrives the tool reads as off. Without this
 *  record, finishing a gradient with a quick second click would also toggle fullscreen. */
class Gate
{
public:
    /*! \brief Record a left press on the viewer.
     *  \param armedNow             anyToolArmed() at this press
     *  \param createdDoubleClick   Qt flagged this press as the one that made a double-click
     *  \param timestampMs          the press's event timestamp
     *  \param doubleClickIntervalMs the platform double-click interval */
    void notePress( bool armedNow, bool createdDoubleClick, quint64 timestampMs, int doubleClickIntervalMs )
    {
        if( createdDoubleClick ) return; // the second press of a double-click: keep the first press's record
        if( m_armed && timestampMs >= m_timestampMs
         && timestampMs - m_timestampMs <= static_cast<quint64>( doubleClickIntervalMs < 0 ? 0 : doubleClickIntervalMs ) )
        {
            return; // a second press inside the window that Qt did not flag: still the same sequence
        }
        // A timestamp that went backwards (wrap or a reset clock) counts as outside the window.
        m_armed = armedNow;
        m_timestampMs = timestampMs;
    }

    bool armedAtFirstPress() const { return m_armed; }

    bool decide( Qt::MouseButton button, Qt::KeyboardModifiers modifiers, const ViewerState &s ) const
    {
        return shouldToggle( button, modifiers, s, armedAtFirstPress() );
    }

private:
    bool m_armed = false;
    quint64 m_timestampMs = 0;
};

} // namespace viewer_dblclick

#endif // VIEWERDOUBLECLICKPOLICY_H
