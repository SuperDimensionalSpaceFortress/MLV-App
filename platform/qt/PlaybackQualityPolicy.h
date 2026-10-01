#ifndef PLAYBACKQUALITYPOLICY_H
#define PLAYBACKQUALITYPOLICY_H

/*
 * Phase 4E: GUI-grade Playback Quality dial.
 *
 * Three stable user-facing modes plus two hidden Phase 3 dogfood modes.
 * Auto adapts based on measured cadence. The mode is persisted to the existing QSettings store
 * (HKCU\Software\magiclantern.MLVApp\MLVApp\Playback\... on Windows).
 *
 * The MLVAPP_PLAYBACK_QUALITY_MODE, MLVAPP_PLAYBACK_PREFER_HQ_MEAN23, and
 * MLVAPP_PLAYBACK_SCALE_FACTOR env vars retain priority for dev/CI overrides;
 * this layer only kicks in when those env vars are unset.
 *
 * Decisions:
 * - Prioritize Smoothness (Fast): non-Dual ISO uses scale=4. Dual ISO uses the color-correct
 *   HQ+mean23 path and may start at scale=8 so it does not fall into the
 *   legacy preview-rowscale magenta-cast path.
 * - Prioritize Quality (HighQuality): HQ + mean23 + scale=4, cast closed, slower cadence.
 * - Auto: Sharp/Smooth starts at HQ scale=4; if measured cadence misses
 *   target, fall back to Fast for the next slot in Sharp/Smooth. Aggressive
 *   Dual ISO starts at HQ scale=8 so the preview keeps early Bayer-domain
 *   reduction ahead of LLRawProc/debayer from the first playback window.
 *   Re-evaluates every kAutoSlidingWindow frames.
 * - Phase3Fast/Phase3HQ: experimental Phase 3 dispatch labels; hidden until
 *   the dogfood gate or personal rollout tier permits them, and inactive until
 *   the one-time acknowledgement is accepted.
 *
 * Sharp/Smooth Dual-ISO clips stay at scale=4 when running HQ. Aggressive
 * Dual ISO may use HQ scale=8 because that path reduces in the Bayer domain
 * before LLRawProc/debayer. Non-dual-ISO HQ playback CAN use scale=2 in Auto
 * mode if there is headroom.
 */

#include <atomic>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>

#include "Phase3Mode.h"

#ifdef QT_CORE_LIB
#include <QSettings>
#include "AutomationSettings.h"
#include <QString>
#include <QStringList>
#endif

enum class PlaybackQualityMode : int
{
    Fast = 0,
    HighQuality = 1,
    Auto = 2,
    Phase3Fast = 3,
    Phase3HQ = 4
};

enum class PlaybackPreviewMode : int
{
    SharpSmooth = 0,
    AggressivePerformance = 1
};

/* Round-3 item 1: GUI-selected playback preview resolution (proxy level).
 * Auto keeps each scale's tuned default; Full disables preview proxies
 * (sharpest, slowest); Half allows one internal halving per axis; Quarter
 * allows two where a core exists (falls back to Half otherwise). Pause,
 * scrub, and export are NEVER proxied regardless of this setting. */
enum class PlaybackPreviewResolution : int
{
    Auto = 0,
    Full = 1,
    Half = 2,
    Quarter = 3
};

enum class PlaybackQualityTier : int
{
    Dev = 0,
    DailyUse = 1,
    PinnedClip = 2
};

namespace PlaybackQualitySettings
{
    inline constexpr const char * kOrganization() { return "magiclantern.MLVApp"; }
    inline constexpr const char * kApplication() { return "MLVApp"; }
    inline constexpr const char * kKeyQualityMode() { return "Playback/QualityMode"; }
    inline constexpr const char * kKeyPreviewMode() { return "Playback/PreviewMode"; }
    inline constexpr const char * kKeyScaleFactorOverride() { return "Playback/ScaleFactorOverride"; }
    inline constexpr const char * kKeyPreviewResolution() { return "Playback/PreviewResolution"; }
    inline constexpr const char * kKeyAutoTargetFps() { return "Playback/AutoTargetFps"; }
    inline constexpr const char * kKeyShowQualityIndicator() { return "Playback/ShowQualityIndicator"; }
    inline constexpr const char * kKeyShowExperimentalPhase3Modes() { return "Playback/ShowExperimentalPhase3Modes"; }
    inline constexpr const char * kKeyPhase3Acknowledged() { return "Playback/Phase3Acknowledged"; }
    inline constexpr const char * kKeyPhase3FastTier() { return "Playback/Phase3FastTier"; }
    inline constexpr const char * kKeyPhase3FastTierEnteredAt() { return "Playback/Phase3FastTierEnteredAt"; }
    inline constexpr const char * kKeyPhase3FastDailyUseClipsValidated() { return "Playback/Phase3FastDailyUseClipsValidated"; }
    inline constexpr const char * kKeyPhase3HQTier() { return "Playback/Phase3HQTier"; }
    inline constexpr const char * kKeyPhase3HQTierEnteredAt() { return "Playback/Phase3HQTierEnteredAt"; }
    inline constexpr const char * kKeyPhase3HQDailyUseClipsValidated() { return "Playback/Phase3HQDailyUseClipsValidated"; }
    inline constexpr const char * kKeyPhase3FastAutoFallbackFiredEpoch() { return "Playback/Phase3FastAutoFallbackFiredEpoch"; }
    inline constexpr const char * kKeyPhase3HQAutoFallbackFiredEpoch() { return "Playback/Phase3HQAutoFallbackFiredEpoch"; }
    inline constexpr const char * kKeyClipPlaytimePrefix() { return "Playback/ClipPlaytime/"; }

