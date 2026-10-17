#include "GpuDebayer.h"

#include "GpuPreviewProcessing.h"

#include <QByteArray>
#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QGuiApplication>
#include <QLibrary>
#include <QMutex>
#include <QOffscreenSurface>
#include <QOpenGLContext>
#include <QOpenGLFramebufferObject>
#include <QOpenGLFramebufferObjectFormat>
#include <QOpenGLFunctions>
#include <QOpenGLShaderProgram>
#include <QOpenGLTexture>
#include <QSurfaceFormat>
#include <QVector2D>

#include "../../tools/gpu/igpu_amaze_debayer.h"
#include "../../tools/gpu/igpu_cuda_error.h"
#include "GpuFieldTelemetry.h"
#include "PlaybackGpuClockKickPolicy.h"
#include "../../src/mlv/llrawproc/llrawproc.h"

#include <atomic>
#include <chrono>
#include <cstring>

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace
{
constexpr GLfloat kQuadVertices[16] = {
    -1.0f,  1.0f, 0.0f, 1.0f,
     1.0f,  1.0f, 1.0f, 1.0f,
    -1.0f, -1.0f, 0.0f, 0.0f,
     1.0f, -1.0f, 1.0f, 0.0f,
};

/* GPU-DEVICE-LOSS-SELF-HEAL-1: every CUDA AMaZE entry fails with this before
 * any runtime or backend call while llrpGpuCudaSuppressed() is set. */
QString cudaSuppressedReason()
{
    return QStringLiteral("CUDA suppressed after a GPU device loss");
}

bool envFlagEnabled(const QByteArray & value)
{
    if ( value.isEmpty() ) return false;

    const QByteArray normalized = value.trimmed().toLower();
    return normalized == "1"
        || normalized == "true"
        || normalized == "yes"
        || normalized == "on";
}

QSurfaceFormat debayerSurfaceFormat()
{
    QSurfaceFormat format;
    format.setRenderableType(QSurfaceFormat::OpenGL);
    format.setVersion(2, 0);
    format.setProfile(QSurfaceFormat::NoProfile);
    return format;
}

QString rendererDescriptionFromFunctions(QOpenGLFunctions * functions)
{
    if ( !functions ) return QStringLiteral("unknown");
    const GLubyte * renderer = functions->glGetString(GL_RENDERER);
    if ( !renderer ) return QStringLiteral("unknown");
    return QString::fromLatin1(reinterpret_cast<const char *>(renderer));
}

QOpenGLTexture * createRawTexture(int width, int height)
{
    QOpenGLTexture * texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
    texture->setFormat(QOpenGLTexture::RGBA16_UNorm);
    texture->setSize(width, height);
    texture->setMipLevels(1);
    texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16);
    texture->setWrapMode(QOpenGLTexture::ClampToEdge);
    texture->setMinMagFilters(QOpenGLTexture::Nearest, QOpenGLTexture::Nearest);
    return texture;
}

QByteArray packRawFrameTexture(const float * inputRawFrame, int pixelCount)
{
    QByteArray packed(static_cast<int>(pixelCount * 4u * sizeof(uint16_t)), Qt::Uninitialized);
    uint16_t * packedValues = reinterpret_cast<uint16_t *>(packed.data());
    for (int pixelIndex = 0; pixelIndex < pixelCount; ++pixelIndex)
    {
        const int clamped = qBound(0,
                                   static_cast<int>(inputRawFrame[pixelIndex] + 0.5f),
                                   65535);
        packedValues[pixelIndex * 4 + 0] = static_cast<uint16_t>(clamped);
        packedValues[pixelIndex * 4 + 1] = 0;
        packedValues[pixelIndex * 4 + 2] = 0;
        packedValues[pixelIndex * 4 + 3] = 65535;
    }
    return packed;
}

void unpackRgb16Readback(const QByteArray & readbackRgba16,
                         uint16_t * outputRgb16,
                         int width,
                         int height)
{
    const uint16_t * pixels = reinterpret_cast<const uint16_t *>(readbackRgba16.constData());
    for (int y = 0; y < height; ++y)
    {
        const int sourceY = height - 1 - y;
        for (int x = 0; x < width; ++x)
        {
            const int sourceIndex = (sourceY * width + x) * 4;
            const int destIndex = (y * width + x) * 3;
            outputRgb16[destIndex + 0] = pixels[sourceIndex + 0];
            outputRgb16[destIndex + 1] = pixels[sourceIndex + 1];
            outputRgb16[destIndex + 2] = pixels[sourceIndex + 2];
        }
    }
}

QByteArray gpuBilinearDebayerFragmentShaderSource(void)
{
    return QByteArrayLiteral(
        "uniform sampler2D rawTexture;\n"
        "uniform vec2 textureSize;\n"
        "varying vec2 vTexCoord;\n"
        "float sampleRaw(vec2 logicalPixel)\n"
        "{\n"
        "    vec2 clamped = clamp(logicalPixel, vec2(0.0), textureSize - vec2(1.0));\n"
        "    vec2 uv = vec2(clamped.x + 0.5, clamped.y + 0.5) / textureSize;\n"
        "    return texture2D(rawTexture, uv).r;\n"
        "}\n"
        "vec3 debayerBilinear(vec2 outputPixel)\n"
        "{\n"
        "    vec2 corePixel = clamp(outputPixel, vec2(1.0), textureSize - vec2(2.0));\n"
        "    float xParity = mod(corePixel.x, 2.0);\n"
        "    float yParity = mod(corePixel.y, 2.0);\n"
        "    float raw = sampleRaw(corePixel);\n"
        "    float up = sampleRaw(corePixel + vec2(0.0, -1.0));\n"
        "    float down = sampleRaw(corePixel + vec2(0.0, 1.0));\n"
        "    float left = sampleRaw(corePixel + vec2(-1.0, 0.0));\n"
        "    float right = sampleRaw(corePixel + vec2(1.0, 0.0));\n"
        "    float upLeft = sampleRaw(corePixel + vec2(-1.0, -1.0));\n"
        "    float upRight = sampleRaw(corePixel + vec2(1.0, -1.0));\n"
        "    float downLeft = sampleRaw(corePixel + vec2(-1.0, 1.0));\n"
        "    float downRight = sampleRaw(corePixel + vec2(1.0, 1.0));\n"
        "    if (xParity < 0.5 && yParity < 0.5)\n"
        "    {\n"
        "        return vec3(raw,\n"
        "                    (up + down + left + right) * 0.25,\n"
        "                    (upLeft + upRight + downLeft + downRight) * 0.25);\n"
        "    }\n"
        "    if (xParity > 0.5 && yParity > 0.5)\n"
        "    {\n"
        "        return vec3((upLeft + upRight + downLeft + downRight) * 0.25,\n"
        "                    (up + down + left + right) * 0.25,\n"
        "                    raw);\n"
        "    }\n"
        "    if (xParity > 0.5)\n"
        "    {\n"
        "        return vec3((left + right) * 0.5,\n"
        "                    raw,\n"
        "                    (up + down) * 0.5);\n"
        "    }\n"
        "    return vec3((up + down) * 0.5,\n"
        "                raw,\n"
        "                (left + right) * 0.5);\n"
        "}\n"
        "void main()\n"
        "{\n"
        "    vec2 outputPixel = floor(vec2(vTexCoord.x * textureSize.x,\n"
        "                                  (1.0 - vTexCoord.y) * textureSize.y));\n"
        "    gl_FragColor = vec4(debayerBilinear(outputPixel), 1.0);\n"
        "}\n");
}

bool buildBilinearDebayerProgram(QOpenGLShaderProgram * program, QString * reason)
{
    if ( !program ) return false;

    const QByteArray vertexShader = gpuPreviewProcessingVertexShaderSource();
    const QByteArray fragmentShader = gpuBilinearDebayerFragmentShaderSource();
    if ( !program->addShaderFromSourceCode(QOpenGLShader::Vertex, vertexShader)
      || !program->addShaderFromSourceCode(QOpenGLShader::Fragment, fragmentShader)
      || !program->link() )
    {
        if ( reason )
        {
            *reason = QStringLiteral("QOpenGLShaderProgram bilinear debayer setup failed: %1")
                .arg(program->log());
        }
        return false;
    }
    return true;
}

