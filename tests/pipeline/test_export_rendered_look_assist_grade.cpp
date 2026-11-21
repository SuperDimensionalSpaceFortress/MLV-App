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
