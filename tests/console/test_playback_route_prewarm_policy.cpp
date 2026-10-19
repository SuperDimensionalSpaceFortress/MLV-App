// PLAYBACK-STARTUP-WAIT-PREWARM-1 Row B: once per clip load, while paused, the GUI thread warms the CUDA texture playback route
// (the preview-processing program, and one AMaZE R16 -> RGBA16 run at the playback texture size on temporary textures), so the first
// Play-present no longer pays the route's one-time setup. These pin when it runs and when it is cancelled, the size it warms, and the
// wiring (kill switch read once, armed at clip load, run only from a paused present, never on an ineligible route).
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackRoutePrewarmPolicy.h"

#include <QFile>
#include <QString>
#include <QStringList>
#include <QTextStream>

#include <cstdio>
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

// PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row W: after the backend preload, ONE paused recon warm run on the recon
// worker (the thread and llrawproc worker state Play frame 0 uses), then AMaZE into the staged slot and a token release.
TEST(PlaybackReconWarmRunWiring, TheRunPostsTheWarmAfterTheBackendPreloadUnderItsKillSwitch)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    ASSERT_TRUE(source.contains(QStringLiteral(
        "static const bool disabled = !playbackEnvSwitch( \"MLVAPP_PLAYBACK_RECON_WARM_RUN\", true );")));
    const QString run = bodyBetween(source, QStringLiteral("void MainWindow::runPlaybackRoutePrewarm( quint64 clipGeneration )"),
                                    QStringLiteral("void MainWindow::postPlaybackReconWarm( quint64 clipGeneration )"));
    ASSERT_FALSE(run.isEmpty());
    const int preload = run.indexOf(QStringLiteral(
        "if( backendPreload ) backendPreloadOk = llrpGpuPlaybackReconPreloadBackend( &backendPreloadMs );"));
    const int preloadedFlag = run.indexOf(QStringLiteral("m_playbackReconBackendPreloaded = backendPreloadOk != 0;"));
    const int post = run.indexOf(QStringLiteral("postPlaybackReconWarm( clipGeneration );"));
    ASSERT_TRUE(preload >= 0);
    ASSERT_TRUE(preloadedFlag > preload);
    ASSERT_TRUE(post > preloadedFlag);
    // the prewarm's own Run decision is what arms the warm (a skipped prewarm returns before it)
    ASSERT_TRUE(run.indexOf(QStringLiteral("m_playbackReconWarmPrewarmRan = true;")) > preloadedFlag);
    ASSERT_TRUE(run.indexOf(QStringLiteral("m_playbackReconWarmScale = scale;")) > preloadedFlag);

    const QString postBody = bodyBetween(source, QStringLiteral("void MainWindow::postPlaybackReconWarm( quint64 clipGeneration )"),
                                         QStringLiteral("void MainWindow::schedulePlaybackReconWarmRepost("));
    ASSERT_FALSE(postBody.isEmpty());
    const int decideAt = postBody.indexOf(QStringLiteral("playback_recon_warm_run::decide("));
    const int requestAt = postBody.indexOf(QStringLiteral("m_pRenderThread->requestPlaybackReconWarm("));
    ASSERT_TRUE(decideAt >= 0);
    ASSERT_TRUE(requestAt > decideAt);
    // both switches gate it, and every guard is the live state
    const QString decideCall = postBody.mid(decideAt, requestAt - decideAt);
    ASSERT_TRUE(decideCall.contains(QStringLiteral("!mlvappPlaybackReconWarmRunDisabled() && !mlvappPlaybackRoutePrewarmDisabled()")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("ui->actionPlay->isChecked()")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("clipGeneration == m_playbackRoutePrewarmClipGeneration")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("m_playbackReconBackendPreloaded")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("llrpGpuCudaSuppressed() != 0")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("!m_pRenderThread->isIdle()")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("if( decision.run )")));
    // one warm per clip
    ASSERT_TRUE(postBody.indexOf(QStringLiteral("|| m_playbackReconWarmRan ) return;")) < decideAt);
    ASSERT_TRUE(postBody.indexOf(QStringLiteral("|| m_playbackReconWarmRan ) return;")) >= 0);
    // a new clip re-arms it with the prewarm
    const QString arm = bodyBetween(source, QStringLiteral("void MainWindow::armPlaybackRoutePrewarm()"),
                                    QStringLiteral("QImage MainWindow::grabIdentityViewport("));
    ASSERT_TRUE(arm.contains(QStringLiteral("m_playbackReconWarmPrewarmRan = false;")));
    ASSERT_TRUE(arm.contains(QStringLiteral("m_playbackReconWarmPosts = 0;")));
    ASSERT_TRUE(arm.contains(QStringLiteral("m_playbackReconWarmRan = false;")));
}

