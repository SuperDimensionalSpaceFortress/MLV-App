/*!
 * \file MainWindowGpuPreviewPolicy.h
 * \author Codex
 * \copyright 2026
 * \brief Shared GPU preview policy helpers for MainWindow and GUI tests.
 */

#ifndef MAINWINDOWGPUPREVIEWPOLICY_H
#define MAINWINDOWGPUPREVIEWPOLICY_H

#include "GpuDisplayViewport.h"
#include "../../src/batch/RawAspectStretchPolicy.h"
#include <QSize>
#include <Qt>
#include <cmath>
#include <limits>

enum class GpuPreviewProcessingBackendRequest
{
    Auto = 0,
    Cpu,
    Gpu
};

enum class GpuBilinearDebayerBackendRequest
{
    Auto = 0,
    Cpu,
    Gpu
};

enum class GpuAmazeDebayerBackendRequest
{
    Auto = 0,
    Cpu,
    Gpu
};

enum class GpuPlaybackPipelineStatus
{
    Cpu = 0,
    GpuPreview,
    GpuReconReadback,
    GpuTextureReadback,
    GpuTextureNoReadback
};

struct MainWindowGpuPreviewPolicyState
{
    bool gpuViewportInstalled = false;
    bool gpuWidgetViewportInstalled = false;
    GpuPreviewProcessingBackendRequest gpuPreviewProcessingBackendRequest =
        GpuPreviewProcessingBackendRequest::Auto;
    bool gpuPreviewProcessingEnvironmentRequested = false;
    bool gpuPreviewProcessingCompatible = false;
    GpuBilinearDebayerBackendRequest gpuBilinearDebayerBackendRequest =
        GpuBilinearDebayerBackendRequest::Auto;
    bool gpuBilinearDebayerEnvironmentRequested = false;
    bool gpuBilinearDebayerCompatible = false;
    GpuAmazeDebayerBackendRequest gpuAmazeDebayerBackendRequest =
        GpuAmazeDebayerBackendRequest::Auto;
    bool gpuAmazeDebayerEnvironmentRequested = false;
    bool gpuAmazeDebayerCompatible = false;
    bool gpuAmazeTexturePresentationEnvironmentRequested = false;
    bool gpuPlaybackReconEnvironmentRequested = false;
    bool gpuPlaybackReconTexturePresentationEnvironmentRequested = false;
    bool gpuPlaybackReconTexturePresentationCompatible = false;
    bool histogramEnabled = false;
    bool waveformEnabled = false;
    bool paradeEnabled = false;
    bool vectorScopeEnabled = false;
    bool renderThreadUsing16BitPreview = false;
    bool renderThreadUsingGpuProcessingPreview = false;
    bool renderThreadUsingGpuBilinearDebayer = false;
    bool renderThreadUsingGpuAmazeDebayer = false;
    bool renderThreadUsingGpuAmazeTexturePresentation = false;
    bool renderThreadUsingGpuPlaybackReconTexturePresentation = false;
    int playbackScaleFactorActive = 1;
    bool betterResizerEnabled = false;
    bool zebrasEnabled = false;
    Qt::TransformationMode transformationMode = Qt::FastTransformation;
};

inline bool mainWindowScopeActionConsumesPresentedPixels(
    bool editAreaVisible,
    bool scopeActionChecked)
{
    return editAreaVisible && scopeActionChecked;
}

inline bool mainWindowHasScopeVisualization(
    const MainWindowGpuPreviewPolicyState &state)
{
    return state.histogramEnabled
        || state.waveformEnabled
        || state.paradeEnabled
        || state.vectorScopeEnabled;
}

inline bool mainWindowAllowsGpu16PreviewRender(
    const MainWindowGpuPreviewPolicyState &state)
{
    return state.gpuViewportInstalled && !mainWindowHasScopeVisualization(state);
}

inline bool mainWindowUsesGpu16PreviewPresentation(
    const MainWindowGpuPreviewPolicyState &state)
{
    return mainWindowAllowsGpu16PreviewRender(state)
        && state.renderThreadUsing16BitPreview;
}

inline bool mainWindowAllowsGpuPreviewProcessing(
    const MainWindowGpuPreviewPolicyState &state)
{
    bool gpuPreviewProcessingRequested = false;
    switch (state.gpuPreviewProcessingBackendRequest)
    {
    case GpuPreviewProcessingBackendRequest::Auto:
        gpuPreviewProcessingRequested = state.gpuPreviewProcessingEnvironmentRequested;
        break;
    case GpuPreviewProcessingBackendRequest::Cpu:
        gpuPreviewProcessingRequested = false;
        break;
    case GpuPreviewProcessingBackendRequest::Gpu:
        gpuPreviewProcessingRequested = true;
        break;
    }

    return mainWindowAllowsGpu16PreviewRender(state)
        && gpuPreviewProcessingRequested
        && state.gpuPreviewProcessingCompatible;
}

