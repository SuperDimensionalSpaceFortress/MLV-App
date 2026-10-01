#ifndef GPUPREVIEWHOSTROUTE_H
#define GPUPREVIEWHOSTROUTE_H

#include "GpuPreviewProcessing.h"
#include "../../src/mlv_include.h"

/* CUDA-PLAYBACK-LOOK-PARITY-2: the route the CPU preview engine takes for the
 * clip, scale and receipt being presented, as the host hands it to the display
 * shader. Header-only so RenderFrameThread (the one call site) and the tests run
 * the SAME code. The engine (mlvPreviewPlaybackCpuRoute) owns the question,
 * including the playback-preview envelope it must be asked in: this host renders
 * OutputDebayered16, where the thread-local preview mode is OFF, and the engine's
 * cheapness gate answers "cheap" in that state. Never evaluate the route here
 * with the thread's own state, and never from anywhere but this helper. */
struct GpuPreviewHostCpuRoute
{
    bool direct8 = false;        /* the CPU preview would run the direct-8-bit kernel */
    bool preCameraClamp = true;  /* ... and that route clamps the WB output before the camera matrix */
};

/* phase3RawConsumed: the frame being presented consumed Phase 3 decoded /
 * reconstructed raw (RenderFrameThread::drawFrame's decodedRawFrame != nullptr),
 * i.e. the CPU would render it through the raw entries, whose route differs from
 * the dispatch at scale > 1 (see mlvPreviewPlaybackCpuRoute). */
inline GpuPreviewHostCpuRoute gpuPreviewHostCpuRoute(mlvObject_t * video,
                                                     int playbackScaleFactor,
                                                     bool phase3RawConsumed)
{
    GpuPreviewHostCpuRoute route;
    if ( !video ) return route;
    int clamp = 1;
    route.direct8 = mlvPreviewPlaybackCpuRoute(video, playbackScaleFactor,
                                               phase3RawConsumed ? 1 : 0, &clamp) != 0;
    route.preCameraClamp = clamp != 0;
    return route;
}

inline void gpuPreviewHostApplyCpuRoute(GpuPreviewProcessingConfig * config,
                                        const GpuPreviewHostCpuRoute & route)
{
    if ( config ) config->preCameraClamp = route.preCameraClamp;
}

#endif /* GPUPREVIEWHOSTROUTE_H */