    inline constexpr int kDefaultQualityMode() { return static_cast<int>( PlaybackQualityMode::HighQuality ); }
    inline constexpr int kDefaultPreviewMode() { return static_cast<int>( PlaybackPreviewMode::SharpSmooth ); }
    inline constexpr int kDefaultScaleFactorOverride() { return 0; }
    inline constexpr int kDefaultPreviewResolution() { return static_cast<int>( PlaybackPreviewResolution::Auto ); }
    inline constexpr int kDefaultAutoTargetFps() { return 30; }
    inline constexpr int kDefaultShowQualityIndicator() { return 1; }
    inline constexpr int kDefaultShowExperimentalPhase3Modes() { return 0; }
    inline constexpr int kDefaultPhase3Acknowledged() { return 0; }
    inline constexpr long long kTierMinimumAgeMs() { return 7LL * 24LL * 60LL * 60LL * 1000LL; }
    inline constexpr long long kPinnedClipMinimumSeconds() { return 300LL; }
}

inline bool playbackQualityEnvVarTruthy(const char * raw)
{
    if (!raw || !*raw) return false;
    return std::strcmp(raw, "0") != 0
        && std::strcmp(raw, "false") != 0
        && std::strcmp(raw, "FALSE") != 0
        && std::strcmp(raw, "False") != 0;
}

inline bool playbackQualityAsciiEqualsIgnoreCase( const char * a, const char * b )
{
    if ( !a || !b ) return false;
    while ( *a && *b )
    {
        char ca = *a++;
        char cb = *b++;
        if ( ca >= 'A' && ca <= 'Z' ) ca = static_cast<char>( ca - 'A' + 'a' );
        if ( cb >= 'A' && cb <= 'Z' ) cb = static_cast<char>( cb - 'A' + 'a' );
        if ( ca != cb ) return false;
    }
    return *a == '\0' && *b == '\0';
}

inline bool playbackQualityModeParseOverride( const char * raw, int * outMode )
{
    if ( !raw || !*raw || !outMode ) return false;

    if ( playbackQualityAsciiEqualsIgnoreCase( raw, "0" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "fast" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "smooth" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "smoothness" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "prioritize_smoothness" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "prioritize-smoothness" ) )
    {
        *outMode = static_cast<int>( PlaybackQualityMode::Fast );
        return true;
    }
    if ( playbackQualityAsciiEqualsIgnoreCase( raw, "1" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "hq" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "high" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "high_quality" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "high-quality" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "quality" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "prioritize_quality" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "prioritize-quality" ) )
    {
        *outMode = static_cast<int>( PlaybackQualityMode::HighQuality );
        return true;
    }
    if ( playbackQualityAsciiEqualsIgnoreCase( raw, "2" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "auto" ) )
    {
        *outMode = static_cast<int>( PlaybackQualityMode::Auto );
        return true;
    }
    if ( playbackQualityAsciiEqualsIgnoreCase( raw, "3" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "phase3fast" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "phase3_fast" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "phase3-fast" ) )
    {
        *outMode = static_cast<int>( PlaybackQualityMode::Phase3Fast );
        return true;
    }
    if ( playbackQualityAsciiEqualsIgnoreCase( raw, "4" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "phase3hq" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "phase3_hq" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "phase3-hq" ) )
    {
        *outMode = static_cast<int>( PlaybackQualityMode::Phase3HQ );
        return true;
    }
    return false;
}

/* Returns -1 when unset, -2 when set to an invalid value, else 0..4. */
inline int playbackQualityModeEnvOverride()
{
    const char * raw = std::getenv( "MLVAPP_PLAYBACK_QUALITY_MODE" );
    if ( !raw || !*raw ) return -1;
    int mode = -1;
    return playbackQualityModeParseOverride( raw, &mode ) ? mode : -2;
}

