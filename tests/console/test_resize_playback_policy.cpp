// PLAYER-FULLSCREEN-KEEPS-PLAYING-1: MainWindow::resizeEvent unchecked Play on every resize, so toggling full screen,
// maximizing / restoring or dragging the window ended playback. The decision is now one pure function
// (ResizePlaybackPolicy.h): a resize during Play keeps playing, and no Play is stopped, so none is resumed. These pin its
// truth table and, by source scan (MainWindow.cpp needs a full GUI build, so it is read as text, mirroring
// test_resize_still_drawing_wait.cpp), that resizeEvent routes through it and that the KeepPlaying path does none of
// the stopped-frame redraw work.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/ResizePlaybackPolicy.h"
#include "../../platform/qt/PlaybackSlotRelease.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QTextStream>

#include <string>

using resize_playback_policy::Action;
using resize_playback_policy::actionName;
using resize_playback_policy::decide;

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    if (path.isEmpty()) return QString();
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) return QString();
    QTextStream stream(&file);
    return stream.readAll();
}

// The resizeEvent body with // comments removed (a comment naming a forbidden call is not a call).
QString resizeEventCode()
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int at = source.indexOf(QStringLiteral("void MainWindow::resizeEvent(QResizeEvent *event)"));
    const int next = source.indexOf(QStringLiteral("void MainWindow::changeEvent( QEvent *event )"), at);
    if (at < 0 || next <= at) return QString();
    QString body = source.mid(at, next - at);
    body.remove(QRegularExpression(QStringLiteral("//[^\\n]*")));
    return body;
}

// The text of one case of the resize switch, up to the next case label (or the end of the body).
QString caseBody(const QString & body, const QString & action)
{
    const QString label = QStringLiteral("case resize_playback_policy::Action::%1:").arg(action);
    const int at = body.indexOf(label);
    if (at < 0) return QString();
    const int next = body.indexOf(QStringLiteral("case resize_playback_policy::Action::"), at + label.size());
    return next < 0 ? body.mid(at) : body.mid(at, next - at);
}

std::string name(Action action) { return std::string(actionName(action)); }

// A MainWindow function body by brace matching, // comments removed ('' when the signature is absent).
QString mainWindowFunction(const QString & signature)
{
    QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    source.remove(QRegularExpression(QStringLiteral("//[^\\n]*")));
    const int at = source.indexOf(signature);
    if (at < 0) return QString();
    const int open = source.indexOf(QLatin1Char('{'), at);
    if (open < 0) return QString();
    int depth = 0;
    for (int i = open; i < source.size(); ++i)
    {
        if (source.at(i) == QLatin1Char('{')) ++depth;
        else if (source.at(i) == QLatin1Char('}') && --depth == 0) return source.mid(at, i + 1 - at);
    }
    return QString();
}

} // namespace

TEST(ResizePlaybackPolicy, TheFullTruthTable)
{
    for (int bits = 0; bits < 8; ++bits)
    {
        const bool opening = (bits & 4) != 0;
        const bool lookAssist = (bits & 2) != 0;
        const bool play = (bits & 1) != 0;
        const Action got = decide(opening, lookAssist, play);
        Action want = Action::RedrawStopped;
        if (opening) want = Action::AcceptOnly;                  // an open in progress wins every input
        else if (lookAssist) want = Action::HoldForLookAssist;   // the Look Assist hold wins over Play
        else if (play) want = Action::KeepPlaying;               // Play keeps playing, never a stop
        ASSERT_EQ(name(want), name(got));
    }
}

TEST(ResizePlaybackPolicy, ActionsHaveTheLogNames)
{
    ASSERT_EQ(std::string("accept_only"), name(Action::AcceptOnly));
    ASSERT_EQ(std::string("hold_for_look_assist"), name(Action::HoldForLookAssist));
    ASSERT_EQ(std::string("keep_playing"), name(Action::KeepPlaying));
    ASSERT_EQ(std::string("redraw_stopped"), name(Action::RedrawStopped));
}

