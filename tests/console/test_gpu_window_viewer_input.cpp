// PLAYER-GL-WINDOW-INPUT-PARITY-1: pins the pure mapping and routing rules in
// platform/qt/GpuWindowViewerInput.h. When the preview is hosted in the GL display window,
// viewer clicks land on that window; the forwarder maps them into scene coordinates through
// the picture rect paintGL last drew (drawnPictureRect of GpuDisplayWindow::lastPaintedPicture,
// then pickPixel) and routes them with route(). C6 is the drift pin: if paintGL's fit, its one
// store of the drawn geometry, or the quad that draws with it changes, this test fails.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../platform/qt/GpuWindowViewerInput.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <cmath>
#include <limits>

using gpu_window_input::Route;

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

// What GpuDisplayWindow::paintGL records (lastPaintedPicture) for a frame of display size dw x dh in a
// w x h window at dpr, with paintGL's own int framebuffer and float fit arithmetic, as the forwarder's
// hook turns it into a rect.
QRectF paintedRect(int w, int h, double dpr, int dw, int dh)
{
    const int fbw = static_cast<int>(w * dpr);
    const int fbh = static_cast<int>(h * dpr);
    const float winAspect = static_cast<float>(w) / static_cast<float>(h);
    const float imgAspect = static_cast<float>(dw) / static_cast<float>(dh);
    float sx = 1.0f;
    float sy = 1.0f;
    if (imgAspect > winAspect) sy = winAspect / imgAspect;
    else                       sx = imgAspect / winAspect;
    return gpu_window_input::drawnPictureRect(true, fbw, fbh, dpr, sx, sy);
}

void assertRect(const QRectF & r, double x, double y, double w, double h)
{
    ASSERT_NEAR(x, r.x(), 1e-9);
    ASSERT_NEAR(y, r.y(), 1e-9);
    ASSERT_NEAR(w, r.width(), 1e-9);
    ASSERT_NEAR(h, r.height(), 1e-9);
}

} // namespace

TEST(GpuWindowViewerInput, C1_LetterboxRectMatchesPaintGlFit)
{
    using gpu_window_input::letterboxRect;
    // Wider picture than window: bars top and bottom.
    assertRect(letterboxRect(QSizeF(1920, 1080), QSizeF(1920, 804)), 0, 138, 1920, 804);
    assertRect(letterboxRect(QSizeF(1000, 1000), QSizeF(2000, 1000)), 0, 250, 1000, 500);
    // Narrower picture than window: bars left and right.
    assertRect(letterboxRect(QSizeF(1920, 1080), QSizeF(1080, 1080)), 420, 0, 1080, 1080);
    // Any zero dimension -> empty.
    ASSERT_TRUE(letterboxRect(QSizeF(0, 1080), QSizeF(1920, 804)).isEmpty());
    ASSERT_TRUE(letterboxRect(QSizeF(1920, 0), QSizeF(1920, 804)).isEmpty());
    ASSERT_TRUE(letterboxRect(QSizeF(1920, 1080), QSizeF(0, 804)).isEmpty());
    ASSERT_TRUE(letterboxRect(QSizeF(1920, 1080), QSizeF(1920, 0)).isEmpty());
    ASSERT_TRUE(letterboxRect(QSizeF(-1, 1080), QSizeF(1920, 804)).isEmpty());
}

