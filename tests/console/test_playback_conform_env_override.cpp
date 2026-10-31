// HFR-NATIVE-60FPS-FULLRES-1: MLVAPP_PLAYBACK_CONFORM, the venue legs' explicit native-HFR arm.
// "1" or "0" wins over the saved Playback/ConformEnabled setting for the process; any other value is no
// override. The env value is never persisted: only a user toggle of the menu item writes the setting.
// The pure parse/resolve run directly; the MainWindow wiring is pinned by reading the source as text
// (MainWindow.cpp needs a full GUI build), like test_playback_conform_wiring.cpp.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackConformPolicy.h"

#include <QByteArray>
#include <QFile>
#include <QString>
#include <QTextStream>

using namespace playback_conform;

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

QString setupMenuBody(const QString & source)
{
    return functionBody(source,
        QStringLiteral("void MainWindow::setupPlaybackConformMenu( void )"),
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"));
}

QString applySettingsBody(const QString & source)
{
    return functionBody(source,
        QStringLiteral("void MainWindow::applyPlaybackConformSettings( bool persist )"),
        QStringLiteral("//Paint the Audio Track Wave to GUI"));
}

} // namespace

// ---- parse ---------------------------------------------------------------------------------

TEST(PlaybackConformEnvOverride, ParsesOneAndZeroOnly)
{
    ASSERT_EQ(1, conformEnabledFromEnvValue(QByteArray("1")));
    ASSERT_EQ(0, conformEnabledFromEnvValue(QByteArray("0")));
}

TEST(PlaybackConformEnvOverride, EmptyOrOtherValuesAreNoOverride)
{
    ASSERT_EQ(-1, conformEnabledFromEnvValue(QByteArray()));
    ASSERT_EQ(-1, conformEnabledFromEnvValue(QByteArray("")));
    ASSERT_EQ(-1, conformEnabledFromEnvValue(QByteArray("2")));
    ASSERT_EQ(-1, conformEnabledFromEnvValue(QByteArray("on")));
    ASSERT_EQ(-1, conformEnabledFromEnvValue(QByteArray("-1")));
}

TEST(PlaybackConformEnvOverride, SurroundingWhitespaceIsTrimmedLikeSwapInterval)
{
    ASSERT_EQ(0, conformEnabledFromEnvValue(QByteArray(" 0 ")));
    ASSERT_EQ(1, conformEnabledFromEnvValue(QByteArray("\t1\n")));
}

// ---- resolve -------------------------------------------------------------------------------

TEST(PlaybackConformEnvOverride, EnvWinsOverTheSetting)
{
    ASSERT_FALSE(resolveConformEnabled(0, true));
    ASSERT_TRUE(resolveConformEnabled(1, false));
    ASSERT_FALSE(resolveConformEnabled(0, false));
    ASSERT_TRUE(resolveConformEnabled(1, true));
}

TEST(PlaybackConformEnvOverride, NoOverrideFallsBackToTheSetting)
{
    ASSERT_TRUE(resolveConformEnabled(-1, true));
    ASSERT_FALSE(resolveConformEnabled(-1, false));
}

TEST(PlaybackConformEnvOverride, NativeArmPlaysA5994ClipAt5994)
{
    const bool enabled = resolveConformEnabled(conformEnabledFromEnvValue(QByteArray("0")), kDefaultEnabled);
    ASSERT_NEAR(59.94, playbackFps(59.94, enabled, kDefaultTargetFps, kDefaultThresholdFps), 1e-9);
    ASSERT_FALSE(conformApplies(59.94, enabled, kDefaultTargetFps, kDefaultThresholdFps));
}

TEST(PlaybackConformEnvOverride, ConformArmStillConformsA5994ClipTo24)
{
    const bool enabled = resolveConformEnabled(conformEnabledFromEnvValue(QByteArray("1")), false);
    ASSERT_NEAR(24.0, playbackFps(59.94, enabled, kDefaultTargetFps, kDefaultThresholdFps), 1e-9);
}

// ---- MainWindow wiring ---------------------------------------------------------------------

TEST(PlaybackConformEnvOverride, EnvIsReadOnceInTheHeader)
{
    const QString policy = readRepoFile(QStringLiteral("platform/qt/PlaybackConformPolicy.h"));
    ASSERT_TRUE(policy.contains(QStringLiteral(
        "static const int envOverride = conformEnabledFromEnvValue( qgetenv( \"MLVAPP_PLAYBACK_CONFORM\" ) );")));
}

