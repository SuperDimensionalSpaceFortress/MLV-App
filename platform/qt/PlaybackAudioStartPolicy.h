/*!
 * \file PlaybackAudioStartPolicy.h
 * \brief How a Play's audio starts once the audio device lives on its own thread (pure, unit-tested).
 *
 * PLAYBACK-STARTUP-WAIT-PREWARM-1 Row A. On Bachelor the first Play-present blocked the GUI thread ~0.3-0.65 s in
 * the audio start (AudioPlayback::play(): QAudioSink::start opens the WASAPI device on every Play, since stop()
 * closes it), and the smoke stamp, the pace clock and the second frame all waited behind it.
 *
 * r1 opened the sink silently while paused and suspended it, and the first present resumed it. On Bachelor that
 * resume (IAudioClient::Start in Qt 6.10) took 0.3-0.63 s on 6 of 9 cuda legs, and the audio started at the prepared
 * frame when it returned, so it trailed the video by that long (r1b S5 MISS, 557 ms). r2:
 *  - The sink pulls from a gate (PlaybackAudioGate.h), never from the clip directly. While held the gate is closed:
 *    the sink runs and plays silence, and no content moves. A held sink is running, not suspended, so the first
 *    present has no device call on its path; after kAudioHeldRunningMs without a Play it is suspended (an active
 *    stream can keep the machine awake), and the Play that follows resumes it.
 *  - At the first present the audio thread hands the content over (opens the gate) only AFTER the sink can play:
 *    after a resume, or after a re-open when the held sink is gone or unhealthy. The content starts where the video
 *    timeline will be when it becomes audible: the presented frame, plus the time since the present, plus the output
 *    latency (what the sink already queued ahead of it). A slow resume or open then delays the sound, never the sync.
 *  - Each open serves kAudioPrimeMs of zeros first. The sink's first fill takes its host buffer from them, so the
 *    processed count right after the open measures that buffer (Qt 6.10 counts frames when they enter the host
 *    buffer, not when they play).
 *  - MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY=1: today's synchronous stop / jumpToPos / play on the GUI thread.
 */

#ifndef PLAYBACKAUDIOSTARTPOLICY_H
#define PLAYBACKAUDIOSTARTPOLICY_H

#include <atomic>
#include <cmath>
#include <cstdint>

