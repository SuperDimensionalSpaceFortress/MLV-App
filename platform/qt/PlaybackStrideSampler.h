/*!
 * \file PlaybackStrideSampler.h
 * \brief A bounded, session-uniform sample of per-present values for the smoke medians and percentiles.
 *
 * HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 (adversary A4 on 2f1ec397, fixed for the whole class): the session
 * medians/percentiles kept the FIRST N values and then stopped, so a session longer than the cap reported a statistic
 * of its prefix while its counts covered the whole session.
 *
 * r2 (adversary A1 on b5f2308e): the first fix kept every stride-th value and halved the store when it REACHED the cap.
 * That thinned a session of exactly cap values (the base kept all of them), and a power-of-two stride aliases any
 * periodic input (alternating values kept only the even presents). This is a reservoir sample (Algorithm R) instead:
 *   - up to and including \a cap values it keeps every value, in order, exactly as the base did;
 *   - past the cap, value i (0-based) replaces a uniformly chosen kept value with probability cap / (i + 1), so the kept
 *     values are a uniform sample of the WHOLE session, never more than \a cap, with no period it can alias.
 * The random source is a fixed-seed splitmix64, so a session's sample is deterministic (tests and reruns agree). The
 * file keeps its name; the class is SessionSampler. Pure, no Qt.
 */

#ifndef PLAYBACKSTRIDESAMPLER_H
#define PLAYBACKSTRIDESAMPLER_H

#include <cstddef>
#include <vector>

namespace playback_sampling
{

class SessionSampler
{
public:
    explicit SessionSampler( size_t cap = 100000 ) : m_cap( cap < 1 ? 1 : cap ) {}

    void add( double v )
    {
        const unsigned long long index = m_seen++;
        if( m_values.size() < m_cap )
        {
            m_values.push_back( v );
            return;
        }
        // Algorithm R: keep the (index+1)-th value with probability cap / (index + 1), in a uniform slot
        const unsigned long long slot = next() % ( index + 1 );
        if( slot < m_cap ) m_values[static_cast<size_t>( slot )] = v;
    }

    const std::vector<double> &values() const { return m_values; }
    size_t size() const { return m_values.size(); }
    bool empty() const { return m_values.empty(); }
    unsigned long long seen() const { return m_seen; }
    size_t cap() const { return m_cap; }
    /*! True once the session outgrew the cap: the statistics then describe a uniform sample of it, not every value. */
    bool sampled() const { return m_seen > m_cap; }

private:
    unsigned long long next()
    {
        unsigned long long z = ( m_state += 0x9E3779B97F4A7C15ull );
        z = ( z ^ ( z >> 30 ) ) * 0xBF58476D1CE4E5B9ull;
        z = ( z ^ ( z >> 27 ) ) * 0x94D049BB133111EBull;
        return z ^ ( z >> 31 );
    }

    size_t m_cap;
    unsigned long long m_seen = 0;
    unsigned long long m_state = 0x5EED5A3B1E5EED01ull;
    std::vector<double> m_values;
};

} // namespace playback_sampling

#endif // PLAYBACKSTRIDESAMPLER_H
