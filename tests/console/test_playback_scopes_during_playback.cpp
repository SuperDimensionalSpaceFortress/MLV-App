// PLAYBACK-SCOPES-OPTION-1: the "Scopes During Playback" setting (Hide / Throttled / Live), its
// MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK override, the throttle interval per mode, and the policy
// helper that turns a visible scope off DURING PLAYBACK ONLY in Hide mode. MainWindow is GUI code not
// linked into console_tests, so its wiring is pinned as source text.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackScopesDuringPlayback.h"

#include <QDir>
#include <QFile>
#include <QSettings>
#include <QString>
#include <QStringList>
#include <QTextStream>
#include <QUuid>
#include <QVariant>

#include <memory>

namespace
{

const char kScopesEnv[] = "MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK";

QStringList &capturedLog()
{
    static QStringList lines;
    return lines;
}

void captureLog(QtMsgType, const QMessageLogContext &, const QString &message)
{
    capturedLog() << message;
}

// PSO-01: one launch's environment, settings store and log, restored however the test ends. Starts as an ordinary
// launch (native store, no automation run) with the setting absent; enterAutomationRun() is main.cpp's automation
// path (a fresh run-scoped store, so the setting is absent there too).
class ScopedScopesLaunch
{
public:
    ScopedScopesLaunch()
        : m_envWasSet(qEnvironmentVariableIsSet(kScopesEnv))
        , m_env(qgetenv(kScopesEnv))
    {
        automation_settings::releaseForTest();
        std::unique_ptr<QSettings> store = automation_settings::openAppSettings();
        m_saved = store->value(PlaybackScopesSettings::kKeyScopesDuringPlayback());
        store->remove(PlaybackScopesSettings::kKeyScopesDuringPlayback());
        store->sync();
        capturedLog().clear();
        m_previousHandler = qInstallMessageHandler(captureLog);
    }

    ~ScopedScopesLaunch()
    {
        qInstallMessageHandler(m_previousHandler);
        automation_settings::releaseForTest();
        if (!m_runDir.isEmpty()) QDir(m_runDir).removeRecursively();
        std::unique_ptr<QSettings> store = automation_settings::openAppSettings();
        if (m_saved.isValid()) store->setValue(PlaybackScopesSettings::kKeyScopesDuringPlayback(), m_saved);
        else store->remove(PlaybackScopesSettings::kKeyScopesDuringPlayback());
        store->sync();
        if (m_envWasSet) qputenv(kScopesEnv, m_env);
        else qunsetenv(kScopesEnv);
    }

    void enterAutomationRun()
    {
        m_runDir = QDir::tempPath() + QStringLiteral("/mlvapp-scopes-pso01-")
                 + QUuid::createUuid().toString(QUuid::WithoutBraces);
        ASSERT_FALSE(automation_settings::isolate(m_runDir).isEmpty());
        ASSERT_TRUE(automation_settings::isolated());
    }

private:
    bool m_envWasSet = false;
    QByteArray m_env;
    QVariant m_saved;
    QString m_runDir;
    QtMessageHandler m_previousHandler = nullptr;
};

int overrideDisclosures()
{
    int count = 0;
    for (const QString &line : capturedLog())
        if (line.contains(QLatin1String(kScopesEnv))) ++count;
    return count;
}

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

int asInt(PlaybackScopesMode mode) { return static_cast<int>(mode); }

} // namespace

TEST(PlaybackScopesDuringPlayback, TokensParseAndAnythingElseIsNoMode)
{
    ASSERT_EQ(asInt(PlaybackScopesMode::Hide), playbackScopesModeFromToken(QByteArrayLiteral("hide")));
    ASSERT_EQ(asInt(PlaybackScopesMode::Throttled), playbackScopesModeFromToken(QByteArrayLiteral(" Throttled ")));
    ASSERT_EQ(asInt(PlaybackScopesMode::Live), playbackScopesModeFromToken(QByteArrayLiteral("LIVE")));
    ASSERT_EQ(-1, playbackScopesModeFromToken(QByteArray()));
    ASSERT_EQ(-1, playbackScopesModeFromToken(QByteArrayLiteral("off")));
    ASSERT_EQ(-1, playbackScopesModeFromToken(QByteArrayLiteral("1")));
    for (PlaybackScopesMode mode : { PlaybackScopesMode::Hide, PlaybackScopesMode::Throttled, PlaybackScopesMode::Live })
        ASSERT_EQ(asInt(mode), playbackScopesModeFromToken(QByteArray(playbackScopesModeToken(mode))));
}

TEST(PlaybackScopesDuringPlayback, EnvOverridesTheSettingAndNothingFallsBackToTheDefault)
{
    const int hide = asInt(PlaybackScopesMode::Hide);
    const int live = asInt(PlaybackScopesMode::Live);
    ASSERT_EQ(asInt(PlaybackScopesMode::Hide), asInt(playbackScopesModeResolve(hide, live)));
    ASSERT_EQ(asInt(PlaybackScopesMode::Live), asInt(playbackScopesModeResolve(-1, live)));
    ASSERT_EQ(asInt(PlaybackScopesSettings::kDefaultMode()), asInt(playbackScopesModeResolve(-1, -1)));
}

TEST(PlaybackScopesDuringPlayback, TheDefaultIsHideWhilePlaying)
{
    // PLAYBACK-SCOPES-OPTION-1 r1 Bachelor A/B: a throttled histogram cost about half the presented fps on
    // the GPU route (texture-present vetoed), far over the ~5 % Throttled bar, so the default is Hide.
    ASSERT_EQ(asInt(PlaybackScopesMode::Hide), asInt(PlaybackScopesSettings::kDefaultMode()));
    ASSERT_EQ(asInt(PlaybackScopesMode::Hide), asInt(playbackScopesModeResolve(-1, -1)));
}

