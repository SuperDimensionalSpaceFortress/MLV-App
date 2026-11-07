// GUI-SMOKE-FOREGROUND-HANG-NOLA-1: MainWindow::resizeEvent used to spin
// `while( m_frameStillDrawing ) qApp->processEvents();` with no bound. A stale-true flag (nothing left to clear
// it) pumped the GUI thread forever after the smoke runner's showFullScreen() resize: two of two nola legs hung
// and were tree-killed at the 60 min process budget. The wait is now one pure bounded decision
// (ResizeStillDrawingWait.h) with injected callbacks. These pin its outcomes with a fake clock and fake
// callbacks, and pin by source scan (MainWindow.cpp needs a full GUI build, so it is read as text, mirroring
// test_playback_fps_meter_wiring.cpp) that both former spin sites call it and no unbounded spin is left.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/ResizeStillDrawingWait.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <cstdint>
#include <string>

using resize_still_drawing_wait::Callbacks;
using resize_still_drawing_wait::Outcome;
using resize_still_drawing_wait::Result;
using resize_still_drawing_wait::kDeadlineMs;
using resize_still_drawing_wait::kMinPumpsBeforeStale;
using resize_still_drawing_wait::kStaleIdleGraceMs;
using resize_still_drawing_wait::outcomeName;
using resize_still_drawing_wait::wait;

namespace
{

// A fake world: each pump advances the fake monotonic clock by stepMs; the flag clears after clearAfterPumps
// pumps (never when < 0); renderIdleFor(t) decides idleness from the fake time.
struct FakeWorld
{
    std::int64_t nowMs = 1000; // a non-zero origin: waited_ms must be relative, never absolute
    std::int64_t stepMs = 5;
    int clearAfterPumps = -1;
    bool flag = true;
    int pumps = 0;
    int clearStaleCalls = 0;
    int renderIdleCalls = 0;
    // idle iff (nowMs - 1000) is inside [idleFromMs, idleUntilMs)
    std::int64_t idleFromMs = 0;
    std::int64_t idleUntilMs = INT64_MAX;

    Callbacks callbacks()
    {
        Callbacks c;
        c.stillDrawing = [this]() { return flag; };
        c.renderIdle = [this]() {
            ++renderIdleCalls;
            const std::int64_t rel = nowMs - 1000;
            return rel >= idleFromMs && rel < idleUntilMs;
        };
        c.pump = [this]() {
            ++pumps;
            nowMs += stepMs;
            if( clearAfterPumps >= 0 && pumps >= clearAfterPumps ) flag = false;
        };
        c.clearStale = [this]() { ++clearStaleCalls; flag = false; };
        c.nowMs = [this]() { return nowMs; };
        return c;
    }
};

std::string name(Outcome outcome) { return std::string(outcomeName(outcome)); }

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    if (path.isEmpty()) return QString();
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) return QString();
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

QString resizeEventBody()
{
    return functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
        QStringLiteral("void MainWindow::resizeEvent(QResizeEvent *event)"),
        QStringLiteral("void MainWindow::changeEvent( QEvent *event )"));
}

} // namespace

TEST(ResizeStillDrawingWait, TheContractConstantsAreTheCardsNumbers)
{
    ASSERT_EQ(static_cast<std::int64_t>(50), kStaleIdleGraceMs);
    ASSERT_EQ(static_cast<std::int64_t>(2000), kDeadlineMs);
    ASSERT_EQ(2, kMinPumpsBeforeStale);
}

TEST(ResizeStillDrawingWait, AClearFlagOnEntryIsNotWaitingWithZeroPumps)
{
    FakeWorld world;
    world.flag = false;
    const Result result = wait(world.callbacks());
    ASSERT_TRUE(result.outcome == Outcome::NotWaiting);
    ASSERT_EQ(0, world.pumps);
    ASSERT_EQ(0, result.pumps);
    ASSERT_EQ(0, world.clearStaleCalls);
    ASSERT_EQ(static_cast<std::int64_t>(0), result.waitedMs);
}

