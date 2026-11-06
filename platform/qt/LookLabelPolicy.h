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
 *  rows of one path by the receipt the open recorded, so the active row winning only breaks a tie nothing recorded.
 *  recordLoadedClipReceipt also uses this to pick the row an open loaded for. */
template <typename IsLoadedRow>
inline int loadedClipOwnerRow( int activeRow, int rowCount, IsLoadedRow isLoadedRow )
{
    if( activeRow >= 0 && activeRow < rowCount && isLoadedRow( activeRow ) ) return activeRow;
    for( int row = 0; row < rowCount; ++row )
        if( isLoadedRow( row ) ) return row;
    return -1;
}

/*! \brief r13 (adversary r13 63319bdb A1): the row an open's record prefers. An opener that applies a known row's receipt next
 *  (showFileInEditor, the export's final reopen) names it; openMlv itself names none (-1) and the active row is the guess. The
 *  active row is not the opener's when a FileOpen delivered in openMlv's event pump opened another row of the same path. */
inline int openRecordRow( int openerRow, int activeRow )
{
    return openerRow >= 0 ? openerRow : activeRow;
}

/*! \brief r14 (sol r13 B1 on cc76ab62): whether setSliders' restore or capture of `restoredReceipt`'s baseline marks it
 *  "Look: Baseline". It keys on the receipt whose state was restored being the one applied to the loaded clip (the opener
 *  records that row first: showFileInEditor, the export's clone and its final reopen), which is the receipt the label reads.
 *  The active row is not read: a FileOpen delivered in showFileInEditor(A)'s event pump can leave a duplicate-path row B active
 *  while A's baseline is restored, and an applied marker already naming A then skips the analysis, so an unmarked restore kept
 *  naming A's old landed flavor over its baseline pixels. A scratch receipt (Reset Sliders' defaults) is never the applied one. */
inline bool restoreMarksBaseline( const void *restoredReceipt, const void *appliedReceipt, const void *activeReceipt )
{
    Q_UNUSED( activeReceipt );
    return restoredReceipt != nullptr && restoredReceipt == appliedReceipt;
}

/*! \brief Whether the look of the loaded clip is on, read from the receipt that owns it. The selector checkbox describes
 *  the active row, and a click there writes nothing into another row's clip (receiptOwnedByLoadedClip refuses it), so it
 *  counts only when the owner is the active row. */
inline bool lookOnForOwner( bool hasOwner, bool ownerLookAssistEnabled, bool ownerIsActiveRow, bool selectorChecked )
{
    return hasOwner && ownerLookAssistEnabled && ( !ownerIsActiveRow || selectorChecked );
}

/*! \brief r13 (adversary r12 31016339 A1): the Look Assist switch of the receipt APPLIED to the loaded clip. An export job
 *  applies a clone of the row's receipt, and the clone does not copy the row's switch (addClipToExportQueue; the export grades
 *  under the clone's own switch, MainWindow.cpp startExport*), so the row's switch can say Off over the clone's grade. The
 *  export site records the clone's switch when it makes the clone the key; a finished job's clone may be freed, so it is never
 *  read later. Any other applied receipt is the owner's, whose live switch decides. */
inline bool appliedLookSwitch( const void *appliedReceipt, const void *ownerReceipt, bool ownerLookAssistEnabled,
                               bool recordedCloneLookAssistEnabled )
{
    return ( appliedReceipt && appliedReceipt != ownerReceipt ) ? recordedCloneLookAssistEnabled : ownerLookAssistEnabled;
}

/*! \brief r13 (adversary r12 31016339 A2): whether the selector checkbox describes the owner's look. Only while the sliders
 *  are attached: detached (a failed reopen left another receipt's clip and grade loaded), a click returns at the ownership
 *  guard (receiptOwnedByLoadedClip) and changes no grade, so the box must not relabel it. */
inline bool selectorDescribesOwner( bool ownerIsActiveRow, bool slidersAttached )
{
    return ownerIsActiveRow && slidersAttached;
}

/*! \brief r12b (adversary r12 A1): which receipt the outcome is read under. The switch comes from the row's receipt, but an
 *  analysis lands on the receipt applied to the clip, and an export job applies a clone of the row's receipt. Identity only:
 *  a finished job's clone is gone, so it is never read. Without one (an open that found no row of its file), the owner. */
inline const void *outcomeKey( const void *appliedReceipt, const void *ownerReceipt )
{
    return appliedReceipt ? appliedReceipt : ownerReceipt;
}

/*! \brief A clip stays loaded but no session row holds it(its row was removed and the next open has not replaced it):
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