TEST(PlaybackScopesDuringPlayback, HideTurnsAVisibleScopeOffOnlyWhilePlaying)
{
    for (PlaybackScopesMode mode : { PlaybackScopesMode::Hide, PlaybackScopesMode::Throttled, PlaybackScopesMode::Live })
    {
        // A hidden edit area never shows a scope, playing or not.
        ASSERT_FALSE(playbackScopeDisplayConsumesPresentedPixels(false, false, mode));
        ASSERT_FALSE(playbackScopeDisplayConsumesPresentedPixels(false, true, mode));
        // Paused or stopped, a visible scope always draws (and keeps vetoing the GPU fast paths).
        ASSERT_TRUE(playbackScopeDisplayConsumesPresentedPixels(true, false, mode));
    }
    ASSERT_FALSE(playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Hide));
    ASSERT_TRUE(playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Throttled));
    ASSERT_TRUE(playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Live));
}

TEST(PlaybackScopesDuringPlayback, ThrottledKeepsTheForkThrottleAndLiveIsEveryFrame)
{
    // Throttled: max(base, 1.4 x render), capped at 500; no render time -> the base.
    ASSERT_EQ(150, playbackScopeUpdateIntervalForMode(PlaybackScopesMode::Throttled, 150, 0.0));
    ASSERT_EQ(150, playbackScopeUpdateIntervalForMode(PlaybackScopesMode::Throttled, 150, 50.0));
    ASSERT_EQ(280, playbackScopeUpdateIntervalForMode(PlaybackScopesMode::Throttled, 150, 200.0));
    ASSERT_EQ(500, playbackScopeUpdateIntervalForMode(PlaybackScopesMode::Throttled, 150, 1000.0));
    ASSERT_EQ(14, playbackScopeUpdateIntervalForMode(PlaybackScopesMode::Throttled, 0, 10.0));
    ASSERT_EQ(0, playbackScopeUpdateIntervalForMode(PlaybackScopesMode::Throttled, 0, 0.0));
    // Live ignores the base and the render time: every presented frame.
    ASSERT_EQ(0, playbackScopeUpdateIntervalForMode(PlaybackScopesMode::Live, 150, 200.0));
    ASSERT_EQ(0, playbackScopeUpdateIntervalForMode(PlaybackScopesMode::Live, 1000, 0.0));
}

TEST(PlaybackScopesDuringPlayback, TheSettingPersistsAsItsToken)
{
    std::unique_ptr<QSettings> store = automation_settings::openAppSettings();
    const QVariant saved = store->value(PlaybackScopesSettings::kKeyScopesDuringPlayback());
    store->remove(PlaybackScopesSettings::kKeyScopesDuringPlayback());
    store->sync();
    ASSERT_EQ(-1, playbackScopesModeSettingValue());

    playbackScopesModeWriteToSettings(PlaybackScopesMode::Hide);
    ASSERT_TRUE(QStringLiteral("hide") == automation_settings::openAppSettings()->value(
        PlaybackScopesSettings::kKeyScopesDuringPlayback()).toString());
    ASSERT_EQ(asInt(PlaybackScopesMode::Hide), playbackScopesModeSettingValue());
    playbackScopesModeWriteToSettings(PlaybackScopesMode::Live);
    ASSERT_EQ(asInt(PlaybackScopesMode::Live), playbackScopesModeSettingValue());

    std::unique_ptr<QSettings> restore = automation_settings::openAppSettings();
    if (saved.isValid()) restore->setValue(PlaybackScopesSettings::kKeyScopesDuringPlayback(), saved);
    else restore->remove(PlaybackScopesSettings::kKeyScopesDuringPlayback());
    restore->sync();
}

// PSO-01 (sol r3): an ordinary launch with the setting absent and an inherited MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK=live
// keeps Hide. The override is a measurement input, honoured only in an automation run, and then disclosed on one line.
// Drives the real resolution path (playbackScopesMode(), which MainWindow's startup applies).
TEST(PlaybackScopesDuringPlayback, PSO01_TheMeasurementOverrideIsHonouredOnlyInAnAutomationRun)
{
    ScopedScopesLaunch launch;
    qputenv(kScopesEnv, QByteArrayLiteral("live"));

    ASSERT_FALSE(automation_settings::isolated());
    ASSERT_EQ(asInt(PlaybackScopesMode::Hide), asInt(playbackScopesMode()));
    ASSERT_EQ(0, overrideDisclosures());
    // Gate off, the stored setting still wins over the inherited override.
    playbackScopesModeWriteToSettings(PlaybackScopesMode::Throttled);
    ASSERT_EQ(asInt(PlaybackScopesMode::Throttled), asInt(playbackScopesMode()));
    ASSERT_EQ(0, overrideDisclosures());

    launch.enterAutomationRun();
    ASSERT_EQ(asInt(PlaybackScopesMode::Live), asInt(playbackScopesMode()));
    ASSERT_EQ(1, overrideDisclosures());
    ASSERT_TRUE(capturedLog().last().contains(QStringLiteral("= live")));
    // Gate on, the override beats a stored setting, and with no override the setting is read (composition pin).
    playbackScopesModeWriteToSettings(PlaybackScopesMode::Hide);
    ASSERT_EQ(asInt(PlaybackScopesMode::Live), asInt(playbackScopesMode()));
    qunsetenv(kScopesEnv);
    playbackScopesModeWriteToSettings(PlaybackScopesMode::Throttled);
    ASSERT_EQ(asInt(PlaybackScopesMode::Throttled), asInt(playbackScopesMode()));
    ASSERT_EQ(2, overrideDisclosures());
}

