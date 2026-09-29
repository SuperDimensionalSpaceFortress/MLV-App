// PLAYBACK-HFR-CONFORM-DEFAULT-1 round 1: the pure conform decision, its saved-settings
// sanitizing, the Auto-target cap, the status text and the elapsed-time early-credit debt.
// Header-only policy (no GUI), so these run in console_tests directly.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackConformPolicy.h"

#include <QDir>
#include <QSettings>
#include <QTemporaryDir>

#include <cmath>
#include <cstdlib>
#include <limits>

using namespace playback_conform;

namespace
{
constexpr bool kOn = true;
constexpr bool kOff = false;
constexpr double kTarget = kDefaultTargetFps;       // 24
constexpr double kThreshold = kDefaultThresholdFps; // 30

double defaultPlayback( double clipFps )
{
    return playbackFps( clipFps, kOn, kTarget, kThreshold );
}
} // namespace

TEST(PlaybackConformPolicy, DefaultsAreEnabled24Over30)
{
    ASSERT_TRUE(kDefaultEnabled);
    ASSERT_EQ(24.0, kDefaultTargetFps);
    ASSERT_EQ(30.0, kDefaultThresholdFps);
}

// The card's unit cases: 23.976, 25, 29.97, 30, 48, 50, 59.94, 60, 120.
TEST(PlaybackConformPolicy, ClipsAtOrBelowThresholdPlayNatively)
{
    ASSERT_EQ(23.976, defaultPlayback(23.976));
    ASSERT_EQ(25.0, defaultPlayback(25.0));
    ASSERT_EQ(29.97, defaultPlayback(29.97));
    ASSERT_EQ(30.0, defaultPlayback(30.0)); // strictly above the threshold only
}

TEST(PlaybackConformPolicy, ClipsAboveThresholdConformToTarget)
{
    ASSERT_EQ(24.0, defaultPlayback(48.0));
    ASSERT_EQ(24.0, defaultPlayback(50.0));
    ASSERT_EQ(24.0, defaultPlayback(59.94));
    ASSERT_EQ(24.0, defaultPlayback(60.0));
    ASSERT_EQ(24.0, defaultPlayback(120.0));
}

TEST(PlaybackConformPolicy, DisabledPlaysNatively)
{
    ASSERT_EQ(59.94, playbackFps(59.94, kOff, kTarget, kThreshold));
    ASSERT_EQ(120.0, playbackFps(120.0, kOff, kTarget, kThreshold));
    ASSERT_FALSE(conformApplies(60.0, kOff, kTarget, kThreshold));
}

// The Export dialog's FPS override is export-only: the decision takes NO override input, so
// override on + a 23.976 clip is 23.976 (the 1.1x over-speed at a saved 25 is gone).
TEST(PlaybackConformPolicy, ExportOverrideHasNoInputAndCannotChangePlayback)
{
    // A saved override of 25 fps has no parameter to arrive through.
    ASSERT_EQ(23.976, playbackFps(23.976, kOn, kTarget, kThreshold));
    ASSERT_EQ(24.0, playbackFps(59.94, kOn, kTarget, kThreshold));
}

// Never speed a clip up: target must be strictly below the clip rate.
TEST(PlaybackConformPolicy, NeverSpeedsAClipUp)
{
    // Target 30 with a threshold under the clip: 31 fps clip conforms down to 30, but a
    // 30 fps... clip at threshold 10 with target 60 stays native.
    ASSERT_EQ(30.0, playbackFps(31.0, kOn, 30.0, 10.0));
    ASSERT_EQ(50.0, playbackFps(50.0, kOn, 60.0, 30.0));
    ASSERT_EQ(48.0, playbackFps(48.0, kOn, 48.0, 30.0)); // target == clip: no change
    ASSERT_FALSE(conformApplies(50.0, kOn, 60.0, 30.0));
}

TEST(PlaybackConformPolicy, InvalidSavedValuesFallBackToDefaults)
{
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    for (const double bad : { 0.0, -24.0, nan, inf, -inf, 1.0e9, 0.5 }) {
        ASSERT_EQ(kDefaultTargetFps, sanitizedTargetFps(bad));
        ASSERT_EQ(kDefaultThresholdFps, sanitizedThresholdFps(bad));
        // A bad target/threshold acts as the default, not as "no conform".
        ASSERT_EQ(24.0, playbackFps(60.0, kOn, bad, kThreshold));
        ASSERT_EQ(24.0, playbackFps(60.0, kOn, kTarget, bad));
    }
    ASSERT_EQ(25.0, sanitizedTargetFps(25.0));
    ASSERT_EQ(50.0, sanitizedThresholdFps(50.0));
}

