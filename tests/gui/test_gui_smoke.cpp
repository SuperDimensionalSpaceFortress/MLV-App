#include "../../platform/qt/ColorToolButton.h"
#include "../../platform/qt/Debayered16ReconReusePolicy.h"
#include "../../platform/qt/DualIsoPlaybackPolicy.h"
#include "../../platform/qt/DualIsoPatternMapping.h"
#include "../../platform/qt/GpuDisplayViewport.h"
#include "../../platform/qt/GpuDisplayWindow.h"
#include "../../platform/qt/GpuWindowViewerInputForwarder.h"
#include "../../platform/qt/GpuPreviewProcessing.h"
#include "../../platform/qt/Histogram.h"
#include "../../platform/qt/LookLabelOverlay.h"
#include "../../platform/qt/MainWindowGpuPreviewPolicy.h"
#include "../../platform/qt/PlaybackScopesDuringPlayback.h"
#include "../../platform/qt/PlayerShortcutBinding.h"
#include "../../platform/qt/PlayerTransportToolbar.h"
#include "../../platform/qt/ScopesLabel.h"
#include "../../platform/qt/VectorScope.h"
#include "../../platform/qt/ViewerCursorSurfaces.h"
#include "../../platform/qt/ViewerDoubleClickFilter.h"
#include "../../platform/qt/WaveFormMonitor.h"
#include "../../platform/qt/ZebraThresholds.h"
#include "../common/image_regression.h"
#include "../common/repo_paths.h"
#include "../common/test_runtime.h"

#include <QApplication>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QElapsedTimer>
#include <QFile>
#include <QFontInfo>
#include <QFontMetrics>
#include <QFrame>
#include <QGraphicsPixmapItem>
#include <QGraphicsScene>
#include <QGraphicsView>
#include <QGuiApplication>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLineEdit>
#include <QListView>
#include <QMainWindow>
#include <QMap>
#include <QMenu>
#include <QMenuBar>
#include <QOffscreenSurface>
#include <QOpenGLContext>
#include <QOpenGLFunctions>
#include <QPalette>
#include <QPaintEvent>
#include <QPainter>
#include <QPainterPath>
#include <QPlainTextEdit>
#include <QPointer>
#include <QScopeGuard>
#include <QScreen>
#include <QScrollBar>
#include <QShortcut>
#include <QSlider>
#include <QSpinBox>
#include <QStringListModel>
#include <QStyleHints>
#include <QTableWidget>
#include <QTemporaryDir>
#include <QTimer>
#include <QToolBar>
#include <QToolButton>
#include <QVBoxLayout>
#include <QWidget>
#include <QWindow>
#include <QXmlStreamReader>
#include <QtTest/QtTest>

#include <cmath>
#include <cstring>
#include <memory>
#include <vector>

#ifdef Q_OS_WIN
#include <windows.h>
#endif
#include "../../platform/qt/ViewerMouseRetrievalHook.h"   // after <windows.h>

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
    void playerShortcutMap_tableIsTheStandardMap();
    void playerShortcutMap_uiActionsExist();
    void playerShortcutMap_bindAppliesExactlyTheTable();
    void playerShortcutMap_hiddenBarsWithoutBindingAreDead();
    void playerShortcutMap_everyKeyFiresWindowedAndFullscreen();
    void playerShortcutMap_textInputsKeepPrintableKeys();
    void playerShortcutMap_focusGuardInputsKeepNavigationKeys();
    void playerShortcutMap_focusGuardControlWithoutGuardInputsLoseKeys();
    void playerShortcutMap_exemptSeekBarAndViewerGetPlayerKeys();
    void playerShortcutMap_disabledActionDoesNotFire();
    void playerShortcutMap_otherTopLevelWindowDoesNotFire();
    void playerShortcutMap_glContainerFocus();
    void playerShortcutMap_helpRowsAreGeneratedFromLiveActions();
    void playerShortcutMap_oneSecondSeekTargetNeverOverflows();
    void transportToolbar_tooltipsCarryTheLiveKeys();
    void transportToolbar_finishIsIdempotent();
    void transportToolbar_runtimeIconsAreDrawn();
    void transportToolbar_stopTargetFrameIsClampedCutIn();
    void mainWindowGpuPreviewPolicyAllowsGpu16OnlyWithoutScopes();
    void mainWindowGpuPreviewPolicyScopesVetoOnlyWhenDisplayed();
    void mainWindowGpuPreviewPolicyHideScopesVetoesNothingWhilePlaying();
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
    void mainWindowGpuPreviewPolicyRoutesCudaSuppressedSessionToCpu();
    void mainWindowGpuPreviewPolicyAllowsExperimentalBilinearDebayerOnlyWhenCompatible();
    void mainWindowGpuPreviewPolicyRoutesFullQualityAmazeThroughAmazeGate();
    void mainWindowGpuPreviewPolicyKeepsAmazeTexturePresentExplicitAndNested();
    void mainWindowGpuPreviewPolicyRequiresWidgetViewportForAmazeTexturePresent();
    void mainWindowGpuPreviewPolicyRequiresWidgetViewportForAmazeTexturePresentAtAllScales();
    void mainWindowGpuPreviewPolicyKeepsPlaybackReconTexturePresentExplicitAndNested();
    void mainWindowClampsPlaybackScaleForGpuTextureRouteDecisionTable();
    void mainWindowHonoursGpuTextureRoutePreviewScaleForLatchedSession();
    void mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOneRequiresLibraryReadyFirst();
    void mainWindowGpuPreviewPolicyClassifiesPlaybackPipelineStatus();
    void mainWindowGpuPreviewPolicyLabelsVisibleScopeCpuFallback();
    // PLAYER-CURSOR-AUTOHIDE-1: fullscreen cursor auto-hide on the two viewer surfaces.
    void viewerCursorAutohide_hideBlanksBothSurfacesAndRestoreGivesBackTheToolCursor();
    void viewerCursorAutohide_restoreLeavesAnUnsetViewportCursorUnset();
    void viewerCursorAutohide_restoreKeepsACursorSetWhileHidden();
    void viewerCursorAutohide_idleTimerHidesAndRealMovementOrBlockingRestores();
    void viewerCursorAutohide_subPixelMoveRestores();
    void viewerCursorAutohide_pointerMovedWhileInputExcludedRestores();
#ifdef Q_OS_WIN
    void viewerCursorAutohide_deferredAwayAndBackMoveRestoresOnBothSurfaces();
    void viewerCursorAutohide_coalescedAwayAndBackRestoresOnBothSurfaces();
    void viewerCursorAutohide_ownHideSameSpotMoveDoesNotReshow();
    void viewerCursorAutohide_periodicFullHistoryExcursionRestoresAndQuietStaysHidden();
    void viewerCursorAutohide_queuedSynthesizedSamePositionMoveStaysHidden();
    void viewerCursorAutohide_fullscreenLeftWithoutTheActionStopsTheIdleMachinery();
    void viewerCursorAutohide_nativeFullscreenReentryRestartsTheIdleMachinery();
#endif
    void viewerCursorAutohide_surfaceReplacedWhileHiddenTouchesOnlyTheLiveSurfaces();
    void viewerCursorAutohide_positionSampleAtThePollCapSeesABriefAwayAndBack();
    void dualIsoPlaybackPolicyKeepsExplicitPreviewAndPlaybackOverrideSeparate();
    void dualIsoPatternMappingKeepsUiAndCoreConventionsAligned();
    // PLAYER-FULLSCREEN-CONTROL-BAR-1: the CPU viewer's fullscreen control bar.
    void fullscreenControlBar_moveShowsTheBarAndHiddenCostsNothing();
    void fullscreenControlBar_doubleClickOnTheBarKeepsFullscreen();
    void fullscreenControlBar_exitClickLeavesFullscreen();
    void fullscreenControlBar_clickHitsWhatIsPaintedWhenTheGeometryLags();
    void fullscreenControlBar_wheelDuringAForeignGestureReachesTheView();
    // PLAYER-DOUBLECLICK-FULLSCREEN-1
    void viewerDoubleClick_viewportTogglesOnceAndIsSwallowed();
    void viewerDoubleClick_glWindowTogglesOnce();
    void viewerDoubleClick_armedToolPassesThrough();
    void viewerDoubleClick_otherButtonModifierSettingOrNoClipDoNotToggle();
    void viewerDoubleClick_otherWidgetDoesNotToggle();
    void viewerDoubleClick_toolArmedAtFirstPressSuppresses();
    void viewerDoubleClick_armedPressElsewhereDoesNotPoisonANewSequence();
    void viewerDoubleClick_newSequenceAfterAMovedOrSlowPressToggles();
    void viewerDoubleClick_glWindowRealQtPairingKeepsTheFirstPressRecord();
    void viewerDoubleClick_firstPressOutsideTheViewerStartsAnUnarmedRecord();
    void gpuViewportRgb888ZebraProcessingMatchesCpuReference();
    void gpuViewportZebraProcessingMatchesCpuReference();
    void gpuViewportPreviewProcessingMatchesCpuReference();
    void gpuViewportPreviewProcessingWithZebrasMatchesCpuReference();
    void gpuPreviewProcessingLutReadinessReflectsSignatureNotJustPointers();
    void gpuPreviewProcessingLutTextureSetFailsClosedOnMissingUploadAndContextLoss();
    void gpuPreviewProcessingLutSetReportsRebuildsAndBlurUploadsKeepTheirBytes();
    void gpuPreviewProcessingShBlurUploadModeParsesOnlyItsThreeValues();
    void gpuPreviewProcessingShBlurPboUploadKeepsBytesAndSkipKeepsAllocation();
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
    void lookLabelOverlaySingleLineIsUnchanged();
    void lookLabelOverlayGrowsWithLineCount();
    void glWindowViewerInput_picksMapToSceneAndRespectArming();
    void glWindowViewerInput_contextMenuOnPlatformTriggerOnly();
    void glWindowViewerInput_dragAndDropReachTheHooks();
    void glWindowViewerInput_otherObjectsAreIgnored();
    void glWindowViewerInput_hiddenViewMapFromGlobalIsInverseOfMapToGlobal();
    void glWindowViewerInput_edgePicksStayInsideTheImage();
    void glWindowViewerInput_roundedSceneEdgeStillPicks();
    void glWindowViewerInput_noDrawnPictureIsNoPick();
    void glWindowViewerInput_getterRefusesWhileAFrameIsHandedOver();
    void glWindowViewerInput_getterRecordsThePaintedPicture();
};

namespace
{
// LOOK-LABEL-PLAYBACK-INFO-1: renderLookLabelOverlayImage as it was on f9ff24b7 (one line only), the reference the
// multi-line renderer must reproduce byte for byte for a single line.
QImage legacyLookLabelOverlayImage( const QString &text, int fontPx )
{
    if( text.isEmpty() || fontPx <= 0 ) return QImage();
    QFont font( QStringLiteral( "Segoe UI" ) );
    font.setPixelSize( fontPx );
    font.setWeight( QFont::DemiBold );
    const QFontMetrics metrics( font );
    const int padX = qMax( 4, qRound( fontPx * 0.55 ) );
    const int padY = qMax( 2, qRound( fontPx * 0.30 ) );
    const int textWidth = metrics.horizontalAdvance( text );
    const QSize size( textWidth + 2 * padX, metrics.height() + 2 * padY );
    QImage image( size, QImage::Format_ARGB32_Premultiplied );
    image.fill( Qt::transparent );
    QPainter painter( &image );
    painter.setRenderHint( QPainter::Antialiasing, true );
    painter.setRenderHint( QPainter::TextAntialiasing, true );
    const qreal radius = size.height() * 0.28;
    QPainterPath backing;
    backing.addRoundedRect( QRectF( 0.5, 0.5, size.width() - 1.0, size.height() - 1.0 ), radius, radius );
    painter.fillPath( backing, QColor( 0, 0, 0, 140 ) );
    painter.setPen( QColor( 255, 255, 255, 40 ) );
    painter.drawPath( backing );
    painter.setFont( font );
    const QRect textRect( padX, padY, textWidth, metrics.height() );
    painter.setPen( QColor( 0, 0, 0, 160 ) );
    painter.drawText( textRect.translated( 0, qMax( 1, fontPx / 20 ) ), Qt::AlignLeft | Qt::AlignVCenter, text );
    painter.setPen( QColor( 255, 255, 255, 235 ) );
    painter.drawText( textRect, Qt::AlignLeft | Qt::AlignVCenter, text );
    painter.end();
    return image;
}
} // namespace

void GuiSmokeTest::lookLabelOverlaySingleLineIsUnchanged()
{
    const QString texts[] = { QStringLiteral( "Look: Cinematic" ), QStringLiteral( "Look: Off" ),
                              QStringLiteral( "Look: Baseline (fallback)" ) };
    const int sizes[] = { 14, 20, 40, 64 };
    for( const QString &text : texts )
    {
        for( const int px : sizes )
        {
            const QImage now = renderLookLabelOverlayImage( text, px );
            const QImage before = legacyLookLabelOverlayImage( text, px );
            QCOMPARE( now.size(), before.size() );
            QCOMPARE( now.format(), before.format() );
            QVERIFY2( now == before, qPrintable( QStringLiteral( "pixels differ for '%1' at %2 px" ).arg( text ).arg( px ) ) );
        }
    }
    QVERIFY( renderLookLabelOverlayImage( QString(), 20 ).isNull() );
}

void GuiSmokeTest::lookLabelOverlayGrowsWithLineCount()
{
    const int px = 20;
    const QImage one = renderLookLabelOverlayImage( QStringLiteral( "Look: Cinematic" ), px );
    const QImage two = renderLookLabelOverlayImage( QStringLiteral( "Look: Cinematic\nGPU display GPU Tex NR" ), px );
    const QImage three = renderLookLabelOverlayImage(
        QStringLiteral( "Look: Cinematic\nGPU display GPU Tex NR\n24.0/24 fps DF on 1920x1080" ), px );
    QVERIFY( !one.isNull() && !two.isNull() && !three.isNull() );
    QFont font( QStringLiteral( "Segoe UI" ) );
    font.setPixelSize( px );
    font.setWeight( QFont::DemiBold );
    const int step = QFontMetrics( font ).lineSpacing();
    QCOMPARE( two.height(), one.height() + step );
    QCOMPARE( three.height(), one.height() + 2 * step );
    // Width is the widest line's.
    QVERIFY( three.width() >= two.width() );
    QVERIFY( two.width() >= one.width() );
}

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

namespace
{
// PLAYER-STANDARD-SHORTCUT-MAP-1 (absorbs PLAYER-FULLSCREEN-SHORTCUTS-LIVE-1's table). This is the
// TEST ORACLE: the design's key map written out literally, so a change to the product table
// (PlayerShortcutBinding::bindings()) that does not match the design reds
// playerShortcutMap_tableIsTheStandardMap. It is not product data and nothing in the product
// reads it.
using PlayerShortcutBinding::KeyKind;

struct OracleKey
{
    QKeySequence sequence;
    KeyKind kind;
};

struct OracleAction
{
    QString name;
    QList<OracleKey> keys;
};

QKeySequence seq( const char *portable )
{
    return QKeySequence( QString::fromLatin1( portable ), QKeySequence::PortableText );
}

QList<OracleAction> standard_map_oracle()
{
    QList<OracleKey> fullscreen{ { seq( "Ctrl+F" ), KeyKind::Primary } };
#ifdef Q_OS_WIN
    fullscreen << OracleKey{ seq( "F11" ), KeyKind::Secondary };
#endif
    fullscreen << OracleKey{ seq( "Return" ), KeyKind::Secondary }
               << OracleKey{ seq( "Enter" ), KeyKind::Secondary };
    QList<OracleAction> map;
    map << OracleAction{ QStringLiteral( "actionPlay" ),                  { { seq( "Space" ), KeyKind::Primary } } }
        << OracleAction{ QStringLiteral( "actionFullscreen" ),            fullscreen }
        << OracleAction{ QStringLiteral( "actionPreviousFrame" ),         { { seq( "Left" ), KeyKind::Primary }, { seq( "," ), KeyKind::Secondary } } }
        << OracleAction{ QStringLiteral( "actionNextFrame" ),             { { seq( "Right" ), KeyKind::Primary }, { seq( "." ), KeyKind::Secondary } } }
        << OracleAction{ QStringLiteral( "actionSeekBackwardOneSecond" ), { { seq( "Shift+Left" ), KeyKind::Primary } } }
        << OracleAction{ QStringLiteral( "actionSeekForwardOneSecond" ),  { { seq( "Shift+Right" ), KeyKind::Primary } } }
        << OracleAction{ QStringLiteral( "actionGoto_First_Frame" ),      { { seq( "Home" ), KeyKind::Primary }, { seq( "X" ), KeyKind::Alias } } }
        << OracleAction{ QStringLiteral( "actionGoto_Last_Frame" ),       { { seq( "End" ), KeyKind::Primary } } }
        << OracleAction{ QStringLiteral( "actionLoop" ),                  { { seq( "Ctrl+L" ), KeyKind::Primary } } }
        << OracleAction{ QStringLiteral( "actionPrevious_Clip" ),         { { seq( "PgUp" ), KeyKind::Primary }, { seq( "J" ), KeyKind::Alias } } }
        << OracleAction{ QStringLiteral( "actionNext_Clip" ),             { { seq( "PgDown" ), KeyKind::Primary }, { seq( "K" ), KeyKind::Alias } } }
        << OracleAction{ QStringLiteral( "actionZoomFit" ),               { { seq( "F" ), KeyKind::Primary } } }
        << OracleAction{ QStringLiteral( "actionZoom100" ),               { { seq( "Ctrl+0" ), KeyKind::Primary } } };
    return map;
}

QList<QKeySequence> oracle_sequences( const OracleAction &row )
{
    QList<QKeySequence> sequences;
    for( const OracleKey &k : row.keys ) sequences << k.sequence;
    return sequences;
}

QString kind_name( KeyKind kind )
{
    switch( kind )
    {
    case KeyKind::Primary: return QStringLiteral( "Primary" );
    case KeyKind::Secondary: return QStringLiteral( "Secondary" );
    case KeyKind::Alias: return QStringLiteral( "Alias" );
    }
    return QStringLiteral( "?" );
}

// One physical key press and the action it must reach.
struct KeyPress
{
    QString action;
    Qt::Key key;
    Qt::KeyboardModifiers modifiers;
    QString label;
};

QList<KeyPress> oracle_key_presses()
{
    QList<KeyPress> presses;
    for( const OracleAction &row : standard_map_oracle() )
        for( const OracleKey &k : row.keys )
        {
            const QKeyCombination c = k.sequence[0];
            presses << KeyPress{ row.name, c.key(), c.keyboardModifiers(), k.sequence.toString() };
        }
    // Numpad Enter arrives as Qt::Key_Enter + Qt::KeypadModifier.
    presses << KeyPress{ QStringLiteral( "actionFullscreen" ), Qt::Key_Enter, Qt::KeypadModifier,
                         QStringLiteral( "keypad Enter" ) };
    return presses;
}

// The navigation keys the focus guard keeps for input widgets.
QList<KeyPress> navigation_key_presses()
{
    QList<KeyPress> presses;
    const struct { Qt::Key key; Qt::KeyboardModifiers modifiers; const char *label; } keys[] = {
        { Qt::Key_Left, Qt::NoModifier, "Left" },       { Qt::Key_Right, Qt::NoModifier, "Right" },
        { Qt::Key_Up, Qt::NoModifier, "Up" },           { Qt::Key_Down, Qt::NoModifier, "Down" },
        { Qt::Key_Home, Qt::NoModifier, "Home" },       { Qt::Key_End, Qt::NoModifier, "End" },
        { Qt::Key_PageUp, Qt::NoModifier, "PgUp" },     { Qt::Key_PageDown, Qt::NoModifier, "PgDown" },
        { Qt::Key_Return, Qt::NoModifier, "Return" },   { Qt::Key_Enter, Qt::KeypadModifier, "keypad Enter" },
        { Qt::Key_Left, Qt::ShiftModifier, "Shift+Left" }, { Qt::Key_Right, Qt::ShiftModifier, "Shift+Right" },
    };
    for( const auto &k : keys )
        presses << KeyPress{ QString(), k.key, k.modifiers, QString::fromLatin1( k.label ) };
    return presses;
}

// A stand-in for MainWindow: every table action parented to the window (as uic does), a
// "Playback" menu in a WIDGET menu bar holding all of them (MainWindow.ui menuPlayback), a
// toolbar holding Goto/Prev/Play/Next (mainToolBar), a focusable non-input viewer, the seek bar
// (the FocusGuard's exempt widget) and one of each input-widget class the guard protects.
// Actions start with NO shortcuts: every key comes from bindToWindow() (or applyOracleShortcuts()).
class PlayerShortcutMirror
{
public:
    PlayerShortcutMirror()
    {
        window.resize( 800, 600 );
        // A widget menu bar on every platform (#846 adversary A1): a macOS native menu bar does
        // not show as a QWidget and counts as visible for shortcut matching even when hidden.
        window.menuBar()->setNativeMenuBar( false );
        QMenu *playback = window.menuBar()->addMenu( QStringLiteral( "Playback" ) );
        toolBar = window.addToolBar( QStringLiteral( "mainToolBar" ) );
        for( const OracleAction &row : standard_map_oracle() )
        {
            const QString name = row.name;
            QAction *a = new QAction( QStringLiteral( "&" ) + name, &window );
            a->setObjectName( name );
            playback->addAction( a );
            counts.insert( name, 0 );
            QObject::connect( a, &QAction::triggered, &window, [this, name]() { ++counts[name]; } );
        }
        for( const char *name : { "actionGoto_First_Frame", "actionPreviousFrame", "actionPlay", "actionNextFrame" } )
            toolBar->addAction( action( QLatin1String( name ) ) );

        central = new QWidget( &window );
        layout = new QVBoxLayout( central );
        viewer = new QWidget( central );
        viewer->setObjectName( QStringLiteral( "viewer" ) );
        viewer->setFocusPolicy( Qt::StrongFocus );
        viewer->setMinimumSize( 160, 90 );
        seekBar = new QSlider( Qt::Horizontal, central );
        seekBar->setObjectName( QStringLiteral( "horizontalSliderPosition" ) );
        seekBar->setRange( 0, 100 );
        seekBar->setValue( 50 );
        slider = new QSlider( Qt::Horizontal, central );
        slider->setRange( 0, 100 );
        slider->setValue( 50 );
        spinBox = new QSpinBox( central );
        spinBox->setRange( 0, 100 );
        spinBox->setValue( 50 );
        doubleSpinBox = new QDoubleSpinBox( central );
        doubleSpinBox->setRange( 0.0, 100.0 );
        doubleSpinBox->setSingleStep( 1.0 );
        doubleSpinBox->setValue( 50.0 );
        listView = new QListView( central );
        listView->setModel( new QStringListModel( QStringList() << "r0" << "r1" << "r2" << "r3" << "r4", listView ) );
        listView->setCurrentIndex( listView->model()->index( 0, 0 ) );
        comboBox = new QComboBox( central );
        comboBox->addItems( QStringList() << "c0" << "c1" << "c2" );
        lineEdit = new QLineEdit( central );
        plainTextEdit = new QPlainTextEdit( central );
        for( QWidget *w : std::initializer_list<QWidget*>{ viewer, seekBar, slider, spinBox, doubleSpinBox,
                                                          listView, comboBox, lineEdit, plainTextEdit } )
            layout->addWidget( w );
        window.setCentralWidget( central );
    }

    ~PlayerShortcutMirror()
    {
        if( guard ) qApp->removeEventFilter( guard );
    }

    QAction *action( const QString &name )
    {
        return window.findChild<QAction*>( name, Qt::FindDirectChildrenOnly );
    }

