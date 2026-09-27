/*!
 * \file GpuDisplayViewport.cpp
 * \author Codex
 * \copyright 2026
 * \brief Optional OpenGL-backed viewport for the existing QGraphicsView preview.
 */

#include "GpuDisplayViewport.h"
#include "GpuDisplayWindow.h"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <limits>
#include <QByteArray>
#include <QColor>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QOpenGLContext>
#include <QPalette>
#include <QPolygonF>
#include <QSurfaceFormat>
#include <QElapsedTimer>
#include <QTimer>
#include <QDir>
#include <QEvent>
#include <QVector2D>
#include <QVector3D>
#include <QtDebug>

namespace
{
/* Vertex layout is (x, y, u, v). The texture's first row (v=0) is the TOP of
 * the image for both the CPU QImage upload and the CUDA-written recon texture,
 * so screen-top must map to v=0 and screen-bottom to v=1. The previous mapping
 * (screen-top -> v=1) sampled the last row at the top and presented every frame
 * vertically flipped (upside down) on every GL-viewport path. */
constexpr GLfloat kQuadVertices[16] = {
    -1.0f,  1.0f, 0.0f, 0.0f,
     1.0f,  1.0f, 1.0f, 0.0f,
    -1.0f, -1.0f, 0.0f, 1.0f,
     1.0f, -1.0f, 1.0f, 1.0f,
};

bool envFlagEnabled(const QByteArray &value)
{
    if ( value.isEmpty() ) return false;

    QByteArray normalized = value.trimmed().toLower();
    return normalized == "1"
        || normalized == "true"
        || normalized == "yes"
        || normalized == "on";
}

/* Bug A diagnostic: env-gated (MLVAPP_VIEWPORT_PRESENT_DIAG=1) per-frame trace of
 * the CPU present -> texture-upload path. Inert unless the env var is set. Used to
 * pin why the CPU image never becomes a GL texture on the Dell 3060-hybrid (the
 * upload happens on the 4090 and llvmpipe but not there). Stderr-only, no state. */
bool viewportPresentDiagEnabled()
{
    static const bool enabled =
        envFlagEnabled(qgetenv("MLVAPP_VIEWPORT_PRESENT_DIAG"));
    return enabled;
}

bool rectsNearlyEqual(const QRectF &a, const QRectF &b)
{
    constexpr qreal kTolerance = 0.01;
    return std::abs(a.left() - b.left()) <= kTolerance
        && std::abs(a.top() - b.top()) <= kTolerance
        && std::abs(a.width() - b.width()) <= kTolerance
        && std::abs(a.height() - b.height()) <= kTolerance;
}

QSize displaySizeForWindowPresent(const QGraphicsView *view,
                                  const QGraphicsPixmapItem *fallbackItem,
                                  const QImage &image)
{
    const QRectF viewSceneRect = view ? view->sceneRect() : QRectF();
    const QRectF sceneObjectRect =
        ( view && view->scene() ) ? view->scene()->sceneRect() : QRectF();
    QRectF itemRect;
    if ( fallbackItem )
    {
        itemRect = fallbackItem->sceneBoundingRect();
    }

    const auto rectMatchesImage = [&image](const QRectF &rect) -> bool
    {
        constexpr qreal kTolerance = 0.01;
        return !rect.isEmpty()
            && std::abs(rect.width() - image.width()) <= kTolerance
            && std::abs(rect.height() - image.height()) <= kTolerance;
    };

    QRectF sceneRect;
    const QRectF candidates[3] = { sceneObjectRect, viewSceneRect, itemRect };
    for ( const QRectF &candidate : candidates )
    {
        if ( !candidate.isEmpty() && !rectMatchesImage(candidate) )
        {
            sceneRect = candidate;
            break;
        }
    }
    if ( sceneRect.isEmpty() )
    {
        for ( const QRectF &candidate : candidates )
        {
            if ( !candidate.isEmpty() )
            {
                sceneRect = candidate;
                break;
            }
        }
    }

    if ( sceneRect.isEmpty() )
    {
        return image.size();
    }

    return QSize(std::max(1, qRound(sceneRect.width())),
                 std::max(1, qRound(sceneRect.height())));
}

/* Frame-grab telemetry: when MLVAPP_FRAME_GRAB_DIR is set, save the exact display
 * frame (the CPU RGBA image presented to the viewport/window) to a full-resolution
 * PNG per frame, capped. This captures the real displayed pixels for an apples-to-
 * apples CPU-vs-GPU-window quality comparison, bypassing lossy/occluded screen grabs.
 * Inert unless the env var is set. */
void maybeGrabDisplayFrame(const QImage &image)
{
    static const QByteArray dir = qgetenv("MLVAPP_FRAME_GRAB_DIR");
    if ( dir.isEmpty() || image.isNull() ) return;
    static int counter = 0;
    if ( counter >= 120 ) return;
    static const bool dirReady = QDir().mkpath(QString::fromLocal8Bit(dir));
    Q_UNUSED(dirReady);
    const QString path = QString::fromLocal8Bit(dir)
        + QStringLiteral("/grab_%1.png").arg(counter, 5, 10, QChar('0'));
    if ( image.save(path, "PNG") )
    {
        qInfo().nospace() << "frame_grab saved " << path << " (" << image.width()
                          << "x" << image.height() << " fmt=" << static_cast<int>(image.format()) << ")";
        ++counter;
    }
}

/* Historical presentation probes, inert unless explicitly enabled. The black
 * viewport was reproduced on both Bachelor and UltraMagnus on September 8:
 * QGraphicsView consumed normal paint events while internal grabs forced GL
 * rendering. Explicit viewport paint ownership fixed that defect on both hosts.
 * Hybrid composition, swap and flush remain hypotheses for other incidents,
 * not the established cause here. Validate the actual application HWND with
 * compositor captures; an internal framebuffer grab cannot prove visibility.
 * The matching global format/context probes live in main.cpp. */
QSurfaceFormat::SwapBehavior viewportAbSwapBehavior(bool *isSet)
{
    const QByteArray v = qgetenv("MLVAPP_VIEWPORT_AB_SWAP").trimmed().toLower();
    if ( isSet ) *isSet = true;
    if ( v == "single" ) return QSurfaceFormat::SingleBuffer;
    if ( v == "double" ) return QSurfaceFormat::DoubleBuffer;
    if ( v == "triple" ) return QSurfaceFormat::TripleBuffer;
    if ( isSet ) *isSet = false;
    return QSurfaceFormat::DefaultSwapBehavior;
}

/* 0 = none, 1 = glFlush, 2 = glFinish, 3 = both. Forces the dGPU to flush/complete
 * the frame before Qt blits the FBO and presents, in case the Optimus cross-adapter
 * copy races an incomplete frame. */
int viewportAbFlushMode()
{
    const QByteArray v = qgetenv("MLVAPP_VIEWPORT_AB_FLUSH").trimmed().toLower();
    if ( v == "flush" ) return 1;
    if ( v == "finish" ) return 2;
    if ( v == "both" ) return 3;
    return 0;
}

/* Force a native HWND for the GL viewport so it presents through its own surface
 * rather than being redirected through the parent's backing store -- a documented
 * Optimus cross-adapter present workaround (low-probability probe for an embedded
 * viewport: a native QOpenGLWidget is still not a QOpenGLWindow). */
bool viewportAbNativeWindow()
{
    return envFlagEnabled(qgetenv("MLVAPP_VIEWPORT_AB_NATIVE"));
}

/* Probe an alpha channel in the per-widget format. Top-level backing-store alpha
 * is a separate Qt composition requirement; this flag did not establish the
 * cause of the September 8 paint-routing defect. */
bool viewportAbAlphaEnabled()
{
    return envFlagEnabled(qgetenv("MLVAPP_VIEWPORT_AB_ALPHA"));
}

/* Probe WA_AlwaysStackOnTop composition. This changes sibling stacking and is
 * retained as an explicit diagnostic option, not the normal playback fix. */
bool viewportAbStackTopEnabled()
{
    return envFlagEnabled(qgetenv("MLVAPP_VIEWPORT_AB_STACKTOP"));
}

/* The Optimus cross-adapter swapchain is established lazily on the first present and
 * can stay black until a geometry change re-warms it ("black until you resize"). A
 * one-shot deferred top-level resize +/-1px after the context is live reproduces that
 * nudge to prime the chain. One-shot, gated, distinct from the alpha/stacktop fixes. */
bool viewportAbPrimeEnabled()
{
    return envFlagEnabled(qgetenv("MLVAPP_VIEWPORT_AB_PRIME"));
}

bool rowLooksLikeUniformTopMagentaBandRgb16(const uint16_t *row, int width)
{
    if( !row || width <= 0 )
    {
        return false;
    }

    const int sampleStep = std::max( 1, width / 512 );
    int samples = 0;
    int magentaSamples = 0;
    int minR = 255, minG = 255, minB = 255;
    int maxR = 0, maxG = 0, maxB = 0;
    uint64_t sumR = 0, sumG = 0, sumB = 0;

    for( int x = 0; x < width; x += sampleStep )
    {
        const uint16_t *pixel = row + static_cast<size_t>( x ) * 4u;
        const int r = static_cast<int>( pixel[0] >> 8 );
        const int g = static_cast<int>( pixel[1] >> 8 );
        const int b = static_cast<int>( pixel[2] >> 8 );
        ++samples;
        sumR += static_cast<uint64_t>( r );
        sumG += static_cast<uint64_t>( g );
        sumB += static_cast<uint64_t>( b );
        minR = std::min( minR, r );
        minG = std::min( minG, g );
        minB = std::min( minB, b );
        maxR = std::max( maxR, r );
        maxG = std::max( maxG, g );
        maxB = std::max( maxB, b );
        if( r > 170
         && b > 140
         && g < 150
         && (((r + b) / 2) - g) > 35 )
        {
            ++magentaSamples;
        }
    }

    if( samples <= 0 )
    {
        return false;
    }

    const double invSamples = 1.0 / static_cast<double>( samples );
    const double avgR = static_cast<double>( sumR ) * invSamples;
    const double avgG = static_cast<double>( sumG ) * invSamples;
    const double avgB = static_cast<double>( sumB ) * invSamples;
    const bool magentaAverage =
        avgR >= 170.0
        && avgB >= 140.0
        && avgG <= 150.0
        && (avgR - avgG) >= 45.0
        && (avgB - avgG) >= 35.0;
    const bool mostlyMagenta =
        magentaSamples * 100 >= samples * 90;
    const bool lowVariance =
        (maxR - minR) <= 24
        && (maxG - minG) <= 80
        && (maxB - minB) <= 24;

    return magentaAverage && mostlyMagenta && lowVariance;
}

int suppressUniformTopMagentaBandRgb16(uint16_t *data, int width, int height)
{
    if( !data || width <= 0 || height <= 0 )
    {
        return 0;
    }

    const int rowPixels = width * 4;
    const int maxProbeRows =
        std::min( height, std::max( 4, height / 8 ) );
    int probeStartRow = 0;
    for( ; probeStartRow < maxProbeRows; ++probeStartRow )
    {
        const uint16_t *row =
            data + static_cast<size_t>( probeStartRow ) * static_cast<size_t>( rowPixels );
        const int sampleStep = std::max( 1, width / 512 );
        int samples = 0;
        uint64_t sumR = 0, sumG = 0, sumB = 0;
        int brightSamples = 0;
        for( int x = 0; x < width; x += sampleStep )
        {
            const uint16_t *pixel = row + static_cast<size_t>( x ) * 4u;
            const int r = static_cast<int>( pixel[0] >> 8 );
            const int g = static_cast<int>( pixel[1] >> 8 );
            const int b = static_cast<int>( pixel[2] >> 8 );
            ++samples;
            sumR += static_cast<uint64_t>( r );
            sumG += static_cast<uint64_t>( g );
            sumB += static_cast<uint64_t>( b );
            if( r > 8 || g > 8 || b > 8 )
            {
                ++brightSamples;
            }
        }

        if( samples <= 0 )
        {
            break;
        }

        const double invSamples = 1.0 / static_cast<double>( samples );
        const double avg =
            (static_cast<double>( sumR + sumG + sumB ) * invSamples) / 3.0;
        if( avg > 4.0 || brightSamples * 100 > samples * 2 )
        {
            break;
        }
    }

    if( probeStartRow >= maxProbeRows )
    {
        return 0;
    }

    int bandRows = 0;
    for( int y = probeStartRow; y < maxProbeRows; ++y )
    {
        const uint16_t *row =
            data + static_cast<size_t>( y ) * static_cast<size_t>( rowPixels );
        if( !rowLooksLikeUniformTopMagentaBandRgb16( row, width ) )
        {
            break;
        }
        ++bandRows;
    }

    if( bandRows < 4 )
    {
        return 0;
    }

    for( int y = probeStartRow; y < probeStartRow + bandRows; ++y )
    {
        uint16_t *row =
            data + static_cast<size_t>( y ) * static_cast<size_t>( rowPixels );
        std::memset( row, 0, static_cast<size_t>( rowPixels ) * sizeof(uint16_t) );
    }

    return bandRows;
}
}