bool makeDebayerContextCurrent(QOffscreenSurface * surface,
                               QOpenGLContext * context,
                               QOpenGLFunctions ** functions,
                               QString * reason,
                               QString * rendererDescription)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        if ( rendererDescription && rendererDescription->isEmpty() )
        {
            *rendererDescription = QStringLiteral("unknown");
        }
        return false;
    };

    if ( !qobject_cast<QGuiApplication *>(QCoreApplication::instance()) )
    {
        return fail(QStringLiteral("QOffscreenSurface requires a QGuiApplication instance"));
    }

    if ( !surface || !context ) return fail(QStringLiteral("QOffscreenSurface setup received null objects"));

    const QSurfaceFormat format = debayerSurfaceFormat();
    surface->setFormat(format);
    surface->create();
    if ( !surface->isValid() )
    {
        return fail(QStringLiteral("QOffscreenSurface creation failed"));
    }

    context->setFormat(surface->requestedFormat());
    if ( !context->create() )
    {
        return fail(QStringLiteral("QOpenGLContext creation failed"));
    }

    if ( !context->makeCurrent(surface) )
    {
        return fail(QStringLiteral("QOffscreenSurface makeCurrent failed"));
    }

    QOpenGLFunctions * glFunctions = context->functions();
    if ( !glFunctions )
    {
        context->doneCurrent();
        return fail(QStringLiteral("QOpenGLContext did not expose QOpenGLFunctions"));
    }

    const QString renderer = rendererDescriptionFromFunctions(glFunctions);
    if ( rendererDescription ) *rendererDescription = renderer;
    if ( functions ) *functions = glFunctions;

    if ( gpuPreviewProcessingRendererIsSoftware(renderer) )
    {
        context->doneCurrent();
        return fail(QStringLiteral("software rasterizer renderer: %1").arg(renderer));
    }

    return true;
}

QString gpuAmazeDebayerUnavailableReason(void)
{
    return QStringLiteral(
        "GPU AMaZE debayer backend DLL is unavailable; set "
        "MLVAPP_GPU_AMAZE_DEBAYER_DLL or deploy igpu_amaze_debayer_cuda.dll; "
        "do not route AMaZE to bilinear or CPU fallback under the GPU backend");
}

const char * gpuAmazeDebayerDllEnvironmentVariableName(void)
{
    return "MLVAPP_GPU_AMAZE_DEBAYER_DLL";
}

QString gpuAmazeDebayerDllPath(void)
{
    const QByteArray configured = qgetenv(gpuAmazeDebayerDllEnvironmentVariableName()).trimmed();
    if ( !configured.isEmpty() )
    {
        return QString::fromLocal8Bit(configured);
    }

    const QString appDir = QCoreApplication::applicationDirPath();
    if ( !appDir.isEmpty() )
    {
        const QString deployed =
            QDir(appDir).filePath(QStringLiteral("igpu_amaze_debayer_cuda.dll"));
        if ( QFileInfo::exists(deployed) )
        {
            return deployed;
        }
    }

    return QStringLiteral("igpu_amaze_debayer_cuda.dll");
}

typedef igpu_amaze_debayer_backend * (*GpuAmazeCreateFn)(const char *);
typedef void (*GpuAmazeDestroyFn)(igpu_amaze_debayer_backend *);
typedef int (*GpuAmazeAbiVersionFn)(igpu_amaze_debayer_backend *);
typedef const char * (*GpuAmazeDescribeFn)(igpu_amaze_debayer_backend *);
typedef int (*GpuAmazeRunFn)(igpu_amaze_debayer_backend *,
                             const float *,
                             uint16_t *,
                             int,
                             int);
typedef int (*GpuAmazeRunPostWbGlTextureFn)(igpu_amaze_debayer_backend *,
                                            const float *,
                                            unsigned int,
                                            int,
                                            int,
                                            int,
                                            double,
                                            double,
                                            double);
typedef int (*GpuAmazeRunPostWbGlTextureFromR16GlTextureFn)(
    igpu_amaze_debayer_backend *,
    unsigned int,
    unsigned int,
    int,
    int,
    int,
    double,
    double,
    double);
typedef int (*GpuAmazeRunPostWbGlTextureFromDeviceBayer16Fn)(
    igpu_amaze_debayer_backend *,
    const uint16_t *,
    unsigned int,
    int,
    int,
    int,
    double,
    double,
    double);
typedef int (*GpuAmazeResetLiveGlTextureResourcesFn)(igpu_amaze_debayer_backend *);
typedef int (*GpuAmazeSetReducedHnyquistFn)(igpu_amaze_debayer_backend *, int);
typedef int (*GpuAmazeLastTimingFn)(igpu_amaze_debayer_backend *,
                                    igpu_amaze_debayer_timing_t *);
typedef int (*GpuAmazeLastCudaErrorFn)(igpu_cuda_error_t *);
// PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Rows 0 and K (optional symbols)
typedef int (*GpuAmazeArmProbeFn)(igpu_amaze_debayer_backend *, int);
typedef int (*GpuAmazeLastProbeFn)(igpu_amaze_debayer_backend *, igpu_amaze_probe_t *);
typedef int (*GpuAmazeClockKickBeginFn)(igpu_amaze_debayer_backend *, int);
typedef int (*GpuAmazeClockKickEndFn)(igpu_amaze_debayer_backend *, igpu_clock_kick_report_t *);

struct GpuAmazeDebayerRuntime
{
    QLibrary library;
    GpuAmazeCreateFn create = nullptr;
    GpuAmazeDestroyFn destroy = nullptr;
    GpuAmazeAbiVersionFn abiVersion = nullptr;
    GpuAmazeDescribeFn describe = nullptr;
    GpuAmazeRunFn run = nullptr;
    GpuAmazeRunPostWbGlTextureFn runPostWbGlTexture = nullptr;
    GpuAmazeRunPostWbGlTextureFromR16GlTextureFn runPostWbGlTextureFromR16GlTexture = nullptr;
    GpuAmazeRunPostWbGlTextureFromDeviceBayer16Fn runPostWbGlTextureFromDeviceBayer16 = nullptr;
    GpuAmazeResetLiveGlTextureResourcesFn resetLiveGlTextureResources = nullptr;
    GpuAmazeSetReducedHnyquistFn setReducedHnyquist = nullptr;
    GpuAmazeLastTimingFn lastTiming = nullptr;
    /* GPU-FIELD-TELEMETRY-1: optional; older DLLs omit them (r6: drain and the
     * pending words; r4: last only). */
    GpuAmazeCudaErrorSource cudaErrors;
    /* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1: optional (live runtime only); older DLLs omit them, and the
     * probe fields then print NA and the kick skips. */
    GpuAmazeArmProbeFn armProbe = nullptr;
    GpuAmazeLastProbeFn lastProbe = nullptr;
    GpuAmazeClockKickBeginFn clockKickBegin = nullptr;
    GpuAmazeClockKickEndFn clockKickEnd = nullptr;
};

typedef int (*GpuAmazeDrainCudaErrorsFn)(int, igpu_cuda_error_t *, int, unsigned long long *);
typedef const volatile long * (*GpuAmazeCudaErrorPendingWordFn)(int);

/* r6: the optional error exports of a loaded AMaZE DLL. pending is set only
 * with drain, so a non-zero word always has a drain behind it. */
GpuAmazeCudaErrorSource resolveAmazeCudaErrorSource(GpuAmazeLastCudaErrorFn last,
                                                    GpuAmazeDrainCudaErrorsFn drain,
                                                    GpuAmazeCudaErrorPendingWordFn pendingWord)
{
    GpuAmazeCudaErrorSource source;
    source.last = last;
    if ( drain && pendingWord )
    {
        source.drain = drain;
        source.pending[IGPU_CUDA_ERROR_CHANNEL_RENDER] = pendingWord(IGPU_CUDA_ERROR_CHANNEL_RENDER);
        source.pending[IGPU_CUDA_ERROR_CHANNEL_PREUPLOAD] = pendingWord(IGPU_CUDA_ERROR_CHANNEL_PREUPLOAD);
    }
    return source;
}

std::atomic<GpuAmazeStageTelemetrySink> g_amazeStageTelemetrySink{nullptr};
std::atomic<const GpuAmazeCudaErrorSource *> g_amazeCudaErrorSourceForTesting{nullptr};

