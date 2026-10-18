/*!
 * \file PlaybackReconWarmRun.h
 * \brief The paused recon warm run itself (PLAYBACK-STARTUP-WAIT-S1-RECON-WARM-RUN-1 Row W); policy and log line in
 *        PlaybackReconWarmRunPolicy.h.
 *
 * What the recon worker does for Play frame 0 on the CUDA texture route (RenderFrameThread::reconFrameForWorker), for
 * one paused frame, on the CALLING thread with ITS llrawproc worker state: the set_luts key holds that worker's
 * scratch pointers (dualiso.c: state->raw2ev = scratch->ev_raw2ev), so a warm on any other worker state would leave
 * set_luts to Play. RenderFrameThread runs it on the recon worker, which then runs Play frame 0.
 *
 * Nothing a later request reads is written: the decode bypasses the LJ92 prefetch ring
 * (getMlvRawFrameUint16Uncached), the frame goes into the caller's scratch buffers, and this thread's last-run recon
 * state is saved before and restored after (llrpGpuPlaybackReconSaveLastRunState). On success the retained device
 * output is the caller's to release. Qt-free, so the pipeline tests drive it against the fake backend.
 */

#ifndef PLAYBACKRECONWARMRUN_H
#define PLAYBACKRECONWARMRUN_H

#include "PlaybackReconWarmRunPolicy.h"
#include "../../src/mlv_include.h"
#include "../../src/mlv/pipeline_stage_capture.h"
#include "../../src/debug/StageTiming.h"

#include <cstdint>
#include <cstring>
#include <new>
#include <vector>

namespace playback_recon_warm_run
{

struct Outcome
{
    bool ran = false;            // the prepare-only recon ran and retained an output
    Reason reason = Reason::None;
    int scale = 1;
    int width = 0;               // the retained output's dims (reduced at x2/x4)
    int height = 0;
    double decodeMs = 0.0;
    double runMs = 0.0;          // the llrawproc call: prepare + preupload + recon (set_clip / set_luts inside)
    double setClipMs = 0.0;
    double setLutsMs = 0.0;
    uint64_t setClipCalls = 0;
    uint64_t setLutsCalls = 0;
    // PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-WARM-OUTPUT-REUSE-1 R2: the process-wide set_clip / set_luts counts right
    // after this run (the warm key: a later configuration call means the warm's output may be stale)
    uint64_t setClipCountAfter = 0;
    uint64_t setLutsCountAfter = 0;
    int backendRc = 0;
    llrpGpuPlaybackRetainedDeviceBayer16_t retained;   // valid when ran; the caller releases retained.token