TEST(ResizeStillDrawingWait, AFlagClearedByTheFirstPumpIsCleared)
{
    FakeWorld world;
    world.clearAfterPumps = 1;
    const Result result = wait(world.callbacks());
    ASSERT_TRUE(result.outcome == Outcome::Cleared);
    ASSERT_EQ(1, world.pumps);
    ASSERT_EQ(0, world.clearStaleCalls);
    ASSERT_EQ(static_cast<std::int64_t>(5), result.waitedMs);
}

TEST(ResizeStillDrawingWait, AStuckFlagOnAnIdleRenderThreadIsStaleClearedWithinTheGrace)
{
    FakeWorld world;
    const Result result = wait(world.callbacks());
    ASSERT_TRUE(result.outcome == Outcome::StaleCleared);
    ASSERT_EQ(1, world.clearStaleCalls);
    ASSERT_FALSE(world.flag);
    ASSERT_TRUE(result.renderIdle);
    // idle is first seen after pump 1 (t=5); stale once it has held >= 50 ms (t=55): one step past the grace at most
    ASSERT_TRUE(result.waitedMs >= kStaleIdleGraceMs);
    ASSERT_TRUE(result.waitedMs <= kStaleIdleGraceMs + 2 * world.stepMs);
    ASSERT_TRUE(result.pumps >= kMinPumpsBeforeStale);
}

TEST(ResizeStillDrawingWait, AStuckFlagOnABusyRenderThreadTimesOutAtTheDeadlineAndNeverClears)
{
    FakeWorld world;
    world.idleFromMs = INT64_MAX; // never idle
    const Result result = wait(world.callbacks());
    ASSERT_TRUE(result.outcome == Outcome::Timeout);
    ASSERT_EQ(0, world.clearStaleCalls);
    ASSERT_TRUE(world.flag); // the helper never forges a clear on a busy render thread
    ASSERT_FALSE(result.renderIdle);
    ASSERT_TRUE(result.waitedMs >= kDeadlineMs);
    ASSERT_TRUE(result.waitedMs < kDeadlineMs + world.stepMs);
}

TEST(ResizeStillDrawingWait, IdleForUnderTheGraceThenTheFlagClearsIsClearedNotStale)
{
    FakeWorld world;
    world.stepMs = 10;
    world.clearAfterPumps = 4; // t=40: idle first seen at t=10, so it held 30 ms < 50 ms
    const Result result = wait(world.callbacks());
    ASSERT_TRUE(result.outcome == Outcome::Cleared);
    ASSERT_EQ(0, world.clearStaleCalls);
    ASSERT_EQ(static_cast<std::int64_t>(40), result.waitedMs);
}

TEST(ResizeStillDrawingWait, StaleNeedsAtLeastTwoPumpsEvenWhenOnePumpTakesLongerThanTheGrace)
{
    FakeWorld world;
    world.stepMs = 100; // one pump already outlasts the 50 ms grace
    const Result result = wait(world.callbacks());
    ASSERT_TRUE(result.outcome == Outcome::StaleCleared);
    ASSERT_EQ(1, world.clearStaleCalls);
    ASSERT_TRUE(world.pumps >= kMinPumpsBeforeStale);
}

TEST(ResizeStillDrawingWait, IdlenessMustHoldContinuouslyABusyBlipRestartsTheGrace)
{
    FakeWorld world;
    world.stepMs = 10;
    // idle t=[0,40), busy t=[40,50), idle again from t=50: the first idle run (30 ms seen) must not count
    world.idleFromMs = 0;
    world.idleUntilMs = 40;
    Callbacks c = world.callbacks();
    c.renderIdle = [&world]() {
        ++world.renderIdleCalls;
        const std::int64_t rel = world.nowMs - 1000;
        return rel < 40 || rel >= 50;
    };
    const Result result = wait(c);
    ASSERT_TRUE(result.outcome == Outcome::StaleCleared);
    ASSERT_EQ(1, world.clearStaleCalls);
    // the second run starts at t=50 and must hold to t>=100
    ASSERT_TRUE(result.waitedMs >= 100);
}

