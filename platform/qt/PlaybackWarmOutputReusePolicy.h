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
 * whatever happens there, and any other outcome runs AMaZE exactly as before. PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-
 * DETERMINISM-1 F-key adds the recon's per-run constants (PreparedState): a difference there is state_mismatch,
 * decided before any compare, and a state or content miss logs both states.
 *
 * MLVAPP_PLAYBACK_WARM_OUTPUT_REUSE=1 (or true/on/yes) turns it on; default OFF since WARM-RECON-DETERMINISM-1 (no
 * venue has shown it hitting yet, and a miss pays the compare).
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
    Disabled,                   // MLVAPP_PLAYBACK_WARM_OUTPUT_REUSE not on (the default)
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
    StateMismatch,              // WARM-RECON-DETERMINISM-1 F-key: the recon's per-run constants differ (or one is unknown)
    // final outcomes after the compare
    Hit,
    ContentMismatch,            // the AMaZE input bytes differ
    CompareFailed,              // the compare's CUDA work failed: AMaZE runs
    SymbolMissing,              // an older AMaZE DLL without the capture exports
    CaptureMissing              // the backend holds no capture (released, or a recreated backend)
};

/*! PLAYBACK-STARTUP-WAIT-S1-WARM-RECON-DETERMINISM-1 F-key: the recon's per-run constants (llrpGpuPlaybackReconState_t's
 *  prepared fields). The GPU recon takes them with each run (cudaMemcpyToSymbol in igpu_recon_cuda.cu), not through
 *  set_clip / set_luts, so the counters cannot see a change in them; the warm's and Play's must be equal before any
 *  compare. playback_preview_scale_factor is not part of it: the present's state never carries it
 *  (MainWindow::buildGpuPlaybackReconStateForTexturePresent leaves it 0), and the dims already key the scale. */
struct PreparedState
{
    bool valid = false;
    int whiteDarkened = 0;
    int blackDelta = 0;
    double evCorrection = 0.0;
    double darkNoise = 0.0;
    int interpMethod = 0;
    int useAliasMap = 0;
    int useFullres = 0;
    int chromaSmoothMethod = 0;
    int applyDither = 0;
    int isBright[4] = { 0, 0, 0, 0 };
    uint64_t frameId = 0;
};

/*! Reads the fields by name from an llrpGpuPlaybackReconState_t (a template, so this header needs no llrawproc include). */
template <typename ReconState>
inline PreparedState preparedStateFrom( const ReconState &s )
{
    PreparedState p;
    p.valid = s.valid != 0;
    p.whiteDarkened = s.white_darkened;
    p.blackDelta = s.black_delta;
    p.evCorrection = s.ev_correction;
    p.darkNoise = s.dark_noise;
    p.interpMethod = s.interp_method;
    p.useAliasMap = s.use_alias_map;
    p.useFullres = s.use_fullres;
    p.chromaSmoothMethod = s.chroma_smooth_method;
    p.applyDither = s.apply_dither;
    for( int i = 0; i < 4; ++i ) p.isBright[i] = s.is_bright[i];
    p.frameId = static_cast<uint64_t>( s.frame_id );
    return p;
}

/*! The names of the fields that differ, comma-separated; "valid" alone when only one side is known; "" when equal. The
 *  doubles compare exactly (one ulp apart is a different recon). */