/* r6: one drain of `channel` (failure / non-empty path only). Returns n. */
int drainAmazeCudaErrors(const GpuAmazeCudaErrorSource & source, int channel,
                         igpu_cuda_error_t (&notes)[IGPU_CUDA_ERROR_PENDING_MAX],
                         unsigned long long * dropped)
{
    *dropped = 0;
    if ( !source.drain ) return 0;
    int n = source.drain(channel, notes, IGPU_CUDA_ERROR_PENDING_MAX, dropped);
    if ( n < 0 ) n = 0;
    if ( n > IGPU_CUDA_ERROR_PENDING_MAX ) n = IGPU_CUDA_ERROR_PENDING_MAX;
    for ( int i = 0; i < n; ++i )
    {
        notes[i].name[sizeof(notes[i].name) - 1] = '\0';
        notes[i].site[sizeof(notes[i].site) - 1] = '\0';
    }
    return n;
}

template <typename FunctionPointer>
bool resolveAmazeDebayerSymbol(QLibrary * library,
                               const char * symbol,
                               FunctionPointer * out,
                               QString * reason)
{
    *out = reinterpret_cast<FunctionPointer>(library->resolve(symbol));
    if ( *out ) return true;
    if ( reason )
    {
        *reason = QStringLiteral("GPU AMaZE debayer backend missing ABI symbol %1")
            .arg(QString::fromLatin1(symbol));
    }
    return false;
}

bool loadAmazeDebayerRuntime(GpuAmazeDebayerRuntime * runtime,
                             QString * reason)
{
    if ( !runtime )
    {
        if ( reason ) *reason = QStringLiteral("GPU AMaZE debayer runtime loader received null storage");
        return false;
    }

    const QString dllPath = gpuAmazeDebayerDllPath();
    runtime->library.setFileName(dllPath);
    if ( !runtime->library.load() )
    {
        if ( reason )
        {
            *reason = QStringLiteral("GPU AMaZE debayer backend DLL load failed (%1): %2")
                .arg(dllPath, runtime->library.errorString());
        }
        return false;
    }

    runtime->cudaErrors = resolveAmazeCudaErrorSource(
        reinterpret_cast<GpuAmazeLastCudaErrorFn>(
            runtime->library.resolve("igpu_amaze_debayer_last_cuda_error")),
        reinterpret_cast<GpuAmazeDrainCudaErrorsFn>(
            runtime->library.resolve("igpu_amaze_debayer_drain_cuda_errors")),
        reinterpret_cast<GpuAmazeCudaErrorPendingWordFn>(
            runtime->library.resolve("igpu_amaze_debayer_cuda_error_pending_word")));
    return resolveAmazeDebayerSymbol(&runtime->library,
                                     "igpu_amaze_debayer_create",
                                     &runtime->create,
                                     reason)
        && resolveAmazeDebayerSymbol(&runtime->library,
                                     "igpu_amaze_debayer_destroy",
                                     &runtime->destroy,
                                     reason)
        && resolveAmazeDebayerSymbol(&runtime->library,
                                     "igpu_amaze_debayer_abi_version",
                                     &runtime->abiVersion,
                                     reason)
        && resolveAmazeDebayerSymbol(&runtime->library,
                                     "igpu_amaze_debayer_describe",
                                     &runtime->describe,
                                     reason)
        && resolveAmazeDebayerSymbol(&runtime->library,
                                     "igpu_amaze_debayer_run",
                                     &runtime->run,
                                     reason)
        && resolveAmazeDebayerSymbol(&runtime->library,
                                     "igpu_amaze_debayer_last_timing",
                                     &runtime->lastTiming,
                                     reason);
}

QString describeAmazeBackend(GpuAmazeDebayerRuntime * runtime,
                             igpu_amaze_debayer_backend * backend)
{
    if ( !runtime || !runtime->describe || !backend ) return QStringLiteral("unknown");
    const char * description = runtime->describe(backend);
    return description && *description
        ? QString::fromUtf8(description)
        : QStringLiteral("unknown");
}

bool validWbMultipliers(const double wbMultipliers[3])
{
    return wbMultipliers
        && wbMultipliers[0] > 0.0
        && wbMultipliers[1] > 0.0
        && wbMultipliers[2] > 0.0;
}

uint16_t clampPostWbChannel(double value)
{
    if ( value <= 0.0 ) return 0;
    if ( value >= 65535.0 ) return 65535;
    return static_cast<uint16_t>(value);
}

void applyPostWbUndoToRgb16(uint16_t * rgb16,
                            int width,
                            int height,
                            int blackLevel,
                            const double wbMultipliers[3])
{
    if ( blackLevel < 1000 ) blackLevel = -1000;
    const size_t pixelCount = static_cast<size_t>(width) * static_cast<size_t>(height);
    for ( size_t i = 0; i < pixelCount; ++i )
    {
        uint16_t * pixel = rgb16 + i * 3u;
        pixel[0] = clampPostWbChannel(
            static_cast<double>(pixel[0]) / wbMultipliers[0] + static_cast<double>(blackLevel));
        pixel[1] = clampPostWbChannel(
            static_cast<double>(pixel[1]) / wbMultipliers[1] + static_cast<double>(blackLevel));
        pixel[2] = clampPostWbChannel(
            static_cast<double>(pixel[2]) / wbMultipliers[2] + static_cast<double>(blackLevel));
    }
}

void copyAmazeTiming(GpuAmazeDebayerRuntime * runtime,
                     igpu_amaze_debayer_backend * backend,
                     GpuAmazeDebayerBackendTiming * timing)
{
    if ( !timing ) return;

    igpu_amaze_debayer_timing_t backendTiming;
    if ( runtime
      && runtime->lastTiming
      && backend
      && runtime->lastTiming(backend, &backendTiming) == 0 )
    {
        timing->available = true;
        timing->uploadMs = backendTiming.upload_ms;
        timing->kernelMs = backendTiming.kernel_ms;
        timing->downloadMs = backendTiming.download_ms;
        timing->totalMs = backendTiming.total_ms;
    }
    else
    {
        *timing = GpuAmazeDebayerBackendTiming();
    }
}

/* r6: before an AMaZE run wrapper's backend call (same lock as the call). Codes
 * still pending were noted by a call nothing captured (create, destroy, the GL
 * reset, ...): they are emitted now as orphans with their own site, never as
 * this call's. While the channel is empty this is one relaxed load. Call sites
 * guard it with GpuFieldTelemetry::enabled(). */
void enterAmazeStageTelemetry(GpuAmazeDebayerRuntime * runtime)
{
    if ( !runtime || !runtime->cudaErrors.pending[IGPU_CUDA_ERROR_CHANNEL_RENDER] ) return;
    if ( __atomic_load_n(runtime->cudaErrors.pending[IGPU_CUDA_ERROR_CHANNEL_RENDER], __ATOMIC_RELAXED) == 0 )
        return;
    gpuAmazeCudaErrorsEmitOrphans(runtime->cudaErrors, 1, g_amazeStageTelemetrySink.load(std::memory_order_relaxed));
}

/* GPU-DEVICE-LOSS-SELF-HEAL-1 r6 (CoS #437, guard under the serializing lock):
 * the last step before every AMaZE backend run. The entry above can hand an
 * orphan device-loss code to the breaker on this thread, which suppresses CUDA,
 * and a loss on another thread can land while this caller waited on live.mutex:
 * the flag is read again after both. false = the run must not submit. */
bool enterAmazeRunUnlessCudaSuppressed(GpuAmazeDebayerRuntime * runtime, bool telemetry)
{
    if ( telemetry ) enterAmazeStageTelemetry(runtime);
    return !llrpGpuCudaSuppressed();
}

/* GPU-FIELD-TELEMETRY-1: one stage record per AMaZE run wrapper call. Success
 * reads the backend's own per-frame timing (no CUDA call); failure (r6) emits
 * every code the call noted (gpuAmazeCudaErrorsEmitFailure). Call sites guard
 * it with GpuFieldTelemetry::enabled(). */
void noteAmazeStageTelemetry(GpuAmazeDebayerRuntime * runtime,
                             igpu_amaze_debayer_backend * backend,
                             const char * stage,
                             int rc)
{
    if ( !runtime || !backend ) return;
    const GpuAmazeStageTelemetrySink sink = g_amazeStageTelemetrySink.load(std::memory_order_relaxed);
    if ( !sink ) return;
    if ( rc == 0 )
    {
        igpu_amaze_debayer_timing_t backendTiming;
        if ( runtime->lastTiming && runtime->lastTiming(backend, &backendTiming) == 0 )
        {
            sink(stage, 0, backendTiming.total_ms, 0, nullptr, nullptr, nullptr, 0);
        }
        return;
    }
    gpuAmazeCudaErrorsEmitFailure(runtime->cudaErrors, stage, rc, sink);
}