// PSO-02 (sol r3): a frame requested under a no-scope policy on a GPU surface (which may have skipped its RGB8 on GPU
// Tex NR) reaches finishPresentedFrame after Hide -> Live or Throttled: no scope is computed from it. The next frame,
// requested under the new policy, feeds the scope.
TEST(PlaybackScopesDuringPlayback, PSO02_AFrameRequestedUnderTheNoScopePolicyNeverFeedsTheScope)
{
    for (PlaybackScopesMode mode : { PlaybackScopesMode::Throttled, PlaybackScopesMode::Live })
    {
        const bool consumesNow = playbackScopeDisplayConsumesPresentedPixels(true, true, mode);
        ASSERT_TRUE(consumesNow);
        // Requested under Hide while playing: no scope consumer, GPU route allowed -> not measured.
        ASSERT_FALSE(playbackScopeFrameFeedsScope(consumesNow, /*allowedRouteWithoutRgb8*/true));
        // Requested under the new mode: the scope vetoes the RGB8-less routes -> measured.
        ASSERT_TRUE(playbackScopeFrameFeedsScope(consumesNow, /*allowedRouteWithoutRgb8*/false));
    }
    // Hide while playing still computes nothing, whatever the frame.
    ASSERT_FALSE(playbackScopeFrameFeedsScope(
        playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Hide), false));

    // MainWindow's scope draw asks with the policy the frame was REQUESTED under, not only the current mode.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_EQ(1, source.count(QStringLiteral(
        "if( playbackScopeFrameFeedsScope(\n"
        "            scopeDisplayConsumesPresentedPixels( ui->actionShowEditArea->isChecked() ),\n"
        "            mainWindowAllowsGpu16PreviewRender( requestContext.gpuPreviewPolicy ) ) )")));
}

TEST(PlaybackScopesDuringPlayback, MainWindowRoutesEveryScopeConsumerThroughThePolicyHelper)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    // No scope consumer may read the bare edit-area visibility any more.
    ASSERT_FALSE(source.contains(QStringLiteral("const bool scopeDisplayVisible = ui->dockWidgetEdit->isVisible();")));
    ASSERT_FALSE(source.contains(QStringLiteral("includeVisibleScopes && ui->dockWidgetEdit->isVisible();")));
    ASSERT_EQ(7, source.count(QStringLiteral("scopeDisplayConsumesPresentedPixels( ui->dockWidgetEdit->isVisible() )")));
    // The early slot release and the scope draw itself.
    ASSERT_EQ(2, source.count(QStringLiteral("scopeDisplayConsumesPresentedPixels( ui->actionShowEditArea->isChecked() )")));
    // PLAYBACK-GPU-SCOPES-NO-VETO-1 (PGT-07): the funnel now also passes the GPU read-back's answer (4th argument).
    ASSERT_TRUE(source.contains(QStringLiteral(
        "editAreaVisible, ui->actionPlay->isChecked(), m_playbackScopesMode,\n"
        "        playbackScopeGpuTapServesNow( editAreaVisible ) );")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "playbackScopeUpdateIntervalForMode( m_playbackScopesMode,")));
    ASSERT_TRUE(source.contains(QStringLiteral("requestFrameRefresh( false, \"play-stop-scopes\" );")));
    ASSERT_TRUE(source.contains(QStringLiteral("applyPlaybackScopesMode( playbackScopesMode(), /*persist*/false );")));
    // The GUI smoke's playback-policy line says which mode the run played under.
    ASSERT_TRUE(source.contains(QStringLiteral("drop_frame=%12 scopes_during_playback=%13")));
    ASSERT_TRUE(source.contains(QStringLiteral(".arg( QString::fromLatin1( playbackScopesModeToken( m_playbackScopesMode ) ) ) );")));

    const QString ui = readRepoFile(QStringLiteral("platform/qt/MainWindow.ui"));
    for (const char *action : { "actionPlaybackScopesHide", "actionPlaybackScopesThrottled", "actionPlaybackScopesLive" })
    {
        ASSERT_TRUE(ui.contains(QStringLiteral("<action name=\"%1\">").arg(QLatin1String(action))));
        ASSERT_TRUE(ui.contains(QStringLiteral("<addaction name=\"%1\"/>").arg(QLatin1String(action))));
    }
}

// ---- PLAYBACK-GPU-SCOPES-NO-VETO-1: the Throttled scope fed by the GL viewport's read-back of the drawn picture ----

namespace
{

PlaybackScopeGpuTapInputs servingTapInputs()
{
    PlaybackScopeGpuTapInputs in;
    in.killSwitch = false;
    in.mode = PlaybackScopesMode::Throttled;
    in.playbackActive = true;
    in.editAreaVisible = true;
    in.anyScopeChecked = true;
    in.gpuWidgetViewportInstalled = true;
    in.gpuDisplayWindowActive = false;
    in.zebrasEnabled = false;
    in.tapSupported = true;
    in.tapRectCapturable = true;
    in.textureRouteEligibleIgnoringScopes = true;
    return in;
}

// The body of one MainWindow member, from its definition line to the first closing brace at column 0.
QString memberBody(const QString &source, const QString &signature)
{
    const int start = source.indexOf(signature);
    if (start < 0) return QString();
    const int end = source.indexOf(QStringLiteral("\n}\n"), start);
    return end < 0 ? QString() : source.mid(start, end - start + 2);
}

} // namespace

TEST(PlaybackScopesDuringPlayback, PGT01_TheReadBackServesOnlyWhenEveryInputAllowsIt)
{
    ASSERT_TRUE(playbackScopeGpuTapServes(servingTapInputs()));
    // Every input flipped alone turns it off.
    PlaybackScopeGpuTapInputs in = servingTapInputs(); in.killSwitch = true; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.mode = PlaybackScopesMode::Hide; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.mode = PlaybackScopesMode::Live; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.playbackActive = false; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.editAreaVisible = false; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.anyScopeChecked = false; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.zebrasEnabled = true; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.tapSupported = false; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    // r2 ruling (the r1 exclusion was scope, not safety): GpuDisplayWindow active with a supported tap serves, with or
    // without the QOpenGLWidget viewport installed; NEITHER GL surface does not serve.
    in = servingTapInputs(); in.gpuDisplayWindowActive = true; ASSERT_TRUE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.gpuWidgetViewportInstalled = false; in.gpuDisplayWindowActive = true;
    ASSERT_TRUE(playbackScopeGpuTapServes(in));
    in.tapSupported = false; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.gpuWidgetViewportInstalled = false; in.gpuDisplayWindowActive = false;
    ASSERT_FALSE(playbackScopeGpuTapServes(in));
    // r2 (CoS #447 blocker 5, PGT-09): a picture the read-back cannot capture (zoomed past fit) is not served.
    in = servingTapInputs(); in.tapRectCapturable = false; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    in = servingTapInputs(); in.textureRouteEligibleIgnoringScopes = false; ASSERT_FALSE(playbackScopeGpuTapServes(in));
    // The default-constructed inputs serve nothing.
    ASSERT_FALSE(playbackScopeGpuTapServes(PlaybackScopeGpuTapInputs()));
}

