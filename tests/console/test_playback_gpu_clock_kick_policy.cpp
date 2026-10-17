// PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row K: at Play, before the play-start frame request, one bounded
// <<<1, 32>>> kernel on a lowest-priority stream of the already-loaded AMaZE backend lets the GPU clocks ramp while
// frame 0 decodes. These pin when it runs (every guard passes), one Skip per reason, how a refused begin is named, the
// ramp arithmetic over fixed (%globaltimer, clock64) samples, and the log line's fields.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackGpuClockKickPolicy.h"

#include <string>
#include <vector>

using playback_gpu_clock_kick::Decision;
using playback_gpu_clock_kick::Reason;
using playback_gpu_clock_kick::decide;
using playback_gpu_clock_kick::reasonName;

namespace {

// enabled, playing, cudaTextureRouteAdmitted, amazeBackendLoaded, cudaSuppressed, kickInFlight
Decision allPass() { return decide(true, true, true, true, false, false); }

std::string name(Reason reason) { return std::string(reasonName(reason)); }

} // namespace

TEST(PlaybackGpuClockKickPolicy, RunsOnlyWhenEveryGuardPasses)
{
    const Decision d = allPass();
    ASSERT_TRUE(d.run);
    ASSERT_TRUE(Reason::None == d.reason);
    ASSERT_EQ(std::string("none"), name(d.reason));
    // the kernel's own deadline: far under WDDM's 2 s TDR
    ASSERT_EQ(400, playback_gpu_clock_kick::kKickMaxMs);
}

TEST(PlaybackGpuClockKickPolicy, TheKillSwitchSkipsAsDisabled)
{
    const Decision d = decide(false, true, true, true, false, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::Disabled == d.reason);
    ASSERT_EQ(std::string("disabled"), name(d.reason));
}

TEST(PlaybackGpuClockKickPolicy, OnlyPlayKicks)
{
    // paused analysis, Look Assist, scrubbing: never
    const Decision d = decide(true, false, true, true, false, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::NotPlaying == d.reason);
    ASSERT_EQ(std::string("not_playing"), name(d.reason));
}

TEST(PlaybackGpuClockKickPolicy, TheCpuRouteSkipsAsCpuRoute)
{
    const Decision d = decide(true, true, false, false, false, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::CpuRoute == d.reason);
    ASSERT_EQ(std::string("cpu_route"), name(d.reason));
}

TEST(PlaybackGpuClockKickPolicy, AnUnloadedBackendSkipsAsBackendNotLoaded)
{
    // the kick never loads the AMaZE DLL
    const Decision d = decide(true, true, true, false, false, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::BackendNotLoaded == d.reason);
    ASSERT_EQ(std::string("backend_not_loaded"), name(d.reason));
}

TEST(PlaybackGpuClockKickPolicy, ADeviceLossSkipsAsCudaSuppressed)
{
    const Decision d = decide(true, true, true, true, true, false);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::CudaSuppressed == d.reason);
    ASSERT_EQ(std::string("cuda_suppressed"), name(d.reason));
}

TEST(PlaybackGpuClockKickPolicy, AKickStillRunningSkipsAsInFlight)
{
    // rapid Play / stop / Play: at most one kick in flight
    const Decision d = decide(true, true, true, true, false, true);
    ASSERT_FALSE(d.run);
    ASSERT_TRUE(Reason::InFlight == d.reason);
    ASSERT_EQ(std::string("in_flight"), name(d.reason));
}

TEST(PlaybackGpuClockKickPolicy, TheFirstFailingGuardNamesTheSkip)
{
    ASSERT_TRUE(Reason::Disabled == decide(false, false, false, false, true, true).reason);
    ASSERT_TRUE(Reason::NotPlaying == decide(true, false, false, false, true, true).reason);
    ASSERT_TRUE(Reason::CpuRoute == decide(true, true, false, false, true, true).reason);
    // a loss outranks the load state: nothing CUDA is touched after it
    ASSERT_TRUE(Reason::CudaSuppressed == decide(true, true, true, false, true, true).reason);
    ASSERT_TRUE(Reason::BackendNotLoaded == decide(true, true, true, false, false, true).reason);
}

