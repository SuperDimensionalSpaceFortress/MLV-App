// PLAYER-DOUBLECLICK-FULLSCREEN-1: the viewer double-click decision (ViewerDoubleClickPolicy.h).
// P1 the toggle rule, P2 the first-press gate (gradient paint is one-shot), P3 the display-surface
// identity test, P4 the persisted setting's key and default.
#include "../common/minitest.h"

#include "../../platform/qt/ViewerDoubleClickPolicy.h"

#include <QObject>
#include <QPointF>

#include <string>

using viewer_dblclick::Gate;
using viewer_dblclick::ViewerState;

namespace
{

const QPointF kAt(200, 200);
constexpr int kDistance = 4;

ViewerState readyState()
{
    ViewerState s;
    s.enabled = true;
    s.clipLoaded = true;
    return s;
}

bool toggles( Qt::MouseButton b, Qt::KeyboardModifiers m, const ViewerState &s, bool armedAtFirstPress = false )
{
    return viewer_dblclick::shouldToggle( b, m, s, armedAtFirstPress );
}

} // namespace

TEST(ViewerDoubleClickPolicy, P1_AllConditionsMetToggles)
{
    ASSERT_TRUE(toggles(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P1_OtherButtonsDoNotToggle)
{
    ASSERT_FALSE(toggles(Qt::RightButton, Qt::NoModifier, readyState()));
    ASSERT_FALSE(toggles(Qt::MiddleButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P1_AnyModifierDoesNotToggle)
{
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::ShiftModifier, readyState()));
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::ControlModifier, readyState()));
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::AltModifier, readyState()));
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::MetaModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P1_SettingOffOrNoClipDoesNotToggle)
{
    ViewerState off = readyState();
    off.enabled = false;
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::NoModifier, off));
    ViewerState noClip = readyState();
    noClip.clipLoaded = false;
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::NoModifier, noClip));
}

TEST(ViewerDoubleClickPolicy, P1_EachArmedToolDoesNotToggle)
{
    ViewerState wb = readyState();
    wb.wbArmed = true;
    ASSERT_TRUE(viewer_dblclick::anyToolArmed(wb));
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::NoModifier, wb));

    ViewerState bp = readyState();
    bp.bpArmed = true;
    ASSERT_TRUE(viewer_dblclick::anyToolArmed(bp));
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::NoModifier, bp));

    ViewerState gp = readyState();
    gp.gradientPaintArmed = true;
    ASSERT_TRUE(viewer_dblclick::anyToolArmed(gp));
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::NoModifier, gp));

    ViewerState gh = readyState();
    gh.gradientHandleHovered = true;
    ASSERT_TRUE(viewer_dblclick::anyToolArmed(gh));
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::NoModifier, gh));

    ASSERT_FALSE(viewer_dblclick::anyToolArmed(readyState()));
}

TEST(ViewerDoubleClickPolicy, P1_ArmedAtFirstPressDoesNotToggle)
{
    ASSERT_FALSE(toggles(Qt::LeftButton, Qt::NoModifier, readyState(), /*armedAtFirstPress*/true));
}

TEST(ViewerDoubleClickPolicy, P2a_ArmedFirstPressSuppressesTheDoubleClick)
{
    // Gradient paint: armed at the first press, switched itself off on that click's release.
    Gate gate;
    gate.notePress(/*armedNow*/true, 1000, 400, kAt, kDistance);
    ASSERT_TRUE(gate.armedAtFirstPress());
    ASSERT_FALSE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2b_ALaterUnarmedSequenceToggles)
{
    Gate gate;
    gate.notePress(true, 1000, 400, kAt, kDistance);
    gate.notePress(false, 5000, 400, kAt, kDistance);
    ASSERT_FALSE(gate.armedAtFirstPress());
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2c_SecondPressKeepsTheArmedRecord)
{
    Gate gate;
    gate.notePress(true, 1000, 400, kAt, kDistance);
    // The second press of the double-click: inside the interval and the distance.
    gate.notePress(false, 1100, 400, kAt + QPointF(kDistance, -kDistance), kDistance);
    ASSERT_TRUE(gate.armedAtFirstPress());
    // A third press still inside the window of the recorded first press.
    gate.notePress(false, 1200, 400, kAt, kDistance);
    ASSERT_TRUE(gate.armedAtFirstPress());
    ASSERT_FALSE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2d_PressOutsideTheIntervalResets)
{
    Gate gate;
    gate.notePress(true, 1000, 400, kAt, kDistance);
    gate.notePress(false, 1500, 400, kAt, kDistance);
    ASSERT_FALSE(gate.armedAtFirstPress());
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2e_TimestampWrapCountsAsOutside)
{
    Gate gate;
    gate.notePress(true, 1000, 400, kAt, kDistance);
    gate.notePress(false, 500, 400, kAt, kDistance); // went backwards: never treated as inside the window
    ASSERT_FALSE(gate.armedAtFirstPress());
    // And an unsigned wrap from near the top of quint64.
    Gate high;
    high.notePress(true, ~quint64(0) - 10, 400, kAt, kDistance);
    high.notePress(false, 5, 400, kAt, kDistance);
    ASSERT_FALSE(high.armedAtFirstPress());
}

