#include "../../platform/qt/ColorToolButton.h"
#include "../../platform/qt/DualIsoPlaybackPolicy.h"
#include "../../platform/qt/DualIsoPatternMapping.h"
#include "../../platform/qt/GpuDisplayViewport.h"
#include "../../platform/qt/GpuDisplayWindow.h"
#include "../../platform/qt/GpuPreviewProcessing.h"
#include "../../platform/qt/Histogram.h"
#include "../../platform/qt/MainWindowGpuPreviewPolicy.h"
#include "../../platform/qt/ScopesLabel.h"
#include "../../platform/qt/VectorScope.h"
#include "../../platform/qt/WaveFormMonitor.h"
#include "../../platform/qt/ZebraThresholds.h"
#include "../common/image_regression.h"
#include "../common/repo_paths.h"
#include "../common/test_runtime.h"

#include <QApplication>
#include <QFile>
#include <QFontInfo>
#include <QFrame>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QMap>
#include <QOffscreenSurface>
#include <QOpenGLContext>
#include <QPalette>
#include <QPaintEvent>
#include <QScopeGuard>
#include <QScreen>
#include <QScrollBar>
#include <QVBoxLayout>
#include <QWidget>
#include <QWindow>
#include <QtTest/QtTest>

#include <cmath>
#include <cstring>
#include <memory>
#include <vector>

#ifdef Q_OS_WIN
#include <windows.h>
#endif

// MLV_REQUIRE_GL_TESTS=1 turns an offscreen-platform skip into a hard failure, for the GL
// window capture/race tests that must not silently skip on a run that is supposed to have
// a real, creatable OpenGL context (a hosted pilot on real hardware). Must NOT be set in
// the offscreen CI job -- it has no creatable GL context and skipping there is correct.
// A plain macro (not a helper function): the skip/fail primitives below both return from
// the calling test function itself, which a helper function could not do on its behalf.
#define MLV_SKIP_OR_FAIL_IF_OFFSCREEN(reason) \
    do { \
        if (QGuiApplication::platformName() == QStringLiteral("offscreen")) { \
            if (qEnvironmentVariable("MLV_REQUIRE_GL_TESTS") == QStringLiteral("1")) { \
                QTest::qFail(reason, __FILE__, __LINE__); \
                return; \
            } \
            QTest::qSkip(reason, __FILE__, __LINE__); \
            return; \
        } \
    } while (0)

// A framebuffer-capture attempt can still fail at runtime on a platform that DID create a
// GL context (so MLV_SKIP_OR_FAIL_IF_OFFSCREEN above already passed) -- e.g. a transient
// readback error. Under MLV_REQUIRE_GL_TESTS=1 (a hosted pilot on real hardware that is
// supposed to have a working GL capture path) that must be a hard failure too, not a silent
// skip that could mask a real regression.
#define MLV_SKIP_OR_FAIL_IF_READBACK_FAILED(reason) \
    do { \
        if (qEnvironmentVariable("MLV_REQUIRE_GL_TESTS") == QStringLiteral("1")) { \
            QTest::qFail(reason, __FILE__, __LINE__); \
            return; \
        } \
        QTest::qSkip(reason, __FILE__, __LINE__); \
        return; \
    } while (0)

namespace {

QImage presenter_expected_orientation(const QImage &submitted)
{
    // Display coordinates preserve the submitted image's top row, just like
    // the raster pixmap fallback. Offscreen GL passes have a separate origin.
    return submitted;
}

QMap<QString, QString> load_expected_hashes()
{
    const QString manifest_path = repo_file_path(QStringLiteral("tests/fixtures/golden/gui_hashes.json"));
    QFile file(manifest_path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return {};
    }

    const QJsonDocument document = QJsonDocument::fromJson(file.readAll());
    if (!document.isObject()) {
        return {};
    }

    QMap<QString, QString> hashes;
    const QJsonObject object = document.object();
    for (auto it = object.begin(); it != object.end(); ++it) {
        hashes.insert(it.key(), it.value().toString());
    }
    return hashes;
}

void set_raw_pixel(std::vector<uint8_t> &raw,
                   int width,
                   int x,
                   int y,
                   uint8_t red,
                   uint8_t green,
                   uint8_t blue)
{
    const int offset = ((y * width) + x) * 3;
    raw[static_cast<std::size_t>(offset + 0)] = red;
    raw[static_cast<std::size_t>(offset + 1)] = green;
    raw[static_cast<std::size_t>(offset + 2)] = blue;
}

std::vector<uint8_t> make_scope_raw_pattern(int width, int height)
{
    std::vector<uint8_t> raw(static_cast<std::size_t>(width) * static_cast<std::size_t>(height) * 3u, 0);

    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            uint8_t red = 0;
            uint8_t green = 0;
            uint8_t blue = 0;

            if (y < height / 2) {
                if (x < width / 4) {
                    red = 128;
                } else if (x < width / 2) {
                    green = 255;
                } else if (x < (width * 3) / 4) {
                    blue = 255;
                } else {
                    red = 128;
                    green = 128;
                    blue = 128;
                }
            } else {
                if (x < width / 4) {
                    red = 0;
                    green = 0;
                    blue = 0;
                } else if (x < width / 2) {
                    red = 255;
                    green = 255;
                } else if (x < (width * 3) / 4) {
                    green = 255;
                    blue = 255;
                } else {
                    red = 255;
                    blue = 255;
                }
            }

            set_raw_pixel(raw, width, x, y, red, green, blue);
        }
    }

    return raw;
}

QImage make_scope_source_image()
{
    const int width = 16;
    const int height = 8;
    const std::vector<uint8_t> raw = make_scope_raw_pattern(width, height);

    QImage image(width, height, QImage::Format_RGB888);
    for (int y = 0; y < height; ++y) {
        uint8_t *line = image.scanLine(y);
        const uint8_t *raw_line = raw.data() + (y * width * 3);
        memcpy(line, raw_line, static_cast<std::size_t>(width) * 3u);
    }
    return image;
}

QImage make_presenter_pattern()
{
    QImage image(4, 4, QImage::Format_RGB888);
    const QRgb colors[16] = {
        qRgb(255, 0, 0),     qRgb(0, 255, 0),     qRgb(0, 0, 255),     qRgb(255, 255, 255),
        qRgb(0, 0, 0),       qRgb(255, 255, 0),   qRgb(0, 255, 255),   qRgb(255, 0, 255),
        qRgb(255, 255, 255), qRgb(0, 0, 255),     qRgb(0, 255, 0),     qRgb(255, 0, 0),
        qRgb(255, 0, 255),   qRgb(0, 255, 255),   qRgb(255, 255, 0),   qRgb(0, 0, 0),
    };

    int index = 0;
    for (int y = 0; y < image.height(); ++y) {
        for (int x = 0; x < image.width(); ++x) {
            image.setPixel(x, y, colors[index++]);
        }
    }

    return image;
}

QImage apply_cpu_zebras(const QImage &submitted)
{
    QImage zebra = submitted.convertToFormat(QImage::Format_RGB888);
    for (int y = 0; y < zebra.height(); ++y) {
        uint8_t *line = zebra.scanLine(y);
        for (int x = 0; x < zebra.width(); ++x) {
            uint8_t *pixel = line + (x * 3);
            const int max_channel = qMax(pixel[0], qMax(pixel[1], pixel[2]));
            const int min_channel = qMin(pixel[0], qMin(pixel[1], pixel[2]));
            const int lightness = (max_channel + min_channel) / 2;
            if (lightness >= preview_zebra::kOverThreshold8Bit) {
                pixel[0] = 255;
                pixel[1] = 0;
                pixel[2] = 0;
            }
            if (lightness <= preview_zebra::kUnderThreshold8Bit) {
                pixel[0] = 0;
                pixel[1] = 0;
                pixel[2] = 255;
            }
        }
    }
    return zebra;
}

std::vector<uint16_t> make_presenter_pattern_rgb16()
{
    const QImage image = make_presenter_pattern();
    std::vector<uint16_t> rgb16(static_cast<std::size_t>(image.width()) * static_cast<std::size_t>(image.height()) * 3u, 0);

    for (int y = 0; y < image.height(); ++y) {
        for (int x = 0; x < image.width(); ++x) {
            const QRgb pixel = image.pixel(x, y);
            const int offset = ((y * image.width()) + x) * 3;
            rgb16[static_cast<std::size_t>(offset + 0)] = qRed(pixel) ? 65535 : 0;
            rgb16[static_cast<std::size_t>(offset + 1)] = qGreen(pixel) ? 65535 : 0;
            rgb16[static_cast<std::size_t>(offset + 2)] = qBlue(pixel) ? 65535 : 0;
        }
    }

    return rgb16;
}

QByteArray make_identity_lut_bytes()
{
    QByteArray lut(static_cast<int>(65536u * sizeof(uint16_t)), 0);
    uint16_t *values = reinterpret_cast<uint16_t *>(lut.data());
    for (int index = 0; index < 65536; ++index) {
        values[index] = static_cast<uint16_t>(index);
    }
    return lut;
}

QByteArray make_scaled_lut_bytes(float factor)
{
    QByteArray lut(static_cast<int>(65536u * sizeof(uint16_t)), 0);
    uint16_t *values = reinterpret_cast<uint16_t *>(lut.data());
    for (int index = 0; index < 65536; ++index) {
        const int scaled = static_cast<int>(std::lround(static_cast<double>(index) * factor));
        values[index] = static_cast<uint16_t>(qBound(0, scaled, 65535));
    }
    return lut;
}

GpuPreviewProcessingConfig make_synthetic_preview_processing_config()
{
    GpuPreviewProcessingConfig config;
    config.enabled = true;
    config.useCameraMatrix = true;
    config.applyGamutCompression = true;

    config.properWbMatrix[0] = 1.0f;
    config.properWbMatrix[1] = 0.04f;
    config.properWbMatrix[2] = 0.00f;
    config.properWbMatrix[3] = 0.02f;
    config.properWbMatrix[4] = 0.98f;
    config.properWbMatrix[5] = 0.02f;
    config.properWbMatrix[6] = 0.00f;
    config.properWbMatrix[7] = 0.05f;
    config.properWbMatrix[8] = 0.95f;

    config.rgbToY[0] = 0.2126729f;
    config.rgbToY[1] = 0.7151522f;
    config.rgbToY[2] = 0.0721750f;
    config.levelsLut = make_identity_lut_bytes();
    config.matrixLutR = make_scaled_lut_bytes(0.80f);
    config.matrixLutG = make_scaled_lut_bytes(1.05f);
    config.matrixLutB = make_scaled_lut_bytes(1.10f);
    config.gammaLut = make_identity_lut_bytes();
    config.signature = 0xBEEFull;
    return config;
}

QImage rgb16_to_qimage(const std::vector<uint16_t> &rgb16, int width, int height)
{
    QImage image(width, height, QImage::Format_RGB888);
    for (int y = 0; y < height; ++y) {
        uint8_t *line = image.scanLine(y);
        for (int x = 0; x < width; ++x) {
            const std::size_t base = (static_cast<std::size_t>(y) * static_cast<std::size_t>(width) + static_cast<std::size_t>(x)) * 3u;
            // Round normalized U16 to U8; a rounded shift can produce 256 and
            // wrap saturated highlights to zero when narrowed to uint8_t.
            line[x * 3 + 0] = static_cast<uint8_t>((rgb16[base + 0] + 128u) / 257u);
            line[x * 3 + 1] = static_cast<uint8_t>((rgb16[base + 1] + 128u) / 257u);
            line[x * 3 + 2] = static_cast<uint8_t>((rgb16[base + 2] + 128u) / 257u);
        }
    }
    return image;
}

QImage grab_gpu_viewport_framebuffer(QGraphicsView *view)
{
    auto *viewport = qobject_cast<GpuDisplayViewport *>(view->viewport());
    if (!viewport) {
        return QImage();
    }

    QImage framebuffer;
    for (int attempt = 0; attempt < 5; ++attempt) {
        QApplication::processEvents();
        viewport->update();
        viewport->repaint();
        QApplication::processEvents();
        framebuffer = viewport->grabFramebuffer();
        if (!framebuffer.isNull()) {
            break;
        }
        QTest::qWait(20);
    }
    return framebuffer;
}

QImage crop_presented_frame(QGraphicsView *view, QGraphicsPixmapItem *item)
{
    auto *viewport = qobject_cast<GpuDisplayViewport *>(view->viewport());
    if (!viewport) {
        return QImage();
    }

    QImage framebuffer;
    for (int attempt = 0; attempt < 5; ++attempt) {
        QApplication::processEvents();
        viewport->update();
        viewport->repaint();
        QApplication::processEvents();
        framebuffer = viewport->grabFramebuffer();
        if (!framebuffer.isNull()) {
            break;
        }
        QTest::qWait(20);
    }

    if (framebuffer.isNull()) {
        return QImage();
    }

    const QRect logical_rect =
        view->mapFromScene(item->sceneBoundingRect()).boundingRect().intersected(view->viewport()->rect());
    if (logical_rect.isEmpty()) {
        return QImage();
    }

    const qreal dpr = viewport->devicePixelRatioF();
    const QRect device_rect(qRound(logical_rect.x() * dpr),
                            qRound(logical_rect.y() * dpr),
                            qRound(logical_rect.width() * dpr),
                            qRound(logical_rect.height() * dpr));
    QImage cropped = framebuffer.copy(device_rect);
    if (cropped.isNull()) {
        return QImage();
    }

    if (device_rect.size() != logical_rect.size()) {
        cropped = cropped.scaled(logical_rect.size(), Qt::IgnoreAspectRatio, Qt::FastTransformation);
    }

    return image_regression::normalize_rgb888(cropped);
}

QImage crop_presented_scene_rect(QGraphicsView *view, const QRectF &scene_rect)
{
    auto *viewport = qobject_cast<GpuDisplayViewport *>(view->viewport());
    if (!viewport) {
        return QImage();
    }

    QImage framebuffer;
    for (int attempt = 0; attempt < 5; ++attempt) {
        QApplication::processEvents();
        viewport->update();
        viewport->repaint();
        QApplication::processEvents();
        framebuffer = viewport->grabFramebuffer();
        if (!framebuffer.isNull()) {
            break;
        }
        QTest::qWait(20);
    }

    if (framebuffer.isNull()) {
        return QImage();
    }

    const QRect logical_rect =
        view->mapFromScene(scene_rect).boundingRect().intersected(view->viewport()->rect());
    if (logical_rect.isEmpty()) {
        return QImage();
    }

    const qreal dpr = viewport->devicePixelRatioF();
    const QRect device_rect(qRound(logical_rect.x() * dpr),
                            qRound(logical_rect.y() * dpr),
                            qRound(logical_rect.width() * dpr),
                            qRound(logical_rect.height() * dpr));
    QImage cropped = framebuffer.copy(device_rect);
    if (cropped.isNull()) {
        return QImage();
    }

    if (device_rect.size() != logical_rect.size()) {
        cropped = cropped.scaled(logical_rect.size(), Qt::IgnoreAspectRatio, Qt::FastTransformation);
    }

    return image_regression::normalize_rgb888(cropped);
}

QImage trim_rounding_border(const QImage &image, const QSize &expected_size)
{
    QImage trimmed = image;
    if (trimmed.width() == expected_size.width() + 1) {
        trimmed = trimmed.copy(0, 0, expected_size.width(), trimmed.height());
    }
    if (trimmed.height() == expected_size.height() + 1) {
        trimmed = trimmed.copy(0, 0, trimmed.width(), expected_size.height());
    }
    return trimmed;
}

QImage normalize_scope_pixmap(const QPixmap &pixmap)
{
    QImage image = pixmap.toImage();
    const QSize logical_size = pixmap.deviceIndependentSize().toSize();
    if (logical_size.isValid() && logical_size != image.size()) {
        image = image.scaled(logical_size, Qt::IgnoreAspectRatio, Qt::SmoothTransformation);
    }
    return image_regression::normalize_rgb888(image);
}

QImage quantize_rgb888(const QImage &image, int quantum)
{
    QImage quantized = image_regression::normalize_rgb888(image);
    if (quantum <= 1) {
        return quantized;
    }

    for (int y = 0; y < quantized.height(); ++y) {
        uint8_t *line = quantized.scanLine(y);
        for (int x = 0; x < quantized.width(); ++x) {
            uint8_t *pixel = line + (x * 3);
            for (int channel = 0; channel < 3; ++channel) {
                pixel[channel] = static_cast<uint8_t>((pixel[channel] / quantum) * quantum);
            }
        }
    }

    return quantized;
}

QImage make_scopeslabel_scope_signature(const QImage &image)
{
    const QRect compare_rect(8, 4, image.width() - 16, image.height() - 8);
    QImage cropped = image.copy(compare_rect);
    cropped = cropped.scaled(64, 20, Qt::IgnoreAspectRatio, Qt::SmoothTransformation);
    return quantize_rgb888(cropped, 64);
}

