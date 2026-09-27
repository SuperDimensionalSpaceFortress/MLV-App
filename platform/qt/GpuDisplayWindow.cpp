/*!
 * \file GpuDisplayWindow.cpp
 * \author Claude
 * \copyright 2026
 * \brief QOpenGLWindow-based GPU preview for NVIDIA Optimus hybrid laptops (see header).
 */

#include "GpuDisplayWindow.h"
#include "GpuDebayer.h"
#include "debug/StageTiming.h"

#include <QGraphicsView>
#include <QWidget>
#include <QGridLayout>
#include <QLayout>
#include <QThread>
#include <QByteArray>
#include <QDateTime>
#include <QSurfaceFormat>
#include <QOpenGLContext>
#include <QElapsedTimer>
#include <QMutex>
#include <QDir>
#include <QtDebug>
#include <algorithm>
#include <atomic>
#include <cstring>
#include <limits>

namespace
{
/* The active window handle is read on the render thread (present/clear) and
 * written on the GUI thread (install / destructor). std::atomic closes the data
 * race; g_activeMutex serializes the render-thread read+post against the GUI-thread
 * destructor clear so the receiver cannot be freed between the load and the post. */
std::atomic<GpuDisplayWindow *> g_activeWindow{nullptr};
QMutex g_activeMutex;

// Swap-telemetry session identity: file scope, not a GpuDisplayWindow member, so it
// survives the window being inactive when a session begins or being destroyed and
// recreated mid-session -- resetSwapTelemetry()/swapTelemetrySnapshot() must agree with
// MainWindow's playback_smoke.gate session id regardless of window lifetime. GUI-thread
// only, like the swaps themselves, so no synchronization is needed.
quint64 g_swapTelemetrySessionId = 0;
bool g_swapTelemetrySessionActive = false;
double g_swapTelemetrySessionBeginQpcMs = 0.0;

bool windowEnvFlagEnabled(const QByteArray &value)
{
    if ( value.isEmpty() ) return false;
    const QByteArray normalized = value.trimmed().toLower();
    return normalized == "1" || normalized == "true" || normalized == "yes" || normalized == "on";
}

// Same env var and the same "set and not literally 0" semantics as MainWindow's
// playbackSmokeFrameTelemetryEnabled() -- deliberately reused rather than a new flag, so
// one env var opts into both the frame-submission and the real-swap telemetry together.
bool swapTelemetryEnabled()
{
    static const bool enabled =
        qEnvironmentVariableIsSet( "MLVAPP_PLAYBACK_SMOKE_TELEMETRY" )
        && qEnvironmentVariable( "MLVAPP_PLAYBACK_SMOKE_TELEMETRY" ) != QStringLiteral("0");
    return enabled;
}

/* GLSL 1.20 passthrough -- works in the NVIDIA compatibility context a QOpenGLWindow
 * gets by default, no LUT/Bayer uniforms. Milestone 1 displays the already-final CPU
 * RGBA frame; the shared GpuPreviewProcessing shader (zebras/LUTs/Bayer) comes with the
 * shared-renderer milestone. */
const char *kVertexShader =
    "attribute vec2 position;\n"
    "attribute vec2 texCoord;\n"
    "varying vec2 vTexCoord;\n"
    "void main() {\n"
    "    vTexCoord = texCoord;\n"
    "    gl_Position = vec4(position, 0.0, 1.0);\n"
    "}\n";

const char *kFragmentShader =
    "varying vec2 vTexCoord;\n"
    "uniform sampler2D frameTexture;\n"
    "void main() {\n"
    "    gl_FragColor = texture2D(frameTexture, vTexCoord);\n"
    "}\n";
}

const char *GpuDisplayWindow::environmentVariableName()
{
    return "MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT";
}

bool GpuDisplayWindow::isRequestedByEnvironment()
{
    return windowEnvFlagEnabled(qgetenv(environmentVariableName()));
}

bool GpuDisplayWindow::isActive()
{
    return g_activeWindow.load(std::memory_order_acquire) != nullptr;
}

QWindow *GpuDisplayWindow::activeWindow()
{
    return g_activeWindow.load(std::memory_order_acquire);
}

QString GpuDisplayWindow::rendererDescription()
{
    GpuDisplayWindow *win = g_activeWindow.load(std::memory_order_acquire);
    return win ? win->m_rendererDescription : QString();
}

QSize GpuDisplayWindow::effectiveDisplaySizeForImage(const QSize &imageSize,
                                                     const QSize &requestedDisplaySize)
{
    if ( requestedDisplaySize.width() > 0 && requestedDisplaySize.height() > 0 )
    {
        return requestedDisplaySize;
    }
    return imageSize;
}

QSize GpuDisplayWindow::displaySize()
{
    GpuDisplayWindow *win = g_activeWindow.load(std::memory_order_acquire);
    return win ? win->size() : QSize();
}

void GpuDisplayWindow::resetSwapTelemetry(quint64 sessionId)
{
    // Telemetry off: no clock sample, no state change -- the instrument does no work at all
    // (CUDA-PERF-DISPLAY-IDENTITY-3, sol on #161).
    if ( !swapTelemetryEnabled() ) return;
    // Session identity is set unconditionally, before any window lookup, so it is
    // correct even when no window is active yet at session begin (hardening: the
    // summary must never print a different session than playback_smoke.gate's).
    g_swapTelemetrySessionId = sessionId;
    g_swapTelemetrySessionActive = true;
    g_swapTelemetrySessionBeginQpcMs = mlv_stage_timing_now() * 1000.0;

    GpuDisplayWindow *win = g_activeWindow.load(std::memory_order_acquire);
    if ( !win ) return;
    win->m_swapTelemetryCounters = GpuWindowSwapTelemetryCounters();
}

GpuWindowSwapTelemetrySnapshot GpuDisplayWindow::swapTelemetrySnapshot()
{
    // Telemetry off: return the default (telemetryEnabled=false) snapshot before any clock sample
    // or state change; no session was ever opened by resetSwapTelemetry either.
    if ( !swapTelemetryEnabled() ) return GpuWindowSwapTelemetrySnapshot();
    // Closes the session: called once, at playback-smoke session end. Deactivating
    // before returning means any swap that happens later in this same synchronous
    // call stack -- e.g. a queued or screenshot-capture swap after the gate -- is not
    // recorded under this (now-finished) session, so this summary and every
    // already-emitted per-swap gpu_window.swap line always agree.
    const double gateQpcMs = mlv_stage_timing_now() * 1000.0;

    GpuWindowSwapTelemetrySnapshot snapshot;
    snapshot.telemetryEnabled = swapTelemetryEnabled();
    snapshot.sessionId = g_swapTelemetrySessionId;
    g_swapTelemetrySessionActive = false;

    GpuDisplayWindow *win = g_activeWindow.load(std::memory_order_acquire);
    if ( !win ) return snapshot;
    snapshot.windowActive = true;
    snapshot.summary = GpuWindowSwapTelemetryPolicy::summarize(
        win->m_swapTelemetryCounters, g_swapTelemetrySessionBeginQpcMs, gateQpcMs );
    return snapshot;
}

