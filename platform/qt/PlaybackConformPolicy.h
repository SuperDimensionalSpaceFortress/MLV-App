/*!
 * \file PlaybackConformPolicy.h
 * \brief Pure decision/arithmetic for PLAYBACK-HFR-CONFORM-DEFAULT-1: a
 *        high-frame-rate clip plays as slow motion at a lower "conform"
 *        rate by default (owner 2026-09-28: "60fps playback at 24fps by
 *        default but be configurable") -- extracted from MainWindow so it
 *        can be unit-tested without the GUI.
 *
 * Playback rate = playbackFps(clipFps, conformEnabled, target, threshold).
 * It deliberately has NO export-override input: the Export dialog's
 * "FPS override" (MainWindow::m_fpsOverride / m_frameRate) is export-only and
 * never reaches playback (MainWindow::getFramerate() keeps serving export,
 * metadata and timecode; MainWindow::getPlaybackFramerate() serves playback).
 *
 * Conform applies only when clipFps > threshold AND target < clipFps, so a
 * clip is never sped up. Invalid target/threshold values (NaN, inf, < 1,
 * absurdly large) fall back to the defaults, like the Auto target-fps
 * setting does for its own invalid saved values.
 */

#ifndef PLAYBACKCONFORMPOLICY_H
#define PLAYBACKCONFORMPOLICY_H

#include <cmath>

#include <QSettings>
#include <QString>