void draw_scope_grid_lines(QImage &image, ScopesLabel::ScopeType type)
{
    QPainter painter(&image);
    QPen pen;
    pen.setStyle(Qt::DotLine);
    pen.setWidth(1);
    pen.setBrush(QColor(200, 200, 200, 96));
    painter.setPen(pen);

    const int width = image.width();
    const int height = image.height();
    if (type == ScopesLabel::ScopeHistogram) {
        painter.drawLine(width * 0.1, 0, width * 0.1, height - 1);
        painter.drawLine(width * 0.25, 0, width * 0.25, height - 1);
        painter.drawLine(width * 0.5, 0, width * 0.5, height - 1);
        painter.drawLine(width * 0.75, 0, width * 0.75, height - 1);
        painter.drawLine(width * 0.9, 0, width * 0.9, height - 1);
    } else if (type == ScopesLabel::ScopeWaveForm || type == ScopesLabel::ScopeRgbParade) {
        painter.drawLine(0, height * 0.1, width, height * 0.1);
        painter.drawLine(0, height * 0.25, width, height * 0.25);
        painter.drawLine(0, height * 0.5, width, height * 0.5);
        painter.drawLine(0, height * 0.75, width, height * 0.75);
        painter.drawLine(0, height * 0.9, width, height * 0.9);
    }
}

QImage render_expected_scope_label(const std::vector<uint8_t> &raw,
                                   int width,
                                   int height,
                                   bool under,
                                   bool over,
                                   ScopesLabel::ScopeType type)
{
    QImage scope_image;
    if (type == ScopesLabel::ScopeHistogram) {
        Histogram histogram;
        scope_image = histogram.getHistogramFromRaw(const_cast<uint8_t *>(raw.data()), width, height, under, over);
    } else if (type == ScopesLabel::ScopeWaveForm) {
        WaveFormMonitor waveform(width);
        scope_image = waveform.getWaveFormMonitorFromRaw(const_cast<uint8_t *>(raw.data()), width, height);
    } else if (type == ScopesLabel::ScopeRgbParade) {
        WaveFormMonitor waveform(width);
        scope_image = waveform.getParadeFromRaw(const_cast<uint8_t *>(raw.data()), width, height);
    } else if (type == ScopesLabel::ScopeVectorScope) {
        VectorScope vector_scope(511, 160);
        scope_image = vector_scope.getVectorScopeFromRaw(const_cast<uint8_t *>(raw.data()), width, height);
    } else {
        scope_image = QImage(511, 160, QImage::Format_RGB888);
        scope_image.fill(Qt::black);
    }

    QImage scaled = scope_image.scaled(511, 160, Qt::IgnoreAspectRatio, Qt::SmoothTransformation);
    draw_scope_grid_lines(scaled, type);
    return image_regression::normalize_rgb888(scaled);
}

QImage render_scopes_label_output(const std::vector<uint8_t> &raw,
                                  int width,
                                  int height,
                                  bool under,
                                  bool over,
                                  ScopesLabel::ScopeType type)
{
    ScopesLabel label;
    if (!QTest::qCompare(label.devicePixelRatioF(), qreal(1.0),
                        "scope fixture device-pixel ratio", "golden device-pixel ratio 1",
                        __FILE__, __LINE__)) {
        return QImage();
    }
    label.resize(511, 160);

    label.setScope(const_cast<uint8_t *>(raw.data()),
                   static_cast<uint16_t>(width),
                   static_cast<uint16_t>(height),
                   under,
                   over,
                   type);

    const QPixmap pixmap = label.pixmap();
    if (qEnvironmentVariableIntValue("MLVAPP_TEST_SCOPE_DIAGNOSTICS") == 1) {
        const QFontInfo font(label.font());
        qInfo() << "scope-fixture" << type << QGuiApplication::platformName()
                << "screenDpr" << label.screen()->devicePixelRatio()
                << "labelDpr" << label.devicePixelRatioF()
                << "labelSize" << label.size() << "pixmapSize" << pixmap.size()
                << "logicalSize" << pixmap.deviceIndependentSize()
                << "font" << font.family() << font.pixelSize()
                << "rawHash" << QString::fromStdString(image_regression::sha256_rgb888(pixmap.toImage()));
    }
    if (pixmap.isNull()) {
        return QImage();
    }

    return normalize_scope_pixmap(pixmap);
}

QGraphicsView *make_presenter_view(QGraphicsScene &scene,
                                   QGraphicsPixmapItem *item,
                                   const QSize &pattern_size)
{
    auto *view = new QGraphicsView(&scene);
    view->setFrameShape(QFrame::NoFrame);
    view->setAlignment(Qt::AlignLeft | Qt::AlignTop);
    view->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setSceneRect(QRectF(QPointF(0.0, 0.0), QSizeF(pattern_size)));
    item->setOffset(0.0, 0.0);
    view->resize(pattern_size.width() + 8, pattern_size.height() + 8);
    return view;
}

void assert_expected_hash(const QMap<QString, QString> &expected_hashes,
                          const QString &key,
                          const QImage &image)
{
    const QString actual_hash = QString::fromStdString(image_regression::sha256_rgb888(image));
    QVERIFY2(expected_hashes.contains(key),
             qPrintable(QStringLiteral("Missing golden hash for key %1 (actual=%2)").arg(key, actual_hash)));
    QCOMPARE(actual_hash, expected_hashes.value(key));
}

} // namespace

class GuiSmokeTest : public QObject
{
    Q_OBJECT

private slots:
    void rgb16DisplayReferencePreservesEndpoints();
    void checkedStateUpdatesPalette();
    void mainWindowGpuPreviewPolicyAllowsGpu16OnlyWithoutScopes();
    void mainWindowGpuPreviewPolicyScopesVetoOnlyWhenDisplayed();
    void mainWindowGpuPreviewPolicyUsesGpuShaderZebrasWhenViewportInstalled();
    void mainWindowGpuPreviewPolicyBuildsExpectedPresenterOptions();
    void mainWindowGpuTexturePresentDisplaySizeAppliesStretch();
    void mainWindowMlvAspectKeepsNeutralReceiptFromSuppressingDesqueeze();
    void gpuViewportFallsBackToPixmapWhenNotInstalled();
    void gpuViewportQueuesAndClearsPresentedFrame();
    void gpuViewportOwnsPaintOnlyWhileFramePending();
    void gpuViewportPreservesContinuousFrameEdges();
    void gpuViewportPresentsThroughNormalPaintEvents();
    void gpuViewportQueuesRgb16Frame();
    void gpuViewportQueuesBayer16Frame();
    void gpuViewportRejectsInvalidPlaybackReconTexture();
    void gpuViewportUsesSceneRectForTexturePresentationGeometry();
    void gpuViewportKeepsNativeSceneRectForTexturePresentationGeometry();
    void gpuViewportPresentsRgb888PatternExactly();
    void gpuViewportPresentsRgb16PatternExactly();
    void gpuDisplayWindowFramebufferReadbackFailsWithoutActiveWindow();
    void gpuDisplayWindowGrabsPresentedFramebufferReadback();
    void gpuDisplayWindowCapturePresentsPendingFrameAsRealPaint();
    void gpuDisplayWindowCaptureIgnoresFailedReconTextureSubmit();
    void gpuDisplayWindowRefusesReconTextureWithoutProcessingOptions();
    void mainWindowGpuPreviewPolicyAllowsExperimentalProcessingOnlyWhenCompatible();
    void mainWindowGpuPreviewPolicyAllowsExperimentalBilinearDebayerOnlyWhenCompatible();
    void mainWindowGpuPreviewPolicyRoutesFullQualityAmazeThroughAmazeGate();
    void mainWindowGpuPreviewPolicyKeepsAmazeTexturePresentExplicitAndNested();
    void mainWindowGpuPreviewPolicyRequiresWidgetViewportForAmazeTexturePresent();
    void mainWindowGpuPreviewPolicyRequiresWidgetViewportForAmazeTexturePresentAtAllScales();
    void mainWindowGpuPreviewPolicyKeepsPlaybackReconTexturePresentExplicitAndNested();
    void mainWindowClampsPlaybackScaleForGpuTextureRouteDecisionTable();
    void mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOneRequiresLibraryReadyFirst();
    void mainWindowGpuPreviewPolicyClassifiesPlaybackPipelineStatus();
    void mainWindowGpuPreviewPolicyLabelsVisibleScopeCpuFallback();
    void dualIsoPlaybackPolicyKeepsExplicitPreviewAndPlaybackOverrideSeparate();
    void dualIsoPatternMappingKeepsUiAndCoreConventionsAligned();
    void gpuViewportRgb888ZebraProcessingMatchesCpuReference();
    void gpuViewportZebraProcessingMatchesCpuReference();
    void gpuViewportPreviewProcessingMatchesCpuReference();
    void gpuViewportPreviewProcessingWithZebrasMatchesCpuReference();
    void gpuPreviewProcessingLutReadinessReflectsSignatureNotJustPointers();
    void gpuPreviewProcessingLutTextureSetFailsClosedOnMissingUploadAndContextLoss();
    void gpuPreviewProcessingReconRefusalMatchesForBothPresentersOnInjectedUploadFailure();
    void gpuViewportRefusesReconTextureDrawWhenLutReadinessIsFalse();
    void gpuDisplayWindowRecoversRetainedQImageAfterContextLossTeardown();
    void gpuPreviewProcessingCpuReferenceCorrectsPostWbUndoGreenCast();
    void histogramRegressionMatchesGolden();
    void vectorScopeRegressionMatchesGolden();
    void waveformRegressionMatchesGolden();
    void scopesLabelDispatchesRawHistogramExactly();
    void scopesLabelDispatchesRawWaveformExactly();
    void scopesLabelDispatchesRawParadeExactly();
    void scopesLabelDispatchesRawVectorScopeExactly();
};

