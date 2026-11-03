#pragma once

/*! HFR-NATIVE-M16-1457-GUI-PRESENT-CADENCE-1 r4 (Dot B1): bounded storage for the due wake's per-Play sample sets (lateness,
 *  due-to-post, post-to-receive). Every ordinary Play is a playback_smoke session (beginPlaybackSmokeTelemetry runs on every
 *  Play start), so there is no measurement-only condition to gate on, and an unbounded vector grew without limit during a
 *  looping Play (about 4.9 MiB/h at 60 wakes/s). Here memory is O(1) in the length of the Play:
 *  - up to kExactCapacity samples are kept exactly, so a measured Play's percentiles are the ones the unbounded vector gave
 *    (same nearest-rank formula, same nth_element selection), in range or not;
 *  - past that, the samples move into a fixed-bin histogram, kBinMs wide over [kMinMs, kMaxMs);
 *  - reset() releases both buffers' capacity (clear() alone kept it).
 *
 *  r5 (Dot B2): the r4 histogram clamped an out-of-range sample into its edge bin, so 16,385 wakes of 500 ms read p50 = p95 =
 *  79.995 ms, a measured-looking value. Now:
 *  - a sample below kMinMs (or NaN, which is never in range) counts in underflow(), one at or above kMaxMs in overflow(); they
 *    are never put in a bin, and the two counts cover every sample of the Play (exact or binned);
 *  - once binned, a rank that lands in the underflow or overflow is CENSORED: rank() says so, percentile() returns NaN (never
 *    a bound that reads as measured), and playbackDueWakePercentileText() prints "<=-20.000" / ">=80.000";
 *  - the bin-centre accuracy (within kBinMs / 2 of the exact rank) holds for in-range ranks only;
 *  - playbackDueWakeRangeFields() is each set's disclosure on the summary line (exact, underflow, overflow, range). */

#include <QString>

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <limits>
#include <vector>

class PlaybackDueWakeSamples
{
public:
    static constexpr std::size_t kExactCapacity = 16384;   // 4.5 min of wakes at 60 fps, kept exactly
    static constexpr double kMinMs = -20.0;                // histogram range [kMinMs, kMaxMs)
    static constexpr double kMaxMs = 80.0;
    static constexpr double kBinMs = 0.01;
    static constexpr std::size_t kBins = 10000;            // ( kMaxMs - kMinMs ) / kBinMs

    //! r5 adversary pass 8 A1: a bin counts as many samples as the Play does (a uint32_t bin wrapped to 0 after 2^32 samples
    //! of one value, and the rank scan then fell through to a false overflow). 10,000 x 8 B stays under the exact store's size.
    using BinCount = std::uint64_t;
    static_assert( sizeof( BinCount ) >= sizeof( std::size_t ), "a bin must hold every sample of the Play" );

    //! a binned rank outside [kMinMs, kMaxMs) is censored: only its side of the range is known
    enum class Censor { None, Underflow, Overflow };
    struct Rank
    {
        double ms;       // the measured value (exact store), the bin centre (in range), or the crossed bound (censored)
        Censor censor;
    };

    void add( double ms )
    {
        ++m_count;
        const Censor side = sideOf( ms );
        if( side == Censor::Underflow ) ++m_underflow;
        else if( side == Censor::Overflow ) ++m_overflow;
        if( m_histogram.empty() )
        {
            if( m_exact.size() < kExactCapacity )
            {
                if( m_exact.size() == m_exact.capacity() )
                    m_exact.reserve( std::min( kExactCapacity, std::max<std::size_t>( 64, m_exact.capacity() * 2 ) ) );
                m_exact.push_back( ms );
                return;
            }
            // the exact store is full: spill its in-range samples into the histogram (the out-of-range ones are already in
            // underflow / overflow), and release it
            m_histogram.assign( kBins, 0u );
            for( const double sample : m_exact )
                if( sideOf( sample ) == Censor::None ) ++m_histogram[binOf( sample )];
            std::vector<double>().swap( m_exact );
        }
        if( side == Censor::None ) ++m_histogram[binOf( ms )];
    }

    //! \a n copies of \a ms: the same state as n add( ms ) calls, in O( min( n, kExactCapacity ) ) once past the exact store
    void addRepeated( double ms, std::size_t n )
    {
        while( n > 0 && m_histogram.empty() ) { add( ms ); --n; }
        if( n == 0 ) return;
        m_count += n;
        const Censor side = sideOf( ms );
        if( side == Censor::Underflow ) m_underflow += n;
        else if( side == Censor::Overflow ) m_overflow += n;
        else m_histogram[binOf( ms )] += n;
    }

