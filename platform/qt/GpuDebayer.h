#ifndef GPUDEBAYER_H
#define GPUDEBAYER_H

#include <QString>

#include <cstdint>

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
bool gpuAmazeDebayerApplyGpuOffscreenPostWb(const float * inputRawFrame,
                                           uint16_t * outputRgb16,
                                           int width,
                                           int height,
                                           int blackLevel,
                                           const double wbMultipliers[3],
                                           QString * reason = nullptr,
                                           QString * rendererDescription = nullptr,
                                           GpuAmazeDebayerBackendTiming * timing = nullptr);

#endif // GPUDEBAYER_H
