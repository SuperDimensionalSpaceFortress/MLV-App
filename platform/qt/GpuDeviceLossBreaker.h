/*!
 * \file GpuDeviceLossBreaker.h
 * \brief GPU device-loss self-healing (GPU-DEVICE-LOSS-SELF-HEAL-1, owner spec
 *        r2 Part 4): detect, suppress CUDA, move playback to the CPU route,
 *        tell the user without interrupting, and persist a per-machine
 *        safe-start flag after repeated losses. The pure rules live in
 *        GpuDeviceLossBreakerCore.h.
 *
 * Detection: faults reach the breaker ONLY through GpuFieldTelemetry's fault
 * listener (noteFault). A device-loss code (700, 702, 719, 999) sets
 * llrpSetGpuCudaSuppressed(1) at once, on the faulting thread, so no CUDA call
 * is made on any thread after it; the rest (settings, notice, refresh) runs on
 * the GUI thread through one queued invoke. No CUDA reset, sync or new CUDA
 * API is ever called here: in-process recreate is GPU-DEVICE-LOSS-INPROCESS-
 * RECREATE-1, the GL context reset GPU-GL-ROBUST-CONTEXT-1.
 *
 * Persisted (automation_settings::openAppSettings): GpuDeviceLoss/RecentLosses
 * and, once two losses hit the same profile within 10 minutes,
 * GpuDeviceLoss/SafeStartEnabled plus the GL renderer, driver, clip profile and
 * UTC it was set on. A flagged launch starts with CUDA suppressed. The flag
 * clears on a GL renderer or driver change (seen at clip open), when the user
 * turns the CUDA playback preset on, or with MLVAPP_GPU_SAFE_START=clear.
 *
 * MLVAPP_GPU_FAULT_INJECT=<code>@<n> (700|702|719|999) notes one injected
 * fault after the n-th successful "recon:run" / "amaze:*" stage, once per
 * process, through GpuFieldTelemetry::noteFault (venue proof only).
 *
 * With MLVAPP_GPU_TELEMETRY=0 the breaker is inert: install() does nothing, no
 * fault reaches it and a persisted flag is neither read nor cleared
 * (follow-up GPU-DEVICE-LOSS-TELEMETRY-OFF-1).
 */
#ifndef GPU_DEVICE_LOSS_BREAKER_H
#define GPU_DEVICE_LOSS_BREAKER_H

#include "GpuFieldTelemetry.h"

#include <QString>

#include <functional>

namespace GpuDeviceLossBreaker {

/*! GUI launches only (never batch or trim). Call right after
 *  GpuFieldTelemetry::install() and before the CUDA preset is applied. */
void install();

/*! The fault listener. Any thread, possibly under a backend lock: atomics and
 *  one queued invoke only. */
void onFault(const GpuFieldTelemetry::CudaFault & fault);

/*! GUI thread: the clip just opened (its profile is the clip half of the key). */
void noteClipOpened(int width, int height, int bitsPerPixel, bool compressed);

/*! GUI thread: the user turned the CUDA playback preset on. */
void userReenableGpu();

/*! GUI thread. Registering a sink shows a queued startup notice at once. An
 *  empty text means the notice was withdrawn. */
void setNoticeSink(std::function<void(const QString &)> sink);
void setRefreshCallback(std::function<void()> callback);

/*! The notice on display ("" when none). */
QString currentNotice();

/*! This launch started on the CPU route because of the persisted flag. */
bool startedSafe();

/*! MLVAPP_GPU_FAULT_INJECT, read once (function-local static). */
bool faultInjectArmed();
void noteRunStageForInjection(const char * stage);

/*! Test-only: forget the session loss, lift suppression, drop the sinks and
 *  the clip profile. */
void resetForTesting();

} // namespace GpuDeviceLossBreaker

#endif // GPU_DEVICE_LOSS_BREAKER_H
