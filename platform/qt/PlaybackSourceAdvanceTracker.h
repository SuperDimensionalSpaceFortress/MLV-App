/*!
 * \file PlaybackSourceAdvanceTracker.h
 * \brief Measures how fast the playback TIMELINE moves, as opposed to how many
 *        new pictures reach the screen -- extracted so it can be unit-tested
 *        without the GUI (PLAYBACK-HFR-CONFORM-DEFAULT-1 item 8).
 *
 * The new-frame swap counter counts presents. A timeline running at 1.4x can
 * still present ~24 new frames a second by skipping source frames, so the
 * conform acceptance also needs (a) source frames advanced per wall-clock
 * second and (b) the largest jump between consecutively presented source
 * frames (1 == every source frame shown), loop wrap excluded.
 */

#ifndef PLAYBACKSOURCEADVANCETRACKER_H
#define PLAYBACKSOURCEADVANCETRACKER_H

#include <cstdint>

class PlaybackSourceAdvanceTracker
{
public:
    void reset() { *this = PlaybackSourceAdvanceTracker(); }

    /*! \param nowSeconds monotonic time of this presentation
     *  \param frame the presented source frame (0-based)
     *  \param wrapsCrossed how many loop wraps lie between the previous presented
     *         frame and this one (cut range [cutInIndex, cutOutIndex]); 0 = not a
     *         wrap (presentationLoopWraps; true / false read as 1 / 0). Each wrap
     *         is credited one cut width (r19, adversary A1 on 924d3d6f).
     *  \param cutInIndex first frame index of the loop range
     *  \param cutOutIndex last frame index of the loop range */
    void notePresented( double nowSeconds, int frame, int wrapsCrossed,
                        int cutInIndex, int cutOutIndex )
    {
        if( m_presented == 0 )
        {
            m_firstTime = nowSeconds;
        }
        else if( wrapsCrossed > 0 )
        {
            m_wraps += static_cast<uint64_t>( wrapsCrossed );
            const int64_t width = static_cast<int64_t>( cutOutIndex ) - cutInIndex + 1;
            const int64_t distance = frame + width * wrapsCrossed - m_lastFrame;
            if( distance > 0 ) m_advanced += distance;
        }
        else if( frame > m_lastFrame )
        {
            const int jump = frame - m_lastFrame;
            m_advanced += jump;
            if( jump > m_maxJump ) m_maxJump = jump;
        }
        else if( frame < m_lastFrame )
        {
            ++m_backward;
        }
        else
        {
            ++m_repeats;
        }
        m_lastTime = nowSeconds;
        m_lastFrame = frame;
        ++m_presented;
    }

    uint64_t presented() const { return m_presented; }
    int64_t advancedFrames() const { return m_advanced; }
    /*! Largest forward jump between consecutive presented frames, wrap excluded. */
    int maxJump() const { return m_maxJump; }
    uint64_t wraps() const { return m_wraps; }
    uint64_t backwardJumps() const { return m_backward; }
    uint64_t repeatedFrames() const { return m_repeats; }
    double spanSeconds() const
    {
        return m_presented > 1 ? m_lastTime - m_firstTime : 0.0;
    }
    /*! Source frames advanced per wall-clock second between the first and last
     *  presentation; 0 when fewer than two frames or no elapsed time. */
    double sourceFps() const
    {
        const double span = spanSeconds();
        return span > 0.0 ? static_cast<double>( m_advanced ) / span : 0.0;
    }

private:
    uint64_t m_presented = 0;
    int64_t m_advanced = 0;
    int m_maxJump = 0;
    uint64_t m_wraps = 0;
    uint64_t m_backward = 0;
    uint64_t m_repeats = 0;
    int m_lastFrame = 0;
    double m_firstTime = 0.0;
    double m_lastTime = 0.0;
};