TEST(PlaybackScopesDuringPlayback, PGT02_AScopeTheReadBackServesConsumesNoPresentedPixelsWhilePlaying)
{
    // Visible, playing, Throttled, served by the read-back: no presented-pixel consumer, so no GPU-route veto.
    ASSERT_FALSE(playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Throttled, true));
    // Not served: today's Throttled scope still consumes.
    ASSERT_TRUE(playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Throttled, false));
    // Hide is unchanged with or without the read-back.
    ASSERT_FALSE(playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Hide, false));
    ASSERT_FALSE(playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Hide, true));
    // A paused or stopped frame always draws its scope, whatever the read-back says.
    for (PlaybackScopesMode mode : { PlaybackScopesMode::Hide, PlaybackScopesMode::Throttled, PlaybackScopesMode::Live })
    {
        ASSERT_TRUE(playbackScopeDisplayConsumesPresentedPixels(true, false, mode, true));
        ASSERT_TRUE(playbackScopeDisplayConsumesPresentedPixels(true, false, mode, false));
        ASSERT_FALSE(playbackScopeDisplayConsumesPresentedPixels(false, true, mode, true));
    }
    // Composition with the real gate: Live is never served (PGT-01), so a Live scope keeps consuming.
    PlaybackScopeGpuTapInputs live = servingTapInputs();
    live.mode = PlaybackScopesMode::Live;
    ASSERT_TRUE(playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Live, playbackScopeGpuTapServes(live)));
    ASSERT_FALSE(playbackScopeDisplayConsumesPresentedPixels(
        true, true, PlaybackScopesMode::Throttled, playbackScopeGpuTapServes(servingTapInputs())));
    // The 3-argument form every existing caller and test uses is the 4th argument false.
    ASSERT_TRUE(playbackScopeDisplayConsumesPresentedPixels(true, true, PlaybackScopesMode::Throttled));
}

TEST(PlaybackScopesDuringPlayback, PGT03_TheTapSizeKeepsTheAspectAndTheRectMustBeInsideTheFramebuffer)
{
    ASSERT_TRUE(QSize(512, 326) == playbackScopeTapSize(2158, 1373));          // the owner-shape viewport, capped at 512
    ASSERT_TRUE(QSize(512, 288) == playbackScopeTapSize(1920, 1080, 512));
    ASSERT_TRUE(QSize(300, 200) == playbackScopeTapSize(300, 200, 512));        // never upscaled
    ASSERT_TRUE(QSize(512, 1) == playbackScopeTapSize(4000, 1, 512));           // each side at least 1
    ASSERT_TRUE(QSize(1, 1) == playbackScopeTapSize(0, 100, 512));
    ASSERT_TRUE(QSize(1, 1) == playbackScopeTapSize(100, 100, 0));

    QRect gl;
    // A fitted image inside a 2158x1373 framebuffer: GL rows are bottom-up.
    ASSERT_TRUE(playbackScopeTapFramebufferRect(10.0, 20.0, 2148.0, 1353.0, 2158, 1373, &gl));
    ASSERT_TRUE(QRect(10, 20, 2138, 1333) == gl);
    ASSERT_TRUE(playbackScopeTapFramebufferRect(0.0, 0.0, 2158.0, 1373.0, 2158, 1373, &gl));
    ASSERT_TRUE(QRect(0, 0, 2158, 1373) == gl);
    ASSERT_TRUE(playbackScopeTapFramebufferRect(0.0, 100.0, 2158.0, 1273.0, 2158, 1373, &gl));
    ASSERT_TRUE(QRect(0, 100, 2158, 1173) == gl);
    // Zoomed past fit (any edge outside), or empty: skip.
    ASSERT_FALSE(playbackScopeTapFramebufferRect(-50.0, 0.0, 2158.0, 1373.0, 2158, 1373, &gl));
    ASSERT_FALSE(playbackScopeTapFramebufferRect(0.0, -50.0, 2158.0, 1373.0, 2158, 1373, &gl));
    ASSERT_FALSE(playbackScopeTapFramebufferRect(0.0, 0.0, 2200.0, 1373.0, 2158, 1373, &gl));
    ASSERT_FALSE(playbackScopeTapFramebufferRect(0.0, 0.0, 2158.0, 1400.0, 2158, 1373, &gl));
    ASSERT_FALSE(playbackScopeTapFramebufferRect(10.0, 10.0, 10.2, 500.0, 2158, 1373, &gl));
    ASSERT_FALSE(playbackScopeTapFramebufferRect(0.0, 0.0, 10.0, 10.0, 0, 0, &gl));
}

