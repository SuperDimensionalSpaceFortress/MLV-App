#ifndef GPUDEBAYER_H
#define GPUDEBAYER_H

#include <QString>

#include <cstdint>
#include <vector>

#include "../../tools/gpu/igpu_cuda_error.h"

struct GpuBilinearDebayerBackendAvailability
{
    bool available = false;
    QString reason;
    QString rendererDescription;
};

struct GpuAmazeDebayerBackendAvailability
{
    bool available = false;
    QString reason;
    QString rendererDescription;
};

struct GpuAmazeDebayerBackendTiming
{
    bool available = false;
    double uploadMs = 0.0;
    double kernelMs = 0.0;
    double downloadMs = 0.0;
    double totalMs = 0.0;
    /* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0: this run's in-process probe (armed runs only).
     * probeAvailable: the DLL exports igpu_amaze_debayer_last_probe; probeValid: this run was armed and every
     * probe step worked. */
    bool probeAvailable = false;
    bool probeValid = false;
    double smMhzBefore = 0.0;
    double smMhzAfter = 0.0;
    double deviceMs = 0.0;
};

/* Row 0: the first armed live runs since the last gpuAmazeDebayerBeginProbeSession() (Play start), in call order. */
struct GpuAmazeProbeSessionRecord
{
    static constexpr int kMaxCalls = 4;
    int calls = 0;
    bool probeAvailable = false;
    double kernelMs[kMaxCalls] = {};
    double deviceMs[kMaxCalls] = {};
    double smMhzBefore[kMaxCalls] = {};
    double smMhzAfter[kMaxCalls] = {};
    bool valid[kMaxCalls] = {};
    double startSteadyMs[kMaxCalls] = {};   // gpuAmazeDebayerSteadyNowMs() as the run's backend call began
};

/* Row K: one collected clock kick (igpu_amaze_debayer_clock_kick_end). */
struct GpuAmazeClockKickReport
{
    int state = 2;            // 0 collected, 1 pending, 2 none / unavailable
    int stoppedBy = 0;        // 0 deadline, 1 end, 2 a live run's drain
    double ranMs = 0.0;
    std::vector<double> sampleMs;    // since the kick's first sample
    std::vector<double> intervalMhz; // SM MHz over each sample interval (sampleMs[i] -> sampleMs[i+1]); -1 across SMs
};

const char * gpuBilinearDebayerEnvironmentVariableName(void);
bool gpuBilinearDebayerRequestedByEnvironment(void);
const char * gpuAmazeDebayerEnvironmentVariableName(void);
bool gpuAmazeDebayerRequestedByEnvironment(void);
const char * gpuAmazeTexturePresentEnvironmentVariableName(void);
bool gpuAmazeTexturePresentRequestedByEnvironment(void);
GpuBilinearDebayerBackendAvailability gpuBilinearDebayerProbeBackend(void);
bool gpuBilinearDebayerApplyGpuOffscreen(const float * inputRawFrame,
                                         uint16_t * outputRgb16,
                                         int width,
                                         int height,
                                         QString * reason = nullptr,
                                         QString * rendererDescription = nullptr);
GpuAmazeDebayerBackendAvailability gpuAmazeDebayerProbeBackend(void);
bool gpuAmazeDebayerApplyGpuOffscreen(const float * inputRawFrame,
                                      uint16_t * outputRgb16,
                                      int width,
                                      int height,
                                      QString * reason = nullptr,
                                      QString * rendererDescription = nullptr,
                                      GpuAmazeDebayerBackendTiming * timing = nullptr);
bool gpuAmazeDebayerRenderPostWbGlTexture(const float * inputRawFrame,
                                          unsigned int glTexture,
                                          int width,
                                          int height,
                                          int blackLevel,
                                          const double wbMultipliers[3],
                                          QString * reason = nullptr,
                                          QString * rendererDescription = nullptr,
                                          GpuAmazeDebayerBackendTiming * timing = nullptr);
GpuAmazeDebayerBackendAvailability gpuAmazeDebayerProbeR16TextureBackend(void);
void gpuAmazeDebayerResetR16TextureBackendResources(void);
/* The two live runs below take reducedHnyquist: true for a reduced (x2/x4) recon
 * texture (playbackReconTextureIsReduced, PlaybackScaling.h), false otherwise. The
 * sticky AMaZE flag is set to it immediately before each run; true on a DLL without
 * igpu_amaze_debayer_set_reduced_hnyquist fails with
 * gpuAmazeDebayerReducedHnyquistMissingReason(). */
