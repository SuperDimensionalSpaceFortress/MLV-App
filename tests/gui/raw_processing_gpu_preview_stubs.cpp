#include "../../src/mlv/llrawproc/llrawproc.h"

#include <cstring>

extern "C" void processingGamutRgbToY(int, double out_rgb_to_Y[3])
{
    out_rgb_to_Y[0] = 0.2126729;
    out_rgb_to_Y[1] = 0.7151522;
    out_rgb_to_Y[2] = 0.0721750;
}

extern "C" void processingAgxMatrices(double out_forward[9], double out_inverse[9])
{
    for (int i = 0; i < 9; ++i) {
        out_forward[i] = (i % 4) == 0 ? 1.0 : 0.0;
        out_inverse[i] = out_forward[i];
    }
}

extern "C" int processingHasShadowsHighlightsAdjustments(const processingObject_t *)
{
    return 0;
}

/* GpuPreviewProcessing.cpp's gpuPreviewProcessingApplyCpuRoute asks the engine
 * (CUDA-PLAYBACK-LOOK-PARITY-2); the GUI harness links no engine. 1 = the
 * display shader's historical unconditional pre-camera clamp. */
extern "C" int processingCpuRoutePreCameraClamps(const processingObject_t *, int)
{
    return 1;
}

extern "C" int processingGetShadowsHighlightsBlurData(
    const processingObject_t *,
    const uint16_t **data,
    int *width,
    int *height,
    int *curveIndexMask)
{
    if (data) {
        *data = nullptr;
    }
    if (width) {
        *width = 0;
    }
    if (height) {
        *height = 0;
    }
    if (curveIndexMask) {
        *curveIndexMask = 0;
    }
    return 0;
}

/* PLAYBACK-SH-OFF-CPU-PATH-1: quarter-res S/H frame state. No engine, so never ready. */
extern "C" int processingGetShadowsHighlightsQuarterBlurData(
    const processingObject_t *,
    const uint16_t **data,
    int *quarterWidth,
    int *quarterHeight,
    int *frameWidth,
    int *frameHeight)
{
    if (data) {
        *data = nullptr;
    }
    if (quarterWidth) {
        *quarterWidth = 0;
    }
    if (quarterHeight) {
        *quarterHeight = 0;
    }
    if (frameWidth) {
        *frameWidth = 0;
    }
    if (frameHeight) {
        *frameHeight = 0;
    }
    return 0;
}

extern "C" int processingExpandShadowsHighlightsQuarterBlur(
    const uint16_t *, int, int, uint16_t *, int, int, int)
{
    return 0;
}

extern "C" int llrpGpuPlaybackReconRunGlTexture(
    const llrpGpuPlaybackReconState_t *,
    const uint16_t *,
    size_t,
    unsigned int,
    int *rc_out,
    llrpGpuPlaybackReconTiming_t *timing_out)
{
    if (rc_out) {
        *rc_out = -1;
    }
    if (timing_out) {
        std::memset(timing_out, 0, sizeof(*timing_out));
    }
    return 0;
}

extern "C" int llrpGpuPlaybackReconResetGlTextureResources(void)
{
    return 0;
}

extern "C" int llrpGpuPlaybackReconRunDeviceBayer16(
    const llrpGpuPlaybackReconState_t *,
    const uint16_t *,
    size_t,
    const uint16_t **device_bayer16_out,
    int *width_out,
    int *height_out,
    int *rc_out,
    llrpGpuPlaybackReconTiming_t *timing_out)
{
    if (device_bayer16_out) {
        *device_bayer16_out = nullptr;
    }
    if (width_out) {
        *width_out = 0;
    }
    if (height_out) {
        *height_out = 0;
    }
    if (rc_out) {
        *rc_out = -1;
    }
    if (timing_out) {
        std::memset(timing_out, 0, sizeof(*timing_out));
    }
    return 0;
}

extern "C" int llrpGpuPlaybackReconCopyLastDeviceBayer16ToGlTexture(
    unsigned int,
    int *rc_out)
{
    if (rc_out) {
        *rc_out = -1;
    }
    return 0;
}
