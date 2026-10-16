/*!
 * \file PlaybackScaleLabel.h
 * \brief The one place a playback scale FACTOR becomes words a person reads.
 *
 * SCALE-LABEL-CLARITY-1: "scale 2" / "x4" means half / quarter resolution, which
 * end users read backwards. Everything a person reads says the resolution; the
 * factor stays only in internal identifiers (playbackScaleFactor,
 * MLVAPP_PLAYBACK_SCALE_FACTOR, receipt keys, leg ids), which are NOT renamed.
 * Header-only and QtCore-only so a console test can pin it.
 */

#ifndef PLAYBACKSCALELABEL_H
#define PLAYBACKSCALELABEL_H

#include <QCoreApplication>
#include <QString>

class PlaybackScaleLabel
{
public:
    /*! \brief 1 -> "Full res (1x)", 2 -> "Half res (0.5x)", 4 -> "Quarter res (0.25x)".
     *  Any other value -> "Custom res (1/N)" (N printed as given); never fails. */
    static QString resolutionLabel( int scaleFactor )
    {
        switch( scaleFactor )
        {
            case 1:
                return QCoreApplication::translate( "PlaybackScaleLabel", "Full res (1x)" );
            case 2:
                return QCoreApplication::translate( "PlaybackScaleLabel", "Half res (0.5x)" );
            case 4:
                return QCoreApplication::translate( "PlaybackScaleLabel", "Quarter res (0.25x)" );
            default:
                return QCoreApplication::translate( "PlaybackScaleLabel", "Custom res (1/%1)" )
                    .arg( scaleFactor );
        }
    }

    /*! \brief Compact toolbar form: "Full", "Half", "Quarter", else "1/N". */
    static QString shortLabel( int scaleFactor )
    {
        switch( scaleFactor )
        {
            case 1:
                return QCoreApplication::translate( "PlaybackScaleLabel", "Full res" );
            case 2:
                return QCoreApplication::translate( "PlaybackScaleLabel", "Half res" );
            case 4:
                return QCoreApplication::translate( "PlaybackScaleLabel", "Quarter res" );
            default:
                return QCoreApplication::translate( "PlaybackScaleLabel", "1/%1 res" )
                    .arg( scaleFactor );
        }
    }
};

#endif // PLAYBACKSCALELABEL_H
