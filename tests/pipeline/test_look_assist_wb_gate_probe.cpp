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
//
// WB-GATE-PROBE-HARDEN-1: the decision replica models every branch of the patch decision (undamped / damped, unstable,
// refused, prior not asked, the request's control range), not only the undamped daylight one the tracked states take. The
// product's own resolveLookAssistWhiteBalance is replayed on the replica's request and both are held to the log, which is
// also how a fallback state's patch and candidate are bound (its line names neither). The DecisionReplica* tests below are
// NOT gated: they drive the product decision on synthetic one-surface pictures, one branch each, and fail naming the branch
// when the product no longer takes it.
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
#include <utility>
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

// One request to the decision, as the headless applier builds it (ReceiptApplier.cpp :1038-1054).
struct DecisionInputs
{
    LookAssistStats stats;
    LookAssistScene scene = LookAssistScene::Shade;
    LookAssistAutoWhiteBalancePatch patch;
    bool solvedOnProcessedPicture = false;
    int baseT = 6000;
    int baseTint = 0;
    int minT = kLookAssistTemperatureMin;   // the request's control range (:1009-1010, :941-944)
    int maxT = kLookAssistTemperatureMax;
    int minTint = kLookAssistTintMin;
    int maxTint = kLookAssistTintMax;
    int rawW = 0;
    int rawH = 0;
    int analysisExposure = 0;               // receipt units: what the verification pictures are rendered at (:954-955)
    bool refineWithoutPatch = true;
    LookAssistRenderBalanceFn render;
    LookAssistWhiteBalanceSolveFn solve;
};

// The verification window (:940-945): the scene's window intersected with the request's control range.
LookAssistWhiteBalanceBounds verificationWindow( const DecisionInputs &in )
{
    LookAssistWhiteBalanceBounds window = lookAssistWhiteBalanceBounds( in.stats, in.scene );
    window.minTemperature = std::max( window.minTemperature, in.minT );
    window.maxTemperature = std::min( window.maxTemperature, in.maxT );
    window.minTint = std::max( window.minTint, in.minTint );
    window.maxTint = std::min( window.maxTint, in.maxTint );
    return window;
}

// initialDaylightPatchIsVerified (:928-983), step for step. `exit` names where it returned. The chroma fields are what it
// leaves in the resolution: 0.0 (the struct's default) where it never measured, never a sentinel.
struct VerificationReplica
{
    QString exit = QStringLiteral( "not-called" );
    bool priorAsked = false;
    int priorT = 0;
    int priorTint = 0;
    double baseChroma = 0.0;
    double baseBlueAmber = 0.0;
    bool refusedAtBase = false;
    int appliedT = 0;
    int appliedTint = 0;
    double finalChroma = 0.0;
    bool verified = false;
};