// adversary r1 A1: one pump that does not return (a nested modal loop) is outside any helper bound; the helper's part is
// to check the deadline right after it and never pump again. The caller's part (pinned below) is a pump that cannot
// dispatch the user input that opens such a loop.
TEST(ResizeStillDrawingWait, APumpThatOutlastsTheDeadlineEndsTheWaitWithoutAnotherPump)
{
    FakeWorld world;
    world.idleFromMs = INT64_MAX; // busy: nothing may call it stale
    Callbacks c = world.callbacks();
    c.pump = [&world]() { ++world.pumps; world.nowMs += 10000; };
    const Result result = wait(c);
    ASSERT_TRUE(result.outcome == Outcome::Timeout);
    ASSERT_EQ(1, world.pumps);
    ASSERT_EQ(0, world.clearStaleCalls);
    ASSERT_EQ(static_cast<std::int64_t>(10000), result.waitedMs);
}

// adversary r1 pass 3 A2: renderIdle() reaches the renderer mutex (RenderFrameThread::isIdle), so it can block. The
// deadline must be judged on a clock sampled AFTER it, or a pump runs past 2000 ms: pump 1 returns at 1999, renderIdle
// blocks to 2003 and reports busy; the wait must end Timeout with no second pump.
TEST(ResizeStillDrawingWait, ARenderIdleThatBlocksPastTheDeadlineEndsTheWaitWithoutAnotherPump)
{
    FakeWorld world;
    Callbacks c = world.callbacks();
    c.pump = [&world]() { ++world.pumps; world.nowMs += 1999; };
    c.renderIdle = [&world]() {
        ++world.renderIdleCalls;
        if (world.nowMs - 1000 == 1999) world.nowMs += 4; // blocked on the renderer mutex 1999 -> 2003
        return false;
    };
    const Result result = wait(c);
    ASSERT_TRUE(result.outcome == Outcome::Timeout);
    ASSERT_EQ(1, world.pumps);
    ASSERT_EQ(1, result.pumps);
    ASSERT_EQ(0, world.clearStaleCalls);
    ASSERT_EQ(static_cast<std::int64_t>(2003), result.waitedMs);
}

// adversary r1 pass 3 A2, same class on the stale grace: time spent blocked inside renderIdle() is time the render
// thread held its mutex (busy), so the idle run starts when the sample RETURNS, not before it. renderIdle blocks 100 ms
// on its first call and then reports idle; the grace must still be a full 50 ms measured from that return.
TEST(ResizeStillDrawingWait, TimeBlockedInsideRenderIdleIsNotCountedAsIdleGrace)
{
    FakeWorld world;
    Callbacks c = world.callbacks();
    c.renderIdle = [&world]() {
        if (world.renderIdleCalls++ == 0) world.nowMs += 100;
        return true;
    };
    const Result result = wait(c);
    ASSERT_TRUE(result.outcome == Outcome::StaleCleared);
    ASSERT_EQ(1, world.clearStaleCalls);
    // pump 1 returns at t=5, the first sample returns at t=105: stale no earlier than t=155
    ASSERT_TRUE(result.waitedMs >= 105 + kStaleIdleGraceMs);
    ASSERT_TRUE(result.waitedMs <= 105 + kStaleIdleGraceMs + world.stepMs);
}

// adversary r2 A1: the same rule once an idle run is already open. renderIdle() reports idle every time but blocks 100 ms
// on its SECOND call; that blocked time must not be credited to the run. r3 A1: the time before the block must not be
// credited either (a blocked sample ends the run), so the grace needs 50 ms of continuous idleness after the block.
TEST(ResizeStillDrawingWait, TimeBlockedInsideALaterRenderIdleSampleIsNotCountedAsIdleGrace)
{
    FakeWorld world;
    Callbacks c = world.callbacks();
    c.renderIdle = [&world]() {
        if (world.renderIdleCalls++ == 1) world.nowMs += 100;
        return true;
    };
    const Result result = wait(c);
    ASSERT_TRUE(result.outcome == Outcome::StaleCleared);
    ASSERT_EQ(1, world.clearStaleCalls);
    // run opens at t=5; sample 2 blocks t=10 -> 110 and the run restarts there: stale no earlier than t=160
    ASSERT_TRUE(result.waitedMs >= 110 + kStaleIdleGraceMs);
    ASSERT_TRUE(result.waitedMs <= 110 + kStaleIdleGraceMs + world.stepMs);
}

