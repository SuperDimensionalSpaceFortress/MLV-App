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

#endif
