// LOOK-ASSIST-M16-CAST-3: the measure-only dual-ISO match probe on the tracked HQ dual-ISO fixture. The isolated seed
// override reaches the reconstruction (nominal -1, histogram -2, explicit values), the probe reports what the match
// actually used, nothing shared moves, the default is today's seed, and the Look Assist trace is off unless asked for.
#include "../common/minitest.h"

#include "mlv_pipeline_fixture.h"

#include "../../src/mlv/llrawproc/llrawproc.h"
#include "../../src/mlv/llrawproc/dualiso.h"
#include "../../src/batch/ReceiptApplier.h"

#include <cmath>
#include <cstring>
#include <vector>

#include <QByteArray>
#include <QRegularExpression>
#include <QStringList>

namespace
{

void openHqDualIso(MlvPipelineFixture &fixture)
{
    QString error;
    ASSERT_TRUE( fixture.openTinyDualIso( &error ) );
    ASSERT_TRUE( fixture.loadReceipt( QStringLiteral( "tests/fixtures/receipts/tiny_dual_iso_hq.marxml" ), &error ) );
    ASSERT_TRUE( fixture.applyReceipt( &error ) );
    ASSERT_EQ( 1, fixture.video()->llrawproc->dual_iso );
    ASSERT_TRUE( fixture.video()->llrawproc->diso1 != fixture.video()->llrawproc->diso2 );
}

struct SharedMatch
{
    int autoCorrection;
    double evCorrection;
    int blackDelta;
    bool operator==( const SharedMatch &o ) const
    {
        return autoCorrection == o.autoCorrection && evCorrection == o.evCorrection && blackDelta == o.blackDelta;
    }
};

SharedMatch sharedMatch(mlvObject_t *video)
{
    return { video->llrawproc->diso_auto_correction, video->llrawproc->diso_ev_correction,
             video->llrawproc->diso_black_delta };
}

dualiso_match_probe_t probe(mlvObject_t *video, int mode, double ev, int bd, bool stopAfterMatch,
                            std::vector<uint16_t> *frame = nullptr)
{
    std::vector<uint16_t> local( static_cast<size_t>( video->RAWI.xRes ) * static_cast<size_t>( video->RAWI.yRes ) );
    std::vector<uint16_t> &out = frame ? *frame : local;
    out.assign( local.size(), 0 );
    const int previousReadOnly = llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( 1 );
    const int previousMode = llrpSetIsolatedAnalysisDualIsoMatchForCurrentThread( mode, ev, bd );
    dualiso_match_probe_reset( stopAfterMatch ? 1 : 0 );
    int shift = 0;
    getMlvRawFrameProcessedUint16Direct( video, 0, out.data(), &shift );
    dualiso_match_probe_t result;
    memset( &result, 0, sizeof( result ) );
    dualiso_match_probe_get( &result );
    dualiso_match_probe_reset( 0 );
    llrpSetIsolatedAnalysisDualIsoMatchForCurrentThread( previousMode, 1.0, -1 );
    llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( previousReadOnly );
    return result;
}

} // namespace

