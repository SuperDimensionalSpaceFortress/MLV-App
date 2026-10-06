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
