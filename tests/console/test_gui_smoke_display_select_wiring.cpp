// Wiring/census test for UM-DISPLAY-SELECT-AND-LOG-1: a GUI smoke leg must never silently
// benchmark whatever screen the window's persisted geometry happened to leave it on (the UM
// LG-TV/Denon-AVR topology has a degraded headless fallback resolution when the TV is off --
// see .claude-state/project-memory/um-display-topology-lg-tv-denon-fallback-20260926.md).
// This pins: (1) every attached QScreen is logged, (2) the highest-physical-pixel screen is
// chosen (ties -> higher refresh, then primary), (3) the window is moved there BEFORE any
// fullscreen request or windowed maximize, (4) --windowed skips full screen and maximizes on
// the target instead, and (5) the pre-smoke geometry is restored before every return so a
// smoke run never overwrites the user's saved mainWindowGeometry.
// MainWindow.cpp/main.cpp need a full GUI build (not linked into console_tests), so this test
// reads the sources as text -- mirrors test_playback_smoke_fullscreen_wiring.cpp's approach.
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

} // namespace

TEST(GuiSmokeDisplaySelectWiring, HeaderDeclaresTheOptionAndTheApi)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool windowed = false;")));
    ASSERT_TRUE(header.contains(QStringLiteral("void logPlaybackSmokeDisplayInventory( void ) const;")));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "QScreen *choosePlaybackSmokeDisplayTarget( bool *outFallback,")));
    ASSERT_TRUE(header.contains(QStringLiteral("void movePlaybackSmokeWindowToScreen( QScreen *target );")));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "bool placePlaybackSmokeWindowWindowed( QScreen *target,")));
    // Forward-declared so the header does not need a full QScreen include.
    ASSERT_TRUE(header.contains(QStringLiteral("class QScreen;")));
}

TEST(GuiSmokeDisplaySelectWiring, MainDeclaresTheWindowedFlagAndWiresItToOptions)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("QStringLiteral(\"windowed\"),")));
    ASSERT_TRUE(source.contains(QStringLiteral("options.windowed = parser.isSet(windowedOpt);")));
}

TEST(GuiSmokeDisplaySelectWiring, MainDeclaresTheDisplayPreferOptionAndWiresItToOptions)
{
    // UM-DISPLAY-SELECT-AND-LOG-1 round 1c (measured topology): a per-venue name substring,
    // forwarded to choosePlaybackSmokeDisplayTarget() as a tie-break only.
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("QStringLiteral(\"display-prefer\"),")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "options.displayPreferSubstring = parser.value(displayPreferOpt);")));
}

TEST(GuiSmokeDisplaySelectWiring, InventoryLogsEveryScreenWithIdentityGeometryAndPrimary)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::logPlaybackSmokeDisplayInventory( void ) const"),
        QStringLiteral("QScreen *MainWindow::choosePlaybackSmokeDisplayTarget("));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("QGuiApplication::screens();")));
    ASSERT_TRUE(body.contains(QStringLiteral("gui_smoke.display_screen")));
    ASSERT_TRUE(body.contains(QStringLiteral("s->manufacturer()")));
    ASSERT_TRUE(body.contains(QStringLiteral("s->model()")));
    ASSERT_TRUE(body.contains(QStringLiteral("s->serialNumber()")));
    ASSERT_TRUE(body.contains(QStringLiteral("s->refreshRate()")));
    ASSERT_TRUE(body.contains(QStringLiteral("bool01( s == primary )")));
    // Physical pixels = geometry size x devicePixelRatio, not the logical (scaled) size.
    ASSERT_TRUE(body.contains(QStringLiteral("qRound( geo.width() * dpr )")));
    ASSERT_TRUE(body.contains(QStringLiteral("qRound( geo.height() * dpr )")));
}

TEST(GuiSmokeDisplaySelectWiring, TargetChoosesMaxPhysicalPixelsTieRefreshThenPrimary)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("QScreen *MainWindow::choosePlaybackSmokeDisplayTarget("),
        QStringLiteral("void MainWindow::movePlaybackSmokeWindowToScreen("));
    ASSERT_FALSE(body.isEmpty());

    const int maxPixelsAt = body.indexOf(QStringLiteral("pixels > bestPixels"));
    const int tieAt = body.indexOf(QStringLiteral("pixels == bestPixels"), maxPixelsAt);
    const int refreshTieAt = body.indexOf(QStringLiteral("refresh > bestRefresh"), tieAt);
    const int primaryTieAt = body.indexOf(
        QStringLiteral("refresh == bestRefresh && isPrimaryScreen && !bestIsPrimary"), refreshTieAt);
    ASSERT_TRUE(maxPixelsAt >= 0);
    ASSERT_TRUE(tieAt > maxPixelsAt);
    ASSERT_TRUE(refreshTieAt > tieAt);
    ASSERT_TRUE(primaryTieAt > refreshTieAt);

    // Fallback signals a choice that differs from either the window's starting screen or
    // the primary screen -- never "wherever the window happened to already be".
    ASSERT_TRUE(body.contains(
        QStringLiteral("*outFallback = best && ( ( best != startScreen ) || ( best != primary ) );")));
    ASSERT_TRUE(body.contains(QStringLiteral("if( outCandidateCount ) *outCandidateCount = screens.size();")));

    // UM-DISPLAY-SELECT-AND-LOG-1 round 1c: the preferred tie-break sits between the pixel
    // comparison and the refresh tie-break -- max pixels, then preferred, then refresh, then
    // primary -- so a preferred display with fewer pixels can never win over real resolution.
    const int preferredTieAt = body.indexOf(
        QStringLiteral("if( isPreferred && !bestIsPreferred )"), tieAt);
    ASSERT_TRUE(preferredTieAt > tieAt);
    ASSERT_TRUE(refreshTieAt > preferredTieAt);
    ASSERT_TRUE(body.contains(QStringLiteral(
        "isPreferred == bestIsPreferred && refresh > bestRefresh")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "isPreferred == bestIsPreferred && refresh == bestRefresh && isPrimaryScreen && !bestIsPrimary")));
}