void GuiSmokeTest::checkedStateUpdatesPalette()
{
    ColorToolButton button;
    button.setCheckable(true);

    const QColor original_button = button.palette().color(QPalette::Button);
    button.setChecked(true);
    QCOMPARE(button.palette().color(QPalette::Button), QColor(127, 127, 127));
    QCOMPARE(button.palette().color(QPalette::ButtonText), QColor(Qt::white));

    button.setChecked(false);
    QCOMPARE(button.palette().color(QPalette::Button), original_button);
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyAllowsGpu16OnlyWithoutScopes()
{
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.renderThreadUsing16BitPreview = true;

    QVERIFY(mainWindowAllowsGpu16PreviewRender(state));
    QVERIFY(mainWindowUsesGpu16PreviewPresentation(state));
    QVERIFY(!mainWindowUsesGpuImagePresentation(state));

    state.histogramEnabled = true;
    QVERIFY(!mainWindowAllowsGpu16PreviewRender(state));
    QVERIFY(!mainWindowUsesGpu16PreviewPresentation(state));
    QVERIFY(mainWindowUsesGpuImagePresentation(state));

    state.histogramEnabled = false;
    state.waveformEnabled = true;
    QVERIFY(!mainWindowAllowsGpu16PreviewRender(state));
    QVERIFY(mainWindowUsesGpuImagePresentation(state));

    state.waveformEnabled = false;
    state.paradeEnabled = true;
    QVERIFY(!mainWindowAllowsGpu16PreviewRender(state));
    QVERIFY(mainWindowUsesGpuImagePresentation(state));

    state.paradeEnabled = false;
    state.vectorScopeEnabled = true;
    QVERIFY(!mainWindowAllowsGpu16PreviewRender(state));
    QVERIFY(mainWindowUsesGpuImagePresentation(state));

    state = MainWindowGpuPreviewPolicyState();
    state.gpuViewportInstalled = true;
    state.renderThreadUsing16BitPreview = false;
    QVERIFY(mainWindowAllowsGpu16PreviewRender(state));
    QVERIFY(!mainWindowUsesGpu16PreviewPresentation(state));
    QVERIFY(mainWindowUsesGpuImagePresentation(state));

    state.gpuViewportInstalled = false;
    QVERIFY(!mainWindowAllowsGpu16PreviewRender(state));
    QVERIFY(!mainWindowUsesGpu16PreviewPresentation(state));
    QVERIFY(!mainWindowUsesGpuImagePresentation(state));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyScopesVetoOnlyWhenDisplayed()
{
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.renderThreadUsing16BitPreview = true;

    state.histogramEnabled = mainWindowScopeActionConsumesPresentedPixels(
        false, true);
    state.waveformEnabled = mainWindowScopeActionConsumesPresentedPixels(
        false, true);
    state.paradeEnabled = mainWindowScopeActionConsumesPresentedPixels(
        false, true);
    state.vectorScopeEnabled = mainWindowScopeActionConsumesPresentedPixels(
        false, true);
    QVERIFY(mainWindowAllowsGpu16PreviewRender(state));
    QVERIFY(mainWindowUsesGpu16PreviewPresentation(state));

    state.histogramEnabled = mainWindowScopeActionConsumesPresentedPixels(
        true, true);
    QVERIFY(!mainWindowAllowsGpu16PreviewRender(state));
    QVERIFY(!mainWindowUsesGpu16PreviewPresentation(state));

    state.histogramEnabled = false;
    state.vectorScopeEnabled = mainWindowScopeActionConsumesPresentedPixels(
        true, true);
    QVERIFY(!mainWindowAllowsGpu16PreviewRender(state));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyBuildsExpectedPresenterOptions()
{
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.zebrasEnabled = true;
    state.transformationMode = Qt::FastTransformation;
    state.playbackScaleFactorActive = 4;

    GpuDisplayViewport::PresentationOptions options =
        mainWindowBuildGpuPresentationOptions(state);
    QVERIFY(options.showZebras);
    QCOMPARE(options.samplingMode, GpuDisplayViewport::SamplingNearest);
    QCOMPARE(options.zebraUnderThreshold, preview_zebra::kUnderThresholdNormalized);
    QCOMPARE(options.zebraOverThreshold, preview_zebra::kOverThresholdNormalized);

    state.transformationMode = Qt::SmoothTransformation;
    state.betterResizerEnabled = false;
    options = mainWindowBuildGpuPresentationOptions(state);
    QCOMPARE(options.samplingMode, GpuDisplayViewport::SamplingLinear);

    state.transformationMode = Qt::FastTransformation;
    state.playbackScaleFactorActive = 8;
    state.betterResizerEnabled = false;
    options = mainWindowBuildGpuPresentationOptions(state);
    QCOMPARE(options.samplingMode, GpuDisplayViewport::SamplingBicubic);

    state.betterResizerEnabled = true;
    options = mainWindowBuildGpuPresentationOptions(state);
    QCOMPARE(options.samplingMode, GpuDisplayViewport::SamplingBicubic);

    state.renderThreadUsing16BitPreview = true;
    options = mainWindowBuildGpuPresentationOptions(state);
    QCOMPARE(options.samplingMode, GpuDisplayViewport::SamplingBicubic);
    QVERIFY(options.showZebras);
}

void GuiSmokeTest::mainWindowGpuTexturePresentDisplaySizeAppliesStretch()
{
    QCOMPARE(mainWindowGpuTexturePresentDisplaySize(1920, 1080, 3.0, 1.0),
             QSize(5760, 1080));
    QCOMPARE(mainWindowGpuTexturePresentDisplaySize(1920, 1080, 1.0, 1.667),
             QSize(1920, 1800));
    QCOMPARE(mainWindowGpuTexturePresentDisplaySize(1920, 1080, 0.0, -1.0),
             QSize(1920, 1080));
    QCOMPARE(mainWindowGpuTexturePresentDisplaySize(0, 1080, 3.0, 1.0),
             QSize(0, 1080));
}

void GuiSmokeTest::mainWindowMlvAspectKeepsNeutralReceiptFromSuppressingDesqueeze()
{
    QCOMPARE(mainWindowVerticalStretchIndexForMlvAspectRatio(1.0), 0);
    QCOMPARE(mainWindowVerticalStretchIndexForMlvAspectRatio(1.6667), 1);
    QCOMPARE(mainWindowVerticalStretchIndexForMlvAspectRatio(3.0), 2);
    QCOMPARE(mainWindowVerticalStretchIndexForMlvAspectRatio(0.3333), 3);
    QCOMPARE(mainWindowVerticalStretchIndexForMlvAspectRatio(0.0), 0);

    const RawAspectStretchSelection horizontal125 =
        rawAspectStretchSelectionForRatio(0.8);
    QVERIFY(horizontal125.valid);
    QCOMPARE(horizontal125.horizontalIndex, 1);
    QCOMPARE(horizontal125.verticalIndex, 0);
    const RawAspectStretchSelection combined =
        rawAspectStretchSelectionForRatio(4.0 / 3.0);
    QVERIFY(combined.valid);
    QCOMPARE(combined.horizontalIndex, 1);
    QCOMPARE(combined.verticalIndex, 1);

    const RawAspectRenderedDimensions v033Combined =
        rawAspectRenderedDimensions(100, 100, STRETCH_H_125, STRETCH_V_033, 65535);
    QVERIFY(v033Combined.valid);
    QCOMPARE(v033Combined.width, 375);
    QCOMPARE(v033Combined.height, 100);
    const RawAspectRenderedDimensions fractional =
        rawAspectRenderedDimensions(3, 3, STRETCH_H_125, STRETCH_V_100, 65535);
    QVERIFY(fractional.valid);
    QCOMPARE(fractional.width, 3);
    QCOMPARE(fractional.height, 3);
    QVERIFY(!rawAspectRenderedDimensions(65535, 100,
                                         STRETCH_H_125, STRETCH_V_033,
                                         65535).valid);

    QVERIFY(mainWindowShouldApplyMlvAspectForNeutralReceiptStretch(1.0, 1.0, 0.3333));
    QVERIFY(mainWindowShouldApplyMlvAspectForNeutralReceiptStretch(1.0, 1.0, 3.0));
    QVERIFY(!mainWindowShouldApplyMlvAspectForNeutralReceiptStretch(1.0, 1.0, 1.0));
    QVERIFY(!mainWindowShouldApplyMlvAspectForNeutralReceiptStretch(1.25, 1.0, 0.3333));
    QVERIFY(!mainWindowShouldApplyMlvAspectForNeutralReceiptStretch(1.0, 0.3333, 0.3333));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyUsesGpuShaderZebrasWhenViewportInstalled()
{
    MainWindowGpuPreviewPolicyState state;
    QVERIFY(!mainWindowUsesGpuShaderZebraProcessing(state));

    state.zebrasEnabled = true;
    QVERIFY(!mainWindowUsesGpuShaderZebraProcessing(state));

    state.gpuViewportInstalled = true;
    QVERIFY(mainWindowUsesGpuShaderZebraProcessing(state));

    state.renderThreadUsing16BitPreview = true;
    QVERIFY(mainWindowUsesGpuShaderZebraProcessing(state));

    state.histogramEnabled = true;
    QVERIFY(mainWindowUsesGpuShaderZebraProcessing(state));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyAllowsExperimentalProcessingOnlyWhenCompatible()
{
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Gpu;
    state.gpuPreviewProcessingCompatible = true;
    state.renderThreadUsing16BitPreview = true;
    state.renderThreadUsingGpuProcessingPreview = true;

    QVERIFY(mainWindowAllowsGpuPreviewProcessing(state));
    QVERIFY(mainWindowUsesGpuPreviewProcessing(state));

    state.gpuPreviewProcessingCompatible = false;
    QVERIFY(!mainWindowAllowsGpuPreviewProcessing(state));
    QVERIFY(!mainWindowUsesGpuPreviewProcessing(state));

    state.gpuPreviewProcessingCompatible = true;
    state.histogramEnabled = true;
    QVERIFY(!mainWindowAllowsGpuPreviewProcessing(state));

    state = MainWindowGpuPreviewPolicyState();
    state.gpuViewportInstalled = true;
    state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Auto;
    state.gpuPreviewProcessingEnvironmentRequested = false;
    state.gpuPreviewProcessingCompatible = true;
    state.renderThreadUsing16BitPreview = true;
    state.renderThreadUsingGpuProcessingPreview = true;
    QVERIFY(!mainWindowAllowsGpuPreviewProcessing(state));

    state.gpuPreviewProcessingEnvironmentRequested = true;
    QVERIFY(mainWindowAllowsGpuPreviewProcessing(state));
    QVERIFY(mainWindowUsesGpuPreviewProcessing(state));

    state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Cpu;
    QVERIFY(!mainWindowAllowsGpuPreviewProcessing(state));
    QVERIFY(!mainWindowUsesGpuPreviewProcessing(state));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyAllowsExperimentalBilinearDebayerOnlyWhenCompatible()
{
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Gpu;
    state.gpuPreviewProcessingCompatible = true;
    state.renderThreadUsing16BitPreview = true;
    state.renderThreadUsingGpuProcessingPreview = true;
    state.gpuBilinearDebayerBackendRequest = GpuBilinearDebayerBackendRequest::Gpu;
    state.gpuBilinearDebayerCompatible = true;
    state.renderThreadUsingGpuBilinearDebayer = true;

    QVERIFY(mainWindowAllowsGpuBilinearDebayer(state));
    QVERIFY(mainWindowUsesGpuBilinearDebayer(state));

    state.gpuBilinearDebayerCompatible = false;
    QVERIFY(!mainWindowAllowsGpuBilinearDebayer(state));
    QVERIFY(!mainWindowUsesGpuBilinearDebayer(state));

    state.gpuBilinearDebayerCompatible = true;
    state.renderThreadUsingGpuBilinearDebayer = false;
    QVERIFY(mainWindowAllowsGpuBilinearDebayer(state));
    QVERIFY(!mainWindowUsesGpuBilinearDebayer(state));

    state.renderThreadUsingGpuBilinearDebayer = true;
    state.gpuPreviewProcessingCompatible = false;
    QVERIFY(!mainWindowAllowsGpuBilinearDebayer(state));
    QVERIFY(!mainWindowUsesGpuBilinearDebayer(state));

    state = MainWindowGpuPreviewPolicyState();
    state.gpuViewportInstalled = true;
    state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Auto;
    state.gpuPreviewProcessingEnvironmentRequested = true;
    state.gpuPreviewProcessingCompatible = true;
    state.renderThreadUsing16BitPreview = true;
    state.renderThreadUsingGpuProcessingPreview = true;
    state.gpuBilinearDebayerBackendRequest = GpuBilinearDebayerBackendRequest::Auto;
    state.gpuBilinearDebayerEnvironmentRequested = false;
    state.gpuBilinearDebayerCompatible = true;
    state.renderThreadUsingGpuBilinearDebayer = true;
    QVERIFY(!mainWindowAllowsGpuBilinearDebayer(state));

    state.gpuBilinearDebayerEnvironmentRequested = true;
    QVERIFY(mainWindowAllowsGpuBilinearDebayer(state));
    QVERIFY(mainWindowUsesGpuBilinearDebayer(state));

    state.gpuBilinearDebayerBackendRequest = GpuBilinearDebayerBackendRequest::Cpu;
    QVERIFY(!mainWindowAllowsGpuBilinearDebayer(state));
    QVERIFY(!mainWindowUsesGpuBilinearDebayer(state));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyRoutesFullQualityAmazeThroughAmazeGate()
{
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Gpu;
    state.gpuPreviewProcessingCompatible = true;
    state.renderThreadUsing16BitPreview = true;
    state.renderThreadUsingGpuProcessingPreview = true;
    state.gpuBilinearDebayerBackendRequest = GpuBilinearDebayerBackendRequest::Gpu;
    state.gpuBilinearDebayerEnvironmentRequested = true;
    // MainWindow sets this false while AMaZE/full-quality debayer is active.
    state.gpuBilinearDebayerCompatible = false;
    state.renderThreadUsingGpuBilinearDebayer = true;
    state.gpuAmazeDebayerBackendRequest = GpuAmazeDebayerBackendRequest::Gpu;
    state.gpuAmazeDebayerCompatible = true;
    state.renderThreadUsingGpuAmazeDebayer = true;

    QVERIFY(mainWindowAllowsGpuPreviewProcessing(state));
    QVERIFY(mainWindowUsesGpuPreviewProcessing(state));
    QVERIFY(!mainWindowAllowsGpuBilinearDebayer(state));
    QVERIFY(!mainWindowUsesGpuBilinearDebayer(state));
    QVERIFY(mainWindowAllowsGpuAmazeDebayer(state));
    QVERIFY(mainWindowUsesGpuAmazeDebayer(state));

    state.gpuBilinearDebayerBackendRequest = GpuBilinearDebayerBackendRequest::Auto;
    QVERIFY(!mainWindowAllowsGpuBilinearDebayer(state));
    QVERIFY(!mainWindowUsesGpuBilinearDebayer(state));

    state.renderThreadUsingGpuAmazeDebayer = false;
    QVERIFY(mainWindowAllowsGpuAmazeDebayer(state));
    QVERIFY(!mainWindowUsesGpuAmazeDebayer(state));

    state.renderThreadUsingGpuAmazeDebayer = true;
    state.gpuAmazeDebayerBackendRequest = GpuAmazeDebayerBackendRequest::Cpu;
    QVERIFY(!mainWindowAllowsGpuAmazeDebayer(state));
    QVERIFY(!mainWindowUsesGpuAmazeDebayer(state));

    state.gpuAmazeDebayerBackendRequest = GpuAmazeDebayerBackendRequest::Auto;
    state.gpuAmazeDebayerEnvironmentRequested = false;
    QVERIFY(!mainWindowAllowsGpuAmazeDebayer(state));
    QVERIFY(!mainWindowUsesGpuAmazeDebayer(state));

    state.gpuAmazeDebayerEnvironmentRequested = true;
    QVERIFY(mainWindowAllowsGpuAmazeDebayer(state));
    QVERIFY(mainWindowUsesGpuAmazeDebayer(state));

    state.gpuBilinearDebayerCompatible = true;
    state.gpuAmazeDebayerCompatible = false;
    QVERIFY(mainWindowAllowsGpuBilinearDebayer(state));
    QVERIFY(mainWindowUsesGpuBilinearDebayer(state));
    QVERIFY(!mainWindowAllowsGpuAmazeDebayer(state));
    QVERIFY(!mainWindowUsesGpuAmazeDebayer(state));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyKeepsAmazeTexturePresentExplicitAndNested()
{
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.gpuWidgetViewportInstalled = true;
    state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Gpu;
    state.gpuPreviewProcessingCompatible = true;
    state.renderThreadUsing16BitPreview = true;
    state.renderThreadUsingGpuProcessingPreview = true;
    state.gpuAmazeDebayerBackendRequest = GpuAmazeDebayerBackendRequest::Gpu;
    state.gpuAmazeDebayerCompatible = true;
    state.renderThreadUsingGpuAmazeDebayer = true;

    QVERIFY(mainWindowUsesGpuAmazeDebayer(state));
    QVERIFY(!mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuAmazeTexturePresentation(state));

    state.gpuAmazeTexturePresentationEnvironmentRequested = true;
    QVERIFY(mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuAmazeTexturePresentation(state));

    state.renderThreadUsingGpuAmazeTexturePresentation = true;
    QVERIFY(mainWindowUsesGpuAmazeTexturePresentation(state));

    state.gpuViewportInstalled = false;
    QVERIFY(!mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuAmazeTexturePresentation(state));

    state.gpuViewportInstalled = true;
    state.gpuAmazeDebayerCompatible = false;
    QVERIFY(!mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuAmazeTexturePresentation(state));

    state.gpuAmazeDebayerCompatible = true;
    state.renderThreadUsingGpuAmazeDebayer = false;
    QVERIFY(!mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuAmazeTexturePresentation(state));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyRequiresWidgetViewportForAmazeTexturePresent()
{
    // CUDA-SCALE4-ZERO-PRESENT-1: GL-window mode (Optimus hybrid, no QOpenGLWidget
    // viewport installed on the QGraphicsView) sets gpuViewportInstalled via
    // GpuDisplayWindow::isActive(), but GpuDisplayViewport::presentAmazePostWbTexture
    // and presentRgb16 can only present through the QOpenGLWidget viewport -- they have
    // no GpuDisplayWindow routing. Without this gate the render thread took the
    // AMaZE-texture branch, deferred debayer to a viewport that never receives the
    // texture, and presented zero frames.
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.gpuWidgetViewportInstalled = false;
    state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Gpu;
    state.gpuPreviewProcessingCompatible = true;
    state.renderThreadUsing16BitPreview = true;
    state.renderThreadUsingGpuProcessingPreview = true;
    state.gpuAmazeDebayerBackendRequest = GpuAmazeDebayerBackendRequest::Gpu;
    state.gpuAmazeDebayerCompatible = true;
    state.renderThreadUsingGpuAmazeDebayer = true;
    state.gpuAmazeTexturePresentationEnvironmentRequested = true;
    state.renderThreadUsingGpuAmazeTexturePresentation = true;

    QVERIFY(mainWindowUsesGpuAmazeDebayer(state));
    QVERIFY(!mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuAmazeTexturePresentation(state));

    state.gpuWidgetViewportInstalled = true;
    QVERIFY(mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(mainWindowUsesGpuAmazeTexturePresentation(state));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyRequiresWidgetViewportForAmazeTexturePresentAtAllScales()
{
    // CUDA-SCALE4-ZERO-PRESENT-1 round 3: round 2 added a tex-nr-eligible carve-out
    // to this gate, but the carve-out was dead at the only production call site
    // (drawFrame evaluates this gate before gpuPlaybackReconTexturePresentationCompatible
    // is computed for the frame), so it never ran and its "byte-for-byte" scale-1
    // restoration claim was never shipped. The disjunct is removed; this test asserts
    // the widget-viewport requirement applies unconditionally, at scale 1 exactly like
    // at scale 4, regardless of whether the tex-nr route would otherwise be eligible.
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.gpuWidgetViewportInstalled = false;
    state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Gpu;
    state.gpuPreviewProcessingCompatible = true;
    state.renderThreadUsing16BitPreview = true;
    state.renderThreadUsingGpuProcessingPreview = true;
    state.gpuAmazeDebayerBackendRequest = GpuAmazeDebayerBackendRequest::Gpu;
    state.gpuAmazeDebayerCompatible = true;
    state.renderThreadUsingGpuAmazeDebayer = true;
    state.gpuAmazeTexturePresentationEnvironmentRequested = true;
    state.renderThreadUsingGpuAmazeTexturePresentation = true;
    state.gpuPlaybackReconEnvironmentRequested = true;
    state.gpuPlaybackReconTexturePresentationEnvironmentRequested = true;
    state.gpuPlaybackReconTexturePresentationCompatible = true;
    state.playbackScaleFactorActive = 1;

    // Scale 1, window mode, no widget viewport, tex-nr route otherwise eligible:
    // still not allowed -- the widget-viewport requirement is unconditional.
    QVERIFY(mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
    QVERIFY(!mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuAmazeTexturePresentation(state));

    // Scale 4, window mode, no widget viewport, tex-nr route not eligible:
    // same result, same reason.
    state.playbackScaleFactorActive = 4;
    state.gpuPlaybackReconTexturePresentationCompatible = false;
    QVERIFY(!mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
    QVERIFY(!mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuAmazeTexturePresentation(state));

    // With a widget viewport installed, both scales are allowed.
    state.gpuWidgetViewportInstalled = true;
    QVERIFY(mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(mainWindowUsesGpuAmazeTexturePresentation(state));

    state.playbackScaleFactorActive = 1;
    state.gpuPlaybackReconTexturePresentationCompatible = true;
    QVERIFY(mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(mainWindowUsesGpuAmazeTexturePresentation(state));
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyKeepsPlaybackReconTexturePresentExplicitAndNested()
{
    MainWindowGpuPreviewPolicyState state;
    state.gpuViewportInstalled = true;
    state.renderThreadUsing16BitPreview = true;
    state.gpuPlaybackReconEnvironmentRequested = true;
    state.gpuPlaybackReconTexturePresentationEnvironmentRequested = true;

    QVERIFY(!mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuPlaybackReconTexturePresentation(state));

    state.gpuPlaybackReconTexturePresentationCompatible = true;
    QVERIFY(mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuPlaybackReconTexturePresentation(state));

    state.renderThreadUsingGpuPlaybackReconTexturePresentation = true;
    QVERIFY(mainWindowUsesGpuPlaybackReconTexturePresentation(state));

    state.gpuPlaybackReconEnvironmentRequested = false;
    QVERIFY(!mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuPlaybackReconTexturePresentation(state));

    state.gpuPlaybackReconEnvironmentRequested = true;
    state.gpuPlaybackReconTexturePresentationEnvironmentRequested = false;
    QVERIFY(!mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuPlaybackReconTexturePresentation(state));

    state.gpuPlaybackReconTexturePresentationEnvironmentRequested = true;
    state.histogramEnabled = true;
    QVERIFY(!mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuPlaybackReconTexturePresentation(state));
}

void GuiSmokeTest::mainWindowClampsPlaybackScaleForGpuTextureRouteDecisionTable()
{
    // CUDA-S4-TEXTURE-ROUTE-CLAMP-1: texture route eligible at scale 1 ->
    // every non-1 request clamps to 1; scale 1 itself is a no-op.
    QCOMPARE( mainWindowClampPlaybackScaleForGpuTextureRoute( 1, true ), 1 );
    QCOMPARE( mainWindowClampPlaybackScaleForGpuTextureRoute( 2, true ), 1 );
    QCOMPARE( mainWindowClampPlaybackScaleForGpuTextureRoute( 4, true ), 1 );
    QCOMPARE( mainWindowClampPlaybackScaleForGpuTextureRoute( 8, true ), 1 );

    // Texture route not eligible -> every requested scale passes through
    // unchanged; the user-facing scale setting is never altered.
    QCOMPARE( mainWindowClampPlaybackScaleForGpuTextureRoute( 1, false ), 1 );
    QCOMPARE( mainWindowClampPlaybackScaleForGpuTextureRoute( 2, false ), 2 );
    QCOMPARE( mainWindowClampPlaybackScaleForGpuTextureRoute( 4, false ), 4 );
    QCOMPARE( mainWindowClampPlaybackScaleForGpuTextureRoute( 8, false ), 8 );
}

void GuiSmokeTest::mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOneRequiresLibraryReadyFirst()
{
    // CUDA-S4-TEXTURE-ROUTE-CLAMP-1 round 2: this is the startup-crash regression.
    // MainWindow::gpuPlaybackReconTextureRouteEligibleAtScaleOne() runs during
    // initGui(), before initLib() has assigned m_pProcessingObject -- a raw
    // pointer with no default member initializer, so it is garbage (not null)
    // at that point. libraryReady=false must short-circuit everything else,
    // even when every other input below says "eligible", exactly matching the
    // GPU env under which bachelor crashed on startup (GL window viewport +
    // GPU playback recon + texture present, all "on").
    QVERIFY( !mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        /*libraryReady*/ false,
        /*gpuPreviewSurfaceActive*/ true,
        /*hasScopeVisualization*/ false,
        /*reconRequestedByEnvironment*/ true,
        /*texturePresentRequestedByEnvironment*/ true,
        /*gpuPreviewProcessingSupported*/ true,
        /*backendRequestIsCpu*/ false,
        /*requestedPhase3ModeIsDecodeReconProcess*/ true,
        /*cachingChecked*/ false ) );

    // Once the library is ready and every other input says "eligible", the
    // route is eligible -- proves libraryReady is a gate, not a permanent veto.
    QVERIFY( mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        /*libraryReady*/ true,
        /*gpuPreviewSurfaceActive*/ true,
        /*hasScopeVisualization*/ false,
        /*reconRequestedByEnvironment*/ true,
        /*texturePresentRequestedByEnvironment*/ true,
        /*gpuPreviewProcessingSupported*/ true,
        /*backendRequestIsCpu*/ false,
        /*requestedPhase3ModeIsDecodeReconProcess*/ true,
        /*cachingChecked*/ false ) );

    // libraryReady=true but every other gate still applies normally.
    QVERIFY( !mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        true, false, false, true, true, true, false, true, false ) );  // no GPU surface
    QVERIFY( !mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        true, true, true, true, true, true, false, true, false ) );    // scope visible
    QVERIFY( !mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        true, true, false, false, true, true, false, true, false ) );  // recon not requested
    QVERIFY( !mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        true, true, false, true, false, true, false, true, false ) );  // texture-present not requested
    QVERIFY( !mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        true, true, false, true, true, false, false, true, false ) );  // processing unsupported
    QVERIFY( !mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        true, true, false, true, true, true, true, true, false ) );    // backend forced CPU
    QVERIFY( !mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        true, true, false, true, true, true, false, false, false ) );  // wrong phase3 mode
    QVERIFY( !mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
        true, true, false, true, true, true, false, true, true ) );    // caching enabled
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyClassifiesPlaybackPipelineStatus()
{
    MainWindowGpuPreviewPolicyState state;
    QCOMPARE( static_cast<int>( mainWindowGpuPlaybackPipelineStatus(
                  state, false, false, false ) ),
              static_cast<int>( GpuPlaybackPipelineStatus::Cpu ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusToken(
                  GpuPlaybackPipelineStatus::Cpu ) ),
              QStringLiteral( "cpu" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusLabel(
                  GpuPlaybackPipelineStatus::Cpu ) ),
              QStringLiteral( "CPU" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusDescription(
                  GpuPlaybackPipelineStatus::Cpu ) ),
              QStringLiteral( "CPU presentation" ) );

    state.gpuViewportInstalled = true;
    state.renderThreadUsing16BitPreview = true;
    state.gpuPreviewProcessingBackendRequest =
        GpuPreviewProcessingBackendRequest::Gpu;
    state.gpuPreviewProcessingCompatible = true;
    state.renderThreadUsingGpuProcessingPreview = true;
    QCOMPARE( static_cast<int>( mainWindowGpuPlaybackPipelineStatus(
                  state, false, false, false ) ),
              static_cast<int>( GpuPlaybackPipelineStatus::GpuPreview ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusToken(
                  GpuPlaybackPipelineStatus::GpuPreview ) ),
              QStringLiteral( "gpu_preview" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusLabel(
                  GpuPlaybackPipelineStatus::GpuPreview ) ),
              QStringLiteral( "GPU Preview" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusDescription(
                  GpuPlaybackPipelineStatus::GpuPreview ) ),
              QStringLiteral( "GPU preview processing or debayer" ) );

    QCOMPARE( static_cast<int>( mainWindowGpuPlaybackPipelineStatus(
                  state, true, false, false ) ),
              static_cast<int>( GpuPlaybackPipelineStatus::GpuReconReadback ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusToken(
                  GpuPlaybackPipelineStatus::GpuReconReadback ) ),
              QStringLiteral( "gpu_recon_readback" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusLabel(
                  GpuPlaybackPipelineStatus::GpuReconReadback ) ),
              QStringLiteral( "GPU RB" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusDescription(
                  GpuPlaybackPipelineStatus::GpuReconReadback ) ),
              QStringLiteral( "CUDA reconstruction with CPU readback" ) );

    QCOMPARE( static_cast<int>( mainWindowGpuPlaybackPipelineStatus(
                  state, true, true, false ) ),
              static_cast<int>( GpuPlaybackPipelineStatus::GpuTextureReadback ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusToken(
                  GpuPlaybackPipelineStatus::GpuTextureReadback ) ),
              QStringLiteral( "gpu_texture_readback" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusLabel(
                  GpuPlaybackPipelineStatus::GpuTextureReadback ) ),
              QStringLiteral( "GPU Tex RB" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusDescription(
                  GpuPlaybackPipelineStatus::GpuTextureReadback ) ),
              QStringLiteral( "GL texture presentation from a CPU-readback Bayer frame" ) );

    QCOMPARE( static_cast<int>( mainWindowGpuPlaybackPipelineStatus(
                  state, true, true, true ) ),
              static_cast<int>( GpuPlaybackPipelineStatus::GpuTextureNoReadback ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusToken(
                  GpuPlaybackPipelineStatus::GpuTextureNoReadback ) ),
              QStringLiteral( "gpu_texture_no_readback" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusLabel(
                  GpuPlaybackPipelineStatus::GpuTextureNoReadback ) ),
              QStringLiteral( "GPU Tex NR" ) );
    QCOMPARE( QString::fromLatin1( mainWindowGpuPlaybackPipelineStatusDescription(
                  GpuPlaybackPipelineStatus::GpuTextureNoReadback ) ),
              QStringLiteral( "CUDA-to-GL texture presentation without per-frame CPU readback" ) );
}

void GuiSmokeTest::mainWindowGpuPreviewPolicyLabelsVisibleScopeCpuFallback()
{
    MainWindowGpuPreviewPolicyState state;
    state.histogramEnabled = true;

    QCOMPARE( QString::fromLatin1(
                  mainWindowGpuPlaybackPipelineStatusBadgeLabel(
                      GpuPlaybackPipelineStatus::Cpu,
                      state,
                      true ) ),
              QStringLiteral( "CPU - scopes active" ) );
    QCOMPARE( QString::fromLatin1(
                  mainWindowGpuPlaybackPipelineStatusBadgeLabel(
                      GpuPlaybackPipelineStatus::Cpu,
                      state,
                      false ) ),
              QStringLiteral( "CPU" ) );
    QCOMPARE( QString::fromLatin1(
                  mainWindowGpuPlaybackPipelineStatusBadgeLabel(
                      GpuPlaybackPipelineStatus::GpuTextureNoReadback,
                      state,
                      true ) ),
              QStringLiteral( "GPU Tex NR" ) );
    QVERIFY( QString::fromLatin1( mainWindowScopePerformanceHintText() )
                 .contains( QStringLiteral( "Hide the edit area with E" ) ) );
}

void GuiSmokeTest::dualIsoPlaybackPolicyKeepsExplicitPreviewAndPlaybackOverrideSeparate()
{
    DualIsoPlaybackRuntimeSettings settings = effectiveDualIsoPlaybackRuntimeSettings(false,
                                                                                      true,
                                                                                      1,
                                                                                      2,
                                                                                      0,
                                                                                      1,
                                                                                      1);
    QCOMPARE(settings.mode, 2);
    QCOMPARE(settings.interpolation, 1);
    QCOMPARE(settings.aliasMap, 0);
    QCOMPARE(settings.fullResBlending, 0);
    QVERIFY(!settings.previewOverrideActive);

    settings = effectiveDualIsoPlaybackRuntimeSettings(true,
                                                       true,
                                                       1,
                                                       1,
                                                       0,
                                                       1,
                                                       1);
    QCOMPARE(settings.mode, 1);
    QCOMPARE(settings.interpolation, 0);
    QCOMPARE(settings.aliasMap, 0);
    QCOMPARE(settings.fullResBlending, 1);
    QVERIFY(settings.previewOverrideActive);
    QVERIFY(settings.playbackForceMean23);

    settings = effectiveDualIsoPlaybackRuntimeSettings(false,
                                                       true,
                                                       1,
                                                       1,
                                                       0,
                                                       1,
                                                       1);
    QCOMPARE(settings.mode, 1);
    QCOMPARE(settings.interpolation, 0);
    QCOMPARE(settings.aliasMap, 1);
    QCOMPARE(settings.fullResBlending, 1);
    QVERIFY(!settings.previewOverrideActive);
}

void GuiSmokeTest::dualIsoPatternMappingKeepsUiAndCoreConventionsAligned()
{
    QCOMPARE(dualIsoUiPatternIndexFromCorePattern(-1), 1);
    QCOMPARE(dualIsoUiPatternIndexFromCorePattern(-4), 4);
    QCOMPARE(dualIsoUiPatternIndexFromCorePattern(1), 1);
    QCOMPARE(dualIsoUiPatternIndexFromCorePattern(4), 4);
    QCOMPARE(dualIsoUiPatternIndexFromCorePattern(0), 0);

    QCOMPARE(dualIsoCorePatternFromUiIndex(1), 1);
    QCOMPARE(dualIsoCorePatternFromUiIndex(4), 4);
    QCOMPARE(dualIsoCorePatternFromUiIndex(-1), 0);
    QCOMPARE(dualIsoCorePatternFromUiIndex(6), 0);
}

void GuiSmokeTest::gpuViewportFallsBackToPixmapWhenNotInstalled()
{
    qunsetenv(GpuDisplayViewport::environmentVariableName());

    QGraphicsScene scene;
    QGraphicsPixmapItem *item = scene.addPixmap(QPixmap(4, 4));
    QGraphicsView view(&scene);

    QVERIFY(!GpuDisplayViewport::isInstalledOn(&view));

    QImage image(8, 6, QImage::Format_RGB888);
    image.fill(Qt::blue);

    QVERIFY(!GpuDisplayViewport::presentImage(&view, item, image));
    QVERIFY(item->isVisible());
    QVERIFY(!GpuDisplayViewport::hasPresentedImage(&view));
    QVERIFY(!GpuDisplayViewport::isTexturePresentationActive(&view));
    QCOMPARE(GpuDisplayViewport::samplingModeFor(&view), GpuDisplayViewport::SamplingLinear);

    GpuDisplayViewport::clearPresentedImage(&view, item);
    QVERIFY(item->isVisible());
}

void GuiSmokeTest::gpuViewportQueuesAndClearsPresentedFrame()
{
    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    QGraphicsScene scene;
    QGraphicsPixmapItem *item = scene.addPixmap(QPixmap(4, 4));
    QGraphicsView view(&scene);
    view.resize(64, 64);
    view.show();
    QApplication::processEvents();

    QVERIFY(GpuDisplayViewport::installOn(&view));
    QVERIFY(GpuDisplayViewport::isInstalledOn(&view));
    QVERIFY(!GpuDisplayViewport::hasPresentedImage(&view));
    QVERIFY(!GpuDisplayViewport::isTexturePresentationActive(&view));
    QCOMPARE(GpuDisplayViewport::samplingModeFor(&view), GpuDisplayViewport::SamplingLinear);

    QImage image(8, 6, QImage::Format_RGB888);
    image.fill(Qt::red);
    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingBicubic;

    QVERIFY(GpuDisplayViewport::presentImage(&view, item, image, options));
    QVERIFY(GpuDisplayViewport::hasPresentedImage(&view));
    QCOMPARE(GpuDisplayViewport::samplingModeFor(&view), GpuDisplayViewport::SamplingBicubic);
    QVERIFY(!item->isVisible());

    QApplication::processEvents();
    QVERIFY(GpuDisplayViewport::hasPresentedImage(&view));

    options.samplingMode = GpuDisplayViewport::SamplingNearest;
    QVERIFY(GpuDisplayViewport::presentImage(&view, item, image, options));
    QCOMPARE(GpuDisplayViewport::samplingModeFor(&view), GpuDisplayViewport::SamplingNearest);
    QApplication::processEvents();
    QVERIFY(GpuDisplayViewport::hasPresentedImage(&view));

    GpuDisplayViewport::clearPresentedImage(&view, item);
    QVERIFY(!GpuDisplayViewport::hasPresentedImage(&view));
    QVERIFY(!GpuDisplayViewport::isTexturePresentationActive(&view));
    QVERIFY(item->isVisible());

    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::rgb16DisplayReferencePreservesEndpoints()
{
    const QImage endpoints = rgb16_to_qimage({0, 65535, 65408}, 1, 1);
    QCOMPARE(endpoints.pixelColor(0, 0), QColor(0, 255, 255));
    std::vector<uint16_t> levels(256 * 3);
    for (int level = 0; level < 256; ++level)
        for (int channel = 0; channel < 3; ++channel)
            levels[level * 3 + channel] = static_cast<uint16_t>(level * 257);
    const QImage roundtrip = rgb16_to_qimage(levels, 256, 1);
    for (int level = 0; level < 256; ++level)
        QCOMPARE(roundtrip.pixelColor(level, 0), QColor(level, level, level));
}

void GuiSmokeTest::gpuViewportOwnsPaintOnlyWhileFramePending()
{
    const QByteArray previous = qgetenv(GpuDisplayViewport::environmentVariableName());
    const auto restore = qScopeGuard([previous]() {
        qputenv(GpuDisplayViewport::environmentVariableName(), previous);
    });
    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    class PaintRoutingView : public QGraphicsView {
    public:
        explicit PaintRoutingView(QGraphicsScene *scene) : QGraphicsView(scene) {}
        int scenePaints = 0;
        int userEvents = 0;
    protected:
        void paintEvent(QPaintEvent *) override { ++scenePaints; }
        bool viewportEvent(QEvent *event) override {
            if (event->type() == QEvent::User) ++userEvents;
            return QGraphicsView::viewportEvent(event);
        }
    };
    QGraphicsScene scene;
    QPixmap fallback(8, 6);
    fallback.fill(Qt::blue);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback);
    PaintRoutingView view(&scene);
    view.resize(64, 64);
    QVERIFY(GpuDisplayViewport::installOn(&view));

    // Exercise Qt's actual viewport event filters without using grab(), which
    // can render a correct FBO even when ordinary paint events go to the scene.
    QPaintEvent emptyPaint(view.viewport()->rect());
    QApplication::sendEvent(view.viewport(), &emptyPaint);
    QCOMPARE(view.scenePaints, 1);
    QImage image(8, 6, QImage::Format_RGB888);
    image.fill(Qt::red);
    QVERIFY(GpuDisplayViewport::presentImage(&view, item, image));
    QVERIFY(!item->isVisible());
    QPaintEvent framePaint(view.viewport()->rect());
    QApplication::sendEvent(view.viewport(), &framePaint);
    QCOMPARE(view.scenePaints, 1);

    QEvent userEvent(QEvent::User);
    QApplication::sendEvent(view.viewport(), &userEvent);
    QCOMPARE(view.userEvents, 1);
    GpuDisplayViewport::clearPresentedImage(&view, item);
    QVERIFY(item->isVisible());
    QPaintEvent fallbackPaint(view.viewport()->rect());
    QApplication::sendEvent(view.viewport(), &fallbackPaint);
    QCOMPARE(view.scenePaints, 2);
}

void GuiSmokeTest::gpuViewportPreservesContinuousFrameEdges()
{
    const QByteArray previous = qgetenv(GpuDisplayViewport::environmentVariableName());
    const auto restore = qScopeGuard([previous]() {
        qputenv(GpuDisplayViewport::environmentVariableName(), previous);
    });
    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));
    QGraphicsScene scene;
    QPixmap fallback(4, 4);
    fallback.fill(Qt::black);
    auto *item = scene.addPixmap(fallback);
    std::unique_ptr<QGraphicsView> view(make_presenter_view(scene, item, QSize(4, 4)));
    QVERIFY(GpuDisplayViewport::installOn(view.get()));
    auto *viewport = qobject_cast<GpuDisplayViewport *>(view->viewport());
    QVERIFY(viewport);
    QVERIFY(GpuDisplayViewport::presentImage(view.get(), item, make_presenter_pattern()));
    // QRect's inclusive integer endpoints used to inflate this extent to 5x5.
    QCOMPARE(viewport->targetRectInViewport().size(), QSizeF(4, 4));
    view->scale(1.25, 1.5);
    QCOMPARE(viewport->targetRectInViewport().size(), QSizeF(5, 6));
    // The caller's scene rectangle carries de-squeeze independently of texture
    // dimensions. Preserve that rectangle through fractional zoom and scrolling.
    view->setSceneRect(QRectF(0, 0, 2048, 512));
    QCOMPARE(viewport->targetRectInViewport().size(), QSizeF(2560, 768));
    view->horizontalScrollBar()->setValue(20);
    const QRectF beforeScroll = viewport->targetRectInViewport();
    view->horizontalScrollBar()->setValue(27);
    QCOMPARE(viewport->targetRectInViewport().left(), beforeScroll.left() - 7);
    QCOMPARE(viewport->targetRectInViewport().size(), beforeScroll.size());
}

void GuiSmokeTest::gpuViewportPresentsThroughNormalPaintEvents()
{
    if (QGuiApplication::platformName() == QStringLiteral("offscreen"))
        QSKIP("Native OpenGL presentation is validated separately with QT_QPA_PLATFORM=windows.");
    const QByteArray previous = qgetenv(GpuDisplayViewport::environmentVariableName());
    const auto restore = qScopeGuard([previous]() {
        qputenv(GpuDisplayViewport::environmentVariableName(), previous);
    });
    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));
    QGraphicsScene scene;
    QPixmap fallback(8, 6);
    fallback.fill(Qt::blue);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback);
    QGraphicsView view(&scene);
    view.resize(128, 96);
    QVERIFY(GpuDisplayViewport::installOn(&view));
    view.show();
    QVERIFY(QTest::qWaitForWindowExposed(&view));
    auto *viewport = qobject_cast<GpuDisplayViewport *>(view.viewport());
    QVERIFY(viewport);
    QVERIFY2(viewport->isValid(), "Native validation requires a working OpenGL context.");

    QImage image(8, 6, QImage::Format_RGB888);
    image.fill(Qt::red);
    QVERIFY(GpuDisplayViewport::presentImage(&view, item, image));
    // No QWidget::grab or grabFramebuffer before this assertion: those calls
    // force paintGL and would hide a broken normal playback paint route.
    QTRY_VERIFY_WITH_TIMEOUT(GpuDisplayViewport::isTexturePresentationActive(&view), 2000);
    // Optional bounded dwell for an external compositor capture of this synthetic
    // red frame. The assertions above do not depend on screenshots or the dwell.
    const int captureHoldMs = qBound(0, qEnvironmentVariableIntValue("MLVAPP_TEST_VISIBLE_HOLD_MS"), 5000);
    if (captureHoldMs > 0) QTest::qWait(captureHoldMs);
    image.fill(Qt::green);
    QVERIFY(GpuDisplayViewport::presentImage(&view, item, image));
    QVERIFY(!GpuDisplayViewport::isTexturePresentationActive(&view));
    QTRY_VERIFY_WITH_TIMEOUT(GpuDisplayViewport::isTexturePresentationActive(&view), 2000);
    QVERIFY(!item->isVisible());
    GpuDisplayViewport::clearPresentedImage(&view, item);
    QVERIFY(item->isVisible());
}

void GuiSmokeTest::gpuViewportQueuesRgb16Frame()
{
    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    QGraphicsScene scene;
    QGraphicsPixmapItem *item = scene.addPixmap(QPixmap(4, 4));
    QGraphicsView view(&scene);
    view.resize(64, 64);
    view.show();
    QApplication::processEvents();

    QVERIFY(GpuDisplayViewport::installOn(&view));

    const uint16_t rgb16[] = {
        65535, 0, 0,
        0, 65535, 0,
        0, 0, 65535,
        65535, 65535, 65535
    };

    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingLinear;
    QVERIFY(GpuDisplayViewport::presentRgb16(&view, item, rgb16, 2, 2, options));
    QVERIFY(GpuDisplayViewport::hasPresentedImage(&view));
    QVERIFY(!item->isVisible());
    QApplication::processEvents();
    QVERIFY(GpuDisplayViewport::hasPresentedImage(&view));

    GpuDisplayViewport::clearPresentedImage(&view, item);
    QVERIFY(item->isVisible());
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuViewportQueuesBayer16Frame()
{
    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    QGraphicsScene scene;
    QGraphicsPixmapItem *item = scene.addPixmap(QPixmap(4, 4));
    QGraphicsView view(&scene);
    view.resize(64, 64);
    view.show();
    QApplication::processEvents();

    QVERIFY(GpuDisplayViewport::installOn(&view));

    const uint16_t bayer16[] = {
        65535, 0,
        0, 65535
    };

    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingBicubic;
    QVERIFY(GpuDisplayViewport::presentBayer16(&view, item, bayer16, 2, 2, options));
    QVERIFY(GpuDisplayViewport::hasPresentedImage(&view));
    QVERIFY(!item->isVisible());
    QApplication::processEvents();
    QVERIFY(GpuDisplayViewport::hasPresentedImage(&view));

    GpuDisplayViewport::clearPresentedImage(&view, item);
    QVERIFY(item->isVisible());
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuViewportRejectsInvalidPlaybackReconTexture()
{
    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    QGraphicsScene scene;
    QGraphicsPixmapItem *item = scene.addPixmap(QPixmap(4, 4));
    QGraphicsView view(&scene);
    view.resize(64, 64);
    view.show();
    QApplication::processEvents();

    QVERIFY(GpuDisplayViewport::installOn(&view));

    const uint16_t rawInput[] = {
        1024, 2048,
        3072, 4096
    };
    llrpGpuPlaybackReconState_t state;
    memset(&state, 0, sizeof(state));
    state.valid = 1;
    state.width = 2;
    state.height = 2;

    QString reason;
    llrpGpuPlaybackReconTiming_t timing;
    memset(&timing, 0, sizeof(timing));
    GpuDisplayViewport::PresentationOptions options;
    QVERIFY(!GpuDisplayViewport::presentGpuPlaybackReconTexture(&view,
                                                                item,
                                                                rawInput,
                                                                4,
                                                                &state,
                                                                options,
                                                                &reason,
                                                                &timing));
    QVERIFY(!reason.isEmpty());
    QVERIFY(item->isVisible());
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuViewportUsesSceneRectForTexturePresentationGeometry()
{
    if (QGuiApplication::platformName() == QStringLiteral("offscreen")) {
        QSKIP("GL viewport capture needs a platform plugin that can create an OpenGL context");
    }

    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    const QSize raw_size(40, 20);
    const QRectF stretched_scene(QPointF(0.0, 0.0), QSizeF(120.0, 20.0));
    QImage submitted(raw_size, QImage::Format_RGB888);
    submitted.fill(QColor(0, 255, 0));

    QGraphicsScene scene;
    QPixmap fallback_pixmap(raw_size);
    fallback_pixmap.fill(Qt::black);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback_pixmap);
    item->setOffset(0.0, 0.0);

    auto *view = new QGraphicsView(&scene);
    std::unique_ptr<QGraphicsView> view_owner(view);
    view->setFrameShape(QFrame::NoFrame);
    view->setAlignment(Qt::AlignLeft | Qt::AlignTop);
    view->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setBackgroundBrush(Qt::black);
    view->setSceneRect(stretched_scene);
    view->resize(static_cast<int>(stretched_scene.width()) + 8,
                 static_cast<int>(stretched_scene.height()) + 8);

    QVERIFY(GpuDisplayViewport::installOn(view));
    view->show();
    QApplication::processEvents();

    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingNearest;
    QVERIFY(GpuDisplayViewport::presentImage(view, item, submitted, options));

    const QImage actual = crop_presented_scene_rect(view, stretched_scene);
    if (actual.isNull()) {
        QSKIP("OpenGL framebuffer capture is unavailable in this environment");
    }

    QVERIFY(actual.height() >= static_cast<int>(stretched_scene.height()) - 1);
    QVERIFY(actual.height() <= static_cast<int>(stretched_scene.height()) + 1);
    QVERIFY(actual.width() >= static_cast<int>(stretched_scene.width()) - 1);

    const QColor right_edge(actual.pixel(actual.width() - 4, actual.height() / 2));
    QVERIFY2(right_edge.green() > 180 && right_edge.red() < 80 && right_edge.blue() < 80,
             qPrintable(QStringLiteral("Expected stretched GL presentation to fill scene rect; right edge was rgb(%1,%2,%3)")
                        .arg(right_edge.red()).arg(right_edge.green()).arg(right_edge.blue())));

    GpuDisplayViewport::clearPresentedImage(view, item);
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuViewportKeepsNativeSceneRectForTexturePresentationGeometry()
{
    if (QGuiApplication::platformName() == QStringLiteral("offscreen")) {
        QSKIP("GL viewport capture needs a platform plugin that can create an OpenGL context");
    }

    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    const QSize raw_size(40, 20);
    const QRectF native_scene(QPointF(0.0, 0.0), QSizeF(raw_size));
    QImage submitted(raw_size, QImage::Format_RGB888);
    submitted.fill(QColor(0, 255, 0));

    QGraphicsScene scene;
    QPixmap fallback_pixmap(raw_size);
    fallback_pixmap.fill(Qt::black);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback_pixmap);
    item->setOffset(0.0, 0.0);

    auto *view = new QGraphicsView(&scene);
    std::unique_ptr<QGraphicsView> view_owner(view);
    view->setFrameShape(QFrame::NoFrame);
    view->setAlignment(Qt::AlignLeft | Qt::AlignTop);
    view->setHorizontalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setVerticalScrollBarPolicy(Qt::ScrollBarAlwaysOff);
    view->setBackgroundBrush(Qt::black);
    view->setSceneRect(native_scene);
    view->resize(128, raw_size.height() + 8);

    QVERIFY(GpuDisplayViewport::installOn(view));
    view->show();
    QApplication::processEvents();

    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingNearest;
    QVERIFY(GpuDisplayViewport::presentImage(view, item, submitted, options));

    const QImage actual = crop_presented_scene_rect(view, native_scene);
    const QImage framebuffer = grab_gpu_viewport_framebuffer(view);
    auto *viewport = qobject_cast<GpuDisplayViewport *>(view->viewport());
    if (actual.isNull() || framebuffer.isNull() || !viewport) {
        QSKIP("OpenGL framebuffer capture is unavailable in this environment");
    }

    QVERIFY(actual.width() >= raw_size.width() - 1);
    QVERIFY(actual.width() <= raw_size.width() + 1);
    const QColor right_edge(actual.pixel(actual.width() - 4, actual.height() / 2));
    QVERIFY2(right_edge.green() > 180 && right_edge.red() < 80 && right_edge.blue() < 80,
             qPrintable(QStringLiteral("Expected native GL presentation to fill native scene rect; right edge was rgb(%1,%2,%3)")
                        .arg(right_edge.red()).arg(right_edge.green()).arg(right_edge.blue())));

    const int outside_x = 80;
    if (outside_x < view->viewport()->width()) {
        const qreal dpr = viewport->devicePixelRatioF();
        const QColor outside(framebuffer.pixel(qRound(outside_x * dpr),
                                               qRound((raw_size.height() / 2) * dpr)));
        QVERIFY2(outside.red() < 20 && outside.green() < 20 && outside.blue() < 20,
                 qPrintable(QStringLiteral("Expected extra viewport area outside native scene to stay black; outside pixel was rgb(%1,%2,%3)")
                            .arg(outside.red()).arg(outside.green()).arg(outside.blue())));
    }

    GpuDisplayViewport::clearPresentedImage(view, item);
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuViewportPresentsRgb888PatternExactly()
{
    if (QGuiApplication::platformName() == QStringLiteral("offscreen")) {
        QSKIP("GL viewport parity needs a platform plugin that can create an OpenGL context");
    }

    const QMap<QString, QString> expected_hashes = load_expected_hashes();
    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    const QImage submitted = make_presenter_pattern();
    const QImage expected = presenter_expected_orientation(submitted);

    QGraphicsScene scene;
    QPixmap fallback_pixmap(submitted.size());
    fallback_pixmap.fill(Qt::black);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback_pixmap);
    std::unique_ptr<QGraphicsView> view(make_presenter_view(scene, item, submitted.size()));

    QVERIFY(GpuDisplayViewport::installOn(view.get()));
    view->show();
    QApplication::processEvents();
    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingNearest;
    QVERIFY(GpuDisplayViewport::presentImage(view.get(), item, submitted, options));

    const QImage actual = crop_presented_frame(view.get(), item);
    if (actual.isNull()) {
        QSKIP("OpenGL framebuffer capture is unavailable in this environment");
    }

    const QImage trimmed = trim_rounding_border(actual, submitted.size());
    QCOMPARE(trimmed.size(), expected.size());
    QString difference_message;
    QVERIFY2(image_regression::images_match_rgb888(expected, trimmed, 0, &difference_message),
             qPrintable(difference_message));
    assert_expected_hash(expected_hashes, QStringLiteral("gpu.viewport.rgb888.pattern_nearest"), trimmed);

    GpuDisplayViewport::clearPresentedImage(view.get(), item);
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuViewportPresentsRgb16PatternExactly()
{
    if (QGuiApplication::platformName() == QStringLiteral("offscreen")) {
        QSKIP("GL viewport parity needs a platform plugin that can create an OpenGL context");
    }

    const QMap<QString, QString> expected_hashes = load_expected_hashes();
    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    const QImage submitted = make_presenter_pattern();
    const QImage expected = presenter_expected_orientation(submitted);
    const std::vector<uint16_t> rgb16 = make_presenter_pattern_rgb16();

    QGraphicsScene scene;
    QPixmap fallback_pixmap(submitted.size());
    fallback_pixmap.fill(Qt::black);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback_pixmap);
    std::unique_ptr<QGraphicsView> view(make_presenter_view(scene, item, submitted.size()));

    QVERIFY(GpuDisplayViewport::installOn(view.get()));
    view->show();
    QApplication::processEvents();

    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingNearest;
    QVERIFY(GpuDisplayViewport::presentRgb16(view.get(),
                                             item,
                                             rgb16.data(),
                                             submitted.width(),
                                             submitted.height(),
                                             options));

    const QImage actual = crop_presented_frame(view.get(), item);
    if (actual.isNull()) {
        QSKIP("OpenGL framebuffer capture is unavailable in this environment");
    }

    const QImage trimmed = trim_rounding_border(actual, submitted.size());
    QCOMPARE(trimmed.size(), expected.size());
    QString difference_message;
    QVERIFY2(image_regression::images_match_rgb888(expected, trimmed, 0, &difference_message),
             qPrintable(difference_message));
    assert_expected_hash(expected_hashes, QStringLiteral("gpu.viewport.rgb16.pattern_nearest"), trimmed);

    GpuDisplayViewport::clearPresentedImage(view.get(), item);
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuDisplayWindowFramebufferReadbackFailsWithoutActiveWindow()
{
    QVERIFY(!GpuDisplayWindow::isActive());

    QImage grabbed;
    QString reason;
    QVERIFY(!GpuDisplayWindow::grabPresentedFramebufferIfActive(&grabbed, &reason));
    QVERIFY(grabbed.isNull());
    QVERIFY(!reason.isEmpty());
}

void GuiSmokeTest::gpuDisplayWindowGrabsPresentedFramebufferReadback()
{
    MLV_SKIP_OR_FAIL_IF_OFFSCREEN("GL window framebuffer readback needs a platform plugin that can create an OpenGL context");

    qputenv(GpuDisplayWindow::environmentVariableName(), QByteArrayLiteral("1"));

    auto host = std::make_unique<QWidget>();
    auto *layout = new QVBoxLayout(host.get());
    auto *view = new QGraphicsView(host.get());
    layout->addWidget(view);
    host->resize(64, 64);

    QVERIFY(GpuDisplayWindow::installInPreview(view));
    host->show();
    QApplication::processEvents();
    static_cast<void>(QTest::qWaitForWindowExposed(host.get()));
    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }

    QImage submitted(32, 32, QImage::Format_RGB888);
    submitted.fill(qRgb(10, 200, 30));
    QVERIFY(GpuDisplayWindow::presentImageIfActive(submitted));

    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }

    QImage grabbed;
    QString reason;
    const bool ok = GpuDisplayWindow::grabPresentedFramebufferIfActive(&grabbed, &reason);
    if (!ok || grabbed.isNull()) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("OpenGL framebuffer capture is unavailable in this environment");
    }
    QVERIFY(reason.isEmpty());
    QVERIFY(grabbed.width() > 0);
    QVERIFY(grabbed.height() > 0);

    const QColor center(grabbed.pixel(grabbed.width() / 2, grabbed.height() / 2));
    QVERIFY2(center.green() > 150 && center.red() < 80 && center.blue() < 80,
             qPrintable(QStringLiteral("Expected the GL window readback to show the presented frame; "
                                        "center pixel was rgb(%1,%2,%3)")
                        .arg(center.red()).arg(center.green()).arg(center.blue())));

    host.reset();
    QVERIFY(!GpuDisplayWindow::isActive());
    qunsetenv(GpuDisplayWindow::environmentVariableName());
}

void GuiSmokeTest::gpuDisplayWindowCapturePresentsPendingFrameAsRealPaint()
{
    MLV_SKIP_OR_FAIL_IF_OFFSCREEN("GL window framebuffer readback needs a platform plugin that can create an OpenGL context");

    qputenv(GpuDisplayWindow::environmentVariableName(), QByteArrayLiteral("1"));

    auto host = std::make_unique<QWidget>();
    auto *layout = new QVBoxLayout(host.get());
    auto *view = new QGraphicsView(host.get());
    layout->addWidget(view);
    host->resize(64, 64);

    QVERIFY(GpuDisplayWindow::installInPreview(view));
    host->show();
    QApplication::processEvents();
    static_cast<void>(QTest::qWaitForWindowExposed(host.get()));
    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }

    // Present frame A and let its real paint event actually run.
    QImage frameA(32, 32, QImage::Format_RGB888);
    frameA.fill(qRgb(10, 200, 30));   // green
    const quint64 serialA = 111;
    QVERIFY(GpuDisplayWindow::presentImageIfActive(frameA, QSize(), serialA));
    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }

    QImage grabbedA;
    QString reasonA;
    quint64 capturedSerialA = 0;
    bool capturedSerialValidA = false;
    const bool okA = GpuDisplayWindow::grabPresentedFramebufferIfActive(
        &grabbedA, &reasonA, &capturedSerialA, &capturedSerialValidA);
    if (!okA || grabbedA.isNull()) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("OpenGL framebuffer capture is unavailable in this environment");
    }
    QVERIFY(capturedSerialValidA);
    QCOMPARE(capturedSerialA, serialA);
    {
        const QColor center(grabbedA.pixel(grabbedA.width() / 2, grabbedA.height() / 2));
        QVERIFY2(center.green() > 150 && center.red() < 80 && center.blue() < 80,
                 qPrintable(QStringLiteral("Expected the initial capture to show frame A; center pixel was rgb(%1,%2,%3)")
                            .arg(center.red()).arg(center.green()).arg(center.blue())));
    }

    // Submit frame B but deliberately do NOT pump the event loop before capturing -- its
    // real Qt-driven paint event has not run yet. Round 5's capture-by-real-present design
    // (PR147 round 4/5 review) means a capture is ITSELF a real, synchronous paintGL()+swap,
    // so it promotes B exactly as a real paint would: the capture must show B, by both
    // pixels and serial, one paint earlier than Qt's own event loop otherwise would have
    // painted it. This deliberately supersedes the round-4 behavior (capture bound to the
    // last Qt-driven paint, A) -- there is no more capture/paint race to close, because
    // capture and paint are now the same synchronous call and can never disagree.
    QImage frameB(32, 32, QImage::Format_RGB888);
    frameB.fill(qRgb(200, 10, 30));   // red
    const quint64 serialB = 222;
    QVERIFY(GpuDisplayWindow::presentImageIfActive(frameB, QSize(), serialB));

    QImage grabbedDuring;
    QString reasonDuring;
    quint64 capturedSerialDuring = 0;
    bool capturedSerialValidDuring = false;
    const bool okDuring = GpuDisplayWindow::grabPresentedFramebufferIfActive(
        &grabbedDuring, &reasonDuring, &capturedSerialDuring, &capturedSerialValidDuring);
    QVERIFY(okDuring);
    QVERIFY(!grabbedDuring.isNull());
    QVERIFY(capturedSerialValidDuring);
    QCOMPARE(capturedSerialDuring, serialB);
    {
        const QColor center(grabbedDuring.pixel(grabbedDuring.width() / 2, grabbedDuring.height() / 2));
        QVERIFY2(center.red() > 150 && center.green() < 80 && center.blue() < 80,
                 qPrintable(QStringLiteral("Expected a capture taken before frame B's Qt-driven paint event to "
                                            "already show frame B (capture is itself a real paint+swap); center "
                                            "pixel was rgb(%1,%2,%3)")
                            .arg(center.red()).arg(center.green()).arg(center.blue())));
    }

    // A second, immediately-repeated capture with nothing new submitted must be stable --
    // idempotent, not a further one-paint-early promotion of anything.
    QImage grabbedDuringAgain;
    QString reasonDuringAgain;
    quint64 capturedSerialDuringAgain = 0;
    bool capturedSerialValidDuringAgain = false;
    const bool okDuringAgain = GpuDisplayWindow::grabPresentedFramebufferIfActive(
        &grabbedDuringAgain, &reasonDuringAgain, &capturedSerialDuringAgain, &capturedSerialValidDuringAgain);
    QVERIFY(okDuringAgain);
    QVERIFY(capturedSerialValidDuringAgain);
    QCOMPARE(capturedSerialDuringAgain, serialB);

    // Now let Qt's own paint-event cycle actually run, and confirm it settles on the SAME
    // content the capture above already forced -- no divergence between a capture-triggered
    // present and the window's own subsequent normal paint.
    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }

    QImage grabbedB;
    QString reasonB;
    quint64 capturedSerialB = 0;
    bool capturedSerialValidB = false;
    const bool okB = GpuDisplayWindow::grabPresentedFramebufferIfActive(
        &grabbedB, &reasonB, &capturedSerialB, &capturedSerialValidB);
    QVERIFY(okB);
    QVERIFY(!grabbedB.isNull());
    QVERIFY(capturedSerialValidB);
    QCOMPARE(capturedSerialB, serialB);
    {
        const QColor center(grabbedB.pixel(grabbedB.width() / 2, grabbedB.height() / 2));
        QVERIFY2(center.red() > 150 && center.green() < 80 && center.blue() < 80,
                 qPrintable(QStringLiteral("Expected the capture after frame B's real paint to still show frame B; "
                                            "center pixel was rgb(%1,%2,%3)")
                            .arg(center.red()).arg(center.green()).arg(center.blue())));
    }

    host.reset();
    QVERIFY(!GpuDisplayWindow::isActive());
    qunsetenv(GpuDisplayWindow::environmentVariableName());
}

