/*!
 * \file PlaybackAchievedScalePolicy.h
 * \brief Pure decision: what playback scale factor was actually ACHIEVED by
 *        the route that rendered this frame, extracted so it can be
 *        unit-tested without the render thread.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 2 (astra major finding): only
 * RenderFrameThread::OutputProcessed8/OutputProcessed16 ever resize their
 * output for a requested playback scale (mlvFrameOutputDimensions, then
 * optionally clamped by the MLV core's own playback_scale_factor_active).
 * RenderFrameThread::OutputDebayered16 always debayers at full source
 * resolution regardless of the requested scale. Reporting the requested
 * scale for that route would let a leg read "achieved_scale=4" while
 * full-resolution work happened underneath -- the central defect this
 * round exists to fix.
 */

#ifndef PLAYBACKACHIEVEDSCALEPOLICY_H
#define PLAYBACKACHIEVEDSCALEPOLICY_H

/*! \brief Which output route rendered the frame. Mirrors
 *  RenderFrameThread::OutputMode's three values without depending on that
 *  (Qt-attached) class, so this header stays dependency-free.
 */
enum class PlaybackOutputRoute
{
    Processed8,
    Processed16,
    Debayered16,
};

class PlaybackAchievedScalePolicy
{
public:
    /*! \param route the route that rendered this frame.
     *  \param requestedScaleFactor the scale factor sent to the render thread
     *         for this request (already past MainWindow's own S4 texture-
     *         route clamp; RenderFrameThread's playbackScaleFactor parameter).
     *  \param coreActiveScale the MLV core's own playback_scale_factor_active
     *         value, which may further clamp requestedScaleFactor down.
     *  \param coreActiveScaleValid whether coreActiveScale is one of the
     *         core's legal values (1/2/4/8); an invalid/uninitialized value
     *         is ignored, matching the pre-existing clamp-acceptance check.
     *  \return the scale factor that actually reduced the work this route
     *          performed; 1 (never scaled) on any route that does not resize
     *          for scale at all.
     */
    static int achievedScaleFactor( PlaybackOutputRoute route,
                                    int requestedScaleFactor,
                                    int coreActiveScale,
                                    bool coreActiveScaleValid )
    {
        if( route != PlaybackOutputRoute::Processed8
         && route != PlaybackOutputRoute::Processed16 )
        {
            return 1;
        }
        if( coreActiveScaleValid )
        {
            return coreActiveScale;
        }
        return requestedScaleFactor;
    }
};

#endif // PLAYBACKACHIEVEDSCALEPOLICY_H