inline bool playbackQualityPhase3UnattendedEnvOverride()
{
    return playbackQualityEnvVarTruthy( std::getenv( "MLVAPP_PLAYBACK_PHASE3_UNATTENDED" ) );
}

inline int playbackPreviewAggressiveEnvOverride()
{
    const char * raw = std::getenv("MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW");
    if ( raw && *raw )
    {
        if ( playbackQualityAsciiEqualsIgnoreCase( raw, "0" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "false" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "off" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "sharp" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "sharp_smooth" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "sharp-smooth" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "smooth" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "quality" ) )
        {
            return 0;
        }
        if ( playbackQualityAsciiEqualsIgnoreCase( raw, "1" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "true" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "yes" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "on" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "aggressive" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "aggressive_performance" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "aggressive-performance" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "performance" )
          || playbackQualityAsciiEqualsIgnoreCase( raw, "fast" ) )
        {
            return 1;
        }
        return 0;
    }

    raw = std::getenv("MLVAPP_PLAYBACK_PREVIEW_MODE");
    if ( !raw || !*raw ) return -1;
    if ( playbackQualityAsciiEqualsIgnoreCase( raw, "1" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "aggressive" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "aggressive_performance" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "aggressive-performance" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "performance" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "fast" ) )
    {
        return 1;
    }
    if ( playbackQualityAsciiEqualsIgnoreCase( raw, "0" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "sharp" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "sharp_smooth" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "sharp-smooth" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "smooth" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "quality" ) )
    {
        return 0;
    }
    if ( playbackQualityAsciiEqualsIgnoreCase( raw, "true" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "yes" )
      || playbackQualityAsciiEqualsIgnoreCase( raw, "on" ) )
    {
        return 1;
    }
    return 0;
}

inline bool playbackQualityModeIsPhase3( PlaybackQualityMode mode )
{
    return mode == PlaybackQualityMode::Phase3Fast
        || mode == PlaybackQualityMode::Phase3HQ;
}

inline bool playbackQualityModeIsExperimental( PlaybackQualityMode mode )
{
    return playbackQualityModeIsPhase3( mode );
}

inline PlaybackQualityMode playbackQualitySelectionAfterAcknowledgement(
    PlaybackQualityMode previous,
    PlaybackQualityMode requested,
    bool acknowledged )
{
    if ( playbackQualityModeIsPhase3( requested ) && !acknowledged )
    {
        return previous;
    }
    return requested;
}

inline const char * playbackQualityTierName( PlaybackQualityTier tier )
{
    switch ( tier )
    {
        case PlaybackQualityTier::Dev: return "Dev";
        case PlaybackQualityTier::DailyUse: return "Daily-Use";
        case PlaybackQualityTier::PinnedClip: return "Pinned-Clip";
    }
    return "Dev";
}

inline const char * playbackPreviewModeName( PlaybackPreviewMode mode )
{
    switch ( mode )
    {
        case PlaybackPreviewMode::SharpSmooth: return "sharp_smooth";
        case PlaybackPreviewMode::AggressivePerformance: return "aggressive_performance";
    }
    return "sharp_smooth";
}

/* Returns the user's persisted QualityMode. Invalid persisted values fall back
 * to the same configured default used when the setting is absent. */
#ifdef QT_CORE_LIB
inline PlaybackQualityMode playbackQualityModeFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    const int raw = set.value( PlaybackQualitySettings::kKeyQualityMode(),
                               PlaybackQualitySettings::kDefaultQualityMode() ).toInt();
    if ( raw < 0 || raw > 4 )
        return static_cast<PlaybackQualityMode>(
            PlaybackQualitySettings::kDefaultQualityMode() );
    return static_cast<PlaybackQualityMode>( raw );
}

inline PlaybackPreviewMode playbackPreviewModeFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    const int raw = set.value( PlaybackQualitySettings::kKeyPreviewMode(),
                               PlaybackQualitySettings::kDefaultPreviewMode() ).toInt();
    return raw == static_cast<int>( PlaybackPreviewMode::AggressivePerformance )
        ? PlaybackPreviewMode::AggressivePerformance
        : PlaybackPreviewMode::SharpSmooth;
}

inline int playbackQualityAutoTargetFpsFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    const int raw = set.value( PlaybackQualitySettings::kKeyAutoTargetFps(),
                               PlaybackQualitySettings::kDefaultAutoTargetFps() ).toInt();
    if ( raw == 24 || raw == 30 || raw == 60 ) return raw;
    return PlaybackQualitySettings::kDefaultAutoTargetFps();
}

inline bool playbackQualityShowIndicatorFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return set.value( PlaybackQualitySettings::kKeyShowQualityIndicator(),
                      PlaybackQualitySettings::kDefaultShowQualityIndicator() ).toBool();
}

inline bool playbackQualityShowExperimentalPhase3ModesFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return playbackQualityPhase3UnattendedEnvOverride()
        || set.value( PlaybackQualitySettings::kKeyShowExperimentalPhase3Modes(),
                      PlaybackQualitySettings::kDefaultShowExperimentalPhase3Modes() ).toBool();
}

inline bool playbackQualityPhase3AcknowledgedFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return playbackQualityPhase3UnattendedEnvOverride()
        || set.value( PlaybackQualitySettings::kKeyPhase3Acknowledged(),
                      PlaybackQualitySettings::kDefaultPhase3Acknowledged() ).toBool();
}

inline void playbackQualityModeWriteToSettings( PlaybackQualityMode mode )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( PlaybackQualitySettings::kKeyQualityMode(),
                  static_cast<int>( mode ) );
}

inline void playbackPreviewModeWriteToSettings( PlaybackPreviewMode mode )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( PlaybackQualitySettings::kKeyPreviewMode(),
                  static_cast<int>( mode ) );
}

inline PlaybackPreviewResolution playbackPreviewResolutionFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    const int raw = set.value( PlaybackQualitySettings::kKeyPreviewResolution(),
                               PlaybackQualitySettings::kDefaultPreviewResolution() ).toInt();
    if ( raw < 0 || raw > 3 ) return PlaybackPreviewResolution::Auto;
    return static_cast<PlaybackPreviewResolution>( raw );
}

inline void playbackPreviewResolutionWriteToSettings( PlaybackPreviewResolution res )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( PlaybackQualitySettings::kKeyPreviewResolution(),
                  static_cast<int>( res ) );
}

/* Maps the GUI enum to the C-side proxy level (mlvSetPlaybackProxyLevel):
 * Auto -> -1, Full -> 0, Half -> 1, Quarter -> 2. */
inline int playbackPreviewResolutionToProxyLevel( PlaybackPreviewResolution res )
{
    switch ( res )
    {
    case PlaybackPreviewResolution::Full:    return 0;
    case PlaybackPreviewResolution::Half:    return 1;
    case PlaybackPreviewResolution::Quarter: return 2;
    case PlaybackPreviewResolution::Auto:
    default:                                 return -1;
    }
}

inline void playbackQualityAutoTargetFpsWriteToSettings( int targetFps )
{
    int v = targetFps;
    if ( v != 24 && v != 30 && v != 60 )
    {
        v = PlaybackQualitySettings::kDefaultAutoTargetFps();
    }
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( PlaybackQualitySettings::kKeyAutoTargetFps(), v );
}

inline void playbackQualityShowIndicatorWriteToSettings( bool show )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( PlaybackQualitySettings::kKeyShowQualityIndicator(),
                  show ? 1 : 0 );
}

inline void playbackQualityShowExperimentalPhase3ModesWriteToSettings( bool show )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( PlaybackQualitySettings::kKeyShowExperimentalPhase3Modes(),
                  show ? 1 : 0 );
}

inline void playbackQualityPhase3AcknowledgedWriteToSettings( bool acknowledged )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( PlaybackQualitySettings::kKeyPhase3Acknowledged(),
                  acknowledged ? 1 : 0 );
}

inline const char * playbackQualityTierKeyForMode( PlaybackQualityMode mode )
{
    return mode == PlaybackQualityMode::Phase3HQ
        ? PlaybackQualitySettings::kKeyPhase3HQTier()
        : PlaybackQualitySettings::kKeyPhase3FastTier();
}

inline const char * playbackQualityTierEnteredAtKeyForMode( PlaybackQualityMode mode )
{
    return mode == PlaybackQualityMode::Phase3HQ
        ? PlaybackQualitySettings::kKeyPhase3HQTierEnteredAt()
        : PlaybackQualitySettings::kKeyPhase3FastTierEnteredAt();
}

inline const char * playbackQualityValidatedClipsKeyForMode( PlaybackQualityMode mode )
{
    return mode == PlaybackQualityMode::Phase3HQ
        ? PlaybackQualitySettings::kKeyPhase3HQDailyUseClipsValidated()
        : PlaybackQualitySettings::kKeyPhase3FastDailyUseClipsValidated();
}

inline const char * playbackQualityAutoFallbackEpochKeyForMode( PlaybackQualityMode mode )
{
    return mode == PlaybackQualityMode::Phase3HQ
        ? PlaybackQualitySettings::kKeyPhase3HQAutoFallbackFiredEpoch()
        : PlaybackQualitySettings::kKeyPhase3FastAutoFallbackFiredEpoch();
}

