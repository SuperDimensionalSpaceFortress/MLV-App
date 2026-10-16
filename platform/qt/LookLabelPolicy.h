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
 *  pending, the one that last landed, so a re-derivation mid-analysis never names the selected flavor early. */
inline QString shownText( bool lookAssistEnabled, const lookassist::LookAssistFlavorOutcome &outcome,
                          const void *receipt, const QString &selectedFlavorName )
{
    const lookassist::LookAssistFlavorOutcome onScreen = outcome.shown();
    return appliedText( lookAssistEnabled, onScreen.receiptValue( receipt, selectedFlavorName ), onScreen.fellBack( receipt ) );
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