TEST(ResizePlaybackPolicyWiring, ResizeEventDecidesOnceAndNeverStopsOrStartsPlay)
{
    const QString body = resizeEventCode();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_EQ(1, static_cast<int>(body.count(QStringLiteral("resize_playback_policy::decide("))));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "resize_playback_policy::decide( m_inOpeningProcess, m_lookAssistPump.sessionOpen(), ui->actionPlay->isChecked() )")));
    // no stop, so no resume (ENFORCE-2: a resume would be a programmatic Play)
    ASSERT_FALSE(body.contains(QStringLiteral("actionPlay->setChecked")));
    ASSERT_FALSE(body.contains(QStringLiteral("actionPlay->trigger")));
    ASSERT_FALSE(body.contains(QStringLiteral("programmaticPlay")));
    ASSERT_FALSE(body.contains(QStringLiteral("on_actionPlay_")));
}

TEST(ResizePlaybackPolicyWiring, TheKeepPlayingCaseDoesNoStoppedFrameWork)
{
    const QString body = resizeEventCode();
    ASSERT_FALSE(body.isEmpty());
    const QString keep = caseBody(body, QStringLiteral("KeepPlaying"));
    ASSERT_FALSE(keep.isEmpty());
    ASSERT_FALSE(keep.contains(QStringLiteral("waitForResizeStillDrawing")));
    ASSERT_FALSE(keep.contains(QStringLiteral("m_pRenderThread->lock")));
    ASSERT_FALSE(keep.contains(QStringLiteral("drawFrame(")));
    ASSERT_FALSE(keep.contains(QStringLiteral("m_pAudioPlayback")));
    // it accepts, logs once, and returns before the stopped-frame redraw
    ASSERT_TRUE(keep.contains(QStringLiteral("event->accept();")));
    ASSERT_TRUE(keep.contains(QStringLiteral("return;")));
    ASSERT_EQ(1, static_cast<int>(keep.count(QStringLiteral("QStringLiteral(\"resize.keep_playing\")"))));
    // the gradient re-anchors on the next landed playback frame (finishPresentedFrame consumes the flag)
    ASSERT_TRUE(keep.contains(QStringLiteral("m_zoomModeChanged = true;")));
}

// Adversary A2 on c6993e68, its literal input: a 100-frame clip, cut 100..100, Loop on, ordinary Play, index 99 drawn
// (lastDrawnPlaybackPosition=99), no render outstanding; then a resize. The tick marks the frame changed at the SAME
// position, and the duplicate-position suppression swallowed it, so the picture kept its old size forever.
TEST(ResizePlaybackPolicy, A2AResizeDuringAOneFramePlayRedrawsPastTheDuplicateSuppression)
{
    using resize_playback_policy::suppressDuplicatePlaybackDraw;
    // master's suppression is unchanged without a resize: a tick at the drawn position re-presents nothing
    ASSERT_TRUE(suppressDuplicatePlaybackDraw(true, 99, 99, false));
    // the A2 input after a KeepPlaying resize: the same position is drawn again, at the new geometry
    ASSERT_FALSE(suppressDuplicatePlaybackDraw(true, 99, 99, true));
    // an advanced position or an unchanged frame is never suppressed
    ASSERT_FALSE(suppressDuplicatePlaybackDraw(true, 100, 99, false));
    ASSERT_FALSE(suppressDuplicatePlaybackDraw(false, 99, 99, false));
    ASSERT_FALSE(suppressDuplicatePlaybackDraw(false, 99, 99, true));
}