GpuDisplayViewport::GpuDisplayViewport(QWidget *parent)
    : QOpenGLWidget(parent)
    , m_loggedContext(false)
    , m_loggedTexturePath(false)
    , m_textureDirty(false)
    , m_texturePresentationActive(false)
    , m_samplingModeDirty(false)
    , m_processingTexturesDirty(false)
    , m_pendingTextureIs16Bit(false)
    , m_pendingTextureIsBayer16(false)
    , m_pendingTextureFromGpuAmaze(false)
    , m_pendingTextureFromGpuRecon(false)
    , m_textureIs16Bit(false)
    , m_textureIsBayer16(false)
    , m_gpuReconSourceTextureCurrent(false)
    , m_abPrimed(false)
    , m_view(qobject_cast<QGraphicsView *>(parent))
    , m_fallbackItem(nullptr)
    , m_pendingTextureWidth(0)
    , m_pendingTextureHeight(0)
    , m_program(nullptr)
    , m_texture(nullptr)
    , m_gpuReconSourceTexture(nullptr)
{
    QSurfaceFormat requestedFormat = format();
    requestedFormat.setSwapInterval(0);
    bool abSwapSet = false;
    const QSurfaceFormat::SwapBehavior abSwap = viewportAbSwapBehavior(&abSwapSet);
    if ( abSwapSet )
        requestedFormat.setSwapBehavior(abSwap);
    if ( viewportAbAlphaEnabled() )
    {
        // Belt-and-suspenders per-widget half of the alpha-backing-store fix; the
        // decisive global default format is set before QApplication (main.cpp).
        requestedFormat.setAlphaBufferSize(8);
        requestedFormat.setDepthBufferSize(24);
        requestedFormat.setStencilBufferSize(8);
        if ( !abSwapSet )
            requestedFormat.setSwapBehavior(QSurfaceFormat::DoubleBuffer);
    }
    setFormat(requestedFormat);
    setUpdateBehavior(QOpenGLWidget::NoPartialUpdate);
    setAutoFillBackground(false);
    if ( viewportAbNativeWindow() )
        setAttribute(Qt::WA_NativeWindow, true);
    if ( viewportAbStackTopEnabled() )
        setAttribute(Qt::WA_AlwaysStackOnTop, true);
    if ( abSwapSet || viewportAbAlphaEnabled() || viewportAbStackTopEnabled()
         || viewportAbNativeWindow() || viewportAbPrimeEnabled() || viewportAbFlushMode() != 0 )
        qInfo().nospace() << "viewport_ab present probe active: swap="
            << qgetenv("MLVAPP_VIEWPORT_AB_SWAP").constData()
            << " alpha=" << (viewportAbAlphaEnabled() ? 1 : 0)
            << " stacktop=" << (viewportAbStackTopEnabled() ? 1 : 0)
            << " native=" << (viewportAbNativeWindow() ? 1 : 0)
            << " prime=" << (viewportAbPrimeEnabled() ? 1 : 0)
            << " flush=" << qgetenv("MLVAPP_VIEWPORT_AB_FLUSH").constData();
}

GpuDisplayViewport::~GpuDisplayViewport()
{
    cleanupGLResources();
}

bool GpuDisplayViewport::isRequestedByEnvironment()
{
    return envFlagEnabled(qgetenv(environmentVariableName()));
}

const char *GpuDisplayViewport::environmentVariableName()
{
    return "MLVAPP_EXPERIMENTAL_GL_VIEWPORT";
}

/* QGraphicsView normally consumes viewport Paint events to draw its scene.
 * Our GPU path hides the scene's pixmap and paints in QOpenGLWidget::paintGL,
 * so it must own Paint while a GPU frame is pending. Otherwise ordinary playback
 * paints an empty scene even though grab()/grabFramebuffer() can force a correct
 * offscreen image. Reproduced with compositor capture on RTX 4090; internal grabs
 * alone cannot establish physical presentation on desktop or hybrid GPUs. */
bool GpuDisplayViewport::installOn(QGraphicsView *view)
{
    if ( !view || !isRequestedByEnvironment() ) return false;
    if ( from(view) ) return true;

    GpuDisplayViewport *viewport = new GpuDisplayViewport(view);
    view->setViewport(viewport);
    // Install after setViewport: Qt invokes the newest event filter first.
    viewport->installEventFilter(viewport);
    view->setViewportUpdateMode(QGraphicsView::FullViewportUpdate);
    view->setCacheMode(QGraphicsView::CacheNone);
    qInfo() << "Experimental GPU viewport enabled via"
            << environmentVariableName()
            << "- QGraphicsView now renders through QOpenGLWidget.";
    return true;
}

bool GpuDisplayViewport::eventFilter(QObject *watched, QEvent *event)
{
    if ( watched == this && event->type() == QEvent::Paint && hasPendingFrame() )
    {
        // Invoke Qt's paint-event path, which binds the widget FBO, calls paintGL,
        // and schedules composition. Calling paintGL directly skips that setup.
        QOpenGLWidget::event(event);
        return true;
    }
    // Preserve QGraphicsView's scene/fallback, input, resize and scroll handling.
    return QOpenGLWidget::eventFilter(watched, event);
}

bool GpuDisplayViewport::isInstalledOn(const QGraphicsView *view)
{
    return from(view) != nullptr;
}

bool GpuDisplayViewport::hasPresentedImage(const QGraphicsView *view)
{
    const GpuDisplayViewport *viewport = from(view);
    return viewport && viewport->hasPendingFrame();
}

