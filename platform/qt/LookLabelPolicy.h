/*!
 * \file LookLabelPolicy.h
 * \brief LOOK-FULLSCREEN-PARITY-1: the "current look" label. Fullscreen draws it in the bottom-right
 *        corner of the playback surface; windowed shows it in the status bar. Pure (QtCore only), so
 *        console_tests pins it; the pixels are drawn by LookLabelOverlay.h.
 *
 * Owner, 2026-10-15: "Full screen dont bypass the look. I just want text in the bottom right showing
 * current look when in full screen and somewhere on GUI when windowed". The label only names the look;
 * it never changes the grade.
 */

#ifndef LOOKLABELPOLICY_H
#define LOOKLABELPOLICY_H

#include "AutomationSettings.h"
#include "../../src/batch/LookAssistAnalysis.h"

#include <QRect>
#include <QSize>
#include <QString>
#include <QtGlobal>

namespace look_label
{

/*! \brief The persisted "Show Look Label in Fullscreen" setting. Shown by default. */
namespace Settings
{
    inline constexpr const char * kKeyShowInFullscreen() { return "Playback/ShowLookLabelInFullscreen"; }
    inline constexpr bool kDefaultShowInFullscreen() { return true; }
}

/*! \brief The flavor's name as the user reads it, from its machine spelling (lookAssistFlavorName:
 *  "classic" / "cinematic" / "film"). The Film item's GUI label is "Film grade"; "vision50d" is the
 *  Kodak Vision3 50D flavor (owner-ruled label, 2026-10-15), listed here so its label is right the
 *  day that flavor lands. A spelling this table does not know is shown as it is. */
inline QString flavorDisplayName( const QString &flavorName )
{
    const QString name = flavorName.trimmed().toLower();
    if( name == QLatin1String( "classic" ) ) return QStringLiteral( "Classic" );
    if( name == QLatin1String( "cinematic" ) ) return QStringLiteral( "Cinematic" );
    if( name == QLatin1String( "film" ) ) return QStringLiteral( "Film grade" );
    if( name == QLatin1String( "vision50d" ) ) return QStringLiteral( "Kodak Vision3 50D" );
    return flavorName.trimmed();
}

/*! \brief The label text: "Look: Off" when Look Assist is off, else "Look: <flavor>". */
inline QString text( bool lookAssistEnabled, const QString &flavorName )
{
    if( !lookAssistEnabled ) return QStringLiteral( "Look: Off" );
    const QString display = flavorDisplayName( flavorName );
    return display.isEmpty() ? QStringLiteral( "Look: Off" ) : QStringLiteral( "Look: " ) + display;
}

/*! \brief The label for the look APPLIED, not the one selected (catalogue L-04): after a safety fallback restored
 *  the baseline sliders it reads "Look: Baseline (fallback)", whatever flavor is selected. `appliedFlavorName` is
 *  LookAssistFlavorOutcome::receiptValue (the flavor applied, or the selector's while the analysis is pending). */
inline QString appliedText( bool lookAssistEnabled, const QString &appliedFlavorName, bool safetyFallback )
{
    if( lookAssistEnabled && safetyFallback ) return QStringLiteral( "Look: Baseline (fallback)" );
    return text( lookAssistEnabled, appliedFlavorName );
}

/*! \brief The ONE label derivation (MainWindow::updateLookLabel; every caller -- fullscreen toggle, setSliders, the
 *  switch, the flavor, a landed analysis -- goes through it). It reads the outcome on screen: while an analysis is
 *  pending, the one that last landed, so a re-derivation mid-analysis never names the selected flavor early.
 *  r9 (adversary A1/A2 on e3704ee7): a flavor is named only once an analysis has LANDED on this receipt. With nothing
 *  landed on it -- analysis disabled (MLVAPP_NO_LOOK_ASSIST) or skipped, a clip whose analysis has not landed yet, or a
 *  flavor change, which clears the outcome and restores the baseline -- the baseline sliders are what is on screen, so
 *  the label says "Look: Baseline", never the selected flavor. `selectedFlavorName` is not read here: the selector's
 *  value is the receipt's and the telemetry's fallback (LookAssistFlavorOutcome::receiptValue), not what the screen shows.
 *  r10 (sol + fable + key3 r9 on 2b748fa3): a receipt whose baseline setSliders put back (outcome.showsBaseline) reads
 *  "Look: Baseline" until an analysis lands on it -- the landing itself forgets the mark, so a later landing (async after a
 *  paste, or a safety fallback) is named, and the mark never depends on the de-dupe marker. */
inline QString shownText( bool lookAssistEnabled, const lookassist::LookAssistFlavorOutcome &outcome,
                          const void *receipt, const QString &selectedFlavorName )
{
    Q_UNUSED( selectedFlavorName );
    if( !lookAssistEnabled ) return text( false, QString() );
    if( outcome.showsBaseline( receipt ) ) return QStringLiteral( "Look: Baseline" );
    const lookassist::LookAssistFlavorOutcome onScreen = outcome.shown();
    if( onScreen.fellBack( receipt ) ) return appliedText( true, QString(), true );
    // Empty unless an analysis landed on this receipt and applied a flavor.
    const QString landedFlavor = onScreen.receiptValue( receipt, QString() );
    return landedFlavor.isEmpty() ? QStringLiteral( "Look: Baseline" ) : text( true, landedFlavor );
}

/*! \brief r12 (adversary r11 A1): the session row whose receipt owns the loaded clip, or -1. The active row is not it
 *  while another row's open is pumping events (addFileToSession activates B before B's loader runs, and A is still what
 *  is on screen). `isLoadedRow(row)` answers for one row in [0, rowCount); MainWindow::loadedClipIsRow already separates two
 *  rows of one path by the receipt the open recorded, so the active row winning only breaks a tie nothing recorded. */
template <typename IsLoadedRow>
inline int loadedClipOwnerRow( int activeRow, int rowCount, IsLoadedRow isLoadedRow )
{
    if( activeRow >= 0 && activeRow < rowCount && isLoadedRow( activeRow ) ) return activeRow;
    for( int row = 0; row < rowCount; ++row )
        if( isLoadedRow( row ) ) return row;
    return -1;
}

/*! \brief Whether the look of the loaded clip is on, read from the receipt that owns it. The selector checkbox describes
 *  the active row, and a click there writes nothing into another row's clip (receiptOwnedByLoadedClip refuses it), so it
 *  counts only when the owner is the active row. */
inline bool lookOnForOwner( bool hasOwner, bool ownerLookAssistEnabled, bool ownerIsActiveRow, bool selectorChecked )
{
    return hasOwner && ownerLookAssistEnabled && ( !ownerIsActiveRow || selectorChecked );
}

/*! \brief A clip stays loaded but no session row holds it (its row was removed and the next open has not replaced it):
 *  no receipt can say what is on screen, so the label keeps the text it had. Nothing loaded is not this case: it reads Off. */
inline bool keepsTextWithoutOwner( bool clipLoaded, int ownerRow )
{
    return clipLoaded && ownerRow < 0;
}

/*! \brief The corner label is drawn only in fullscreen, and only while the setting shows it. */
inline bool fullscreenOverlayShown( bool fullscreen, bool showInFullscreenSetting )
{
    return fullscreen && showInFullscreenSetting;
}

/*! \brief Text height in device pixels for a surface of this device-pixel height: about 1/54 of it
 *  (40 px on a 2160-line panel, 20 px on 1080), never under 14 or over 64. Small and readable from a sofa. */
inline int fontPixels( int surfaceHeightPx )
{
    return qBound( 14, qRound( surfaceHeightPx / 54.0 ), 64 );
}

/*! \brief Gap between the label and the surface's right and bottom edges, in device pixels. */
inline int marginPixels( int fontPx )
{
    return qMax( 8, fontPx );
}

/*! \brief Where a label of size `label` sits on a surface of size `surface` (both device pixels,
 *  top-left origin): the bottom-right corner, `margin` in from both edges. Clamped so it never
 *  leaves the surface; empty when the surface or the label is empty. */
inline QRect overlayRect( const QSize &surface, const QSize &label, int margin )
{
    if( surface.isEmpty() || label.isEmpty() ) return QRect();
    const int w = qMin( label.width(), surface.width() );
    const int h = qMin( label.height(), surface.height() );
    const int x = qMax( 0, surface.width() - margin - w );
    const int y = qMax( 0, surface.height() - margin - h );
    return QRect( x, y, w, h );
}

inline bool showInFullscreenFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return set.value( Settings::kKeyShowInFullscreen(), Settings::kDefaultShowInFullscreen() ).toBool();
}

inline void showInFullscreenWriteToSettings( bool show )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( Settings::kKeyShowInFullscreen(), show );
}

} // namespace look_label

#endif // LOOKLABELPOLICY_H