TEST(ResizePlaybackPolicyWiring, A2TheKeepPlayingResizeArmsOneRedrawThatTheTickConsumes)
{
    const QString keep = caseBody(resizeEventCode(), QStringLiteral("KeepPlaying"));
    ASSERT_FALSE(keep.isEmpty());
    ASSERT_EQ(1, static_cast<int>(keep.count(QStringLiteral("m_playbackRedrawAfterResize = true;"))));
    ASSERT_EQ(1, static_cast<int>(keep.count(QStringLiteral("m_frameChanged = true;"))));
    // the tick draws it, not the handler (no stopped-frame work in the case: TheKeepPlayingCaseDoesNoStoppedFrameWork)
    const QString tick = mainWindowFunction(QStringLiteral("void MainWindow::timerFrameEvent( bool predictivePlaybackAdvance )"));
    ASSERT_FALSE(tick.isEmpty());
    const int suppress = tick.indexOf(QStringLiteral("resize_playback_policy::suppressDuplicatePlaybackDraw( m_frameChanged,"));
    ASSERT_TRUE(suppress >= 0);
    // the full argument list, verbatim on one line, so every bool argument is pinned (POLICY-CALLSITE-BOOL-MUTATION-SMOKE-1)
    ASSERT_TRUE(tick.mid(suppress, 600).contains(QStringLiteral("resize_playback_policy::suppressDuplicatePlaybackDraw( m_frameChanged, ui->horizontalSliderPosition->value(), lastDrawnPlaybackPosition, m_playbackRedrawAfterResize ) )")));
    ASSERT_FALSE(tick.contains(QStringLiteral("ui->horizontalSliderPosition->value() == lastDrawnPlaybackPosition")));
    // one draw consumes it; a stopped Play forgets it
    const int draw = tick.indexOf(QStringLiteral("m_frameChanged = false; "), suppress);
    ASSERT_TRUE(draw > suppress);
    ASSERT_TRUE(tick.mid(draw, 120).contains(QStringLiteral("m_playbackRedrawAfterResize = false;")));
    ASSERT_EQ(2, static_cast<int>(tick.count(QStringLiteral("m_playbackRedrawAfterResize = false;"))));
}

// Adversary A1 on c6993e68, its literal input: 1920x1080, Play, ZoomFit, CPU OutputProcessed8 fast-scale route; a frame
// queued for scene 960x540, then a resize to 640x360 before frameReady. That frame was pre-scaled to 960x540
// (RenderFrameThread playbackScaledWidth) and drawFrameReady set the stale scene from it. The KeepPlaying resize now runs
// the display-change invalidation: a new presentation generation, so every prepared, pending or in-flight frame of the
// old geometry is dropped (both present paths check the generation) and the old generation's render requests cancelled.
TEST(ResizePlaybackPolicyWiring, A1TheKeepPlayingResizeRetiresEveryFrameOfTheOldGeometry)
{
    const QString keep = caseBody(resizeEventCode(), QStringLiteral("KeepPlaying"));
    ASSERT_FALSE(keep.isEmpty());
    ASSERT_EQ(1, static_cast<int>(keep.count(QStringLiteral("invalidatePlaybackPrepForDisplayChange( \"resize-keep-playing\" );"))));
    // before the redraw it arms, so the redrawn frame is of the new generation
    ASSERT_TRUE(keep.indexOf(QStringLiteral("invalidatePlaybackPrepForDisplayChange(")) < keep.indexOf(QStringLiteral("m_playbackRedrawAfterResize = true;")));
    const QString invalidate = mainWindowFunction(QStringLiteral("void MainWindow::invalidatePlaybackPrepForDisplayChange( const char *reason )"));
    ASSERT_TRUE(invalidate.contains(QStringLiteral("m_playbackPresentationGeneration.fetch_add( 1, std::memory_order_acq_rel )")));
    ASSERT_TRUE(invalidate.contains(QStringLiteral("m_playbackPrepResults.clear();")));
    ASSERT_TRUE(invalidate.contains(QStringLiteral("cancelPlaybackPresentationRequests( oldGeneration )")));
    // the siblings: the prep present path and the direct ready-frame path both drop a frame of a retired generation
    const QString prepReady = mainWindowFunction(QStringLiteral("void MainWindow::onPlaybackPrepResultReady("));
    ASSERT_TRUE(prepReady.contains(QStringLiteral("playbackPrepCompletedResultShouldPresent(")));
    const QString ready = mainWindowFunction(QStringLiteral("void MainWindow::drawFrameReady("));
    ASSERT_TRUE(ready.contains(QStringLiteral("requestContext.presentationGeneration != activeGeneration")));
}