inline PlaybackQualityTier playbackQualityTierFromSettings( PlaybackQualityMode mode )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    const int raw = set.value( playbackQualityTierKeyForMode( mode ),
                               static_cast<int>( PlaybackQualityTier::Dev ) ).toInt();
    if ( raw < static_cast<int>( PlaybackQualityTier::Dev )
      || raw > static_cast<int>( PlaybackQualityTier::PinnedClip ) )
    {
        return PlaybackQualityTier::Dev;
    }
    return static_cast<PlaybackQualityTier>( raw );
}

inline qint64 playbackQualityTierEnteredAtFromSettings( PlaybackQualityMode mode )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return set.value( playbackQualityTierEnteredAtKeyForMode( mode ), 0 ).toLongLong();
}

inline QStringList playbackQualityValidatedClipsFromSettings( PlaybackQualityMode mode )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return set.value( playbackQualityValidatedClipsKeyForMode( mode ) ).toString()
        .split( QLatin1Char(','), Qt::SkipEmptyParts );
}

inline qint64 playbackQualityAutoFallbackEpochFromSettings( PlaybackQualityMode mode )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return set.value( playbackQualityAutoFallbackEpochKeyForMode( mode ), 0 ).toLongLong();
}

inline void playbackQualityAutoFallbackEpochWriteToSettings( PlaybackQualityMode mode,
                                                             qint64 epochMs )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( playbackQualityAutoFallbackEpochKeyForMode( mode ), epochMs );
}

inline qint64 playbackQualityClipPlaytimeSecondsFromSettings( const QString & fingerprint )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return set.value( QString::fromLatin1( PlaybackQualitySettings::kKeyClipPlaytimePrefix() )
                      + fingerprint, 0 ).toLongLong();
}

inline void playbackQualityTierWriteToSettings( PlaybackQualityMode mode,
                                                PlaybackQualityTier tier,
                                                qint64 nowMs )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( playbackQualityTierKeyForMode( mode ), static_cast<int>( tier ) );
    set.setValue( playbackQualityTierEnteredAtKeyForMode( mode ), nowMs );
}

inline void playbackQualityValidatedClipsWriteToSettings( PlaybackQualityMode mode,
                                                          const QStringList & fingerprints )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( playbackQualityValidatedClipsKeyForMode( mode ),
                  fingerprints.join( QLatin1Char(',') ) );
}

inline void playbackQualityClipPlaytimeSecondsWriteToSettings( const QString & fingerprint,
                                                               qint64 seconds )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( QString::fromLatin1( PlaybackQualitySettings::kKeyClipPlaytimePrefix() )
                  + fingerprint, seconds );
}

inline bool playbackQualityCanPromoteToDailyUse( PlaybackQualityTier tier,
                                                 qint64 tierEnteredAtMs,
                                                 qint64 nowMs,
                                                 qint64 autoFallbackEpochMs )
{
    if ( tier != PlaybackQualityTier::Dev ) return false;
    if ( tierEnteredAtMs <= 0 ) return false;
    if ( nowMs - tierEnteredAtMs < PlaybackQualitySettings::kTierMinimumAgeMs() ) return false;
    if ( autoFallbackEpochMs > 0
      && nowMs - autoFallbackEpochMs < PlaybackQualitySettings::kTierMinimumAgeMs() ) return false;
    return true;
}

inline bool playbackQualityCanPromoteToPinnedClip( PlaybackQualityTier tier,
                                                   qint64 tierEnteredAtMs,
                                                   qint64 nowMs,
                                                   const QStringList & validatedFingerprints,
                                                   const QStringList & requiredFingerprints )
{
    if ( tier != PlaybackQualityTier::DailyUse ) return false;
    if ( tierEnteredAtMs <= 0 ) return false;
    if ( nowMs - tierEnteredAtMs < PlaybackQualitySettings::kTierMinimumAgeMs() ) return false;
    for ( const QString & required : requiredFingerprints )
    {
        if ( !validatedFingerprints.contains( required ) ) return false;
        if ( playbackQualityClipPlaytimeSecondsFromSettings( required )
             < PlaybackQualitySettings::kPinnedClipMinimumSeconds() )
        {
            return false;
        }
    }
    return !requiredFingerprints.isEmpty();
}

inline bool playbackQualityPhase3ModeSelectable( PlaybackQualityMode mode )
{
    if ( !playbackQualityModeIsPhase3( mode ) ) return true;
    return playbackQualityShowExperimentalPhase3ModesFromSettings()
        || playbackQualityTierFromSettings( mode ) >= PlaybackQualityTier::DailyUse;
}