    // What MainWindow::initGui() installs right after bindToWindow().
    void installGuard()
    {
        guard = new PlayerShortcutBinding::FocusGuard( seekBar, &window );
        qApp->installEventFilter( guard );
    }

    void applyOracleShortcuts()
    {
        for( const OracleAction &row : standard_map_oracle() ) action( row.name )->setShortcuts( oracle_sequences( row ) );
    }

    void hideBars()
    {
        window.menuBar()->hide();
        toolBar->hide();
    }

    // Windowed: bars visible. Fullscreen: bars hidden, as on_actionFullscreen_triggered() does.
    bool showAndActivate( bool fullscreen )
    {
        if( fullscreen )
        {
            hideBars();
            window.showFullScreen();
        }
        else window.show();
        window.activateWindow();
        return QTest::qWaitForWindowActive( &window );
    }

    bool focusOn( QWidget *w )
    {
        w->setFocus( Qt::OtherFocusReason );
        return QTest::qWaitFor( [w]() { return QApplication::focusWidget() == w; }, 2000 );
    }

    int total() const
    {
        int sum = 0;
        for( const int c : counts ) sum += c;
        return sum;
    }

    QMainWindow window;
    QToolBar *toolBar = nullptr;
    QWidget *central = nullptr;
    QVBoxLayout *layout = nullptr;
    QWidget *viewer = nullptr;
    QSlider *seekBar = nullptr;
    QSlider *slider = nullptr;
    QSpinBox *spinBox = nullptr;
    QDoubleSpinBox *doubleSpinBox = nullptr;
    QListView *listView = nullptr;
    QComboBox *comboBox = nullptr;
    QLineEdit *lineEdit = nullptr;
    QPlainTextEdit *plainTextEdit = nullptr;
    QPointer<PlayerShortcutBinding::FocusGuard> guard;
    QMap<QString, int> counts;
};

QString press_label( const KeyPress &k )
{
    return QStringLiteral( "%1 (key 0x%2 mods 0x%3)" ).arg( k.label ).arg( int( k.key ), 0, 16 ).arg( int( k.modifiers ), 0, 16 );
}

// Presses each key on `target`, one at a time: the key's own action fires `expectedPerKey`
// times and no other action fires.
void press_and_check( PlayerShortcutMirror &m, QWidget *target, const QList<KeyPress> &presses,
                      int expectedPerKey, const QString &context )
{
    for( const KeyPress &k : presses )
    {
        const QMap<QString, int> before = m.counts;
        QTest::keyClick( target, k.key, k.modifiers );
        for( auto it = m.counts.cbegin(); it != m.counts.cend(); ++it )
        {
            const int delta = it.value() - before.value( it.key() );
            const int expected = ( it.key() == k.action ) ? expectedPerKey : 0;
            QVERIFY2( delta == expected,
                      qPrintable( QStringLiteral( "%1: key %2: action %3 fired %4 time(s), expected %5" )
                                  .arg( context, press_label( k ), it.key() ).arg( delta ).arg( expected ) ) );
        }
    }
}

// The facts MainWindow.ui states about actions, menus and shortcut strings.
struct UiFacts
{
    QMap<QString, int> actionDefinitions;          // <action name=...> count
    QMap<QString, QStringList> menusByAction;      // <addaction> inside a QMenu widget
    QMap<QString, QStringList> shortcutsByOwner;   // <property name="shortcut"><string> of an action or widget
};

bool parse_main_window_ui( UiFacts &facts, QString &error )
{
    const QString path = repo_file_path( QStringLiteral( "platform/qt/MainWindow.ui" ) );
    if( path.isEmpty() ) { error = QStringLiteral( "MainWindow.ui not found" ); return false; }
    QFile file( path );
    if( !file.open( QIODevice::ReadOnly ) ) { error = QStringLiteral( "cannot open %1" ).arg( path ); return false; }
    QXmlStreamReader xml( &file );
    struct Element { QString tag; QString name; QString cls; };
    QVector<Element> stack;
    while( !xml.atEnd() )
    {
        xml.readNext();
        if( xml.isStartElement() )
        {
            const Element e{ xml.name().toString(),
                             xml.attributes().value( QStringLiteral( "name" ) ).toString(),
                             xml.attributes().value( QStringLiteral( "class" ) ).toString() };
            if( e.tag == QLatin1String( "string" ) && stack.size() >= 2
             && stack.last().tag == QLatin1String( "property" ) && stack.last().name == QLatin1String( "shortcut" ) )
            {
                facts.shortcutsByOwner[stack.at( stack.size() - 2 ).name] << xml.readElementText();
                continue; // readElementText() consumed the end element
            }
            if( e.tag == QLatin1String( "action" ) ) ++facts.actionDefinitions[e.name];
            if( e.tag == QLatin1String( "addaction" ) && !stack.isEmpty()
             && stack.last().tag == QLatin1String( "widget" ) && stack.last().cls == QLatin1String( "QMenu" ) )
                facts.menusByAction[e.name] << stack.last().name;
            stack.push_back( e );
        }
        else if( xml.isEndElement() && !stack.isEmpty() )
        {
            stack.pop_back();
        }
    }
    if( xml.hasError() ) { error = xml.errorString(); return false; }
    return true;
}

} // namespace

void GuiSmokeTest::playerShortcutMap_tableIsTheStandardMap()
{
    // The product table equals the design table: same actions in the same order, same sequences
    // in the same order, same kinds.
    const QList<PlayerShortcutBinding::ActionBinding> table = PlayerShortcutBinding::bindings();
    const QList<OracleAction> oracle = standard_map_oracle();
    QStringList oracleNames;
    for( const OracleAction &row : oracle ) oracleNames << row.name;
    QCOMPARE( PlayerShortcutBinding::liveActionNames(), oracleNames );
    QCOMPARE( table.size(), oracle.size() );
    for( int i = 0; i < oracle.size(); ++i )
    {
        QCOMPARE( table.at( i ).objectName, oracle.at( i ).name );
        QCOMPARE( table.at( i ).keys.size(), oracle.at( i ).keys.size() );
        for( int k = 0; k < oracle.at( i ).keys.size(); ++k )
        {
            QCOMPARE( table.at( i ).keys.at( k ).sequence, oracle.at( i ).keys.at( k ).sequence );
            QVERIFY2( table.at( i ).keys.at( k ).kind == oracle.at( i ).keys.at( k ).kind,
                      qPrintable( QStringLiteral( "%1 key %2: kind %3, design %4" )
                                  .arg( oracle.at( i ).name, oracle.at( i ).keys.at( k ).sequence.toString(),
                                        kind_name( table.at( i ).keys.at( k ).kind ), kind_name( oracle.at( i ).keys.at( k ).kind ) ) ) );
        }
        QVERIFY( table.at( i ).keys.first().kind == KeyKind::Primary );
    }

    // No sequence twice across the table (one key, one action).
    QList<QKeySequence> all;
    for( const PlayerShortcutBinding::ActionBinding &row : table )
        for( const PlayerShortcutBinding::KeyBinding &k : row.keys )
        {
            QVERIFY2( !all.contains( k.sequence ), qPrintable( QStringLiteral( "%1 bound twice" ).arg( k.sequence.toString() ) ) );
            all << k.sequence;
        }

    // No table key collides with any other shortcut MainWindow.ui declares (actions and tool
    // buttons), nor with the keys MainWindow.cpp creates in code (Q, Esc) or reserves for later
    // cards (Up/Down: PLAYER-MUTE-VOLUME-1; L: PLAYER-JKL-SHUTTLE-1).
    UiFacts facts;
    QString error;
    QVERIFY2( parse_main_window_ui( facts, error ), qPrintable( error ) );
    for( auto it = facts.shortcutsByOwner.cbegin(); it != facts.shortcutsByOwner.cend(); ++it )
    {
        if( oracleNames.contains( it.key() ) ) continue;
        for( const QString &s : it.value() )
            QVERIFY2( !all.contains( QKeySequence( s, QKeySequence::PortableText ) ),
                      qPrintable( QStringLiteral( "table key %1 collides with %2 in MainWindow.ui" ).arg( s, it.key() ) ) );
    }
    for( const char *other : { "M", "I", "Q", "Esc", "Up", "Down", "L" } )
        QVERIFY2( !all.contains( seq( other ) ), qPrintable( QStringLiteral( "table binds reserved key %1" ).arg( QLatin1String( other ) ) ) );
}

void GuiSmokeTest::playerShortcutMap_uiActionsExist()
{
    // Every table action exists once in MainWindow.ui, sits on a menu, and any shortcut string
    // the .ui carries for it is one of its table sequences. Help > Keyboard Shortcuts exists.
    UiFacts facts;
    QString error;
    QVERIFY2( parse_main_window_ui( facts, error ), qPrintable( error ) );
    for( const OracleAction &row : standard_map_oracle() )
    {
        QVERIFY2( facts.actionDefinitions.value( row.name ) == 1,
                  qPrintable( QStringLiteral( "%1 defined %2 time(s) in MainWindow.ui" ).arg( row.name ).arg( facts.actionDefinitions.value( row.name ) ) ) );
        QVERIFY2( !facts.menusByAction.value( row.name ).isEmpty(),
                  qPrintable( QStringLiteral( "%1 is on no menu in MainWindow.ui" ).arg( row.name ) ) );
        const QList<QKeySequence> sequences = oracle_sequences( row );
        for( const QString &s : facts.shortcutsByOwner.value( row.name ) )
            QVERIFY2( sequences.contains( QKeySequence( s, QKeySequence::PortableText ) ),
                      qPrintable( QStringLiteral( "%1: .ui shortcut %2 is not in its table set" ).arg( row.name, s ) ) );
    }
    QCOMPARE( facts.actionDefinitions.value( QStringLiteral( "actionKeyboardShortcuts" ) ), 1 );
    QVERIFY( facts.menusByAction.value( QStringLiteral( "actionKeyboardShortcuts" ) ).contains( QStringLiteral( "menuHelp" ) ) );
}

void GuiSmokeTest::playerShortcutMap_bindAppliesExactlyTheTable()
{
    PlayerShortcutMirror m;
    const QList<OracleAction> oracle = standard_map_oracle();
    // A stray key set before the bind (as a .ui string would be) is replaced, not kept.
    m.action( QStringLiteral( "actionPlay" ) )->setShortcuts( { seq( "Space" ), seq( "Ctrl+Shift+P" ) } );
    QCOMPARE( PlayerShortcutBinding::bindToWindow( &m.window ), int( oracle.size() ) );
    for( const OracleAction &row : oracle )
    {
        QAction *a = m.action( row.name );
        QVERIFY( a );
        QCOMPARE( a->shortcuts(), oracle_sequences( row ) );
        QCOMPARE( a->shortcut(), row.keys.first().sequence );
        QVERIFY( m.window.actions().contains( a ) );
    }
    // A second bind leaves the window's actions as they are -- no remove/re-append reorder past
    // an unrelated action added in between (#846 adversary pass 2 A1).
    QAction *extra = new QAction( QStringLiteral( "Extra" ), &m.window );
    m.window.addAction( extra );
    const QList<QAction*> before = m.window.actions();
    QCOMPARE( before.size(), int( oracle.size() ) + 1 );
    QCOMPARE( before.last(), extra );
    QCOMPARE( PlayerShortcutBinding::bindToWindow( &m.window ), int( oracle.size() ) );
    QCOMPARE( m.window.actions(), before );
}

void GuiSmokeTest::playerShortcutMap_hiddenBarsWithoutBindingAreDead()
{
    // CONTROL (#846's defect): bars hidden, window fullscreen and active, the keys set on the
    // actions but nothing bound to the window -> no key reaches its action.
    PlayerShortcutMirror m;
    m.applyOracleShortcuts();
    QVERIFY( m.showAndActivate( true ) );
    QVERIFY( m.focusOn( m.viewer ) );
    press_and_check( m, &m.window, oracle_key_presses(), 0, QStringLiteral( "unbound fullscreen" ) );
    if( QTest::currentTestFailed() ) return;
    QCOMPARE( m.total(), 0 );
}

void GuiSmokeTest::playerShortcutMap_everyKeyFiresWindowedAndFullscreen()
{
    // Bars visible + windowed, then bars hidden + fullscreen; viewer focused; the guard installed
    // as MainWindow installs it. Every sequence (aliases J K X and Return/Enter/keypad Enter
    // included) fires its own action exactly once and no other.
    for( const bool fullscreen : { false, true } )
    {
        PlayerShortcutMirror m;
        QCOMPARE( PlayerShortcutBinding::bindToWindow( &m.window ), int( standard_map_oracle().size() ) );
        m.installGuard();
        QVERIFY( m.showAndActivate( fullscreen ) );
        if( !fullscreen )
        {
            QVERIFY( m.window.menuBar()->isVisible() );
            QVERIFY( m.toolBar->isVisible() );
        }
        QVERIFY( m.focusOn( m.viewer ) );
        const QList<KeyPress> presses = oracle_key_presses();
        press_and_check( m, &m.window, presses, 1, fullscreen ? QStringLiteral( "fullscreen" ) : QStringLiteral( "windowed" ) );
        if( QTest::currentTestFailed() ) return;
        QCOMPARE( m.total(), int( presses.size() ) );
    }
}

void GuiSmokeTest::playerShortcutMap_textInputsKeepPrintableKeys()
{
    // Text inputs claim printable keys in ShortcutOverride; the window binding must not steal
    // Space , . X F J K from them.
    PlayerShortcutMirror m;
    PlayerShortcutBinding::bindToWindow( &m.window );
    m.installGuard();
    QVERIFY( m.showAndActivate( true ) );
    const Qt::Key printable[] = { Qt::Key_Space, Qt::Key_Comma, Qt::Key_Period, Qt::Key_X, Qt::Key_F, Qt::Key_J, Qt::Key_K };

    QVERIFY( m.focusOn( m.lineEdit ) );
    for( const Qt::Key key : printable ) QTest::keyClick( m.lineEdit, key );
    QCOMPARE( m.total(), 0 );
    QCOMPARE( m.lineEdit->text(), QStringLiteral( " ,.xfjk" ) );

    QVERIFY( m.focusOn( m.spinBox ) );
    for( const Qt::Key key : printable ) QTest::keyClick( m.spinBox, key );
    QCOMPARE( m.total(), 0 );
}

void GuiSmokeTest::playerShortcutMap_focusGuardInputsKeepNavigationKeys()
{
    // With an input widget focused, windowed AND fullscreen, the navigation keys fire no player
    // action and the widget does its own thing with them.
    for( const bool fullscreen : { false, true } )
    {
        const QString mode = fullscreen ? QStringLiteral( "fullscreen" ) : QStringLiteral( "windowed" );
        PlayerShortcutMirror m;
        PlayerShortcutBinding::bindToWindow( &m.window );
        m.installGuard();
        QVERIFY( m.showAndActivate( fullscreen ) );

        QVERIFY( m.focusOn( m.spinBox ) );
        QTest::keyClick( m.spinBox, Qt::Key_Up );
        QCOMPARE( m.spinBox->value(), 51 );
        QVERIFY( m.focusOn( m.doubleSpinBox ) );
        QTest::keyClick( m.doubleSpinBox, Qt::Key_Up );
        QCOMPARE( m.doubleSpinBox->value(), 51.0 );
        QVERIFY( m.focusOn( m.slider ) );
        QTest::keyClick( m.slider, Qt::Key_Right );
        QCOMPARE( m.slider->value(), 51 );
        QTest::keyClick( m.slider, Qt::Key_End );
        QCOMPARE( m.slider->value(), m.slider->maximum() );
        QVERIFY( m.focusOn( m.listView ) );
        QTest::keyClick( m.listView, Qt::Key_Down );
        QCOMPARE( m.listView->currentIndex().row(), 1 );
        QTest::keyClick( m.listView, Qt::Key_End );
        QCOMPARE( m.listView->currentIndex().row(), 4 );
        QVERIFY( m.focusOn( m.lineEdit ) );
        QSignalSpy returnPressed( m.lineEdit, &QLineEdit::returnPressed );
        QTest::keyClick( m.lineEdit, Qt::Key_Return );
        QCOMPARE( returnPressed.count(), 1 );
        QCOMPARE( m.total(), 0 );

        for( QWidget *w : std::initializer_list<QWidget*>{ m.spinBox, m.doubleSpinBox, m.slider, m.listView,
                                                          m.comboBox, m.lineEdit, m.plainTextEdit } )
        {
            QVERIFY( m.focusOn( w ) );
            press_and_check( m, w, navigation_key_presses(), 0,
                             QStringLiteral( "%1 %2 focused" ).arg( mode, QString::fromLatin1( w->metaObject()->className() ) ) );
            if( QTest::currentTestFailed() ) return;
        }
        QCOMPARE( m.total(), 0 );
    }
}

void GuiSmokeTest::playerShortcutMap_focusGuardControlWithoutGuardInputsLoseKeys()
{
    // CONTROL: the guard is load-bearing. Without it, a focused slider loses Right to
    // actionNextFrame and a focused spin box loses Return to actionFullscreen.
    PlayerShortcutMirror m;
    PlayerShortcutBinding::bindToWindow( &m.window );
    QVERIFY( m.showAndActivate( true ) );
    QVERIFY( m.focusOn( m.slider ) );
    QTest::keyClick( m.slider, Qt::Key_Right );
    QCOMPARE( m.counts.value( QStringLiteral( "actionNextFrame" ) ), 1 );
    QCOMPARE( m.slider->value(), 50 );
    QVERIFY( m.focusOn( m.spinBox ) );
    QTest::keyClick( m.spinBox, Qt::Key_Return );
    QCOMPARE( m.counts.value( QStringLiteral( "actionFullscreen" ) ), 1 );

    // The pure predicate agrees: input classes own the key, the exempt widget and the viewer do not.
    using PlayerShortcutBinding::FocusGuard;
    QVERIFY( FocusGuard::focusOwnsKey( m.slider, Qt::Key_Right, Qt::NoModifier, m.seekBar ) );
    QVERIFY( FocusGuard::focusOwnsKey( m.spinBox, Qt::Key_Enter, Qt::KeypadModifier, m.seekBar ) );
    QVERIFY( !FocusGuard::focusOwnsKey( m.seekBar, Qt::Key_Right, Qt::NoModifier, m.seekBar ) );
    QVERIFY( !FocusGuard::focusOwnsKey( m.viewer, Qt::Key_Right, Qt::NoModifier, m.seekBar ) );
    QVERIFY( !FocusGuard::focusOwnsKey( m.slider, Qt::Key_Right, Qt::ControlModifier, m.seekBar ) );
    QVERIFY( !FocusGuard::focusOwnsKey( m.lineEdit, Qt::Key_Space, Qt::NoModifier, m.seekBar ) );
    QVERIFY( !FocusGuard::focusOwnsKey( nullptr, Qt::Key_Right, Qt::NoModifier, m.seekBar ) );
}

void GuiSmokeTest::playerShortcutMap_exemptSeekBarAndViewerGetPlayerKeys()
{
    // The seek bar (the guard's exempt widget) and the viewer keep the player keys.
    PlayerShortcutMirror m;
    PlayerShortcutBinding::bindToWindow( &m.window );
    m.installGuard();
    QVERIFY( m.showAndActivate( true ) );
    QList<KeyPress> presses;
    presses << KeyPress{ QStringLiteral( "actionNextFrame" ), Qt::Key_Right, Qt::NoModifier, QStringLiteral( "Right" ) }
            << KeyPress{ QStringLiteral( "actionGoto_First_Frame" ), Qt::Key_Home, Qt::NoModifier, QStringLiteral( "Home" ) }
            << KeyPress{ QStringLiteral( "actionGoto_Last_Frame" ), Qt::Key_End, Qt::NoModifier, QStringLiteral( "End" ) }
            << KeyPress{ QStringLiteral( "actionSeekForwardOneSecond" ), Qt::Key_Right, Qt::ShiftModifier, QStringLiteral( "Shift+Right" ) };
    for( QWidget *w : std::initializer_list<QWidget*>{ m.seekBar, m.viewer } )
    {
        QVERIFY( m.focusOn( w ) );
        press_and_check( m, w, presses, 1, QString::fromLatin1( w->metaObject()->className() ) );
        if( QTest::currentTestFailed() ) return;
    }
    QCOMPARE( m.seekBar->value(), 50 ); // the action took the key, the slider did not step
    QCOMPARE( m.total(), 2 * int( presses.size() ) );
}

void GuiSmokeTest::playerShortcutMap_disabledActionDoesNotFire()
{
    // CoS #846 prevention: a disabled table action's keys fire nothing -- not it, not another
    // action that might match the key next.
    PlayerShortcutMirror m;
    PlayerShortcutBinding::bindToWindow( &m.window );
    m.installGuard();
    QVERIFY( m.showAndActivate( true ) );
    QVERIFY( m.focusOn( m.viewer ) );
    QAction *next = m.action( QStringLiteral( "actionNextFrame" ) );
    next->setEnabled( false );
    QTest::keyClick( &m.window, Qt::Key_Right );
    QTest::keyClick( &m.window, Qt::Key_Period );
    QCOMPARE( m.total(), 0 );
    next->setEnabled( true );
    QTest::keyClick( &m.window, Qt::Key_Right );
    QCOMPARE( m.counts.value( QStringLiteral( "actionNextFrame" ) ), 1 );
    QCOMPARE( m.total(), 1 );
}

void GuiSmokeTest::playerShortcutMap_otherTopLevelWindowDoesNotFire()
{
    // Window scope kept (never an application-wide context): with another top-level window
    // active, no table key fires the player's actions.
    PlayerShortcutMirror m;
    PlayerShortcutBinding::bindToWindow( &m.window );
    m.installGuard();
    QVERIFY( m.showAndActivate( true ) );

    QWidget other;
    other.setWindowTitle( QStringLiteral( "other top-level" ) );
    other.setFocusPolicy( Qt::StrongFocus );
    other.resize( 200, 100 );
    other.show();
    other.activateWindow();
    QVERIFY( QTest::qWaitForWindowActive( &other ) );
    QTRY_COMPARE( QApplication::activeWindow(), &other );
    press_and_check( m, &other, oracle_key_presses(), 0, QStringLiteral( "other window" ) );
    if( QTest::currentTestFailed() ) return;
    QCOMPARE( m.total(), 0 );

    // Positive control in the same process: back on the player window, Space fires again.
    other.hide();
    m.window.activateWindow();
    QVERIFY( QTest::qWaitForWindowActive( &m.window ) );
    QVERIFY( m.focusOn( m.viewer ) );
    QTest::keyClick( &m.window, Qt::Key_Space );
    QCOMPARE( m.counts.value( QStringLiteral( "actionPlay" ) ), 1 );
}

void GuiSmokeTest::playerShortcutMap_glContainerFocus()
{
    // The GL viewer path: a native child QWindow inside a window container has focus. The key
    // event goes to the QWindow, not to a widget; the window-scoped table action must still fire.
    PlayerShortcutMirror m;
    PlayerShortcutBinding::bindToWindow( &m.window );
    m.installGuard();
    QWindow *child = new QWindow();
    QWidget *container = QWidget::createWindowContainer( child, m.central );
    container->setMinimumSize( 160, 90 );
    container->setFocusPolicy( Qt::StrongFocus );
    m.layout->addWidget( container );
    QVERIFY( m.showAndActivate( true ) );
    container->setFocus( Qt::OtherFocusReason );
    child->requestActivate();
    if( !QTest::qWaitFor( [child]() { return QGuiApplication::focusWindow() == child; }, 2000 ) )
        QSKIP( "the QPA did not make the container's child QWindow the focus window; "
               "GL path UNMEASURED offscreen; venue proof owed by the umbrella smoke" );
    QTest::keyClick( child, Qt::Key_Right );
    QCOMPARE( m.counts.value( QStringLiteral( "actionNextFrame" ) ), 1 );
    QCOMPARE( m.total(), 1 );
}