// Adversary A1 on 4bbdcd9a, its literal input: 100 frames, cut 1..100, Loop off, Play, CUDA recon texture route,
// MLVAPP_PHASE3_FRAME_SLOT_COUNT=1, MLVAPP_PLAYBACK_RENDER_LOOKAHEAD_FRAMES=2. Frame 10 presented; the resize invalidates
// (generation G -> G+1) while speculative frame 11 of G renders in the sole slot. It is published Ready with G later:
// both target-aware acquisitions skip it, the prune skipped other generations, so the G+1 redraw waited for that slot
// forever. Such a slot is released; a Presenting slot, a non-lookahead slot or one of the active generation never is.
TEST(ResizePlaybackPolicy, A1bARetiredGenerationsReadyLookaheadSlotIsReleased)
{
    const uint64_t G = 41;
    ASSERT_TRUE(playbackRetiredLookaheadSlotReleases(PlaybackSlotHold::Ready, true, G, G + 1));     // the A1 input
    ASSERT_FALSE(playbackRetiredLookaheadSlotReleases(PlaybackSlotHold::Ready, true, G + 1, G + 1)); // the active one
    ASSERT_FALSE(playbackRetiredLookaheadSlotReleases(PlaybackSlotHold::Ready, true, G + 2, G + 1)); // never a newer one
    ASSERT_FALSE(playbackRetiredLookaheadSlotReleases(PlaybackSlotHold::Presenting, true, G, G + 1)); // pinned by its borrower
    ASSERT_FALSE(playbackRetiredLookaheadSlotReleases(PlaybackSlotHold::Other, true, G, G + 1));      // still rendering
    ASSERT_FALSE(playbackRetiredLookaheadSlotReleases(PlaybackSlotHold::Ready, false, G, G + 1));     // a non-lookahead slot
}

TEST(ResizePlaybackPolicyWiring, A1bEverySiteThatSkipsARetiredLookaheadSlotFreesItFirst)
{
    QString source = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    source.remove(QRegularExpression(QStringLiteral("//[^\\n]*")));
    ASSERT_FALSE(source.isEmpty());
    // the class: the two target-aware acquisitions and the forward-window prune each skip another generation's lookahead slot
    const char *sites[] = {
        "bool RenderFrameThread::acquireOldestGpuTextureNoReadbackReadyFrameForPlaybackTarget(",
        "bool RenderFrameThread::acquireLatestReadyFrameForPlaybackTarget(",
        "int RenderFrameThread::prunePlaybackLookaheadOutsideForwardWindow(",
    };
    for (const char *site : sites)
    {
        const int at = source.indexOf(QString::fromLatin1(site));
        ASSERT_TRUE(at >= 0);
        const int next = source.indexOf(QStringLiteral("\n}\n"), at);
        const QString body = source.mid(at, next - at);
        const int release = body.indexOf(QStringLiteral("releaseRetiredPlaybackLookaheadSlotsLocked( activeGeneration );"));
        const int skip = body.indexOf(QStringLiteral("presentationGeneration != activeGeneration"));
        ASSERT_TRUE(release > 0);
        ASSERT_TRUE(skip > release);
    }
    const int helper = source.indexOf(QStringLiteral("int RenderFrameThread::releaseRetiredPlaybackLookaheadSlotsLocked( uint64_t activeGeneration )"));
    ASSERT_TRUE(helper >= 0);
    const QString helperBody = source.mid(helper, source.indexOf(QStringLiteral("\n}\n"), helper) - helper);
    ASSERT_TRUE(helperBody.contains(QStringLiteral("playbackRetiredLookaheadSlotReleases( hold,")));
    ASSERT_TRUE(helperBody.contains(QStringLiteral("m_waitCondition.wakeAll();")));
}