namespace playback_audio_start
{

/*! One frame period at 23.976 fps: a resume slower than this is logged slow (the handover re-anchors either way). */
inline constexpr double kAudioResumeSlackMs = 42.0;
/*! Zeros served at every open, so the sink's first fill (its host buffer) is measurable. */
inline constexpr double kAudioPrimeMs = 100.0;
/*! A held (silent, running) sink is suspended after this long without a Play. */
inline constexpr double kAudioHeldRunningMs = 60000.0;

enum class Mode { Prepared, Reopen, Legacy };

inline const char *modeName( Mode mode )
{
    switch( mode )
    {
    case Mode::Prepared: return "prepared";
    case Mode::Reopen: return "reopen";
    case Mode::Legacy: return "legacy";
    }
    return "none";
}

/*! The held sink is used whenever it is open and healthy: the handover positions the content, so neither the frame
 *  it was prepared at nor the lag matters any more. A missing or unhealthy sink (a device change or a stream error
 *  since the prepare) is re-opened. */
inline Mode chooseMode( bool legacy, bool sinkOpen, bool sinkHealthy )
{
    if( legacy ) return Mode::Legacy;
    if( sinkOpen && sinkHealthy ) return Mode::Prepared;
    return Mode::Reopen;
}

inline double frameMs( int frame, double fps )
{
    if( !( fps > 0.0 ) ) return 0.0;
    return ( frame > 0 ? frame : 0 ) * 1000.0 / fps;
}

/*! What plays before content handed over now: zeros or content already in the sink's ring (served minus
 *  processed), then its host buffer. */
inline double outputLatencyMs( double servedMs, double processedMs, double hostBufferMs )
{
    const double queued = servedMs - processedMs;
    return ( queued > 0.0 ? queued : 0.0 ) + ( hostBufferMs > 0.0 ? hostBufferMs : 0.0 );
}

/*! Where the content starts so it is on the video timeline when audible: \a frame was presented \a sincePresentMs
 *  ago, and the content becomes audible \a latencyMs from now. */
inline double handoverContentMs( int frame, double sincePresentMs, double latencyMs, double fps )
{
    return frameMs( frame, fps ) + ( sincePresentMs > 0.0 ? sincePresentMs : 0.0 ) + ( latencyMs > 0.0 ? latencyMs : 0.0 );
}

/*! App-side A/V offset (the r1 pre-registered quantity): the audio content time (it starts at \a audioFrame, plus
 *  \a audioPlayedMs already consumed) minus the video timeline time (\a videoFrame was presented \a sincePresentMs
 *  earlier). Signed: audio late is negative. It takes the moment of the handover as the moment of sound; the
 *  audible reading subtracts the output latency. */
inline double avOffsetEstMs( int audioFrame, double audioPlayedMs, int videoFrame, double sincePresentMs, double fps )
{
    if( !( fps > 0.0 ) ) return 0.0;
    const double audioMs = audioFrame * 1000.0 / fps + audioPlayedMs;
    const double videoMs = videoFrame * 1000.0 / fps + sincePresentMs;
    return audioMs - videoMs;
}

struct StartReport
{
    bool valid = false;
    const char *mode = "none";      // prepared | reopen | legacy
    const char *sink = "none";      // running | resumed | opened | failed
    const char *health = "none";    // ok | closed | error | stopped | device_changed
    double prepareMs = 0.0;         // the newest open while paused (or at Play)
    double resumeMs = 0.0;          // the resume() call of a suspended held sink
    double resumeLagMs = 0.0;       // first present -> content handed over
    double reopenMs = 0.0;          // the open when the held sink could not be used
    double outLatencyMs = 0.0;      // queued ahead of the content at the handover
    int syncFrame = -1;             // the presented frame the audio is synced to
    int seekFrame = -1;             // the frame the content starts in
    double avOffsetEstMs = 0.0;     // r1 formula: content minus video timeline at the handover
    double avOffsetAudibleMs = 0.0; // the same when the content is audible (minus the output latency)
};

/*! What the engine drives: AudioPlayback adapts QAudioSink and the gate, the tests a simulated sink. Every call is
 *  on the audio thread; open, close and resume may block. */
class SinkPort
{
public:
    virtual ~SinkPort() = default;
    virtual double nowMs() = 0;                    // the clock the presents are stamped with
    virtual bool open( double primeMs ) = 0;       // start pulling from the closed gate, \a primeMs of zeros first
    virtual void close() = 0;                      // stop and reset the sink; the gate closes
    virtual void suspend() = 0;
    virtual void resume() = 0;
    virtual void silence() = 0;                    // close the gate: the sink keeps running on silence
    virtual void handOver( double contentMs ) = 0; // open the gate at \a contentMs
    virtual double processedMs() = 0;              // what the sink took into its host buffer since the open
    virtual double servedMs() = 0;                 // what the gate served (zeros and content) since the open
    virtual const char *unhealthyReason() = 0;     // nullptr when healthy
};

/*! The held-sink state machine. Audio thread only. */
class StartEngine
{
public:
    explicit StartEngine( SinkPort &sink ) : m_sink( sink ) {}

    void setFps( double fps ) { m_fps = fps; }
    bool isOpen() const { return m_open; }
    bool isSuspended() const { return m_suspended; }
    bool isPlaying() const { return m_playing; }
    double hostBufferMs() const { return m_hostBufferMs; }
    int openCount() const { return m_openCount; }

    /*! At a settled paused frame and at Play: hold the sink open, running and silent. An open sink is kept (any
     *  frame); a suspended one is resumed. Returns true when it opened the sink. */
    bool prepare()
    {
        if( !m_open )
        {
            const double startMs = m_sink.nowMs();
            const bool opened = openNow();
            m_prepareMs = m_sink.nowMs() - startMs;
            return opened;
        }
        if( !m_playing && m_suspended )
        {
            m_sink.resume();
            m_suspended = false;
        }
        return false;
    }

