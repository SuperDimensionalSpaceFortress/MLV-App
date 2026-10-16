// PLAYBACK-STARTUP-WAIT-PREWARM-1 r2 (both keys' blocker on 8f64fe85): the held-sink audio start, driven against a
// simulated WASAPI pull sink. The simulation keeps 1 ms slots: a ring the gate fills (Qt's ringbuffer, 170 ms) and a
// host buffer the device plays from, refilled every 10 ms period (Qt 6.10 QWASAPIAudioSinkStream). Open, close and
// resume are blocking calls that advance the clock with the device not running, the way IAudioClient::Start did on
// Bachelor (0.3-0.63 s). The A/V offset is read from what the simulated device actually plays, not from the engine.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackAudioStartPolicy.h"

#include <cmath>
#include <deque>
#include <string>

using playback_audio_start::SinkPort;
using playback_audio_start::StartEngine;
using playback_audio_start::StartReport;

namespace
{

constexpr double kFps = 24000.0 / 1001.0; // 23.976
constexpr double kSilence = -1.0;

class SimulatedSink : public SinkPort
{
public:
    int hostSlots = 46;          // Bachelor's first fill (r1: processedUSecs 46.6 ms on the fast-resume legs)
    int ringSlots = 170;         // 32768 bytes at 48 kHz stereo 16-bit
    int periodMs = 10;
    double openCostMs = 300.0;
    double resumeCostMs = 0.5;
    const char *unhealthy = nullptr;

    int opens = 0;
    int closes = 0;
    int suspends = 0;
    int resumes = 0;

    double clock = 0.0;
    double lastAudibleAt = -1.0;
    double lastAudible = kSilence;

    void advance( double ms )
    {
        for( int i = 0; i < static_cast<int>( ms ); ++i ) tick();
    }

    double nowMs() override { return clock; }

    bool open( double primeMs ) override
    {
        ++opens;
        idle( openCostMs );
        m_host.clear();
        m_ring.clear();
        m_processed = 0.0;
        m_served = 0.0;
        m_gateOpen = false;
        m_primeLeft = primeMs;
        pull();
        refill();          // fillInitialHostBuffer
        m_running = true;  // IAudioClient::Start
        return true;
    }

    void close() override
    {
        ++closes;
        m_running = false;
        m_host.clear();
        m_ring.clear();
        m_gateOpen = false;
        m_primeLeft = 0.0;
    }

    void suspend() override
    {
        ++suspends;
        m_running = false;
    }

    void resume() override
    {
        ++resumes;
        idle( resumeCostMs );
        m_running = true;
    }

    void silence() override
    {
        m_gateOpen = false;
        m_primeLeft = 0.0;
    }

    void handOver( double contentMs ) override
    {
        m_gateOpen = true;
        m_gatePos = std::floor( contentMs );
        pull(); // readyRead: Qt pulls at once
    }

    double processedMs() override { return m_processed; }
    double servedMs() override { return m_served; }
    const char *unhealthyReason() override { return unhealthy; }

private:
    void idle( double ms )
    {
        const bool was = m_running;
        m_running = false;
        advance( ms );
        m_running = was;
    }

    void tick()
    {
        clock += 1.0;
        if( !m_running ) return;
        if( !m_host.empty() )
        {
            lastAudible = m_host.front();
            lastAudibleAt = clock;
            m_host.pop_front();
        }
        if( static_cast<long long>( clock ) % periodMs == 0 ) refill();
    }

    void refill()
    {
        while( static_cast<int>( m_host.size() ) < hostSlots )
        {
            if( !m_ring.empty() )
            {
                m_host.push_back( m_ring.front() );
                m_ring.pop_front();
                m_processed += 1.0;
            }
            else
            {
                m_host.push_back( kSilence ); // AUDCLNT_BUFFERFLAGS_SILENT, not counted as processed
            }
        }
        pull();
    }

    void pull()
    {
        while( static_cast<int>( m_ring.size() ) < ringSlots )
        {
            if( m_gateOpen )
            {
                m_ring.push_back( m_gatePos );
                m_gatePos += 1.0;
                m_served += 1.0;
            }
            else if( m_primeLeft >= 1.0 )
            {
                m_ring.push_back( kSilence );
                m_primeLeft -= 1.0;
                m_served += 1.0;
            }
            else
            {
                break;
            }
        }
    }

