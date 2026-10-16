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
#include "ZebraThresholds.h"

#include <QByteArray>
#include <QDebug>
#include <QRect>
#include <QSize>
#include <QString>
#include <QtGlobal>

#include <cstdint>
#include <vector>

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

/*! \brief MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK, the measurement override (automation runs only,
 *  see playbackScopesModeEnvOverride): a valid token wins over the setting; unset or anything else
 *  is no override. Then the setting; an unreadable setting is the default. */
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

/*! \brief PSO-01 (sol r3): the measurement override is honoured only in an automation run (main.cpp's
 *  automationRun: --profile-playback, --gui-smoke-playback or MLVAPP_AUTOPLAY_SECONDS, which is what puts
 *  the app on its run-scoped settings store, automation_settings::isolated()). An ordinary launch that
 *  inherited the variable ignores it, so the stored setting (or the Hide default) wins. -1 = no override. */
inline int playbackScopesModeEnvOverride( const QByteArray &envValue, bool automationRun )
{
    return automationRun ? playbackScopesModeFromToken( envValue ) : -1;
}

/*! \brief The mode in effect at startup: the measurement override in an automation run, else the setting,
 *  else Hide. An honoured override is disclosed on one log line. */
inline PlaybackScopesMode playbackScopesMode()
{
    const int envOverride = playbackScopesModeEnvOverride(
        qgetenv( "MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK" ), automation_settings::isolated() );
    if ( envOverride >= 0 )
    {
        qInfo().noquote()
            << "MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK ="
            << playbackScopesModeToken( static_cast<PlaybackScopesMode>( envOverride ) )
            << "(measurement override, automation run; Playback/ScopesDuringPlayback setting is bypassed).";
    }
    return playbackScopesModeResolve( envOverride, playbackScopesModeSettingValue() );
}

/*! \brief PLAYBACK-GPU-SCOPES-NO-VETO-1: MLVAPP_PLAYBACK_SCOPE_GPU_TAP is an ordinary kill switch for the
 *  GPU scope read-back, honoured in any run: exactly "0" (surrounding space ignored) turns it off. Unset or
 *  anything else leaves the read-back on. */
inline const char * playbackScopeGpuTapKillSwitchName() { return "MLVAPP_PLAYBACK_SCOPE_GPU_TAP"; }

inline bool playbackScopeGpuTapKillSwitch( const QByteArray &env )
{
    return env.trimmed() == "0";
}

/*! \brief PLAYBACK-GPU-SCOPES-NO-VETO-1: the live state that decides whether the Throttled scope is fed by a
 *  read-back of the picture the active GL surface drew (a small async PBO read, see GpuScopeTap) instead of
 *  CPU-side presented pixels. r2: the active surface is the QOpenGLWidget viewport or GpuDisplayWindow, which
 *  ever presents (MainWindow asks the window when it is active, else the viewport); tapSupported and
 *  tapRectCapturable come from that surface. textureRouteEligibleIgnoringScopes is the GPU recon texture-route
 *  table asked with no scope (it must never ask the scope funnel itself: that funnel asks this). */
struct PlaybackScopeGpuTapInputs
{
    bool killSwitch = false;
    PlaybackScopesMode mode = PlaybackScopesMode::Hide;
    bool playbackActive = false;
    bool editAreaVisible = false;
    bool anyScopeChecked = false;
    bool gpuWidgetViewportInstalled = false;
    bool gpuDisplayWindowActive = false;
    bool zebrasEnabled = false;
    bool tapSupported = false;
    bool tapRectCapturable = false;
    bool textureRouteEligibleIgnoringScopes = false;
};

/*! \brief Does the GPU read-back serve the scope right now? Only Throttled, only while playing with a visible
 *  scope on a GL surface (the QOpenGLWidget viewport or GpuDisplayWindow), with zebras off (the shader draws them
 *  into the same picture), a context that can do the async read, a drawn picture the read-back can capture
 *  (zoomed past fit it cannot: CoS #447 blocker 5, the scope would freeze), and a texture route that would be
 *  eligible without the scope. Anything else keeps today's CPU scope and its veto. */
inline bool playbackScopeGpuTapServes( const PlaybackScopeGpuTapInputs &in )
{
    return in.mode == PlaybackScopesMode::Throttled
        && in.playbackActive
        && in.editAreaVisible
        && in.anyScopeChecked
        && ( in.gpuWidgetViewportInstalled || in.gpuDisplayWindowActive )
        && !in.zebrasEnabled
        && in.tapSupported
        && in.tapRectCapturable
        && in.textureRouteEligibleIgnoringScopes
        && !in.killSwitch;
}

