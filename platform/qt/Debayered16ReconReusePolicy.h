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
 *
 * CPU-DEBAYERED16-HONOUR-SCALE-1: at preview scale 2/4 the recon worker may
 * reconstruct this route's frame at the reduced scale too, when the request was
 * admitted (debayered16HonourScaleRefusal, decided on the GUI thread and carried
 * in the request's PresentationContext). The render then debayers the reduced
 * recon (getMlvRawFrameDebayeredFromReducedReconnedRaw16) at the dims the
 * playback prep would have box-reduced the full frame to. A reduced slot whose
 * request was not admitted keeps the ReducedScale refusal: its rawImage16 is not
 * a recon, so today's getMlvRawFrameDebayered renders it at full size.
 *
 * CPU-DUALISO-PREVIEW-SCALE-VERIFY-1: with Phase 3 Disabled (quality modes 0-2) no
 * recon worker runs, so an admitted request reached the render unreconned and
 * rendered the full frame. debayered16InlineReducedReconWanted admits such a frame
 * and debayered16RunInlineReducedRecon reconstructs it on the render thread with
 * the #271/#405 pieces; renderDebayered16FromSlot then consumes it unchanged.
 */

#ifndef DEBAYERED16RECONREUSEPOLICY_H
#define DEBAYERED16RECONREUSEPOLICY_H

#include "../../src/debug/StageTiming.h"
#include "../../src/mlv/pipeline_stage_capture.h"
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

/*! CPU-DEBAYERED16-HONOUR-SCALE-1 switch: exactly "0" disables the reduced recon on
 *  the debayered-16 route (today's full-resolution route); unset, empty or any
 *  other value enables it. */
#define MLVAPP_CPU_DEBAYERED16_HONOUR_SCALE_ENV "MLVAPP_CPU_DEBAYERED16_HONOUR_SCALE"

/*! Why a debayered-16 request does not ask the recon worker for the reduced
 *  recon. None = it does. */
enum class Debayered16HonourScaleRefusal : int
{
    None = 0,
    KillSwitch,
    NotPlaying,
    NotCpuPreviewRoute,
    GpuPreviewRoute,
    GpuDebayer,
    GpuReconTextureRoute,
    AmazeDebayer,
    ScaleUnsupported,
    PrepWouldNotReduce,
    Count
};

/*! Stable telemetry name of each admission outcome. */
inline const char * debayered16HonourScaleRefusalName( Debayered16HonourScaleRefusal refusal )
{
    switch( refusal )
    {
    case Debayered16HonourScaleRefusal::None: return "admitted";
    case Debayered16HonourScaleRefusal::KillSwitch: return "kill_switch";
    case Debayered16HonourScaleRefusal::NotPlaying: return "not_playing";
    case Debayered16HonourScaleRefusal::NotCpuPreviewRoute: return "not_cpu_preview_route";
    case Debayered16HonourScaleRefusal::GpuPreviewRoute: return "gpu_preview_route";
    case Debayered16HonourScaleRefusal::GpuDebayer: return "gpu_debayer";
    case Debayered16HonourScaleRefusal::GpuReconTextureRoute: return "gpu_recon_texture_route";
    case Debayered16HonourScaleRefusal::AmazeDebayer: return "amaze_debayer";
    case Debayered16HonourScaleRefusal::ScaleUnsupported: return "scale_unsupported";
    case Debayered16HonourScaleRefusal::PrepWouldNotReduce: return "prep_would_not_reduce";
    case Debayered16HonourScaleRefusal::Count: break;
    }
    return "unknown";
}

/*! Human-readable reason, for the log line and cpu_dualiso_recon_fallback_reason. */
inline const char * debayered16HonourScaleRefusalReason( Debayered16HonourScaleRefusal refusal )
{
    switch( refusal )
    {
    case Debayered16HonourScaleRefusal::None: return "";
    case Debayered16HonourScaleRefusal::KillSwitch:
        return "debayered16 reduced recon disabled by " MLVAPP_CPU_DEBAYERED16_HONOUR_SCALE_ENV "=0";
    case Debayered16HonourScaleRefusal::NotPlaying: return "debayered16 reduced recon: not playing";
    case Debayered16HonourScaleRefusal::NotCpuPreviewRoute:
        return "debayered16 reduced recon: not the CPU preview-processing route";
    case Debayered16HonourScaleRefusal::GpuPreviewRoute:
        return "debayered16 reduced recon: GPU preview-processing route";
    case Debayered16HonourScaleRefusal::GpuDebayer: return "debayered16 reduced recon: GPU debayer route";
    case Debayered16HonourScaleRefusal::GpuReconTextureRoute:
        return "debayered16 reduced recon: GPU playback recon texture route";
    case Debayered16HonourScaleRefusal::AmazeDebayer:
        return "debayered16 reduced recon: the debayer is not bilinear";
    case Debayered16HonourScaleRefusal::ScaleUnsupported:
        return "debayered16 reduced recon: preview scale is not 2 or 4";
    case Debayered16HonourScaleRefusal::PrepWouldNotReduce:
        return "debayered16 reduced recon: the playback prep would not box-reduce this frame to the preview scale";
    case Debayered16HonourScaleRefusal::Count: break;
    }
    return "unknown";
}

/*! What MainWindow::drawFrame knows about the request when it decides. */
struct Debayered16HonourScaleInputs
{
    bool playbackActive = false;
    /*! PresentationContext::renderThreadUsingCpuPreviewProcessing / ...GpuPreviewProcessing. */
    bool cpuPreviewRoute = false;
    bool gpuPreviewRoute = false;
    /*! PresentationContext::renderThreadUsingGpuAmazeDebayer / ...GpuBilinearDebayer, the
     *  fields that drive the render's useGpuAmazeDebayer / useGpuBilinearDebayer. */
    bool gpuAmazeDebayer = false;
    bool gpuBilinearDebayer = false;
    /*! PresentationContext::gpuPlaybackReconTexturePresentRequested. */
    bool gpuReconTextureRoute = false;
    /*! doesMlvAlwaysUseAmaze != 0: the route's CPU debayer is not bilinear. */
    bool amazeDebayer = false;
    /*! PresentationContext::playbackScaleFactor. */
    int requestedScale = 1;
    /*! playbackPrepDecideReduced(...).factor with the prep's own inputs but the
     *  full-resolution clip dims as the source: what the prep would box-reduce the
     *  full frame by. */
    int prepFactor = 1;
};

inline bool debayered16HonourScaleKillSwitchSet( void )
{
    const char * value = std::getenv( MLVAPP_CPU_DEBAYERED16_HONOUR_SCALE_ENV );
    return value && value[0] == '0' && value[1] == '\0';
}

/*! \return None when the request may ask for the reduced recon, else why not. */
inline Debayered16HonourScaleRefusal debayered16HonourScaleRefusal( const Debayered16HonourScaleInputs & in )
{
    if( debayered16HonourScaleKillSwitchSet() ) return Debayered16HonourScaleRefusal::KillSwitch;
    if( !in.playbackActive ) return Debayered16HonourScaleRefusal::NotPlaying;
    if( !in.cpuPreviewRoute ) return Debayered16HonourScaleRefusal::NotCpuPreviewRoute;
    if( in.gpuPreviewRoute ) return Debayered16HonourScaleRefusal::GpuPreviewRoute;
    if( in.gpuAmazeDebayer || in.gpuBilinearDebayer ) return Debayered16HonourScaleRefusal::GpuDebayer;
    if( in.gpuReconTextureRoute ) return Debayered16HonourScaleRefusal::GpuReconTextureRoute;
    if( in.amazeDebayer ) return Debayered16HonourScaleRefusal::AmazeDebayer;
    if( in.requestedScale != 2 && in.requestedScale != 4 ) return Debayered16HonourScaleRefusal::ScaleUnsupported;
    if( in.prepFactor != in.requestedScale ) return Debayered16HonourScaleRefusal::PrepWouldNotReduce;
    return Debayered16HonourScaleRefusal::None;
}

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
    /*! CPU-DEBAYERED16-HONOUR-SCALE-1: the request admitted the reduced recon at the
     *  slot's scale, and the slot's reduced Bayer (reducedReconWidth x
     *  reducedReconHeight words; nullptr when the slot's buffer is shorter). */
    bool honourScaleAdmitted = false;
    const uint16_t * reducedReconBayer = nullptr;
    int reducedReconWidth = 0;
    int reducedReconHeight = 0;
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
    /*! llrpHQDualIso as the recon ran (r3): the bit shift the reconned debayer applies. */
    bool reconHqDualIso = false;
    /*! rawImage16 holds at least Width * Height words. */
    bool reconBufferComplete = false;
    /*! CPU-DUALISO-PREVIEW-SCALE-VERIFY-1 r3: the calling thread decoded and
     *  reconstructed this frame itself (the inline reduced recon), so its last
     *  decode and llrawproc records describe this frame and survive the debayer. */
    bool reconOnCallingThread = false;
};

