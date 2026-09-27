/*!
 * \file GpuDisplayWindow.h
 * \author Claude
 * \copyright 2026
 * \brief QOpenGLWindow-based GPU preview for NVIDIA Optimus hybrid laptops.
 *
 * A QOpenGLWidget set as a QGraphicsView viewport (GpuDisplayViewport) renders
 * correctly into its offscreen FBO but is NEVER composited to the panel on an NVIDIA
 * Optimus hybrid -> solid black (proven on the Dell RTX 3060 across drivers and even
 * software llvmpipe; it is the QOpenGLWidget-into-backing-store composition that fails,
 * not the GPU). A QOpenGLWindow is a real native window with its OWN swapchain that DWM
 * presents directly, so it DOES display on the hybrid (proven on the Dell: a
 * QOpenGLWindow cycles colors on the panel, both top-level and createWindowContainer-
 * embedded, rendering on the discrete 3060). This is QTBUG-68329's accepted workaround.
 *
 * This hosts the preview frame in a QOpenGLWindow embedded via
 * QWidget::createWindowContainer in place of the QGraphicsView, gated by
 * MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT. Milestone 1: the CPU presentImage path
 * (passthrough RGBA). Later milestones add the GPU no-readback texture paths and a
 * shared renderer with GpuDisplayViewport.
 */
#ifndef GPUDISPLAYWINDOW_H
#define GPUDISPLAYWINDOW_H

#include <QImage>
#include <QByteArray>
#include <QSize>
#include <QString>
#include <QOpenGLFunctions>
#include <QOpenGLShaderProgram>
#include <QOpenGLTexture>
#include <QOpenGLWindow>
#include "GpuDisplayViewport.h"
#include "GpuPreviewProcessing.h"
#include "GpuWindowSwapTelemetry.h"
#include "../../src/mlv/llrawproc/llrawproc.h"
#include <array>
#include <cstddef>
#include <cstdint>

class QGraphicsView;

class GpuDisplayWindow : public QOpenGLWindow, protected QOpenGLFunctions
{
    Q_OBJECT
public:
    static const char *environmentVariableName(void);
    static bool isRequestedByEnvironment(void);
    static bool isActive(void);
    /* The active window's own QWindow, or nullptr when none is active. Used by
     * --gui-smoke-playback's foreground-forcing (CUDA-PERF-PLAYBACK-FOREGROUND-1) to
     * activate the GPU display window alongside the main window -- it is embedded via
     * createWindowContainer, so it has no OS-level foreground state of its own (that
     * belongs to the top-level MainWindow), but still needs QWindow::requestActivate()
     * so Qt does not consider it backgrounded. */
    static QWindow *activeWindow(void);

    /* Replace the QGraphicsView with a QOpenGLWindow container in the same layout
     * slot and register it as the active preview window. Returns true on success. */
    static bool installInPreview(QGraphicsView *view);

