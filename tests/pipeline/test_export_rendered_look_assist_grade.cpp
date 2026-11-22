// EXPORT-PRORES-LOOK-ASSIST-1: what a rendered batch export does with Look Assist, on the tracked tiny dual-ISO clip,
// frame 0, in the order BatchRunner::exportRenderedVideoFile runs it: applyToMlv, then applyHeadlessLookAssist on the
// clip-wide anchor frame, then (only when it returned true) ReceiptApplier::applyLookAssistGradeToProcessing.
//  (i)   Look Assist on: the call returns true, the processing object then holds the receipt's grade mapped the way the
//        app's slider handlers map it, and the rendered frame differs both from the Look Assist off render and from the
//        render with only the white balance Look Assist pushes itself (what the rendered route showed before).
//  (ii)  Look Assist off (the receipt flag; MLVAPP_NO_LOOK_ASSIST is a once-per-process static): the call returns false,
//        nothing is pushed, and the frame is byte-identical to a render after applyToMlv alone.
//  (iii) The helper leaves the curve tables alone for a default curve, and pushes a user curve exactly as the Curves
//        widget does (look_assist_gradation_curve.h: Y = 0, R = 1, G = 2, B = 3).
// No timing.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "look_assist_gradation_curve.h"
#include "mlv_pipeline_fixture.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/batch/CdngSequenceExport.h"
#include "../../src/batch/ReceiptApplier.h"

#include <QString>

#include <cmath>
#include <cstdint>
#include <vector>

namespace
{

const char *const kTinyClip = "tests/fixtures/clips/tiny_dual_iso.mlv";

bool openTiny( MlvPipelineFixture &fixture )
{
    QString error_message;
    if( !fixture.openClipFile( repo_file_path( QString::fromLatin1( kTinyClip ) ), &error_message ) ) return false;
    return fixture.applyReceipt( &error_message );   // ReceiptApplier::applyToMlv, as the rendered export runs it
}

std::vector<uint8_t> renderFresh( MlvPipelineFixture &fixture )
{
    resetMlvCache( fixture.video() );
    resetMlvCachedFrame( fixture.video() );
    return fixture.renderFrame8( 0 );
}

// The rendered export's anchor for a receipt cut-in of 1 (frame 0).
uint32_t anchorFrame()
{
    return CdngSequenceExport::lookAssistAnalysisFrameIndex( 1, 1 );
}

struct CurveTables
{
    std::vector<uint16_t> y, r, g, b;
    bool operator==( const CurveTables &o ) const { return y == o.y && r == o.r && g == o.g && b == o.b; }
};

CurveTables curveTables( const processingObject_t *processing )
{
    CurveTables t;
    t.y.assign( processing->gcurve_y, processing->gcurve_y + 65536 );
    t.r.assign( processing->gcurve_r, processing->gcurve_r + 65536 );
    t.g.assign( processing->gcurve_g, processing->gcurve_g + 65536 );
    t.b.assign( processing->gcurve_b, processing->gcurve_b + 65536 );
    return t;
}

// Four different lines, so a channel swap shows.
const char *const kUserCurve =
    "1e-5;1e-5;0.5;0.62;1;1;?1e-5;1e-5;0.5;0.40;1;1;?1e-5;1e-5;0.5;0.55;1;1;?1e-5;1e-5;0.5;0.47;1;1;";

} // namespace

// (i)
TEST(ExportRenderedLookAssistGrade, LookAssistOnPushesTheGradeAndChangesTheRenderedFrame)
{
    MlvPipelineFixture off;
    ASSERT_TRUE( openTiny( off ) );
    off.receipt().setLookAssistEnabled( false );
    const std::vector<uint8_t> offFrame = renderFresh( off );
    ASSERT_FALSE( offFrame.empty() );

    MlvPipelineFixture on;
    ASSERT_TRUE( openTiny( on ) );
    ReceiptSettings &receipt = on.receipt();
    receipt.setLookAssistEnabled( true );
    ASSERT_TRUE( ReceiptApplier::applyHeadlessLookAssist( &receipt, on.video(), on.processing(), anchorFrame() ) );
    const std::vector<uint8_t> whiteBalanceOnly = renderFresh( on );

    ReceiptApplier::applyLookAssistGradeToProcessing( &receipt, on.processing() );
    const processingObject_t *p = on.processing();
    ASSERT_NEAR( receipt.exposure() / 100.0 + 1.2, processingGetExposureStops( p ), 1e-12 );
    ASSERT_NEAR( receipt.contrast() / 100.0 * 0.65, processingGetSimpleContrast( p ), 1e-12 );
    ASSERT_NEAR( receipt.pivot() / 100.0, processingGetPivot( p ), 1e-12 );
    ASSERT_NEAR( std::pow( ( receipt.vibrance() + 100 ) / 200.0 * 2.0, std::log( 3.6 ) / std::log( 2.0 ) ),
                 processingGetVibrance( p ), 1e-12 );
    ASSERT_NEAR( receipt.shadows() * 1.5 / 100.0, p->shadows_highlights.shadows, 1e-12 );
    ASSERT_NEAR( receipt.highlights() * 1.5 / 100.0, p->shadows_highlights.highlights, 1e-12 );

    const std::vector<uint8_t> graded = renderFresh( on );
    ASSERT_EQ( offFrame.size(), graded.size() );
    ASSERT_TRUE( graded != offFrame );
    ASSERT_TRUE( graded != whiteBalanceOnly );
}