TEST(PlaybackReconWarmRunWiring, TheWarmNeverPresentsNeverTouchesPlayAndWritesNoCache)
{
    const QString thread = sourceFile("platform/qt/RenderFrameThread.cpp");
    const QString warm = bodyBetween(thread, QStringLiteral("void RenderFrameThread::warmReconForWorker("),
                                     QStringLiteral("void RenderFrameThread::reconFrameForWorker("));
    ASSERT_FALSE(warm.isEmpty());
    ASSERT_FALSE(warm.contains(QStringLiteral("frameReady")));
    ASSERT_FALSE(warm.contains(QStringLiteral("m_frameSlots")));
    ASSERT_FALSE(warm.contains(QStringLiteral("publishRenderedSlot")));
    ASSERT_FALSE(warm.contains(QStringLiteral("m_active")));
    ASSERT_FALSE(warm.contains(QStringLiteral("Cache")));
    ASSERT_TRUE(warm.contains(QStringLiteral("playback_recon_warm_run::run( m_pMlvObject,")));
    ASSERT_TRUE(warm.contains(QStringLiteral("workerState,")));   // the recon worker's own llrawproc state
    ASSERT_TRUE(warm.contains(QStringLiteral("emit playbackReconWarmDone( done );")));
    ASSERT_TRUE(warm.contains(QStringLiteral("Qt::QueuedConnection")));
    // the recon worker routes a warm entry there before any slot access
    const QString recon = bodyBetween(thread, QStringLiteral("void RenderFrameThread::reconFrameForWorker("),
                                      QStringLiteral("    if( entry.slotIndex < 0 || entry.slotIndex >= static_cast<int>( m_frameSlots.size() ) )"));
    ASSERT_TRUE(recon.contains(QStringLiteral("if( entry.warm )")));
    ASSERT_TRUE(recon.contains(QStringLiteral("warmReconForWorker( entry, workerState );")));
    // real requests win: re-checked under the lock as the warm is taken (after the pop, so it sees only other work),
    // and a queued warm is not pipeline work
    const QString take = bodyBetween(thread, QStringLiteral("bool RenderFrameThread::takeReconRequestForWorker("),
                                     QStringLiteral("bool RenderFrameThread::realWorkPendingLocked("));
    ASSERT_FALSE(take.isEmpty());
    const int pop = take.indexOf(QStringLiteral("m_reconRequests.pop_front();"));
    const int recheck = take.indexOf(QStringLiteral("entry->warmRealRequestPending = realWorkPendingLocked();"));
    const int running = take.indexOf(QStringLiteral("m_warmRunning = !entry->warmRealRequestPending;"));
    ASSERT_TRUE(pop >= 0);
    ASSERT_TRUE(recheck > pop);
    ASSERT_TRUE(running > recheck);
    // a queued warm is not pipeline work; a RUNNING one is (it reads m_pMlvObject: lock() / isIdle() wait it out
    // before a clip change or a raw-level change), and it is cleared before the done is posted
    const QString inFlight = bodyBetween(thread, QStringLiteral("bool RenderFrameThread::phase3WorkInFlightLocked( void ) const"),
                                         QStringLiteral("void RenderFrameThread::queueDecodeRequestLocked("));
    ASSERT_TRUE(inFlight.contains(QStringLiteral("if( m_warmRunning ) return true;")));
    ASSERT_TRUE(inFlight.contains(QStringLiteral("[]( const ReconQueueEntry &entry ) { return !entry.warm; }")));
    const int cleared = warm.indexOf(QStringLiteral("m_warmRunning = false;"));
    const int posted = warm.indexOf(QStringLiteral("QMetaObject::invokeMethod( this,"));
    ASSERT_TRUE(cleared >= 0);
    ASSERT_TRUE(posted > cleared);

    // the core: uncached decode, last-run state saved before and restored after, the Play frame-0 thread opt-ins
    const QString core = sourceFile("platform/qt/PlaybackReconWarmRun.h");
    ASSERT_TRUE(core.contains(QStringLiteral("getMlvRawFrameUint16Uncached( video, frame, raw.data() )")));
    ASSERT_FALSE(core.contains(QStringLiteral("getMlvRawFrameUint16( ")));
    const int save = core.indexOf(QStringLiteral("void *saved = llrpGpuPlaybackReconSaveLastRunState();"));
    const int decode = core.indexOf(QStringLiteral("getMlvRawFrameUint16Uncached( video"));
    const int apply = core.indexOf(QStringLiteral("applyLLRawProcObjectWorker( video, raw.data()"));
    const int retained = core.indexOf(QStringLiteral("llrpGpuPlaybackReconGetLastRetainedDeviceBayer16( &retained )"));
    const int restore = core.indexOf(QStringLiteral("llrpGpuPlaybackReconRestoreLastRunState( saved );"));
    ASSERT_TRUE(save >= 0);
    ASSERT_TRUE(decode > save);
    ASSERT_TRUE(apply > decode);
    ASSERT_TRUE(retained > apply);
    ASSERT_TRUE(restore > retained);
    ASSERT_TRUE(core.contains(QStringLiteral("llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread( 1 );")));
    ASSERT_TRUE(core.contains(QStringLiteral("mlvDualIsoGpuPreviewScaleReconPlan( video, scale, 0, &plan )")));
    // the uncached decode is the prefetch-free path
    const QString video = sourceFile("src/mlv/video_mlv.c");
    const int uncached = video.indexOf(QStringLiteral("int getMlvRawFrameUint16Uncached(mlvObject_t * video, uint64_t frameIndex, uint16_t * unpackedFrame)\n{"));
    ASSERT_TRUE(uncached >= 0);
    const QString uncachedBody = video.mid(uncached, video.indexOf(QStringLiteral("\n}\n"), uncached) - uncached);
    ASSERT_TRUE(uncachedBody.contains(QStringLiteral("return getMlvRawFrameUint16Direct(video, frameIndex, unpackedFrame);")));
    ASSERT_FALSE(uncachedBody.contains(QStringLiteral("prefetch")));

    // the GUI side never plays, loops or programs a Play (one Play per process)
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    const QString gui = bodyBetween(source, QStringLiteral("void MainWindow::postPlaybackReconWarm( quint64 clipGeneration )"),
                                    QStringLiteral("void MainWindow::armPlaybackRoutePrewarm()"));
    ASSERT_FALSE(gui.isEmpty());
    ASSERT_FALSE(gui.contains(QStringLiteral("actionPlay->setChecked")));
    ASSERT_FALSE(gui.contains(QStringLiteral("actionPlay->trigger")));
    ASSERT_FALSE(gui.contains(QStringLiteral("programmaticPlay")));
    ASSERT_FALSE(gui.contains(QStringLiteral("ProgrammaticPlayLedger")));
    ASSERT_FALSE(gui.contains(QStringLiteral("actionLoop")));
}

TEST(PlaybackReconWarmRunWiring, TheGuiReleasesTheTokenOnEveryPath)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    ASSERT_TRUE(source.contains(QStringLiteral(
        "connect( m_pRenderThread, &RenderFrameThread::playbackReconWarmDone, this, &MainWindow::onPlaybackReconWarmDone );")));
    const QString done = bodyBetween(source, QStringLiteral("void MainWindow::onPlaybackReconWarmDone("),
                                     QStringLiteral("void MainWindow::armPlaybackRoutePrewarm()"));
    ASSERT_FALSE(done.isEmpty());
    // no early exit: every warm-done reaches the release
    ASSERT_FALSE(done.contains(QStringLiteral("return")));
    const int gate = done.indexOf(QStringLiteral("if( playback_recon_warm_run::guiAmazeAllowed( hasOutput, playing, clipMatches ) )"));
    const int amaze = done.indexOf(QStringLiteral("GpuDisplayWindow::warmStagedReconSlotFromDeviceBayer16IfActive("));
    const int release = done.indexOf(QStringLiteral(
        "line.released = llrpGpuPlaybackReconReleaseRetainedDeviceBayer16( outcome.retained.token ) != 0;"));
    const int outstanding = done.indexOf(QStringLiteral("line.outstandingAfter = llrpGpuPlaybackReconRetainedOutstandingCount();"));
    const int log = done.indexOf(QStringLiteral("qInfo().noquote() << line.format()"));
    ASSERT_TRUE(gate >= 0);
    ASSERT_TRUE(amaze > gate);
    ASSERT_TRUE(release > amaze);
    ASSERT_TRUE(outstanding > release);
    ASSERT_TRUE(log > outstanding);
    // the release is unconditional: the line before it closes the AMaZE if/else at function level
    ASSERT_TRUE(done.mid(amaze, release - amaze).endsWith(QStringLiteral("\n    }\n    ")));
    ASSERT_EQ(1, done.count(QStringLiteral("llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(")));
}

TEST(PlaybackReconWarmRunWiring, TheWarmGuiFunctionNeverAssignsTheDisplayedTextures)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString warm = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::warmStagedReconSlotFromDeviceBayer16(const uint16_t"),
                                     QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth"));
    ASSERT_FALSE(warm.isEmpty());
    ASSERT_FALSE(warm.contains(QStringLiteral("m_texture =")));
    ASSERT_FALSE(warm.contains(QStringLiteral("m_texture->")));
    ASSERT_FALSE(warm.contains(QStringLiteral("m_gpuReconSourceTexture =")));
    ASSERT_FALSE(warm.contains(QStringLiteral("m_stagedReconOutput =")));
    ASSERT_FALSE(warm.contains(QStringLiteral("update()")));
    ASSERT_FALSE(warm.contains(QStringLiteral("paintGL")));
    ASSERT_FALSE(warm.contains(QStringLiteral("delete ")));
    // into the staged output only, and only when a staged pair exists at exactly the warm's size
    const int noSlot = warm.indexOf(QStringLiteral("if ( !m_stagedReconOutput || !m_stagedReconSource )"));
    const int size = warm.indexOf(QStringLiteral("if ( m_stagedReconWidth != width || m_stagedReconHeight != height )"));
    const int amaze = warm.indexOf(QStringLiteral("gpuAmazeDebayerRenderPostWbGlTextureFromDeviceBayer16(deviceBayer16,"));
    ASSERT_TRUE(noSlot >= 0);
    ASSERT_TRUE(size > noSlot);
    ASSERT_TRUE(amaze > size);
    ASSERT_TRUE(warm.contains(QStringLiteral("m_stagedReconOutput->textureId(),")));
}

// PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row K (PlaybackGpuClockKickPolicy.h): the Play-start GPU clock kick starts
// before the play-start frame request, ends at the first present / stop / clip change / device loss / window teardown,
// never loads the AMaZE DLL, and its kernel is bounded by a %globaltimer deadline on its own lowest-priority stream.
TEST(PlaybackGpuClockKickWiring, PlayStartsTheKickBeforeThePlayStartFrameRequest)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    ASSERT_TRUE(source.contains(QStringLiteral(
        "static const bool disabled = !playbackEnvSwitch( \"MLVAPP_PLAYBACK_GPU_CLOCK_KICK\", false );")));
    // HUB RULING 2026-10-18T06:16:26Z item (1): the kick defaults OFF (=1 opts in); the default-on literal is gone
    ASSERT_FALSE(source.contains(QStringLiteral(
        "static const bool disabled = !playbackEnvSwitch( \"MLVAPP_PLAYBACK_GPU_CLOCK_KICK\", true );")));
    const QString toggled = bodyBetween(source, QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
                                        QStringLiteral("double MainWindow::playGateNativeFps() const"));
    ASSERT_FALSE(toggled.isEmpty());
    const int smoke = toggled.indexOf(QStringLiteral("beginPlaybackSmokeTelemetry();"));
    const int kick = toggled.indexOf(QStringLiteral("beginPlaybackGpuClockKick();"));
    const int request = toggled.indexOf(QStringLiteral("requestFrameRefresh( true, \"play-start\" );"));
    ASSERT_TRUE(smoke >= 0);
    ASSERT_TRUE(kick > smoke);       // the session id the kick line carries is assigned
    ASSERT_TRUE(request > kick);     // the clocks ramp while frame 0 decodes
    ASSERT_EQ(1, toggled.count(QStringLiteral("beginPlaybackGpuClockKick();")));
    // stop ends it before the smoke session is finished
    const int stop = toggled.indexOf(QStringLiteral("endPlaybackGpuClockKick( \"play_stop\" );"));
    const int finish = toggled.indexOf(QStringLiteral("finishPlaybackSmokeTelemetry( \"play-stop\" );"));
    ASSERT_TRUE(stop >= 0);
    ASSERT_TRUE(finish > stop);

    // the begin: the policy decides on the live state, then the backend re-checks under its lock
    const QString begin = bodyBetween(source, QStringLiteral("void MainWindow::beginPlaybackGpuClockKick()"),
                                      QStringLiteral("void MainWindow::endPlaybackGpuClockKick( const char *site )"));
    ASSERT_FALSE(begin.isEmpty());
    const int decideAt = begin.indexOf(QStringLiteral("playback_gpu_clock_kick::decide("));
    const int beginAt = begin.indexOf(QStringLiteral("gpuAmazeDebayerClockKickBegin( playback_gpu_clock_kick::kKickMaxMs, &why, &beginSteadyMs )"));
    ASSERT_TRUE(decideAt >= 0);
    ASSERT_TRUE(beginAt > decideAt);
    const QString decideCall = begin.mid(decideAt, beginAt - decideAt);
    ASSERT_TRUE(decideCall.contains(QStringLiteral("!mlvappPlaybackGpuClockKickDisabled(),")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("ui->actionPlay->isChecked(),")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("routeAdmitted,")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("gpuAmazeDebayerLiveBackendLoaded()")));
    ASSERT_TRUE(decideCall.contains(QStringLiteral("if( decision.run )")));
    // the route and the loss flag are the live state, read just before the decision
    const QString inputs = begin.left(decideAt);
    ASSERT_TRUE(inputs.contains(QStringLiteral("&& m_lastPresentedRequestContext.gpuPlaybackReconTexturePresentRequested")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("&& m_lastPresentedRequestContext.gpuPlaybackReconAmazeTexturePresentAdmitted;")));
    ASSERT_TRUE(inputs.contains(QStringLiteral("const bool cudaSuppressed = llrpGpuCudaSuppressed() != 0;")));
    // Row 0's session probe is armed on every Play, kick or not
    ASSERT_TRUE(begin.indexOf(QStringLiteral("gpuAmazeDebayerBeginProbeSession();")) < decideAt);
    ASSERT_TRUE(begin.indexOf(QStringLiteral("gpuAmazeDebayerBeginProbeSession();")) >= 0);
    // at most one kick per Play: a held one is ended first
    ASSERT_TRUE(begin.indexOf(QStringLiteral("if( m_playbackGpuClockKickActive ) endPlaybackGpuClockKick( \"next_play\" );")) >= 0);
    // a deadline timer collects it when no present came first, and a stale timer is a no-op
    ASSERT_TRUE(begin.contains(QStringLiteral("QTimer::singleShot( playback_gpu_clock_kick::kKickMaxMs + 50, this,")));
    ASSERT_TRUE(begin.contains(QStringLiteral("if( serial == m_playbackGpuClockKickSerial ) endPlaybackGpuClockKick( \"deadline_timer\" );")));
}

TEST(PlaybackGpuClockKickWiring, TheKickEndsAtTheFirstPresentClipChangeDeviceLossAndWindowTeardown)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    const QString finish = bodyBetween(source, QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"),
                                       QStringLiteral("void MainWindow::drawFrameReady()"));
    ASSERT_TRUE(finish.contains(QStringLiteral(
        "if( m_playbackGpuClockKickActive && ui->actionPlay->isChecked() ) endPlaybackGpuClockKick( \"first_present\" );")));
    const QString arm = bodyBetween(source, QStringLiteral("void MainWindow::armPlaybackRoutePrewarm()"),
                                    QStringLiteral("void MainWindow::beginPlaybackGpuClockKick()"));
    ASSERT_TRUE(arm.contains(QStringLiteral("endPlaybackGpuClockKick( \"clip_change\" );")));
    const int refresh = source.indexOf(QStringLiteral("GpuDeviceLossBreaker::setRefreshCallback( [this]()"));
    const int loss = source.indexOf(QStringLiteral("endPlaybackGpuClockKick( \"device_loss\" );"), refresh);
    const int cpuRoute = source.indexOf(QStringLiteral("requestFrameRefresh( true, \"gpu-device-loss-cpu-route\" );"), refresh);
    ASSERT_TRUE(refresh >= 0);
    ASSERT_TRUE(loss > refresh);
    ASSERT_TRUE(cpuRoute > loss);
    // the end is once per kick and logs its line
    const QString end = bodyBetween(source, QStringLiteral("void MainWindow::endPlaybackGpuClockKick( const char *site )"),
                                    QStringLiteral("QImage MainWindow::grabIdentityViewport("));
    ASSERT_FALSE(end.isEmpty());
    const int guard = end.indexOf(QStringLiteral("if( !m_playbackGpuClockKickActive ) return;"));
    const int collect = end.indexOf(QStringLiteral("gpuAmazeDebayerClockKickEnd();"));
    ASSERT_TRUE(guard >= 0);
    ASSERT_TRUE(collect > guard);
    ASSERT_TRUE(end.contains(QStringLiteral("++m_playbackGpuClockKickSerial;")));
    ASSERT_TRUE(end.contains(QStringLiteral("qInfo().noquote() << line.format();")));
    // the window going away ends it (it holds no GL resource)
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString destructor = bodyBetween(window, QStringLiteral("GpuDisplayWindow::~GpuDisplayWindow()"),
                                           QStringLiteral("    if ( isValid() )"));
    ASSERT_TRUE(destructor.contains(QStringLiteral("gpuAmazeDebayerClockKickEnd();")));
}

TEST(PlaybackGpuClockKickWiring, TheKickNeverLoadsTheDllAndReChecksTheLossUnderTheLiveLock)
{
    const QString debayer = sourceFile("platform/qt/GpuDebayer.cpp");
    const QString kick = bodyBetween(debayer, QStringLiteral("bool gpuAmazeDebayerLiveBackendLoaded(void)"),
                                     QStringLiteral("GpuAmazeClockKickReport gpuAmazeDebayerClockKickEnd(void)\n{"));
    const QString kickEnd = bodyBetween(debayer, QStringLiteral("GpuAmazeClockKickReport gpuAmazeDebayerClockKickEnd(void)\n{"),
                                        QStringLiteral("    return report;\n}\n"));
    ASSERT_FALSE(kick.isEmpty());
    ASSERT_FALSE(kickEnd.isEmpty());
    // code lines only: the comment that says "never live.ensure()" is not a call
    const auto codeOnly = [](const QString & body)
    {
        QStringList kept;
        for (const QString & line : body.split(QLatin1Char('\n')))
        {
            const QString t = line.trimmed();
            if (t.startsWith(QStringLiteral("//")) || t.startsWith(QStringLiteral("/*")) || t.startsWith(QStringLiteral("*")))
                continue;
            kept << line;
        }
        return kept.join(QLatin1Char('\n'));
    };
    ASSERT_TRUE(kick.contains(QStringLiteral("never live.ensure()")));
    ASSERT_FALSE(codeOnly(kick).contains(QStringLiteral("ensure(")));
    ASSERT_FALSE(codeOnly(kickEnd).contains(QStringLiteral("ensure(")));
    ASSERT_TRUE(kick.contains(QStringLiteral("if ( !live.attempted || !live.available || !live.backend ) return fail(\"backend_not_loaded\");")));
    const int lock = kick.indexOf(QStringLiteral("QMutexLocker locker(&live.mutex);"), kick.indexOf(QStringLiteral("bool gpuAmazeDebayerClockKickBegin(")));
    const int recheck = kick.indexOf(QStringLiteral(
        "const bool mayLaunch = enterAmazeRunUnlessCudaSuppressed(&live.runtime, GpuFieldTelemetry::enabled());"));
    ASSERT_TRUE(kick.contains(QStringLiteral("if ( !mayLaunch ) return fail(\"cuda_suppressed\");")));
    const int launch = kick.indexOf(QStringLiteral("live.runtime.clockKickBegin(live.backend, maxMs)"));
    ASSERT_TRUE(lock >= 0);
    ASSERT_TRUE(recheck > lock);
    ASSERT_TRUE(launch > recheck);
    // both symbols are optional: an older DLL skips the kick and prints NA for the probe
    ASSERT_TRUE(debayer.contains(QStringLiteral("runtime.library.resolve(\"igpu_amaze_debayer_clock_kick_begin\")")));
    ASSERT_TRUE(debayer.contains(QStringLiteral("runtime.library.resolve(\"igpu_amaze_debayer_last_probe\")")));
    ASSERT_FALSE(debayer.contains(QStringLiteral("\"igpu_amaze_debayer_clock_kick_begin\",\n")));   // never a required resolve
}

