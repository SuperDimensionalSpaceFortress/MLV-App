/*!
 * \file GpuFieldTelemetry.h
 * \brief Local-first GPU field telemetry (GPU-FIELD-TELEMETRY-1, owner spec r2
 *        Part 3): per-stage timings, every non-success CUDA return code with
 *        its call site, the GL identity and robustness reset status, and
 *        whether the System log holds nvlddmkm 153 / Display 4101 since app
 *        start. Written to <logs>/gpu-field-telemetry.jsonl (rotated at 1 MiB,
 *        3 kept) in CrashForensics::logsDirectoryPath().
 *
 * ON by default; MLVAPP_GPU_TELEMETRY=0|off|false|no removes all telemetry
 * work: install() installs no sink, no file is written, no thread starts.
 * Upload is not implemented ("upload":"none").
 *
 * Cost: no new CUDA call and no synchronisation anywhere. Stage times come from
 * the per-frame timings the backends already compute. One mutex plus a stats add
 * per backend call. Writes are never per frame: flush() runs at session end
 * (GUI thread, bounded), once after a fault (worker thread: waits 5 s, reads the
 * System log, writes; debounced to one per 10 s) and on noteClip (GUI, cold).
 * Never the NVIDIA management library or its CLI (either wakes a sleeping dGPU;
 * a console text census pins it): the GPU model is the CUDA describe string or
 * GL_RENDERER, the driver is parsed from GL_VERSION.
 *
 * GPU-DEVICE-LOSS-SELF-HEAL-1 consumes this seam: setFaultListener(),
 * isDeviceLossCode() and noteRecovery().
 */
#ifndef GPU_FIELD_TELEMETRY_H
#define GPU_FIELD_TELEMETRY_H

#include <QString>

#include <functional>

namespace GpuFieldTelemetry {

/*! MLVAPP_GPU_TELEMETRY read once (a function-local static); unset = ON. */
bool enabled();

/*! Installs the llrawproc recon stage sink and the session-end flush. Call once
 *  at startup, not in batch mode. No-op when disabled or when the logs
 *  directory is unknown. */
void install();

/*! A successful backend stage call and its time in ms. */
void noteStage(const char * dll, const char * stage, double ms);

struct CudaFault
{
    const char * dll = nullptr;      /* "recon" | "amaze" */
    const char * stage = nullptr;    /* e.g. "recon:run", "amaze:r16_gl" */
    int rc = 0;                      /* backend return code */
    int cudaCode = 0;                /* 0 = none noted (validation failure) */
    const char * cudaName = nullptr; /* cudaGetErrorName, or NULL */
    const char * site = nullptr;     /* failing call site text, or NULL */
    double utcMs = 0.0;              /* when observed; 0 = now */
};

/*! A failed backend stage call (rc != 0). Recorded at once, written by the
 *  after-fault flush; the fault listener (if any) is called on the caller's
 *  thread. */
void noteFault(const CudaFault & fault);

/*! 700, 702, 719 and the "unknown" rule (999). */
bool isDeviceLossCode(int cudaCode);

/*! GPU-DEVICE-LOSS-SELF-HEAL-1 subscribes here. Pass an empty function to
 *  unsubscribe. */
void setFaultListener(std::function<void(const CudaFault &)> listener);

/*! Reserved for GPU-DEVICE-LOSS-SELF-HEAL-1: writes one recovery line. */
void noteRecovery(const char * what, const char * outcome);

/*! The clip just opened. Only the basename's hash is kept (clip_hash). */
void noteClip(int width, int height, int bitsPerPixel, bool compressed,
              bool dualIsoGpu, const QString & basename);

/*! The GL identity, recorded once (the first call wins). */
void noteGlIdentity(const QString & renderer, const QString & vendor,
                    const QString & version, const QString & resetStrategy,
                    const QString & resetStatus);

/*! Writes the session snapshot (stage lines, unwritten faults, ...). */
void flush(const char * reason);

/*! clip_hash: first 12 hex of SHA-256 of the file name alone (any directory
 *  part is dropped first). */
QString clipHash(const QString & basenameOrPath);

} // namespace GpuFieldTelemetry

#endif // GPU_FIELD_TELEMETRY_H