    //! nearest-rank rank, \a p in [0, 1]; { 0.0, None } with no samples (the summary line's old convention)
    Rank rank( double p ) const
    {
        if( m_count == 0 ) return { 0.0, Censor::None };
        const std::size_t index = static_cast<std::size_t>( std::floor( p * static_cast<double>( m_count - 1 ) + 0.5 ) );
        if( m_histogram.empty() )
        {
            std::vector<double> values( m_exact );
            std::nth_element( values.begin(), values.begin() + static_cast<std::ptrdiff_t>( index ), values.end() );
            return { values[index], Censor::None };
        }
        // ascending order: underflow, the bins, overflow
        if( index < m_underflow ) return { kMinMs, Censor::Underflow };
        std::size_t seen = m_underflow;
        for( std::size_t bin = 0; bin < kBins; ++bin )
        {
            seen += m_histogram[bin];
            if( seen > index ) return { kMinMs + ( static_cast<double>( bin ) + 0.5 ) * kBinMs, Censor::None };
        }
        return { kMaxMs, Censor::Overflow };
    }

    //! the measured percentile, or NaN when the rank is censored (never a clamped bound)
    double percentile( double p ) const
    {
        const Rank r = rank( p );
        return r.censor == Censor::None ? r.ms : std::numeric_limits<double>::quiet_NaN();
    }

    std::size_t size() const { return m_count; }
    bool exact() const { return m_histogram.empty(); }
    //! samples below kMinMs (or NaN) / at or above kMaxMs, over the whole Play
    std::size_t underflow() const { return m_underflow; }
    std::size_t overflow() const { return m_overflow; }
    //! bytes held by the two buffers (their capacity, not their size)
    std::size_t storageBytes() const
    {
        return m_exact.capacity() * sizeof( double ) + m_histogram.capacity() * sizeof( BinCount );
    }
    static constexpr std::size_t maxStorageBytes()
    {
        return kExactCapacity * sizeof( double ) > kBins * sizeof( BinCount )
            ? kExactCapacity * sizeof( double )
            : kBins * sizeof( BinCount );
    }

    void reset()
    {
        std::vector<double>().swap( m_exact );
        std::vector<BinCount>().swap( m_histogram );
        m_count = 0;
        m_underflow = 0;
        m_overflow = 0;
    }

private:
    static Censor sideOf( double ms )
    {
        if( !( ms >= kMinMs ) ) return Censor::Underflow;   // also NaN: never in range
        if( ms >= kMaxMs ) return Censor::Overflow;
        return Censor::None;
    }

    //! an in-range sample's bin (the clamp only absorbs the floating-point rounding just below kMaxMs)
    static std::size_t binOf( double ms )
    {
        const double bin = std::floor( ( ms - kMinMs ) / kBinMs );
        if( !( bin > 0.0 ) ) return 0;
        if( bin >= static_cast<double>( kBins - 1 ) ) return kBins - 1;
        return static_cast<std::size_t>( bin );
    }

    std::vector<double> m_exact;
    std::vector<BinCount> m_histogram;
    std::size_t m_count = 0;
    std::size_t m_underflow = 0;
    std::size_t m_overflow = 0;
};

//! a percentile field's value on the playback_due_wake line: the measured value to 3 decimals, or the CENSORED bound with
//! its marker ("<=-20.000" below the range, ">=80.000" at or above it) -- never a clamped value that reads as measured
inline QString playbackDueWakePercentileText( const PlaybackDueWakeSamples &samples, double p )
{
    const PlaybackDueWakeSamples::Rank r = samples.rank( p );
    if( r.censor == PlaybackDueWakeSamples::Censor::Underflow )
        return QStringLiteral( "<=%1" ).arg( PlaybackDueWakeSamples::kMinMs, 0, 'f', 3 );
    if( r.censor == PlaybackDueWakeSamples::Censor::Overflow )
        return QStringLiteral( ">=%1" ).arg( PlaybackDueWakeSamples::kMaxMs, 0, 'f', 3 );
    return QStringLiteral( "%1" ).arg( r.ms, 0, 'f', 3 );   // the r4 line's own formatting
}

//! one set's disclosure on the playback_due_wake line: whether its percentiles are exact, how many samples fell below / at
//! or above the histogram range, and that range
inline QString playbackDueWakeRangeFields( const PlaybackDueWakeSamples &samples, const char *set )
{
    const QString name = QLatin1String( set );
    return QStringLiteral( " %1_exact=%2 %1_underflow=%3 %1_overflow=%4 %1_range_ms=%5:%6" )
        .arg( name )
        .arg( samples.exact() ? 1 : 0 )
        .arg( static_cast<qulonglong>( samples.underflow() ) )
        .arg( static_cast<qulonglong>( samples.overflow() ) )
        .arg( PlaybackDueWakeSamples::kMinMs, 0, 'f', 3 )
        .arg( PlaybackDueWakeSamples::kMaxMs, 0, 'f', 3 );
}