bool GpuDisplayViewport::hasPresentedGpuReconTexture(const QGraphicsView *view)
{
    const GpuDisplayViewport *viewport = from(view);
    return viewport
        && viewport->m_texture
        && viewport->m_pendingTextureFromGpuRecon
        && viewport->m_textureIs16Bit
        && viewport->m_pendingTextureWidth > 0
        && viewport->m_pendingTextureHeight > 0;
}

bool GpuDisplayViewport::isTexturePresentationActive(const QGraphicsView *view)
{
    const GpuDisplayViewport *viewport = from(view);
    return viewport && viewport->m_texturePresentationActive;
}

GpuDisplayViewport::SamplingMode GpuDisplayViewport::samplingModeFor(const QGraphicsView *view)
{
    const GpuDisplayViewport *viewport = from(view);
    return viewport ? viewport->m_presentationOptions.samplingMode : SamplingLinear;
}

QString GpuDisplayViewport::rendererDescriptionFor(const QGraphicsView *view)
{
    const GpuDisplayViewport *viewport = from(view);
    return viewport ? viewport->m_rendererDescription : QString();
}

bool GpuDisplayViewport::readPresentedBayer16Texture(QGraphicsView *view,
                                                     QByteArray *textureBytes,
                                                     int *width,
                                                     int *height,
                                                     QString *reason)
{
    GpuDisplayViewport *viewport = from(view);
    auto fail = [reason](const QString &why) -> bool
    {
        if ( reason ) *reason = why;
        return false;
    };

    if ( textureBytes ) textureBytes->clear();
    if ( width ) *width = 0;
    if ( height ) *height = 0;

    if ( !viewport || !textureBytes )
    {
        return fail(QStringLiteral("GL texture readback requires an installed GPU viewport"));
    }
    if ( !viewport->m_texture
      || !viewport->m_pendingTextureFromGpuRecon
      || !viewport->m_textureIs16Bit
      || !viewport->m_textureIsBayer16
      || viewport->m_pendingTextureWidth <= 0
      || viewport->m_pendingTextureHeight <= 0 )
    {
        return fail(QStringLiteral("GL texture readback requires an active GPU recon R16 Bayer texture"));
    }

    QOpenGLContext *glContext = viewport->context();
    if ( !glContext )
    {
        return fail(QStringLiteral("GL texture readback requires an initialized OpenGL context"));
    }

#if defined(QT_OPENGL_ES_2)
    return fail(QStringLiteral("GL texture readback via glGetTexImage is unavailable on OpenGL ES"));
#else
    const bool needsCurrent = QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (viewport->makeCurrent(), true) : false;

    const int texWidth = viewport->m_pendingTextureWidth;
    const int texHeight = viewport->m_pendingTextureHeight;
    const size_t byteCount =
        static_cast<size_t>(texWidth) * static_cast<size_t>(texHeight) * sizeof(uint16_t);
    if ( byteCount > static_cast<size_t>(std::numeric_limits<int>::max()) )
    {
        if ( madeCurrent ) viewport->doneCurrent();
        return fail(QStringLiteral("GL texture readback frame is too large"));
    }

    QOpenGLFunctions *gl = glContext->functions();
    if ( !gl )
    {
        if ( madeCurrent ) viewport->doneCurrent();
        return fail(QStringLiteral("GL texture readback requires OpenGL functions"));
    }
    using GlGetTexImageFn = void (*)(GLenum, GLint, GLenum, GLenum, void *);
    GlGetTexImageFn glGetTexImageProc =
        reinterpret_cast<GlGetTexImageFn>(glContext->getProcAddress("glGetTexImage"));
    if ( !glGetTexImageProc )
    {
        if ( madeCurrent ) viewport->doneCurrent();
        return fail(QStringLiteral("GL texture readback function glGetTexImage is unavailable"));
    }

    textureBytes->resize(static_cast<int>(byteCount));
    viewport->m_texture->bind(0);
    gl->glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glGetTexImageProc(GL_TEXTURE_2D,
                      0,
                      GL_RED,
                      GL_UNSIGNED_SHORT,
                      textureBytes->data());
    const GLenum error = gl->glGetError();
    viewport->m_texture->release();
    if ( madeCurrent ) viewport->doneCurrent();

    if ( error != GL_NO_ERROR )
    {
        textureBytes->clear();
        return fail(QStringLiteral("glGetTexImage failed with GL error 0x%1")
                    .arg(static_cast<unsigned int>(error), 0, 16));
    }

    if ( width ) *width = texWidth;
    if ( height ) *height = texHeight;
    if ( reason ) reason->clear();
    return true;
#endif
}

bool GpuDisplayViewport::readGpuReconSourceBayer16Texture(QGraphicsView *view,
                                                          QByteArray *textureBytes,
                                                          int *width,
                                                          int *height,
                                                          QString *reason)
{
    GpuDisplayViewport *viewport = from(view);
    auto fail = [reason](const QString &why) -> bool
    {
        if ( reason ) *reason = why;
        return false;
    };

    if ( textureBytes ) textureBytes->clear();
    if ( width ) *width = 0;
    if ( height ) *height = 0;

    if ( !viewport || !textureBytes )
    {
        return fail(QStringLiteral("GL recon-source texture readback requires an installed GPU viewport"));
    }
    if ( !viewport->m_gpuReconSourceTexture
      || !viewport->m_gpuReconSourceTextureCurrent
      || !viewport->m_pendingTextureFromGpuRecon
      || !viewport->m_textureIs16Bit
      || viewport->m_textureIsBayer16
      || viewport->m_pendingTextureWidth <= 0
      || viewport->m_pendingTextureHeight <= 0 )
    {
        return fail(QStringLiteral("GL recon-source texture readback requires an active GPU AMaZE recon source texture"));
    }

    QOpenGLContext *glContext = viewport->context();
    if ( !glContext )
    {
        return fail(QStringLiteral("GL recon-source texture readback requires an initialized OpenGL context"));
    }

#if defined(QT_OPENGL_ES_2)
    return fail(QStringLiteral("GL texture readback via glGetTexImage is unavailable on OpenGL ES"));
#else
    const bool needsCurrent = QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (viewport->makeCurrent(), true) : false;

    const int texWidth = viewport->m_pendingTextureWidth;
    const int texHeight = viewport->m_pendingTextureHeight;
    const size_t byteCount =
        static_cast<size_t>(texWidth) * static_cast<size_t>(texHeight) * sizeof(uint16_t);
    if ( byteCount > static_cast<size_t>(std::numeric_limits<int>::max()) )
    {
        if ( madeCurrent ) viewport->doneCurrent();
        return fail(QStringLiteral("GL recon-source texture readback frame is too large"));
    }

    QOpenGLFunctions *gl = glContext->functions();
    if ( !gl )
    {
        if ( madeCurrent ) viewport->doneCurrent();
        return fail(QStringLiteral("GL recon-source texture readback requires OpenGL functions"));
    }
    using GlGetTexImageFn = void (*)(GLenum, GLint, GLenum, GLenum, void *);
    GlGetTexImageFn glGetTexImageProc =
        reinterpret_cast<GlGetTexImageFn>(glContext->getProcAddress("glGetTexImage"));
    if ( !glGetTexImageProc )
    {
        if ( madeCurrent ) viewport->doneCurrent();
        return fail(QStringLiteral("GL texture readback function glGetTexImage is unavailable"));
    }

    textureBytes->resize(static_cast<int>(byteCount));
    viewport->m_gpuReconSourceTexture->bind(0);
    gl->glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glGetTexImageProc(GL_TEXTURE_2D,
                      0,
                      GL_RED,
                      GL_UNSIGNED_SHORT,
                      textureBytes->data());
    const GLenum error = gl->glGetError();
    viewport->m_gpuReconSourceTexture->release();
    if ( madeCurrent ) viewport->doneCurrent();

    if ( error != GL_NO_ERROR )
    {
        textureBytes->clear();
        return fail(QStringLiteral("glGetTexImage failed for recon-source texture with GL error 0x%1")
                    .arg(static_cast<unsigned int>(error), 0, 16));
    }

    if ( width ) *width = texWidth;
    if ( height ) *height = texHeight;
    if ( reason ) reason->clear();
    return true;
#endif
}

bool GpuDisplayViewport::presentImage(QGraphicsView *view,
                                      QGraphicsPixmapItem *fallbackItem,
                                      const QImage &image,
                                      const PresentationOptions &options)
{
    maybeGrabDisplayFrame(image);

    // Hybrid (Optimus) path: when the QOpenGLWindow preview is active, route the CPU
    // frame (or a clear, for a null image) to it -- the QOpenGLWidget viewport is not
    // installed in that mode.
    if ( !image.isNull() )
    {
        const QSize displaySize =
            displaySizeForWindowPresent(view, fallbackItem, image);
        if ( GpuDisplayWindow::presentImageIfActive(image, displaySize, options.presentationSerial) )
            return true;
    }
    else if ( GpuDisplayWindow::clearIfActive() )
    {
        return false;
    }

    GpuDisplayViewport *viewport = from(view);
    if ( !viewport || image.isNull() )
    {
        if ( fallbackItem ) fallbackItem->setVisible(true);
        return false;
    }

    viewport->setFallbackItem(fallbackItem);
    viewport->setPresentedImage(image, options);
    return true;
}