    /* Route a CPU RGBA frame (or a clear) to the active window. Return true when the
     * window handled it (so the QOpenGLWidget/pixmap path can be skipped). Safe to call
     * from any thread; work is marshaled to the window's GUI thread. */
    static QSize effectiveDisplaySizeForImage(const QSize &imageSize,
                                              const QSize &requestedDisplaySize);
    static bool presentImageIfActive(const QImage &image,
                                     const QSize &displaySize = QSize(),
                                     quint64 presentationSerial = 0);
    static bool clearIfActive(void);
    static QString rendererDescription(void);
    static bool presentGpuPlaybackReconAmazePostWbTextureIfActive(
        const uint16_t *rawInputBayer14,
        size_t rawInputBayer14Words,
        const llrpGpuPlaybackReconState_t *state,
        int blackLevel,
        const double wbMultipliers[3],
        const GpuDisplayViewport::PresentationOptions &options,
        QString *reason = nullptr,
        llrpGpuPlaybackReconTiming_t *timing = nullptr,
        QString *handoffMode = nullptr,
        bool validationProbeTexture = false,
        const uint16_t *retainedDeviceBayer16 = nullptr,
        int retainedDeviceWidth = 0,
        int retainedDeviceHeight = 0,
        int displayWidth = 0,
        int displayHeight = 0,
        quint64 presentationSerial = 0);
    static bool readGpuReconSourceBayer16TextureIfActive(QByteArray *textureBytes,
                                                        int *width,
                                                        int *height,
                                                        QString *reason = nullptr);
    /* Read back the frame this window's OWN swapchain is about to present: makes the
     * context current, calls this window's real paintGL() SYNCHRONOUSLY on the GUI
     * thread (the same function Qt's own paint-event cycle calls), reads the resulting
     * default framebuffer with glReadPixels from inside that same paintGL() call --
     * before any further swap can leave it stale -- and only then swaps. Because
     * paintGL() runs normally (it is not suppressed or isolated in any way), it promotes
     * whatever frame is currently pending exactly as a real paint would, for BOTH the
     * QImage and the GPU-recon texture route; the pixels read back are therefore always
     * exactly what THIS call just drew and swapped. Owner-visible effect: a capture
     * presents a pending frame one paint early, rather than waiting for Qt's own next
     * paint event -- there is no "stale-but-valid" gap, because capture and paint are the
     * same synchronous operation and never race. Never a screen-region capture: pixels
     * come only from this window's own default framebuffer. GUI-thread only; call after a
     * completed present. Image size is the window's real device-pixel framebuffer size.
     * presentedSerial, when non-null, receives the presentationSerial (see
     * presentImageIfActive / presentGpuPlaybackReconAmazePostWbTextureIfActive) of the
     * frame this call actually presented, and presentedSerialValid reports whether that
     * route supplied a real serial (false before any frame has ever been presented, after
     * clearIfActive(), or when a route did not supply a presentationSerial). */
    static bool grabPresentedFramebufferIfActive(QImage *outImage,
                                                 QString *reason = nullptr,
                                                 quint64 *presentedSerial = nullptr,
                                                 bool *presentedSerialValid = nullptr);
    /* Logical size of the active preview window (empty if none). Used by the display
     * scene-geometry calc so the playback preview resolution tracks the QOpenGLWindow
     * surface, not the hidden QGraphicsView. GUI-thread only. */
    static QSize displaySize(void);

    /* Opt-in (MLVAPP_PLAYBACK_SMOKE_TELEMETRY, the same env var that gates MainWindow's
     * playback smoke frame telemetry) swap-cadence telemetry: records every REAL buffer
     * swap this window performs -- Qt's own automatic swap after paintGL() and the
     * explicit swapBuffers() in grabPresentedFramebufferIfActive -- so displayed cadence
     * can be joined against PresentMon and against frames_presented (which counts
     * submissions, not swaps). Call resetSwapTelemetry() at playback-smoke-session start
     * and swapTelemetrySnapshot() at session end; the latter also CLOSES the session (see
     * below), so it must be called exactly once per session, at the end. GUI-thread only,
     * like the swaps themselves.
     *
     * Zero cost and zero output when the env var is unset: the constructor never connects
     * frameSwapped() to the recorder in that case (the env var is cached and cannot change
     * mid-run, so this is decided once, not re-checked per swap), and swapTelemetrySnapshot()
     * never emits a summary.
     *
     * The session id and its active/closed state live at file scope in GpuDisplayWindow.cpp,
     * not on this instance: they must survive the window being inactive when a session
     * begins, or being destroyed and recreated mid-session, so the summary swapTelemetrySnapshot()
     * returns never reports a different session than the playback_smoke.gate line it is
     * paired with. Once a session is closed, noteRealSwap() is a no-op for any further swap
     * (queued or screenshot-capture swaps included) until the next resetSwapTelemetry() --
     * so the summary and every already-emitted per-swap gpu_window.swap line always agree. */
    static void resetSwapTelemetry(quint64 sessionId);
    static GpuWindowSwapTelemetrySnapshot swapTelemetrySnapshot(void);

    explicit GpuDisplayWindow(QWindow *parent = nullptr);
    ~GpuDisplayWindow() override;

    void setPresentedImage(const QImage &image,
                           const QSize &displaySize = QSize(),
                           quint64 presentationSerial = 0);
    void clearPresented(void);
    bool setPresentedGpuPlaybackReconAmazePostWbTexture(
        const uint16_t *rawInputBayer14,
        size_t rawInputBayer14Words,
        const llrpGpuPlaybackReconState_t *state,
        int blackLevel,
        const double wbMultipliers[3],
        const GpuDisplayViewport::PresentationOptions &options,
        QString *reason,
        llrpGpuPlaybackReconTiming_t *timing,
        QString *handoffMode,
        bool validationProbeTexture,
        const uint16_t *retainedDeviceBayer16,
        int retainedDeviceWidth,
        int retainedDeviceHeight,
        int displayWidth,
        int displayHeight,
        quint64 presentationSerial = 0);
    bool readGpuReconSourceBayer16Texture(QByteArray *textureBytes,
                                          int *width,
                                          int *height,
                                          QString *reason);

protected:
    void initializeGL() override;
    void paintGL() override;

private:
    friend class GuiSmokeTest; // Exercise real context-loss teardown/rebuild without requiring the OS to recreate the platform window.

