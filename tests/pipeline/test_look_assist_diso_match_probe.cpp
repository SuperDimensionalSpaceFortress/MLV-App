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
        // An isolated render with the arm first: nothing it handed the recon may outlive it.
        (void)isolatedFrame( video );
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

namespace { bool switchArmsAreReset(); } // LOOK-ASSIST-M16-CAST-5, below

TEST(LookAssistDisoArms, TheTracePinsTheBalanceFixesBothMasksOnR0AndResetsEveryArm)
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
        video, 0, 3, 0.0, 6000, 0,
        [&sunk]( const QString &name, int w, int h, const unsigned char *rgb )
        {
            if( w > 0 && h > 0 && rgb ) sunk << name;
        } );
    if( wasSet ) qputenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE", previous );
    else qunsetenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );

    ASSERT_TRUE( trace.contains( QStringLiteral( " diso_levels valid=1 " ) ) );
    ASSERT_TRUE( trace.contains( QStringLiteral( " arms_pin=ev3.80/6724/0 la=ev0.00/6000/0 " ) ) );
    ASSERT_TRUE( trace.contains( QStringLiteral( " diso_fieldratio valid=1 " ) ) );
    ASSERT_TRUE( armsAreReset() );
    ASSERT_TRUE( switchArmsAreReset() );
    ASSERT_TRUE( before == sharedMatch( video ) );

    const QRegularExpression armRe( QStringLiteral(
        " (R0|A6lo|A6|A10|A11|A12)=dbm(-?[0-9.]+)/psh([0-9.]+)/blk([0-9.]+)/m([0-9]+)/own([0-9]+)"
        "/lav([0-9.]+)/cyn([0-9.]+)/blkhi([0-9.]+)/mh([0-9]+)/ownh([0-9]+)" ) );
    const QStringList blocks = trace.split( QStringLiteral( " arms@" ) );
    ASSERT_EQ( 4, blocks.size() );
    bool someArmOwnsAnotherLo = false;
    bool someArmOwnsAnotherHi = false;
    for( int b = 1; b < blocks.size(); ++b )
    {
        QRegularExpressionMatchIterator it = armRe.globalMatch( blocks[b] );
        int r0Lo = -1;
        int r0Hi = -1;
        QStringList names;
        while( it.hasNext() )
        {
            const QRegularExpressionMatch m = it.next();
            const QString name = m.captured( 1 );
            const int lo = m.captured( 5 ).toInt();
            const int ownLo = m.captured( 6 ).toInt();
            const int hi = m.captured( 10 ).toInt();
            const int ownHi = m.captured( 11 ).toInt();
            names << name;
            if( name == QStringLiteral( "R0" ) )
            {
                r0Lo = lo;
                r0Hi = hi;
                ASSERT_EQ( ownLo, lo );
                ASSERT_EQ( ownHi, hi );
                ASSERT_TRUE( r0Lo > 0 );
                ASSERT_TRUE( r0Hi > 0 );
            }
            else
            {
                ASSERT_EQ( r0Lo, lo ); // R0's LO mask, fixed once per frame
                ASSERT_EQ( r0Hi, hi ); // R0's HI mask, fixed once per frame
                if( ownLo != r0Lo ) someArmOwnsAnotherLo = true;
                if( ownHi != r0Hi ) someArmOwnsAnotherHi = true;
            }
        }
        ASSERT_TRUE( names.join( QLatin1Char( ',' ) ) == QStringLiteral( "R0,A6,A6lo,A10,A11,A12" ) );
        ASSERT_TRUE( blocks[b].contains( QStringLiteral( " p1 map=" ) ) );
        ASSERT_TRUE( blocks[b].contains( QStringLiteral( " a11_used=" ) ) );
    }
    ASSERT_TRUE( someArmOwnsAnotherLo );
    ASSERT_TRUE( someArmOwnsAnotherHi );
    ASSERT_TRUE( sunk.contains( QStringLiteral( "f0-R0" ) ) );
    ASSERT_TRUE( sunk.contains( QStringLiteral( "f0-P1" ) ) );
    ASSERT_TRUE( sunk.contains( QStringLiteral( "f0-A12" ) ) );
}