bool GpuDisplayViewport::presentRgb16(QGraphicsView *view,
                                      QGraphicsPixmapItem *fallbackItem,
                                      const uint16_t *imageData,
                                      int width,
                                      int height,
                                      const PresentationOptions &options)
{
    GpuDisplayViewport *viewport = from(view);
    if ( !viewport || !imageData || width <= 0 || height <= 0 )
    {
        if ( fallbackItem ) fallbackItem->setVisible(true);
        return false;
    }

    viewport->setFallbackItem(fallbackItem);
    viewport->setPresentedRgb16(imageData, width, height, options);
    return true;
}

bool GpuDisplayViewport::presentBayer16(QGraphicsView *view,
                                        QGraphicsPixmapItem *fallbackItem,
                                        const uint16_t *imageData,
                                        int width,
                                        int height,
                                        const PresentationOptions &options)
{
    GpuDisplayViewport *viewport = from(view);
    if ( !viewport || !imageData || width <= 0 || height <= 0 )
    {
        if ( fallbackItem ) fallbackItem->setVisible(true);
        return false;
    }

    viewport->setFallbackItem(fallbackItem);
    viewport->setPresentedBayer16(imageData, width, height, options);
    return true;
}

bool GpuDisplayViewport::presentGpuPlaybackReconTexture(
    QGraphicsView *view,
    QGraphicsPixmapItem *fallbackItem,
    const uint16_t *rawInputBayer14,
    size_t rawInputBayer14Words,
    const llrpGpuPlaybackReconState_t *state,
    const PresentationOptions &options,
    QString *reason,
    llrpGpuPlaybackReconTiming_t *timing)
{
    GpuDisplayViewport *viewport = from(view);
    if ( !viewport
      || !rawInputBayer14
      || !state
      || !state->valid
      || state->width <= 0
      || state->height <= 0 )
    {
        if ( reason ) *reason = QStringLiteral("GPU playback recon texture-present viewport or input is invalid");
        if ( fallbackItem ) fallbackItem->setVisible(true);
        return false;
    }

    const size_t expectedWords =
        static_cast<size_t>(state->width) * static_cast<size_t>(state->height);
    if ( rawInputBayer14Words < expectedWords )
    {
        if ( reason ) *reason = QStringLiteral("GPU playback recon texture-present input is shorter than the frame geometry");
        if ( fallbackItem ) fallbackItem->setVisible(true);
        return false;
    }

    viewport->setFallbackItem(fallbackItem);
    return viewport->setPresentedGpuPlaybackReconTexture(rawInputBayer14,
                                                         rawInputBayer14Words,
                                                         state,
                                                         options,
                                                         reason,
                                                         timing);
}

bool GpuDisplayViewport::presentGpuPlaybackReconAmazePostWbTexture(
    QGraphicsView *view,
    QGraphicsPixmapItem *fallbackItem,
    const uint16_t *rawInputBayer14,
    size_t rawInputBayer14Words,
    const llrpGpuPlaybackReconState_t *state,
    int blackLevel,
    const double wbMultipliers[3],
    const PresentationOptions &options,
    QString *reason,
    llrpGpuPlaybackReconTiming_t *timing,
    QString *handoffMode,
    bool validationProbeTexture,
    const uint16_t *retainedDeviceBayer16,
    int retainedDeviceWidth,
    int retainedDeviceHeight)
{
    GpuDisplayViewport *viewport = from(view);
    const bool retainedDeviceValid =
        retainedDeviceBayer16
        && state
        && state->valid
        && retainedDeviceWidth == state->width
        && retainedDeviceHeight == state->height
        && !validationProbeTexture;
    if ( !viewport
      || !state
      || !state->valid
      || state->width <= 0
      || state->height <= 0
      || !wbMultipliers
      || ( !rawInputBayer14 && !retainedDeviceValid ) )
    {
        if ( reason ) *reason = QStringLiteral("GPU playback recon AMaZE texture-present viewport or input is invalid");
        if ( fallbackItem ) fallbackItem->setVisible(true);
        return false;
    }

    const size_t expectedWords =
        static_cast<size_t>(state->width) * static_cast<size_t>(state->height);
    if ( !retainedDeviceValid && rawInputBayer14Words < expectedWords )
    {
        if ( reason ) *reason = QStringLiteral("GPU playback recon AMaZE texture-present input is shorter than the frame geometry");
        if ( fallbackItem ) fallbackItem->setVisible(true);
        return false;
    }

    viewport->setFallbackItem(fallbackItem);
    return viewport->setPresentedGpuPlaybackReconAmazePostWbTexture(
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
        retainedDeviceHeight);
}

bool GpuDisplayViewport::presentAmazePostWbTexture(QGraphicsView *view,
                                                   QGraphicsPixmapItem *fallbackItem,
                                                   const float *rawFrame,
                                                   int width,
                                                   int height,
                                                   int blackLevel,
                                                   const double wbMultipliers[3],
                                                   const PresentationOptions &options,
                                                   QString *reason,
                                                   QString *rendererDescription,
                                                   GpuAmazeDebayerBackendTiming *timing)
{
    GpuDisplayViewport *viewport = from(view);
    if ( !viewport || !rawFrame || width <= 0 || height <= 0 )
    {
        if ( reason ) *reason = QStringLiteral("GPU AMaZE texture-present viewport or input is invalid");
        if ( fallbackItem ) fallbackItem->setVisible(true);
        return false;
    }

    viewport->setFallbackItem(fallbackItem);
    const bool ok = viewport->setPresentedAmazePostWbTexture(rawFrame,
                                                            width,
                                                            height,
                                                            blackLevel,
                                                            wbMultipliers,
                                                            options,
                                                            reason,
                                                            rendererDescription,
                                                            timing);
    if ( !ok && fallbackItem ) fallbackItem->setVisible(true);
    return ok;
}

void GpuDisplayViewport::clearPresentedImage(QGraphicsView *view,
                                             QGraphicsPixmapItem *fallbackItem)
{
    if ( GpuDisplayWindow::clearIfActive() ) return;

    GpuDisplayViewport *viewport = from(view);
    if ( !viewport )
    {
        if ( fallbackItem ) fallbackItem->setVisible(true);
        return;
    }

    if ( fallbackItem ) viewport->setFallbackItem(fallbackItem);
    viewport->clearPresentedImage();
}

void GpuDisplayViewport::initializeGL()
{
    initializeOpenGLFunctions();

    if ( context() )
    {
        connect(context(), &QOpenGLContext::aboutToBeDestroyed, this, [this]()
        {
            cleanupGLResources();
        }, Qt::UniqueConnection);
    }

    if ( viewportAbPrimeEnabled() && !m_abPrimed )
    {
        m_abPrimed = true;
        // Bug A on-screen present A/B (prime): one-shot deferred top-level resize
        // +/-1px to warm the Optimus cross-adapter swapchain ("black until you
        // resize"). Context object is `this`, so the timer auto-cancels if the
        // widget is destroyed before it fires.
        QTimer::singleShot(80, this, [this]()
        {
            if ( QWidget *top = window() )
            {
                const QSize s = top->size();
                top->resize(s + QSize(1, 1));
                top->resize(s);
            }
            update();
        });
    }

    if ( m_loggedContext ) return;

    QOpenGLContext *glContext = context();
    if ( !glContext )
    {
        qWarning() << "Experimental GPU viewport was requested, but no OpenGL context is available.";
        return;
    }

    const int requestedSwapInterval = this->format().swapInterval();
    const QSurfaceFormat realizedFormat = glContext->format();
    QOpenGLFunctions *functions = glContext->functions();
    const GLubyte *renderer = functions ? functions->glGetString(GL_RENDERER) : nullptr;
    const GLubyte *vendor = functions ? functions->glGetString(GL_VENDOR) : nullptr;
    const GLubyte *version = functions ? functions->glGetString(GL_VERSION) : nullptr;
    m_rendererDescription = renderer
        ? QString::fromLatin1(reinterpret_cast<const char *>(renderer))
        : QStringLiteral("unknown");

    qInfo().nospace()
        << "Experimental GPU viewport initialized ("
        << realizedFormat.majorVersion() << '.'
        << realizedFormat.minorVersion()
        << ", renderer=" << m_rendererDescription
        << ", vendor=" << (vendor ? reinterpret_cast<const char *>(vendor) : "unknown")
        << ", version=" << (version ? reinterpret_cast<const char *>(version) : "unknown")
        << ", requested_swap_interval=" << requestedSwapInterval
        << ", realized_swap_interval=" << realizedFormat.swapInterval()
        << ").";

    m_loggedContext = true;
}