// PGT-04 (r2, CoS #447 blocker 1): a PARITY test, not a rule of the read-back's own. The same RGB8 picture and zebra
// state through the CPU feed (the frame's underOver -- 0 with zebras off, the zebra scan with them on -- decoded by the
// draw gate) and through the read-back feed give identical clip flags and the same scope choice.
TEST(PlaybackScopesDuringPlayback, PGT04_TheReadBackFeedPassesTheCpuFeedsClipFlagsAndScope)
{
    // 2x2, glReadPixels order: row 0 is the BOTTOM of the picture. Alpha is dropped.
    const uint8_t rgba[] = {
        10, 11, 12, 99,   20, 21, 22, 99,     // bottom row
        30, 31, 32, 99,   40, 41, 42, 99 };   // top row
    std::vector<uint8_t> rgb8;
    playbackScopeTapRgbaBottomUpToRgb8(rgba, 2, 2, &rgb8);
    const std::vector<uint8_t> expected = { 30, 31, 32, 40, 41, 42, 10, 11, 12, 20, 21, 22 };
    ASSERT_TRUE(expected == rgb8);

    // Pictures: one black channel (r1 flagged it under), one 255 channel (r1 flagged it over), deep black and
    // blown white (the zebra rule's under and over), and a mid grey.
    const std::vector<std::vector<uint8_t>> pictures = {
        { 0, 128, 128,   128, 128, 128 },
        { 255, 128, 128,   128, 128, 128 },
        { 0, 0, 2,   255, 255, 254 },
        { 128, 128, 128,   100, 110, 120 },
    };
    for (const std::vector<uint8_t> &picture : pictures)
    {
        for (bool zebras : { false, true })
        {
            // CPU feed: the frame's underOver as the render prep sets it, decoded as the draw gate does.
            const uint8_t cpuUnderOver = zebras ? playbackScopeZebraScanRgb8(picture.data(), 2, 1) : uint8_t(0);
            bool cpuUnder = true;
            bool cpuOver = true;
            playbackScopeUnderOverFlags(cpuUnderOver, &cpuUnder, &cpuOver);
            // Read-back feed.
            bool tapUnder = true;
            bool tapOver = true;
            playbackScopeFeedUnderOver(zebras, picture.data(), 2, 1, &tapUnder, &tapOver);
            ASSERT_EQ(cpuUnder, tapUnder);
            ASSERT_EQ(cpuOver, tapOver);
            // Zebras off (the only state the read-back serves): no clip markers, whatever the picture.
            if (!zebras)
            {
                ASSERT_FALSE(tapUnder);
                ASSERT_FALSE(tapOver);
            }
        }
    }
    // The zebra rule itself is unchanged: HSL lightness against preview_zebra's thresholds.
    const uint8_t deep[] = { 0, 0, 2,   255, 255, 254 };
    ASSERT_EQ(3, static_cast<int>(playbackScopeZebraScanRgb8(deep, 2, 1)));
    const uint8_t oneBlackChannel[] = { 0, 128, 128 };
    ASSERT_EQ(0, static_cast<int>(playbackScopeZebraScanRgb8(oneBlackChannel, 1, 1)));

    // Scope choice: both feeds ask the same helper, in the draw gate's order.
    ASSERT_TRUE(PlaybackScopeKind::Histogram == playbackScopeKindForChecks(true, true, true, true));
    ASSERT_TRUE(PlaybackScopeKind::WaveForm == playbackScopeKindForChecks(false, true, true, true));
    ASSERT_TRUE(PlaybackScopeKind::RgbParade == playbackScopeKindForChecks(false, false, true, true));
    ASSERT_TRUE(PlaybackScopeKind::VectorScope == playbackScopeKindForChecks(false, false, false, true));
    ASSERT_TRUE(PlaybackScopeKind::None == playbackScopeKindForChecks(false, false, false, false));

    // MainWindow: both feeds take the scope choice and the clip flags from these helpers, and the CPU zebra scan is the
    // shared rule.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_EQ(2, source.count(QStringLiteral("playbackScopeKindForChecks(")));
    ASSERT_TRUE(source.contains(QStringLiteral("scopesLabelTypeForPlaybackScopeKind( tapScopeKind )")));
    ASSERT_TRUE(source.contains(QStringLiteral("scopesLabelTypeForPlaybackScopeKind( scopeKind )")));
    ASSERT_TRUE(source.contains(QStringLiteral("playbackScopeUnderOverFlags( underOver, &under, &over );")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "playbackScopeFeedUnderOver( ui->actionShowZebras->isChecked(), m_playbackScopeTapRgb8.data(),")));
    ASSERT_TRUE(source.contains(QStringLiteral("return playbackScopeZebraScanRgb8( rgbData, width, height );")));
}

TEST(PlaybackScopesDuringPlayback, PGT05_TheKillSwitchIsExactlyZero)
{
    ASSERT_TRUE(QByteArray(playbackScopeGpuTapKillSwitchName()) == QByteArrayLiteral("MLVAPP_PLAYBACK_SCOPE_GPU_TAP"));
    ASSERT_TRUE(playbackScopeGpuTapKillSwitch(QByteArrayLiteral("0")));
    ASSERT_TRUE(playbackScopeGpuTapKillSwitch(QByteArrayLiteral(" 0 ")));
    ASSERT_FALSE(playbackScopeGpuTapKillSwitch(QByteArray()));           // unset: the read-back is on
    ASSERT_FALSE(playbackScopeGpuTapKillSwitch(QByteArrayLiteral("1")));
    ASSERT_FALSE(playbackScopeGpuTapKillSwitch(QByteArrayLiteral("off")));
    ASSERT_FALSE(playbackScopeGpuTapKillSwitch(QByteArrayLiteral("00")));
    ASSERT_FALSE(playbackScopeGpuTapKillSwitch(QByteArrayLiteral("false")));
}

TEST(PlaybackScopesDuringPlayback, PGT06_AStoppedPlayTheReadBackServedRedrawsItsFullResolutionScope)
{
    // Hide: today's rule, whatever the read-back did.
    ASSERT_TRUE(playbackScopesNeedStopRefresh(PlaybackScopesMode::Hide, false, true));
    ASSERT_TRUE(playbackScopesNeedStopRefresh(PlaybackScopesMode::Hide, true, true));
    // A Throttled play the read-back served redraws; one it did not serve keeps today's no-refresh.
    ASSERT_TRUE(playbackScopesNeedStopRefresh(PlaybackScopesMode::Throttled, true, true));
    ASSERT_FALSE(playbackScopesNeedStopRefresh(PlaybackScopesMode::Throttled, false, true));
    ASSERT_FALSE(playbackScopesNeedStopRefresh(PlaybackScopesMode::Live, false, true));
    // A hidden edit area never asks.
    for (PlaybackScopesMode mode : { PlaybackScopesMode::Hide, PlaybackScopesMode::Throttled, PlaybackScopesMode::Live })
    {
        ASSERT_FALSE(playbackScopesNeedStopRefresh(mode, true, false));
        ASSERT_FALSE(playbackScopesNeedStopRefresh(mode, false, false));
    }
}

