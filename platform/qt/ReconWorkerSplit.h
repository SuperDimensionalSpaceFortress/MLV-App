/*!
 * \file ReconWorkerSplit.h
 * \brief CPU-S1-RECON-UNDER-LOOK-PACE-LEVER-1 (Part A): the phase-3 recon WORKER's llrawproc_* and
 * dual_iso_full20_* sub-spans, carried to the presented frame and summarised on
 * playback_smoke.recon_worker_split_summary.
 *
 * Why: those timers are thread-local (src/mlv/llrawproc/dualiso.c). The worker writes them into the
 * frame slot (RenderFrameThread.cpp insertLlrawprocReconTimingTelemetry), then the render thread
 * that consumes the reconned frame writes its OWN values (zero: it ran no recon) over the same keys,
 * so cpu_summary read avg_llrawproc_dual_iso_ms=0.000 on every consumed-recon CPU frame. The worker's
 * values are kept here under the recon_worker_ prefix, which no render-thread writer touches; the
 * render thread's own keys are left as they are (consumers read both).
 *
 * Header-only and Qt-core-only, so console_tests drives it.
 */

#ifndef RECONWORKERSPLIT_H
#define RECONWORKERSPLIT_H

#include <QJsonObject>
#include <QString>
#include <QtGlobal>

#include <array>
#include <cstddef>