void GpuDisplayWindow::noteRealSwap()
{
    if ( !swapTelemetryEnabled() ) return;
    // Closed session (past the gate, or no session begun yet): a queued or
    // screenshot-capture swap must not be attributed to a finished session.
    if ( !g_swapTelemetrySessionActive ) return;

    const double qpcMs = mlv_stage_timing_now() * 1000.0;
    const quint64 swapSerial = ++m_swapTelemetryCounters.swapCount;
    const QString utc = QDateTime::currentDateTimeUtc().toString( Qt::ISODateWithMs );

    SwapTelemetryRecord record;
    record.swapSerial = swapSerial;
    record.qpcMs = qpcMs;
    record.presentedSerial = m_presentedSerial;
    record.presentedSerialValid = m_presentedSerialValid;
    m_swapTelemetryRing[ static_cast<std::size_t>( ( swapSerial - 1 ) % kSwapTelemetryRingCapacity ) ] = record;

    if ( swapSerial == 1 )
    {
        m_swapTelemetryCounters.firstSwapQpcMs = qpcMs;
        m_swapTelemetryCounters.firstSwapUtc = utc;
    }
    else
    {
        const double gapMs = qpcMs - m_swapTelemetryCounters.lastSwapQpcMs;
        if ( gapMs > m_swapTelemetryCounters.maxGapMs )
        {
            m_swapTelemetryCounters.maxGapMs = gapMs;
            m_swapTelemetryCounters.maxGapBeforeSerial = swapSerial - 1;
            m_swapTelemetryCounters.maxGapAfterSerial = swapSerial;
        }
    }
    m_swapTelemetryCounters.lastSwapQpcMs = qpcMs;
    m_swapTelemetryCounters.lastSwapUtc = utc;

    qInfo().noquote()
        << QStringLiteral(
               "gpu_window.swap session=%1 serial=%2 qpc_ms=%3 utc=%4 "
               "presented_serial=%5 presented_serial_valid=%6" )
               .arg( static_cast<qulonglong>( g_swapTelemetrySessionId ) )
               .arg( static_cast<qulonglong>( record.swapSerial ) )
               .arg( record.qpcMs, 0, 'f', 3 )
               .arg( utc )
               .arg( static_cast<qulonglong>( record.presentedSerial ) )
               .arg( record.presentedSerialValid ? 1 : 0 );
}

bool GpuDisplayWindow::installInPreview(QGraphicsView *view)
{
    if ( !view || !isRequestedByEnvironment() ) return false;
    if ( g_activeWindow ) return true;

    GpuDisplayWindow *win = new GpuDisplayWindow();
    QWidget *container = QWidget::createWindowContainer(win);
    container->setObjectName(QStringLiteral("gpuDisplayWindowContainer"));
    container->setSizePolicy(QSizePolicy::Expanding, QSizePolicy::Expanding);
    container->setFocusPolicy(Qt::NoFocus);

    QWidget *host = view->parentWidget();
    bool placed = false;
    if ( host && host->layout() )
    {
        if ( QGridLayout *grid = qobject_cast<QGridLayout *>(host->layout()) )
        {
            const int idx = grid->indexOf(view);
            if ( idx >= 0 )
            {
                int r = 0, c = 0, rs = 1, cs = 1;
                grid->getItemPosition(idx, &r, &c, &rs, &cs);
                grid->removeWidget(view);   // clean swap: don't leave two items in the cell
                container->setParent(host);
                grid->addWidget(container, r, c, rs, cs);
                placed = true;
            }
        }
        if ( !placed )
        {
            host->layout()->replaceWidget(view, container);
            placed = true;
        }
    }
    if ( !placed )
    {
        container->setParent(host ? host : view);
        container->setGeometry(view->geometry());
        container->raise();
    }
    view->hide();
    container->show();

    g_activeWindow.store(win, std::memory_order_release);
    qInfo() << "Experimental GPU window viewport enabled via"
            << environmentVariableName()
            << "- preview renders through a QOpenGLWindow (createWindowContainer).";
    return true;
}

bool GpuDisplayWindow::presentImageIfActive(const QImage &image,
                                            const QSize &displaySize,
                                            quint64 presentationSerial)
{
    QMutexLocker lock(&g_activeMutex);
    GpuDisplayWindow *win = g_activeWindow.load(std::memory_order_acquire);
    if ( !win ) return false;

    if ( QThread::currentThread() == win->thread() )
    {
        // Same (GUI) thread: setPresentedImage deep-copies synchronously, and the
        // destructor runs on this same thread, so there is no race here.
        win->setPresentedImage(image, displaySize, presentationSerial);
    }
    else
    {
        // Render thread: the source QImage is often a NON-owning view over a frame
        // buffer the prefetch pipeline frees right after this returns. Deep-copy NOW
        // (while it is alive) so the deferred GUI-thread upload reads owned memory --
        // the copy() inside setPresentedImage would run too late. The mutex keeps the
        // destructor from freeing `win` between this load and the queued post.
        const QImage owned = image.copy();
        QMetaObject::invokeMethod(win, [win, owned, displaySize, presentationSerial]() {
                                      win->setPresentedImage(owned, displaySize, presentationSerial);
                                  },
                                  Qt::QueuedConnection);
    }
    return true;
}

bool GpuDisplayWindow::clearIfActive()
{
    QMutexLocker lock(&g_activeMutex);
    GpuDisplayWindow *win = g_activeWindow.load(std::memory_order_acquire);
    if ( !win ) return false;

    if ( QThread::currentThread() == win->thread() )
        win->clearPresented();
    else
        QMetaObject::invokeMethod(win, [win]() { win->clearPresented(); }, Qt::QueuedConnection);
    return true;
}

bool GpuDisplayWindow::presentGpuPlaybackReconAmazePostWbTextureIfActive(
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
    quint64 presentationSerial)
{
    QMutexLocker lock(&g_activeMutex);
    GpuDisplayWindow *win = g_activeWindow.load(std::memory_order_acquire);
    if ( !win )
    {
        if ( reason ) *reason = QStringLiteral("GPU window texture-present requires an active GPU display window");
        return false;
    }
    if ( QThread::currentThread() != win->thread() )
    {
        if ( reason ) *reason = QStringLiteral("GPU window texture-present must run on the GUI thread");
        return false;
    }
    return win->setPresentedGpuPlaybackReconAmazePostWbTexture(
        rawInputBayer14,
        rawInputBayer14Words,
        state,
        blackLevel,
        wbMultipliers,
        options,
        reason,
        timing,
        handoffMode,
        validationProbeTexture,
        retainedDeviceBayer16,
        retainedDeviceWidth,
        retainedDeviceHeight,
        displayWidth,
        displayHeight,
        presentationSerial);
}

bool GpuDisplayWindow::readGpuReconSourceBayer16TextureIfActive(
    QByteArray *textureBytes,
    int *width,
    int *height,
    QString *reason)
{
    QMutexLocker lock(&g_activeMutex);
    GpuDisplayWindow *win = g_activeWindow.load(std::memory_order_acquire);
    if ( !win )
    {
        if ( reason ) *reason = QStringLiteral("GPU window recon-source texture readback requires an active GPU display window");
        return false;
    }
    if ( QThread::currentThread() != win->thread() )
    {
        if ( reason ) *reason = QStringLiteral("GPU window recon-source texture readback must run on the GUI thread");
        return false;
    }
    return win->readGpuReconSourceBayer16Texture(textureBytes, width, height, reason);
}

