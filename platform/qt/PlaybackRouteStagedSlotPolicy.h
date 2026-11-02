/*!
 * \file PlaybackRouteStagedSlotPolicy.h
 * \brief Whether the first recon present adopts the paused prewarm's staged texture pair (pure, unit-tested).
 *
 * PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-SETUP-1 Row A. The #450 prewarm warmed AMaZE on temporary textures and
 * freed them, so the first Play-present at s1 still re-allocated the 1808x2268 RGBA16 + R16 pair and registered it
 * with CUDA again (~100-200 ms on the GUI thread). The prewarm now keeps its warmed pair, still registered, as a
 * staged slot; the first recon present adopts it instead of calling destroyTexture() and allocating.
 *
 * Adopt only when the old texture was never CUDA-registered (it is the paused QImage texture, not a recon texture),
 * no recon source texture exists, and the staged pair is exactly the texture size. Any other staged pair is
 * discarded and today's re-allocation runs. MLVAPP_PLAYBACK_ROUTE_PREWARM_STAGE=0 turns staging off.
 */

#ifndef PLAYBACKROUTESTAGEDSLOTPOLICY_H
#define PLAYBACKROUTESTAGEDSLOTPOLICY_H

namespace playback_route_staged_slot
{

enum class Decision { None, Adopt, Discard };

inline Decision decide( bool stageEnabled,
                        bool stagedValid,
                        int stagedWidth,
                        int stagedHeight,
                        int texWidth,
                        int texHeight,
                        bool currentTextureIsRecon,
                        bool reconSourceExists )
{
    if( !stagedValid ) return Decision::None;
    if( stageEnabled
     && stagedWidth == texWidth && stagedHeight == texHeight && texWidth > 0 && texHeight > 0
     && !currentTextureIsRecon
     && !reconSourceExists )
        return Decision::Adopt;
    return Decision::Discard;
}

inline const char *decisionName( Decision decision )
{
    switch( decision )
    {
    case Decision::None: return "none";
    case Decision::Adopt: return "adopted";
    case Decision::Discard: return "discarded";
    }
    return "none";
}

} // namespace playback_route_staged_slot

#endif // PLAYBACKROUTESTAGEDSLOTPOLICY_H
