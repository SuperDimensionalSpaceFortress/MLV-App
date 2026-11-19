// PLAYER-CURSOR-AUTOHIDE-1: the fullscreen viewer idle clock (ViewerIdlePolicy.h), driven with a
// fake clock. The cursor surfaces and the live timer are covered by gui_tests (test_gui_smoke.cpp).
#include "../common/minitest.h"

#include "../../platform/qt/ViewerIdlePolicy.h"

#include <QEvent>
#include <QPoint>

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
    ASSERT_TRUE(c.hidden());
    ASSERT_EQ(tickValue(viewer_idle::Tick::None), tickValue(c.onTimeout(allPass(), 2001, 2000, &rearm)));

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

    viewer_idle::Controller f;
    f.setFullscreen(true, 0);
    ASSERT_EQ(tickValue(viewer_idle::Tick::Hide), tickValue(f.onTimeout(allPass(), 2000, 2000, &rearm)));
    ASSERT_TRUE(f.forceShown());
    ASSERT_FALSE(f.hidden());
    ASSERT_FALSE(f.forceShown());
}

// P4
TEST(ViewerIdlePolicy, IdleHideIsTwoSeconds)
{
    ASSERT_EQ(2000, viewer_idle::kIdleHideMs());
}