TEST(PlaybackGpuClockKickWiring, TheKernelIsBoundedByAGlobaltimerDeadlineOnItsOwnLowestPriorityStream)
{
    const QString cu = sourceFile("tools/gpu/backend/igpu_amaze_debayer_cuda.cu");
    const QString kernel = bodyBetween(cu, QStringLiteral("__global__ void k_clock_kick(ClockKickShared * s,"),
                                       QStringLiteral("constexpr unsigned long long kProbeSpinNs"));
    ASSERT_FALSE(kernel.isEmpty());
    ASSERT_TRUE(kernel.contains(QStringLiteral("const unsigned long long t = igpu_globaltimer_ns();")));
    // the kick's own deadline: maxNs after the first chunk's start (startNs), seen by every chunk
    ASSERT_TRUE(kernel.contains(QStringLiteral("            if (t - t0 >= maxNs)\n            {\n                s->expired = 1;\n                break;\n")));
    ASSERT_TRUE(kernel.contains(QStringLiteral("const unsigned long long t0 = s->startNs;")));
    ASSERT_TRUE(kernel.contains(QStringLiteral("const unsigned int stop = s->stop;")));
    ASSERT_TRUE(cu.contains(QStringLiteral("asm volatile(\"mov.u64 %0, %%globaltimer;\" : \"=l\"(t));")));
    const QString begin = bodyBetween(cu, QStringLiteral("extern \"C\" int igpu_amaze_debayer_clock_kick_begin("),
                                      QStringLiteral("extern \"C\" int igpu_amaze_debayer_clock_kick_end("));
    ASSERT_FALSE(begin.isEmpty());
    ASSERT_TRUE(begin.contains(QStringLiteral(
        "cudaStreamCreateWithPriority(&backend->kickStream, cudaStreamNonBlocking, leastPriority);")));
    // the kick is a chain of short launches: none exceeds the 100 ms per-launch budget (GPU-LAUNCH-BUDGET-TILING-1),
    // and a host feeder keeps at most two queued, so work that must wait for the device's queued work (frame 0's recon
    // on the venue waited for a whole up-front chain: llrawproc 353 ms) waits for at most ~20 ms of kick
    ASSERT_TRUE(cu.contains(QStringLiteral("constexpr int kClockKickChunkMs = 10;")));
    ASSERT_TRUE(cu.contains(QStringLiteral("constexpr unsigned int kClockKickChunksInFlight = 2;")));
    ASSERT_TRUE(kernel.contains(QStringLiteral("if (t - tIn >= chunkNs) break;")));
    ASSERT_TRUE(kernel.contains(QStringLiteral("s->chunksDone = chunk + 1;")));
    const QString feeder = bodyBetween(cu, QStringLiteral("void clock_kick_feeder(igpu_amaze_debayer_backend * backend, unsigned long long maxNs)"),
                                       QStringLiteral("void free_probe_and_kick(igpu_amaze_debayer_backend * backend)"));
    ASSERT_FALSE(feeder.isEmpty());
    const int inFlight = feeder.indexOf(QStringLiteral("if (launched >= s->chunksDone + kClockKickChunksInFlight)"));
    const int launch = feeder.indexOf(QStringLiteral("k_clock_kick<<<1, 32, 0, backend->kickStream>>>("));
    ASSERT_TRUE(inFlight >= 0);
    ASSERT_TRUE(launch > inFlight);
    ASSERT_TRUE(feeder.contains(QStringLiteral("while (s->stop == 0 && s->expired == 0 && launched < kClockKickMaxChunks")));
    // the feeder never synchronises: it sleeps, launches, and publishes done once its chunks drain
    ASSERT_FALSE(feeder.contains(QStringLiteral("Synchronize")));
    ASSERT_TRUE(feeder.contains(QStringLiteral("s->done = 1;")));
    // clock64 is per SM: every sample carries its %smid
    ASSERT_TRUE(kernel.contains(QStringLiteral("s->sampleSm[count] = igpu_smid();")));
    // begin starts the feeder; bad max_ms (0 or below, or above the 2 s cap) is refused before anything starts
    ASSERT_TRUE(begin.contains(QStringLiteral("backend->kickFeeder = std::thread(clock_kick_feeder, backend, maxNs);")));
    ASSERT_TRUE(begin.contains(QStringLiteral("> kClockKickMaxMsCap) return -1;")));
    ASSERT_TRUE(begin.indexOf(QStringLiteral("> kClockKickMaxMsCap) return -1;"))
                < begin.indexOf(QStringLiteral("backend->kickFeeder = std::thread(")));
    ASSERT_TRUE(begin.contains(QStringLiteral("if (backend->kickLaunched && backend->kickHost && !backend->kickHost->done) return 1;")));
    // it never touches the AMaZE live buffers or tile streams
    ASSERT_FALSE(begin.contains(QStringLiteral("liveTileStreams")));
    ASSERT_FALSE(begin.contains(QStringLiteral("liveTileDeviceBuffers")));
    ASSERT_FALSE(begin.contains(QStringLiteral("ensure_live_texture_buffers")));
    // the end makes no CUDA call (safe after a device loss) and waits a bounded time
    const QString end = bodyBetween(cu, QStringLiteral("extern \"C\" int igpu_amaze_debayer_clock_kick_end("),
                                    QStringLiteral("extern \"C\" int igpu_amaze_debayer_last_cuda_error("));
    ASSERT_FALSE(end.isEmpty());
    ASSERT_FALSE(end.contains(QStringLiteral("cuda")));
    ASSERT_TRUE(end.contains(QStringLiteral("while (!s->done && now_ms() - waitStart < kClockKickEndWaitMs)")));
    // both live runs end a running kick right after their tile drain, before the CUDA-GL handoff
    ASSERT_EQ(2, cu.count(QStringLiteral("            probe);\n        stop_clock_kick_for_live_run(backend);\n")));
}

// PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-WARM-OUTPUT-REUSE-1 R2 (PlaybackWarmOutputReusePolicy.h): Play's first recon present
// keeps the paused warm's AMaZE output only after preGate and the exact input compare; every other path runs the AMaZE
// call unchanged. These pin the order of the key's life (set only after the warm's AMaZE succeeded, moved out by the
// adopting present before the staged members are cleared, cleared with the slot and by a re-run prewarm) and the call
// order in the present.
TEST(PlaybackWarmOutputReuseWiring, TheWarmKeysTheSlotOnlyAfterItsAmazeSucceeded)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString warm = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::warmStagedReconSlotFromDeviceBayer16(const uint16_t"),
                                     QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth"));
    ASSERT_FALSE(warm.isEmpty());
    // an older key is dropped first, whatever this warm does
    const int clearAt = warm.indexOf(QStringLiteral("clearStagedReconWarmKey();"));
    const int noOutput = warm.indexOf(QStringLiteral("if ( reason ) *reason = QStringLiteral(\"no_device_output\");"));
    ASSERT_TRUE(clearAt >= 0);
    ASSERT_TRUE(noOutput > clearAt);
    const int amaze = warm.indexOf(QStringLiteral("const bool amazeOk =\n        gpuAmazeDebayerRenderPostWbGlTextureFromDeviceBayer16(deviceBayer16,"));
    const int gate = warm.indexOf(QStringLiteral("    if ( amazeOk && playbackWarmOutputReuseEnabled() )\n    {\n        quint64 captureId = 0;"));
    const int capture = warm.indexOf(QStringLiteral("gpuAmazeDebayerCaptureInputBayer16(deviceBayer16, width, height, &captureId, &captureWhy)"));
    const int keyed = warm.indexOf(QStringLiteral("m_stagedReconWarmKey = key;"));
    ASSERT_TRUE(amaze >= 0);
    ASSERT_TRUE(gate > amaze);
    ASSERT_TRUE(capture > gate);
    ASSERT_TRUE(keyed > capture);
    ASSERT_EQ(1, (int)warm.count(QStringLiteral("m_stagedReconWarmKey = ")));
    // the key carries the capture id and every non-content AMaZE input
    for (const char *field : {"key.valid = true;", "key.frame = frame;", "key.width = width;", "key.height = height;",
                              "key.blackLevel = blackLevel;", "key.wb[0] = wbMultipliers[0];", "key.wb[1] = wbMultipliers[1];",
                              "key.wb[2] = wbMultipliers[2];", "key.reducedHnyquist = reducedHnyquist;",
                              "key.setClipCountAfterWarm = setClipCountAfter;", "key.setLutsCountAfterWarm = setLutsCountAfter;",
                              "key.captureId = captureId;"})
        ASSERT_TRUE(warm.contains(QString::fromLatin1(field)));
    // MainWindow hands the warm its frame and the counts read on the worker right after the run
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    ASSERT_TRUE(source.contains(QStringLiteral(
        "static_cast<uint64_t>( done.frameNumber ), outcome.setClipCountAfter, outcome.setLutsCountAfter,")));
    const QString core = sourceFile("platform/qt/PlaybackReconWarmRun.h");
    const int after = core.indexOf(QStringLiteral("out.setClipCountAfter = llrpGpuPlaybackReconSetClipCount();"));
    ASSERT_TRUE(after > core.indexOf(QStringLiteral("applyLLRawProcObjectWorker( video, raw.data()")));
    ASSERT_TRUE(core.contains(QStringLiteral("out.setLutsCountAfter = llrpGpuPlaybackReconSetLutsCount();")));
}

TEST(PlaybackWarmOutputReuseWiring, TheKeyIsClearedWithTheSlotAndByAReRunPrewarm)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString clear = bodyBetween(window, QStringLiteral("void GpuDisplayWindow::clearStagedReconWarmKey()"),
                                      QStringLiteral("void GpuDisplayWindow::releaseStagedReconSlot()"));
    ASSERT_TRUE(clear.contains(QStringLiteral("m_stagedReconWarmKey = playback_warm_output_reuse::Key();")));
    ASSERT_TRUE(clear.contains(QStringLiteral("if ( held ) gpuAmazeDebayerReleaseInputCapture();")));
    // the slot release clears the key first, before its no-pair early return
    const QString release = bodyBetween(window, QStringLiteral("void GpuDisplayWindow::releaseStagedReconSlot()"),
                                        QStringLiteral("void GpuDisplayWindow::cleanupGLResources()"));
    const int cleared = release.indexOf(QStringLiteral("clearStagedReconWarmKey();"));
    const int early = release.indexOf(QStringLiteral("if ( !m_stagedReconOutput && !m_stagedReconSource ) return;"));
    ASSERT_TRUE(cleared >= 0);
    ASSERT_TRUE(early > cleared);
    // cleanupGLResources (window/fullscreen toggle, context loss) goes through the slot release
    const QString cleanup = bodyBetween(window, QStringLiteral("void GpuDisplayWindow::cleanupGLResources()"),
                                        QStringLiteral("    destroyTexture();"));
    ASSERT_TRUE(cleanup.contains(QStringLiteral("releaseStagedReconSlot();")));
    // a re-run prewarm clears it at the top, before its own early returns
    const QString prewarm = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth"),
                                        QStringLiteral("    QOpenGLContext *glContext = context();"));
    ASSERT_TRUE(prewarm.contains(QStringLiteral("clearStagedReconWarmKey();")));
}

TEST(PlaybackWarmOutputReuseWiring, TheAdoptingPresentMovesTheKeyOutBeforeTheStagedMembersAreCleared)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString present = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture("),
                                        QStringLiteral("double GpuDisplayWindow::verifyWarmOutputReuse("));
    ASSERT_FALSE(present.isEmpty());
    const int adopt = present.indexOf(QStringLiteral("if ( stagedSlot == playback_route_staged_slot::Decision::Adopt )"));
    const int moved = present.indexOf(QStringLiteral("warmKey = m_stagedReconWarmKey;"), adopt);
    const int emptied = present.indexOf(QStringLiteral("m_stagedReconWarmKey = playback_warm_output_reuse::Key();"), adopt);
    const int adopted = present.indexOf(QStringLiteral("stagedAdopted = true;"), adopt);
    const int stagedCleared = present.indexOf(QStringLiteral("m_stagedReconOutput = nullptr;"), adopt);
    ASSERT_TRUE(adopt >= 0);
    ASSERT_TRUE(moved > adopt);
    ASSERT_TRUE(emptied > moved);
    ASSERT_TRUE(adopted > moved);
    ASSERT_TRUE(stagedCleared > adopted);
    // a key the present did not move out is consumed anyway
    ASSERT_TRUE(present.contains(QStringLiteral("if ( !stagedAdopted && m_stagedReconWarmKey.valid ) clearStagedReconWarmKey();")));
}