TEST(GpuWindowViewerInput, C2_WindowToSceneRoundTripsAndRejectsTheBars)
{
    using gpu_window_input::letterboxRect;
    using gpu_window_input::sceneToWindow;
    using gpu_window_input::windowToScene;
    const QSizeF window(1920, 1080);
    const QSizeF scene(1920, 804);
    const QRectF r = letterboxRect(window, scene);
    ASSERT_FALSE(r.isEmpty());
    for (int i = 0; i < 5; ++i)
    {
        for (int j = 0; j < 5; ++j)
        {
            const QPointF p(r.x() + r.width() * (0.1 + 0.2 * i), r.y() + r.height() * (0.1 + 0.2 * j));
            QPointF s;
            ASSERT_TRUE(windowToScene(p, window, scene, &s));
            const QPointF back = sceneToWindow(s, window, scene);
            ASSERT_NEAR(p.x(), back.x(), 1e-9);
            ASSERT_NEAR(p.y(), back.y(), 1e-9);
        }
    }
    QPointF topLeft;
    ASSERT_TRUE(windowToScene(r.topLeft(), window, scene, &topLeft));
    ASSERT_NEAR(0.0, topLeft.x(), 1e-9);
    ASSERT_NEAR(0.0, topLeft.y(), 1e-9);
    QPointF bottomRight;
    // Half-open picture (adversary A2, r1): the bottom-right corner is one past the last pixel.
    ASSERT_FALSE(windowToScene(r.bottomRight(), window, scene, &bottomRight));
    ASSERT_NEAR(scene.width(), bottomRight.x(), 1e-9);
    ASSERT_NEAR(scene.height(), bottomRight.y(), 1e-9);
    QPointF edge;
    ASSERT_FALSE(windowToScene(QPointF(960, r.bottom()), window, scene, &edge));
    ASSERT_FALSE(windowToScene(QPointF(r.right(), 540), window, scene, &edge));
    ASSERT_TRUE(windowToScene(QPointF(960, r.bottom() - 0.01), window, scene, &edge));

    // The centre of the window is the centre of the scene.
    QPointF centre;
    ASSERT_TRUE(windowToScene(QPointF(960, 540), window, scene, &centre));
    ASSERT_NEAR(960.0, centre.x(), 1e-9);
    ASSERT_NEAR(402.0, centre.y(), 1e-9);

    // A point in the top bar is outside the picture.
    QPointF bar;
    ASSERT_FALSE(windowToScene(QPointF(960, 20), window, scene, &bar));
    // Empty geometry is refused.
    QPointF none;
    ASSERT_FALSE(windowToScene(QPointF(10, 10), window, QSizeF(), &none));
    ASSERT_FALSE(windowToScene(QPointF(10, 10), QSizeF(), scene, &none));
}

TEST(GpuWindowViewerInput, C3_DevicePixelRatioCancels)
{
    using gpu_window_input::windowToScene;
    const QSizeF window(1280, 720);
    const QSizeF scenes[] = { QSizeF(1920, 804), QSizeF(5760, 2410) };
    const QPointF points[] = { QPointF(640, 360), QPointF(100, 200), QPointF(1200, 600) };
    for (const QSizeF & scene : scenes)
    {
        for (const QPointF & p : points)
        {
            QPointF a;
            QPointF b;
            const bool insideA = windowToScene(p, window, scene, &a);
            const bool insideB = windowToScene(p * 1.5, window * 1.5, scene, &b);
            ASSERT_EQ(insideA, insideB);
            ASSERT_NEAR(a.x(), b.x(), 1e-9);
            ASSERT_NEAR(a.y(), b.y(), 1e-9);
        }
    }
}

