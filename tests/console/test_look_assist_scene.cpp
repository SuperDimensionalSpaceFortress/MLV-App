// LOOK-ASSIST-SCENE-CLASSIFY-1: Look Assist scene classification and white balance.
//
// The tracked daylight fixture (tests/fixtures/clips/*_dual_iso.mlv: ISO 100, 1/2150 s, f/5.6 =
// EV100 16) used to be classified NIGHT: its RAW thumbnail is a flat floor (median 37, p05 35,
// p95 37 -- the sensor black offset), and display statistics cannot tell an under-exposed day
// from a night. The recorded exposure can. The solved white balance (8594 K, tint -23) was also
// accepted although it left the daylight locus.
//
// These tests pin: (1) EV100 from the clip metadata, (2) the daylight fixture is NOT night while
// the same statistics WITHOUT metadata still read exactly as before, (3) synthetic day / night /
// tungsten / mixed frames, (4) white balance stays neutral on a neutral patch and inside the
// daylight window, (5) clips that were already classified correctly are bit-for-bit unchanged
// (sweep against a verbatim copy of the previous classifier), and (6) CPU and CUDA cannot
// diverge: the headless CPU applier and the GUI (which drives the CUDA/GL display path too) call
// the ONE shared implementation and define no classifier of their own.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../src/batch/LookAssistAnalysis.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QTextStream>

#include <cmath>
#include <vector>

using namespace lookassist;