void GuiSmokeTest::gpuDisplayWindowCaptureIgnoresFailedReconTextureSubmit()
{
    // This test binary always stubs the CUDA/AMaZE GPU-recon backend to fail
    // (tests/gui/raw_processing_gpu_preview_stubs.cpp: llrpGpuPlaybackReconRunGlTexture and
    // llrpGpuPlaybackReconRunDeviceBayer16 both unconditionally report failure), so a
    // SUCCESSFUL GPU-recon texture present cannot be exercised here on any platform. What
    // this test instead proves is the concrete regression PR147 round 4/5 review flagged
    // for the texture route: a FAILED recon submit attempt (which threads a presentation
    // serial the same way a successful one now does -- see
    // GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture) must never corrupt
    // the previously-presented frame's identity, and must never leave a stale-but-"valid"
    // serial bound to content that never actually presented.
    MLV_SKIP_OR_FAIL_IF_OFFSCREEN("GL window framebuffer readback needs a platform plugin that can create an OpenGL context");

    qputenv(GpuDisplayWindow::environmentVariableName(), QByteArrayLiteral("1"));

    auto host = std::make_unique<QWidget>();
    auto *layout = new QVBoxLayout(host.get());
    auto *view = new QGraphicsView(host.get());
    layout->addWidget(view);
    host->resize(64, 64);

    QVERIFY(GpuDisplayWindow::installInPreview(view));
    host->show();
    QApplication::processEvents();
    static_cast<void>(QTest::qWaitForWindowExposed(host.get()));
    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }

    // Present frame A (QImage route) and let its real paint event run.
    QImage frameA(32, 32, QImage::Format_RGB888);
    frameA.fill(qRgb(10, 200, 30));   // green
    const quint64 serialA = 111;
    QVERIFY(GpuDisplayWindow::presentImageIfActive(frameA, QSize(), serialA));
    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }

    QImage grabbedA;
    QString reasonA;
    quint64 capturedSerialA = 0;
    bool capturedSerialValidA = false;
    const bool okA = GpuDisplayWindow::grabPresentedFramebufferIfActive(
        &grabbedA, &reasonA, &capturedSerialA, &capturedSerialValidA);
    if (!okA || grabbedA.isNull()) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("OpenGL framebuffer capture is unavailable in this environment");
    }
    QVERIFY(capturedSerialValidA);
    QCOMPARE(capturedSerialA, serialA);

    // Attempt a GPU-recon texture-route submit with a DIFFERENT serial. The stub backend
    // guarantees this fails -- assert that explicitly, so this test cannot silently pass
    // because the attempt happened to succeed against a real backend elsewhere.
    const uint16_t rawInput[] = { 1024, 2048, 3072, 4096 };
    llrpGpuPlaybackReconState_t reconState;
    memset(&reconState, 0, sizeof(reconState));
    reconState.valid = 1;
    reconState.width = 2;
    reconState.height = 2;
    const double wbMultipliers[3] = { 1.0, 1.0, 1.0 };
    QString reconReason;
    llrpGpuPlaybackReconTiming_t reconTiming;
    memset(&reconTiming, 0, sizeof(reconTiming));
    QString reconHandoffMode;
    const quint64 serialRecon = 222;
    // Usable preview-processing options: this test's failure must come from the stub
    // AMaZE backend (asserted below via reconReason), never from the fail-closed
    // missing-processing-options gate GPU-TEXNR-S1-DARK-GREEN-1 added ahead of it.
    GpuDisplayViewport::PresentationOptions reconOptions;
    reconOptions.previewProcessing = make_synthetic_preview_processing_config();
    const bool reconPresented =
        GpuDisplayWindow::presentGpuPlaybackReconAmazePostWbTextureIfActive(
            rawInput, 4, &reconState, 0, wbMultipliers, reconOptions,
            &reconReason, &reconTiming, &reconHandoffMode,
            false, nullptr, 0, 0, 0, 0, serialRecon);
    QVERIFY(!reconPresented);
    QVERIFY(!reconReason.isEmpty());
    QVERIFY2(!reconReason.contains(QStringLiteral("missing_processing_options")),
             qPrintable(QStringLiteral("Expected the stub AMaZE backend to fail this submit, not the "
                                        "fail-closed processing-options gate; reason=%1").arg(reconReason)));

    // The failed submit must leave the previously-presented frame A completely intact --
    // never a stale-but-valid serial bound to content that never actually presented, and
    // never a corrupted/blank capture.
    QImage grabbedAfterFailedRecon;
    QString reasonAfterFailedRecon;
    quint64 capturedSerialAfterFailedRecon = 0;
    bool capturedSerialValidAfterFailedRecon = false;
    const bool okAfterFailedRecon = GpuDisplayWindow::grabPresentedFramebufferIfActive(
        &grabbedAfterFailedRecon, &reasonAfterFailedRecon,
        &capturedSerialAfterFailedRecon, &capturedSerialValidAfterFailedRecon);
    QVERIFY(okAfterFailedRecon);
    QVERIFY(!grabbedAfterFailedRecon.isNull());
    QVERIFY(capturedSerialValidAfterFailedRecon);
    QCOMPARE(capturedSerialAfterFailedRecon, serialA);
    QVERIFY(capturedSerialAfterFailedRecon != serialRecon);
    {
        const QColor center(grabbedAfterFailedRecon.pixel(
            grabbedAfterFailedRecon.width() / 2, grabbedAfterFailedRecon.height() / 2));
        QVERIFY2(center.green() > 150 && center.red() < 80 && center.blue() < 80,
                 qPrintable(QStringLiteral("Expected a capture after a FAILED recon texture submit to still show "
                                            "frame A untouched; center pixel was rgb(%1,%2,%3)")
                            .arg(center.red()).arg(center.green()).arg(center.blue())));
    }

    host.reset();
    QVERIFY(!GpuDisplayWindow::isActive());
    qunsetenv(GpuDisplayWindow::environmentVariableName());
}

