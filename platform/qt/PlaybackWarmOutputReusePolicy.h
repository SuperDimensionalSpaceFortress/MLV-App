/*!
 * \file PlaybackWarmOutputReusePolicy.h
 * \brief Whether Play's first recon present may keep the paused warm's AMaZE output instead of running AMaZE again,
 *        and its log fields (pure, unit-tested).
 *
 * PLAYBACK-STARTUP-WAIT-S1-FIRST-PRESENT-WARM-OUTPUT-REUSE-1 R2. The paused recon warm (PLAYBACK-STARTUP-WAIT-S1-RECON-
 * WARM-RUN-1) already runs frame 0's AMaZE into the staged output texture that the first Play present adopts
 * (GpuDisplayWindow::warmStagedReconSlotFromDeviceBayer16); that present then ran the same AMaZE again into the same
 * texture, at the idle GPU clock. The output is linear post-WB-undo camera RGB: LUTs, look and blur are applied later,
 * at paint, from the present's own options, so they are never part of this key.
 *
 * A parameter-only key is not enough: llrpGpuPlaybackReconState_t holds the recon settings but not the CPU llrawproc
 * stages before them, and the set_clip / set_luts counters count configuration calls, not content. So the warm keeps a
 * device copy of its AMaZE input (igpu_amaze_debayer_capture_input_bayer16) and the present compares its own input
 * against it exactly (igpu_amaze_debayer_input_bayer16_matches_capture) after preGate() passes on everything else the
 * AMaZE output depends on (igpu_amaze_debayer_cuda.cu): the dims, black level, the three WB doubles (exact ==) and the
 * reduced H-Nyquist flag. The key is set only after the warm's AMaZE succeeded, is consumed by the first Play present
 * whatever happens there, and any other outcome runs AMaZE exactly as before.
 *
 * MLVAPP_PLAYBACK_WARM_OUTPUT_REUSE=0 (or false/off/no) turns it off; default on.
 *
 *   ... warm_output_reuse=<hit|miss|skip> reuse_reason=<name> reuse_compare_ms=<x>
 *
 * hit: AMaZE was skipped (the staged output is kept). miss: preGate passed and the compare said different, or failed.
 * skip: preGate refused before any compare (no CUDA call).
 */

#ifndef PLAYBACKWARMOUTPUTREUSEPOLICY_H
#define PLAYBACKWARMOUTPUTREUSEPOLICY_H

#include <QString>

#include <cstdint>