namespace
{

// Measured from the tracked fixtures through the same thumbnail path the app uses.
LookAssistStats fixtureRawStats()
{
    LookAssistStats s;
    s.median = 37; s.p05 = 35; s.p95 = 37; s.p99 = 38;
    s.dynamicRange = s.p95 - s.p05;
    s.clipLow = 0.0; s.clipHigh = 0.0;
    s.medianR = 33; s.medianG = 38; s.medianB = 38;
    s.balanceR = 33; s.balanceG = 38; s.balanceB = 38;
    s.balanceSamples = 40680;
    return s;
}

// Verbatim copy of the classifier before this card -- the reference for "no regression".
LookAssistScene legacyClassify( const LookAssistStats &stats )
{
    if( stats.p95 >= 220.0 || stats.clipHigh > 0.015 )
        return LookAssistScene::BrightSun;
    if( stats.median < 60.0 )
    {
        if( stats.clipHigh > 0.006 || stats.p99 >= 236.0 || stats.p95 >= 185.0 )
            return LookAssistScene::ArtificialLights;
        return LookAssistScene::Night;
    }
    return LookAssistScene::Shade;
}

LookAssistStats withEv( LookAssistStats s, double iso, double shutterUs, double apertureX100 )
{
    lookAssistSetSceneEv100( &s, iso, shutterUs, apertureX100 );
    return s;
}

// Frame of w*h pixels filled by fn(x, y) -> {r,g,b}.
template<class F>
LookAssistStats analyzeFrame( int w, int h, F fn )
{
    std::vector<unsigned char> rgb( static_cast<size_t>( w ) * h * 3 );
    for( int y = 0; y < h; ++y )
        for( int x = 0; x < w; ++x )
        {
            const auto px = fn( x, y );
            unsigned char *p = &rgb[( static_cast<size_t>( y ) * w + x ) * 3];
            p[0] = px[0]; p[1] = px[1]; p[2] = px[2];
        }
    return analyzeLookAssistThumbnail( rgb.data(), w, h );
}

QString readRepoFile( const QString &relativePath )
{
    const QString path = repo_file_path( relativePath );
    QFile file( path );
    if( path.isEmpty() || !file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return QString();
    QTextStream stream( &file );
    return stream.readAll();
}

} // namespace

TEST(LookAssistScene, Ev100FromClipMetadata)
{
    double ev = 0.0;
    // The tracked fixtures: ISO 100, 465 us, f/5.6.
    ASSERT_TRUE( lookAssistSceneEv100( 100, 465, 560, &ev ) );
    ASSERT_NEAR( 16.0, ev, 0.1 );
    // Sunny 16: ISO 100, 1/100 s, f/16 -> EV100 ~14.6 (daylight); ISO 1600 reduces it by 4.
    ASSERT_TRUE( lookAssistSceneEv100( 100, 10000, 1600, &ev ) );
    ASSERT_NEAR( 14.64, ev, 0.05 );
    ASSERT_TRUE( lookAssistSceneEv100( 1600, 10000, 1600, &ev ) );
    ASSERT_NEAR( 10.64, ev, 0.05 );
    // A dim interior: ISO 3200, 1/50 s, f/2.8 -> log2(2.8^2 * 50) - 5 = 3.6.
    ASSERT_TRUE( lookAssistSceneEv100( 3200, 20000, 280, &ev ) );
    ASSERT_NEAR( 3.6, ev, 0.1 );
    // Missing or zero metadata is "unknown", never a guess.
    ASSERT_FALSE( lookAssistSceneEv100( 0, 465, 560, &ev ) );
    ASSERT_FALSE( lookAssistSceneEv100( 100, 0, 560, &ev ) );
    ASSERT_FALSE( lookAssistSceneEv100( 100, 465, 0, &ev ) );
    ASSERT_FALSE( lookAssistSceneEv100( 100, 465, 560, nullptr ) );
    LookAssistStats s;
    lookAssistSetSceneEv100( &s, 0, 0, 0 );
    ASSERT_FALSE( s.hasSceneEv100 );
}

TEST(LookAssistScene, DaylightFixtureIsNotNight)
{
    const LookAssistStats fixture = withEv( fixtureRawStats(), 100, 465, 560 );
    ASSERT_TRUE( fixture.hasSceneEv100 );
    ASSERT_TRUE( lookAssistSceneIsDaylightByMetadata( fixture ) );
    const LookAssistScene scene = classifyLookAssistScene( fixture );
    ASSERT_TRUE( scene != LookAssistScene::Night );
    ASSERT_TRUE( scene != LookAssistScene::ArtificialLights );
    ASSERT_TRUE( scene == LookAssistScene::Shade );   // dim picture, daylight exposure: day, under-exposed

    // Same display statistics with NO metadata read exactly as before (the legacy behaviour is
    // kept for clips that carry no exposure block).
    ASSERT_TRUE( classifyLookAssistScene( fixtureRawStats() ) == LookAssistScene::Night );

    // The preset for the daylight class lifts the picture but is not the night rescue.
    const LookAssistPreset day = presetForLookAssistScene( scene, fixture );
    const LookAssistPreset night = presetForLookAssistScene( LookAssistScene::Night, fixtureRawStats() );
    ASSERT_TRUE( day.exposure > 0 );
    ASSERT_TRUE( day.exposure <= 180 );
    ASSERT_TRUE( day.shadows < night.shadows );
}

TEST(LookAssistScene, SyntheticDayNightTungstenMixed)
{
    // Open-shade daylight: mid-grey scene, bright sky, EV100 12.5 (ISO 100, 1/250, f/4 ~ 12).
    const LookAssistStats day = withEv( analyzeFrame( 64, 64, []( int x, int ) {
        return std::vector<int>{ 110 + x / 4, 120 + x / 4, 135 + x / 4 }; } ), 100, 4000, 400 );
    ASSERT_TRUE( classifyLookAssistScene( day ) == LookAssistScene::Shade );

    // Hard sun: clipped highlights stay BrightSun, with or without metadata.
    const LookAssistStats sun = withEv( analyzeFrame( 64, 64, []( int x, int y ) {
        return ( x < 40 ) ? std::vector<int>{ 250, 250, 250 } : std::vector<int>{ 90, 100, 110 }; } ), 100, 500, 800 );
    ASSERT_TRUE( classifyLookAssistScene( sun ) == LookAssistScene::BrightSun );

    // True night: dark picture AND dark recorded exposure (ISO 3200, 1/30, f/1.8 -> EV100 ~ 0).
    const LookAssistStats dark = analyzeFrame( 64, 64, []( int x, int y ) {
        return std::vector<int>{ 12 + ( x + y ) % 5, 14 + ( x + y ) % 5, 18 + ( x + y ) % 5 }; } );
    ASSERT_TRUE( classifyLookAssistScene( dark ) == LookAssistScene::Night );
    ASSERT_TRUE( classifyLookAssistScene( withEv( dark, 3200, 33333, 180 ) ) == LookAssistScene::Night );

    // Tungsten / indoor: dim room, small bright lamp, warm; EV100 ~ 6 (ISO 1600, 1/50, f/1.8).
    const LookAssistStats tungsten = withEv( analyzeFrame( 64, 64, []( int x, int y ) {
        return ( x < 10 && y < 10 ) ? std::vector<int>{ 255, 240, 200 }
                                  : std::vector<int>{ 52 + ( x % 7 ), 38 + ( y % 5 ), 22 }; } ), 1600, 20000, 180 );
    ASSERT_TRUE( classifyLookAssistScene( tungsten ) == LookAssistScene::ArtificialLights );
    // ... and the white balance window is NOT narrowed for it: indoor light may be 2800 K.
    const LookAssistWhiteBalanceBounds indoor = lookAssistWhiteBalanceBounds( tungsten, LookAssistScene::ArtificialLights );
    ASSERT_EQ( 2000, indoor.minTemperature );
    ASSERT_EQ( 10000, indoor.maxTemperature );
    ASSERT_EQ( -100, indoor.minTint );
    ASSERT_EQ( 100, indoor.maxTint );

    // Mixed: half dark interior, half bright window, daylight exposure -> day, not night.
    const LookAssistStats mixed = withEv( analyzeFrame( 64, 64, []( int x, int ) {
        return ( x < 32 ) ? std::vector<int>{ 20, 20, 24 } : std::vector<int>{ 200, 205, 215 }; } ), 100, 2000, 560 );
    ASSERT_TRUE( classifyLookAssistScene( mixed ) != LookAssistScene::Night );
}

TEST(LookAssistScene, WhiteBalanceStaysNeutralAndInsideTheDaylightWindow)
{
    // A neutral grey patch (R=G=B) must request no correction at all.
    const LookAssistStats neutral = withEv( analyzeFrame( 64, 64, []( int, int ) {
        return std::vector<int>{ 118, 118, 118 }; } ), 100, 4000, 400 );
    const LookAssistPreset flat = presetForLookAssistScene( classifyLookAssistScene( neutral ), neutral );
    ASSERT_EQ( 0, flat.temperatureDelta );
    ASSERT_EQ( 0, flat.tintDelta );

    // A warm cast on the grey patch (R > B) is cooled; a blue cast is warmed; both are bounded.
    const LookAssistStats warm = withEv( analyzeFrame( 64, 64, []( int, int ) {
        return std::vector<int>{ 128, 118, 108 }; } ), 100, 4000, 400 );
    ASSERT_TRUE( presetForLookAssistScene( classifyLookAssistScene( warm ), warm ).temperatureDelta < 0 );
    const LookAssistStats cool = withEv( analyzeFrame( 64, 64, []( int, int ) {
        return std::vector<int>{ 108, 118, 128 }; } ), 100, 4000, 400 );
    ASSERT_TRUE( presetForLookAssistScene( classifyLookAssistScene( cool ), cool ).temperatureDelta > 0 );

    // The daylight window (clamp, never reject): a daylight clip cannot be tungsten (<4800 K) nor
    // carry a strong magenta tint (>+10); the warm end is the slider's own 10000 K because open
    // shade is legitimately 7500-10000 K. The window is MEASURED, not guessed: the fixture's neutral
    // deck solves at 9990 K / tint -35 (Lab chroma 4.8 rendered); the earlier 7500 K / tint -10
    // ceiling left it at chroma 17.3 and the 8594 K / -23 damped solve at 11.5 (lavender).
    const LookAssistStats fixture = withEv( fixtureRawStats(), 100, 465, 560 );
    const LookAssistWhiteBalanceBounds day = lookAssistWhiteBalanceBounds( fixture, LookAssistScene::Shade );
    ASSERT_EQ( 4800, day.minTemperature );
    ASSERT_EQ( 10000, day.maxTemperature );
    ASSERT_EQ( -35, day.minTint );
    ASSERT_EQ( 10, day.maxTint );
    int temperature = 9990, tint = -35;   // the fixture's solved neutral deck: left alone
    lookAssistClampWhiteBalance( day, &temperature, &tint );
    ASSERT_EQ( 9990, temperature );
    ASSERT_EQ( -35, tint );
    temperature = 3000; tint = 30;        // tungsten-and-magenta in a daylight scene: clamped, not rejected
    lookAssistClampWhiteBalance( day, &temperature, &tint );
    ASSERT_EQ( 4800, temperature );
    ASSERT_EQ( 10, tint );
    temperature = 6150; tint = -4;
    lookAssistClampWhiteBalance( day, &temperature, &tint );
    ASSERT_EQ( 6150, temperature );
    ASSERT_EQ( -4, tint );
    const LookAssistWhiteBalanceBounds unknown = lookAssistWhiteBalanceBounds( fixtureRawStats(), LookAssistScene::Night );
    temperature = 8594; tint = -23;
    lookAssistClampWhiteBalance( unknown, &temperature, &tint );
    ASSERT_EQ( 8594, temperature );
    ASSERT_EQ( -23, tint );
}

TEST(LookAssistScene, ProcessedColourIsAnalysedForAnyFlatFloorThumbnailNotJustNight)
{
    // The defect: colour was read from the rendered picture only when the scene was NIGHT, so the day
    // the classifier stopped calling the daylight fixture night, no white balance ran on rendered
    // pixels and the base 6000 K stood (deck chroma 11.5 -> 22.4).
    const LookAssistStats flatDay = withEv( fixtureRawStats(), 100, 465, 560 );
    ASSERT_TRUE( lookAssistIsFlatFloorRawThumbnail( flatDay ) );
    const LookAssistScene day = classifyLookAssistScene( flatDay );
    ASSERT_TRUE( day == LookAssistScene::Shade );
    ASSERT_TRUE( lookAssistShouldAnalyzeProcessedColor( day, flatDay ) );
    // The night-only rescue stays night-only.
    ASSERT_FALSE( lookAssistIsFloorLiftedNightThumbnail( day, flatDay ) );
    ASSERT_TRUE( lookAssistIsFloorLiftedNightThumbnail( LookAssistScene::Night, fixtureRawStats() ) );
    ASSERT_TRUE( lookAssistShouldAnalyzeProcessedColor( LookAssistScene::Night, fixtureRawStats() ) );

    // A usable thumbnail (real tonal spread) keeps reading colour from the RAW thumbnail.
    const LookAssistStats usable = withEv( analyzeFrame( 64, 64, []( int x, int ) {
        return std::vector<int>{ 40 + x, 50 + x, 60 + x }; } ), 100, 4000, 400 );
    ASSERT_FALSE( lookAssistIsFlatFloorRawThumbnail( usable ) );
    ASSERT_FALSE( lookAssistShouldAnalyzeProcessedColor( classifyLookAssistScene( usable ), usable ) );
}

TEST(LookAssistScene, DaylightFlatFloorGetsNoNightRescueExposure)
{
    // The night rescue measures the floor-lifted spread (median - p05 + 2). If that leaked into a
    // daylight clip, a 2-count spread would ask for a huge exposure.
    LookAssistStats tinySpread = withEv( fixtureRawStats(), 100, 465, 560 );
    tinySpread.p05 = 36; tinySpread.dynamicRange = tinySpread.p95 - tinySpread.p05;   // spread of 1 count
    const LookAssistScene day = classifyLookAssistScene( tinySpread );
    ASSERT_TRUE( day == LookAssistScene::Shade );
    const LookAssistPreset p = presetForLookAssistScene( day, tinySpread );
    ASSERT_TRUE( p.exposure > 0 );
    ASSERT_TRUE( p.exposure <= 180 );
    // Independent of the spread: p05 anywhere in the flat band gives the same daylight exposure.
    for( double p05 : { 18.0, 25.0, 30.0, 35.0, 36.0 } )
    {
        LookAssistStats s = tinySpread;
        s.p05 = p05; s.dynamicRange = s.p95 - s.p05;
        ASSERT_EQ( p.exposure, presetForLookAssistScene( classifyLookAssistScene( s ), s ).exposure );
    }
    // The same statistics read as night DO get the (much larger) rescue.
    ASSERT_TRUE( presetForLookAssistScene( LookAssistScene::Night, fixtureRawStats() ).exposure > p.exposure );
}

TEST(LookAssistScene, DaylightSolveIsUndampedOnlyFromTheRenderedPicture)
{
    const LookAssistStats day = withEv( fixtureRawStats(), 100, 465, 560 );
    ASSERT_TRUE( lookAssistDaylightSolveIsUndamped( day, LookAssistScene::Shade, true ) );
    ASSERT_FALSE( lookAssistDaylightSolveIsUndamped( day, LookAssistScene::Shade, false ) );   // raw patch: hedge as before
    ASSERT_FALSE( lookAssistDaylightSolveIsUndamped( day, LookAssistScene::Night, true ) );
    ASSERT_FALSE( lookAssistDaylightSolveIsUndamped( fixtureRawStats(), LookAssistScene::Night, true ) );
    ASSERT_FALSE( lookAssistDaylightSolveIsUndamped( fixtureRawStats(), LookAssistScene::Shade, true ) );   // no exposure metadata
}

TEST(LookAssistScene, DaylightSolveIsNotRejectedByTheThumbnailBrightnessCoinFlip)
{
    // The fixture's neutral-deck patch, measured in the real app: chroma 12-13, blue-amber +12..13,
    // luma 199.97 (receipt exposure) or 208.9 (exposure normalised). The correct solution is
    // 9990 K / tint -35 from base 6000 K / 0. The generic two-axis-swing rule rejected it from the
    // brighter of the two (luma >= 200) and accepted it from the dimmer: a coin flip on brightness.
    LookAssistAutoWhiteBalancePatch dim;
    dim.valid = true; dim.luma = 199.965; dim.chroma = 13.0; dim.greenAxis = -6.5; dim.blueAmberAxis = 13.0;
    LookAssistAutoWhiteBalancePatch bright = dim;
    bright.luma = 208.891; bright.chroma = 12.0; bright.greenAxis = -6.0; bright.blueAmberAxis = 12.0;
    ASSERT_TRUE( lookAssistAutoWhiteBalanceSolutionIsStable( dim, 6000, 0, 9990, -35 ) );
    ASSERT_FALSE( lookAssistAutoWhiteBalanceSolutionIsStable( bright, 6000, 0, 9990, -35 ) );   // generic rule, unchanged
    // A daylight solve from the rendered picture is bounded by the window instead: both accepted.
    ASSERT_TRUE( lookAssistAutoWhiteBalanceSolutionIsStable( dim, 6000, 0, 9990, -35, true ) );
    ASSERT_TRUE( lookAssistAutoWhiteBalanceSolutionIsStable( bright, 6000, 0, 9990, -35, true ) );
    // ... but the green-clamp / cooling guards still apply to it.
    LookAssistAutoWhiteBalancePatch neutralBright = bright;
    neutralBright.luma = 215.0; neutralBright.chroma = 5.0;
    ASSERT_FALSE( lookAssistAutoWhiteBalanceSolutionIsStable( neutralBright, 6000, 0, 6500, -35, true ) );
    ASSERT_FALSE( lookAssistAutoWhiteBalanceSolutionIsStable( LookAssistAutoWhiteBalancePatch(), 6000, 0, 9990, -35, true ) );
}

TEST(LookAssistScene, AsShotWhiteBalanceIsTheDaylightFallbackPrior)
{
    LookAssistStats s = withEv( fixtureRawStats(), 100, 465, 560 );
    int temperature = 0, tint = 0;
    // No recorded white balance: no prior.
    ASSERT_FALSE( lookAssistAsShotPrior( s, LookAssistScene::Shade, &temperature, &tint ) );
    lookAssistSetAsShotWhiteBalance( &s, true, 5500, -3 );
    ASSERT_TRUE( lookAssistAsShotPrior( s, LookAssistScene::Shade, &temperature, &tint ) );
    ASSERT_EQ( 5500, temperature );
    ASSERT_EQ( -3, tint );
    // A tungsten as-shot on a daylight clip is clamped into the daylight bounds, not trusted raw.
    lookAssistSetAsShotWhiteBalance( &s, true, 3200, 30 );
    ASSERT_TRUE( lookAssistAsShotPrior( s, LookAssistScene::Shade, &temperature, &tint ) );
    ASSERT_EQ( 4800, temperature );
    ASSERT_EQ( 10, tint );
    // Never for night / artificial light / clips without daylight metadata.
    ASSERT_FALSE( lookAssistAsShotPrior( s, LookAssistScene::Night, &temperature, &tint ) );
    ASSERT_FALSE( lookAssistAsShotPrior( s, LookAssistScene::ArtificialLights, &temperature, &tint ) );
    LookAssistStats noMeta = fixtureRawStats();
    lookAssistSetAsShotWhiteBalance( &noMeta, true, 5500, 0 );
    ASSERT_FALSE( lookAssistAsShotPrior( noMeta, LookAssistScene::Shade, &temperature, &tint ) );
    lookAssistSetAsShotWhiteBalance( &s, false, 5500, 0 );
    ASSERT_FALSE( s.hasAsShotWb );
    ASSERT_FALSE( lookAssistAsShotPrior( s, LookAssistScene::Shade, &temperature, &tint ) );
}

TEST(LookAssistScene, ClipsWithoutExposureMetadataClassifyExactlyAsBefore)
{
    // Sweep a dense grid of statistics: with no EV100 the new classifier must equal the old one.
    int checked = 0;
    for( int median = 0; median <= 255; median += 5 )
        for( int p95 = 0; p95 <= 255; p95 += 15 )
            for( int p99 = 0; p99 <= 255; p99 += 17 )
                for( double clipHigh : { 0.0, 0.004, 0.007, 0.012, 0.02 } )
                {
                    LookAssistStats s;
                    s.median = median; s.p95 = p95; s.p99 = p99; s.clipHigh = clipHigh;
                    ASSERT_TRUE( classifyLookAssistScene( s ) == legacyClassify( s ) );
                    // Dim exposure metadata (night / indoor) never changes a verdict either.
                    ASSERT_TRUE( classifyLookAssistScene( withEv( s, 3200, 33333, 180 ) ) == legacyClassify( s ) );
                    ++checked;
                }
    ASSERT_TRUE( checked > 5000 );

    // With daylight metadata the ONLY change is that night / artificial-lights / shade collapse
    // to shade (sun stays sun): every bright-sun verdict is preserved.
    for( int median = 0; median <= 255; median += 5 )
        for( int p95 = 0; p95 <= 255; p95 += 15 )
        {
            LookAssistStats s;
            s.median = median; s.p95 = p95; s.p99 = p95;
            const LookAssistScene before = legacyClassify( s );
            const LookAssistScene after = classifyLookAssistScene( withEv( s, 100, 465, 560 ) );
            if( before == LookAssistScene::BrightSun ) ASSERT_TRUE( after == LookAssistScene::BrightSun );
            else ASSERT_TRUE( after == LookAssistScene::Shade );
        }
}

TEST(LookAssistScene, CpuAndCudaShareOneClassifier)
{
    // Look Assist only ever writes the eight receipt sliders, so the CUDA/GL display path and the
    // CPU path see the same scene class iff the analysis is one implementation. The two
    // consumers (headless ReceiptApplier = CPU batch; MainWindow = GUI, both display paths) must
    // call the shared module and must not define a classifier or white-balance window of their own.
    const QString applier = readRepoFile( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    const QString window = readRepoFile( QStringLiteral("platform/qt/MainWindow.cpp") );
    ASSERT_FALSE( applier.isEmpty() );
    ASSERT_FALSE( window.isEmpty() );
    for( const QString &source : { applier, window } )
    {
        ASSERT_TRUE( source.contains( QStringLiteral("LookAssistAnalysis.h") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("classifyLookAssistScene(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("lookAssistSetSceneEv100(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("lookAssistWhiteBalanceBounds(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("lookAssistClampWhiteBalance(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("lookAssistShouldAnalyzeProcessedColor(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("lookAssistDaylightSolveIsUndamped(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("lookAssistAsShotPrior(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("presetForLookAssistScene(") ) );
        // A definition (return type at line start, body follows) would be a second implementation.
        const QRegularExpression ownDefinition( QStringLiteral(
            "^(static\\s+)?(LookAssistScene|LookAssistPreset|LookAssistStats|LookAssistWhiteBalanceBounds)\\s+"
            "(classifyLookAssistScene|presetForLookAssistScene|analyzeLookAssistThumbnail|lookAssistWhiteBalanceBounds)\\s*\\("),
            QRegularExpression::MultilineOption );
        ASSERT_FALSE( ownDefinition.match( source ).hasMatch() );
    }
    // Both builds compile the module.
    ASSERT_TRUE( readRepoFile( QStringLiteral("platform/qt/MLVApp.pro") ).contains( QStringLiteral("LookAssistAnalysis.cpp") ) );
    ASSERT_TRUE( readRepoFile( QStringLiteral("tests/pipeline/pipeline_tests.pro") ).contains( QStringLiteral("LookAssistAnalysis.cpp") ) );

    // And, as a pure function, the same statistics give the same decision every time.
    const LookAssistStats s = withEv( fixtureRawStats(), 100, 465, 560 );
    const LookAssistScene a = classifyLookAssistScene( s );
    const LookAssistScene b = classifyLookAssistScene( s );
    ASSERT_TRUE( a == b );
    const LookAssistPreset pa = presetForLookAssistScene( a, s );
    const LookAssistPreset pb = presetForLookAssistScene( b, s );
    ASSERT_EQ( pa.exposure, pb.exposure );
    ASSERT_EQ( pa.temperatureDelta, pb.temperatureDelta );
    ASSERT_EQ( pa.tintDelta, pb.tintDelta );
}
