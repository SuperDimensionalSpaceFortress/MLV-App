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
 */

#ifndef DEBAYERED16RECONREUSEPOLICY_H
#define DEBAYERED16RECONREUSEPOLICY_H

#include <cstdint>
#include <cstdlib>

/*! Kill switch: any non-empty value other than "0" forces today's path. */
#define MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV "MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE"

struct Debayered16ReconReuseInputs
{
    /*! The slot reached ProcessReady through the recon worker (renderDecodedSlot's consumeReconnedRaw). */
    bool consumeReconnedRaw = false;
    bool playbackActive = false;
    bool useGpuAmazeDebayer = false;
    bool useGpuBilinearDebayer = false;
    /*! An AMaZE frame cache could serve the frame (mlvRawDebayerCacheMayServeFrame). */
    bool frameCacheMayServe = false;
    bool gpuPlaybackReconTexturePresentRequested = false;
    /*! The slot's reducedReconScale: 1 means rawImage16 holds the full-resolution recon. */
    int reducedReconScale = 1;
    /*! Frame number and request serial the recon worker reconstructed, and the ones being rendered. */
    uint32_t reconFrameNumber = 0;
    uint32_t renderFrameNumber = 0;
    uint64_t reconRequestSerial = 0;
    uint64_t renderRequestSerial = 0;
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

    /*! \return nullptr when the render may consume the recon, else the reason it may not. */
    static const char * ineligibleReason( const Debayered16ReconReuseInputs & in )
    {
        if( killSwitchSet() ) return MLVAPP_DISABLE_DEBAYERED16_RECON_REUSE_ENV " is set";
        if( !in.consumeReconnedRaw ) return "slot was not reconstructed by the phase-3 recon worker";
        if( !in.playbackActive ) return "not playing (paused and scrubbed frames keep the full render)";
        if( in.useGpuAmazeDebayer ) return "GPU AMaZE debayer route";
        if( in.useGpuBilinearDebayer ) return "GPU bilinear debayer route";
        if( in.gpuPlaybackReconTexturePresentRequested ) return "GPU playback recon texture route";
        if( in.frameCacheMayServe ) return "AMaZE frame cache may serve this frame";
        if( in.reducedReconScale > 1 ) return "recon is at reduced scale";
        if( in.reconFrameNumber != in.renderFrameNumber
         || in.reconRequestSerial != in.renderRequestSerial )
        {
            return "recon frame identity does not match the render request";
        }
        if( !in.reconBufferComplete ) return "recon buffer is smaller than the frame";
        return nullptr;
    }
};

#endif // DEBAYERED16RECONREUSEPOLICY_H
