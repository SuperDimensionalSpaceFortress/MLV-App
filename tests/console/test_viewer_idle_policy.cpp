// PLAYER-CURSOR-AUTOHIDE-1: the fullscreen viewer idle clock (ViewerIdlePolicy.h), driven with a
// fake clock. The cursor surfaces and the live timer are covered by gui_tests (test_gui_smoke.cpp).
#include "../common/minitest.h"

#include "../../platform/qt/ViewerIdlePolicy.h"
#include "../../platform/qt/ViewerMouseRetrievalHook.h"

#include <QEvent>
#include <QPoint>
#include <QPointF>

namespace
{

viewer_idle::Inputs allPass()
{
    viewer_idle::Inputs in;
    in.fullscreen = true;
    in.windowActive = true;
    in.toolArmed = false;
    in.popupOrModalOpen = false;
    return in;
}

int tickValue(viewer_idle::Tick t)
{
    return int(t);
}

} // namespace

// P1
TEST(ViewerIdlePolicy, MayHideOnlyWhenEveryGatePasses)
{
    ASSERT_TRUE(viewer_idle::mayHide(allPass()));

    viewer_idle::Inputs in = allPass();
    in.fullscreen = false;
    ASSERT_FALSE(viewer_idle::mayHide(in));

    in = allPass();
    in.windowActive = false;
    ASSERT_FALSE(viewer_idle::mayHide(in));

    in = allPass();
    in.toolArmed = true;
    ASSERT_FALSE(viewer_idle::mayHide(in));

    in = allPass();
    in.popupOrModalOpen = true;
    ASSERT_FALSE(viewer_idle::mayHide(in));

    ASSERT_FALSE(viewer_idle::mayHide(viewer_idle::Inputs()));
}

// P2
TEST(ViewerIdlePolicy, ActivityIgnoresSamePositionMovesAndNonMouseEvents)
{
    const QPoint a(10, 20);
    const QPoint b(11, 20);
    ASSERT_FALSE(viewer_idle::isActivity(QEvent::MouseMove, a, a, true));
    ASSERT_TRUE(viewer_idle::isActivity(QEvent::MouseMove, b, a, true));
    ASSERT_TRUE(viewer_idle::isActivity(QEvent::MouseMove, a, a, false));
    // Adversary A1: positions are compared unrounded; one physical pixel at 300% is 1/3 logical pixel.
    ASSERT_TRUE(viewer_idle::isActivity(QEvent::MouseMove, QPointF(100.0 + 1.0 / 3.0, 100.0), QPointF(100.0, 100.0), true));
    ASSERT_FALSE(viewer_idle::isActivity(QEvent::MouseMove, QPointF(100.25, 7.5), QPointF(100.25, 7.5), true));

    ASSERT_TRUE(viewer_idle::isActivity(QEvent::MouseButtonPress, a, a, true));
    ASSERT_TRUE(viewer_idle::isActivity(QEvent::MouseButtonRelease, a, a, true));
    ASSERT_TRUE(viewer_idle::isActivity(QEvent::MouseButtonDblClick, a, a, true));
    ASSERT_TRUE(viewer_idle::isActivity(QEvent::Wheel, a, a, true));

    ASSERT_FALSE(viewer_idle::isActivity(QEvent::Paint, b, a, true));
    ASSERT_FALSE(viewer_idle::isActivity(QEvent::Timer, b, a, true));
    ASSERT_FALSE(viewer_idle::isActivity(QEvent::Enter, b, a, true));
    ASSERT_FALSE(viewer_idle::isActivity(QEvent::Leave, b, a, true));

    ASSERT_TRUE(viewer_idle::forcesRestore(QEvent::WindowBlocked));
    ASSERT_TRUE(viewer_idle::forcesRestore(QEvent::WindowDeactivate));
    ASSERT_TRUE(viewer_idle::forcesRestore(QEvent::ContextMenu));
    ASSERT_FALSE(viewer_idle::forcesRestore(QEvent::MouseMove));
}

// P3: hide after idle, show on activity, re-hide after a fresh idle period.
TEST(ViewerIdlePolicy, ControllerHidesAfterIdleAndActivityRestartsTheClock)
{
    viewer_idle::Controller c;
    int rearm = -1;
    ASSERT_EQ(tickValue(viewer_idle::Tick::None), tickValue(c.onTimeout(allPass(), 5000, 2000, &rearm)));

    c.setFullscreen(true, 0);
    ASSERT_TRUE(c.fullscreen());
    ASSERT_FALSE(c.hidden());

    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 1999, 2000, &rearm)));
    ASSERT_EQ(1, rearm);
    ASSERT_FALSE(c.hidden());

    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm)));
    ASSERT_EQ(2000, rearm);
    ASSERT_TRUE(c.hidden());
    // While hidden the tick only keeps watching the pointer (adversary A2): no second hide, still hidden.
    rearm = -1;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 2001, 2000, &rearm)));
    ASSERT_EQ(2000, rearm);
    ASSERT_TRUE(c.hidden());

    ASSERT_TRUE(c.noteActivity(2500));
    ASSERT_FALSE(c.hidden());
    ASSERT_FALSE(c.noteActivity(2500));

    rearm = -1;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 4499, 2000, &rearm)));
    ASSERT_EQ(1, rearm);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 4500, 2000, &rearm)));
    ASSERT_TRUE(c.hidden());
}

// P3: any failing gate rearms for a full idle period and never hides, however long it has been.
TEST(ViewerIdlePolicy, ControllerNeverHidesWhileAGateFails)
{
    for (int gate = 0; gate < 4; ++gate)
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        viewer_idle::Inputs in = allPass();
        if (gate == 0) in.toolArmed = true;
        if (gate == 1) in.popupOrModalOpen = true;
        if (gate == 2) in.windowActive = false;
        if (gate == 3) in.fullscreen = false;
        int rearm = -1;
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(in, 10000, 2000, &rearm)));
        ASSERT_EQ(2000, rearm);
        ASSERT_FALSE(c.hidden());
    }
}

// P3: leaving fullscreen clears the hidden state and stops the clock; a backwards clock never hides;
// forceShown reports only a real hidden-to-shown change.
TEST(ViewerIdlePolicy, ControllerLeaveFullscreenBackwardsClockAndForceShown)
{
    viewer_idle::Controller c;
    int rearm = -1;
    c.setFullscreen(true, 0);
    ASSERT_FALSE(c.forceShown());
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 3000, 2000, &rearm)));
    ASSERT_TRUE(c.hidden());
    c.setFullscreen(false, 3100);
    ASSERT_FALSE(c.hidden());
    ASSERT_FALSE(c.fullscreen());
    ASSERT_EQ(tickValue(viewer_idle::Tick::None), tickValue(c.onTimeout(allPass(), 9000, 2000, &rearm)));

    viewer_idle::Controller back;
    back.setFullscreen(true, 5000);
    rearm = -1;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(back.onTimeout(allPass(), 100, 2000, &rearm)));
    ASSERT_EQ(2000, rearm);
    ASSERT_FALSE(back.hidden());

    // Adversary A3: a non-positive idle interval never turns a backwards clock into a hide.
    viewer_idle::Controller nonPositive;
    nonPositive.setFullscreen(true, 5000);
    rearm = -1;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(nonPositive.onTimeout(allPass(), 100, -1, &rearm)));
    ASSERT_TRUE(rearm >= 1);
    ASSERT_FALSE(nonPositive.hidden());

    viewer_idle::Controller f;
    f.setFullscreen(true, 0);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(f.onTimeout(allPass(), 2000, 2000, &rearm)));
    ASSERT_TRUE(f.forceShown());
    ASSERT_FALSE(f.hidden());
    ASSERT_FALSE(f.forceShown());
}