void GuiSmokeTest::gpuDisplayWindowRefusesReconTextureWithoutProcessingOptions()
{
    // GPU-TEXNR-S1-DARK-GREEN-1: the original defect was MainWindow.cpp calling the GL-
    // window recon-texture present API with NO preview-processing options at all, so a
    // linear post-WB-undo texture got drawn through the plain passthrough shader (flat
    // dark grey-green). Default-constructed PresentationOptions has
    // previewProcessing.enabled == false -- exactly that shape -- and must now be
    // refused up front rather than ever presented.
    MLV_SKIP_OR_FAIL_IF_OFFSCREEN("GL window texture-present refusal needs a platform plugin that can create an OpenGL context");

    qputenv(GpuDisplayWindow::environmentVariableName(), QByteArrayLiteral("1"));

    auto host = std::make_unique<QWidget>();
    auto *layout = new QVBoxLayout(host.get());
    auto *view = new QGraphicsView(host.get());
    layout->addWidget(view);
    host->resize(64, 64);

    QVERIFY(GpuDisplayWindow::installInPreview(view));
    host->show();
    QApplication::processEvents();
    static_cast<void>(QTest::qWaitForWindowExposed(host.get()));

    const uint16_t rawInput[] = { 1024, 2048, 3072, 4096 };
    llrpGpuPlaybackReconState_t reconState;
    memset(&reconState, 0, sizeof(reconState));
    reconState.valid = 1;
    reconState.width = 2;
    reconState.height = 2;
    const double wbMultipliers[3] = { 1.0, 1.0, 1.0 };
    QString reason;
    const GpuDisplayViewport::PresentationOptions defaultOptions;
    QVERIFY(!defaultOptions.previewProcessing.enabled);
    const bool presented =
        GpuDisplayWindow::presentGpuPlaybackReconAmazePostWbTextureIfActive(
            rawInput, 4, &reconState, 0, wbMultipliers, defaultOptions, &reason);

    QVERIFY(!presented);
    QVERIFY2(reason.contains(QStringLiteral("gpu_window_recon_missing_processing_options")),
             qPrintable(QStringLiteral("Expected the fail-closed gate to refuse a recon texture with no "
                                        "usable preview-processing options; reason=%1").arg(reason)));

    host.reset();
    QVERIFY(!GpuDisplayWindow::isActive());
    qunsetenv(GpuDisplayWindow::environmentVariableName());
}