TEST(PlaybackConformPolicy, UnusableClipRateIsReturnedUnchanged)
{
    ASSERT_EQ(0.0, defaultPlayback(0.0));
    ASSERT_EQ(-1.0, defaultPlayback(-1.0));
    ASSERT_FALSE(conformApplies(0.0, kOn, kTarget, kThreshold));
    ASSERT_FALSE(conformApplies(std::numeric_limits<double>::quiet_NaN(), kOn, kTarget, kThreshold));
}

TEST(PlaybackConformPolicy, SettingsRoundTripAndDefaultsWhenAbsentOrInvalid)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = QDir(dir.path()).filePath(QStringLiteral("conform.ini"));
    {
        QSettings empty(path, QSettings::IniFormat);
        const Settings s = loadSettings(empty);
        ASSERT_EQ(kDefaultEnabled, s.enabled);
        ASSERT_EQ(kDefaultTargetFps, s.targetFps);
        ASSERT_EQ(kDefaultThresholdFps, s.thresholdFps);
        Settings custom;
        custom.enabled = false;
        custom.targetFps = 25.0;
        custom.thresholdFps = 50.0;
        saveSettings(empty, custom);
        empty.sync();
    }
    {
        QSettings again(path, QSettings::IniFormat);
        const Settings s = loadSettings(again);
        ASSERT_FALSE(s.enabled);
        ASSERT_EQ(25.0, s.targetFps);
        ASSERT_EQ(50.0, s.thresholdFps);
        again.setValue(kKeyTargetFps(), QStringLiteral("not-a-number"));
        again.setValue(kKeyThresholdFps(), -3.0);
        const Settings bad = loadSettings(again);
        ASSERT_EQ(kDefaultTargetFps, bad.targetFps);
        ASSERT_EQ(kDefaultThresholdFps, bad.thresholdFps);
    }
}

TEST(PlaybackConformPolicy, SettingsKeysAreNewAndDoNotReuseAutoTargetFps)
{
    ASSERT_EQ(std::string("Playback/ConformEnabled"), std::string(kKeyEnabled()));
    ASSERT_EQ(std::string("Playback/ConformTargetFps"), std::string(kKeyTargetFps()));
    ASSERT_EQ(std::string("Playback/ConformThresholdFps"), std::string(kKeyThresholdFps()));
    ASSERT_TRUE(std::string(kKeyTargetFps()) != std::string("Playback/AutoTargetFps"));
}

// Item 6: effective Auto target = min(autoTarget, playbackFps).
TEST(PlaybackConformPolicy, AutoTargetIsCappedByPlaybackRate)
{
    ASSERT_EQ(24, effectiveAutoTargetFps(30, 24.0));   // conformed 60 -> 24: 30 would over-spend
    ASSERT_EQ(24, effectiveAutoTargetFps(60, 24.0));
    ASSERT_EQ(24, effectiveAutoTargetFps(30, 23.976)); // 23.976 rounds to 24
    ASSERT_EQ(25, effectiveAutoTargetFps(30, 25.0));
    ASSERT_EQ(30, effectiveAutoTargetFps(30, 29.97));
    ASSERT_EQ(24, effectiveAutoTargetFps(24, 60.0));   // never raises the user's target
    ASSERT_EQ(30, effectiveAutoTargetFps(30, 60.0));
}

TEST(PlaybackConformPolicy, AutoTargetLeavesUnusableInputsAlone)
{
    ASSERT_EQ(30, effectiveAutoTargetFps(30, 0.0));
    ASSERT_EQ(30, effectiveAutoTargetFps(30, std::numeric_limits<double>::quiet_NaN()));
    ASSERT_EQ(0, effectiveAutoTargetFps(0, 24.0));
}

TEST(PlaybackConformPolicy, AudioIsMutedOnlyWhileConformIsActive)
{
    ASSERT_FALSE(audioSyncAllowed(true));
    ASSERT_TRUE(audioSyncAllowed(false));
    ASSERT_EQ(std::string("audio muted (conform)"), audioMutedStatusText().toStdString());
}

// Item 7: status text.
TEST(PlaybackConformPolicy, StatusTextShowsConformSuffixOnlyWhenConforming)
{
    ASSERT_EQ(std::string("Playback: 24 fps (60 -> 24)"),
              playbackFpsStatusText(24.3, 60.0, 24.0).toStdString());
    ASSERT_EQ(std::string("Playback: 24 fps (59.94 -> 24)"),
              playbackFpsStatusText(24.0, 59.94, 24.0).toStdString());
    ASSERT_EQ(std::string("Playback: 23 fps"),
              playbackFpsStatusText(23.9, 23.976, 23.976).toStdString());
    ASSERT_EQ(std::string("Playback: 0.0 fps (60 -> 24)"),
              playbackFpsStatusText(0.0, 60.0, 24.0).toStdString());
    ASSERT_EQ(std::string("Playback: 5.5 fps"),
              playbackFpsStatusText(5.5, 0.0, 0.0).toStdString());
    ASSERT_EQ(std::string("Playback: 0.0 fps"),
              playbackFpsStatusText(-3.0, 30.0, 30.0).toStdString());
}

