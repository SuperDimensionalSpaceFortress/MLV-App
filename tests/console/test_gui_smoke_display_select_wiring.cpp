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
    // UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1: the derived GDI device rides on the same line, so the
    // recorded log always says which \\.\DISPLAYn the app believes each QScreen is (empty = unknown).
    ASSERT_TRUE(body.contains(QStringLiteral("primary=%14 device=\\\"%15\\\"")));
    ASSERT_TRUE(body.contains(QStringLiteral(".arg( playbackSmokeScreenGdiDevice( s ) );")));
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
    // The rules moved to DisplayDeviceMapping.h (UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1) so the
    // recorded UM values can drive them as real unit tests; MainWindow.cpp only feeds it the screen.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int fnAt = source.indexOf(QStringLiteral(
        "static QString playbackSmokeDisplayPreferenceMatchedField("));
    ASSERT_TRUE(fnAt >= 0);
    const QString tail = source.mid(fnAt, 900);
    ASSERT_TRUE(tail.contains(QStringLiteral("DisplayDeviceMapping::preferenceMatchedField(")));
    ASSERT_TRUE(tail.contains(QStringLiteral("screen->name(), screen->model(), screen->manufacturer(),")));

    const QString mapping = readRepoFile(QStringLiteral("platform/qt/DisplayDeviceMapping.h"));
    ASSERT_TRUE(mapping.contains(QStringLiteral("screenName.contains( prefer, Qt::CaseInsensitive )")));
    ASSERT_TRUE(mapping.contains(QStringLiteral("model.contains( prefer, Qt::CaseInsensitive )")));
    ASSERT_TRUE(mapping.contains(QStringLiteral("manufacturer.contains( prefer, Qt::CaseInsensitive )")));
}

// UM-DISPLAY-SELECT-AND-LOG-1 round 2 (sol PRE-REVIEW #2 BLOCKER a). The job resolves the
// preferred Windows monitor name ('ASUS PA329C') to its GDI device name and hands the app THAT.
// A device name is matched for EXACT equality first -- a substring match would make
// \\.\DISPLAY1 also select \\.\DISPLAY10 -- and reported as its own matched field.
// UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1: it is compared with the GDI device DERIVED for the
// screen (native origin + physical size), because QScreen::name() is the EDID friendly name on a
// monitor that has one (measured on UM). MUTATION CAUGHT: passing screen->name() as the only
// candidate again (the preference then matches nothing on that topology); deleting the exact
// branch; letting a device preference fall through to the substring fields. The behaviour itself
// is pinned by test_display_device_mapping.cpp; this pins the wiring that feeds it.
TEST(GuiSmokeDisplaySelectWiring, PreferredDeviceNameIsMatchedExactlyAgainstTheDerivedGdiDevice)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int fnAt = source.indexOf(QStringLiteral(
        "static QString playbackSmokeDisplayPreferenceMatchedField("));
    ASSERT_TRUE(fnAt >= 0);
    const QString tail = source.mid(fnAt, 900);
    ASSERT_TRUE(tail.contains(QStringLiteral(
        "preferSubstring.startsWith( QStringLiteral( \"\\\\\\\\.\\\\\" ) ) ? playbackSmokeScreenGdiDevice( screen ) : QString(),")));

    const QString mapping = readRepoFile(QStringLiteral("platform/qt/DisplayDeviceMapping.h"));
    const int deviceBranchAt = mapping.indexOf(QStringLiteral("prefer.startsWith( QStringLiteral( \"\\\\\\\\.\\\\\" ) )"));
    const int derivedAt = mapping.indexOf(QStringLiteral(
        "gdiDevice.compare( prefer, Qt::CaseInsensitive ) == 0"), deviceBranchAt);
    const int exactAt = mapping.indexOf(QStringLiteral(
        "screenName.compare( prefer, Qt::CaseInsensitive ) == 0"), deviceBranchAt);
    const int substringAt = mapping.indexOf(QStringLiteral("screenName.contains( prefer, Qt::CaseInsensitive )"));
    ASSERT_TRUE(deviceBranchAt >= 0);
    ASSERT_TRUE(derivedAt > deviceBranchAt);
    ASSERT_TRUE(exactAt > derivedAt);
    ASSERT_TRUE(mapping.contains(QStringLiteral("QStringLiteral(\"device_name\")")));
    // A device-name preference NEVER falls through to the substring fields.
    ASSERT_TRUE(substringAt > exactAt);
    ASSERT_TRUE(mapping.mid(deviceBranchAt, substringAt - deviceBranchAt).contains(QStringLiteral("return QString();")));
}

// UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1: the derivation feeds the mapping the screen's NATIVE origin
// (geometry().topLeft() -- Qt divides only the SIZE by the DPR) and PHYSICAL size, against the Win32
// monitor list; a Qt origin scaled by dpr, or a logical size, would never match a GDI rect on a
// scaled desktop (UM runs at 150%).
TEST(GuiSmokeDisplaySelectWiring, TheGdiDeviceIsDerivedFromNativeOriginAndPhysicalSizeAgainstEnumDisplayMonitors)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("static QString playbackSmokeScreenGdiDevice( QScreen *screen )"),
        QStringLiteral("static QString playbackSmokeDisplayPreferenceMatchedField("));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("DisplayDeviceMapping::gdiDeviceForScreen(")));
    ASSERT_TRUE(body.contains(QStringLiteral("geo.topLeft(),")));
    ASSERT_TRUE(body.contains(QStringLiteral("QSize( qRound( geo.width() * dpr ), qRound( geo.height() * dpr ) ),")));
    ASSERT_TRUE(body.contains(QStringLiteral("DisplayDeviceMapping::enumerateGdiMonitors()")));

    const QString mapping = readRepoFile(QStringLiteral("platform/qt/DisplayDeviceMapping.h"));
    ASSERT_TRUE(mapping.contains(QStringLiteral("EnumDisplayMonitors(")));
    ASSERT_TRUE(mapping.contains(QStringLiteral("GetMonitorInfoW(")));
    // Uniqueness: a cloned pair (two monitors on one rect) is UNKNOWN, never the first hit.
    ASSERT_TRUE(mapping.contains(QStringLiteral("return matches == 1 ? found : QString();")));
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