void GpuDisplayViewport::paintGL()
{
    const QColor clearColor = m_view
        ? m_view->backgroundBrush().color()
        : palette().color(QPalette::Window);
    glDisable(GL_DEPTH_TEST);
    glDisable(GL_CULL_FACE);
    glDisable(GL_BLEND);
    glClearColor(clearColor.redF(), clearColor.greenF(), clearColor.blueF(), 1.0f);
    glClear(GL_COLOR_BUFFER_BIT);

    updateTextureIfNeeded();

    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 3, sol major): a GPU-recon/AMaZE
    // texture is the post-WB-undo linear camera RGB output, not display-referred -- it
    // must never be drawn with preview processing unavailable, which the shared shader's
    // previewProcessingEnabled=0 branch would otherwise render as a passthrough-equivalent
    // (the dark/green-cast regression this fix line exists to close). The submit-time gate
    // in setPresentedGpuPlaybackReconAmazePostWbTexture() already refuses to accept such a
    // texture when its LUTs are not usable, but that is re-checked here rather than
    // trusted -- mirrors GpuDisplayWindow::paintGL's reconRefused re-check -- so a texture
    // that somehow reached this point with its LUTs torn down (e.g. destroyTexture()
    // racing a paint, or updateTextureIfNeeded()'s own re-upload attempt above failing)
    // is refused rather than ever shown through the in-shader disable.
    const bool presentingReconTexture = m_pendingTextureFromGpuRecon || m_pendingTextureFromGpuAmaze;
    const bool reconLutsReady = presentingReconTexture
        && gpuPreviewProcessingLutTextureSetReady(m_lutSet, m_presentationOptions.previewProcessing);
    const bool reconRefused = gpuPreviewProcessingReconTexturePresentationRefused(
        presentingReconTexture, m_lutSet, m_presentationOptions.previewProcessing);

    if ( !m_texture || !m_program || !m_view || reconRefused )
    {
        m_texturePresentationActive = false;
        if ( viewportPresentDiagEnabled() )
            qInfo().nospace() << "viewport_diag paintGL.skip tex=" << (m_texture ? 1 : 0)
                << " prog=" << (m_program ? 1 : 0) << " view=" << (m_view ? 1 : 0)
                << " hasPending=" << hasPendingFrame() << " imgNull=" << m_pendingImage.isNull()
                << " bytesEmpty=" << m_pendingTextureBytes.isEmpty()
                << " recon=" << m_pendingTextureFromGpuRecon << " amaze=" << m_pendingTextureFromGpuAmaze
                << " dirty=" << m_textureDirty << " reconRefused=" << reconRefused;
        return;
    }

    const QRectF targetRect = targetRectInViewport();
    if ( targetRect.isEmpty() )
    {
        m_texturePresentationActive = false;
        return;
    }

    const qreal dpr = devicePixelRatioF();
    const float fbWidth = static_cast<float>(width() * dpr);
    const float fbHeight = static_cast<float>(height() * dpr);
    if ( fbWidth <= 0.0f || fbHeight <= 0.0f )
    {
        m_texturePresentationActive = false;
        return;
    }
    const float leftPx = static_cast<float>(targetRect.left() * dpr);
    const float topPx = static_cast<float>(targetRect.top() * dpr);
    const float rightPx = static_cast<float>((targetRect.left() + targetRect.width()) * dpr);
    const float bottomPx = static_cast<float>((targetRect.top() + targetRect.height()) * dpr);

    GLfloat vertices[16];
    std::copy(std::begin(kQuadVertices), std::end(kQuadVertices), std::begin(vertices));
    vertices[0] = (leftPx / fbWidth) * 2.0f - 1.0f;
    vertices[1] = 1.0f - (topPx / fbHeight) * 2.0f;
    vertices[4] = (rightPx / fbWidth) * 2.0f - 1.0f;
    vertices[5] = vertices[1];
    vertices[8] = vertices[0];
    vertices[9] = 1.0f - (bottomPx / fbHeight) * 2.0f;
    vertices[12] = vertices[4];
    vertices[13] = vertices[9];

    m_program->bind();
    // reconRefused above already proved reconLutsReady==true whenever presentingReconTexture
    // is true (otherwise paintGL returned before reaching here) -- reuse it instead of
    // re-evaluating the same predicate for that case.
    const bool previewProcessingReady = presentingReconTexture
        ? reconLutsReady
        : gpuPreviewProcessingLutTextureSetReady(m_lutSet, m_presentationOptions.previewProcessing);
    m_program->setUniformValue("frameTexture", 0);
    GpuPreviewProcessingDisplayUniforms displayUniforms;
    displayUniforms.textureSize = QVector2D(static_cast<float>(pendingWidth()),
                                            static_cast<float>(pendingHeight()));
    displayUniforms.frameTextureMode = m_textureIsBayer16 ? 1 : 0;
    displayUniforms.samplingMode = static_cast<int>(m_presentationOptions.samplingMode);
    displayUniforms.zebraEnabled = m_presentationOptions.showZebras;
    displayUniforms.zebraUnderThreshold = m_presentationOptions.zebraUnderThreshold;
    displayUniforms.zebraOverThreshold = m_presentationOptions.zebraOverThreshold;
    m_texture->bind(0);
    gpuPreviewProcessingBindDisplayUniformsAndTextures(
        m_program, m_presentationOptions.previewProcessing, m_lutSet,
        displayUniforms, previewProcessingReady);

    const int posLoc = m_program->attributeLocation("position");
    const int texLoc = m_program->attributeLocation("texCoord");
    m_program->enableAttributeArray(posLoc);
    m_program->enableAttributeArray(texLoc);
    m_program->setAttributeArray(posLoc, GL_FLOAT, vertices, 2, 4 * sizeof(GLfloat));
    m_program->setAttributeArray(texLoc, GL_FLOAT, vertices + 2, 2, 4 * sizeof(GLfloat));

    glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);

    m_program->disableAttributeArray(posLoc);
    m_program->disableAttributeArray(texLoc);
    m_texture->release();
    gpuPreviewProcessingReleaseDisplayTextures(m_lutSet, previewProcessingReady);
    m_program->release();
    m_texturePresentationActive = true;

    // Bug A on-screen present A/B (V4): force the dGPU to flush/complete this frame
    // before Qt blits the FBO and presents it, in case the Optimus cross-adapter
    // copy is racing an incomplete frame. Inert unless MLVAPP_VIEWPORT_AB_FLUSH set.
    switch ( viewportAbFlushMode() )
    {
        case 1: glFlush(); break;
        case 2: glFinish(); break;
        case 3: glFlush(); glFinish(); break;
        default: break;
    }
}

void GpuDisplayViewport::resizeGL(int, int)
{
}

GpuDisplayViewport *GpuDisplayViewport::from(QGraphicsView *view)
{
    if ( !view ) return nullptr;
    return qobject_cast<GpuDisplayViewport *>(view->viewport());
}

const GpuDisplayViewport *GpuDisplayViewport::from(const QGraphicsView *view)
{
    return from(const_cast<QGraphicsView *>(view));
}

void GpuDisplayViewport::cleanupGLResources()
{
    m_texturePresentationActive = false;

    QOpenGLContext *glContext = context();
    if ( !glContext )
    {
        destroyTexture();
        destroyProcessingTextures();
        if ( m_program )
        {
            delete m_program;
            m_program = nullptr;
        }
        return;
    }

    const bool needsCurrent = QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (makeCurrent(), true) : false;

    destroyTexture();
    destroyProcessingTextures();
    if ( m_program )
    {
        delete m_program;
        m_program = nullptr;
    }

    if ( madeCurrent ) doneCurrent();
}

void GpuDisplayViewport::setFallbackItem(QGraphicsPixmapItem *item)
{
    m_fallbackItem = item;
}

void GpuDisplayViewport::setPresentedImage(const QImage &image, const PresentationOptions &options)
{
    if ( viewportPresentDiagEnabled() )
        qInfo().nospace() << "viewport_diag setPresentedImage imgNull=" << image.isNull()
            << " w=" << image.width() << " h=" << image.height()
            << " fmt=" << static_cast<int>(image.format());
    m_pendingImage = image.format() == QImage::Format_RGBA8888
        ? image.copy()
        : image.convertToFormat(QImage::Format_RGBA8888);
    m_pendingTextureBytes.clear();
    m_pendingTextureFromGpuAmaze = false;
    m_pendingTextureFromGpuRecon = false;
    m_pendingTextureIsBayer16 = false;
    m_pendingTextureWidth = m_pendingImage.width();
    m_pendingTextureHeight = m_pendingImage.height();
    m_pendingTextureIs16Bit = false;
    m_textureDirty = true;
    setPresentationOptions(options);
    m_texturePresentationActive = false;
    if ( m_fallbackItem ) m_fallbackItem->setVisible(false);
    update();
}

void GpuDisplayViewport::setPresentedRgb16(const uint16_t *imageData,
                                           int width,
                                           int height,
                                           const PresentationOptions &options)
{
    const size_t pixelCount = static_cast<size_t>(width) * static_cast<size_t>(height);
    const size_t bytesNeeded = pixelCount * 4u * sizeof(uint16_t);
    m_pendingTextureBytes.resize(static_cast<int>(bytesNeeded));

    uint16_t * rgba = reinterpret_cast<uint16_t *>(m_pendingTextureBytes.data());
    for ( size_t i = 0; i < pixelCount; ++i )
    {
        rgba[i * 4 + 0] = imageData[i * 3 + 0];
        rgba[i * 4 + 1] = imageData[i * 3 + 1];
        rgba[i * 4 + 2] = imageData[i * 3 + 2];
        rgba[i * 4 + 3] = 65535;
    }

    suppressUniformTopMagentaBandRgb16(rgba, width, height);

    m_pendingImage = QImage();
    m_pendingTextureFromGpuAmaze = false;
    m_pendingTextureFromGpuRecon = false;
    m_pendingTextureIsBayer16 = false;
    m_pendingTextureWidth = width;
    m_pendingTextureHeight = height;
    m_pendingTextureIs16Bit = true;
    m_textureDirty = true;
    setPresentationOptions(options);
    m_texturePresentationActive = false;
    if ( m_fallbackItem ) m_fallbackItem->setVisible(false);
    update();
}

