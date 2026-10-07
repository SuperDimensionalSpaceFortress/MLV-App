// LOOK-ASSIST-M16-NEUTRAL-1: a MEASURE-ONLY probe of the fixture deck-cast debt (S1 / S2 of
// test_look_assist_fixture_scene.cpp: applied_cast <= 6.0, ratcheted to 9.9 / 9.8 after stripes stopped running on
// dual-ISO frames). It asserts no product behaviour. It answers one question with numbers: what is the lowest deck chroma
// ANY balance in the daylight window reaches on the de-striped fixtures (c_min), how far Look Assist's applied balance is
// from it, and whether the shared neutral-patch solver, aimed at the deck itself, neutralises the deck's render (c_ds).
//
// SKIPPED unless MLVAPP_LA_NEUTRAL_PROBE=1 (it renders a few hundred frames per state; CI cost stays zero). When it runs it
// prints one parseable "NEUTRAL_PROBE ..." line per measurement and asserts only its own premises:
//  - the deck chroma it measures at the applied balance equals S1's / S2's applied_cast for the same state to 1e-9
//    (it measures what S1 / S2 measure);
//  - c_min <= the applied value (the sweep includes the applied point).
//
// The helpers deckCastChroma / renderWith and the S1 / S2 setup are copied from test_look_assist_fixture_scene.cpp
// (not shared: that file is not edited by this card). deckLab is the probe's own a / b variant.
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

// The daylight window S1 / S2 assert the applied balance into (test_look_assist_fixture_scene.cpp).
const int kWindowMinTemperature = 4800;
const int kWindowMaxTemperature = 10000;
const int kWindowMinTint = -35;
const int kWindowMaxTint = 10;

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

// The deck region (rows 65-98 %, columns 2-25 %), as the probe uses it.
struct DeckRegion { int x0, y0, x1, y1; };

DeckRegion deckRegion( int width, int height )
{
    return { static_cast<int>( width * 0.02 ), static_cast<int>( height * 0.65 ),
             static_cast<int>( width * 0.25 ), static_cast<int>( height * 0.98 ) };
}

// CIELAB a, b and chroma of the deck's mean colour: the same maths as deckCastChroma, with a and b returned.
// Sign convention: +a magenta, -a green, +b yellow, -b blue.
struct Lab { double a = 0.0, b = 0.0, c = 1.0e9; };

Lab deckLab( const std::vector<uint8_t> &rgb, int width, int height )
{
    const DeckRegion r = deckRegion( width, height );
    double sum[3] = { 0.0, 0.0, 0.0 };
    int n = 0;
    for( int y = r.y0; y < r.y1; ++y )
        for( int x = r.x0; x < r.x1; ++x )
        {
            const size_t i = ( static_cast<size_t>( y ) * width + x ) * 3;
            for( int c = 0; c < 3; ++c ) sum[c] += rgb[i + c];
            ++n;
        }
    Lab out;
    if( n == 0 ) return out;
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
    out.a = 500.0 * ( f( X ) - f( Y ) );
    out.b = 200.0 * ( f( Y ) - f( Z ) );
    out.c = std::hypot( out.a, out.b );
    return out;
}

// The sweep's render: the same white balance / exposure writes as renderWith, without dropping the single cached debayered
// frame (white balance and exposure are applied after the debayer), processed on kSweepThreads threads. Proven
// byte-identical to renderWith (one thread, the debayer redone) at the applied balance and at the base balance before the
// sweep uses it; if it is not, the sweep uses renderWith.
const int kSweepThreads = 4;

std::vector<uint8_t> renderKeepingTheDebayer( MlvPipelineFixture &fixture, int frame, int temperature, int tint, int exposure )
{
    processingSetWhiteBalance( fixture.processing(), temperature, tint / 10.0 );
    processingSetExposureStops( fixture.processing(), exposure / 100.0 );
    return fixture.renderFrame8( static_cast<uint64_t>( frame ), kSweepThreads );
}

// MLVAPP_LA_NEUTRAL_PROBE_STATES (comma list of state tags, e.g. "tiny-f0-S1,large-f5-S1") runs a subset; unset = all.
bool stateSelected( const char *tag )
{
    const QByteArray states = qgetenv( "MLVAPP_LA_NEUTRAL_PROBE_STATES" );
    return states.isEmpty() || states.split( ',' ).contains( QByteArray( tag ) );
}

