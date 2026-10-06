/*!
 * \file Debayered16ReconReusePolicy.h
 * \brief Pure decision: may the debayered-16 render consume the phase-3 recon
 *        worker's full-resolution reconstruction instead of decoding and
 *        reconstructing the frame again? Extracted so it can be unit-tested
 *        without the render thread.
 *
 * CPU-DEBAYERED16-REUSE-PHASE3-RECON-1: on the CPU preview-processing route
 * (fast processing for playback, subset, with the preview config enabled) the
 * render requests OutputDebayered16. The recon worker has already decoded the
 * frame and run the full llrawproc (dual-ISO recon included) into the slot's
 * rawImage16, and the render used to discard that and run decode + llrawproc
 * again inside getMlvRawFrameDebayered. When every condition below holds the
 * render debayers the worker's reconstruction instead
 * (getMlvRawFrameDebayeredFromReconnedRaw16); otherwise it takes today's path
 * and logs the returned reason. Full resolution only: a reduced (#271) recon
 * never qualifies.
 *
 * r2: the slot also carries how its recon was made. A failed worker decode, a
 * CUDA playback recon, or llrawproc settings that changed during the recon or
 * since it are refused, so the frame takes today's path (and its failure
 * handling) byte-for-byte. renderDebayered16FromSlot is the render's whole
 * OutputDebayered16 CPU branch, shared by RenderFrameThread and the tests.
 */

#ifndef DEBAYERED16RECONREUSEPOLICY_H
#define DEBAYERED16RECONREUSEPOLICY_H

#include "../../src/mlv_include.h"

#include <algorithm>
#include <array>
#include <atomic>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <new>
#include <vector>

/*! Kill switch: any non-empty value other than "0" forces today's path. */
#define MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV "MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE"

/*! Why a debayered-16 render did not consume the recon. None = it did. */
enum class Debayered16ReconRefusal : int
{
    None = 0,
    KillSwitch,
    NotReconned,
    AcquisitionFailed,
    NotPlaying,
    GpuAmazeDebayer,
    GpuBilinearDebayer,
    GpuReconTextureRoute,
    GpuPlaybackRecon,
    FrameCacheMayServe,
    ReducedScale,
    FrameIdentity,
    SettingsChangedDuringRecon,
    SettingsChangedSinceRecon,
    BufferIncomplete,
    ScratchAllocationFailed,
    DebayerDeclined,
    /*! Not a policy refusal: a GPU debayer rendered the frame before the CPU branch. */
    GpuDebayerRendered,
    Count
};

/*! Stable telemetry name of each outcome (summary-line counter names). */
inline const char * debayered16ReconRefusalName( Debayered16ReconRefusal refusal )
{
    switch( refusal )
    {
    case Debayered16ReconRefusal::None: return "consumed";
    case Debayered16ReconRefusal::KillSwitch: return "kill_switch";
    case Debayered16ReconRefusal::NotReconned: return "not_reconned";
    case Debayered16ReconRefusal::AcquisitionFailed: return "acquisition_failed";
    case Debayered16ReconRefusal::NotPlaying: return "not_playing";
    case Debayered16ReconRefusal::GpuAmazeDebayer: return "gpu_amaze_debayer";
    case Debayered16ReconRefusal::GpuBilinearDebayer: return "gpu_bilinear_debayer";
    case Debayered16ReconRefusal::GpuReconTextureRoute: return "gpu_recon_texture_route";
    case Debayered16ReconRefusal::GpuPlaybackRecon: return "gpu_playback_recon";
    case Debayered16ReconRefusal::FrameCacheMayServe: return "frame_cache_may_serve";
    case Debayered16ReconRefusal::ReducedScale: return "reduced_scale";
    case Debayered16ReconRefusal::FrameIdentity: return "frame_identity";
    case Debayered16ReconRefusal::SettingsChangedDuringRecon: return "settings_changed_during_recon";
    case Debayered16ReconRefusal::SettingsChangedSinceRecon: return "settings_changed_since_recon";
    case Debayered16ReconRefusal::BufferIncomplete: return "buffer_incomplete";
    case Debayered16ReconRefusal::ScratchAllocationFailed: return "scratch_allocation_failed";
    case Debayered16ReconRefusal::DebayerDeclined: return "debayer_declined";
    case Debayered16ReconRefusal::GpuDebayerRendered: return "gpu_debayer_rendered";
    case Debayered16ReconRefusal::Count: break;
    }
    return "unknown";
}