// ---------------------------------------------------------------------------------------------------------------------
// LOOK-ASSIST-M16-CAST-5: the switch arms (A6 / A6lo through the matched threshold, A11 per-channel darkening, A12 the
// quad-coherent switch), the P1 provenance capture and the P2 field ratio. Every test here must fail if its arm is inert.

extern "C" int dualisoHqReinitDispatchForTesting(void);

namespace
{

struct SwitchMaps
{
    std::vector<unsigned char> site[DUALISO_SWITCH_SITES];
    int width = 0;
    int height = 0;
};

// One isolated render of frame 0 with the arms given and the switch maps captured; the arms are reset after it.
SwitchMaps capturedRender(mlvObject_t *video, int whiteBright, const double *channelEv, const double *channelBd,
                          bool quad, std::vector<uint16_t> *frame = nullptr)
{
    llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, whiteBright, 0.0, nullptr );
    llrpSetIsolatedAnalysisDualIsoSwitchArmsForCurrentThread( channelEv, channelBd, quad ? 1 : 0, 1 );
    dualiso_switch_capture_clear();
    const std::vector<uint16_t> out = isolatedFrame( video );
    llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, 0, 0.0, nullptr );
    SwitchMaps maps;
    for( int s = 0; s < DUALISO_SWITCH_SITES; ++s )
    {
        int w = 0;
        int h = 0;
        const unsigned char *m = dualiso_switch_capture_map( s, &w, &h );
        if( !m ) continue;
        maps.width = w;
        maps.height = h;
        maps.site[s].assign( m, m + static_cast<size_t>( w ) * static_cast<size_t>( h ) );
    }
    dualiso_switch_capture_clear();
    if( frame ) *frame = out;
    return maps;
}

long long flaggedCount(const std::vector<unsigned char> &map)
{
    long long n = 0;
    for( unsigned char v : map ) n += v ? 1 : 0;
    return n;
}

// Quads (x&~1, y&~1) whose four flags disagree.
long long mixedQuads(const std::vector<unsigned char> &map, int w, int h)
{
    long long mixed = 0;
    for( int y = 0; y + 1 < h; y += 2 )
        for( int x = 0; x + 1 < w; x += 2 )
        {
            const size_t i = static_cast<size_t>( y ) * w + x;
            const int sum = map[i] + map[i + 1] + map[i + w] + map[i + w + 1];
            if( sum != 0 && sum != 4 ) ++mixed;
        }
    return mixed;
}

bool switchArmsAreReset()
{
    int channel = -1;
    int quad = -1;
    int capture = -1;
    return llrpGetIsolatedAnalysisDualIsoSwitchArmsForCurrentThread( &channel, &quad, &capture ) == 0
        && channel == 0 && quad == 0 && capture == 0;
}

dualiso_match_probe_t matchProbeWithArms(mlvObject_t *video, int whiteBright, const double *channelEv,
                                         const double *channelBd)
{
    llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, whiteBright, 0.0, nullptr );
    llrpSetIsolatedAnalysisDualIsoSwitchArmsForCurrentThread( channelEv, channelBd, 0, 0 );
    const dualiso_match_probe_t p = probe( video, LLRP_ANALYSIS_DISO_MATCH_SEED, 1.0, -1, true );
    llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, 0, 0.0, nullptr );
    return p;
}

// The matched threshold for a seed (14-bit), by compute_match_exposure_scalars' own formula.
int matchedThreshold(const dualiso_match_probe_t &p, int seed14)
{
    const int white14 = p.white / 64;
    const int seed20 = qMin( seed14, white14 ) * 64;
    return static_cast<int>( ( static_cast<double>( qMin( p.white, seed20 ) ) - p.black + p.black_delta )
                             * std::pow( 2.0, -p.ev ) + p.black );
}

} // namespace