    bool m_running = false;
    bool m_gateOpen = false;
    double m_gatePos = 0.0;
    double m_primeLeft = 0.0;
    double m_processed = 0.0;
    double m_served = 0.0;
    std::deque<double> m_host;
    std::deque<double> m_ring;
};

// What the simulated device plays now, minus where the video timeline is: frame \a frame was presented at \a presentMs.
double playedOffsetMs( const SimulatedSink &sink, int frame, double presentMs )
{
    const double videoMs = frame * 1000.0 / kFps + ( sink.lastAudibleAt - presentMs );
    return sink.lastAudible - videoMs;
}

} // namespace

TEST(PlaybackAudioStartEngine, APreparedFrameZeroWhoseResumeTakes600MsStaysWithin45Ms)
{
    // sol's catalogue row: prepared frame 0, prompt sync dispatch, resume takes 600 ms -> |A/V offset| <= 45 ms
    SimulatedSink sink;
    StartEngine engine(sink);
    engine.setFps(kFps);
    ASSERT_TRUE(engine.prepare());
    sink.advance(5000.0);
    engine.holdExpired();                 // held long enough to be suspended: the slow path
    ASSERT_TRUE(engine.isSuspended());
    sink.resumeCostMs = 600.0;
    const double presentMs = sink.clock;
    sink.advance(2.0);                    // prompt dispatch
    const StartReport report = engine.sync(0, presentMs);
    ASSERT_EQ(std::string("prepared"), std::string(report.mode));
    ASSERT_EQ(std::string("resumed"), std::string(report.sink));
    ASSERT_NEAR(600.0, report.resumeMs, 1.0);
    sink.advance(800.0);
    ASSERT_TRUE(sink.lastAudible >= 0.0);
    const double offset = playedOffsetMs(sink, 0, presentMs);
    ASSERT_TRUE(std::fabs(offset) <= 45.0);
    // the printed audible reading agrees with what plays
    ASSERT_TRUE(std::fabs(report.avOffsetAudibleMs - offset) <= 15.0);
}

TEST(PlaybackAudioStartEngine, AHeldSinkIsRunningSoTheFirstPresentHasNoDeviceCallAndStaysInSync)
{
    SimulatedSink sink;
    StartEngine engine(sink);
    engine.setFps(kFps);
    ASSERT_TRUE(engine.prepare());
    ASSERT_NEAR(46.0, engine.hostBufferMs(), 1e-9); // measured from the zeros of the first fill
    sink.advance(3000.0);
    const double presentMs = sink.clock;
    sink.advance(3.0);
    const StartReport report = engine.sync(120, presentMs);
    ASSERT_EQ(std::string("running"), std::string(report.sink));
    ASSERT_EQ(0, sink.resumes);
    ASSERT_EQ(1, sink.opens);
    ASSERT_NEAR(0.0, report.resumeMs, 1e-9);
    sink.advance(500.0);
    ASSERT_TRUE(std::fabs(playedOffsetMs(sink, 120, presentMs)) <= 45.0);
}

TEST(PlaybackAudioStartEngine, AResumeAboveTheSlackReanchorsAtAnyFrame)
{
    // fable's catalogue row: a prepared resume_lag above the slack must still land within the slack
    for( double resumeMs : { 287.0, 450.0, 633.0 } )
    {
        SimulatedSink sink;
        StartEngine engine(sink);
        engine.setFps(kFps);
        engine.prepare();
        sink.advance(2000.0);
        engine.holdExpired();
        sink.resumeCostMs = resumeMs;
        const double presentMs = sink.clock;
        sink.advance(1.0);
        const StartReport report = engine.sync(240, presentMs);
        ASSERT_TRUE(report.resumeLagMs > playback_audio_start::kAudioResumeSlackMs);
        sink.advance(700.0);
        ASSERT_TRUE(std::fabs(playedOffsetMs(sink, 240, presentMs)) <= 45.0);
    }
}