inline QString preparedStateDiff( const PreparedState &a, const PreparedState &b )
{
    if( a.valid != b.valid ) return QStringLiteral( "valid" );
    if( !a.valid ) return QString();
    QString diff;
    const auto add = [&diff]( bool differs, const char *name )
    {
        if( !differs ) return;
        if( !diff.isEmpty() ) diff += QLatin1Char( ',' );
        diff += QLatin1String( name );
    };
    add( a.whiteDarkened != b.whiteDarkened, "white_darkened" );
    add( a.blackDelta != b.blackDelta, "black_delta" );
    add( !( a.evCorrection == b.evCorrection ), "ev_correction" );
    add( !( a.darkNoise == b.darkNoise ), "dark_noise" );
    add( a.interpMethod != b.interpMethod, "interp_method" );
    add( a.useAliasMap != b.useAliasMap, "use_alias_map" );
    add( a.useFullres != b.useFullres, "use_fullres" );
    add( a.chromaSmoothMethod != b.chromaSmoothMethod, "chroma_smooth_method" );
    add( a.applyDither != b.applyDither, "apply_dither" );
    add( a.isBright[0] != b.isBright[0] || a.isBright[1] != b.isBright[1] || a.isBright[2] != b.isBright[2]
           || a.isBright[3] != b.isBright[3],
         "is_bright" );
    add( a.frameId != b.frameId, "frame_id" );
    return diff;
}

/*! "wd=..,bd=..,ev=..,dn=..,interp=..,alias=..,fullres=..,cs=..,dither=..,bright=a/b/c/d,fid=.." (doubles to 17
 *  significant digits); "none" when unknown. */
inline QString preparedStateText( const PreparedState &s )
{
    if( !s.valid ) return QStringLiteral( "none" );
    const auto n = []( int v ) { return QString::number( v ); };
    const auto d = []( double v ) { return QString::number( v, 'g', 17 ); };
    return QStringLiteral( "wd=" ) + n( s.whiteDarkened ) + QStringLiteral( ",bd=" ) + n( s.blackDelta )
         + QStringLiteral( ",ev=" ) + d( s.evCorrection ) + QStringLiteral( ",dn=" ) + d( s.darkNoise )
         + QStringLiteral( ",interp=" ) + n( s.interpMethod ) + QStringLiteral( ",alias=" ) + n( s.useAliasMap )
         + QStringLiteral( ",fullres=" ) + n( s.useFullres ) + QStringLiteral( ",cs=" ) + n( s.chromaSmoothMethod )
         + QStringLiteral( ",dither=" ) + n( s.applyDither ) + QStringLiteral( ",bright=" ) + n( s.isBright[0] )
         + QLatin1Char( '/' ) + n( s.isBright[1] ) + QLatin1Char( '/' ) + n( s.isBright[2] ) + QLatin1Char( '/' )
         + n( s.isBright[3] ) + QStringLiteral( ",fid=" ) + QString::number( static_cast<qulonglong>( s.frameId ) );
}

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
    PreparedState state;   // F-key: the recon's per-run constants this AMaZE input was made with
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
    // F-key: decided here, before any compare; an unknown state on either side is never proven equal
    if( !warmKey.state.valid || !presentKey.state.valid || !preparedStateDiff( warmKey.state, presentKey.state ).isEmpty() )
        return Reason::StateMismatch;
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
    case Reason::StateMismatch: return "state_mismatch";
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

/*! F-key: on a state or content miss, " warm_state=<text> play_state=<text> state_diff=<fields|none>" (appended to the
 *  first-present split); "" for every other reason. A content miss with state_diff=none says the difference is in the
 *  recon's input, not in its constants. */
inline QString stateFields( Reason reason, const Key &warmKey, const Key &presentKey )
{
    if( reason != Reason::StateMismatch && reason != Reason::ContentMismatch ) return QString();
    const QString diff = preparedStateDiff( warmKey.state, presentKey.state );
    return QStringLiteral( " warm_state=" ) + preparedStateText( warmKey.state )
         + QStringLiteral( " play_state=" ) + preparedStateText( presentKey.state )
         + QStringLiteral( " state_diff=" ) + ( diff.isEmpty() ? QStringLiteral( "none" ) : diff );
}

/*! The warm line's capture field: " reuse_capture=<ok|failed|symbol_missing|none>". */
inline QString captureField( const QString &capture )
{
    return QStringLiteral( " reuse_capture=" ) + ( capture.isEmpty() ? QStringLiteral( "none" ) : capture );
}

} // namespace playback_warm_output_reuse

#endif // PLAYBACKWARMOUTPUTREUSEPOLICY_H
