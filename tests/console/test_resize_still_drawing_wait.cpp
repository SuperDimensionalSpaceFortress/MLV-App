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
    const int gradientSite = body.indexOf(QStringLiteral("waitForResizeStillDrawing( QStringLiteral(\"gradient\") );"));
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
        QStringLiteral("void MainWindow::waitForResizeStillDrawing( const QString &site )"),
        QStringLiteral("//Window resized -> scale picture"));
    ASSERT_FALSE(body.isEmpty());
    ASSERT_TRUE(body.contains(QStringLiteral("resize_still_drawing_wait::wait(")));
    ASSERT_TRUE(body.contains(QStringLiteral("!renderThreadBusyForPlaybackAdvance( currentPlaybackAdvanceTarget() )")));
    ASSERT_TRUE(body.contains(QStringLiteral("qApp->processEvents();")));
    ASSERT_TRUE(body.contains(QStringLiteral("m_frameStillDrawing = false;")));
    ASSERT_TRUE(body.contains(QStringLiteral("QStringLiteral(\"resize.still_drawing_wait\")")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "\"outcome=%1 waited_ms=%2 site=%3 render_idle=%4\"")));
    ASSERT_TRUE(body.contains(QStringLiteral("Outcome::NotWaiting")));
    // always-on: the log call is not trace-only
    ASSERT_FALSE(body.contains(QStringLiteral("true );")));
    ASSERT_FALSE(body.contains(QStringLiteral("while( m_frameStillDrawing )")));
}