// Adversary A2: a wait that excludes user input delivers no mouse event but still runs the timer. The OS
// pointer moving is then activity: it shows a hidden cursor, and it keeps a shown cursor from hiding.
TEST(ViewerIdlePolicy, PointerMovedWithoutEventsShowsAndDefersTheHide)
{
    viewer_idle::Controller c;
    int rearm = -1;
    c.setFullscreen(true, 0);
    viewer_idle::Inputs moved = allPass();
    moved.pointerMoved = true;

    // Shown, pointer moving every tick with no event delivered: never hides.
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(moved, 2000, 2000, &rearm)));
    ASSERT_EQ(2000, rearm);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(moved, 4000, 2000, &rearm)));
    ASSERT_FALSE(c.hidden());
    // The pointer stops: hides one idle period after the last movement seen.
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 6000, 2000, &rearm)));
    ASSERT_TRUE(c.hidden());
    // Hidden, then the pointer moves with input excluded: the next tick shows it.
    rearm = -1;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(c.onTimeout(moved, 8000, 2000, &rearm)));
    ASSERT_EQ(2000, rearm);
    ASSERT_FALSE(c.hidden());
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 9999, 2000, &rearm)));
    ASSERT_EQ(1, rearm);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 10000, 2000, &rearm)));

    // Not fullscreen: pointer movement changes nothing.
    viewer_idle::Controller off;
    ASSERT_EQ(tickValue(viewer_idle::Tick::None), tickValue(off.onTimeout(moved, 5000, 2000, &rearm)));
}

// Adversary pass 2 A2: real fullscreen can end without actionFullscreen toggling (a window-manager
// restore). The state change restores at once, and any failing gate seen while hidden shows the cursor.
TEST(ViewerIdlePolicy, HiddenCursorShowsWhenAGateFailsOrTheWindowStateChanges)
{
    ASSERT_TRUE(viewer_idle::forcesRestore(QEvent::WindowStateChange));

    for (int gate = 0; gate < 4; ++gate)
    {
        viewer_idle::Controller c;
        int rearm = -1;
        c.setFullscreen(true, 0);
        ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm)));
        viewer_idle::Inputs in = allPass();
        if (gate == 0) in.fullscreen = false;
        if (gate == 1) in.windowActive = false;
        if (gate == 2) in.toolArmed = true;
        if (gate == 3) in.popupOrModalOpen = true;
        rearm = -1;
        ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(c.onTimeout(in, 2500, 2000, &rearm)));
        ASSERT_EQ(2000, rearm);
        ASSERT_FALSE(c.hidden());
    }
}

TEST(ViewerIdlePolicy, PointerWatchReportsOnlyAChangeSinceTheLastLook)
{
    viewer_idle::PointerWatch w;
    ASSERT_FALSE(w.moved(QPoint(10, 10)));   // first look only remembers
    ASSERT_FALSE(w.moved(QPoint(10, 10)));
    ASSERT_TRUE(w.moved(QPoint(11, 10)));
    ASSERT_FALSE(w.moved(QPoint(11, 10)));
    w.seed(QPoint(50, 60));
    ASSERT_FALSE(w.moved(QPoint(50, 60)));
    ASSERT_TRUE(w.moved(QPoint(50, 61)));
}

// P4
TEST(ViewerIdlePolicy, IdleHideIsTwoSeconds)
{
    ASSERT_EQ(2000, viewer_idle::kIdleHideMs());
}

// P5 (r2, adversary r1 pass 3 A1): the retrieval witness sees a move away and back that leaves the sampled
// position unchanged, ignores a synthesized move to the last position (Qt discards those too), counts a
// removed button or wheel, and is cleared by take/clear/seed. That is the position rule alone; a coalesced
// away-and-back (one move at the starting pixel) is the hook's history check plus noteExcursion (r6, P11/P12).
TEST(ViewerIdlePolicy, RetrievalWitnessSeesAwayAndBackAndIgnoresSamePositionMoves)
{
    viewer_idle::RetrievalWitness w;
    w.seed(QPoint(500, 500));
    ASSERT_FALSE(w.pending());
    w.note(viewer_idle::RetrievedKind::Move, QPoint(500, 500), true);   // synthesized, same position
    ASSERT_FALSE(w.take());

    w.note(viewer_idle::RetrievedKind::Move, QPoint(510, 500), true);   // away
    w.note(viewer_idle::RetrievedKind::Move, QPoint(500, 500), true);   // and back
    viewer_idle::PointerWatch sampled;
    sampled.seed(QPoint(500, 500));
    ASSERT_FALSE(sampled.moved(QPoint(500, 500)));   // the sample alone misses it
    ASSERT_TRUE(w.take());
    ASSERT_FALSE(w.take());                          // taken once

    // A peek that leaves a move in the queue counts it once; its removal at the same position does not again.
    w.note(viewer_idle::RetrievedKind::Move, QPoint(520, 500), false);
    w.note(viewer_idle::RetrievedKind::Move, QPoint(520, 500), true);
    ASSERT_TRUE(w.take());
    ASSERT_FALSE(w.take());

    // Buttons and wheel count when removed, not when only peeked.
    w.note(viewer_idle::RetrievedKind::Button, QPoint(520, 500), false);
    ASSERT_FALSE(w.take());
    w.note(viewer_idle::RetrievedKind::Button, QPoint(520, 500), true);
    ASSERT_TRUE(w.take());
    w.note(viewer_idle::RetrievedKind::Wheel, QPoint(520, 500), true);
    ASSERT_TRUE(w.take());

    // The event path delivered it: clear keeps the position, so the same position stays not-new.
    w.note(viewer_idle::RetrievedKind::Move, QPoint(530, 500), true);
    w.clear();
    ASSERT_FALSE(w.take());
    w.note(viewer_idle::RetrievedKind::Move, QPoint(530, 500), true);
    ASSERT_FALSE(w.take());

    // No position known: the first move counts.
    w.reset();
    w.note(viewer_idle::RetrievedKind::Move, QPoint(530, 500), true);
    ASSERT_TRUE(w.take());
    w.note(viewer_idle::RetrievedKind::Move, QPoint(540, 500), true);
    w.seed(QPoint(540, 500));
    ASSERT_FALSE(w.take());
}

// P6: a tick that reads the witness turns an away-and-back the sample missed into Show while hidden.
TEST(ViewerIdlePolicy, RetrievalWitnessReadAtTheTickShowsTheHiddenCursor)
{
    viewer_idle::Controller c;
    c.setFullscreen(true, 0);
    int rearm = 0;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm)));
    viewer_idle::PointerWatch sampled;
    sampled.seed(QPoint(500, 500));
    viewer_idle::RetrievalWitness w;
    w.seed(QPoint(500, 500));
    w.note(viewer_idle::RetrievedKind::Move, QPoint(510, 500), true);
    w.note(viewer_idle::RetrievedKind::Move, QPoint(500, 500), true);

    viewer_idle::Inputs without = allPass();
    without.pointerMoved = sampled.moved(QPoint(500, 500));
    viewer_idle::Controller old = c;   // the r1 tick read only the sample: it stays hidden
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(old.onTimeout(without, 4000, 2000, &rearm)));
    ASSERT_TRUE(old.hidden());

    viewer_idle::Inputs with = allPass();
    with.pointerMoved = sampled.moved(QPoint(500, 500));
    if (w.take()) with.pointerMoved = true;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(c.onTimeout(with, 4000, 2000, &rearm)));
    ASSERT_FALSE(c.hidden());
}

