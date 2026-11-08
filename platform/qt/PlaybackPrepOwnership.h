#ifndef PLAYBACKPREPOWNERSHIP_H
#define PLAYBACKPREPOWNERSHIP_H

// CPU-S1-PACE-NEXT-LEVER-1 (Part A): may drawFrameReady hand the prep worker the pinned render
// slot's RGB16 frame by pointer instead of deep-copying it into task.ownedSourceImage16?
//
// The slot is pinned while presenting (RenderFrameThread::acquireReadySlotLocked sets
// slot.presenting), findFreeSlotLocked never hands a presenting slot to a render, and the slot is
// released only by releasePresentedFrameForRequestSerial once the prep result is presented or
// dropped -- the same lifetime the 8-bit slot buffer already relies on (task.sourceImage is never
// copied). The only writer that ignores the pin, RenderFrameThread::init, runs in openMlv after
// playbackPrepQuiescentBeforeClipReplacement and the freeActiveMlvObjectAfterLifecycleBarrier
// backstop have dropped the pending task and drained the in-flight one.
//
// Only the CPU playing route borrows. Every GPU route (gpu16, GPU preview processing, the AMaZE
// texture, the playback-recon texture and its no-readback variant) keeps its copy unchanged, and
// so does every paused / cached display (displayPreviewCachingAllowed).

#include <cstddef>
#include <cstdint>

struct PlaybackPrepBorrowInputs
{
    bool playing = false;                       // ui->actionPlay->isChecked()
    bool displayPreviewCachingAllowed = true;   // !playbackPolicyActive()
    bool cpuPreviewProcessingActive = false;
    bool outputProcessed8 = true;               // readyFrame.outputMode == OutputProcessed8
    bool gpu16PreviewActive = false;
    bool gpuPreviewProcessingActive = false;
    bool gpuAmazeTexturePresentCandidate = false;
    bool gpuPlaybackReconTexturePresentCandidate = false;
    bool gpuPlaybackReconTextureNoReadbackCandidate = false;
    const uint16_t *rawImage16 = nullptr;       // the slot's buffer
    size_t rawImage16Bytes = 0;                 // its real size
    size_t sourceImage16Bytes = 0;              // width * height * 3 * 2
};

inline bool playbackPrepMayBorrowSlotRgb16( const PlaybackPrepBorrowInputs &in )
{
    if( !in.playing ) return false;
    if( in.displayPreviewCachingAllowed ) return false;
    // The CPU prep route: the RGB16 frame is what the worker reads.
    if( !in.cpuPreviewProcessingActive && in.outputProcessed8 ) return false;
    // Every GPU route keeps its copy.
    if( in.gpu16PreviewActive || in.gpuPreviewProcessingActive ) return false;
    if( in.gpuAmazeTexturePresentCandidate
     || in.gpuPlaybackReconTexturePresentCandidate
     || in.gpuPlaybackReconTextureNoReadbackCandidate ) return false;
    if( !in.rawImage16 || in.sourceImage16Bytes == 0
     || in.rawImage16Bytes < in.sourceImage16Bytes ) return false;
    return true;
}

// Fail-closed lifetime check for a borrowed frame: FNV-1a (32-bit, so it survives a QJsonValue
// double exactly) over at most 8192 evenly strided samples. Hashed once on the GUI thread at
// enqueue and again by the prep worker after its build; a difference means the slot changed under
// the worker (handoff_borrowed_rgb16_mutated_frames on playback_smoke.prep_summary).
inline uint32_t playbackPrepSampledRgb16Fnv( const uint16_t *data, size_t words )
{
    uint32_t hash = 2166136261u;
    if( !data || words == 0 ) return hash;
    const size_t kMaxSamples = 8192;
    const size_t step = words > kMaxSamples ? words / kMaxSamples : 1;
    size_t taken = 0;
    for( size_t i = 0; i < words && taken < kMaxSamples; i += step, ++taken )
    {
        const uint16_t v = data[i];
        hash ^= static_cast<uint32_t>( v & 0xFFu );
        hash *= 16777619u;
        hash ^= static_cast<uint32_t>( v >> 8 );
        hash *= 16777619u;
    }
    return hash;
}

#endif // PLAYBACKPREPOWNERSHIP_H