TEST(LookAssistDisoMatchProbe, TheSeedOverrideReachesTheMatchAndTheProbeReportsWhatItUsed)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    const SharedMatch before = sharedMatch( video );
    const int lo = qMin( video->llrawproc->diso1, video->llrawproc->diso2 );
    const int hi = qMax( video->llrawproc->diso1, video->llrawproc->diso2 );

    // Nominal: today's seed, log2(hi/lo) and ((hi/100) - (lo/100)) * 64.
    const dualiso_match_probe_t nominal = probe( video, LLRP_ANALYSIS_DISO_MATCH_SEED, 1.0, -1, true );
    ASSERT_EQ( 1, nominal.valid );
    ASSERT_TRUE( nominal.rc > 0 );
    ASSERT_EQ( -1, nominal.mode );
    ASSERT_NEAR( std::log2( static_cast<double>( hi / lo ) ), nominal.ev, 1e-9 );
    ASSERT_EQ( ( ( hi / 100 ) * 64 ) - ( ( lo / 100 ) * 64 ), nominal.black_delta );
    ASSERT_TRUE( nominal.black > 0 && nominal.white > nominal.black );

    // Measured: the existing histogram match (-2) ran.
    const dualiso_match_probe_t measured = probe( video, LLRP_ANALYSIS_DISO_MATCH_MEASURED, 1.0, -1, true );
    ASSERT_EQ( 1, measured.valid );
    ASSERT_EQ( -2, measured.mode );

    // Explicit: the values given, in the shared fields' convention (negative stops, 14-bit black delta units).
    const dualiso_match_probe_t explicitMatch = probe( video, LLRP_ANALYSIS_DISO_MATCH_EXPLICIT, -3.5, 20, true );
    ASSERT_EQ( 1, explicitMatch.valid );
    ASSERT_TRUE( explicitMatch.rc > 0 );
    ASSERT_EQ( 0, explicitMatch.mode );
    ASSERT_NEAR( 3.5, explicitMatch.ev, 1e-9 );
    ASSERT_EQ( 20 * 64, explicitMatch.black_delta );

    // Nothing shared moved, and the thread is back on today's seed.
    ASSERT_TRUE( before == sharedMatch( video ) );
    ASSERT_EQ( LLRP_ANALYSIS_DISO_MATCH_SEED, llrpSetIsolatedAnalysisDualIsoMatchForCurrentThread( LLRP_ANALYSIS_DISO_MATCH_SEED, 1.0, -1 ) );
    ASSERT_EQ( 0, llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( 0 ) );
}

TEST(LookAssistDisoMatchProbe, TheDefaultSeedAndResetReconOverridesRenderTodaysFrame)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();

    std::vector<uint16_t> today;
    probe( video, LLRP_ANALYSIS_DISO_MATCH_SEED, 1.0, -1, false, &today );
    // An explicit match equal to the nominal one renders the same frame (the override is the only change)...
    const int lo = qMin( video->llrawproc->diso1, video->llrawproc->diso2 );
    const int hi = qMax( video->llrawproc->diso1, video->llrawproc->diso2 );
    std::vector<uint16_t> explicitNominal;
    probe( video, LLRP_ANALYSIS_DISO_MATCH_EXPLICIT, -std::log2( static_cast<double>( hi / lo ) ),
           ( hi / 100 ) - ( lo / 100 ), false, &explicitNominal );
    ASSERT_TRUE( today == explicitNominal );
    // ...a different explicit match does not...
    std::vector<uint16_t> moved;
    probe( video, LLRP_ANALYSIS_DISO_MATCH_EXPLICIT, -3.5, 40, false, &moved );
    ASSERT_TRUE( today != moved );
    // ...and recon overrides set and then reset leave today's render.
    llrpSetIsolatedAnalysisDualIsoReconForCurrentThread( 1, 1, 0, 0 );
    std::vector<uint16_t> variant;
    probe( video, LLRP_ANALYSIS_DISO_MATCH_SEED, 1.0, -1, false, &variant );
    llrpSetIsolatedAnalysisDualIsoReconForCurrentThread( -1, -1, -1, -1 );
    ASSERT_TRUE( today != variant );
    std::vector<uint16_t> again;
    probe( video, LLRP_ANALYSIS_DISO_MATCH_SEED, 1.0, -1, false, &again );
    ASSERT_TRUE( today == again );
}

TEST(LookAssistDisoMatchProbe, TheTraceIsOffUnlessAskedForAndNamesTheMatchWhenOn)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    const QByteArray previous = qgetenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );
    const bool wasSet = qEnvironmentVariableIsSet( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );
    const SharedMatch before = sharedMatch( video );

    qunsetenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );
    ASSERT_TRUE( ReceiptApplier::lookAssistDualIsoMatchTrace( video, 0, 2, 0.0, 6000, 0 ).isEmpty() );

    qputenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE", "1" );
    const QString trace = ReceiptApplier::lookAssistDualIsoMatchTrace( video, 0, 2, 0.0, 6000, 0 );
    if( wasSet ) qputenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE", previous );
    else qunsetenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );
    ASSERT_TRUE( trace.contains( QStringLiteral( "nominal_rc=1" ) ) );
    ASSERT_TRUE( trace.contains( QStringLiteral( "measured_rc=" ) ) );
    ASSERT_TRUE( trace.contains( QStringLiteral( " frames=" ) ) );
    ASSERT_TRUE( before == sharedMatch( video ) );
}

