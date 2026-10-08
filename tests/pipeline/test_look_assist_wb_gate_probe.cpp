// LOOK-ASSIST-WB-DECISION-1: a MEASURE-ONLY probe of Look Assist's daylight white-balance decision on the tracked fixtures
// (S1's setup of test_look_assist_fixture_scene.cpp). It asserts no product behaviour. It names, with numbers, which gate of
// the decision stands between each candidate surface and the balance that neutralises it:
//  - g1: the patch-search filters at the consumer picture (LookAssistAnalysis.cpp :503-509);
//  - g2: stability (:541-600, daylightSolve = true);
//  - g3: base neutrality at the as-shot prior (:949-951, :970);
//  - g4: the solution check at the window-clamped solve (:973-982), found / final chroma and the margin vs the 0.75 slack;
//  - g5: whether the daylight window clamp binds (:974, :659).
// for two surfaces: (i) Look Assist's own patch, (ii) the deck box centre (r1 Phase B's deck region, rows 65-98 %,
// columns 2-25 %). It also prints the deck chroma (S1 / S2's deckCastChroma) at CANDw (LA's candidate clamped into the
// window), DSw (the deck-solved balance, clamped) and DSt (the applied temperature with the deck-solved tint, unclamped).
//
// SKIPPED unless MLVAPP_LA_WB_GATE_PROBE=1 (CI cost stays zero). When it runs it prints one parseable "WB_GATE ..." line per
// measurement and asserts only its own premises, against the applier's own log of the same run:
//  - the replicated patch == the applied line's patch fields (a frame the daylight pass accepted);
//  - the replicated g3 / g4 chroma == the logged initialPatchBaseChroma / initialPatchFinalChroma;
//  - the replicated decision == the logged decision (accepted, or the initial_patch_unverified fallback), and the
//    replicated candidate == autoWbCandidateTemp / autoWbCandidateTint where the daylight pass logged it.
//
// Public API only. The static verification (initialDaylightPatchIsVerified, :928-983) and lookAssistSurfaceAt (:710) are
// file-static in LookAssistAnalysis.cpp, so they are replicated here; deckCastChroma / renderWith and S1's setup are copied
// from test_look_assist_fixture_scene.cpp (that file is not edited by this card).
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/batch/BatchLogger.h"
#include "../../src/batch/LookAssistAnalysis.h"
#include "../../src/batch/ReceiptApplier.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QTemporaryDir>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <vector>

using namespace lookassist;

namespace
{

const char *const kTinyClip = "tests/fixtures/clips/tiny_dual_iso.mlv";
const char *const kLargeClip = "tests/fixtures/clips/large_dual_iso.mlv";

// kRefineVerifyChromaSlack, LookAssistAnalysis.cpp:692 (file-static there): the surface at the solution may be this much
// more cast than where it was found.
const double kVerifyChromaSlack = 0.75;
// The solver's own tint rails, LookAssistAnalysis.cpp:1011.
const int kSolverMinTint = -35;
const int kSolverMaxTint = 18;

// ---- Copied from test_look_assist_fixture_scene.cpp (S1 / S2's own measurement) ----
double deckCastChroma( const std::vector<uint8_t> &rgb, int width, int height )
{
    const int y0 = static_cast<int>( height * 0.65 ), y1 = static_cast<int>( height * 0.98 );
    const int x0 = static_cast<int>( width * 0.02 ), x1 = static_cast<int>( width * 0.25 );
    double sum[3] = { 0.0, 0.0, 0.0 };
    int n = 0;
    for( int y = y0; y < y1; ++y )
        for( int x = x0; x < x1; ++x )
        {
            const size_t i = ( static_cast<size_t>( y ) * width + x ) * 3;
            for( int c = 0; c < 3; ++c ) sum[c] += rgb[i + c];
            ++n;
        }
    if( n == 0 ) return 1.0e9;
    double lin[3];
    for( int c = 0; c < 3; ++c )
    {
        const double v = sum[c] / n / 255.0;
        lin[c] = v <= 0.04045 ? v / 12.92 : std::pow( ( v + 0.055 ) / 1.055, 2.4 );
    }
    const double X = ( 0.4124 * lin[0] + 0.3576 * lin[1] + 0.1805 * lin[2] ) / 0.95047;
    const double Y = 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2];
    const double Z = ( 0.0193 * lin[0] + 0.1192 * lin[1] + 0.9505 * lin[2] ) / 1.08883;
    auto f = []( double t ) { return t > 0.008856 ? std::cbrt( t ) : 7.787 * t + 16.0 / 116.0; };
    const double a = 500.0 * ( f( X ) - f( Y ) );
    const double b = 200.0 * ( f( Y ) - f( Z ) );
    return std::hypot( a, b );
}

