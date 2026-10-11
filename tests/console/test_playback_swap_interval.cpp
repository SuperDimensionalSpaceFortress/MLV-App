// PLAYBACK-VSYNC-DEFAULT-1: the persisted VSync setting defaults to on (interval 1),
// MLVAPP_SWAP_INTERVAL 0|1 overrides it, and every playback GL surface requests the result
// (the four call sites are GUI/GL code not linked into console_tests, so they are pinned as
// source text).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/GpuWindowSwapTelemetry.h"
#include "../../platform/qt/PlaybackSwapInterval.h"

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

} // namespace

TEST(PlaybackSwapInterval, UnsetEnvIsNoOverride)
{
    ASSERT_EQ(-1, playbackSwapIntervalFromEnvValue(QByteArray()));
    ASSERT_EQ(-1, playbackSwapIntervalFromEnvValue(QByteArrayLiteral("")));
}

TEST(PlaybackSwapInterval, ZeroAndOneParse)
{
    ASSERT_EQ(0, playbackSwapIntervalFromEnvValue(QByteArrayLiteral("0")));
    ASSERT_EQ(0, playbackSwapIntervalFromEnvValue(QByteArrayLiteral(" 0 ")));
    ASSERT_EQ(1, playbackSwapIntervalFromEnvValue(QByteArrayLiteral("1")));
    ASSERT_EQ(1, playbackSwapIntervalFromEnvValue(QByteArrayLiteral(" 1 ")));
}

TEST(PlaybackSwapInterval, AnythingElseIsNoOverride)
{
    ASSERT_EQ(-1, playbackSwapIntervalFromEnvValue(QByteArrayLiteral("2")));
    ASSERT_EQ(-1, playbackSwapIntervalFromEnvValue(QByteArrayLiteral("-1")));
    ASSERT_EQ(-1, playbackSwapIntervalFromEnvValue(QByteArrayLiteral("on")));
    ASSERT_EQ(-1, playbackSwapIntervalFromEnvValue(QByteArrayLiteral("true")));
}

TEST(PlaybackSwapInterval, EnvOverridesTheSetting)
{
    ASSERT_EQ(0, playbackSwapIntervalResolve(playbackSwapIntervalFromEnvValue("0"), true));
    ASSERT_EQ(1, playbackSwapIntervalResolve(playbackSwapIntervalFromEnvValue("1"), false));
    ASSERT_EQ(1, playbackSwapIntervalResolve(playbackSwapIntervalFromEnvValue(""), true));
    ASSERT_EQ(0, playbackSwapIntervalResolve(playbackSwapIntervalFromEnvValue(""), false));
    ASSERT_EQ(1, playbackSwapIntervalResolve(playbackSwapIntervalFromEnvValue("on"), true));
}

TEST(PlaybackSwapInterval, DefaultIsOneWithNoEnvAndNoSettingAndTheSettingPersists)
{
    std::unique_ptr<QSettings> store = automation_settings::openAppSettings();
    const QVariant saved = store->value(PlaybackSwapIntervalSettings::kKeyVSync());
    store->remove(PlaybackSwapIntervalSettings::kKeyVSync());
    store->sync();

    // Nothing else in console_tests reads it, so this is the first (cached) env read.
    qunsetenv("MLVAPP_SWAP_INTERVAL");
    ASSERT_TRUE(playbackVSyncFromSettings());
    ASSERT_EQ(1, playbackSwapInterval());

    playbackVSyncWriteToSettings(false);
    ASSERT_FALSE(automation_settings::openAppSettings()->value(
        PlaybackSwapIntervalSettings::kKeyVSync(), true).toBool());
    ASSERT_FALSE(playbackVSyncFromSettings());
    ASSERT_EQ(0, playbackSwapInterval());

    playbackVSyncWriteToSettings(true);
    ASSERT_EQ(1, playbackSwapInterval());

    if ( saved.isValid() ) store->setValue(PlaybackSwapIntervalSettings::kKeyVSync(), saved);
    else store->remove(PlaybackSwapIntervalSettings::kKeyVSync());
    store->sync();
}

TEST(PlaybackSwapInterval, MissingWglEntryPointReadsMinusOne)
{
    ASSERT_EQ(-1, wglSwapIntervalActual(nullptr));
}

TEST(PlaybackSwapInterval, EveryPlaybackSurfaceRequestsTheEnvInterval)
{
    const char *sources[] = {
        "platform/qt/GpuDisplayViewport.cpp",
        "platform/qt/GpuDisplayWindow.cpp",
        "platform/qt/MainWindow.cpp",
        "platform/qt/main.cpp",
    };
    for ( const char *source : sources )
    {
        const QString text = readRepoFile(QString::fromLatin1(source));
        ASSERT_FALSE(text.contains(QStringLiteral("setSwapInterval(0)")));
        ASSERT_TRUE(text.contains(QStringLiteral("setSwapInterval(playbackSwapInterval())")));
    }
}

TEST(PlaybackSwapInterval, BothInitLinesLogTheWglIntervalBesideQts)
{
    const char *sources[] = {
        "platform/qt/GpuDisplayViewport.cpp",
        "platform/qt/GpuDisplayWindow.cpp",
    };
    for ( const char *source : sources )
    {
        const QString text = readRepoFile(QString::fromLatin1(source));
        const int realizedAt = text.indexOf(QStringLiteral("\", realized_swap_interval=\""));
        const int wglAt = text.indexOf(QStringLiteral("\", wgl_swap_interval_actual=\""));
        ASSERT_TRUE(realizedAt >= 0 && wglAt > realizedAt);
    }
}

TEST(PlaybackSwapInterval, SwapCallSummaryIsZeroWithoutTimedSwaps)
{
    GpuWindowSwapTelemetryCounters counters;
    const GpuWindowSwapTelemetrySummary summary = GpuWindowSwapTelemetryPolicy::summarize(counters);
    ASSERT_EQ(static_cast<quint64>(0), summary.swapCallCount);
    ASSERT_NEAR(0.0, summary.swapCallAvgMs, 1e-9);
    ASSERT_NEAR(0.0, summary.swapCallP95Ms, 1e-9);
    ASSERT_NEAR(0.0, summary.swapCallMaxMs, 1e-9);
    ASSERT_EQ(-2, summary.wglSwapIntervalAtFirstTimedSwap);
}

TEST(PlaybackSwapInterval, SwapCallSummaryReportsAvgP95Max)
{
    GpuWindowSwapTelemetryCounters counters;
    for ( int i = 1; i <= 20; ++i ) counters.swapCallSamplesMs.push_back(static_cast<double>(i));
    counters.wglSwapIntervalAtFirstTimedSwap = 1;
    const GpuWindowSwapTelemetrySummary summary = GpuWindowSwapTelemetryPolicy::summarize(counters);
    ASSERT_EQ(static_cast<quint64>(20), summary.swapCallCount);
    ASSERT_NEAR(10.5, summary.swapCallAvgMs, 1e-9);
    ASSERT_NEAR(19.0, summary.swapCallP95Ms, 1e-9);
    ASSERT_NEAR(20.0, summary.swapCallMaxMs, 1e-9);
    ASSERT_EQ(1, summary.wglSwapIntervalAtFirstTimedSwap);
}
