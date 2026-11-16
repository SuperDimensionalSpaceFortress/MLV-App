#include "../common/minitest.h"
#include "../../src/batch/WorkerThreadCount.h"

namespace {

class EnvGuard
{
public:
    explicit EnvGuard(const char *name)
        : m_name(name),
          m_original(qgetenv(name)),
          m_hadOriginal(!m_original.isNull())
    {}

    ~EnvGuard()
    {
        if (m_hadOriginal) {
            qputenv(m_name, m_original);
        } else {
            qunsetenv(m_name);
        }
    }

private:
    const char *m_name;
    QByteArray m_original;
    bool m_hadOriginal;
};

} // namespace

TEST(WorkerThreadCount, HonorsForcedThreadOverride)
{
    EnvGuard forceThreads("MLVAPP_FORCE_THREADS");
    EnvGuard forceSingle("MLVAPP_FORCE_SINGLETHREAD");

    qunsetenv("MLVAPP_FORCE_SINGLETHREAD");
    qputenv("MLVAPP_FORCE_THREADS", QByteArrayLiteral("1"));

    ASSERT_EQ(1, mlvappEffectiveWorkerThreadCount());
}

TEST(WorkerThreadCount, FallsBackToPositiveWorkerCount)
{
    EnvGuard forceThreads("MLVAPP_FORCE_THREADS");
    EnvGuard forceSingle("MLVAPP_FORCE_SINGLETHREAD");

    qunsetenv("MLVAPP_FORCE_THREADS");
    qunsetenv("MLVAPP_FORCE_SINGLETHREAD");

    ASSERT_TRUE(mlvappEffectiveWorkerThreadCount() > 0);
}

TEST(WorkerThreadCount, PlaybackWorkerCountHonorsForcedOverride)
{
    EnvGuard forceThreads("MLVAPP_FORCE_THREADS");
    EnvGuard forceSingle("MLVAPP_FORCE_SINGLETHREAD");
    EnvGuard playbackCap("MLVAPP_PLAYBACK_MAX_THREADS");
    EnvGuard disableCap("MLVAPP_DISABLE_PLAYBACK_THREAD_CAP");

    qunsetenv("MLVAPP_FORCE_SINGLETHREAD");
    qputenv("MLVAPP_FORCE_THREADS", QByteArrayLiteral("8"));
    qputenv("MLVAPP_PLAYBACK_MAX_THREADS", QByteArrayLiteral("4"));
    qunsetenv("MLVAPP_DISABLE_PLAYBACK_THREAD_CAP");

    ASSERT_EQ(8, mlvappEffectivePlaybackWorkerThreadCount());
}

TEST(WorkerThreadCount, PlaybackWorkerCountCapsAutoThreads)
{
    EnvGuard forceThreads("MLVAPP_FORCE_THREADS");
    EnvGuard forceSingle("MLVAPP_FORCE_SINGLETHREAD");
    EnvGuard playbackCap("MLVAPP_PLAYBACK_MAX_THREADS");
    EnvGuard disableCap("MLVAPP_DISABLE_PLAYBACK_THREAD_CAP");

    qunsetenv("MLVAPP_FORCE_THREADS");
    qunsetenv("MLVAPP_FORCE_SINGLETHREAD");
    qputenv("MLVAPP_PLAYBACK_MAX_THREADS", QByteArrayLiteral("1"));
    qunsetenv("MLVAPP_DISABLE_PLAYBACK_THREAD_CAP");

    ASSERT_EQ(1, mlvappEffectivePlaybackWorkerThreadCount());
}

TEST(WorkerThreadCount, PlaybackWorkerCountCanDisableCap)
{
    EnvGuard forceThreads("MLVAPP_FORCE_THREADS");
    EnvGuard forceSingle("MLVAPP_FORCE_SINGLETHREAD");
    EnvGuard playbackCap("MLVAPP_PLAYBACK_MAX_THREADS");
    EnvGuard disableCap("MLVAPP_DISABLE_PLAYBACK_THREAD_CAP");

    qunsetenv("MLVAPP_FORCE_THREADS");
    qunsetenv("MLVAPP_FORCE_SINGLETHREAD");
    qputenv("MLVAPP_PLAYBACK_MAX_THREADS", QByteArrayLiteral("1"));
    qputenv("MLVAPP_DISABLE_PLAYBACK_THREAD_CAP", QByteArrayLiteral("1"));

    ASSERT_EQ(mlvappEffectiveWorkerThreadCount(),
              mlvappEffectivePlaybackWorkerThreadCount());
}