// P8 (r3, adversary r1 pass 4 A1): off Windows the position sample is the only witness during an input-excluded
// wait. Sampled once per idle period, a move away at 2100 and back at 2200 is invisible (the adversary's input);
// with the poll cap every rearm is at most kPointerPollMs(), so a sample lands while the pointer is away and the
// cursor shows within one idle period. The cap never changes when the cursor hides.
TEST(ViewerIdlePolicy, PollCapSamplesThePointerOftenEnoughToSeeAnAwayAndBack)
{
    ASSERT_TRUE(viewer_idle::kPointerPollMs() > 0);
    ASSERT_TRUE(viewer_idle::kPointerPollMs() <= 50);
    const int poll = viewer_idle::kPointerPollMs();

    // Where the pointer is at time t: away from 2100 until 2200, at (500,500) otherwise.
    auto pointerAt = [](qint64 t) { return (t >= 2100 && t < 2200) ? QPoint(510, 500) : QPoint(500, 500); };

    // Control: no cap (the pass-4 tick). Hidden at 2000, next look at 4000: the pointer is back, it stays hidden.
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        viewer_idle::PointerWatch sampled;
        sampled.seed(pointerAt(0));
        int rearm = 0;
        qint64 now = 2000;
        ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), now, 2000, &rearm)));
        ASSERT_EQ(2000, rearm);
        now += rearm;
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = sampled.moved(pointerAt(now));
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(in, now, 2000, &rearm)));
        ASSERT_TRUE(c.hidden());
    }

    // With the cap: follow the rearm intervals the controller hands back, sampling the pointer at each tick.
    viewer_idle::Controller c;
    c.setFullscreen(true, 0);
    viewer_idle::PointerWatch sampled;
    sampled.seed(pointerAt(0));
    int rearm = 0;
    qint64 now = poll;
    qint64 hiddenAt = -1;
    qint64 shownAt = -1;
    int ticks = 0;
    while (now <= 6000 && shownAt < 0 && ticks < 1000)
    {
        ++ticks;
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = sampled.moved(pointerAt(now));
        const viewer_idle::Tick t = c.onTimeout(in, now, 2000, &rearm, poll);
        ASSERT_TRUE(rearm >= 1);
        ASSERT_TRUE(rearm <= poll);   // every rearm, shown or hidden, is capped
        if (t == viewer_idle::Tick::Hide) hiddenAt = now;
        if (t == viewer_idle::Tick::Show) shownAt = now;
        now += rearm;
    }
    ASSERT_EQ(2000, int(hiddenAt));   // the cap does not move the hide
    ASSERT_TRUE(shownAt >= 2100);
    ASSERT_TRUE(shownAt <= 2100 + poll);
    ASSERT_FALSE(c.hidden());

    // pollMs 0 or negative is no cap (old callers); a cap above the interval changes nothing.
    viewer_idle::Controller d;
    d.setFullscreen(true, 0);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(d.onTimeout(allPass(), 500, 2000, &rearm, 0)));
    ASSERT_EQ(1500, rearm);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(d.onTimeout(allPass(), 500, 2000, &rearm, -5)));
    ASSERT_EQ(1500, rearm);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(d.onTimeout(allPass(), 500, 2000, &rearm, 5000)));
    ASSERT_EQ(1500, rearm);
}

// P9 (r3, adversary r1 pass 5 A1): the exact bound of the sampled witness, with the adversary's input. Hidden
// at 2000; the pointer is away from 2010 to 2070; the tick asked for 2050 fires late, at 2080 (a busy GUI
// thread). The excursion lies entirely between two samples, so the sample alone cannot see it: that is the
// documented non-promise, pinned here so no comment or PR text claims more. What does see it: on Windows the
// retrieval witness (the hook took both moves), and on every platform the event path once the wait delivers
// the deferred moves. An excursion still in progress at a late tick is seen by the sample.
TEST(ViewerIdlePolicy, SampledWitnessBoundIsAnExcursionBetweenTwoSamplesAndTheOtherWitnessesCoverIt)
{
    const int poll = viewer_idle::kPointerPollMs();
    viewer_idle::Controller hidden;
    hidden.setFullscreen(true, 0);
    int rearm = 0;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(hidden.onTimeout(allPass(), 2000, 2000, &rearm, poll)));
    ASSERT_EQ(poll, rearm);   // the tick asks for 2050

    viewer_idle::PointerWatch sampled;
    sampled.seed(QPoint(500, 500));   // the sample at 2000

    // The sample alone at the late tick (2080): back at (500,500), nothing seen, still hidden.
    {
        viewer_idle::Controller c = hidden;
        viewer_idle::PointerWatch s = sampled;
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = s.moved(QPoint(500, 500));
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(in, 2080, 2000, &rearm, poll)));
        ASSERT_TRUE(c.hidden());

        // The wait returns and Qt delivers the deferred moves: the event path notes activity and restores.
        ASSERT_TRUE(c.noteActivity(2300));
        ASSERT_FALSE(c.hidden());
    }

    // Windows: the hook took both moves when the wait removed them, so the same late tick shows.
    {
        viewer_idle::Controller c = hidden;
        viewer_idle::PointerWatch s = sampled;
        viewer_idle::RetrievalWitness w;
        w.seed(QPoint(500, 500));
        w.note(viewer_idle::RetrievedKind::Move, QPoint(510, 500), true);
        w.note(viewer_idle::RetrievedKind::Move, QPoint(500, 500), true);
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = s.moved(QPoint(500, 500));
        if (w.take()) in.pointerMoved = true;
        ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(c.onTimeout(in, 2080, 2000, &rearm, poll)));
        ASSERT_FALSE(c.hidden());
    }

    // Still away at the late tick (back only at 2090): the sample sees it however late the tick is.
    {
        viewer_idle::Controller c = hidden;
        viewer_idle::PointerWatch s = sampled;
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = s.moved(QPoint(510, 500));
        ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(c.onTimeout(in, 2080, 2000, &rearm, poll)));
        ASSERT_FALSE(c.hidden());
    }
}