void GuiSmokeTest::gpuViewportZebraProcessingMatchesCpuReference()
{
    if (QGuiApplication::platformName() == QStringLiteral("offscreen")) {
        QSKIP("GL viewport parity needs a platform plugin that can create an OpenGL context");
    }

    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    QImage submitted(4, 4, QImage::Format_RGB888);
    submitted.fill(Qt::gray);
    submitted.setPixel(0, 0, qRgb(255, 255, 255));
    submitted.setPixel(1, 0, qRgb(0, 0, 0));
    submitted.setPixel(2, 0, qRgb(250, 250, 250));
    submitted.setPixel(3, 0, qRgb(5, 5, 5));

    const QImage expected = presenter_expected_orientation(apply_cpu_zebras(submitted));
    std::vector<uint16_t> rgb16(static_cast<std::size_t>(submitted.width()) * static_cast<std::size_t>(submitted.height()) * 3u, 0);
    for (int y = 0; y < submitted.height(); ++y) {
        for (int x = 0; x < submitted.width(); ++x) {
            const QRgb pixel = submitted.pixel(x, y);
            const int offset = ((y * submitted.width()) + x) * 3;
            rgb16[static_cast<std::size_t>(offset + 0)] = static_cast<uint16_t>(qRed(pixel) * 257u);
            rgb16[static_cast<std::size_t>(offset + 1)] = static_cast<uint16_t>(qGreen(pixel) * 257u);
            rgb16[static_cast<std::size_t>(offset + 2)] = static_cast<uint16_t>(qBlue(pixel) * 257u);
        }
    }

    QGraphicsScene scene;
    QPixmap fallback_pixmap(submitted.size());
    fallback_pixmap.fill(Qt::black);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback_pixmap);
    std::unique_ptr<QGraphicsView> view(make_presenter_view(scene, item, submitted.size()));

    QVERIFY(GpuDisplayViewport::installOn(view.get()));
    view->show();
    QApplication::processEvents();
    const QString renderer = GpuDisplayViewport::rendererDescriptionFor(view.get());
    if (renderer.contains(QStringLiteral("llvmpipe"), Qt::CaseInsensitive)) {
        QSKIP("GPU zebra parity is skipped on llvmpipe because the software GL stack does not produce stable shader-processed output here.");
    }

    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingNearest;
    options.showZebras = true;
    QVERIFY(GpuDisplayViewport::presentRgb16(view.get(),
                                             item,
                                             rgb16.data(),
                                             submitted.width(),
                                             submitted.height(),
                                             options));

    const QImage actual = crop_presented_frame(view.get(), item);
    if (actual.isNull()) {
        QSKIP("OpenGL framebuffer capture is unavailable in this environment");
    }

    const QImage trimmed = trim_rounding_border(actual, submitted.size());
    QCOMPARE(trimmed.size(), expected.size());
    QString difference_message;
    QVERIFY2(image_regression::images_match_rgb888(expected, trimmed, 0, &difference_message),
             qPrintable(difference_message));

    GpuDisplayViewport::clearPresentedImage(view.get(), item);
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuViewportRgb888ZebraProcessingMatchesCpuReference()
{
    if (QGuiApplication::platformName() == QStringLiteral("offscreen")) {
        QSKIP("GL viewport parity needs a platform plugin that can create an OpenGL context");
    }

    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    QImage submitted(4, 4, QImage::Format_RGB888);
    submitted.fill(Qt::gray);
    submitted.setPixel(0, 0, qRgb(255, 255, 255));
    submitted.setPixel(1, 0, qRgb(0, 0, 0));
    submitted.setPixel(2, 0, qRgb(250, 250, 250));
    submitted.setPixel(3, 0, qRgb(5, 5, 5));

    const QImage expected = presenter_expected_orientation(apply_cpu_zebras(submitted));

    QGraphicsScene scene;
    QPixmap fallback_pixmap(submitted.size());
    fallback_pixmap.fill(Qt::black);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback_pixmap);
    std::unique_ptr<QGraphicsView> view(make_presenter_view(scene, item, submitted.size()));

    QVERIFY(GpuDisplayViewport::installOn(view.get()));
    view->show();
    QApplication::processEvents();
    const QString renderer = GpuDisplayViewport::rendererDescriptionFor(view.get());
    if (renderer.contains(QStringLiteral("llvmpipe"), Qt::CaseInsensitive)) {
        QSKIP("GPU zebra parity is skipped on llvmpipe because the software GL stack does not produce stable shader-processed output here.");
    }

    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingNearest;
    options.showZebras = true;
    QVERIFY(GpuDisplayViewport::presentImage(view.get(), item, submitted, options));

    const QImage actual = crop_presented_frame(view.get(), item);
    if (actual.isNull()) {
        QSKIP("OpenGL framebuffer capture is unavailable in this environment");
    }

    const QImage trimmed = trim_rounding_border(actual, submitted.size());
    QCOMPARE(trimmed.size(), expected.size());
    QString difference_message;
    QVERIFY2(image_regression::images_match_rgb888(expected, trimmed, 0, &difference_message),
             qPrintable(difference_message));

    GpuDisplayViewport::clearPresentedImage(view.get(), item);
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuViewportPreviewProcessingMatchesCpuReference()
{
    if (QGuiApplication::platformName() == QStringLiteral("offscreen")) {
        QSKIP("GL viewport parity needs a platform plugin that can create an OpenGL context");
    }

    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    const QImage submitted = make_presenter_pattern();
    const std::vector<uint16_t> rgb16 = make_presenter_pattern_rgb16();
    const GpuPreviewProcessingConfig processing = make_synthetic_preview_processing_config();
    std::vector<uint16_t> expected16(rgb16.size(), 0);
    gpuPreviewProcessingApplyCpuReference(processing, rgb16.data(), expected16.data(), submitted.width(), submitted.height());
    const QImage expected = presenter_expected_orientation(rgb16_to_qimage(expected16, submitted.width(), submitted.height()));

    QGraphicsScene scene;
    QPixmap fallback_pixmap(submitted.size());
    fallback_pixmap.fill(Qt::black);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback_pixmap);
    std::unique_ptr<QGraphicsView> view(make_presenter_view(scene, item, submitted.size()));

    QVERIFY(GpuDisplayViewport::installOn(view.get()));
    view->show();
    QApplication::processEvents();
    const QString renderer = GpuDisplayViewport::rendererDescriptionFor(view.get());
    if (renderer.contains(QStringLiteral("llvmpipe"), Qt::CaseInsensitive)) {
        QSKIP("GPU preview-processing parity is skipped on llvmpipe because the software GL stack does not produce stable shader output here.");
    }

    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingNearest;
    options.previewProcessing = processing;
    QVERIFY(GpuDisplayViewport::presentRgb16(view.get(), item, rgb16.data(), submitted.width(), submitted.height(), options));

    const QImage actual = crop_presented_frame(view.get(), item);
    if (actual.isNull()) {
        QSKIP("OpenGL framebuffer capture is unavailable in this environment");
    }

    const QImage trimmed = trim_rounding_border(actual, submitted.size());
    QCOMPARE(trimmed.size(), expected.size());
    QString difference_message;
    QVERIFY2(image_regression::images_match_rgb888(expected, trimmed, 1, &difference_message),
             qPrintable(difference_message));

    GpuDisplayViewport::clearPresentedImage(view.get(), item);
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuViewportPreviewProcessingWithZebrasMatchesCpuReference()
{
    if (QGuiApplication::platformName() == QStringLiteral("offscreen")) {
        QSKIP("GL viewport parity needs a platform plugin that can create an OpenGL context");
    }

    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    const QImage submitted = make_presenter_pattern();
    const std::vector<uint16_t> rgb16 = make_presenter_pattern_rgb16();
    const GpuPreviewProcessingConfig processing = make_synthetic_preview_processing_config();
    std::vector<uint16_t> expected16(rgb16.size(), 0);
    gpuPreviewProcessingApplyCpuReference(processing, rgb16.data(), expected16.data(), submitted.width(), submitted.height());
    const QImage expected = presenter_expected_orientation(apply_cpu_zebras(rgb16_to_qimage(expected16, submitted.width(), submitted.height())));

    QGraphicsScene scene;
    QPixmap fallback_pixmap(submitted.size());
    fallback_pixmap.fill(Qt::black);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback_pixmap);
    std::unique_ptr<QGraphicsView> view(make_presenter_view(scene, item, submitted.size()));

    QVERIFY(GpuDisplayViewport::installOn(view.get()));
    view->show();
    QApplication::processEvents();
    const QString renderer = GpuDisplayViewport::rendererDescriptionFor(view.get());
    if (renderer.contains(QStringLiteral("llvmpipe"), Qt::CaseInsensitive)) {
        QSKIP("GPU preview-processing parity is skipped on llvmpipe because the software GL stack does not produce stable shader output here.");
    }

    GpuDisplayViewport::PresentationOptions options;
    options.samplingMode = GpuDisplayViewport::SamplingNearest;
    options.showZebras = true;
    options.previewProcessing = processing;
    QVERIFY(GpuDisplayViewport::presentRgb16(view.get(), item, rgb16.data(), submitted.width(), submitted.height(), options));

    const QImage actual = crop_presented_frame(view.get(), item);
    if (actual.isNull()) {
        QSKIP("OpenGL framebuffer capture is unavailable in this environment");
    }

    const QImage trimmed = trim_rounding_border(actual, submitted.size());
    QCOMPARE(trimmed.size(), expected.size());
    QString difference_message;
    QVERIFY2(image_regression::images_match_rgb888(expected, trimmed, 1, &difference_message),
             qPrintable(difference_message));

    GpuDisplayViewport::clearPresentedImage(view.get(), item);
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuPreviewProcessingLutReadinessReflectsSignatureNotJustPointers()
{
    // GPU-TEXNR-S1-DARK-GREEN-1 round 2 (sol minor 3): the round-1 version of this test
    // (gpuPreviewProcessingLutReadinessMatchesForBothPresenters) was tautological -- it
    // constructed QOpenGLTexture wrappers that were never create()'d and asserted the
    // predicate called them "ready", which encoded the pointer-only fail-open defect
    // sol's round-1 major finding was about rather than detecting it. This test instead
    // proves the fix: gpuPreviewProcessingLutTextureSetReady now requires
    // signatureValid, which gpuPreviewProcessingUpdateLutTextureSet only sets once every
    // LUT texture is confirmed GL-created (see
    // gpuPreviewProcessingLutTextureSetFailsClosedOnMissingUploadAndContextLoss for the
    // live-GL path that exercises that production function directly). No live GL
    // context is needed here because the predicate itself only reads struct fields --
    // this is pure logic coverage of the signature/pointer combination table, including
    // the shapes an injected failed upload or a context-loss teardown would leave
    // behind.
    const GpuPreviewProcessingConfig config = make_synthetic_preview_processing_config();

    GpuPreviewProcessingLutTextureSet readySet;
    readySet.levels = reinterpret_cast<QOpenGLTexture *>(0x1);
    readySet.matrixR = reinterpret_cast<QOpenGLTexture *>(0x1);
    readySet.matrixG = reinterpret_cast<QOpenGLTexture *>(0x1);
    readySet.matrixB = reinterpret_cast<QOpenGLTexture *>(0x1);
    readySet.gamma = reinterpret_cast<QOpenGLTexture *>(0x1);
    readySet.signature = config.signature;
    readySet.signatureValid = true;
    QVERIFY(gpuPreviewProcessingLutTextureSetReady(readySet, config));

    // Shape of an injected failed upload: allocation returned non-null pointers for
    // every texture except one (a partial GL failure), so signatureValid was correctly
    // never set -- must NOT be ready even though 4 of 5 pointers are non-null.
    GpuPreviewProcessingLutTextureSet failedUploadSet = readySet;
    failedUploadSet.signatureValid = false;
    QVERIFY(!gpuPreviewProcessingLutTextureSetReady(failedUploadSet, config));

    // Shape of a fully failed upload: no textures survived, signature never stamped --
    // this is what gpuPreviewProcessingUpdateLutTextureSet leaves behind today on any
    // allocation failure (it destroys the whole set rather than a partial one).
    GpuPreviewProcessingLutTextureSet emptyFailedSet;
    QVERIFY(!gpuPreviewProcessingLutTextureSetReady(emptyFailedSet, config));

    // Shape left behind by a context-loss teardown (GpuDisplayWindow/GpuDisplayViewport
    // ::cleanupGLResources -> gpuPreviewProcessingDestroyLutTextureSet leaves every
    // pointer null and signatureValid false -- verified directly against a live GL
    // context, not fake pointers, in
    // gpuPreviewProcessingLutTextureSetFailsClosedOnMissingUploadAndContextLoss). A
    // stale non-zero cached signature surviving the teardown must not matter.
    GpuPreviewProcessingLutTextureSet contextLostSet;
    contextLostSet.signature = config.signature;
    contextLostSet.signatureValid = false;
    QVERIFY(!gpuPreviewProcessingLutTextureSetReady(contextLostSet, config));

    // A stale signature match with missing pointers (one dropped) must still refuse --
    // signatureValid alone is not sufficient, exactly mirroring the round-1 test's
    // missing-gamma case.
    GpuPreviewProcessingLutTextureSet missingGammaSet = readySet;
    missingGammaSet.gamma = nullptr;
    QVERIFY(!gpuPreviewProcessingLutTextureSetReady(missingGammaSet, config));

    // Disabled config must refuse regardless of an otherwise-ready set.
    GpuPreviewProcessingConfig disabledConfig = config;
    disabledConfig.enabled = false;
    QVERIFY(!gpuPreviewProcessingLutTextureSetReady(readySet, disabledConfig));
}

