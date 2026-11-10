/*!
 * \file PlaybackStrideSampler.h
 * \brief A bounded, session-uniform sample of per-present values for the smoke medians and percentiles.
 *
 * HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 (adversary A4 on 2f1ec397, fixed for the whole class): the session
 * medians/percentiles kept the FIRST N values and then stopped, so a session longer than the cap reported a statistic
 * of its prefix while its counts covered the whole session. This keeps every stride-th value instead (population index
 * i is kept iff i % stride == 0); when the store reaches its cap it drops every second kept value and doubles the stride.
 * The kept values are then a uniform sample of the WHOLE session, never more than \a cap of them, and below the cap
 * (every leg today: a 25 s leg is ~1500 presents) it keeps every value exactly as before. Pure, no Qt.
 */

#ifndef PLAYBACKSTRIDESAMPLER_H
#define PLAYBACKSTRIDESAMPLER_H

#include <cstddef>
#include <vector>

namespace playback_sampling
{

class StrideSampler
{
public:
    explicit StrideSampler( size_t cap = 100000 ) : m_cap( cap < 2 ? 2 : cap ) {}

    void add( double v )
    {
        const unsigned long long index = m_seen++;
        if( index % m_stride != 0 ) return;
        m_values.push_back( v );
        if( m_values.size() < m_cap ) return;
        size_t kept = 0;
        for( size_t i = 0; i < m_values.size(); i += 2 ) m_values[kept++] = m_values[i];
        m_values.resize( kept );
        m_stride *= 2;
    }

    const std::vector<double> &values() const { return m_values; }
    size_t size() const { return m_values.size(); }
    bool empty() const { return m_values.empty(); }
    unsigned long long seen() const { return m_seen; }
    unsigned long long stride() const { return m_stride; }
    size_t cap() const { return m_cap; }

private:
    size_t m_cap;
    unsigned long long m_stride = 1;
    unsigned long long m_seen = 0;
    std::vector<double> m_values;
};

} // namespace playback_sampling

#endif // PLAYBACKSTRIDESAMPLER_H