bool gpuAmazeDebayerRenderPostWbGlTextureFromR16GlTexture(unsigned int inputR16GlTexture,
                                                          unsigned int outputRgba16GlTexture,
                                                          int width,
                                                          int height,
                                                          int blackLevel,
                                                          const double wbMultipliers[3],
                                                          QString * reason = nullptr,
                                                          QString * rendererDescription = nullptr,
                                                          GpuAmazeDebayerBackendTiming * timing = nullptr,
                                                          bool reducedHnyquist = false);
bool gpuAmazeDebayerRenderPostWbGlTextureFromDeviceBayer16(const uint16_t *deviceBayer16,
                                                           unsigned int outputRgba16GlTexture,
                                                           int width,
                                                           int height,
                                                           int blackLevel,
                                                           const double wbMultipliers[3],
                                                           QString * reason = nullptr,
                                                           QString * rendererDescription = nullptr,
                                                           GpuAmazeDebayerBackendTiming * timing = nullptr,
                                                           bool reducedHnyquist = false);
/* PLAYBACK-CUDA-HONOUR-SCALE-1 r3: the live AMaZE DLL exports the optional
 * reduced H-Nyquist symbol. Presenters refuse a reduced texture present when it does
 * not, with gpuAmazeDebayerReducedHnyquistMissingReason(). Process-wide counters for
 * the play-stop smoke summary. */
QString gpuAmazeDebayerReducedHnyquistMissingReason(void);
bool gpuAmazeDebayerReducedHnyquistAvailable(void);
void gpuAmazeDebayerNoteReducedHnyquistFrame(void);
void gpuAmazeDebayerNoteReducedHnyquistRefusal(void);
quint64 gpuAmazeDebayerReducedHnyquistFrames(void);
quint64 gpuAmazeDebayerReducedHnyquistRefusals(void);
/* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0: arm the next `calls` live AMaZE runs (the two
 * *FromR16GlTexture / *FromDeviceBayer16 wrappers above) for the in-process SM-clock + device-time probe. Never loads
 * the DLL: the count is kept here and handed to the backend under the live lock right before each run. An armed run
 * reports into its GpuAmazeDebayerBackendTiming and into the session record. Zero cost for unarmed runs. */
void gpuAmazeDebayerArmProbe(int calls);
/* Row 0: clears the session record and arms GpuAmazeProbeSessionRecord::kMaxCalls runs (MainWindow, at Play). */
void gpuAmazeDebayerBeginProbeSession(void);
GpuAmazeProbeSessionRecord gpuAmazeDebayerProbeSessionRecord(void);
/* The steady clock the session record and the kick are stamped with (ms). */
double gpuAmazeDebayerSteadyNowMs(void);
/* Row K: the AMaZE live runtime is already loaded and created (never loads it). */
bool gpuAmazeDebayerLiveBackendLoaded(void);
/* Row K: begin the Play-start clock kick on the loaded backend. Returns false with *why (backend_not_loaded,
 * symbol_missing, cuda_suppressed, in_flight, begin_failed) when nothing was launched. Re-checks the device-loss flag
 * under the live lock. *beginSteadyMs = gpuAmazeDebayerSteadyNowMs() at the launch. */
bool gpuAmazeDebayerClockKickBegin(int maxMs, QString * why, double * beginSteadyMs);
/* Row K: end (raise the stop flag) and collect; never loads the DLL, never makes a CUDA call. */
GpuAmazeClockKickReport gpuAmazeDebayerClockKickEnd(void);
/* PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-WARM-OUTPUT-REUSE-1 R1 (PlaybackWarmOutputReusePolicy.h). Capture copies one
 * AMaZE input (device Bayer16) into the loaded backend and returns its capture id; false with *why (cuda_suppressed,
 * backend_not_loaded, symbol_missing, failed) and *captureId 0 otherwise. MatchesCapture compares a device Bayer16
 * against that capture exactly: true with *equal on a completed compare, false with *why (cuda_suppressed,
 * capture_missing, symbol_missing, compare_failed). Both re-check the device-loss flag under the live lock and never
 * load the DLL. Release clears the capture id; never gated, never a CUDA call. */
