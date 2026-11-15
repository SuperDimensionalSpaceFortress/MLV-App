/*!
 * \file GuiHopWaitSplit.h
 * \brief CPU-S1-PRESENT-INTERVAL-BOUND-1: splits a GUI-thread wait window [a, b] (seconds on
 * mlv_stage_timing_now(), the clock of RenderFrameThread::ReadyFrame::frameReadyEmitStageTime) into the
 * GUI work that ran inside it: the smoke runner's own sleep, a worker prep present
 * (presentPlaybackPreparedFrame) and a preview viewport paint, plus "other" (the rest of the window).
 * Read by playback_smoke.critical_hop_summary. Pure: the caller passes every clock reading.
 *
 * GUI thread only. Each kind keeps its last kRingCapacity closed intervals in a fixed ring (no
 * allocation per frame). Kinds can nest (a paint run inside a present), so a window's time is charged
 * EXCLUSIVELY, to the first kind in kChargeOrder that covers it: runner sleep, then viewport paint, then
 * prepared present. The kinds and other therefore sum to the window (closure).
 *
 * A window that starts before the oldest interval a kind still holds, once that kind's ring has
 * dropped an interval, may have lost an interval of that kind: such a window is UNATTRIBUTED. It is
 * counted on its own and never guessed into the averages.
 */

#ifndef GUIHOPWAITSPLIT_H
#define GUIHOPWAITSPLIT_H

#include <cmath>

namespace gui_hop_wait
{

enum class Kind
{
    RunnerSleep = 0,
    PreparedPresent,
    ViewportPaint,
    Count
};

constexpr int kKinds = static_cast<int>( Kind::Count );
constexpr int kRingCapacity = 16;
// r1 fix iteration 1 (advisory A4 on 6a6d7ef4): a window or sample longer than this (about 11.6 days) is not a
// GUI hop; it is refused, so no session sum can overflow to infinity.
constexpr double kMaxSampleMs = 1.0e9;

constexpr unsigned kindBit( Kind kind ) { return 1u << static_cast<unsigned>( kind ); }
constexpr unsigned kAllKinds = ( 1u << kKinds ) - 1u;

// The exclusive charging order when kinds overlap (see the file comment).
constexpr Kind kChargeOrder[kKinds] = { Kind::RunnerSleep, Kind::ViewportPaint, Kind::PreparedPresent };

struct Interval
{
    double startS = 0.0;
    double endS = 0.0;
};

// One kind's last kRingCapacity closed intervals, oldest first by push order.
class Ring
{
public:
    // A closed interval; one that is not finite or ends before it starts is ignored.
    void push( double startS, double endS )
    {
        if( !std::isfinite( startS ) || !std::isfinite( endS ) || endS < startS ) return;
        const int slot = ( m_first + m_count ) % kRingCapacity;
        if( m_count == kRingCapacity )
        {
            m_first = ( m_first + 1 ) % kRingCapacity;   // the oldest is dropped
            m_overflowed = true;
        }
        else
        {
            ++m_count;
        }
        m_items[slot].startS = startS;
        m_items[slot].endS = endS;
    }
    int count( void ) const { return m_count; }
    bool overflowed( void ) const { return m_overflowed; }
    const Interval &at( int i ) const { return m_items[( m_first + i ) % kRingCapacity]; }
    // The earliest start among the retained intervals (push order is end order; a long interval
    // pushed late can start before an earlier short one).
    double oldestStartS( void ) const
    {
        double oldest = m_count > 0 ? at( 0 ).startS : 0.0;
        for( int i = 1; i < m_count; ++i )
            if( at( i ).startS < oldest ) oldest = at( i ).startS;
        return oldest;
    }
    void reset( void ) { *this = Ring(); }

private:
    Interval m_items[kRingCapacity];
    int m_first = 0;
    int m_count = 0;
    bool m_overflowed = false;
};

struct Split
{
    bool valid = false;          // false: a or b not finite, or b < a (nothing is accumulated)
    bool unattributed = false;   // see the file comment
    double waitMs = 0.0;
    double kindMs[kKinds] = {};
    double otherMs = 0.0;
};

class Recorder
{
public:
    void push( Kind kind, double startS, double endS )
    {
        const int k = static_cast<int>( kind );
        if( k < 0 || k >= kKinds ) return;
        m_rings[k].push( startS, endS );
    }
    // r1 fix iteration 2 (adversary A1 on df1b864e): an interval still OPEN when a window closes inside it (a present
    // whose own finishPresentedFrame drains the next frame's drawFrameReady before it returns) covers the window from
    // its start; it is pushed as a closed interval only when it ends. Nested opens of one kind keep the outermost start.
    // False (nothing opened, so the caller must not close) for a bad kind or a start that is not finite.
    bool open( Kind kind, double startS )
    {
        const int k = static_cast<int>( kind );
        if( k < 0 || k >= kKinds || !std::isfinite( startS ) ) return false;
        if( m_openDepth[k]++ == 0 ) m_openStartS[k] = startS;
        return true;
    }
    void close( Kind kind, double startS, double endS )
    {
        const int k = static_cast<int>( kind );
        if( k < 0 || k >= kKinds ) return;
        if( m_openDepth[k] > 0 ) --m_openDepth[k];
        m_rings[k].push( startS, endS );
    }
    int openDepth( Kind kind ) const { return m_openDepth[static_cast<int>( kind )]; }
    const Ring &ring( Kind kind ) const { return m_rings[static_cast<int>( kind )]; }
    void reset( void )
    {
        for( Ring &r : m_rings ) r.reset();
        for( int k = 0; k < kKinds; ++k )
        {
            m_openDepth[k] = 0;
            m_openStartS[k] = 0.0;
        }
    }