void GuiSmokeTest::playerShortcutMap_oneSecondSeekTargetNeverOverflows()
{
    // Adversary pass 2 A1 (95d6cb1c): a 201-frame clip whose accepted metadata says 2147483648
    // fps (or 2147483647), paused at 100: Shift+Left/Right must clamp to the slider range, never
    // overflow an int on the way there.
    using PlayerShortcutBinding::oneSecondSeekTarget;
    QCOMPARE( oneSecondSeekTarget( 100, 2147483648.0, -1, 0, 200 ), 0 );
    QCOMPARE( oneSecondSeekTarget( 100, 2147483648.0, +1, 0, 200 ), 200 );
    QCOMPARE( oneSecondSeekTarget( 100, 2147483647.0, +1, 0, 200 ), 200 );
    QCOMPARE( oneSecondSeekTarget( 100, 2147483647.0, -1, 0, 200 ), 0 );
    QCOMPARE( oneSecondSeekTarget( 2147483647, 25.0, +1, 0, 2147483647 ), 2147483647 );
    QCOMPARE( oneSecondSeekTarget( -2147483647 - 1, 25.0, -1, -2147483647 - 1, 0 ), -2147483647 - 1 );
    QCOMPARE( oneSecondSeekTarget( 100, 1e300, +1, 0, 200 ), 200 );
    // Ordinary rates: one second of frames, rounded, at least one frame.
    QCOMPARE( oneSecondSeekTarget( 100, 25.0, +1, 0, 200 ), 125 );
    QCOMPARE( oneSecondSeekTarget( 100, 23.976, -1, 0, 200 ), 76 );
    QCOMPARE( oneSecondSeekTarget( 100, 0.4, +1, 0, 200 ), 101 );
    // No usable rate: one frame.
    QCOMPARE( oneSecondSeekTarget( 100, 0.0, +1, 0, 200 ), 101 );
    QCOMPARE( oneSecondSeekTarget( 100, -30.0, -1, 0, 200 ), 99 );
    QCOMPARE( oneSecondSeekTarget( 100, std::numeric_limits<double>::quiet_NaN(), +1, 0, 200 ), 101 );
    QCOMPARE( oneSecondSeekTarget( 100, std::numeric_limits<double>::infinity(), -1, 0, 200 ), 99 );
    // Clamped at the ends; an empty range stays at its minimum.
    QCOMPARE( oneSecondSeekTarget( 10, 25.0, -1, 0, 200 ), 0 );
    QCOMPARE( oneSecondSeekTarget( 190, 25.0, +1, 0, 200 ), 200 );
    QCOMPARE( oneSecondSeekTarget( 0, 25.0, +1, 0, -1 ), 0 );
}

void GuiSmokeTest::playerShortcutMap_helpRowsAreGeneratedFromLiveActions()
{
    // Help > Keyboard Shortcuts is built from the live actions: table rows first in table order,
    // then every other window action with a key, then described QShortcuts. A new action shows
    // up with no list edit anywhere.
    PlayerShortcutMirror m;
    PlayerShortcutBinding::bindToWindow( &m.window );
    QAction *unrelated = new QAction( QStringLiteral( "Zz &Unrelated" ), &m.window );
    unrelated->setShortcut( seq( "Ctrl+Shift+Y" ) );
    new QAction( QStringLiteral( "No key" ), &m.window );                 // no shortcut: not listed
    QAction *hidden = new QAction( QStringLiteral( "Hidden" ), &m.window );
    hidden->setShortcut( seq( "Ctrl+Shift+H" ) );
    hidden->setVisible( false );                                           // a dead key: not listed
    QShortcut *described = new QShortcut( seq( "Esc" ), &m.window );
    described->setWhatsThis( QStringLiteral( "Exit fullscreen" ) );
    new QShortcut( seq( "Q" ), &m.window );                               // undescribed: listed by its owner's name
    // Adversary A1 (pass 1, 0142890b): keys that live OFF the window's direct children must be
    // listed too -- a tool button's own shortcut (MainWindow.ui M, Shift+I), a QShortcut owned by
    // a child widget (GraphicsZoomView zoom in/out) and an action in a submenu (QRecentFilesMenu
    // Ctrl+R). A dialog's keys are its own and stay out.
    QToolButton *badPixels = new QToolButton( m.central );
    badPixels->setToolTip( QStringLiteral( "Select Bad Pixels" ) );
    badPixels->setShortcut( seq( "M" ) );
    QToolButton *cutIn = new QToolButton( m.central );
    cutIn->setText( QStringLiteral( "&In" ) );
    cutIn->setShortcut( seq( "Shift+I" ) );
    QShortcut *zoomIn = new QShortcut( QKeySequence::ZoomIn, m.viewer );
    zoomIn->setWhatsThis( QStringLiteral( "Zoom in" ) );
    QMenu *recent = m.window.menuBar()->actions().first()->menu()->addMenu( QStringLiteral( "Recent" ) );
    recent->addAction( QStringLiteral( "1 recent session" ) )->setShortcut( seq( "Ctrl+R" ) );
    QDialog *otherWindow = new QDialog( &m.window );
    QAction *dialogAction = new QAction( QStringLiteral( "Dialog action" ), otherWindow );
    dialogAction->setShortcut( seq( "Ctrl+Shift+D" ) );
    otherWindow->addAction( dialogAction );
    new QShortcut( seq( "Ctrl+Shift+G" ), otherWindow );
    QToolButton *dialogButton = new QToolButton( otherWindow );
    dialogButton->setShortcut( seq( "Ctrl+Shift+B" ) );

    const QList<PlayerShortcutBinding::ShortcutRow> rows = PlayerShortcutBinding::shortcutRows( &m.window );
    const QList<OracleAction> oracle = standard_map_oracle();
    QStringList listed;
    for( const PlayerShortcutBinding::ShortcutRow &r : rows ) listed << r.action + QStringLiteral( " = " ) + r.keys;
    const QString eq = QStringLiteral( " = " );
    const int n = int( oracle.size() );
    const QStringList expectedExtras = QStringList()
        << QStringLiteral( "1 recent session" ) + eq + seq( "Ctrl+R" ).toString( QKeySequence::NativeText )
        << QStringLiteral( "In" ) + eq + seq( "Shift+I" ).toString( QKeySequence::NativeText )
        << QStringLiteral( "Select Bad Pixels" ) + eq + seq( "M" ).toString( QKeySequence::NativeText )
        << QStringLiteral( "Zz Unrelated" ) + eq + seq( "Ctrl+Shift+Y" ).toString( QKeySequence::NativeText )
        << QStringLiteral( "Exit fullscreen" ) + eq + seq( "Esc" ).toString( QKeySequence::NativeText )
        << QStringLiteral( "QMainWindow" ) + eq + seq( "Q" ).toString( QKeySequence::NativeText )
        << QStringLiteral( "Zoom in" ) + eq + PlayerShortcutBinding::keysText( zoomIn->keys() );
    QCOMPARE( listed.mid( n ), expectedExtras );
    QCOMPARE( rows.size(), n + int( expectedExtras.size() ) );
    for( int i = 0; i < oracle.size(); ++i )
    {
        const OracleAction &row = oracle.at( i );
        QCOMPARE( rows.at( i ).action, row.name ); // the text "&<name>" without the mnemonic
        QCOMPARE( rows.at( i ).keys, PlayerShortcutBinding::keysText( m.action( row.name )->shortcuts() ) );
        bool hasAlias = false;
        for( const OracleKey &k : row.keys )
        {
            QVERIFY2( rows.at( i ).keys.contains( k.sequence.toString( QKeySequence::NativeText ) ),
                      qPrintable( QStringLiteral( "%1 row lacks %2" ).arg( row.name, k.sequence.toString() ) ) );
            if( k.kind == KeyKind::Alias )
            {
                hasAlias = true;
                QVERIFY( rows.at( i ).note.contains( k.sequence.toString( QKeySequence::NativeText ) ) );
            }
        }
        QCOMPARE( rows.at( i ).note.contains( PlayerShortcutBinding::aliasRetireNote() ), hasAlias );
    }
    // Nothing from the dialog, which is its own window with its own keys.
    QVERIFY( !listed.join( QLatin1Char( '\n' ) ).contains( QStringLiteral( "Dialog action" ) ) );
    QVERIFY( !listed.join( QLatin1Char( '\n' ) ).contains( seq( "Ctrl+Shift+G" ).toString( QKeySequence::NativeText ) ) );
    QVERIFY( !listed.join( QLatin1Char( '\n' ) ).contains( seq( "Ctrl+Shift+B" ).toString( QKeySequence::NativeText ) ) );
    QVERIFY( !listed.join( QLatin1Char( '\n' ) ).contains( seq( "Ctrl+Shift+H" ).toString( QKeySequence::NativeText ) ) );

    std::unique_ptr<QDialog> dialog( PlayerShortcutBinding::createShortcutsDialog( &m.window ) );
    QVERIFY( dialog );
    QTableWidget *table = dialog->findChild<QTableWidget*>();
    QVERIFY( table );
    QCOMPARE( table->rowCount(), int( rows.size() ) );
    QCOMPARE( table->columnCount(), 3 );
    QVERIFY( table->editTriggers() == QAbstractItemView::NoEditTriggers );
    for( int r = 0; r < rows.size(); ++r )
    {
        QCOMPARE( table->item( r, 0 )->text(), rows.at( r ).action );
        QCOMPARE( table->item( r, 1 )->text(), rows.at( r ).keys );
        QCOMPARE( table->item( r, 2 )->text(), rows.at( r ).note );
        QVERIFY( !( table->item( r, 0 )->flags() & Qt::ItemIsEditable ) );
    }
    QVERIFY( dialog->findChild<QDialogButtonBox*>() );
}

namespace
{

// PLAYER-TOOLBAR-TRANSPORT-BUTTONS-1: a main window carrying the transport cluster (named by
// PlayerTransportToolbar::clusterActionNames(), in order) on a toolbar, plus a widget like the
// playback-quality button. Stop and Goto Last Frame have no icon, as in MainWindow.ui.
class TransportToolbarFixture
{
public:
    TransportToolbarFixture()
    {
        bar = new QToolBar( &window );
        window.addToolBar( bar );
        QPixmap standIn( 24, 24 );
        standIn.fill( Qt::gray );
        const QStringList withoutIcon{ QStringLiteral( "actionStop" ), QStringLiteral( "actionGoto_Last_Frame" ) };
        const QMap<QString, QString> tips{
            { QStringLiteral( "actionGoto_First_Frame" ), QStringLiteral( "Jump to first frame..." ) },
            { QStringLiteral( "actionPlay" ), QStringLiteral( "Play clip..." ) },
            { QStringLiteral( "actionStop" ), QStringLiteral( "Stop and return to cut in" ) },
            { QStringLiteral( "actionGoto_Last_Frame" ), QStringLiteral( "Jump to last frame..." ) },
            { QStringLiteral( "actionAudioOutput" ), QStringLiteral( "Audio Output on Playback" ) } };
        for( const QString &name : PlayerTransportToolbar::clusterActionNames() )
        {
            QAction *action = new QAction( QString( name ).remove( QStringLiteral( "action" ) ), &window );
            action->setObjectName( name );
            if( !withoutIcon.contains( name ) ) action->setIcon( QIcon( standIn ) );
            if( tips.contains( name ) ) action->setToolTip( tips.value( name ) );
            bar->addAction( action );
            actions.insert( name, action );
        }
        quality = new QToolButton;
        quality->setToolTip( QStringLiteral( "quality" ) );
        bar->addWidget( quality );
    }

    QMainWindow window;
    QToolBar *bar = nullptr;
    QToolButton *quality = nullptr;
    QMap<QString, QAction*> actions;
};

QStringList transportToolTips( const TransportToolbarFixture &f )
{
    QStringList tips;
    for( const QString &name : PlayerTransportToolbar::clusterActionNames() )
        tips << f.actions.value( name )->toolTip() << f.bar->widgetForAction( f.actions.value( name ) )->toolTip();
    return tips;
}

} // namespace

void GuiSmokeTest::transportToolbar_tooltipsCarryTheLiveKeys()
{
    // G1: every table action's tooltip ends with its LIVE keys (set by bindToWindow, never typed
    // by hand); unbound Stop and Audio Output get none; the quality widget is left alone.
    TransportToolbarFixture f;
    PlayerShortcutBinding::bindToWindow( &f.window );
    PlayerTransportToolbar::finish( f.bar, &f.window );
    const QStringList table = PlayerShortcutBinding::liveActionNames();
    int keyed = 0;
    for( const QString &name : PlayerTransportToolbar::clusterActionNames() )
    {
        QAction *action = f.actions.value( name );
        QVERIFY2( action, qPrintable( name ) );
        QWidget *button = f.bar->widgetForAction( action );
        QVERIFY2( button, qPrintable( name ) );
        QCOMPARE( button->toolTip(), action->toolTip() );
        if( table.contains( name ) )
        {
            QVERIFY2( !action->shortcuts().isEmpty(), qPrintable( name ) );
            const QString suffix = QStringLiteral( "(" ) + PlayerShortcutBinding::keysText( action->shortcuts() ) + QStringLiteral( ")" );
            QVERIFY2( action->toolTip().endsWith( suffix ), qPrintable( name + QStringLiteral( ": " ) + action->toolTip() ) );
            ++keyed;
        }
    }
    QCOMPARE( keyed, 9 );
    QVERIFY( !f.actions.value( QStringLiteral( "actionStop" ) )->toolTip().contains( QStringLiteral( " (" ) ) );
    QCOMPARE( f.actions.value( QStringLiteral( "actionStop" ) )->toolTip(), QStringLiteral( "Stop and return to cut in" ) );
    QVERIFY( !f.actions.value( QStringLiteral( "actionAudioOutput" ) )->toolTip().contains( QStringLiteral( " (" ) ) );
    // The trailing "..." goes before the keys are appended.
    QCOMPARE( f.actions.value( QStringLiteral( "actionGoto_Last_Frame" ) )->toolTip(),
              QStringLiteral( "Jump to last frame (%1)" ).arg( QKeySequence( Qt::Key_End ).toString( QKeySequence::NativeText ) ) );
    QCOMPARE( f.quality->toolTip(), QStringLiteral( "quality" ) );
    QCOMPARE( PlayerTransportToolbar::keyedToolTip( QStringLiteral( "Play." ), {} ), QStringLiteral( "Play." ) );
    QCOMPARE( PlayerTransportToolbar::keyedToolTip( QStringLiteral( "Play clip... " ), { QKeySequence( Qt::Key_P ) } ),
              QStringLiteral( "Play clip (%1)" ).arg( QKeySequence( Qt::Key_P ).toString( QKeySequence::NativeText ) ) );
}

void GuiSmokeTest::transportToolbar_finishIsIdempotent()
{
    // G2: running finish() again never stacks a second key suffix.
    TransportToolbarFixture f;
    PlayerShortcutBinding::bindToWindow( &f.window );
    PlayerTransportToolbar::finish( f.bar, &f.window );
    const QStringList once = transportToolTips( f );
    PlayerTransportToolbar::finish( f.bar, &f.window );
    QCOMPARE( transportToolTips( f ), once );
    QCOMPARE( f.quality->toolTip(), QStringLiteral( "quality" ) );
}

void GuiSmokeTest::transportToolbar_runtimeIconsAreDrawn()
{
    // G3: gui_tests does not link ressources.qrc, so stand-in Play / First Frame images are
    // written to a temporary directory and handed to finish().
    QTemporaryDir dir;
    QVERIFY( dir.isValid() );
    const QColor ink( 200, 30, 40 );
    QImage play( 24, 24, QImage::Format_ARGB32 );
    play.fill( Qt::transparent );
    for( int y = 4; y < 20; ++y )
        for( int x = 6; x < 6 + ( y < 12 ? y - 3 : 20 - y ); ++x )
            play.setPixelColor( x, y, ink );
    play.setPixelColor( 0, 0, QColor( 10, 10, 10, 40 ) );   // a faint pixel first in scan order
    const QString playPath = dir.filePath( QStringLiteral( "Play.png" ) );
    QVERIFY( play.save( playPath ) );
    QImage first( 24, 24, QImage::Format_ARGB32 );
    first.fill( Qt::transparent );
    first.setPixelColor( 1, 12, ink );                       // left-only mark
    const QString firstPath = dir.filePath( QStringLiteral( "FirstFrame.png" ) );
    QVERIFY( first.save( firstPath ) );

    TransportToolbarFixture f;
    PlayerShortcutBinding::bindToWindow( &f.window );
    PlayerTransportToolbar::finish( f.bar, &f.window, firstPath, playPath );
    QVERIFY( !f.actions.value( QStringLiteral( "actionStop" ) )->icon().isNull() );
    QVERIFY( !f.actions.value( QStringLiteral( "actionGoto_Last_Frame" ) )->icon().isNull() );

    const QImage stop = PlayerTransportToolbar::stopIcon( playPath ).pixmap( 24, 24 ).toImage().convertToFormat( QImage::Format_ARGB32 );
    QCOMPARE( stop.size(), QSize( 24, 24 ) );
    QCOMPARE( stop.pixelColor( 12, 12 ), ink );
    QCOMPARE( stop.pixelColor( 0, 0 ).alpha(), 0 );
    QCOMPARE( stop.pixelColor( 23, 23 ).alpha(), 0 );
    const QImage last = PlayerTransportToolbar::mirroredIcon( firstPath ).pixmap( 24, 24 ).toImage().convertToFormat( QImage::Format_ARGB32 );
    QCOMPARE( last.pixelColor( 22, 12 ), ink );
    QCOMPARE( last.pixelColor( 1, 12 ).alpha(), 0 );
    QVERIFY( PlayerTransportToolbar::stopIcon( dir.filePath( QStringLiteral( "missing.png" ) ) ).isNull() );
}

void GuiSmokeTest::transportToolbar_stopTargetFrameIsClampedCutIn()
{
    // G4: cut in is a 1-based spin value; Stop seeks to its clamped 0-based frame.
    using PlayerTransportToolbar::stopTargetFrame;
    QCOMPARE( stopTargetFrame( 1, 100 ), 0 );
    QCOMPARE( stopTargetFrame( 50, 100 ), 49 );
    QCOMPARE( stopTargetFrame( 500, 100 ), 99 );
    QCOMPARE( stopTargetFrame( 5, 0 ), 0 );
    QCOMPARE( stopTargetFrame( 0, 100 ), 0 );
    QCOMPARE( stopTargetFrame( std::numeric_limits<int>::min(), 100 ), 0 );
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

// PLAYBACK-SCOPES-OPTION-1: MainWindow feeds every scope flag through
// playbackScopeDisplayConsumesPresentedPixels(editArea, playing, mode). In Hide mode a
// visible histogram vetoes no GPU fast path while playing, and vetoes again once paused.
void GuiSmokeTest::mainWindowGpuPreviewPolicyHideScopesVetoesNothingWhilePlaying()
{
    const auto stateFor = [](bool playing, PlaybackScopesMode mode) {
        MainWindowGpuPreviewPolicyState state;
        state.gpuViewportInstalled = true;
        state.renderThreadUsing16BitPreview = true;
        state.histogramEnabled = mainWindowScopeActionConsumesPresentedPixels(
            playbackScopeDisplayConsumesPresentedPixels(true, playing, mode), true);
        return state;
    };
    const auto textureRouteEligible = [](const MainWindowGpuPreviewPolicyState &state) {
        return mainWindowGpuPlaybackReconTextureRouteEligibleAtScaleOne(
            true, true, mainWindowHasScopeVisualization(state), true, true, true, false, true, false);
    };

    const MainWindowGpuPreviewPolicyState hidePlaying = stateFor(true, PlaybackScopesMode::Hide);
    QVERIFY(!mainWindowHasScopeVisualization(hidePlaying));
    QVERIFY(mainWindowAllowsGpu16PreviewRender(hidePlaying));
    QVERIFY(mainWindowUsesGpu16PreviewPresentation(hidePlaying));
    QVERIFY(textureRouteEligible(hidePlaying));

    const MainWindowGpuPreviewPolicyState hidePaused = stateFor(false, PlaybackScopesMode::Hide);
    QVERIFY(mainWindowHasScopeVisualization(hidePaused));
    QVERIFY(!mainWindowAllowsGpu16PreviewRender(hidePaused));
    QVERIFY(!textureRouteEligible(hidePaused));

    for (PlaybackScopesMode mode : { PlaybackScopesMode::Throttled, PlaybackScopesMode::Live })
    {
        const MainWindowGpuPreviewPolicyState playing = stateFor(true, mode);
        QVERIFY(mainWindowHasScopeVisualization(playing));
        QVERIFY(!mainWindowAllowsGpu16PreviewRender(playing));
        QVERIFY(!textureRouteEligible(playing));
        QCOMPARE(QString::fromLatin1(mainWindowGpuPlaybackPipelineStatusBadgeLabel(
                     GpuPlaybackPipelineStatus::Cpu, playing, true)),
                 QStringLiteral("CPU - scopes active"));
    }
    QCOMPARE(QString::fromLatin1(mainWindowGpuPlaybackPipelineStatusBadgeLabel(
                 GpuPlaybackPipelineStatus::Cpu, hidePlaying, true)),
             QStringLiteral("CPU"));
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

// GPU-DEVICE-LOSS-SELF-HEAL-1 r3: with MLVAPP_EXPERIMENTAL_GPU_PROCESSING set (the CUDA preset), a
// CUDA-suppressed session lands on the plain CPU route, not GpuPreview.
// r5, the declared route (hub ruling r5): the fixture builds the state as MainWindow::drawFrame
// does (16-bit preview = mainWindowAllowsGpu16PreviewRender, the CPU preview route when fast
// processing for playback is on and GPU preview processing is not, OutputDebayered16 for either
// preview-processing route, and the honour-scale admission from those). A suppressed session must
// take the CPU-subset Debayered16 route with the reduced recon admitted; the no-fault CUDA session
// keeps GpuPreview, where the reduced CPU recon is refused. The GL surface the preset installed
// stays, so the frame presents through the 16-bit GL path (direct8=0 at the venue). Turning 16-bit
// off (r4's F1) would make it GpuImagePresentation, which the playback prep refuses to reduce
// (PlaybackPrepCpuRoute.cpp, pinned in the pipeline suite), so the reduced recon would be refused.
void GuiSmokeTest::mainWindowGpuPreviewPolicyRoutesCudaSuppressedSessionToCpu()
{
    QVERIFY(mainWindowGpuPreviewProcessingRequestedForSession(true, false));
    QVERIFY(!mainWindowGpuPreviewProcessingRequestedForSession(true, true));
    QVERIFY(!mainWindowGpuPreviewProcessingRequestedForSession(false, false));
    QVERIFY(!mainWindowGpuPreviewProcessingRequestedForSession(false, true));

    for (const bool suppressed : {false, true})
    {
        MainWindowGpuPreviewPolicyState state;
        state.gpuViewportInstalled = true;
        state.gpuWidgetViewportInstalled = true;
        state.gpuPreviewProcessingBackendRequest = GpuPreviewProcessingBackendRequest::Auto;
        state.gpuPreviewProcessingEnvironmentRequested =
            mainWindowGpuPreviewProcessingRequestedForSession(true, suppressed);
        state.gpuPreviewProcessingCompatible = true;
        state.gpuBilinearDebayerEnvironmentRequested = true;
        state.gpuBilinearDebayerCompatible = true;
        state.playbackScaleFactorActive = 4;
        state.renderThreadUsing16BitPreview = mainWindowAllowsGpu16PreviewRender(state);
        state.renderThreadUsingGpuProcessingPreview = mainWindowAllowsGpuPreviewProcessing(state);
        state.renderThreadUsingGpuBilinearDebayer = mainWindowAllowsGpuBilinearDebayer(state);

        QCOMPARE(mainWindowAllowsGpuPreviewProcessing(state), !suppressed);
        QCOMPARE(mainWindowUsesGpuPreviewProcessing(state), !suppressed);
        QCOMPARE(mainWindowAllowsGpuBilinearDebayer(state), !suppressed);
        QCOMPARE(static_cast<int>(mainWindowGpuPlaybackPipelineStatus(state, false, false, false)),
                 static_cast<int>(suppressed ? GpuPlaybackPipelineStatus::Cpu
                                             : GpuPlaybackPipelineStatus::GpuPreview));

        // The route drawFrame requests (fast processing for playback on, its config enabled).
        const bool cpuPreviewRoute = !state.renderThreadUsingGpuProcessingPreview;
        const bool debayered16 = state.renderThreadUsingGpuProcessingPreview || cpuPreviewRoute;
        QVERIFY(debayered16);
        QCOMPARE(cpuPreviewRoute, suppressed);
        QVERIFY(state.renderThreadUsing16BitPreview);
        QVERIFY(mainWindowUsesGpu16PreviewPresentation(state));
        QVERIFY(!mainWindowUsesGpuImagePresentation(state));

        Debayered16HonourScaleInputs honourScale;
        honourScale.playbackActive = true;
        honourScale.cpuPreviewRoute = cpuPreviewRoute && debayered16;
        honourScale.gpuPreviewRoute = state.renderThreadUsingGpuProcessingPreview;
        honourScale.gpuBilinearDebayer = state.renderThreadUsingGpuBilinearDebayer;
        honourScale.requestedScale = state.playbackScaleFactorActive;
        // The prep reduces by the scale only without GpuImagePresentation (asserted above).
        honourScale.prepFactor = mainWindowUsesGpuImagePresentation(state) ? 1 : state.playbackScaleFactorActive;
        QCOMPARE(static_cast<int>(debayered16HonourScaleRefusal(honourScale)),
                 static_cast<int>(suppressed ? Debayered16HonourScaleRefusal::None
                                             : Debayered16HonourScaleRefusal::NotCpuPreviewRoute));

        if (suppressed)
        {
            // r4's F1 (16-bit off while suppressed): the GL surface presents the 8-bit image instead,
            // and the prep would refuse the reduction, so the reduced recon is refused too.
            MainWindowGpuPreviewPolicyState f1 = state;
            f1.renderThreadUsing16BitPreview = false;
            QVERIFY(mainWindowUsesGpuImagePresentation(f1));
            honourScale.prepFactor = mainWindowUsesGpuImagePresentation(f1) ? 1 : f1.playbackScaleFactorActive;
            QCOMPARE(static_cast<int>(debayered16HonourScaleRefusal(honourScale)),
                     static_cast<int>(Debayered16HonourScaleRefusal::PrepWouldNotReduce));
        }
    }
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

    // PLAYBACK-SEEK-RENDER-PARITY-1: a stage the live display shader does not
    // apply refuses BOTH texture-present routes, whatever else is eligible.
    QVERIFY(mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
    state.gpuPreviewProcessingDisplayShaderCompatible = false;
    QVERIFY(!mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
    QVERIFY(!mainWindowAllowsGpuAmazeTexturePresentation(state));
    QVERIFY(!mainWindowUsesGpuAmazeTexturePresentation(state));
    state.gpuPreviewProcessingDisplayShaderCompatible = true;
    QVERIFY(mainWindowAllowsGpuPlaybackReconTexturePresentation(state));
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

void GuiSmokeTest::mainWindowHonoursGpuTextureRoutePreviewScaleForLatchedSession()
{
    // PLAYBACK-CUDA-HONOUR-SCALE-1 (b): with the texture route eligible, a play
    // session latched as honourable keeps x2/x4 instead of clamping to 1.
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 2, true, true, true ), 2 );
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 4, true, true, true ), 4 );
    // (d) the mutation guard: restoring the unconditional clamp turns the two
    // lines above red. Everything outside an honoured session keeps the clamp:
    // a refused session, x8, scale 1, and paused/scrubbed requests (e).
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 2, true, true, false ), 1 );
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 4, true, true, false ), 1 );
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 8, true, true, true ), 1 );
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 1, true, true, true ), 1 );
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 2, true, false, true ), 1 );
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 4, true, false, true ), 1 );
    // Texture route not eligible: the request passes through, as before.
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 2, false, true, false ), 2 );
    QCOMPARE( mainWindowGpuTextureRouteEffectivePlaybackScale( 8, false, true, false ), 8 );

    // The policy builders' former 'scale == 1' gates: x2/x4 admit the texture
    // route only at the latched session scale.
    QVERIFY( mainWindowGpuTextureRouteAdmitsPlaybackScale( 1, 1 ) );
    QVERIFY( mainWindowGpuTextureRouteAdmitsPlaybackScale( 2, 2 ) );
    QVERIFY( mainWindowGpuTextureRouteAdmitsPlaybackScale( 4, 4 ) );
    QVERIFY( !mainWindowGpuTextureRouteAdmitsPlaybackScale( 2, 1 ) );
    QVERIFY( !mainWindowGpuTextureRouteAdmitsPlaybackScale( 4, 2 ) );
    QVERIFY( !mainWindowGpuTextureRouteAdmitsPlaybackScale( 8, 8 ) );
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