// P10 (r4, adversary r1 pass 6 A1): the sub-logical-pixel bound of the sampled witness, with the adversary's
// input. QCursor::pos() is the native position rounded to a logical QPoint (QHighDpi divides by the scale factor
// and rounds), and Qt has no public sub-logical-pixel cursor position. At scale 3 a persistent move from native
// (300,300) to (301,300) rounds to the same QPoint(100,100), so the sample alone does not see it at any tick: that
// is the documented non-promise, pinned here so no comment or PR text claims more. What does see it: the event path
// once the wait delivers the move (positions compared unrounded), and on Windows the retrieval witness (the hook's
// own physical pixels). A displacement of one logical pixel or more on either axis always changes the rounded point,
// at every scale factor, so it is always seen at the next sample.
TEST(ViewerIdlePolicy, SampledWitnessBoundIsASubLogicalPixelMoveAndTheOtherWitnessesCoverIt)
{
    const int poll = viewer_idle::kPointerPollMs();
    // The QHighDpi fromNative rounding for a point at screen origin (0,0): (pos / scale), each axis qRound-ed.
    auto logical = [](const QPoint &native, qreal scale) {
        return QPoint(qRound(native.x() / scale), qRound(native.y() / scale));
    };
    const qreal scale = 3.0;
    const QPoint before(300, 300);
    const QPoint after(301, 300);
    ASSERT_TRUE(logical(before, scale) == QPoint(100, 100));
    ASSERT_TRUE(logical(after, scale) == QPoint(100, 100));   // the adversary's input rounds to one point

    viewer_idle::Controller hidden;
    hidden.setFullscreen(true, 0);
    int rearm = 0;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(hidden.onTimeout(allPass(), 2000, 2000, &rearm, poll)));

    // The sample alone (off Windows): moved at 2010 and held there, every tick to 6000 stays hidden, past 4050.
    {
        viewer_idle::Controller c = hidden;
        viewer_idle::PointerWatch s;
        s.seed(logical(before, scale));
        qint64 now = 2000 + rearm;
        qint64 lastTick = 0;
        int ticks = 0;
        while (now <= 6000 && ticks < 1000)
        {
            ++ticks;
            viewer_idle::Inputs in = allPass();
            in.pointerMoved = s.moved(logical(now >= 2010 ? after : before, scale));
            ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(in, now, 2000, &rearm, poll)));
            ASSERT_TRUE(c.hidden());
            lastTick = now;
            now += rearm;
        }
        ASSERT_TRUE(lastTick > 4050);

        // The wait returns and Qt delivers the move: the event path compares unrounded positions and restores.
        const QPointF globalBefore(before.x() / scale, before.y() / scale);
        const QPointF globalAfter(after.x() / scale, after.y() / scale);
        ASSERT_TRUE(viewer_idle::isActivity(QEvent::MouseMove, globalAfter, globalBefore, true));
        ASSERT_TRUE(c.noteActivity(6010));
        ASSERT_FALSE(c.hidden());
    }

    // Windows: the hook records physical pixels, so the same one-pixel move shows at the next tick.
    {
        viewer_idle::Controller c = hidden;
        viewer_idle::PointerWatch s;
        s.seed(logical(before, scale));
        viewer_idle::RetrievalWitness w;
        w.seed(before);
        w.note(viewer_idle::RetrievedKind::Move, after, true);
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = s.moved(logical(after, scale));
        ASSERT_FALSE(in.pointerMoved);
        if (w.take()) in.pointerMoved = true;
        ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(c.onTimeout(in, 2050, 2000, &rearm, poll)));
        ASSERT_FALSE(c.hidden());
    }

    // The bound is below one logical pixel and no wider: a displacement of ceil(scale) native pixels (one logical
    // pixel or more) on either axis changes the rounded point from every starting pixel, at every scale factor.
    const qreal scales[] = { 1.0, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0 };
    for (const qreal f : scales)
    {
        const int step = int(f) < f ? int(f) + 1 : int(f);
        for (int x = -40; x <= 40; ++x)
        {
            const QPoint from(x, x + 7);
            viewer_idle::PointerWatch s;
            s.seed(logical(from, f));
            ASSERT_TRUE(s.moved(logical(QPoint(x + step, x + 7), f)));
            s.seed(logical(from, f));
            ASSERT_TRUE(s.moved(logical(QPoint(x - step, x + 7), f)));
            s.seed(logical(from, f));
            ASSERT_TRUE(s.moved(logical(QPoint(x, x + 7 + step), f)));
        }
    }
    // At scale 1 there is no sub-logical pixel: every one-pixel move is seen.
    {
        viewer_idle::PointerWatch s;
        s.seed(logical(before, 1.0));
        ASSERT_TRUE(s.moved(logical(after, 1.0)));
    }
}

#ifdef Q_OS_WIN
// P7: the WH_MOUSE procedure maps each mouse message to its kind, records the hook position, passes every
// message on, and the hook installs for this thread and removes cleanly.
TEST(ViewerIdlePolicy, RetrievalHookProcRecordsMouseMessagesAndInstallsForThisThread)
{
    viewer_idle::RetrievedKind kind = viewer_idle::RetrievedKind::Wheel;
    ASSERT_TRUE(viewer_idle::win_hook::kindOfMessage(WM_MOUSEMOVE, &kind));
    ASSERT_TRUE(kind == viewer_idle::RetrievedKind::Move);
    ASSERT_TRUE(viewer_idle::win_hook::kindOfMessage(WM_NCMOUSEMOVE, &kind));
    ASSERT_TRUE(kind == viewer_idle::RetrievedKind::Move);
    ASSERT_TRUE(viewer_idle::win_hook::kindOfMessage(WM_LBUTTONDOWN, &kind));
    ASSERT_TRUE(kind == viewer_idle::RetrievedKind::Button);
    ASSERT_TRUE(viewer_idle::win_hook::kindOfMessage(WM_XBUTTONUP, &kind));
    ASSERT_TRUE(kind == viewer_idle::RetrievedKind::Button);
    ASSERT_TRUE(viewer_idle::win_hook::kindOfMessage(WM_MOUSEHWHEEL, &kind));
    ASSERT_TRUE(kind == viewer_idle::RetrievedKind::Wheel);
    ASSERT_FALSE(viewer_idle::win_hook::kindOfMessage(WM_KEYDOWN, &kind));   // a key press is not mouse input
    ASSERT_FALSE(viewer_idle::win_hook::kindOfMessage(WM_TIMER, &kind));

    viewer_idle::win_hook::witness().seed(QPoint(500, 500));
    MOUSEHOOKSTRUCT away = {};
    away.pt.x = 510;
    away.pt.y = 500;
    MOUSEHOOKSTRUCT back = {};
    back.pt.x = 500;
    back.pt.y = 500;
    viewer_idle::win_hook::proc(HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>(&away));
    viewer_idle::win_hook::proc(HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>(&back));
    ASSERT_TRUE(viewer_idle::win_hook::witness().take());
    viewer_idle::win_hook::proc(HC_ACTION, WM_KEYDOWN, reinterpret_cast<LPARAM>(&away));
    viewer_idle::win_hook::proc(-1, WM_MOUSEMOVE, reinterpret_cast<LPARAM>(&away));   // code < 0: not ours
    viewer_idle::win_hook::proc(HC_ACTION, WM_MOUSEMOVE, 0);
    ASSERT_FALSE(viewer_idle::win_hook::witness().take());

    ASSERT_TRUE(viewer_idle::win_hook::install());
    ASSERT_TRUE(viewer_idle::win_hook::installed());
    ASSERT_TRUE(viewer_idle::win_hook::install());   // idempotent
    viewer_idle::win_hook::remove();
    ASSERT_FALSE(viewer_idle::win_hook::installed());
    viewer_idle::win_hook::remove();
    ASSERT_FALSE(viewer_idle::win_hook::witness().pending());
}
#endif

namespace
{
viewer_idle::HistoryPoint hp(int x, int y, quint32 time)
{
    viewer_idle::HistoryPoint p;
    p.x = x;
    p.y = y;
    p.time = time;
    return p;
}
} // namespace