GpuDisplayWindow::GpuDisplayWindow(QWindow *parent)
    : QOpenGLWindow(QOpenGLWindow::NoPartialUpdate, parent)
    , m_program(nullptr)
    , m_previewProcessingProgram(nullptr)
    , m_texture(nullptr)
    , m_gpuReconSourceTexture(nullptr)
    , m_pendingTextureWidth(0)
    , m_pendingTextureHeight(0)
    , m_pendingDisplayWidth(0)
    , m_pendingDisplayHeight(0)
    , m_gpuReconSourceTextureCurrent(false)
    , m_pendingTextureFromGpuRecon(false)
    , m_textureFromGpuRecon(false)
    , m_texturePresentationActive(false)
    , m_textureDirty(false)
    , m_pendingPresentationSerial(0)
    , m_pendingPresentationSerialValid(false)
    , m_presentedSerial(0)
    , m_presentedSerialValid(false)
    , m_captureReadbackRequested(false)
    , m_captureReadbackSucceeded(false)
    , m_loggedContext(false)
    , m_loggedPaint(false)
    , m_loggedPresented(false)
    , m_loggedSetImage(false)
    , m_loggedSetGpuTexture(false)
{
    QSurfaceFormat fmt = format();
    fmt.setSwapInterval(0);
    setFormat(fmt);

    // Real swap path 1 of 2: Qt's own automatic swap after paintGL() returns, in its
    // normal paint-event cycle (frameSwapped() fires only for THIS swap, not for the
    // explicit manual one in grabPresentedFramebufferIfActive -- see path 2 there).
    // Decided once, here, rather than re-checked per swap: the env var is cached (see
    // swapTelemetryEnabled()) and cannot change mid-run, so when telemetry is off this
    // window never even connects the signal -- zero cost, not just an early return in
    // the slot.
    if ( swapTelemetryEnabled() )
    {
        connect(this, &QOpenGLWindow::frameSwapped, this, &GpuDisplayWindow::noteRealSwap);
    }
}

GpuDisplayWindow::~GpuDisplayWindow()
{
    {
        // Clear the active handle under the same lock the present path uses, so a
        // render-thread present cannot post to this window after it is cleared/freed.
        QMutexLocker lock(&g_activeMutex);
        if ( g_activeWindow.load(std::memory_order_acquire) == this )
            g_activeWindow.store(nullptr, std::memory_order_release);
    }
    // Tear down GL objects only while the context is valid (makeCurrent() is void on
    // QOpenGLWindow); anything missed here is reclaimed when the context is destroyed.
    if ( isValid() )
    {
        makeCurrent();
        cleanupGLResources();
        doneCurrent();
    }
}

void GpuDisplayWindow::setPresentedImage(const QImage &image,
                                         const QSize &displaySize,
                                         quint64 presentationSerial)
{
    const QSize previousDisplaySize(m_pendingDisplayWidth,
                                    m_pendingDisplayHeight);
    const QSize previousImageSize = m_pendingImage.size();
    const QSize previousTextureSize(m_pendingTextureWidth,
                                    m_pendingTextureHeight);
    m_pendingImage = image.format() == QImage::Format_RGBA8888
        ? image.copy()
        : image.convertToFormat(QImage::Format_RGBA8888);
    QSize effectiveDisplaySize =
        effectiveDisplaySizeForImage(m_pendingImage.size(), displaySize);
    if ( effectiveDisplaySize == m_pendingImage.size()
      && previousDisplaySize.width() > 0
      && previousDisplaySize.height() > 0
      && previousDisplaySize != effectiveDisplaySize
      && ( previousImageSize == m_pendingImage.size()
        || previousTextureSize == m_pendingImage.size() ) )
    {
        effectiveDisplaySize = previousDisplaySize;
    }
    m_pendingTextureFromGpuRecon = false;
    m_gpuReconSourceTextureCurrent = false;
    m_texturePresentationActive = false;
    m_pendingDisplayWidth = effectiveDisplaySize.width();
    m_pendingDisplayHeight = effectiveDisplaySize.height();
    m_textureDirty = true;
    m_pendingPresentationSerial = presentationSerial;
    m_pendingPresentationSerialValid = presentationSerial != 0;
    if ( !m_loggedSetImage )
    {
        qInfo().nospace() << "gpu_window setPresentedImage: first frame received ("
                          << m_pendingImage.width() << "x" << m_pendingImage.height()
                          << ", display=" << m_pendingDisplayWidth
                          << "x" << m_pendingDisplayHeight << ").";
        m_loggedSetImage = true;
    }
    update();
}

void GpuDisplayWindow::clearPresented()
{
    m_pendingImage = QImage();
    m_pendingTextureWidth = 0;
    m_pendingTextureHeight = 0;
    m_pendingDisplayWidth = 0;
    m_pendingDisplayHeight = 0;
    m_gpuReconSourceTextureCurrent = false;
    m_pendingTextureFromGpuRecon = false;
    m_texturePresentationActive = false;
    m_textureDirty = true;
    m_pendingPresentationSerial = 0;
    m_pendingPresentationSerialValid = false;
    m_presentedSerial = 0;
    m_presentedSerialValid = false;
    update();
}

bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture(
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
    quint64 presentationSerial)
{
    auto fail = [&](const QString &why) -> bool
    {
        if ( reason ) *reason = why;
        if ( handoffMode ) handoffMode->clear();
        m_gpuReconSourceTextureCurrent = false;
        m_pendingTextureFromGpuRecon = false;
        m_texturePresentationActive = false;
        return false;
    };

    if ( !state || !state->valid || state->width <= 0 || state->height <= 0 || !wbMultipliers )
    {
        return fail(QStringLiteral("GPU window playback recon texture-present input is invalid"));
    }

    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1): this texture is the post-WB-undo linear
    // camera RGB output of AMaZE -- it is NOT display-referred, and drawing it straight
    // through a passthrough shader (or through the processing shader with the LUTs
    // missing/disabled) is exactly the dark grey-green regression this fix exists to
    // close. Refuse the present up front, before any GL work, rather than ever showing
    // that linear content on screen; the caller's existing fallback (CPU/readback route,
    // or simply not presenting this frame via the no-readback texture route) takes over.
    const GpuPreviewProcessingConfig &previewProcessing = options.previewProcessing;
    const bool previewProcessingOptionsUsable =
        previewProcessing.enabled
        && previewProcessing.levelsLut.size() >= static_cast<int>(65536u * sizeof(uint16_t))
        && previewProcessing.matrixLutR.size() >= static_cast<int>(65536u * sizeof(uint16_t))
        && previewProcessing.matrixLutG.size() >= static_cast<int>(65536u * sizeof(uint16_t))
        && previewProcessing.matrixLutB.size() >= static_cast<int>(65536u * sizeof(uint16_t))
        && previewProcessing.gammaLut.size() >= static_cast<int>(65536u * sizeof(uint16_t));
    if ( !previewProcessingOptionsUsable )
    {
        return fail(QStringLiteral(
            "GPU window playback recon texture-present refused: preview-processing options/LUTs "
            "are not ready for a linear post-WB-undo texture "
            "(trace=gpu_window_recon_missing_processing_options)"));
    }

    const int texWidth = state->width;
    const int texHeight = state->height;
    const bool retainedDeviceValid =
        retainedDeviceBayer16
        && retainedDeviceWidth == texWidth
        && retainedDeviceHeight == texHeight
        && !validationProbeTexture;
    const size_t expectedWords =
        static_cast<size_t>(texWidth) * static_cast<size_t>(texHeight);
    if ( !rawInputBayer14 && !retainedDeviceValid )
    {
        return fail(QStringLiteral("GPU window playback recon texture-present input is invalid"));
    }
    if ( !retainedDeviceValid && rawInputBayer14Words < expectedWords )
    {
        return fail(QStringLiteral("GPU window playback recon texture-present Bayer input is incomplete"));
    }
    if ( handoffMode ) handoffMode->clear();

    QElapsedTimer wallTimer;
    wallTimer.start();
    auto elapsedMs = [&wallTimer]() -> double
    {
        return static_cast<double>(wallTimer.nsecsElapsed()) / 1000000.0;
    };
    double contextMs = 0.0;
    double setupMs = 0.0;
    double reconWallMs = 0.0;
    double amazeWallMs = 0.0;
    double postMs = 0.0;

    QOpenGLContext *glContext = context();
    if ( !glContext )
    {
        return fail(QStringLiteral("GPU window texture-present requires an initialized OpenGL context"));
    }

    const double contextStartMs = elapsedMs();
    const bool needsCurrent = QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (makeCurrent(), true) : false;
    contextMs = elapsedMs() - contextStartMs;

    const double setupStartMs = elapsedMs();
    ensurePreviewProcessingProgram();
    if ( !m_previewProcessingProgram )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral("GPU window texture-present shader setup failed"));
    }
    gpuPreviewProcessingUpdateLutTextureSet(m_lutSet, previewProcessing);
    if ( !gpuPreviewProcessingLutTextureSetReady(m_lutSet, previewProcessing) )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral(
            "GPU window playback recon texture-present refused: LUT texture upload failed "
            "for a linear post-WB-undo texture (trace=gpu_window_recon_lut_upload_failed)"));
    }

    if ( !m_texture
      || m_texture->width() != texWidth
      || m_texture->height() != texHeight
      || !m_textureFromGpuRecon )
    {
        destroyTexture();
        m_texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
        m_texture->setFormat(QOpenGLTexture::RGBA16_UNorm);
        m_texture->setSize(texWidth, texHeight);
        m_texture->setMipLevels(1);
        m_texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16);
        m_texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    }
    m_textureFromGpuRecon = true;
    if ( !m_gpuReconSourceTexture
      || m_gpuReconSourceTexture->width() != texWidth
      || m_gpuReconSourceTexture->height() != texHeight )
    {
        delete m_gpuReconSourceTexture;
        m_gpuReconSourceTexture = new QOpenGLTexture(QOpenGLTexture::Target2D);
        m_gpuReconSourceTexture->setFormat(QOpenGLTexture::R16_UNorm);
        m_gpuReconSourceTexture->setSize(texWidth, texHeight);
        m_gpuReconSourceTexture->setMipLevels(1);
        m_gpuReconSourceTexture->allocateStorage(QOpenGLTexture::Red, QOpenGLTexture::UInt16);
        m_gpuReconSourceTexture->setWrapMode(QOpenGLTexture::ClampToEdge);
    }
    m_gpuReconSourceTextureCurrent = false;
    applySamplingMode(options.samplingMode);
    setupMs = elapsedMs() - setupStartMs;

    int rc = -1;
    llrpGpuPlaybackReconTiming_t reconTiming;
    memset(&reconTiming, 0, sizeof(reconTiming));
    GpuAmazeDebayerBackendTiming amazeTiming;
    QString amazeReason;
    QString amazeRenderer;
    QString handoffModeValue;
    QString directFailureReason;
    bool reconOk = false;
    bool amazeOk = false;
    bool sourceTextureCurrent = false;

    {
        llrpGpuPlaybackReconTiming_t directReconTiming;
        memset(&directReconTiming, 0, sizeof(directReconTiming));
        GpuAmazeDebayerBackendTiming directAmazeTiming;
        QString directAmazeReason;
        QString directAmazeRenderer;
        const uint16_t *deviceBayer16 = nullptr;
        int deviceWidth = 0;
        int deviceHeight = 0;
        int directRc = -1;
        const double directReconStartMs = elapsedMs();
        bool directReconOk = false;
        if ( retainedDeviceValid )
        {
            deviceBayer16 = retainedDeviceBayer16;
            deviceWidth = retainedDeviceWidth;
            deviceHeight = retainedDeviceHeight;
            directRc = 0;
            directReconOk = true;
        }
        else
        {
            directReconOk =
                llrpGpuPlaybackReconRunDeviceBayer16(state,
                                                     rawInputBayer14,
                                                     expectedWords * sizeof(uint16_t),
                                                     &deviceBayer16,
                                                     &deviceWidth,
                                                     &deviceHeight,
                                                     &directRc,
                                                     &directReconTiming) != 0;
        }
        const double directReconWallMs = elapsedMs() - directReconStartMs;
        if ( directReconOk
          && deviceBayer16
          && deviceWidth == texWidth
          && deviceHeight == texHeight )
        {
            const double directAmazeStartMs = elapsedMs();
            const bool directAmazeOk =
                gpuAmazeDebayerRenderPostWbGlTextureFromDeviceBayer16(
                    deviceBayer16,
                    m_texture->textureId(),
                    texWidth,
                    texHeight,
                    blackLevel,
                    wbMultipliers,
                    &directAmazeReason,
                    &directAmazeRenderer,
                    &directAmazeTiming);
            const double directAmazeWallMs = elapsedMs() - directAmazeStartMs;
            if ( directAmazeOk )
            {
                int validationProofRc = 0;
                llrpGpuPlaybackReconTiming_t validationProofTiming;
                memset(&validationProofTiming, 0, sizeof(validationProofTiming));
                const bool validationProofOk =
                    !validationProbeTexture
                    || llrpGpuPlaybackReconRunGlTexture(
                        state,
                        rawInputBayer14,
                        expectedWords * sizeof(uint16_t),
                        m_gpuReconSourceTexture->textureId(),
                        &validationProofRc,
                        &validationProofTiming) != 0;
                if ( validationProofOk )
                {
                    rc = directRc;
                    reconTiming = directReconTiming;
                    amazeTiming = directAmazeTiming;
                    amazeReason = directAmazeReason;
                    amazeRenderer = directAmazeRenderer;
                    Q_UNUSED(amazeReason);
                    Q_UNUSED(amazeRenderer);
                    reconWallMs = directReconWallMs;
                    amazeWallMs = directAmazeWallMs;
                    reconOk = true;
                    amazeOk = true;
                    handoffModeValue = retainedDeviceValid
                        ? QStringLiteral("retained_device_bayer16")
                        : QStringLiteral("direct_device_bayer16");
                    sourceTextureCurrent = validationProbeTexture;
                }
                else
                {
                    directFailureReason =
                        QStringLiteral(
                            "GPU window direct device proof texture render failed (rc=%1)")
                            .arg(validationProofRc);
                }
            }
            else
            {
                directFailureReason = directAmazeReason.isEmpty()
                    ? QStringLiteral("GPU window AMaZE direct device texture-present failed")
                    : directAmazeReason;
            }
        }
        else
        {
            directFailureReason =
                QStringLiteral("GPU window playback recon direct device handoff failed (recon_rc=%1)")
                    .arg(directRc);
        }
    }

    if ( !reconOk || !amazeOk )
    {
        const double reconStartMs = elapsedMs();
        reconOk =
            llrpGpuPlaybackReconRunGlTexture(state,
                                             rawInputBayer14,
                                             expectedWords * sizeof(uint16_t),
                                             m_gpuReconSourceTexture->textureId(),
                                             &rc,
                                             &reconTiming) != 0;
        reconWallMs = elapsedMs() - reconStartMs;
        const double amazeStartMs = elapsedMs();
        amazeOk =
            reconOk
            && gpuAmazeDebayerRenderPostWbGlTextureFromR16GlTexture(
                m_gpuReconSourceTexture->textureId(),
                m_texture->textureId(),
                texWidth,
                texHeight,
                blackLevel,
                wbMultipliers,
                &amazeReason,
                &amazeRenderer,
                &amazeTiming);
        amazeWallMs = elapsedMs() - amazeStartMs;
        if ( reconOk && amazeOk )
        {
            handoffModeValue = QStringLiteral("gl_r16_bridge");
            sourceTextureCurrent = true;
        }
        else if ( !directFailureReason.isEmpty() && amazeReason.isEmpty() )
        {
            amazeReason = directFailureReason;
        }
    }

    const bool ok = reconOk && amazeOk;
    if ( timing )
    {
        memset(timing, 0, sizeof(*timing));
        timing->available = reconTiming.available || amazeTiming.available;
        timing->upload_ms =
            (reconTiming.available ? reconTiming.upload_ms : 0.0)
            + (amazeTiming.available ? amazeTiming.uploadMs : 0.0);
        timing->kernel_ms =
            (reconTiming.available ? reconTiming.kernel_ms : 0.0)
            + (amazeTiming.available ? amazeTiming.kernelMs : 0.0);
        timing->interop_ms =
            (reconTiming.available ? reconTiming.interop_ms : 0.0)
            + (amazeTiming.available ? amazeTiming.downloadMs : 0.0);
        timing->total_ms =
            (reconTiming.available ? reconTiming.total_ms : 0.0)
            + (amazeTiming.available ? amazeTiming.totalMs : 0.0);
    }
    if ( !ok )
    {
        destroyTexture();
        if ( madeCurrent ) doneCurrent();
        if ( rc == LLRP_GPU_PLAYBACK_RECON_RC_UNSUPPORTED_STATE )
        {
            return fail(QStringLiteral(
                "GPU window playback recon AMaZE texture handoff skipped for unsupported live Dual ISO state (rc=%1)").arg(rc));
        }
        if ( reconOk && !amazeReason.isEmpty() )
        {
            return fail(amazeReason);
        }
        return fail(QStringLiteral(
            "GPU window playback recon AMaZE texture handoff failed (recon_rc=%1)").arg(rc));
    }

    const double postStartMs = elapsedMs();
    m_pendingImage = QImage();
    m_pendingTextureWidth = texWidth;
    m_pendingTextureHeight = texHeight;
    m_pendingDisplayWidth = displayWidth > 0 ? displayWidth : texWidth;
    m_pendingDisplayHeight = displayHeight > 0 ? displayHeight : texHeight;
    m_gpuReconSourceTextureCurrent = sourceTextureCurrent;
    m_pendingTextureFromGpuRecon = true;
    m_textureFromGpuRecon = true;
    m_textureDirty = false;
    m_texturePresentationActive = false;
    // Captured for paintGL(), which draws this texture through the shared preview-
    // processing shader (see the fail-closed gate above) and runs later than this call.
    m_reconPresentationOptions = options;
    // This texture is already fully written (the AMaZE render above drew straight into
    // it), unlike the QImage route where the upload is deferred to updateTextureIfNeeded().
    // The presentation serial is still only promoted to m_presentedSerial by paintGL()
    // (see paintGL()), so a submit that never reaches a real paint cannot be mistaken for
    // a presented frame.
    m_pendingPresentationSerial = presentationSerial;
    m_pendingPresentationSerialValid = presentationSerial != 0;
    if ( madeCurrent ) doneCurrent();
    if ( !m_loggedSetGpuTexture )
    {
        qInfo().nospace()
            << "gpu_window setPresentedGpuPlaybackReconAmazePostWbTexture: first texture received ("
            << texWidth << "x" << texHeight
            << ", display=" << m_pendingDisplayWidth
            << "x" << m_pendingDisplayHeight << ").";
        m_loggedSetGpuTexture = true;
    }
    update();
    postMs = elapsedMs() - postStartMs;
    if ( handoffMode ) *handoffMode = handoffModeValue;
    if ( timing )
    {
        timing->wall_ms = elapsedMs();
        timing->host_gap_ms = timing->wall_ms - timing->total_ms;
        timing->context_ms = contextMs;
        timing->setup_ms = setupMs;
        timing->recon_wall_ms = reconWallMs;
        timing->amaze_wall_ms = amazeWallMs;
        timing->post_ms = postMs;
    }
    return true;
}