/*! What the recon worker records when a recon finishes, on the recon thread. */
struct Debayered16ReconDoneStamp
{
    uint64_t settings = 0;
    bool usedGpuPlaybackRecon = false;
    bool hqDualIso = false;
};

/*! How a slot's recon was made. The decode stage sets the first two fields, the
 *  recon-done signal the stamp; a slot nobody stamped is refused (fail closed). */
struct Debayered16ReconProvenance
{
    bool acquisitionSucceeded = false;
    uint64_t settingsAtDecode = 0;
    Debayered16ReconDoneStamp done;
};

/*! The recon-done stamp, read on the recon worker thread right after the recon
 *  (RenderFrameThread::signalReconDoneFromWorker). r3 (sol r2 B1): the HQ flag is
 *  read here, BEFORE the settings fingerprint, so a fingerprint equal to the
 *  decode stamp also covers it: the render applies this flag, never the live one. */
inline Debayered16ReconDoneStamp debayered16ReconDoneStamp( mlvObject_t * video )
{
    Debayered16ReconDoneStamp stamp;
    stamp.hqDualIso = video && llrpHQDualIso( video ) != 0;
    stamp.settings = video ? getMlvLlrawprocSettingsFingerprint( video ) : 0;
    stamp.usedGpuPlaybackRecon = llrpGpuPlaybackReconLastUsedForTesting() != 0;
    return stamp;
}