namespace recon_worker_split
{

inline QString prefix() { return QStringLiteral( "recon_worker_" ); }

// CPU-S1-DUALISO-MIX-CHROMA-LEVER-2: the full20 flag saying the matched-source smooth ran AVX2.
inline QString mixChromaAvx2UsedKey() { return QStringLiteral( "dual_iso_full20_mix_chroma_avx2_used" ); }

// A worker key this split carries: an llrawproc_* or dual_iso_full20_* duration, the full20 valid flag,
// or the mix-chroma AVX2 path flag.
inline bool isSplitKey( const QString &key )
{
    const bool family = key.startsWith( QLatin1String( "llrawproc_" ) )
                     || key.startsWith( QLatin1String( "dual_iso_full20_" ) );
    return family && ( key.endsWith( QLatin1String( "_ms" ) )
                       || key == QLatin1String( "dual_iso_full20_valid" )
                       || key == mixChromaAvx2UsedKey() );
}

// A presented frame whose matched-source smooth ran the AVX2 kernel, on the render thread's own recon
// or on the recon worker's (adversary 5f16bcdf A1: a consumed worker recon leaves the render thread's
// unprefixed flag false; the worker's survives under recon_worker_).
inline bool mixChromaAvx2Used( const QJsonObject &presented )
{
    return presented.value( mixChromaAvx2UsedKey() ).toBool()
        || presented.value( prefix() + mixChromaAvx2UsedKey() ).toBool();
}

// Copies the worker's split keys from `worker` (the slot telemetry as the recon worker left it) into
// `target` under recon_worker_. Never writes an unprefixed key.
inline void copyWorkerSplit( const QJsonObject &worker, QJsonObject &target )
{
    for( auto it = worker.begin(); it != worker.end(); ++it )
    {
        if( isSplitKey( it.key() ) ) target.insert( prefix() + it.key(), it.value() );
    }
}

// One summary field: its name on the line (avg_<name>_ms) and the worker key it averages.
struct Field
{
    const char *name;
    const char *key;
};

// Order is the line's order. Fixed nesting, used for the residues below: llrawproc_total > { the
// seven llrawproc stages }, llrawproc_dual_iso > full20_total, and full20_total > { pattern, noise,
// scratch, convert20, match, interp, fullres, mix, final_blend, convert16 } with the rest in other
// (dualiso.c dualiso_debug_finish_full20bit_timing, its known_ms).
// PATH-DEPENDENT nesting, so NO residue is computed under mix or mix_chroma (r1 adversary A1): in
// mix_images (dualiso.c:5303-5323) the chroma split runs inside mix; on the CPU full20 path the chroma
// smoothing runs after interp, outside mix and outside the ten parts (dualiso.c:6988
// smooth_matched_source_planes, so it lands in full20 other), its copy is timed outside mix_chroma
// (dualiso.c:4708-4711), and mix_chroma_fullres also takes the post-mix fullres_reconstruction
// (dualiso.c:7059-7069). The mix and mix_chroma fields are reported as measured, leaves only.
enum FieldIndex
{
    LlrawprocTotal = 0, LlrawprocDarkFrame, LlrawprocVerticalStripes, LlrawprocFocusPixels,
    LlrawprocBadPixels, LlrawprocPatternNoise, LlrawprocDualIso, LlrawprocChromaSmooth, LlrawprocOther,
    Full20Total, Full20Pattern, Full20Noise, Full20Scratch, Full20Convert20, Full20Match, Full20Interp,
    Full20Fullres, Full20Mix, Full20MixHalfres, Full20MixChroma, Full20MixChromaCopy,
    Full20MixChromaFullres, Full20MixChromaHalfres, Full20MixAliasMap, Full20MixOverexposed,
    Full20FinalBlend, Full20Convert16, Full20Other,
    FieldCount
};

inline const std::array<Field, FieldCount> &fields()
{
    static const std::array<Field, FieldCount> table = {{
        { "llrawproc_total", "llrawproc_total_ms" },
        { "llrawproc_dark_frame", "llrawproc_dark_frame_ms" },
        { "llrawproc_vertical_stripes", "llrawproc_vertical_stripes_ms" },
        { "llrawproc_focus_pixels", "llrawproc_focus_pixels_ms" },
        { "llrawproc_bad_pixels", "llrawproc_bad_pixels_ms" },
        { "llrawproc_pattern_noise", "llrawproc_pattern_noise_ms" },
        { "llrawproc_dual_iso", "llrawproc_dual_iso_ms" },
        { "llrawproc_chroma_smooth", "llrawproc_chroma_smooth_ms" },
        { "llrawproc_other", "llrawproc_other_ms" },
        { "full20_total", "dual_iso_full20_total_ms" },
        { "full20_pattern", "dual_iso_full20_pattern_ms" },
        { "full20_noise", "dual_iso_full20_noise_ms" },
        { "full20_scratch", "dual_iso_full20_scratch_ms" },
        { "full20_convert20", "dual_iso_full20_convert20_ms" },
        { "full20_match", "dual_iso_full20_match_ms" },
        { "full20_interp", "dual_iso_full20_interp_ms" },
        { "full20_fullres", "dual_iso_full20_fullres_ms" },
        { "full20_mix", "dual_iso_full20_mix_ms" },
        { "full20_mix_halfres", "dual_iso_full20_mix_halfres_ms" },
        { "full20_mix_chroma", "dual_iso_full20_mix_chroma_ms" },
        { "full20_mix_chroma_copy", "dual_iso_full20_mix_chroma_copy_ms" },
        { "full20_mix_chroma_fullres", "dual_iso_full20_mix_chroma_fullres_ms" },
        { "full20_mix_chroma_halfres", "dual_iso_full20_mix_chroma_halfres_ms" },
        { "full20_mix_alias_map", "dual_iso_full20_mix_alias_map_ms" },
        { "full20_mix_overexposed", "dual_iso_full20_mix_overexposed_ms" },
        { "full20_final_blend", "dual_iso_full20_final_blend_ms" },
        { "full20_convert16", "dual_iso_full20_convert16_ms" },
        { "full20_other", "dual_iso_full20_other_ms" },
    }};
    return table;
}

// The residue at each level of the FIXED nesting above (never below 0); none under mix or mix_chroma.
struct Unattributed
{
    double llrawprocMs = 0.0;      // llrawproc_total less its seven stages
    double dualIsoOutsideFull20Ms = 0.0;   // llrawproc_dual_iso less full20_total
    double full20Ms = 0.0;         // full20_total less its ten named parts (dualiso.c's other_ms)
    double totalMs = 0.0;          // llrawproc_total less the six non-dual-iso stages and full20's ten parts
};

inline Unattributed unattributed( const std::array<double, FieldCount> &v )
{
    const auto pos = []( double x ) { return x > 0.0 ? x : 0.0; };
    Unattributed u;
    const double nonDualIsoStages = v[LlrawprocDarkFrame] + v[LlrawprocVerticalStripes]
        + v[LlrawprocFocusPixels] + v[LlrawprocBadPixels] + v[LlrawprocPatternNoise] + v[LlrawprocChromaSmooth];
    const double full20Parts = v[Full20Pattern] + v[Full20Noise] + v[Full20Scratch] + v[Full20Convert20]
        + v[Full20Match] + v[Full20Interp] + v[Full20Fullres] + v[Full20Mix] + v[Full20FinalBlend] + v[Full20Convert16];
    u.llrawprocMs = pos( v[LlrawprocTotal] - nonDualIsoStages - v[LlrawprocDualIso] );
    u.dualIsoOutsideFull20Ms = pos( v[LlrawprocDualIso] - v[Full20Total] );
    u.full20Ms = pos( v[Full20Total] - full20Parts );
    u.totalMs = pos( v[LlrawprocTotal] - nonDualIsoStages - full20Parts );
    return u;
}

// Session sums over presented frames that carry the worker's split (recon_worker_llrawproc_total_ms).
struct Totals
{
    quint64 frames = 0;
    quint64 full20ValidFrames = 0;
    std::array<double, FieldCount> sumMs{};