// P11 (r6, key B1): MoveHistoryMark tells a coalesced away-and-back from a synthesized same-position move by the
// system move history. Points newer than the mark are those in front of where the marked copy lines up again.
TEST(ViewerIdlePolicy, MoveHistoryMarkSeesAnExcursionAfterTheMarkAndNothingElse)
{
    const viewer_idle::HistoryPoint marked[] = {hp(500, 500, 1000), hp(490, 500, 990), hp(480, 500, 980)};
    viewer_idle::MoveHistoryMark m;
    ASSERT_FALSE(m.marked());
    ASSERT_FALSE(m.leftSinceMark(marked, 3, 500, 500));   // unmarked never counts (the r5 rule stands)
    m.mark(marked, 3);
    ASSERT_TRUE(m.marked());

    // Nothing new: the history is the marked copy.
    ASSERT_FALSE(m.leftSinceMark(marked, 3, 500, 500));
    // A synthesized move adds no point elsewhere; even a new point at the same pixel is not movement.
    const viewer_idle::HistoryPoint samePixel[] = {hp(500, 500, 1300), hp(500, 500, 1000), hp(490, 500, 990)};
    ASSERT_FALSE(m.leftSinceMark(samePixel, 3, 500, 500));
    // The sol row: away to (510,500) and back to (500,500) after the mark.
    const viewer_idle::HistoryPoint awayAndBack[] = {hp(500, 500, 1200), hp(510, 500, 1100), hp(500, 500, 1000),
                                                     hp(490, 500, 990)};
    ASSERT_TRUE(m.leftSinceMark(awayAndBack, 4, 500, 500));
    // The same excursion inside one time-stamp quantum of the mark: order, not time, decides.
    const viewer_idle::HistoryPoint sameQuantum[] = {hp(500, 500, 1000), hp(510, 500, 1000), hp(500, 500, 1000),
                                                     hp(490, 500, 990)};
    ASSERT_TRUE(m.leftSinceMark(sameQuantum, 4, 500, 500));
    // The approach to the pixel in the mark's own quantum is older than the mark: not counted.
    const viewer_idle::HistoryPoint approach[] = {hp(500, 500, 1000), hp(490, 500, 1000)};
    viewer_idle::MoveHistoryMark q;
    q.mark(approach, 2);
    ASSERT_FALSE(q.leftSinceMark(approach, 2, 500, 500));
    // A negative (left monitor) excursion counts like any other.
    const viewer_idle::HistoryPoint negative[] = {hp(500, 500, 1200), hp(-20, 500, 1100), hp(500, 500, 1000),
                                                  hp(490, 500, 990)};
    ASSERT_TRUE(m.leftSinceMark(negative, 4, 500, 500));
    // The marked copy scrolled out of the history (more new points than it holds): a point elsewhere counts.
    viewer_idle::HistoryPoint scrolled[viewer_idle::MoveHistoryMark::kMaxPoints];
    for (int i = 0; i < viewer_idle::MoveHistoryMark::kMaxPoints; ++i)
        scrolled[i] = hp(500 + (i % 2), 500, 5000u - quint32(i));
    scrolled[0] = hp(500, 500, 5000);
    ASSERT_TRUE(m.leftSinceMark(scrolled, viewer_idle::MoveHistoryMark::kMaxPoints, 500, 500));
    // A failed read (-1) or an empty one never counts.
    ASSERT_FALSE(m.leftSinceMark(awayAndBack, -1, 500, 500));
    ASSERT_FALSE(m.leftSinceMark(awayAndBack, 0, 500, 500));
    // A failed mark unmarks.
    m.mark(marked, -1);
    ASSERT_FALSE(m.marked());
    ASSERT_FALSE(m.leftSinceMark(awayAndBack, 4, 500, 500));
}

#ifdef Q_OS_WIN
namespace
{
viewer_idle::HistoryPoint g_script[viewer_idle::MoveHistoryMark::kMaxPoints];
int g_scriptCount = 0;
int scriptedHistory(const QPoint &, viewer_idle::HistoryPoint *out, int max)
{
    if (g_scriptCount <= 0) return -1;
    const int n = qMin(max, g_scriptCount);
    for (int i = 0; i < n; ++i) out[i] = g_script[i];
    return n;
}
void setScript(std::initializer_list<viewer_idle::HistoryPoint> points)
{
    g_scriptCount = 0;
    for (const viewer_idle::HistoryPoint &p : points) g_script[g_scriptCount++] = p;
}
} // namespace

// P12 (r6, key B1, the sol catalogue row at the hook): hidden, marked at (500,500); the excursion is coalesced into
// ONE WM_MOUSEMOVE at (500,500). The hook reads the history and the witness is pending. A synthesized same-position
// move (no point elsewhere in the history) is not pending; an unmarked hook keeps the r5 position rule; any
// pending movement unmarks, and removing the hook unmarks.
TEST(ViewerIdlePolicy, RetrievalHookSeesACoalescedAwayAndBackOnlyThroughTheMarkedHistory)
{
    viewer_idle::win_hook::HistoryReader saved = viewer_idle::win_hook::historyReader();
    viewer_idle::win_hook::historyReader() = scriptedHistory;
    MOUSEHOOKSTRUCT back = {};
    back.pt.x = 500;
    back.pt.y = 500;

    // The hide marks the history.
    viewer_idle::win_hook::witness().seed(QPoint(500, 500));
    setScript({hp(500, 500, 1000), hp(490, 500, 990)});
    viewer_idle::win_hook::markHistory();
    ASSERT_TRUE(viewer_idle::win_hook::historyMark().marked());

    // The app's own hide provokes a same-position move: nothing in the history moved.
    setScript({hp(500, 500, 1050), hp(500, 500, 1000), hp(490, 500, 990)});
    viewer_idle::win_hook::proc(HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>(&back));
    ASSERT_FALSE(viewer_idle::win_hook::witness().take());
    ASSERT_TRUE(viewer_idle::win_hook::historyMark().marked());

    // Away and back before retrieval, coalesced: one move at the starting pixel.
    setScript({hp(500, 500, 1200), hp(510, 500, 1100), hp(500, 500, 1050), hp(500, 500, 1000), hp(490, 500, 990)});
    viewer_idle::win_hook::proc(HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>(&back));
    ASSERT_TRUE(viewer_idle::win_hook::witness().take());
    ASSERT_FALSE(viewer_idle::win_hook::historyMark().marked());   // used up; the next hide marks afresh

    // Unmarked (the cursor is shown, or the mark failed): the r5 position rule.
    viewer_idle::win_hook::proc(HC_ACTION, WM_NCMOUSEMOVE, reinterpret_cast<LPARAM>(&back));
    ASSERT_FALSE(viewer_idle::win_hook::witness().take());

    // A mark that cannot be read (no point at the cursor) unmarks.
    g_scriptCount = 0;
    viewer_idle::win_hook::markHistory();
    ASSERT_FALSE(viewer_idle::win_hook::historyMark().marked());

    // A move seen by position unmarks too; remove() unmarks.
    setScript({hp(500, 500, 1000)});
    viewer_idle::win_hook::markHistory();
    MOUSEHOOKSTRUCT away = {};
    away.pt.x = 510;
    away.pt.y = 500;
    viewer_idle::win_hook::proc(HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>(&away));
    ASSERT_TRUE(viewer_idle::win_hook::witness().take());
    ASSERT_FALSE(viewer_idle::win_hook::historyMark().marked());
    viewer_idle::win_hook::markHistory();
    viewer_idle::win_hook::remove();
    ASSERT_FALSE(viewer_idle::win_hook::historyMark().marked());

    viewer_idle::win_hook::historyReader() = saved;
    // The real reader runs: a point that is not in the history is -1, never a crash.
    viewer_idle::win_hook::remove();
    viewer_idle::HistoryPoint out[viewer_idle::MoveHistoryMark::kMaxPoints];
    const int n = viewer_idle::win_hook::readSystemHistory(QPoint(-31000, -31000), out,
                                                           viewer_idle::MoveHistoryMark::kMaxPoints);
    ASSERT_TRUE(n >= -1 && n <= viewer_idle::MoveHistoryMark::kMaxPoints);
}