void GpuDisplayViewport::setPresentedBayer16(const uint16_t *imageData,
                                             int width,
                                             int height,
                                             const PresentationOptions &options)
{
    const size_t pixelCount = static_cast<size_t>(width) * static_cast<size_t>(height);
    const size_t bytesNeeded = pixelCount * sizeof(uint16_t);
    m_pendingTextureBytes.resize(static_cast<int>(bytesNeeded));
    std::memcpy(m_pendingTextureBytes.data(), imageData, bytesNeeded);

    m_pendingImage = QImage();
    m_pendingTextureFromGpuAmaze = false;
    m_pendingTextureFromGpuRecon = false;
    m_pendingTextureIsBayer16 = true;
    m_pendingTextureWidth = width;
    m_pendingTextureHeight = height;
    m_pendingTextureIs16Bit = true;
    m_textureDirty = true;
    setPresentationOptions(options);
    m_texturePresentationActive = false;
    if ( m_fallbackItem ) m_fallbackItem->setVisible(false);
    update();
}

bool GpuDisplayViewport::setPresentedGpuPlaybackReconTexture(
    const uint16_t *rawInputBayer14,
    size_t rawInputBayer14Words,
    const llrpGpuPlaybackReconState_t *state,
    const PresentationOptions &options,
    QString *reason,
    llrpGpuPlaybackReconTiming_t *timing)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        m_pendingTextureFromGpuRecon = false;
        m_texturePresentationActive = false;
        return false;
    };

    if ( !rawInputBayer14 || !state || !state->valid || state->width <= 0 || state->height <= 0 )
    {
        return fail(QStringLiteral("GPU playback recon texture-present input is invalid"));
    }

    const int width = state->width;
    const int height = state->height;
    const size_t expectedWords =
        static_cast<size_t>(width) * static_cast<size_t>(height);
    if ( rawInputBayer14Words < expectedWords )
    {
        return fail(QStringLiteral("GPU playback recon texture-present Bayer input is incomplete"));
    }

    QElapsedTimer wallTimer;
    wallTimer.start();
    auto elapsedMs = [&wallTimer]() -> double
    {
        return static_cast<double>(wallTimer.nsecsElapsed()) / 1000000.0;
    };
    double contextMs = 0.0;
    double setupMs = 0.0;
    double reconWallMs = 0.0;
    double postMs = 0.0;

    QOpenGLContext *glContext = context();
    if ( !glContext )
    {
        return fail(QStringLiteral("GPU playback recon texture-present requires an initialized viewport OpenGL context"));
    }

    const double contextStartMs = elapsedMs();
    const bool needsCurrent = QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (makeCurrent(), true) : false;
    contextMs = elapsedMs() - contextStartMs;

    const double setupStartMs = elapsedMs();
    ensureProgram();
    if ( !m_program )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral("GPU playback recon texture-present shader setup failed"));
    }

    setPresentationOptions(options);
    updateProcessingTexturesIfNeeded();

    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 4, fable minor): this raw-Bayer16
    // route feeds the same shared display shader/LUT set as the AMaZE route, whose
    // submit path already refuses here (see setPresentedGpuPlaybackReconAmazePostWbTexture
    // above) -- this route was missing the same gate, so an unready LUT set only refused
    // at paintGL's draw-time re-check, AFTER this function had already returned true and
    // MainWindow had recorded a successful present with no fallback. Refuse up front, same
    // as the AMaZE route, so MainWindow's existing fallback runs instead.
    if ( !gpuPreviewProcessingLutTextureSetReady(m_lutSet, options.previewProcessing) )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral(
            "GPU playback recon texture-present refused: LUT texture upload failed "
            "for a linear post-WB-undo texture (trace=gpu_viewport_recon_raw_lut_upload_failed)"));
    }

    if ( !m_texture
      || m_texture->width() != width
      || m_texture->height() != height
      || !m_textureIs16Bit
      || !m_textureIsBayer16 )
    {
        destroyTexture();
        m_texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
        m_texture->setFormat(QOpenGLTexture::R16_UNorm);
        m_texture->setSize(width, height);
        m_texture->setMipLevels(1);
        m_texture->allocateStorage(QOpenGLTexture::Red, QOpenGLTexture::UInt16);
        m_texture->setWrapMode(QOpenGLTexture::ClampToEdge);
        m_textureIs16Bit = true;
        m_textureIsBayer16 = true;
    }
    applySamplingMode();
    setupMs = elapsedMs() - setupStartMs;
    m_gpuReconSourceTextureCurrent = false;

    int rc = -1;
    llrpGpuPlaybackReconTiming_t localTiming;
    memset(&localTiming, 0, sizeof(localTiming));
    llrpGpuPlaybackReconTiming_t *timingTarget = timing ? timing : &localTiming;
    const double reconStartMs = elapsedMs();
    const bool ok =
        llrpGpuPlaybackReconRunGlTexture(state,
                                         rawInputBayer14,
                                         expectedWords * sizeof(uint16_t),
                                         m_texture->textureId(),
                                         &rc,
                                         timingTarget) != 0;
    reconWallMs = elapsedMs() - reconStartMs;
    if ( !ok )
    {
        destroyTexture();
        m_pendingTextureFromGpuRecon = false;
        if ( madeCurrent ) doneCurrent();
        if ( rc == LLRP_GPU_PLAYBACK_RECON_RC_UNSUPPORTED_STATE )
        {
            return fail(QStringLiteral(
                "GPU playback recon CUDA-to-GL texture handoff skipped for unsupported live Dual ISO state (rc=%1)").arg(rc));
        }
        return fail(QStringLiteral(
            "GPU playback recon CUDA-to-GL texture handoff failed (rc=%1)").arg(rc));
    }

    const double postStartMs = elapsedMs();
    m_pendingImage = QImage();
    m_pendingTextureBytes.clear();
    m_pendingTextureWidth = width;
    m_pendingTextureHeight = height;
    m_pendingTextureIs16Bit = true;
    m_pendingTextureIsBayer16 = true;
    m_pendingTextureFromGpuAmaze = false;
    m_pendingTextureFromGpuRecon = true;
    m_gpuReconSourceTextureCurrent = false;
    m_textureDirty = false;
    m_samplingModeDirty = false;
    m_texturePresentationActive = false;
    if ( m_fallbackItem ) m_fallbackItem->setVisible(false);
    if ( madeCurrent ) doneCurrent();
    update();
    postMs = elapsedMs() - postStartMs;
    if ( timingTarget )
    {
        timingTarget->wall_ms = elapsedMs();
        timingTarget->host_gap_ms = timingTarget->wall_ms - timingTarget->total_ms;
        timingTarget->context_ms = contextMs;
        timingTarget->setup_ms = setupMs;
        timingTarget->recon_wall_ms = reconWallMs;
        timingTarget->amaze_wall_ms = 0.0;
        timingTarget->post_ms = postMs;
    }
    return true;
}