TEST(WorkerThreadCount, DefaultPlaybackCapScalesToSixteenThreads)
{
    ASSERT_EQ(1, mlvappDefaultPlaybackWorkerThreadCapFor(1));
    ASSERT_EQ(6, mlvappDefaultPlaybackWorkerThreadCapFor(6));
    ASSERT_EQ(8, mlvappDefaultPlaybackWorkerThreadCapFor(8));
    ASSERT_EQ(16, mlvappDefaultPlaybackWorkerThreadCapFor(16));
    ASSERT_EQ(16, mlvappDefaultPlaybackWorkerThreadCapFor(32));
}

TEST(WorkerThreadCount, PlaybackOpenMpTargetCapsOversubscribedTeams)
{
    ASSERT_EQ(6, mlvappPlaybackOpenMpThreadTargetFor(6, 16));
}

TEST(WorkerThreadCount, PlaybackOpenMpTargetRespectsLowerUserLimit)
{
    ASSERT_EQ(4, mlvappPlaybackOpenMpThreadTargetFor(6, 4));
}

TEST(WorkerThreadCount, PlaybackOpenMpTargetFallsBackToCurrentForInvalidWorkerCount)
{
    ASSERT_EQ(3, mlvappPlaybackOpenMpThreadTargetFor(0, 3));
}

// CPU-S1-FS-SPIKE-TEAM-PARTITION-1: the recon / prep team knobs.
TEST(WorkerThreadCount, PlaybackTeamValueAcceptsOnlyPositiveDecimalIntegers)
{
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArray()));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("0")));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("000")));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("-4")));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("+4")));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral(" 4")));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("4 ")));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("4x")));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("0x8")));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("1.5")));
    ASSERT_EQ(0, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("junk")));
    ASSERT_EQ(1, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("1")));
    ASSERT_EQ(12, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("12")));
    ASSERT_EQ(6, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("006")));
    ASSERT_EQ(1000000, mlvappPlaybackTeamRequestFromValue(QByteArrayLiteral("99999999999999999999")));
}

TEST(WorkerThreadCount, PlaybackTeamKnobsUnsetAreExactlyThePlaybackCount)
{
    EnvGuard forceThreads("MLVAPP_FORCE_THREADS");
    EnvGuard forceSingle("MLVAPP_FORCE_SINGLETHREAD");
    EnvGuard recon("MLVAPP_PLAYBACK_RECON_OMP_THREADS");
    EnvGuard prep("MLVAPP_PLAYBACK_PREP_OMP_THREADS");
    qunsetenv("MLVAPP_FORCE_THREADS");
    qunsetenv("MLVAPP_FORCE_SINGLETHREAD");
    for (const char *value : { static_cast<const char *>(nullptr), "", "0", "junk", "-3" }) {
        if (value) {
            qputenv("MLVAPP_PLAYBACK_RECON_OMP_THREADS", QByteArray(value));
            qputenv("MLVAPP_PLAYBACK_PREP_OMP_THREADS", QByteArray(value));
        } else {
            qunsetenv("MLVAPP_PLAYBACK_RECON_OMP_THREADS");
            qunsetenv("MLVAPP_PLAYBACK_PREP_OMP_THREADS");
        }
        ASSERT_EQ(0, mlvappPlaybackReconTeamRequest());
        ASSERT_EQ(0, mlvappPlaybackPrepTeamRequest());
        ASSERT_EQ(mlvappEffectivePlaybackWorkerThreadCount(), mlvappPlaybackReconWorkerThreadCount());
        ASSERT_EQ(mlvappEffectivePlaybackWorkerThreadCount(), mlvappPlaybackPrepWorkerThreadCount());
    }
}