// adversary r3 A1 (r2 advisory A2 raised to major): a blocked renderIdle() sample is a break in idleness (the render
// thread held its mutex), so it must not join the idle time before it to the idle time after it. The adversary's input:
// entry t=0, pumps return at 1, 50, 151 (then 1 ms apart), every sample idle, the second blocks 50 -> 150. r2's
// arithmetic kept 49 ms of pre-block credit and stale-cleared at t=151, 1 ms after the block.
TEST(ResizeStillDrawingWait, ABlockedSampleEndsTheIdleRunSoSeparateIdlePeriodsNeverJoin)
{
    std::int64_t now = 0;
    bool flag = true;
    int pumps = 0;
    int samples = 0;
    int clears = 0;
    Callbacks c;
    c.stillDrawing = [&flag]() { return flag; };
    c.pump = [&now, &pumps]() {
        ++pumps;
        if (pumps == 1) now = 1;
        else if (pumps == 2) now = 50;
        else if (pumps == 3) now = 151;
        else now += 1;
    };
    c.renderIdle = [&now, &samples]() {
        if (samples++ == 1) now = 150; // blocked on the renderer mutex 50 -> 150
        return true;
    };
    c.clearStale = [&flag, &clears]() { ++clears; flag = false; };
    c.nowMs = [&now]() { return now; };
    const Result result = wait(c);
    ASSERT_TRUE(result.outcome == Outcome::StaleCleared);
    ASSERT_EQ(1, clears);
    // the only continuous idle run starts when the blocked sample returns (t=150)
    ASSERT_TRUE(result.waitedMs >= 150 + kStaleIdleGraceMs);
    ASSERT_TRUE(result.waitedMs <= 150 + kStaleIdleGraceMs + 1);
}

// r3 (adversary r2 pass 3 A1, which supersedes r2's request-serial floor). drawFrameReady sizes the scene from the zoom and
// stretch state at PRESENT time (computeDisplaySceneGeometry + setSceneRect), so the geometry a frame lands on is not a
// property of its request serial: an OLDER request can present at the NEW geometry. The serial floor rejected it and left
// the gradient at the old anchor. The re-anchor is now keyed on what decides placement: finishPresentedFrame re-anchors
// when m_zoomModeChanged is set (consumed on any frame, as the base does) OR the scene differs from the geometry the
// gradient was last anchored on (GradientElement records it on every redrawGradientElement call).
namespace
{

// finishPresentedFrame's re-anchor as GradientElement::redrawGradientElement places the overlay: x = startX * sceneW / picW.
// The adversary's clip: 1920x1080 picture, gradient start x=480, so x=480 at scene width 1920 and x=960 at 3840.
struct GradientModel
{
    bool zoomModeChanged = false;
    int anchoredW = -1;
    int anchoredH = -1;
    int redraws = 0;
    std::int64_t x = -1;

    void redraw(int sceneW, int sceneH)
    {
        anchoredW = sceneW;
        anchoredH = sceneH;
        x = 480 * static_cast<std::int64_t>(sceneW) / 1920;
        ++redraws;
    }
    void present(int sceneW, int sceneH)
    {
        if (resize_still_drawing_wait::gradientNeedsReanchor(zoomModeChanged, anchoredW, anchoredH, sceneW, sceneH))
        {
            zoomModeChanged = false;
            redraw(sceneW, sceneH);
        }
    }
};

} // namespace