// P13 (r6, adversary pass 8 A1): the cursor is SHOWN, last activity at 0, both trackers at (500,500). The tick at
// 1950 sees nothing and marks the history (as every MainWindow tick does). In an input-excluded wait the pointer
// goes to (510,500) at 1960 and back at 1980; Windows coalesces that into one WM_MOUSEMOVE at (500,500), removed at
// 1985. The tick at 2000 must not hide 20 ms after real movement: the excursion restarts the idle period. Control:
// an unmarked history (the 78630f2d mark-at-hide-only tick) hides at 2000.
TEST(ViewerIdlePolicy, CoalescedExcursionWhileShownRestartsTheIdlePeriod)
{
    viewer_idle::win_hook::HistoryReader saved = viewer_idle::win_hook::historyReader();
    viewer_idle::win_hook::historyReader() = scriptedHistory;
    MOUSEHOOKSTRUCT back = {};
    back.pt.x = 500;
    back.pt.y = 500;
    for (const bool markEachTick : {false, true})
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        viewer_idle::PointerWatch sampled;
        sampled.seed(QPoint(500, 500));
        viewer_idle::win_hook::witness().seed(QPoint(500, 500));
        viewer_idle::win_hook::historyMark().unmark();
        int rearm = 0;

        // Tick at 1950: nothing moved; then the mark.
        setScript({hp(500, 500, 900), hp(490, 500, 890)});
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = sampled.moved(QPoint(500, 500));
        if (viewer_idle::win_hook::witness().take()) in.pointerMoved = true;
        if (markEachTick) viewer_idle::win_hook::markHistory();
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(in, 1950, 2000, &rearm, 50)));
        ASSERT_FALSE(c.hidden());

        // The coalesced excursion is retrieved at 1985.
        setScript({hp(500, 500, 1980), hp(510, 500, 1960), hp(500, 500, 900), hp(490, 500, 890)});
        viewer_idle::win_hook::proc(HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>(&back));

        // Tick at 2000.
        in = allPass();
        in.pointerMoved = sampled.moved(QPoint(500, 500));
        if (viewer_idle::win_hook::witness().take()) in.pointerMoved = true;
        const viewer_idle::Tick tick = c.onTimeout(in, 2000, 2000, &rearm, 50);
        if (markEachTick)
        {
            ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(tick));
            ASSERT_FALSE(c.hidden());
            // The idle period restarted at 2000: still shown at 3999, hidden at 4000.
            ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 3999, 2000, &rearm, 50)));
            ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 4000, 2000, &rearm, 50)));
        }
        else
        {
            ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(tick));   // the defect: 20 ms after movement
        }
    }
    viewer_idle::win_hook::historyReader() = saved;
    viewer_idle::win_hook::remove();
}

// P13b (r11, adversary pass 15 A1): shown, idle 2000 ms, poll 50 ms. The tick at 1950 marks (500,500). At 1960 the hook
// retrieves a move to (510,500), which unmarks the history, and the move is delivered: the event path notes activity
// at 1960 and clears the witness. In an input-excluded wait the pointer goes to (520,500) at 1970 and back to (510,500)
// at 1980; the coalesced WM_MOUSEMOVE at (510,500) is retrieved at 1985. Expected: the excursion is seen and the idle
// period restarts at the 2000 tick (still shown at 3999, hidden at 4000). RED control: the bare clear (the event path
// before r11) leaves the history unmarked, so the hook judges the move by position alone and the cursor hides at 3960,
// 1980 ms after real movement.
TEST(ViewerIdlePolicy, DeliveredMoveMarksAgainSoALaterCoalescedExcursionRestartsTheIdlePeriod)
{
    viewer_idle::win_hook::HistoryReader saved = viewer_idle::win_hook::historyReader();
    viewer_idle::win_hook::historyReader() = scriptedHistory;
    MOUSEHOOKSTRUCT away = {};
    away.pt.x = 510;
    away.pt.y = 500;
    for (const bool markOnDelivery : {false, true})
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        viewer_idle::win_hook::witness().seed(QPoint(500, 500));
        viewer_idle::win_hook::historyMark().unmark();
        int rearm = 0;

        // Tick at 1950: nothing moved; then the mark (as every MainWindow tick).
        setScript({hp(500, 500, 900), hp(490, 500, 890)});
        viewer_idle::Inputs in = allPass();
        if (viewer_idle::win_hook::witness().take()) in.pointerMoved = true;
        viewer_idle::win_hook::markHistory();
        ASSERT_TRUE(viewer_idle::win_hook::historyMark().marked());
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(in, 1950, 2000, &rearm, 50)));

        // 1960: the hook retrieves the move (it unmarks), then the move is delivered and noted.
        setScript({hp(510, 500, 1960), hp(500, 500, 900), hp(490, 500, 890)});
        viewer_idle::win_hook::proc(HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>(&away));
        ASSERT_FALSE(viewer_idle::win_hook::historyMark().marked());
        c.noteActivity(1960);
        if (markOnDelivery) viewer_idle::win_hook::noteDelivered();
        else viewer_idle::win_hook::witness().clear();   // the event path before r11 (RED control)
        ASSERT_EQ(markOnDelivery, viewer_idle::win_hook::historyMark().marked());

        // 1985: the coalesced excursion (to 520 at 1970, back to 510 at 1980) is retrieved at (510,500).
        setScript({hp(510, 500, 1980), hp(520, 500, 1970), hp(510, 500, 1960), hp(500, 500, 900), hp(490, 500, 890)});
        viewer_idle::win_hook::proc(HC_ACTION, WM_MOUSEMOVE, reinterpret_cast<LPARAM>(&away));

        // Tick at 2000.
        in = allPass();
        if (viewer_idle::win_hook::witness().take()) in.pointerMoved = true;
        ASSERT_EQ(markOnDelivery, in.pointerMoved);
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(in, 2000, 2000, &rearm, 50)));
        if (markOnDelivery)
        {
            ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 3960, 2000, &rearm, 50)));
            ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 3999, 2000, &rearm, 50)));
            ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 4000, 2000, &rearm, 50)));
        }
        else
        {
            // The defect: hidden 1980 ms after the movement at 1980.
            ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 3960, 2000, &rearm, 50)));
        }

        // A delivered event with the mark still standing keeps it (no history read).
        if (markOnDelivery)
        {
            viewer_idle::win_hook::markHistory();
            g_scriptCount = 0;   // a read now would unmark
            viewer_idle::win_hook::noteDelivered();
            ASSERT_TRUE(viewer_idle::win_hook::historyMark().marked());
            ASSERT_FALSE(viewer_idle::win_hook::witness().take());
        }
    }
    viewer_idle::win_hook::historyReader() = saved;
    viewer_idle::win_hook::remove();
}

namespace
{
QPoint g_cursor(500, 500);
DWORD g_queueStatus = 0;
bool scriptedCursor(QPoint *at)
{
    *at = g_cursor;
    return true;
}
DWORD scriptedQueueStatus(UINT flags)
{
    return g_queueStatus & ((DWORD(flags) << 16) | flags);
}
// The b03e08c5 tick's queue fallback and mark (the r7 RED control): any queued mouse input counts.
bool b03e08c5QueueFallback()
{
    const bool moved = (HIWORD(viewer_idle::win_hook::queueStatusReader()(QS_MOUSE)) & QS_MOUSE) != 0;
    viewer_idle::win_hook::markHistory();
    return moved;
}
} // namespace