bool GpuDisplayWindow::readGpuReconSourceBayer16Texture(QByteArray *textureBytes,
                                                       int *width,
                                                       int *height,
                                                       QString *reason)
{
    auto fail = [reason](const QString &why) -> bool
    {
        if ( reason ) *reason = why;
        return false;
    };

    if ( textureBytes ) textureBytes->clear();
    if ( width ) *width = 0;
    if ( height ) *height = 0;
    if ( !textureBytes )
    {
        return fail(QStringLiteral("GPU window recon-source texture readback requires output storage"));
    }
    if ( !m_gpuReconSourceTexture
      || !m_gpuReconSourceTextureCurrent
      || !m_pendingTextureFromGpuRecon
      || m_pendingTextureWidth <= 0
      || m_pendingTextureHeight <= 0 )
    {
        return fail(QStringLiteral("GPU window recon-source texture readback requires an active GPU recon source texture"));
    }

#if defined(QT_OPENGL_ES_2)
    return fail(QStringLiteral("GL texture readback via glGetTexImage is unavailable on OpenGL ES"));
#else
    QOpenGLContext *glContext = context();
    if ( !glContext )
    {
        return fail(QStringLiteral("GPU window recon-source texture readback requires an initialized OpenGL context"));
    }
    const bool needsCurrent = QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (makeCurrent(), true) : false;
    QOpenGLFunctions *gl = glContext->functions();
    if ( !gl )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral("GPU window recon-source texture readback requires OpenGL functions"));
    }
    using GlGetTexImageFn = void (*)(GLenum, GLint, GLenum, GLenum, void *);
    GlGetTexImageFn glGetTexImageProc =
        reinterpret_cast<GlGetTexImageFn>(glContext->getProcAddress("glGetTexImage"));
    if ( !glGetTexImageProc )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral("GL texture readback function glGetTexImage is unavailable"));
    }

    const size_t byteCount =
        static_cast<size_t>(m_pendingTextureWidth)
        * static_cast<size_t>(m_pendingTextureHeight)
        * sizeof(uint16_t);
    if ( byteCount > static_cast<size_t>(std::numeric_limits<int>::max()) )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral("GPU window recon-source texture is too large to read back"));
    }
    textureBytes->resize(static_cast<int>(byteCount));
    m_gpuReconSourceTexture->bind(0);
    gl->glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glGetTexImageProc(GL_TEXTURE_2D,
                      0,
                      GL_RED,
                      GL_UNSIGNED_SHORT,
                      textureBytes->data());
    const GLenum error = gl->glGetError();
    m_gpuReconSourceTexture->release();
    if ( madeCurrent ) doneCurrent();
    if ( error != GL_NO_ERROR )
    {
        textureBytes->clear();
        return fail(QStringLiteral("glGetTexImage failed for GPU window recon-source texture with GL error 0x%1")
                    .arg(static_cast<unsigned int>(error), 0, 16));
    }
    if ( width ) *width = m_pendingTextureWidth;
    if ( height ) *height = m_pendingTextureHeight;
    if ( reason ) reason->clear();
    return true;
#endif
}