inline bool mainWindowUsesGpuPreviewProcessing(
    const MainWindowGpuPreviewPolicyState &state)
{
    return mainWindowAllowsGpuPreviewProcessing(state)
        && mainWindowUsesGpu16PreviewPresentation(state)
        && state.renderThreadUsingGpuProcessingPreview;
}

inline bool mainWindowAllowsGpuBilinearDebayer(
    const MainWindowGpuPreviewPolicyState &state)
{
    bool gpuBilinearDebayerRequested = false;
    switch (state.gpuBilinearDebayerBackendRequest)
    {
    case GpuBilinearDebayerBackendRequest::Auto:
        gpuBilinearDebayerRequested = state.gpuBilinearDebayerEnvironmentRequested;
        break;
    case GpuBilinearDebayerBackendRequest::Cpu:
        gpuBilinearDebayerRequested = false;
        break;
    case GpuBilinearDebayerBackendRequest::Gpu:
        gpuBilinearDebayerRequested = true;
        break;
    }

    return mainWindowAllowsGpuPreviewProcessing(state)
        && gpuBilinearDebayerRequested
        && state.gpuBilinearDebayerCompatible;
}

inline bool mainWindowUsesGpuBilinearDebayer(
    const MainWindowGpuPreviewPolicyState &state)
{
    return mainWindowUsesGpuPreviewProcessing(state)
        && mainWindowAllowsGpuBilinearDebayer(state)
        && state.renderThreadUsingGpuBilinearDebayer;
}

inline bool mainWindowAllowsGpuAmazeDebayer(
    const MainWindowGpuPreviewPolicyState &state)
{
    bool gpuAmazeDebayerRequested = false;
    switch (state.gpuAmazeDebayerBackendRequest)
    {
    case GpuAmazeDebayerBackendRequest::Auto:
        gpuAmazeDebayerRequested = state.gpuAmazeDebayerEnvironmentRequested;
        break;
    case GpuAmazeDebayerBackendRequest::Cpu:
        gpuAmazeDebayerRequested = false;
        break;
    case GpuAmazeDebayerBackendRequest::Gpu:
        gpuAmazeDebayerRequested = true;
        break;
    }

    return mainWindowAllowsGpuPreviewProcessing(state)
        && gpuAmazeDebayerRequested
        && state.gpuAmazeDebayerCompatible;
}

inline bool mainWindowUsesGpuAmazeDebayer(
    const MainWindowGpuPreviewPolicyState &state)
{
    return mainWindowUsesGpuPreviewProcessing(state)
        && mainWindowAllowsGpuAmazeDebayer(state)
        && state.renderThreadUsingGpuAmazeDebayer;
}

inline bool mainWindowAllowsGpuAmazeTexturePresentation(
    const MainWindowGpuPreviewPolicyState &state)
{
    // CUDA-SCALE4-ZERO-PRESENT-1: presentAmazePostWbTexture/presentRgb16 have no
    // GpuDisplayWindow routing (see root cause), so the AMaZE-texture-present
    // route requires the QOpenGLWidget viewport unconditionally.
    return mainWindowUsesGpuAmazeDebayer(state)
        && state.gpuAmazeTexturePresentationEnvironmentRequested
        && state.gpuWidgetViewportInstalled;
}

inline bool mainWindowUsesGpuAmazeTexturePresentation(
    const MainWindowGpuPreviewPolicyState &state)
{
    return mainWindowAllowsGpuAmazeTexturePresentation(state)
        && state.renderThreadUsingGpuAmazeTexturePresentation;
}

inline bool mainWindowAllowsGpuPlaybackReconTexturePresentation(
    const MainWindowGpuPreviewPolicyState &state)
{
    return mainWindowAllowsGpu16PreviewRender(state)
        && state.gpuPlaybackReconEnvironmentRequested
        && state.gpuPlaybackReconTexturePresentationEnvironmentRequested
        && state.gpuPlaybackReconTexturePresentationCompatible;
}

inline bool mainWindowUsesGpuPlaybackReconTexturePresentation(
    const MainWindowGpuPreviewPolicyState &state)
{
    return mainWindowAllowsGpuPlaybackReconTexturePresentation(state)
        && state.renderThreadUsingGpuPlaybackReconTexturePresentation;
}

