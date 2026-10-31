// PLAYBACK-HFR-CONFORM-DEFAULT-1 (re-land of #188): the pure conform decision, its saved-settings
// sanitizing, the conditional Auto-target cap and the status text. (The #188 early-credit debt is gone:
// master's PlaybackNativePaceGuard.h is the only pacing.)
// Header-only policy (no GUI), so these run in console_tests directly.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackConformPolicy.h"

#include <QDir>
#include <QFile>
#include <QSettings>
#include <QStringList>
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

// MERGE-MASTER r4 (adversary ADV-A2, hub ruling): a stored Playback/ConformEnabled that is not a valid bool (an empty or
// non-bool string, a list, a number other than 0/1, an @Invalid() value) is MISSING and takes the documented default (on);
// a valid stored false stays false. Before r4, QVariant::toBool turned the empty INI value into false and silently
// disabled conform (a 59.94 clip played at 59.94 instead of the default 24).
TEST(PlaybackConformPolicy, InvalidStoredEnabledIsMissingAndTakesTheDefault)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    int n = 0;
    auto enabledFromIni = [&](const QByteArray &body) {
        const QString path = QDir(dir.path()).filePath(QStringLiteral("enabled%1.ini").arg(n++));
        QFile file(path);
        if (!file.open(QIODevice::WriteOnly)) return !kDefaultEnabled; // fails the default checks below, never passes silently
        file.write(body);
        file.close();
        QSettings set(path, QSettings::IniFormat);
        return loadSettings(set).enabled;
    };
    // The adversary's input: INI [Playback] followed by "ConformEnabled=" (empty), target and threshold absent.
    ASSERT_EQ(kDefaultEnabled, enabledFromIni("[Playback]\nConformEnabled=\n"));
    ASSERT_EQ(kDefaultEnabled, enabledFromIni("[Playback]\nConformEnabled=   \n"));
    ASSERT_EQ(kDefaultEnabled, enabledFromIni("[Playback]\nConformEnabled=@Invalid()\n"));
    ASSERT_EQ(kDefaultEnabled, enabledFromIni("[Playback]\nConformEnabled=\"true, false\"\n"));
    ASSERT_EQ(kDefaultEnabled, enabledFromIni("[Playback]\nConformEnabled=true, false\n")); // a QStringList
    ASSERT_EQ(kDefaultEnabled, enabledFromIni("[Playback]\nConformEnabled=maybe\n"));
    ASSERT_EQ(kDefaultEnabled, enabledFromIni("[Playback]\nConformEnabled=2\n"));
    {
        QSettings empty(QDir(dir.path()).filePath(QStringLiteral("empty.ini")), QSettings::IniFormat);
        const Settings s = loadSettings(empty);
        ASSERT_EQ(kDefaultEnabled, s.enabled); // a missing key: the default
        ASSERT_EQ(24.0, playbackFps(59.94, s.enabled, s.targetFps, s.thresholdFps));
    }
    // A VALID stored value is honoured, false included.
    ASSERT_FALSE(enabledFromIni("[Playback]\nConformEnabled=false\n"));
    ASSERT_FALSE(enabledFromIni("[Playback]\nConformEnabled=FALSE\n"));
    ASSERT_FALSE(enabledFromIni("[Playback]\nConformEnabled=0\n"));
    ASSERT_TRUE(enabledFromIni("[Playback]\nConformEnabled=true\n"));
    ASSERT_TRUE(enabledFromIni("[Playback]\nConformEnabled=1\n"));
    // Typed values (a non-INI store, e.g. the registry): a bool or 0/1 is valid, any other type or number is missing.
    QSettings typed(QDir(dir.path()).filePath(QStringLiteral("typed.ini")), QSettings::IniFormat);
    typed.setValue(kKeyEnabled(), false);
    ASSERT_FALSE(loadSettings(typed).enabled);
    typed.setValue(kKeyEnabled(), 0);
    ASSERT_FALSE(loadSettings(typed).enabled);
    typed.setValue(kKeyEnabled(), 1);
    ASSERT_TRUE(loadSettings(typed).enabled);
    typed.setValue(kKeyEnabled(), QStringList{ QStringLiteral("false") });
    ASSERT_EQ(kDefaultEnabled, loadSettings(typed).enabled);
    typed.setValue(kKeyEnabled(), 0.0);
    ASSERT_EQ(kDefaultEnabled, loadSettings(typed).enabled);
    typed.setValue(kKeyEnabled(), 7);
    ASSERT_EQ(kDefaultEnabled, loadSettings(typed).enabled);
    // A round trip of the saved false still reads back false (saveSettings writes a real bool).
    Settings off;
    off.enabled = false;
    saveSettings(typed, off);
    typed.sync();
    QSettings reread(QDir(dir.path()).filePath(QStringLiteral("typed.ini")), QSettings::IniFormat);
    ASSERT_FALSE(loadSettings(reread).enabled);
}