TEST(LookAssistDisoSwitch, A6AndA6loReachTheMatchedThresholdAndTheOverexposedMap)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    const dualiso_match_probe_t r0 = matchProbeWithArms( video, 0, nullptr, nullptr );
    ASSERT_TRUE( r0.rc > 0 );
    const int white14 = r0.white / 64;
    const int a6 = white14 * 7 / 8;   // a clip above today's white/2, as on the clip (13876 vs 7906)
    const int a6lo = white14 / 4;     // the brief's A6lo
    ASSERT_EQ( matchedThreshold( r0, white14 / 2 ), r0.white_darkened ); // today: the seed is white/2
    const dualiso_match_probe_t pa6 = matchProbeWithArms( video, a6, nullptr, nullptr );
    const dualiso_match_probe_t pa6lo = matchProbeWithArms( video, a6lo, nullptr, nullptr );
    ASSERT_EQ( matchedThreshold( r0, a6 ), pa6.white_darkened );
    ASSERT_EQ( matchedThreshold( r0, a6lo ), pa6lo.white_darkened );
    ASSERT_TRUE( pa6.white_darkened != r0.white_darkened );
    ASSERT_TRUE( pa6lo.white_darkened != r0.white_darkened );

    const SwitchMaps mr0 = capturedRender( video, 0, nullptr, nullptr, false );
    const SwitchMaps ma6 = capturedRender( video, a6, nullptr, nullptr, false );
    const SwitchMaps ma6lo = capturedRender( video, a6lo, nullptr, nullptr, false );
    ASSERT_EQ( video->RAWI.xRes, mr0.width );
    ASSERT_EQ( video->RAWI.yRes, mr0.height );
    const long long n0 = flaggedCount( mr0.site[DUALISO_SWITCH_SITE_OVEREXPOSED] );
    ASSERT_TRUE( flaggedCount( ma6.site[DUALISO_SWITCH_SITE_OVEREXPOSED] ) != n0 );
    ASSERT_TRUE( flaggedCount( ma6lo.site[DUALISO_SWITCH_SITE_OVEREXPOSED] ) > n0 );
    ASSERT_TRUE( armsAreReset() );
    ASSERT_TRUE( switchArmsAreReset() );
}

TEST(LookAssistDisoSwitch, A11DarkensEachBrightChannelByItsOwnFactors)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    const dualiso_match_probe_t r0 = matchProbeWithArms( video, 0, nullptr, nullptr );
    ASSERT_TRUE( r0.rc > 0 );
    const double bd14 = r0.black_delta / 64.0;
    const double ev[4] = { r0.ev, r0.ev + 0.5, r0.ev, r0.ev - 0.25 };
    const double bd[4] = { bd14, bd14 + 4.0, bd14, bd14 };
    const dualiso_match_probe_t a11 = matchProbeWithArms( video, 0, ev, bd );
    ASSERT_TRUE( a11.rc > 0 );
    ASSERT_EQ( r0.white_darkened, a11.white_darkened ); // the threshold stays global
    const double f = std::pow( 2.0, -r0.ev );
    for( int c = 0; c < 4; ++c )
    {
        ASSERT_TRUE( r0.bright_count[c] > 0 );
        ASSERT_NEAR( r0.bright_pre_mean[c], a11.bright_pre_mean[c], 1e-9 );
        // Today: (p - black + bd) * 2^-ev + black, truncated per pixel.
        ASSERT_NEAR( ( r0.bright_pre_mean[c] - r0.black + r0.black_delta ) * f + r0.black, r0.bright_post_mean[c], 1.0 );
        // A11: (p - black) * 2^-ev_c + black + bd_c - bd * (1 - 2^-ev).
        const double fc = std::pow( 2.0, -ev[c] );
        const double predicted = ( a11.bright_pre_mean[c] - r0.black ) * fc + r0.black + bd[c] * 64.0
                               - r0.black_delta * ( 1.0 - f );
        ASSERT_NEAR( predicted, a11.bright_post_mean[c], 1.0 );
    }
    // Channels at the global factors match today's; the others moved.
    ASSERT_NEAR( r0.bright_post_mean[0], a11.bright_post_mean[0], 1.0 );
    ASSERT_NEAR( r0.bright_post_mean[2], a11.bright_post_mean[2], 1.0 );
    ASSERT_TRUE( std::fabs( r0.bright_post_mean[1] - a11.bright_post_mean[1] ) > 4.0 );
    ASSERT_TRUE( std::fabs( r0.bright_post_mean[3] - a11.bright_post_mean[3] ) > 4.0 );
    ASSERT_TRUE( armsAreReset() );
    ASSERT_TRUE( switchArmsAreReset() );
}