TEST(ViewerDoubleClickPolicy, P2f_ArmedPressElsewhereDoesNotPoisonANewSequence)
{
    // Adversary A1 (r1, dd9f5441), its input verbatim: interval 400 ms, distance 4 px. A press on
    // the gradient handle at (100,100) t=1000; a press with nothing armed at (500,500) t=1100; the
    // double-click there. The far press starts a new sequence, so the double-click toggles.
    Gate gate;
    gate.notePress(true, 1000, 400, QPointF(100, 100), 4);
    gate.notePress(false, 1100, 400, QPointF(500, 500), 4);
    ASSERT_FALSE(gate.armedAtFirstPress());
    // The second press of that double-click (t=1200, same place) pairs with the unarmed record.
    gate.notePress(false, 1200, 400, QPointF(500, 500), 4);
    ASSERT_FALSE(gate.armedAtFirstPress());
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2g_DistanceIsCheckedOnEachAxisAtTheBoundary)
{
    // Exactly the distance away on an axis still pairs; one pixel more does not (Qt's rule).
    Gate onEdge;
    onEdge.notePress(true, 1000, 400, QPointF(100, 100), 4);
    onEdge.notePress(false, 1100, 400, QPointF(104, 96), 4);
    ASSERT_TRUE(onEdge.armedAtFirstPress());

    Gate pastX;
    pastX.notePress(true, 1000, 400, QPointF(100, 100), 4);
    pastX.notePress(false, 1100, 400, QPointF(105, 100), 4);
    ASSERT_FALSE(pastX.armedAtFirstPress());

    Gate pastY;
    pastY.notePress(true, 1000, 400, QPointF(100, 100), 4);
    pastY.notePress(false, 1100, 400, QPointF(100, 95), 4);
    ASSERT_FALSE(pastY.armedAtFirstPress());

    // A negative distance pairs only the same point.
    Gate negative;
    negative.notePress(true, 1000, 400, QPointF(100, 100), -1);
    negative.notePress(false, 1100, 400, QPointF(100, 100), -1);
    ASSERT_TRUE(negative.armedAtFirstPress());
    negative.notePress(false, 1150, 400, QPointF(101, 100), -1);
    ASSERT_FALSE(negative.armedAtFirstPress());
}

TEST(ViewerDoubleClickPolicy, P3_IsViewerTargetKeysOnIdentity)
{
    QObject viewport;
    QObject gl;
    QObject other;
    ASSERT_TRUE(viewer_dblclick::isViewerTarget(&viewport, &viewport, &gl));
    ASSERT_TRUE(viewer_dblclick::isViewerTarget(&gl, &viewport, &gl));
    ASSERT_FALSE(viewer_dblclick::isViewerTarget(&other, &viewport, &gl));
    ASSERT_FALSE(viewer_dblclick::isViewerTarget(nullptr, &viewport, &gl));
    // No GL window active: a null watched never matches the null GL window.
    ASSERT_FALSE(viewer_dblclick::isViewerTarget(nullptr, &viewport, nullptr));
    ASSERT_TRUE(viewer_dblclick::isViewerTarget(&viewport, &viewport, nullptr));
}

TEST(ViewerDoubleClickPolicy, P4_SettingsKeyAndDefault)
{
    ASSERT_EQ(std::string("Playback/DoubleClickViewerTogglesFullscreen"), std::string(viewer_dblclick::kSettingsKey()));
    ASSERT_TRUE(viewer_dblclick::kSettingsDefault());
}