TEST(PlaybackConformPolicy, SettingsKeysAreNewAndDoNotReuseAutoTargetFps)
{
    ASSERT_EQ(std::string("Playback/ConformEnabled"), std::string(kKeyEnabled()));
    ASSERT_EQ(std::string("Playback/ConformTargetFps"), std::string(kKeyTargetFps()));
    ASSERT_EQ(std::string("Playback/ConformThresholdFps"), std::string(kKeyThresholdFps()));
    ASSERT_TRUE(std::string(kKeyTargetFps()) != std::string("Playback/AutoTargetFps"));
}

// While conform is ACTIVE the effective Auto target = min(autoTarget, playbackFps).
TEST(PlaybackConformPolicy, AutoTargetIsCappedByPlaybackRateWhileConforming)
{
    ASSERT_EQ(24, effectiveAutoTargetFps(30, 24.0, kOn));   // conformed 60 -> 24: 30 would over-spend
    ASSERT_EQ(24, effectiveAutoTargetFps(60, 24.0, kOn));
    ASSERT_EQ(25, effectiveAutoTargetFps(30, 25.0, kOn));
    ASSERT_EQ(30, effectiveAutoTargetFps(30, 30.0, kOn));
    ASSERT_EQ(24, effectiveAutoTargetFps(24, 25.0, kOn));   // never raises the user's target
}

// Mutation M3 (cap applied unconditionally): a clip that is NOT conformed keeps the user's Auto target.
// The 23.976 perf-goal clip (M16-1243) keeps Auto's 30 fps budget (33 ms), not a 24 fps one (41.7 ms).
TEST(PlaybackConformPolicy, AutoTargetIsUntouchedWhenConformIsInactive)
{
    ASSERT_EQ(30, effectiveAutoTargetFps(30, 23.976, kOff));
    ASSERT_EQ(30, effectiveAutoTargetFps(30, defaultPlayback(23.976), kOff));
    ASSERT_EQ(30, effectiveAutoTargetFps(30, 25.0, kOff));
    ASSERT_EQ(60, effectiveAutoTargetFps(60, 29.97, kOff));
    ASSERT_EQ(24, effectiveAutoTargetFps(24, 23.976, kOff));
    // the conform decision for that clip really is "inactive" under the defaults
    ASSERT_FALSE(conformApplies(23.976, kOn, kTarget, kThreshold));
    ASSERT_FALSE(conformApplies(30.0, kOn, kTarget, kThreshold));
}