TEST(PlaybackWarmOutputReuseWiring, TheGateAndTheCompareComeBeforeTheDeviceAmazeWhichStaysOnEveryOtherPath)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString present = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture("),
                                        QStringLiteral("double GpuDisplayWindow::verifyWarmOutputReuse("));
    const int dims = present.indexOf(QStringLiteral("          && deviceHeight == texHeight )"));
    const int gate = present.indexOf(QStringLiteral("playback_warm_output_reuse::Reason reuseReason = playback_warm_output_reuse::preGate("));
    const int onNone = present.indexOf(QStringLiteral("if ( reuseReason == playback_warm_output_reuse::Reason::None )"));
    const int compare = present.indexOf(QStringLiteral("gpuAmazeDebayerInputBayer16MatchesCapture("));
    const int released = present.indexOf(QStringLiteral("if ( warmKey.valid ) gpuAmazeDebayerReleaseInputCapture();"));
    const int amaze = present.indexOf(QStringLiteral(
        "                warmOutputReused\n"
        "                || gpuAmazeDebayerRenderPostWbGlTextureFromDeviceBayer16(\n"
        "                    deviceBayer16,\n"
        "                    m_texture->textureId(),"));
    ASSERT_TRUE(dims >= 0);
    ASSERT_TRUE(gate > dims);
    ASSERT_TRUE(onNone > gate);
    ASSERT_TRUE(compare > onNone);
    ASSERT_TRUE(released > compare);
    ASSERT_TRUE(amaze > released);
    // the gate reads the live state: the frame id, the dims, black, WB, H-Nyquist, the loss flag, the probe, the counts
    const QString gateCall = present.mid(gate, onNone - gate);
    for (const char *input : {"playbackWarmOutputReuseEnabled(),", "stagedAdopted,", "warmKey,", "presentKey,",
                              "llrpGpuCudaSuppressed() != 0,", "validationProbeTexture,",
                              "llrpGpuPlaybackReconSetClipCount(),", "llrpGpuPlaybackReconSetLutsCount());"})
        ASSERT_TRUE(gateCall.contains(QString::fromLatin1(input)));
    for (const char *field : {"presentKey.frame = state->frame_id;", "presentKey.width = texWidth;", "presentKey.height = texHeight;",
                              "presentKey.blackLevel = blackLevel;", "presentKey.wb[2] = wbMultipliers[2];",
                              "presentKey.reducedHnyquist = reducedHnyquist;"})
        ASSERT_TRUE(present.contains(QString::fromLatin1(field)));
    // only a hit skips the call: reuse() is true for Hit alone (PlaybackWarmOutputReusePolicy.h)
    ASSERT_TRUE(present.contains(QStringLiteral("const bool warmOutputReused = playback_warm_output_reuse::reuse(reuseReason);")));
    ASSERT_EQ(1, (int)present.count(QStringLiteral("warmOutputReused\n                || ")));
    // the validation-proof step and the r16 bridge fallback are untouched
    ASSERT_TRUE(present.indexOf(QStringLiteral("const bool validationProofOk =")) > amaze);
    ASSERT_TRUE(present.contains(QStringLiteral("            && gpuAmazeDebayerRenderPostWbGlTextureFromR16GlTexture(\n")));
    // the capture is consumed on the paths that never reached the compare, too
    ASSERT_EQ(2, (int)present.count(QStringLiteral("if ( warmKey.valid ) gpuAmazeDebayerReleaseInputCapture();")));
    // R4: the identity check runs after the paint and the swap, only on an identity leg, only on a reused frame
    const int swap = present.indexOf(QStringLiteral("glContext->swapBuffers(this);"));
    const int verify = present.indexOf(QStringLiteral("if ( reusedDeviceBayer16 && warmOutputReuseVerifyRequested() )"));
    ASSERT_TRUE(swap >= 0);
    ASSERT_TRUE(verify > swap);
    ASSERT_TRUE(present.contains(QStringLiteral("postMs = elapsedMs() - postStartMs - verifyMs;")));
    ASSERT_TRUE(window.contains(QStringLiteral("windowEnvFlagEnabled( qgetenv( \"MLVAPP_SMOKE_FIRST_PRESENT_CAPTURE\" ) )")));
    // PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1: default OFF (opt-in) until a venue shows the reuse hitting
    ASSERT_TRUE(window.contains(QStringLiteral("qgetenv( \"MLVAPP_PLAYBACK_WARM_OUTPUT_REUSE\" ).constData() ), false );")));
    // the verify unregisters its scratch texture from the AMaZE cache before freeing it
    const QString check = bodyBetween(window, QStringLiteral("double GpuDisplayWindow::verifyWarmOutputReuse("),
                                      QStringLiteral("bool GpuDisplayWindow::readGpuReconSourceBayer16Texture("));
    const int reset = check.indexOf(QStringLiteral("gpuAmazeDebayerResetR16TextureBackendResources();"));
    ASSERT_TRUE(reset >= 0);
    ASSERT_TRUE(check.indexOf(QStringLiteral("delete scratch;")) > reset);
}

TEST(PlaybackWarmOutputReuseWiring, TheBackendWrappersRespectTheAmazeRunCensusAndNeverLoadTheDll)
{
    const QString debayer = sourceFile("platform/qt/GpuDebayer.cpp");
    // the census forms (test_gpu_device_loss_breaker.cpp, test_gpu_field_telemetry.cpp): still exactly 4 AMaZE runs
    ASSERT_EQ(4, (int)debayer.count(QStringLiteral("if ( !enterAmazeRunUnlessCudaSuppressed(&")));
    ASSERT_TRUE(debayer.contains(QStringLiteral(
        "const bool mayCapture = enterAmazeRunUnlessCudaSuppressed(&live.runtime, GpuFieldTelemetry::enabled());")));
    ASSERT_TRUE(debayer.contains(QStringLiteral(
        "const bool mayCompare = enterAmazeRunUnlessCudaSuppressed(&live.runtime, GpuFieldTelemetry::enabled());")));
    const QString capture = bodyBetween(debayer, QStringLiteral("bool gpuAmazeDebayerCaptureInputBayer16("),
                                        QStringLiteral("bool gpuAmazeDebayerInputBayer16MatchesCapture("));
    const QString matches = bodyBetween(debayer, QStringLiteral("bool gpuAmazeDebayerInputBayer16MatchesCapture("),
                                        QStringLiteral("void gpuAmazeDebayerReleaseInputCapture(void)"));
    const QString release = bodyBetween(debayer, QStringLiteral("void gpuAmazeDebayerReleaseInputCapture(void)"),
                                        QStringLiteral("\n}\n"));
    ASSERT_FALSE(capture.isEmpty());
    ASSERT_FALSE(matches.isEmpty());
    ASSERT_FALSE(release.isEmpty());
    // code lines only: the comments that say "never live.ensure()" are not calls
    const auto codeOnly = [](const QString & body)
    {
        QStringList kept;
        for (const QString & line : body.split(QLatin1Char('\n')))
            if (!line.trimmed().startsWith(QStringLiteral("//"))) kept << line;
        return kept.join(QLatin1Char('\n'));
    };
    ASSERT_TRUE(capture.contains(QStringLiteral("never live.ensure()")));
    const QString captureCode = codeOnly(capture);
    const QString matchesCode = codeOnly(matches);
    for (const QString *body : {&captureCode, &matchesCode})
    {
        // suppressed first, the live lock, then the re-check; never live.ensure()
        const int first = body->indexOf(QStringLiteral("if ( llrpGpuCudaSuppressed() ) return fail(\"cuda_suppressed\");"));
        const int lock = body->indexOf(QStringLiteral("QMutexLocker locker(&live.mutex);"));
        const int recheck = body->indexOf(QStringLiteral("enterAmazeRunUnlessCudaSuppressed(&live.runtime"));
        ASSERT_TRUE(first >= 0);
        ASSERT_TRUE(lock > first);
        ASSERT_TRUE(recheck > lock);
        ASSERT_FALSE(body->contains(QStringLiteral("live.ensure(")));
    }
    // the release is never gated and never loads
    ASSERT_FALSE(codeOnly(release).contains(QStringLiteral("llrpGpuCudaSuppressed")));
    ASSERT_FALSE(codeOnly(release).contains(QStringLiteral("ensure(")));
    // all three symbols are optional resolves
    for (const char *symbol : {"igpu_amaze_debayer_capture_input_bayer16", "igpu_amaze_debayer_input_bayer16_matches_capture",
                               "igpu_amaze_debayer_release_input_capture"})
    {
        ASSERT_TRUE(debayer.contains(QStringLiteral("runtime.library.resolve(\"%1\")").arg(QString::fromLatin1(symbol))));
        ASSERT_TRUE(sourceFile("tools/gpu/backend/igpu_amaze_debayer_cuda.def").contains(
            QStringLiteral("    %1\n").arg(QString::fromLatin1(symbol))));
    }
    // the backend: no compare is launched without the matching capture, and the release makes no CUDA call
    const QString cu = sourceFile("tools/gpu/backend/igpu_amaze_debayer_cuda.cu");
    const QString match = bodyBetween(cu, QStringLiteral("extern \"C\" int igpu_amaze_debayer_input_bayer16_matches_capture("),
                                      QStringLiteral("extern \"C\" int igpu_amaze_debayer_release_input_capture("));
    const int refuse = match.indexOf(QStringLiteral("if (backend->captureId == 0 || !backend->captureBayer16 || capture_id != backend->captureId"));
    const int launch = match.indexOf(QStringLiteral("k_bayer16_differs<<<"));
    ASSERT_TRUE(refuse >= 0);
    ASSERT_TRUE(launch > refuse);
    const QString rel = bodyBetween(cu, QStringLiteral("extern \"C\" int igpu_amaze_debayer_release_input_capture("),
                                    QStringLiteral("\n}\n"));
    ASSERT_FALSE(rel.isEmpty());
    ASSERT_FALSE(rel.contains(QStringLiteral("cuda")));
    // destroy frees the capture, before the backend is deleted
    const QString destroy = bodyBetween(cu, QStringLiteral("extern \"C\" void igpu_amaze_debayer_destroy("),
                                        QStringLiteral("    delete backend;"));
    ASSERT_TRUE(destroy.contains(QStringLiteral("free_input_capture(backend);")));
}