// Item 3: early credit is a debt, not a keepsake.
TEST(PlaybackConformPolicy, EarlyCreditStillGivesAFreshFrameAtOnce)
{
    // 24 fps period 41.67 ms; a render finished 5 ms after the last step.
    const ElapsedCredit c = applyEarlyCredit(5, 1000.0 / 24.0, 42, true, 0);
    ASSERT_EQ(42, c.creditedMs);
    ASSERT_EQ(37, c.debtMs);
}

TEST(PlaybackConformPolicy, EarlyCreditDebtIsDeductedFromFollowingSteps)
{
    const double period = 1000.0 / 24.0;
    ElapsedCredit c = applyEarlyCredit(5, period, 42, true, 0);
    ASSERT_EQ(42, c.creditedMs);
    c = applyEarlyCredit(8, period, 42, false, c.debtMs); // debt 37 -> 29, nothing credited
    ASSERT_EQ(0, c.creditedMs);
    ASSERT_EQ(29, c.debtMs);
    // No new early credit while a debt is outstanding, even for a predictive step.
    c = applyEarlyCredit(8, period, 42, true, c.debtMs);
    ASSERT_EQ(0, c.creditedMs);
    ASSERT_EQ(21, c.debtMs);
}

TEST(PlaybackConformPolicy, NonEarlyStepsPassElapsedThrough)
{
    const double period = 1000.0 / 24.0;
    ElapsedCredit c = applyEarlyCredit(8, period, 42, false, 0);
    ASSERT_EQ(8, c.creditedMs);
    ASSERT_EQ(0, c.debtMs);
    c = applyEarlyCredit(45, period, 42, true, 0); // not early: >= one period
    ASSERT_EQ(45, c.creditedMs);
    ASSERT_EQ(0, c.debtMs);
    c = applyEarlyCredit(-4, period, 42, false, -9); // clock hiccup: clamped
    ASSERT_EQ(0, c.creditedMs);
    ASSERT_EQ(0, c.debtMs);
}

// The invariant that makes the timeline speed exact: over any run of steps
// sum(credited) == sum(elapsed) + debt, and debt never exceeds one period, so the timeline
// can never run more than one frame period ahead of the wall clock.
TEST(PlaybackConformPolicy, EarlyCreditConservesElapsedTimeAndBoundsTheLead)
{
    struct Case { double fps; int stepMs; };
    const Case cases[] = { {24.0, 5}, {24.0, 8}, {59.94, 3}, {59.94, 8}, {23.976, 11}, {24.0, 41} };
    for (const Case & k : cases) {
        const double period = 1000.0 / k.fps;
        const int ceilPeriod = static_cast<int>(period + 0.999);
        int debt = 0;
        long long elapsedSum = 0;
        long long creditedSum = 0;
        // Deterministic jittered predictive/timer mix.
        unsigned seed = 12345u;
        for (int i = 0; i < 20000; ++i) {
            seed = seed * 1664525u + 1013904223u;
            const int jitter = static_cast<int>((seed >> 16) % 5) - 2;
            const int elapsed = k.stepMs + jitter < 0 ? 0 : k.stepMs + jitter;
            const bool predictive = ((seed >> 8) & 1u) != 0u;
            const ElapsedCredit c = applyEarlyCredit(elapsed, period, ceilPeriod, predictive, debt);
            debt = c.debtMs;
            elapsedSum += elapsed;
            creditedSum += c.creditedMs;
            ASSERT_TRUE(debt >= 0);
            ASSERT_TRUE(debt <= ceilPeriod);
            ASSERT_EQ(elapsedSum + debt, creditedSum);
        }
        // Timeline time never exceeds wall time by more than one period.
        ASSERT_TRUE(creditedSum <= elapsedSum + ceilPeriod);
        ASSERT_TRUE(creditedSum >= elapsedSum);
    }
}

// The old behaviour (round every early step up to a whole period) let the timeline run away
// at render speed. Demonstrate the bound against that model so the regression is explicit.
TEST(PlaybackConformPolicy, OldRoundUpModelRanAheadAndTheDebtModelDoesNot)
{
    const double period = 1000.0 / 24.0;
    const int ceilPeriod = 42;
    long long oldCredited = 0;
    long long newCredited = 0;
    long long elapsed = 0;
    int debt = 0;
    for (int i = 0; i < 1000; ++i) {
        const int step = 6; // a fast renderer finishing every 6 ms
        elapsed += step;
        oldCredited += step < period ? ceilPeriod : step; // old: round up every time
        const ElapsedCredit c = applyEarlyCredit(step, period, ceilPeriod, true, debt);
        debt = c.debtMs;
        newCredited += c.creditedMs;
    }
    ASSERT_TRUE(oldCredited > elapsed * 5);          // ~7x too fast
    ASSERT_TRUE(newCredited <= elapsed + ceilPeriod); // within one period of real time
}