VerificationReplica replicateVerification( const DecisionInputs &in, int solvedT, int solvedTint )
{
    VerificationReplica v;
    const LookAssistAutoWhiteBalancePatch &patch = in.patch;
    if( !in.refineWithoutPatch || !in.render || in.rawW <= 0 || in.rawH <= 0 )
    {
        v.exit = QStringLiteral( "cannot-ask" );
        return v;
    }
    const LookAssistWhiteBalanceBounds window = verificationWindow( in );
    if( window.minTemperature > window.maxTemperature || window.minTint > window.maxTint )
    {
        v.exit = QStringLiteral( "window-empty" );
        return v;
    }
    v.priorT = in.baseT;
    v.priorTint = in.baseTint;
    v.priorAsked = lookAssistAsShotPrior( in.stats, in.scene, &v.priorT, &v.priorTint );
    if( !v.priorAsked )
    {
        v.exit = QStringLiteral( "no-prior" );
        return v;
    }
    v.priorT = std::max( window.minTemperature, std::min( v.priorT, window.maxTemperature ) );
    v.priorTint = std::max( window.minTint, std::min( v.priorTint, window.maxTint ) );

    const double stops = in.analysisExposure / 100.0;
    LookAssistRenderedPicture base;
    if( !in.render( stops, v.priorT, v.priorTint, &base ) || base.stats.median <= 0.0 )
    {
        v.exit = QStringLiteral( "base-render" );
        return v;
    }
    if( base.width <= 0 || base.height <= 0 || base.downscaleFactor <= 0
     || base.rgb.size() < static_cast<size_t>( base.width ) * base.height * 3
     || patch.thumbnailX < 0 || patch.thumbnailX >= base.width || patch.thumbnailY < 0 || patch.thumbnailY >= base.height
     || patch.rawX != std::max( 0, std::min( patch.thumbnailX * base.downscaleFactor + base.downscaleFactor / 2, in.rawW - 1 ) )
     || patch.rawY != std::max( 0, std::min( patch.thumbnailY * base.downscaleFactor + base.downscaleFactor / 2, in.rawH - 1 ) ) )
    {
        v.exit = QStringLiteral( "not-a-pixel" );
        return v;
    }
    const LookAssistAutoWhiteBalancePatch atBase =
        patchAt( base.rgb, base.width, base.height, patch.thumbnailX, patch.thumbnailY, base.downscaleFactor, in.rawW, in.rawH );
    v.baseChroma = atBase.chroma;
    v.baseBlueAmber = atBase.blueAmberAxis;
    if( !lookAssistDaylightPatchIsNeutralEnough( atBase ) )
    {
        v.refusedAtBase = true;
        v.exit = QStringLiteral( "refused-at-base" );
        return v;
    }

    v.appliedT = std::max( window.minTemperature, std::min( solvedT, window.maxTemperature ) );
    v.appliedTint = std::max( window.minTint, std::min( solvedTint, window.maxTint ) );
    LookAssistRenderedPicture verify;
    // lookAssistSamePictureGeometry (:732-737).
    if( !in.render( stops, v.appliedT, v.appliedTint, &verify ) || verify.width != base.width || verify.height != base.height
     || verify.downscaleFactor != base.downscaleFactor
     || verify.rgb.size() < static_cast<size_t>( verify.width ) * verify.height * 3 )
    {
        v.exit = QStringLiteral( "verify-render" );
        return v;
    }
    const LookAssistAutoWhiteBalancePatch atSolution =
        patchAt( verify.rgb, verify.width, verify.height, patch.thumbnailX, patch.thumbnailY, base.downscaleFactor, in.rawW, in.rawH );
    v.finalChroma = atSolution.valid ? atSolution.chroma : 0.0;
    v.verified = atSolution.valid && lookAssistDaylightPatchIsNeutralEnough( atSolution )
              && atSolution.chroma <= patch.chroma + kVerifyChromaSlack;
    v.exit = v.verified ? QStringLiteral( "verified" ) : QStringLiteral( "unverified-at-solution" );
    return v;
}

// resolveLookAssistWhiteBalance's patch branch (:1003-1057), and what the applier LOGS for it (ReceiptApplier.cpp
// :1065-1073): corroborated daylight with nothing accepted falls back to master's pass and names the reason there.
struct DecisionReplica
{
    QString branch = QStringLiteral( "none" );  // accepted | accepted-damped | rejected-unverified | rejected-unstable | none
    QString logged;                             // autoWbDecision on the applied line, or the fallback line's reason
    bool fallback = false;
    // Daylight, nothing accepted and nothing refused: the refinement walk runs (:1064-1080). The replica does not model the
    // walk; it predicts the fallback the walk ends in when it acquires no surface.
    bool walkUnmodelled = false;
    bool undamped = false;
    bool stable = false;
    double damping = 1.0;
    int candT = 0;
    int candTint = 0;
    int solvedT = 0;
    int solvedTint = 0;
    bool verificationRan = false;               // the product's initialPatchChecked
    VerificationReplica verification;
};

