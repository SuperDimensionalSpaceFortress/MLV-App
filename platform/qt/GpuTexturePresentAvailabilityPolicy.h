/*!
 * \file GpuTexturePresentAvailabilityPolicy.h
 * \brief Pure decision: what basis label does the GPU texture-present
 *        upload/kernel/interop/total timing carry for this frame, extracted
 *        so it can be unit-tested without the GUI or a GPU.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 2 (astra major 3 / sol major 2 / astra
 * minor 6): the combined `available` flag is an OR of the recon and AMaZE
 * component flags, so it cannot by itself say whether the summed timing
 * fields are a complete measurement, half a silent zero-fill, or -- when the
 * texture-present path never ran at all this frame -- not applicable in the
 * first place. A combined flag may never upgrade a partial reading to
 * "measured", and non-execution must never read the same as an
 * attempted-and-failed fallback.
 */

#ifndef GPUTEXTUREPRESENTAVAILABILITYPOLICY_H
#define GPUTEXTUREPRESENTAVAILABILITYPOLICY_H

enum class GpuTexturePresentTimingBasis
{
    /*! The texture-present path did not run at all this frame (e.g. a
     *  CPU-only debayer frame) -- there is nothing to have measured. */
    NotExecuted,
    /*! The path was attempted but neither component reported. */
    Unavailable,
    /*! Exactly one of recon/AMaZE reported; the other's contribution to the
     *  summed fields is a silent 0.0 fill. */
    Partial,
    /*! Both components reported. */
    Measured,
};

class GpuTexturePresentAvailabilityPolicy
{
public:
    /*! \param attempted whether the texture-present path ran at all this
     *         frame (i.e. whether its telemetry keys were even written).
     *  \param reconAvailable the writer's recon component flag.
     *  \param amazeAvailable the writer's AMaZE component flag.
     */
    static GpuTexturePresentTimingBasis classify( bool attempted,
                                                   bool reconAvailable,
                                                   bool amazeAvailable )
    {
        if( !attempted )
        {
            return GpuTexturePresentTimingBasis::NotExecuted;
        }
        if( !reconAvailable && !amazeAvailable )
        {
            return GpuTexturePresentTimingBasis::Unavailable;
        }
        if( reconAvailable && amazeAvailable )
        {
            return GpuTexturePresentTimingBasis::Measured;
        }
        return GpuTexturePresentTimingBasis::Partial;
    }
};

#endif // GPUTEXTUREPRESENTAVAILABILITYPOLICY_H