inline Phase3Mode phase3ModeFor( PlaybackQualityMode mode )
{
    if ( !playbackQualityModeIsPhase3( mode ) ) return Phase3Mode::Disabled;
    if ( !playbackQualityPhase3ModeSelectable( mode ) ) return Phase3Mode::Disabled;
    if ( !playbackQualityPhase3AcknowledgedFromSettings() ) return Phase3Mode::Disabled;
    return Phase3Mode::DecodeReconProcess;
}

inline int playbackQualityNextModeForCycle( int currentMode )
{
    int modes[5] = {
        static_cast<int>( PlaybackQualityMode::Fast ),
        static_cast<int>( PlaybackQualityMode::HighQuality ),
        static_cast<int>( PlaybackQualityMode::Auto ),
        0,
        0
    };
    int count = 3;
    if ( playbackQualityPhase3ModeSelectable( PlaybackQualityMode::Phase3Fast ) )
        modes[count++] = static_cast<int>( PlaybackQualityMode::Phase3Fast );
    if ( playbackQualityPhase3ModeSelectable( PlaybackQualityMode::Phase3HQ ) )
        modes[count++] = static_cast<int>( PlaybackQualityMode::Phase3HQ );

    for ( int index = 0; index < count; ++index )
    {
        if ( modes[index] == currentMode )
            return modes[(index + 1) % count];
    }
    return modes[0];
}
#endif // QT_CORE_LIB

/* Effective HQ-mean23 desire considering env override + GUI fallback.
 * Env var takes priority. GUI modes ask for HQ on Dual ISO playback so they
 * avoid the legacy preview-rowscale path's deterministic magenta cast; the
 * active scale policy keeps Fast/Auto playable by reducing earlier in the
 * pipeline. */
inline bool playbackQualityWantsHqMean23( PlaybackQualityMode mode )
{
    /* Env var takes precedence: matches existing
     * dualIsoPlaybackPreferHqMean23ViaEnv() semantics. */
    const char * env = std::getenv("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23");
    if ( env && *env )
    {
        return playbackQualityEnvVarTruthy( env );
    }
    return mode == PlaybackQualityMode::Fast
        || mode == PlaybackQualityMode::HighQuality
        || mode == PlaybackQualityMode::Auto
        || mode == PlaybackQualityMode::Phase3HQ;
}

/* Fast playback quality keeps the dual-ISO HQ shortcut active for the
 * performance-sensitive preview scales. This is a GUI policy knob, not a
 * pixel policy: the actual reconstruction path is still chosen lower in
 * the pipeline. */
inline bool playbackQualityWantsFastDualIsoHqPath( PlaybackQualityMode mode,
                                                   int effectiveScale,
                                                   bool dualIsoActive )
{
    return dualIsoActive
        && mode == PlaybackQualityMode::Fast
        && ( effectiveScale == 2 || effectiveScale == 4 );
}

/* Effective playback scale factor considering env override + GUI fallback.
 * Returns 1, 2, 4, or 8 (clamped). For Auto mode, the dynamic decision is
 * made by the cadence sampler; this returns the mode's *initial* scale
 * factor. */
inline int playbackQualityScaleFactorForMode( PlaybackQualityMode mode,
                                              bool dualIsoActive )
{
    /* Env var takes precedence. */
    const char * env = std::getenv("MLVAPP_PLAYBACK_SCALE_FACTOR");
    if ( env && *env )
    {
        const int v = std::atoi(env);
        if ( v == 1 || v == 2 || v == 4 || v == 8 ) return v;
    }
    switch ( mode )
    {
        case PlaybackQualityMode::Fast:
            /* Fast mode previously returned 1 (full resolution) which made
             * it counterintuitively slower than HQ on Dual ISO content:
             * Fast at scale=1 ran the entire pipeline at full sensor
             * resolution (preview rowscale + debayer + processing + 16->8
             * conversion all at 5K), while HQ at scale=4 ran a more
             * expensive recon but on 1/16 the pixels with debayer
             * bypassed entirely. Net result on the user's 5K M16-1210
             * clip: Fast = 13 fps GUI, HQ = 15 fps GUI — confusing UX.
             *
             * Returning 4 kept Fast fast, but the preview rowscale path
             * carried a deterministic magenta cast on Dual ISO clips. For
             * Dual ISO, prefer scale=8 with HQ+mean23 instead: the early
             * Bayer-domain reduction keeps cadence high while avoiding the
             * rowscale color artifact. Non-Dual ISO Fast stays at scale=4.
             *
             * Users who want full-resolution playback unconditionally
             * can set MLVAPP_PLAYBACK_SCALE_FACTOR=1 (handled above). */
            return dualIsoActive ? 8 : 4;
        case PlaybackQualityMode::HighQuality:
            return 4;
        case PlaybackQualityMode::Auto:
            return 4; /* start optimistic; sampler may downgrade */
        case PlaybackQualityMode::Phase3Fast:
            return 4;
        case PlaybackQualityMode::Phase3HQ:
            return 4;
    }
    (void)dualIsoActive;
    return 1;
}