// ---------------------------------------------------------------------------------------------------------------------
// LOOK-ASSIST-M16-CAST-4: the measure-only arms (recon mode, bright clip, dark noise, dark-field black) and the levels
// probe. Each arm moves the isolated render when set, never a live one, and the caller's reset puts today's render back.

namespace
{

std::vector<uint16_t> isolatedFrame(mlvObject_t *video)
{
    std::vector<uint16_t> out( static_cast<size_t>( video->RAWI.xRes ) * static_cast<size_t>( video->RAWI.yRes ) );
    const int previousReadOnly = llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( 1 );
    int shift = 0;
    getMlvRawFrameProcessedUint16Direct( video, 0, out.data(), &shift );
    llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( previousReadOnly );
    return out;
}

bool armsAreReset()
{
    int mode = 0;
    int whiteBright = 0;
    double noise = 0.0;
    int black = 0;
    return llrpGetIsolatedAnalysisDualIsoArmsForCurrentThread( &mode, &whiteBright, &noise, &black ) == 0
        && mode == -1 && whiteBright == 0 && noise == 0.0 && black == 0;
}

struct ArmCase { const char *name; int mode; int whiteBright; double noise; bool black; };

} // namespace

TEST(LookAssistDisoArms, EachArmMovesTheIsolatedRenderAndTheResetRestoresIt)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    const SharedMatch before = sharedMatch( video );
    const int sharedMode = video->llrawproc->dual_iso;
    ASSERT_TRUE( armsAreReset() );

    const std::vector<uint16_t> today = isolatedFrame( video );
    const int offsets[4] = { 6, 6, 6, 6 };
    const ArmCase cases[5] = {
        { "preview recon", 2, 0, 0.0, false },
        { "bright clip", -1, 5000, 0.0, false },
        { "dark noise x0.5", -1, 0, 0.5, false },
        { "dark noise x2", -1, 0, 2.0, false },
        { "dark-field black", -1, 0, 0.0, true } };
    for( const ArmCase &c : cases )
    {
        llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( c.mode, c.whiteBright, c.noise, c.black ? offsets : nullptr );
        ASSERT_FALSE( armsAreReset() );
        const std::vector<uint16_t> armed = isolatedFrame( video );
        llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, 0, 0.0, nullptr );
        ASSERT_TRUE( armsAreReset() );
        ASSERT_TRUE( armed != today ); // the arm reached the isolated render
        ASSERT_TRUE( isolatedFrame( video ) == today );
    }

    // The recon mode arm also decides llrpHQDualIso, but only inside a read-only isolated render.
    llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( 2, 0, 0.0, nullptr );
    ASSERT_EQ( 1, llrpHQDualIso( video ) );
    const int previousReadOnly = llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( 1 );
    ASSERT_EQ( 0, llrpHQDualIso( video ) );
    llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( previousReadOnly );
    llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, 0, 0.0, nullptr );

    // Nothing shared moved.
    ASSERT_EQ( sharedMode, video->llrawproc->dual_iso );
    ASSERT_TRUE( before == sharedMatch( video ) );
}