bool GpuDisplayViewport::setPresentedGpuPlaybackReconAmazePostWbTexture(
    const uint16_t *rawInputBayer14,
    size_t rawInputBayer14Words,
    const llrpGpuPlaybackReconState_t *state,
    int blackLevel,
    const double wbMultipliers[3],
    const PresentationOptions &options,
    QString *reason,
    llrpGpuPlaybackReconTiming_t *timing,
    QString *handoffMode,
    bool validationProbeTexture,
    const uint16_t *retainedDeviceBayer16,
    int retainedDeviceWidth,
    int retainedDeviceHeight)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        if ( handoffMode ) handoffMode->clear();
        m_pendingTextureFromGpuAmaze = false;
        m_pendingTextureFromGpuRecon = false;
        m_texturePresentationActive = false;
        return false;
    };

    if ( !state
      || !state->valid
      || state->width <= 0
      || state->height <= 0
      || !wbMultipliers )
    {
        return fail(QStringLiteral("GPU playback recon AMaZE texture-present input is invalid"));
    }

    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 2): same gate as
    // GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture -- this texture is
    // the post-WB-undo linear camera RGB output of AMaZE, not display-referred. Without
    // usable LUTs the shared display shader would still draw it, just with
    // previewProcessingEnabled=0 (passthrough-equivalent in-shader), which is the same
    // dark/green-cast regression this fix line exists to close. Refuse up front rather
    // than let the in-shader disable stand in for a real refusal. MainWindow passes the
    // same task.gpuPresentationOptions to this route as to the window route today
    // (MainWindow.cpp), so this is not reachable in production; it closes the gap for
    // any future/direct caller.
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
            "GPU playback recon AMaZE texture-present refused: preview-processing options/LUTs "
            "are not ready for a linear post-WB-undo texture "
            "(trace=gpu_viewport_recon_missing_processing_options)"));
    }

    const int width = state->width;
    const int height = state->height;
    const bool retainedDeviceValid =
        retainedDeviceBayer16
        && retainedDeviceWidth == width
        && retainedDeviceHeight == height
        && !validationProbeTexture;
    const size_t expectedWords =
        static_cast<size_t>(width) * static_cast<size_t>(height);
    if ( !rawInputBayer14 && !retainedDeviceValid )
    {
        return fail(QStringLiteral("GPU playback recon AMaZE texture-present input is invalid"));
    }
    if ( !retainedDeviceValid && rawInputBayer14Words < expectedWords )
    {
        return fail(QStringLiteral("GPU playback recon AMaZE texture-present Bayer input is incomplete"));
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
        return fail(QStringLiteral("GPU playback recon AMaZE texture-present requires an initialized viewport OpenGL context"));
    }

    const double contextStartMs = elapsedMs();
    const bool needsCurrent = QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (makeCurrent(), true) : false;
    contextMs = elapsedMs() - contextStartMs;

    const double setupStartMs = elapsedMs();
    ensureProgram();
    if ( !m_program )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral("GPU playback recon AMaZE texture-present shader setup failed"));
    }

    setPresentationOptions(options);
    updateProcessingTexturesIfNeeded();

    // FAIL CLOSED (GPU-TEXNR-S1-DARK-GREEN-1 round 3, sol major): the options-usable
    // check above only proves the LUT *source bytes* were big enough to attempt an
    // upload -- it says nothing about whether updateProcessingTexturesIfNeeded() (via
    // gpuPreviewProcessingUpdateLutTextureSet) actually got real GL textures created and
    // uploaded. Re-check the SAME shared readiness predicate the window's equivalent
    // submit path checks (GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture)
    // and refuse here too, before any recon/AMaZE GL work runs, rather than letting a
    // failed upload reach paintGL's draw-time re-check as the only backstop.
    if ( !gpuPreviewProcessingLutTextureSetReady(m_lutSet, previewProcessing) )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral(
            "GPU playback recon AMaZE texture-present refused: LUT texture upload failed "
            "for a linear post-WB-undo texture (trace=gpu_viewport_recon_lut_upload_failed)"));
    }

    if ( !m_texture
      || m_texture->width() != width
      || m_texture->height() != height
      || !m_textureIs16Bit
      || m_textureIsBayer16 )
    {
        destroyTexture();
        m_texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
        m_texture->setFormat(QOpenGLTexture::RGBA16_UNorm);
        m_texture->setSize(width, height);
        m_texture->setMipLevels(1);
        m_texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16);
        m_texture->setWrapMode(QOpenGLTexture::ClampToEdge);
        m_textureIs16Bit = true;
        m_textureIsBayer16 = false;
    }
    if ( !m_gpuReconSourceTexture
      || m_gpuReconSourceTexture->width() != width
      || m_gpuReconSourceTexture->height() != height )
    {
        delete m_gpuReconSourceTexture;
        m_gpuReconSourceTexture = new QOpenGLTexture(QOpenGLTexture::Target2D);
        m_gpuReconSourceTexture->setFormat(QOpenGLTexture::R16_UNorm);
        m_gpuReconSourceTexture->setSize(width, height);
        m_gpuReconSourceTexture->setMipLevels(1);
        m_gpuReconSourceTexture->allocateStorage(QOpenGLTexture::Red, QOpenGLTexture::UInt16);
        m_gpuReconSourceTexture->setWrapMode(QOpenGLTexture::ClampToEdge);
    }
    applySamplingMode();
    setupMs = elapsedMs() - setupStartMs;
    m_gpuReconSourceTextureCurrent = false;

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
          && deviceWidth == width
          && deviceHeight == height )
        {
            const double directAmazeStartMs = elapsedMs();
            const bool directAmazeOk =
                gpuAmazeDebayerRenderPostWbGlTextureFromDeviceBayer16(
                    deviceBayer16,
                    m_texture->textureId(),
                    width,
                    height,
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
                    reconWallMs = directReconWallMs;
                    amazeWallMs = directAmazeWallMs;
                    reconOk = true;
                    amazeOk = true;
                    handoffModeValue = retainedDeviceValid
                        ? QStringLiteral("retained_device_bayer16")
                        : QStringLiteral("direct_device_bayer16");
                    m_gpuReconSourceTextureCurrent = validationProbeTexture;
                }
                else
                {
                    directFailureReason =
                        QStringLiteral(
                            "GPU playback recon direct device proof texture render failed (rc=%1)")
                            .arg(validationProofRc);
                }
            }
            else
            {
                directFailureReason = directAmazeReason.isEmpty()
                    ? QStringLiteral("GPU AMaZE direct device texture-present failed")
                    : directAmazeReason;
            }
        }
        else
        {
            directFailureReason =
                QStringLiteral("GPU playback recon direct device handoff failed (recon_rc=%1)")
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
                width,
                height,
                blackLevel,
                wbMultipliers,
                &amazeReason,
                &amazeRenderer,
                &amazeTiming);
        amazeWallMs = elapsedMs() - amazeStartMs;
        if ( reconOk && amazeOk )
        {
            handoffModeValue = QStringLiteral("gl_r16_bridge");
            m_gpuReconSourceTextureCurrent = true;
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
        m_pendingTextureFromGpuAmaze = false;
        m_pendingTextureFromGpuRecon = false;
        m_pendingTextureIsBayer16 = false;
        if ( madeCurrent ) doneCurrent();
        if ( rc == LLRP_GPU_PLAYBACK_RECON_RC_UNSUPPORTED_STATE )
        {
            return fail(QStringLiteral(
                "GPU playback recon AMaZE texture handoff skipped for unsupported live Dual ISO state (rc=%1)").arg(rc));
        }
        if ( reconOk && !amazeReason.isEmpty() )
        {
            return fail(amazeReason);
        }
        return fail(QStringLiteral(
            "GPU playback recon AMaZE texture handoff failed (recon_rc=%1)").arg(rc));
    }

    const double postStartMs = elapsedMs();
    m_pendingImage = QImage();
    m_pendingTextureBytes.clear();
    m_pendingTextureWidth = width;
    m_pendingTextureHeight = height;
    m_pendingTextureIs16Bit = true;
    m_pendingTextureIsBayer16 = false;
    m_pendingTextureFromGpuAmaze = false;
    m_pendingTextureFromGpuRecon = true;
    m_textureDirty = false;
    m_samplingModeDirty = false;
    m_texturePresentationActive = false;
    if ( m_fallbackItem ) m_fallbackItem->setVisible(false);
    if ( madeCurrent ) doneCurrent();
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

bool GpuDisplayViewport::setPresentedAmazePostWbTexture(const float *rawFrame,
                                                        int width,
                                                        int height,
                                                        int blackLevel,
                                                        const double wbMultipliers[3],
                                                        const PresentationOptions &options,
                                                        QString *reason,
                                                        QString *rendererDescription,
                                                        GpuAmazeDebayerBackendTiming *timing)
{
    auto fail = [&](const QString & why) -> bool
    {
        if ( reason ) *reason = why;
        m_pendingTextureFromGpuAmaze = false;
        m_pendingTextureFromGpuRecon = false;
        m_pendingTextureIsBayer16 = false;
        m_texturePresentationActive = false;
        return false;
    };

    if ( !rawFrame || width <= 0 || height <= 0 )
    {
        return fail(QStringLiteral("GPU AMaZE texture-present input is invalid"));
    }

    QOpenGLContext *glContext = context();
    if ( !glContext )
    {
        return fail(QStringLiteral("GPU AMaZE texture-present requires an initialized viewport OpenGL context"));
    }

    const bool needsCurrent = QOpenGLContext::currentContext() != glContext;
    const bool madeCurrent = needsCurrent ? (makeCurrent(), true) : false;

    ensureProgram();
    if ( !m_program )
    {
        if ( madeCurrent ) doneCurrent();
        return fail(QStringLiteral("GPU AMaZE texture-present shader setup failed"));
    }

    setPresentationOptions(options);
    updateProcessingTexturesIfNeeded();

    if ( !m_texture
      || m_texture->width() != width
      || m_texture->height() != height
      || !m_textureIs16Bit
      || m_textureIsBayer16 )
    {
        destroyTexture();
        m_texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
        m_texture->setFormat(QOpenGLTexture::RGBA16_UNorm);
        m_texture->setSize(width, height);
        m_texture->setMipLevels(1);
        m_texture->allocateStorage(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16);
        m_texture->setWrapMode(QOpenGLTexture::ClampToEdge);
        m_textureIs16Bit = true;
        m_textureIsBayer16 = false;
    }
    applySamplingMode();

    const bool ok = gpuAmazeDebayerRenderPostWbGlTexture(rawFrame,
                                                        m_texture->textureId(),
                                                        width,
                                                        height,
                                                        blackLevel,
                                                        wbMultipliers,
                                                        reason,
                                                        rendererDescription,
                                                        timing);
    if ( !ok )
    {
        destroyTexture();
        m_pendingTextureFromGpuAmaze = false;
        m_pendingTextureFromGpuRecon = false;
        m_pendingTextureIsBayer16 = false;
        if ( madeCurrent ) doneCurrent();
        return false;
    }

    m_pendingImage = QImage();
    m_pendingTextureBytes.clear();
    m_pendingTextureWidth = width;
    m_pendingTextureHeight = height;
    m_pendingTextureIs16Bit = true;
    m_pendingTextureIsBayer16 = false;
    m_pendingTextureFromGpuAmaze = true;
    m_pendingTextureFromGpuRecon = false;
    m_textureDirty = false;
    m_samplingModeDirty = false;
    m_texturePresentationActive = false;
    if ( m_fallbackItem ) m_fallbackItem->setVisible(false);
    if ( madeCurrent ) doneCurrent();
    update();
    return true;
}