// P15 (r7, key B1, the sol r6 catalogue row): the QueueLook grammar. A queued button or wheel counts; a queued move
// counts iff the cursor is not where it was at the last look, or the history has a point elsewhere newer than the last
// mark; unknown counts; nothing queued does not.
TEST(ViewerIdlePolicy, QueueLookCountsAQueuedMoveOnlyWhenThePointerReallyMoved)
{
    viewer_idle::QueueLook q;
    ASSERT_FALSE(viewer_idle::queuedMouseIsActivity(q));   // nothing queued
    q.buttonQueued = true;
    ASSERT_TRUE(viewer_idle::queuedMouseIsActivity(q));    // a button or wheel
    q.buttonQueued = false;
    q.moveQueued = true;
    ASSERT_TRUE(viewer_idle::queuedMouseIsActivity(q));    // a move, cursor unknown
    q.haveCursor = true;
    q.cursor = QPoint(500, 500);
    ASSERT_TRUE(viewer_idle::queuedMouseIsActivity(q));    // a move, no last look
    q.haveLastLook = true;
    q.lastLook = QPoint(500, 500);
    ASSERT_FALSE(viewer_idle::queuedMouseIsActivity(q));   // the row: a synthesized move at the sampled position
    q.leftSinceMark = true;
    ASSERT_TRUE(viewer_idle::queuedMouseIsActivity(q));    // a coalesced away-and-back still queued
    q.leftSinceMark = false;
    q.cursor = QPoint(510, 500);
    ASSERT_TRUE(viewer_idle::queuedMouseIsActivity(q));    // the control: a move at another point
    q.cursor = QPoint(500, 500);
    q.buttonQueued = true;
    ASSERT_TRUE(viewer_idle::queuedMouseIsActivity(q));    // a button behind a synthesized move
}

// P16 (r7, key B1, the sol r6 repro): hidden at 2000 ms, sample and retrieved position (500,500), unchanged history,
// empty witness. A nonactivating window visibility change makes Windows synthesize a move at (500,500) that is still
// queued during the 2050 ms tick: GetQueueStatus(QS_MOUSE) = 0x00020002. The r7 tick stays hidden; the b03e08c5 tick
// (RED control) shows. Shown variant: the same queued move at 1950 does not reset the idle clock (hides at 2000).
// Control: the queued move with the cursor at (510,500) shows. H1: an away-and-back completed before the tick's mark
// whose coalesced move is still queued is judged against the previous mark and shows.
TEST(ViewerIdlePolicy, QueuedSynthesizedSamePositionMoveLeavesTheCursorHiddenAndTheIdleClockAlone)
{
    viewer_idle::win_hook::HistoryReader savedHistory = viewer_idle::win_hook::historyReader();
    viewer_idle::win_hook::CursorReader savedCursor = viewer_idle::win_hook::cursorReader();
    viewer_idle::win_hook::QueueStatusReader savedQueue = viewer_idle::win_hook::queueStatusReader();
    viewer_idle::win_hook::historyReader() = scriptedHistory;
    viewer_idle::win_hook::cursorReader() = scriptedCursor;
    viewer_idle::win_hook::queueStatusReader() = scriptedQueueStatus;
    int rearm = 0;

    // The row, then its RED control (run 0: the r7 tick; run 1: the b03e08c5 tick).
    for (int run = 0; run < 2; ++run)
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        g_cursor = QPoint(500, 500);
        g_queueStatus = 0;
        viewer_idle::PointerWatch sampled;
        sampled.seed(QPoint(500, 500));
        viewer_idle::win_hook::witness().seed(QPoint(500, 500));
        setScript({hp(500, 500, 900), hp(490, 500, 890)});
        viewer_idle::win_hook::markHistory();   // install()
        ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm, 50)));
        viewer_idle::win_hook::markHistory();   // the 2000 tick's mark

        g_queueStatus = 0x00020002;
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = sampled.moved(QPoint(500, 500));
        if (viewer_idle::win_hook::witness().take()) in.pointerMoved = true;
        if (run == 0 ? viewer_idle::win_hook::lookAtQueue() : b03e08c5QueueFallback()) in.pointerMoved = true;
        const viewer_idle::Tick tick = c.onTimeout(in, 2050, 2000, &rearm, 50);
        if (run == 0)
        {
            ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(tick));
            ASSERT_TRUE(c.hidden());
        }
        else
        {
            ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(tick));   // the defect
            ASSERT_FALSE(c.hidden());
        }
    }

    // Shown: the queued synthesized move at 1950 does not reset the idle clock.
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        g_cursor = QPoint(500, 500);
        g_queueStatus = 0;
        setScript({hp(500, 500, 900)});
        viewer_idle::win_hook::markHistory();
        g_queueStatus = 0x00020002;
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = viewer_idle::win_hook::lookAtQueue();
        ASSERT_FALSE(in.pointerMoved);
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(in, 1950, 2000, &rearm, 50)));
        in.pointerMoved = viewer_idle::win_hook::lookAtQueue();
        ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(in, 2000, 2000, &rearm, 50)));
    }

    // Control: the queued move with the cursor at another point shows.
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        g_cursor = QPoint(500, 500);
        g_queueStatus = 0;
        setScript({hp(500, 500, 900)});
        viewer_idle::win_hook::markHistory();
        ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm, 50)));
        g_cursor = QPoint(510, 500);
        setScript({hp(510, 500, 2010), hp(500, 500, 900)});
        g_queueStatus = 0x00020002;
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = viewer_idle::win_hook::lookAtQueue();
        ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(c.onTimeout(in, 2050, 2000, &rearm, 50)));
    }

    // H1: hidden, marked at 2000; away to (510,500) and back before the 2050 tick's mark, the coalesced move still
    // queued. The one read is judged against the 2000 mark: it shows. A button still queued shows as well.
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        g_cursor = QPoint(500, 500);
        g_queueStatus = 0;
        setScript({hp(500, 500, 900)});
        viewer_idle::win_hook::markHistory();
        ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm, 50)));
        setScript({hp(500, 500, 2040), hp(510, 500, 2020), hp(500, 500, 900)});
        g_queueStatus = 0x00020002;
        viewer_idle::Inputs in = allPass();
        in.pointerMoved = viewer_idle::win_hook::lookAtQueue();
        ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(c.onTimeout(in, 2050, 2000, &rearm, 50)));
        // That read is the new mark: the same queued move at the next tick is not movement again.
        ASSERT_FALSE(viewer_idle::win_hook::lookAtQueue());
        g_queueStatus = 0x00040004;   // QS_MOUSEBUTTON
        ASSERT_TRUE(viewer_idle::win_hook::lookAtQueue());
    }

    g_queueStatus = 0;
    viewer_idle::win_hook::historyReader() = savedHistory;
    viewer_idle::win_hook::cursorReader() = savedCursor;
    viewer_idle::win_hook::queueStatusReader() = savedQueue;
    viewer_idle::win_hook::remove();
    // The real readers run: nothing is queued on this console thread, so the look is not activity.
    ASSERT_FALSE(viewer_idle::win_hook::lookAtQueue());
    viewer_idle::win_hook::remove();
}
#endif

