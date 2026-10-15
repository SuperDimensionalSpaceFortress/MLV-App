// PLAYBACK-STARTUP-WAIT-PREWARM-1 Row B: once per clip load, while paused, the GUI thread warms the CUDA texture playback route
// (the preview-processing program, and one AMaZE R16 -> RGBA16 run at the playback texture size on temporary textures), so the first
// Play-present no longer pays the route's one-time setup. These pin when it runs and when it is cancelled, the size it warms, and the
// wiring (kill switch read once, armed at clip load, run only from a paused present, never on an ineligible route).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackRoutePrewarmPolicy.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <string>

using playback_route_prewarm::Decision;
using playback_route_prewarm::Inputs;
using playback_route_prewarm::decide;

namespace
{

Inputs eligible()
{
    Inputs in;
    in.enabled = true;
    in.playing = false;
    in.fileLoaded = true;
    in.clipGenerationMatches = true;
    in.gpuWindowActive = true;
    in.textureRouteRequested = true;
    in.r16TextureBackendAdmitted = true;
    return in;
}

QString sourceFile(const char *relative)
{
    const QString path = repo_file_path(QString::fromLatin1(relative));
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString bodyBetween(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(PlaybackRoutePrewarmPolicy, AnEligiblePausedClipIsWarmed)
{
    ASSERT_TRUE(Decision::Run == decide(eligible()));
    ASSERT_EQ(std::string("run"), std::string(playback_route_prewarm::decisionName(Decision::Run)));
}

TEST(PlaybackRoutePrewarmPolicy, PlayCancelsIt)
{
    Inputs in = eligible();
    in.playing = true;
    ASSERT_TRUE(Decision::SkipPlaying == decide(in));
    ASSERT_EQ(std::string("playing"), std::string(playback_route_prewarm::decisionName(Decision::SkipPlaying)));
}

TEST(PlaybackRoutePrewarmPolicy, AClipCloseOrAnotherClipCancelsIt)
{
    Inputs in = eligible();
    in.fileLoaded = false;
    ASSERT_TRUE(Decision::SkipNoClip == decide(in));
    in = eligible();
    in.clipGenerationMatches = false; // armed for a clip that is no longer the loaded one
    ASSERT_TRUE(Decision::SkipStale == decide(in));
}

TEST(PlaybackRoutePrewarmPolicy, AnIneligibleRouteDoesNoWork)
{
    Inputs in = eligible();
    in.gpuWindowActive = false;
    ASSERT_TRUE(Decision::SkipIneligible == decide(in));
    in = eligible();
    in.textureRouteRequested = false; // the CPU route, or any route that is not the CUDA texture present
    ASSERT_TRUE(Decision::SkipIneligible == decide(in));
    in = eligible();
    in.r16TextureBackendAdmitted = false;
    ASSERT_TRUE(Decision::SkipIneligible == decide(in));
}

TEST(PlaybackRoutePrewarmPolicy, TheKillSwitchWinsOverEverything)
{
    Inputs in = eligible();
    in.enabled = false;
    ASSERT_TRUE(Decision::SkipDisabled == decide(in));
    in.playing = true;
    ASSERT_TRUE(Decision::SkipDisabled == decide(in));
}

TEST(PlaybackRoutePrewarmPolicy, ItWarmsThePlaybackTextureSize)
{
    using playback_route_prewarm::textureSize;
    // scale 1: the clip's own size
    ASSERT_EQ(1808, textureSize(1808, 2268, 1, false, 0, 0).width);
    ASSERT_EQ(2268, textureSize(1808, 2268, 1, false, 0, 0).height);
    // x2 / x4 with a reduced-recon plan: the plan's size (rows cropped to whole ISO blocks)
    ASSERT_EQ(452, textureSize(1808, 2268, 4, true, 452, 564).width);
    ASSERT_EQ(564, textureSize(1808, 2268, 4, true, 452, 564).height);
    // no plan (not dual ISO, or refused): the full-size texture route
    ASSERT_EQ(1808, textureSize(1808, 2268, 4, false, 0, 0).width);
    // any other scale stays on the full-size texture route
    ASSERT_EQ(2268, textureSize(1808, 2268, 3, true, 600, 756).height);
}

TEST(PlaybackRoutePrewarmWiring, ArmedAtClipLoadRunFromAPausedPresentAndKillSwitchedOnce)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    ASSERT_TRUE(source.contains(QStringLiteral(
        "static const bool disabled = qgetenv( \"MLVAPP_PLAYBACK_ROUTE_PREWARM\" ).trimmed() == QByteArrayLiteral( \"0\" );")));
    // armed right after the clip's audio engine is set up (the open has settled)
    ASSERT_TRUE(source.contains(QStringLiteral(
        "    m_pAudioPlayback->initAudioEngine( m_pMlvObject );\n"
        "    armPlaybackRoutePrewarm();\n")));
    const QString finish = bodyBetween(source, QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"),
                                       QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_FALSE(finish.isEmpty());
    ASSERT_TRUE(finish.contains(QStringLiteral(
        "if( m_playbackRoutePrewarmArmed && !ui->actionPlay->isChecked() )")));
    // the run re-decides with the policy (Play, clip close or another clip in between cancels it)
    const QString run = bodyBetween(source, QStringLiteral("void MainWindow::runPlaybackRoutePrewarm( quint64 clipGeneration )"),
                                    QStringLiteral("void MainWindow::armPlaybackRoutePrewarm()"));
    ASSERT_FALSE(run.isEmpty());
    const int decideAt = run.indexOf(QStringLiteral("playback_route_prewarm::decide( in )"));
    const int warmAt = run.indexOf(QStringLiteral("GpuDisplayWindow::prewarmPlaybackReconRouteIfActive("));
    ASSERT_TRUE(decideAt >= 0);
    ASSERT_TRUE(warmAt > decideAt);
    ASSERT_TRUE(run.mid(decideAt, warmAt - decideAt).contains(QStringLiteral("if( decision != playback_route_prewarm::Decision::Run )")));
    // the window warms on temporary textures and frees them: nothing it creates is ever presented
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString prewarm = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth"),
                                        QStringLiteral("GpuPresentSetupTiming GpuDisplayWindow::lastPresentSetupTimingIfActive()"));
    ASSERT_FALSE(prewarm.isEmpty());
    ASSERT_FALSE(prewarm.contains(QStringLiteral("m_texture =")));
    ASSERT_FALSE(prewarm.contains(QStringLiteral("m_gpuReconSourceTexture =")));
    ASSERT_FALSE(prewarm.contains(QStringLiteral("update()")));
    const int reset = prewarm.indexOf(QStringLiteral("gpuAmazeDebayerResetR16TextureBackendResources();"));
    const int freeTextures = prewarm.indexOf(QStringLiteral("delete warmOutput;"));
    ASSERT_TRUE(reset >= 0);
    ASSERT_TRUE(freeTextures > reset); // unregistered from CUDA before the GL textures go
}
