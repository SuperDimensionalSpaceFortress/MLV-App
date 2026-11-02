#ifndef PLAYBACKPREPPRESENTATIONPOLICY_H
#define PLAYBACKPREPPRESENTATIONPOLICY_H

#include <cstddef>
#include <cstdint>

inline bool playbackPrepTaskShouldCompute( uint64_t taskSerial,
                                           uint64_t latestSerial,
                                           uint64_t taskGeneration,
                                           uint64_t activeGeneration )
{
    return taskSerial == latestSerial && taskGeneration == activeGeneration;
}

inline bool playbackPrepCompletedResultShouldPresent( uint64_t taskGeneration,
                                                       uint64_t activeGeneration )
{
    // A newer serial may arrive while expensive prep is running. The finished
    // frame is still safe to present when its display generation is current;
    // rejecting it by serial can starve continuous playback indefinitely.
    return taskGeneration == activeGeneration;
}

// CPU-SCOPE-LIVE-HEAP-CORRUPTION-1 F3: the 8-bit display source the prep path reads
// requiredBytes (W*H*3) from. The sized source image when it is long enough, else the
// slot's rawImage8 only when ITS buffer is long enough, else nullptr: the frame then
// has no 8-bit source, and the scope keeps its previous image.
inline const uint8_t *playbackSelectRgb8Source( const uint8_t *sourceImage,
                                                size_t sourceImageSize,
                                                const uint8_t *rawImage8,
                                                size_t rawImage8Bytes,
                                                size_t requiredBytes )
{
    if( sourceImage && sourceImageSize > 0 && sourceImageSize >= requiredBytes )
        return sourceImage;
    if( rawImage8 && rawImage8Bytes >= requiredBytes )
        return rawImage8;
    return nullptr;
}

#endif