void GuiSmokeTest::gpuPreviewProcessingLutTextureSetFailsClosedOnMissingUploadAndContextLoss()
{
    // GPU-TEXNR-S1-DARK-GREEN-1 round 2 (sol minor 3): exercises the REAL production
    // functions (gpuPreviewProcessingUpdateLutTextureSet / ...Ready /
    // ...DestroyLutTextureSet) against a live GL context, rather than hand-built
    // struct shapes -- this is the GL-dependent half sol asked for: a test that fails
    // when an upload is missing, and after a simulated context loss.
    MLV_SKIP_OR_FAIL_IF_OFFSCREEN("GPU LUT texture set upload needs a platform plugin that can create an OpenGL context");

    QOffscreenSurface surface;
    surface.setFormat(QSurfaceFormat::defaultFormat());
    surface.create();
    if (!surface.isValid()) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("QOffscreenSurface creation failed in this environment");
    }
    QOpenGLContext context;
    context.setFormat(surface.requestedFormat());
    if (!context.create() || !context.makeCurrent(&surface)) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("QOpenGLContext creation/makeCurrent failed in this environment");
    }

    GpuPreviewProcessingLutTextureSet set;
    const GpuPreviewProcessingConfig validConfig = make_synthetic_preview_processing_config();

    // Real success: a full upload against a live context must actually report ready.
    gpuPreviewProcessingUpdateLutTextureSet(set, validConfig);
    QVERIFY(gpuPreviewProcessingLutTextureSetReady(set, validConfig));

    // Missing upload: undersized LUT bytes (as if the source LUT was never populated)
    // must be refused rather than silently keeping the previous upload's readiness.
    GpuPreviewProcessingConfig missingUploadConfig = validConfig;
    missingUploadConfig.gammaLut.clear();
    missingUploadConfig.signature = validConfig.signature + 1;
    gpuPreviewProcessingUpdateLutTextureSet(set, missingUploadConfig);
    QVERIFY(!gpuPreviewProcessingLutTextureSetReady(set, missingUploadConfig));

    // Rebuild for the context-loss case.
    gpuPreviewProcessingUpdateLutTextureSet(set, validConfig);
    QVERIFY(gpuPreviewProcessingLutTextureSetReady(set, validConfig));

    // Simulated context loss: this is exactly the call
    // GpuDisplayWindow/GpuDisplayViewport now make from their
    // QOpenGLContext::aboutToBeDestroyed handler (cleanupGLResources). Readiness must
    // drop immediately, and a subsequent rebuild against the still-current context
    // (standing in for "a new context was made current after recreation") must recover.
    gpuPreviewProcessingDestroyLutTextureSet(set);
    QVERIFY(!gpuPreviewProcessingLutTextureSetReady(set, validConfig));

    gpuPreviewProcessingUpdateLutTextureSet(set, validConfig);
    QVERIFY(gpuPreviewProcessingLutTextureSetReady(set, validConfig));

    gpuPreviewProcessingDestroyLutTextureSet(set);
    context.doneCurrent();
}

void GuiSmokeTest::gpuPreviewProcessingReconRefusalMatchesForBothPresentersOnInjectedUploadFailure()
{
    // GPU-TEXNR-S1-DARK-GREEN-1 round 3 (sol major, tests_run gap), tightened round 4
    // (sol minor + fable minor 3): round 2's viewport paintGL() drew a GPU-recon/AMaZE
    // texture through the shared shader's previewProcessingEnabled=0 branch whenever its
    // LUT set was not ready, instead of refusing to present it -- the same fail-open
    // shape GpuDisplayWindow::paintGL's reconRefused re-check already closed for the
    // window route. Round 3's test re-encoded that formula in two local lambdas rather
    // than exercising production code, so it could not catch a divergence in either
    // presenter; round 4 removes the re-encoding entirely and calls the SAME production
    // function (gpuPreviewProcessingReconTexturePresentationRefused) that both
    // GpuDisplayWindow::paintGL and GpuDisplayViewport::paintGL now call for their
    // re-check, so a regression in either presenter's re-check is necessarily a
    // regression in this function too, against an injected-upload-failure shape
    // (signatureValid=false despite an otherwise usable config and mostly-populated
    // pointers, exactly what gpuPreviewProcessingUpdateLutTextureSet leaves behind on a
    // partial GL failure).
    const GpuPreviewProcessingConfig config = make_synthetic_preview_processing_config();

    GpuPreviewProcessingLutTextureSet failedUploadSet;
    failedUploadSet.levels = reinterpret_cast<QOpenGLTexture *>(0x1);
    failedUploadSet.matrixR = reinterpret_cast<QOpenGLTexture *>(0x1);
    failedUploadSet.matrixG = reinterpret_cast<QOpenGLTexture *>(0x1);
    failedUploadSet.matrixB = reinterpret_cast<QOpenGLTexture *>(0x1);
    failedUploadSet.gamma = reinterpret_cast<QOpenGLTexture *>(0x1);
    failedUploadSet.signature = config.signature;
    failedUploadSet.signatureValid = false;   // the shape an injected upload failure leaves behind

    GpuPreviewProcessingLutTextureSet readySet = failedUploadSet;
    readySet.signatureValid = true;

    // Presenting a recon/AMaZE texture with a failed upload: the shared decision refuses.
    QVERIFY(gpuPreviewProcessingReconTexturePresentationRefused(true, failedUploadSet, config));

    // Presenting a recon/AMaZE texture with a successful upload: it does not refuse.
    QVERIFY(!gpuPreviewProcessingReconTexturePresentationRefused(true, readySet, config));

    // NOT presenting a recon/AMaZE texture (e.g. the ordinary already-processed QImage
    // route): never refuse just because grading LUTs aren't ready -- that content is
    // already display-referred and correct on its own -- regardless of set readiness.
    QVERIFY(!gpuPreviewProcessingReconTexturePresentationRefused(false, failedUploadSet, config));
    QVERIFY(!gpuPreviewProcessingReconTexturePresentationRefused(false, readySet, config));

    // A single missing LUT pointer (not just signatureValid) must also refuse.
    GpuPreviewProcessingLutTextureSet missingMatrixSet = readySet;
    missingMatrixSet.matrixG = nullptr;
    QVERIFY(gpuPreviewProcessingReconTexturePresentationRefused(true, missingMatrixSet, config));

    // A disabled processing config refuses too, even with an otherwise-ready set.
    GpuPreviewProcessingConfig disabledConfig = config;
    disabledConfig.enabled = false;
    QVERIFY(gpuPreviewProcessingReconTexturePresentationRefused(true, readySet, disabledConfig));

    // The shared decision is exactly the negation of readiness whenever presenting.
    QCOMPARE(gpuPreviewProcessingReconTexturePresentationRefused(true, failedUploadSet, config),
             !gpuPreviewProcessingLutTextureSetReady(failedUploadSet, config));
}