    // The window [a, b] split by the kinds in kindMask (a kind outside the mask is charged to other).
    Split split( double a, double b, unsigned kindMask = kAllKinds ) const
    {
        Split s;
        if( !std::isfinite( a ) || !std::isfinite( b ) || b < a ) return s;
        const double waitMs = ( b - a ) * 1000.0;
        if( !std::isfinite( waitMs ) || waitMs > kMaxSampleMs ) return s;
        s.valid = true;
        s.waitMs = waitMs;
        for( int k = 0; k < kKinds; ++k )
        {
            if( !( kindMask & ( 1u << k ) ) ) continue;
            const Ring &r = m_rings[k];
            if( r.overflowed() && r.count() > 0 && a < r.oldestStartS() ) s.unattributed = true;
        }
        if( b == a ) return s;

        // Every retained interval (and an open one, up to b) clipped to the window: its ends cut the window into
        // pieces, and each piece goes to the first kind in kChargeOrder covering it (fixed arrays, no allocation).
        double points[2 * kKinds * ( kRingCapacity + 1 ) + 2];
        int n = 0;
        points[n++] = a;
        points[n++] = b;
        for( int k = 0; k < kKinds; ++k )
        {
            if( !( kindMask & ( 1u << k ) ) ) continue;
            const Ring &r = m_rings[k];
            for( int i = 0; i < r.count(); ++i )
            {
                const double lo = r.at( i ).startS > a ? r.at( i ).startS : a;
                const double hi = r.at( i ).endS < b ? r.at( i ).endS : b;
                if( hi <= lo ) continue;
                points[n++] = lo;
                points[n++] = hi;
            }
            if( m_openDepth[k] > 0 && m_openStartS[k] < b )
                points[n++] = m_openStartS[k] > a ? m_openStartS[k] : a;
        }
        for( int i = 1; i < n; ++i )   // insertion sort: n <= 104
        {
            const double v = points[i];
            int j = i - 1;
            while( j >= 0 && points[j] > v )
            {
                points[j + 1] = points[j];
                --j;
            }
            points[j + 1] = v;
        }
        double chargedMs = 0.0;
        for( int i = 0; i + 1 < n; ++i )
        {
            const double lo = points[i];
            const double hi = points[i + 1];
            if( hi <= lo ) continue;
            for( const Kind kind : kChargeOrder )
            {
                const int k = static_cast<int>( kind );
                if( !( kindMask & ( 1u << k ) ) ) continue;
                if( covers( k, lo, hi ) )
                {
                    const double ms = ( hi - lo ) * 1000.0;
                    s.kindMs[k] += ms;
                    chargedMs += ms;
                    break;
                }
            }
        }
        const double other = s.waitMs - chargedMs;
        s.otherMs = other > 0.0 ? other : 0.0;
        return s;
    }

private:
    // Whether kind k covers the piece [lo, hi]. Every interval end is a cut point, so an interval either holds the
    // whole piece or none of its interior: compare the ends, never a midpoint (r1 fix iteration 2, advisory A4 on
    // df1b864e: a one-ulp piece's midpoint rounds onto its start and a strict test missed it).
    bool covers( int k, double lo, double hi ) const
    {
        const Ring &r = m_rings[k];
        for( int i = 0; i < r.count(); ++i )
            if( r.at( i ).startS <= lo && hi <= r.at( i ).endS ) return true;
        return m_openDepth[k] > 0 && m_openStartS[k] <= lo;
    }

