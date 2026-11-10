/*!
 * \file PlaybackTexturePresentSplit.h
 * \brief HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 K1: the session split of the GUI-thread texture present.
 *
 * After #743, M16-1457 fullscreen s2 slips are gui_late with the frame ready 6-10 ms before its deadline, and the
 * fullscreen draw averages 9.2-11.7 ms with slip-frame draws of 13-19 ms. The per-frame parts of that draw are
 * already timed (gpu_playback_recon_texture_present_* stage keys, GpuPresentTailTiming) but were never summed. This
 * header sums them, per present path, into ONE LIGHT line:
 *
 *   playback_smoke.texture_present_split session=<n> path=<window|viewport> frames=<n> ... avg_<part>_ms= p95_<part>_ms=
 *   slip_avg_<part>_ms=
 *
 * r2 (adversary A1/A2 on 639d7cfc): the design's ahead_candidate_ready_at_present_return field is NOT emitted. It
 * needed a retained-slot count kept beside the slot tokens, which the GUI can read between a token step and its count
 * step and which init() does not conserve; the K1 gates (G1, G2) never read it, and K2 measures its own adoption.
 *
 * Pure and Qt-free (pattern: PlaybackSlipHistogram.h): MainWindow feeds values it already has, so the hot path takes
 * no new clock reads. The session's first present (one-time allocation) is excluded from every count and reported
 * as first_wall_ms.
 *
 * Parts (ms, GUI-thread wall unless noted):
 *   context        makeCurrent before the setup
 *   setup          the whole setup block (LUT, S/H blur upload, realloc, sampling)
 *   setup_lut      its LUT part
 *   setup_blur     its S/H blur parts (drain + upload + check)
 *   amaze_kernel   the AMaZE kernels (GpuAmazeDebayerBackendTiming::kernelMs)
 *   amaze_handoff  the CUDA->GL hand-off (register/map/copy/unmap; GpuAmazeDebayerBackendTiming::downloadMs)
 *   amaze_wall     the GUI-thread wall of the whole AMaZE call
 *   recon_wall     the GUI-thread wall of the recon hand-off before AMaZE
 *   paint          the paint-per-submit paintGL (window path only)
 *   swap           the paint-per-submit swapBuffers call (window path only)
 *   post           everything after AMaZE (includes paint + swap)
 *   wall           the presenter call's whole wall
 *   host_gap       wall minus the backend's timed total
 *   tail_window    previous present's swap return -> this setup (only when the window is known)
 *   draw_total     drawFrameReady's whole draw for this present
 */

#ifndef PLAYBACKTEXTUREPRESENTSPLIT_H
#define PLAYBACKTEXTUREPRESENTSPLIT_H

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdio>
#include <string>
#include <vector>

#include "PlaybackStrideSampler.h"

namespace playback_texture_split
{

inline constexpr int kMaxSamples = 100000;   // per part, per path: the sampler's cap (it strides beyond it, A4)

enum class Part
{
    Context = 0, Setup, SetupLut, SetupBlur, AmazeKernel, AmazeHandoff, AmazeWall, ReconWall,
    Paint, Swap, Post, Wall, HostGap, TailWindow, DrawTotal, Count
};
inline constexpr int kPartCount = static_cast<int>( Part::Count );

inline const char *partName( Part p )
{
    switch( p )
    {
    case Part::Context: return "context";
    case Part::Setup: return "setup";
    case Part::SetupLut: return "setup_lut";
    case Part::SetupBlur: return "setup_blur";
    case Part::AmazeKernel: return "amaze_kernel";
    case Part::AmazeHandoff: return "amaze_handoff";
    case Part::AmazeWall: return "amaze_wall";
    case Part::ReconWall: return "recon_wall";
    case Part::Paint: return "paint";
    case Part::Swap: return "swap";
    case Part::Post: return "post";
    case Part::Wall: return "wall";
    case Part::HostGap: return "host_gap";
    case Part::TailWindow: return "tail_window";
    case Part::DrawTotal: return "draw_total";
    default: return "none";
    }
}

enum class Path { Window = 0, Viewport, Count };

inline const char *pathName( Path p )
{
    return p == Path::Window ? "window" : p == Path::Viewport ? "viewport" : "none";
}

/*! Adversary A3 on 2f1ec397: a present enters the split only when the AMaZE texture presenter itself drew it. A
 *  presenter that ran and FAILED still leaves its setup keys behind, and a later fallback (the bilinear R16 texture or
 *  the readback RGB16 present) then marks the frame presented: those parts describe a failed attempt, not this
 *  present. \a setupTimingKnown: the setup keys are in the frame's telemetry; \a amazeTexturePresented:
 *  gpu_playback_recon_amaze_texture_present_active (the AMaZE presenter's own return). */
inline bool sampleEligible( bool setupTimingKnown, bool amazeTexturePresented )
{
    return setupTimingKnown && amazeTexturePresented;
}

/*! One texture present, as MainWindow has it after the presenter returned. */
struct Sample
{
    std::array<double, kPartCount> ms {};
    bool tailWindowKnown = false;       // TailWindow holds a measured window (else it is not counted)
    bool painted = false;               // paint-per-submit ran (Paint/Swap are real)
    bool reconInFlight = false;         // a worker recon ran at some point during this present
    Path path = Path::Window;

