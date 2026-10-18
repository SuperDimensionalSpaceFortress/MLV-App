/*!
 * \file PlaybackGpuClockKickPolicy.h
 * \brief Whether Play starts the GPU clock kick, the kick's ramp arithmetic and its log line (pure, unit-tested).
 *
 * PLAYBACK-STARTUP-WAIT-S1-FIRST-AMAZE-AT-PLAY-1 Row K. The first AMaZE at Play costs 118-176 ms on Bachelor s1 against
 * ~18 ms once the GPU is busy: after the paused idle before Play the GPU sits in a low P-state (P8, ~210 MHz under a
 * held CUDA context), and the first frame's AMaZE pays the ramp. At Play, before the play-start frame request,
 * MainWindow starts a <<<1, 32>>> spin on a lowest-priority stream of the already-loaded AMaZE backend
 * (igpu_amaze_debayer_clock_kick_begin): a chain of launches of at most 20 ms each (the 100 ms per-launch budget),
 * spinning on %globaltimer for at most kKickMaxMs in all while frame 0 decodes and reconstructs, so the clocks ramp
 * before its AMaZE starts, sampling (%globaltimer, clock64, %smid) every ~5 ms. Frame 0's
 * AMaZE drain raises its stop flag; MainWindow collects it at the first present, Play stop, a clip change, a device
 * loss, the window going away or the deadline. Zero cost while paused; it never keeps clocks up while idle.
 * Default off since the HUB RULING of 2026-10-18T06:16:26Z item (1): MLVAPP_PLAYBACK_GPU_CLOCK_KICK=1 opts in
 * (=0, or unset, leaves it off).
 *
 *   playback_gpu_clock_kick session=N decision=<run|skip> reason=<name> max_ms= ran_ms= samples= mhz_first=
 *   mhz_at_amaze0= mhz_last= ramp_ms_to_80pct= stopped_by=<deadline|end|amaze_drain|pending|none>
 *   ended_at=<site|none> kick_lead_ms=
 *
 * mhz_* are SM MHz over one ~5 ms sample interval (clock64 over %globaltimer); mhz_at_amaze0 is frame 0's own
 * in-process probe (amaze_sm_mhz_before, Row 0). ramp_ms_to_80pct: kick start -> the end of the first interval at
 * >= 80 % of the highest interval the kick saw. kick_lead_ms: kick begin -> frame 0's AMaZE call. NA = unknown.
 */

#ifndef PLAYBACKGPUCLOCKKICKPOLICY_H
#define PLAYBACKGPUCLOCKKICKPOLICY_H

#include <QString>

#include <vector>