/*! The stamp signalReconDoneFromWorker takes for one finished recon-queue entry.
 *  PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 r2 (sol r1 B2): only a slot
 *  (index >= 0) is stamped. A paused warm (slot -1) has no slot to store a stamp in,
 *  and has already released its lifetime guard (RenderFrameThread::m_warmRunning),
 *  so a clip close that saw the pipeline idle may have freed the clip: it reads
 *  nothing. `readStamp` is the clip read (debayered16ReconDoneStamp in the app). */
template <typename ReadStamp>
inline Debayered16ReconDoneStamp debayered16ReconDoneStampForEntry( int slotIndex, ReadStamp readStamp )
{
    if( slotIndex < 0 ) return Debayered16ReconDoneStamp();
    return readStamp();
}

/*! The render's copy of a slot's provenance into the policy inputs. */
inline void applyDebayered16ReconProvenance( const Debayered16ReconProvenance & provenance,
                                             Debayered16ReconReuseInputs & in )
{
    in.reconAcquisitionSucceeded = provenance.acquisitionSucceeded;
    in.reconSettingsAtDecode = provenance.settingsAtDecode;
    in.reconSettingsAtReconDone = provenance.done.settings;
    in.reconUsedGpuPlaybackRecon = provenance.done.usedGpuPlaybackRecon;
    in.reconHqDualIso = provenance.done.hqDualIso;
}

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
        if( in.reducedReconScale > 1
         && ( !in.honourScaleAdmitted
           || !in.reducedReconBayer
           || in.reducedReconWidth <= 0
           || in.reducedReconHeight <= 0 ) )
        {
            return Debayered16ReconRefusal::ReducedScale;
        }
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