TEST(LookAssistDisoArms, NoArmReachesALiveRender)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    const std::vector<float> liveRaw = fixture.renderRawFrameFloat( 0 );
    const std::vector<uint16_t> live16 = fixture.renderFrame16( 0 );
    const int offsets[4] = { 6, 6, 6, 6 };
    const ArmCase cases[5] = {
        { "preview recon", 2, 0, 0.0, false },
        { "bright clip", -1, 5000, 0.0, false },
        { "dark noise x0.5", -1, 0, 0.5, false },
        { "dark noise x2", -1, 0, 2.0, false },
        { "dark-field black", -1, 0, 0.0, true } };
    for( const ArmCase &c : cases )
    {
        llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( c.mode, c.whiteBright, c.noise, c.black ? offsets : nullptr );
        const std::vector<float> armedRaw = fixture.renderRawFrameFloat( 0 );
        const std::vector<uint16_t> armed16 = fixture.renderFrame16( 0 );
        llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, 0, 0.0, nullptr );
        ASSERT_TRUE( armedRaw == liveRaw ); // the arm never reaches the live raw read
        ASSERT_TRUE( armed16 == live16 );   // nor the live processed read
    }
}

TEST(LookAssistDisoLevelsProbe, ReportsTheSyntheticFloorAndClipPerField)
{
    // 64x64 RGGB, rows 0,1 of every 4 dark (ISO100), rows 2,3 bright. Per field and channel one pixel holds the floor
    // and one the top, so on 512 samples p0.1 is the floor and p99.99 the top.
    const int w = 64;
    const int h = 64;
    std::vector<uint16_t> raw( static_cast<size_t>( w ) * h );
    const int darkBase = 2600, darkFloor = 2050, darkTop = 3100;
    const int brightBase = 7000, brightFloor = 5200, brightTop = 9100;
    for( int y = 0; y < h; ++y )
        for( int x = 0; x < w; ++x )
            raw[static_cast<size_t>( y ) * w + x] = static_cast<uint16_t>( ( y % 4 ) < 2 ? darkBase : brightBase );
    for( int ch = 0; ch < 4; ++ch )
    {
        const int cy = ch >> 1;
        const int cx = ch & 1;
        // dark rows: y % 4 in {0, 1}; bright rows: y % 4 in {2, 3}. y = 4k + cy is dark, 4k + 2 + cy bright.
        raw[static_cast<size_t>( 4 + cy ) * w + 10 + cx] = static_cast<uint16_t>( darkFloor );
        raw[static_cast<size_t>( 8 + cy ) * w + 20 + cx] = static_cast<uint16_t>( darkTop );
        raw[static_cast<size_t>( 6 + cy ) * w + 30 + cx] = static_cast<uint16_t>( brightFloor );
        raw[static_cast<size_t>( 10 + cy ) * w + 40 + cx] = static_cast<uint16_t>( brightTop );
    }
    struct raw_info info;
    memset( &info, 0, sizeof( info ) );
    info.width = w;
    info.height = h;
    info.pitch = w;
    info.bits_per_pixel = 14;
    info.black_level = 2048;
    info.white_level = 15000;
    info.active_area.x1 = 0;
    info.active_area.y1 = 0;
    info.active_area.x2 = w;
    info.active_area.y2 = h;
    info.cfa_pattern = 0x02010100;
    dualiso_full20bit_scratch_t scratch = {};
    int isoPattern = 0;
    int autoCorrection = -1;
    double evCorrection = 1.0;
    int blackDelta = -1;
    dualiso_levels_probe_reset( 1 );
    ASSERT_EQ( 0, diso_get_full20bit( info, raw.data(), 0, 100, 1600, &isoPattern, &autoCorrection, &evCorrection,
                                      &blackDelta, 1, 0, 1, 0, 0, 1, &scratch ) );
    dualiso_levels_probe_t levels;
    memset( &levels, 0, sizeof( levels ) );
    ASSERT_EQ( 1, dualiso_levels_probe_get( &levels ) );
    dualiso_levels_probe_reset( 0 );
    free_dualiso_full20bit_scratch( &scratch );

    ASSERT_EQ( 0, levels.is_bright[0] );
    ASSERT_EQ( 0, levels.is_bright[1] );
    ASSERT_EQ( 1, levels.is_bright[2] );
    ASSERT_EQ( 1, levels.is_bright[3] );
    ASSERT_EQ( 2048, levels.black );
    ASSERT_EQ( 15000, levels.white );
    ASSERT_EQ( 7500, levels.white_bright_default );
    ASSERT_EQ( 7500, levels.white_bright_used );
    ASSERT_EQ( 0, levels.has_noise_samples );
    ASSERT_NEAR( 8.0, levels.dark_noise, 1e-9 );
    for( int ch = 0; ch < 4; ++ch )
    {
        ASSERT_EQ( 512, levels.count[0][ch] );
        ASSERT_EQ( 512, levels.count[1][ch] );
        ASSERT_EQ( darkFloor, levels.p001[0][ch] );
        ASSERT_EQ( darkBase, levels.p1[0][ch] );
        ASSERT_EQ( darkTop, levels.p9999[0][ch] );
        ASSERT_EQ( darkTop, levels.max[0][ch] );
        ASSERT_EQ( brightFloor, levels.p001[1][ch] );
        ASSERT_EQ( brightTop, levels.p9999[1][ch] );
        ASSERT_EQ( brightTop, levels.max[1][ch] );
    }
}

