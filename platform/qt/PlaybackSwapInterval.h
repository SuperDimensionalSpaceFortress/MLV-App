/*!
 * \file PlaybackSwapInterval.h
 * \brief PLAYBACK-VSYNC-DEFAULT-1: the swap interval every playback GL surface requests,
 *        and a readback of the interval the driver really holds.
 */

#ifndef PLAYBACKSWAPINTERVAL_H
#define PLAYBACKSWAPINTERVAL_H

#include "AutomationSettings.h"

#include <QByteArray>
#include <QtGlobal>

/*! \brief The persisted "VSync (prevent tearing)" playback setting. On (swap interval 1) by
 *  default: at interval 0 the overlay-promoted playback surface tore on every present, windowed
 *  and fullscreen, and interval 1 cost nothing measurable (PLAYBACK-VSYNC-DEFAULT-1 r1/r1b). */
namespace PlaybackSwapIntervalSettings
{
    inline constexpr const char * kKeyVSync() { return "Playback/VSync"; }
    inline constexpr bool kDefaultVSync() { return true; }
}

/*! \brief MLVAPP_SWAP_INTERVAL, the measurement override for the vsync A/B: "0" or "1" wins
 *  over the setting; unset or any other value is -1, no override. */
inline int playbackSwapIntervalFromEnvValue(const QByteArray &value)
{
    const QByteArray trimmed = value.trimmed();
    if ( trimmed == "1" ) return 1;
    if ( trimmed == "0" ) return 0;
    return -1;
}

/*! \brief The env override when there is one, else the setting. */
inline int playbackSwapIntervalResolve(int envOverride, bool vsyncSetting)
{
    if ( envOverride >= 0 ) return envOverride;
    return vsyncSetting ? 1 : 0;
}

inline bool playbackVSyncFromSettings()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return set.value( PlaybackSwapIntervalSettings::kKeyVSync(),
                      PlaybackSwapIntervalSettings::kDefaultVSync() ).toBool();
}

inline void playbackVSyncWriteToSettings( bool vsync )
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue( PlaybackSwapIntervalSettings::kKeyVSync(), vsync );
}

/*! \brief The interval a NEW playback GL surface requests. The env is read once per process;
 *  the setting is read on every call. Every surface is created once per process, so a changed
 *  setting takes effect at the next app start. */
inline int playbackSwapInterval()
{
    static const int envOverride = playbackSwapIntervalFromEnvValue(qgetenv("MLVAPP_SWAP_INTERVAL"));
    return playbackSwapIntervalResolve(envOverride, playbackVSyncFromSettings());
}

/*! \brief The swap interval the driver holds for the CURRENT context and drawable, from
 *  WGL_EXT_swap_control's wglGetSwapIntervalEXT. Qt's realized QSurfaceFormat is not proof
 *  of it (Qt reported 1 while wglSwapIntervalEXT(0) had really set 0). Pass
 *  QOpenGLContext::getProcAddress("wglGetSwapIntervalEXT") with that context current.
 *  -1 means the entry point is unavailable (or not Windows). */
inline int wglSwapIntervalActual(QFunctionPointer getSwapIntervalExt)
{
#ifdef Q_OS_WIN
    if ( !getSwapIntervalExt ) return -1;
    typedef int (__stdcall *WglGetSwapIntervalExtFn)(void);
    return reinterpret_cast<WglGetSwapIntervalExtFn>(getSwapIntervalExt)();
#else
    Q_UNUSED(getSwapIntervalExt);
    return -1;
#endif
}

#endif // PLAYBACKSWAPINTERVAL_H
