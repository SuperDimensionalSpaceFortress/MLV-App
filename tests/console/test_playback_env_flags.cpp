// PLAYBACK-STARTUP-WAIT-PREWARM-1 r2 (both keys, catalogue P-01): MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY and
// MLVAPP_PLAYBACK_ROUTE_PREWARM take an exact value set (PlaybackEnvFlags.h). r1 disabled the prewarm only on "0" (so
// "off" kept it on) and enabled the legacy audio path on anything not in the off set ("1garbage", "trueish").
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackEnvFlags.h"

using playback_env_flag::Value;
using playback_env_flag::parse;
using playback_env_flag::resolve;

TEST(PlaybackEnvFlags, TheOnAndOffSetsAreExactTrimmedAndCaseInsensitive)
{
    for (const char *on : { "1", "true", "on", "yes", "TRUE", " On ", "\tyes\n" })
        ASSERT_TRUE(parse(on) == Value::On);
    for (const char *off : { "0", "false", "off", "no", "OFF", " No ", "False\r\n" })
        ASSERT_TRUE(parse(off) == Value::Off);
}

TEST(PlaybackEnvFlags, UnsetOrEmptyKeepsTheDefault)
{
    ASSERT_TRUE(parse(nullptr) == Value::Unset);
    ASSERT_TRUE(parse("") == Value::Unset);
    ASSERT_TRUE(parse("   ") == Value::Unset);
    ASSERT_TRUE(resolve(Value::Unset, true));
    ASSERT_FALSE(resolve(Value::Unset, false));
}

TEST(PlaybackEnvFlags, AnythingElseIsInvalidAndKeepsTheDefaultEitherWay)
{
    for (const char *bad : { "1garbage", "trueish", "2", "enable", "o", "yes please", "0x1", "-1" })
    {
        ASSERT_TRUE(parse(bad) == Value::Invalid);
        ASSERT_FALSE(resolve(parse(bad), false)); // the legacy audio switch stays off
        ASSERT_TRUE(resolve(parse(bad), true));   // the prewarm stays on
    }
}

TEST(PlaybackEnvFlags, BothSwitchesResolveTheirDocumentedValues)
{
    // MLVAPP_PLAYBACK_ROUTE_PREWARM (default on): off/false/no now disable it too, not only "0"
    for (const char *off : { "0", "off", "false", "no" }) ASSERT_FALSE(resolve(parse(off), true));
    ASSERT_TRUE(resolve(parse("1"), true));
    // MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY (default off)
    for (const char *on : { "1", "true", "on", "yes" }) ASSERT_TRUE(resolve(parse(on), false));
    ASSERT_FALSE(resolve(parse("0"), false));
}