TEST(GuiSmokeDisplaySelectWiring, PreferredDisplayMatchesNameModelOrManufacturerCaseInsensitively)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int fnAt = source.indexOf(QStringLiteral(
        "static QString playbackSmokeDisplayPreferenceMatchedField("));
    ASSERT_TRUE(fnAt >= 0);
    const QString tail = source.mid(fnAt, 700);
    ASSERT_TRUE(tail.contains(QStringLiteral("screen->name().contains( preferSubstring, Qt::CaseInsensitive )")));
    ASSERT_TRUE(tail.contains(QStringLiteral("screen->model().contains( preferSubstring, Qt::CaseInsensitive )")));
    ASSERT_TRUE(tail.contains(QStringLiteral("screen->manufacturer().contains( preferSubstring, Qt::CaseInsensitive )")));
}

TEST(GuiSmokeDisplaySelectWiring, DisplayTargetLineCarriesThePreferredFieldsAppendedAfterFallback)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(
        "gui_smoke.display_target screen=\\\"%1\\\" reason=%2 candidates=%3 fallback=%4 ")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(
        "preferred=\\\"%5\\\" preferred_matched=%6")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(
        "choosePlaybackSmokeDisplayTarget( &displayFallback, &displayCandidateCount, &displayTargetReason,\n"
        "                                           options.displayPreferSubstring, &displayPreferredStatus );")));
}

TEST(GuiSmokeDisplaySelectWiring, PlacementMovesToTargetBeforeFullscreenOrMaximize)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::movePlaybackSmokeWindowToScreen( QScreen *target )"),
        QStringLiteral("bool MainWindow::placePlaybackSmokeWindowWindowed("));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("handle->setScreen( target );")));
    ASSERT_TRUE(body.contains(QStringLiteral("move( target->availableGeometry().topLeft() );")));

    const QString windowedBody = functionBody(source,
        QStringLiteral("bool MainWindow::placePlaybackSmokeWindowWindowed("),
        QStringLiteral("// --gui-smoke-playback only (CUDA-PERF-PLAYBACK-FULLSCREEN-1)"));
    ASSERT_FALSE(windowedBody.isEmpty());
    const int moveAt = windowedBody.indexOf(QStringLiteral("movePlaybackSmokeWindowToScreen( target );"));
    const int maximizeAt = windowedBody.indexOf(QStringLiteral("showMaximized();"), moveAt);
    ASSERT_TRUE(moveAt >= 0);
    ASSERT_TRUE(maximizeAt > moveAt);
}

TEST(GuiSmokeDisplaySelectWiring, RunGuiPlaybackSmokePlacesBeforeForegroundAndFullscreen)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int windowedFlagAt = smokeBody.indexOf(
        QStringLiteral("const bool windowedSmoke = options.windowed;"));
    const int inventoryAt = smokeBody.indexOf(
        QStringLiteral("logPlaybackSmokeDisplayInventory();"), windowedFlagAt);
    const int chooseAt = smokeBody.indexOf(
        QStringLiteral("choosePlaybackSmokeDisplayTarget("), inventoryAt);
    const int guardAt = smokeBody.indexOf(
        QStringLiteral("PlaybackSmokeGeometryGuard"), chooseAt);
    const int moveAt = smokeBody.indexOf(
        QStringLiteral("movePlaybackSmokeWindowToScreen( displayTarget );"), guardAt);
    const int firstForegroundAt = smokeBody.indexOf(
        QStringLiteral("forcePlaybackSmokeWindowForeground();"), moveAt);
    const int branchAt = smokeBody.indexOf(QStringLiteral("if( windowedSmoke )"), firstForegroundAt);
    const int placeWindowedAt = smokeBody.indexOf(
        QStringLiteral("placePlaybackSmokeWindowWindowed("), branchAt);
    const int enterFullscreenAt = smokeBody.indexOf(
        QStringLiteral("fullscreenVerified = enterPlaybackSmokeFullscreen( displayTarget );"), branchAt);

    ASSERT_TRUE(windowedFlagAt >= 0);
    ASSERT_TRUE(inventoryAt > windowedFlagAt);
    ASSERT_TRUE(chooseAt > inventoryAt);
    ASSERT_TRUE(guardAt > chooseAt);
    ASSERT_TRUE(moveAt > guardAt);
    ASSERT_TRUE(firstForegroundAt > moveAt);
    ASSERT_TRUE(branchAt > firstForegroundAt);
    ASSERT_TRUE(placeWindowedAt > branchAt);
    ASSERT_TRUE(enterFullscreenAt > placeWindowedAt);
}