DecisionReplica replicateDecision( const DecisionInputs &in )
{
    DecisionReplica r;
    const bool daylight = lookAssistIsDaylightScene( in.stats, in.scene );
    r.undamped = in.solvedOnProcessedPicture && daylight;   // lookAssistDaylightSolveIsUndamped (:232-235)
    bool accepted = false;
    bool refused = false;
    if( in.patch.valid )
    {
        int t = in.baseT;
        int tint = in.baseTint;
        if( in.solve ) in.solve( in.patch.rawX, in.patch.rawY, &t, &tint );
        r.candT = std::max( in.minT, std::min( t, in.maxT ) );
        r.candTint = std::max( in.minTint, std::min( tint, in.maxTint ) );
        r.candTint = std::max( kSolverMinTint, std::min( r.candTint, kSolverMaxTint ) );
        r.solvedT = r.candT;
        r.solvedTint = r.candTint;
        r.stable = lookAssistAutoWhiteBalanceSolutionIsStable( in.patch, in.baseT, in.baseTint, r.candT, r.candTint, r.undamped );
        // Only an undamped stable solve is verified (:1018-1019); a damped one never is.
        if( r.stable && r.undamped )
        {
            r.verificationRan = true;
            r.verification = replicateVerification( in, r.candT, r.candTint );
            refused = !r.verification.verified;
        }
        if( refused )
        {
            r.branch = QStringLiteral( "rejected-unverified" );
        }
        else if( r.stable )
        {
            r.damping = r.undamped ? 1.0
                : lookAssistAutoWhiteBalanceDampingFactor( in.patch, in.baseT, in.baseTint, r.candT, r.candTint, in.scene );
            if( r.damping < 0.999 )
            {
                r.solvedT = std::max( in.minT, std::min( in.baseT + qRound( ( r.candT - in.baseT ) * r.damping ), in.maxT ) );
                r.solvedTint = std::max( in.minTint, std::min( in.baseTint + qRound( ( r.candTint - in.baseTint ) * r.damping ),
                                                               in.maxTint ) );
                r.branch = QStringLiteral( "accepted-damped" );
            }
            else
            {
                r.branch = QStringLiteral( "accepted" );
            }
            accepted = true;
        }
        else
        {
            r.branch = QStringLiteral( "rejected-unstable" );
        }
    }
    if( !accepted && daylight )
    {
        r.fallback = true;
        r.walkUnmodelled = !refused && in.refineWithoutPatch && static_cast<bool>( in.render );
        r.logged = refused ? QStringLiteral( "initial_patch_unverified" ) : QStringLiteral( "no_verified_surface" );
    }
    else
    {
        r.logged = r.branch;
    }
    return r;
}

// ReceiptApplier.cpp :1070: the reason the applier logs when the daylight pass falls back to master's.
QString applierFallbackReason( const LookAssistWhiteBalanceResolution &wb )
{
    return wb.initialPatchRefused ? QStringLiteral( "initial_patch_unverified" ) : QStringLiteral( "no_verified_surface" );
}

// The product's own decision on the same request, with every balance it asked the renderer for.
struct ProductRun
{
    LookAssistWhiteBalanceResolution wb;
    std::vector<std::pair<int, int>> renders;
    bool fallback = false;   // ReceiptApplier.cpp :1065 (the daylight pass)
    QString logged;
};

ProductRun runProduct( const DecisionInputs &in )
{
    ProductRun run;
    LookAssistWhiteBalanceRequest request;
    request.stats = &in.stats;
    request.scene = in.scene;
    request.patch = in.patch;
    request.solvedOnProcessedPicture = in.solvedOnProcessedPicture;
    request.baseTemperature = in.baseT;
    request.baseTint = in.baseTint;
    request.minTemperature = in.minT;
    request.maxTemperature = in.maxT;
    request.minTint = in.minTint;
    request.maxTint = in.maxTint;
    request.rawWidth = in.rawW;
    request.rawHeight = in.rawH;
    request.analysisExposure = in.analysisExposure;
    request.refineWithoutPatch = in.refineWithoutPatch;
    if( in.render )
    {
        request.renderBalance = [&run, &in]( double stops, int temperature, int tint, LookAssistRenderedPicture *picture )
        {
            run.renders.emplace_back( temperature, tint );
            return in.render( stops, temperature, tint, picture );
        };
    }
    LookAssistPreset preset;
    run.wb = resolveLookAssistWhiteBalance( request, in.solve, &preset );
    run.fallback = run.wb.legacyBalance;
    run.logged = run.fallback ? applierFallbackReason( run.wb ) : run.wb.decision;
    return run;
}

// The replica held to the product, field by field: everything the applier logs or the decision turns on.
bool replicaMatchesProduct( const char *tag, const DecisionReplica &r, const ProductRun &p )
{
    const bool logged = r.logged == p.logged;
    const bool fallback = r.fallback == p.fallback;
    const bool verification = r.verificationRan == p.wb.initialPatchChecked;
    const bool candidate = r.candT == p.wb.candidateTemperature && r.candTint == p.wb.candidateTint;
    const bool solved = r.solvedT == p.wb.solvedTemperature && r.solvedTint == p.wb.solvedTint;
    const bool damping = r.damping == p.wb.damping;
    const bool chroma = r.verification.baseChroma == p.wb.initialPatchBaseChroma
                     && r.verification.finalChroma == p.wb.initialPatchFinalChroma;
    const bool refusedAtBase = r.verification.refusedAtBase == p.wb.initialPatchRefusedAtBase;
    std::printf( "WB_GATE %s replica_vs_product logged=%d(%s/%s) fallback=%d verification=%d(%s) candidate=%d(%d/%d vs %d/%d) "
                 "solved=%d damping=%d(%.3f vs %.3f) chroma=%d(%.1f/%.1f vs %.1f/%.1f) refusedAtBase=%d\n",
                 tag, logged ? 1 : 0, r.logged.toUtf8().constData(), p.logged.toUtf8().constData(), fallback ? 1 : 0,
                 verification ? 1 : 0, r.verification.exit.toUtf8().constData(), candidate ? 1 : 0, r.candT, r.candTint,
                 p.wb.candidateTemperature, p.wb.candidateTint, solved ? 1 : 0, damping ? 1 : 0, r.damping, p.wb.damping,
                 chroma ? 1 : 0, r.verification.baseChroma, r.verification.finalChroma, p.wb.initialPatchBaseChroma,
                 p.wb.initialPatchFinalChroma, refusedAtBase ? 1 : 0 );
    std::fflush( stdout );
    return logged && fallback && verification && candidate && solved && damping && chroma && refusedAtBase;
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
    DecisionReplica decision;                     // what the product decides for this surface, and what the applier logs
    QString firstRefusing;                        // g1 | g2 | g3 | g4 | none
};

