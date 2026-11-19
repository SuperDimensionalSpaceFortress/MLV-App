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

// P2: the gate follows Qt's pairing. Widget path: Qt hands the widget the first press, then only the
// double-click (never the second press). Window (GL) path: the second press arrives flagged
// Qt::MouseEventCreatedDoubleClick, then the double-click.

TEST(ViewerDoubleClickPolicy, P2a_ArmedFirstPressSuppressesTheDoubleClick)
{
    // Gradient paint: armed at the first press, switched itself off on that click's release.
    Gate gate;
    gate.notePress(/*armedNow*/true, /*createdDoubleClick*/false);
    ASSERT_TRUE(gate.armedAtFirstPress());
    ASSERT_FALSE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2b_ALaterUnflaggedPressStartsANewSequence)
{
    // Any press Qt did not flag is the first press of whatever comes next.
    Gate gate;
    gate.notePress(true, false);
    gate.notePress(false, false);
    ASSERT_FALSE(gate.armedAtFirstPress());
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2c_FlaggedSecondPressKeepsTheArmedRecord)
{
    // GL path: the flagged second press is skipped, even though the tool is off by then.
    Gate gate;
    gate.notePress(true, false);
    gate.notePress(false, /*createdDoubleClick*/true);
    ASSERT_TRUE(gate.armedAtFirstPress());
    ASSERT_FALSE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2d_FlaggedPressKeepsAnUnarmedRecordToo)
{
    // A flagged press with a tool armed at it does not re-arm the record: it is not a first press
    // (the double-click still checks the tools armed now, through shouldToggle).
    Gate gate;
    gate.notePress(false, false);
    gate.notePress(true, true);
    ASSERT_FALSE(gate.armedAtFirstPress());
}

TEST(ViewerDoubleClickPolicy, P2e_A1r1_ArmedPressElsewhereThenANewPairToggles)
{
    // Adversary r1 pass 1 A1: handle press at (100,100), then an unarmed press far away (Qt does
    // not pair them, so it is unflagged), then its flagged second press and the double-click.
    Gate gate;
    gate.notePress(true, false);   // (100,100) t=1000
    gate.notePress(false, false);  // (500,500) t=1100, not paired by Qt
    gate.notePress(false, true);   // (500,500) t=1200, Qt's second press
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2f_A1A2r1p2_MovementOrAFullIntervalStartsANewSequence)
{
    // Adversary r1 pass 2 A1 (cursor moved away and back, or a middle press, between the presses)
    // and A2 (the unarmed press exactly one interval after the armed one). Qt resets pairing in
    // each case, so the unarmed press is unflagged and is the first press of the next pair.
    Gate gate;
    gate.notePress(true, false);   // WB armed, t=1000
    gate.notePress(false, false);  // t=1100 after movement, or t=1400 (delta == interval)
    // Widget path: the double-click follows with no further press.
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2g_A3r1p2_WrappedNativeTimestampsCannotLoseTheArmedRecord)
{
    // Adversary r1 pass 2 A3: GL path, WB-armed first press at native 4294967200, Qt pairs the
    // second press at 100 after a 32-bit wrap and flags it. The gate measures no timestamp.
    Gate gate;
    gate.notePress(true, false);
    gate.notePress(false, true);
    ASSERT_TRUE(gate.armedAtFirstPress());
    ASSERT_FALSE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2h_ACompletedSuppressedPairDoesNotPoisonTheNextPair)
{
    // Adversary r1 pass 2 A1 variant: an armed pair completes (suppressed), then a new unarmed pair.
    Gate gate;
    gate.notePress(true, false);
    gate.notePress(false, true);
    ASSERT_FALSE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
    gate.notePress(false, false);
    gate.notePress(false, true);
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2i_A2r1p3_AnUnflaggedPressElsewhereStartsAnUnarmedRecord)
{
    // Adversary r1 pass 3 A2: WB-armed viewer press at t=1000; WB off; Qt's first press of the
    // new pair lands in the layout gap outside the viewer at t=10000 (unflagged); the second press
    // on the viewer is flagged. The double-click must toggle.
    // The gap press reaches the top-level QWidgetWindow (a QWindow) first.
    Gate gate;
    gate.notePress(true, false);
    gate.noteWindowPress(false);
    gate.notePress(false, true);
    ASSERT_FALSE(gate.armedAtFirstPress());
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P2j_A2r1p3_TheSamePressReachingTheWindowFirstKeepsTheViewerRecord)
{
    // One physical press reaches the top-level QWidgetWindow first, then the viewport (the
    // QGraphicsView parent's propagated copy is a widget delivery, never counted). An armed viewer
    // press stays armed.
    {
        Gate gate;
        gate.noteWindowPress(false); // the top-level QWidgetWindow sees it first
        gate.notePress(true, false); // then the viewport
        ASSERT_TRUE(gate.armedAtFirstPress());
        ASSERT_FALSE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
    }
    {
        // A flagged second press at the window never resets the record either.
        Gate gate;
        gate.notePress(true, false);
        gate.noteWindowPress(true);
        ASSERT_TRUE(gate.armedAtFirstPress());
    }
}

TEST(ViewerDoubleClickPolicy, P2k_A2r2p4_ADistinctPressElsewhereReusingTheTimestampStillResets)
{
    // r2 pass 4 A2: armed viewer press at 1000; a distinct gap press reuses timestamp 1000 (native
    // injection). The gate reads no timestamp at all, so the window press resets the record.
    Gate gate;
    gate.notePress(true, false);
    gate.noteWindowPress(false);
    gate.notePress(false, true);
    ASSERT_FALSE(gate.armedAtFirstPress());
    ASSERT_TRUE(gate.decide(Qt::LeftButton, Qt::NoModifier, readyState()));
}

TEST(ViewerDoubleClickPolicy, P5_A1r1p3_GradientHandleClaimsOnlyOnTheDisplayedView)
{
    ASSERT_TRUE(viewer_dblclick::gradientHandleClaims(true, true, true));
    // GL path: the QGraphicsView is hidden; its scene item still reads visible and under the mouse.
    ASSERT_FALSE(viewer_dblclick::gradientHandleClaims(false, true, true));
    ASSERT_FALSE(viewer_dblclick::gradientHandleClaims(true, false, true));
    ASSERT_FALSE(viewer_dblclick::gradientHandleClaims(true, true, false));
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