namespace
{
// PLAYER-CURSOR-AUTOHIDE-1: drives the same Controller / CursorHider / eventIsActivity calls that
// MainWindow::viewerIdleNoteEvent, viewerIdleRestore and viewerIdleTimeout make, with a short idle
// interval so the live single-shot timer can be observed in a test.
class ViewerIdleHarness : public QObject
{
public:
    ViewerIdleHarness( const viewer_idle::Surfaces &surfaces, int idleMs )
        : m_surfaces( surfaces ), m_idleMs( idleMs )
    {
        inputs.fullscreen = true;
        inputs.windowActive = true;
        m_clock.start();
        m_timer.setSingleShot( true );
        QObject::connect( &m_timer, &QTimer::timeout, this, [this]() { timeout(); } );
    }

    void enterFullscreen()
    {
        ++entries;
        m_idle.setFullscreen( true, m_clock.elapsed() );
        m_haveLast = false;
        m_pointer.seed( QCursor::pos() );
#ifdef Q_OS_WIN
        hookInstalled = viewer_idle::win_hook::install();
        if( !markHistory ) viewer_idle::win_hook::historyMark().unmark();
#endif
        m_timer.start( firstLookMs() );
    }

    void leaveFullscreen()
    {
        m_timer.stop();
#ifdef Q_OS_WIN
        viewer_idle::win_hook::remove();
#endif
        if( m_idle.forceShown() ) m_hider.restore( m_surfaces );
        m_idle.setFullscreen( false, m_clock.elapsed() );
    }

    bool hidden() const { return m_idle.hidden(); }

    viewer_idle::Inputs inputs;
    // Emulates a wait that excludes user input (SyncDownloadWaiter-style): input events are not noted.
    bool inputExcluded = false;
    // r2: false reproduces the r1 tick, which did not read the thread mouse hook's witness (RED control).
    bool readRetrievalWitness = true;
    bool hookInstalled = false;
    // r3: the tick off Windows, where the position sample is the only witness (no hook, no queue status).
    bool positionSampleOnly = false;
    // r3: MainWindow passes viewer_idle::kPointerPollMs(); 0 keeps the uncapped tick (the pass-4 control).
    int pollMs = 0;
    // r6 (key B1, pass 8 A1): install() and every MainWindow tick mark the system move history; false marks
    // nothing (install's mark is undone), which is the r5 tick (RED control).
    bool markHistory = true;
    // r7 (key B1): true is the b03e08c5 tick, whose queue fallback counts any queued mouse input (RED control).
    bool queueStatusTick = false;
    // r7: how many ticks returned Show (a control that re-hides one idle period later is still seen).
    int shows = 0;
    // r11 (merge_group eviction of ca1c1b53): a tick's Show lasts one idle period, since the next tick hides again
    // with nothing moving. At 40 ms that is shorter than QTRY's 50 ms poll step, so a QTRY on the cursor shape can
    // miss the whole shown state. The Show tick records here what both surfaces and the controller were right after
    // it restored them; a test waits on shows and reads these, which no poll step can miss.
    Qt::CursorShape shownViewportShape = Qt::BlankCursor;
    Qt::CursorShape shownGlShape = Qt::BlankCursor;
    bool shownHidden = true;
    // r7 (adversary pass 11 A1): MainWindow's isFullScreen() at a tick (false: the window manager restored it).
    bool windowReallyFullscreen = true;
    bool timerActive() const { return m_timer.isActive(); }
    // r8 (adversary pass 11 A1): false is the 5426572e tick, which never asks the window's real state (RED control).
    bool windowStateTick = true;
    // r8: how many ticks ran (a RED control proves "still polling" by tick count, not by wall time).
    int ticks = 0;
    // r10 (sol r9 B1): the Fullscreen action's check, and how many times the idle machinery was started.
    bool actionChecked = true;
    int entries = 0;

    // r10 (sol r9 B1): as MainWindow::changeEvent on QEvent::WindowStateChange (native exit, native re-entry,
    // restore from minimize), with the window's real state in windowReallyFullscreen.
    void windowStateChanged()
    {
        switch( m_idle.onWindowState( actionChecked, windowReallyFullscreen, m_clock.elapsed(), m_idleMs ) )
        {
        case viewer_idle::Lifecycle::Start:
            enterFullscreen();
            m_idle.onWindowState( actionChecked, windowReallyFullscreen, m_clock.elapsed(), m_idleMs );
            break;
        case viewer_idle::Lifecycle::Stop: leaveFullscreen(); break;
        case viewer_idle::Lifecycle::None: break;
        }
    }

protected:
    bool eventFilter( QObject *watched, QEvent *event ) override
    {
        if( !inputExcluded ) note( event );
        return QObject::eventFilter( watched, event );
    }

private:
    // MainWindow starts the timer with kPointerPollMs(); with no cap the first look is one idle period.
    int firstLookMs() const { return pollMs > 0 && pollMs < m_idleMs ? pollMs : m_idleMs; }

#ifdef Q_OS_WIN
    // The event path saw the input. As MainWindow (r11, pass 15 A1): nothing pending and the history marked again.
    // With markHistory false (the r5 RED control, which marks nothing) it is the r5 event path: the bare clear.
    void noteDeliveredAsTheTick()
    {
        if( markHistory ) viewer_idle::win_hook::noteDelivered();
        else viewer_idle::win_hook::witness().clear();
    }
#endif

    void restoreNow()
    {
        if( m_idle.forceShown() ) m_hider.restore( m_surfaces );
        if( m_idle.fullscreen() )
        {
            m_pointer.seed( QCursor::pos() );
#ifdef Q_OS_WIN
            noteDeliveredAsTheTick();
#endif
            m_idle.noteActivity( m_clock.elapsed() );
            if( !m_timer.isActive() ) m_timer.start( firstLookMs() );
        }
    }

    void note( QEvent *event )
    {
        if( !m_idle.fullscreen() ) return;
        if( viewer_idle::forcesRestore( event->type() ) )
        {
            restoreNow();
            return;
        }
        if( viewer_idle::eventIsActivity( event, &m_lastGlobal, &m_haveLast ) )
        {
            m_pointer.seed( QCursor::pos() );
#ifdef Q_OS_WIN
            noteDeliveredAsTheTick();
#endif
            if( m_idle.noteActivity( m_clock.elapsed() ) ) m_hider.restore( m_surfaces );
            if( !m_timer.isActive() ) m_timer.start( firstLookMs() );
        }
    }

    void timeout()
    {
        ++ticks;
        // As MainWindow (r7, adversary pass 11 A1): fullscreen left without the action leaves the idle machinery.
        if( windowStateTick && m_idle.windowLeftFullscreen( windowReallyFullscreen, m_clock.elapsed(), m_idleMs ) )
        {
            leaveFullscreen();
            return;
        }
        int ms = 0;
        viewer_idle::Inputs in = inputs;
        // As MainWindow::viewerIdleInputs (r8): the hide gate is the action's check AND isFullScreen().
        in.fullscreen = inputs.fullscreen && windowReallyFullscreen;
        // As MainWindow: a logical QPoint, so a sub-logical-pixel move is not seen here (r4 bound, PointerWatch).
        in.pointerMoved = m_pointer.moved( QCursor::pos() );
#ifdef Q_OS_WIN
        if( positionSampleOnly ) {}
        else if( readRetrievalWitness )
        {
            if( viewer_idle::win_hook::witness().take() ) in.pointerMoved = true;
            if( queueStatusTick )
            {
                // The b03e08c5 tick (r7 RED control): any queued mouse input counts, then the mark.
                if( HIWORD( viewer_idle::win_hook::queueStatusReader()( QS_MOUSE ) ) & QS_MOUSE ) in.pointerMoved = true;
                viewer_idle::win_hook::markHistory();
            }
            else if( markHistory )
            {
                if( viewer_idle::win_hook::lookAtQueue() ) in.pointerMoved = true;   // as MainWindow (r7, key B1)
            }
            else if( HIWORD( GetQueueStatus( QS_MOUSE ) ) & QS_MOUSE ) in.pointerMoved = true;   // the r5 tick
            in.witnessMissing = !viewer_idle::win_hook::installed();   // as MainWindow (r6, pass 9 A1)
        }
        else if( HIWORD( GetQueueStatus( QS_MOUSEMOVE ) ) & QS_MOUSEMOVE ) in.pointerMoved = true;   // the r1 tick
#endif
        switch( m_idle.onTimeout( in, m_clock.elapsed(), m_idleMs, &ms, pollMs ) )
        {
        case viewer_idle::Tick::Hide: m_hider.hide( m_surfaces ); m_timer.start( ms ); break;
        case viewer_idle::Tick::Show:
            m_hider.restore( m_surfaces );
            shownViewportShape = m_surfaces.viewport ? m_surfaces.viewport->cursor().shape() : Qt::BlankCursor;
            shownGlShape = m_surfaces.glWindow ? m_surfaces.glWindow->cursor().shape() : Qt::BlankCursor;
            shownHidden = m_idle.hidden();
            ++shows;
            m_timer.start( ms );
            break;
        case viewer_idle::Tick::Rearm: m_timer.start( ms ); break;
        case viewer_idle::Tick::None: break;
        }
    }

    viewer_idle::Surfaces m_surfaces;
    int m_idleMs;
    viewer_idle::Controller m_idle;
    viewer_idle::CursorHider m_hider;
    QTimer m_timer;
    QElapsedTimer m_clock;
    QPointF m_lastGlobal;
    bool m_haveLast = false;
    viewer_idle::PointerWatch m_pointer;
};

// A plain mouse move with no button held. The (type, local, global, button, buttons, modifiers)
// constructor exists in Qt 5 and Qt 6 alike.
QMouseEvent viewerIdleMove( const QPoint &global )
{
    return QMouseEvent( QEvent::MouseMove, QPointF( 5, 5 ), QPointF( global ),
                        Qt::NoButton, Qt::NoButton, Qt::NoModifier );
}
} // namespace

// G1
void GuiSmokeTest::viewerCursorAutohide_hideBlanksBothSurfacesAndRestoreGivesBackTheToolCursor()
{
    QWidget viewport;
    viewport.setCursor( Qt::OpenHandCursor );
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;
    viewer_idle::CursorHider hider;

    hider.hide( surfaces );
    QCOMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );

    hider.restore( surfaces );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
    QVERIFY( gl.cursor().shape() != Qt::BlankCursor );

    // Null surfaces are skipped.
    hider.hide( viewer_idle::Surfaces() );
    hider.restore( viewer_idle::Surfaces() );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
}

// G2
void GuiSmokeTest::viewerCursorAutohide_restoreLeavesAnUnsetViewportCursorUnset()
{
    // A child widget, like the QGraphicsView viewport: QWidget::unsetCursor() clears WA_SetCursor only
    // on a non-window widget.
    QWidget view;
    QWidget viewport( &view );
    QVERIFY( !viewport.testAttribute( Qt::WA_SetCursor ) );
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    viewer_idle::CursorHider hider;

    hider.hide( surfaces );
    QVERIFY( viewport.testAttribute( Qt::WA_SetCursor ) );
    QCOMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    hider.restore( surfaces );
    QVERIFY( !viewport.testAttribute( Qt::WA_SetCursor ) );
}

// G3: a viewer tool armed while hidden has set its own cursor; restore must not overwrite it.
void GuiSmokeTest::viewerCursorAutohide_restoreKeepsACursorSetWhileHidden()
{
    QWidget viewport;
    viewport.setCursor( Qt::OpenHandCursor );
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;
    viewer_idle::CursorHider hider;

    hider.hide( surfaces );
    viewport.setCursor( Qt::CrossCursor );
    gl.setCursor( Qt::CrossCursor );
    hider.restore( surfaces );
    QCOMPARE( viewport.cursor().shape(), Qt::CrossCursor );
    QCOMPARE( gl.cursor().shape(), Qt::CrossCursor );
}

// G4: the live single-shot timer hides; a real move restores at once; a same-position move does
// not; a modal blocking the window restores.
void GuiSmokeTest::viewerCursorAutohide_idleTimerHidesAndRealMovementOrBlockingRestores()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;

    ViewerIdleHarness harness( surfaces, 40 );
    qApp->installEventFilter( &harness );
    auto removeFilter = qScopeGuard( [&harness]() { qApp->removeEventFilter( &harness ); } );

    harness.enterFullscreen();
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
    QVERIFY( harness.hidden() );

    QMouseEvent moveA = viewerIdleMove( QPoint( 100, 100 ) );
    QCoreApplication::sendEvent( &viewport, &moveA );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
    QVERIFY( gl.cursor().shape() != Qt::BlankCursor );
    QVERIFY( !harness.hidden() );

    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QMouseEvent sameA = viewerIdleMove( QPoint( 100, 100 ) );
    QCoreApplication::sendEvent( &viewport, &sameA );
    QCOMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QVERIFY( harness.hidden() );

    QEvent blocked( QEvent::WindowBlocked );
    QCoreApplication::sendEvent( &viewport, &blocked );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
    QVERIFY( !harness.hidden() );

    // While a gate fails (a tool armed) the timer only rearms: the cursor stays shown.
    harness.inputs.toolArmed = true;
    QTest::qWait( 200 );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
    harness.inputs.toolArmed = false;
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );

    harness.leaveFullscreen();
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
    QVERIFY( gl.cursor().shape() != Qt::BlankCursor );
}

// G5 (adversary A1): at a fractional device-pixel ratio one physical pixel is a sub-logical-pixel
// global move; it is real movement and must restore the cursor.
void GuiSmokeTest::viewerCursorAutohide_subPixelMoveRestores()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;

    ViewerIdleHarness harness( surfaces, 40 );
    qApp->installEventFilter( &harness );
    auto removeFilter = qScopeGuard( [&harness]() { qApp->removeEventFilter( &harness ); } );
    harness.enterFullscreen();

    QMouseEvent first( QEvent::MouseMove, QPointF( 5, 5 ), QPointF( 100.0, 100.0 ),
                       Qt::NoButton, Qt::NoButton, Qt::NoModifier );
    QCoreApplication::sendEvent( &viewport, &first );
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );

    QMouseEvent onePhysicalPixel( QEvent::MouseMove, QPointF( 5.333333, 5 ), QPointF( 100.0 + 1.0 / 3.0, 100.0 ),
                                  Qt::NoButton, Qt::NoButton, Qt::NoModifier );
    QCoreApplication::sendEvent( &viewport, &onePhysicalPixel );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
    QVERIFY( !harness.hidden() );
    harness.leaveFullscreen();
}

// G6 (adversary A2): while a wait excludes user input, timers still run but mouse events are not
// delivered. The OS pointer moving must still keep the cursor shown / bring it back.
void GuiSmokeTest::viewerCursorAutohide_pointerMovedWhileInputExcludedRestores()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;

    // Positions inside the primary screen, so a native platform does not clip both to the same edge point.
    QScreen *screen = QGuiApplication::primaryScreen();
    QVERIFY( screen != nullptr );
    const QPoint start = screen->geometry().center();
    QCursor::setPos( start );
    ViewerIdleHarness harness( surfaces, 40 );
    qApp->installEventFilter( &harness );
    auto removeFilter = qScopeGuard( [&harness]() { qApp->removeEventFilter( &harness ); } );
    harness.enterFullscreen();
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );

    harness.inputExcluded = true;
    const int showsBefore = harness.shows;
    QCursor::setPos( start + QPoint( 10, 0 ) );
    // r11: one sampled move is one Show tick, shown for one idle period (40 ms) only: wait on the tick, not the shape.
    QTRY_VERIFY_WITH_TIMEOUT( harness.shows > showsBefore, 1000 );
    QCOMPARE( harness.shownViewportShape, Qt::OpenHandCursor );
    QVERIFY( !harness.shownHidden );
    harness.inputExcluded = false;
    harness.leaveFullscreen();
}

#ifdef Q_OS_WIN
// G7 (r2, adversary r1 pass 3 A1): in an input-excluded wait Qt's dispatcher takes WM_MOUSEMOVE off the Win32
// queue into its own deferred queue. A move away and back between two ticks then leaves the sampled position
// unchanged and the queue empty, and no Qt event arrives. The thread mouse hook sees both moves as they are
// taken; the tick reads it and both surfaces (viewport and GL window) get their cursor back within one idle
// period. Control: the r1 tick (sample + queue status only) keeps both hidden for many idle periods.
// Windows-only: the witness is a Win32 thread WH_MOUSE hook (r3 hub ruling: compiled on Windows, no skip branch).
void GuiSmokeTest::viewerCursorAutohide_deferredAwayAndBackMoveRestoresOnBothSurfaces()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;

    QScreen *screen = QGuiApplication::primaryScreen();
    QVERIFY( screen != nullptr );
    const QPoint start = screen->geometry().center();
    QCursor::setPos( start );
    ViewerIdleHarness harness( surfaces, 40 );
    qApp->installEventFilter( &harness );
    auto cleanup = qScopeGuard( [&harness]() {
        qApp->removeEventFilter( &harness );
        viewer_idle::win_hook::remove();
    } );
    harness.enterFullscreen();
    QVERIFY( harness.hookInstalled );   // a real WH_MOUSE hook for this (GUI) thread
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );

    // The wait begins; the pointer goes away and back. These are the two moves the system hands the hook
    // while Qt's dispatcher removes them (PM_REMOVE) and defers them. The sampled position never changes.
    harness.inputExcluded = true;
    harness.readRetrievalWitness = false;
    viewer_idle::win_hook::witness().seed( QPoint( 500, 500 ) );
    MOUSEHOOKSTRUCT away = {};
    away.pt.x = 510;
    away.pt.y = 500;
    MOUSEHOOKSTRUCT back = {};
    back.pt.x = 500;
    back.pt.y = 500;
    viewer_idle::win_hook::proc( HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>( &away ) );
    viewer_idle::win_hook::proc( HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>( &back ) );
    QCOMPARE( QCursor::pos(), start );

    // r11: the control and the row are judged by the Show tick itself (harness.shows and what it recorded), never
    // by sampling the cursor shape: a Show lasts one idle period (40 ms), shorter than QTRY's 50 ms poll step, and
    // merge_group run 44634859056 missed it that way (1 in 50 locally).
    const int showsBeforeR1 = harness.shows;
    QTest::qWait( 300 );   // about seven idle periods of the r1 tick
    QCOMPARE( harness.shows, showsBeforeR1 );   // no Show tick at all, not only none sampled
    QCOMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
    QVERIFY( harness.hidden() );

    harness.readRetrievalWitness = true;   // the r2 tick
    QTRY_VERIFY_WITH_TIMEOUT( harness.shows > showsBeforeR1, 1000 );
    QCOMPARE( harness.shownViewportShape, Qt::OpenHandCursor );
    QVERIFY( harness.shownGlShape != Qt::BlankCursor );
    QVERIFY( !harness.shownHidden );

    // A synthesized move to the last position (Qt discards those too) does not bring it back again: the system
    // move history shows no point elsewhere since the hide (r6), so it stays a same-position move.
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    const int showsBeforeSame = harness.shows;
    viewer_idle::win_hook::proc( HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>( &back ) );
    QTest::qWait( 200 );
    QCOMPARE( harness.shows, showsBeforeSame );   // no Show tick at all, not only none sampled
    QCOMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );

    harness.inputExcluded = false;
    harness.leaveFullscreen();
    QVERIFY( !viewer_idle::win_hook::installed() );
}