TEST(PlaybackWarmOutputReuseWiring, TheFirstPresentSplitReadsTheReuseAndAReusedFrameZeroHasNoProbe)
{
    const QString source = sourceFile("platform/qt/MainWindow.cpp");
    for (const char *key : {"gpu_playback_recon_texture_present_warm_output_reuse", "gpu_playback_recon_texture_present_reuse_reason",
                            "gpu_playback_recon_texture_present_reuse_compare_ms", "gpu_playback_recon_texture_present_reuse_verify_ran"})
        ASSERT_EQ(2, (int)source.count(QStringLiteral("\"%1\"").arg(QString::fromLatin1(key))));   // written once, read once
    ASSERT_TRUE(source.contains(QStringLiteral("        if( fp.frame0Reused() )\n        {")));
    ASSERT_TRUE(source.contains(QStringLiteral("            fp.probeValid = false;")));
    ASSERT_TRUE(source.contains(QStringLiteral("frames.frame0Reused = firstSplit.frame0Reused();")));
    // the kick (default off) still ends at the first present
    ASSERT_TRUE(source.contains(QStringLiteral(
        "if( m_playbackGpuClockKickActive && ui->actionPlay->isChecked() ) endPlaybackGpuClockKick( \"first_present\" );")));
    // the warm line carries the capture outcome
    ASSERT_TRUE(source.contains(QStringLiteral("+ playback_warm_output_reuse::captureField( reuseCapture );")));
}

// PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1. Code lines only (the comments around these sites name the calls).
namespace {

QString codeLinesOnly(const QString & body)
{
    QStringList kept;
    bool inBlock = false;   // a /* ... */ comment spanning lines (a code line may start with '*': a dereference)
    for (const QString & line : body.split(QLatin1Char('\n')))
    {
        const QString t = line.trimmed();
        if (inBlock)
        {
            if (t.contains(QStringLiteral("*/"))) inBlock = false;
            continue;
        }
        if (t.startsWith(QStringLiteral("//"))) continue;
        if (t.startsWith(QStringLiteral("/*")))
        {
            inBlock = !t.contains(QStringLiteral("*/"));
            continue;
        }
        kept << line;
    }
    return kept.join(QLatin1Char('\n'));
}

} // namespace

// T-C (hypothesis (c)): the retained copy is a device-to-device cudaMemcpy, which returns before the copy completes and
// runs on the legacy default stream; every reader of the retained pointer (the AMaZE streams, the capture and the
// compare) is cudaStreamNonBlocking and so is not ordered after it. The copy must be host-complete before the pointer
// is handed out, through the launch-timing ledger and the error-slot noting CK.
TEST(PlaybackWarmReconDeterminismWiring, TheRetainedCopyIsHostCompleteBeforeItsPointerIsHandedOut)
{
    const QString cu = sourceFile("tools/gpu/backend/igpu_recon_cuda.cu");
    const QString body = codeLinesOnly(bodyBetween(cu, QStringLiteral("int igpu_recon_retain_last_device_output(igpu_recon_backend* b,"),
                                                   QStringLiteral("int igpu_recon_release_retained_device_output(igpu_recon_backend* b,")));
    ASSERT_FALSE(body.isEmpty());
    const int copy = body.indexOf(QStringLiteral("cudaMemcpyDeviceToDevice"));
    const int handed = body.indexOf(QStringLiteral("*device_bayer16 = b->retained_device_output[slot];"));
    ASSERT_TRUE(copy >= 0);
    ASSERT_TRUE(handed > copy);
    const QString between = body.mid(copy, handed - copy);
    const bool hostComplete = between.contains(QStringLiteral("cudaEventSynchronize("))
                           || between.contains(QStringLiteral("cudaStreamSynchronize("));
    ASSERT_TRUE(hostComplete);
    ASSERT_TRUE(between.contains(QStringLiteral("CK(IGPU_LT_SYNC_CALL(\"retain:")));
}

// F-key: the prepared recon state travels with the warm key and the present's key, and is compared before the compare.
TEST(PlaybackWarmReconDeterminismWiring, ThePreparedStateIsReadBeforeTheRestoreAndKeysBothSides)
{
    const QString core = sourceFile("platform/qt/PlaybackReconWarmRun.h");
    const int apply = core.indexOf(QStringLiteral("applyLLRawProcObjectWorker( video, raw.data()"));
    const int read = core.indexOf(QStringLiteral("llrpGpuPlaybackReconGetLastPreparedState( &prepared )"));
    const int stamped = core.indexOf(QStringLiteral("prepared.frame_id = frame;"));
    const int restore = core.indexOf(QStringLiteral("llrpGpuPlaybackReconRestoreLastRunState( saved );"));
    ASSERT_TRUE(apply >= 0);
    ASSERT_TRUE(read > apply);
    ASSERT_TRUE(stamped > read);
    ASSERT_TRUE(restore > stamped);
    const QString main = sourceFile("platform/qt/MainWindow.cpp");
    ASSERT_TRUE(main.contains(QStringLiteral("const playback_warm_output_reuse::PreparedState warmState = "
                                             "playback_warm_output_reuse::preparedStateFrom( outcome.prepared );")));
    ASSERT_TRUE(main.contains(QStringLiteral("&reuseCapture, &warmState );")));
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString warm = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::warmStagedReconSlotFromDeviceBayer16(const uint16_t"),
                                     QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth"));
    ASSERT_TRUE(warm.contains(QStringLiteral("if ( warmState ) key.state = *warmState;")));
    const QString present = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture("),
                                        QStringLiteral("double GpuDisplayWindow::verifyWarmOutputReuse("));
    const int presentState = present.indexOf(QStringLiteral("presentKey.state = playback_warm_output_reuse::preparedStateFrom( *state );"));
    const int gate = present.indexOf(QStringLiteral("playback_warm_output_reuse::Reason reuseReason = playback_warm_output_reuse::preGate("));
    const int compare = present.indexOf(QStringLiteral("gpuAmazeDebayerInputBayer16MatchesCapture("));
    const int states = present.indexOf(QStringLiteral("setupTiming.reuse_states = playback_warm_output_reuse::stateFields( reuseReason, warmKey, presentKey );"));
    ASSERT_TRUE(presentState >= 0);
    ASSERT_TRUE(gate > presentState);
    ASSERT_TRUE(compare > gate);
    ASSERT_TRUE(states > compare);
    // the states reach the first-present split: written once, read once
    ASSERT_EQ(2, (int)main.count(QStringLiteral("\"gpu_playback_recon_texture_present_reuse_states\"")));
    ASSERT_TRUE(main.contains(QStringLiteral("fp.reuseStates = telemetryStringValue( t, \"gpu_playback_recon_texture_present_reuse_states\" );")));
}

// F-diag: the miss dump is opt-in. Nothing under it runs unless MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR is set, and it
// runs only on a content miss, before the capture is released (the dump reads it).
TEST(PlaybackWarmReconDeterminismWiring, TheMissDumpRunsOnlyWithItsEnvAndOnlyOnAContentMiss)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    ASSERT_EQ(1, (int)window.count(QStringLiteral("qgetenv( \"MLVAPP_PLAYBACK_WARM_REUSE_MISS_DUMP_DIR\" )")));
    const QString dump = bodyBetween(window, QStringLiteral("void GpuDisplayWindow::dumpWarmOutputReuseMiss("),
                                     QStringLiteral("\n}\n"));
    ASSERT_FALSE(dump.isEmpty());
    const QString code = codeLinesOnly(dump);
    const int env = code.indexOf(QStringLiteral("const QString dir = warmOutputReuseMissDumpDir();"));
    const int gate = code.indexOf(QStringLiteral("if ( dir.isEmpty() ) return;"));
    const int download = code.indexOf(QStringLiteral("gpuAmazeDebayerDownloadBayer16ForDiagnostics("));
    ASSERT_TRUE(env >= 0);
    ASSERT_TRUE(gate > env);
    ASSERT_TRUE(download > gate);
    const QString present = bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture("),
                                        QStringLiteral("double GpuDisplayWindow::verifyWarmOutputReuse("));
    const int onMiss = present.indexOf(QStringLiteral("if ( reuseReason == playback_warm_output_reuse::Reason::ContentMismatch )"));
    const int called = present.indexOf(QStringLiteral("dumpWarmOutputReuseMiss( deviceBayer16, texWidth, texHeight, warmKey, presentKey );"));
    const int released = present.indexOf(QStringLiteral("if ( warmKey.valid ) gpuAmazeDebayerReleaseInputCapture();"));
    ASSERT_TRUE(onMiss >= 0);
    ASSERT_TRUE(called > onMiss);
    ASSERT_TRUE(released > called);
    // the backend export is optional, and like the capture it never loads the DLL and re-checks the loss flag
    const QString debayer = sourceFile("platform/qt/GpuDebayer.cpp");
    ASSERT_TRUE(debayer.contains(QStringLiteral("runtime.library.resolve(\"igpu_amaze_debayer_download_bayer16\")")));
    ASSERT_TRUE(debayer.contains(QStringLiteral(
        "const bool mayDownload = enterAmazeRunUnlessCudaSuppressed(&live.runtime, GpuFieldTelemetry::enabled());")));
    ASSERT_TRUE(sourceFile("tools/gpu/backend/igpu_amaze_debayer_cuda.def").contains(
        QStringLiteral("    igpu_amaze_debayer_download_bayer16\n")));
}

