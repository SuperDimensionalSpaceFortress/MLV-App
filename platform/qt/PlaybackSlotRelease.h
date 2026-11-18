#ifndef PLAYBACKSLOTRELEASE_H
#define PLAYBACKSLOTRELEASE_H

#include <cstdint>

// CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 F1: which slots a playback generation cancel may
// release. A Ready slot has no borrower, so cancelling it is safe. A Presenting slot is
// pinned (MainWindow.h PlaybackPrepTask): its borrower -- the prep worker's in-flight
// task, a queued prep result, or the present in progress -- still reads it, and the
// borrower releases it by serial on its own drop path. Releasing it here let the
// render thread recycle a buffer the borrower was still using.
enum class PlaybackSlotHold
{
    Ready,
    Presenting,
    Other
};

inline bool playbackCancelReleasesSlot( PlaybackSlotHold hold,
                                        bool playbackActive,
                                        uint64_t slotGeneration,
                                        uint64_t cancelGeneration )
{
    return playbackActive
        && slotGeneration == cancelGeneration
        && hold == PlaybackSlotHold::Ready;
}

// PLAYER-FULLSCREEN-KEEPS-PLAYING-1 r1 fix 2 (adversary A1 on 4bbdcd9a): a display-change invalidation mid-Play cancels the
// old generation's queued requests and Ready slots, but a slot still RENDERING then is published Ready later, carrying the
// old generation. A lookahead slot is acquired only for the active generation and the forward-window prune looks only at
// the active generation, so that slot was never freed: with one frame slot the next request waited for it forever. A
// Ready lookahead slot of an OLDER generation can never be presented, so it is released; a Presenting one stays pinned
// (its borrower releases it by serial), and a slot of the active (or a newer) generation is never touched.
inline bool playbackRetiredLookaheadSlotReleases( PlaybackSlotHold hold,
                                                  bool lookaheadRequest,
                                                  uint64_t slotGeneration,
                                                  uint64_t activeGeneration )
{
    return hold == PlaybackSlotHold::Ready
        && lookaheadRequest
        && slotGeneration < activeGeneration;
}

#endif