TEST(GuiSmokeDisplaySelectWiring, GeometryGuardCapturesBeforeMoveAndRestoresOnDestruction)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // A stack-scoped RAII guard declared before the first geometry mutation, so every
    // return past this point (success and every early return) restores it -- never a
    // save/restore pair a return could step around.
    const int guardStructAt = smokeBody.indexOf(QStringLiteral("struct PlaybackSmokeGeometryGuard"));
    const int destructorAt = smokeBody.indexOf(
        QStringLiteral("window->restoreGeometry( geometry );"), guardStructAt);
    const int captureAt = smokeBody.indexOf(
        QStringLiteral("playbackSmokeGeometryGuard{ this, saveGeometry() };"), guardStructAt);
    const int moveAt = smokeBody.indexOf(
        QStringLiteral("movePlaybackSmokeWindowToScreen( displayTarget );"), captureAt);

    ASSERT_TRUE(guardStructAt >= 0);
    ASSERT_TRUE(destructorAt > guardStructAt);
    ASSERT_TRUE(captureAt > destructorAt);
    ASSERT_TRUE(moveAt > captureAt);
}

TEST(GuiSmokeDisplaySelectWiring, WindowedModeSkipsTheFullscreenLossGateAndLeaveCall)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // The mid-loop-end fullscreen-loss gate does not apply to a windowed leg, which never
    // enters full screen in the first place -- isFullScreen() would be trivially false.
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(
        "if( !windowedSmoke && ( !isFullScreen() || m_playbackSmokeFullscreenLostCount > 0 ) )")));

    // The teardown guard only calls leavePlaybackSmokeFullscreen() when full screen was
    // actually entered (active == !windowedSmoke), so a windowed session never toggles the
    // fullscreen action it never turned on.
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("playbackSmokeFullscreenGuard{ this, !windowedSmoke };")));
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("if( window && active ) window->leavePlaybackSmokeFullscreen();")));
}

TEST(GuiSmokeDisplaySelectWiring, DisplayTargetAndWindowPlacementLinesAreLoggedForBothModes)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = functionBody(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("void MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    ASSERT_TRUE(smokeBody.contains(QStringLiteral(
        "gui_smoke.display_target screen=\\\"%1\\\" reason=%2 candidates=%3 fallback=%4")));

    // The mode=windowed/mode=fullscreen prefix and the shared window=/preview= tail are
    // adjacent QStringLiteral pieces split across two source lines (like the existing
    // gui_smoke.fullscreen_request pin above), so they are matched as separate substrings
    // rather than one span that would also have to match the raw newline and indentation
    // between them.
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(
        "gui_smoke.window_placement mode=windowed screen=\\\"%1\\\" verified=%2 ")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(
        "gui_smoke.window_placement mode=fullscreen screen=\\\"%1\\\" verified=%2 ")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("window=%3,%4 %5x%6 preview=%7x%8 target_screen=\\\"%9\\\" ")));
    // UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol pre-review BLOCKER 3 / opus design-review item
    // 3): appended after preview=, never inserted -- the ACTUAL presentation screen (queried
    // fresh after placement/fullscreen settles), never the possibly-wrong screen the window
    // started on.
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(
        "presentation_screen=\\\"%10\\\" presentation_physical=%11x%12")));
}

TEST(GuiSmokeDisplaySelectWiring, FullscreenRequestLineCarriesTheTargetScreenName)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int lineAt = source.indexOf(QStringLiteral(
        "gui_smoke.fullscreen_request requested=1 verified=%1 screen=%2x%3 "));
    ASSERT_TRUE(lineAt >= 0);
    const QString tail = source.mid(lineAt, 900);
    ASSERT_TRUE(tail.contains(QStringLiteral("target_screen=\\\"%9\\\"")));
    // UM-DISPLAY-SELECT-AND-LOG-1 round 1c (sol pre-review BLOCKER 3): target_screen is the
    // CHOSEN target's own name (never the possibly-wrong screen the window actually ended up
    // on), while presentation_screen/presentation_physical -- appended after it, never
    // inserted -- report the screen actually verified against on every settle pass.
    ASSERT_TRUE(tail.contains(QStringLiteral(
        "presentation_screen=\\\"%10\\\" presentation_physical=%11x%12")));
    ASSERT_TRUE(tail.contains(QStringLiteral(".arg( target->name() )")));
    ASSERT_TRUE(tail.contains(QStringLiteral(
        ".arg( presentationScreen ? presentationScreen->name() : QStringLiteral(\"none\") )")));
}