    /*! kAudioHeldRunningMs after the newest prepare or stop with no Play: suspend the silent sink. */
    void holdExpired()
    {
        if( m_open && !m_playing && !m_suspended )
        {
            m_sink.suspend();
            m_suspended = true;
        }
    }

    /*! A public stop (pause, seek, resize): silence, but keep the sink running and held. */
    void stop()
    {
        if( m_open && m_playing ) m_sink.silence();
        m_playing = false;
    }

    /*! Clip change or shutdown: close the sink. */
    void reset()
    {
        if( m_open ) m_sink.close();
        m_open = false;
        m_suspended = false;
        m_playing = false;
    }

    /*! At a present: hand the content over so it is audible on the video timeline. */
    StartReport sync( int frame, double presentMs )
    {
        StartReport report;
        report.valid = true;
        report.syncFrame = frame;
        report.prepareMs = m_prepareMs;
        const char *unhealthy = m_open ? m_sink.unhealthyReason() : nullptr;
        report.health = !m_open ? "closed" : ( unhealthy ? unhealthy : "ok" );
        const Mode mode = chooseMode( false, m_open, unhealthy == nullptr );
        report.mode = modeName( mode );
        if( mode == Mode::Prepared )
        {
            report.sink = "running";
            if( m_suspended )
            {
                const double resumeStartMs = m_sink.nowMs();
                m_sink.resume();
                m_suspended = false;
                report.resumeMs = m_sink.nowMs() - resumeStartMs;
                report.sink = "resumed";
            }
        }
        else
        {
            if( m_open ) m_sink.close();
            m_open = false;
            m_suspended = false;
            const double openStartMs = m_sink.nowMs();
            const bool opened = openNow();
            report.reopenMs = m_sink.nowMs() - openStartMs;
            report.sink = opened ? "opened" : "failed";
            if( !opened ) return report;
        }

        const double handoverMs = m_sink.nowMs();
        const double sinceMs = handoverMs - presentMs;
        const double servedMs = m_sink.servedMs();
        const double processedMs = m_sink.processedMs();
        const double latencyMs = outputLatencyMs( servedMs, processedMs, m_hostBufferMs );
        const double contentMs = handoverContentMs( frame, sinceMs, latencyMs, m_fps );
        m_sink.handOver( contentMs );
        m_playing = true;

        const double videoMs = frameMs( frame, m_fps ) + ( sinceMs > 0.0 ? sinceMs : 0.0 );
        report.resumeLagMs = sinceMs;
        report.outLatencyMs = latencyMs;
        report.seekFrame = m_fps > 0.0 ? static_cast<int>( std::floor( contentMs * m_fps / 1000.0 + 1e-9 ) ) : frame;
        report.avOffsetEstMs = contentMs - videoMs;
        report.avOffsetAudibleMs = report.avOffsetEstMs - latencyMs;

        return report;
    }

private:
    bool openNow()
    {
        ++m_openCount;
        if( !m_sink.open( kAudioPrimeMs ) )
        {
            m_sink.close();
            return false;
        }
        // the first fill took the host buffer from the zeros; nothing else has been consumed yet
        const double processed = m_sink.processedMs();
        m_hostBufferMs = processed < kAudioPrimeMs ? processed : kAudioPrimeMs;
        m_open = true;
        m_suspended = false;
        m_playing = false;
        return true;
    }

    SinkPort &m_sink;
    double m_fps = 0.0;
    bool m_open = false;
    bool m_suspended = false;
    bool m_playing = false;
    double m_prepareMs = 0.0;
    double m_hostBufferMs = 0.0;
    int m_openCount = 0;
};

/*! A play-session stamp: a sync posted before a stop (pause, seek, resize) is dropped when it reaches the audio
 *  thread, so a pause before the first present cannot blip the audio on. */
class SessionStamp
{
public:
    std::uint64_t current() const { return m_generation.load(); }
    void invalidate() { ++m_generation; }
    bool isCurrent( std::uint64_t stamp ) const { return stamp == m_generation.load(); }

private:
    std::atomic<std::uint64_t> m_generation { 0 };
};

} // namespace playback_audio_start

#endif // PLAYBACKAUDIOSTARTPOLICY_H