namespace playback_conform
{

inline constexpr bool   kDefaultEnabled      = true;
inline constexpr double kDefaultTargetFps    = 24.0;
inline constexpr double kDefaultThresholdFps = 30.0;

// New Playback/Conform* QSettings keys. Distinct from Playback/AutoTargetFps
// (the Auto *quality* frame budget) on purpose -- see the card's item 5.
inline constexpr const char * kKeyEnabled()      { return "Playback/ConformEnabled"; }
inline constexpr const char * kKeyTargetFps()    { return "Playback/ConformTargetFps"; }
inline constexpr const char * kKeyThresholdFps() { return "Playback/ConformThresholdFps"; }

inline bool isValidRate( double rate )
{
    return std::isfinite( rate ) && rate >= 1.0 && rate <= 1000.0;
}

inline double sanitizedTargetFps( double target )
{
    return isValidRate( target ) ? target : kDefaultTargetFps;
}

inline double sanitizedThresholdFps( double threshold )
{
    return isValidRate( threshold ) ? threshold : kDefaultThresholdFps;
}

/*! \return true when the clip is conformed: enabled, clipFps above the
 *  threshold, and the (sanitized) target strictly below the clip rate. */
inline bool conformApplies( double clipFps,
                            bool conformEnabled,
                            double targetFps,
                            double thresholdFps )
{
    if( !conformEnabled ) return false;
    if( !std::isfinite( clipFps ) || clipFps <= 0.0 ) return false;
    return clipFps > sanitizedThresholdFps( thresholdFps )
        && sanitizedTargetFps( targetFps ) < clipFps;
}

/*! \return the rate the playback timeline runs at, in source frames per
 *  wall-clock second. The clip's own rate unless conformApplies(). */
inline double playbackFps( double clipFps,
                           bool conformEnabled,
                           double targetFps,
                           double thresholdFps )
{
    return conformApplies( clipFps, conformEnabled, targetFps, thresholdFps )
        ? sanitizedTargetFps( targetFps )
        : clipFps;
}

struct Settings
{
    bool   enabled      = kDefaultEnabled;
    double targetFps    = kDefaultTargetFps;
    double thresholdFps = kDefaultThresholdFps;
};

inline Settings loadSettings( QSettings & set )
{
    Settings s;
    s.enabled      = set.value( kKeyEnabled(), kDefaultEnabled ).toBool();
    bool okTarget = false;
    bool okThreshold = false;
    const double target =
        set.value( kKeyTargetFps(), kDefaultTargetFps ).toDouble( &okTarget );
    const double threshold =
        set.value( kKeyThresholdFps(), kDefaultThresholdFps ).toDouble( &okThreshold );
    s.targetFps    = okTarget ? sanitizedTargetFps( target ) : kDefaultTargetFps;
    s.thresholdFps = okThreshold ? sanitizedThresholdFps( threshold ) : kDefaultThresholdFps;
    return s;
}

inline void saveSettings( QSettings & set, const Settings & s )
{
    set.setValue( kKeyEnabled(), s.enabled );
    set.setValue( kKeyTargetFps(), sanitizedTargetFps( s.targetFps ) );
    set.setValue( kKeyThresholdFps(), sanitizedThresholdFps( s.thresholdFps ) );
}

/*! Effective Auto quality target. While a clip is CONFORMED, Auto must not
 *  spend quality chasing a frame budget tighter than the rate playback
 *  actually runs at, so it is min(autoTarget, playbackFps). When conform is
 *  NOT active the user's target is returned unchanged: a 23.976 clip keeps
 *  Auto's 30 fps budget (capping it to 24 loosened the budget from 33 ms to
 *  41.7 ms on the perf-goal clip -- the #188 regression). A non-positive
 *  autoTarget or unusable playbackFps leaves the target untouched. Integer
 *  result (Auto works in whole fps): playbackFps is rounded to nearest, >= 1. */
inline int effectiveAutoTargetFps( int autoTargetFps, double playbackRate, bool conformActive )
{
    if( !conformActive ) return autoTargetFps;
    if( autoTargetFps <= 0 ) return autoTargetFps;
    if( !std::isfinite( playbackRate ) || playbackRate < 1.0 ) return autoTargetFps;
    const int rounded = static_cast<int>( playbackRate + 0.5 );
    const int cap = rounded < 1 ? 1 : rounded;
    return autoTargetFps < cap ? autoTargetFps : cap;
}

/*! Audio plays at the file's native rate and is only re-synced at play
 *  start, loop wrap and seek, so slowed-down picture would run out of step
 *  with it. Round 1: audio is MUTED while conform is active (time-stretch is
 *  the follow-up card PLAYBACK-HFR-AUDIO-STRETCH-1). The saved
 *  actionAudioOutput is deliberately left alone. */
inline bool audioSyncAllowed( bool conformActive )
{
    return !conformActive;
}

inline QString audioMutedStatusText()
{
    return QStringLiteral( "audio muted (conform)" );
}

inline QString formatRateForStatus( double rate )
{
    return QString::number( rate, 'g', 5 );
}

/*! Status-bar text: measured fps, and when the clip is being conformed the
 *  clip rate and playback rate, e.g. "Playback: 24 fps (60 -> 24)". */
inline QString playbackFpsStatusText( double measuredFps,
                                      double clipFps,
                                      double playbackRate )
{
    if( measuredFps < 0.0 ) measuredFps = 0.0;
    QString text = measuredFps < 10.0
        ? QStringLiteral( "Playback: %1 fps" ).arg( measuredFps, 0, 'f', 1 )
        : QStringLiteral( "Playback: %1 fps" ).arg( static_cast<int>( measuredFps ) );
    if( std::isfinite( clipFps ) && std::isfinite( playbackRate )
     && playbackRate > 0.0 && playbackRate + 0.005 < clipFps )
    {
        text += QStringLiteral( " (%1 -> %2)" )
                    .arg( formatRateForStatus( clipFps ),
                          formatRateForStatus( playbackRate ) );
    }
    return text;
}

/* The status-bar fps meter arithmetic (draw-to-draw smoothing, stall reset) lives in
 * PlaybackFpsMeterPolicy.h, namespace playback_fps_meter; this header only formats its text.
 * Pacing (early-tick round-up, stall clamp, the native-pace ceiling) lives in
 * PlaybackNativePaceGuard.h; it is fed getPlaybackFramerate(), so it paces the conform rate too. */

} // namespace playback_conform

#endif // PLAYBACKCONFORMPOLICY_H