/*! Test seam: runs after the policy admitted the recon and before the reconned
 *  debayer, the window sol r2 B1 names. Production passes none. */
typedef void (*Debayered16AfterAdmissionHook)( mlvObject_t * video, void * context );

/*! The dims of the frame renderDebayered16FromSlot wrote: the clip's on every
 *  path except the reduced recon, which writes Width/scale x Height/scale. */
struct Debayered16RenderedDims
{
    int width = 0;
    int height = 0;
    int reducedScale = 1;
};

/*! The render's OutputDebayered16 CPU branch for one frame. On entry the first
 *  pixelCount words of slotRawImage16 hold the slot's recon (when there is one);
 *  on return it holds the debayered frame (pixelCount * 3 words), from the recon
 *  when the policy admits it, else from today's getMlvRawFrameDebayered. The
 *  caller fills every input except frameCacheMayServe and renderSettings, which
 *  are read here from the live object at the moment of the decision. After the
 *  decision nothing here re-reads a setting the fingerprint covers: the bit shift
 *  comes from the slot's reconHqDualIso (r3).
 *  CPU-DEBAYERED16-HONOUR-SCALE-1: an admitted reduced slot is debayered from its
 *  reduced Bayer instead (fewer words; renderedDims says how many). */
inline Debayered16ReconRefusal renderDebayered16FromSlot( mlvObject_t * video,
                                                          uint32_t frameNumber,
                                                          Debayered16ReconReuseInputs reuse,
                                                          uint16_t * slotRawImage16,
                                                          size_t pixelCount,
                                                          std::vector<float> & scratch,
                                                          Debayered16ReconReuseCounters * counters,
                                                          Debayered16AfterAdmissionHook afterAdmissionForTesting = nullptr,
                                                          void * afterAdmissionContext = nullptr,
                                                          Debayered16RenderedDims * renderedDims = nullptr )
{
    reuse.frameCacheMayServe =
        !video || mlvRawDebayerCacheMayServeFrame( video, frameNumber ) != 0;
    reuse.renderSettings = video ? getMlvLlrawprocSettingsFingerprint( video ) : 0;
    Debayered16ReconRefusal outcome = Debayered16ReconReusePolicy::refusal( reuse );
    Debayered16RenderedDims dims;
    dims.width = video ? static_cast<int>( getMlvWidth( video ) ) : 0;
    dims.height = video ? static_cast<int>( getMlvHeight( video ) ) : 0;
    if( outcome == Debayered16ReconRefusal::None && reuse.reducedReconScale > 1 )
    {
        if( afterAdmissionForTesting ) afterAdmissionForTesting( video, afterAdmissionContext );
        int reducedWidth = 0;
        int reducedHeight = 0;
        /* r3 (ADV-A1): the reduced debayer clears the thread's decode and llrawproc
         * records, which after an inline recon are this frame's own. */
        if( reuse.reconOnCallingThread ) mlvSaveLastDecodeAndLlrawprocTelemetryForCurrentThread();
        const int reducedDebayered =
            getMlvRawFrameDebayeredFromReducedReconnedRaw16( video, frameNumber, reuse.reducedReconBayer,
                                                             reuse.reducedReconWidth, reuse.reducedReconHeight,
                                                             reuse.reducedReconScale, slotRawImage16,
                                                             reuse.reconHqDualIso ? 1 : 0,
                                                             &reducedWidth, &reducedHeight );
        if( reuse.reconOnCallingThread ) mlvRestoreLastDecodeAndLlrawprocTelemetryForCurrentThread();
        if( reducedDebayered )
        {
            dims.width = reducedWidth;
            dims.height = reducedHeight;
            dims.reducedScale = reuse.reducedReconScale;
        }
        else
        {
            outcome = Debayered16ReconRefusal::DebayerDeclined;
        }
    }
    else if( outcome == Debayered16ReconRefusal::None )
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
    if( outcome == Debayered16ReconRefusal::None && dims.reducedScale == 1 )
    {
        if( afterAdmissionForTesting ) afterAdmissionForTesting( video, afterAdmissionContext );
        /* The debayer writes pixelCount * 3 words over slotRawImage16. */
        std::copy_n( slotRawImage16, pixelCount, reinterpret_cast<uint16_t *>( scratch.data() ) );
        if( !getMlvRawFrameDebayeredFromReconnedRaw16( video, frameNumber, scratch.data(), slotRawImage16,
                                                       reuse.reconHqDualIso ? 1 : 0 ) )
        {
            outcome = Debayered16ReconRefusal::DebayerDeclined;
        }
    }
    if( outcome != Debayered16ReconRefusal::None )
    {
        /* On a reduced slot rawImage16 is the decoded frame, not a recon: today's
         * path decodes and reconstructs the full frame on its own. */
        getMlvRawFrameDebayered( video, frameNumber, slotRawImage16 );
        dims.reducedScale = 1;
    }
    if( counters ) counters->note( outcome );
    if( renderedDims ) *renderedDims = dims;
    return outcome;
}

