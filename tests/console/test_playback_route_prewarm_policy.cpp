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
        "static const bool disabled = !playbackEnvSwitch( \"MLVAPP_PLAYBACK_ROUTE_PREWARM\", true );")));
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
    // r2 row d: a window already presenting the recon route at this size keeps its live CUDA-GL slot (no warm, no
    // reset), and every warm says whether it dropped a held slot
    const int keepSlot = prewarm.indexOf(QStringLiteral("skipped=live_slot_warm"));
    ASSERT_TRUE(keepSlot >= 0);
    ASSERT_TRUE(keepSlot < reset);
    ASSERT_TRUE(prewarm.contains(QStringLiteral("live_slot_evicted=%9")));
    // and the warm runs without g_activeMutex (r1 held it 133-470 ms against every render-thread present)
    const QString ifActive = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRouteIfActive("),
                                         QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth"));
    const int lockScopeEnd = ifActive.indexOf(QStringLiteral("    }\n    // r2: the warm runs WITHOUT g_activeMutex"));
    const int warm = ifActive.indexOf(QStringLiteral("return win->prewarmPlaybackReconRoute(texWidth, texHeight, summary);"));
    ASSERT_TRUE(lockScopeEnd >= 0);
    ASSERT_TRUE(warm > lockScopeEnd);
}

// PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-SETUP-1 Row A: the warm keeps its pair as the staged slot (still registered),
// and the first recon present adopts it before destroyTexture() could reset the caches holding that registration.
TEST(PlaybackRouteStagedSlotWiring, ThePrewarmKeepsItsWarmedPairAsTheStagedSlotWithoutAReset)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    ASSERT_TRUE(window.contains(QStringLiteral(
        "playback_env_flag::parse( qgetenv( \"MLVAPP_PLAYBACK_ROUTE_PREWARM_STAGE\" ).constData() ), true );")));
    const QString prewarm = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth"),
                                        QStringLiteral("GpuPresentSetupTiming GpuDisplayWindow::lastPresentSetupTimingIfActive()"));
    ASSERT_FALSE(prewarm.isEmpty());
    // an earlier staged pair is freed first (after the live-slot skip, before any new allocation)
    const int keepSlot = prewarm.indexOf(QStringLiteral("skipped=live_slot_warm"));
    const int freePrevious = prewarm.indexOf(QStringLiteral("releaseStagedReconSlot();"));
    const int alloc = prewarm.indexOf(QStringLiteral("new QOpenGLTexture("));
    ASSERT_TRUE(keepSlot >= 0);
    ASSERT_TRUE(freePrevious > keepSlot);
    ASSERT_TRUE(alloc > freePrevious);
    // staged only when the warm succeeded and staging is on
    ASSERT_TRUE(prewarm.contains(QStringLiteral("const bool staged = amazeOk && playbackRoutePrewarmStageEnabled();")));
    const int stagedAt = prewarm.indexOf(QStringLiteral("    if ( staged )\n"));
    const int elseAt = prewarm.indexOf(QStringLiteral("    else\n"), stagedAt);
    ASSERT_TRUE(stagedAt >= 0);
    ASSERT_TRUE(elseAt > stagedAt);
    const QString stagedBranch = prewarm.mid(stagedAt, elseAt - stagedAt);
    ASSERT_TRUE(stagedBranch.contains(QStringLiteral("m_stagedReconOutput = warmOutput;")));
    ASSERT_TRUE(stagedBranch.contains(QStringLiteral("m_stagedReconSource = warmInput;")));
    ASSERT_TRUE(stagedBranch.contains(QStringLiteral("m_stagedReconWidth = texWidth;")));
    ASSERT_FALSE(stagedBranch.contains(QStringLiteral("ResetR16TextureBackendResources")));
    ASSERT_FALSE(stagedBranch.contains(QStringLiteral("delete warm")));
    // the disabled / failed branch is today's code: reset BEFORE the GL textures are freed
    const int reset = prewarm.indexOf(QStringLiteral("gpuAmazeDebayerResetR16TextureBackendResources();"));
    const int freeTextures = prewarm.indexOf(QStringLiteral("delete warmOutput;"));
    ASSERT_TRUE(reset > elseAt);
    ASSERT_TRUE(freeTextures > reset);
    ASSERT_TRUE(prewarm.contains(QStringLiteral("staged=%10")));
}

