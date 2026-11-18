// PLAYER-DOUBLECLICK-FULLSCREEN-1: the viewer double-click decision (ViewerDoubleClickPolicy.h).
// P1 the toggle rule, P2 the first-press gate (gradient paint is one-shot), P3 the display-surface
// identity test, P4 the persisted setting's key and default.
#include "../common/minitest.h"

#include "../../platform/qt/ViewerDoubleClickPolicy.h"

#include <QObject>

#include <string>

using viewer_dblclick::Gate;
using viewer_dblclick::ViewerState;

namespace
{

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
    gate.notePress(/*armedNow*/true, /*createdDoubleClick*/false, 1000, 400);
    ASSERT_TRUE(gate.armedAtFirstPress());
    ASSERT_FALSE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2b_ALaterUnarmedSequenceToggles)
{
    Gate gate;
    gate.notePress(true, false, 1000, 400);
    gate.notePress(false, false, 5000, 400);
    ASSERT_FALSE(gate.armedAtFirstPress());
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2c_SecondPressKeepsTheArmedRecord)
{
    Gate gate;
    gate.notePress(true, false, 1000, 400);
    // The press Qt flagged as the one that made the double-click.
    gate.notePress(false, /*createdDoubleClick*/true, 1100, 400);
    ASSERT_TRUE(gate.armedAtFirstPress());
    // An unflagged second press inside the interval.
    gate.notePress(false, false, 1200, 400);
    ASSERT_TRUE(gate.armedAtFirstPress());
    ASSERT_FALSE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2d_PressOutsideTheIntervalResets)
{
    Gate gate;
    gate.notePress(true, false, 1000, 400);
    gate.notePress(false, false, 1500, 400);
    ASSERT_FALSE(gate.armedAtFirstPress());
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2e_TimestampWrapCountsAsOutside)
{
    Gate gate;
    gate.notePress(true, false, 1000, 400);
    gate.notePress(false, false, 500, 400); // went backwards: never treated as inside the window
    ASSERT_FALSE(gate.armedAtFirstPress());
    // And an unsigned wrap from near the top of quint64.
    Gate high;
    high.notePress(true, false, ~quint64(0) - 10, 400);
    high.notePress(false, false, 5, 400);
    ASSERT_FALSE(high.armedAtFirstPress());
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