TEST(WorkerThreadCount, PlaybackTeamKnobsClampToThePlaybackCount)
{
    EnvGuard forceThreads("MLVAPP_FORCE_THREADS");
    EnvGuard forceSingle("MLVAPP_FORCE_SINGLETHREAD");
    EnvGuard playbackCap("MLVAPP_PLAYBACK_MAX_THREADS");
    EnvGuard recon("MLVAPP_PLAYBACK_RECON_OMP_THREADS");
    EnvGuard prep("MLVAPP_PLAYBACK_PREP_OMP_THREADS");
    qunsetenv("MLVAPP_FORCE_THREADS");
    qunsetenv("MLVAPP_FORCE_SINGLETHREAD");
    qunsetenv("MLVAPP_PLAYBACK_MAX_THREADS");
    const int cap = mlvappEffectivePlaybackWorkerThreadCount();
    qputenv("MLVAPP_PLAYBACK_RECON_OMP_THREADS", QByteArrayLiteral("1"));
    qputenv("MLVAPP_PLAYBACK_PREP_OMP_THREADS", QByteArrayLiteral("999"));
    ASSERT_EQ(1, mlvappPlaybackReconWorkerThreadCount());
    ASSERT_EQ(1, mlvappPlaybackReconTeamRequest());
    ASSERT_EQ(cap, mlvappPlaybackPrepWorkerThreadCount());
    ASSERT_EQ(cap, mlvappPlaybackPrepTeamRequest());
    qputenv("MLVAPP_PLAYBACK_RECON_OMP_THREADS", QByteArrayLiteral("12"));
    qputenv("MLVAPP_PLAYBACK_PREP_OMP_THREADS", QByteArrayLiteral("6"));
    // Locals: qMin returns a reference, which must not outlive its temporaries inside ASSERT_EQ.
    const int expectedRecon = cap < 12 ? cap : 12;
    const int expectedPrep = cap < 6 ? cap : 6;
    ASSERT_EQ(expectedRecon, mlvappPlaybackReconWorkerThreadCount());
    ASSERT_EQ(expectedPrep, mlvappPlaybackPrepWorkerThreadCount());
    // The general playback count itself is untouched by the knobs.
    ASSERT_EQ(cap, mlvappEffectivePlaybackWorkerThreadCount());
}

TEST(WorkerThreadCount, PlaybackTeamKnobsAreInertUnderAThreadOverride)
{
    EnvGuard forceThreads("MLVAPP_FORCE_THREADS");
    EnvGuard forceSingle("MLVAPP_FORCE_SINGLETHREAD");
    EnvGuard recon("MLVAPP_PLAYBACK_RECON_OMP_THREADS");
    EnvGuard prep("MLVAPP_PLAYBACK_PREP_OMP_THREADS");
    qputenv("MLVAPP_PLAYBACK_RECON_OMP_THREADS", QByteArrayLiteral("2"));
    qputenv("MLVAPP_PLAYBACK_PREP_OMP_THREADS", QByteArrayLiteral("3"));
    qunsetenv("MLVAPP_FORCE_SINGLETHREAD");
    qputenv("MLVAPP_FORCE_THREADS", QByteArrayLiteral("8"));
    ASSERT_EQ(0, mlvappPlaybackReconTeamRequest());
    ASSERT_EQ(0, mlvappPlaybackPrepTeamRequest());
    ASSERT_EQ(8, mlvappPlaybackReconWorkerThreadCount());
    ASSERT_EQ(8, mlvappPlaybackPrepWorkerThreadCount());
    qunsetenv("MLVAPP_FORCE_THREADS");
    qputenv("MLVAPP_FORCE_SINGLETHREAD", QByteArrayLiteral("1"));
    ASSERT_EQ(0, mlvappPlaybackReconTeamRequest());
    ASSERT_EQ(0, mlvappPlaybackPrepTeamRequest());
    ASSERT_EQ(1, mlvappPlaybackReconWorkerThreadCount());
    ASSERT_EQ(1, mlvappPlaybackPrepWorkerThreadCount());
}