// (ii)
TEST(ExportRenderedLookAssistGrade, LookAssistOffLeavesTheRenderedFrameByteIdentical)
{
    MlvPipelineFixture plain;
    ASSERT_TRUE( openTiny( plain ) );
    plain.receipt().setLookAssistEnabled( false );
    const std::vector<uint8_t> applyToMlvOnly = renderFresh( plain );
    ASSERT_FALSE( applyToMlvOnly.empty() );

    MlvPipelineFixture routed;
    ASSERT_TRUE( openTiny( routed ) );
    ReceiptSettings &receipt = routed.receipt();
    receipt.setLookAssistEnabled( false );
    const bool applied = ReceiptApplier::applyHeadlessLookAssist(
        &receipt, routed.video(), routed.processing(), anchorFrame() );
    ASSERT_FALSE( applied );
    if( applied ) ReceiptApplier::applyLookAssistGradeToProcessing( &receipt, routed.processing() );   // the export's guard
    ASSERT_TRUE( renderFresh( routed ) == applyToMlvOnly );
}

// (iii)
TEST(ExportRenderedLookAssistGrade, TheHelperLeavesTheCurveAloneForADefaultCurveAndPushesAUserCurveLikeTheWidget)
{
    ReceiptSettings receipt;   // default gradation curve
    ASSERT_TRUE( lookassist::lookAssistIsDefaultGradationCurve( receipt.gradationCurve() ) );

    processingObject_t *viaHelper = initProcessingObject();
    processingObject_t *viaWidget = initProcessingObject();
    ASSERT_TRUE( viaHelper != nullptr );
    ASSERT_TRUE( viaWidget != nullptr );

    // A curve already in the tables stays exactly as it is when the receipt's curve is the default.
    lookAssistTestApplyGradationCurve( viaHelper, QString::fromLatin1( kUserCurve ) );
    const CurveTables before = curveTables( viaHelper );
    ReceiptApplier::applyLookAssistGradeToProcessing( &receipt, viaHelper );
    ASSERT_TRUE( curveTables( viaHelper ) == before );

    // A user curve: the helper's tables are the widget's, channel for channel.
    freeProcessingObject( viaHelper );
    viaHelper = initProcessingObject();
    const CurveTables identity = curveTables( viaHelper );
    receipt.setGradationCurve( QString::fromLatin1( kUserCurve ) );
    ReceiptApplier::applyLookAssistGradeToProcessing( &receipt, viaHelper );
    lookAssistTestApplyGradationCurve( viaWidget, QString::fromLatin1( kUserCurve ) );
    const CurveTables helperTables = curveTables( viaHelper );
    ASSERT_TRUE( helperTables == curveTables( viaWidget ) );
    ASSERT_TRUE( helperTables.y != identity.y );
    ASSERT_TRUE( helperTables.r != helperTables.b );

    // Null-safe.
    ReceiptApplier::applyLookAssistGradeToProcessing( nullptr, viaHelper );
    ReceiptApplier::applyLookAssistGradeToProcessing( &receipt, nullptr );

    freeProcessingObject( viaHelper );
    freeProcessingObject( viaWidget );
}

// ---- adversary findings on 76653777 ---------------------------------------------------------------------------------

// A2: an analysis that skips after the raw-levels auto-fix leaves the levels the receipt applied, so the rendered frame
// is the one a render after applyToMlv alone shows. The skip is forced with an invalid raw width for the call only.
TEST(ExportRenderedLookAssistGrade, ASkippedAnalysisLeavesTheReceiptRawLevels)
{
    MlvPipelineFixture fixture;
    QString error_message;
    ASSERT_TRUE( fixture.openClipFile( repo_file_path( QString::fromLatin1( kTinyClip ) ), &error_message ) );
    ReceiptSettings &receipt = fixture.receipt();
    receipt.setLookAssistEnabled( true );
    ASSERT_TRUE( fixture.applyReceipt( &error_message ) );

    // Receipt-applied levels away from the clip's own (rawBlack 20000 = 2000, rawWhite 4500), set the way
    // ReceiptApplier::applyToMlv sets them.
    mlvObject_t *video = fixture.video();
    processingObject_t *processing = fixture.processing();
    receipt.setRawBlack( 20000 );
    receipt.setRawWhite( 4500 );
    setMlvWhiteLevel( video, 4500 );
    processingSetWhiteLevel( processing, 4500, getMlvBitdepth( video ) );
    setMlvBlackLevel( video, 2000.0 );
    processingSetBlackLevel( processing, 2000.0, getMlvBitdepth( video ) );
    llrpResetFpmStatus( video );
    llrpResetBpmStatus( video );
    // Rendered first: on this dual-ISO clip a render itself settles the processing levels, so the state to compare
    // against is the one after it.
    const std::vector<uint8_t> before = renderFresh( fixture );
    ASSERT_FALSE( before.empty() );
    const int32_t mlvBlack = getMlvBlackLevel( video );
    const int32_t mlvWhite = getMlvWhiteLevel( video );
    const float processingBlack = processing->black_level;
    const int processingWhite = processing->white_level;
    const int dngBlack = video->llrawproc->dng_black_level;
    const int dngWhite = video->llrawproc->dng_white_level;
    ASSERT_EQ( 2000, mlvBlack );
    ASSERT_EQ( 4500, mlvWhite );

    const int rawWidth = video->RAWI.xRes;
    video->RAWI.xRes = 0;
    const bool applied = ReceiptApplier::applyHeadlessLookAssist( &receipt, video, processing, anchorFrame() );
    video->RAWI.xRes = rawWidth;
    ASSERT_FALSE( applied );

    ASSERT_EQ( mlvBlack, getMlvBlackLevel( video ) );
    ASSERT_EQ( mlvWhite, getMlvWhiteLevel( video ) );
    ASSERT_TRUE( processingBlack == processing->black_level );
    ASSERT_EQ( processingWhite, processing->white_level );
    ASSERT_EQ( dngBlack, video->llrawproc->dng_black_level );
    ASSERT_EQ( dngWhite, video->llrawproc->dng_white_level );
    ASSERT_EQ( 20000, receipt.rawBlack() );
    ASSERT_EQ( 4500, receipt.rawWhite() );
    ASSERT_TRUE( renderFresh( fixture ) == before );
}