TEST(PlaybackGpuClockKickPolicy, ARefusedBeginIsNamedByTheBackendsWhy)
{
    using playback_gpu_clock_kick::reasonFromBeginFailure;
    ASSERT_TRUE(Reason::CudaSuppressed == reasonFromBeginFailure(QStringLiteral("cuda_suppressed")));
    ASSERT_TRUE(Reason::InFlight == reasonFromBeginFailure(QStringLiteral("in_flight")));
    ASSERT_TRUE(Reason::BackendNotLoaded == reasonFromBeginFailure(QStringLiteral("backend_not_loaded")));
    // an older DLL without the kick symbols
    ASSERT_TRUE(Reason::BackendNotLoaded == reasonFromBeginFailure(QStringLiteral("symbol_missing")));
    ASSERT_TRUE(Reason::BeginFailed == reasonFromBeginFailure(QStringLiteral("begin_failed")));
    ASSERT_TRUE(Reason::BeginFailed == reasonFromBeginFailure(QString()));
    ASSERT_EQ(std::string("begin_failed"), name(Reason::BeginFailed));
}

TEST(PlaybackGpuClockKickPolicy, IntervalMhzIsCyclesOverNanoseconds)
{
    // 5 ms apart: 1,050,000 cycles = 210 MHz (P8), then 7.5 M = 1500 MHz
    const std::vector<unsigned long long> ns = {1000000000ull, 1005000000ull, 1010000000ull};
    const std::vector<long long> clk = {100, 100 + 1050000, 100 + 1050000 + 7500000};
    const std::vector<double> mhz = playback_gpu_clock_kick::intervalMhz(ns, clk);
    ASSERT_EQ(2, static_cast<int>(mhz.size()));
    ASSERT_NEAR(210.0, mhz[0], 1e-6);
    ASSERT_NEAR(1500.0, mhz[1], 1e-6);
    // sample offsets from the first sample: the first is the origin, then 5 and 10
    const std::vector<double> offsets = playback_gpu_clock_kick::sampleMs(ns);
    ASSERT_EQ(3, static_cast<int>(offsets.size()));
    ASSERT_NEAR(0.0, offsets[0], 1e-9);
    ASSERT_NEAR(5.0, offsets[1], 1e-9);
    ASSERT_NEAR(10.0, offsets[2], 1e-9);
    // a sample that does not advance in time is 0 MHz, never a division by zero; one sample has no interval
    const std::vector<double> flat = playback_gpu_clock_kick::intervalMhz({7ull, 7ull}, {1, 500});
    ASSERT_EQ(1, static_cast<int>(flat.size()));
    ASSERT_NEAR(0.0, flat[0], 1e-9);
    ASSERT_TRUE(playback_gpu_clock_kick::intervalMhz({7ull}, {1}).empty());
}

TEST(PlaybackGpuClockKickPolicy, AnIntervalAcrossTwoSmsIsUnknown)
{
    // clock64 is per SM and the kick's chunk launches may land on different SMs: such an interval is -1
    const std::vector<unsigned long long> ns = {0ull, 5000000ull, 10000000ull, 15000000ull};
    const std::vector<long long> clk = {1000, 1000 + 1050000, 77, 77 + 7500000};
    const std::vector<unsigned int> sm = {3u, 3u, 9u, 9u};
    const std::vector<double> mhz = playback_gpu_clock_kick::intervalMhz(ns, clk, sm);
    ASSERT_EQ(3, static_cast<int>(mhz.size()));
    ASSERT_NEAR(210.0, mhz[0], 1e-6);
    ASSERT_NEAR(-1.0, mhz[1], 1e-9);
    ASSERT_NEAR(1500.0, mhz[2], 1e-6);
    // first / last known skip the unknown ones; none known is -1
    ASSERT_NEAR(210.0, playback_gpu_clock_kick::firstKnownMhz(mhz), 1e-6);
    ASSERT_NEAR(1500.0, playback_gpu_clock_kick::lastKnownMhz(mhz), 1e-6);
    ASSERT_NEAR(1500.0, playback_gpu_clock_kick::firstKnownMhz({-1.0, 1500.0, -1.0}), 1e-6);
    ASSERT_NEAR(1500.0, playback_gpu_clock_kick::lastKnownMhz({-1.0, 1500.0, -1.0}), 1e-6);
    ASSERT_NEAR(-1.0, playback_gpu_clock_kick::firstKnownMhz({-1.0, -1.0}), 1e-9);
    ASSERT_NEAR(-1.0, playback_gpu_clock_kick::lastKnownMhz({}), 1e-9);
    // an unknown interval is never the peak and never the ramp point
    ASSERT_NEAR(15.0, playback_gpu_clock_kick::rampMsTo80pct({0.0, 5.0, 10.0, 15.0}, {210.0, -1.0, 1500.0}), 1e-9);
    // fewer SM ids than samples: the rest count as one SM
    ASSERT_NEAR(1500.0, playback_gpu_clock_kick::intervalMhz({0ull, 5000000ull}, {0, 7500000}, {4u})[0], 1e-6);
}