struct GpuAmazeDebayerLiveTextureRuntime
{
    QMutex mutex;
    GpuAmazeDebayerRuntime runtime;
    igpu_amaze_debayer_backend *backend = nullptr;
    bool attempted = false;
    bool available = false;
    QString reason;
    QString rendererDescription;

    ~GpuAmazeDebayerLiveTextureRuntime()
    {
        if ( backend && runtime.destroy )
        {
            runtime.destroy(backend);
            backend = nullptr;
        }
    }

    bool ensure(QString *outReason = nullptr)
    {
        QMutexLocker locker(&mutex);
        if ( attempted )
        {
            if ( outReason ) *outReason = reason;
            return available;
        }

        /* r6: create('cuda') below is a CUDA call, so the flag is read again
         * under the lock; a suppressed session leaves the runtime unattempted. */
        if ( llrpGpuCudaSuppressed() )
        {
            if ( outReason ) *outReason = cudaSuppressedReason();
            return false;
        }

        attempted = true;
        QString loadReason;
        if ( !loadAmazeDebayerRuntime(&runtime, &loadReason) )
        {
            reason = loadReason.isEmpty() ? gpuAmazeDebayerUnavailableReason() : loadReason;
            if ( outReason ) *outReason = reason;
            return false;
        }

        if ( !resolveAmazeDebayerSymbol(&runtime.library,
                                        "igpu_amaze_debayer_run_post_wb_gl_texture_from_r16_gl_texture",
                                        &runtime.runPostWbGlTextureFromR16GlTexture,
                                        &reason) )
        {
            if ( outReason ) *outReason = reason;
            return false;
        }

        runtime.resetLiveGlTextureResources =
            reinterpret_cast<GpuAmazeResetLiveGlTextureResourcesFn>(
                runtime.library.resolve("igpu_amaze_debayer_reset_live_gl_texture_resources"));
        runtime.runPostWbGlTextureFromDeviceBayer16 =
            reinterpret_cast<GpuAmazeRunPostWbGlTextureFromDeviceBayer16Fn>(
                runtime.library.resolve("igpu_amaze_debayer_run_post_wb_gl_texture_from_device_bayer16"));
        /* PLAYBACK-CUDA-HONOUR-SCALE-1 r3: optional; without it a reduced texture
         * present is refused (gpuAmazeDebayerReducedHnyquistAvailable). */
        runtime.setReducedHnyquist =
            reinterpret_cast<GpuAmazeSetReducedHnyquistFn>(
                runtime.library.resolve("igpu_amaze_debayer_set_reduced_hnyquist"));
        /* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Rows 0 and K: optional; a missing symbol prints NA (probe)
         * or skips (kick), never fails a present. */
        runtime.armProbe =
            reinterpret_cast<GpuAmazeArmProbeFn>(runtime.library.resolve("igpu_amaze_debayer_arm_probe"));
        runtime.lastProbe =
            reinterpret_cast<GpuAmazeLastProbeFn>(runtime.library.resolve("igpu_amaze_debayer_last_probe"));
        runtime.clockKickBegin =
            reinterpret_cast<GpuAmazeClockKickBeginFn>(runtime.library.resolve("igpu_amaze_debayer_clock_kick_begin"));
        runtime.clockKickEnd =
            reinterpret_cast<GpuAmazeClockKickEndFn>(runtime.library.resolve("igpu_amaze_debayer_clock_kick_end"));

        backend = runtime.create("cuda");
        if ( !backend )
        {
            reason = QStringLiteral("GPU AMaZE debayer backend create('cuda') failed");
            if ( outReason ) *outReason = reason;
            return false;
        }

        const int abiVersion = runtime.abiVersion(backend);
        if ( abiVersion != IGPU_AMAZE_DEBAYER_ABI_VERSION )
        {
            reason = QStringLiteral("GPU AMaZE debayer backend ABI mismatch: got %1 expected %2")
                .arg(abiVersion)
                .arg(IGPU_AMAZE_DEBAYER_ABI_VERSION);
            runtime.destroy(backend);
            backend = nullptr;
            if ( outReason ) *outReason = reason;
            return false;
        }

        rendererDescription = describeAmazeBackend(&runtime, backend);
        reason.clear();
        available = true;
        if ( outReason ) outReason->clear();
        return true;
    }
};

GpuAmazeDebayerLiveTextureRuntime & liveAmazeTextureRuntime()
{
    static GpuAmazeDebayerLiveTextureRuntime runtime;
    return runtime;
}

std::atomic<quint64> g_reducedHnyquistFrames{0};
std::atomic<quint64> g_reducedHnyquistRefusals{0};

/* Sets the sticky flag immediately before a live run (caller holds live.mutex). A
 * reduced run on a DLL without the symbol is refused; 0 on such a DLL is a no-op,
 * since that DLL cannot filter. */
bool setLiveReducedHnyquist(GpuAmazeDebayerLiveTextureRuntime & live,
                            bool reducedHnyquist,
                            QString * why)
{
    if ( !live.runtime.setReducedHnyquist )
    {
        if ( reducedHnyquist && why ) *why = gpuAmazeDebayerReducedHnyquistMissingReason();
        return !reducedHnyquist;
    }
    if ( live.runtime.setReducedHnyquist(live.backend, reducedHnyquist ? 1 : 0) != 0 )
    {
        if ( why ) *why = QStringLiteral("GPU AMaZE set_reduced_hnyquist(%1) failed")
            .arg(reducedHnyquist ? 1 : 0);
        return false;
    }
    return true;
}
}

namespace
{
/* PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0: the armed-run counts (arming never needs the DLL) and the Play
 * session's record. All under live.mutex. A session (Play) arm outranks a paused one; a paused arm ends a session. */
int g_probeArmCalls = 0;
int g_probeSessionCallsLeft = 0;
GpuAmazeProbeSessionRecord g_probeSessionRecord;

struct LiveProbeArm
{
    bool armed = false;     // this run consumed an armed count
    bool session = false;   // ... a Play session's
};

/* Caller holds live.mutex, right before a live run's backend call. The backend is armed for exactly this run (or
 * disarmed), so a run that returns before its probe step never leaves a stale arm behind. */
LiveProbeArm takeLiveProbeArm(GpuAmazeDebayerLiveTextureRuntime & live)
{
    LiveProbeArm arm;
    if ( g_probeSessionCallsLeft > 0 )
    {
        --g_probeSessionCallsLeft;
        arm.armed = arm.session = true;
    }
    else if ( g_probeArmCalls > 0 )
    {
        --g_probeArmCalls;
        arm.armed = true;
    }
    if ( live.runtime.armProbe && live.runtime.lastProbe ) live.runtime.armProbe(live.backend, arm.armed ? 1 : 0);
    return arm;
}

/* Caller holds live.mutex, right after a successful armed run: its probe into *timing and the session record. */
void noteLiveProbeAfterRun(GpuAmazeDebayerLiveTextureRuntime & live,
                           const LiveProbeArm & arm,
                           double runStartSteadyMs,
                           GpuAmazeDebayerBackendTiming * timing)
{
    if ( !arm.armed ) return;
    igpu_amaze_probe_t probe;
    std::memset(&probe, 0, sizeof(probe));
    const bool probeAvailable = live.runtime.lastProbe != nullptr;
    if ( probeAvailable && live.runtime.lastProbe(live.backend, &probe) != 0 ) probe.valid = 0;
    if ( timing )
    {
        timing->probeAvailable = probeAvailable;
        timing->probeValid = probe.valid != 0;
        timing->smMhzBefore = probe.sm_mhz_before;
        timing->smMhzAfter = probe.sm_mhz_after;
        timing->deviceMs = probe.device_ms;
    }
    if ( !arm.session || g_probeSessionRecord.calls >= GpuAmazeProbeSessionRecord::kMaxCalls ) return;
    igpu_amaze_debayer_timing_t backendTiming;
    std::memset(&backendTiming, 0, sizeof(backendTiming));
    if ( live.runtime.lastTiming ) live.runtime.lastTiming(live.backend, &backendTiming);
    const int i = g_probeSessionRecord.calls++;
    g_probeSessionRecord.probeAvailable = probeAvailable;
    g_probeSessionRecord.kernelMs[i] = backendTiming.kernel_ms;
    g_probeSessionRecord.deviceMs[i] = probe.device_ms;
    g_probeSessionRecord.smMhzBefore[i] = probe.sm_mhz_before;
    g_probeSessionRecord.smMhzAfter[i] = probe.sm_mhz_after;
    g_probeSessionRecord.valid[i] = probe.valid != 0;
    g_probeSessionRecord.startSteadyMs[i] = runStartSteadyMs;
}
} // namespace