/*! \brief Does a visible scope consume presented pixels right now? A scope is shown only with
 *  the edit area visible; while playback runs in Hide mode it does no work, so it neither
 *  computes nor vetoes the GPU fast paths. Paused or stopped, it always draws.
 *  PLAYBACK-GPU-SCOPES-NO-VETO-1: while playing, a scope the GPU read-back serves (gpuTapServes)
 *  consumes no presented pixels either, so it does not veto the GPU texture route. */
inline bool playbackScopeDisplayConsumesPresentedPixels(
    bool editAreaVisible,
    bool playbackActive,
    PlaybackScopesMode mode,
    bool gpuTapServes = false)
{
    return editAreaVisible
        && !( playbackActive && ( mode == PlaybackScopesMode::Hide || gpuTapServes ) );
}

/*! \brief The read-back's size: the drawn image rectangle scaled to at most maxW wide, aspect kept,
 *  each side at least 1. */
inline QSize playbackScopeTapSize( int rectW, int rectH, int maxW = 512 )
{
    if ( rectW <= 0 || rectH <= 0 ) return QSize( 1, 1 );
    const int w = qMax( 1, qMin( rectW, qMax( 1, maxW ) ) );
    const int h = qMax( 1, qRound( static_cast<double>( rectH ) * w / rectW ) );
    return QSize( w, h );
}

/*! \brief The drawn image rectangle (widget-top-down framebuffer pixels, edges as drawn) as a GL
 *  read rectangle (bottom-up rows). False when the rounded rectangle is empty or not wholly inside the
 *  framebuffer (zoomed past fit): the read-back is then skipped, never clipped. */
inline bool playbackScopeTapFramebufferRect( double leftPx, double topPx, double rightPx, double bottomPx,
                                             int fbW, int fbH, QRect *glRectOut )
{
    const int x0 = qRound( leftPx );
    const int x1 = qRound( rightPx );
    const int yTop = qRound( topPx );
    const int yBottom = qRound( bottomPx );
    if ( fbW <= 0 || fbH <= 0 || x1 <= x0 || yBottom <= yTop ) return false;
    if ( x0 < 0 || yTop < 0 || x1 > fbW || yBottom > fbH ) return false;
    if ( glRectOut ) *glRectOut = QRect( x0, fbH - yBottom, x1 - x0, yBottom - yTop );
    return true;
}

/*! \brief r2 (A4): GpuDisplayWindow's letterboxed picture as the read-back's image rectangle. The window draws the
 *  frame as an NDC quad of +-sx by +-sy over its whole fbW x fbH framebuffer, so in top-down framebuffer pixels the
 *  picture runs from (1-sx)/2 to (1+sx)/2 of the width and (1-sy)/2 to (1+sy)/2 of the height. With sx, sy <= 1
 *  (a fit, never a zoom) it is always inside the framebuffer. */
inline void playbackScopeTapLetterboxRect( double sx, double sy, int fbW, int fbH,
                                           double *leftPx, double *topPx, double *rightPx, double *bottomPx )
{
    if ( leftPx ) *leftPx = ( 1.0 - sx ) * 0.5 * fbW;
    if ( rightPx ) *rightPx = ( 1.0 + sx ) * 0.5 * fbW;
    if ( topPx ) *topPx = ( 1.0 - sy ) * 0.5 * fbH;
    if ( bottomPx ) *bottomPx = ( 1.0 + sy ) * 0.5 * fbH;
}

/*! \brief glReadPixels RGBA8 rows (bottom-up) to the RGB8 interleaved top-down rows ScopesLabel::setScope takes.
 *  Pixels only: the clip flags come from the scope feed's own rule (playbackScopeFeedUnderOver), never from here. */
inline void playbackScopeTapRgbaBottomUpToRgb8( const uint8_t *rgba, int w, int h, std::vector<uint8_t> *rgb8 )
{
    if ( rgb8 ) rgb8->assign( static_cast<size_t>( qMax( 0, w ) ) * static_cast<size_t>( qMax( 0, h ) ) * 3u, 0 );
    if ( rgba && rgb8 && w > 0 && h > 0 )
    {
        for ( int y = 0; y < h; ++y )
        {
            const uint8_t *src = rgba + static_cast<size_t>( h - 1 - y ) * static_cast<size_t>( w ) * 4u;
            uint8_t *dst = rgb8->data() + static_cast<size_t>( y ) * static_cast<size_t>( w ) * 3u;
            for ( int x = 0; x < w; ++x )
            {
                for ( int c = 0; c < 3; ++c ) dst[ x * 3 + c ] = src[ x * 4 + c ];
            }
        }
    }
}