TEST(GuiSmokeDisplaySelectWiring, PostPlacementForegroundRequestKeepsTheWindowedMaximize)
{
    // CUDA-PERF-DISPLAY-MODE-AB-1: the SECOND forcePlaybackSmokeWindowForeground() call runs
    // after placement, so on a --windowed leg the window is maximized by then. resizeEvent()
    // stops Play on every resize, so a showNormal()/SW_SHOWNORMAL there un-maximized the
    // window and ended the measured Play ~40 ms after the trigger (bachelor, 0 frames).
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString resizeBody = functionBody(source,
        QStringLiteral("void MainWindow::resizeEvent(QResizeEvent *event)"),
        QStringLiteral("void MainWindow::changeEvent( QEvent *event )"));
    ASSERT_FALSE(resizeBody.isEmpty());
    ASSERT_TRUE(resizeBody.contains(QStringLiteral("ui->actionPlay->setChecked( false );")));

    const QString body = functionBody(source,
        QStringLiteral("void MainWindow::forcePlaybackSmokeWindowForeground( void )"),
        QStringLiteral("bool MainWindow::enterPlaybackSmokeFullscreen( QScreen *target )"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral(
        "const bool keepWindowState = wasFullScreen || isMaximized();")));
    ASSERT_TRUE(body.contains(QStringLiteral("if( !keepWindowState ) showNormal();")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "ShowWindow( target, keepWindowState ? SW_SHOW : SW_SHOWNORMAL );")));
    ASSERT_FALSE(body.contains(QStringLiteral("if( !wasFullScreen ) showNormal();")));
    ASSERT_FALSE(body.contains(QStringLiteral("wasFullScreen ? SW_SHOW : SW_SHOWNORMAL")));
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

TEST(GuiSmokeDisplaySelectWiring, TheDisplayPreferFeatureProbeArgumentsExitWithoutStartingTheGui)
{
    // UM-DISPLAY-SELECT-AND-LOG-1 round 4 (fable BLOCKER 1). The runner's --display-prefer probe
    // (run-release-gui-smoke.ps1, Test-GuiSmokeDisplayPreferSupport) runs `MLVApp.exe
    // --gui-smoke-playback --help`. That is only a safe probe because of three facts about
    // main.cpp, pinned here so a refactor cannot silently turn the probe back into a GUI launch
    // that never exits (the round-1c probe was a bare `--help`, which is normal GUI mode):
    //   (1) --gui-smoke-playback routes to runGuiPlaybackSmoke, never to MainWindow;
    //   (2) that sub-parser registers `--help` as its own option, so process() does not exit
    //       and does not need an --input;
    //   (3) the help handler prints the parser's help text and returns 0 BEFORE the "--input is
    //       required" check and before anything that could create a window.
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));

    const int smokeAt = source.indexOf(QStringLiteral("static int runGuiPlaybackSmoke(QApplication &app)"));
    ASSERT_TRUE(smokeAt >= 0);
    const QString smoke = source.mid(smokeAt);

    const int helpRegAt = smoke.indexOf(QStringLiteral("QCommandLineOption helpOpt("));
    ASSERT_TRUE(helpRegAt >= 0);
    ASSERT_TRUE(smoke.mid(helpRegAt, 200).contains(QStringLiteral("QStringLiteral(\"help\")")));

    const int helpUseAt = smoke.indexOf(QStringLiteral("if (parser.isSet(helpOpt))"));
    ASSERT_TRUE(helpUseAt > helpRegAt);
    const QString helpBlock = smoke.mid(helpUseAt, 160);
    ASSERT_TRUE(helpBlock.contains(QStringLiteral("parser.helpText()")));
    ASSERT_TRUE(helpBlock.contains(QStringLiteral("return 0;")));

    const int inputRequiredAt = smoke.indexOf(QStringLiteral("--input is required."));
    ASSERT_TRUE(inputRequiredAt > helpUseAt);

    // Nothing that builds a window sits between the start of runGuiPlaybackSmoke and its help
    // handler.
    const QString beforeHelp = smoke.left(helpUseAt);
    ASSERT_FALSE(beforeHelp.contains(QStringLiteral("MainWindow ")));
    ASSERT_FALSE(beforeHelp.contains(QStringLiteral(".show()")));
    ASSERT_FALSE(beforeHelp.contains(QStringLiteral("a.exec()")));

    // (1) the routing: the smoke flag excludes normal GUI mode and dispatches to the smoke
    // function; a bare --help (no smoke flag) is normal GUI mode -- the reason the probe must
    // carry --gui-smoke-playback.
    ASSERT_TRUE(source.contains(QStringLiteral(
        "!batch && !trim_mlv && !profile_playback && !gui_playback_smoke;")));
    ASSERT_TRUE(source.contains(QStringLiteral("return runGuiPlaybackSmoke(a);")));
    ASSERT_TRUE(source.contains(QStringLiteral("/* Normal GUI mode")));
}