TEST(GpuWindowViewerInput, C4_RouteTable)
{
    using gpu_window_input::route;
    for (int release = 0; release < 2; ++release)
    {
        const bool onRelease = release != 0;
        // Left press: WB first, then BP, then nothing.
        ASSERT_TRUE(route(QEvent::MouseButtonPress, Qt::LeftButton, true, false, onRelease) == Route::PickWhiteBalance);
        ASSERT_TRUE(route(QEvent::MouseButtonPress, Qt::LeftButton, true, true, onRelease) == Route::PickWhiteBalance);
        ASSERT_TRUE(route(QEvent::MouseButtonPress, Qt::LeftButton, false, true, onRelease) == Route::PickBadPixel);
        ASSERT_TRUE(route(QEvent::MouseButtonPress, Qt::LeftButton, false, false, onRelease) == Route::None);
        // Left release never picks.
        ASSERT_TRUE(route(QEvent::MouseButtonRelease, Qt::LeftButton, true, true, onRelease) == Route::None);
        // Right button: the platform trigger type only, armed pickers or not.
        for (int armed = 0; armed < 2; ++armed)
        {
            const bool a = armed != 0;
            ASSERT_TRUE(route(QEvent::MouseButtonPress, Qt::RightButton, a, a, onRelease)
                        == (onRelease ? Route::None : Route::ContextMenu));
            ASSERT_TRUE(route(QEvent::MouseButtonRelease, Qt::RightButton, a, a, onRelease)
                        == (onRelease ? Route::ContextMenu : Route::None));
        }
        // Middle button: nothing.
        ASSERT_TRUE(route(QEvent::MouseButtonPress, Qt::MiddleButton, true, true, onRelease) == Route::None);
        ASSERT_TRUE(route(QEvent::MouseButtonRelease, Qt::MiddleButton, true, true, onRelease) == Route::None);
        // Drag and drop.
        ASSERT_TRUE(route(QEvent::DragEnter, Qt::NoButton, false, false, onRelease) == Route::AcceptDrag);
        ASSERT_TRUE(route(QEvent::DragMove, Qt::NoButton, true, true, onRelease) == Route::AcceptDrag);
        ASSERT_TRUE(route(QEvent::Drop, Qt::NoButton, true, false, onRelease) == Route::Drop);
        // Everything else.
        ASSERT_TRUE(route(QEvent::MouseMove, Qt::LeftButton, true, true, onRelease) == Route::None);
        ASSERT_TRUE(route(QEvent::MouseButtonDblClick, Qt::LeftButton, true, true, onRelease) == Route::None);
        ASSERT_TRUE(route(QEvent::MouseButtonDblClick, Qt::RightButton, true, true, onRelease) == Route::None);
        ASSERT_TRUE(route(QEvent::Wheel, Qt::NoButton, true, true, onRelease) == Route::None);
        ASSERT_TRUE(route(QEvent::ContextMenu, Qt::RightButton, true, true, onRelease) == Route::None);
        ASSERT_TRUE(route(QEvent::DragLeave, Qt::NoButton, true, true, onRelease) == Route::None);
    }
#ifdef Q_OS_WIN
    ASSERT_TRUE(gpu_window_input::contextMenuOnReleaseDefault());
#else
    ASSERT_FALSE(gpu_window_input::contextMenuOnReleaseDefault());
#endif
}

TEST(GpuWindowViewerInput, C5_PressIsOnViewer)
{
    using gpu_window_input::pressIsOnViewer;
    QObject gl;
    QObject other;
    ASSERT_TRUE(pressIsOnViewer(false, &gl, &gl));
    ASSERT_FALSE(pressIsOnViewer(false, &other, &gl));
    ASSERT_TRUE(pressIsOnViewer(true, &other, &gl));
    ASSERT_FALSE(pressIsOnViewer(false, &other, nullptr));
    ASSERT_FALSE(pressIsOnViewer(false, nullptr, nullptr));
}