/*! Human-readable reason, for the log line and the fallback-reason telemetry. */
inline const char * debayered16ReconRefusalReason( Debayered16ReconRefusal refusal )
{
    switch( refusal )
    {
    case Debayered16ReconRefusal::None: return "";
    case Debayered16ReconRefusal::KillSwitch: return MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV " is set";
    case Debayered16ReconRefusal::NotReconned: return "slot was not reconstructed by the phase-3 recon worker";
    case Debayered16ReconRefusal::AcquisitionFailed: return "the recon worker's decode of this frame failed";
    case Debayered16ReconRefusal::NotPlaying: return "not playing (paused and scrubbed frames keep the full render)";
    case Debayered16ReconRefusal::GpuAmazeDebayer: return "GPU AMaZE debayer route";
    case Debayered16ReconRefusal::GpuBilinearDebayer: return "GPU bilinear debayer route";
    case Debayered16ReconRefusal::GpuReconTextureRoute: return "GPU playback recon texture route";
    case Debayered16ReconRefusal::GpuPlaybackRecon: return "recon ran on the CUDA playback recon (no exactness test covers it)";
    case Debayered16ReconRefusal::FrameCacheMayServe: return "AMaZE frame cache may serve this frame";
    case Debayered16ReconRefusal::ReducedScale: return "recon is at reduced scale";
    case Debayered16ReconRefusal::FrameIdentity: return "recon frame identity does not match the render request";
    case Debayered16ReconRefusal::SettingsChangedDuringRecon: return "llrawproc settings changed while the worker reconstructed";
    case Debayered16ReconRefusal::SettingsChangedSinceRecon: return "llrawproc settings changed since the worker reconstructed";
    case Debayered16ReconRefusal::BufferIncomplete: return "recon buffer is smaller than the frame";
    case Debayered16ReconRefusal::ScratchAllocationFailed: return "recon scratch allocation failed";
    case Debayered16ReconRefusal::DebayerDeclined: return "debayer of the reconstructed raw declined";
    case Debayered16ReconRefusal::GpuDebayerRendered: return "GPU debayer rendered the frame";
    case Debayered16ReconRefusal::Count: break;
    }
    return "unknown";
}

struct Debayered16ReconReuseInputs
{
    /*! The slot reached ProcessReady through the recon worker (renderDecodedSlot's consumeReconnedRaw). */
    bool consumeReconnedRaw = false;
    /*! The recon worker's decode (getMlvRawFrameUint16) of this frame returned success. */
    bool reconAcquisitionSucceeded = false;
    bool playbackActive = false;
    bool useGpuAmazeDebayer = false;
    bool useGpuBilinearDebayer = false;
    /*! An AMaZE frame cache could serve the frame (mlvRawDebayerCacheMayServeFrame). */
    bool frameCacheMayServe = false;
    bool gpuPlaybackReconTexturePresentRequested = false;
    /*! The recon ran on the CUDA playback recon (MLVAPP_GPU_PLAYBACK_RECON opt-in). */
    bool reconUsedGpuPlaybackRecon = false;
    /*! The slot's reducedReconScale: 1 means rawImage16 holds the full-resolution recon. */
    int reducedReconScale = 1;
    /*! Frame number and request serial the recon worker reconstructed, and the ones being rendered. */
    uint32_t reconFrameNumber = 0;
    uint32_t renderFrameNumber = 0;
    uint64_t reconRequestSerial = 0;
    uint64_t renderRequestSerial = 0;
    /*! getMlvLlrawprocSettingsFingerprint when the worker decoded, when its recon
     *  finished, and at the render. */
    uint64_t reconSettingsAtDecode = 0;
    uint64_t reconSettingsAtReconDone = 0;
    uint64_t renderSettings = 0;
    /*! rawImage16 holds at least Width * Height words. */
    bool reconBufferComplete = false;
};

class Debayered16ReconReusePolicy
{
public:
    static bool killSwitchSet( void )
    {
        const char * value = std::getenv( MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV );
        return value && value[0] != '\0' && !( value[0] == '0' && value[1] == '\0' );
    }