TEST(LookAssistDisoSwitch, A12LeavesNoMixedQuadAtAnySiteOnBothDispatches)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    // Chroma smoothing on, so the phase-balanced fullres (site 1) and the smoothed fullres_reconstruction (site 2) run.
    llrpSetIsolatedAnalysisDualIsoReconForCurrentThread( -1, -1, 1, 2 );
    const QByteArray previous = qgetenv( "MLVAPP_DISABLE_AVX2_DUALISO_HQ" );
    const bool wasSet = qEnvironmentVariableIsSet( "MLVAPP_DISABLE_AVX2_DUALISO_HQ" );
    for( int pass = 0; pass < 2; ++pass )
    {
        if( pass == 1 ) qputenv( "MLVAPP_DISABLE_AVX2_DUALISO_HQ", "1" ); // the scalar sites
        dualisoHqReinitDispatchForTesting();
        std::vector<uint16_t> today;
        std::vector<uint16_t> quad;
        const SwitchMaps r0 = capturedRender( video, 0, nullptr, nullptr, false, &today );
        const SwitchMaps a12 = capturedRender( video, 0, nullptr, nullptr, true, &quad );
        for( int s = 0; s < DUALISO_SWITCH_SITES; ++s )
        {
            ASSERT_EQ( static_cast<size_t>( r0.width ) * r0.height, r0.site[s].size() );
            ASSERT_EQ( r0.site[s].size(), a12.site[s].size() );
            ASSERT_TRUE( mixedQuads( r0.site[s], r0.width, r0.height ) > 0 ); // today switches per pixel
            ASSERT_EQ( 0, mixedQuads( a12.site[s], a12.width, a12.height ) );
            ASSERT_TRUE( flaggedCount( a12.site[s] ) > flaggedCount( r0.site[s] ) );
        }
        ASSERT_TRUE( quad != today ); // the arm reached the picture
    }
    if( wasSet ) qputenv( "MLVAPP_DISABLE_AVX2_DUALISO_HQ", previous );
    else qunsetenv( "MLVAPP_DISABLE_AVX2_DUALISO_HQ" );
    dualisoHqReinitDispatchForTesting();
    llrpSetIsolatedAnalysisDualIsoReconForCurrentThread( -1, -1, -1, -1 );
    ASSERT_TRUE( armsAreReset() );
    ASSERT_TRUE( switchArmsAreReset() );
}

TEST(LookAssistDisoSwitch, TheSwitchArmsReachNoLiveRenderAndResetAfterTheRender)
{
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    const std::vector<uint16_t> today = isolatedFrame( video );
    const std::vector<float> liveRaw = fixture.renderRawFrameFloat( 0 );
    const std::vector<uint16_t> live16 = fixture.renderFrame16( 0 );
    const SharedMatch before = sharedMatch( video ); // after the live renders, which keep their own match
    const dualiso_match_probe_t r0 = matchProbeWithArms( video, 0, nullptr, nullptr );
    const double ev[4] = { r0.ev + 0.5, r0.ev - 0.5, r0.ev + 0.25, r0.ev };
    const double bd[4] = { 10.0, 20.0, 15.0, 5.0 };
    for( int arm = 0; arm < 3; ++arm )
    {
        llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, 0, 0.0, nullptr );
        llrpSetIsolatedAnalysisDualIsoSwitchArmsForCurrentThread( arm == 0 ? ev : nullptr, arm == 0 ? bd : nullptr,
                                                                  arm == 1 ? 1 : 0, arm == 2 ? 1 : 0 );
        ASSERT_FALSE( switchArmsAreReset() );
        const std::vector<uint16_t> armed = isolatedFrame( video );
        if( arm < 2 ) ASSERT_TRUE( armed != today ); // A11 and A12 reach the isolated render
        else ASSERT_TRUE( armed == today );          // the capture alone changes no pixel
        // ...but never a live read, even right after an armed isolated render.
        dualiso_switch_capture_clear();
        ASSERT_TRUE( fixture.renderRawFrameFloat( 0 ) == liveRaw );
        ASSERT_TRUE( fixture.renderFrame16( 0 ) == live16 );
        ASSERT_TRUE( dualiso_switch_capture_map( DUALISO_SWITCH_SITE_OVEREXPOSED, nullptr, nullptr ) == nullptr );
        // The CAST-4 setter (the trace's per-render reset) clears the switch arms too.
        llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, 0, 0.0, nullptr );
        ASSERT_TRUE( switchArmsAreReset() );
        ASSERT_TRUE( isolatedFrame( video ) == today );
    }
    ASSERT_TRUE( before == sharedMatch( video ) );
}