    Outcome() { std::memset( &retained, 0, sizeof( retained ) ); }
};

/*! The three thread opt-ins of a Play texture-route frame (RenderFrameThread's GpuPlaybackReconScope,
 *  ...TexturePresentScope and ...PrepareOnlyScope), cleared on exit exactly as they are. */
struct ThreadOptIn
{
    ThreadOptIn()
    {
        llrpSetGpuPlaybackReconAllowedForCurrentThread( 1 );
        llrpSetGpuPlaybackReconTexturePresentPreferredForCurrentThread( 1 );
        llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread( 1 );
    }
    ~ThreadOptIn()
    {
        llrpSetGpuPlaybackReconTexturePrepareOnlyForCurrentThread( 0 );
        llrpSetGpuPlaybackReconTexturePresentPreferredForCurrentThread( 0 );
        llrpSetGpuPlaybackReconAllowedForCurrentThread( 0 );
    }
};

inline Outcome run( mlvObject_t *video,
                    uint32_t frame,
                    int scale,
                    int fullWidth,
                    int fullHeight,
                    llrawprocWorkerState_t *worker,
                    int threads,
                    std::vector<uint16_t> &raw,
                    std::vector<uint16_t> &reduced )
{
    Outcome out;
    out.scale = scale;
    if( mlv_pipeline_capture_enabled() )
    {
        out.reason = Reason::PipelineCapture;
        return out;
    }
    if( !video || !worker || fullWidth <= 0 || fullHeight <= 0 )
    {
        out.reason = Reason::DecodeFailed;
        return out;
    }
    // x2/x4: the reduced plan the recon worker uses, not latched (no play session exists yet)
    mlvDualIsoPreviewScaleRecon_t plan;
    std::memset( &plan, 0, sizeof( plan ) );
    if( scale != 1
     && ( ( scale != 2 && scale != 4 )
       || !mlvDualIsoGpuPreviewScaleReconPlan( video, scale, 0, &plan )
       || plan.scale != scale ) )
    {
        out.reason = Reason::PlanRefused;
        return out;
    }
    const size_t words = static_cast<size_t>( fullWidth ) * static_cast<size_t>( fullHeight );
    try
    {
        raw.resize( words );
        if( scale != 1 )
            reduced.resize( static_cast<size_t>( plan.reducedWidth ) * static_cast<size_t>( plan.reducedHeight ) );
    }
    catch( const std::bad_alloc & )
    {
        out.reason = Reason::DecodeFailed;
        return out;
    }

    void *saved = llrpGpuPlaybackReconSaveLastRunState();
    if( !saved )
    {
        out.reason = Reason::SnapshotFailed;
        return out;
    }
    const double decodeStart = mlv_stage_timing_now();
    const bool decoded = getMlvRawFrameUint16Uncached( video, frame, raw.data() ) == 0;
    out.decodeMs = ( mlv_stage_timing_now() - decodeStart ) * 1000.0;
    if( !decoded )
    {
        out.reason = Reason::DecodeFailed;
    }
    else
    {
        const uint64_t setClipBefore = llrpGpuPlaybackReconSetClipCount();
        const uint64_t setLutsBefore = llrpGpuPlaybackReconSetLutsCount();
        const double runStart = mlv_stage_timing_now();
        {
            const ThreadOptIn optIn;
            mlv_pipeline_capture_set_current_frame( frame );
            if( scale != 1 )
            {
                (void)mlvDualIsoGpuPreviewScaleReconRun( video, &plan, raw.data(), reduced.data(), worker,
                                                         threads > 0 ? threads : 1, nullptr, nullptr );
            }
            else
            {
                applyLLRawProcObjectWorker( video, raw.data(), words * sizeof( uint16_t ), worker, 0 );
            }
        }
        out.runMs = ( mlv_stage_timing_now() - runStart ) * 1000.0;
        out.setClipCountAfter = llrpGpuPlaybackReconSetClipCount();
        out.setLutsCountAfter = llrpGpuPlaybackReconSetLutsCount();
        out.setClipCalls = out.setClipCountAfter - setClipBefore;
        out.setLutsCalls = out.setLutsCountAfter - setLutsBefore;
        // this thread's run_backend just measured them (0 when the run needed neither)
        out.setClipMs = out.setClipCalls ? llrpGpuPlaybackReconLastSetClipMilliseconds() : 0.0;
        out.setLutsMs = out.setLutsCalls ? llrpGpuPlaybackReconLastSetLutsMilliseconds() : 0.0;
        out.backendRc = llrpGpuPlaybackReconLastRunRcForTesting();
        llrpGpuPlaybackRetainedDeviceBayer16_t retained;
        std::memset( &retained, 0, sizeof( retained ) );
        if( llrpGpuPlaybackReconGetLastRetainedDeviceBayer16( &retained ) != 0
         && retained.valid
         && retained.device_bayer16
         && retained.token != 0 )
        {
            out.retained = retained;
            out.width = retained.width;
            out.height = retained.height;
            out.ran = true;
        }
        else
        {
            out.reason = Reason::ReconNotRetained;
        }
    }
    llrpGpuPlaybackReconRestoreLastRunState( saved );
    return out;
}

} // namespace playback_recon_warm_run

#endif // PLAYBACKRECONWARMRUN_H