QString gpuAmazeDebayerReducedHnyquistMissingReason(void)
{
    return QStringLiteral("AMaZE DLL lacks the reduced H-Nyquist filter");
}

bool gpuAmazeDebayerReducedHnyquistAvailable(void)
{
    if ( llrpGpuCudaSuppressed() ) return false;
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    if ( !live.ensure() ) return false;
    QMutexLocker locker(&live.mutex);
    return live.runtime.setReducedHnyquist != nullptr;
}

void gpuAmazeDebayerNoteReducedHnyquistFrame(void)
{
    g_reducedHnyquistFrames.fetch_add(1, std::memory_order_relaxed);
}

void gpuAmazeDebayerNoteReducedHnyquistRefusal(void)
{
    g_reducedHnyquistRefusals.fetch_add(1, std::memory_order_relaxed);
}

quint64 gpuAmazeDebayerReducedHnyquistFrames(void)
{
    return g_reducedHnyquistFrames.load(std::memory_order_relaxed);
}

quint64 gpuAmazeDebayerReducedHnyquistRefusals(void)
{
    return g_reducedHnyquistRefusals.load(std::memory_order_relaxed);
}

const char * gpuBilinearDebayerEnvironmentVariableName(void)
{
    return "MLVAPP_EXPERIMENTAL_GPU_DEBAYER";
}

bool gpuBilinearDebayerRequestedByEnvironment(void)
{
    return envFlagEnabled(qgetenv(gpuBilinearDebayerEnvironmentVariableName()));
}

const char * gpuAmazeDebayerEnvironmentVariableName(void)
{
    return "MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER";
}

bool gpuAmazeDebayerRequestedByEnvironment(void)
{
    return envFlagEnabled(qgetenv(gpuAmazeDebayerEnvironmentVariableName()));
}

const char * gpuAmazeTexturePresentEnvironmentVariableName(void)
{
    return "MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT";
}

bool gpuAmazeTexturePresentRequestedByEnvironment(void)
{
    return envFlagEnabled(qgetenv(gpuAmazeTexturePresentEnvironmentVariableName()));
}

GpuBilinearDebayerBackendAvailability gpuBilinearDebayerProbeBackend(void)
{
    GpuBilinearDebayerBackendAvailability availability;
    QOffscreenSurface surface;
    QOpenGLContext context;
    QOpenGLFunctions * glFunctions = nullptr;
    if ( !makeDebayerContextCurrent(&surface,
                                    &context,
                                    &glFunctions,
                                    &availability.reason,
                                    &availability.rendererDescription) )
    {
        return availability;
    }

    QOpenGLShaderProgram program;
    QString shaderReason;
    const bool shaderReady = buildBilinearDebayerProgram(&program, &shaderReason);
    context.doneCurrent();
    if ( !shaderReady )
    {
        availability.reason = shaderReason;
        return availability;
    }

    availability.available = true;
    availability.reason.clear();
    return availability;
}

bool gpuBilinearDebayerApplyGpuOffscreen(const float * inputRawFrame,
                                         uint16_t * outputRgb16,
                                         int width,
                                         int height,
                                         QString * reason,
                                         QString * rendererDescription)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        return false;
    };

    if ( !inputRawFrame || !outputRgb16 || width <= 2 || height <= 2 )
    {
        return fail(QStringLiteral("GPU bilinear debayer input/output buffers are invalid"));
    }

    QOffscreenSurface surface;
    QOpenGLContext context;
    QOpenGLFunctions * glFunctions = nullptr;
    if ( !makeDebayerContextCurrent(&surface,
                                    &context,
                                    &glFunctions,
                                    reason,
                                    rendererDescription) )
    {
        return false;
    }

    QOpenGLFramebufferObjectFormat fboFormat;
    fboFormat.setAttachment(QOpenGLFramebufferObject::NoAttachment);
    fboFormat.setTextureTarget(GL_TEXTURE_2D);
    fboFormat.setInternalTextureFormat(GL_RGBA16);
    QOpenGLFramebufferObject fbo(width, height, fboFormat);
    if ( !fbo.isValid() )
    {
        context.doneCurrent();
        return fail(QStringLiteral("QOpenGLFramebufferObject creation failed"));
    }

    QOpenGLShaderProgram program;
    if ( !buildBilinearDebayerProgram(&program, reason) )
    {
        context.doneCurrent();
        return false;
    }

    const QByteArray packedFrame = packRawFrameTexture(inputRawFrame, width * height);
    QOpenGLTexture * rawTexture = createRawTexture(width, height);
    rawTexture->setData(QOpenGLTexture::RGBA,
                        QOpenGLTexture::UInt16,
                        packedFrame.constData());

    fbo.bind();
    glFunctions->glViewport(0, 0, width, height);
    glFunctions->glDisable(GL_DEPTH_TEST);
    glFunctions->glDisable(GL_BLEND);
    glFunctions->glClearColor(0.0f, 0.0f, 0.0f, 1.0f);
    glFunctions->glClear(GL_COLOR_BUFFER_BIT);

    program.bind();
    program.setUniformValue("rawTexture", 0);
    program.setUniformValue("textureSize",
                            QVector2D(static_cast<float>(width),
                                      static_cast<float>(height)));

    rawTexture->bind(0);

    const int posLoc = program.attributeLocation("position");
    const int texLoc = program.attributeLocation("texCoord");
    program.enableAttributeArray(posLoc);
    program.enableAttributeArray(texLoc);
    program.setAttributeArray(posLoc, GL_FLOAT, kQuadVertices, 2, 4 * sizeof(GLfloat));
    program.setAttributeArray(texLoc, GL_FLOAT, kQuadVertices + 2, 2, 4 * sizeof(GLfloat));
    glFunctions->glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);
    glFunctions->glFinish();

    QByteArray readback(static_cast<int>(width * height * 4u * sizeof(uint16_t)), Qt::Uninitialized);
    glFunctions->glReadPixels(0,
                              0,
                              width,
                              height,
                              GL_RGBA,
                              GL_UNSIGNED_SHORT,
                              readback.data());
    unpackRgb16Readback(readback, outputRgb16, width, height);

    program.disableAttributeArray(posLoc);
    program.disableAttributeArray(texLoc);
    rawTexture->release();
    program.release();
    fbo.release();
    delete rawTexture;
    context.doneCurrent();

    if ( reason ) reason->clear();
    return true;
}

GpuAmazeDebayerBackendAvailability gpuAmazeDebayerProbeBackend(void)
{
    GpuAmazeDebayerBackendAvailability availability;
    if ( llrpGpuCudaSuppressed() )
    {
        availability.reason = cudaSuppressedReason();
        return availability;
    }

    GpuAmazeDebayerRuntime runtime;
    if ( !loadAmazeDebayerRuntime(&runtime, &availability.reason) )
    {
        if ( availability.reason.isEmpty() )
        {
            availability.reason = gpuAmazeDebayerUnavailableReason();
        }
        return availability;
    }

    igpu_amaze_debayer_backend * backend = runtime.create("cuda");
    if ( !backend )
    {
        availability.reason = QStringLiteral("GPU AMaZE debayer backend create('cuda') failed");
        return availability;
    }

    const int abiVersion = runtime.abiVersion(backend);
    if ( abiVersion != IGPU_AMAZE_DEBAYER_ABI_VERSION )
    {
        availability.reason =
            QStringLiteral("GPU AMaZE debayer backend ABI mismatch: got %1 expected %2")
                .arg(abiVersion)
                .arg(IGPU_AMAZE_DEBAYER_ABI_VERSION);
        runtime.destroy(backend);
        return availability;
    }

    availability.rendererDescription = describeAmazeBackend(&runtime, backend);
    runtime.destroy(backend);
    availability.available = true;
    availability.reason.clear();
    return availability;
}