void GpuDisplayViewport::clearPresentedImage()
{
    m_pendingImage = QImage();
    m_pendingTextureBytes.clear();
    m_pendingTextureWidth = 0;
    m_pendingTextureHeight = 0;
    m_pendingTextureIs16Bit = false;
    m_pendingTextureIsBayer16 = false;
    m_pendingTextureFromGpuAmaze = false;
    m_pendingTextureFromGpuRecon = false;
    m_textureDirty = false;
    m_samplingModeDirty = false;
    m_processingTexturesDirty = false;
    cleanupGLResources();
    if ( m_fallbackItem ) m_fallbackItem->setVisible(true);
    update();
}

void GpuDisplayViewport::updateTextureIfNeeded()
{
    if ( viewportPresentDiagEnabled() )
        qInfo().nospace() << "viewport_diag updateTex.enter dirty=" << m_textureDirty
            << " samp=" << m_samplingModeDirty << " proc=" << m_processingTexturesDirty
            << " hasPending=" << hasPendingFrame() << " imgNull=" << m_pendingImage.isNull()
            << " bytesEmpty=" << m_pendingTextureBytes.isEmpty()
            << " is16=" << m_pendingTextureIs16Bit << " bayer=" << m_pendingTextureIsBayer16
            << " recon=" << m_pendingTextureFromGpuRecon << " amaze=" << m_pendingTextureFromGpuAmaze
            << " tex=" << (m_texture ? 1 : 0) << " prog=" << (m_program ? 1 : 0);
    if ( !m_textureDirty && !m_samplingModeDirty && !m_processingTexturesDirty )
    {
        if ( viewportPresentDiagEnabled() )
            qInfo() << "viewport_diag updateTex.ret not-dirty (tex=" << (m_texture ? 1 : 0) << ")";
        if ( m_fallbackItem && m_texture ) m_fallbackItem->setVisible(false);
        return;
    }

    if ( !hasPendingFrame() )
    {
        if ( viewportPresentDiagEnabled() )
            qInfo() << "viewport_diag updateTex.ret no-pending-frame";
        destroyTexture();
        m_textureDirty = false;
        m_texturePresentationActive = false;
        if ( m_fallbackItem ) m_fallbackItem->setVisible(true);
        return;
    }

    ensureProgram();
    if ( !m_program )
    {
        if ( viewportPresentDiagEnabled() )
            qInfo() << "viewport_diag updateTex.ret no-program";
        m_texturePresentationActive = false;
        if ( m_fallbackItem ) m_fallbackItem->setVisible(true);
        return;
    }

    updateProcessingTexturesIfNeeded();
    if ( !m_texture
      || m_texture->width() != pendingWidth()
      || m_texture->height() != pendingHeight()
      || m_textureIs16Bit != m_pendingTextureIs16Bit
      || m_textureIsBayer16 != m_pendingTextureIsBayer16 )
    {
        destroyTexture();
        m_texture = new QOpenGLTexture(QOpenGLTexture::Target2D);
        if ( m_pendingTextureIsBayer16 )
        {
            m_texture->setFormat(QOpenGLTexture::R16_UNorm);
        }
        else
        {
            m_texture->setFormat(m_pendingTextureIs16Bit ? QOpenGLTexture::RGBA16_UNorm
                                                         : QOpenGLTexture::RGBA8_UNorm);
        }
        m_texture->setSize(pendingWidth(), pendingHeight());
        m_texture->setMipLevels(1);
        m_texture->allocateStorage(m_pendingTextureIsBayer16 ? QOpenGLTexture::Red
                                                             : QOpenGLTexture::RGBA,
                                   m_pendingTextureIs16Bit ? QOpenGLTexture::UInt16
                                                           : QOpenGLTexture::UInt8);
        m_texture->setWrapMode(QOpenGLTexture::ClampToEdge);
        m_textureIs16Bit = m_pendingTextureIs16Bit;
        m_textureIsBayer16 = m_pendingTextureIsBayer16;
    }

    applySamplingMode();
    if ( m_pendingTextureFromGpuAmaze || m_pendingTextureFromGpuRecon )
    {
        /* The CUDA backend already wrote the viewport-owned GL texture. */
    }
    else if ( m_pendingTextureIsBayer16 )
    {
        m_texture->setData(QOpenGLTexture::Red,
                           QOpenGLTexture::UInt16,
                           m_pendingTextureBytes.constData());
    }
    else if ( m_pendingTextureIs16Bit )
    {
        m_texture->setData(QOpenGLTexture::RGBA,
                           QOpenGLTexture::UInt16,
                           m_pendingTextureBytes.constData());
    }
    else
    {
        m_texture->setData(m_pendingImage);
    }
    m_textureDirty = false;

    if ( viewportPresentDiagEnabled() )
        qInfo().nospace() << "viewport_diag updateTex.uploaded recon=" << m_pendingTextureFromGpuRecon
            << " amaze=" << m_pendingTextureFromGpuAmaze << " bayer=" << m_pendingTextureIsBayer16
            << " is16=" << m_pendingTextureIs16Bit << " w=" << pendingWidth() << " h=" << pendingHeight();
    if ( m_fallbackItem ) m_fallbackItem->setVisible(false);
    if ( !m_loggedTexturePath )
    {
        qInfo() << "Experimental GPU viewport is presenting frames from a persistent OpenGL texture with shader-side sampling and display overlays.";
        m_loggedTexturePath = true;
    }
}

void GpuDisplayViewport::ensureProgram()
{
    gpuPreviewProcessingEnsureDisplayProgram(m_program, this);
}

void GpuDisplayViewport::destroyTexture()
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
    m_gpuReconSourceTextureCurrent = false;
    m_texturePresentationActive = false;
    m_samplingModeDirty = false;
    m_textureIs16Bit = false;
    m_textureIsBayer16 = false;
}

void GpuDisplayViewport::destroyProcessingTextures()
{
    gpuPreviewProcessingDestroyLutTextureSet(m_lutSet);
}

void GpuDisplayViewport::setPresentationOptions(const PresentationOptions &options)
{
    const bool samplingChanged =
        m_presentationOptions.samplingMode != options.samplingMode;
    const bool processingSignatureChanged =
        m_presentationOptions.previewProcessing.enabled != options.previewProcessing.enabled
        || m_presentationOptions.previewProcessing.signature
            != options.previewProcessing.signature;
    m_samplingModeDirty = m_samplingModeDirty || samplingChanged;
    if ( processingSignatureChanged )
    {
        m_processingTexturesDirty = true;
    }
    m_presentationOptions = options;
}

void GpuDisplayViewport::updateProcessingTexturesIfNeeded()
{
    if ( !m_processingTexturesDirty && !m_presentationOptions.previewProcessing.enabled )
    {
        return;
    }

    gpuPreviewProcessingUpdateLutTextureSet(m_lutSet, m_presentationOptions.previewProcessing);
    m_processingTexturesDirty = false;
}

void GpuDisplayViewport::applySamplingMode()
{
    if ( !m_texture ) return;

    const bool useNearest = m_textureIsBayer16 || m_presentationOptions.samplingMode == SamplingNearest;
    const bool useBicubic = m_presentationOptions.samplingMode == SamplingBicubic;
    const QOpenGLTexture::Filter filter = (useNearest || useBicubic)
        ? QOpenGLTexture::Nearest
        : QOpenGLTexture::Linear;
    m_texture->setMinMagFilters(filter, filter);
    m_samplingModeDirty = false;
}

QRectF GpuDisplayViewport::targetRectInViewport() const
{
    if ( !m_view ) return QRectF();

    const QRectF viewSceneRect = m_view->sceneRect();
    QRectF itemRect;
    if ( m_fallbackItem )
    {
        itemRect = m_fallbackItem->sceneBoundingRect();
    }

    QRectF sceneRect = itemRect.isEmpty() ? viewSceneRect : itemRect;
    if ( hasPendingFrame() && !viewSceneRect.isEmpty()
         && (itemRect.isEmpty() || !rectsNearlyEqual(viewSceneRect, itemRect)) )
    {
        sceneRect = viewSceneRect;
    }

    if ( sceneRect.isEmpty() ) return QRectF();

    // GL vertices describe continuous edges. Integer QPolygon/QRect bounds
    // include both endpoints and would expand a 4x4 frame to a 5x5 quad.
    return m_view->viewportTransform().mapRect(sceneRect);
}

int GpuDisplayViewport::pendingWidth() const
{
    return (m_pendingTextureIs16Bit || m_pendingTextureIsBayer16) ? m_pendingTextureWidth : m_pendingImage.width();
}

int GpuDisplayViewport::pendingHeight() const
{
    return (m_pendingTextureIs16Bit || m_pendingTextureIsBayer16) ? m_pendingTextureHeight : m_pendingImage.height();
}

bool GpuDisplayViewport::hasPendingFrame() const
{
    if ( m_pendingTextureFromGpuAmaze || m_pendingTextureFromGpuRecon )
    {
        return m_pendingTextureWidth > 0 && m_pendingTextureHeight > 0 && m_texture != nullptr;
    }
    return (m_pendingTextureIs16Bit || m_pendingTextureIsBayer16)
        ? (!m_pendingTextureBytes.isEmpty() && m_pendingTextureWidth > 0 && m_pendingTextureHeight > 0)
        : !m_pendingImage.isNull();
}
