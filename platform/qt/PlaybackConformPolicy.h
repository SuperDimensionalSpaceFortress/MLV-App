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

/*! Effective Auto quality target: Auto must not spend quality chasing a
 *  frame budget tighter than the rate playback actually runs at, so it is
 *  min(autoTarget, playbackFps). A non-positive autoTarget or unusable
 *  playbackFps leaves the user's target untouched. Integer result (Auto works
 *  in whole fps): playbackFps is rounded to nearest, at least 1. */
inline int effectiveAutoTargetFps( int autoTargetFps, double playbackRate )
{
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
 * PlaybackFpsMeterPolicy.h, namespace playback_fps_meter; this header only formats its text. */

/*! Result of one elapsed-time pacing step (see applyEarlyCredit()). */
struct ElapsedCredit
{
    int creditedMs = 0; //!< elapsed time to feed the drop-frame advance
    int debtMs = 0;     //!< early credit still owed, carried to the next step
};

/*! Elapsed-time pacing with early credit carried as a DEBT.
 *
 * A predictive advance (a render finished before the next frame period had
 * elapsed) used to round the elapsed time UP to a whole frame period, which
 * put the timeline ahead of the wall clock every time it fired -- and it
 * fires more often the faster frames render (PR #185 paint-per-submit), and
 * the period is longer under conform (41.7 ms at 24 fps). Instead the early
 * advance still credits one full period (so a fresh frame is available at
 * once) but the excess over the real elapsed time is recorded as debt and
 * deducted from the elapsed time of the following steps. Invariant, over any
 * run of steps: sum(creditedMs) == sum(elapsedMs) + debtMs, and debtMs never
 * exceeds one frame period, so the timeline can lead the wall clock by at
 * most one period and never runs away. A new early credit is only granted
 * once the debt is fully repaid.
 *
 * \param elapsedMs real elapsed ms since the previous step (negative -> 0)
 * \param targetFrameMs one frame period at the playback rate (ms, fractional)
 * \param targetFrameMsCeil the same, rounded up to whole ms (>= 1)
 * \param predictiveAdvance the step was triggered by a finished render
 * \param debtMs debt carried from the previous step (negative -> 0) */
inline ElapsedCredit applyEarlyCredit( int elapsedMs,
                                       double targetFrameMs,
                                       int targetFrameMsCeil,
                                       bool predictiveAdvance,
                                       int debtMs )
{
    ElapsedCredit r;
    int credited = elapsedMs < 0 ? 0 : elapsedMs;
    int debt = debtMs < 0 ? 0 : debtMs;
    const int pay = credited < debt ? credited : debt;
    credited -= pay;
    debt -= pay;
    if( predictiveAdvance && debt == 0 && credited < targetFrameMs )
    {
        debt = targetFrameMsCeil - credited;
        credited = targetFrameMsCeil;
    }
    r.creditedMs = credited;
    r.debtMs = debt;
    return r;
}

} // namespace playback_conform

#endif // PLAYBACKCONFORMPOLICY_H