bool gpuAmazeDebayerApplyGpuOffscreen(const float * inputRawFrame,
                                      uint16_t * outputRgb16,
                                      int width,
                                      int height,
                                      QString * reason,
                                      QString * rendererDescription,
                                      GpuAmazeDebayerBackendTiming * timing)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        if ( rendererDescription ) rendererDescription->clear();
        if ( timing ) *timing = GpuAmazeDebayerBackendTiming();
        return false;
    };

    if ( llrpGpuCudaSuppressed() ) return fail(cudaSuppressedReason());
    if ( !inputRawFrame || !outputRgb16 || width <= 32 || height <= 32 )
    {
        return fail(QStringLiteral("GPU AMaZE debayer input/output buffers are invalid"));
    }

    GpuAmazeDebayerRuntime runtime;
    QString loadReason;
    if ( !loadAmazeDebayerRuntime(&runtime, &loadReason) )
    {
        return fail(loadReason.isEmpty() ? gpuAmazeDebayerUnavailableReason() : loadReason);
    }

    igpu_amaze_debayer_backend * backend = runtime.create("cuda");
    if ( !backend )
    {
        return fail(QStringLiteral("GPU AMaZE debayer backend create('cuda') failed"));
    }

    const int abiVersion = runtime.abiVersion(backend);
    if ( abiVersion != IGPU_AMAZE_DEBAYER_ABI_VERSION )
    {
        const QString mismatch =
            QStringLiteral("GPU AMaZE debayer backend ABI mismatch: got %1 expected %2")
                .arg(abiVersion)
                .arg(IGPU_AMAZE_DEBAYER_ABI_VERSION);
        runtime.destroy(backend);
        return fail(mismatch);
    }

    if ( !enterAmazeRunUnlessCudaSuppressed(&runtime, GpuFieldTelemetry::enabled()) )
    {
        runtime.destroy(backend);
        return fail(cudaSuppressedReason());
    }
    const int rc = runtime.run(backend, inputRawFrame, outputRgb16, width, height);
    if ( GpuFieldTelemetry::enabled() ) noteAmazeStageTelemetry(&runtime, backend, "amaze:offscreen", rc);
    if ( rc != 0 )
    {
        const QString backendDescription = describeAmazeBackend(&runtime, backend);
        runtime.destroy(backend);
        return fail(QStringLiteral("GPU AMaZE debayer backend run failed rc=%1 renderer=%2")
            .arg(rc)
            .arg(backendDescription));
    }

    if ( rendererDescription )
    {
        *rendererDescription = describeAmazeBackend(&runtime, backend);
    }
    if ( timing )
    {
        copyAmazeTiming(&runtime, backend, timing);
    }
    runtime.destroy(backend);
    if ( reason ) reason->clear();
    return true;
}

bool gpuAmazeDebayerRenderPostWbGlTexture(const float * inputRawFrame,
                                          unsigned int glTexture,
                                          int width,
                                          int height,
                                          int blackLevel,
                                          const double wbMultipliers[3],
                                          QString * reason,
                                          QString * rendererDescription,
                                          GpuAmazeDebayerBackendTiming * timing)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        if ( rendererDescription ) rendererDescription->clear();
        if ( timing ) *timing = GpuAmazeDebayerBackendTiming();
        return false;
    };

    if ( llrpGpuCudaSuppressed() ) return fail(cudaSuppressedReason());
    if ( !inputRawFrame || glTexture == 0 || width <= 32 || height <= 32 )
    {
        return fail(QStringLiteral("GPU AMaZE texture-present input or GL texture is invalid"));
    }
    if ( !validWbMultipliers(wbMultipliers) )
    {
        return fail(QStringLiteral("GPU AMaZE texture-present WB multipliers are invalid"));
    }

    GpuAmazeDebayerRuntime runtime;
    QString loadReason;
    if ( !loadAmazeDebayerRuntime(&runtime, &loadReason) )
    {
        return fail(loadReason.isEmpty() ? gpuAmazeDebayerUnavailableReason() : loadReason);
    }
    if ( !resolveAmazeDebayerSymbol(&runtime.library,
                                    "igpu_amaze_debayer_run_post_wb_gl_texture",
                                    &runtime.runPostWbGlTexture,
                                    reason) )
    {
        return fail(reason ? *reason : QStringLiteral("GPU AMaZE post-WB GL texture symbol is unavailable"));
    }

    igpu_amaze_debayer_backend * backend = runtime.create("cuda");
    if ( !backend )
    {
        return fail(QStringLiteral("GPU AMaZE debayer backend create('cuda') failed"));
    }

    const int abiVersion = runtime.abiVersion(backend);
    if ( abiVersion != IGPU_AMAZE_DEBAYER_ABI_VERSION )
    {
        const QString mismatch =
            QStringLiteral("GPU AMaZE debayer backend ABI mismatch: got %1 expected %2")
                .arg(abiVersion)
                .arg(IGPU_AMAZE_DEBAYER_ABI_VERSION);
        runtime.destroy(backend);
        return fail(mismatch);
    }

    if ( !enterAmazeRunUnlessCudaSuppressed(&runtime, GpuFieldTelemetry::enabled()) )
    {
        runtime.destroy(backend);
        return fail(cudaSuppressedReason());
    }
    const int rc = runtime.runPostWbGlTexture(backend,
                                             inputRawFrame,
                                             glTexture,
                                             width,
                                             height,
                                             blackLevel,
                                             wbMultipliers[0],
                                             wbMultipliers[1],
                                             wbMultipliers[2]);
    if ( GpuFieldTelemetry::enabled() ) noteAmazeStageTelemetry(&runtime, backend, "amaze:post_wb_gl", rc);
    const QString backendDescription = describeAmazeBackend(&runtime, backend);
    if ( rc != 0 )
    {
        runtime.destroy(backend);
        return fail(QStringLiteral("GPU AMaZE post-WB GL texture run failed rc=%1 renderer=%2")
            .arg(rc)
            .arg(backendDescription));
    }

    if ( rendererDescription ) *rendererDescription = backendDescription;
    copyAmazeTiming(&runtime, backend, timing);
    runtime.destroy(backend);
    if ( reason ) reason->clear();
    return true;
}

GpuAmazeDebayerBackendAvailability gpuAmazeDebayerProbeR16TextureBackend(void)
{
    GpuAmazeDebayerBackendAvailability availability;
    if ( llrpGpuCudaSuppressed() )
    {
        availability.reason = cudaSuppressedReason();
        return availability;
    }
    QString reason;
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    if ( !live.ensure(&reason) )
    {
        availability.reason = reason;
        return availability;
    }

    QMutexLocker locker(&live.mutex);
    availability.available = live.available;
    availability.reason = live.reason;
    availability.rendererDescription = live.rendererDescription;
    return availability;
}

void gpuAmazeDebayerResetR16TextureBackendResources(void)
{
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    QMutexLocker locker(&live.mutex);
    if ( live.backend && live.runtime.resetLiveGlTextureResources )
    {
        live.runtime.resetLiveGlTextureResources(live.backend);
    }
    /* r6 terminal drain at GL release (teardown only): the reset is not a
     * captured call, and the view may never render again, so whatever the DLL
     * still holds (the reset's gl_cache:unregister, ...) is emitted now. */
    const GpuAmazeStageTelemetrySink sink = g_amazeStageTelemetrySink.load(std::memory_order_relaxed);
    if ( sink )
    {
        const GpuAmazeCudaErrorSource * forTesting = g_amazeCudaErrorSourceForTesting.load();
        gpuAmazeCudaErrorsEmitOrphans(forTesting ? *forTesting : live.runtime.cudaErrors, 2, sink);
    }
}

