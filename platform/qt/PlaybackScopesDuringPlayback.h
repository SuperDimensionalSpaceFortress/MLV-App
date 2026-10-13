/*!
 * \file PlaybackScopesDuringPlayback.h
 * \brief PLAYBACK-SCOPES-OPTION-1: what a visible scope does while playback runs.
 *        Hide (no scope work while playing, so the GPU fast paths stay eligible),
 *        Throttled (the fork's interval throttle) or Live (every presented frame,
 *        like upstream). A paused or stopped frame always draws its scope.
 */

#ifndef PLAYBACKSCOPESDURINGPLAYBACK_H
#define PLAYBACKSCOPESDURINGPLAYBACK_H

#include "AutomationSettings.h"

#include <QByteArray>
#include <QString>
#include <QtGlobal>

enum class PlaybackScopesMode
{
    Hide = 0,
    Throttled,
    Live
};

/*! \brief The persisted "Scopes During Playback" setting. Hide by default, from the
 *  PLAYBACK-SCOPES-OPTION-1 Bachelor A/B (windowed s1, M16-1243). On the default GPU playback
 *  route a visible throttled histogram vetoed the GPU recon texture-present path (every frame
 *  fell to GPU recon readback) and roughly halved presented fps. The rule was: Throttled only
 *  if it cost under ~5 % on both routes, else Hide. */
namespace PlaybackScopesSettings
{
    inline constexpr const char * kKeyScopesDuringPlayback() { return "Playback/ScopesDuringPlayback"; }
    inline constexpr PlaybackScopesMode kDefaultMode() { return PlaybackScopesMode::Hide; }
}

inline const char * playbackScopesModeToken(PlaybackScopesMode mode)
{
    switch ( mode )
    {
    case PlaybackScopesMode::Hide: return "hide";
    case PlaybackScopesMode::Throttled: return "throttled";
    case PlaybackScopesMode::Live: return "live";
    }
    return "throttled";
}

/*! \brief "hide" | "throttled" | "live" (case and surrounding space ignored) is a mode;
 *  anything else is -1, no mode. */
inline int playbackScopesModeFromToken(const QByteArray &value)
{
    const QByteArray token = value.trimmed().toLower();
    if ( token == "hide" ) return static_cast<int>( PlaybackScopesMode::Hide );
    if ( token == "throttled" ) return static_cast<int>( PlaybackScopesMode::Throttled );
    if ( token == "live" ) return static_cast<int>( PlaybackScopesMode::Live );
    return -1;
}

/*! \brief MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK, the measurement override: a valid token wins
 *  over the setting; unset or anything else is no override. Then the setting; an unreadable
 *  setting is the default. */
inline PlaybackScopesMode playbackScopesModeResolve(int envOverride, int setting)
{
    if ( envOverride >= 0 ) return static_cast<PlaybackScopesMode>( envOverride );
    if ( setting >= 0 ) return static_cast<PlaybackScopesMode>( setting );
    return PlaybackScopesSettings::kDefaultMode();
}

inline int playbackScopesModeSettingValue()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return playbackScopesModeFromToken(
        set.value( PlaybackScopesSettings::kKeyScopesDuringPlayback() ).toString().toLatin1() );
}

inline void playbackScopesModeWriteToSettings( PlaybackScopesMode mode )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( PlaybackScopesSettings::kKeyScopesDuringPlayback(),
                  QString::fromLatin1( playbackScopesModeToken( mode ) ) );
}

/*! \brief The mode in effect: the env override (read once per process) or the setting (read
 *  on every call, so a menu change applies at once). */
inline PlaybackScopesMode playbackScopesMode()
{
    static const int envOverride =
        playbackScopesModeFromToken( qgetenv( "MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK" ) );
    return playbackScopesModeResolve( envOverride, playbackScopesModeSettingValue() );
}

/*! \brief Does a visible scope consume presented pixels right now? A scope is shown only with
 *  the edit area visible; while playback runs in Hide mode it does no work, so it neither
 *  computes nor vetoes the GPU fast paths. Paused or stopped, it always draws. */
inline bool playbackScopeDisplayConsumesPresentedPixels(
    bool editAreaVisible,
    bool playbackActive,
    PlaybackScopesMode mode)
{
    return editAreaVisible
        && !( playbackActive && mode == PlaybackScopesMode::Hide );
}

/*! \brief The minimum ms between two scope updates while playing. Live: 0, every presented
 *  frame (upstream). Throttled: the base interval (MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS, else
 *  150), raised to 1.4 x the frame's render time and capped at 500 ms. Hide never reaches here. */
inline int playbackScopeUpdateIntervalForMode(
    PlaybackScopesMode mode,
    int baseIntervalMs,
    double renderTotalMs)
{
    if ( mode == PlaybackScopesMode::Live ) return 0;
    return renderTotalMs > 0.0
        ? qBound( baseIntervalMs, qRound( renderTotalMs * 1.4 ), 500 )
        : baseIntervalMs;
}

#endif // PLAYBACKSCOPESDURINGPLAYBACK_H