TEST(PlaybackRouteStagedSlotWiring, TheReallocBranchAdoptsTheStagedPairBeforeDestroyTextureWithNoCudaReset)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString present = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture("),
                                        QStringLiteral("bool GpuDisplayWindow::readGpuReconSourceBayer16Texture("));
    ASSERT_FALSE(present.isEmpty());
    const int decideAt = present.indexOf(QStringLiteral("playback_route_staged_slot::decide("));
    const int destroyAt = present.indexOf(QStringLiteral("destroyTexture();"));
    ASSERT_TRUE(decideAt >= 0);
    ASSERT_TRUE(destroyAt > decideAt);
    // the policy sees the window's own state: is the old texture a recon one, does a recon source exist
    const QString decideCall = present.mid(decideAt, present.indexOf(QStringLiteral(");"), decideAt) - decideAt);
    ASSERT_TRUE(decideCall.contains(QStringLiteral("m_textureFromGpuRecon")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("m_gpuReconSourceTexture != nullptr")));
    // Adopt: the old texture deleted with neither CUDA reset, the staged pair moved in, the source left not current
    const int adoptAt = present.indexOf(QStringLiteral("if ( stagedSlot == playback_route_staged_slot::Decision::Adopt )"));
    const int adoptEvent = present.indexOf(QStringLiteral("gpuPresentEventNoteTextureRealloc(\"window_recon_slot_adopted\""), adoptAt);
    ASSERT_TRUE(adoptAt > decideAt);
    ASSERT_TRUE(adoptAt < destroyAt);
    ASSERT_TRUE(adoptEvent > adoptAt);
    const QString adopt = present.mid(adoptAt, adoptEvent - adoptAt);
    ASSERT_TRUE(adopt.contains(QStringLiteral("delete m_texture;")));
    ASSERT_TRUE(adopt.contains(QStringLiteral("m_texture = m_stagedReconOutput;")));
    ASSERT_TRUE(adopt.contains(QStringLiteral("m_gpuReconSourceTexture = m_stagedReconSource;")));
    ASSERT_TRUE(adopt.contains(QStringLiteral("m_gpuReconSourceTextureCurrent = false;")));
    ASSERT_FALSE(adopt.contains(QStringLiteral("ResetR16TextureBackendResources")));
    ASSERT_FALSE(adopt.contains(QStringLiteral("llrpGpuPlaybackReconResetGlTextureResources")));
    ASSERT_FALSE(adopt.contains(QStringLiteral("destroyTexture();")));
    // otherwise: a discarded pair is freed, then today's path (destroyTexture + the two realloc events)
    const int discard = present.indexOf(
        QStringLiteral("if ( stagedSlot == playback_route_staged_slot::Decision::Discard ) releaseStagedReconSlot();"), adoptEvent);
    ASSERT_TRUE(discard > adoptEvent);
    ASSERT_TRUE(destroyAt > discard);
    ASSERT_TRUE(present.indexOf(QStringLiteral("\"window_rgba16_texture_realloc\""), destroyAt) > destroyAt);
    ASSERT_TRUE(present.indexOf(QStringLiteral("\"window_r16_texture_realloc\""), destroyAt) > destroyAt);
    // a failed present still frees whatever slot it holds (an adopted one included), so it is never shown
    ASSERT_TRUE(present.contains(QStringLiteral("    if ( !ok )\n    {\n        destroyTexture();")));
}

