// PLAYBACK-SCOPES-OPTION-1: the "Scopes During Playback" setting (Hide / Throttled / Live), its
// MLVAPP_PLAYBACK_SCOPES_DURING_PLAYBACK override, the throttle interval per mode, and the policy
// helper that turns a visible scope off DURING PLAYBACK ONLY in Hide mode. MainWindow is GUI code not
// linked into console_tests, so its wiring is pinned as source text.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackScopesDuringPlayback.h"

#include <QFile>
#include <QSettings>
#include <QString>
#include <QTextStream>
#include <QVariant>

#include <memory>

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

    const QString ui = readRepoFile(QStringLiteral("platform/qt/MainWindow.ui"));
    for (const char *action : { "actionPlaybackScopesHide", "actionPlaybackScopesThrottled", "actionPlaybackScopesLive" })
    {
        ASSERT_TRUE(ui.contains(QStringLiteral("<action name=\"%1\">").arg(QLatin1String(action))));
        ASSERT_TRUE(ui.contains(QStringLiteral("<addaction name=\"%1\"/>").arg(QLatin1String(action))));
    }
}