TEST(ResizeStillDrawingWait, TheReanchorDecisionIsTheFlagOrAChangedAnchoredGeometry)
{
    using resize_still_drawing_wait::gradientNeedsReanchor;
    ASSERT_TRUE(gradientNeedsReanchor(true, 1920, 1080, 3840, 1080));   // r2 pass 3 A1: armed, lands at the new geometry
    ASSERT_TRUE(gradientNeedsReanchor(false, 1920, 1080, 3840, 1080));  // flag already consumed on the old scene
    ASSERT_TRUE(gradientNeedsReanchor(false, 1920, 1080, 1920, 2160));  // a height-only change (vertical stretch)
    ASSERT_TRUE(gradientNeedsReanchor(true, 1920, 1080, 1920, 1080));   // the flag is consumed on any frame (base)
    ASSERT_TRUE(gradientNeedsReanchor(false, -1, -1, 1920, 1080));      // never anchored
    ASSERT_FALSE(gradientNeedsReanchor(false, 1920, 1080, 1920, 1080)); // no spare redraw per frame on a steady scene
}

// r2 pass 3 A1, the adversary's input: a stretch change arms the re-anchor, then request 100 (older than the next serial
// 101) presents at 3840x1080. The gradient must move to x=960 on that frame; r2's floor left it at 480.
TEST(ResizeStillDrawingWait, AnOlderRequestPresentingAtTheNewGeometryReanchorsTheGradient)
{
    GradientModel g;
    g.redraw(1920, 1080);
    ASSERT_EQ(static_cast<std::int64_t>(480), g.x);
    g.zoomModeChanged = true;   // on_comboBoxHStretch_currentIndexChanged (factor 2)
    g.present(3840, 1080);      // request 100
    ASSERT_EQ(static_cast<std::int64_t>(960), g.x);
    ASSERT_FALSE(g.zoomModeChanged);
    const int redraws = g.redraws;
    g.present(3840, 1080);      // request 101, same geometry: no spare redraw
    ASSERT_EQ(redraws, g.redraws);
    ASSERT_EQ(static_cast<std::int64_t>(960), g.x);
}

// r2 pass 1 A2: an older request consumes the flag on the OLD scene; the frame the resize asked for then changes the scene.
// r1 pass 3 A1: a StaleCleared redraw anchors on the old scene and a late frame changes it. Both need no flag at all: the
// changed geometry re-anchors.
TEST(ResizeStillDrawingWait, AFlagConsumedOnTheOldSceneStillLeavesTheLandedFrameReanchored)
{
    GradientModel g;
    g.redraw(1920, 1080);       // the gradient site's immediate redraw on the old scene (Cleared or StaleCleared)
    g.zoomModeChanged = true;   // resizeEvent arms it after the wait
    g.present(1920, 1080);      // request 100, old geometry: consumes the flag
    ASSERT_FALSE(g.zoomModeChanged);
    ASSERT_EQ(static_cast<std::int64_t>(480), g.x);
    g.present(3840, 1080);      // request 101 (or the late Ready frame) lands at the new geometry
    ASSERT_EQ(static_cast<std::int64_t>(960), g.x);

    GradientModel late;         // r1 pass 3 A1 with no flag at all
    late.redraw(1920, 1080);
    late.present(3840, 1080);
    ASSERT_EQ(static_cast<std::int64_t>(960), late.x);
}

// r2 pass 2 A1: a stretch change dispatched inside the resize wait arms the re-anchor too. With no floor there is no order
// to get wrong: whichever request lands at the final geometry re-anchors, whether the old one lands first or not.
TEST(ResizeStillDrawingWait, ANestedGeometryChangeDuringTheResizeWaitReanchorsInEitherLandingOrder)
{
    GradientModel oldFirst;
    oldFirst.redraw(1920, 1080);
    oldFirst.zoomModeChanged = true; // nested stretch change
    oldFirst.zoomModeChanged = true; // resize arm after the wait
    oldFirst.present(1920, 1080);
    oldFirst.present(3840, 1080);
    ASSERT_EQ(static_cast<std::int64_t>(960), oldFirst.x);

    GradientModel newFirst;
    newFirst.redraw(1920, 1080);
    newFirst.zoomModeChanged = true;
    newFirst.present(3840, 1080);    // the older request already presents at the new geometry
    newFirst.present(3840, 1080);
    ASSERT_EQ(static_cast<std::int64_t>(960), newFirst.x);
}