TEST(PlaybackScopesDuringPlayback, PGT07_MainWindowWiresTheReadBackWithoutRecursion)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    // The funnel passes the read-back's answer as its 4th argument.
    const QString funnel = memberBody(source, QStringLiteral("bool MainWindow::scopeDisplayConsumesPresentedPixels( bool editAreaVisible ) const"));
    ASSERT_FALSE(funnel.isEmpty());
    ASSERT_TRUE(funnel.contains(QStringLiteral("playbackScopeGpuTapServesNow( editAreaVisible )")));
    // The draw gate: the read-back branch comes BEFORE the PSO-02 branch.
    const int tapBranch = source.indexOf(QStringLiteral("    if( m_playbackScopeGpuTapServing )\n"));
    const int pso02Branch = source.indexOf(QStringLiteral("    else if( playbackScopeFrameFeedsScope(\n"));
    ASSERT_TRUE(tapBranch > 0);
    ASSERT_TRUE(pso02Branch > tapBranch);
    // r2: the draw gate asks the ACTIVE surface (GpuDisplayWindow, else the viewport) through the dispatch helpers.
    const QString drawGate = source.mid(tapBranch, pso02Branch - tapBranch);
    ASSERT_TRUE(drawGate.contains(QStringLiteral("requestPlaybackScopeTap( 512 );")));
    ASSERT_TRUE(drawGate.contains(QStringLiteral("takePlaybackScopeTap( &m_playbackScopeTapRgb8,")));
    ASSERT_FALSE(drawGate.contains(QStringLiteral("GpuDisplayViewport::requestScopeTap(")));
    ASSERT_FALSE(drawGate.contains(QStringLiteral("GpuDisplayViewport::takeScopeTap(")));
    for (const char *helper : { "bool MainWindow::playbackScopeTapSupported( void ) const",
                                "bool MainWindow::playbackScopeTapRectCapturable( void ) const",
                                "void MainWindow::requestPlaybackScopeTap( int maxWidth ) const",
                                "bool MainWindow::takePlaybackScopeTap( std::vector<uint8_t> *rgb8, int *width, int *height, quint64 *serial ) const",
                                "GpuScopeTap::Counters MainWindow::playbackScopeTapCounters( void ) const" })
    {
        const QString body = memberBody(source, QString::fromLatin1(helper));
        ASSERT_FALSE(body.isEmpty());
        ASSERT_TRUE(body.contains(QStringLiteral("GpuDisplayWindow::isActive()")));
        ASSERT_TRUE(body.contains(QStringLiteral("IfActive(")));
        ASSERT_TRUE(body.contains(QStringLiteral("GpuDisplayViewport::")));
    }
    // The smoke's scope_tap_* counters read the active surface at both ends of the measured window.
    ASSERT_EQ(2, source.count(QStringLiteral("= playbackScopeTapCounters();")));
    ASSERT_EQ(1, source.count(QStringLiteral("GpuDisplayViewport::scopeTapCountersFor( ui->graphicsView )"))); // the helper's own
    // NO RECURSION: the funnel -> playbackScopeGpuTapServesNow -> textureRouteEligibleIgnoringScopes -> the table. Neither of
    // the last two may call the funnel (or the scope-aware route check that calls it), so the call graph has no cycle.
    const QString ignoring = memberBody(source, QStringLiteral("bool MainWindow::textureRouteEligibleIgnoringScopes( void ) const"));
    const QString table = memberBody(source, QStringLiteral("bool MainWindow::gpuPlaybackReconTextureRouteTableAtScaleOne( bool hasScopeVisualization ) const"));
    const QString servesNow = memberBody(source, QStringLiteral("bool MainWindow::playbackScopeGpuTapServesNow( bool editAreaVisible ) const"));
    ASSERT_FALSE(ignoring.isEmpty());
    ASSERT_FALSE(table.isEmpty());
    ASSERT_FALSE(servesNow.isEmpty());
    for (const QString &body : { ignoring, table, servesNow })
    {
        ASSERT_FALSE(body.contains(QStringLiteral("scopeDisplayConsumesPresentedPixels(")));
        ASSERT_FALSE(body.contains(QStringLiteral("gpuPlaybackReconTextureRouteEligibleAtScaleOne()")));
        ASSERT_FALSE(body.contains(QStringLiteral("gpuPreviewPolicyForCurrentScopeState(")));
    }
    ASSERT_TRUE(ignoring.contains(QStringLiteral("gpuPlaybackReconTextureRouteTableAtScaleOne( /*hasScopeVisualization*/ false )")));
    ASSERT_TRUE(servesNow.contains(QStringLiteral("textureRouteEligibleIgnoringScopes()")));
    ASSERT_TRUE(servesNow.contains(QStringLiteral("playbackScopeGpuTapServes( in )")));
    ASSERT_TRUE(servesNow.contains(QStringLiteral("in.tapSupported = playbackScopeTapSupported();")));
    ASSERT_TRUE(servesNow.contains(QStringLiteral("in.tapRectCapturable = in.tapSupported && playbackScopeTapRectCapturable();")));
    ASSERT_FALSE(servesNow.contains(QStringLiteral("GpuDisplayViewport::scopeTapSupported(")));
    // The stop refresh asks the pure rule.
    ASSERT_TRUE(source.contains(QStringLiteral(
        "playbackScopesNeedStopRefresh( m_playbackScopesMode, m_playbackScopeGpuTapServedThisPlay,")));
}

// ---- PLAYBACK-GPU-SCOPES-NO-VETO-1 r2: GpuDisplayWindow, zoom past fit, stale read-backs ----

namespace
{
QString sourceBetween(const QString &source, const QString &from, const QString &to)
{
    const int start = source.indexOf(from);
    if (start < 0) return QString();
    const int end = source.indexOf(to, start);
    return end < 0 ? QString() : source.mid(start, end - start);
}
} // namespace