/*! HFR-NATIVE-60FPS-FULLRES-1-MERGE-MASTER r16 (adversary A1 on ff41b4fa): whether a presentation is a
 *  loop wrap's landing, and (r19) over how many wraps, for notePresented's `wrapsCrossed`. The presented-frame inference
 *  (playback_frame_range::isContactSheetLoopWrapTransition) misses a genuine wrap whenever dropped frames
 *  leave the last presented frame more than 8 frames short of the cut-out: Loop + drop-frame, cut 1..720,
 *  700 -> 0 jumps back by 700, under its 711. The tracker then counted the wrap as a backward jump and lost
 *  the source advance across it. The ENGINE's wrap count (PlaybackWrapRecorder::engineWraps, noted by the
 *  wrap branches in MainWindow::playbackHandling) is the authority, as it already is for the summary's
 *  wrap_count. An engine wrap belongs to ONE presented transition only (the rule is below). The measured
 *  Play's first presentation consumes only the pending wraps that came before its own request (r16 re-pass
 *  A1 on c9b4ca5e: a wrap before the first presentation stayed pending, and a later backward seek took it
 *  and was credited 719 frames; r18 below: a wrap after its request stays pending). The inference alone
 *  never makes a wrap here (r16 re-pass A2 on 215bd10e: over a cut range narrower than its 8-frame
 *  tolerance, cut 1..8, it calls any backward seek a wrap, and the seek 5 -> 4 was credited 7 frames).
 *  r17: neither count alone places the wrap.
 *  - The LIVE count (liveEngineWraps) is read after the draw's own early/tail advance. That advance can
 *    wrap the engine before an in-flight frame requested earlier is noted (718, 719, 0: adversary A1 on
 *    6451a8d9). With the live count alone, 719 took the wrap and 719 -> 0 counted backward.
 *  - The REQUEST stamp (requestEngineWraps, RenderFrameThread::ReadyFrame::PresentationContext::
 *    engineWrapsAtRequest) misses a lookahead frame requested across the boundary before the wrap and
 *    reused after it (speculative 0 stamped 0: adversary A1 on eb5af034).
 *  So a wrap is PENDING while the live count is above consumedEngineWraps. The function returns HOW MANY
 *  pending wraps this presentation lands (0 = it is no wrap), for notePresented's wrapsCrossed. The wraps a
 *  presented frame crossed are the pending ones before its own request: min(stamp, live) - consumed. With a
 *  previous presentation in the measured Play, pending wraps are TAKEN by
 *  - a presentation that goes backward, whatever its stamp: it IS a wrap's landing, so it takes the wraps
 *    before its request and at least one (a lookahead stamped before the wrap, eb5af034, takes that one); and
 *  - r19 (sol r18 B1, catalogue M-HFR-FORWARD-WRAP): a forward or repeated presentation REQUESTED AFTER a
 *    pending wrap (stamp above consumedEngineWraps). It is the wrap's forward landing: over cut 0..7,
 *    3 -> wrap -> 5 travelled 5 + 8 - 3 = 10 frames, not a forward jump of 2. 5196d4a6 consumed such a wrap
 *    without taking it.
 *  r19 (adversary A1 on 924d3d6f): each landing takes only the wraps it crossed, and is credited one cut
 *  width per wrap. Two wraps before the first landing's presentation, the second after its request: 5 ->
 *  wrap -> 1 (stamp 1) -> wrap -> 3 (stamp 2) is 4 + 10 = 14 frames and 2 wraps, never 6 and 1; and 2 -> two
 *  wraps -> 4 (stamp 2) is 4 + 2 x 8 - 2 = 18 frames. A wrap after a landing's request stays pending for the
 *  next frame that crosses it. A wrap stays pending across a forward or repeated frame requested before it
 *  (stamp not above consumedEngineWraps): an in-flight frame from before the wrap. The measured Play's FIRST
 *  presentation never takes a wrap (it has no transition); it consumes up to min(stamp, live), only the
 *  wraps before its own request. r18 (adversary A1 on 26f235a4): 719 requested with stamp 0 whose draw wraps
 *  the engine first used to consume the wrap, so 719 -> 0 counted backward.
 *  consumedEngineWraps is the caller's running count. It is set to the live count when the measured Play
 *  starts and only ever rises, never above the live count.
 *  \param havePrevious a frame was presented before this one in the measured Play */
inline int presentationLoopWraps( int liveEngineWraps, int requestEngineWraps, int &consumedEngineWraps,
                                  bool havePrevious, int lastFrame, int frame )
{
    if( liveEngineWraps <= consumedEngineWraps ) return 0;   // no wrap pending
    // the wraps before this frame's request (never above the live count), and how many of them are pending
    const int beforeRequest = requestEngineWraps < liveEngineWraps ? requestEngineWraps : liveEngineWraps;
    const int crossed = beforeRequest > consumedEngineWraps ? beforeRequest - consumedEngineWraps : 0;
    if( !havePrevious )
    {
        // r18 (adversary A1 on 26f235a4): the first presentation has no transition; it consumes only the wraps
        // that came before its own request. A later wrap stays pending for the frame that crosses it.
        consumedEngineWraps += crossed;
        return 0;
    }
    if( frame < lastFrame )
    {
        // the backward landing IS a wrap: the ones before its request, and at least one (a lookahead stamped before it)
        const int taken = crossed > 0 ? crossed : 1;
        consumedEngineWraps += taken;
        return taken;
    }
    // r19 (sol r18 B1): a forward or repeated frame requested after a pending wrap is its forward landing; one
    // requested before every pending wrap (crossed 0) is an in-flight frame, and the wraps stay pending
    consumedEngineWraps += crossed;
    return crossed;
}

#endif // PLAYBACKSOURCEADVANCETRACKER_H