bool gpuAmazeDebayerCaptureInputBayer16(const uint16_t * deviceBayer16, int width, int height,
                                        quint64 * captureId, QString * why);
bool gpuAmazeDebayerInputBayer16MatchesCapture(const uint16_t * deviceBayer16, int width, int height,
                                               quint64 captureId, bool * equal, QString * why);
void gpuAmazeDebayerReleaseInputCapture(void);
bool gpuAmazeDebayerApplyGpuOffscreenPostWb(const float * inputRawFrame,
                                           uint16_t * outputRgb16,
                                           int width,
                                           int height,
                                           int blackLevel,
                                           const double wbMultipliers[3],
                                           QString * reason = nullptr,
                                           QString * rendererDescription = nullptr,
                                           GpuAmazeDebayerBackendTiming * timing = nullptr);

/* GPU-FIELD-TELEMETRY-1 r6: the AMaZE stage sink, the same shape as recon's
 * llrpGpuStageTelemetrySink_t (llrawproc.h): a success is (stage, 0, ms, 0,
 * NULL, NULL, NULL, 0); a failed call is one record per CUDA code it noted,
 * "primary" then "secondary" (none noted: one "primary" with code 0); a code
 * left pending by a call nothing captures (create, destroy, the GL reset) is
 * stage "amaze:uncaptured", rc 0, role "orphan", with its own site. dropped is
 * the DLL queue's drops since the previous drain, on that drain's first record.
 * GpuFieldTelemetry::install() installs it; NULL uninstalls (nothing is drained
 * then, so a code waits for an installed sink). */
typedef void (*GpuAmazeStageTelemetrySink)(const char * stage, int rc, double ms,
                                           int cudaCode, const char * cudaName,
                                           const char * site, const char * role,
                                           unsigned long long dropped);
void gpuAmazeDebayerSetStageTelemetrySink(GpuAmazeStageTelemetrySink sink);

/* r6: the AMaZE DLL's optional CUDA-error exports (igpu_cuda_error.h). drain
 * and pending come from an r6 DLL; an older DLL has only last, and a failure
 * then reports the last code noted since the previous read (the r4 rule). */
struct GpuAmazeCudaErrorSource
{
    int (*drain)(int, igpu_cuda_error_t *, int, unsigned long long *) = nullptr;
    const volatile long * pending[2] = {nullptr, nullptr};
    int (*last)(igpu_cuda_error_t *) = nullptr;
};

/* r6: emits every code pending on the first `channels` channels (1: render,
 * where every AMaZE site notes; 2: both) as an "amaze:uncaptured" orphan and
 * empties them. A channel whose pending word reads 0 (one relaxed load) is not
 * drained. Returns the records emitted (0 with no sink or no drain export). */
int gpuAmazeCudaErrorsEmitOrphans(const GpuAmazeCudaErrorSource & source, int channels,
                                  GpuAmazeStageTelemetrySink sink);
/* r6: a failed call (rc != 0): every code the call noted, oldest first, the
 * first "primary" and the rest "secondary", each with its own site; the render
 * channel is empty afterwards. Returns the records emitted. */
int gpuAmazeCudaErrorsEmitFailure(const GpuAmazeCudaErrorSource & source, const char * stage, int rc,
                                  GpuAmazeStageTelemetrySink sink);
/* r6 terminal drain (session end): every code still pending in the loaded AMaZE
 * DLL, as orphans through the installed sink. Waits up to waitMs for a live run
 * in flight to finish, so its own codes stay its own. Never loads the DLL. */
int gpuAmazeDebayerDrainCudaErrorsAtTeardown(int waitMs);
/* Test-only: the source the GL reset and the terminal drain read (NULL restores
 * the DLL's). */
void gpuAmazeDebayerSetCudaErrorSourceForTesting(const GpuAmazeCudaErrorSource * source);
/* GPU-DEVICE-LOSS-SELF-HEAL-1 r6, test-only: the step every AMaZE run takes
 * right before its backend call (telemetry entry on, its orphans read from
 * `source`, then the suppression flag read again). true = the run may submit. */
bool gpuAmazeDebayerEnterRunForTesting(const GpuAmazeCudaErrorSource & source);

#endif // GPUDEBAYER_H
