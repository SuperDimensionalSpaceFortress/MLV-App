/*!
 * \file PlaybackSwapInterval.h
 * \brief PLAYBACK-VSYNC-DEFAULT-1: the swap interval every playback GL surface requests,
 *        and a readback of the interval the driver really holds.
 */

#ifndef PLAYBACKSWAPINTERVAL_H
#define PLAYBACKSWAPINTERVAL_H

#include <QByteArray>
#include <QtGlobal>

/*! \brief MLVAPP_SWAP_INTERVAL: "1" requests vsync (swap interval 1) on every playback GL
 *  surface; "0", unset or any other value keeps the shipping interval 0. A measurement
 *  switch for the vsync A/B, not a user setting. */
inline int playbackSwapIntervalFromEnvValue(const QByteArray &value)
{
    return value.trimmed() == "1" ? 1 : 0;
}

/*! \brief The interval to request, read once per process. */
inline int playbackSwapInterval()
{
    static const int interval = playbackSwapIntervalFromEnvValue(qgetenv("MLVAPP_SWAP_INTERVAL"));
    return interval;
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