// CUDA-S4-TEXTURE-ROUTE-CLAMP-1: the GPU recon texture-present route is only
// wired for playbackScaleFactor == 1. Rather than gate every '== 1' call site
// individually, the effective playback scale request is clamped to 1 at its
// policy source whenever that route would otherwise be armed. requestedScale
// is returned unchanged for every scale when the route is not eligible at
// scale 1, and for scale 1 itself (a no-op clamp).
inline int mainWindowClampPlaybackScaleForGpuTextureRoute(
    int requestedScale,
    bool gpuPlaybackReconTextureRouteEligibleAtScaleOne)
{
    if (requestedScale != 1 && gpuPlaybackReconTextureRouteEligibleAtScaleOne)
    {
        return 1;
    }
    return requestedScale;
}

// CUDA-S4-TEXTURE-ROUTE-CLAMP-1 round 2: pure decision table backing
// MainWindow::gpuPlaybackReconTextureRouteEligibleAtScaleOne(), pulled out so it
// can be unit-tested without a live MainWindow/processing library. libraryReady
// must gate everything else -- before initLib() has run, m_pProcessingObject
// (gpuPreviewProcessingSupported's input) has no default member initializer, so
// evaluating any of the other inputs first would be an indeterminate-pointer
// read, not a null one.
inline bool mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
    bool libraryReady,
    bool gpuPreviewSurfaceActive,
    bool hasScopeVisualization,
    bool reconRequestedByEnvironment,
    bool texturePresentRequestedByEnvironment,
    bool gpuPreviewProcessingSupported,
    bool backendRequestIsCpu,
    bool requestedPhase3ModeIsDecodeReconProcess,
    bool cachingChecked)
{
    if (!libraryReady) return false;
    if (!gpuPreviewSurfaceActive) return false;
    if (hasScopeVisualization) return false;
    if (!reconRequestedByEnvironment) return false;
    if (!texturePresentRequestedByEnvironment) return false;
    if (!gpuPreviewProcessingSupported) return false;
    if (backendRequestIsCpu) return false;
    if (!requestedPhase3ModeIsDecodeReconProcess) return false;
    if (cachingChecked) return false;
    return true;
}

inline bool mainWindowUsesGpuImagePresentation(
    const MainWindowGpuPreviewPolicyState &state)
{
    return state.gpuViewportInstalled
        && !mainWindowUsesGpu16PreviewPresentation(state);
}

inline GpuPlaybackPipelineStatus mainWindowGpuPlaybackPipelineStatus(
    const MainWindowGpuPreviewPolicyState &state,
    bool gpuPlaybackReconUsed,
    bool gpuPlaybackReconTexturePresentActive,
    bool gpuPlaybackReconTexturePresentNoReadbackActive)
{
    if (gpuPlaybackReconTexturePresentActive)
    {
        return gpuPlaybackReconTexturePresentNoReadbackActive
            ? GpuPlaybackPipelineStatus::GpuTextureNoReadback
            : GpuPlaybackPipelineStatus::GpuTextureReadback;
    }
    if (gpuPlaybackReconUsed)
    {
        return GpuPlaybackPipelineStatus::GpuReconReadback;
    }
    if (mainWindowUsesGpuPreviewProcessing(state)
     || mainWindowUsesGpuBilinearDebayer(state)
     || mainWindowUsesGpuAmazeDebayer(state))
    {
        return GpuPlaybackPipelineStatus::GpuPreview;
    }
    return GpuPlaybackPipelineStatus::Cpu;
}

inline const char *mainWindowGpuPlaybackPipelineStatusToken(
    GpuPlaybackPipelineStatus status)
{
    switch (status)
    {
    case GpuPlaybackPipelineStatus::Cpu:
        return "cpu";
    case GpuPlaybackPipelineStatus::GpuPreview:
        return "gpu_preview";
    case GpuPlaybackPipelineStatus::GpuReconReadback:
        return "gpu_recon_readback";
    case GpuPlaybackPipelineStatus::GpuTextureReadback:
        return "gpu_texture_readback";
    case GpuPlaybackPipelineStatus::GpuTextureNoReadback:
        return "gpu_texture_no_readback";
    }
    return "cpu";
}

inline const char *mainWindowGpuPlaybackPipelineStatusLabel(
    GpuPlaybackPipelineStatus status)
{
    switch (status)
    {
    case GpuPlaybackPipelineStatus::Cpu:
        return "CPU";
    case GpuPlaybackPipelineStatus::GpuPreview:
        return "GPU Preview";
    case GpuPlaybackPipelineStatus::GpuReconReadback:
        return "GPU RB";
    case GpuPlaybackPipelineStatus::GpuTextureReadback:
        return "GPU Tex RB";
    case GpuPlaybackPipelineStatus::GpuTextureNoReadback:
        return "GPU Tex NR";
    }
    return "CPU";
}

