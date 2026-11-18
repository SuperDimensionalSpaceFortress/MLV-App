// Wiring test: pins PLAYER-GL-WINDOW-INPUT-PARITY-1's call sites in MainWindow.cpp. When the
// preview is hosted in the GL display window, (1) the app filter counts a press on that
// window as a press on the viewer, so it no longer disarms an armed picker, (2) initGui
// installs the GL-window input forwarder after the GL window is created, and (3) the
// forwarder's hooks call the QGraphicsView path's own slots unchanged.
// MainWindow.cpp needs a full GUI build (not linked into console_tests), so this test reads
// the source as text (mirrors test_playback_fullscreen_ui_wiring.cpp). It deliberately does
// not include the new headers, so it builds on the pre-change base as a RED test.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
#include <QString>
#include <QTextStream>

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

QString functionBody(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

const QString kDefinition = QStringLiteral("void MainWindow::installGlWindowViewerInput");
const QString kNextDefinition = QStringLiteral("void MainWindow::pictureCustomContextMenuRequested");

} // namespace

TEST(GpuWindowViewerInputWiring, W1_PressOnTheGlWindowCountsAsOnTheViewer)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString filter = functionBody(source,
        QStringLiteral("bool MainWindow::eventFilter"),
        QStringLiteral("bool MainWindow::playbackPolicyActive"));
    ASSERT_FALSE(filter.isEmpty());
    const int pressAt = filter.indexOf(QStringLiteral("QEvent::MouseButtonPress"));
    ASSERT_TRUE(pressAt >= 0);
    const int nextAt = filter.indexOf(QStringLiteral("else if( event->type()"), pressAt);
    ASSERT_TRUE(nextAt > pressAt);
    const QString press = filter.mid(pressAt, nextAt - pressAt);
    ASSERT_TRUE(press.contains(QStringLiteral(
        "gpu_window_input::pressIsOnViewer( ui->graphicsView->underMouse(), watched, GpuDisplayWindow::activeWindow() )")));
    ASSERT_FALSE(press.contains(QStringLiteral("!ui->graphicsView->underMouse()")));
}

TEST(GpuWindowViewerInputWiring, W2_ForwarderIsInstalledOnceAfterTheGlWindowExists)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString call = QStringLiteral("installGlWindowViewerInput( glWindow )");
    // Remove the definition so only call sites are counted.
    QString rest = source;
    const QString body = functionBody(source, kDefinition, kNextDefinition);
    if (!body.isEmpty()) rest.remove(source.indexOf(body), body.size());
    const int callAt = rest.indexOf(call);
    ASSERT_TRUE(callAt >= 0);
    ASSERT_TRUE(rest.indexOf(call, callAt + 1) < 0);
    const int installAt = rest.indexOf(QStringLiteral("GpuDisplayWindow::installInPreview( ui->graphicsView );"));
    ASSERT_TRUE(installAt >= 0);
    ASSERT_TRUE(callAt > installAt);
}

TEST(GpuWindowViewerInputWiring, W3_HooksCallTheViewerPathsOwnSlots)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source, kDefinition, kNextDefinition);
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("whiteBalancePicked( x, y )")));
    ASSERT_TRUE(body.contains(QStringLiteral("badPixelPicked( x, y )")));
    ASSERT_TRUE(body.contains(QStringLiteral("pictureCustomContextMenuRequested( ui->graphicsView->mapFromGlobal( globalPos ) )")));
    ASSERT_TRUE(body.contains(QStringLiteral("installEventFilter( new gpu_window_input::Forwarder( glWindow")));
}

TEST(GpuWindowViewerInputWiring, W5_PickMappingUsesTheTextureRoutesOwnDisplaySize)
{
    // Adversary r1 pass 1 A3 / pass 2 A1: the CUDA texture route letterboxes with the clip's own
    // stretched size, not the scene's. The hook must compute it as that route does: the same
    // helper, the clip's width and height, and the same stretch reads the prep task carries.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source, kDefinition, kNextDefinition);
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral(
        "mainWindowGpuTexturePresentDisplaySize( static_cast<int>( getMlvWidth( m_pMlvObject ) ), static_cast<int>( getMlvHeight( m_pMlvObject ) ), getHorizontalStretchFactor( false ), getVerticalStretchFactor( false ) )")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "m_lastPresentedRequestContextValid && m_lastPresentedRequestContext.gpuPlaybackReconTexturePresentRequested")));
    // The route side of the mirror: the texture present's display size and the task's stretch.
    ASSERT_TRUE(source.contains(QStringLiteral("? static_cast<int>( getMlvWidth( m_pMlvObject ) )")));
    ASSERT_TRUE(source.contains(QStringLiteral("? static_cast<int>( getMlvHeight( m_pMlvObject ) )")));
    const int helperAt = source.indexOf(QStringLiteral("mainWindowGpuTexturePresentDisplaySize(\n"));
    ASSERT_TRUE(helperAt >= 0);
    const QString helperCall = source.mid(helperAt, 300);
    ASSERT_TRUE(helperCall.contains(QStringLiteral("task.stretchX")));
    ASSERT_TRUE(helperCall.contains(QStringLiteral("task.stretchY")));
    ASSERT_TRUE(source.contains(QStringLiteral("task.stretchX = stretchX;")));
    ASSERT_TRUE(source.contains(QStringLiteral("const double stretchX = getHorizontalStretchFactor(false);")));
    ASSERT_TRUE(source.contains(QStringLiteral("const double stretchY = getVerticalStretchFactor(false);")));
}

TEST(GpuWindowViewerInputWiring, W4_GlDropOpensLikeTheSceneNotLikeTheTopLevelWindow)
{
    // Adversary A1 (r1): the QGraphicsView path's drop is GraphicsPickerScene::dropEvent ->
    // filesDropped(every local path) -> openMlvSet. MainWindow::dropEvent (the top-level window's
    // drop) classifies by the FIRST path only, so a drop whose first file is a split-chunk part
    // and whose second is the main clip imports nothing there. The GL window's drop must take the
    // scene's route.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source, kDefinition, kNextDefinition);
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("hooks.openFiles = [this]( const QStringList &paths ) { openMlvSet( paths ); };")));
    ASSERT_FALSE(body.contains(QStringLiteral("dropEvent(")));
    // The scene's own route is unchanged: the same slot.
    ASSERT_TRUE(source.contains(QStringLiteral(
        "connect( m_pScene, SIGNAL( filesDropped(QStringList) ), this, SLOT( openMlvSet(QStringList) ) );")));
}