TEST(PlaybackConformEnvOverride, SetupMenuAppliesTheEnvAfterLoadingSettings)
{
    const QString menu = setupMenuBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")));
    ASSERT_FALSE(menu.isEmpty());
    const int load = menu.indexOf(QStringLiteral("playback_conform::loadSettings( set )"));
    const int resolve = menu.indexOf(QStringLiteral(
        "m_conformEnabled = playback_conform::resolveConformEnabled( playback_conform::conformEnvOverride(),"));
    ASSERT_TRUE(load >= 0);
    ASSERT_TRUE(resolve > load);
    // the checkbox shows the effective value
    ASSERT_TRUE(menu.contains(QStringLiteral("enable->setChecked( m_conformEnabled );")));
}

TEST(PlaybackConformEnvOverride, EnvPathNeverSavesSettings)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString menu = setupMenuBody(source);
    ASSERT_FALSE(menu.isEmpty());
    ASSERT_FALSE(menu.contains(QStringLiteral("saveSettings")));
    ASSERT_FALSE(menu.contains(QStringLiteral("setValue(")));
    // the persisted value is the setting, which only the user toggle changes; never the effective one
    const QString apply = applySettingsBody(source);
    ASSERT_FALSE(apply.isEmpty());
    ASSERT_TRUE(apply.contains(QStringLiteral("s.enabled = m_conformEnabledSetting;")));
    ASSERT_FALSE(apply.contains(QStringLiteral("s.enabled = m_conformEnabled;")));
    ASSERT_TRUE(menu.contains(QStringLiteral("m_conformEnabledSetting = saved.enabled;")));
    ASSERT_TRUE(menu.contains(QStringLiteral(
        "m_conformEnabledSetting = on;\n"
        "        m_conformEnabled = playback_conform::resolveConformEnabled( playback_conform::conformEnvOverride(), on );\n"
        "        applyPlaybackConformSettings( true );")));
}

// MERGE-MASTER r3 (adversary A2): MLVAPP_PLAYBACK_CONFORM=0|1 wins over the setting for the WHOLE process, so the menu toggle
// re-resolves the override and never assigns the effective value straight from the checkbox (env=0 then toggle on stayed 24 fps).
TEST(PlaybackConformEnvOverride, MenuToggleKeepsTheEnvOverride)
{
    const QString menu = setupMenuBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")));
    ASSERT_FALSE(menu.isEmpty());
    ASSERT_FALSE(menu.contains(QStringLiteral("m_conformEnabled = on;")));
    ASSERT_EQ(menu.count(QStringLiteral("m_conformEnabled = playback_conform::resolveConformEnabled( playback_conform::conformEnvOverride(),")), 2);
    // the resolution the toggle relies on: env 0 / 1 beat either checkbox state, no override follows the checkbox
    ASSERT_FALSE(playback_conform::resolveConformEnabled(0, true));
    ASSERT_TRUE(playback_conform::resolveConformEnabled(1, false));
    ASSERT_TRUE(playback_conform::resolveConformEnabled(-1, true));
    ASSERT_FALSE(playback_conform::resolveConformEnabled(-1, false));
}

TEST(PlaybackConformEnvOverride, TelemetryCarriesTheOverride)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int at = source.indexOf(QStringLiteral("playback_smoke.conform session=%1"));
    ASSERT_TRUE(at >= 0);
    const QString line = source.mid(at, 2000);
    ASSERT_TRUE(line.contains(QStringLiteral("conform_env_override=%21")));
    ASSERT_TRUE(line.contains(QStringLiteral(".arg( playback_conform::conformEnvOverride() );")));
    const int start = source.indexOf(QStringLiteral("playback_smoke.start session=%1"));
    ASSERT_TRUE(start >= 0);
    const int startEnd = source.indexOf(QStringLiteral(";\n"), source.indexOf(QStringLiteral("MLVAPP_RAW_UINT16_PREFETCH_DECODERS"), start));
    ASSERT_TRUE(startEnd > start);
    const QString startLine = source.mid(start, startEnd - start + 1);
    ASSERT_TRUE(startLine.contains(QStringLiteral("env_playback_conform=%35")));
    ASSERT_TRUE(startLine.contains(QStringLiteral(".arg( envValueForLog( \"MLVAPP_PLAYBACK_CONFORM\" ) );")));
}
