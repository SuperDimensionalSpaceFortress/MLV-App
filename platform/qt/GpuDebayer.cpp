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

#include <atomic>
#include <cstring>

namespace
{
constexpr GLfloat kQuadVertices[16] = {
    -1.0f,  1.0f, 0.0f, 1.0f,
     1.0f,  1.0f, 1.0f, 1.0f,
    -1.0f, -1.0f, 0.0f, 0.0f,
     1.0f, -1.0f, 1.0f, 0.0f,
};

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
};

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

QString gpuAmazeDebayerReducedHnyquistMissingReason(void)
{
    return QStringLiteral("AMaZE DLL lacks the reduced H-Nyquist filter");
}

bool gpuAmazeDebayerReducedHnyquistAvailable(void)
{
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

    const int rc = runtime.run(backend, inputRawFrame, outputRgb16, width, height);
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

    const int rc = runtime.runPostWbGlTexture(backend,
                                             inputRawFrame,
                                             glTexture,
                                             width,
                                             height,
                                             blackLevel,
                                             wbMultipliers[0],
                                             wbMultipliers[1],
                                             wbMultipliers[2]);
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
    QString hnyquistReason;
    if ( !setLiveReducedHnyquist(live, reducedHnyquist, &hnyquistReason) )
    {
        return fail(hnyquistReason);
    }
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
    if ( !live.runtime.runPostWbGlTextureFromDeviceBayer16 )
    {
        return fail(QStringLiteral("GPU AMaZE direct device texture-present symbol is unavailable"));
    }
    QString hnyquistReason;
    if ( !setLiveReducedHnyquist(live, reducedHnyquist, &hnyquistReason) )
    {
        return fail(hnyquistReason);
    }

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