void GuiSmokeTest::gpuViewportRefusesReconTextureDrawWhenLutReadinessIsFalse()
{
    // GPU-TEXNR-S1-DARK-GREEN-1 round 3 (sol major): exercises the REAL
    // GpuDisplayViewport::paintGL() fail-closed re-check added this round, against a live
    // GL context. This test binary's CUDA/AMaZE backend is stubbed to always fail (see
    // gpuDisplayWindowCaptureIgnoresFailedReconTextureSubmit), so a successful recon
    // texture submit can't be driven through the public present*() API here; instead
    // (GuiSmokeTest is a friend of GpuDisplayViewport) this drives the viewport's private
    // state directly into exactly the shape a successful recon handoff with a failed LUT
    // upload leaves behind -- a real, currently-bound GL texture, the recon flag set, and
    // an LUT set matching gpuPreviewProcessingDestroyLutTextureSet's post-failure shape --
    // then calls the real paintGL() and asserts it refuses to present rather than drawing
    // through the shared shader's previewProcessingEnabled=0 branch.
    MLV_SKIP_OR_FAIL_IF_OFFSCREEN("GL viewport fail-closed draw check needs a platform plugin that can create an OpenGL context");

    qputenv(GpuDisplayViewport::environmentVariableName(), QByteArrayLiteral("1"));

    QGraphicsScene scene;
    QPixmap fallback_pixmap(4, 4);
    fallback_pixmap.fill(Qt::black);
    QGraphicsPixmapItem *item = scene.addPixmap(fallback_pixmap);
    std::unique_ptr<QGraphicsView> view(make_presenter_view(scene, item, QSize(4, 4)));

    QVERIFY(GpuDisplayViewport::installOn(view.get()));
    view->show();
    QApplication::processEvents();

    // Seed a real GL program/texture via the ordinary image route first.
    QImage seed(4, 4, QImage::Format_RGB888);
    seed.fill(Qt::black);
    QVERIFY(GpuDisplayViewport::presentImage(view.get(), item, seed));
    const QImage seeded = crop_presented_frame(view.get(), item);
    if (seeded.isNull()) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("OpenGL framebuffer capture is unavailable in this environment");
    }
    QVERIFY(GpuDisplayViewport::isTexturePresentationActive(view.get()));

    auto *viewport = qobject_cast<GpuDisplayViewport *>(view->viewport());
    QVERIFY(viewport);
    viewport->makeCurrent();
    viewport->destroyProcessingTextures();   // leaves m_lutSet exactly as a failed upload would
    viewport->m_presentationOptions.previewProcessing = make_synthetic_preview_processing_config();
    viewport->m_pendingTextureFromGpuRecon = true;
    viewport->m_pendingTextureFromGpuAmaze = false;
    viewport->m_textureDirty = false;           // the (real) texture is already "uploaded"
    viewport->m_processingTexturesDirty = false; // don't let updateTextureIfNeeded rebuild m_lutSet back to ready
    viewport->doneCurrent();

    viewport->update();
    QApplication::processEvents();
    viewport->repaint();
    QApplication::processEvents();

    QVERIFY2(!GpuDisplayViewport::isTexturePresentationActive(view.get()),
             "Expected paintGL() to refuse presenting a GPU-recon texture with an unready LUT set "
             "rather than drawing it through the shared shader's previewProcessingEnabled=0 branch.");

    GpuDisplayViewport::clearPresentedImage(view.get(), item);
    qunsetenv(GpuDisplayViewport::environmentVariableName());
}

void GuiSmokeTest::gpuDisplayWindowRecoversRetainedQImageAfterContextLossTeardown()
{
    // GPU-TEXNR-S1-DARK-GREEN-1 round 3 (sol minor 1 / fable evidence gap): round 2's
    // context-loss coverage
    // (gpuPreviewProcessingLutTextureSetFailsClosedOnMissingUploadAndContextLoss) only
    // simulated cleanupGLResources()'s EFFECT by calling the free LUT-set functions
    // directly; it never called the real presenter teardown function, so it could not
    // catch either fix this round closes: (1) cleanupGLResources() must makeCurrent()
    // before deleting GL wrappers, and (2) it must re-arm m_textureDirty for a retained
    // QImage so the next paint re-uploads instead of presenting blank. This test presents
    // a real frame, then calls the REAL GpuDisplayWindow::cleanupGLResources() directly
    // (GuiSmokeTest is now a friend) -- exactly what the QOpenGLContext::aboutToBeDestroyed
    // handler does on a genuine context recreation -- and asserts the SAME frame
    // reappears on the next real paint with NO resubmission.
    MLV_SKIP_OR_FAIL_IF_OFFSCREEN("GL window context-loss recovery needs a platform plugin that can create an OpenGL context");

    qputenv(GpuDisplayWindow::environmentVariableName(), QByteArrayLiteral("1"));

    auto host = std::make_unique<QWidget>();
    auto *layout = new QVBoxLayout(host.get());
    auto *view = new QGraphicsView(host.get());
    layout->addWidget(view);
    host->resize(64, 64);

    QVERIFY(GpuDisplayWindow::installInPreview(view));
    host->show();
    QApplication::processEvents();
    static_cast<void>(QTest::qWaitForWindowExposed(host.get()));
    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }

    QImage frameA(32, 32, QImage::Format_RGB888);
    frameA.fill(qRgb(10, 200, 30));   // green
    const quint64 serialA = 4242;
    QVERIFY(GpuDisplayWindow::presentImageIfActive(frameA, QSize(), serialA));
    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }

    QImage grabbedBefore;
    QString reasonBefore;
    quint64 serialBefore = 0;
    bool serialValidBefore = false;
    const bool okBefore = GpuDisplayWindow::grabPresentedFramebufferIfActive(
        &grabbedBefore, &reasonBefore, &serialBefore, &serialValidBefore);
    if (!okBefore || grabbedBefore.isNull()) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("OpenGL framebuffer capture is unavailable in this environment");
    }
    QVERIFY(serialValidBefore);
    QCOMPARE(serialBefore, serialA);

    GpuDisplayWindow *win = nullptr;
    for (QWindow *w : QGuiApplication::allWindows()) {
        if (GpuDisplayWindow *candidate = qobject_cast<GpuDisplayWindow *>(w)) {
            win = candidate;
            break;
        }
    }
    if (!win) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("Could not locate the active GpuDisplayWindow's native window in this environment");
    }

    // The real teardown the aboutToBeDestroyed handler runs on a genuine context
    // recreation -- destroys the GL texture/programs/LUT set, and (this round's fix)
    // re-arms m_textureDirty because m_pendingImage (frame A) is still retained.
    win->makeCurrent();
    win->cleanupGLResources();
    win->doneCurrent();
    QVERIFY2(!win->m_pendingImage.isNull(),
             "Test setup expected frame A to still be retained in m_pendingImage after teardown");
    QVERIFY2(win->m_textureDirty,
             "Expected cleanupGLResources() to re-arm m_textureDirty for the retained QImage");

    // No resubmission here -- the next real paint alone must recover frame A.
    QImage grabbedAfter;
    QString reasonAfter;
    quint64 serialAfter = 0;
    bool serialValidAfter = false;
    const bool okAfter = GpuDisplayWindow::grabPresentedFramebufferIfActive(
        &grabbedAfter, &reasonAfter, &serialAfter, &serialValidAfter);
    if (!okAfter || grabbedAfter.isNull()) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("OpenGL framebuffer capture is unavailable after teardown in this environment");
    }
    QVERIFY2(serialValidAfter && serialAfter == serialA,
             qPrintable(QStringLiteral("Expected frame A's presentation serial to survive teardown without "
                                        "resubmission; serialValid=%1 serial=%2")
                        .arg(serialValidAfter).arg(serialAfter)));
    {
        // Frame A was filled green (qRgb(10, 200, 30)) above -- assert that colour comes
        // back, not an unrelated one, so this test is internally consistent. Checking for
        // non-black and green-dominant (rather than an exact match) is also what makes
        // this fail if cleanupGLResources() did NOT re-arm m_textureDirty: with m_texture
        // destroyed and never re-uploaded, paintGL()'s "!m_texture" early-return clears to
        // black (glClearColor(0,0,0,1) above) instead of drawing frame A, so a stale/
        // missing upload shows as a near-black center pixel and this assertion catches it.
        const QColor center(grabbedAfter.pixel(grabbedAfter.width() / 2, grabbedAfter.height() / 2));
        QVERIFY2(center.green() > 150 && center.red() < 80 && center.blue() < 80,
                 qPrintable(QStringLiteral("Expected frame A (green, rgb(10,200,30)) to reappear after "
                                            "context-loss teardown with no resubmission; center pixel was "
                                            "rgb(%1,%2,%3)")
                            .arg(center.red()).arg(center.green()).arg(center.blue())));
    }

    host.reset();
    QVERIFY(!GpuDisplayWindow::isActive());
    qunsetenv(GpuDisplayWindow::environmentVariableName());
}

void GuiSmokeTest::gpuPreviewProcessingCpuReferenceCorrectsPostWbUndoGreenCast()
{
    // GPU-TEXNR-S1-DARK-GREEN-1 root cause: the GL-window texture route was drawing the
    // CUDA AMaZE post-WB-undo texture -- linear camera RGB, WB undone and black re-added
    // by k_pack_rgb16_to_rgba16_post_wb_undo -- through a passthrough shader instead of
    // the preview-processing shader's levels/matrix/WB/gamma LUT math, which produced a
    // flat dark grey-green picture ("floor 32/255 = 14-bit black 2048; G~B>R = pre-WB
    // camera green" per the root-cause note). gpuPreviewProcessingApplyCpuReference is
    // the CPU ground truth for the exact LUT/WB math the shared display shader runs (see
    // gpuViewportPreviewProcessingMatchesCpuReference), so this proves that math, driven
    // by a WB-correcting config, actually turns a synthetic pre-WB-green post-WB-undo
    // pixel into a red-dominant/neutral one -- the correction GpuDisplayWindow's
    // recon-texture route was previously skipping entirely.
    const int width = 2;
    const int height = 2;
    // 14-bit black level 2048, rescaled into the 16-bit space these buffers use.
    const uint16_t black16 = static_cast<uint16_t>(std::lround((2048.0 / 16383.0) * 65535.0));
    std::vector<uint16_t> inputRgb16(static_cast<std::size_t>(width) * height * 3u, 0);
    for (int i = 0; i < width * height; ++i) {
        inputRgb16[static_cast<std::size_t>(i) * 3 + 0] = black16;                                 // R: at the black floor
        inputRgb16[static_cast<std::size_t>(i) * 3 + 1] = static_cast<uint16_t>(black16 + 12000);  // G: strong pre-WB green
        inputRgb16[static_cast<std::size_t>(i) * 3 + 2] = static_cast<uint16_t>(black16 + 6000);   // B: mid pre-WB
    }
    QVERIFY2(inputRgb16[1] > inputRgb16[0],
             "fixture sanity: the synthetic post-WB-undo pixel must itself show a green cast");

    GpuPreviewProcessingConfig config;
    config.enabled = true;
    config.useCameraMatrix = true;
    config.applyGamutCompression = false;
    config.levelsLut = make_identity_lut_bytes();
    config.matrixLutR = make_identity_lut_bytes();
    config.matrixLutG = make_identity_lut_bytes();
    config.matrixLutB = make_identity_lut_bytes();
    config.gammaLut = make_identity_lut_bytes();
    // A WB correction that boosts red and suppresses green relative to blue, isolating
    // the WB-row effect (the LUTs above are all identity).
    config.properWbMatrix[0] = 1.6f; config.properWbMatrix[1] = 0.0f;  config.properWbMatrix[2] = 0.0f;
    config.properWbMatrix[3] = 0.0f; config.properWbMatrix[4] = 0.55f; config.properWbMatrix[5] = 0.0f;
    config.properWbMatrix[6] = 0.0f; config.properWbMatrix[7] = 0.0f;  config.properWbMatrix[8] = 1.0f;
    config.signature = 1;

    std::vector<uint16_t> outputRgb16(inputRgb16.size(), 0);
    gpuPreviewProcessingApplyCpuReference(config, inputRgb16.data(), outputRgb16.data(), width, height);

    for (int i = 0; i < width * height; ++i) {
        const int r = outputRgb16[static_cast<std::size_t>(i) * 3 + 0];
        const int g = outputRgb16[static_cast<std::size_t>(i) * 3 + 1];
        QVERIFY2(r > g,
                 qPrintable(QStringLiteral("Expected the WB-correcting CPU reference to turn a pre-WB green "
                                            "cast into red > green (the corrected picture, not the dark-green "
                                            "regression); got r=%1 g=%2").arg(r).arg(g)));
    }
}

void GuiSmokeTest::histogramRegressionMatchesGolden()
{
    const QMap<QString, QString> expected_hashes = load_expected_hashes();
    const QImage source = make_scope_source_image();

    Histogram histogram;
    QImage image = histogram.getHistogramFromImg(const_cast<QImage *>(&source));

    QCOMPARE(image.size(), QSize(511, 140));
    assert_expected_hash(expected_hashes, QStringLiteral("scope.histogram.synthetic_rgb888"), image);
}

void GuiSmokeTest::vectorScopeRegressionMatchesGolden()
{
    const QMap<QString, QString> expected_hashes = load_expected_hashes();
    std::vector<uint8_t> raw = make_scope_raw_pattern(16, 8);

    VectorScope scope(128, 128);
    QImage image = scope.getVectorScopeFromRaw(raw.data(), 16, 8);

    QCOMPARE(image.size(), QSize(128, 128));
    QCOMPARE(image.pixelColor(64, 64), QColor(128, 128, 128));
    assert_expected_hash(expected_hashes, QStringLiteral("scope.vectorscope.synthetic_raw"), image);
}

void GuiSmokeTest::waveformRegressionMatchesGolden()
{
    const QMap<QString, QString> expected_hashes = load_expected_hashes();
    std::vector<uint8_t> raw = make_scope_raw_pattern(16, 8);

    WaveFormMonitor waveform(16);
    QImage image = waveform.getWaveFormMonitorFromRaw(raw.data(), 16, 8);

    QCOMPARE(image.size(), QSize(2, 256));
    QCOMPARE(image.pixelColor(0, 127), QColor(40, 0, 0));
    QCOMPARE(image.pixelColor(0, 255), QColor(40, 80, 80));
    QCOMPARE(image.pixelColor(1, 0), QColor(0, 40, 80));
    QCOMPARE(image.pixelColor(1, 255), QColor(80, 40, 0));
    assert_expected_hash(expected_hashes, QStringLiteral("scope.waveform.synthetic_raw"), image);
}

void GuiSmokeTest::scopesLabelDispatchesRawHistogramExactly()
{
    const QMap<QString, QString> expected_hashes = load_expected_hashes();
    const std::vector<uint8_t> raw = make_scope_raw_pattern(16, 8);
    const QImage actual = render_scopes_label_output(raw, 16, 8, true, true, ScopesLabel::ScopeHistogram);
    QVERIFY(!actual.isNull());
    assert_expected_hash(expected_hashes, QStringLiteral("scopeslabel.raw_histogram"), actual);
}

void GuiSmokeTest::scopesLabelDispatchesRawWaveformExactly()
{
    const std::vector<uint8_t> raw = make_scope_raw_pattern(16, 8);
    const QImage actual = render_scopes_label_output(raw, 16, 8, false, false, ScopesLabel::ScopeWaveForm);
    QVERIFY(!actual.isNull());
    const QImage expected = render_expected_scope_label(raw, 16, 8, false, false, ScopesLabel::ScopeWaveForm);
    const QImage actual_signature = make_scopeslabel_scope_signature(actual);
    const QImage expected_signature = make_scopeslabel_scope_signature(expected);
    QString difference_message;
    QVERIFY2(image_regression::images_match_rgb888(expected_signature, actual_signature, 64, &difference_message),
             qPrintable(difference_message));
}

void GuiSmokeTest::scopesLabelDispatchesRawParadeExactly()
{
    const QMap<QString, QString> expected_hashes = load_expected_hashes();
    const std::vector<uint8_t> raw = make_scope_raw_pattern(16, 8);

    // Prime a separate instance with the same dimensions. A function-static
    // dimension cache used to make the fresh parade label retain its
    // constructor's width-200 WaveFormMonitor and read unstable tail pixels.
    const QImage priming = render_scopes_label_output(raw, 16, 8, false, false, ScopesLabel::ScopeWaveForm);
    QVERIFY(!priming.isNull());

    const QImage actual = render_scopes_label_output(raw, 16, 8, false, false, ScopesLabel::ScopeRgbParade);
    QVERIFY(!actual.isNull());
    const QImage expected = render_expected_scope_label(raw, 16, 8, false, false, ScopesLabel::ScopeRgbParade);
    const QImage actual_signature = make_scopeslabel_scope_signature(actual);
    const QImage expected_signature = make_scopeslabel_scope_signature(expected);
    QString difference_message;
    QVERIFY2(image_regression::images_match_rgb888(expected_signature, actual_signature, 0, &difference_message),
             qPrintable(difference_message));
    assert_expected_hash(expected_hashes,
                         QStringLiteral("scopeslabel.raw_parade.signature"),
                         actual_signature);
}

void GuiSmokeTest::scopesLabelDispatchesRawVectorScopeExactly()
{
    const QMap<QString, QString> expected_hashes = load_expected_hashes();
    const std::vector<uint8_t> raw = make_scope_raw_pattern(16, 8);
    const QImage actual = render_scopes_label_output(raw, 16, 8, false, false, ScopesLabel::ScopeVectorScope);
    QVERIFY(!actual.isNull());
    assert_expected_hash(expected_hashes, QStringLiteral("scopeslabel.raw_vectorscope"), actual);
}

int main(int argc, char ** argv)
{
    test_runtime::force_single_threaded_pipeline();
    test_runtime::prefer_desktop_opengl_on_windows();
#ifdef Q_OS_WIN
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    // These pixel goldens were captured at device-pixel ratio 1. Scale-factor
    // multipliers alone do not disable native Windows per-monitor scaling.
    // This is confined to the test process; real GUI smoke uses the host DPI.
    qputenv("QT_ENABLE_HIGHDPI_SCALING", QByteArrayLiteral("0"));
#endif
    if ( qEnvironmentVariableIsEmpty("QT_QPA_PLATFORM") )
    {
        // Default GUI smoke runs to the offscreen platform so local launches do not
        // create native-window/OpenGL fail-fast dialogs in this workspace.
        qputenv("QT_QPA_PLATFORM", QByteArrayLiteral("offscreen"));
    }
    qputenv("QT_SCALE_FACTOR", QByteArrayLiteral("1"));
    qputenv("QT_SCREEN_SCALE_FACTORS", QByteArrayLiteral("1"));
    QApplication app(argc, argv);

    GuiSmokeTest test;
    return QTest::qExec(&test, argc, argv);
}

#include "test_gui_smoke.moc"