TEST(LookAssistDisoArms, TheTracePinsTheBalanceFixesTheMaskOnR0AndResetsEveryArm)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    const QByteArray previous = qgetenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );
    const bool wasSet = qEnvironmentVariableIsSet( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );
    const SharedMatch before = sharedMatch( video );

    QStringList sunk;
    qputenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE", "1" );
    // Look Assist's own decision (exposure 0, 6000 K) is logged, never rendered with.
    const QString trace = ReceiptApplier::lookAssistDualIsoMatchTrace(
        video, 0, 2, 0.0, 6000, 0,
        [&sunk]( const QString &name, int w, int h, const unsigned char *rgb )
        {
            if( w > 0 && h > 0 && rgb ) sunk << name;
        } );
    if( wasSet ) qputenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE", previous );
    else qunsetenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );

    ASSERT_TRUE( trace.contains( QStringLiteral( " diso_levels valid=1 " ) ) );
    ASSERT_TRUE( trace.contains( QStringLiteral( " arms_pin=ev3.80/6724/0 la=ev0.00/6000/0 " ) ) );
    ASSERT_TRUE( armsAreReset() );
    ASSERT_TRUE( before == sharedMatch( video ) );

    const QRegularExpression armRe( QStringLiteral( " (R0|A1|A3|A6|A7a|A7b|A8|A5)=dbm(-?[0-9.]+)/psh([0-9.]+)/blk([0-9.]+)/m([0-9]+)/own([0-9]+)" ) );
    const QStringList blocks = trace.split( QStringLiteral( " arms@" ) );
    ASSERT_EQ( 4, blocks.size() );
    bool someGradedArmOwnsAnotherMask = false;
    for( int b = 1; b < blocks.size(); ++b )
    {
        QRegularExpressionMatchIterator it = armRe.globalMatch( blocks[b] );
        int r0Mask = -1;
        int arms = 0;
        while( it.hasNext() )
        {
            const QRegularExpressionMatch m = it.next();
            const QString name = m.captured( 1 );
            const int scored = m.captured( 5 ).toInt();
            const int own = m.captured( 6 ).toInt();
            ++arms;
            if( name == QStringLiteral( "R0" ) )
            {
                r0Mask = scored;
                ASSERT_EQ( own, scored );
                ASSERT_TRUE( r0Mask > 0 );
            }
            else if( name == QStringLiteral( "A5" ) )
            {
                ASSERT_EQ( own, scored ); // its own mask, never graded
            }
            else
            {
                ASSERT_EQ( r0Mask, scored ); // R0's mask, fixed once per frame
                if( own != r0Mask ) someGradedArmOwnsAnotherMask = true;
            }
        }
        ASSERT_TRUE( arms >= 6 );
    }
    ASSERT_TRUE( someGradedArmOwnsAnotherMask );
    ASSERT_TRUE( sunk.contains( QStringLiteral( "f0-R0" ) ) );
    ASSERT_TRUE( sunk.contains( QStringLiteral( "f0-A5" ) ) );
}
