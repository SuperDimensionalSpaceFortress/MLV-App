#pragma once

/*! HFR-NATIVE-M16-1457-GUI-PRESENT-CADENCE-1 r4 (Dot B1): bounded storage for the due wake's per-Play sample sets (lateness,
 *  due-to-post, post-to-receive). Every ordinary Play is a playback_smoke session (beginPlaybackSmokeTelemetry runs on every
 *  Play start), so there is no measurement-only condition to gate on, and an unbounded vector grew without limit during a
 *  looping Play (about 4.9 MiB/h at 60 wakes/s). Here memory is O(1) in the length of the Play:
 *  - up to kExactCapacity samples are kept exactly, so a measured Play's percentiles are the ones the unbounded vector gave
 *    (same nearest-rank formula, same nth_element selection);
 *  - past that, the samples move into a fixed-bin histogram (kBinMs wide over [kMinMs, kMaxMs), the edge bins clamp), and the
 *    percentile is the centre of the bin that holds the rank (within kBinMs / 2 of the exact value inside the range);
 *  - reset() releases both buffers' capacity (clear() alone kept it). */

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <cstdint>
#include <vector>

class PlaybackDueWakeSamples
{
public:
    static constexpr std::size_t kExactCapacity = 16384;   // 4.5 min of wakes at 60 fps, kept exactly
    static constexpr double kMinMs = -20.0;
    static constexpr double kMaxMs = 80.0;
    static constexpr double kBinMs = 0.01;
    static constexpr std::size_t kBins = 10000;            // ( kMaxMs - kMinMs ) / kBinMs

    void add( double ms )
    {
        ++m_count;
        if( m_histogram.empty() )
        {
            if( m_exact.size() < kExactCapacity )
            {
                if( m_exact.size() == m_exact.capacity() )
                    m_exact.reserve( std::min( kExactCapacity, std::max<std::size_t>( 64, m_exact.capacity() * 2 ) ) );
                m_exact.push_back( ms );
                return;
            }
            // the exact store is full: spill it into the histogram, and release it
            m_histogram.assign( kBins, 0u );
            for( const double sample : m_exact ) ++m_histogram[binOf( sample )];
            std::vector<double>().swap( m_exact );
        }
        ++m_histogram[binOf( ms )];
    }

    //! nearest-rank percentile, \a p in [0, 1]; 0.0 with no samples (the summary line's old convention)
    double percentile( double p ) const
    {
        if( m_count == 0 ) return 0.0;
        const std::size_t index = static_cast<std::size_t>( std::floor( p * static_cast<double>( m_count - 1 ) + 0.5 ) );
        if( m_histogram.empty() )
        {
            std::vector<double> values( m_exact );
            std::nth_element( values.begin(), values.begin() + static_cast<std::ptrdiff_t>( index ), values.end() );
            return values[index];
        }
        std::size_t seen = 0;
        for( std::size_t bin = 0; bin < kBins; ++bin )
        {
            seen += m_histogram[bin];
            if( seen > index ) return kMinMs + ( static_cast<double>( bin ) + 0.5 ) * kBinMs;
        }
        return kMaxMs;
    }

    std::size_t size() const { return m_count; }
    bool exact() const { return m_histogram.empty(); }
    //! bytes held by the two buffers (their capacity, not their size)
    std::size_t storageBytes() const
    {
        return m_exact.capacity() * sizeof( double ) + m_histogram.capacity() * sizeof( std::uint32_t );
    }
    static constexpr std::size_t maxStorageBytes()
    {
        return kExactCapacity * sizeof( double ) > kBins * sizeof( std::uint32_t )
            ? kExactCapacity * sizeof( double )
            : kBins * sizeof( std::uint32_t );
    }

    void reset()
    {
        std::vector<double>().swap( m_exact );
        std::vector<std::uint32_t>().swap( m_histogram );
        m_count = 0;
    }

private:
    static std::size_t binOf( double ms )
    {
        if( !( ms > kMinMs ) ) return 0;   // also NaN
        const double bin = std::floor( ( ms - kMinMs ) / kBinMs );
        if( bin >= static_cast<double>( kBins - 1 ) ) return kBins - 1;
        return static_cast<std::size_t>( bin );
    }

    std::vector<double> m_exact;
    std::vector<std::uint32_t> m_histogram;
    std::size_t m_count = 0;
};