/* Cadence sampler used by Auto mode. Maintains a sliding window of frame
 * timings and decides which mode the next slot should use.
 *
 * Thread-safety: timer events run on the GUI thread and the read paths
 * from RenderFrameThread don't share state with this sampler, so a simple
 * mutex around the deque is enough. The whole class is intentionally
 * header-only and stateless across runs. */
enum class PlaybackQualityAutoDecisionReason
{
    WarmupHq,
    AggressiveDualIsoDeepHq,
    MissedTargetFast,
    MissedTargetAggressiveDeepHq,
    HeadroomAwaitingValidatedCapability,
    HeadroomNonDualIsoSharperHq,
    SteadyHq
};

inline const char * playbackQualityAutoDecisionReasonName(
    PlaybackQualityAutoDecisionReason reason )
{
    switch ( reason )
    {
        case PlaybackQualityAutoDecisionReason::WarmupHq:
            return "warmup_hq";
        case PlaybackQualityAutoDecisionReason::AggressiveDualIsoDeepHq:
            return "aggressive_dual_iso_deep_hq";
        case PlaybackQualityAutoDecisionReason::MissedTargetFast:
            return "missed_target_fast";
        case PlaybackQualityAutoDecisionReason::MissedTargetAggressiveDeepHq:
            return "missed_target_aggressive_deep_hq";
        case PlaybackQualityAutoDecisionReason::HeadroomAwaitingValidatedCapability:
            return "headroom_waiting_for_validated_capability";
        case PlaybackQualityAutoDecisionReason::HeadroomNonDualIsoSharperHq:
            return "headroom_non_dual_iso_sharper_hq";
        case PlaybackQualityAutoDecisionReason::SteadyHq:
            return "steady_hq";
    }
    return "unknown";
}

inline double playbackQualityFpsEquivalentForFrameMs( double frameMs )
{
    return frameMs > 0.0 ? 1000.0 / frameMs : 0.0;
}

struct PlaybackQualityAutoSampler
{
    static constexpr size_t kSlidingWindow = 16;