TEST(PlaybackAudioStartEngine, AnUnhealthyHeldSinkIsReopenedAndStillLandsOnTheTimeline)
{
    // r2 row b: a device change or stream error between the prepare and the first present re-opens
    SimulatedSink sink;
    StartEngine engine(sink);
    engine.setFps(kFps);
    engine.prepare();
    sink.advance(1000.0);
    sink.unhealthy = "device_changed";
    sink.openCostMs = 600.0;
    const double presentMs = sink.clock;
    const StartReport report = engine.sync(48, presentMs);
    sink.unhealthy = nullptr;
    ASSERT_EQ(std::string("reopen"), std::string(report.mode));
    ASSERT_EQ(std::string("device_changed"), std::string(report.health));
    ASSERT_EQ(std::string("opened"), std::string(report.sink));
    ASSERT_EQ(2, sink.opens);
    ASSERT_EQ(1, sink.closes);
    ASSERT_NEAR(600.0, report.reopenMs, 1.0);
    sink.advance(500.0);
    ASSERT_TRUE(std::fabs(playedOffsetMs(sink, 48, presentMs)) <= 45.0);
}

TEST(PlaybackAudioStartEngine, StopKeepsTheSinkHeldAndAPausePlayCycleNeverReopens)
{
    SimulatedSink sink;
    StartEngine engine(sink);
    engine.setFps(kFps);
    engine.prepare();
    sink.advance(500.0);
    double presentMs = sink.clock;
    engine.sync(10, presentMs);
    sink.advance(1000.0);
    engine.stop();                         // pause: the gate closes, the sink keeps running
    ASSERT_TRUE(engine.isOpen());
    ASSERT_EQ(0, sink.closes);
    ASSERT_FALSE(engine.prepare());        // the paused present at another frame keeps it
    ASSERT_EQ(1, sink.opens);
    sink.advance(60.0);                    // Play again while the old content is still queued
    presentMs = sink.clock;
    const StartReport report = engine.sync(400, presentMs);
    ASSERT_EQ(std::string("running"), std::string(report.sink));
    ASSERT_TRUE(report.outLatencyMs > 46.0); // the queued tail of the old content is ahead of it
    sink.advance(600.0);
    ASSERT_TRUE(std::fabs(playedOffsetMs(sink, 400, presentMs)) <= 45.0);
}

TEST(PlaybackAudioStartEngine, AHeldSinkSuspendsOnlyAfterTheHoldAndThePlayAtPrepareResumesIt)
{
    SimulatedSink sink;
    StartEngine engine(sink);
    engine.prepare();
    engine.holdExpired();
    ASSERT_TRUE(engine.isSuspended());
    ASSERT_EQ(1, sink.suspends);
    engine.holdExpired();                  // once
    ASSERT_EQ(1, sink.suspends);
    ASSERT_FALSE(engine.prepare());        // Play's prepare resumes it off the first present's path
    ASSERT_FALSE(engine.isSuspended());
    ASSERT_EQ(1, sink.resumes);
    engine.reset();
    ASSERT_FALSE(engine.isOpen());
    ASSERT_EQ(1, sink.closes);
}

TEST(PlaybackAudioStartEngine, ThePrintedReadingsAreTheR1FormulaAndItsAudibleCorrection)
{
    SimulatedSink sink;
    StartEngine engine(sink);
    engine.setFps(kFps);
    engine.prepare();
    sink.advance(400.0);
    const double presentMs = sink.clock;
    sink.advance(4.0);
    const StartReport report = engine.sync(0, presentMs);
    // r1's quantity takes the handover as the moment of sound: the content is ahead by the output latency
    ASSERT_NEAR(report.outLatencyMs, report.avOffsetEstMs, 1e-6);
    ASSERT_NEAR(0.0, report.avOffsetAudibleMs, 1e-6);
    ASSERT_NEAR(4.0, report.resumeLagMs, 1e-9);
    ASSERT_EQ(static_cast<int>(std::floor((4.0 + report.outLatencyMs) * kFps / 1000.0)), report.seekFrame);
}

TEST(PlaybackAudioStartSession, AStopInvalidatesASyncPostedBeforeIt)
{
    // r2 row c: Play, the first present posts a sync, Pause before the audio thread runs it -> no blip
    playback_audio_start::SessionStamp stamp;
    const auto posted = stamp.current();
    ASSERT_TRUE(stamp.isCurrent(posted));
    stamp.invalidate();
    ASSERT_FALSE(stamp.isCurrent(posted));
    ASSERT_TRUE(stamp.isCurrent(stamp.current()));
}