// PGT-07 (r2 window pins): the read-back on GpuDisplayWindow polls at paint start, issues only after the synchronous
// capture block and never on a capture paint, and is freed before destroyTexture with the context current.
TEST(PlaybackScopesDuringPlayback, PGT07_GpuDisplayWindowIssuesTheReadBackAfterTheCaptureAndFreesItFirst)
{
    const QString window = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString paint = memberBody(window, QStringLiteral("void GpuDisplayWindow::paintGL()"));
    ASSERT_FALSE(paint.isEmpty());
    // Poll at the very start of the paint.
    const int pollAt = paint.indexOf(QStringLiteral("m_scopeTap.poll(context());"));
    ASSERT_TRUE(pollAt > 0);
    ASSERT_TRUE(pollAt < paint.indexOf(QStringLiteral("glDisable(GL_DEPTH_TEST);")));
    // Issue after the synchronous capture block, before the env-gated read-back diagnostic, never on a capture paint.
    const int captureAt = paint.indexOf(QStringLiteral("    if ( m_captureReadbackRequested )\n"));
    const int issueAt = paint.indexOf(QStringLiteral("m_scopeTap.issue(context(), defaultFramebufferObject(),"));
    const int diagnosticAt = paint.indexOf(QStringLiteral("MLVAPP_WINDOW_READBACK_DIR"));
    ASSERT_TRUE(captureAt > 0);
    ASSERT_TRUE(issueAt > captureAt);
    ASSERT_TRUE(diagnosticAt > issueAt);
    ASSERT_EQ(1, paint.count(QStringLiteral("m_scopeTap.issue(")));
    ASSERT_TRUE(paint.contains(QStringLiteral(
        "if ( m_scopeTap.requested() && !m_captureReadbackRequested && m_texturePresentationActive )")));
    ASSERT_TRUE(paint.contains(QStringLiteral(
        "playbackScopeTapLetterboxRect(sx, sy, fbw, fbh, &tapLeft, &tapTop, &tapRight, &tapBottom);")));
    // The synchronous capture block is unchanged.
    ASSERT_TRUE(paint.contains(QStringLiteral(
        "            QImage grabbed(fbw, fbh, QImage::Format_RGBA8888);\n"
        "            glPixelStorei(GL_PACK_ALIGNMENT, 1);\n"
        "            glReadPixels(0, 0, fbw, fbh, GL_RGBA, GL_UNSIGNED_BYTE, grabbed.bits());\n")));
    // cleanupGLResources frees the read-back (GL only with the context current) BEFORE destroyTexture.
    const QString cleanup = memberBody(window, QStringLiteral("void GpuDisplayWindow::cleanupGLResources()"));
    ASSERT_FALSE(cleanup.isEmpty());
    const int destroyAt = cleanup.indexOf(QStringLiteral(
        "m_scopeTap.destroy(glContext && QOpenGLContext::currentContext() == glContext);"));
    ASSERT_TRUE(destroyAt > 0);
    ASSERT_TRUE(destroyAt < cleanup.indexOf(QStringLiteral("destroyTexture();")));
    // Every window static takes the mutex and refuses off the GUI thread.
    ASSERT_TRUE(window.contains(QStringLiteral(
        "return ( win && QThread::currentThread() == win->thread() ) ? win : nullptr;")));
    for (const char *name : { "scopeTapSupportedIfActive()", "scopeTapRectCapturableIfActive()",
                              "requestScopeTapIfActive(int maxWidth)",
                              "takeScopeTapIfActive(std::vector<uint8_t> *rgb8, int *width, int *height, quint64 *serial)",
                              "invalidateScopeTapIfActive()", "scopeTapCountersIfActive()" })
    {
        const QString body = sourceBetween(window, QStringLiteral("GpuDisplayWindow::%1").arg(QLatin1String(name)),
                                           QStringLiteral("\n}\n"));
        ASSERT_FALSE(body.isEmpty());
        ASSERT_TRUE(body.contains(QStringLiteral("QMutexLocker lock(&g_activeMutex);")));
        ASSERT_TRUE(body.contains(QStringLiteral("activeWindowOnItsThreadLocked()")));
    }
    // take never calls GL: no poll, no makeCurrent in it.
    const QString take = sourceBetween(window, QStringLiteral("GpuDisplayWindow::takeScopeTapIfActive("), QStringLiteral("\n}\n"));
    ASSERT_FALSE(take.contains(QStringLiteral("poll(")));
    ASSERT_FALSE(take.contains(QStringLiteral("makeCurrent")));

    // Both surfaces share ONE state machine.
    const QString viewport = readRepoFile(QStringLiteral("platform/qt/GpuDisplayViewport.cpp"));
    ASSERT_TRUE(viewport.contains(QStringLiteral("m_scopeTap.issue(context(), defaultFramebufferObject(),")));
    ASSERT_FALSE(viewport.contains(QStringLiteral("glFenceSync")));
    ASSERT_FALSE(window.contains(QStringLiteral("glFenceSync")));
}

TEST(PlaybackScopesDuringPlayback, PGT08_TheWindowLetterboxIsTheDrawnPictureInsideTheFramebuffer)
{
    double l = -1.0, t = -1.0, r = -1.0, b = -1.0;
    QRect gl;
    // Exact fit: the whole framebuffer.
    playbackScopeTapLetterboxRect(1.0, 1.0, 2158, 1373, &l, &t, &r, &b);
    ASSERT_TRUE(l == 0.0 && t == 0.0 && r == 2158.0 && b == 1373.0);
    ASSERT_TRUE(playbackScopeTapFramebufferRect(l, t, r, b, 2158, 1373, &gl));
    ASSERT_TRUE(QRect(0, 0, 2158, 1373) == gl);
    // Picture wider than the window (sy < 1): bars top and bottom.
    playbackScopeTapLetterboxRect(1.0, 0.5, 2000, 1000, &l, &t, &r, &b);
    ASSERT_TRUE(l == 0.0 && r == 2000.0 && t == 250.0 && b == 750.0);
    ASSERT_TRUE(playbackScopeTapFramebufferRect(l, t, r, b, 2000, 1000, &gl));
    ASSERT_TRUE(QRect(0, 250, 2000, 500) == gl);
    // Picture taller than the window (sx < 1): bars left and right.
    playbackScopeTapLetterboxRect(0.25, 1.0, 2000, 1000, &l, &t, &r, &b);
    ASSERT_TRUE(l == 750.0 && r == 1250.0 && t == 0.0 && b == 1000.0);
    ASSERT_TRUE(playbackScopeTapFramebufferRect(l, t, r, b, 2000, 1000, &gl));
    ASSERT_TRUE(QRect(750, 0, 500, 1000) == gl);
    // The window's own fit math (sx, sy <= 1) is always capturable, so the window never skips.
    for (double imgAspect : { 0.5, 1.0, 1.7778, 2.39, 4.0 })
    {
        const double winAspect = 2158.0 / 1373.0;
        const double sx = imgAspect > winAspect ? 1.0 : imgAspect / winAspect;
        const double sy = imgAspect > winAspect ? winAspect / imgAspect : 1.0;
        playbackScopeTapLetterboxRect(sx, sy, 2158, 1373, &l, &t, &r, &b);
        ASSERT_TRUE(playbackScopeTapFramebufferRect(l, t, r, b, 2158, 1373, &gl));
    }
}