bool gpuAmazeDebayerRenderPostWbGlTextureFromR16GlTexture(
    unsigned int inputR16GlTexture,
    unsigned int outputRgba16GlTexture,
    int width,
    int height,
    int blackLevel,
    const double wbMultipliers[3],
    QString * reason,
    QString * rendererDescription,
    GpuAmazeDebayerBackendTiming * timing,
    bool reducedHnyquist)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        if ( rendererDescription ) rendererDescription->clear();
        if ( timing ) *timing = GpuAmazeDebayerBackendTiming();
        return false;
    };

    if ( llrpGpuCudaSuppressed() ) return fail(cudaSuppressedReason());
    if ( inputR16GlTexture == 0 || outputRgba16GlTexture == 0 || width <= 32 || height <= 32 )
    {
        return fail(QStringLiteral("GPU AMaZE R16 texture-present input/output GL texture is invalid"));
    }
    if ( !validWbMultipliers(wbMultipliers) )
    {
        return fail(QStringLiteral("GPU AMaZE R16 texture-present WB multipliers are invalid"));
    }

    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    QString ensureReason;
    if ( !live.ensure(&ensureReason) )
    {
        return fail(ensureReason.isEmpty()
            ? QStringLiteral("GPU AMaZE R16 texture-present backend is unavailable")
            : ensureReason);
    }

    QMutexLocker locker(&live.mutex);
    /* r6: read again under the lock: a caller queued here through a loss must not reach CUDA. */
    if ( llrpGpuCudaSuppressed() ) return fail(cudaSuppressedReason());
    QString hnyquistReason;
    if ( !setLiveReducedHnyquist(live, reducedHnyquist, &hnyquistReason) )
    {
        return fail(hnyquistReason);
    }
    if ( !enterAmazeRunUnlessCudaSuppressed(&live.runtime, GpuFieldTelemetry::enabled()) ) return fail(cudaSuppressedReason());
    const LiveProbeArm probeArm = takeLiveProbeArm(live);
    const double runStartSteadyMs = gpuAmazeDebayerSteadyNowMs();
    const int rc =
        live.runtime.runPostWbGlTextureFromR16GlTexture(
            live.backend,
            inputR16GlTexture,
            outputRgba16GlTexture,
            width,
            height,
            blackLevel,
            wbMultipliers[0],
            wbMultipliers[1],
            wbMultipliers[2]);
    if ( GpuFieldTelemetry::enabled() ) noteAmazeStageTelemetry(&live.runtime, live.backend, "amaze:r16_gl", rc);
    if ( rc != 0 )
    {
        return fail(QStringLiteral("GPU AMaZE R16 texture-present run failed rc=%1 renderer=%2")
            .arg(rc)
            .arg(live.rendererDescription.isEmpty()
                ? QStringLiteral("unknown")
                : live.rendererDescription));
    }

    if ( rendererDescription ) *rendererDescription = live.rendererDescription;
    copyAmazeTiming(&live.runtime, live.backend, timing);
    noteLiveProbeAfterRun(live, probeArm, runStartSteadyMs, timing);
    if ( reason ) reason->clear();
    return true;
}

bool gpuAmazeDebayerRenderPostWbGlTextureFromDeviceBayer16(
    const uint16_t *deviceBayer16,
    unsigned int outputRgba16GlTexture,
    int width,
    int height,
    int blackLevel,
    const double wbMultipliers[3],
    QString * reason,
    QString * rendererDescription,
    GpuAmazeDebayerBackendTiming * timing,
    bool reducedHnyquist)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        if ( rendererDescription ) rendererDescription->clear();
        if ( timing ) *timing = GpuAmazeDebayerBackendTiming();
        return false;
    };

    if ( llrpGpuCudaSuppressed() ) return fail(cudaSuppressedReason());
    if ( !deviceBayer16 || outputRgba16GlTexture == 0 || width <= 32 || height <= 32 )
    {
        return fail(QStringLiteral("GPU AMaZE direct device texture-present input/output is invalid"));
    }
    if ( !validWbMultipliers(wbMultipliers) )
    {
        return fail(QStringLiteral("GPU AMaZE direct device texture-present WB multipliers are invalid"));
    }

    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    QString ensureReason;
    if ( !live.ensure(&ensureReason) )
    {
        return fail(ensureReason.isEmpty()
            ? QStringLiteral("GPU AMaZE direct device texture-present backend is unavailable")
            : ensureReason);
    }

    QMutexLocker locker(&live.mutex);
    /* r6: read again under the lock: a caller queued here through a loss must not reach CUDA. */
    if ( llrpGpuCudaSuppressed() ) return fail(cudaSuppressedReason());
    if ( !live.runtime.runPostWbGlTextureFromDeviceBayer16 )
    {
        return fail(QStringLiteral("GPU AMaZE direct device texture-present symbol is unavailable"));
    }
    QString hnyquistReason;
    if ( !setLiveReducedHnyquist(live, reducedHnyquist, &hnyquistReason) )
    {
        return fail(hnyquistReason);
    }

    if ( !enterAmazeRunUnlessCudaSuppressed(&live.runtime, GpuFieldTelemetry::enabled()) ) return fail(cudaSuppressedReason());
    const LiveProbeArm probeArm = takeLiveProbeArm(live);
    const double runStartSteadyMs = gpuAmazeDebayerSteadyNowMs();
    const int rc =
        live.runtime.runPostWbGlTextureFromDeviceBayer16(
            live.backend,
            deviceBayer16,
            outputRgba16GlTexture,
            width,
            height,
            blackLevel,
            wbMultipliers[0],
            wbMultipliers[1],
            wbMultipliers[2]);
    if ( GpuFieldTelemetry::enabled() ) noteAmazeStageTelemetry(&live.runtime, live.backend, "amaze:device_bayer16", rc);
    if ( rc != 0 )
    {
        return fail(QStringLiteral("GPU AMaZE direct device texture-present run failed rc=%1 renderer=%2")
            .arg(rc)
            .arg(live.rendererDescription.isEmpty()
                ? QStringLiteral("unknown")
                : live.rendererDescription));
    }

    if ( rendererDescription ) *rendererDescription = live.rendererDescription;
    copyAmazeTiming(&live.runtime, live.backend, timing);
    noteLiveProbeAfterRun(live, probeArm, runStartSteadyMs, timing);
    if ( reason ) reason->clear();
    return true;
}

bool gpuAmazeDebayerApplyGpuOffscreenPostWb(const float * inputRawFrame,
                                           uint16_t * outputRgb16,
                                           int width,
                                           int height,
                                           int blackLevel,
                                           const double wbMultipliers[3],
                                           QString * reason,
                                           QString * rendererDescription,
                                           GpuAmazeDebayerBackendTiming * timing)
{
    if ( llrpGpuCudaSuppressed() )
    {
        if ( reason ) *reason = cudaSuppressedReason();
        if ( rendererDescription ) rendererDescription->clear();
        if ( timing ) *timing = GpuAmazeDebayerBackendTiming();
        return false;
    }
    if ( !validWbMultipliers(wbMultipliers) )
    {
        if ( reason ) *reason = QStringLiteral("GPU AMaZE post-WB fallback WB multipliers are invalid");
        if ( rendererDescription ) rendererDescription->clear();
        if ( timing ) *timing = GpuAmazeDebayerBackendTiming();
        return false;
    }

    const bool ok = gpuAmazeDebayerApplyGpuOffscreen(inputRawFrame,
                                                    outputRgb16,
                                                    width,
                                                    height,
                                                    reason,
                                                    rendererDescription,
                                                    timing);
    if ( !ok ) return false;

    applyPostWbUndoToRgb16(outputRgb16, width, height, blackLevel, wbMultipliers);
    return true;
}

void gpuAmazeDebayerSetStageTelemetrySink(GpuAmazeStageTelemetrySink sink)
{
    g_amazeStageTelemetrySink.store(sink);
}

int gpuAmazeCudaErrorsEmitOrphans(const GpuAmazeCudaErrorSource & source, int channels,
                                  GpuAmazeStageTelemetrySink sink)
{
    if ( !sink || !source.drain ) return 0;
    int emitted = 0;
    for ( int channel = 0; channel < channels && channel < 2; ++channel )
    {
        if ( source.pending[channel] && __atomic_load_n(source.pending[channel], __ATOMIC_RELAXED) == 0 )
            continue;
        igpu_cuda_error_t notes[IGPU_CUDA_ERROR_PENDING_MAX];
        unsigned long long dropped = 0;
        const int n = drainAmazeCudaErrors(source, channel, notes, &dropped);
        for ( int i = 0; i < n; ++i, ++emitted )
        {
            sink("amaze:uncaptured", 0, -1.0, notes[i].code, notes[i].name, notes[i].site, "orphan",
                 i == 0 ? dropped : 0);
        }
    }
    return emitted;
}

