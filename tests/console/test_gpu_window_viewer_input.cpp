// PLAYER-GL-WINDOW-INPUT-PARITY-1: pins the pure mapping and routing rules in
// platform/qt/GpuWindowViewerInput.h. When the preview is hosted in the GL display window,
// viewer clicks land on that window; the forwarder maps them into scene coordinates with
// windowToScene (the input mirror of GpuDisplayWindow::paintGL's letterbox fit) and routes
// them with route(). C6 is the drift pin: if paintGL's fit changes, this test fails so the
// mirror is updated with it.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../platform/qt/GpuWindowViewerInput.h"

#include <QFile>
#include <QString>
#include <QTextStream>

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
    windowToScene(r.topLeft(), window, scene, &topLeft);
    ASSERT_NEAR(0.0, topLeft.x(), 1e-9);
    ASSERT_NEAR(0.0, topLeft.y(), 1e-9);
    QPointF bottomRight;
    windowToScene(r.bottomRight(), window, scene, &bottomRight);
    ASSERT_NEAR(scene.width(), bottomRight.x(), 1e-9);
    ASSERT_NEAR(scene.height(), bottomRight.y(), 1e-9);

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

TEST(GpuWindowViewerInput, C6_PaintGlFitBlockIsUnchanged)
{
    // GpuWindowViewerInput.h letterboxRect mirrors these two lines; change both together.
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("if ( imgAspect > winAspect ) sy = winAspect / imgAspect;")));
    ASSERT_TRUE(source.contains(QStringLiteral("else                         sx = imgAspect / winAspect;")));
    // Aspect from the logical size (the mapper works in logical pixels too).
    ASSERT_TRUE(source.contains(QStringLiteral("const float winAspect = static_cast<float>(width()) / static_cast<float>(height());")));
}
