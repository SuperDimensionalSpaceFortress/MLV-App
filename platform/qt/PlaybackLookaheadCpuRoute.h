#ifndef PLAYBACKLOOKAHEADCPUROUTE_H
#define PLAYBACKLOOKAHEADCPUROUTE_H

// CPU-S1-PACE-POST-785-LEVER-1: may the playback lookahead (PLAYBACK-DECODE-RENDER-OVERLAP-1) run on
// the CPU route? On the CPU route one frame cost recon (~36 ms) + render work (~8 ms) + GUI hop in
// series (overlap_ms=0, lookahead_depth=0): the lookahead was texture-only. Depth 1 lets the recon
// of N+1 run while N renders and presents.
//
// Admitted ONLY when every one of these holds:
//  - playing, a file loaded, drop-frame mode (the lookahead machinery's drop-frame erase and the
//    target-filtered acquire are what keep a lookahead slot from presenting out of turn; whole-frame
//    playback is not admitted, so it keeps today's single-frame path);
//  - Phase 3 DecodeReconProcess is active (not killed, no live fallback), so the upstream recon
//    worker exists and the lookahead has something to overlap;
//  - not the GPU texture route (it keeps its own depth-2 admission) and no GPU debayer;
//  - the CPU prep route, decided by #774's own predicate (playbackPrepMayBorrowSlotRgb16) on the
//    request's route inputs: playing, playback policy active, CPU preview processing or a non-8-bit
//    output, and no gpu16 / GPU preview processing / GPU texture candidate. The borrow's buffer-size
//    checks are per frame, at present time; here they are satisfied by a probe word so only the
//    route decides.

#include "PlaybackPrepOwnership.h"

#include <cstdint>

struct PlaybackLookaheadCpuRouteInputs
{
    bool playing = false;                   // ui->actionPlay->isChecked()
    bool fileLoaded = false;                // m_fileLoaded && m_pMlvObject && m_pRenderThread
    bool dropFrameMode = false;             // ui->actionDropFrameMode->isChecked()
    bool phase3DecodeReconProcess = false;  // phase3ModeFor(...) == DecodeReconProcess, not killed, no fallback
    bool textureRouteRequested = false;     // requestContext.gpuPlaybackReconTexturePresentRequested
    bool gpuBilinearDebayer = false;        // m_renderThreadUsingGpuBilinearDebayer
    bool gpuAmazeDebayer = false;           // m_renderThreadUsingGpuAmazeDebayer
    PlaybackPrepBorrowInputs prepRoute;     // #774's borrow inputs, request side (buffer fields unused)
};

inline bool playbackLookaheadCpuRouteAdmits( const PlaybackLookaheadCpuRouteInputs &in )
{
    if( !in.playing || !in.fileLoaded || !in.dropFrameMode ) return false;
    if( !in.phase3DecodeReconProcess ) return false;
    if( in.textureRouteRequested ) return false;
    if( in.gpuBilinearDebayer || in.gpuAmazeDebayer ) return false;
    PlaybackPrepBorrowInputs route = in.prepRoute;
    static const uint16_t kRouteProbeWord = 0;
    route.rawImage16 = &kRouteProbeWord;
    route.rawImage16Bytes = sizeof( kRouteProbeWord );
    route.sourceImage16Bytes = sizeof( kRouteProbeWord );
    return playbackPrepMayBorrowSlotRgb16( route );
}

#endif // PLAYBACKLOOKAHEADCPUROUTE_H