namespace
{
// r6 (key B1): a scripted system move history, newest first, so a test controls what GetMouseMovePointsEx says.
viewer_idle::HistoryPoint g_viewerIdleScript[viewer_idle::MoveHistoryMark::kMaxPoints];
int g_viewerIdleScriptCount = 0;
int viewerIdleScriptedHistory( const QPoint &, viewer_idle::HistoryPoint *out, int max )
{
    if( g_viewerIdleScriptCount <= 0 ) return -1;
    const int n = qMin( max, g_viewerIdleScriptCount );
    for( int i = 0; i < n; ++i ) out[i] = g_viewerIdleScript[i];
    return n;
}
void viewerIdleSetScript( std::initializer_list<viewer_idle::HistoryPoint> points )
{
    g_viewerIdleScriptCount = 0;
    for( const viewer_idle::HistoryPoint &p : points ) g_viewerIdleScript[g_viewerIdleScriptCount++] = p;
}
// r12 (adversary pass 18 A1): a scripted system last-input time (GetLastInputInfo).
quint32 g_viewerIdleLastInput = 0;
bool viewerIdleScriptedLastInput( quint32 *tick )
{
    *tick = g_viewerIdleLastInput;
    return true;
}
viewer_idle::HistoryPoint viewerIdlePoint( int x, int y, quint32 time )
{
    viewer_idle::HistoryPoint p;
    p.x = x;
    p.y = y;
    p.time = time;
    return p;
}
} // namespace

// G10 (r6, key B1, the sol catalogue row): hidden fullscreen; the pointer goes away and back before the GUI thread
// takes its queue, and Windows coalesces that into ONE WM_MOUSEMOVE at the starting pixel. It reaches the hook proc
// and, as a same-position QMouseEvent, the event path. Expected: the cursor comes back on both viewer surfaces.
// The witness is the system move history, marked on entering fullscreen and at every tick (scripted here: the
// test must not move the real pointer). Control: the r5 tick, which marks nothing, keeps both hidden.
void GuiSmokeTest::viewerCursorAutohide_coalescedAwayAndBackRestoresOnBothSurfaces()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;

    viewer_idle::win_hook::HistoryReader saved = viewer_idle::win_hook::historyReader();
    viewer_idle::win_hook::historyReader() = viewerIdleScriptedHistory;
    ViewerIdleHarness harness( surfaces, 40 );
    harness.pollMs = 10;
    qApp->installEventFilter( &harness );
    auto cleanup = qScopeGuard( [&harness, saved]() {
        qApp->removeEventFilter( &harness );
        viewer_idle::win_hook::remove();
        viewer_idle::win_hook::historyReader() = saved;
    } );
    const QPoint start( 500, 500 );
    const QPoint away( 510, 500 );
    MOUSEHOOKSTRUCT back = {};
    back.pt.x = start.x();
    back.pt.y = start.y();

    for( const bool markOnHide : { false, true } )
    {
        harness.markHistory = markOnHide;
        viewerIdleSetScript( { viewerIdlePoint( start.x(), start.y(), 1000 ), viewerIdlePoint( 490, 500, 990 ) } );
        harness.enterFullscreen();
        QVERIFY( harness.hookInstalled );
        // The pointer rests at start: the event path and the hook both last saw it there.
        QMouseEvent rest = viewerIdleMove( start );
        QCoreApplication::sendEvent( &viewport, &rest );
        QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
        QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
        viewer_idle::win_hook::witness().seed( start );

        // Away and back, coalesced: the history has the excursion, the one message is at start.
        viewerIdleSetScript( { viewerIdlePoint( start.x(), start.y(), 1200 ), viewerIdlePoint( away.x(), away.y(), 1100 ),
                               viewerIdlePoint( start.x(), start.y(), 1000 ), viewerIdlePoint( 490, 500, 990 ) } );
        viewer_idle::win_hook::proc( HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>( &back ) );
        QMouseEvent same = viewerIdleMove( start );
        QCoreApplication::sendEvent( &viewport, &same );

        // r11: judged by the Show tick (shown for one idle period, 40 ms, only), never by sampling the shape.
        const int showsBefore = harness.shows;
        if( !markOnHide )
        {
            QTest::qWait( 300 );   // many poll periods of the r5 tick
            QCOMPARE( harness.shows, showsBefore );   // no Show tick at all, not only none sampled
            QCOMPARE( viewport.cursor().shape(), Qt::BlankCursor );
            QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
            QVERIFY( harness.hidden() );
        }
        else
        {
            QTRY_VERIFY_WITH_TIMEOUT( harness.shows > showsBefore, 1000 );
            QCOMPARE( harness.shownViewportShape, Qt::OpenHandCursor );
            QVERIFY( harness.shownGlShape != Qt::BlankCursor );
            QVERIFY( !harness.shownHidden );
        }
        harness.leaveFullscreen();
        QVERIFY( !viewer_idle::win_hook::installed() );
    }
}

// G11 (r6, key B1): the app's own hide does not bring the cursor straight back. Hiding sets the cursor and can make
// Windows synthesize a move at the unchanged position; the history then has no point elsewhere (at most one more at
// the same pixel), so the cursor stays hidden. Scripted, and then with the real system history and the real hook.
void GuiSmokeTest::viewerCursorAutohide_ownHideSameSpotMoveDoesNotReshow()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;

    viewer_idle::win_hook::HistoryReader saved = viewer_idle::win_hook::historyReader();
    viewer_idle::win_hook::historyReader() = viewerIdleScriptedHistory;
    ViewerIdleHarness harness( surfaces, 40 );
    harness.pollMs = 10;
    qApp->installEventFilter( &harness );
    auto cleanup = qScopeGuard( [&harness, saved]() {
        qApp->removeEventFilter( &harness );
        viewer_idle::win_hook::remove();
        viewer_idle::win_hook::historyReader() = saved;
    } );
    const QPoint start( 500, 500 );
    MOUSEHOOKSTRUCT same = {};
    same.pt.x = start.x();
    same.pt.y = start.y();

    viewerIdleSetScript( { viewerIdlePoint( start.x(), start.y(), 1000 ), viewerIdlePoint( 490, 500, 1000 ) } );
    harness.enterFullscreen();
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    viewer_idle::win_hook::witness().seed( start );
    QVERIFY( viewer_idle::win_hook::historyMark().marked() );
    // r11: no Show tick at all, not only none sampled (a Show lasts one idle period, 40 ms).
    const int showsBefore = harness.shows;
    // The provoked move, with and without a new point at the same pixel.
    viewer_idle::win_hook::proc( HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>( &same ) );
    viewerIdleSetScript( { viewerIdlePoint( start.x(), start.y(), 1016 ), viewerIdlePoint( start.x(), start.y(), 1000 ),
                           viewerIdlePoint( 490, 500, 1000 ) } );
    viewer_idle::win_hook::proc( HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>( &same ) );
    QTest::qWait( 300 );
    QCOMPARE( harness.shows, showsBefore );
    QCOMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
    QVERIFY( harness.hidden() );
    QVERIFY( viewer_idle::win_hook::historyMark().marked() );
    harness.leaveFullscreen();

    // The real history and hook: the hide's own cursor change does not re-show it.
    viewer_idle::win_hook::historyReader() = saved;
    harness.enterFullscreen();
    QVERIFY( harness.hookInstalled );
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    const int showsBeforeReal = harness.shows;
    QTest::qWait( 300 );
    QCOMPARE( harness.shows, showsBeforeReal );   // r11: no Show tick at all, not only none sampled
    QCOMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QVERIFY( harness.hidden() );
    harness.leaveFullscreen();
}

// G11b (r12, sol r11 B1): sol's periodic full history. P=(500,500,1000), Q=(510,500,1000), H=[P,Q] x 32 newest first;
// one more Q,P excursion leaves the identical list and ONE coalesced WM_MOUSEMOVE at P. Liveness first: with H in the
// history and no message the cursor hides and no Show tick follows. Then the excursion: a Show tick that restores both
// viewer surfaces (106a1819 judged the identical list as no movement and stayed hidden).
void GuiSmokeTest::viewerCursorAutohide_periodicFullHistoryExcursionRestoresAndQuietStaysHidden()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;

    viewer_idle::win_hook::HistoryReader saved = viewer_idle::win_hook::historyReader();
    viewer_idle::win_hook::historyReader() = viewerIdleScriptedHistory;
    // r12 (adversary pass 18 A1): a scripted last-input time, so the test controls whether input came after the mark.
    viewer_idle::win_hook::LastInputReader savedInput = viewer_idle::win_hook::lastInputReader();
    viewer_idle::win_hook::lastInputReader() = viewerIdleScriptedLastInput;
    g_viewerIdleLastInput = 5000;
    ViewerIdleHarness harness( surfaces, 40 );
    harness.pollMs = 10;
    harness.markHistory = true;
    qApp->installEventFilter( &harness );
    auto cleanup = qScopeGuard( [&harness, saved, savedInput]() {
        qApp->removeEventFilter( &harness );
        viewer_idle::win_hook::remove();
        viewer_idle::win_hook::historyReader() = saved;
        viewer_idle::win_hook::lastInputReader() = savedInput;
    } );
    const QPoint start( 500, 500 );
    MOUSEHOOKSTRUCT back = {};
    back.pt.x = start.x();
    back.pt.y = start.y();
    for( int i = 0; i < viewer_idle::MoveHistoryMark::kMaxPoints; ++i )
        g_viewerIdleScript[i] = ( i % 2 ) == 0 ? viewerIdlePoint( 500, 500, 1000 ) : viewerIdlePoint( 510, 500, 1000 );
    g_viewerIdleScriptCount = viewer_idle::MoveHistoryMark::kMaxPoints;

    harness.enterFullscreen();
    QVERIFY( harness.hookInstalled );
    QMouseEvent rest = viewerIdleMove( start );
    QCoreApplication::sendEvent( &viewport, &rest );
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
    viewer_idle::win_hook::witness().seed( start );

    // Liveness: H stays, no message: no Show tick at all over many poll periods. Then the move Windows synthesizes
    // after the app's own hide (adversary pass 18 A1: no input, the last-input time stays): still no Show tick.
    const int showsQuiet = harness.shows;
    QTest::qWait( 300 );
    viewer_idle::win_hook::proc( HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>( &back ) );
    QTest::qWait( 300 );
    QCOMPARE( harness.shows, showsQuiet );
    QVERIFY( harness.hidden() );

    // The excursion: identical list, one coalesced move at start, and it was input (the last-input time moved).
    const int showsBefore = harness.shows;
    ++g_viewerIdleLastInput;
    viewer_idle::win_hook::proc( HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>( &back ) );
    QTRY_VERIFY_WITH_TIMEOUT( harness.shows > showsBefore, 1000 );
    QCOMPARE( harness.shownViewportShape, Qt::OpenHandCursor );
    QVERIFY( harness.shownGlShape != Qt::BlankCursor );
    QVERIFY( !harness.shownHidden );
    harness.leaveFullscreen();
    QVERIFY( !viewer_idle::win_hook::installed() );
}

namespace
{
// r7 (key B1): a scripted cursor position and queue status, so a test controls what GetCursorPos and
// GetQueueStatus say without moving the real pointer or queuing real input.
QPoint g_viewerIdleCursor( 500, 500 );
DWORD g_viewerIdleQueueStatus = 0;
// The control's pointer keeps moving (one pixel per read), so its shown state lasts instead of re-hiding one idle
// period after the show.
bool g_viewerIdleCursorWalks = false;
bool viewerIdleScriptedCursor( QPoint *at )
{
    if( g_viewerIdleCursorWalks ) g_viewerIdleCursor.rx() += 1;
    *at = g_viewerIdleCursor;
    return true;
}
DWORD viewerIdleScriptedQueueStatus( UINT flags )
{
    return g_viewerIdleQueueStatus & ( ( DWORD( flags ) << 16 ) | flags );
}
} // namespace

// G12 (r7, key B1, the sol r6 catalogue row): hidden fullscreen, the pointer stationary at (500,500) with an unchanged
// move history and an empty witness. A nonactivating window shown under the cursor makes Windows synthesize a move at
// (500,500); it is still queued at the tick (GetQueueStatus(QS_MOUSE) = 0x00020002). Expected: both viewer surfaces
// stay hidden. RED control: the b03e08c5 tick (any queued mouse input counts) shows both. Control: a queued move with
// the cursor at another point (510,500) shows both.
void GuiSmokeTest::viewerCursorAutohide_queuedSynthesizedSamePositionMoveStaysHidden()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;

    viewer_idle::win_hook::HistoryReader savedHistory = viewer_idle::win_hook::historyReader();
    viewer_idle::win_hook::CursorReader savedCursor = viewer_idle::win_hook::cursorReader();
    viewer_idle::win_hook::QueueStatusReader savedQueue = viewer_idle::win_hook::queueStatusReader();
    viewer_idle::win_hook::historyReader() = viewerIdleScriptedHistory;
    viewer_idle::win_hook::cursorReader() = viewerIdleScriptedCursor;
    viewer_idle::win_hook::queueStatusReader() = viewerIdleScriptedQueueStatus;
    ViewerIdleHarness harness( surfaces, 40 );
    harness.pollMs = 10;
    harness.inputExcluded = true;
    qApp->installEventFilter( &harness );
    auto cleanup = qScopeGuard( [&harness, savedHistory, savedCursor, savedQueue]() {
        qApp->removeEventFilter( &harness );
        viewer_idle::win_hook::remove();
        viewer_idle::win_hook::historyReader() = savedHistory;
        viewer_idle::win_hook::cursorReader() = savedCursor;
        viewer_idle::win_hook::queueStatusReader() = savedQueue;
        g_viewerIdleQueueStatus = 0;
        g_viewerIdleCursorWalks = false;
        g_viewerIdleCursor = QPoint( 500, 500 );
    } );
    const QPoint start( 500, 500 );

    // 0: the b03e08c5 tick (RED control); 1: the r7 tick (the row); 2: the r7 tick, cursor elsewhere (control).
    for( int run = 0; run < 3; ++run )
    {
        harness.queueStatusTick = run == 0;
        g_viewerIdleQueueStatus = 0;
        g_viewerIdleCursor = start;
        viewerIdleSetScript( { viewerIdlePoint( start.x(), start.y(), 1000 ), viewerIdlePoint( 490, 500, 990 ) } );
        harness.enterFullscreen();
        QVERIFY( harness.hookInstalled );
        QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
        QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
        viewer_idle::win_hook::witness().seed( start );

        // The synthesized move is queued (QS_MOUSEMOVE in the high and low words); the history is unchanged.
        if( run == 2 )
        {
            g_viewerIdleCursor = QPoint( 510, 500 );
            g_viewerIdleCursorWalks = true;
            viewerIdleSetScript( { viewerIdlePoint( 510, 500, 1200 ), viewerIdlePoint( start.x(), start.y(), 1000 ),
                                   viewerIdlePoint( 490, 500, 990 ) } );
        }
        const int showsBefore = harness.shows;
        g_viewerIdleQueueStatus = 0x00020002;
        if( run == 1 )
        {
            QTest::qWait( 300 );   // many poll periods with the move still queued
            QCOMPARE( harness.shows, showsBefore );   // r11: no Show tick at all, not only none sampled
            QCOMPARE( viewport.cursor().shape(), Qt::BlankCursor );
            QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
            QVERIFY( harness.hidden() );
        }
        else
        {
            QTRY_COMPARE_WITH_TIMEOUT( viewport.cursor().shape(), Qt::OpenHandCursor, 1000 );
            QVERIFY( gl.cursor().shape() != Qt::BlankCursor );
            QVERIFY( !harness.hidden() );
            QVERIFY( harness.shows > 0 );
        }
        g_viewerIdleQueueStatus = 0;
        g_viewerIdleCursorWalks = false;
        harness.leaveFullscreen();
        QVERIFY( !viewer_idle::win_hook::installed() );
    }
}

// G13 (r7, adversary pass 11 A1): enter fullscreen, hidden after one idle period; then the window manager restores
// the window without the action (isFullScreen() false, the action still checked) and the pointer stays still.
// Expected: both surfaces are shown, the hook is removed and the idle timer stops. And a window not yet seen
// fullscreen (showFullScreen() pending) is given one idle period before the same exit.
void GuiSmokeTest::viewerCursorAutohide_fullscreenLeftWithoutTheActionStopsTheIdleMachinery()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;

    ViewerIdleHarness harness( surfaces, 40 );
    harness.pollMs = 10;
    harness.inputExcluded = true;
    auto cleanup = qScopeGuard( [&harness]() { harness.leaveFullscreen(); } );

    // r8 RED control: the 5426572e tick (no window-state check) on the same input keeps polling with the hook
    // installed. Proven by tick count: ten more ticks after the restore, and the timer and hook are still live.
    harness.windowStateTick = false;
    harness.enterFullscreen();
    QVERIFY( harness.hookInstalled );
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    harness.windowReallyFullscreen = false;
    const int redTicksAtRestore = harness.ticks;
    QTRY_VERIFY_WITH_TIMEOUT( harness.ticks >= redTicksAtRestore + 10, 5000 );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );   // the isFullScreen() gate shows it (not stranded)
    QVERIFY( harness.timerActive() );
    QVERIFY( viewer_idle::win_hook::installed() );
    harness.leaveFullscreen();
    QVERIFY( !viewer_idle::win_hook::installed() );
    harness.windowStateTick = true;
    harness.windowReallyFullscreen = true;

    harness.enterFullscreen();
    QVERIFY( harness.hookInstalled );
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );

    harness.windowReallyFullscreen = false;
    QTRY_VERIFY_WITH_TIMEOUT( !viewer_idle::win_hook::installed(), 1000 );
    QVERIFY( !harness.timerActive() );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
    QVERIFY( gl.cursor().shape() != Qt::BlankCursor );
    QVERIFY( !harness.hidden() );

    // Never seen fullscreen: polled for one idle period, then the same exit; it never hides meanwhile.
    harness.enterFullscreen();
    QVERIFY( harness.hookInstalled );
    QTRY_VERIFY_WITH_TIMEOUT( !viewer_idle::win_hook::installed(), 1000 );
    QVERIFY( !harness.timerActive() );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
    harness.windowReallyFullscreen = true;
}

// G14 (r10, sol r9 B1 / carried A1-pass12): sol's row, scaled to a 40 ms idle period: entered through the action,
// hidden after one idle period; the window manager restores the window (native exit, the action stays checked) and
// a tick follows; then the window is made fullscreen again natively, the action still checked, and the pointer stays
// still. Expected: re-entry restarts the idle machinery (hook, timer) and the cursor hides again one idle period
// later. The same holds for a second exit and re-entry (restore from minimize). A duplicate state change while
// fullscreen starts nothing; a window made fullscreen with the action unchecked starts nothing.
void GuiSmokeTest::viewerCursorAutohide_nativeFullscreenReentryRestartsTheIdleMachinery()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;

    ViewerIdleHarness harness( surfaces, 40 );
    harness.pollMs = 10;
    harness.inputExcluded = true;
    auto cleanup = qScopeGuard( [&harness]() { harness.leaveFullscreen(); } );

    harness.enterFullscreen();   // the action's toggled(true)
    QVERIFY( harness.hookInstalled );
    QTRY_COMPARE( viewport.cursor().shape(), Qt::BlankCursor );

    for( int cycle = 0; cycle < 2; ++cycle )
    {
        // Native exit, the action still checked: the state change and the next tick leave the idle machinery.
        harness.windowReallyFullscreen = false;
        harness.windowStateChanged();
        QTRY_VERIFY_WITH_TIMEOUT( !viewer_idle::win_hook::installed(), 1000 );
        QVERIFY( !harness.timerActive() );
        QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );

        // Native re-entry, the action still checked, the pointer still: hidden again after one idle period.
        const int entriesBefore = harness.entries;
        harness.windowReallyFullscreen = true;
        harness.windowStateChanged();
        QTRY_COMPARE_WITH_TIMEOUT( viewport.cursor().shape(), Qt::BlankCursor, 2000 );
        QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
        QCOMPARE( harness.entries, entriesBefore + 1 );
        QVERIFY( viewer_idle::win_hook::installed() );
        QVERIFY( harness.timerActive() );

        // A duplicate state change while really fullscreen starts nothing and leaves it hidden.
        harness.windowStateChanged();
        QCOMPARE( harness.entries, entriesBefore + 1 );
        QVERIFY( harness.hidden() );
    }

    // The action unchecked (its own toggled(false) already left): a window made fullscreen starts nothing.
    harness.leaveFullscreen();
    harness.actionChecked = false;
    harness.windowReallyFullscreen = false;
    harness.windowStateChanged();
    harness.windowReallyFullscreen = true;
    const int entriesUnchecked = harness.entries;
    harness.windowStateChanged();
    QCOMPARE( harness.entries, entriesUnchecked );
    QVERIFY( !harness.timerActive() );
    QVERIFY( !viewer_idle::win_hook::installed() );
    QCOMPARE( viewport.cursor().shape(), Qt::OpenHandCursor );
}
#endif

// G8 (Dot 0d82fed): a viewer surface replaced while the cursor is hidden. MainWindow::viewerIdleSurfaces() reads
// both surfaces fresh at every hide/restore; the viewport is replaced by QGraphicsView::setViewport (as
// GpuDisplayViewport does, which deletes the old one) and the GL window comes and goes. Restore must touch only
// the live surfaces, never a deleted one, and leave a new surface's own cursor state alone; the next hide
// blanks the new surfaces, and the restore after it gives the viewport back exactly what it had and unsets the
// GL window's cursor (CursorHider does not save a GL cursor: production never sets one on GpuDisplayWindow;
// r3 pass 5 advisory A2 pins that here rather than claim more).
void GuiSmokeTest::viewerCursorAutohide_surfaceReplacedWhileHiddenTouchesOnlyTheLiveSurfaces()
{
    QGraphicsView view;
    view.viewport()->setCursor( Qt::OpenHandCursor );
    QPointer<QWidget> oldViewport = view.viewport();
    auto gl1 = std::make_unique<QWindow>();
    viewer_idle::Surfaces before;
    before.viewport = view.viewport();
    before.glWindow = gl1.get();
    viewer_idle::CursorHider hider;
    hider.hide( before );
    QCOMPARE( view.viewport()->cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl1->cursor().shape(), Qt::BlankCursor );

    // Replaced while hidden.
    QWidget *fresh = new QWidget;
    view.setViewport( fresh );
    QVERIFY( oldViewport.isNull() );   // QAbstractScrollArea deleted the hidden viewport
    gl1.reset();
    QWindow gl2;
    gl2.setCursor( Qt::CrossCursor );   // the new GL window carries its own cursor
    viewer_idle::Surfaces after;
    after.viewport = view.viewport();
    after.glWindow = &gl2;
    QCOMPARE( after.viewport, fresh );
    const bool freshHadOwn = fresh->testAttribute( Qt::WA_SetCursor );
    const Qt::CursorShape freshShape = fresh->cursor().shape();
    QVERIFY( freshShape != Qt::BlankCursor );

    hider.restore( after );
    QCOMPARE( fresh->testAttribute( Qt::WA_SetCursor ), freshHadOwn );
    QCOMPARE( fresh->cursor().shape(), freshShape );
    QCOMPARE( gl2.cursor().shape(), Qt::CrossCursor );

    hider.hide( after );
    QCOMPARE( fresh->cursor().shape(), Qt::BlankCursor );
    QCOMPARE( gl2.cursor().shape(), Qt::BlankCursor );
    hider.restore( after );
    QCOMPARE( fresh->testAttribute( Qt::WA_SetCursor ), freshHadOwn );
    QCOMPARE( fresh->cursor().shape(), freshShape );
    QCOMPARE( gl2.cursor().shape(), Qt::ArrowCursor );   // unset: the default, not the CrossCursor it had
}