TEST(GpuWindowViewerInput, C7_PickPixelStaysInsideTheImageAndFollowsTheDrawnPicture)
{
    using gpu_window_input::letterboxRect;
    using gpu_window_input::pickPixel;
    int x = -1;
    int y = -1;
    // Adversary r1 pass 1 A2: the picture is half-open, and a hit is clamped to a real pixel.
    const QRectF wide = letterboxRect(QSizeF(1920, 1080), QSizeF(1920, 804)); // y 138..942
    ASSERT_FALSE(pickPixel(QPointF(960, 942), wide, QSizeF(1920, 804), &x, &y));
    ASSERT_TRUE(pickPixel(QPointF(960, 941.9), wide, QSizeF(1920, 804), &x, &y));
    ASSERT_EQ(960, x);
    ASSERT_EQ(803, y);
    ASSERT_FALSE(pickPixel(QPointF(1920, 540), wide, QSizeF(1920, 804), &x, &y));
    ASSERT_TRUE(pickPixel(QPointF(1919.9, 540), wide, QSizeF(1920, 804), &x, &y));
    ASSERT_EQ(1919, x);
    ASSERT_EQ(402, y);
    // Pillarbox (1080x1080 in 1920x1080, picture x 420..1500).
    const QRectF tall = letterboxRect(QSizeF(1920, 1080), QSizeF(1080, 1080));
    ASSERT_FALSE(pickPixel(QPointF(1500, 540), tall, QSizeF(1080, 1080), &x, &y));
    ASSERT_TRUE(pickPixel(QPointF(1499.5, 540), tall, QSizeF(1080, 1080), &x, &y));
    ASSERT_EQ(1079, x);
    ASSERT_EQ(540, y);
    ASSERT_TRUE(pickPixel(QPointF(0, 138), wide, QSizeF(1920, 804), &x, &y));
    ASSERT_EQ(0, x);
    ASSERT_EQ(0, y);
    ASSERT_TRUE(pickPixel(QPointF(960, 540), wide, QSizeF(1920, 804), &x, &y));
    ASSERT_EQ(960, x);
    ASSERT_EQ(402, y);

    // Adversary r1 pass 1 A3: 1920x804 clip fit into 1000x1000 has a 1000x418 scene; the CUDA texture
    // route draws the 1920x804 display size, from y 290.625 (the scene's own letterbox starts at 291).
    // The drawn rect decides, never the scene's aspect.
    const QSizeF w1000(1000, 1000);
    const QSizeF scene418(1000, 418);
    const QRectF drawn804 = letterboxRect(w1000, QSizeF(1920, 804));
    ASSERT_TRUE(pickPixel(QPointF(500, 290.75), drawn804, scene418, &x, &y));
    ASSERT_EQ(500, x);
    ASSERT_EQ(0, y);
    ASSERT_FALSE(pickPixel(QPointF(500, 290.5), drawn804, scene418, &x, &y)); // the bar
    // The image route drew the scene itself: the same click is on its black bar.
    ASSERT_FALSE(pickPixel(QPointF(500, 290.75), letterboxRect(w1000, scene418), scene418, &x, &y));

    // Adversary r1 pass 2 A1: clip 1920x420 at stretchY 3 in a 1000x1000 window. The scene is
    // 1000x654 (integer division before the stretch) but the texture route draws a 1920x1260
    // display, 656.25 high from y 171.875.
    const QSizeF scene654(1000, 654);
    const QRectF drawn1260 = letterboxRect(w1000, QSizeF(1920, 1260));
    ASSERT_TRUE(pickPixel(QPointF(500, 172), drawn1260, scene654, &x, &y));
    ASSERT_EQ(500, x);
    ASSERT_EQ(0, y);
    ASSERT_TRUE(pickPixel(QPointF(500, 828.0), drawn1260, scene654, &x, &y));
    ASSERT_EQ(653, y);
    ASSERT_FALSE(pickPixel(QPointF(500, 171.8), drawn1260, scene654, &x, &y));
    ASSERT_FALSE(pickPixel(QPointF(500, 828.2), drawn1260, scene654, &x, &y));
    ASSERT_TRUE(pickPixel(QPointF(500, 500), drawn1260, scene654, &x, &y));
    ASSERT_EQ(327, y);
    // Width sibling: clip 1080x1922 at stretchX 2 in 1200x1000: scene 1122x1000, texture display
    // 2160x1922 drawn from x 38.08.
    const QRectF drawn2160 = letterboxRect(QSizeF(1200, 1000), QSizeF(2160, 1922));
    ASSERT_TRUE(pickPixel(QPointF(38.4, 500), drawn2160, QSizeF(1122, 1000), &x, &y));
    ASSERT_EQ(0, x);
    ASSERT_FALSE(pickPixel(QPointF(38.0, 500), drawn2160, QSizeF(1122, 1000), &x, &y));

    // Bars and empty geometry: no drawn picture, or no scene, is no pick.
    ASSERT_FALSE(pickPixel(QPointF(960, 20), wide, QSizeF(1920, 804), &x, &y));
    ASSERT_FALSE(pickPixel(QPointF(10, 10), wide, QSizeF(), &x, &y));
    ASSERT_FALSE(pickPixel(QPointF(960, 540), QRectF(), QSizeF(1920, 804), &x, &y));
    ASSERT_FALSE(pickPixel(QPointF(0, 0), QRectF(0, 0, 0, 10), QSizeF(1920, 804), &x, &y));
    // Every hit over a dense sweep, through either drawn picture, is a real pixel.
    for (int pass = 0; pass < 2; ++pass)
    {
        const QRectF drawn = pass ? drawn1260 : letterboxRect(w1000, scene654);
        for (int wy = 0; wy <= 1000; wy += 3)
        {
            for (int wx = 0; wx <= 1000; wx += 7)
            {
                if (pickPixel(QPointF(wx + 0.5, wy + 0.5), drawn, scene654, &x, &y))
                {
                    ASSERT_TRUE(x >= 0 && x <= 999);
                    ASSERT_TRUE(y >= 0 && y <= 653);
                }
            }
        }
    }
}
// r2 (adversary r1 pass 3 A1-A3 + no-paint refusal). Red on r1's API (red.txt: r1's hooks picked
// from the requested route and the live stretch); here the pick goes through the rect paintGL
// recorded, computed with paintGL's own float fit (paintedRect). Window 1000x1000 at DPR 2, as the
// adversary inputs.
TEST(GpuWindowViewerInput, C9a_ImageRouteBlackBarIsNoPick)
{
    // A1: the image route drew the scene-sized 1000x654 image, from y 173; the texture route's taller
    // 1920x1260 rect would start at 171.875, but it is not what is on screen. (500,172) is the bar.
    const QRectF drawn = paintedRect(1000, 1000, 2.0, 1000, 654);
    int x = -1;
    int y = -1;
    ASSERT_FALSE(gpu_window_input::pickPixel(QPointF(500, 172), drawn, QSizeF(1000, 654), &x, &y));
    ASSERT_TRUE(gpu_window_input::pickPixel(QPointF(500, 173.5), drawn, QSizeF(1000, 654), &x, &y));
    ASSERT_EQ(0, y);
}