TEST(PlaybackConformPolicy, AutoTargetLeavesUnusableInputsAlone)
{
    ASSERT_EQ(30, effectiveAutoTargetFps(30, 0.0, kOn));
    ASSERT_EQ(30, effectiveAutoTargetFps(30, std::numeric_limits<double>::quiet_NaN(), kOn));
    ASSERT_EQ(0, effectiveAutoTargetFps(0, 24.0, kOn));
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

// MERGE-MASTER r5 (AUDIO-A1, Dot 6542640078): a conform control change before the first present must not cancel the pending
// audio start of a clip it leaves non-conformed. Each case runs the trace end to end on the pure functions: Play raised the
// pending start (non-conformed), the control changes the settings, the decision is applied, and the gated consumer
// (audioSyncAllowed(conformActive), MainWindow.cpp's syncAndPlay gate) either starts the audio or not.
namespace
{
struct ConformControls
{
    bool enabled;
    double target;
    double threshold;
};

struct PendingTrace
{
    PendingAudioStart decision;
    bool pendingAfter;  // m_tryToSyncAudio after applyPlaybackConformSettings (play, audio output and drop frame are on)
    bool audioStarts;   // the first present's gated consumer starts the audio
};

PendingTrace runControlChange( double clipFps, const ConformControls & before, const ConformControls & after )
{
    const bool wasActive = conformApplies( clipFps, before.enabled, before.target, before.threshold );
    const double wasFps = playbackFps( clipFps, before.enabled, before.target, before.threshold );
    bool pending = audioSyncAllowed( wasActive ); // Play: requestPlaybackAudioSync() raises it only when not conformed
    const bool nowActive = conformApplies( clipFps, after.enabled, after.target, after.threshold );
    const double nowFps = playbackFps( clipFps, after.enabled, after.target, after.threshold );
    const PendingAudioStart decision = pendingAudioStartOnConformChange( wasActive, wasFps, nowActive, nowFps );
    if( decision == PendingAudioStart::Clear ) pending = false;
    if( decision == PendingAudioStart::Rearm ) pending = audioSyncAllowed( nowActive );
    return { decision, pending, pending && audioSyncAllowed( nowActive ) };
}

constexpr double kClip23976 = 24000.0 / 1001.0;
constexpr double kClip5994 = 60000.0 / 1001.0;
} // namespace

TEST(PlaybackConformPendingAudio, NonConformedThresholdChangeKeepsThePendingStart)
{
    // Dot's trace: 23.976 clip, threshold 30 -> 50 (and back) before the first present; the clip stays non-conformed.
    const PendingTrace up = runControlChange( kClip23976, { kOn, 24.0, 30.0 }, { kOn, 24.0, 50.0 } );
    ASSERT_TRUE(up.decision == PendingAudioStart::Keep);
    ASSERT_TRUE(up.pendingAfter);
    ASSERT_TRUE(up.audioStarts);
    const PendingTrace down = runControlChange( kClip23976, { kOn, 24.0, 50.0 }, { kOn, 24.0, 30.0 } );
    ASSERT_TRUE(down.decision == PendingAudioStart::Keep);
    ASSERT_TRUE(down.audioStarts);
}

TEST(PlaybackConformPendingAudio, NonConformedTargetChangeKeepsThePendingStart)
{
    for( const double target : { 24.0, 25.0, 30.0 } )
    {
        const PendingTrace t = runControlChange( kClip23976, { kOn, 24.0, 30.0 }, { kOn, target, 30.0 } );
        ASSERT_TRUE(t.decision == PendingAudioStart::Keep);
        ASSERT_TRUE(t.audioStarts);
    }
}

TEST(PlaybackConformPendingAudio, NonConformedToggleKeepsThePendingStart)
{
    const PendingTrace off = runControlChange( kClip23976, { kOn, 24.0, 30.0 }, { kOff, 24.0, 30.0 } );
    ASSERT_TRUE(off.decision == PendingAudioStart::Keep);
    ASSERT_TRUE(off.audioStarts);
    const PendingTrace on = runControlChange( kClip23976, { kOff, 24.0, 30.0 }, { kOn, 24.0, 30.0 } );
    ASSERT_TRUE(on.decision == PendingAudioStart::Keep);
    ASSERT_TRUE(on.audioStarts);
}

TEST(PlaybackConformPendingAudio, BecomingConformedClearsThePendingStart)
{
    // toggle on for a 59.94 clip
    const PendingTrace toggle = runControlChange( kClip5994, { kOff, 24.0, 30.0 }, { kOn, 24.0, 30.0 } );
    ASSERT_TRUE(toggle.decision == PendingAudioStart::Clear);
    ASSERT_FALSE(toggle.pendingAfter);
    ASSERT_FALSE(toggle.audioStarts);
    // threshold 50 -> 30 conforms a 48 fps clip
    const PendingTrace threshold = runControlChange( 48.0, { kOn, 24.0, 50.0 }, { kOn, 24.0, 30.0 } );
    ASSERT_TRUE(threshold.decision == PendingAudioStart::Clear);
    ASSERT_FALSE(threshold.audioStarts);
}

TEST(PlaybackConformPendingAudio, StayingConformedAtAnotherTargetKeepsAudioMuted)
{
    const PendingTrace t = runControlChange( kClip5994, { kOn, 24.0, 30.0 }, { kOn, 30.0, 30.0 } );
    ASSERT_TRUE(t.decision == PendingAudioStart::Clear);
    ASSERT_FALSE(t.pendingAfter);
    ASSERT_FALSE(t.audioStarts);
}

TEST(PlaybackConformPendingAudio, LeavingConformRearmsTheStart)
{
    // toggle off for a 59.94 clip: Play never raised the start (muted); the clip now plays at its native rate with audio
    const PendingTrace toggle = runControlChange( kClip5994, { kOn, 24.0, 30.0 }, { kOff, 24.0, 30.0 } );
    ASSERT_TRUE(toggle.decision == PendingAudioStart::Rearm);
    ASSERT_TRUE(toggle.pendingAfter);
    ASSERT_TRUE(toggle.audioStarts);
    // threshold 30 -> 50 un-conforms a 48 fps clip
    const PendingTrace threshold = runControlChange( 48.0, { kOn, 24.0, 30.0 }, { kOn, 24.0, 50.0 } );
    ASSERT_TRUE(threshold.decision == PendingAudioStart::Rearm);
    ASSERT_TRUE(threshold.audioStarts);
    // the menu's targets (24/25/30) never reach a clip above the lowest threshold (30), so a target change alone keeps a
    // conformed clip conformed and its audio muted (31 fps clip, target 24 -> 30)
    const PendingTrace target = runControlChange( 31.0, { kOn, 24.0, 30.0 }, { kOn, 30.0, 30.0 } );
    ASSERT_TRUE(target.decision == PendingAudioStart::Clear);
    ASSERT_FALSE(target.audioStarts);
}

TEST(PlaybackConformPendingAudio, UnchangedOrMovedRatesWithoutConform)
{
    ASSERT_TRUE(pendingAudioStartOnConformChange( false, 23.976, false, 23.976 ) == PendingAudioStart::Keep);
    // a rate move with conform inactive on both sides re-anchors the audio
    ASSERT_TRUE(pendingAudioStartOnConformChange( false, 23.976, false, 25.0 ) == PendingAudioStart::Rearm);
    // conformed now always clears, whatever the rates
    ASSERT_TRUE(pendingAudioStartOnConformChange( true, 24.0, true, 24.0 ) == PendingAudioStart::Clear);
    ASSERT_TRUE(pendingAudioStartOnConformChange( false, 23.976, true, 24.0 ) == PendingAudioStart::Clear);
}

TEST(PlaybackConformPendingAudio, NonFiniteRatesNeverKeepSilently)
{
    const double nan = std::numeric_limits<double>::quiet_NaN();
    const double inf = std::numeric_limits<double>::infinity();
    ASSERT_TRUE(pendingAudioStartOnConformChange( false, nan, false, nan ) == PendingAudioStart::Rearm);
    ASSERT_TRUE(pendingAudioStartOnConformChange( false, 23.976, false, inf ) == PendingAudioStart::Rearm);
    ASSERT_TRUE(pendingAudioStartOnConformChange( false, -inf, false, 23.976 ) == PendingAudioStart::Rearm);
    ASSERT_TRUE(pendingAudioStartOnConformChange( false, nan, true, nan ) == PendingAudioStart::Clear);
}
