/*!
 * \file PlaybackInfoLabelPolicy.h
 * \brief LOOK-LABEL-PLAYBACK-INFO-1: the playback-info lines drawn under the fullscreen look label
 *        (and, without fps, in the status-bar twin). Pure (QtCore only), so console_tests pins it;
 *        the pixels are drawn by LookLabelOverlay.h, the facts are read by MainWindow.
 *
 * Owner, 2026-11-06 (PR 459): "We need to show other useful info as well. CPU vs GPU. Scale (Full, Half,
 * Quarter). Am I missing anything?" Owner, 2026-11-16: "show other useful info such as fps, resolution, etc".
 * The label only names what is on screen; it never changes playback.
 */

#ifndef PLAYBACKINFOLABELPOLICY_H
#define PLAYBACKINFOLABELPOLICY_H

#include "AutomationSettings.h"
#include "PlaybackConformPolicy.h"
#include "PlaybackScaleLabel.h"

#include <QChar>
#include <QString>
#include <QStringList>

#include <cmath>

namespace playback_info_label
{

/*! \brief What the screen shows, read by MainWindow from state it already keeps (no new decode work). */
struct Facts
{
    bool gpuDisplay = false;          // the GPU display window is the preview surface
    QString processingBadge;          // the toolbar's processing badge ("GPU Preview", "CPU", ...)
    int requestedScale = 0;           // 1, 2, 4, 8 (0 = unknown, the scale field is omitted)
    int presentedScale = 0;
    bool presentedValid = false;      // presentedScale belongs to the requested scale's frames
    QString debayer;                  // the chosen debayer's menu text ("" = omitted)
    bool debayerOnGpu = false;
    bool playing = false;
    double achievedFps = 0.0;         // smoothed presented rate while playing
    double clipFps = 0.0;             // the clip's own rate (<= 0 = unknown, the fps field is omitted)
    double playbackRate = 0.0;        // the conform rate (getPlaybackFramerate)
    bool dropFrame = false;
    bool dualIsoClip = false;         // the Dual-ISO field is shown only for a Dual-ISO clip
    bool dualIsoOn = false;
    int width = 0;                    // source resolution (<= 0 = omitted)
    int height = 0;
};

/*! \brief Which fields the user shows (View > Fullscreen Info). All shown by default. */
struct Toggles
{
    bool pipeline = true;
    bool scale = true;
    bool debayer = true;
    bool fps = true;                  // the rate and DF
    bool dualIso = true;
    bool resolution = true;
};

namespace Settings
{
    inline constexpr const char * kKeyShowPipeline() { return "Playback/LookLabelInfo/ShowPipeline"; }
    inline constexpr const char * kKeyShowScale() { return "Playback/LookLabelInfo/ShowScale"; }
    inline constexpr const char * kKeyShowDebayer() { return "Playback/LookLabelInfo/ShowDebayer"; }
    inline constexpr const char * kKeyShowFps() { return "Playback/LookLabelInfo/ShowFps"; }
    inline constexpr const char * kKeyShowDualIso() { return "Playback/LookLabelInfo/ShowDualIso"; }
    inline constexpr const char * kKeyShowResolution() { return "Playback/LookLabelInfo/ShowResolution"; }
    inline constexpr bool kDefaultShow() { return true; }
}

/*! \brief " . " with a middle dot, built in code so the source stays ASCII. */
inline QString separator()
{
    return QStringLiteral( " " ) + QChar( 0x00B7 ) + QStringLiteral( " " );
}

inline QString pipelineField( const Facts &facts )
{
    QString text = facts.gpuDisplay ? QStringLiteral( "GPU display" ) : QStringLiteral( "CPU display" );
    const QString badge = facts.processingBadge.trimmed();
    if( !badge.isEmpty() ) text += QStringLiteral( " " ) + badge;
    return text;
}

inline QString scaleField( const Facts &facts )
{
    if( facts.requestedScale <= 0 ) return QString();
    if( facts.presentedValid && facts.presentedScale > 0 && facts.presentedScale != facts.requestedScale )
    {
        return PlaybackScaleLabel::fractionWord( facts.requestedScale ) + QStringLiteral( "->" )
             + PlaybackScaleLabel::fractionWord( facts.presentedScale );
    }
    return PlaybackScaleLabel::fractionWord( facts.requestedScale );
}

inline QString debayerField( const Facts &facts )
{
    const QString name = facts.debayer.trimmed();
    if( name.isEmpty() ) return QString();
    return facts.debayerOnGpu ? name + QStringLiteral( " (GPU)" ) : name;
}

inline bool clipFpsKnown( const Facts &facts )
{
    return std::isfinite( facts.clipFps ) && facts.clipFps > 0.0;
}

/*! \brief Playing: "<achieved>/<clip> fps", plus " (<clip>-><rate>)" while conformed below the clip rate.
 *  Stopped: "<clip> fps". An achieved rate not yet measured (the first ticks of a play) reads "--". */
inline QString fpsField( const Facts &facts )
{
    if( !clipFpsKnown( facts ) ) return QString();
    const QString clip = playback_conform::formatRateForStatus( facts.clipFps );
    if( !facts.playing ) return clip + QStringLiteral( " fps" );
    const QString achieved = ( std::isfinite( facts.achievedFps ) && facts.achievedFps > 0.0 )
        ? QString::number( facts.achievedFps, 'f', 1 ) : QStringLiteral( "--" );
    QString text = achieved + QStringLiteral( "/" ) + clip + QStringLiteral( " fps" );
    if( std::isfinite( facts.playbackRate ) && facts.playbackRate > 0.0 && facts.playbackRate + 0.005 < facts.clipFps )
    {
        text += QStringLiteral( " (" ) + clip + QStringLiteral( "->" )
              + playback_conform::formatRateForStatus( facts.playbackRate ) + QStringLiteral( ")" );
    }
    return text;
}

inline QString dropFrameField( const Facts &facts )
{
    return facts.dropFrame ? QStringLiteral( "DF on" ) : QStringLiteral( "DF off" );
}

inline QString dualIsoField( const Facts &facts )
{
    if( !facts.dualIsoClip ) return QString();
    return facts.dualIsoOn ? QStringLiteral( "Dual-ISO on" ) : QStringLiteral( "Dual-ISO off" );
}

inline QString resolutionField( const Facts &facts )
{
    if( facts.width <= 0 || facts.height <= 0 ) return QString();
    return QString::number( facts.width ) + QStringLiteral( "x" ) + QString::number( facts.height );
}

/*! \brief At most two lines joined by '\n': line A = pipeline . scale . debayer; line B = fps . DF .
 *  Dual-ISO . resolution. A line with no enabled, known field is omitted; "" when nothing is shown.
 *  `includeFps` false (the status-bar twin, which sits beside the fps meter) drops the rate but keeps DF. */
inline QString compose( const Facts &facts, const Toggles &toggles, bool includeFps )
{
    const QString sep = separator();
    QStringList lineA;
    if( toggles.pipeline ) lineA << pipelineField( facts );
    if( toggles.scale ) lineA << scaleField( facts );
    if( toggles.debayer ) lineA << debayerField( facts );
    lineA.removeAll( QString() );

    QStringList lineB;
    if( toggles.fps && includeFps ) lineB << fpsField( facts );
    if( toggles.fps ) lineB << dropFrameField( facts );
    if( toggles.dualIso ) lineB << dualIsoField( facts );
    if( toggles.resolution ) lineB << resolutionField( facts );
    lineB.removeAll( QString() );
    // r1 adversary A5: a field never adds a line of its own (a newline inside one reads as a space). r2 A2: a CR too.
    for( QString &field : lineA ) { field.replace( QLatin1Char( '\r' ), QLatin1Char( ' ' ) ); field.replace( QLatin1Char( '\n' ), QLatin1Char( ' ' ) ); }
    for( QString &field : lineB ) { field.replace( QLatin1Char( '\r' ), QLatin1Char( ' ' ) ); field.replace( QLatin1Char( '\n' ), QLatin1Char( ' ' ) ); }

    QStringList lines;
    if( !lineA.isEmpty() ) lines << lineA.join( sep );
    if( !lineB.isEmpty() ) lines << lineB.join( sep );
    return lines.join( QLatin1Char( '\n' ) );
}

/*! \brief r2 adversary A1: is a deferred (playing) relabel due? `dirty` = an info input changed since the last relabel;
 *  `clockValid` / `elapsedMs` = a MONOTONIC clock (QElapsedTimer) restarted by every relabel, never the wall-clock QTime the
 *  fps meter reads (it wraps at midnight), and never an fps measurement (a 1 fps conform resets the meter). <= 4 Hz. */
constexpr qint64 kDeferredRelabelMs = 250;
inline bool deferredRelabelDue( bool dirty, bool clockValid, qint64 elapsedMs )
{
    return dirty && ( !clockValid || elapsedMs >= kDeferredRelabelMs );
}

/*! \brief One-line form for a log or receipt field: '\n' -> " / ", '"' -> '\''. */
inline QString logForm( const QString &text )
{
    QString out = text;
    // r2 adversary A2: a CR (alone or in CRLF) breaks a log line too.
    out.replace( QStringLiteral( "\r\n" ), QStringLiteral( "\n" ) );
    out.replace( QLatin1Char( '\r' ), QLatin1Char( '\n' ) );
    out.replace( QLatin1Char( '\n' ), QStringLiteral( " / " ) );
    out.replace( QLatin1Char( '"' ), QLatin1Char( '\'' ) );
    return out;
}

/*! \brief The enabled fields, comma separated ("none" when every field is off). */
inline QString fieldsCsv( const Toggles &toggles )
{
    QStringList names;
    if( toggles.pipeline ) names << QStringLiteral( "pipeline" );
    if( toggles.scale ) names << QStringLiteral( "scale" );
    if( toggles.debayer ) names << QStringLiteral( "debayer" );
    if( toggles.fps ) names << QStringLiteral( "fps" );
    if( toggles.dualIso ) names << QStringLiteral( "dual_iso" );
    if( toggles.resolution ) names << QStringLiteral( "resolution" );
    return names.isEmpty() ? QStringLiteral( "none" ) : names.join( QLatin1Char( ',' ) );
}

inline Toggles togglesFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    Toggles toggles;
    toggles.pipeline = set.value( Settings::kKeyShowPipeline(), Settings::kDefaultShow() ).toBool();
    toggles.scale = set.value( Settings::kKeyShowScale(), Settings::kDefaultShow() ).toBool();
    toggles.debayer = set.value( Settings::kKeyShowDebayer(), Settings::kDefaultShow() ).toBool();
    toggles.fps = set.value( Settings::kKeyShowFps(), Settings::kDefaultShow() ).toBool();
    toggles.dualIso = set.value( Settings::kKeyShowDualIso(), Settings::kDefaultShow() ).toBool();
    toggles.resolution = set.value( Settings::kKeyShowResolution(), Settings::kDefaultShow() ).toBool();
    return toggles;
}

inline void togglesWriteToSettings( const Toggles &toggles )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( Settings::kKeyShowPipeline(), toggles.pipeline );
    set.setValue( Settings::kKeyShowScale(), toggles.scale );
    set.setValue( Settings::kKeyShowDebayer(), toggles.debayer );
    set.setValue( Settings::kKeyShowFps(), toggles.fps );
    set.setValue( Settings::kKeyShowDualIso(), toggles.dualIso );
    set.setValue( Settings::kKeyShowResolution(), toggles.resolution );
}

} // namespace playback_info_label

#endif // PLAYBACKINFOLABELPOLICY_H