TEST(LookAssistDisoSwitch, P1IsR0sOwnMapAndCAndRUseTheirOwnDenominators)
{
    // Synthetic: a 4x2 display at factor 2 over an 8x4 raw map. Display pixels: 0..2 LAV, 3 grey, 4..7 grey too; all in
    // HI. Raw flags: one pixel in the quad under display (0,0), one in the quad under (1,0), one under (2,0); none else.
    const int w = 4, h = 2, f = 2, rw = 8, rh = 4;
    std::vector<unsigned char> rgb( static_cast<size_t>( w ) * h * 3 );
    for( int i = 0; i < w * h; ++i )
    {
        const bool lav = i < 3;
        rgb[i * 3] = lav ? 200 : 180;
        rgb[i * 3 + 1] = lav ? 170 : 180;
        rgb[i * 3 + 2] = lav ? 210 : 180;
    }
    std::vector<unsigned char> map( static_cast<size_t>( rw ) * rh, 0 );
    map[1] = 1;        // quad (0,0) -> display (0,0)
    map[rw + 2] = 1;   // quad (1,0) -> display (1,0)
    map[5] = 1;        // quad (2,0) -> display (2,0)
    std::vector<unsigned char> flags;
    const ReceiptApplier::DisoProvenance p =
        ReceiptApplier::lookAssistDisoProvenance( rgb.data(), w, h, map.data(), rw, rh, f, &flags );
    ASSERT_EQ( 3, p.lav );
    ASSERT_EQ( 3, p.lavFlagged );
    ASSERT_EQ( 5, p.other );
    ASSERT_EQ( 0, p.otherFlagged );
    ASSERT_NEAR( 1.0, p.c, 1e-12 );
    ASSERT_NEAR( 0.0, p.r, 1e-12 );
    ASSERT_EQ( 3, p.rawFlagged );
    ASSERT_EQ( 3, p.displayFlagged );
    // One more flag under a grey pixel: r = 1/5, c unchanged.
    map[rw * 2 + 7] = 1; // quad (3,1) -> display (3,1)
    const ReceiptApplier::DisoProvenance q =
        ReceiptApplier::lookAssistDisoProvenance( rgb.data(), w, h, map.data(), rw, rh, f, nullptr );
    ASSERT_NEAR( 1.0, q.c, 1e-12 );
    ASSERT_NEAR( 0.2, q.r, 1e-12 );
    // Any-of at an odd factor: display (1,0) at factor 3 covers raw x 3..5, so quads 1 and 2 (x 2..5).
    std::vector<unsigned char> odd( static_cast<size_t>( 9 ) * 3, 0 );
    odd[2] = 1; // raw (2,0): quad 1, under display (1,0) only through the quad
    std::vector<unsigned char> rgb3( static_cast<size_t>( 3 ) * 1 * 3, 180 );
    std::vector<unsigned char> oddFlags;
    ReceiptApplier::lookAssistDisoProvenance( rgb3.data(), 3, 1, odd.data(), 9, 3, 3, &oddFlags );
    ASSERT_EQ( 1, oddFlags[0] );
    ASSERT_EQ( 1, oddFlags[1] );
    ASSERT_EQ( 0, oddFlags[2] );

    // On the fixture: the trace's P1 is R0's own map (R0's flagged count), not an arm's (A12 flags more).
    MlvPipelineFixture fixture;
    openHqDualIso( fixture );
    mlvObject_t *video = fixture.video();
    const long long r0Flagged = flaggedCount( capturedRender( video, 0, nullptr, nullptr, false ).site[DUALISO_SWITCH_SITE_OVEREXPOSED] );
    const long long a12Flagged = flaggedCount( capturedRender( video, 0, nullptr, nullptr, true ).site[DUALISO_SWITCH_SITE_OVEREXPOSED] );
    ASSERT_TRUE( r0Flagged > 0 );
    ASSERT_TRUE( a12Flagged != r0Flagged );
    const QByteArray previous = qgetenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );
    const bool wasSet = qEnvironmentVariableIsSet( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );
    qputenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE", "1" );
    std::vector<unsigned char> overlay;
    std::vector<unsigned char> r0Render;
    const QString trace = ReceiptApplier::lookAssistDualIsoMatchTrace(
        video, 0, 3, 0.0, 6000, 0,
        [&overlay, &r0Render]( const QString &name, int w2, int h2, const unsigned char *px )
        {
            const size_t n = static_cast<size_t>( w2 ) * h2 * 3;
            if( name == QStringLiteral( "f0-P1" ) ) overlay.assign( px, px + n );
            if( name == QStringLiteral( "f0-R0" ) ) r0Render.assign( px, px + n );
        } );
    if( wasSet ) qputenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE", previous );
    else qunsetenv( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" );
    const QRegularExpression p1Re( QStringLiteral( " arms@0:.*? p1 map=([0-9]+)x([0-9]+) factor=3 raw_flagged=([0-9]+) " ) );
    const QRegularExpressionMatch m = p1Re.match( trace );
    ASSERT_TRUE( m.hasMatch() );
    ASSERT_EQ( video->RAWI.xRes, m.captured( 1 ).toInt() );
    ASSERT_EQ( r0Flagged, m.captured( 3 ).toLongLong() );
    ASSERT_FALSE( overlay.empty() );
    ASSERT_TRUE( overlay != r0Render ); // the flags are painted on R0
    ASSERT_TRUE( switchArmsAreReset() );
}