TEST(GpuWindowViewerInput, C9b_RgbFallbackMapsThroughTheRectDrawn)
{
    // A2: the texture present was requested but the RGB fallback presented, at 1000x654: the pick maps
    // through that picture (row 651), not the requested texture's 1920x1260 (row 649).
    const QRectF drawn = paintedRect(1000, 1000, 2.0, 1000, 654);
    int x = -1;
    int y = -1;
    ASSERT_TRUE(gpu_window_input::pickPixel(QPointF(500, 824.3), drawn, QSizeF(1000, 654), &x, &y));
    ASSERT_EQ(500, x);
    ASSERT_EQ(651, y);
}

TEST(GpuWindowViewerInput, C9c_StretchChangeBeforeTheNextFrameMapsThroughTheRectLastDrawn)
{
    // A3: paintGL drew 1920x1260 (stretchY 3: from y 171.875, 656.25 high). stretchY is then set to 1,
    // and no frame has landed: the recorded rect still describes the picture on screen.
    // (200 - 171.875) / 656.25 * 654 = 28.03.
    const QRectF drawn = paintedRect(1000, 1000, 2.0, 1920, 1260);
    int x = -1;
    int y = -1;
    ASSERT_TRUE(gpu_window_input::pickPixel(QPointF(500, 200), drawn, QSizeF(1000, 654), &x, &y));
    ASSERT_EQ(28, y);
    ASSERT_FALSE(gpu_window_input::pickPixel(QPointF(500, 171.8), drawn, QSizeF(1000, 654), &x, &y));
}

TEST(GpuWindowViewerInput, C9d_NoPaintYetIsNoPick)
{
    // Nothing drawn yet (lastPaintedPicture().valid == false): the rect is empty and there is no pick.
    const QRectF none = gpu_window_input::drawnPictureRect(false, 2000, 2000, 2.0, 1.0, 0.654);
    ASSERT_TRUE(none.isEmpty());
    int x = -1;
    int y = -1;
    ASSERT_FALSE(gpu_window_input::pickPixel(QPointF(500, 500), none, QSizeF(1000, 654), &x, &y));
    ASSERT_EQ(-1, x);
    ASSERT_EQ(-1, y);
}