    void reset() { *this = Totals(); }

    // Returns false (and adds nothing) for a frame without the worker's split.
    bool add( const QJsonObject &presented )
    {
        const QString totalKey = prefix() + QLatin1String( fields()[LlrawprocTotal].key );
        if( !presented.contains( totalKey ) ) return false;
        ++frames;
        if( presented.value( prefix() + QLatin1String( "dual_iso_full20_valid" ) ).toBool() ) ++full20ValidFrames;
        for( std::size_t i = 0; i < fields().size(); ++i )
            sumMs[i] += presented.value( prefix() + QLatin1String( fields()[i].key ) ).toDouble();
        return true;
    }

    std::array<double, FieldCount> averages() const
    {
        std::array<double, FieldCount> avg{};
        for( std::size_t i = 0; i < avg.size(); ++i )
            avg[i] = frames > 0 ? sumMs[i] / static_cast<double>( frames ) : 0.0;
        return avg;
    }

    // " avg_<name>_ms=<v>" for every field, then the four unattributed residues, %.3f, no spaces in values.
    QString fieldsText() const
    {
        const std::array<double, FieldCount> avg = averages();
        QString text;
        for( std::size_t i = 0; i < avg.size(); ++i )
            text += QStringLiteral( " avg_%1_ms=%2" ).arg( QLatin1String( fields()[i].name ) ).arg( avg[i], 0, 'f', 3 );
        const Unattributed u = unattributed( avg );
        text += QStringLiteral( " avg_llrawproc_unattributed_ms=%1" ).arg( u.llrawprocMs, 0, 'f', 3 );
        text += QStringLiteral( " avg_dual_iso_outside_full20_ms=%1" ).arg( u.dualIsoOutsideFull20Ms, 0, 'f', 3 );
        text += QStringLiteral( " avg_full20_unattributed_ms=%1" ).arg( u.full20Ms, 0, 'f', 3 );
        text += QStringLiteral( " avg_unattributed_ms=%1" ).arg( u.totalMs, 0, 'f', 3 );
        return text;
    }
};

} // namespace recon_worker_split

#endif // RECONWORKERSPLIT_H