bool GpuDisplayWindow::grabPresentedFramebufferIfActive(QImage *outImage,
                                                        QString *reason,
                                                        quint64 *presentedSerial,
                                                        bool *presentedSerialValid)
{
    if ( presentedSerial ) *presentedSerial = 0;
    if ( presentedSerialValid ) *presentedSerialValid = false;

    QMutexLocker lock(&g_activeMutex);
    GpuDisplayWindow *win = g_activeWindow.load(std::memory_order_acquire);
    if ( !win )
    {
        if ( reason ) *reason = QStringLiteral("GPU window framebuffer readback requires an active GPU display window");
        return false;
    }
    if ( QThread::currentThread() != win->thread() )
    {
        if ( reason ) *reason = QStringLiteral("GPU window framebuffer readback must run on the GUI thread");
        return false;
    }
    if ( !win->isExposed() || !win->isValid() )
    {
        if ( reason ) *reason = QStringLiteral("GPU window framebuffer readback requires an exposed, valid window");
        return false;
    }

    QOpenGLContext *glContext = win->context();
    if ( !glContext )
    {
        if ( reason ) *reason = QStringLiteral("GPU window framebuffer readback requires an initialized OpenGL context");
        return false;
    }
    const bool needsCurrent = QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (win->makeCurrent(), true) : false;

    // Call this window's real paintGL() SYNCHRONOUSLY, on the GUI thread, exactly as Qt's
    // own paint-event cycle would -- it is not suppressed or isolated in any way, so it
    // promotes whatever frame is currently pending (QImage upload or GPU-recon texture,
    // whichever route is pending) exactly as a real paint would. Reading naively via
    // QOpenGLWindow::grabFramebuffer() instead would see the BACK buffer left over from a
    // PRIOR frame once a swap has already happened (as it always has by the time a
    // screenshot is requested) -- content the GL spec leaves undefined post-swap, measured
    // here as a solid black readback despite a successful present -- so paintGL() draws
    // fresh into that framebuffer, reads it back from the END OF THAT SAME paintGL() call
    // (see m_captureReadbackRequested there), and only then is it swapped, below. Because
    // the draw and the readback are literally the same call, there is no capture/paint race
    // to isolate: whatever paintGL() just drew is unconditionally what gets read back and
    // then swapped, and the presentation serial promoted inside that same call (also see
    // paintGL()) identifies exactly that content. Owner-visible effect: a capture can
    // present a pending frame one paint earlier than Qt's own event loop otherwise would.
    win->m_captureReadbackRequested = true;
    win->m_captureReadbackSucceeded = false;
    win->m_captureReadbackImage = QImage();
    win->m_captureReadbackError.clear();
    win->paintGL();
    win->m_captureReadbackRequested = false;

    if ( presentedSerial ) *presentedSerial = win->m_presentedSerial;
    if ( presentedSerialValid ) *presentedSerialValid = win->m_presentedSerialValid;

    if ( !win->m_captureReadbackSucceeded )
    {
        if ( madeCurrent ) win->doneCurrent();
        if ( reason ) *reason = win->m_captureReadbackError.isEmpty()
            ? QStringLiteral("GPU window framebuffer readback failed")
            : win->m_captureReadbackError;
        return false;
    }

    // The capture-triggered paintGL() call above genuinely drew and read back a frame --
    // swap now so the window's own swapchain reflects exactly what was just captured,
    // making this a real present rather than a side-channel readback.
    glContext->swapBuffers(win);
    // Real swap path 2 of 2: this manual swap runs outside Qt's own paint-event cycle, so
    // QOpenGLWindow's frameSwapped() signal (path 1, connected in the constructor) does
    // NOT fire for it -- record it explicitly so swap telemetry covers every real swap.
    if ( swapTelemetryEnabled() ) win->noteRealSwap();
    if ( madeCurrent ) win->doneCurrent();

    if ( outImage ) *outImage = win->m_captureReadbackImage;
    if ( reason ) reason->clear();
    return true;
}

void GpuDisplayWindow::initializeGL()
{
    initializeOpenGLFunctions();

    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 2): mirrors
    // GpuDisplayViewport::initializeGL's connection. Without this, a context recreation
    // (not just window teardown) left m_program/m_previewProcessingProgram/m_lutSet
    // non-null pointing at objects the destroyed context owned, so ensureProgram() and
    // ensurePreviewProcessingProgram()'s "already built, return" fast path would trust
    // them instead of rebuilding against the new context.
    if ( context() )
    {
        connect(context(), &QOpenGLContext::aboutToBeDestroyed, this, [this]()
        {
            cleanupGLResources();
        }, Qt::UniqueConnection);
    }

    if ( m_loggedContext ) return;

    const GLubyte *renderer = glGetString(GL_RENDERER);
    const GLubyte *vendor = glGetString(GL_VENDOR);
    const GLubyte *version = glGetString(GL_VERSION);
    QOpenGLContext *glContext = context();
    const int requestedSwapInterval = format().swapInterval();
    const QSurfaceFormat realizedFormat = glContext ? glContext->format() : format();
    m_rendererDescription = renderer
        ? QString::fromLatin1(reinterpret_cast<const char *>(renderer))
        : QStringLiteral("unknown");
    qInfo().nospace()
        << "Experimental GPU window viewport initialized (renderer=" << m_rendererDescription
        << ", vendor=" << (vendor ? reinterpret_cast<const char *>(vendor) : "unknown")
        << ", version=" << (version ? reinterpret_cast<const char *>(version) : "unknown")
        << ", requested_swap_interval=" << requestedSwapInterval
        << ", realized_swap_interval=" << realizedFormat.swapInterval()
        << ").";
    m_loggedContext = true;
}

void GpuDisplayWindow::ensureProgram()
{
    if ( m_program ) return;
    m_program = new QOpenGLShaderProgram(this);
    m_program->bindAttributeLocation(QStringLiteral("position"), 0);
    m_program->bindAttributeLocation(QStringLiteral("texCoord"), 1);
    if ( !m_program->addShaderFromSourceCode(QOpenGLShader::Vertex, kVertexShader)
      || !m_program->addShaderFromSourceCode(QOpenGLShader::Fragment, kFragmentShader)
      || !m_program->link() )
    {
        qWarning() << "Experimental GPU window viewport shader setup failed:" << m_program->log();
        delete m_program;
        m_program = nullptr;
    }
}

void GpuDisplayWindow::ensurePreviewProcessingProgram()
{
    // Deliberately no bindAttributeLocation() pre-binding here (unlike ensureProgram()
    // above): this links the shared shader (GpuPreviewProcessing.h) exactly as
    // GpuDisplayViewport does, which queries "position"/"texCoord" locations via
    // attributeLocation() AFTER linking (see paintGL()) rather than pre-binding fixed
    // indices -- pre-binding after gpuPreviewProcessingEnsureDisplayProgram() has
    // already linked would be a no-op and silently diverge from the viewport's plan.
    gpuPreviewProcessingEnsureDisplayProgram(m_previewProcessingProgram, this);
}

void GpuDisplayWindow::destroyTexture()
{
    if ( m_texture || m_gpuReconSourceTexture )
    {
        gpuAmazeDebayerResetR16TextureBackendResources();
        llrpGpuPlaybackReconResetGlTextureResources();
    }
    if ( m_texture )
    {
        delete m_texture;
        m_texture = nullptr;
    }
    if ( m_gpuReconSourceTexture )
    {
        delete m_gpuReconSourceTexture;
        m_gpuReconSourceTexture = nullptr;
    }
    m_pendingTextureFromGpuRecon = false;
    m_gpuReconSourceTextureCurrent = false;
    m_textureFromGpuRecon = false;
    m_texturePresentationActive = false;
    m_pendingTextureWidth = 0;
    m_pendingTextureHeight = 0;
    m_pendingDisplayWidth = 0;
    m_pendingDisplayHeight = 0;
}