    double &at( Part p ) { return ms[static_cast<size_t>( p )]; }
    double at( Part p ) const { return ms[static_cast<size_t>( p )]; }
};

/*! The parts summed over a set of presents (the slip frames); avg = sum / events. */
struct PartSums
{
    std::array<double, kPartCount> ms {};
    int events = 0;
    int tailWindowEvents = 0;           // events whose TailWindow is known (TailWindow's divisor)
    int reconInFlight = 0;

    void add( const Sample &s )
    {
        for( int i = 0; i < kPartCount; ++i )
        {
            if( static_cast<Part>( i ) == Part::TailWindow && !s.tailWindowKnown ) continue;
            ms[static_cast<size_t>( i )] += s.ms[static_cast<size_t>( i )];
        }
        ++events;
        if( s.tailWindowKnown ) ++tailWindowEvents;
        if( s.reconInFlight ) ++reconInFlight;
    }

    double avg( Part p ) const
    {
        const int n = p == Part::TailWindow ? tailWindowEvents : events;
        return n > 0 ? ms[static_cast<size_t>( p )] / n : 0.0;
    }
};

/*! Nearest-rank percentile \a q in [0, 1] of \a v (0 when empty). */
inline double percentile( std::vector<double> v, double q )
{
    if( v.empty() ) return 0.0;
    std::sort( v.begin(), v.end() );
    const double rank = q * static_cast<double>( v.size() );
    size_t idx = static_cast<size_t>( rank );
    if( static_cast<double>( idx ) < rank ) ++idx;   // ceil
    if( idx > 0 ) --idx;                              // 1-based rank -> index
    if( idx >= v.size() ) idx = v.size() - 1;
    return v[idx];
}

/*! One path's session. */
class PathAggregator
{
public:
    /*! Returns whether \a s was counted (false for the path's first present). Fix 2 (adversary A3 on 3390ee51): the
     *  caller hands ONLY counted samples to the slip histogram, so the excluded first present is in no slip sum. */
    bool note( const Sample &s )
    {
        if( !m_firstSeen )
        {
            m_firstSeen = true;
            m_firstWallMs = s.at( Part::Wall );
            return false;
        }
        m_sums.add( s );
        if( s.painted ) ++m_painted;
        for( int i = 0; i < kPartCount; ++i )
        {
            if( static_cast<Part>( i ) == Part::TailWindow && !s.tailWindowKnown ) continue;
            m_values[static_cast<size_t>( i )].add( s.ms[static_cast<size_t>( i )] );
        }
        return true;
    }

    bool any() const { return m_firstSeen; }
    int frames() const { return m_sums.events; }
    int painted() const { return m_painted; }
    int reconInFlight() const { return m_sums.reconInFlight; }
    int tailWindowKnown() const { return m_sums.tailWindowEvents; }
    double firstWallMs() const { return m_firstWallMs; }
    double avg( Part p ) const { return m_sums.avg( p ); }
    double p95( Part p ) const { return percentile( m_values[static_cast<size_t>( p )].values(), 0.95 ); }
    double median( Part p ) const { return percentile( m_values[static_cast<size_t>( p )].values(), 0.50 ); }
    /*! The draw_total sampler's stride: 1 = every present is in the percentiles (A4). */
    unsigned long long percentileStride() const
    {
        return m_values[static_cast<size_t>( Part::DrawTotal )].stride();
    }

private:
    static std::array<playback_sampling::StrideSampler, kPartCount> makeSamplers()
    {
        std::array<playback_sampling::StrideSampler, kPartCount> a;
        for( auto &s : a ) s = playback_sampling::StrideSampler( static_cast<size_t>( kMaxSamples ) );
        return a;
    }

