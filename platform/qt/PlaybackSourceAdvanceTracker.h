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
     *         is credited one cut width (r19, adversary A1 on 924d3d6f), the
     *         non-drop Loop wrap's cutOut -> cutIn step. A Loop + Drop Frame wrap
     *         moves back one frame less (DropFrameTickResult::wrapWidth), and
     *         MainWindow credits each wrap's recorded width through the overload
     *         below (r21, key3 M-HFR-DROP-WRAP-WIDTH).
     *  \param cutInIndex first frame index of the loop range
     *  \param cutOutIndex last frame index of the loop range */
    void notePresented( double nowSeconds, int frame, int wrapsCrossed,
                        int cutInIndex, int cutOutIndex )
    {
        const int64_t width = static_cast<int64_t>( cutOutIndex ) - cutInIndex + 1;
        notePresented( nowSeconds, frame, wrapsCrossed, width * wrapsCrossed );
    }

    /*! r20 (adversary A1 on ce603f37): the same, with the total cut width of the wraps crossed as each had it when the
     *  engine made it (PlaybackWrapRecorder::engineWrapsWidth), not the cut range as it stands at this presentation. */
    void notePresented( double nowSeconds, int frame, int wrapsCrossed, int64_t wrapsCrossedWidth )
    {
        if( m_presented == 0 )
        {
            m_firstTime = nowSeconds;
        }
        else if( wrapsCrossed > 0 )
        {
            m_wraps += static_cast<uint64_t>( wrapsCrossed );
            const int64_t distance = frame + wrapsCrossedWidth - m_lastFrame;
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

/*! HFR-NATIVE-60FPS-FULLRES-1-MERGE-MASTER r16 (adversary A1 on ff41b4fa), rule rewritten in r20: how many loop wraps a
 *  presentation lands, for notePresented's `wrapsCrossed`. The presented-frame inference
 *  (playback_frame_range::isContactSheetLoopWrapTransition) misses a genuine wrap whenever dropped frames leave the last
 *  presented frame more than 8 frames short of the cut-out (Loop + drop-frame, cut 1..720, 700 -> 0 jumps back by 700,
 *  under its 711), and over a cut range narrower than its tolerance it calls any backward seek a wrap (r16 re-pass A2 on
 *  215bd10e: cut 1..8, the seek 5 -> 4 credited 7 frames). So the ENGINE's wrap count (PlaybackWrapRecorder::engineWraps,
 *  noted by the wrap branches in MainWindow::playbackHandling) is the authority, as it already is for the summary's
 *  wrap_count, and an engine wrap belongs to ONE presented transition only.
 *
 *  The rule: a presentation lands the pending wraps that came before the request that made it the timeline's position,
 *  min(requestEngineWraps, liveEngineWraps) - consumedEngineWraps, one cut width per wrap. Direction decides nothing,
 *  because the engine notes a wrap BEFORE it requests the frame the wrap lands on, so a stamp below a wrap proves the
 *  frame is not that wrap's landing:
 *  - in flight: 718, 719 (stamp 0, its draw's own early/tail advance wraps the engine first, live 1), 0 (stamp 1). The
 *    wrap stays pending across 719 and 0 lands it (r17, adversary A1 on 6451a8d9). The measured Play's FIRST presentation
 *    behaves the same but has no transition: it consumes the wraps before its request and is credited none (r18,
 *    adversary A1 on 26f235a4; r16 re-pass A1 on c9b4ca5e).
 *  - a backward seek: cut 0..7, 5, then 4 (stamp 0) whose draw wraps the engine (live 1), then the landing 2 (stamp 1). 4
 *    is an ordinary backward jump and leaves the wrap pending: 2 is credited 2 + 8 - 4 = 6 (r20, adversary A2 on dbeb9424:
 *    a "backward means landing" branch took the wrap and credited 4 + 8 - 5 = 7).
 *  - a forward landing: Loop + Drop Frame, cut 0..7, 3 -> wrap -> 5 (stamp 1) travelled 5 + 7 - 3 = 9 frames, never a
 *    forward jump of 2 (r19, sol r18 B1, catalogue M-HFR-FORWARD-WRAP). The drop-frame tick moves back by cutOut - cutIn =
 *    7, and that is the width the wrap is credited (r21, key3 M-HFR-DROP-WRAP-WIDTH: 818dd037 credited 8, so 10).
 *  - several wraps: 5 -> wrap -> 1 (stamp 1) -> wrap -> 3 (stamp 2) is 4 + 10 = 14 frames and 2 wraps; 2 -> two wraps -> 4
 *    (stamp 2) is 4 + 2 x 8 - 2 = 18 (r19, adversary A1 on 924d3d6f). A wrap after a landing's request stays pending for
 *    the next frame that crosses it.
 *  A request after a pending wrap whose landing was never presented, a seek included, lands that wrap: the timeline did
 *  cross it, and the frame is the first presented from beyond it.
 *  consumedEngineWraps is the caller's running count: set to the live count when the measured Play starts, it only ever
 *  rises, never above the live count.
 *  \param requestEngineWraps a non-lookahead frame's PresentationContext::engineWrapsAtRequest, or a presented lookahead's
 *         presentationAcceptedLookaheadStamp, which MainWindow::drawFrameReady writes there
 *  \param havePrevious a frame was presented before this one in the measured Play */
inline int presentationLoopWraps( int liveEngineWraps, int requestEngineWraps, int &consumedEngineWraps,
                                  bool havePrevious )
{
    const int beforeRequest = requestEngineWraps < liveEngineWraps ? requestEngineWraps : liveEngineWraps;
    const int crossed = beforeRequest > consumedEngineWraps ? beforeRequest - consumedEngineWraps : 0;
    consumedEngineWraps += crossed;
    return havePrevious ? crossed : 0;
}

/*! r20 (adversary A1 on dbeb9424, closing A1 on eb5af034 and on 1df419eb): the request stamp of a presented playback
 *  LOOKAHEAD frame. It is rendered ahead of the timeline and stamped with the wrap count of the request that spawned it,
 *  which can predate a wrap the timeline crossed before reaching it (cut 0..3, 0 presented, cached 1 stamped 0, the engine
 *  wraps and lands on 1). MainWindow::drawFrameReady drops a lookahead frame unless it IS the engine's active target, so a
 *  presented lookahead is the timeline's position at that check, and the live count there, read before that draw's own
 *  early/tail advance can move the target or the count, is the wraps before its landing. A record of the engine's last
 *  request (dbeb9424) lost this when that advance requested the next frame first, and read advanced 1, wraps 0 where
 *  1 + 4 - 0 = 5 and 1 are right.
 *  \param contextStamp the frame's own PresentationContext::engineWrapsAtRequest
 *  \param engineWrapsAtAcceptance the live count at drawFrameReady's target check */
inline int presentationAcceptedLookaheadStamp( int contextStamp, int engineWrapsAtAcceptance )
{
    return engineWrapsAtAcceptance > contextStamp ? engineWrapsAtAcceptance : contextStamp;
}

#endif // PLAYBACKSOURCEADVANCETRACKER_H