// r2 (sol r1 B1): with MLVAPP_PLAYBACK_WARM_OUTPUT_REUSE unset (default OFF) a successful paused warm makes no reuse
// capture at all: no device/pinned allocation, copy, stream sync or retained storage. The capture call's enclosing
// branch reads the switch; it is the only capture call, and the backend export is reached only through that wrapper.
TEST(PlaybackWarmReconDeterminismWiring, TheWarmCapturesItsInputOnlyWhenTheReuseIsOn)
{
    const QString window = sourceFile("platform/qt/GpuDisplayWindow.cpp");
    const QString warm = codeLinesOnly(bodyBetween(window, QStringLiteral("bool GpuDisplayWindow::warmStagedReconSlotFromDeviceBayer16(const uint16_t"),
                                                   QStringLiteral("bool GpuDisplayWindow::prewarmPlaybackReconRoute(int texWidth")));
    ASSERT_FALSE(warm.isEmpty());
    const QStringList lines = warm.split(QLatin1Char('\n'));
    int captureLine = -1;
    for (int i = 0; i < lines.size(); ++i)
        if (lines[i].contains(QStringLiteral("gpuAmazeDebayerCaptureInputBayer16("))) { captureLine = i; break; }
    ASSERT_TRUE(captureLine >= 0);
    // walk out of the capture's block: the condition of the first unmatched '{' above it
    int depth = 0;
    QString enclosing;
    for (int i = captureLine - 1; i >= 0 && enclosing.isEmpty(); --i)
    {
        depth += lines[i].count(QLatin1Char('}')) - lines[i].count(QLatin1Char('{'));
        if (depth < 0)
        {
            const bool braceOnly = lines[i].trimmed() == QStringLiteral("{");
            enclosing = (braceOnly && i > 0) ? lines[i - 1] : lines[i];
        }
    }
    printf("[B1] capture branch: %s\n", enclosing.trimmed().toUtf8().constData());
    ASSERT_TRUE(enclosing.contains(QStringLiteral("if (")));
    ASSERT_TRUE(enclosing.contains(QStringLiteral("amazeOk")));
    ASSERT_TRUE(enclosing.contains(QStringLiteral("playbackWarmOutputReuseEnabled()")));
    // the default answer when it does not run
    ASSERT_TRUE(warm.indexOf(QStringLiteral("if ( reuseCapture ) *reuseCapture = QStringLiteral(\"none\");")) >= 0);
    ASSERT_TRUE(warm.indexOf(QStringLiteral("if ( reuseCapture ) *reuseCapture = QStringLiteral(\"none\");"))
                < warm.indexOf(QStringLiteral("gpuAmazeDebayerCaptureInputBayer16(")));
    ASSERT_EQ(1, (int)codeLinesOnly(window).count(QStringLiteral("gpuAmazeDebayerCaptureInputBayer16(")));
    const QString debayer = codeLinesOnly(sourceFile("platform/qt/GpuDebayer.cpp"));
    ASSERT_EQ(1, (int)debayer.count(QStringLiteral("live.runtime.captureInputBayer16(")));
    const QString wrapper = bodyBetween(debayer, QStringLiteral("bool gpuAmazeDebayerCaptureInputBayer16("),
                                        QStringLiteral("bool gpuAmazeDebayerInputBayer16MatchesCapture("));
    ASSERT_TRUE(wrapper.contains(QStringLiteral("live.runtime.captureInputBayer16(")));
}

// r2 (sol r1 B2): a clip close waits only for RenderFrameThread::isIdle() / lock(), and a running warm counts as busy
// only while m_warmRunning holds. Nothing the warm entry runs after that guard is released may read the clip: not the
// rest of warmReconForWorker, and not signalReconDoneFromWorker( -1 )'s recon-done stamp (a warm has no slot to stamp).
// A warm dropped at take never held the guard, so it reads nothing of the clip either.
TEST(PlaybackWarmReconDeterminismWiring, NoWarmEntryReadsTheClipAfterItsLifetimeGuardIsReleased)
{
    const QString thread = sourceFile("platform/qt/RenderFrameThread.cpp");
    // the completion stamp is taken only for a slot (Debayered16ReconReusePolicy.h), never read unconditionally
    const QString done = codeLinesOnly(bodyBetween(thread, QStringLiteral("void RenderFrameThread::signalReconDoneFromWorker( int slotIndex )"),
                                                   QStringLiteral("int RenderFrameThread::waitForDecodedSlotLocked( void )")));
    ASSERT_FALSE(done.isEmpty());
    ASSERT_FALSE(done.contains(QStringLiteral("const Debayered16ReconDoneStamp reconDone = debayered16ReconDoneStamp( m_pMlvObject );")));
    ASSERT_TRUE(done.contains(QStringLiteral("debayered16ReconDoneStampForEntry(")));
    ASSERT_TRUE(done.contains(QStringLiteral("slotIndex, [this]() { return debayered16ReconDoneStamp( m_pMlvObject ); } );")));
    ASSERT_EQ(1, (int)done.count(QStringLiteral("m_pMlvObject")));
    // a warm entry is queued without a slot, and the worker signals its completion with that slot
    ASSERT_TRUE(thread.contains(QStringLiteral("    entry.slotIndex = -1;\n    entry.request.frameNumber = frameNumber;\n    entry.warm = true;")));
    const QString worker = sourceFile("platform/qt/ReconWorker.cpp");
    const int recon = worker.indexOf(QStringLiteral("m_parent->reconFrameForWorker( entry, &m_workerState );"));
    const int signal = worker.indexOf(QStringLiteral("m_parent->signalReconDoneFromWorker( entry.slotIndex );"));
    ASSERT_TRUE(recon >= 0);
    ASSERT_TRUE(signal > recon);
    // the rest of the warm: nothing after the guard release reads the clip, and a dropped warm reads none of it
    const QString warm = codeLinesOnly(bodyBetween(thread, QStringLiteral("void RenderFrameThread::warmReconForWorker("),
                                                   QStringLiteral("void RenderFrameThread::reconFrameForWorker(")));
    ASSERT_FALSE(warm.isEmpty());
    const int released = warm.indexOf(QStringLiteral("m_warmRunning = false;"));
    ASSERT_TRUE(released >= 0);
    ASSERT_EQ(-1, (int)warm.indexOf(QStringLiteral("m_pMlvObject"), released));
    ASSERT_TRUE(warm.contains(QStringLiteral(
        "done.blackLevel = ( m_pMlvObject && !entry.warmRealRequestPending ) ? getMlvBlackLevel( m_pMlvObject ) : 0;")));
}