TEST(ResizeStillDrawingWaitWiring, TheGradientReanchorHasNoRequestSerialFloor)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const QString wait = readRepoFile(QStringLiteral("platform/qt/ResizeStillDrawingWait.h"));
    ASSERT_FALSE(source.isEmpty());
    ASSERT_FALSE(header.isEmpty());
    ASSERT_FALSE(wait.isEmpty());
    for (const QString & text : {source, header, wait})
    {
        ASSERT_FALSE(text.contains(QStringLiteral("m_gradientReanchorSerialFloor")));
        ASSERT_FALSE(text.contains(QStringLiteral("gradientReanchorSerialFloor")));
        ASSERT_FALSE(text.contains(QStringLiteral("raisedReanchorFloor")));
    }
}

TEST(ResizeStillDrawingWaitWiring, FinishPresentedFrameReanchorsThroughTheGeometryDecision)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString finish = functionBody(source,
        QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"),
        QStringLiteral("\nvoid MainWindow::"));
    ASSERT_FALSE(finish.isEmpty());
    const int decideAt = finish.indexOf(QStringLiteral(
        "if( resize_still_drawing_wait::gradientNeedsReanchor( m_zoomModeChanged,"));
    ASSERT_TRUE(decideAt >= 0);
    const QString decide = finish.mid(decideAt, 700);
    ASSERT_TRUE(decide.contains(QStringLiteral("m_pGradientElement->anchoredSceneWidth()")));
    ASSERT_TRUE(decide.contains(QStringLiteral("m_pGradientElement->anchoredSceneHeight()")));
    ASSERT_TRUE(decide.contains(QStringLiteral("static_cast<int>( m_pScene->width() )")));
    ASSERT_TRUE(decide.contains(QStringLiteral("static_cast<int>( m_pScene->height() )")));
    ASSERT_TRUE(decide.contains(QStringLiteral("m_zoomModeChanged = false;")));
    ASSERT_TRUE(decide.contains(QStringLiteral("m_pGradientElement->redrawGradientElement( m_pScene->width(),")));
    ASSERT_FALSE(finish.contains(QStringLiteral("requestSerial >= m_gradient")));
}