inline const char *mainWindowGpuPlaybackPipelineStatusBadgeLabel(
    GpuPlaybackPipelineStatus status,
    const MainWindowGpuPreviewPolicyState &state,
    bool limitedByVisibleScopes)
{
    if (status == GpuPlaybackPipelineStatus::Cpu
     && limitedByVisibleScopes
     && mainWindowHasScopeVisualization(state))
    {
        return "CPU - scopes active";
    }
    return mainWindowGpuPlaybackPipelineStatusLabel(status);
}

inline const char *mainWindowScopePerformanceHintText()
{
    return "Visible scopes can disable the fastest GPU texture playback path. "
           "Hide the edit area with E when you need faster playback.";
}

inline const char *mainWindowGpuPlaybackPipelineStatusDescription(
    GpuPlaybackPipelineStatus status)
{
    switch (status)
    {
    case GpuPlaybackPipelineStatus::Cpu:
        return "CPU presentation";
    case GpuPlaybackPipelineStatus::GpuPreview:
        return "GPU preview processing or debayer";
    case GpuPlaybackPipelineStatus::GpuReconReadback:
        return "CUDA reconstruction with CPU readback";
    case GpuPlaybackPipelineStatus::GpuTextureReadback:
        return "GL texture presentation from a CPU-readback Bayer frame";
    case GpuPlaybackPipelineStatus::GpuTextureNoReadback:
        return "CUDA-to-GL texture presentation without per-frame CPU readback";
    }
    return "CPU presentation";
}

inline bool mainWindowUsesGpuShaderZebraProcessing(
    const MainWindowGpuPreviewPolicyState &state)
{
    return state.gpuViewportInstalled && state.zebrasEnabled;
}

inline GpuDisplayViewport::PresentationOptions mainWindowBuildGpuPresentationOptions(
    const MainWindowGpuPreviewPolicyState &state)
{
    GpuDisplayViewport::PresentationOptions options;
    options.showZebras = state.zebrasEnabled;

    if (!state.gpuViewportInstalled)
    {
        return options;
    }

    if (state.transformationMode == Qt::FastTransformation)
    {
        options.samplingMode =
            (state.playbackScaleFactorActive >= 8)
                ? GpuDisplayViewport::SamplingBicubic
                : GpuDisplayViewport::SamplingNearest;
    }
    else if (state.betterResizerEnabled)
    {
        options.samplingMode = GpuDisplayViewport::SamplingBicubic;
    }
    else
    {
        options.samplingMode = GpuDisplayViewport::SamplingLinear;
    }

    return options;
}

inline int mainWindowGpuTexturePresentScaledDimension(
    int textureDimension,
    double stretch)
{
    if (textureDimension <= 0) return 0;

    const double safeStretch =
        (std::isfinite(stretch) && stretch > 0.0) ? stretch : 1.0;
    const double scaled = static_cast<double>(textureDimension) * safeStretch;
    if (!std::isfinite(scaled)) return textureDimension;
    if (scaled >= static_cast<double>(std::numeric_limits<int>::max()))
    {
        return std::numeric_limits<int>::max();
    }

    const long rounded = std::lround(scaled);
    return rounded < 1L ? 1 : static_cast<int>(rounded);
}

inline QSize mainWindowGpuTexturePresentDisplaySize(
    int textureWidth,
    int textureHeight,
    double stretchX,
    double stretchY)
{
    return QSize(
        mainWindowGpuTexturePresentScaledDimension(textureWidth, stretchX),
        mainWindowGpuTexturePresentScaledDimension(textureHeight, stretchY));
}

inline int mainWindowVerticalStretchIndexForMlvAspectRatio(double aspectRatio)
{
    if (!std::isfinite(aspectRatio) || aspectRatio <= 0.0) return 0;
    const RawAspectStretchSelection selection =
        rawAspectStretchSelectionForRatio(aspectRatio);
    return selection.valid ? selection.verticalIndex : 3;
}

inline bool mainWindowShouldApplyMlvAspectForNeutralReceiptStretch(
    double receiptStretchX,
    double receiptStretchY,
    double aspectRatio)
{
    const RawAspectStretchSelection selection =
        rawAspectStretchSelectionForRatio(aspectRatio);
    if (selection.valid
        && selection.horizontalIndex == 0 && selection.verticalIndex == 0)
        return false;
    if (!selection.valid
        && (!std::isfinite(aspectRatio) || aspectRatio <= 0.0)) return false;

    constexpr double kNeutralTolerance = 0.0001;
    return std::isfinite(receiptStretchX)
        && std::isfinite(receiptStretchY)
        && std::fabs(receiptStretchX - 1.0) <= kNeutralTolerance
        && std::fabs(receiptStretchY - 1.0) <= kNeutralTolerance;
}

#endif // MAINWINDOWGPUPREVIEWPOLICY_H