namespace playback_gpu_clock_kick
{

/*! The kernel's own deadline (ms). WDDM's TDR (2 s) is far above it. */
constexpr int kKickMaxMs = 400;

/*! The share of the kick's highest interval MHz that counts as ramped. */
constexpr double kRampFraction = 0.8;

/*! A collect that finds the backend's feeder still draining its last chunk (state pending) is retried this often, this
 *  many times, before the line is logged as pending. */
constexpr int kKickPendingRetryMs = 20;
constexpr int kKickPendingRetries = 10;

enum class Reason
{
    None,              // run
    Disabled,          // MLVAPP_PLAYBACK_GPU_CLOCK_KICK=0
    NotPlaying,        // only Play starts a kick (never paused analysis, Look Assist, scrubbing)
    CpuRoute,          // the CUDA texture route is not this Play's route: CUDA is never touched for it
    BackendNotLoaded,  // the AMaZE backend is not loaded yet (the kick never loads it); also an old DLL
    CudaSuppressed,    // GPU-DEVICE-LOSS-SELF-HEAL: no CUDA call after a device loss
    InFlight,          // a kick is still running (rapid Play / stop / Play)
    BeginFailed        // never from decide(): the backend's begin failed (its CUDA code is noted)
};

struct Decision
{
    bool run = false;
    Reason reason = Reason::None;
};

inline Decision decide( bool enabled,
                        bool playing,
                        bool cudaTextureRouteAdmitted,
                        bool amazeBackendLoaded,
                        bool cudaSuppressed,
                        bool kickInFlight )
{
    Decision d;
    if( !enabled ) d.reason = Reason::Disabled;
    else if( !playing ) d.reason = Reason::NotPlaying;
    else if( !cudaTextureRouteAdmitted ) d.reason = Reason::CpuRoute;
    else if( cudaSuppressed ) d.reason = Reason::CudaSuppressed;
    else if( !amazeBackendLoaded ) d.reason = Reason::BackendNotLoaded;
    else if( kickInFlight ) d.reason = Reason::InFlight;
    else d.run = true;
    return d;
}

inline const char *reasonName( Reason reason )
{
    switch( reason )
    {
    case Reason::None: return "none";
    case Reason::Disabled: return "disabled";
    case Reason::NotPlaying: return "not_playing";
    case Reason::CpuRoute: return "cpu_route";
    case Reason::BackendNotLoaded: return "backend_not_loaded";
    case Reason::CudaSuppressed: return "cuda_suppressed";
    case Reason::InFlight: return "in_flight";
    case Reason::BeginFailed: return "begin_failed";
    }
    return "none";
}

/*! A Run that gpuAmazeDebayerClockKickBegin refused, by its why (GpuDebayer.h): the backend's own re-check under the
 *  live lock outranks the decision taken before it. */
inline Reason reasonFromBeginFailure( const QString &why )
{
    if( why == QLatin1String( "cuda_suppressed" ) ) return Reason::CudaSuppressed;
    if( why == QLatin1String( "in_flight" ) ) return Reason::InFlight;
    if( why == QLatin1String( "backend_not_loaded" ) || why == QLatin1String( "symbol_missing" ) )
        return Reason::BackendNotLoaded;
    return Reason::BeginFailed;
}

/*! SM MHz over each interval between consecutive (%globaltimer ns, clock64) samples; an interval whose time does not
 *  advance is 0. One fewer value than samples (none for fewer than 2). clock64 is per SM and the kick's chunk launches
 *  may run on different SMs: with \a sm given (one %smid per sample), an interval whose two samples are on different
 *  SMs is -1 (unknown, skipped by every reader below). */
inline std::vector<double> intervalMhz( const std::vector<unsigned long long> &ns, const std::vector<long long> &clk,
                                        const std::vector<unsigned int> &sm = std::vector<unsigned int>() )
{
    std::vector<double> mhz;
    const size_t n = ns.size() < clk.size() ? ns.size() : clk.size();
    for( size_t i = 1; i < n; ++i )
    {
        if( i < sm.size() && sm[i] != sm[i - 1] )
        {
            mhz.push_back( -1.0 );
            continue;
        }
        const double dns = ns[i] > ns[i - 1] ? static_cast<double>( ns[i] - ns[i - 1] ) : 0.0;
        const double dclk = clk[i] > clk[i - 1] ? static_cast<double>( clk[i] - clk[i - 1] ) : 0.0;
        mhz.push_back( dns > 0.0 ? dclk * 1000.0 / dns : 0.0 );
    }
    return mhz;
}

/*! The first / last known (>= 0) interval MHz; -1 when none is known. */
inline double firstKnownMhz( const std::vector<double> &intervalMhzValues )
{
    for( double mhz : intervalMhzValues ) if( mhz >= 0.0 ) return mhz;
    return -1.0;
}
inline double lastKnownMhz( const std::vector<double> &intervalMhzValues )
{
    for( size_t i = intervalMhzValues.size(); i > 0; --i ) if( intervalMhzValues[i - 1] >= 0.0 ) return intervalMhzValues[i - 1];
    return -1.0;
}

/*! Each sample's time since the first, in ms. */
inline std::vector<double> sampleMs( const std::vector<unsigned long long> &ns )
{
    std::vector<double> ms;
    for( size_t i = 0; i < ns.size(); ++i )
        ms.push_back( ns[i] > ns[0] ? static_cast<double>( ns[i] - ns[0] ) / 1000000.0 : 0.0 );
    return ms;
}

/*! Kick start -> the end of the first interval at >= kRampFraction of the highest interval MHz; -1 when unknown
 *  (fewer than 2 samples, or no interval above 0). intervalMhz[i] spans sampleMs[i] -> sampleMs[i + 1]; an unknown
 *  (-1, cross-SM) interval is never the peak and never reaches the threshold. */
inline double rampMsTo80pct( const std::vector<double> &sampleMsSinceFirst, const std::vector<double> &intervalMhzValues )
{
    if( sampleMsSinceFirst.size() < 2 || intervalMhzValues.empty() ) return -1.0;
    double peak = 0.0;
    for( double mhz : intervalMhzValues ) if( mhz > peak ) peak = mhz;
    if( peak <= 0.0 ) return -1.0;
    for( size_t i = 0; i < intervalMhzValues.size() && i + 1 < sampleMsSinceFirst.size(); ++i )
    {
        if( intervalMhzValues[i] >= kRampFraction * peak ) return sampleMsSinceFirst[i + 1];
    }
    return -1.0;
}

/*! stopped_by from the backend's report (state 0 collected, 1 pending, 2 none; stopped_by 0 deadline, 1 end,
 *  2 a live run's drain). */
inline const char *stoppedByName( int state, int stoppedBy )
{
    if( state == 1 ) return "pending";
    if( state != 0 ) return "none";
    switch( stoppedBy )
    {
    case 0: return "deadline";
    case 1: return "end";
    case 2: return "amaze_drain";
    }
    return "none";
}

struct Line
{
    unsigned long long session = 0;
    Decision decision;
    int maxMs = kKickMaxMs;
    double ranMs = 0.0;
    int samples = 0;
    double mhzFirst = -1.0;      // < 0: NA
    double mhzAtAmaze0 = -1.0;
    double mhzLast = -1.0;
    double rampMs = -1.0;
    QString stoppedBy = QStringLiteral( "none" );
    QString endedAt = QStringLiteral( "none" );
    double kickLeadMs = -1.0;