// Every gradient redraw records the geometry it anchored on, by construction: the one definition of redrawGradientElement
// stores it, and MainWindow reaches the overlay only through m_pGradientElement->redrawGradientElement.
TEST(ResizeStillDrawingWaitWiring, EveryGradientRedrawRecordsTheAnchoredGeometry)
{
    const QString cpp = readRepoFile(QStringLiteral("platform/qt/GradientElement.cpp"));
    const QString header = readRepoFile(QStringLiteral("platform/qt/GradientElement.h"));
    ASSERT_FALSE(cpp.isEmpty());
    ASSERT_FALSE(header.isEmpty());
    const QString redraw = functionBody(cpp,
        QStringLiteral("void GradientElement::redrawGradientElement(int sceneX, int sceneY, int picX, int picY)"),
        QStringLiteral("\n}"));
    ASSERT_FALSE(redraw.isEmpty());
    ASSERT_TRUE(redraw.contains(QStringLiteral("m_anchoredSceneWidth = sceneX;")));
    ASSERT_TRUE(redraw.contains(QStringLiteral("m_anchoredSceneHeight = sceneY;")));
    ASSERT_EQ(1, cpp.count(QStringLiteral("GradientElement::redrawGradientElement(")));
    ASSERT_TRUE(header.contains(QStringLiteral("int m_anchoredSceneWidth = -1;")));
    ASSERT_TRUE(header.contains(QStringLiteral("int m_anchoredSceneHeight = -1;")));
    ASSERT_TRUE(header.contains(QStringLiteral("int anchoredSceneWidth( void ) const;")));
    ASSERT_TRUE(header.contains(QStringLiteral("int anchoredSceneHeight( void ) const;")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int calls = source.count(QStringLiteral("redrawGradientElement("));
    ASSERT_TRUE(calls >= 7);
    ASSERT_EQ(calls, source.count(QStringLiteral("m_pGradientElement->redrawGradientElement(")));
}

// adversary r1 re-pass A1: a handler the pump dispatches (Updater::getReleaseList's processEvents() loop) can run its
// own unfiltered nested loop, which admits Ctrl+I's modal dialog: no synchronous pump can bound that. The contract is
// therefore stated as what it is (zero pumps after the deadline; one pump's duration is the caller's), never as
// "prevents" a nested modal loop.
TEST(ResizeStillDrawingWaitWiring, TheBoundIsDocumentedAsPerPumpAndNestedLoopsAsTheCallers)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/ResizeStillDrawingWait.h"));
    ASSERT_FALSE(header.isEmpty());
    ASSERT_TRUE(header.contains(QStringLiteral("WHAT IS BOUNDED")));
    ASSERT_TRUE(header.contains(QStringLiteral("the number of pumps\n// after the deadline is zero")));
    ASSERT_TRUE(header.contains(QStringLiteral("Updater::getReleaseList")));
    ASSERT_FALSE(header.contains(QStringLiteral("this never blocks")));
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int at = source.indexOf(QStringLiteral("bool MainWindow::waitForResizeStillDrawing( const QString &site )"));
    ASSERT_TRUE(at > 0);
    const QString preface = source.mid(qMax(0, at - 1600), 1600);
    ASSERT_TRUE(preface.contains(QStringLiteral("still suspends the wait until that\n// loop returns")));
    ASSERT_FALSE(preface.contains(QStringLiteral("would run a nested loop inside one pump that the deadline cannot bound;")));
}

TEST(ResizeStillDrawingWait, OutcomesHaveTheLogNames)
{
    ASSERT_EQ(std::string("not_waiting"), name(Outcome::NotWaiting));
    ASSERT_EQ(std::string("cleared"), name(Outcome::Cleared));
    ASSERT_EQ(std::string("stale_cleared"), name(Outcome::StaleCleared));
    ASSERT_EQ(std::string("timeout"), name(Outcome::Timeout));
}

TEST(ResizeStillDrawingWaitWiring, ResizeEventHasNoUnboundedSpinAndCallsTheHelperAtBothSites)
{
    const QString body = resizeEventBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_FALSE(body.contains(QStringLiteral("while( m_frameStillDrawing )")));
    ASSERT_FALSE(body.contains(QStringLiteral("while(m_frameStillDrawing)")));
    ASSERT_FALSE(body.contains(QStringLiteral("while (m_frameStillDrawing)")));
    // both former sites call the helper, each with its own site label, before the work it guarded
    const int resizeSite = body.indexOf(QStringLiteral("waitForResizeStillDrawing( QStringLiteral(\"resize\") );"));
    const int drawAt = body.indexOf(QStringLiteral("drawFrame();"));
    const int gradientSite = body.indexOf(QStringLiteral("waitForResizeStillDrawing( QStringLiteral(\"gradient\") )"));
    const int redrawAt = body.indexOf(QStringLiteral("m_pGradientElement->redrawGradientElement("));
    ASSERT_TRUE(resizeSite >= 0);
    ASSERT_TRUE(drawAt > resizeSite);
    ASSERT_TRUE(gradientSite > drawAt);
    ASSERT_TRUE(redrawAt > gradientSite);
    // the lock()/unlock() handshake still runs before the first wait
    const int unlockAt = body.indexOf(QStringLiteral("m_pRenderThread->unlock();"));
    ASSERT_TRUE(unlockAt >= 0 && unlockAt < resizeSite);
}

TEST(ResizeStillDrawingWaitWiring, TheSiteWaitUsesTheBoundedHelperAndLogsAlwaysOn)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("MainWindow::waitForResizeStillDrawing( const QString &site )"),
        QStringLiteral("//Window resized -> scale picture"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("resize_still_drawing_wait::wait(")));
    ASSERT_TRUE(body.contains(QStringLiteral("!renderThreadBusyForPlaybackAdvance( currentPlaybackAdvanceTarget() )")));
    // adversary r1 A1: the pump defers user input, so a shortcut (Ctrl+I's file dialog) cannot open a nested modal loop
    // inside the bounded wait; the input is delivered by the next unfiltered event loop pass, not dropped
    ASSERT_TRUE(body.contains(QStringLiteral("qApp->processEvents( QEventLoop::ExcludeUserInputEvents );")));
    ASSERT_FALSE(body.contains(QStringLiteral("qApp->processEvents();")));
    ASSERT_TRUE(body.contains(QStringLiteral("m_frameStillDrawing = false;")));
    ASSERT_TRUE(body.contains(QStringLiteral("QStringLiteral(\"resize.still_drawing_wait\")")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "\"outcome=%1 waited_ms=%2 site=%3 render_idle=%4\"")));
    ASSERT_TRUE(body.contains(QStringLiteral("Outcome::NotWaiting")));
    // always-on: the log call is not trace-only
    ASSERT_FALSE(body.contains(QStringLiteral("true );")));
    ASSERT_FALSE(body.contains(QStringLiteral("while( m_frameStillDrawing )")));
}