// P17 (r7, adversary pass 11 A1): fullscreen entered at 0 and hidden at 2000; at 2101 the window manager restores the
// window without the action (isFullScreen() false, the action still checked). The next tick must leave the idle
// machinery. Before the window is first seen fullscreen (showFullScreen() is asynchronous) it waits out the grace
// period; a backwards clock leaves; not fullscreen never asks to leave; entering again starts a fresh look.
TEST(ViewerIdlePolicy, WindowLeftFullscreenWithoutTheActionAsksTheCallerToLeave)
{
    viewer_idle::Controller c;
    ASSERT_FALSE(c.windowLeftFullscreen(false, 0, 2000));   // not fullscreen
    c.setFullscreen(true, 0);
    ASSERT_FALSE(c.windowLeftFullscreen(false, 50, 2000));  // showFullScreen() still pending
    ASSERT_FALSE(c.windowLeftFullscreen(true, 100, 2000));
    int rearm = 0;
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm, 50)));
    ASSERT_FALSE(c.windowLeftFullscreen(true, 2050, 2000));
    ASSERT_TRUE(c.windowLeftFullscreen(false, 2101, 2000));  // the pass-11 input
    c.setFullscreen(false, 2101);
    ASSERT_FALSE(c.hidden());
    ASSERT_FALSE(c.windowLeftFullscreen(false, 2150, 2000));

    // Never seen fullscreen: leaves once the grace period is over (or the clock went backwards).
    c.setFullscreen(true, 3000);
    ASSERT_FALSE(c.windowLeftFullscreen(false, 4999, 2000));
    ASSERT_TRUE(c.windowLeftFullscreen(false, 5000, 2000));
    ASSERT_TRUE(c.windowLeftFullscreen(false, 2999, 2000));
    // Entering again forgets the earlier sighting.
    ASSERT_FALSE(c.windowLeftFullscreen(true, 5000, 2000));
    c.setFullscreen(true, 6000);
    ASSERT_FALSE(c.windowLeftFullscreen(false, 6050, 2000));
}

// P18 (r10, sol r9 B1 / carried A1-pass12): sol's row. Entered through the action at 0, hidden at 2000; native exit at
// 2101 (the action stays checked) is a Stop; the tick at 2150 does nothing; native re-entry at 2300 with the action
// still checked is a Start (the action's toggled() never fires), after which asking again records the sighting; the
// pointer stays still until 10000, so the cursor hides again at 4300 and stays hidden. A repeated change is None; the
// action unchecked stops a running machinery and never starts one; a pending entry waits out its grace.
// Control: the r9 lifecycle (start only on the action's toggle) never hides again after the native exit.
TEST(ViewerIdlePolicy, WindowStateChangeRestartsTheMachineryOnNativeReentryWithTheActionChecked)
{
    auto lifecycle = [](viewer_idle::Lifecycle l) { return int(l); };
    const int none = lifecycle(viewer_idle::Lifecycle::None);
    const int start = lifecycle(viewer_idle::Lifecycle::Start);
    const int stop = lifecycle(viewer_idle::Lifecycle::Stop);
    int rearm = 0;

    // Control: the r9 lifecycle. After the native exit nothing restarts; every later tick is None and never hides.
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        ASSERT_FALSE(c.windowLeftFullscreen(true, 50, 2000));
        ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm, 50)));
        ASSERT_TRUE(c.windowLeftFullscreen(false, 2150, 2000));
        c.setFullscreen(false, 2150);
        ASSERT_EQ(tickValue(viewer_idle::Tick::None), tickValue(c.onTimeout(allPass(), 10000, 2000, &rearm, 50)));
        ASSERT_FALSE(c.hidden());
    }

    viewer_idle::Controller c;
    ASSERT_EQ(none, lifecycle(c.onWindowState(false, false, 0, 2000)));
    c.setFullscreen(true, 0);                                                  // the action's toggled(true)
    ASSERT_EQ(none, lifecycle(c.onWindowState(true, false, 10, 2000)));        // showFullScreen() pending: grace
    ASSERT_EQ(none, lifecycle(c.onWindowState(true, true, 20, 2000)));         // seen fullscreen
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm, 50)));
    ASSERT_EQ(stop, lifecycle(c.onWindowState(true, false, 2101, 2000)));      // native exit, action checked
    c.setFullscreen(false, 2101);
    ASSERT_FALSE(c.hidden());
    ASSERT_FALSE(c.windowLeftFullscreen(false, 2150, 2000));                   // the tick at 2150
    ASSERT_EQ(tickValue(viewer_idle::Tick::None), tickValue(c.onTimeout(allPass(), 2150, 2000, &rearm, 50)));
    ASSERT_EQ(none, lifecycle(c.onWindowState(true, false, 2200, 2000)));      // still out: nothing to stop
    ASSERT_EQ(start, lifecycle(c.onWindowState(true, true, 2300, 2000)));      // native re-entry, action checked
    c.setFullscreen(true, 2300);
    ASSERT_EQ(none, lifecycle(c.onWindowState(true, true, 2300, 2000)));       // the sighting
    ASSERT_EQ(none, lifecycle(c.onWindowState(true, true, 2400, 2000)));       // repeated: idempotent
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 4299, 2000, &rearm, 50)));
    ASSERT_FALSE(c.hidden());
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 4300, 2000, &rearm, 50)));
    ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 10000, 2000, &rearm, 50)));
    ASSERT_TRUE(c.hidden());

    // The action unchecked: a running machinery stops; a stopped one never starts, really fullscreen or not.
    ASSERT_EQ(stop, lifecycle(c.onWindowState(false, true, 10050, 2000)));
    c.setFullscreen(false, 10050);
    ASSERT_EQ(none, lifecycle(c.onWindowState(false, true, 10100, 2000)));
    ASSERT_EQ(none, lifecycle(c.onWindowState(false, false, 10150, 2000)));

    // A pending entry (never seen fullscreen) waits out its grace, then stops.
    c.setFullscreen(true, 20000);
    ASSERT_EQ(none, lifecycle(c.onWindowState(true, false, 21999, 2000)));
    ASSERT_EQ(stop, lifecycle(c.onWindowState(true, false, 22000, 2000)));
}

// P14 (r6, adversary pass 9 A1): the Windows hook failed to install (SetWindowsHookExW returned NULL), so input an
// excluded wait defers has no witness. The pass-9 input: fullscreen at (500,500), the tick at 2000 would hide; with
// the witness missing it never hides, and a cursor already hidden is shown at the next tick. Control: the
// e5701666 tick (no witnessMissing gate) hides at 2000 and then only rearms while hidden through the wait.
TEST(ViewerIdlePolicy, MissingRetrievalWitnessNeverHidesAndShowsAHiddenCursor)
{
    viewer_idle::Inputs missing = allPass();
    missing.witnessMissing = true;
    ASSERT_FALSE(viewer_idle::mayHide(missing));
    ASSERT_TRUE(viewer_idle::mayHide(allPass()));
    int rearm = 0;

    // Control: no gate. Hidden at 2000; the deferred away-and-back leaves every witness empty: hidden at 2050.
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm, 50)));
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(allPass(), 2050, 2000, &rearm, 50)));
        ASSERT_TRUE(c.hidden());
    }
    // The gate: never hidden while the witness is missing.
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(missing, 2000, 2000, &rearm, 50)));
        ASSERT_FALSE(c.hidden());
        ASSERT_EQ(tickValue(viewer_idle::Tick::Rearm), tickValue(c.onTimeout(missing, 9000, 2000, &rearm, 50)));
        ASSERT_FALSE(c.hidden());
        ASSERT_TRUE(rearm <= 50);
    }
    // Hidden, then the hook goes missing: the next tick shows.
    {
        viewer_idle::Controller c;
        c.setFullscreen(true, 0);
        ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(c.onTimeout(allPass(), 2000, 2000, &rearm, 50)));
        ASSERT_EQ(tickValue(viewer_idle::Tick::Show), tickValue(c.onTimeout(missing, 2050, 2000, &rearm, 50)));
        ASSERT_FALSE(c.hidden());
    }
}