std::vector<uint8_t> renderWith( MlvPipelineFixture &fixture, int frame, int temperature, int tint, int exposure )
{
    processingSetWhiteBalance( fixture.processing(), temperature, tint / 10.0 );
    processingSetExposureStops( fixture.processing(), exposure / 100.0 );
    resetMlvCache( fixture.video() );
    resetMlvCachedFrame( fixture.video() );
    return fixture.renderFrame8( static_cast<uint64_t>( frame ) );
}
// ---- end of the copy ----

QByteArray readLog( const QString &path )
{
    QFile log_file( path );
    if( !log_file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return QByteArray();
    return log_file.readAll();
}

QString lastLine( const QByteArray &log, const char *marker )
{
    QString line;
    for( const QByteArray &candidate : log.split( '\n' ) )
        if( candidate.contains( marker ) ) line = QString::fromUtf8( candidate ).trimmed();
    return line;
}

QString field( const QString &line, const char *name )
{
    const QRegularExpression re( QStringLiteral( "(?:^| )%1=(\\S+)" ).arg( QLatin1String( name ) ) );
    const QRegularExpressionMatch m = re.match( line );
    return m.hasMatch() ? m.captured( 1 ) : QStringLiteral( "NA" );
}

QByteArray f1( double v ) { return QString::number( v, 'f', 1 ).toUtf8(); }

// A pixel of a thumbnail, measured exactly as the patch search measures it (:495-506) and with the search's raw mapping
// (:521-522). The replica of lookAssistSurfaceAt (:710, file-static), plus the mapping.
LookAssistAutoWhiteBalancePatch patchAt( const std::vector<unsigned char> &rgb, int width, int height, int x, int y,
                                         int downscale, int rawW, int rawH )
{
    LookAssistAutoWhiteBalancePatch p;
    if( width <= 0 || height <= 0 || x < 0 || y < 0 || x >= width || y >= height
     || rgb.size() < static_cast<size_t>( width ) * height * 3 )
        return p;
    const unsigned char *px = &rgb[( static_cast<size_t>( y ) * width + x ) * 3];
    const int r = px[0], g = px[1], b = px[2];
    p.valid = true;
    p.thumbnailX = x;
    p.thumbnailY = y;
    p.rawX = std::max( 0, std::min( x * downscale + downscale / 2, rawW - 1 ) );
    p.rawY = std::max( 0, std::min( y * downscale + downscale / 2, rawH - 1 ) );
    p.chroma = static_cast<double>( std::max( r, std::max( g, b ) ) - std::min( r, std::min( g, b ) ) );
    p.luma = ( 54.0 * r + 183.0 * g + 19.0 * b ) / 256.0;
    p.greenAxis = static_cast<double>( g ) - ( ( static_cast<double>( r ) + b ) * 0.5 );
    p.blueAmberAxis = static_cast<double>( b ) - static_cast<double>( r );
    return p;
}

// The analysis the headless applier ran on this frame, replicated on the fixture as the run left it
// (ReceiptApplier.cpp applyHeadlessLookAssist: raw thumbnail stats -> scene -> the consumer picture at the scene's own lift,
// rendered at the balance the processing object held on entry -> findLookAssistAutoWhiteBalancePatch).
struct Analysis
{
    LookAssistStats stats;
    LookAssistScene scene = LookAssistScene::Night;
    bool processed = false;
    int colorDownscale = 0;
    int cw = 0;
    int ch = 0;
    int rawW = 0;
    int rawH = 0;
    double consumerStops = 0.0;
    int analysisExposure = 0;      // receipt units: what the g3 / g4 pictures are rendered at
    bool displayMeterValid = false;
    std::vector<unsigned char> consumer;
    LookAssistAutoWhiteBalancePatch patch;
};

Analysis replicateAnalysis( MlvPipelineFixture &fixture, int frame, double entryKelvin, double entryRenderTint )
{
    mlvObject_t *video = fixture.video();
    processingObject_t *processing = fixture.processing();
    processing->wb_tint = entryRenderTint;
    processingSetWhiteBalance( processing, entryKelvin, entryRenderTint );
    resetMlvCache( video );
    resetMlvCachedFrame( video );

    Analysis a;
    a.rawW = video->RAWI.xRes;
    a.rawH = video->RAWI.yRes;
    int downscale = 8;
    if( a.rawW > 4000 || a.rawH > 2500 ) downscale = 12;
    else if( a.rawW > 2800 || a.rawH > 1900 ) downscale = 10;
    else if( a.rawW > 1800 || a.rawH > 1200 ) downscale = 8;
    else downscale = 6;
    const int w = a.rawW / downscale;
    const int h = a.rawH / downscale;
    std::vector<unsigned char> thumbnail( static_cast<size_t>( w ) * h * 3 );
    get_area_average_downscale_raw_thumnail( video, frame, downscale, thumbnail.data() );
    a.stats = analyzeLookAssistThumbnail( thumbnail.data(), w, h );
    lookAssistSetSceneEv100( &a.stats, video->EXPO.isoValue, static_cast<double>( video->EXPO.shutterValue ),
                             video->LENS.aperture, ReceiptApplier::lookAssistRecoveryIso( video ) );
    int asShotTemperature = 6000;
    int asShotTint = 0;
    const bool hasAsShot = ReceiptApplier::asShotWhiteBalanceControls( video, &asShotTemperature, &asShotTint );
    lookAssistSetAsShotWhiteBalance( &a.stats, hasAsShot, asShotTemperature, asShotTint );

    a.colorDownscale = lookAssistIsFlatFloorRawThumbnail( a.stats ) ? std::max( 3, downscale / 3 ) : downscale;
    a.cw = a.rawW / a.colorDownscale;
    a.ch = a.rawH / a.colorDownscale;
    a.consumer.assign( static_cast<size_t>( a.cw ) * a.ch * 3, 0 );
    auto renderProcessed = [&]( double stops, LookAssistStats *s ) -> bool
    {
        if( !ReceiptApplier::processedThumbnailAtExposure( video, frame, a.colorDownscale, 1, stops, a.consumer.data() ) )
            return false;
        *s = analyzeLookAssistThumbnail( a.consumer.data(), a.cw, a.ch );
        return true;
    };
    a.scene = resolveLookAssistScene( &a.stats, renderProcessed );
    LookAssistStats colorStats;
    if( lookAssistShouldAnalyzeProcessedColor( a.scene, a.stats ) )
    {
        bool rendered = false;
        if( a.scene != LookAssistScene::Night )
        {
            a.consumerStops = presetForLookAssistScene( a.scene, a.stats ).exposure / 100.0;
            rendered = ReceiptApplier::processedThumbnailAtExposure( video, frame, a.colorDownscale, 1, a.consumerStops,
                                                                     a.consumer.data() );
        }
        if( !rendered ) get_area_average_downscale_thumnail( video, frame, a.colorDownscale, 1, a.consumer.data() );
        colorStats = analyzeLookAssistThumbnail( a.consumer.data(), a.cw, a.ch );
        a.processed = colorStats.median > 0.0 && colorStats.balanceSamples >= std::max( 32, ( a.cw * a.ch ) / 100 );
    }
    LookAssistStats displayStats;
    a.displayMeterValid = ReceiptApplier::lookAssistDisplayMeter( video, frame, downscale, 1, &displayStats );
    // ReceiptApplier.cpp :1010-1011 and initialDaylightPatchIsVerified :954-955.
    a.analysisExposure = a.displayMeterValid
        ? presetForLookAssistScene( a.scene, a.stats ).exposure
        : presetForLookAssistScene( a.scene, a.stats, a.processed ? &colorStats : nullptr, nullptr ).exposure;
    if( a.processed )
        a.patch = findLookAssistAutoWhiteBalancePatch( a.consumer.data(), a.cw, a.ch, a.colorDownscale, a.rawW, a.rawH );
    return a;
}

// One surface through the gates, exactly as resolveLookAssistWhiteBalance + initialDaylightPatchIsVerified judge Look
// Assist's own patch (:1003-1025, :928-983).
struct GateTrace
{
    LookAssistAutoWhiteBalancePatch surface;
    bool g1 = false, g1Luma = false, g1Chroma = false, g1Green = false, g1BlueAmber = false, g1Edge = false;
    double score = 0.0;                           // the search's ranking score (:511-515); the highest filtered pixel wins
    int rawSolveT = 0, rawSolveTint = 0;          // the solver's own output
    int candT = 0, candTint = 0;                  // after the control range and the rails (:1009-1011)
    bool g2 = false, g2NeutralEnough = false;
    int priorT = 0, priorTint = 0;
    bool priorAsked = false;
    double baseChroma = -1.0, baseBlueAmber = 0.0;
    bool g3 = false;
    int appliedT = 0, appliedTint = 0;            // the window-clamped solution (:973-974)
    double finalChroma = -1.0;
    bool g4Neutral = false, g4 = false;
    bool g5T = false, g5Tint = false;
    QString decision;                             // accepted | rejected-unstable | initial_patch_unverified
    QString firstRefusing;                        // g1 | g2 | g3 | g4 | none
};

GateTrace traceGates( MlvPipelineFixture &fixture, int frame, const Analysis &a, const LookAssistAutoWhiteBalancePatch &surface )
{
    GateTrace t;
    t.surface = surface;
    mlvObject_t *video = fixture.video();
    const LookAssistAutoWhiteBalancePatch &p = surface;

    // g1: the patch-search filters (:489-490 edge, :503-509).
    const int edgeX = std::max( 1, a.cw / 80 );
    const int edgeY = std::max( 1, a.ch / 80 );
    t.g1Edge = p.thumbnailX >= edgeX && p.thumbnailX < a.cw - edgeX && p.thumbnailY >= edgeY && p.thumbnailY < a.ch - edgeY;
    t.g1Luma = p.luma >= 70.0 && p.luma <= 220.0;
    t.g1Chroma = p.chroma <= std::max( 10.0, p.luma * 0.16 );
    t.g1Green = p.greenAxis <= 14.0;
    t.g1BlueAmber = std::fabs( p.blueAmberAxis ) <= 30.0;
    t.g1 = p.valid && t.g1Edge && t.g1Luma && t.g1Chroma && t.g1Green && t.g1BlueAmber;
    t.score = p.luma * 0.75 - p.chroma * 1.6 - std::max( 0.0, p.greenAxis ) * 2.8 - std::fabs( p.blueAmberAxis ) * 0.4;

    // The solve, as the headless applier runs it: the live object at the base balance (:984), the live solver (:1029).
    const int baseT = 6000, baseTint = 0;
    processingSetWhiteBalance( fixture.processing(), baseT, baseTint / 10.0 );
    t.rawSolveT = baseT;
    t.rawSolveTint = baseTint;
    findMlvWhiteBalanceAtAnalysisLevels( video, static_cast<uint64_t>( frame ), p.rawX, p.rawY, &t.rawSolveT, &t.rawSolveTint, 0 );
    t.candT = std::max( kLookAssistTemperatureMin, std::min( t.rawSolveT, kLookAssistTemperatureMax ) );
    t.candTint = std::max( kLookAssistTintMin, std::min( t.rawSolveTint, kLookAssistTintMax ) );
    t.candTint = std::max( kSolverMinTint, std::min( t.candTint, kSolverMaxTint ) );

    // g2: stability, daylight solve.
    t.g2NeutralEnough = lookAssistDaylightPatchIsNeutralEnough( p );
    t.g2 = lookAssistAutoWhiteBalanceSolutionIsStable( p, baseT, baseTint, t.candT, t.candTint, true );

    // g3: the same pixel at the as-shot prior, clamped into the window (:940-951, :967-970).
    const LookAssistWhiteBalanceBounds window = lookAssistWhiteBalanceBounds( a.stats, a.scene );
    const double stops = a.analysisExposure / 100.0;
    const LookAssistRenderBalanceFn render =
        ReceiptApplier::lookAssistBalanceRenderer( video, frame, a.colorDownscale, a.cw, a.ch, 1, false );
    t.priorT = baseT;
    t.priorTint = baseTint;
    t.priorAsked = lookAssistAsShotPrior( a.stats, a.scene, &t.priorT, &t.priorTint );
    if( t.priorAsked )
    {
        LookAssistRenderedPicture base;
        if( render( stops, t.priorT, t.priorTint, &base ) && base.width == a.cw && base.height == a.ch )
        {
            const LookAssistAutoWhiteBalancePatch s =
                patchAt( base.rgb, base.width, base.height, p.thumbnailX, p.thumbnailY, a.colorDownscale, a.rawW, a.rawH );
            t.baseChroma = s.chroma;
            t.baseBlueAmber = s.blueAmberAxis;
            t.g3 = lookAssistDaylightPatchIsNeutralEnough( s );
        }
    }

    // g4: the same pixel at the window-clamped solution (:973-982), slack hard-coded (:692).
    t.appliedT = std::max( window.minTemperature, std::min( t.candT, window.maxTemperature ) );
    t.appliedTint = std::max( window.minTint, std::min( t.candTint, window.maxTint ) );
    t.g5T = t.appliedT != t.candT;
    t.g5Tint = t.appliedTint != t.candTint;
    LookAssistRenderedPicture verify;
    if( render( stops, t.appliedT, t.appliedTint, &verify ) && verify.width == a.cw && verify.height == a.ch )
    {
        const LookAssistAutoWhiteBalancePatch s =
            patchAt( verify.rgb, verify.width, verify.height, p.thumbnailX, p.thumbnailY, a.colorDownscale, a.rawW, a.rawH );
        t.finalChroma = s.valid ? s.chroma : 0.0;
        t.g4Neutral = s.valid && lookAssistDaylightPatchIsNeutralEnough( s );
        t.g4 = t.g4Neutral && s.chroma <= p.chroma + kVerifyChromaSlack;
    }

    // The decision (:1014-1025): unstable -> rejected; stable but unverified (g3 or g4) -> master's fallback.
    if( !t.g2 ) t.decision = QStringLiteral( "rejected-unstable" );
    else if( !t.priorAsked || !t.g3 || !t.g4 ) t.decision = QStringLiteral( "initial_patch_unverified" );
    else t.decision = QStringLiteral( "accepted" );
    t.firstRefusing = !t.g1 ? QStringLiteral( "g1" ) : !t.g2 ? QStringLiteral( "g2" ) : !t.g3 ? QStringLiteral( "g3" )
                    : !t.g4 ? QStringLiteral( "g4" ) : QStringLiteral( "none" );
    return t;
}

void printTrace( const char *tag, const char *which, const GateTrace &t )
{
    const LookAssistAutoWhiteBalancePatch &p = t.surface;
    std::printf( "WB_GATE %s %s surface valid=%d thumb=%d,%d raw=%d,%d luma=%.1f chroma=%.1f green=%.1f blueAmber=%.1f\n",
                 tag, which, p.valid ? 1 : 0, p.thumbnailX, p.thumbnailY, p.rawX, p.rawY, p.luma, p.chroma, p.greenAxis,
                 p.blueAmberAxis );
    std::printf( "WB_GATE %s %s g1 pass=%d edge=%d luma=%d chroma=%d(lim %.1f) green=%d blueAmber=%d score=%.2f\n", tag, which,
                 t.g1 ? 1 : 0, t.g1Edge ? 1 : 0, t.g1Luma ? 1 : 0, t.g1Chroma ? 1 : 0, std::max( 10.0, p.luma * 0.16 ),
                 t.g1Green ? 1 : 0, t.g1BlueAmber ? 1 : 0, t.score );
    std::printf( "WB_GATE %s %s solve raw=%d/%d cand=%d/%d\n", tag, which, t.rawSolveT, t.rawSolveTint, t.candT, t.candTint );
    std::printf( "WB_GATE %s %s g2 pass=%d neutralEnough=%d(chroma lim %.1f, |B-R| lim 20)\n", tag, which, t.g2 ? 1 : 0,
                 t.g2NeutralEnough ? 1 : 0, std::max( 10.0, p.luma * 0.09 ) );
    std::printf( "WB_GATE %s %s g3 pass=%d prior=%d/%d asked=%d baseChroma=%.1f baseBlueAmber=%.1f\n", tag, which,
                 t.g3 ? 1 : 0, t.priorT, t.priorTint, t.priorAsked ? 1 : 0, t.baseChroma, t.baseBlueAmber );
    std::printf( "WB_GATE %s %s g4 pass=%d at=%d/%d foundChroma=%.1f finalChroma=%.1f margin=%.2f slack=%.2f neutralEnough=%d\n",
                 tag, which, t.g4 ? 1 : 0, t.appliedT, t.appliedTint, p.chroma, t.finalChroma, t.finalChroma - p.chroma,
                 kVerifyChromaSlack, t.g4Neutral ? 1 : 0 );
    std::printf( "WB_GATE %s %s g5 binds=%d temperature=%d tint=%d\n", tag, which, ( t.g5T || t.g5Tint ) ? 1 : 0,
                 t.g5T ? 1 : 0, t.g5Tint ? 1 : 0 );
    std::printf( "WB_GATE %s %s decision=%s first_refusing=%s\n", tag, which, t.decision.toUtf8().constData(),
                 t.firstRefusing.toUtf8().constData() );
    std::fflush( stdout );
}

void probeState( const char *tag, const char *clip, int frame )
{
    MlvPipelineFixture fixture;
    QString error_message;
    ASSERT_TRUE( fixture.openClipFile( repo_file_path( QString::fromLatin1( clip ) ), &error_message ) );
    ASSERT_TRUE( fixture.applyReceipt( &error_message ) );
    ReceiptSettings &receipt = fixture.receipt();
    receipt.setLookAssistEnabled( true );
    receipt.setLookAssistBaselineValid( false );
    receipt.setExposure( 0 );
    receipt.setTemperature( -1 );
    receipt.setTint( 0 );
    const double entryKelvin = processingGetWhiteBalanceKelvin( fixture.processing() );
    const double entryRenderTint = processingGetWhiteBalanceTint( fixture.processing() );

    QTemporaryDir temporary_dir;
    const QString log_path = temporary_dir.filePath( QStringLiteral( "look_assist.log" ) );
    BatchLogger::init( log_path );
    const bool applied = ReceiptApplier::applyHeadlessLookAssist(
        &receipt, fixture.video(), fixture.processing(), static_cast<uint32_t>( frame ) );
    BatchLogger::shutdown();
    ASSERT_TRUE( applied );
    const QByteArray log = readLog( log_path );
    const QString appliedLine = lastLine( log, "LOOK_ASSIST applied" );
    const QString fallbackLine = lastLine( log, "daylight_fallback_to_master" );
    const bool fallback = !fallbackLine.isEmpty();
    const int appliedT = receipt.temperature();
    const int appliedTint = receipt.tint();
    const int exposure = receipt.exposure();
    std::printf( "WB_GATE %s state clip=%s frame=%d entry=%.3f/%.9f applied=%d/%d exp=%d source=%s decision=%s scene=%s "
                 "chromaSmoothAuto=%s logCand=%s/%s fallback=%d reason=%s logBaseChroma=%s logFinalChroma=%s\n",
                 tag, clip, frame, entryKelvin, entryRenderTint, appliedT, appliedTint, exposure,
                 field( appliedLine, "autoWbSource" ).toUtf8().constData(),
                 field( appliedLine, "autoWbDecision" ).toUtf8().constData(),
                 field( appliedLine, "scene" ).toUtf8().constData(),
                 field( appliedLine, "chromaSmoothAuto" ).toUtf8().constData(),
                 field( appliedLine, "autoWbCandidateTemp" ).toUtf8().constData(),
                 field( appliedLine, "autoWbCandidateTint" ).toUtf8().constData(), fallback ? 1 : 0,
                 field( fallbackLine, "reason" ).toUtf8().constData(),
                 field( fallback ? fallbackLine : appliedLine, "initialPatchBaseChroma" ).toUtf8().constData(),
                 field( fallback ? fallbackLine : appliedLine, "initialPatchFinalChroma" ).toUtf8().constData() );
    std::fflush( stdout );

    if( fallback )
    {
        // The daylight pass searched its patch with chroma smoothing on (r1 Phase B); the fallback switched it back.
        llrpSetChromaSmoothMode( fixture.video(), 1 );
        llrpResetFpmStatus( fixture.video() );
        llrpResetBpmStatus( fixture.video() );
    }
    const Analysis a = replicateAnalysis( fixture, frame, entryKelvin, entryRenderTint );
    std::printf( "WB_GATE %s analysis scene=%s daylight=%d processed=%d colorDownscale=%d consumer=%dx%d stops=%.2f "
                 "analysisExposure=%d displayMeter=%d asShot=%d/%d/%d\n",
                 tag, lookAssistSceneName( a.scene ).toUtf8().constData(), lookAssistIsDaylightScene( a.stats, a.scene ) ? 1 : 0,
                 a.processed ? 1 : 0, a.colorDownscale, a.cw, a.ch, a.consumerStops, a.analysisExposure,
                 a.displayMeterValid ? 1 : 0, a.stats.hasAsShotWb ? 1 : 0, a.stats.asShotTemperature, a.stats.asShotTint );
    std::fflush( stdout );
    ASSERT_TRUE( a.processed );
    ASSERT_TRUE( a.patch.valid );
    const LookAssistAutoWhiteBalancePatch laPatch = a.patch;

    // (i) Look Assist's own patch.
    const GateTrace la = traceGates( fixture, frame, a, laPatch );
    printTrace( tag, "la", la );

    // PREMISES, against the applier's own log of this run.
    if( !fallback )
    {
        const bool patchMatches = field( appliedLine, "patchValid" ) == QStringLiteral( "true" )
            && field( appliedLine, "patchLuma" ).toUtf8() == f1( laPatch.luma )
            && field( appliedLine, "patchChroma" ).toUtf8() == f1( laPatch.chroma )
            && field( appliedLine, "patchBlueAmber" ).toUtf8() == f1( laPatch.blueAmberAxis )
            && field( appliedLine, "patchGreenAxis" ).toUtf8() == f1( laPatch.greenAxis );
        const bool candidateMatches = field( appliedLine, "autoWbCandidateTemp" ) == QString::number( la.candT )
            && field( appliedLine, "autoWbCandidateTint" ) == QString::number( la.candTint );
        std::printf( "WB_GATE %s premise patch=%d candidate=%d\n", tag, patchMatches ? 1 : 0, candidateMatches ? 1 : 0 );
        std::fflush( stdout );
        ASSERT_TRUE( patchMatches );
        ASSERT_TRUE( candidateMatches );
    }
    // What the decision logs: nothing (0.0) past a gate that returned early (unstable: never checked, :1018-1019;
    // refused at base: no verification render, :970).
    const QString &chromaLine = fallback ? fallbackLine : appliedLine;
    const double expectBase = la.g2 ? la.baseChroma : 0.0;
    const double expectFinal = ( la.g2 && la.g3 ) ? la.finalChroma : 0.0;
    const bool chromaMatches = field( chromaLine, "initialPatchBaseChroma" ).toUtf8() == f1( expectBase )
        && field( chromaLine, "initialPatchFinalChroma" ).toUtf8() == f1( expectFinal );
    const QString loggedDecision = fallback
        ? field( fallbackLine, "reason" )
        : field( appliedLine, "autoWbDecision" );
    const bool decisionMatches = loggedDecision == la.decision;
    std::printf( "WB_GATE %s premise g3g4_chroma=%d decision=%d (logged %s, replicated %s)\n", tag, chromaMatches ? 1 : 0,
                 decisionMatches ? 1 : 0, loggedDecision.toUtf8().constData(), la.decision.toUtf8().constData() );
    std::fflush( stdout );
    ASSERT_TRUE( chromaMatches );
    ASSERT_TRUE( decisionMatches );

    // (ii) The deck box centre (r1 Phase B's deck region, in RAW / render coordinates), as a thumbnail pixel.
    const int width = fixture.width();
    const int height = fixture.height();
    const int deckCx = ( static_cast<int>( width * 0.02 ) + static_cast<int>( width * 0.25 ) ) / 2;
    const int deckCy = ( static_cast<int>( height * 0.65 ) + static_cast<int>( height * 0.98 ) ) / 2;
    const LookAssistAutoWhiteBalancePatch deckPatch = patchAt( a.consumer, a.cw, a.ch, deckCx / a.colorDownscale,
                                                               deckCy / a.colorDownscale, a.colorDownscale, a.rawW, a.rawH );
    std::printf( "WB_GATE %s deck centre=%d,%d render=%dx%d raw=%dx%d\n", tag, deckCx, deckCy, width, height, a.rawW, a.rawH );
    const GateTrace deck = traceGates( fixture, frame, a, deckPatch );
    printTrace( tag, "deck", deck );
    // The search keeps the highest-scoring filtered pixel (:516): a deck that passes g1 still loses to a higher score.
    std::printf( "WB_GATE %s deck rank score=%.2f la_score=%.2f loses_ranking=%d\n", tag, deck.score, la.score,
                 ( deck.g1 && deck.score < la.score ) ? 1 : 0 );
    std::fflush( stdout );

    // The deck chroma (S1 / S2's measurement) at the applied exposure, last: renderWith moves the live balance.
    const LookAssistWhiteBalanceBounds window = lookAssistWhiteBalanceBounds( a.stats, a.scene );
    auto clampT = [&]( int v ) { return std::max( window.minTemperature, std::min( v, window.maxTemperature ) ); };
    auto clampTint = [&]( int v ) { return std::max( window.minTint, std::min( v, window.maxTint ) ); };
    struct Balance { const char *name; int t; int tint; };
    const Balance balances[] = {
        { "applied", appliedT, appliedTint },
        { "CANDw", clampT( la.candT ), clampTint( la.candTint ) },
        { "DSw", clampT( deck.rawSolveT ), clampTint( deck.rawSolveTint ) },
        { "DSt", appliedT, deck.rawSolveTint },
    };
    for( const Balance &b : balances )
    {
        const double c = deckCastChroma( renderWith( fixture, frame, b.t, b.tint, exposure ), width, height );
        std::printf( "WB_GATE %s deck_chroma %s=%d/%d exp=%d c=%.4f vs S1=9.9 S2=9.8 staged=8.6 original=6.0\n", tag, b.name,
                     b.t, b.tint, exposure, c );
    }
    std::fflush( stdout );
}

} // namespace

TEST(LookAssistWbGateProbe, GateTraceOnTheTrackedFixtures)
{
    if( qgetenv( "MLVAPP_LA_WB_GATE_PROBE" ) != "1" )
        SKIP_TEST( "measure-only probe (LOOK-ASSIST-WB-DECISION-1); set MLVAPP_LA_WB_GATE_PROBE=1 to run it" );
    probeState( "tiny-f0", kTinyClip, 0 );
    probeState( "large-f0", kLargeClip, 0 );
    probeState( "large-f5", kLargeClip, 5 );
    probeState( "large-f10", kLargeClip, 10 );
    probeState( "large-f15", kLargeClip, 15 );
}