void GpuDisplayWindow::cleanupGLResources()
{
    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 3, fable minor 1): mirrors
    // GpuDisplayViewport::cleanupGLResources -- makeCurrent() before deleting GL
    // wrappers (QOpenGLTexture/QOpenGLShaderProgram destructors require a current
    // context to release their GL-side objects; without this, Qt warns and skips the
    // GL-side deletion on a context that is not current, e.g. when this runs from the
    // QOpenGLContext::aboutToBeDestroyed handler with a different context current).
    QOpenGLContext *glContext = context();
    const bool needsCurrent = glContext && QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (makeCurrent(), true) : false;

    destroyTexture();
    gpuPreviewProcessingDestroyLutTextureSet(m_lutSet);
    delete m_program;
    m_program = nullptr;
    delete m_previewProcessingProgram;
    m_previewProcessingProgram = nullptr;

    if ( madeCurrent ) doneCurrent();

    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 3, sol minor 1): destroyTexture()
    // above just deleted the GL texture holding whatever was last presented, without
    // touching m_pendingImage. If that content was a completed QImage upload,
    // m_textureDirty was already false (cleared by the upload that produced it), so
    // updateTextureIfNeeded()'s "not dirty -> skip" fast path would leave the window
    // blank after context recreation until the next frame happens to be submitted.
    // Re-arm the dirty flag so the next paint re-uploads m_pendingImage. A GPU-recon
    // texture has no such retained source to re-upload from (m_pendingImage is empty
    // for that route -- see setPresentedGpuPlaybackReconAmazePostWbTexture/destroyTexture,
    // which already dropped m_pendingTextureFromGpuRecon/m_textureFromGpuRecon above);
    // that content can only be regenerated by the caller re-submitting it.
    if ( !m_pendingImage.isNull() )
    {
        m_textureDirty = true;
    }
}

void GpuDisplayWindow::applySamplingMode(GpuDisplayViewport::SamplingMode samplingMode)
{
    if ( !m_texture ) return;
    // Matches GpuDisplayViewport::applySamplingMode's non-Bayer case exactly (the
    // window's recon texture is always already-debayered RGBA16, frameTextureMode=0,
    // so there is no m_textureIsBayer16-equivalent forced-nearest case here): bicubic
    // is done in-shader (GpuPreviewProcessing.cpp's samplingMode==2 branch) against a
    // Nearest-filtered source, so both SamplingNearest and SamplingBicubic select the
    // GL Nearest filter and only SamplingLinear selects Linear
    // (GPU-TEXNR-S1-DARK-GREEN-1 round 2 -- previously this always forced Linear,
    // ignoring PresentationOptions::samplingMode).
    const bool useNearest = samplingMode == GpuDisplayViewport::SamplingNearest;
    const bool useBicubic = samplingMode == GpuDisplayViewport::SamplingBicubic;
    const QOpenGLTexture::Filter filter = (useNearest || useBicubic)
        ? QOpenGLTexture::Nearest
        : QOpenGLTexture::Linear;
    m_texture->setMinMagFilters(filter, filter);
}

void GpuDisplayWindow::updateTextureIfNeeded()
{
    if ( !m_textureDirty ) return;
    m_textureDirty = false;

    if ( m_pendingImage.isNull() )
    {
        destroyTexture();
        return;
    }
    ensureProgram();
    if ( !m_program ) return;

    const QSize pendingDisplaySize(m_pendingDisplayWidth,
                                   m_pendingDisplayHeight);
    if ( m_textureFromGpuRecon )
    {
        destroyTexture();
    }
    const QImage uploadImage =
        m_pendingImage.format() == QImage::Format_RGBA8888
            ? m_pendingImage
            : m_pendingImage.convertToFormat(QImage::Format_RGBA8888);
    if ( !m_texture
      || m_texture->width() != uploadImage.width()
      || m_texture->height() != uploadImage.height() )
    {
        destroyTexture();
        m_texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
        m_texture->setFormat(QOpenGLTexture::RGBA8_UNorm);
        m_texture->setSize(uploadImage.width(), uploadImage.height());
        m_texture->setMipLevels(1);
        m_texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::UInt8);
        m_texture->setWrapMode(QOpenGLTexture::ClampToEdge);
        m_texture->setMinMagFilters(QOpenGLTexture::Linear, QOpenGLTexture::Linear);
    }
    if ( pendingDisplaySize.width() > 0 && pendingDisplaySize.height() > 0 )
    {
        m_pendingDisplayWidth = pendingDisplaySize.width();
        m_pendingDisplayHeight = pendingDisplaySize.height();
    }
    m_texture->setData(QOpenGLTexture::RGBA,
                       QOpenGLTexture::UInt8,
                       uploadImage.constBits());
    m_pendingTextureFromGpuRecon = false;
    m_gpuReconSourceTextureCurrent = false;
    m_textureFromGpuRecon = false;
    m_pendingTextureWidth = uploadImage.width();
    m_pendingTextureHeight = uploadImage.height();
    m_texturePresentationActive = false;

    // Presentation-serial promotion happens once, uniformly, in paintGL() -- for both
    // this QImage upload and the GPU-recon texture route (which never reaches this
    // function at all, since its texture write happens at submit time) -- so it always
    // reflects whichever route's content m_texture currently holds. See paintGL().
}