/*! What the render knows when it decides to reconstruct a frame itself. */
struct Debayered16InlineReducedReconInputs
{
    /*! The slot reached the render through the recon worker (consumeReconnedRaw). */
    bool slotReconned = false;
    /*! No recon worker runs in this quality mode. A Phase 3 frame that missed the
     *  worker keeps today's full render. */
    bool phase3Disabled = false;
    /*! PresentationContext::cpuDebayered16HonourScaleAdmitted. */
    bool honourScaleAdmitted = false;
    bool playbackActive = false;
    /*! PresentationContext::playbackScaleFactor. */
    int scale = 1;
    /*! mlvRawDebayerCacheMayServeFrame != 0, read before any decode. */
    bool frameCacheMayServe = true;
};

/*! \return true when the render should reconstruct this frame at the reduced scale. */
inline bool debayered16InlineReducedReconWanted( const Debayered16InlineReducedReconInputs & in )
{
    if( Debayered16ReconReusePolicy::killSwitchSet() ) return false;
    if( in.slotReconned ) return false;
    if( !in.phase3Disabled ) return false;
    if( !in.honourScaleAdmitted ) return false;
    if( !in.playbackActive ) return false;
    if( in.scale != 2 && in.scale != 4 ) return false;
    return !in.frameCacheMayServe;
}

/*! One inline reduced recon. Reconstructed false leaves the frame to today's
 *  getMlvRawFrameDebayered, which decodes for itself; fallbackReason says why. */
struct Debayered16InlineReducedRecon
{
    bool reconstructed = false;
    int scale = 1;
    int reducedWidth = 0;
    int reducedHeight = 0;
    Debayered16ReconProvenance provenance;
    /*! The decode ran, so its thread-local telemetry describes this frame. */
    bool decoded = false;
    double decodeMs = 0.0;
    double fullResFixesMs = 0.0;
    double downsampleMs = 0.0;
    /*! llrpGetLastTotalMilliseconds of the recon: the reduced debayer clears it. */
    double llrawprocMs = 0.0;
    /*! The full-resolution fix pass's llrpGetLastPreDualIsoFix*, cleared the same way. */
    bool preDualIsoFixCompleted = false;
    double preDualIsoFixMs = 0.0;
    const char * fallbackReason = "";
};