    QString format() const
    {
        const auto ms = []( double value ) { return QString::number( value, 'f', 3 ); };
        const auto opt = []( double value, int decimals )
        { return value < 0.0 ? QStringLiteral( "NA" ) : QString::number( value, 'f', decimals ); };
        // concatenated, never .arg()-chained: stopped_by / ended_at are free text
        return QStringLiteral( "playback_gpu_clock_kick session=" ) + QString::number( session )
             + QStringLiteral( " decision=" ) + ( decision.run ? QStringLiteral( "run" ) : QStringLiteral( "skip" ) )
             + QStringLiteral( " reason=" ) + QString::fromLatin1( reasonName( decision.reason ) )
             + QStringLiteral( " max_ms=" ) + QString::number( maxMs )
             + QStringLiteral( " ran_ms=" ) + ms( ranMs )
             + QStringLiteral( " samples=" ) + QString::number( samples )
             + QStringLiteral( " mhz_first=" ) + opt( mhzFirst, 1 )
             + QStringLiteral( " mhz_at_amaze0=" ) + opt( mhzAtAmaze0, 1 )
             + QStringLiteral( " mhz_last=" ) + opt( mhzLast, 1 )
             + QStringLiteral( " ramp_ms_to_80pct=" ) + opt( rampMs, 3 )
             + QStringLiteral( " stopped_by=" ) + ( stoppedBy.isEmpty() ? QStringLiteral( "none" ) : stoppedBy )
             + QStringLiteral( " ended_at=" ) + ( endedAt.isEmpty() ? QStringLiteral( "none" ) : endedAt )
             + QStringLiteral( " kick_lead_ms=" ) + opt( kickLeadMs, 3 );
    }
};

} // namespace playback_gpu_clock_kick

#endif // PLAYBACKGPUCLOCKKICKPOLICY_H