void GpuDisplayWindow::paintGL()
{
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);
    glDisable(GL_BLEND);

    const qreal dpr = devicePixelRatio();
    const int fbw = static_cast<int>(width() * dpr);
    const int fbh = static_cast<int>(height() * dpr);
    glViewport(0, 0, std::max(1, fbw), std::max(1, fbh));
    glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    // Always runs, whether this paintGL() call came from Qt's own paint-event cycle or
    // synchronously from grabPresentedFramebufferIfActive() -- a capture is a real paint,
    // so it promotes pending state exactly as any other paint would (see the header doc
    // comment on grabPresentedFramebufferIfActive for the owner-visible effect).
    updateTextureIfNeeded();

    // A GPU-recon texture (post-WB-undo linear camera RGB) must ALWAYS draw through the
    // shared preview-processing shader with its LUTs bound -- never the plain passthrough
    // program, which is only correct for already display-referred content (the QImage
    // route). setPresentedGpuPlaybackReconAmazePostWbTexture already refuses to accept a
    // recon texture whose processing options/LUTs are not usable, but that is re-checked
    // here rather than trusted, so a texture that somehow reached this point with its
    // LUTs torn down (e.g. destroyTexture() racing a paint) is refused rather than ever
    // shown through passthrough (GPU-TEXNR-S1-DARK-GREEN-1).
    const bool presentingReconTexture = m_textureFromGpuRecon;
    const bool reconLutsReady = presentingReconTexture
        && gpuPreviewProcessingLutTextureSetReady(m_lutSet, m_reconPresentationOptions.previewProcessing);
    const bool reconRefused = gpuPreviewProcessingReconTexturePresentationRefused(
        presentingReconTexture, m_lutSet, m_reconPresentationOptions.previewProcessing);
    QOpenGLShaderProgram *activeProgram = presentingReconTexture ? m_previewProcessingProgram : m_program;

    if ( !m_texture || !activeProgram || width() <= 0 || height() <= 0 || reconRefused )
    {
        m_texturePresentationActive = false;
        m_presentedSerialValid = false;
        if ( m_captureReadbackRequested )
        {
            m_captureReadbackSucceeded = false;
            m_captureReadbackError = QStringLiteral("GPU window framebuffer readback found no presented texture");
        }
        if ( !m_loggedPaint )
        { qInfo() << "gpu_window paintGL: no texture/program yet (clearing)."; m_loggedPaint = true; }
        return;
    }

    // Fit the frame to the window, preserving aspect ratio (letterbox).
    const float winAspect = static_cast<float>(width()) / static_cast<float>(height());
    const int displayWidth = m_pendingDisplayWidth > 0 ? m_pendingDisplayWidth : m_texture->width();
    const int displayHeight = m_pendingDisplayHeight > 0 ? m_pendingDisplayHeight : m_texture->height();
    const float imgAspect = static_cast<float>(displayWidth) / static_cast<float>(displayHeight);
    float sx = 1.0f;
    float sy = 1.0f;
    if ( imgAspect > winAspect ) sy = winAspect / imgAspect;
    else                         sx = imgAspect / winAspect;

    // Interleaved [x, y, u, v]; screen-top maps to texture v=0 (matches the corrected
    // GpuDisplayViewport quad so QImage row 0 / image top shows at the top -> upright).
    const float verts[16] = {
        -sx,  sy, 0.0f, 0.0f,
         sx,  sy, 1.0f, 0.0f,
        -sx, -sy, 0.0f, 1.0f,
         sx, -sy, 1.0f, 1.0f,
    };

    activeProgram->bind();
    m_texture->bind(0);
    activeProgram->setUniformValue("frameTexture", 0);
    if ( presentingReconTexture )
    {
        GpuPreviewProcessingDisplayUniforms displayUniforms;
        displayUniforms.textureSize = QVector2D(static_cast<float>(m_texture->width()),
                                                static_cast<float>(m_texture->height()));
        displayUniforms.frameTextureMode = 0;   // already-debayered RGBA16 post-WB-undo texture
        displayUniforms.samplingMode = static_cast<int>(m_reconPresentationOptions.samplingMode);
        displayUniforms.zebraEnabled = m_reconPresentationOptions.showZebras;
        displayUniforms.zebraUnderThreshold = m_reconPresentationOptions.zebraUnderThreshold;
        displayUniforms.zebraOverThreshold = m_reconPresentationOptions.zebraOverThreshold;
        gpuPreviewProcessingBindDisplayUniformsAndTextures(
            activeProgram, m_reconPresentationOptions.previewProcessing, m_lutSet,
            displayUniforms, reconLutsReady);
    }
    // attributeLocation() (not hardcoded 0/1) works for both programs: the passthrough
    // program's locations were pre-bound to 0/1 before linking (see ensureProgram()), and
    // the shared preview-processing program's were left to driver auto-assignment and are
    // looked up the same way GpuDisplayViewport::paintGL() does.
    const int posLoc = activeProgram->attributeLocation("position");
    const int texLoc = activeProgram->attributeLocation("texCoord");
    activeProgram->enableAttributeArray(posLoc);
    activeProgram->enableAttributeArray(texLoc);
    activeProgram->setAttributeArray(posLoc, GL_FLOAT, verts, 2, 4 * sizeof(float));
    activeProgram->setAttributeArray(texLoc, GL_FLOAT, verts + 2, 2, 4 * sizeof(float));
    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    activeProgram->disableAttributeArray(posLoc);
    activeProgram->disableAttributeArray(texLoc);
    m_texture->release();
    if ( presentingReconTexture )
    {
        gpuPreviewProcessingReleaseDisplayTextures(m_lutSet, reconLutsReady);
    }
    activeProgram->release();
    m_texturePresentationActive = true;

    // Promote whichever route (QImage upload or GPU-recon texture) most recently
    // supplied pending state into "presented" -- runs for EVERY real paintGL() draw,
    // including a capture-triggered one (see grabPresentedFramebufferIfActive), so the
    // presented serial always identifies exactly the content m_texture holds right now,
    // for both routes, not only the QImage path. presentedSerialValid is false when the
    // route that produced this content did not supply a presentationSerial.
    m_presentedSerial = m_pendingPresentationSerial;
    m_presentedSerialValid = m_pendingPresentationSerialValid;

    if ( !m_loggedPresented )
    {
        qInfo().nospace() << "gpu_window paintGL: presented a frame texture ("
                          << m_texture->width() << "x" << m_texture->height()
                          << ", display=" << displayWidth
                          << "x" << displayHeight << ").";
        m_loggedPresented = true;
    }

    // Capture readback: grabPresentedFramebufferIfActive() sets m_captureReadbackRequested
    // and calls this paintGL() directly and synchronously, then swaps afterward. Reading
    // back here, at the end of the very call that just drew, means the pixels captured are
    // always exactly what this call presented -- never a stale back buffer, never a frame
    // one step removed from what m_presentedSerial above identifies.
    if ( m_captureReadbackRequested )
    {
        if ( fbw <= 0 || fbh <= 0 )
        {
            m_captureReadbackSucceeded = false;
            m_captureReadbackError = QStringLiteral("GPU window framebuffer readback found a non-positive window size");
        }
        else
        {
            QImage grabbed(fbw, fbh, QImage::Format_RGBA8888);
            glPixelStorei(GL_PACK_ALIGNMENT, 1);
            glReadPixels(0, 0, fbw, fbh, GL_RGBA, GL_UNSIGNED_BYTE, grabbed.bits());
            const GLenum readbackError = glGetError();
            if ( readbackError != GL_NO_ERROR )
            {
                m_captureReadbackSucceeded = false;
                m_captureReadbackError = QStringLiteral(
                    "glReadPixels failed for GPU window framebuffer readback with GL error 0x%1")
                    .arg(static_cast<unsigned int>(readbackError), 0, 16);
            }
            else
            {
                // GL reads bottom-up; QImage rows are top-down.
                m_captureReadbackImage = grabbed.flipped(Qt::Vertical);
                m_captureReadbackSucceeded = true;
                m_captureReadbackError.clear();
            }
        }
    }

    // Diagnostic (env-gated MLVAPP_WINDOW_READBACK_DIR): read back the ACTUAL rendered
    // framebuffer so the on-screen output can be compared against the clean input
    // displayImage. Reveals upload/shader corruption (interleaving/banding) -- but NOT
    // DWM alpha compositing, which happens after this readback. Capped; inert by default.
    static const QByteArray rbDir = qgetenv("MLVAPP_WINDOW_READBACK_DIR");
    if ( !rbDir.isEmpty() )
    {
        static int rbCounter = 0;
        if ( rbCounter < 24 && fbw > 0 && fbh > 0 )
        {
            QImage out(fbw, fbh, QImage::Format_RGBA8888);
            glReadPixels(0, 0, fbw, fbh, GL_RGBA, GL_UNSIGNED_BYTE, out.bits());
            static const bool mk = QDir().mkpath(QString::fromLocal8Bit(rbDir));
            Q_UNUSED(mk);
            const QString p = QString::fromLocal8Bit(rbDir)
                + QStringLiteral("/rb_%1.png").arg(rbCounter, 5, 10, QChar('0'));
            out.mirrored(false, true).save(p, "PNG");   // GL bottom-up -> top-down
            ++rbCounter;
        }
    }
}