// adversary r1 A2: a gradient-site Timeout means the frame drawFrame() asked for has not landed, so the scene still has
// the old geometry. The redraw is then deferred to the frame-ready path (m_zoomModeChanged, consumed by
// finishPresentedFrame against the landed scene) instead of placing the overlay on the old scene.
TEST(ResizeStillDrawingWaitWiring, AGradientSiteTimeoutDefersTheRedrawToTheLandedFrame)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString wrapper = functionBody(source,
        QStringLiteral("MainWindow::waitForResizeStillDrawing( const QString &site )"),
        QStringLiteral("//Window resized -> scale picture"));
    ASSERT_FALSE(wrapper.isEmpty());
    ASSERT_TRUE(source.contains(QStringLiteral("bool MainWindow::waitForResizeStillDrawing( const QString &site )")));
    ASSERT_TRUE(wrapper.contains(QStringLiteral(
        "return result.outcome != resize_still_drawing_wait::Outcome::Timeout;")));

    const QString body = resizeEventBody();
    ASSERT_FALSE(body.isEmpty());
    const int gradientIf = body.indexOf(QStringLiteral("if( waitForResizeStillDrawing( QStringLiteral(\"gradient\") ) )"));
    ASSERT_TRUE(gradientIf >= 0);
    const QString tail = body.mid(gradientIf);
    const int redrawAt = tail.indexOf(QStringLiteral("m_pGradientElement->redrawGradientElement("));
    const int deferAt = tail.indexOf(QStringLiteral("m_zoomModeChanged = true;"));
    ASSERT_TRUE(redrawAt > 0);
    ASSERT_TRUE(deferAt > redrawAt);
    // adversary r1 pass 3 A1: StaleCleared (and Cleared) can return while a Ready frame is published but frameReady has
    // not been emitted yet (isIdle ignores Ready slots), so no outcome proves the scene is final. The re-anchor on the
    // landed frame is armed on EVERY outcome, after the wait and the immediate redraw: never inside an else (Timeout
    // only), never before the wait (a frame landing inside a pump would consume it early).
    ASSERT_FALSE(tail.left(deferAt).contains(QStringLiteral("else")));
    const QString gradientBlock = tail.left(tail.indexOf(QStringLiteral("event->accept();")));
    ASSERT_EQ(1, gradientBlock.count(QStringLiteral("m_zoomModeChanged = true;")));

    // the frame-ready path still consumes the flag with a gradient redraw on the landed scene (r3: through the geometry
    // decision, which takes the flag first)
    const QString finish = functionBody(source,
        QStringLiteral("void MainWindow::finishPresentedFrame( uint64_t displayFrame,"),
        QStringLiteral("\nvoid MainWindow::"));
    ASSERT_FALSE(finish.isEmpty());
    const int consumeAt = finish.indexOf(QStringLiteral(
        "if( resize_still_drawing_wait::gradientNeedsReanchor( m_zoomModeChanged,"));
    ASSERT_TRUE(consumeAt >= 0);
    const QString consume = finish.mid(consumeAt, 700);
    ASSERT_TRUE(consume.contains(QStringLiteral("m_zoomModeChanged = false;")));
    ASSERT_TRUE(consume.contains(QStringLiteral("m_pGradientElement->redrawGradientElement( m_pScene->width(),")));
}