/*! The recon worker's reduced recon on the calling thread. rawBuffer (rawWords
 *  words) receives the decode. The plan reads no pixels, so a frame it refuses
 *  costs no decode. A null worker state is the calling thread's own pool slot,
 *  which reseeds from the shared object on every call, like the recon worker's. */
inline Debayered16InlineReducedRecon debayered16RunInlineReducedRecon( mlvObject_t * video,
                                                                       uint32_t frameNumber,
                                                                       int scale,
                                                                       int threads,
                                                                       uint16_t * rawBuffer,
                                                                       size_t rawWords,
                                                                       std::vector<uint16_t> & reducedBayer )
{
    Debayered16InlineReducedRecon recon;
    recon.provenance.settingsAtDecode = video ? getMlvLlrawprocSettingsFingerprint( video ) : 0;
    mlvDualIsoPreviewScaleRecon_t plan;
    if( !mlvDualIsoPreviewScaleReconPlan( video, scale, &plan ) )
    {
        recon.fallbackReason = plan.reason;
        return recon;
    }
    if( plan.scale != scale )
    {
        recon.fallbackReason = "reduced recon plan is not at the requested scale";
        return recon;
    }
    if( !rawBuffer
     || rawWords < static_cast<size_t>( plan.fullWidth ) * static_cast<size_t>( plan.fullHeight ) )
    {
        recon.fallbackReason = "raw buffer is smaller than the frame";
        return recon;
    }
    try
    {
        reducedBayer.resize( static_cast<size_t>( plan.reducedWidth )
                             * static_cast<size_t>( plan.reducedHeight ) );
    }
    catch( const std::bad_alloc & )
    {
        recon.fallbackReason = "reduced recon buffer allocation failed";
        return recon;
    }
    const double decodeStart = mlv_stage_timing_now();
    recon.provenance.acquisitionSucceeded =
        getMlvRawFrameUint16( video, frameNumber, rawBuffer ) == 0;
    recon.decoded = true;
    recon.decodeMs = ( mlv_stage_timing_now() - decodeStart ) * 1000.0;
    if( !recon.provenance.acquisitionSucceeded )
    {
        recon.fallbackReason = "the render's decode of this frame failed";
        return recon;
    }
    mlv_pipeline_capture_set_current_frame( frameNumber );
    if( mlvDualIsoPreviewScaleReconRun( video, &plan, rawBuffer, reducedBayer.data(), nullptr, threads,
                                        &recon.fullResFixesMs, &recon.downsampleMs ) != 1 )
    {
        recon.fallbackReason = "reduced recon rejected by the scaled llrawproc subset";
        return recon;
    }
    recon.llrawprocMs = llrpGetLastTotalMilliseconds();
    recon.preDualIsoFixCompleted = llrpGetLastPreDualIsoFixCompleted() != 0;
    recon.preDualIsoFixMs = llrpGetLastPreDualIsoFixMilliseconds();
    recon.provenance.done = debayered16ReconDoneStamp( video );
    recon.scale = plan.scale;
    recon.reducedWidth = plan.reducedWidth;
    recon.reducedHeight = plan.reducedHeight;
    recon.reconstructed = true;
    return recon;
}

/*! The policy inputs of a frame the render reconstructed itself: its own recon,
 *  so frame and serial are the render's. */
inline void applyDebayered16InlineReducedRecon( const Debayered16InlineReducedRecon & recon,
                                                uint32_t frameNumber,
                                                uint64_t requestSerial,
                                                Debayered16ReconReuseInputs & in )
{
    in.consumeReconnedRaw = recon.reconstructed;
    in.reconOnCallingThread = recon.reconstructed;
    applyDebayered16ReconProvenance( recon.provenance, in );
    in.reconFrameNumber = frameNumber;
    in.renderFrameNumber = frameNumber;
    in.reconRequestSerial = requestSerial;
    in.renderRequestSerial = requestSerial;
}

#endif // DEBAYERED16RECONREUSEPOLICY_H