TEST(GpuWindowViewerInput, C10_DrawnPictureRectIsTheQuadPaintGlDrew)
{
    using gpu_window_input::drawnPictureRect;
    using gpu_window_input::letterboxRect;
    // At DPR 1 and 1.5 it is the letterbox of the display size it was painted with (float fit).
    const QRectF a = paintedRect(1920, 1080, 1.0, 1920, 804);
    const QRectF b = letterboxRect(QSizeF(1920, 1080), QSizeF(1920, 804));
    ASSERT_NEAR(b.x(), a.x(), 1e-3);
    ASSERT_NEAR(b.y(), a.y(), 1e-3);
    ASSERT_NEAR(b.width(), a.width(), 1e-3);
    ASSERT_NEAR(b.height(), a.height(), 1e-3);
    const QRectF c = paintedRect(1280, 720, 1.5, 1080, 1080);
    const QRectF d = letterboxRect(QSizeF(1280, 720), QSizeF(1080, 1080));
    ASSERT_NEAR(d.x(), c.x(), 1e-3);
    ASSERT_NEAR(d.y(), c.y(), 1e-3);
    ASSERT_NEAR(d.width(), c.width(), 1e-3);
    ASSERT_NEAR(d.height(), c.height(), 1e-3);
    // The framebuffer is truncated to whole device pixels: 1001 logical at DPR 1.25 is 1251 device
    // pixels, so a full-width picture ends at logical 1000.8, where paintGL's viewport ends.
    const QRectF e = drawnPictureRect(true, 1251, 1251, 1.25, 1.0, 1.0);
    ASSERT_NEAR(0.0, e.x(), 1e-9);
    ASSERT_NEAR(1000.8, e.width(), 1e-9);
    // Not a positive finite input: none.
    ASSERT_TRUE(drawnPictureRect(true, 0, 1000, 1.0, 1.0, 1.0).isEmpty());
    ASSERT_TRUE(drawnPictureRect(true, 1000, -1, 1.0, 1.0, 1.0).isEmpty());
    ASSERT_TRUE(drawnPictureRect(true, 1000, 1000, 0.0, 1.0, 1.0).isEmpty());
    ASSERT_TRUE(drawnPictureRect(true, 1000, 1000, 1.0, 0.0, 1.0).isEmpty());
    ASSERT_TRUE(drawnPictureRect(true, 1000, 1000, 1.0, 1.0, -0.5).isEmpty());
    ASSERT_TRUE(drawnPictureRect(true, 1000, 1000, std::nan(""), 1.0, 1.0).isEmpty());
    ASSERT_TRUE(drawnPictureRect(true, 1000, 1000, 1.0, std::numeric_limits<double>::infinity(), 1.0).isEmpty());
    ASSERT_FALSE(drawnPictureRect(true, 1000, 1000, 1.0, 1.0, 1.0).isEmpty());
}

TEST(GpuWindowViewerInput, C8_DropPathsMatchTheScenesFilesDroppedList)
{
    // A1: the scene's dropEvent hands openMlvSet every toLocalFile() path, in order, skipping
    // non-file URLs; UNC hosts survive (the 2026-04-24 \\server\share fix).
    const QList<QUrl> urls = {
        QUrl::fromLocalFile(QStringLiteral("C:/drop-test/part.M00")),
        QUrl(QStringLiteral("https://example.invalid/not-a-file")),
        QUrl::fromLocalFile(QStringLiteral("C:/drop-test/main.dat")),
        QUrl(QStringLiteral("file://server/share/clip.dat")),
    };
    const QStringList paths = gpu_window_input::localPathsFromUrls(urls);
    ASSERT_EQ(3, paths.size());
    ASSERT_TRUE(paths.at(0) == QStringLiteral("C:/drop-test/part.M00"));
    ASSERT_TRUE(paths.at(1) == QStringLiteral("C:/drop-test/main.dat"));
    ASSERT_TRUE(paths.at(2) == QStringLiteral("//server/share/clip.dat"));
    ASSERT_TRUE(gpu_window_input::localPathsFromUrls(QList<QUrl>()).isEmpty());

    // The mirror is the scene's own loop: pin GraphicsPickerScene::dropEvent's body text.
    const QString scene = readRepoFile(QStringLiteral("platform/qt/GraphicsPickerScene.cpp"));
    ASSERT_TRUE(scene.contains(QStringLiteral("const QString localPath = url.toLocalFile();")));
    ASSERT_TRUE(scene.contains(QStringLiteral("if( !localPath.isEmpty() )")));
    ASSERT_TRUE(scene.contains(QStringLiteral("emit filesDropped( list );")));
}