// G9 (r3, adversary r1 pass 4 A1): off Windows the position sample is the only witness in an input-excluded
// wait. With the poll cap the live timer samples every poll period, so a move away and back that is over long
// before one idle period ends, but spans at least one sample (80 ms away, 10 ms poll), still brings the cursor
// back on both surfaces; the cap does not stop the next hide. An excursion between two samples is the
// documented bound (P9), not exercised here.
// The harness's positionSampleOnly tick is the off-Windows tick (no hook, no queue status), so this runs here.
void GuiSmokeTest::viewerCursorAutohide_positionSampleAtThePollCapSeesABriefAwayAndBack()
{
    QWidget viewport;
    viewport.resize( 64, 48 );
    viewport.setCursor( Qt::OpenHandCursor );
    viewport.show();
    QWindow gl;
    viewer_idle::Surfaces surfaces;
    surfaces.viewport = &viewport;
    surfaces.glWindow = &gl;

    QScreen *screen = QGuiApplication::primaryScreen();
    QVERIFY( screen != nullptr );
    const QPoint start = screen->geometry().center();
    QCursor::setPos( start );
    ViewerIdleHarness harness( surfaces, 300 );
    harness.positionSampleOnly = true;
    harness.pollMs = 10;
    qApp->installEventFilter( &harness );
    auto cleanup = qScopeGuard( [&harness]() { qApp->removeEventFilter( &harness ); } );
    harness.enterFullscreen();
    QTRY_COMPARE_WITH_TIMEOUT( viewport.cursor().shape(), Qt::BlankCursor, 2000 );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );

    harness.inputExcluded = true;
    const int showsBefore = harness.shows;
    QCursor::setPos( start + QPoint( 10, 0 ) );
    QTest::qWait( 80 );   // away for well under one idle period (300 ms), several poll periods (10 ms)
    QCursor::setPos( start );
    // r11: judged by the Show tick, not by sampling the shape (the shown state lasts one idle period only).
    QTRY_VERIFY_WITH_TIMEOUT( harness.shows > showsBefore, 1000 );
    QCOMPARE( harness.shownViewportShape, Qt::OpenHandCursor );
    QVERIFY( harness.shownGlShape != Qt::BlankCursor );
    QVERIFY( !harness.shownHidden );

    // Still and excluded: it hides again after the idle period.
    QTRY_COMPARE_WITH_TIMEOUT( viewport.cursor().shape(), Qt::BlankCursor, 2000 );
    QCOMPARE( gl.cursor().shape(), Qt::BlankCursor );
    harness.inputExcluded = false;
    harness.leaveFullscreen();
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

namespace {
/* Reads an RGBA16 texture back through a framebuffer attachment (desktop GL accepts
 * GL_UNSIGNED_SHORT here). Empty on any framebuffer failure. */
QByteArray read_rgba16_texture(QOpenGLFunctions *gl, GLuint textureId, int width, int height)
{
    GLuint fbo = 0;
    gl->glGenFramebuffers(1, &fbo);
    gl->glBindFramebuffer(GL_FRAMEBUFFER, fbo);
    gl->glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, textureId, 0);
    QByteArray bytes;
    if (gl->glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE) {
        bytes.resize(width * height * 4 * static_cast<int>(sizeof(uint16_t)));
        gl->glPixelStorei(GL_PACK_ALIGNMENT, 1);
        gl->glReadPixels(0, 0, width, height, GL_RGBA, GL_UNSIGNED_SHORT, bytes.data());
    }
    gl->glBindFramebuffer(GL_FRAMEBUFFER, 0);
    gl->glDeleteFramebuffers(1, &fbo);
    return bytes;
}

QByteArray make_rgb16_blur_bytes(int width, int height, uint16_t seed)
{
    QByteArray bytes(width * height * 3 * static_cast<int>(sizeof(uint16_t)), Qt::Uninitialized);
    uint16_t *values = reinterpret_cast<uint16_t *>(bytes.data());
    for (int index = 0; index < width * height * 3; ++index) {
        values[index] = static_cast<uint16_t>(seed + index * 257);
    }
    return bytes;
}

QByteArray expected_rgba16(const QByteArray &rgb16, int pixelCount)
{
    QByteArray rgba(pixelCount * 4 * static_cast<int>(sizeof(uint16_t)), Qt::Uninitialized);
    const uint16_t *source = reinterpret_cast<const uint16_t *>(rgb16.constData());
    uint16_t *dest = reinterpret_cast<uint16_t *>(rgba.data());
    for (int pixel = 0; pixel < pixelCount; ++pixel) {
        dest[pixel * 4 + 0] = source[pixel * 3 + 0];
        dest[pixel * 4 + 1] = source[pixel * 3 + 1];
        dest[pixel * 4 + 2] = source[pixel * 3 + 2];
        dest[pixel * 4 + 3] = 65535;
    }
    return rgba;
}
} // namespace

void GuiSmokeTest::gpuPreviewProcessingLutSetReportsRebuildsAndBlurUploadsKeepTheirBytes()
{
    // PLAYBACK-GL-PRESENT-SETUP-STALL-1 (T2): against a live GL context, (1) the LUT set's
    // setup out-param reports a rebuild only when the key moves, and names the group that
    // moved; (2) the per-frame shadows/highlights blur texture holds exactly the uploaded
    // bytes on BOTH the allocation upload (drained + checked) and the steady-state upload
    // (row R3: no glGetError), so dropping the glGetError pair cannot change a pixel.
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
    QOpenGLFunctions *gl = context.functions();

    GpuPreviewProcessingLutTextureSet set;
    const GpuPreviewProcessingConfig config = make_synthetic_preview_processing_config();

    GpuPresentSetupTiming first;
    gpuPreviewProcessingUpdateLutTextureSet(set, config, &first);
    QVERIFY(gpuPreviewProcessingLutTextureSetReady(set, config));
    QVERIFY(first.lut_rebuilt);
    QCOMPARE(QString::fromLatin1(first.lut_miss), QStringLiteral("unbuilt"));

    // An equal config: no rebuild, and the out-param says so.
    const GpuPreviewProcessingConfig same = config;
    GpuPresentSetupTiming second;
    gpuPreviewProcessingUpdateLutTextureSet(set, same, &second);
    QVERIFY(!second.lut_rebuilt);
    QCOMPARE(QString::fromLatin1(second.lut_miss), QStringLiteral("none"));
    QVERIFY(gpuPreviewProcessingLutTextureSetReady(set, same));

    // A texture-content change rebuilds once and names its group; the next equal call does not.
    GpuPreviewProcessingConfig moved = config;
    moved.levelsLut = make_scaled_lut_bytes(0.90f);
    moved.signature = config.signature + 1;
    moved.lutGroupDigests[GpuPreviewLutGroupLevels] = config.lutGroupDigests[GpuPreviewLutGroupLevels] + 1;
    GpuPresentSetupTiming third;
    gpuPreviewProcessingUpdateLutTextureSet(set, moved, &third);
    QVERIFY(third.lut_rebuilt);
    QCOMPARE(QString::fromLatin1(third.lut_miss), QStringLiteral("levels"));
    QVERIFY(gpuPreviewProcessingLutTextureSetReady(set, moved));
    GpuPresentSetupTiming fourth;
    gpuPreviewProcessingUpdateLutTextureSet(set, moved, &fourth);
    QVERIFY(!fourth.lut_rebuilt);

    // The blur: an allocation upload, then two steady-state uploads of new content.
    const int width = 16;
    const int height = 8;
    GpuPreviewProcessingConfig blurConfig = moved;
    blurConfig.applyShadowsHighlights = true;
    blurConfig.shadowsHighlightsFrameStateReady = true;
    blurConfig.shadowsHighlightsFrameWidth = width;
    blurConfig.shadowsHighlightsFrameHeight = height;
    // gpuPreviewProcessingHasShadowsHighlightsFrameState also requires the full S/H curve.
    blurConfig.shadowsHighlightsCurve = QByteArray(static_cast<int>(65536u * sizeof(float)), '\0');
    const uint16_t seeds[3] = { 11, 4099, 30001 };
    GLuint allocatedId = 0;
    for (int round = 0; round < 3; ++round) {
        blurConfig.shadowsHighlightsBlur = make_rgb16_blur_bytes(width, height, seeds[round]);
        GpuPresentSetupTiming timing;
        QVERIFY(gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture(set, blurConfig, width, height, &timing));
        QVERIFY(set.shadowsHighlightsBlurReady);
        QVERIFY(set.shadowsHighlightsBlur != nullptr);
        if (round == 0) {
            allocatedId = set.shadowsHighlightsBlur->textureId();
        } else {
            // Steady state: the same texture, no drain, no check.
            QCOMPARE(set.shadowsHighlightsBlur->textureId(), allocatedId);
            QCOMPARE(timing.blur_drain_ms, 0.0);
            QCOMPARE(timing.blur_check_ms, 0.0);
        }
        const QByteArray readback = read_rgba16_texture(gl, set.shadowsHighlightsBlur->textureId(), width, height);
        if (readback.isEmpty()) {
            MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("RGBA16 framebuffer attachment readback is not supported here");
        }
        QCOMPARE(readback, expected_rgba16(blurConfig.shadowsHighlightsBlur, width * height));
    }
    QCOMPARE(gl->glGetError(), static_cast<GLenum>(GL_NO_ERROR));

    gpuPreviewProcessingDestroyLutTextureSet(set);
    context.doneCurrent();
}

void GuiSmokeTest::gpuPreviewProcessingShBlurUploadModeParsesOnlyItsThreeValues()
{
    // PLAYBACK-GPU-EPISODIC-STALL-1 r2: MLVAPP_GPU_SH_BLUR_UPLOAD_MODE. Unset, empty and
    // unknown values keep the pre-r2 client upload; only the three names select a mode.
    QCOMPARE(QString::fromLatin1(gpuPreviewProcessingShBlurUploadModeSwitchName()),
             QStringLiteral("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE"));
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QString()), GpuShBlurUploadClient);
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QStringLiteral("")), GpuShBlurUploadClient);
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QStringLiteral("client")), GpuShBlurUploadClient);
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QStringLiteral("skip")), GpuShBlurUploadSkip);
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QStringLiteral("pbo")), GpuShBlurUploadPbo);
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QStringLiteral(" PBO ")), GpuShBlurUploadPbo);
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QStringLiteral("Skip")), GpuShBlurUploadSkip);
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QStringLiteral("1")), GpuShBlurUploadClient);
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QStringLiteral("pbo2")), GpuShBlurUploadClient);
    QCOMPARE(gpuPreviewProcessingShBlurUploadModeFromValue(QStringLiteral("skipx")), GpuShBlurUploadClient);
    QCOMPARE(QString::fromLatin1(gpuPreviewProcessingShBlurUploadModeName(GpuShBlurUploadClient)), QStringLiteral("client"));
    QCOMPARE(QString::fromLatin1(gpuPreviewProcessingShBlurUploadModeName(GpuShBlurUploadSkip)), QStringLiteral("skip"));
    QCOMPARE(QString::fromLatin1(gpuPreviewProcessingShBlurUploadModeName(GpuShBlurUploadPbo)), QStringLiteral("pbo"));

    // The production reader is the environment, read on every call.
    const QByteArray saved = qgetenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE");
    const bool hadSaved = qEnvironmentVariableIsSet("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE");
    auto restore = qScopeGuard([&] {
        if (hadSaved) qputenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE", saved);
        else qunsetenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE");
    });
    qunsetenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE");
    QCOMPARE(gpuPreviewProcessingShBlurUploadMode(), GpuShBlurUploadClient);
    qputenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE", "pbo");
    QCOMPARE(gpuPreviewProcessingShBlurUploadMode(), GpuShBlurUploadPbo);
    qputenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE", "skip");
    QCOMPARE(gpuPreviewProcessingShBlurUploadMode(), GpuShBlurUploadSkip);
}

void GuiSmokeTest::gpuPreviewProcessingShBlurPboUploadKeepsBytesAndSkipKeepsAllocation()
{
    // PLAYBACK-GPU-EPISODIC-STALL-1 r2, against a live GL context: (1) in pbo mode every
    // steady-state upload goes through the set's pixel unpack buffer and the texture holds
    // exactly the bytes the client upload would have put there (pixel-identical), with no
    // buffer left bound; (2) in skip mode a steady call uploads nothing, so the texture
    // keeps the allocation-time blur; (3) destroying the set releases the buffer.
    MLV_SKIP_OR_FAIL_IF_OFFSCREEN("GPU blur PBO upload needs a platform plugin that can create an OpenGL context");

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
    if (context.format().version() < qMakePair(3, 0) && !context.hasExtension(QByteArrayLiteral("GL_ARB_map_buffer_range"))) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("this GL context cannot map a buffer range (the PBO path falls back to client)");
    }
    QOpenGLFunctions *gl = context.functions();

    const QByteArray saved = qgetenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE");
    const bool hadSaved = qEnvironmentVariableIsSet("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE");
    auto restore = qScopeGuard([&] {
        if (hadSaved) qputenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE", saved);
        else qunsetenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE");
    });

    GpuPreviewProcessingLutTextureSet set;
    GpuPreviewProcessingConfig blurConfig = make_synthetic_preview_processing_config();
    gpuPreviewProcessingUpdateLutTextureSet(set, blurConfig);
    QVERIFY(gpuPreviewProcessingLutTextureSetReady(set, blurConfig));
    const int width = 16;
    const int height = 8;
    blurConfig.applyShadowsHighlights = true;
    blurConfig.shadowsHighlightsFrameStateReady = true;
    blurConfig.shadowsHighlightsFrameWidth = width;
    blurConfig.shadowsHighlightsFrameHeight = height;
    blurConfig.shadowsHighlightsCurve = QByteArray(static_cast<int>(65536u * sizeof(float)), '\0');

    qputenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE", "pbo");
    const uint16_t seeds[3] = { 7, 2053, 40009 };
    GLuint allocatedId = 0;
    for (int round = 0; round < 3; ++round) {
        blurConfig.shadowsHighlightsBlur = make_rgb16_blur_bytes(width, height, seeds[round]);
        GpuPresentSetupTiming timing;
        QVERIFY(gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture(set, blurConfig, width, height, &timing));
        QVERIFY(set.shadowsHighlightsBlurReady);
        if (round == 0) {
            // The allocation upload is never routed through the buffer.
            allocatedId = set.shadowsHighlightsBlur->textureId();
            QCOMPARE(set.shadowsHighlightsBlurPbo, 0u);
        } else {
            QCOMPARE(set.shadowsHighlightsBlur->textureId(), allocatedId);
            QVERIFY(set.shadowsHighlightsBlurPbo != 0u);
            GLint boundUnpack = -1;
            gl->glGetIntegerv(0x88EF /* GL_PIXEL_UNPACK_BUFFER_BINDING */, &boundUnpack);
            QCOMPARE(boundUnpack, 0);
        }
        const QByteArray readback = read_rgba16_texture(gl, set.shadowsHighlightsBlur->textureId(), width, height);
        if (readback.isEmpty()) {
            MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("RGBA16 framebuffer attachment readback is not supported here");
        }
        QCOMPARE(readback, expected_rgba16(blurConfig.shadowsHighlightsBlur, width * height));
    }
    const QByteArray lastUploaded = blurConfig.shadowsHighlightsBlur;

    // skip: a steady call reports ready but leaves the texture as it was.
    qputenv("MLVAPP_GPU_SH_BLUR_UPLOAD_MODE", "skip");
    blurConfig.shadowsHighlightsBlur = make_rgb16_blur_bytes(width, height, 12345);
    QVERIFY(gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture(set, blurConfig, width, height));
    QVERIFY(set.shadowsHighlightsBlurReady);
    QCOMPARE(read_rgba16_texture(gl, set.shadowsHighlightsBlur->textureId(), width, height),
             expected_rgba16(lastUploaded, width * height));
    QCOMPARE(gl->glGetError(), static_cast<GLenum>(GL_NO_ERROR));

    gpuPreviewProcessingDestroyLutTextureSet(set);
    QCOMPARE(set.shadowsHighlightsBlurPbo, 0u);
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

// PLAYER-GL-WINDOW-INPUT-PARITY-1: the GL display window's viewer-input forwarder, driven on a
// plain QWindow (never shown) with hooks that record their calls.
namespace
{

struct GlWindowInputRecorder
{
    int wbCalls = 0;
    int bpCalls = 0;
    QPoint lastPick;
    int menuCalls = 0;
    QPoint lastMenuGlobal;
    int dragEnterCalls = 0;
    int dropCalls = 0;
    QStringList droppedPaths;
    bool wbArmed = false;
    bool bpArmed = false;
};

// Sees only the mouse-button events the forwarder did not consume (filters run newest first, so
// this one, installed before the forwarder, runs after it). Other events (ChildAdded when the
// forwarder is parented to the window) are not counted.
class PassThroughSpy : public QObject
{
public:
    int seen = 0;
    bool eventFilter(QObject *, QEvent *event) override
    {
        if (event->type() == QEvent::MouseButtonPress || event->type() == QEvent::MouseButtonRelease) ++seen;
        return false;
    }
};

gpu_window_input::Forwarder *installRecordingForwarder(QWindow *window, GlWindowInputRecorder *rec)
{
    gpu_window_input::Forwarder::Hooks hooks;
    // A 1920x804 clip in fit mode with a scene of the same size: the slots' conversion is the identity.
    hooks.pickBasis = []() {
        gpu_window_input::PickBasis basis;
        basis.zoomFit = true;
        basis.sceneRect = QSizeF(1920, 804);
        basis.rawWidth = 1920;
        basis.rawHeight = 804;
        return basis;
    };
    // As if paintGL last drew the scene-sized image letterboxed into the window.
    hooks.drawnPicture = [window]() { return gpu_window_input::letterboxRect(QSizeF(window->size()), QSizeF(1920, 804)); };
    hooks.wbArmed = [rec]() { return rec->wbArmed; };
    hooks.bpArmed = [rec]() { return rec->bpArmed; };
    hooks.pickWhiteBalance = [rec](int x, int y) { ++rec->wbCalls; rec->lastPick = QPoint(x, y); };
    hooks.pickBadPixel = [rec](int x, int y) { ++rec->bpCalls; rec->lastPick = QPoint(x, y); };
    hooks.contextMenuAtGlobal = [rec](const QPoint & global) { ++rec->menuCalls; rec->lastMenuGlobal = global; };
    hooks.dragEnter = [rec](QDragEnterEvent * e) {
        ++rec->dragEnterCalls;
        if (e->mimeData()->hasUrls()) e->acceptProposedAction();
    };
    hooks.openFiles = [rec](const QStringList & paths) { ++rec->dropCalls; rec->droppedPaths = paths; };
    auto *forwarder = new gpu_window_input::Forwarder(window, std::move(hooks));
    window->installEventFilter(forwarder);
    return forwarder;
}

void deliverMouse(QObject *receiver, QEvent::Type type, Qt::MouseButton button, const QPointF & local, const QPointF & global)
{
    QMouseEvent event(type, local, global, button, type == QEvent::MouseButtonRelease ? Qt::NoButton : Qt::MouseButtons(button), Qt::NoModifier);
    QCoreApplication::sendEvent(receiver, &event);
}

} // namespace

void GuiSmokeTest::glWindowViewerInput_picksMapToSceneAndRespectArming()
{
    GlWindowInputRecorder rec;
    QWindow window;
    window.resize(1920, 1080);
    QCOMPARE(window.size(), QSize(1920, 1080));
    PassThroughSpy spy;
    window.installEventFilter(&spy);
    installRecordingForwarder(&window, &rec);

    // White balance armed: the window centre is the scene centre (1920x804 letterboxed into 1920x1080).
    rec.wbArmed = true;
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(960, 540), QPointF(960, 540));
    QCOMPARE(rec.wbCalls, 1);
    QCOMPARE(rec.bpCalls, 0);
    QCOMPARE(rec.lastPick, QPoint(960, 402));
    QCOMPARE(spy.seen, 0); // consumed: the filter returned true

    // A press on the top black bar calls nothing (and is still consumed).
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(960, 20), QPointF(960, 20));
    QCOMPARE(rec.wbCalls, 1);
    QCOMPARE(rec.bpCalls, 0);
    QCOMPARE(spy.seen, 0);

    // Bad-pixel armed only: the bad-pixel hook instead.
    rec.wbArmed = false;
    rec.bpArmed = true;
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(0.5, 138.5), QPointF(0.5, 138.5));
    QCOMPARE(rec.wbCalls, 1);
    QCOMPARE(rec.bpCalls, 1);
    QCOMPARE(rec.lastPick, QPoint(0, 0));
    QCOMPARE(spy.seen, 0);

    // Neither armed: nothing is called and the event passes through (the filter returned false).
    rec.bpArmed = false;
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(960, 540), QPointF(960, 540));
    QCOMPARE(rec.wbCalls, 1);
    QCOMPARE(rec.bpCalls, 1);
    QCOMPARE(spy.seen, 1);
}

void GuiSmokeTest::glWindowViewerInput_contextMenuOnPlatformTriggerOnly()
{
    GlWindowInputRecorder rec;
    QWindow window;
    window.resize(1920, 1080);
    PassThroughSpy spy;
    window.installEventFilter(&spy);
    installRecordingForwarder(&window, &rec);
    rec.wbArmed = true; // the menu does not depend on arming

    const bool onRelease = gpu_window_input::contextMenuOnReleaseDefault();
    const QEvent::Type trigger = onRelease ? QEvent::MouseButtonRelease : QEvent::MouseButtonPress;
    const QEvent::Type other = onRelease ? QEvent::MouseButtonPress : QEvent::MouseButtonRelease;

    deliverMouse(&window, other, Qt::RightButton, QPointF(100, 200), QPointF(1234, 567));
    QCOMPARE(rec.menuCalls, 0);
    QCOMPARE(rec.wbCalls, 0);

    deliverMouse(&window, trigger, Qt::RightButton, QPointF(100, 200), QPointF(1234, 567));
    QCOMPARE(rec.menuCalls, 1);
    QCOMPARE(rec.lastMenuGlobal, QPoint(1234, 567));
    QCOMPARE(rec.wbCalls, 0);
    QCOMPARE(spy.seen, 1); // only the non-trigger event passed through
}