    Ring m_rings[kKinds];
    int m_openDepth[kKinds] = {};
    double m_openStartS[kKinds] = {};
};

// Session sums of attributed windows; an unattributed window is only counted.
struct Accumulator
{
    unsigned long long frames = 0;
    unsigned long long unattributedFrames = 0;
    double waitSumMs = 0.0;
    double kindSumMs[kKinds] = {};
    double otherSumMs = 0.0;

    void add( const Split &s )
    {
        if( !s.valid ) return;
        if( s.unattributed )
        {
            ++unattributedFrames;
            return;
        }
        ++frames;
        waitSumMs += s.waitMs;
        for( int k = 0; k < kKinds; ++k ) kindSumMs[k] += s.kindMs[k];
        otherSumMs += s.otherMs;
    }
    double avg( double sum ) const { return frames > 0 ? sum / static_cast<double>( frames ) : 0.0; }
    double avgWaitMs( void ) const { return avg( waitSumMs ); }
    double avgKindMs( Kind kind ) const { return avg( kindSumMs[static_cast<int>( kind )] ); }
    double avgOtherMs( void ) const { return avg( otherSumMs ); }
    void reset( void ) { *this = Accumulator(); }
};

// A plain mean of one duration per sample (emit_to_request_issue).
struct MeanMs
{
    unsigned long long count = 0;
    double sumMs = 0.0;
    void add( double ms )
    {
        if( !std::isfinite( ms ) || ms < 0.0 || ms > kMaxSampleMs ) return;
        ++count;
        sumMs += ms;
    }
    double avg( void ) const { return count > 0 ? sumMs / static_cast<double>( count ) : 0.0; }
    void reset( void ) { *this = MeanMs(); }
};

// Opens `kind` at startS on construction (a window closing while it runs is covered from startS) and pushes
// [startS, clock()] as a closed interval when it leaves scope; a null recorder records nothing.
class IntervalScope
{
public:
    using Clock = double ( * )( void );
    IntervalScope( Recorder *recorder, Kind kind, double startS, Clock clock )
        : m_recorder( recorder && clock && recorder->open( kind, startS ) ? recorder : nullptr ),
          m_kind( kind ), m_startS( startS ), m_clock( clock ) {}
    ~IntervalScope()
    {
        if( m_recorder ) m_recorder->close( m_kind, m_startS, m_clock() );
    }
    IntervalScope( const IntervalScope & ) = delete;
    IntervalScope &operator=( const IntervalScope & ) = delete;

private:
    Recorder *m_recorder;
    Kind m_kind;
    double m_startS;
    Clock m_clock;
};

/* CPU-S1-GUI-HOP-SLEEP-AND-PRESENT-1 r1 fix iteration 1 (adversary A1 on cf09566f): a bounded smoke event wait
 * (MainWindow::waitGuiThreadForSmokeEvents) timed out when its timer event was delivered, OR when the wait itself
 * lasted its bound, less the precise timer's 1 ms resolution. On Windows the precise timer's callback POSTS its
 * QTimerEvent and the dispatcher can return on that wake-up before delivering it, so "the timer is still active"
 * alone does not mean an event ended the wait. */
inline bool smokeEventWaitTimedOut( bool timerStillActive, long long waitedNs, int maxMs )
{
    if( !timerStillActive ) return true;
    const long long boundNs = static_cast<long long>( maxMs > 1 ? maxMs - 1 : 0 ) * 1000000LL;
    return waitedNs >= boundNs;
}

/* (adversary A2 on cf09566f): a present's legacy uiLat (m_lastDrawFrameReadyQueueMs, read at present accounting)
 * was overwritten when ANOTHER request's drawFrameReady wrote it after this present's own did -- even with an equal
 * value -- or when its value differs from the present's own window. */
inline bool presentUiLatencyOverwritten( double ownWindowMs, double legacyMs,
                                         unsigned long long legacyWriterSerial, unsigned long long presentSerial )
{
    return legacyWriterSerial != presentSerial || std::fabs( ownWindowMs - legacyMs ) > 1e-6;
}

} // namespace gui_hop_wait

#endif // GUIHOPWAITSPLIT_H
