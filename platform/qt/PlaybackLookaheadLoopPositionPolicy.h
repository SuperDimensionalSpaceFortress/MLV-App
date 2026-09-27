/*!
 * \file PlaybackLookaheadLoopPositionPolicy.h
 * \brief Pure arithmetic: given a speculative lookahead offset that lands
 *        past the active loop range's cut-out, which in-range frame does it
 *        wrap to and how many additional laps ahead is that -- extracted
 *        from MainWindow::queuePlaybackLookaheadRequests() so it can be
 *        unit-tested without the GUI.
 *
 * CUDA-ATTRIBUTION-BASELINE-1 round 6 (sol + astra BLOCKER): a lookahead
 * request can be issued for a position beyond cut-out while the loop is
 * enabled, in which case it belongs to a LATER lap than the request that
 * spawned it (MainWindow::drawFrame()'s own requestedFrame). Presentations
 * are now identity-keyed by (loop epoch, displayFrame) -- see
 * PlaybackPresentedFrameIdentityTracker.h -- so a lookahead's epoch must be
 * computed correctly or its occurrence identity collides with (or
 * incorrectly diverges from) the lap it actually belongs to.
 */

#ifndef PLAYBACKLOOKAHEADLOOPPOSITIONPOLICY_H
#define PLAYBACKLOOKAHEADLOOPPOSITIONPOLICY_H

#include <cstdint>

struct PlaybackLookaheadLoopPosition
{
    int wrappedFrame = 0;
    uint64_t lapsAhead = 0;
};

class PlaybackLookaheadLoopPositionPolicy
{
public:
    /*! \param rawLookaheadFrame requestedFrame + offset, BEFORE wrapping --
     *         caller must only invoke this when rawLookaheadFrame exceeds
     *         cutOutFrame (see MainWindow::queuePlaybackLookaheadRequests(),
     *         which handles the non-wrapping case without calling this at
     *         all).
     *  \param cutInFrame the active loop range's first frame (inclusive).
     *  \param loopSpan cutOutFrame - cutInFrame + 1; must be positive --
     *         callers guard this the same way MainWindow already does
     *         before wrapping (loop disabled or a degenerate span leaves
     *         the position unwrapped instead of calling this).
     */
    static PlaybackLookaheadLoopPosition wrap( int rawLookaheadFrame,
                                               int cutInFrame,
                                               int loopSpan )
    {
        PlaybackLookaheadLoopPosition result;
        if( loopSpan <= 0 )
        {
            result.wrappedFrame = rawLookaheadFrame;
            return result;
        }
        result.lapsAhead = static_cast<uint64_t>(
            ( rawLookaheadFrame - cutInFrame ) / loopSpan );
        result.wrappedFrame =
            cutInFrame + ( ( rawLookaheadFrame - cutInFrame ) % loopSpan );
        return result;
    }
};

#endif // PLAYBACKLOOKAHEADLOOPPOSITIONPOLICY_H