    bool m_firstSeen = false;
    double m_firstWallMs = 0.0;
    PartSums m_sums;
    int m_painted = 0;
    std::array<playback_sampling::StrideSampler, kPartCount> m_values = makeSamplers();
};

/*! The session: one aggregator per path. */
class Aggregator
{
public:
    void reset() { *this = Aggregator(); }
    bool note( const Sample &s )
    {
        if( s.path == Path::Count ) return false;
        return m_paths[static_cast<size_t>( s.path )].note( s );
    }
    const PathAggregator &path( Path p ) const { return m_paths[static_cast<size_t>( p )]; }

private:
    std::array<PathAggregator, static_cast<int>( Path::Count )> m_paths;
};

inline void appendMs( std::string &out, const char *prefix, Part p, double v )
{
    char buf[96];
    std::snprintf( buf, sizeof( buf ), " %s%s_ms=%.3f", prefix, partName( p ), v );
    out += buf;
}

inline void appendInt( std::string &out, const char *key, long long v )
{
    char buf[96];
    std::snprintf( buf, sizeof( buf ), " %s=%lld", key, v );
    out += buf;
}

/*! The LIGHT line for one path. \a guiLateSlips sums the parts of this path's gui_late slip presents (the
 *  frames G1 asks about); \a allSlips the parts of every slip present of this path. */
inline std::string formatLine( unsigned long long session, Path path, const PathAggregator &agg,
                               const PartSums &guiLateSlips, const PartSums &allSlips )
{
    std::string out = "playback_smoke.texture_present_split";
    appendInt( out, "session", static_cast<long long>( session ) );
    out += " path=";
    out += pathName( path );
    appendInt( out, "frames", agg.frames() );
    appendInt( out, "painted", agg.painted() );
    appendInt( out, "tail_window_known", agg.tailWindowKnown() );
    appendInt( out, "recon_in_flight", agg.reconInFlight() );
    appendInt( out, "pct_stride", static_cast<long long>( agg.percentileStride() ) );
    {
        char buf[64];
        std::snprintf( buf, sizeof( buf ), " first_wall_ms=%.3f", agg.firstWallMs() );
        out += buf;
        std::snprintf( buf, sizeof( buf ), " median_draw_total_ms=%.3f", agg.median( Part::DrawTotal ) );
        out += buf;
    }
    for( int i = 0; i < kPartCount; ++i ) appendMs( out, "avg_", static_cast<Part>( i ), agg.avg( static_cast<Part>( i ) ) );
    for( int i = 0; i < kPartCount; ++i ) appendMs( out, "p95_", static_cast<Part>( i ), agg.p95( static_cast<Part>( i ) ) );
    appendInt( out, "slip_events", guiLateSlips.events );
    appendInt( out, "slip_recon_in_flight", guiLateSlips.reconInFlight );
    for( int i = 0; i < kPartCount; ++i )
        appendMs( out, "slip_avg_", static_cast<Part>( i ), guiLateSlips.avg( static_cast<Part>( i ) ) );
    appendInt( out, "any_slip_events", allSlips.events );
    appendMs( out, "any_slip_avg_", Part::DrawTotal, allSlips.avg( Part::DrawTotal ) );
    appendMs( out, "any_slip_avg_", Part::AmazeKernel, allSlips.avg( Part::AmazeKernel ) );
    appendMs( out, "any_slip_avg_", Part::AmazeHandoff, allSlips.avg( Part::AmazeHandoff ) );
    return out;
}

/*! The per-slip fields appended to each playback_smoke.slip line (\a known false: the slip's present ran no
 *  texture presenter, and every field reads 0 / -1). */
inline std::string formatSlipFields( bool known, const Sample &s )
{
    char buf[320];
    std::snprintf( buf, sizeof( buf ),
                   " amaze_kernel_ms=%.3f amaze_handoff_ms=%.3f amaze_wall_ms=%.3f present_setup_ms=%.3f"
                   " present_paint_ms=%.3f present_swap_ms=%.3f present_wall_ms=%.3f recon_in_flight=%d"
                   " present_path=%s",
                   known ? s.at( Part::AmazeKernel ) : 0.0,
                   known ? s.at( Part::AmazeHandoff ) : 0.0,
                   known ? s.at( Part::AmazeWall ) : 0.0,
                   known ? s.at( Part::Setup ) : 0.0,
                   known ? s.at( Part::Paint ) : 0.0,
                   known ? s.at( Part::Swap ) : 0.0,
                   known ? s.at( Part::Wall ) : 0.0,
                   known ? ( s.reconInFlight ? 1 : 0 ) : -1,
                   known ? pathName( s.path ) : "none" );
    return buf;
}

} // namespace playback_texture_split

#endif // PLAYBACKTEXTUREPRESENTSPLIT_H