    /* One recorded real swap: monotonic swap serial (1-based, distinct from the
     * presentationSerial identifying WHICH submitted frame it displayed), the QPC-ms
     * timestamp of the swap (the UTC-ISO timestamp is formatted only for the per-swap log
     * line, not retained here), and that frame's presentationSerial when known. */
    struct SwapTelemetryRecord
    {
        quint64 swapSerial = 0;
        double qpcMs = 0.0;
        quint64 presentedSerial = 0;
        bool presentedSerialValid = false;
    };
    // Bounded, preallocated once at construction -- never grows, never reallocates, so a
    // long playback session costs no more memory than a short one. Oldest entries are
    // overwritten (ring index = swapSerial % capacity); the running aggregates below (not
    // a ring scan) answer the session-summary questions (count/fps/max gap).
    static const int kSwapTelemetryRingCapacity = 2048;
    void noteRealSwap(void);

    void ensureProgram(void);
    void ensurePreviewProcessingProgram(void);
    void updateTextureIfNeeded(void);
    void destroyTexture(void);
    void applySamplingMode(GpuDisplayViewport::SamplingMode samplingMode);
    /* Releases every context-bound processing resource (passthrough program, shared
     * preview-processing program, LUT texture set) so the next present rebuilds them
     * from scratch, mirroring GpuDisplayViewport::cleanupGLResources. Called from the
     * destructor and from QOpenGLContext::aboutToBeDestroyed so a context recreation
     * (not just window teardown) cannot leave stale/non-null GL wrappers behind that
     * would otherwise short-circuit ensureProgram()/ensurePreviewProcessingProgram()'s
     * "already built" check and get bound as if still valid (GPU-TEXNR-S1-DARK-GREEN-1
     * round 2). */
    void cleanupGLResources(void);

    QOpenGLShaderProgram *m_program;
    // Shared with GpuDisplayViewport (GpuPreviewProcessing.h): the display shader +
    // LUT set used to render recon/preview textures (post-WB-undo linear camera RGB)
    // through the SAME processing path the viewport uses, instead of passthrough.
    QOpenGLShaderProgram *m_previewProcessingProgram;
    GpuPreviewProcessingLutTextureSet m_lutSet;
    // Presentation options captured at the most recent recon-texture submit; consumed
    // by paintGL() when it draws that texture (paintGL runs later / can run twice --
    // see grabPresentedFramebufferIfActive -- so the options must outlive the submit
    // call that produced them).
    GpuDisplayViewport::PresentationOptions m_reconPresentationOptions;
    QOpenGLTexture *m_texture;
    QOpenGLTexture *m_gpuReconSourceTexture;
    QImage m_pendingImage;
    int m_pendingTextureWidth;
    int m_pendingTextureHeight;
    int m_pendingDisplayWidth;
    int m_pendingDisplayHeight;
    bool m_gpuReconSourceTextureCurrent;
    bool m_pendingTextureFromGpuRecon;
    bool m_textureFromGpuRecon;
    bool m_texturePresentationActive;
    bool m_textureDirty;
    quint64 m_pendingPresentationSerial;
    bool m_pendingPresentationSerialValid;
    quint64 m_presentedSerial;
    bool m_presentedSerialValid;
    /* Set only for the duration of a synchronous grabPresentedFramebufferIfActive()
     * call: tells paintGL() to glReadPixels into m_captureReadback* right after it
     * finishes drawing, so the readback and the draw it reads back are the same call. */
    bool m_captureReadbackRequested;
    bool m_captureReadbackSucceeded;
    QImage m_captureReadbackImage;
    QString m_captureReadbackError;
    bool m_loggedContext;
    bool m_loggedPaint;
    bool m_loggedPresented;
    bool m_loggedSetImage;
    bool m_loggedSetGpuTexture;
    QString m_rendererDescription;

    // Swap telemetry (see resetSwapTelemetry()/swapTelemetrySnapshot()/noteRealSwap()).
    // The session id/active-state live at file scope in the .cpp, not here -- see the
    // resetSwapTelemetry() doc comment above.
    std::array<SwapTelemetryRecord, kSwapTelemetryRingCapacity> m_swapTelemetryRing;
    GpuWindowSwapTelemetryCounters m_swapTelemetryCounters;
};

#endif // GPUDISPLAYWINDOW_H