namespace playback_warm_output_reuse
{

enum class Reason
{
    None,                       // preGate: every field matches, go compare
    Disabled,                   // MLVAPP_PLAYBACK_WARM_OUTPUT_REUSE=0
    NotAdopted,                 // this present did not adopt the staged pair (realloc, kept, discarded)
    NoWarmKey,                  // no successful warm since the slot was staged (or the key was consumed)
    ValidationProbe,            // a validation-probe present always runs the full path
    CudaSuppressed,             // GPU-DEVICE-LOSS-SELF-HEAL: no CUDA call after a device loss
    FrameMismatch,              // the present is not the warm's frame (a scrub after the warm)
    SizeMismatch,               // other dims
    BlackMismatch,
    WbMismatch,                 // any of the three doubles not exactly equal
    HnyquistMismatch,
    ReconConfiguredSinceWarm,   // a set_clip or set_luts ran after the warm
    // final outcomes after the compare
    Hit,
    ContentMismatch,            // the AMaZE input bytes differ
    CompareFailed,              // the compare's CUDA work failed: AMaZE runs
    SymbolMissing,              // an older AMaZE DLL without the capture exports
    CaptureMissing              // the backend holds no capture (released, or a recreated backend)
};

/*! What the warm's AMaZE ran on (warmKey), or what this present's AMaZE would run on (presentKey; its two counts are
 *  unused there: preGate takes the live counts). */
struct Key
{
    bool valid = false;
    uint64_t frame = 0;
    int width = 0;
    int height = 0;
    int blackLevel = 0;
    double wb[3] = { 0.0, 0.0, 0.0 };
    bool reducedHnyquist = false;
    uint64_t setClipCountAfterWarm = 0;
    uint64_t setLutsCountAfterWarm = 0;
    uint64_t captureId = 0;
};

/*! Everything but the content: Reason::None means compare the input bytes against warmKey.captureId. Exact double
 *  equality on WB on purpose (a 1-ulp difference is a different AMaZE output). */
inline Reason preGate( bool enabled,
                       bool adopted,
                       const Key &warmKey,
                       const Key &presentKey,
                       bool cudaSuppressed,
                       bool validationProbe,
                       uint64_t setClipNow,
                       uint64_t setLutsNow )
{
    if( !enabled ) return Reason::Disabled;
    if( !adopted ) return Reason::NotAdopted;
    if( !warmKey.valid || warmKey.captureId == 0 ) return Reason::NoWarmKey;
    if( validationProbe ) return Reason::ValidationProbe;
    if( cudaSuppressed ) return Reason::CudaSuppressed;
    if( warmKey.frame != presentKey.frame ) return Reason::FrameMismatch;
    if( warmKey.width != presentKey.width || warmKey.height != presentKey.height ) return Reason::SizeMismatch;
    if( warmKey.blackLevel != presentKey.blackLevel ) return Reason::BlackMismatch;
    for( int i = 0; i < 3; ++i )
    {
        if( !( warmKey.wb[i] == presentKey.wb[i] ) ) return Reason::WbMismatch;
    }
    if( warmKey.reducedHnyquist != presentKey.reducedHnyquist ) return Reason::HnyquistMismatch;
    if( setClipNow != warmKey.setClipCountAfterWarm || setLutsNow != warmKey.setLutsCountAfterWarm )
        return Reason::ReconConfiguredSinceWarm;
    return Reason::None;
}

/*! The final reason after preGate passed: the compare's result, or why it did not complete (the why of
 *  gpuAmazeDebayerInputBayer16MatchesCapture). */
inline Reason afterCompare( bool compareCompleted, bool equal, const QString &why )
{
    if( compareCompleted ) return equal ? Reason::Hit : Reason::ContentMismatch;
    if( why == QLatin1String( "symbol_missing" ) ) return Reason::SymbolMissing;
    if( why == QLatin1String( "capture_missing" ) ) return Reason::CaptureMissing;
    if( why == QLatin1String( "cuda_suppressed" ) ) return Reason::CudaSuppressed;
    return Reason::CompareFailed;
}

/*! Reuse only on a hit: every other reason runs AMaZE unchanged. */
inline bool reuse( Reason reason ) { return reason == Reason::Hit; }

inline const char *reasonName( Reason reason )
{
    switch( reason )
    {
    case Reason::None: return "none";
    case Reason::Disabled: return "disabled";
    case Reason::NotAdopted: return "not_adopted";
    case Reason::NoWarmKey: return "no_warm_key";
    case Reason::ValidationProbe: return "validation_probe";
    case Reason::CudaSuppressed: return "cuda_suppressed";
    case Reason::FrameMismatch: return "frame_mismatch";
    case Reason::SizeMismatch: return "size_mismatch";
    case Reason::BlackMismatch: return "black_mismatch";
    case Reason::WbMismatch: return "wb_mismatch";
    case Reason::HnyquistMismatch: return "hnyquist_mismatch";
    case Reason::ReconConfiguredSinceWarm: return "recon_configured_since_warm";
    case Reason::Hit: return "hit";
    case Reason::ContentMismatch: return "content_mismatch";
    case Reason::CompareFailed: return "compare_failed";
    case Reason::SymbolMissing: return "symbol_missing";
    case Reason::CaptureMissing: return "capture_missing";
    }
    return "none";
}

/*! hit, miss (a compare was attempted: content_mismatch, compare_failed, symbol_missing, capture_missing, or a loss
 *  seen under the live lock) or skip (refused before any compare). */
inline const char *outcomeName( Reason reason, bool compareAttempted )
{
    if( reason == Reason::Hit ) return "hit";
    return compareAttempted ? "miss" : "skip";
}

/*! " warm_output_reuse=<hit|miss|skip> reuse_reason=<name> reuse_compare_ms=<x>" (concatenated; never .arg()-chained). */
inline QString fields( const QString &outcome, const QString &reason, double compareMs )
{
    return QStringLiteral( " warm_output_reuse=" ) + ( outcome.isEmpty() ? QStringLiteral( "skip" ) : outcome )
         + QStringLiteral( " reuse_reason=" ) + ( reason.isEmpty() ? QStringLiteral( "none" ) : reason )
         + QStringLiteral( " reuse_compare_ms=" ) + QString::number( compareMs, 'f', 3 );
}

/*! The warm line's capture field: " reuse_capture=<ok|failed|symbol_missing|none>". */
inline QString captureField( const QString &capture )
{
    return QStringLiteral( " reuse_capture=" ) + ( capture.isEmpty() ? QStringLiteral( "none" ) : capture );
}

} // namespace playback_warm_output_reuse

#endif // PLAYBACKWARMOUTPUTREUSEPOLICY_H