// PGT-09 (r2, CoS #447 blocker 5): zoomed past fit the read-back cannot capture the picture, so it must not serve: the
// scope stays on the CPU feed (the funnel counts it as a presented-pixel consumer, and a frame requested under that
// policy feeds it) and keeps updating -- it never freezes on a read-back that only skips.
TEST(PlaybackScopesDuringPlayback, PGT09_AZoomedPastFitScopeKeepsUpdatingFromTheCpuFeed)
{
    PlaybackScopeGpuTapInputs zoomed = servingTapInputs();
    zoomed.tapRectCapturable = false;   // what a paint records for a picture past the framebuffer
    ASSERT_FALSE(playbackScopeGpuTapServes(zoomed));
    const bool consumes = playbackScopeDisplayConsumesPresentedPixels(
        true, true, PlaybackScopesMode::Throttled, playbackScopeGpuTapServes(zoomed));
    ASSERT_TRUE(consumes);
    // The next frame, requested under that policy (an RGB8 route), feeds the CPU scope.
    ASSERT_TRUE(playbackScopeFrameFeedsScope(consumes, /*allowedRouteWithoutRgb8*/false));
    // Back to fit: served again, no consumer.
    ASSERT_FALSE(playbackScopeDisplayConsumesPresentedPixels(
        true, true, PlaybackScopesMode::Throttled, playbackScopeGpuTapServes(servingTapInputs())));

    // Every drawn picture records its capturability, on both surfaces, before any issue.
    const QString viewport = readRepoFile(QStringLiteral("platform/qt/GpuDisplayViewport.cpp"));
    const QString viewportPaint = memberBody(viewport, QStringLiteral("void GpuDisplayViewport::paintGL()"));
    ASSERT_FALSE(viewportPaint.isEmpty());
    const int noted = viewportPaint.indexOf(QStringLiteral(
        "m_scopeTap.noteDrawnRect(leftPx, topPx, rightPx, bottomPx, fbWidthPx, fbHeightPx);"));
    ASSERT_TRUE(noted > 0);
    ASSERT_TRUE(noted < viewportPaint.indexOf(QStringLiteral("m_scopeTap.issue(")));
    const QString window = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    ASSERT_TRUE(window.contains(QStringLiteral("m_scopeTap.noteDrawnRect(tapLeft, tapTop, tapRight, tapBottom, fbw, fbh);")));
}

// PGT-10 (r2, CoS #447 improvement): a read-back of the old picture never lands on the scope after a stop, a seek, a
// clip change or a mode change: each drops queued and finished read-backs on both surfaces (GpuScopeTap::invalidate,
// whose no-GL behaviour the gui row gpuScopeTapCountsOnlyDroppedRequestsAndInvalidateDropsStaleReadBacks pins).
TEST(PlaybackScopesDuringPlayback, PGT10_StopSeekClipAndModeChangesDropStaleReadBacks)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString invalidate = memberBody(source, QStringLiteral("void MainWindow::invalidatePlaybackScopeTap( void ) const"));
    ASSERT_TRUE(invalidate.contains(QStringLiteral("GpuDisplayWindow::invalidateScopeTapIfActive();")));
    ASSERT_TRUE(invalidate.contains(QStringLiteral("GpuDisplayViewport::invalidateScopeTap( ui->graphicsView );")));
    // Seek: a timeline move no playback engine path made.
    const QString seek = memberBody(source, QStringLiteral("void MainWindow::on_horizontalSliderPosition_valueChanged(int position)"));
    ASSERT_TRUE(sourceBetween(seek, QStringLiteral("if( !m_playbackInternalSliderAdvance )"), QStringLiteral("}"))
                    .contains(QStringLiteral("invalidatePlaybackScopeTap();")));
    // Clip change: every clip open clears the presentation here.
    const QString clip = memberBody(source, QStringLiteral("void MainWindow::clearPresentationForClipOpen( const char *reason )"));
    ASSERT_TRUE(clip.contains(QStringLiteral("invalidatePlaybackScopeTap();")));
    ASSERT_TRUE(source.count(QStringLiteral("clearPresentationForClipOpen( \"clip-open")) >= 2);
    // Stop (and start) and a mode change.
    const QString play = memberBody(source, QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"));
    ASSERT_TRUE(play.contains(QStringLiteral("invalidatePlaybackScopeTap();")));
    const QString mode = memberBody(source, QStringLiteral("void MainWindow::applyPlaybackScopesMode( PlaybackScopesMode mode, bool persist )"));
    ASSERT_TRUE(mode.contains(QStringLiteral("if ( mode != m_playbackScopesMode ) invalidatePlaybackScopeTap();")));
    // A stale in-flight read-back is freed unread.
    const QString tap = readRepoFile(QStringLiteral("platform/qt/GpuScopeTap.cpp"));
    ASSERT_TRUE(tap.contains(QStringLiteral(
        "( state == GL_ALREADY_SIGNALED || state == GL_CONDITION_SATISFIED ) && slot.generation == m_generation")));
    ASSERT_TRUE(tap.contains(QStringLiteral("slot.generation = m_generation;")));
}