TEST(ResizePlaybackPolicyWiring, TheStoppedRedrawAndTheLookAssistHoldAreUnchanged)
{
    const QString body = resizeEventCode();
    ASSERT_FALSE(body.isEmpty());
    // the Look Assist hold still re-posts the resize at landing, where decide() runs again
    const QString hold = caseBody(body, QStringLiteral("HoldForLookAssist"));
    ASSERT_TRUE(hold.contains(QStringLiteral("m_lookAssistPump.hold(")));
    ASSERT_TRUE(hold.contains(QStringLiteral("resizeEvent( &heldResize );")));
    // the open-in-progress case only accepts
    const QString accept = caseBody(body, QStringLiteral("AcceptOnly"));
    ASSERT_TRUE(accept.contains(QStringLiteral("event->accept();")));
    ASSERT_FALSE(accept.contains(QStringLiteral("drawFrame(")));
    // Play off: the audio backstop and the redraw block run after the switch
    const int stopped = body.indexOf(QStringLiteral("case resize_playback_policy::Action::RedrawStopped:"));
    const int audio = body.indexOf(QStringLiteral("m_pAudioPlayback->stop();"));
    const int draw = body.indexOf(QStringLiteral("drawFrame();"));
    ASSERT_TRUE(stopped >= 0);
    ASSERT_TRUE(audio > stopped);
    ASSERT_TRUE(draw > audio);
    ASSERT_EQ(1, static_cast<int>(body.count(QStringLiteral("m_pAudioPlayback->stop();"))));
}

