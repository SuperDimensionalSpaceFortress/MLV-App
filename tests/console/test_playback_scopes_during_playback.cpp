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
    ASSERT_TRUE(source.contains(QStringLiteral(
        "editAreaVisible, ui->actionPlay->isChecked(), m_playbackScopesMode );")));
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