    /*! \return None when the render may consume the recon, else why it may not. */
    static Debayered16ReconRefusal refusal( const Debayered16ReconReuseInputs & in )
    {
        if( killSwitchSet() ) return Debayered16ReconRefusal::KillSwitch;
        if( !in.consumeReconnedRaw ) return Debayered16ReconRefusal::NotReconned;
        if( !in.reconAcquisitionSucceeded ) return Debayered16ReconRefusal::AcquisitionFailed;
        if( !in.playbackActive ) return Debayered16ReconRefusal::NotPlaying;
        if( in.useGpuAmazeDebayer ) return Debayered16ReconRefusal::GpuAmazeDebayer;
        if( in.useGpuBilinearDebayer ) return Debayered16ReconRefusal::GpuBilinearDebayer;
        if( in.gpuPlaybackReconTexturePresentRequested ) return Debayered16ReconRefusal::GpuReconTextureRoute;
        if( in.reconUsedGpuPlaybackRecon ) return Debayered16ReconRefusal::GpuPlaybackRecon;
        if( in.frameCacheMayServe ) return Debayered16ReconRefusal::FrameCacheMayServe;
        if( in.reducedReconScale > 1 ) return Debayered16ReconRefusal::ReducedScale;
        if( in.reconFrameNumber != in.renderFrameNumber
         || in.reconRequestSerial != in.renderRequestSerial )
        {
            return Debayered16ReconRefusal::FrameIdentity;
        }
        if( in.reconSettingsAtDecode != in.reconSettingsAtReconDone )
        {
            return Debayered16ReconRefusal::SettingsChangedDuringRecon;
        }
        if( in.reconSettingsAtReconDone != in.renderSettings )
        {
            return Debayered16ReconRefusal::SettingsChangedSinceRecon;
        }
        if( !in.reconBufferComplete ) return Debayered16ReconRefusal::BufferIncomplete;
        return Debayered16ReconRefusal::None;
    }

    /*! \return nullptr when the render may consume the recon, else the reason it may not. */
    static const char * ineligibleReason( const Debayered16ReconReuseInputs & in )
    {
        const Debayered16ReconRefusal why = refusal( in );
        return why == Debayered16ReconRefusal::None ? nullptr : debayered16ReconRefusalReason( why );
    }
};

/*! Every debayered-16 render attempt by outcome, whether or not it is presented. */
struct Debayered16ReconReuseCounters
{
    std::array<std::atomic<uint64_t>, static_cast<size_t>( Debayered16ReconRefusal::Count )> byOutcome{};

    void note( Debayered16ReconRefusal outcome )
    {
        byOutcome[static_cast<size_t>( outcome )].fetch_add( 1, std::memory_order_relaxed );
    }
    uint64_t count( Debayered16ReconRefusal outcome ) const
    {
        return byOutcome[static_cast<size_t>( outcome )].load( std::memory_order_relaxed );
    }
    uint64_t consumed( void ) const { return count( Debayered16ReconRefusal::None ); }
    uint64_t ownRecon( void ) const
    {
        uint64_t total = 0;
        for( size_t i = 1; i < byOutcome.size(); ++i ) total += byOutcome[i].load( std::memory_order_relaxed );
        return total;
    }
};

/*! The render's OutputDebayered16 CPU branch for one frame. On entry the first
 *  pixelCount words of slotRawImage16 hold the slot's recon (when there is one);
 *  on return it holds the debayered frame (pixelCount * 3 words), from the recon
 *  when the policy admits it, else from today's getMlvRawFrameDebayered. The
 *  caller fills every input except frameCacheMayServe and renderSettings, which
 *  are read here from the live object at the moment of the decision. */
inline Debayered16ReconRefusal renderDebayered16FromSlot( mlvObject_t * video,
                                                          uint32_t frameNumber,
                                                          Debayered16ReconReuseInputs reuse,
                                                          uint16_t * slotRawImage16,
                                                          size_t pixelCount,
                                                          std::vector<float> & scratch,
                                                          Debayered16ReconReuseCounters * counters )
{
    reuse.frameCacheMayServe =
        !video || mlvRawDebayerCacheMayServeFrame( video, frameNumber ) != 0;
    reuse.renderSettings = video ? getMlvLlrawprocSettingsFingerprint( video ) : 0;
    Debayered16ReconRefusal outcome = Debayered16ReconReusePolicy::refusal( reuse );
    if( outcome == Debayered16ReconRefusal::None )
    {
        try
        {
            scratch.resize( pixelCount );
        }
        catch( const std::bad_alloc & )
        {
            outcome = Debayered16ReconRefusal::ScratchAllocationFailed;
        }
    }
    if( outcome == Debayered16ReconRefusal::None )
    {
        /* The debayer writes pixelCount * 3 words over slotRawImage16. */
        std::copy_n( slotRawImage16, pixelCount, reinterpret_cast<uint16_t *>( scratch.data() ) );
        if( !getMlvRawFrameDebayeredFromReconnedRaw16( video, frameNumber, scratch.data(), slotRawImage16 ) )
        {
            outcome = Debayered16ReconRefusal::DebayerDeclined;
        }
    }
    if( outcome != Debayered16ReconRefusal::None )
    {
        getMlvRawFrameDebayered( video, frameNumber, slotRawImage16 );
    }
    if( counters ) counters->note( outcome );
    return outcome;
}

#endif // DEBAYERED16RECONREUSEPOLICY_H