TEST(PlaybackRouteStagedSlotWiring, TheStagedPairIsFreedResetFirstOnContextCleanupClipChangeAndRearm)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString release = bodyBetween(window, QStringLiteral("void GpuDisplayWindow::releaseStagedReconSlot()"),
                                        QStringLiteral("void GpuDisplayWindow::cleanupGLResources()"));
    ASSERT_FALSE(release.isEmpty());
    const int amazeReset = release.indexOf(QStringLiteral("gpuAmazeDebayerResetR16TextureBackendResources();"));
    const int reconReset = release.indexOf(QStringLiteral("llrpGpuPlaybackReconResetGlTextureResources();"));
    const int freeOutput = release.indexOf(QStringLiteral("delete m_stagedReconOutput;"));
    const int freeSource = release.indexOf(QStringLiteral("delete m_stagedReconSource;"));
    ASSERT_TRUE(amazeReset >= 0);
    ASSERT_TRUE(reconReset >= 0);
    ASSERT_TRUE(freeOutput > amazeReset);
    ASSERT_TRUE(freeOutput > reconReset);
    ASSERT_TRUE(freeSource > amazeReset);
    ASSERT_TRUE(freeSource > reconReset);
    const QString cleanup = bodyBetween(window, QStringLiteral("void GpuDisplayWindow::cleanupGLResources()"),
                                        QStringLiteral("void GpuDisplayWindow::applySamplingMode("));
    ASSERT_FALSE(cleanup.isEmpty());
    ASSERT_TRUE(cleanup.contains(QStringLiteral("releaseStagedReconSlot();")));
    // the GUI-thread entry MainWindow uses
    const QString ifActive = bodyBetween(window, QStringLiteral("void GpuDisplayWindow::releaseStagedPlaybackReconSlotIfActive()"),
                                         QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth"));
    ASSERT_TRUE(ifActive.contains(QStringLiteral("QThread::currentThread() != win->thread()")));
    ASSERT_TRUE(ifActive.contains(QStringLiteral("win->releaseStagedReconSlot();")));

    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    const QString arm = bodyBetween(source, QStringLiteral("void MainWindow::armPlaybackRoutePrewarm()"),
                                    QStringLiteral("QImage MainWindow::grabIdentityViewport("));
    ASSERT_TRUE(arm.contains(QStringLiteral("GpuDisplayWindow::releaseStagedPlaybackReconSlotIfActive();")));
    const QString clipChange = bodyBetween(source, QStringLiteral("void MainWindow::clearPresentationForClipOpen( const char *reason )"),
                                           QStringLiteral("invalidateDisplayPreviewCache();"));
    ASSERT_TRUE(clipChange.contains(QStringLiteral("GpuDisplayWindow::releaseStagedPlaybackReconSlotIfActive();")));
}

// Row B: the recon backend's DLL load + create, paid while paused through the guarded loader.
TEST(PlaybackReconBackendPreloadWiring, RunCallsThePreloadAfterTheRunDecisionAndTheWindowWarmUnderItsKillSwitch)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    ASSERT_TRUE(source.contains(QStringLiteral(
        "static const bool disabled = !playbackEnvSwitch( \"MLVAPP_PLAYBACK_RECON_BACKEND_PRELOAD\", true );")));
    const QString run = bodyBetween(source, QStringLiteral("void MainWindow::runPlaybackRoutePrewarm( quint64 clipGeneration )"),
                                    QStringLiteral("void MainWindow::armPlaybackRoutePrewarm()"));
    ASSERT_FALSE(run.isEmpty());
    const int notRun = run.indexOf(QStringLiteral("if( decision != playback_route_prewarm::Decision::Run )"));
    const int warmAt = run.indexOf(QStringLiteral("GpuDisplayWindow::prewarmPlaybackReconRouteIfActive("));
    const int gate = run.indexOf(QStringLiteral(
        "const bool backendPreload = !mlvappPlaybackReconBackendPreloadDisabled() && playback_recon_requested_by_environment();"));
    const int preload = run.indexOf(QStringLiteral(
        "if( backendPreload ) backendPreloadOk = llrpGpuPlaybackReconPreloadBackend( &backendPreloadMs );"));
    ASSERT_TRUE(notRun >= 0);
    ASSERT_TRUE(warmAt > notRun);
    ASSERT_TRUE(gate > warmAt);
    ASSERT_TRUE(preload > gate);
    ASSERT_TRUE(run.contains(QStringLiteral(" backend_preload_ok=%1 backend_preload_ms=%2")));
    // the loader only: never a clip/LUT configuration or a run from the preload
    const QString llrawproc = sourceFile("src/mlv/llrawproc/llrawproc.c");
    const int first = llrawproc.indexOf(QStringLiteral("int llrpGpuPlaybackReconPreloadBackend(double * load_ms)\n{"));
    ASSERT_TRUE(first >= 0);
    const QString body = llrawproc.mid(first, llrawproc.indexOf(QStringLiteral("\n}\n"), first) - first);
    ASSERT_TRUE(body.contains(QStringLiteral("llrawproc_gpu_recon_backend_available_guarded(1)")));
    ASSERT_FALSE(body.contains(QStringLiteral("set_clip")));
    ASSERT_FALSE(body.contains(QStringLiteral("set_luts")));
    ASSERT_FALSE(body.contains(QStringLiteral("run(")));
    // and the non-GPU build has the matching stub
    ASSERT_TRUE(llrawproc.indexOf(QStringLiteral("int llrpGpuPlaybackReconPreloadBackend(double * load_ms)\n{"), first + 1) > first);
}