/*! \brief The CPU feed's zebra scan (1=under, 2=over, 3=both, 0=neither) over RGB8 interleaved pixels: HSL
 *  lightness (max+min)/2 against preview_zebra's 8-bit thresholds. MainWindow's scanZebrasRgb8 is this rule (it
 *  delegates here), and applyZebrasToImage applies the same rule while painting the zebras. */
inline uint8_t playbackScopeZebraScanRgb8( const uint8_t *rgb8, int w, int h )
{
    uint8_t underOver = 0;
    if ( !rgb8 || w <= 0 || h <= 0 ) return underOver;
    const int pixelCount = w * h;
    for ( int i = 0; i < pixelCount; ++i )
    {
        const uint8_t red = rgb8[ i * 3 + 0 ];
        const uint8_t green = rgb8[ i * 3 + 1 ];
        const uint8_t blue = rgb8[ i * 3 + 2 ];
        const int maxChannel = qMax( red, qMax( green, blue ) );
        const int minChannel = qMin( red, qMin( green, blue ) );
        const int lightness = ( maxChannel + minChannel ) / 2;
        if ( lightness >= preview_zebra::kOverThreshold8Bit ) underOver |= 0x02;
        if ( lightness <= preview_zebra::kUnderThreshold8Bit ) underOver |= 0x01;
    }
    return underOver;
}

/*! \brief The draw gate's decode of a frame's underOver into setScope's clip flags. */
inline void playbackScopeUnderOverFlags( uint8_t underOver, bool *under, bool *over )
{
    if ( under ) *under = ( underOver & 0x01 ) == 0x01;
    if ( over ) *over = ( underOver & 0x02 ) == 0x02;
}

/*! \brief CoS #447 blocker 1: the clip flags a scope feed passes to ScopesLabel::setScope. ONE rule for every feed,
 *  the CPU feed's: with zebras off the frame's underOver is 0 (applyZebrasToImage returns early, scanZebrasRgb8 runs
 *  only under GPU shader zebras), so no clip markers; with zebras on, the zebra scan. The read-back serves only with
 *  zebras off, so it always passes under=false, over=false -- never a per-channel 0/255 rule. */
inline void playbackScopeFeedUnderOver( bool zebrasEnabled, const uint8_t *rgb8, int w, int h, bool *under, bool *over )
{
    playbackScopeUnderOverFlags( zebrasEnabled ? playbackScopeZebraScanRgb8( rgb8, w, h ) : uint8_t( 0 ), under, over );
}

/*! \brief Which scope a feed draws, from the View menu checks, in the draw gate's order (histogram, waveform,
 *  parade, vectorscope). None when no scope is checked. Both feeds ask this (CoS #447 parity). */
enum class PlaybackScopeKind
{
    None = 0,
    Histogram,
    WaveForm,
    RgbParade,
    VectorScope
};

inline PlaybackScopeKind playbackScopeKindForChecks( bool histogram, bool waveForm, bool parade, bool vectorScope )
{
    return histogram ? PlaybackScopeKind::Histogram
         : waveForm ? PlaybackScopeKind::WaveForm
         : parade ? PlaybackScopeKind::RgbParade
         : vectorScope ? PlaybackScopeKind::VectorScope
         : PlaybackScopeKind::None;
}

/*! \brief Redraw the stopped frame for its scope when Play stops? Hide skipped scope work while playing
 *  (#427), and a play the GPU read-back served showed a downscaled read-back: both redraw the stopped
 *  frame through the CPU path, which draws the full-resolution scope. Only with the edit area shown. */
inline bool playbackScopesNeedStopRefresh( PlaybackScopesMode mode, bool tapServedThisPlay, bool editAreaChecked )
{
    return editAreaChecked && ( mode == PlaybackScopesMode::Hide || tapServedThisPlay );
}

/*! \brief PSO-02 (sol r3): may a presented frame feed the scope? Not when the request that rendered it was built
 *  with no scope consumer on a GPU surface (mainWindowAllowsGpu16PreviewRender of its policy): such a frame may have
 *  taken a route that never developed its RGB8 (GPU Tex NR), so after a switch from Hide to Throttled or Live (or a
 *  scope turned on) it is presented but not measured. The next frame, requested under the new policy, supplies it. */
inline bool playbackScopeFrameFeedsScope(
    bool scopeDisplayConsumesNow,
    bool frameRequestAllowedRouteWithoutRgb8)
{
    return scopeDisplayConsumesNow && !frameRequestAllowedRouteWithoutRgb8;
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