void GuiSmokeTest::glWindowViewerInput_dragAndDropReachTheHooks()
{
    GlWindowInputRecorder rec;
    QWindow window;
    window.resize(1920, 1080);
    installRecordingForwarder(&window, &rec);

    QMimeData mime;
    const QList<QUrl> urls = { QUrl::fromLocalFile(QStringLiteral("C:/drop-test/first.dat")),
                               QUrl::fromLocalFile(QStringLiteral("C:/drop-test/second.dat")) };
    mime.setUrls(urls);

    QDragEnterEvent enter(QPoint(100, 100), Qt::CopyAction, &mime, Qt::LeftButton, Qt::NoModifier);
    QCoreApplication::sendEvent(&window, &enter);
    QCOMPARE(rec.dragEnterCalls, 1);
    QVERIFY(enter.isAccepted());

    QDragMoveEvent move(QPoint(120, 110), Qt::CopyAction, &mime, Qt::LeftButton, Qt::NoModifier);
    move.ignore();
    QCoreApplication::sendEvent(&window, &move);
    QVERIFY(move.isAccepted());
    QCOMPARE(rec.dragEnterCalls, 1);

    // Every local path reaches the hook, in order (the scene's filesDropped list), and the drop
    // is accepted; a non-file URL is skipped as the scene skips it.
    QMimeData mixed;
    mixed.setUrls({ urls.at(0), QUrl(QStringLiteral("https://example.invalid/x")), urls.at(1) });
    QDropEvent drop(QPointF(120, 110), Qt::CopyAction, &mixed, Qt::LeftButton, Qt::NoModifier);
    drop.ignore();
    QCoreApplication::sendEvent(&window, &drop);
    QCOMPARE(rec.dropCalls, 1);
    QCOMPARE(rec.droppedPaths, QStringList({ urls.at(0).toLocalFile(), urls.at(1).toLocalFile() }));
    QVERIFY(drop.isAccepted());
}

void GuiSmokeTest::glWindowViewerInput_otherObjectsAreIgnored()
{
    GlWindowInputRecorder rec;
    QWindow window;
    window.resize(1920, 1080);
    gpu_window_input::Forwarder *forwarder = installRecordingForwarder(&window, &rec);
    rec.wbArmed = true;

    QWindow otherWindow;
    otherWindow.resize(1920, 1080);
    otherWindow.installEventFilter(forwarder);
    PassThroughSpy spy;
    otherWindow.installEventFilter(&spy); // newest: runs first, so it sees every event
    deliverMouse(&otherWindow, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(960, 540), QPointF(960, 540));
    QCOMPARE(spy.seen, 1);
    QCOMPARE(rec.wbCalls, 0);
    QCOMPARE(rec.menuCalls, 0);
    // The direct answer for a foreign watched object is false.
    QMouseEvent press(QEvent::MouseButtonPress, QPointF(960, 540), QPointF(960, 540), Qt::LeftButton, Qt::LeftButton, Qt::NoModifier);
    QVERIFY(!forwarder->eventFilter(&otherWindow, &press));
    QCOMPARE(rec.wbCalls, 0);
    otherWindow.removeEventFilter(forwarder);
}

void GuiSmokeTest::glWindowViewerInput_hiddenViewMapFromGlobalIsInverseOfMapToGlobal()
{
    // The context-menu hook passes ui->graphicsView->mapFromGlobal( global ) to a slot that maps
    // it back with mapToGlobal while the view is hidden behind the GL window container.
    QWidget parent;
    parent.resize(800, 600);
    parent.move(50, 70);
    QWidget *child = new QWidget(&parent);
    child->setGeometry(30, 40, 400, 300);
    child->hide();
    const QPoint global(1234, 567);
    QCOMPARE(child->mapToGlobal(child->mapFromGlobal(global)), global);
    parent.show();
    QCOMPARE(child->mapToGlobal(child->mapFromGlobal(global)), global);
    parent.hide();
}

void GuiSmokeTest::glWindowViewerInput_edgePicksStayInsideTheImage()
{
    // Adversary A2 (r1): the bottom and right picture edges map to scene y == height and
    // x == width, one past the last pixel; the slots' "x > width" guard lets that through.
    GlWindowInputRecorder rec;
    QWindow window;
    window.resize(1920, 1080);
    installRecordingForwarder(&window, &rec); // scene 1920x804, letterboxed to y 138..942
    rec.bpArmed = true;
    // The edge itself is one past the picture (half-open): no pick, so never row 804.
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(960, 942), QPointF(960, 942));
    QCOMPARE(rec.bpCalls, 0);
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(960, 941.9), QPointF(960, 941.9));
    QCOMPARE(rec.bpCalls, 1);
    QCOMPARE(rec.lastPick, QPoint(960, 803));
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(1920, 540), QPointF(1920, 540));
    QCOMPARE(rec.bpCalls, 1);
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(1919.9, 540), QPointF(1919.9, 540));
    QCOMPARE(rec.bpCalls, 2);
    QCOMPARE(rec.lastPick, QPoint(1919, 402));
    rec.bpArmed = false;
    rec.wbArmed = true;
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(1919.9, 941.9), QPointF(1919.9, 941.9));
    QCOMPARE(rec.wbCalls, 1);
    QCOMPARE(rec.lastPick, QPoint(1919, 803));
    // Inside the bottom bar: nothing.
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(960, 944), QPointF(960, 944));
    QCOMPARE(rec.wbCalls, 1);
}

void GuiSmokeTest::glWindowViewerInput_roundedSceneEdgeStillPicks()
{
    // Adversary A3 (r1): the fit-mode scene is an integer size (1000x418 for a 1920x804 clip in a
    // 1000x1000 window), so its aspect differs from the display aspect paintGL letterboxes with by
    // up to one scene pixel. paintGL's picture starts at y = 290.625; the scene-aspect rect at 291.
    // A click on the displayed picture between the two must still pick (the top row).
    // r2 (adversary r1 pass 3): only the rect paintGL drew decides; the scene's aspect does not.
    GlWindowInputRecorder rec;
    QWindow window;
    window.resize(1000, 1000);
    QSizeF scene(1000, 418);
    QSize raw(1000, 418); // the slots' conversion is the identity until the A3 part below
    QRectF drawn = gpu_window_input::letterboxRect(QSizeF(1000, 1000), QSizeF(1920, 804)); // texture route: from y 290.625
    gpu_window_input::Forwarder::Hooks hooks;
    hooks.pickBasis = [&scene, &raw]() {
        gpu_window_input::PickBasis basis;
        basis.zoomFit = true;
        basis.sceneRect = scene;
        basis.rawWidth = raw.width();
        basis.rawHeight = raw.height();
        return basis;
    };
    hooks.drawnPicture = [&drawn]() { return drawn; };
    hooks.wbArmed = [&rec]() { return rec.wbArmed; };
    hooks.bpArmed = [&rec]() { return rec.bpArmed; };
    hooks.pickWhiteBalance = [&rec](int x, int y) { ++rec.wbCalls; rec.lastPick = QPoint(x, y); };
    hooks.pickBadPixel = [&rec](int x, int y) { ++rec.bpCalls; rec.lastPick = QPoint(x, y); };
    window.installEventFilter(new gpu_window_input::Forwarder(&window, std::move(hooks)));
    rec.wbArmed = true;
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(500, 290.75), QPointF(500, 290.75));
    QCOMPARE(rec.wbCalls, 1);
    QCOMPARE(rec.lastPick, QPoint(500, 0));
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(500, 290.5), QPointF(500, 290.5));
    QCOMPARE(rec.wbCalls, 1);
    // A1: the image route drew the scene itself (from y 291): the same click is now on its bar.
    drawn = gpu_window_input::letterboxRect(QSizeF(1000, 1000), scene);
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(500, 290.75), QPointF(500, 290.75));
    QCOMPARE(rec.wbCalls, 1);

    // A3: a 1920x420 clip at stretchY 3 drawn as 1920x1260 (from y 171.875) with a 1000x654 scene;
    // whatever the stretch controls say next, until paintGL draws again the drawn rect decides.
    // The clip is 1920x420 in fit mode with a 1000x654 scene: the slots turn the handed integers into
    // raw pixels with int( c * 1920 / 1000 ), int( c * 420 / 654 ); the raw pixel shown must come out.
    scene = QSizeF(1000, 654);
    raw = QSize(1920, 420);
    drawn = gpu_window_input::letterboxRect(QSizeF(1000, 1000), QSizeF(1920, 1260));
    const auto slotRawY = [](int c) { return static_cast<int>(c * (420 / 654.0)); };
    rec.wbArmed = false;
    rec.bpArmed = true;
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(500, 172), QPointF(500, 172));
    QCOMPARE(rec.bpCalls, 1);
    QCOMPARE(rec.lastPick.x(), 500);
    QCOMPARE(slotRawY(rec.lastPick.y()), 0);
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(500, 200.5), QPointF(500, 200.5));
    QCOMPARE(rec.bpCalls, 2);
    QCOMPARE(slotRawY(rec.lastPick.y()), 18);   // (200.5 - 171.875) / 656.25 * 420 = 18.32
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(500, 171.8), QPointF(500, 171.8));
    QCOMPARE(rec.bpCalls, 2);
}

void GuiSmokeTest::glWindowViewerInput_noDrawnPictureIsNoPick()
{
    // r2: before paintGL has drawn a frame the getter reports none, and an armed pick is refused
    // (still consumed, so it does not reach the view's own handling), never guessed.
    GlWindowInputRecorder rec;
    QWindow window;
    window.resize(1000, 1000);
    PassThroughSpy spy;
    window.installEventFilter(&spy);
    const auto usableBasis = []() {
        gpu_window_input::PickBasis basis;
        basis.zoomFit = true;
        basis.sceneRect = QSizeF(1000, 654);
        basis.rawWidth = 1000;
        basis.rawHeight = 654;
        return basis;
    };
    gpu_window_input::Forwarder::Hooks hooks;
    hooks.pickBasis = usableBasis;
    hooks.drawnPicture = []() { return QRectF(); };
    hooks.wbArmed = [&rec]() { return rec.wbArmed; };
    hooks.bpArmed = [&rec]() { return rec.bpArmed; };
    hooks.pickWhiteBalance = [&rec](int x, int y) { ++rec.wbCalls; rec.lastPick = QPoint(x, y); };
    hooks.pickBadPixel = [&rec](int x, int y) { ++rec.bpCalls; rec.lastPick = QPoint(x, y); };
    window.installEventFilter(new gpu_window_input::Forwarder(&window, std::move(hooks)));
    rec.wbArmed = true;
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(500, 500), QPointF(500, 500));
    rec.wbArmed = false;
    rec.bpArmed = true;
    deliverMouse(&window, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(500, 500), QPointF(500, 500));
    QCOMPARE(rec.wbCalls, 0);
    QCOMPARE(rec.bpCalls, 0);
    QCOMPARE(spy.seen, 0);

    // An unset hook is the same: no pick.
    GlWindowInputRecorder rec2;
    QWindow window2;
    window2.resize(1000, 1000);
    gpu_window_input::Forwarder::Hooks bare;
    bare.pickBasis = usableBasis;
    bare.wbArmed = []() { return true; };
    bare.pickWhiteBalance = [&rec2](int, int) { ++rec2.wbCalls; };
    window2.installEventFilter(new gpu_window_input::Forwarder(&window2, std::move(bare)));
    deliverMouse(&window2, QEvent::MouseButtonPress, Qt::LeftButton, QPointF(500, 500), QPointF(500, 500));
    QCOMPARE(rec2.wbCalls, 0);

    // The real getter on a GL window that never painted reports none.
    GpuDisplayWindow glWindow;
    QVERIFY(!glWindow.lastPaintedPicture().valid);
    const GpuDisplayWindow::PaintedPicture p = glWindow.lastPaintedPicture();
    QVERIFY(gpu_window_input::drawnPictureRect(p.valid, p.framebufferWidth, p.framebufferHeight,
                                               p.devicePixelRatio, p.scaleX, p.scaleY).isEmpty());
}

void GuiSmokeTest::glWindowViewerInput_getterRefusesWhileAFrameIsHandedOver()
{
    // Adversary r2 pass 1 A2: a painted clear, then a hand-over that restores a texture without
    // painting, must not bring the pre-clear picture back. The getter reports a picture only while
    // the last paintGL drew one and nothing has been handed over since. Offscreen: the paint is
    // stood in for by the state paintGL leaves (friend access); the hand-overs are the real calls.
    GpuDisplayWindow win;
    QOpenGLTexture texture(QOpenGLTexture::Target2D); // never created: no GL call
    const GpuDisplayWindow::PaintedPicture painted{ true, 2000, 2000, 2.0, 1.0f, 0.418f };
    const auto standInForADrawingPaint = [&]() {
        win.m_lastPaintedPicture = painted;
        win.m_texture = &texture;
        win.m_texturePresentationActive = true;
    };
    standInForADrawingPaint();
    QVERIFY(win.lastPaintedPicture().valid);
    QCOMPARE(win.lastPaintedPicture().scaleY, 0.418f);

    // A frame handed over and not painted yet: none (the picture on screen is about to change).
    QImage frame(16, 8, QImage::Format_RGB888);
    frame.fill(Qt::gray);
    win.setPresentedImage(frame, QSize(1920, 804));
    QVERIFY(!win.lastPaintedPicture().valid);

    // A clear: none.
    standInForADrawingPaint();
    win.clearPresented();
    QVERIFY(!win.lastPaintedPicture().valid);

    // A paint that drew nothing (paintGL's no-texture branch) leaves the flag false; a texture that
    // comes back without a paint (recon adoption at submit) does not revive the old picture.
    standInForADrawingPaint();
    win.m_texturePresentationActive = false;
    QVERIFY(!win.lastPaintedPicture().valid);

    // No texture at all: none.
    standInForADrawingPaint();
    win.m_texture = nullptr;
    QVERIFY(!win.lastPaintedPicture().valid);
    win.m_texture = nullptr;
}

void GuiSmokeTest::glWindowViewerInput_getterRecordsThePaintedPicture()
{
    MLV_SKIP_OR_FAIL_IF_OFFSCREEN("the GL window's paintGL needs a platform plugin that can create an OpenGL context");

    qputenv(GpuDisplayWindow::environmentVariableName(), QByteArrayLiteral("1"));
    auto host = std::make_unique<QWidget>();
    auto *layout = new QVBoxLayout(host.get());
    auto *view = new QGraphicsView(host.get());
    layout->addWidget(view);
    host->resize(96, 96);
    QVERIFY(GpuDisplayWindow::installInPreview(view));
    host->show();
    QApplication::processEvents();
    static_cast<void>(QTest::qWaitForWindowExposed(host.get()));
    for (int i = 0; i < 20; ++i) {
        QApplication::processEvents();
        QTest::qWait(10);
    }
    GpuDisplayWindow *glWindow = qobject_cast<GpuDisplayWindow *>(GpuDisplayWindow::activeWindow());
    QVERIFY(glWindow);
    QVERIFY(!glWindow->lastPaintedPicture().valid);

    // A 32x8 frame shown at display size 64x8: the picture is the letterbox of the DISPLAY size.
    QImage submitted(32, 8, QImage::Format_RGB888);
    submitted.fill(qRgb(10, 200, 30));
    QVERIFY(GpuDisplayWindow::presentImageIfActive(submitted, QSize(64, 8)));
    QImage grabbed;
    QString reason;
    const bool ok = GpuDisplayWindow::grabPresentedFramebufferIfActive(&grabbed, &reason); // a real, synchronous paintGL
    if (!ok || grabbed.isNull()) {
        MLV_SKIP_OR_FAIL_IF_READBACK_FAILED("OpenGL framebuffer capture is unavailable in this environment");
    }
    const GpuDisplayWindow::PaintedPicture p = glWindow->lastPaintedPicture();
    QVERIFY(p.valid);
    const QRectF drawn = gpu_window_input::drawnPictureRect(p.valid, p.framebufferWidth, p.framebufferHeight,
                                                            p.devicePixelRatio, p.scaleX, p.scaleY);
    const QRectF expected = gpu_window_input::letterboxRect(QSizeF(glWindow->size()), QSizeF(64, 8));
    const double slack = 1.0 / p.devicePixelRatio; // the framebuffer is truncated to whole device pixels
    QVERIFY2(qAbs(drawn.x() - expected.x()) <= slack && qAbs(drawn.y() - expected.y()) <= slack
             && qAbs(drawn.width() - expected.width()) <= slack && qAbs(drawn.height() - expected.height()) <= slack,
             qPrintable(QStringLiteral("drawn (%1,%2 %3x%4) vs expected (%5,%6 %7x%8)")
                        .arg(drawn.x()).arg(drawn.y()).arg(drawn.width()).arg(drawn.height())
                        .arg(expected.x()).arg(expected.y()).arg(expected.width()).arg(expected.height())));

    // A new frame handed over but not painted yet: none until it is painted (adversary r2 pass 1 A2),
    // then the new picture.
    QImage square(16, 16, QImage::Format_RGB888);
    square.fill(qRgb(200, 10, 30));
    QVERIFY(GpuDisplayWindow::presentImageIfActive(square));
    QVERIFY(!glWindow->lastPaintedPicture().valid);
    QImage grabbedSquare;
    QString squareReason;
    QVERIFY(GpuDisplayWindow::grabPresentedFramebufferIfActive(&grabbedSquare, &squareReason));
    const GpuDisplayWindow::PaintedPicture after = glWindow->lastPaintedPicture();
    QVERIFY(after.valid);
    const QRectF squareDrawn = gpu_window_input::drawnPictureRect(after.valid, after.framebufferWidth, after.framebufferHeight,
                                                                  after.devicePixelRatio, after.scaleX, after.scaleY);
    const QRectF squareExpected = gpu_window_input::letterboxRect(QSizeF(glWindow->size()), QSizeF(16, 16));
    QVERIFY(qAbs(squareDrawn.width() - squareExpected.width()) <= slack);
    QVERIFY(qAbs(squareDrawn.height() - squareExpected.height()) <= slack);

    // Cleared and painted (the paint releases the texture and draws black): none.
    QVERIFY(GpuDisplayWindow::clearIfActive());
    QImage cleared;
    QString clearedReason;
    static_cast<void>(GpuDisplayWindow::grabPresentedFramebufferIfActive(&cleared, &clearedReason));
    QVERIFY(!glWindow->lastPaintedPicture().valid);

    host.reset();
    QVERIFY(!GpuDisplayWindow::isActive());
    qunsetenv(GpuDisplayWindow::environmentVariableName());
}

// PLAYER-DOUBLECLICK-FULLSCREEN-1: the viewer double-click filter, driven through a real application
// event filter exactly as MainWindow::eventFilter calls it. "viewport" stands in for the
// QGraphicsView viewport, "gl" for the GL display window, "fullscreen" for actionFullscreen.
namespace
{

class DoubleClickCountingWidget : public QWidget
{
public:
    int doubleClicks = 0;

protected:
    void mouseDoubleClickEvent(QMouseEvent *event) override
    {
        ++doubleClicks;
        event->accept();
    }
};

class ViewerDoubleClickFixture : public QObject
{
public:
    ViewerDoubleClickFixture()
    {
        fullscreen.setCheckable(true);
        state.enabled = true;
        state.clipLoaded = true;
        qApp->installEventFilter(this);
    }
    ~ViewerDoubleClickFixture() override { qApp->removeEventFilter(this); }

    bool eventFilter(QObject *watched, QEvent *event) override
    {
        if (watched == &gl && event->type() == QEvent::MouseButtonDblClick) ++glDoubleClicksSeen;
        if (watched == &gl && event->type() == QEvent::MouseButtonPress
         && static_cast<QMouseEvent *>(event)->flags().testFlag(Qt::MouseEventCreatedDoubleClick)) ++glFlaggedPressesSeen;
        const auto stateFn = [this](const QObject *target, const QPointF &pos) {
            lastTarget = target;
            viewer_dblclick::ViewerState now = state;
            if (!handleAt.isNull() && target == &viewport && (pos - handleAt).manhattanLength() < 2.0) {
                now.gradientHandleHovered = true; // a handle hit-tested at the event's own position
            }
            if (disarmAfterFirstRead) { // a one-shot tool (gradient paint) turns itself off after its click
                state.wbArmed = state.bpArmed = state.gradientPaintArmed = state.gradientHandleHovered = false;
            }
            return now;
        };
        if (viewer_dblclick::filterEvent(gate, watched, event, &viewport, &gl, stateFn)) {
            fullscreen.trigger();
            return true;
        }
        return QObject::eventFilter(watched, event);
    }

    viewer_dblclick::Gate gate;
    viewer_dblclick::ViewerState state;
    bool disarmAfterFirstRead = false;
    QPointF handleAt;                       // null: no handle anywhere
    const QObject *lastTarget = nullptr;    // the surface the reader was last handed
    int glDoubleClicksSeen = 0;
    int glFlaggedPressesSeen = 0;
    DoubleClickCountingWidget viewport;
    QWindow gl;
    QAction fullscreen;
};

void deliverViewerMouse(QObject *receiver, QEvent::Type type, Qt::MouseButton button,
                        Qt::KeyboardModifiers modifiers = Qt::NoModifier, quint64 timestampMs = 0,
                        const QPointF & pos = QPointF(10, 10))
{
    QMouseEvent event(type, pos, pos, button,
                      type == QEvent::MouseButtonRelease ? Qt::NoButton : Qt::MouseButtons(button), modifiers);
    if (timestampMs != 0) {
        event.setTimestamp(timestampMs);
    }
    QCoreApplication::sendEvent(receiver, &event);
}

int doubleClickIntervalMs()
{
    return QGuiApplication::styleHints()->mouseDoubleClickInterval();
}

} // namespace

void GuiSmokeTest::viewerDoubleClick_viewportTogglesOnceAndIsSwallowed()
{
    ViewerDoubleClickFixture f;
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton);
    QCOMPARE(f.fullscreen.isChecked(), true);
    QCOMPARE(f.viewport.doubleClicks, 0); // swallowed: the scene's double-click press path never runs
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton);
    QCOMPARE(f.fullscreen.isChecked(), false); // a second double-click leaves fullscreen
    QCOMPARE(f.viewport.doubleClicks, 0);
}

void GuiSmokeTest::viewerDoubleClick_glWindowTogglesOnce()
{
    ViewerDoubleClickFixture f;
    QSignalSpy triggered(&f.fullscreen, &QAction::triggered);
    deliverViewerMouse(&f.gl, QEvent::MouseButtonDblClick, Qt::LeftButton);
    QCOMPARE(triggered.count(), 1);
    QCOMPARE(f.fullscreen.isChecked(), true);
}

void GuiSmokeTest::viewerDoubleClick_armedToolPassesThrough()
{
    for (int tool = 0; tool < 4; ++tool) {
        ViewerDoubleClickFixture f;
        f.state.wbArmed = tool == 0;
        f.state.bpArmed = tool == 1;
        f.state.gradientPaintArmed = tool == 2;
        f.state.gradientHandleHovered = tool == 3;
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton);
        QVERIFY2(!f.fullscreen.isChecked(), qPrintable(QStringLiteral("tool %1 armed still toggled").arg(tool)));
        QCOMPARE(f.viewport.doubleClicks, 1); // passed through to the viewer unchanged
    }
}

void GuiSmokeTest::viewerDoubleClick_otherButtonModifierSettingOrNoClipDoNotToggle()
{
    {
        ViewerDoubleClickFixture f;
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::RightButton);
        QVERIFY(!f.fullscreen.isChecked());
        QCOMPARE(f.viewport.doubleClicks, 1);
    }
    {
        ViewerDoubleClickFixture f;
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::ShiftModifier);
        QVERIFY(!f.fullscreen.isChecked());
        QCOMPARE(f.viewport.doubleClicks, 1);
    }
    {
        ViewerDoubleClickFixture f;
        f.state.enabled = false;
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton);
        QVERIFY(!f.fullscreen.isChecked());
        QCOMPARE(f.viewport.doubleClicks, 1);
    }
    {
        ViewerDoubleClickFixture f;
        f.state.clipLoaded = false;
        deliverViewerMouse(&f.gl, QEvent::MouseButtonDblClick, Qt::LeftButton);
        QVERIFY(!f.fullscreen.isChecked());
    }
}