// The request the headless applier builds for `surface` on this frame: the live solver at the base balance (:1016, :1059-1063)
// and the live renderer (:1053-1054).
DecisionInputs decisionInputs( MlvPipelineFixture &fixture, int frame, const Analysis &a,
                               const LookAssistAutoWhiteBalancePatch &surface )
{
    mlvObject_t *video = fixture.video();
    processingObject_t *processing = fixture.processing();
    DecisionInputs in;
    in.stats = a.stats;
    in.scene = a.scene;
    in.patch = surface;
    in.solvedOnProcessedPicture = a.processed;
    in.rawW = a.rawW;
    in.rawH = a.rawH;
    in.analysisExposure = a.analysisExposure;
    in.refineWithoutPatch = lookAssistRefineDaylightWithoutPatchEnabled();
    in.render = ReceiptApplier::lookAssistBalanceRenderer( video, frame, a.colorDownscale, a.cw, a.ch, 1, false );
    const int baseT = in.baseT, baseTint = in.baseTint;
    in.solve = [video, processing, frame, baseT, baseTint]( int rawX, int rawY, int *temperature, int *tint )
    {
        processingSetWhiteBalance( processing, baseT, baseTint / 10.0 );
        findMlvWhiteBalanceAtAnalysisLevels( video, static_cast<uint64_t>( frame ), rawX, rawY, temperature, tint, 0 );
    };
    return in;
}

