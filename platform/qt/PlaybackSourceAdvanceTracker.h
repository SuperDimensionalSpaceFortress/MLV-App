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
     *  \param wrapped true when this presentation is the loop wrap from the
     *         previous presented frame (cut range [cutInIndex, cutOutIndex])
     *  \param cutInIndex first frame index of the loop range
     *  \param cutOutIndex last frame index of the loop range */
    void notePresented( double nowSeconds, int frame, bool wrapped,
                        int cutInIndex, int cutOutIndex )
    {
        if( m_presented == 0 )
        {
            m_firstTime = nowSeconds;
        }
        else if( wrapped )
        {
            ++m_wraps;
            const int width = cutOutIndex - cutInIndex + 1;
            const int distance = frame + width - m_lastFrame;
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

/*! HFR-NATIVE-60FPS-FULLRES-1-MERGE-MASTER r16 (adversary A1 on ff41b4fa): whether a presentation is the
 *  loop wrap, for notePresented's `wrapped`. The presented-frame inference
 *  (playback_frame_range::isContactSheetLoopWrapTransition) misses a genuine wrap whenever dropped frames
 *  leave the last presented frame more than 8 frames short of the cut-out: Loop + drop-frame, cut 1..720,
 *  700 -> 0 jumps back by 700, under its 711. The tracker then counted the wrap as a backward jump and lost
 *  the source advance across it. The ENGINE's wrap count (PlaybackWrapRecorder::engineWraps, noted by the
 *  wrap branches in MainWindow::playbackHandling) is the authority, as it already is for the summary's
 *  wrap_count. A presentation that goes backward while an engine wrap is still unconsumed IS the wrap, and
 *  so is one the inference recognises. consumedEngineWraps is the caller's running count; it is set to the
 *  engine's count whenever a wrap is taken, and when the measured Play starts.
 *  \param havePrevious a frame was presented before this one in the measured Play */
inline bool presentationIsLoopWrap( bool inferredWrap, int engineWraps, int &consumedEngineWraps,
                                    bool havePrevious, int lastFrame, int frame )
{
    const bool wrap = havePrevious
                   && ( inferredWrap || ( engineWraps > consumedEngineWraps && frame < lastFrame ) );
    if( wrap ) consumedEngineWraps = engineWraps;
    return wrap;
}

#endif // PLAYBACKSOURCEADVANCETRACKER_H