    void recordFrameMs( double frameMs )
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        if ( frameMs <= 0.0 ) return;
        m_window.push_back( frameMs );
        if ( m_window.size() > kSlidingWindow ) m_window.pop_front();
    }

    void reset()
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        m_window.clear();
    }

    /* Returns the recommended scale factor for the next slot.
     * targetFps is the user's chosen target (24, 30, or 60).
     * dualIsoActive keeps Auto conservative at scale=4 for throughput; an
     * explicit user x2 override is still honored by the renderer.
     *
     * Logic:
     *   - if Aggressive + Dual ISO -> use deep HQ x8 preview immediately
     *   - if window not full yet -> stay at HQ scale=4 (gather more data)
     *   - else compute avg cadence
     *       because that path moves Bayer reduction before LLRawProc/debayer
     *   - if avg cadence > 1.10 * frame budget (under-meeting target by >10%)
     *       -> downgrade to Fast (scale=4, no HQ) in Sharp/Smooth preview
     *       -> use deep HQ x8 preview in Aggressive preview
     *   - else if avg cadence < 0.65 * frame budget (lots of headroom)
     *       and !dualIsoActive and sharper headroom promotion is allowed
     *       -> upgrade HQ to scale=2 (try sharper)
     *   - else stay at HQ scale=4 */
    struct Decision
    {
        int scaleFactor;       /* 1, 2, 4, or 8 */
        bool useHqMean23;      /* true => HQ + mean23, false => Fast preview */
        PlaybackQualityAutoDecisionReason reason;
        bool sharperHeadroomScaleAllowed;
        double averageFrameMs; /* 0 while still warming up */
        double frameBudgetMs;
        size_t sampleCount;
    };

    Decision decideNextSlot( int targetFps,
                             bool dualIsoActive,
                             bool aggressivePreviewActive = false,
                             bool sharperHeadroomScaleAllowed = false ) const
    {
        std::lock_guard<std::mutex> lock( m_mutex );
        if ( targetFps <= 0 ) targetFps = 30;
        const double frameBudgetMs = 1000.0 / static_cast<double>( targetFps );
        const size_t sampleCount = m_window.size();

        if ( aggressivePreviewActive && dualIsoActive )
        {
            /* Aggressive Dual ISO is the coarse/deep preview mode. Recent
             * measured M16 runs show x8 beats x4 even when x4 is merely near
             * target, because x8 keeps reduction before LLRawProc/debayer and
             * the presentation scaler is now cheap. Start there immediately
             * instead of spending the warmup window in the slower x4 state. */
            return Decision{
                8,
                true,
                PlaybackQualityAutoDecisionReason::AggressiveDualIsoDeepHq,
                sharperHeadroomScaleAllowed,
                0.0,
                frameBudgetMs,
                sampleCount
            };
        }

        if ( sampleCount < kSlidingWindow )
        {
            /* Optimistic Sharp/Smooth start: HQ scale=4 until we have a full window. */
            return Decision{
                4,
                true,
                PlaybackQualityAutoDecisionReason::WarmupHq,
                sharperHeadroomScaleAllowed,
                0.0,
                frameBudgetMs,
                sampleCount
            };
        }

        double sum = 0.0;
        for ( const double v : m_window ) sum += v;
        const double avgMs = sum / static_cast<double>( sampleCount );

        if ( avgMs > frameBudgetMs * 1.10 )
        {
            if ( aggressivePreviewActive )
            {
                /* Missing target in Aggressive preview: keep HQ Dual ISO so
                 * the x8 pre-recon path can reduce in the Bayer domain. */
                return Decision{
                    8,
                    true,
                    PlaybackQualityAutoDecisionReason::MissedTargetAggressiveDeepHq,
                    sharperHeadroomScaleAllowed,
                    avgMs,
                    frameBudgetMs,
                    sampleCount
                };
            }
            /* Missing target in Sharp/Smooth preview: drop to the same x4
             * Fast contract as the fixed Fast mode. */
            return Decision{
                4,
                false,
                PlaybackQualityAutoDecisionReason::MissedTargetFast,
                sharperHeadroomScaleAllowed,
                avgMs,
                frameBudgetMs,
                sampleCount
            };
        }
        if ( !dualIsoActive && avgMs < frameBudgetMs * 0.65 )
        {
            if ( !sharperHeadroomScaleAllowed )
            {
                /* Headroom alone is not capability proof. Keep the safer x4
                 * HQ preview until the caller has observed a validated
                 * presentation path that can justify sharpening Auto. */
                return Decision{
                    4,
                    true,
                    PlaybackQualityAutoDecisionReason::HeadroomAwaitingValidatedCapability,
                    sharperHeadroomScaleAllowed,
                    avgMs,
                    frameBudgetMs,
                    sampleCount
                };
            }
            /* Plenty of headroom on a non-DI clip: try sharper HQ. */
            return Decision{
                2,
                true,
                PlaybackQualityAutoDecisionReason::HeadroomNonDualIsoSharperHq,
                sharperHeadroomScaleAllowed,
                avgMs,
                frameBudgetMs,
                sampleCount
            };
        }
        /* Steady state: HQ scale=4. */
        return Decision{
            4,
            true,
            PlaybackQualityAutoDecisionReason::SteadyHq,
            sharperHeadroomScaleAllowed,
            avgMs,
            frameBudgetMs,
            sampleCount
        };
    }

private:
    mutable std::mutex m_mutex;
    std::deque<double> m_window;
};

struct PlaybackQualityAutoCapabilityTracker
{
    bool notePresentedPipeline( bool gpuTextureNoReadbackActive,
                                bool gpuTextureNoReadbackCandidate = false,
                                bool dualIsoActive = false )
    {
        m_lastObservationDemotedCapability = false;
        if ( gpuTextureNoReadbackActive )
        {
            m_validatedNoReadbackObserved = true;
            if ( !dualIsoActive )
            {
                m_validatedNoReadbackObservedForHeadroom = true;
            }
            return m_validatedNoReadbackObserved;
        }
        if ( gpuTextureNoReadbackCandidate && m_validatedNoReadbackObserved )
        {
            m_validatedNoReadbackObserved = false;
            m_validatedNoReadbackObservedForHeadroom = false;
            m_lastObservationDemotedCapability = true;
        }
        return m_validatedNoReadbackObserved;
    }

    bool sharperHeadroomScaleAllowed() const
    {
        return m_validatedNoReadbackObservedForHeadroom;
    }

    bool validatedNoReadbackObserved() const
    {
        return m_validatedNoReadbackObserved;
    }

    bool lastObservationDemotedCapability() const
    {
        return m_lastObservationDemotedCapability;
    }

    void reset()
    {
        m_validatedNoReadbackObserved = false;
        m_validatedNoReadbackObservedForHeadroom = false;
        m_lastObservationDemotedCapability = false;
    }

private:
    bool m_validatedNoReadbackObserved = false;
    bool m_validatedNoReadbackObservedForHeadroom = false;
    bool m_lastObservationDemotedCapability = false;
};

#endif // PLAYBACKQUALITYPOLICY_H