TEST(GpuWindowViewerInput, C6_PaintGlFitBlockIsUnchanged)
{
    // Picks map through what paintGL recorded (lastPaintedPicture -> drawnPictureRect), and
    // drawnPictureRect assumes the quad spans NDC [-sx, sx] x [-sy, sy] of a framebuffer-sized
    // viewport. Pin the whole path as one contiguous text: the viewport from fbw/fbh, then the fit
    // block, the one store of exactly those values, and the vertex block that draws with them.
    // Adversary A4 (r1, advisory on f81879eb): a line such as "sy = 1.0f;" added after the fit block
    // passed the old pin; any line between the fit, the store and the vertices now fails it.
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString viewport = QStringLiteral(
        "    const qreal dpr = devicePixelRatio();\n"
        "    const int fbw = static_cast<int>(width() * dpr);\n"
        "    const int fbh = static_cast<int>(height() * dpr);\n"
        "    glViewport(0, 0, std::max(1, fbw), std::max(1, fbh));\n");
    ASSERT_TRUE(source.contains(viewport));
    const QString fitStoreAndQuad = QStringLiteral(
        "    const float winAspect = static_cast<float>(width()) / static_cast<float>(height());\n"
        "    const int displayWidth = m_pendingDisplayWidth > 0 ? m_pendingDisplayWidth : m_texture->width();\n"
        "    const int displayHeight = m_pendingDisplayHeight > 0 ? m_pendingDisplayHeight : m_texture->height();\n"
        "    const float imgAspect = static_cast<float>(displayWidth) / static_cast<float>(displayHeight);\n"
        "    float sx = 1.0f;\n"
        "    float sy = 1.0f;\n"
        "    if ( imgAspect > winAspect ) sy = winAspect / imgAspect;\n"
        "    else                         sx = imgAspect / winAspect;\n"
        "    m_lastPaintedPicture = PaintedPicture{ true, fbw, fbh, dpr, sx, sy };   // PLAYER-GL-WINDOW-INPUT-PARITY-1 r2: read by lastPaintedPicture()\n"
        "\n"
        "    // Interleaved [x, y, u, v]; screen-top maps to texture v=0 (matches the corrected\n"
        "    // GpuDisplayViewport quad so QImage row 0 / image top shows at the top -> upright).\n"
        "    const float verts[16] = {\n"
        "        -sx,  sy, 0.0f, 0.0f,\n"
        "         sx,  sy, 1.0f, 0.0f,\n"
        "        -sx, -sy, 0.0f, 1.0f,\n"
        "         sx, -sy, 1.0f, 1.0f,\n"
        "    };\n");
    ASSERT_TRUE(source.contains(fitStoreAndQuad));
    // Exactly one fit block and one store, so a second (divergent) copy cannot hide behind the pinned one.
    ASSERT_EQ(1, source.count(QStringLiteral("if ( imgAspect > winAspect ) sy = winAspect / imgAspect;")));
    ASSERT_EQ(1, source.count(QStringLiteral("m_lastPaintedPicture =")));
    // The viewport comes before the fit inside paintGL (no reassignment of fbw/fbh/dpr in between).
    const int paintAt = source.indexOf(QStringLiteral("void GpuDisplayWindow::paintGL()"));
    ASSERT_TRUE(paintAt >= 0);
    const int viewportAt = source.indexOf(viewport, paintAt);
    const int fitAt = source.indexOf(fitStoreAndQuad, paintAt);
    ASSERT_TRUE(viewportAt > paintAt);
    ASSERT_TRUE(fitAt > viewportAt);
    const QString between = source.mid(viewportAt + viewport.size(), fitAt - viewportAt - viewport.size());
    ASSERT_FALSE(between.contains(QStringLiteral("fbw =")));
    ASSERT_FALSE(between.contains(QStringLiteral("fbh =")));
    ASSERT_FALSE(between.contains(QStringLiteral("dpr =")));
    // The getter reports none while the window holds no texture.
    ASSERT_TRUE(source.contains(QStringLiteral("if ( !m_texture ) return PaintedPicture();\n    return m_lastPaintedPicture;")));
}