// REFERENCE-FRAME-PRESENTED-COUNT-LOOP-EXIT-1 r8 (the r7 venue run on 970ef3f9: observed=470 of 480; the pure rules are pinned by
// PlaybackMeasuredLoopExit.TheVenueSequenceNeverStopsOnARawCountAndEveryFramePlayPresentsEveryWindowFrame): the wiring. Every-frame
// Play acquires the oldest ready frame on BOTH ready-frame acquisitions; the pinned capture's present-path stop counts the distinct
// pinned-window frames (never raw presents alone; the pinned raw-count statement is unchanged and stays a necessary condition) and
// the exit-11 check judges the same ledger, armed from the frame after the counter's start frame.
TEST(ResizePlaybackPolicyWiring, R8EveryFramePlayAcquiresOldestFirstAndThePinnedStopAndGateCountTheSameWindowFrames)
{
    const QString ready = mainWindowFunction(QStringLiteral("void MainWindow::drawFrameReady("));
    ASSERT_FALSE(ready.isEmpty());
    ASSERT_TRUE(ready.contains(QStringLiteral("playback_frame_range::playbackAcquiresOldestReadyFirst(")));
    // r8 fix 1: a pinned capture's Play too (the automation forces drop-frame mode on: the r7 venue log's drop_frame=1)
    ASSERT_TRUE(ready.contains(QStringLiteral(
        "ui->actionPlay->isChecked(), ui->actionDropFrameMode->isChecked(), m_playbackSmokeTargetPresentedFrames > 0 );")));
    ASSERT_TRUE(ready.contains(QStringLiteral("acquireLatestReadyFrame( &readyFrame, oldestReadyFirst )")));
    ASSERT_TRUE(ready.contains(QRegularExpression(QStringLiteral(
        "acquireLatestReadyFrameForPlaybackTarget\\(\\s*&readyFrame,\\s*activePlaybackTarget,\\s*activeGeneration,\\s*oldestReadyFirst\\s*\\)"))));

    QString render = readRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    render.remove(QRegularExpression(QStringLiteral("//[^\\n]*")));
    ASSERT_FALSE(render.isEmpty());
    auto body = [&render](const char *signature) -> QString
    {
        const int at = render.indexOf(QString::fromLatin1(signature));
        if (at < 0) return QString();
        return render.mid(at, render.indexOf(QStringLiteral("\n}\n"), at) - at);
    };
    const QString targetAware = body("bool RenderFrameThread::acquireLatestReadyFrameForPlaybackTarget(");
    ASSERT_TRUE(targetAware.contains(QStringLiteral(
        "playback_frame_range::readySlotPreferred( oldestFirst, readySlotIndex >= 0, slot.requestSerial, chosenRequestSerial )")));
    ASSERT_FALSE(targetAware.contains(QStringLiteral("slot.requestSerial >= latestRequestSerial")));
    const QString plain = body("bool RenderFrameThread::acquireLatestReadyFrame(ReadyFrame *frame, bool oldestFirst)");
    ASSERT_TRUE(plain.contains(QStringLiteral("oldestFirst ? findOldestReadySlotLocked() : findLatestReadySlotLocked()")));
    ASSERT_TRUE(plain.contains(QStringLiteral("acquireReadySlotLocked( frame, readySlotIndex, !oldestFirst )")));

    const QString note = mainWindowFunction(QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("));
    ASSERT_FALSE(note.isEmpty());
    const int arm = note.indexOf(QStringLiteral(
        "m_playbackSmokePinnedWindow.arm( playback_frame_range::pinnedWindowFirstFrame( m_sourceAdvance.startFrame ),"));
    const int count = note.indexOf(QStringLiteral("m_playbackSmokePinnedWindow.notePresent( static_cast<int64_t>( displayFrame ) );"));
    const int rawStop = note.indexOf(QStringLiteral("&& m_playbackSmokePresentedFrames >= m_playbackSmokeTargetPresentedFrames"));
    const int windowGuard = note.indexOf(QStringLiteral(
        "if( playback_frame_range::pinnedPresentStopCount( m_playbackSmokePinnedWindow ) < m_playbackSmokeTargetPresentedFrames )\n            return;"));
    const int stop = note.indexOf(QStringLiteral("ui->actionPlay->setChecked( false );"), rawStop);
    ASSERT_TRUE(arm > 0);
    ASSERT_TRUE(count > arm);
    ASSERT_TRUE(rawStop > count);
    ASSERT_TRUE(windowGuard > rawStop);
    ASSERT_TRUE(stop > windowGuard);

    const QString mainWindow = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int smokeAt = mainWindow.indexOf(QStringLiteral("int MainWindow::runGuiPlaybackSmoke("));
    const int observedAt = mainWindow.indexOf(QStringLiteral("playback_frame_range::pinnedWindowObservedFrames("), smokeAt);
    const int judgedAt = mainWindow.indexOf(QStringLiteral("pinnedObservedFrames != m_playbackSmokeTargetPresentedFrames"), observedAt);
    ASSERT_TRUE(smokeAt > 0);
    ASSERT_TRUE(observedAt > smokeAt);
    ASSERT_TRUE(judgedAt > observedAt);
    ASSERT_FALSE(mainWindow.contains(QStringLiteral("m_playbackSmokeWindowPresentedFrames")));

    // r8 fix 1 (adversary pass 8 on 77fa2218): the ledger is armed with the target (its identity frame start + N); the unpaced
    // isolated hold waits for the identity frame (A1); a pin whose identity frame is not before the clip's last frame is refused
    // before the measured Play (A2).
    ASSERT_TRUE(note.contains(QStringLiteral("m_playRequiredSourceFrames, m_playbackSmokeTargetPresentedFrames );")));
    const QString handling = mainWindowFunction(QStringLiteral("void MainWindow::playbackHandling("));
    ASSERT_TRUE(handling.contains(QStringLiteral(
        "if( m_automationPacingIsolated && mlvappPlaybackUnpacedRequested() && programmaticPlayConsumed()\n"
        "         && playback_frame_range::pinnedUnpacedHoldReached( m_playbackSmokeTargetPresentedFrames, m_sourceAdvance.consumed(),\n"
        "                                                            m_playRequiredSourceFrames ) ) return;")));
    const int floorAt = mainWindow.indexOf(QStringLiteral("presentedFramesTargetReachesFloor( options.targetPresentedFrames"), smokeAt);
    const int identityAt = mainWindow.indexOf(QStringLiteral("playback_frame_range::pinnedIdentityFrameAdmitted("), smokeAt);
    const int measuredPlayAt = mainWindow.indexOf(QStringLiteral("site=gui-smoke-measured reason=PINNED_FRAME_PAST_CLIP_END"), smokeAt);
    ASSERT_TRUE(floorAt > smokeAt);
    ASSERT_TRUE(identityAt > floorAt);
    ASSERT_TRUE(measuredPlayAt > identityAt);
}