QByteArray readLog( const QString &path )
{
    QFile log_file( path );
    if( !log_file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return QByteArray();
    return log_file.readAll();
}

// The last line of the log containing the marker (empty if none).
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

// The headless applier's analysis-thumbnail patch search, replicated on the fixture as it stands (ReceiptApplier.cpp
// applyHeadlessLookAssist: raw thumbnail stats -> scene -> the processed picture at the scene's own lift, rendered at the
// balance the processing object held on entry -> findLookAssistAutoWhiteBalancePatch). The entry balance is put back
// first. Cross-checked against the applied line's patchLuma / patchChroma / patchBlueAmber / patchGreenAxis.
struct PatchReplica
{
    LookAssistAutoWhiteBalancePatch patch;
    LookAssistScene scene = LookAssistScene::Night;
    bool processed = false;
    int colorDownscale = 0;
    double stops = 0.0;
};

PatchReplica replicateHeadlessPatch( MlvPipelineFixture &fixture, int frame, double entryKelvin, double entryRenderTint )
{
    mlvObject_t *video = fixture.video();
    processingObject_t *processing = fixture.processing();
    processing->wb_tint = entryRenderTint;
    processingSetWhiteBalance( processing, entryKelvin, entryRenderTint );
    resetMlvCache( video );
    resetMlvCachedFrame( video );

    PatchReplica out;
    const int rawW = video->RAWI.xRes;
    const int rawH = video->RAWI.yRes;
    int downscale = 8;
    if( rawW > 4000 || rawH > 2500 ) downscale = 12;
    else if( rawW > 2800 || rawH > 1900 ) downscale = 10;
    else if( rawW > 1800 || rawH > 1200 ) downscale = 8;
    else downscale = 6;
    const int w = rawW / downscale;
    const int h = rawH / downscale;
    std::vector<unsigned char> thumbnail( static_cast<size_t>( w ) * h * 3 );
    get_area_average_downscale_raw_thumnail( video, frame, downscale, thumbnail.data() );
    LookAssistStats stats = analyzeLookAssistThumbnail( thumbnail.data(), w, h );
    lookAssistSetSceneEv100( &stats, video->EXPO.isoValue, static_cast<double>( video->EXPO.shutterValue ),
                             video->LENS.aperture, ReceiptApplier::lookAssistRecoveryIso( video ) );
    int asShotTemperature = 6000;
    int asShotTint = 0;
    const bool hasAsShot = ReceiptApplier::asShotWhiteBalanceControls( video, &asShotTemperature, &asShotTint );
    lookAssistSetAsShotWhiteBalance( &stats, hasAsShot, asShotTemperature, asShotTint );

    const int colorDownscale = lookAssistIsFlatFloorRawThumbnail( stats ) ? std::max( 3, downscale / 3 ) : downscale;
    const int cw = rawW / colorDownscale;
    const int ch = rawH / colorDownscale;
    std::vector<unsigned char> processed( static_cast<size_t>( cw ) * ch * 3 );
    auto renderProcessed = [&]( double stops, LookAssistStats *s ) -> bool
    {
        if( !ReceiptApplier::processedThumbnailAtExposure( video, frame, colorDownscale, 1, stops, processed.data() ) )
            return false;
        *s = analyzeLookAssistThumbnail( processed.data(), cw, ch );
        return true;
    };
    out.scene = resolveLookAssistScene( &stats, renderProcessed );
    out.colorDownscale = colorDownscale;
    bool useProcessed = false;
    if( lookAssistShouldAnalyzeProcessedColor( out.scene, stats ) )
    {
        bool rendered = false;
        if( out.scene != LookAssistScene::Night )
        {
            out.stops = presetForLookAssistScene( out.scene, stats ).exposure / 100.0;
            rendered = ReceiptApplier::processedThumbnailAtExposure( video, frame, colorDownscale, 1, out.stops,
                                                                     processed.data() );
        }
        if( !rendered ) get_area_average_downscale_thumnail( video, frame, colorDownscale, 1, processed.data() );
        const LookAssistStats colorStats = analyzeLookAssistThumbnail( processed.data(), cw, ch );
        useProcessed = colorStats.median > 0.0 && colorStats.balanceSamples >= std::max( 32, ( cw * ch ) / 100 );
    }
    out.processed = useProcessed;
    out.patch = useProcessed
        ? findLookAssistAutoWhiteBalancePatch( processed.data(), cw, ch, colorDownscale, rawW, rawH )
        : findLookAssistAutoWhiteBalancePatch( thumbnail.data(), w, h, downscale, rawW, rawH );
    return out;
}

void printPatch( const char *tag, const PatchReplica &r, const QString &appliedLine, int renderWidth, int renderHeight )
{
    const DeckRegion d = deckRegion( renderWidth, renderHeight );
    const LookAssistAutoWhiteBalancePatch &p = r.patch;
    const bool onDeck = p.valid && p.rawX >= d.x0 && p.rawX < d.x1 && p.rawY >= d.y0 && p.rawY < d.y1;
    QString matches = QStringLiteral( "NA" );
    if( !appliedLine.isEmpty() )
    {
        const bool same = field( appliedLine, "patchValid" ) == ( p.valid ? QStringLiteral( "true" ) : QStringLiteral( "false" ) )
            && field( appliedLine, "patchLuma" ) == QString::number( p.luma, 'f', 1 )
            && field( appliedLine, "patchChroma" ) == QString::number( p.chroma, 'f', 1 )
            && field( appliedLine, "patchBlueAmber" ) == QString::number( p.blueAmberAxis, 'f', 1 )
            && field( appliedLine, "patchGreenAxis" ) == QString::number( p.greenAxis, 'f', 1 );
        matches = same ? QStringLiteral( "1" ) : QStringLiteral( "0" );
    }
    std::printf( "NEUTRAL_PROBE %s patch valid=%d scene=%s processed=%d colorDownscale=%d stops=%.2f thumbnailX=%d thumbnailY=%d "
                 "rawX=%d rawY=%d luma=%.1f chroma=%.1f blueAmber=%.1f green=%.1f on_deck=%d deck=%d,%d-%d,%d "
                 "log_patch=%s/%s/%s/%s/%s matches_log=%s\n",
                 tag, p.valid ? 1 : 0, lookAssistSceneName( r.scene ).toUtf8().constData(), r.processed ? 1 : 0,
                 r.colorDownscale, r.stops, p.thumbnailX, p.thumbnailY, p.rawX, p.rawY, p.luma, p.chroma, p.blueAmberAxis,
                 p.greenAxis, onDeck ? 1 : 0, d.x0, d.y0, d.x1, d.y1,
                 field( appliedLine, "patchValid" ).toUtf8().constData(), field( appliedLine, "patchLuma" ).toUtf8().constData(),
                 field( appliedLine, "patchChroma" ).toUtf8().constData(),
                 field( appliedLine, "patchBlueAmber" ).toUtf8().constData(),
                 field( appliedLine, "patchGreenAxis" ).toUtf8().constData(), matches.toUtf8().constData() );
    std::fflush( stdout );
}

// One fixture state: S1 (receipt-less, the app's default balance on entry) or S2 (the stale 3000 K entry balance).
enum class Scenario { S1, S2 };

struct Probe
{
    MlvPipelineFixture fixture;
    QByteArray log;
    QString appliedLine;
    double entryKelvin = 0.0;
    double entryRenderTint = 0.0;
    bool ok = false;
};

// S1 / S2's exact setup and Look Assist run (test_look_assist_fixture_scene.cpp).
void runScenario( Probe *p, const char *clip, Scenario scenario, int frame )
{
    QString error_message;
    if( !p->fixture.openClipFile( repo_file_path( QString::fromLatin1( clip ) ), &error_message ) ) return;
    if( !p->fixture.applyReceipt( &error_message ) ) return;
    ReceiptSettings &receipt = p->fixture.receipt();
    receipt.setLookAssistEnabled( true );
    receipt.setLookAssistBaselineValid( false );
    receipt.setExposure( 0 );
    receipt.setTemperature( -1 );
    receipt.setTint( 0 );
    if( scenario == Scenario::S2 ) processingSetWhiteBalance( p->fixture.processing(), 3000, 0.0 );
    p->entryKelvin = processingGetWhiteBalanceKelvin( p->fixture.processing() );
    p->entryRenderTint = processingGetWhiteBalanceTint( p->fixture.processing() );

    QTemporaryDir temporary_dir;
    const QString log_path = temporary_dir.filePath( QStringLiteral( "look_assist.log" ) );
    BatchLogger::init( log_path );
    const bool applied = ReceiptApplier::applyHeadlessLookAssist(
        &receipt, p->fixture.video(), p->fixture.processing(), static_cast<uint32_t>( frame ) );
    BatchLogger::shutdown();
    p->log = readLog( log_path );
    p->appliedLine = lastLine( p->log, "LOOK_ASSIST applied" );
    p->ok = applied && !p->log.isEmpty();
}

void printLab( const char *tag, const char *what, int temperature, int tint, int exposure, const Lab &lab )
{
    std::printf( "NEUTRAL_PROBE %s %s T=%d tint=%d exp=%d a=%.4f b=%.4f c=%.6f\n", tag, what, temperature, tint, exposure,
                 lab.a, lab.b, lab.c );
    std::fflush( stdout );
}

struct SweepResult { int temperature = 0; int tint = 0; Lab lab; int renders = 0; };

// The minimum deck chroma over a grid; `seed` (if its chroma is finite) is included as a candidate.
template <typename Render>
SweepResult sweepGrid( Render &&render, int tMin, int tMax, int tStep, int tintMin, int tintMax, int tintStep,
                       const SweepResult &seed )
{
    SweepResult best = seed;
    for( int t = tMin; t <= tMax; t += tStep )
        for( int ti = tintMin; ti <= tintMax; ti += tintStep )
        {
            const Lab lab = render( t, ti );
            ++best.renders;
            if( lab.c < best.lab.c ) { best.temperature = t; best.tint = ti; best.lab = lab; }
        }
    return best;
}

void probeState( const char *tag, const char *clip, Scenario scenario )
{
    const int frame = 0;
    Probe p;
    runScenario( &p, clip, scenario, frame );
    ASSERT_TRUE( p.ok );
    MlvPipelineFixture &fixture = p.fixture;
    ReceiptSettings &receipt = fixture.receipt();
    const int width = fixture.width();
    const int height = fixture.height();
    const int exposure = receipt.exposure();
    const int appliedT = receipt.temperature();
    const int appliedTint = receipt.tint();
    std::printf( "NEUTRAL_PROBE %s state clip=%s scenario=%s frame=%d entry=%.3f/%.9f applied T=%d tint=%d exp=%d "
                 "source=%s decision=%s scene=%s chromaSmoothAuto=%s refineRenders=%s render=%dx%d raw=%dx%d\n",
                 tag, clip, scenario == Scenario::S1 ? "S1" : "S2", frame, p.entryKelvin, p.entryRenderTint, appliedT,
                 appliedTint, exposure, field( p.appliedLine, "autoWbSource" ).toUtf8().constData(),
                 field( p.appliedLine, "autoWbDecision" ).toUtf8().constData(),
                 field( p.appliedLine, "scene" ).toUtf8().constData(),
                 field( p.appliedLine, "chromaSmoothAuto" ).toUtf8().constData(),
                 field( p.appliedLine, "refineRenders" ).toUtf8().constData(), width, height, fixture.video()->RAWI.xRes,
                 fixture.video()->RAWI.yRes );
    std::fflush( stdout );

    // S1 / S2's own measurement, in their order (S1: base, master, applied; S2: applied, prior, master).
    double appliedCast = 0.0;
    if( scenario == Scenario::S1 )
    {
        const double baseCast = deckCastChroma( renderWith( fixture, frame, 6000, 0, exposure ), width, height );
        const double masterCast = deckCastChroma( renderWith( fixture, frame, 8594, -23, exposure ), width, height );
        appliedCast = deckCastChroma( renderWith( fixture, frame, appliedT, appliedTint, exposure ), width, height );
        std::printf( "NEUTRAL_PROBE %s s_own applied_cast=%.9f base_cast=%.6f master_cast=%.6f bound=9.9\n", tag, appliedCast,
                     baseCast, masterCast );
    }
    else
    {
        appliedCast = deckCastChroma( renderWith( fixture, frame, appliedT, appliedTint, exposure ), width, height );
        const double priorCast = deckCastChroma( renderWith( fixture, frame, 6000, 0, exposure ), width, height );
        const double masterCast = deckCastChroma( renderWith( fixture, frame, 6480, -19, 174 ), width, height );
        std::printf( "NEUTRAL_PROBE %s s_own applied_cast=%.9f prior_cast=%.6f master_cast=%.6f bound=9.8\n", tag, appliedCast,
                     priorCast, masterCast );
    }
    std::fflush( stdout );

    // The probe's own measurement at the applied balance, then the fast render's equality with renderWith.
    const std::vector<uint8_t> appliedFull = renderWith( fixture, frame, appliedT, appliedTint, exposure );
    const Lab applied = deckLab( appliedFull, width, height );
    printLab( tag, "applied", appliedT, appliedTint, exposure, applied );
    // PREMISE: the probe measures what S1 / S2 measure.
    std::printf( "NEUTRAL_PROBE %s premise probe_vs_s_own=%.3e\n", tag, std::fabs( applied.c - appliedCast ) );
    std::fflush( stdout );
    ASSERT_TRUE( std::fabs( applied.c - appliedCast ) <= 1e-9 );

    const std::vector<uint8_t> appliedFast = renderKeepingTheDebayer( fixture, frame, appliedT, appliedTint, exposure );
    const std::vector<uint8_t> baseFast = renderKeepingTheDebayer( fixture, frame, 6000, 0, exposure );
    const std::vector<uint8_t> baseFull = renderWith( fixture, frame, 6000, 0, exposure );
    const bool fastIsExact = appliedFast == appliedFull && baseFast == baseFull;
    std::printf( "NEUTRAL_PROBE %s fast_render_exact=%d\n", tag, fastIsExact ? 1 : 0 );
    std::fflush( stdout );
    auto deckAt = [&]( int t, int ti ) -> Lab
    {
        return deckLab( fastIsExact ? renderKeepingTheDebayer( fixture, frame, t, ti, exposure )
                                    : renderWith( fixture, frame, t, ti, exposure ), width, height );
    };

    printLab( tag, "stripes_era", 6540, -35, exposure, deckAt( 6540, -35 ) );
    printLab( tag, "base", 6000, 0, exposure, deckAt( 6000, 0 ) );

    // The sweep inside the daylight window, at the applied exposure: coarse (T step 200, tint step 5), then fine around the
    // coarse minimum (T +-200 step 25, tint +-5 step 1). The applied point is a candidate.
    SweepResult seed;
    seed.temperature = appliedT;
    seed.tint = appliedTint;
    if( appliedT >= kWindowMinTemperature && appliedT <= kWindowMaxTemperature
     && appliedTint >= kWindowMinTint && appliedTint <= kWindowMaxTint )
        seed.lab = applied;
    const SweepResult coarse = sweepGrid( deckAt, kWindowMinTemperature, kWindowMaxTemperature, 200,
                                          kWindowMinTint, kWindowMaxTint, 5, seed );
    const SweepResult fine = sweepGrid( deckAt,
                                        std::max( kWindowMinTemperature, coarse.temperature - 200 ),
                                        std::min( kWindowMaxTemperature, coarse.temperature + 200 ), 25,
                                        std::max( kWindowMinTint, coarse.tint - 5 ),
                                        std::min( kWindowMaxTint, coarse.tint + 5 ), 1, coarse );
    std::printf( "NEUTRAL_PROBE %s sweep coarse_min T=%d tint=%d c=%.6f renders=%d\n", tag, coarse.temperature, coarse.tint,
                 coarse.lab.c, coarse.renders );
    std::printf( "NEUTRAL_PROBE %s sweep c_min=%.6f T*=%d tint*=%d a=%.4f b=%.4f renders=%d applied=%.6f applied_minus_c_min=%.6f\n",
                 tag, fine.lab.c, fine.temperature, fine.tint, fine.lab.a, fine.lab.b, fine.renders, applied.c,
                 applied.c - fine.lab.c );
    std::fflush( stdout );
    // PREMISE: the sweep includes the applied point.
    ASSERT_TRUE( fine.lab.c <= applied.c );

    // For information only: a coarse unconstrained minimum (T 2300..9500 step 800 plus 10000, tint -100..100 step 20).
    SweepResult none;
    SweepResult wide = sweepGrid( deckAt, 2300, 9500, 800, -100, 100, 20, none );
    wide = sweepGrid( deckAt, 10000, 10000, 1, -100, 100, 20, wide );
    std::printf( "NEUTRAL_PROBE %s sweep unconstrained_coarse_min T=%d tint=%d a=%.4f b=%.4f c=%.6f renders=%d\n", tag,
                 wide.temperature, wide.tint, wide.lab.a, wide.lab.b, wide.lab.c, wide.renders );
    std::fflush( stdout );

    // The deck-solved balance: the shared solver (the one Look Assist calls) aimed at the centre of the deck region,
    // with the live object at the applied balance and exposure; then the deck rendered there (c_ds).
    const DeckRegion d = deckRegion( width, height );
    const int cx = ( d.x0 + d.x1 ) / 2;
    const int cy = ( d.y0 + d.y1 ) / 2;
    renderWith( fixture, frame, appliedT, appliedTint, exposure );
    int dsT = appliedT;
    int dsTint = appliedTint;
    findMlvWhiteBalanceAtAnalysisLevels( fixture.video(), static_cast<uint64_t>( frame ), cx, cy, &dsT, &dsTint, 0 );
    const Lab ds = deckLab( renderWith( fixture, frame, dsT, dsTint, exposure ), width, height );
    std::printf( "NEUTRAL_PROBE %s deck_solved at=%d,%d T=%d tint=%d a=%.4f b=%.4f c_ds=%.6f c_ds_minus_c_min=%.6f\n", tag, cx,
                 cy, dsT, dsTint, ds.a, ds.b, ds.c, ds.c - fine.lab.c );
    std::fflush( stdout );

    // The patch Look Assist's search picks on this state (its analysis thumbnail, replicated), last: it moves the balance.
    const PatchReplica replica = replicateHeadlessPatch( fixture, frame, p.entryKelvin, p.entryRenderTint );
    printPatch( tag, replica, p.appliedLine, width, height );
}

// LOOK-ASSIST-LARGE-F5-PATCH-UNVERIFIED-1's frames: data only, not graded here.
void probeFallbackFrame( int frame )
{
    char tag[32];
    std::snprintf( tag, sizeof( tag ), "large-f%d-S1", frame );
    Probe p;
    runScenario( &p, kLargeClip, Scenario::S1, frame );
    ASSERT_TRUE( p.ok );
    const QString fallback = lastLine( p.log, "daylight_fallback_to_master" );
    std::printf( "NEUTRAL_PROBE %s fallback present=%d reason=%s refusedAtBase=%s initialPatchBaseChroma=%s "
                 "initialPatchFinalChroma=%s applied T=%d tint=%d exp=%d\n",
                 tag, fallback.isEmpty() ? 0 : 1, field( fallback, "reason" ).toUtf8().constData(),
                 field( fallback, "refusedAtBase" ).toUtf8().constData(),
                 field( fallback, "initialPatchBaseChroma" ).toUtf8().constData(),
                 field( fallback, "initialPatchFinalChroma" ).toUtf8().constData(), p.fixture.receipt().temperature(),
                 p.fixture.receipt().tint(), p.fixture.receipt().exposure() );
    std::fflush( stdout );
    // The daylight pass searched its patch with chroma smoothing switched on (the fallback switched it back); so does this.
    llrpSetChromaSmoothMode( p.fixture.video(), 1 );
    llrpResetFpmStatus( p.fixture.video() );
    llrpResetBpmStatus( p.fixture.video() );
    const PatchReplica replica = replicateHeadlessPatch( p.fixture, frame, p.entryKelvin, p.entryRenderTint );
    // No applied line belongs to the daylight pass on a fallback frame, so there is nothing to cross-check against.
    printPatch( tag, replica, QString(), p.fixture.width(), p.fixture.height() );
}

} // namespace

TEST(LookAssistFixtureNeutralProbe, DeckBalanceSweep)
{
    if( qgetenv( "MLVAPP_LA_NEUTRAL_PROBE" ) != "1" )
        SKIP_TEST( "measure-only probe (LOOK-ASSIST-M16-NEUTRAL-1); set MLVAPP_LA_NEUTRAL_PROBE=1 to run it" );
    if( stateSelected( "tiny-f0-S1" ) ) probeState( "tiny-f0-S1", kTinyClip, Scenario::S1 );
    if( stateSelected( "large-f0-S1" ) ) probeState( "large-f0-S1", kLargeClip, Scenario::S1 );
    if( stateSelected( "tiny-f0-S2" ) ) probeState( "tiny-f0-S2", kTinyClip, Scenario::S2 );
    if( stateSelected( "large-f0-S2" ) ) probeState( "large-f0-S2", kLargeClip, Scenario::S2 );
    if( stateSelected( "large-f5-S1" ) ) probeFallbackFrame( 5 );
    if( stateSelected( "large-f10-S1" ) ) probeFallbackFrame( 10 );
    if( stateSelected( "large-f15-S1" ) ) probeFallbackFrame( 15 );
}