int gpuAmazeCudaErrorsEmitFailure(const GpuAmazeCudaErrorSource & source, const char * stage, int rc,
                                  GpuAmazeStageTelemetrySink sink)
{
    if ( !sink || rc == 0 ) return 0;
    if ( !source.drain )
    {
        /* An r4 DLL: the last code, attributed only when one was noted since
         * the previous read. */
        static std::atomic<unsigned long long> seenCudaErrorCount{0};
        igpu_cuda_error_t error;
        std::memset(&error, 0, sizeof(error));
        if ( source.last
          && source.last(&error) == 0
          && seenCudaErrorCount.exchange(error.count) != error.count
          && error.code != 0 )
        {
            error.name[sizeof(error.name) - 1] = '\0';
            error.site[sizeof(error.site) - 1] = '\0';
            sink(stage, rc, -1.0, error.code, error.name, error.site, "primary", 0);
        }
        else
        {
            sink(stage, rc, -1.0, 0, nullptr, nullptr, "primary", 0);
        }
        return 1;
    }
    igpu_cuda_error_t notes[IGPU_CUDA_ERROR_PENDING_MAX];
    unsigned long long dropped = 0;
    const int n = drainAmazeCudaErrors(source, IGPU_CUDA_ERROR_CHANNEL_RENDER, notes, &dropped);
    if ( n == 0 )
    {
        sink(stage, rc, -1.0, 0, nullptr, nullptr, "primary", dropped);
        return 1;
    }
    for ( int i = 0; i < n; ++i )
    {
        sink(stage, rc, -1.0, notes[i].code, notes[i].name, notes[i].site,
             i == 0 ? "primary" : "secondary", i == 0 ? dropped : 0);
    }
    return n;
}

int gpuAmazeDebayerDrainCudaErrorsAtTeardown(int waitMs)
{
    const GpuAmazeStageTelemetrySink sink = g_amazeStageTelemetrySink.load(std::memory_order_relaxed);
    if ( !sink ) return 0;
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    /* A live run still in flight keeps its own codes: wait for it (bounded). A
     * run that does not return in time cannot hide a code either: the drain
     * runs anyway, and the code is reported as an orphan with its own site. */
    const bool locked = live.mutex.tryLock(waitMs > 0 ? waitMs : 0);
    GpuAmazeCudaErrorSource source;
    if ( const GpuAmazeCudaErrorSource * forTesting = g_amazeCudaErrorSourceForTesting.load() )
    {
        source = *forTesting;
    }
#ifdef Q_OS_WIN
    else
    {
        /* Only a DLL this process already loaded (offscreen runs load it too,
         * and nothing unloads it); never a fresh load at exit. */
        const QString path = QDir::toNativeSeparators(gpuAmazeDebayerDllPath());
        HMODULE module = GetModuleHandleW(reinterpret_cast<const wchar_t *>(path.utf16()));
        if ( !module )
        {
            const QString name = QFileInfo(path).fileName();
            module = GetModuleHandleW(reinterpret_cast<const wchar_t *>(name.utf16()));
        }
        if ( module )
        {
            source = resolveAmazeCudaErrorSource(
                reinterpret_cast<GpuAmazeLastCudaErrorFn>(
                    reinterpret_cast<void *>(GetProcAddress(module, "igpu_amaze_debayer_last_cuda_error"))),
                reinterpret_cast<GpuAmazeDrainCudaErrorsFn>(
                    reinterpret_cast<void *>(GetProcAddress(module, "igpu_amaze_debayer_drain_cuda_errors"))),
                reinterpret_cast<GpuAmazeCudaErrorPendingWordFn>(
                    reinterpret_cast<void *>(GetProcAddress(module, "igpu_amaze_debayer_cuda_error_pending_word"))));
        }
    }
#endif
    const int emitted = gpuAmazeCudaErrorsEmitOrphans(source, 2, sink);
    if ( locked ) live.mutex.unlock();
    return emitted;
}

void gpuAmazeDebayerSetCudaErrorSourceForTesting(const GpuAmazeCudaErrorSource * source)
{
    g_amazeCudaErrorSourceForTesting.store(source);
}

bool gpuAmazeDebayerEnterRunForTesting(const GpuAmazeCudaErrorSource & source)
{
    GpuAmazeDebayerRuntime runtime;
    runtime.cudaErrors = source;
    return enterAmazeRunUnlessCudaSuppressed(&runtime, true);
}

// PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row 0
double gpuAmazeDebayerSteadyNowMs(void)
{
    const auto now = std::chrono::steady_clock::now().time_since_epoch();
    return std::chrono::duration<double, std::milli>(now).count();
}

void gpuAmazeDebayerArmProbe(int calls)
{
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    QMutexLocker locker(&live.mutex);
    g_probeArmCalls = calls > 0 ? calls : 0;
    g_probeSessionCallsLeft = 0;   // a paused arm ends any Play session's arm
}

void gpuAmazeDebayerBeginProbeSession(void)
{
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    QMutexLocker locker(&live.mutex);
    g_probeSessionRecord = GpuAmazeProbeSessionRecord();
    g_probeSessionCallsLeft = GpuAmazeProbeSessionRecord::kMaxCalls;
    g_probeArmCalls = 0;
}

GpuAmazeProbeSessionRecord gpuAmazeDebayerProbeSessionRecord(void)
{
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    QMutexLocker locker(&live.mutex);
    return g_probeSessionRecord;
}

// Row K
bool gpuAmazeDebayerLiveBackendLoaded(void)
{
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    QMutexLocker locker(&live.mutex);
    return live.attempted && live.available && live.backend != nullptr;
}

bool gpuAmazeDebayerClockKickBegin(int maxMs, QString * why, double * beginSteadyMs)
{
    if ( beginSteadyMs ) *beginSteadyMs = 0.0;
    auto fail = [&](const char * reason) -> bool
    {
        if ( why ) *why = QString::fromLatin1(reason);
        return false;
    };
    if ( llrpGpuCudaSuppressed() ) return fail("cuda_suppressed");
    // never live.ensure(): the kick only uses a backend the route prewarm (or a present) already loaded
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    QMutexLocker locker(&live.mutex);
    if ( !live.attempted || !live.available || !live.backend ) return fail("backend_not_loaded");
    if ( !live.runtime.clockKickBegin || !live.runtime.clockKickEnd ) return fail("symbol_missing");
    // like every AMaZE run: read the device-loss flag again under the lock, after any orphan code was emitted (the
    // kick is not one of the four AMaZE runs the r6 wiring census counts)
    const bool mayLaunch = enterAmazeRunUnlessCudaSuppressed(&live.runtime, GpuFieldTelemetry::enabled());
    if ( !mayLaunch ) return fail("cuda_suppressed");
    const double startMs = gpuAmazeDebayerSteadyNowMs();
    const int rc = live.runtime.clockKickBegin(live.backend, maxMs);
    if ( rc == 1 ) return fail("in_flight");
    if ( rc != 0 ) return fail("begin_failed");
    if ( beginSteadyMs ) *beginSteadyMs = startMs;
    if ( why ) why->clear();
    return true;
}

GpuAmazeClockKickReport gpuAmazeDebayerClockKickEnd(void)
{
    GpuAmazeClockKickReport report;
    GpuAmazeDebayerLiveTextureRuntime & live = liveAmazeTextureRuntime();
    QMutexLocker locker(&live.mutex);
    if ( !live.backend || !live.runtime.clockKickEnd ) return report;
    igpu_clock_kick_report_t raw;
    std::memset(&raw, 0, sizeof(raw));
    if ( live.runtime.clockKickEnd(live.backend, &raw) < 0 ) return report;
    report.state = raw.state;
    report.stoppedBy = raw.stopped_by;
    report.ranMs = raw.ran_ms;
    if ( raw.state != 0 ) return report;
    const int n = raw.samples < 0 ? 0 : ( raw.samples > IGPU_CLOCK_KICK_MAX_SAMPLES ? IGPU_CLOCK_KICK_MAX_SAMPLES : raw.samples );
    std::vector<unsigned long long> ns(raw.sample_ns, raw.sample_ns + n);
    std::vector<long long> clk(raw.sample_clk, raw.sample_clk + n);
    report.sampleMs = playback_gpu_clock_kick::sampleMs(ns);
    report.intervalMhz = playback_gpu_clock_kick::intervalMhz(ns, clk);
    return report;
}