TEST(LookAssistDisoFieldRatio, P2RecoversAKnownPerChannelRatio)
{
    // 64x64 RGGB, rows 0,1 of every 4 dark, rows 2,3 bright (the CAST-4 levels layout). The dark field varies along x
    // only, so the vertical interpolation is exact; each bright channel c is black + 2^ev_c * (dark - black - bd_c).
    const int w = 64;
    const int h = 64;
    const int black = 2048;
    const double ev[4] = { 3.8, 4.0, 4.1, 4.3 };
    const double bd[4] = { 12.0, 15.0, 15.0, 20.0 };
    std::vector<uint16_t> raw( static_cast<size_t>( w ) * h );
    for( int y = 0; y < h; ++y )
        for( int x = 0; x < w; ++x )
        {
            const int c = ( ( y & 1 ) << 1 ) | ( x & 1 );
            const double dark = 2200.0 + 9.0 * x + 3.0 * c;
            const double value = ( y % 4 ) < 2 ? dark : black + std::pow( 2.0, ev[c] ) * ( dark - black - bd[c] );
            raw[static_cast<size_t>( y ) * w + x] = static_cast<uint16_t>( std::lround( value ) );
        }
    struct raw_info info;
    memset( &info, 0, sizeof( info ) );
    info.width = w;
    info.height = h;
    info.pitch = w;
    info.bits_per_pixel = 14;
    info.black_level = black;
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
    ASSERT_TRUE( levels.fr_bright_clip > 0 );
    for( int c = 0; c < 4; ++c )
    {
        ASSERT_TRUE( levels.fr_count[c] > 100 );
        ASSERT_NEAR( ev[c], levels.fr_ev[c], 0.01 );
        ASSERT_NEAR( bd[c], levels.fr_bd[c], 1.0 );
    }
}
