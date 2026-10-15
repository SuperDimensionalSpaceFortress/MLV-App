/*!
 * \file PlaybackRoutePrewarmPolicy.h
 * \brief When the CUDA texture playback route is warmed while paused, and at what size (pure, unit-tested).
 *
 * PLAYBACK-STARTUP-WAIT-PREWARM-1 Row B. On Bachelor the first Play-present paid the route's one-time setup on the GUI
 * thread (present_setup_first_ms 57-184 ms, then ~125-219 ms for the first AMaZE run into a new GL texture: lazy
 * kernel loading, the live device buffers sized to the frame, CUDA-GL interop). Once per clip load, after the first
 * paused present, the GUI thread compiles the preview-processing program and runs AMaZE once at the playback texture
 * size on temporary textures that are then freed (nothing it creates is ever presented). Play, a clip close or
 * another clip before it runs cancels it; an ineligible route does no work; MLVAPP_PLAYBACK_ROUTE_PREWARM=0 turns
 * it off.
 */

#ifndef PLAYBACKROUTEPREWARMPOLICY_H
#define PLAYBACKROUTEPREWARMPOLICY_H

namespace playback_route_prewarm
{

struct Inputs
{
    bool enabled = true;                   // not MLVAPP_PLAYBACK_ROUTE_PREWARM=0
    bool playing = false;
    bool fileLoaded = false;
    bool clipGenerationMatches = false;    // armed for the clip that is loaded now
    bool gpuWindowActive = false;          // the GL window the texture route presents into
    bool textureRouteRequested = false;    // the CUDA recon texture present is the playback route
    bool r16TextureBackendAdmitted = false;
};

enum class Decision { Run, SkipDisabled, SkipPlaying, SkipNoClip, SkipStale, SkipIneligible };

inline Decision decide( const Inputs &in )
{
    if( !in.enabled ) return Decision::SkipDisabled;
    if( in.playing ) return Decision::SkipPlaying;
    if( !in.fileLoaded ) return Decision::SkipNoClip;
    if( !in.clipGenerationMatches ) return Decision::SkipStale;
    if( !in.gpuWindowActive || !in.textureRouteRequested || !in.r16TextureBackendAdmitted )
        return Decision::SkipIneligible;
    return Decision::Run;
}

inline const char *decisionName( Decision decision )
{
    switch( decision )
    {
    case Decision::Run: return "run";
    case Decision::SkipDisabled: return "disabled";
    case Decision::SkipPlaying: return "playing";
    case Decision::SkipNoClip: return "no_clip";
    case Decision::SkipStale: return "stale";
    case Decision::SkipIneligible: return "ineligible";
    }
    return "none";
}

struct Size { int width = 0; int height = 0; };

/*! The texture the playback route will present: at x2/x4 with a reduced-recon plan, the plan's size (rows cropped to
 *  whole ISO blocks, RenderFrameThread's gpuReducedReconSlot); otherwise the clip's own size. A guess that turns out
 *  wrong only warms less (the live buffers are re-sized at the first present), never shows anything. */
inline Size textureSize( int clipWidth, int clipHeight, int playbackScale, bool reducedPlanOk, int planWidth, int planHeight )
{
    if( ( playbackScale == 2 || playbackScale == 4 ) && reducedPlanOk && planWidth > 0 && planHeight > 0 )
        return Size{ planWidth, planHeight };
    return Size{ clipWidth, clipHeight };
}

} // namespace playback_route_prewarm

#endif // PLAYBACKROUTEPREWARMPOLICY_H