void GuiSmokeTest::viewerDoubleClick_otherWidgetDoesNotToggle()
{
    ViewerDoubleClickFixture f;
    DoubleClickCountingWidget other;
    deliverViewerMouse(&other, QEvent::MouseButtonDblClick, Qt::LeftButton);
    QVERIFY(!f.fullscreen.isChecked());
    QCOMPARE(other.doubleClicks, 1);
    QCOMPARE(f.viewport.doubleClicks, 0);
}

void GuiSmokeTest::viewerDoubleClick_toolArmedAtFirstPressSuppresses()
{
    const quint64 interval = static_cast<quint64>(qMax(1, doubleClickIntervalMs()));
    ViewerDoubleClickFixture f;
    // Gradient paint armed at the first press; it switches itself off on that click's release.
    f.state.gradientPaintArmed = true;
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, 100000);
    f.state.gradientPaintArmed = false;
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoModifier, 100000 + interval / 8);
    // Widget path: Qt hands the widget only the double-click for the second click, never its press.
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::NoModifier, 100000 + interval / 4);
    QVERIFY(!f.fullscreen.isChecked());
    QCOMPARE(f.viewport.doubleClicks, 1);

    // A later double-click with nothing armed toggles as usual.
    const quint64 later = 100000 + interval * 10;
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, later);
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::NoModifier, later + interval / 4);
    QVERIFY(f.fullscreen.isChecked());
    QCOMPARE(f.viewport.doubleClicks, 1);
}

void GuiSmokeTest::viewerDoubleClick_armedPressElsewhereDoesNotPoisonANewSequence()
{
    // Adversary A1 (r1, dd9f5441): a press on the gradient handle at (100,100), then within the
    // interval a press far away with nothing armed, then a double-click there. Qt does not pair
    // presses farther apart than the double-click distance, so the far press starts a new
    // sequence and its double-click must toggle.
    const quint64 interval = static_cast<quint64>(qMax(4, doubleClickIntervalMs()));
    ViewerDoubleClickFixture f;
    f.state.gradientHandleHovered = true;
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, 100000, QPointF(100, 100));
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoModifier, 100000 + interval / 20, QPointF(100, 100));
    f.state.gradientHandleHovered = false;
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, 100000 + interval / 4, QPointF(500, 500));
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoModifier, 100000 + interval / 4 + 1, QPointF(500, 500));
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, 100000 + interval / 2, QPointF(500, 500));
    deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::NoModifier, 100000 + interval / 2, QPointF(500, 500));
    QVERIFY(f.fullscreen.isChecked());
    QCOMPARE(f.viewport.doubleClicks, 0);
}

void GuiSmokeTest::viewerDoubleClick_newSequenceAfterAMovedOrSlowPressToggles()
{
    // Adversary pass 2 (65c29245) A1 and A2, on the widget path (Qt never hands a widget the
    // second press of a double-click, only the double-click). A1: WB-armed press at (100,100)
    // t=1000, the cursor moves away and back (Qt resets pairing), an unarmed press at t=1100,
    // the double-click at t=1200. A2: the same with the unarmed press at t=1400 (exactly one
    // interval later: Qt starts a new sequence) and the double-click at t=1500. Both toggle.
    const quint64 interval = 400;
    for (int variant = 0; variant < 2; ++variant) {
        ViewerDoubleClickFixture f;
        const quint64 t0 = 1000;
        const quint64 unarmedAt = variant == 0 ? t0 + 100 : t0 + interval;
        f.state.wbArmed = true;
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, t0, QPointF(100, 100));
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoModifier, t0 + 10, QPointF(100, 100));
        f.state.wbArmed = false;
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, unarmedAt, QPointF(100, 100));
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoModifier, unarmedAt + 10, QPointF(100, 100));
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::NoModifier, unarmedAt + 100, QPointF(100, 100));
        QVERIFY2(f.fullscreen.isChecked(), qPrintable(QStringLiteral("variant %1 (A%2) did not toggle").arg(variant).arg(variant + 1)));
        QCOMPARE(f.viewport.doubleClicks, 0);
    }
}

void GuiSmokeTest::viewerDoubleClick_glWindowRealQtPairingKeepsTheFirstPressRecord()
{
    // GL path through Qt's own pairing (adversary r1 pass 2 A3): QTest feeds the window system,
    // so QGuiApplication forms the double-click, flags the second press and sends the
    // double-click. A tool armed at the first press only (it turns itself off after the first
    // read) still suppresses; with nothing armed the same gesture toggles.
    {
        ViewerDoubleClickFixture f;
        f.gl.resize(64, 64);
        f.state.gradientPaintArmed = true;
        f.disarmAfterFirstRead = true;
        QTest::mouseDClick(&f.gl, Qt::LeftButton, Qt::NoModifier, QPoint(10, 10));
        QCOMPARE(f.glDoubleClicksSeen, 1);
        QCOMPARE(f.glFlaggedPressesSeen, 1);
        QVERIFY(!f.state.gradientPaintArmed); // it really was off by the double-click
        QVERIFY(!f.fullscreen.isChecked());
    }
    {
        ViewerDoubleClickFixture f;
        f.gl.resize(64, 64);
        QTest::mouseDClick(&f.gl, Qt::LeftButton, Qt::NoModifier, QPoint(10, 10));
        QCOMPARE(f.glDoubleClicksSeen, 1);
        QVERIFY(f.fullscreen.isChecked());
    }
}

void GuiSmokeTest::viewerDoubleClick_firstPressOutsideTheViewerStartsAnUnarmedRecord()
{
    // Adversary r1 pass 3 A2, widget path: a WB-armed viewer press/release at t=1000, then WB off.
    // Qt's first press of the next pair lands just outside the viewer (a layout gap of the same
    // native window) at t=10000; the second press on the viewer is flagged and never reaches the
    // widget; the double-click goes to the viewport at t=10100. It must toggle.
    // Every native press reaches a QWindow (the top-level QWidgetWindow) before any widget, so the
    // gap press is delivered to a window here. r2 pass 4 A2: a distinct press may reuse the
    // recorded press's timestamp (native injection); it still starts a new unarmed record.
    for (int reuseTimestamp = 0; reuseTimestamp < 2; ++reuseTimestamp) {
        ViewerDoubleClickFixture f;
        QWindow topLevelWindow;
        f.state.wbArmed = true;
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, 1000);
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoModifier, 1010);
        f.state.wbArmed = false;
        const quint64 gapAt = reuseTimestamp ? 1000 : 10000;
        deliverViewerMouse(&topLevelWindow, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, gapAt);
        deliverViewerMouse(&topLevelWindow, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoModifier, gapAt + 10);
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::NoModifier, gapAt + 100);
        QVERIFY2(f.fullscreen.isChecked(), qPrintable(QStringLiteral("reuseTimestamp=%1 did not toggle").arg(reuseTimestamp)));
        QCOMPARE(f.viewport.doubleClicks, 0);
    }
    // r2 pass 4 A1: the handle is hit-tested at the event's own position, which the reader is
    // handed; the live cursor plays no part. A press on the handle at (100,55) suppresses.
    {
        ViewerDoubleClickFixture f;
        f.handleAt = QPointF(100, 55);
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, 30000, QPointF(100, 55));
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoModifier, 30010, QPointF(100, 55));
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::NoModifier, 30100, QPointF(100, 55));
        QVERIFY(!f.fullscreen.isChecked());
        QCOMPARE(f.viewport.doubleClicks, 1);
        QCOMPARE(f.lastTarget, static_cast<const QObject *>(&f.viewport));
    }
    // The same physical press reaching the top-level window first and the QGraphicsView parent
    // after the viewport keeps an armed viewer record: the double-click passes through.
    {
        ViewerDoubleClickFixture f;
        QWindow topLevel;
        DoubleClickCountingWidget view;
        f.state.gradientPaintArmed = true;
        deliverViewerMouse(&topLevel, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, 20000);
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, 20000);
        deliverViewerMouse(&view, QEvent::MouseButtonPress, Qt::LeftButton, Qt::NoModifier, 20000);
        f.state.gradientPaintArmed = false; // the one-shot tool turns itself off on release
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonRelease, Qt::LeftButton, Qt::NoModifier, 20010);
        deliverViewerMouse(&f.viewport, QEvent::MouseButtonDblClick, Qt::LeftButton, Qt::NoModifier, 20100);
        QVERIFY(!f.fullscreen.isChecked());
        QCOMPARE(f.viewport.doubleClicks, 1);
    }
}

// PLAYER-FULLSCREEN-CONTROL-BAR-1: the CPU viewer's bar widget in a fullscreen top-level, driven the way MainWindow
// drives it: an application filter notes mouse activity on #855's idle clock and shows the bar (not for a move still
// at the entry position), and the bar's input hook maps its events through the same fullscreen_bar policy. "view"
// stands in for ui->graphicsView; a double-click that reaches it toggles fullscreen, as the viewer's does.
#include "../../platform/qt/FullscreenControlBar.h"

namespace
{

class FullscreenTogglingView : public QWidget
{
public:
    int doubleClicks = 0;
    int wheels = 0;

protected:
    void wheelEvent(QWheelEvent *event) override
    {
        ++wheels;
        event->accept();
    }

    void mouseDoubleClickEvent(QMouseEvent *event) override
    {
        ++doubleClicks;
        QWidget *top = window();
        if (top->isFullScreen()) top->showNormal();
        else top->showFullScreen();
        event->accept();
    }
};

class FullscreenBarFixture : public QObject
{
public:
    FullscreenBarFixture()
    {
        host.resize(960, 540);
        auto *layout = new QVBoxLayout(&host);
        layout->setContentsMargins(0, 0, 0, 0);
        view = new FullscreenTogglingView;
        layout->addWidget(view);
        viewport = new QWidget(view);
        viewport->setMouseTracking(true);
        bar = new FullscreenControlBarWidget(view);
        bar->setInputHook([this](const fullscreen_bar::Layout &l, QEvent *event, const QPointF &devicePos) {
            const QMouseEvent *mouse = dynamic_cast<const QMouseEvent *>(event);
            const QWheelEvent *wheel = dynamic_cast<const QWheelEvent *>(event);
            const Qt::MouseButton button = mouse ? mouse->button() : Qt::NoButton;
            const Qt::MouseButtons buttons = mouse ? mouse->buttons()
                                           : wheel ? wheel->buttons() : Qt::MouseButtons(Qt::NoButton);
            const fullscreen_bar::Part part = fullscreen_bar::hitTest(l, devicePos);
            // As MainWindow::fullscreenBarInput: a wheel during a gesture begun elsewhere is not the bar's.
            if (event->type() == QEvent::Wheel
                && !fullscreen_bar::claims(input, bar->presenter().shown(), event->type(), button, buttons, part)) return false;
            switch (fullscreen_bar::onMouse(&input, event->type(), button, buttons, part)) {
            case fullscreen_bar::Action::ExitFullscreen:
                // As MainWindow::viewerIdleFullscreenChanged( false ): the idle clock leaves fullscreen, the bar hides
                // and the drag ends (adversary r2 pass 2 A3, advisory).
                ++exits;
                idle.setFullscreen(false, clock.elapsed());
                bar->setBarShown(false, clock.elapsed());
                input.seekDrag = false;
                host.showNormal();
                break;
            case fullscreen_bar::Action::TogglePlay: ++plays; break;
            case fullscreen_bar::Action::Seek: lastSeek = fullscreen_bar::seekFrameAt(l, devicePos.x(), 600); break;
            default: break;
            }
            return true;
        });
        clock.start();
        qApp->installEventFilter(this);
    }
    ~FullscreenBarFixture() override { qApp->removeEventFilter(this); }

    void enterFullscreen()
    {
        host.showFullScreen();
        QVERIFY(QTest::qWaitForWindowExposed(&host));
        QTRY_VERIFY(host.isFullScreen());
        viewport->setGeometry(view->rect());
        idle.setFullscreen(true, clock.elapsed());
        entryPos = QCursor::pos();
        sawMove = false;
        haveLast = false;
    }

    bool eventFilter(QObject *watched, QEvent *event) override
    {
        Q_UNUSED(watched);
        // As MainWindow::fullscreenBarRouteGpuEvent: every non-wheel mouse event settles the bar's stale state.
        if (const QMouseEvent *mouse = dynamic_cast<const QMouseEvent *>(event)) {
            fullscreen_bar::settle(&input, event->type(), mouse->button(), mouse->buttons());
        }
        if (idle.fullscreen() && viewer_idle::eventIsActivity(event, &lastGlobal, &haveLast)) {
            idle.noteActivity(clock.elapsed());
            // The event's own position (eventIsActivity just stored it), as MainWindow::viewerIdleNoteEvent; not the
            // cursor's now (adversary r2 pass 2 A2, advisory).
            if (fullscreen_bar::activityShowsBar(event->type(), &sawMove, entryPos, lastGlobal.toPoint())) {
                bar->setBarShown(true, clock.elapsed());
            }
        }
        return QObject::eventFilter(watched, event);
    }

    // A point on a bar part, in the bar widget's own coordinates (device pixel ratio 1 in this test).
    QPoint onPart(const QRect &deviceRect) const { return deviceRect.center() - bar->pos(); }

    QWidget host;
    FullscreenTogglingView *view = nullptr;
    QWidget *viewport = nullptr;
    FullscreenControlBarWidget *bar = nullptr;
    viewer_idle::Controller idle;
    QElapsedTimer clock;
    QPoint entryPos;
    bool sawMove = false;
    QPointF lastGlobal;
    bool haveLast = false;
    fullscreen_bar::InputState input;
    int exits = 0;
    int plays = 0;
    int lastSeek = -1;
};

void sendBarMouse(QWidget *target, QEvent::Type type, const QPoint &pos, Qt::MouseButton button = Qt::LeftButton)
{
    const QPointF global = target->mapToGlobal(QPointF(pos));
    const Qt::MouseButtons buttons = (type == QEvent::MouseButtonRelease || type == QEvent::MouseMove)
        ? Qt::MouseButtons(Qt::NoButton) : Qt::MouseButtons(button);
    QMouseEvent event(type, QPointF(pos), global, type == QEvent::MouseMove ? Qt::NoButton : button, buttons, Qt::NoModifier);
    QCoreApplication::sendEvent(target, &event);
}

void sendBarWheel(QWidget *target, const QPoint &pos, Qt::MouseButtons buttons)
{
    const QPointF global = target->mapToGlobal(QPointF(pos));
    QWheelEvent event(QPointF(pos), global, QPoint(), QPoint(0, 120), buttons, Qt::NoModifier, Qt::NoScrollPhase, false);
    QCoreApplication::sendEvent(target, &event);
}

} // namespace

void GuiSmokeTest::fullscreenControlBar_moveShowsTheBarAndHiddenCostsNothing()
{
    FullscreenBarFixture f;
    f.enterFullscreen();
    QVERIFY(!f.bar->isVisible());

    // Hidden: 600 feeds (one per frame of playback) neither show nor rasterise anything.
    for (int i = 0; i < 600; ++i) {
        fullscreen_bar::Feed feed;
        feed.frame = i;
        feed.totalFrames = 600;
        feed.playing = true;
        feed.timecode = QStringLiteral("00:00:%1").arg(i, 2, 10, QLatin1Char('0'));
        f.bar->setFeed(feed);
    }
    QCoreApplication::processEvents();
    QVERIFY(!f.bar->isVisible());
    QCOMPARE(f.bar->presenter().rasterCount(), 0);
    QCOMPARE(f.bar->presenter().uploadCount(), 0);

    // A move still at the entry position (Windows synthesizes one) does not show it. Judged at the event's own
    // position, as MainWindow does; the cursor's now is elsewhere.
    const QPoint atEntry = f.viewport->mapFromGlobal(f.entryPos);
    QCursor::setPos(f.entryPos + QPoint(40, 0));
    sendBarMouse(f.viewport, QEvent::MouseMove, atEntry);
    QVERIFY(!f.bar->isVisible());

    // A real move shows it, at the bottom of the view, above the viewport.
    sendBarMouse(f.viewport, QEvent::MouseMove, atEntry + QPoint(40, 0));
    QVERIFY(f.bar->isVisible());
    const fullscreen_bar::Layout l = f.bar->viewLayout();
    QCOMPARE(f.bar->geometry().bottom(), f.view->rect().bottom());
    QCOMPARE(f.bar->width(), f.view->width());
    QCOMPARE(f.bar->height(), l.bar.height());
    QCOMPARE(f.view->childAt(QPoint(f.view->width() / 2, f.view->height() - 2)), static_cast<QWidget *>(f.bar));
    QVERIFY(f.bar->testAttribute(Qt::WA_OpaquePaintEvent));

    // Shown: the first paint rasterises once; another paint with nothing changed reuses it.
    fullscreen_bar::Feed feed;
    feed.totalFrames = 600;
    feed.timecode = QStringLiteral("00:00:00:00");
    f.bar->setFeed(feed);
    f.bar->grab();
    QCOMPARE(f.bar->presenter().rasterCount(), 1);
    QCOMPARE(f.bar->presenter().uploadCount(), 1);
    f.bar->grab();
    QCOMPARE(f.bar->presenter().rasterCount(), 1);

    // The seek column seeks.
    sendBarMouse(f.bar, QEvent::MouseButtonPress, f.onPart(l.seek));
    sendBarMouse(f.bar, QEvent::MouseButtonRelease, f.onPart(l.seek));
    QVERIFY(f.lastSeek > 0 && f.lastSeek < 599);
}

void GuiSmokeTest::fullscreenControlBar_doubleClickOnTheBarKeepsFullscreen()
{
    FullscreenBarFixture f;
    f.enterFullscreen();
    QCursor::setPos(f.entryPos + QPoint(40, 0));
    sendBarMouse(f.viewport, QEvent::MouseMove, f.viewport->mapFromGlobal(f.entryPos) + QPoint(40, 0));
    QVERIFY(f.bar->isVisible());
    const fullscreen_bar::Layout l = f.bar->viewLayout();

    // On the timecode (no action), on Play, and on the bare seek column: taken by the bar, never the view's toggle.
    for (const QRect &part : { l.timecode, l.play, l.seek }) {
        sendBarMouse(f.bar, QEvent::MouseButtonPress, f.onPart(part));
        sendBarMouse(f.bar, QEvent::MouseButtonRelease, f.onPart(part));
        sendBarMouse(f.bar, QEvent::MouseButtonDblClick, f.onPart(part));
        sendBarMouse(f.bar, QEvent::MouseButtonRelease, f.onPart(part));
        QCoreApplication::processEvents();
        QVERIFY(f.host.isFullScreen());
        QCOMPARE(f.view->doubleClicks, 0);
    }
    QCOMPARE(f.plays, 2);   // the press and Qt's second press (the double-click) on Play

    // Control: the same double-click on the view itself does toggle.
    sendBarMouse(f.view, QEvent::MouseButtonDblClick, QPoint(f.view->width() / 2, 10));
    QCOMPARE(f.view->doubleClicks, 1);
    QTRY_VERIFY(!f.host.isFullScreen());
}

void GuiSmokeTest::fullscreenControlBar_exitClickLeavesFullscreen()
{
    FullscreenBarFixture f;
    f.enterFullscreen();
    QCursor::setPos(f.entryPos + QPoint(40, 0));
    sendBarMouse(f.viewport, QEvent::MouseMove, f.viewport->mapFromGlobal(f.entryPos) + QPoint(40, 0));
    QVERIFY(f.bar->isVisible());
    const fullscreen_bar::Layout l = f.bar->viewLayout();
    sendBarMouse(f.bar, QEvent::MouseButtonPress, f.onPart(l.exit));
    QCOMPARE(f.exits, 1);
    QTRY_VERIFY(!f.host.isFullScreen());
    // Leaving fullscreen tears the bar down with the idle clock (adversary r2 pass 2 A3, advisory).
    QVERIFY(!f.idle.fullscreen());
    QVERIFY(!f.bar->presenter().shown());
    QVERIFY(!f.bar->isVisible());
    sendBarMouse(f.bar, QEvent::MouseButtonRelease, f.onPart(l.exit));
    QCOMPARE(f.exits, 1);
    QCOMPARE(f.view->doubleClicks, 0);
    QVERIFY(f.input.owned == Qt::NoButton);
}

// Adversary r1 pass 3 A2: after a device-pixel-ratio change the view saw no Resize for, the widget's geometry lags the
// layout it paints (2560x1440 at 1 -> 2: height 80/top 1360 kept, where 60/1380 is right), and a click on the painted
// Play hit-tested None. Stand-in for the ratio change (the offscreen ratio is fixed at 1): the same layout with the
// widget one bar height higher. A click maps through the painted layout, and a screen or ratio change re-follows.
void GuiSmokeTest::fullscreenControlBar_clickHitsWhatIsPaintedWhenTheGeometryLags()
{
    FullscreenBarFixture f;
    f.enterFullscreen();
    QCursor::setPos(f.entryPos + QPoint(40, 0));
    sendBarMouse(f.viewport, QEvent::MouseMove, f.viewport->mapFromGlobal(f.entryPos) + QPoint(40, 0));
    QVERIFY(f.bar->isVisible());
    const fullscreen_bar::Layout l = f.bar->viewLayout();
    const QRect current = f.bar->geometry();
    f.bar->setGeometry(current.translated(0, -current.height()));

    // The pixmap sits at the widget's origin: a click on the painted Play plays.
    const QPoint onPlay = l.play.center() - l.bar.topLeft();
    sendBarMouse(f.bar, QEvent::MouseButtonPress, onPlay);
    sendBarMouse(f.bar, QEvent::MouseButtonRelease, onPlay);
    QCOMPARE(f.plays, 1);

    // A move to another screen re-follows the view (every Qt), and a ratio change on the same screen (Qt 6.6+).
    QEvent screenChange(QEvent::ScreenChangeInternal);
    QCoreApplication::sendEvent(f.view, &screenChange);
    QCOMPARE(f.bar->geometry(), current);
#if QT_VERSION >= QT_VERSION_CHECK(6, 6, 0)
    f.bar->setGeometry(current.translated(0, -current.height()));
    QEvent ratioChange(QEvent::DevicePixelRatioChange);
    QCoreApplication::sendEvent(f.view, &ratioChange);
    QCOMPARE(f.bar->geometry(), current);
#endif
}

// Adversary r1 pass 3 A3, the CPU sibling: the widget gets a wheel by position even while a gesture begun on the
// viewer holds a button (Qt grabs only mouse events). That wheel is the gesture's and reaches the view; with no
// foreign button held the bar keeps it (the viewer never zooms under the bar).
void GuiSmokeTest::fullscreenControlBar_wheelDuringAForeignGestureReachesTheView()
{
    FullscreenBarFixture f;
    f.enterFullscreen();
    QCursor::setPos(f.entryPos + QPoint(40, 0));
    sendBarMouse(f.viewport, QEvent::MouseMove, f.viewport->mapFromGlobal(f.entryPos) + QPoint(40, 0));
    QVERIFY(f.bar->isVisible());
    const fullscreen_bar::Layout l = f.bar->viewLayout();
    sendBarWheel(f.bar, f.onPart(l.timecode), Qt::NoButton);
    QCOMPARE(f.view->wheels, 0);
    sendBarWheel(f.bar, f.onPart(l.timecode), Qt::RightButton);
    QCOMPARE(f.view->wheels, 1);

    // Adversary r1 pass 4 A2: a seek press on the bar whose release is lost (a popup took it), then a fresh left drag
    // begun on the viewer: its wheel over the bar is the viewer's, and the stale seek no longer holds the bar.
    sendBarMouse(f.bar, QEvent::MouseButtonPress, f.onPart(l.seek));
    QVERIFY(f.input.seekDrag);
    sendBarMouse(f.viewport, QEvent::MouseButtonPress, f.viewport->rect().center());
    QVERIFY(!f.input.seekDrag);
    sendBarWheel(f.bar, f.onPart(l.timecode), Qt::LeftButton);
    QCOMPARE(f.view->wheels, 2);
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