// A3: a receipt curve with fewer than four lines pushes nothing, as Curves::setConfiguration pushes nothing.
TEST(ExportRenderedLookAssistGrade, APartialCurvePushesNothing)
{
    ReceiptSettings receipt;
    receipt.setGradationCurve( QStringLiteral( "1e-5;1e-5;0.5;0.62;1;1;" ) );
    processingObject_t *processing = initProcessingObject();
    ASSERT_TRUE( processing != nullptr );
    const CurveTables identity = curveTables( processing );
    ReceiptApplier::applyLookAssistGradeToProcessing( &receipt, processing );
    ASSERT_EQ( 32768, static_cast<int>( processing->gcurve_y[32768] ) );
    ASSERT_TRUE( curveTables( processing ) == identity );
    freeProcessingObject( processing );
}

// A4: knots the spline cannot use (an x the equal-x nudge cannot move past the previous one, a non-finite value) leave
// the curve tables as they were, and nothing aborts.
TEST(ExportRenderedLookAssistGrade, UnusableCurveKnotsLeaveTheTablesAlone)
{
    const char *const unusable[] = {
        "32;0;32;1;?1e-5;1e-5;1;1;?1e-5;1e-5;1;1;?1e-5;1e-5;1;1;",
        "1e-5;1e-5;nan;0.5;1;1;?1e-5;1e-5;1;1;?1e-5;1e-5;1;1;?1e-5;1e-5;1;1;",
        "1e-5;1e-5;0.5;inf;1;1;?1e-5;1e-5;1;1;?1e-5;1e-5;1;1;?1e-5;1e-5;1;1;",
    };
    for( const char *curve : unusable )
    {
        ReceiptSettings receipt;
        receipt.setGradationCurve( QString::fromLatin1( curve ) );
        processingObject_t *processing = initProcessingObject();
        ASSERT_TRUE( processing != nullptr );
        const CurveTables identity = curveTables( processing );
        ReceiptApplier::applyLookAssistGradeToProcessing( &receipt, processing );
        ASSERT_TRUE( curveTables( processing ).y == identity.y );
        freeProcessingObject( processing );
    }
    // Equal x values inside 0..1 are still nudged apart and pushed, as before.
    float xs[] = { 0.0f, 0.5f, 0.5f, 1.0f };
    float ys[] = { 0.0f, 0.4f, 0.6f, 1.0f };
    processingObject_t *processing = initProcessingObject();
    ASSERT_TRUE( processing != nullptr );
    processingSetGCurve( processing, 4, xs, ys, 0 );
    ASSERT_TRUE( processing->gcurve_y[32768] != 32768 );
    freeProcessingObject( processing );
}

// A11: an out-of-range receipt vibrance is the slider's end, never a non-finite factor.
TEST(ExportRenderedLookAssistGrade, VibranceIsBoundedToTheSliderRange)
{
    processingObject_t *processing = initProcessingObject();
    ASSERT_TRUE( processing != nullptr );
    ReceiptSettings receipt;
    const double top = std::pow( 2.0, std::log( 3.6 ) / std::log( 2.0 ) );
    receipt.setVibrance( 2147483647 );
    ReceiptApplier::applyLookAssistGradeToProcessing( &receipt, processing );
    ASSERT_NEAR( top, processingGetVibrance( processing ), 1e-12 );
    receipt.setVibrance( -101 );
    ReceiptApplier::applyLookAssistGradeToProcessing( &receipt, processing );
    ASSERT_TRUE( std::isfinite( processingGetVibrance( processing ) ) );
    ASSERT_NEAR( 0.0, processingGetVibrance( processing ), 1e-12 );
    freeProcessingObject( processing );
}