GateTrace traceGates( const Analysis &a, const DecisionInputs &in )
{
    GateTrace t;
    t.surface = in.patch;
    const LookAssistAutoWhiteBalancePatch &p = in.patch;

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
    const int baseT = in.baseT, baseTint = in.baseTint;
    t.rawSolveT = baseT;
    t.rawSolveTint = baseTint;
    in.solve( p.rawX, p.rawY, &t.rawSolveT, &t.rawSolveTint );
    t.candT = std::max( in.minT, std::min( t.rawSolveT, in.maxT ) );
    t.candTint = std::max( in.minTint, std::min( t.rawSolveTint, in.maxTint ) );
    t.candTint = std::max( kSolverMinTint, std::min( t.candTint, kSolverMaxTint ) );

    // g2: stability, daylight solve.
    t.g2NeutralEnough = lookAssistDaylightPatchIsNeutralEnough( p );
    t.g2 = lookAssistAutoWhiteBalanceSolutionIsStable( p, baseT, baseTint, t.candT, t.candTint, true );

    // g3: the same pixel at the as-shot prior, clamped into the window (:940-951, :967-970). Measured whatever g2 says (this is
    // the trace; what the product does is the decision replica below).
    const LookAssistWhiteBalanceBounds window = verificationWindow( in );
    const double stops = a.analysisExposure / 100.0;
    const LookAssistRenderBalanceFn &render = in.render;
    t.priorT = baseT;
    t.priorTint = baseTint;
    t.priorAsked = lookAssistAsShotPrior( a.stats, a.scene, &t.priorT, &t.priorTint );
    t.priorT = std::max( window.minTemperature, std::min( t.priorT, window.maxTemperature ) );
    t.priorTint = std::max( window.minTint, std::min( t.priorTint, window.maxTint ) );
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

    // The decision (:1003-1090), every branch, as the applier logs it.
    t.decision = replicateDecision( in );
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
    std::printf( "WB_GATE %s %s decision=%s first_refusing=%s branch=%s undamped=%d verification=%s walk_unmodelled=%d\n", tag,
                 which, t.decision.logged.toUtf8().constData(), t.firstRefusing.toUtf8().constData(),
                 t.decision.branch.toUtf8().constData(), t.decision.undamped ? 1 : 0,
                 t.decision.verification.exit.toUtf8().constData(), t.decision.walkUnmodelled ? 1 : 0 );
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
    const DecisionInputs laInputs = decisionInputs( fixture, frame, a, laPatch );
    const GateTrace la = traceGates( a, laInputs );
    printTrace( tag, "la", la );
    const DecisionReplica &replica = la.decision;
    // The product's own decision on the replica's request (same patch, the live solver and renderer).
    const ProductRun replay = runProduct( laInputs );

    // PREMISES, against the applier's own log of this run.
    // The replica stops at the refinement walk; a walk that acquired a surface is a branch it does not replicate.
    const bool branchReplicated = !replica.walkUnmodelled || fallback;
    std::printf( "WB_GATE %s premise branch_replicated=%d (branch %s, walk_unmodelled=%d, logged fallback=%d)\n", tag,
                 branchReplicated ? 1 : 0, replica.branch.toUtf8().constData(), replica.walkUnmodelled ? 1 : 0, fallback ? 1 : 0 );
    std::fflush( stdout );
    ASSERT_TRUE( branchReplicated );
    const QString &chromaLine = fallback ? fallbackLine : appliedLine;
    const QString loggedDecision = fallback
        ? field( fallbackLine, "reason" )
        : field( appliedLine, "autoWbDecision" );
    // The replayed decision reproduces every field the applier logged for its own run. A fallback line names no patch and no
    // candidate (ReceiptApplier.cpp :1068-1073), so for a fallback state this is the binding the log affords for both: the
    // patch and candidate the replica found, through the product's own decision, give the logged reason, refusedAtBase and
    // base / final chroma (WB-GATE-FALLBACK-PREMISE-COVERAGE-1).
    const bool replayMatchesLog = replay.fallback == fallback && replay.logged == loggedDecision
        && field( chromaLine, "initialPatchBaseChroma" ).toUtf8() == f1( replay.wb.initialPatchBaseChroma )
        && field( chromaLine, "initialPatchFinalChroma" ).toUtf8() == f1( replay.wb.initialPatchFinalChroma )
        && ( fallback
             ? field( fallbackLine, "refusedAtBase" )
                   == ( ( replay.wb.refineRefusedAtBase || replay.wb.initialPatchRefusedAtBase ) ? QStringLiteral( "true" )
                                                                                                 : QStringLiteral( "false" ) )
             : field( appliedLine, "autoWbCandidateTemp" ) == QString::number( replay.wb.candidateTemperature )
                   && field( appliedLine, "autoWbCandidateTint" ) == QString::number( replay.wb.candidateTint ) );
    bool patchMatches = false;
    bool candidateMatches = false;
    if( !fallback )
    {
        patchMatches = field( appliedLine, "patchValid" ) == QStringLiteral( "true" )
            && field( appliedLine, "patchLuma" ).toUtf8() == f1( laPatch.luma )
            && field( appliedLine, "patchChroma" ).toUtf8() == f1( laPatch.chroma )
            && field( appliedLine, "patchBlueAmber" ).toUtf8() == f1( laPatch.blueAmberAxis )
            && field( appliedLine, "patchGreenAxis" ).toUtf8() == f1( laPatch.greenAxis );
        candidateMatches = field( appliedLine, "autoWbCandidateTemp" ) == QString::number( la.candT )
            && field( appliedLine, "autoWbCandidateTint" ) == QString::number( la.candTint );
    }
    else
    {
        patchMatches = replayMatchesLog;
        candidateMatches = la.candT == replay.wb.candidateTemperature && la.candTint == replay.wb.candidateTint
            && replica.candT == la.candT && replica.candTint == la.candTint;
    }
    std::printf( "WB_GATE %s premise patch=%d candidate=%d bound_by=%s replay_vs_log=%d\n", tag, patchMatches ? 1 : 0,
                 candidateMatches ? 1 : 0, fallback ? "replayed-decision" : "applied-line", replayMatchesLog ? 1 : 0 );
    std::fflush( stdout );
    ASSERT_TRUE( patchMatches );
    ASSERT_TRUE( candidateMatches );
    ASSERT_TRUE( replayMatchesLog );
    // What the decision logs: nothing (0.0) past a gate that returned early (unstable or damped: never checked, :1018-1019;
    // no prior / empty window: :945, :949; refused at base: no verification render, :970).
    const bool chromaMatches = field( chromaLine, "initialPatchBaseChroma" ).toUtf8() == f1( replica.verification.baseChroma )
        && field( chromaLine, "initialPatchFinalChroma" ).toUtf8() == f1( replica.verification.finalChroma );
    const bool decisionMatches = loggedDecision == replica.logged;
    std::printf( "WB_GATE %s premise g3g4_chroma=%d decision=%d (logged %s, replicated %s)\n", tag, chromaMatches ? 1 : 0,
                 decisionMatches ? 1 : 0, loggedDecision.toUtf8().constData(), replica.logged.toUtf8().constData() );
    std::fflush( stdout );
    ASSERT_TRUE( chromaMatches );
    ASSERT_TRUE( decisionMatches );
    ASSERT_TRUE( replicaMatchesProduct( tag, replica, replay ) );

    // (ii) The deck box centre (r1 Phase B's deck region, in RAW / render coordinates), as a thumbnail pixel.
    const int width = fixture.width();
    const int height = fixture.height();
    const int deckCx = ( static_cast<int>( width * 0.02 ) + static_cast<int>( width * 0.25 ) ) / 2;
    const int deckCy = ( static_cast<int>( height * 0.65 ) + static_cast<int>( height * 0.98 ) ) / 2;
    const LookAssistAutoWhiteBalancePatch deckPatch = patchAt( a.consumer, a.cw, a.ch, deckCx / a.colorDownscale,
                                                               deckCy / a.colorDownscale, a.colorDownscale, a.rawW, a.rawH );
    std::printf( "WB_GATE %s deck centre=%d,%d render=%dx%d raw=%dx%d\n", tag, deckCx, deckCy, width, height, a.rawW, a.rawH );
    const GateTrace deck = traceGates( a, decisionInputs( fixture, frame, a, deckPatch ) );
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

// ---- Decision-replica fixtures (WB-GATE-PROBE-HARDEN-1) ----
// The five tracked states all take the undamped daylight path with the as-shot prior asked and the default control range, so
// the replica was only ever held to that branch there. These drive the product decision on synthetic requests, one branch
// each, and hold the replica to it.
const int kSynWidth = 16;
const int kSynHeight = 16;
const int kSynDownscale = 4;

// Every pixel is one surface, neutral when rendered at (neutralT, neutralTint): B-R moves one unit per 100 K and the green
// axis one unit per 2 receipt tint units away from there. It has no neutral samples (balanceSamples 0), so the refinement
// walk acquires nothing on it.
LookAssistRenderBalanceFn syntheticRenderer( int neutralT, int neutralTint )
{
    return [neutralT, neutralTint]( double, int temperature, int tint, LookAssistRenderedPicture *picture ) -> bool
    {
        const int blueAmber = ( temperature - neutralT ) / 100;
        const int green = ( neutralTint - tint ) / 2;
        const int r = 150 - blueAmber / 2;
        const int b = r + blueAmber;
        const int g = ( r + b ) / 2 + green;
        picture->width = kSynWidth;
        picture->height = kSynHeight;
        picture->downscaleFactor = kSynDownscale;
        picture->stats = LookAssistStats();
        picture->stats.median = 128.0;
        picture->rgb.assign( static_cast<size_t>( kSynWidth ) * kSynHeight * 3, 0 );
        for( size_t i = 0; i < picture->rgb.size(); i += 3 )
        {
            picture->rgb[i] = static_cast<unsigned char>( std::max( 0, std::min( r, 255 ) ) );
            picture->rgb[i + 1] = static_cast<unsigned char>( std::max( 0, std::min( g, 255 ) ) );
            picture->rgb[i + 2] = static_cast<unsigned char>( std::max( 0, std::min( b, 255 ) ) );
        }
        return true;
    };
}

// Corroborated daylight (a bright recorded exposure that the rendered picture agrees with), as-shot 5600 K / 0 if asked.
LookAssistStats syntheticDaylightStats( bool hasAsShot )
{
    LookAssistStats stats;
    stats.median = 128.0;
    stats.hasSceneEv100 = true;
    stats.sceneEv100 = 14.0;
    stats.daylightPictureEvidence = true;
    lookAssistSetAsShotWhiteBalance( &stats, hasAsShot, 5600, 0 );
    return stats;
}

LookAssistAutoWhiteBalancePatch syntheticPatch( double luma, double chroma, double blueAmber )
{
    LookAssistAutoWhiteBalancePatch p;
    p.valid = true;
    p.thumbnailX = kSynWidth / 2;
    p.thumbnailY = kSynHeight / 2;
    p.rawX = p.thumbnailX * kSynDownscale + kSynDownscale / 2;
    p.rawY = p.thumbnailY * kSynDownscale + kSynDownscale / 2;
    p.luma = luma;
    p.chroma = chroma;
    p.blueAmberAxis = blueAmber;
    p.greenAxis = 0.0;
    return p;
}

DecisionInputs syntheticInputs( const LookAssistStats &stats, LookAssistScene scene, const LookAssistAutoWhiteBalancePatch &patch,
                                bool processed, int solveT, int solveTint, int neutralT )
{
    DecisionInputs in;
    in.stats = stats;
    in.scene = scene;
    in.patch = patch;
    in.solvedOnProcessedPicture = processed;
    in.rawW = kSynWidth * kSynDownscale;
    in.rawH = kSynHeight * kSynDownscale;
    in.analysisExposure = 120;
    in.refineWithoutPatch = true;
    in.render = syntheticRenderer( neutralT, 0 );
    in.solve = [solveT, solveTint]( int, int, int *temperature, int *tint )
    {
        *temperature = solveT;
        *tint = solveTint;
    };
    return in;
}

const LookAssistAutoWhiteBalancePatch kNeutralPatch = syntheticPatch( 150.0, 4.0, -4.0 );

// A fixture's premise: the product took the branch the fixture is about. Fails naming the branch.
void requireBranch( const char *fixture, const char *branch, bool taken, const ProductRun &run )
{
    std::printf( "WB_GATE fixture %s premise branch=%s taken=%d (product decision=%s source=%s checked=%d refused=%d "
                 "refusedAtBase=%d legacy=%d refineAttempted=%d renders=%d first_render=%d/%d)\n",
                 fixture, branch, taken ? 1 : 0, run.wb.decision.toUtf8().constData(), run.wb.source.toUtf8().constData(),
                 run.wb.initialPatchChecked ? 1 : 0, run.wb.initialPatchRefused ? 1 : 0, run.wb.initialPatchRefusedAtBase ? 1 : 0,
                 run.wb.legacyBalance ? 1 : 0, run.wb.refineAttempted ? 1 : 0, static_cast<int>( run.renders.size() ),
                 run.renders.empty() ? 0 : run.renders.front().first, run.renders.empty() ? 0 : run.renders.front().second );
    std::fflush( stdout );
    if( !taken )
        ::minitest::fail( __FILE__, __LINE__, std::string( "premise: the product takes branch " ) + branch,
                          std::string( "fixture " ) + fixture + " is about that branch and the product did not take it" );
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

// The control: the branch the tracked states take (undamped daylight, prior asked, verified).
TEST(LookAssistWbGateProbe, DecisionReplicaUndampedAcceptedFixture)
{
    const DecisionInputs in = syntheticInputs( syntheticDaylightStats( true ), LookAssistScene::Shade, kNeutralPatch, true,
                                               5600, 0, 5600 );
    const ProductRun run = runProduct( in );
    requireBranch( "undamped-accepted", "accepted (undamped, verified)",
                   run.wb.decision == QStringLiteral( "accepted" ) && run.wb.initialPatchChecked && !run.fallback, run );
    ASSERT_TRUE( replicaMatchesProduct( "fixture undamped-accepted", replicateDecision( in ), run ) );
}

// A daylight frame whose solve is damped (solved on the RAW thumbnail, not the processed picture): accepted-damped, and the
// product never asks initialDaylightPatchIsVerified (initialPatchChecked stays false, nothing is rendered).
TEST(LookAssistWbGateProbe, DecisionReplicaAcceptedDampedFixture)
{
    const DecisionInputs in = syntheticInputs( syntheticDaylightStats( true ), LookAssistScene::Shade,
                                               syntheticPatch( 150.0, 14.0, 10.0 ), false, 7500, 0, 5600 );
    const ProductRun run = runProduct( in );
    requireBranch( "accepted-damped", "accepted-damped (verification never called)",
                   run.wb.decision == QStringLiteral( "accepted-damped" ) && !run.wb.initialPatchChecked && run.renders.empty()
                       && !run.fallback,
                   run );
    ASSERT_TRUE( replicaMatchesProduct( "fixture accepted-damped", replicateDecision( in ), run ) );
}

// Daylight, an unstable solve (a blue-locus patch): rejected-unstable, then the refinement walk acquires nothing and the
// applier falls back naming no_verified_surface, never rejected-unstable.
TEST(LookAssistWbGateProbe, DecisionReplicaRejectedUnstableDaylightFixture)
{
    const DecisionInputs in = syntheticInputs( syntheticDaylightStats( true ), LookAssistScene::Shade,
                                               syntheticPatch( 150.0, 9.0, 25.0 ), true, 6500, 0, 5600 );
    const ProductRun run = runProduct( in );
    requireBranch( "rejected-unstable-daylight", "rejected-unstable, then a refinement walk that acquired nothing",
                   run.fallback && !run.wb.initialPatchChecked && !run.wb.initialPatchRefused && run.wb.refineAttempted
                       && run.wb.candidateTemperature == 6500,
                   run );
    ASSERT_TRUE( replicaMatchesProduct( "fixture rejected-unstable-daylight", replicateDecision( in ), run ) );
}

// Not daylight, an unstable solve: rejected-unstable is what the applied line logs (no fallback).
TEST(LookAssistWbGateProbe, DecisionReplicaRejectedUnstableOffDaylightFixture)
{
    const DecisionInputs in = syntheticInputs( LookAssistStats(), LookAssistScene::Night, syntheticPatch( 205.0, 12.0, 14.0 ),
                                               true, 4000, -35, 5600 );
    const ProductRun run = runProduct( in );
    requireBranch( "rejected-unstable-off-daylight", "rejected-unstable (not daylight, no fallback)",
                   run.wb.decision == QStringLiteral( "rejected-unstable" ) && !run.fallback, run );
    ASSERT_TRUE( replicaMatchesProduct( "fixture rejected-unstable-off-daylight", replicateDecision( in ), run ) );
}

// Daylight, no as-shot balance to ask: the verification returns before any render, so the logged base chroma is 0.0
// (the resolution's default), not a "not measured" sentinel.
TEST(LookAssistWbGateProbe, DecisionReplicaPriorUnavailableFixture)
{
    const DecisionInputs in = syntheticInputs( syntheticDaylightStats( false ), LookAssistScene::Shade, kNeutralPatch, true,
                                               5600, 0, 5600 );
    const ProductRun run = runProduct( in );
    requireBranch( "prior-unavailable", "rejected-unverified at the as-shot prior (none to ask, nothing rendered)",
                   run.wb.initialPatchChecked && run.wb.initialPatchRefused && !run.wb.initialPatchRefusedAtBase
                       && run.renders.empty() && run.fallback,
                   run );
    ASSERT_TRUE( replicaMatchesProduct( "fixture prior-unavailable", replicateDecision( in ), run ) );
}

// The request's control range is narrower than the daylight window: the candidate, the prior and the verification balance are
// clamped into the intersection (:1009-1010, :941-951), so the prior is asked at 6500 K, not at the as-shot 5600 K.
TEST(LookAssistWbGateProbe, DecisionReplicaClampToRequestRangeFixture)
{
    DecisionInputs in = syntheticInputs( syntheticDaylightStats( true ), LookAssistScene::Shade, kNeutralPatch, true, 6000, 0,
                                         6500 );
    in.minT = 6500;
    const ProductRun run = runProduct( in );
    const bool windowAloneAsksTheAsShot = lookAssistWhiteBalanceBounds( in.stats, in.scene ).minTemperature <= 5600;
    requireBranch( "clamp-to-request-range", "the request range binds the prior (asked at 6500, the window alone: 5600)",
                   windowAloneAsksTheAsShot && !run.renders.empty() && run.renders.front() == std::make_pair( 6500, 0 )
                       && run.wb.initialPatchChecked,
                   run );
    ASSERT_TRUE( replicaMatchesProduct( "fixture clamp-to-request-range", replicateDecision( in ), run ) );
}

// The request's control range lies outside the daylight window: the verification returns before the prior (:945).
TEST(LookAssistWbGateProbe, DecisionReplicaRequestRangeOutsideWindowFixture)
{
    DecisionInputs in = syntheticInputs( syntheticDaylightStats( true ), LookAssistScene::Shade, kNeutralPatch, true, 4300, 0,
                                         4300 );
    in.maxT = 4500;
    const ProductRun run = runProduct( in );
    const bool windowAndRangeDisjoint = lookAssistWhiteBalanceBounds( in.stats, in.scene ).minTemperature > in.maxT;
    requireBranch( "request-range-outside-window", "rejected-unverified at the empty window (nothing rendered)",
                   windowAndRangeDisjoint && run.wb.initialPatchChecked && run.wb.initialPatchRefused && run.renders.empty()
                       && run.fallback,
                   run );
    ASSERT_TRUE( replicaMatchesProduct( "fixture request-range-outside-window", replicateDecision( in ), run ) );
}