TEST(PlaybackGpuClockKickPolicy, TheRampIsTheEndOfTheFirstIntervalAt80PercentOfThePeak)
{
    using playback_gpu_clock_kick::rampMsTo80pct;
    // 210 -> 600 -> 1500 -> 1800 MHz: 80 % of 1800 is 1440, first reached over 10-15 ms
    ASSERT_NEAR(15.0, rampMsTo80pct({0.0, 5.0, 10.0, 15.0, 20.0}, {210.0, 600.0, 1500.0, 1800.0}), 1e-9);
    // already warm: the first interval counts, so it is its end, never 0
    ASSERT_NEAR(5.0, rampMsTo80pct({0.0, 5.0, 10.0}, {1800.0, 1795.0}), 1e-9);
    // unknown: no interval, fewer than 2 samples, or nothing above 0
    ASSERT_NEAR(-1.0, rampMsTo80pct({}, {}), 1e-9);
    ASSERT_NEAR(-1.0, rampMsTo80pct({0.0}, {}), 1e-9);
    ASSERT_NEAR(-1.0, rampMsTo80pct({0.0, 5.0}, {0.0}), 1e-9);
    // a closing sample after a short last interval still bounds the ramp by real time
    ASSERT_NEAR(12.5, rampMsTo80pct({0.0, 5.0, 10.0, 12.5}, {300.0, 900.0, 1400.0}), 1e-9);
}

TEST(PlaybackGpuClockKickPolicy, StoppedByNamesTheBackendReport)
{
    using playback_gpu_clock_kick::stoppedByName;
    ASSERT_EQ(std::string("deadline"), std::string(stoppedByName(0, 0)));
    ASSERT_EQ(std::string("end"), std::string(stoppedByName(0, 1)));
    ASSERT_EQ(std::string("amaze_drain"), std::string(stoppedByName(0, 2)));
    ASSERT_EQ(std::string("pending"), std::string(stoppedByName(1, 0)));
    ASSERT_EQ(std::string("none"), std::string(stoppedByName(2, 0)));
    ASSERT_EQ(std::string("none"), std::string(stoppedByName(0, 9)));
}

TEST(PlaybackGpuClockKickPolicy, TheRunLineCarriesEveryFieldInOrder)
{
    playback_gpu_clock_kick::Line line;
    line.session = 3;
    line.decision = allPass();
    line.ranMs = 187.25;
    line.samples = 39;
    line.mhzFirst = 210.0;
    line.mhzAtAmaze0 = 1695.5;
    line.mhzLast = 1800.0;
    line.rampMs = 45.0;
    line.stoppedBy = QStringLiteral("amaze_drain");
    line.endedAt = QStringLiteral("first_present");
    line.kickLeadMs = 190.125;
    ASSERT_EQ(std::string("playback_gpu_clock_kick session=3 decision=run reason=none max_ms=400 ran_ms=187.250 samples=39 "
                          "mhz_first=210.0 mhz_at_amaze0=1695.5 mhz_last=1800.0 ramp_ms_to_80pct=45.000 "
                          "stopped_by=amaze_drain ended_at=first_present kick_lead_ms=190.125"),
              line.format().toStdString());
}

TEST(PlaybackGpuClockKickPolicy, ASkipLineSaysSkipAndWhyWithNAForEveryMeasure)
{
    playback_gpu_clock_kick::Line line;
    line.session = 1;
    line.decision = decide(true, true, false, false, false, false);
    ASSERT_EQ(std::string("playback_gpu_clock_kick session=1 decision=skip reason=cpu_route max_ms=400 ran_ms=0.000 "
                          "samples=0 mhz_first=NA mhz_at_amaze0=NA mhz_last=NA ramp_ms_to_80pct=NA stopped_by=none "
                          "ended_at=none kick_lead_ms=NA"),
              line.format().toStdString());
    // free text is printed as is, never substituted, and never left empty
    line.endedAt = QStringLiteral("odd%1site");
    line.stoppedBy.clear();
    const std::string text = line.format().toStdString();
    ASSERT_TRUE(text.find(" ended_at=odd%1site ") != std::string::npos);
    ASSERT_TRUE(text.find(" stopped_by=none ") != std::string::npos);
}
