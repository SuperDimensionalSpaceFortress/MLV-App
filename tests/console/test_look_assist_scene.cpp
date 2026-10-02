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

// The daylight fixture as the app sees it AFTER the picture check: ISO 100, 1/2150 s, f/5.6 (EV100 16) and
// a rendered picture that corroborates it (see DaylightNeedsThePictureNotJustTheExposure).
LookAssistStats daylightFixture()
{
    LookAssistStats s = withEv( fixtureRawStats(), 100, 465, 560 );
    s.daylightPictureEvidence = true;
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

// The processed picture a render callback would return, built from per-pixel luma (grey).
template<class F>
LookAssistStats renderedPicture( int w, int h, F lumaAt )
{
    return analyzeFrame( w, h, [&]( int x, int y ) {
        const int l = lumaAt( x, y );
        return std::vector<int>{ l, l, l }; } );
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
    const LookAssistStats fixture = daylightFixture();
    ASSERT_TRUE( fixture.hasSceneEv100 );
    ASSERT_TRUE( lookAssistExposureIsDaylightBright( fixture ) );
    ASSERT_TRUE( lookAssistSceneIsDaylight( fixture ) );
    const LookAssistScene scene = classifyLookAssistScene( fixture );
    ASSERT_TRUE( scene != LookAssistScene::Night );
    ASSERT_TRUE( scene != LookAssistScene::ArtificialLights );
    ASSERT_TRUE( scene == LookAssistScene::Shade );   // dim RAW floor, daylight exposure AND picture: day

    // The exposure alone is NOT daylight (a night moon records the same EV): without the picture
    // evidence the same display statistics read exactly as before.
    const LookAssistStats exposureOnly = withEv( fixtureRawStats(), 100, 465, 560 );
    ASSERT_TRUE( lookAssistExposureIsDaylightBright( exposureOnly ) );
    ASSERT_FALSE( lookAssistSceneIsDaylight( exposureOnly ) );
    ASSERT_TRUE( classifyLookAssistScene( exposureOnly ) == LookAssistScene::Night );
    ASSERT_TRUE( classifyLookAssistScene( fixtureRawStats() ) == LookAssistScene::Night );

    // The preset for the daylight class lifts the picture but is not the night rescue.
    const LookAssistPreset day = presetForLookAssistScene( scene, fixture );
    const LookAssistPreset night = presetForLookAssistScene( LookAssistScene::Night, fixtureRawStats() );
    ASSERT_TRUE( day.exposure > 0 );
    ASSERT_TRUE( day.exposure <= 180 );
    ASSERT_TRUE( day.shadows < night.shadows );
}

TEST(LookAssistScene, DaylightNeedsThePictureNotJustTheExposure)
{
    // Round-2 blocker (sol): a bright-SUBJECT night shot -- the moon, ISO 200, 1/500 s, f/7.1 -- records
    // EV100 13.6 over a black sky. Its RAW thumbnail is a flat floor, exactly like the daylight fixture's,
    // so only the RENDERED picture can tell them apart.
    const LookAssistStats moonRaw = withEv( fixtureRawStats(), 200, 2000, 710 );
    ASSERT_NEAR( 13.62, moonRaw.sceneEv100, 0.05 );
    ASSERT_TRUE( lookAssistIsFlatFloorRawThumbnail( moonRaw ) );
    ASSERT_TRUE( classifyLookAssistScene( moonRaw ) == LookAssistScene::Night );     // legacy verdict
    ASSERT_TRUE( lookAssistDaylightNeedsPictureEvidence( moonRaw, LookAssistScene::Night ) );

    // Black sky (luma 6..10) with a small bright disc: rendered at the lift the daylight verdict would apply.
    int moonRenders = 0;
    double moonStops = -1.0;
    LookAssistStats moon = moonRaw;
    const LookAssistScene moonScene = resolveLookAssistScene( &moon, [&]( double stops, LookAssistStats *out ) {
        ++moonRenders;
        moonStops = stops;
        *out = renderedPicture( 64, 64, []( int x, int y ) {
            return ( ( x - 32 ) * ( x - 32 ) + ( y - 20 ) * ( y - 20 ) < 30 ) ? 238 : 6 + ( x + y ) % 5; } );
        return true; } );
    ASSERT_EQ( 1, moonRenders );
    // Judged at the exposure the daylight verdict WOULD apply (the Shade preset's lift of this floor).
    LookAssistStats hypothesis = moonRaw;
    hypothesis.daylightPictureEvidence = true;
    const double plannedStops = presetForLookAssistScene( LookAssistScene::Shade, hypothesis ).exposure / 100.0;
    ASSERT_TRUE( plannedStops > 1.0 && plannedStops <= 1.8 );
    ASSERT_NEAR( plannedStops, moonStops, 1e-9 );
    ASSERT_TRUE( moonScene == LookAssistScene::Night );
    ASSERT_FALSE( moon.daylightPictureEvidence );
    ASSERT_FALSE( lookAssistIsDaylightScene( moon, moonScene ) );
    // ... so the NIGHT rescue stays on, and no daylight window / prior / undamped solve applies.
    ASSERT_TRUE( lookAssistIsFloorLiftedNightThumbnail( moonScene, moon ) );
    ASSERT_TRUE( lookAssistShouldAnalyzeProcessedColor( moonScene, moon ) );
    ASSERT_EQ( 2000, lookAssistWhiteBalanceBounds( moon, moonScene ).minTemperature );
    int t = 0, tint = 0;
    lookAssistSetAsShotWhiteBalance( &moon, true, 5500, 0 );
    ASSERT_FALSE( lookAssistAsShotPrior( moon, moonScene, &t, &tint ) );
    ASSERT_FALSE( lookAssistDaylightSolveIsUndamped( moon, moonScene, true ) );
    ASSERT_EQ( presetForLookAssistScene( LookAssistScene::Night, moonRaw ).exposure,
               presetForLookAssistScene( moonScene, moon ).exposure );

    // A night noise floor lifted by the pipeline (everything luma ~36..50, narrow, below the lit band) is
    // not daylight either; nor is a picture that is lit over only a small part of the frame.
    LookAssistStats noise = moonRaw;
    ASSERT_TRUE( resolveLookAssistScene( &noise, []( double, LookAssistStats *out ) {
        *out = renderedPicture( 64, 64, []( int x, int y ) { return 36 + ( x * 3 + y ) % 15; } );
        return true; } ) == LookAssistScene::Night );
    LookAssistStats stage = moonRaw;   // a lit stage: 15 % of the frame bright mid-tones, the rest black
    ASSERT_TRUE( resolveLookAssistScene( &stage, []( double, LookAssistStats *out ) {
        *out = renderedPicture( 64, 64, []( int x, int ) { return x < 10 ? 140 : 8; } );
        return true; } ) == LookAssistScene::Night );

    // The band, by its edges: lit means >= 60 % mid-tones AND a median of 55..190.
    LookAssistStats edge;
    edge.midtoneFraction = 0.9; edge.median = 56.0;
    ASSERT_TRUE( lookAssistPictureCorroboratesDaylight( edge ) );
    edge.median = 54.0;
    ASSERT_FALSE( lookAssistPictureCorroboratesDaylight( edge ) );
    edge.median = 120.0; edge.midtoneFraction = 0.55;
    ASSERT_FALSE( lookAssistPictureCorroboratesDaylight( edge ) );
    edge.midtoneFraction = 0.65;
    ASSERT_TRUE( lookAssistPictureCorroboratesDaylight( edge ) );
    edge.median = 200.0;
    ASSERT_FALSE( lookAssistPictureCorroboratesDaylight( edge ) );

    // The tracked fixture's picture at its lift: median 77 (app profile render) / ~116 (app playback
    // render) / 140-147 (headless), 95-99 % mid-tones.
    LookAssistStats fixture = withEv( fixtureRawStats(), 100, 465, 560 );
    int fixtureRenders = 0;
    const LookAssistScene fixtureScene = resolveLookAssistScene( &fixture, [&]( double, LookAssistStats *out ) {
        ++fixtureRenders;
        *out = renderedPicture( 64, 64, []( int x, int y ) { return 70 + ( x + 2 * y ) % 24; } );
        return true; } );
    ASSERT_EQ( 1, fixtureRenders );
    ASSERT_TRUE( fixtureScene == LookAssistScene::Shade );
    ASSERT_TRUE( fixture.daylightPictureEvidence );
    ASSERT_TRUE( lookAssistIsDaylightScene( fixture, fixtureScene ) );

    // ND-filter daylight (ISO 100, 1/50 s, f/2.8 = EV100 8.6): the exposure cannot say daylight, so the
    // picture is never even consulted and the verdict is the legacy one (no regression vs master).
    LookAssistStats nd = withEv( fixtureRawStats(), 100, 20000, 280 );
    ASSERT_FALSE( lookAssistExposureIsDaylightBright( nd ) );
    int ndRenders = 0;
    ASSERT_TRUE( resolveLookAssistScene( &nd, [&]( double, LookAssistStats *out ) {
        ++ndRenders; *out = renderedPicture( 8, 8, []( int, int ) { return 100; } ); return true; } ) == LookAssistScene::Night );
    ASSERT_EQ( 0, ndRenders );

    // No exposure block, or a render that fails, or no render callback: legacy verdict, nothing guessed.
    LookAssistStats noMeta = fixtureRawStats();
    ASSERT_TRUE( resolveLookAssistScene( &noMeta, []( double, LookAssistStats * ) { return true; } ) == LookAssistScene::Night );
    LookAssistStats failing = withEv( fixtureRawStats(), 100, 465, 560 );
    ASSERT_TRUE( resolveLookAssistScene( &failing, []( double, LookAssistStats * ) { return false; } ) == LookAssistScene::Night );
    LookAssistStats noCallback = withEv( fixtureRawStats(), 100, 465, 560 );
    ASSERT_TRUE( resolveLookAssistScene( &noCallback, LookAssistRenderFn() ) == LookAssistScene::Night );

    // A usable (non-flat) RAW thumbnail is trusted as it is: no picture is rendered for it.
    LookAssistStats usableDark = withEv( analyzeFrame( 64, 64, []( int x, int ) {
        return std::vector<int>{ 20 + x / 2, 20 + x / 2, 20 + x / 2 }; } ), 100, 465, 560 );
    ASSERT_FALSE( lookAssistIsFlatFloorRawThumbnail( usableDark ) );
    int usableRenders = 0;
    ASSERT_TRUE( resolveLookAssistScene( &usableDark, [&]( double, LookAssistStats *out ) {
        ++usableRenders; *out = renderedPicture( 8, 8, []( int, int ) { return 100; } ); return true; } )
                 == legacyClassify( usableDark ) );
    ASSERT_EQ( 0, usableRenders );
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
    const LookAssistStats fixture = daylightFixture();
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
    const LookAssistStats flatDay = daylightFixture();
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

TEST(LookAssistScene, FlatFloorGateIsNotWidenedToArtificialLightsOrBrightSun)
{
    // Flat-floor artificial-lights / bright-sun clips WITHOUT daylight evidence behave exactly as on
    // master: no processed-colour analysis (and so no auto chroma smoothing, no processed-patch white
    // balance), because the night-only fail-closed guards do not cover them.
    for( double median : { 26.0, 33.0, 40.0, 66.0 } )
        for( double p99 : { 40.0, 190.0, 240.0 } )
            for( double clipHigh : { 0.0, 0.01, 0.02 } )
            {
                LookAssistStats s = fixtureRawStats();
                s.median = median; s.p05 = 24; s.p95 = 40; s.p99 = p99; s.clipHigh = clipHigh;
                s.dynamicRange = s.p95 - s.p05;
                for( bool withExposure : { false, true } )
                {
                    const LookAssistStats t = withExposure ? withEv( s, 100, 465, 560 ) : s;   // EV only, no evidence
                    const LookAssistScene scene = classifyLookAssistScene( t );
                    ASSERT_TRUE( scene == legacyClassify( s ) );
                    ASSERT_EQ( lookAssistIsFloorLiftedNightThumbnail( scene, t ),
                               lookAssistShouldAnalyzeProcessedColor( scene, t ) );   // the master rule
                }
            }
    // The one addition: a flat floor in a corroborated daylight scene.
    const LookAssistStats day = daylightFixture();
    ASSERT_TRUE( lookAssistShouldAnalyzeProcessedColor( classifyLookAssistScene( day ), day ) );
}

TEST(LookAssistScene, DaylightFlatFloorGetsNoNightRescueExposure)
{
    // The night rescue measures the floor-lifted spread (median - p05 + 2). If that leaked into a
    // daylight clip, a 2-count spread would ask for a huge exposure.
    LookAssistStats tinySpread = daylightFixture();
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
    const LookAssistStats day = daylightFixture();
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

TEST(LookAssistScene, BlueSurfaceIsNotSolvedToTheRailUndamped)
{
    // A daylight solve skips the two-axis-swing rejection, so the patch itself must be able to be
    // neutral. A pale-blue sky / water patch (B-R +30, chroma 30 at luma 202) neutralised would drive
    // the picture to 10000 K / tint -35 undamped.
    LookAssistAutoWhiteBalancePatch blue;
    blue.valid = true; blue.luma = 202.0; blue.chroma = 30.0; blue.greenAxis = -2.0; blue.blueAmberAxis = 30.0;
    ASSERT_FALSE( lookAssistDaylightPatchIsNeutralEnough( blue ) );
    ASSERT_FALSE( lookAssistAutoWhiteBalanceSolutionIsStable( blue, 6000, 0, 9990, -35, true ) );
    LookAssistAutoWhiteBalancePatch paleBlue = blue;   // mildly blue: still not neutral
    paleBlue.chroma = 20.0; paleBlue.blueAmberAxis = 20.0; paleBlue.luma = 200.0;
    ASSERT_FALSE( lookAssistDaylightPatchIsNeutralEnough( paleBlue ) );
    // The tracked deck patch (real app: chroma 12-13, luma 200-209, B-R +12..13) is accepted.
    LookAssistAutoWhiteBalancePatch deck;
    deck.valid = true; deck.luma = 199.965; deck.chroma = 13.0; deck.greenAxis = -6.5; deck.blueAmberAxis = 13.0;
    ASSERT_TRUE( lookAssistDaylightPatchIsNeutralEnough( deck ) );
    ASSERT_TRUE( lookAssistAutoWhiteBalanceSolutionIsStable( deck, 6000, 0, 9990, -35, true ) );
    deck.luma = 208.891; deck.chroma = 12.0; deck.blueAmberAxis = 12.0;
    ASSERT_TRUE( lookAssistAutoWhiteBalanceSolutionIsStable( deck, 6000, 0, 9990, -35, true ) );

    // End to end through the ONE resolution: the solver answers 9990 K / -35 for the blue patch; the
    // result is the as-shot (mode-aware) base inside the daylight window, NOT the rail.
    LookAssistStats day = daylightFixture();
    lookAssistSetAsShotWhiteBalance( &day, true, 6000, 0 );
    LookAssistWhiteBalanceRequest request;
    request.stats = &day; request.scene = LookAssistScene::Shade; request.patch = blue;
    request.solvedOnProcessedPicture = true; request.baseTemperature = 6000; request.baseTint = 0;
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
        request, []( int, int, int *t, int *tint ) { *t = 9990; *tint = -35; }, &preset );
    ASSERT_FALSE( r.autoValid );
    ASSERT_TRUE( r.decision == QStringLiteral("rejected-unstable") || r.decision == QStringLiteral("prior") );
    ASSERT_TRUE( r.source == QStringLiteral("as-shot-prior") );
    ASSERT_EQ( 6000, r.temperature );
    ASSERT_EQ( 0, r.tint );
    ASSERT_EQ( 9990, r.candidateTemperature );   // reported, never applied
}

TEST(LookAssistScene, AsShotWhiteBalanceIsTheDaylightFallbackPrior)
{
    LookAssistStats s = daylightFixture();
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

    // Daylight EXPOSURE alone changes nothing (no picture evidence): every verdict is the legacy one.
    for( int median = 0; median <= 255; median += 5 )
        for( int p95 = 0; p95 <= 255; p95 += 15 )
        {
            LookAssistStats s;
            s.median = median; s.p95 = p95; s.p99 = p95;
            ASSERT_TRUE( classifyLookAssistScene( withEv( s, 100, 465, 560 ) ) == legacyClassify( s ) );
        }

    // With the picture's evidence the ONLY change is that night / artificial-lights / shade collapse
    // to shade (sun stays sun): every bright-sun verdict is preserved.
    for( int median = 0; median <= 255; median += 5 )
        for( int p95 = 0; p95 <= 255; p95 += 15 )
        {
            LookAssistStats s;
            s.median = median; s.p95 = p95; s.p99 = p95;
            const LookAssistScene before = legacyClassify( s );
            LookAssistStats day = withEv( s, 100, 465, 560 );
            day.daylightPictureEvidence = true;
            const LookAssistScene after = classifyLookAssistScene( day );
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
        ASSERT_TRUE( source.contains( QStringLiteral("lookAssistSetSceneEv100(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("lookAssistShouldAnalyzeProcessedColor(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("resolveLookAssistScene(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("resolveLookAssistWhiteBalance(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("presetForLookAssistScene(") ) );
        // The white-balance decision (solve -> stability -> damping -> prior -> clamp) lives ONCE, in
        // the shared module. A direct call to any step from a consumer is a second orchestration.
        ASSERT_FALSE( source.contains( QStringLiteral("lookAssistAutoWhiteBalanceSolutionIsStable(") ) );
        ASSERT_FALSE( source.contains( QStringLiteral("lookAssistAutoWhiteBalanceDampingFactor(") ) );
        ASSERT_FALSE( source.contains( QStringLiteral("lookAssistDaylightSolveIsUndamped(") ) );
        ASSERT_FALSE( source.contains( QStringLiteral("lookAssistAsShotPrior(") ) );
        ASSERT_FALSE( source.contains( QStringLiteral("classifyLookAssistScene(") ) );
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
    const LookAssistStats s = daylightFixture();
    const LookAssistScene a = classifyLookAssistScene( s );
    const LookAssistScene b = classifyLookAssistScene( s );
    ASSERT_TRUE( a == b );
    const LookAssistPreset pa = presetForLookAssistScene( a, s );
    const LookAssistPreset pb = presetForLookAssistScene( b, s );
    ASSERT_EQ( pa.exposure, pb.exposure );
    ASSERT_EQ( pa.temperatureDelta, pb.temperatureDelta );
    ASSERT_EQ( pa.tintDelta, pb.tintDelta );
}

TEST(LookAssistScene, OneWhiteBalanceDecisionForEveryPath)
{
    // GUI sync, GUI async and the headless applier differ only in the solver they hand over and the
    // control ranges. The ranges are the shared constants (MainWindow.ui is pinned to them) ...
    const QString ui = readRepoFile( QStringLiteral("platform/qt/MainWindow.ui") );
    ASSERT_FALSE( ui.isEmpty() );
    auto sliderRange = [&]( const char *name, int *minimum, int *maximum ) {
        const int at = ui.indexOf( QStringLiteral("name=\"%1\"").arg( QLatin1String( name ) ) );
        if( at < 0 ) return false;
        const QRegularExpression min( QStringLiteral("<property name=\"minimum\">\\s*<number>(-?\\d+)</number>") );
        const QRegularExpression max( QStringLiteral("<property name=\"maximum\">\\s*<number>(-?\\d+)</number>") );
        const QRegularExpressionMatch a = min.match( ui, at );
        const QRegularExpressionMatch b = max.match( ui, at );
        if( !a.hasMatch() || !b.hasMatch() ) return false;
        *minimum = a.captured( 1 ).toInt();
        *maximum = b.captured( 1 ).toInt();
        return true;
    };
    int tMin = 0, tMax = 0, nMin = 0, nMax = 0;
    ASSERT_TRUE( sliderRange( "horizontalSliderTemperature", &tMin, &tMax ) );
    ASSERT_TRUE( sliderRange( "horizontalSliderTint", &nMin, &nMax ) );
    ASSERT_EQ( kLookAssistTemperatureMin, tMin );
    ASSERT_EQ( kLookAssistTemperatureMax, tMax );
    ASSERT_EQ( kLookAssistTintMin, nMin );
    ASSERT_EQ( kLookAssistTintMax, nMax );

    // ... so the same input gives the same final white balance whichever path asks (receipt parity).
    LookAssistStats day = daylightFixture();
    lookAssistSetAsShotWhiteBalance( &day, true, 7000, 0 );
    const LookAssistStats night = fixtureRawStats();
    struct Case { const LookAssistStats *stats; LookAssistScene scene; bool processed; double luma, chroma, blueAmber; int solvedT, solvedTint; };
    const Case cases[] = {
        { &day, LookAssistScene::Shade, true, 205.0, 12.0, 12.0, 9990, -35 },   // the deck
        { &day, LookAssistScene::Shade, true, 202.0, 30.0, 30.0, 9990, -35 },   // blue surface
        { &day, LookAssistScene::Shade, true, 90.0, 5.0, 2.0, 6100, 4 },        // neutral mid patch
        { &day, LookAssistScene::Shade, false, 205.0, 12.0, 12.0, 9990, -35 },  // raw patch: damped
        { &night, LookAssistScene::Night, true, 120.0, 14.0, 8.0, 8594, -23 },
        { &night, LookAssistScene::Night, false, 120.0, 4.0, 3.0, 5200, 10 },
    };
    for( const Case &c : cases )
        for( int baseT : { 6000, 7000 } )
            for( bool havePatch : { true, false } )
            {
                LookAssistWhiteBalanceRequest headless;
                headless.stats = c.stats; headless.scene = c.scene; headless.solvedOnProcessedPicture = c.processed;
                headless.baseTemperature = baseT; headless.baseTint = 0;
                headless.patch.valid = havePatch;
                headless.patch.luma = c.luma; headless.patch.chroma = c.chroma;
                headless.patch.blueAmberAxis = c.blueAmber; headless.patch.greenAxis = -3.0;
                LookAssistWhiteBalanceRequest gui = headless;   // sync and async: the slider ranges
                gui.minTemperature = tMin; gui.maxTemperature = tMax; gui.minTint = nMin; gui.maxTint = nMax;
                int calls = 0;
                auto solver = [&]( int, int, int *t, int *tint ) { ++calls; *t = c.solvedT; *tint = c.solvedTint; };
                LookAssistPreset pa = presetForLookAssistScene( c.scene, *c.stats );
                LookAssistPreset pb = pa;
                const LookAssistWhiteBalanceResolution a = resolveLookAssistWhiteBalance( headless, solver, &pa );
                const LookAssistWhiteBalanceResolution b = resolveLookAssistWhiteBalance( gui, solver, &pb );
                ASSERT_EQ( havePatch ? 2 : 0, calls );
                ASSERT_EQ( a.temperature, b.temperature );
                ASSERT_EQ( a.tint, b.tint );
                ASSERT_EQ( a.autoValid, b.autoValid );
                ASSERT_TRUE( a.decision == b.decision );
                ASSERT_TRUE( a.source == b.source );
                ASSERT_EQ( pa.temperatureDelta, pb.temperatureDelta );
                ASSERT_EQ( pa.tintDelta, pb.tintDelta );
                // The preset describes what was applied.
                ASSERT_EQ( a.temperature, baseT + pa.temperatureDelta );
                ASSERT_EQ( a.tint, pa.tintDelta );
            }

    // The deck itself: accepted undamped from the rendered picture, at the solver's answer.
    LookAssistWhiteBalanceRequest deck;
    deck.stats = &day; deck.scene = LookAssistScene::Shade; deck.solvedOnProcessedPicture = true;
    deck.patch.valid = true; deck.patch.luma = 205.0; deck.patch.chroma = 12.0; deck.patch.blueAmberAxis = 12.0;
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
        deck, []( int, int, int *t, int *tint ) { *t = 9990; *tint = -35; }, &preset );
    ASSERT_TRUE( r.autoValid );
    ASSERT_TRUE( r.decision == QStringLiteral("accepted") );
    ASSERT_EQ( 9990, r.temperature );
    ASSERT_EQ( -35, r.tint );
    ASSERT_NEAR( 1.0, r.damping, 1e-9 );
}

TEST(LookAssistScene, WhiteBalanceDecoderIsSharedWithTheGui)
{
    // One mode-aware WBAL decoder (ReceiptApplier::asShotWhiteBalanceControls); the GUI's
    // setWhiteBalanceFromMlv delegates to it and carries no WBAL switch of its own.
    const QString window = readRepoFile( QStringLiteral("platform/qt/MainWindow.cpp") );
    ASSERT_TRUE( window.contains( QStringLiteral("ReceiptApplier::asShotWhiteBalanceControls(") ) );
    ASSERT_FALSE( window.contains( QStringLiteral("getMlvWbMode(") ) );
    ASSERT_FALSE( window.contains( QStringLiteral("getMlvWbKelvin(") ) );
    ASSERT_FALSE( window.contains( QStringLiteral("getMlvWbRgain(") ) );
}

// ---- LOOK-ASSIST-SCENE-CLASSIFY-2: corroborated daylight WITHOUT a trusted patch is balanced from the picture ----
//
// PR #221 r2 left such a clip on the as-shot prior (6000 K / tint 0 in the real app's profile-settle state:
// deck chroma 18.9 against master's 13.5). The refinement renders the picture at the white balance under test,
// steps it until it has neutral samples, runs the SAME patch search + solver + guards on it, and verifies by
// rendering at the solution. These tests drive resolveLookAssistWhiteBalance end to end over a synthetic scene
// whose rendered colours follow the white balance the way a real picture does (a blue "water" majority that no
// white balance neutralises, and a physically neutral "deck" that the right one does).
namespace
{

struct DeckScene
{
    int neutralTemperature = 6540;     // the white balance at which the deck renders neutral
    int neutralTint = -20;   // not on the -35 rail: a mid-tone near-neutral patch solved onto the rail is distrusted by design
    bool hasDeck = true;
    int width = 80;
    int height = 60;
    int downscale = 4;
    mutable int renders = 0;
    mutable int solveCalls = 0;
    mutable int solveRawX = -1;
    mutable int solveRawY = -1;
    // How the deck's cast moves with the white balance: blue-amber per mired, green per tint unit.
    double blueAmberPerMired = 0.5;
    double greenPerTint = -0.15;

    bool onDeck( int x, int y ) const { return hasDeck && x >= 10 && x < 50 && y >= 30 && y < 45; }

    LookAssistRenderBalanceFn renderer() const
    {
        return [this]( double, int temperature, int tint, LookAssistRenderedPicture *out ) -> bool
        {
            ++renders;
            const double dMired = 1.0e6 / temperature - 1.0e6 / neutralTemperature;
            const double blueAmber = blueAmberPerMired * dMired;
            const double green = greenPerTint * ( tint - neutralTint );
            auto clamp = []( double v ) { return static_cast<unsigned char>( qBound( 0, qRound( v ), 255 ) ); };
            out->width = width;
            out->height = height;
            out->downscaleFactor = downscale;
            out->rgb.assign( static_cast<size_t>( width ) * height * 3, 0 );
            for( int y = 0; y < height; ++y )
                for( int x = 0; x < width; ++x )
                {
                    unsigned char *p = &out->rgb[( static_cast<size_t>( y ) * width + x ) * 3];
                    if( onDeck( x, y ) )
                    {
                        p[0] = clamp( 140.0 - blueAmber / 2.0 );
                        p[1] = clamp( 140.0 + green );
                        p[2] = clamp( 140.0 + blueAmber / 2.0 );
                    }
                    else
                    {
                        p[0] = 70; p[1] = 110; p[2] = 170;   // water / sky: blue whatever the white balance
                    }
                }
            out->stats = analyzeLookAssistThumbnail( out->rgb.data(), width, height );
            return true;
        };
    }

    LookAssistWhiteBalanceSolveFn solver( int wrongTemperature = 3000, int wrongTint = 30 ) const
    {
        return [this, wrongTemperature, wrongTint]( int rawX, int rawY, int *t, int *tint )
        {
            ++solveCalls;
            solveRawX = rawX;
            solveRawY = rawY;
            const int x = rawX / downscale;
            const int y = rawY / downscale;
            if( onDeck( x, y ) ) { *t = neutralTemperature; *tint = neutralTint; }
            else                 { *t = wrongTemperature; *tint = wrongTint; }
        };
    }
};

LookAssistStats daylightWithPrior( int temperature, int tint )
{
    LookAssistStats day = daylightFixture();
    lookAssistSetAsShotWhiteBalance( &day, true, temperature, tint );
    return day;
}

LookAssistWhiteBalanceRequest noPatchRequest( const LookAssistStats *stats, const DeckScene &scene )
{
    LookAssistWhiteBalanceRequest request;
    request.stats = stats;
    request.scene = LookAssistScene::Shade;
    request.solvedOnProcessedPicture = false;   // the base picture had no neutral samples: no patch was found
    request.baseTemperature = 6000;
    request.baseTint = 0;
    request.rawWidth = scene.width * scene.downscale;
    request.rawHeight = scene.height * scene.downscale;
    return request;
}

} // namespace

TEST(LookAssistScene, DaylightWithoutATrustedPatchIsBalancedFromTheRenderedPicture)
{
    // The as-shot prior is a blue start (4200 K): the deck is far from neutral (no neutral samples at all),
    // so the search has to MOVE the picture before any patch exists.
    DeckScene scene;
    const LookAssistStats day = daylightWithPrior( 4200, 0 );
    LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
    request.renderBalance = scene.renderer();
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );

    ASSERT_TRUE( r.refineAttempted );
    ASSERT_TRUE( r.refined );
    ASSERT_TRUE( r.refinePatchAcquired );
    ASSERT_TRUE( r.autoValid );
    ASSERT_TRUE( r.source == QStringLiteral("rendered-neutral-patch") );
    ASSERT_TRUE( r.decision == QStringLiteral("accepted") );
    // The solver was asked about the DECK (the only neutral surface), exactly once, and its answer stands.
    ASSERT_EQ( 1, scene.solveCalls );
    ASSERT_TRUE( scene.onDeck( scene.solveRawX / scene.downscale, scene.solveRawY / scene.downscale ) );
    ASSERT_EQ( 6540, r.temperature );
    ASSERT_EQ( -20, r.tint );
    ASSERT_EQ( r.temperature, 6000 + preset.temperatureDelta );   // the preset describes what is applied
    ASSERT_EQ( r.tint, preset.tintDelta );
    // Bounded: the start picture, a few probes, one verification.
    ASSERT_TRUE( r.refineRenders >= 3 && r.refineRenders <= 9 );
    ASSERT_TRUE( r.refineFinalPatchChroma <= 1.5 );          // the deck at the result is neutral
    ASSERT_TRUE( r.refineStartPatchChroma > r.refineFinalPatchChroma );
    ASSERT_TRUE( r.refineScore < r.refineStartScore );        // the picture measures better there
}

TEST(LookAssistScene, TheRefinementReadsTheRenderedPictureNotTheRawThumbnail)
{
    // The same scene with the as-shot prior already near the deck's balance: the FIRST picture has neutral
    // samples, so the patch is taken from it (no probing) and verified. Either way the answer is the solver's.
    DeckScene scene;
    const LookAssistStats day = daylightWithPrior( 6400, -15 );
    LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
    request.renderBalance = scene.renderer();
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
    ASSERT_TRUE( r.refinePatchAcquired );
    ASSERT_EQ( 2, scene.renders );          // the start picture and the verification: no probe was needed
    ASSERT_EQ( 6540, r.temperature );
    ASSERT_EQ( -20, r.tint );
}

TEST(LookAssistScene, TheRefinementNeverEndsWorseThanItsStartAndStaysInTheWindow)
{
    // No neutral surface anywhere (all water): nothing can be acquired, and moving the white balance does not
    // improve the picture's score. The result is the as-shot prior -- not a drift, not a guess.
    DeckScene scene;
    scene.hasDeck = false;
    const LookAssistStats day = daylightWithPrior( 7000, 0 );
    LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
    request.renderBalance = scene.renderer();
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
    ASSERT_TRUE( r.refineAttempted );
    ASSERT_FALSE( r.refined );
    ASSERT_FALSE( r.autoValid );
    ASSERT_TRUE( r.source == QStringLiteral("as-shot-prior") );
    ASSERT_EQ( 7000, r.temperature );
    ASSERT_EQ( 0, r.tint );
    ASSERT_EQ( 0, scene.solveCalls );
    ASSERT_TRUE( r.refineRenders <= 8 );
    ASSERT_NEAR( r.refineStartScore, r.refineScore, 1e-9 );   // never measured worse than the start
}

TEST(LookAssistScene, ARefinedBalanceNeverLeavesTheDaylightWindowAndIsVerifiedByTheRender)
{
    // (a) the solver answers a tungsten balance (3000 K / +30) for the deck: clamped into the window, and the
    // verification render (the deck is NOT neutral there) rejects it -- the as-shot prior's region stands.
    {
        DeckScene scene;
        const LookAssistStats day = daylightWithPrior( 4200, 0 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = scene.renderer();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
            request, [&]( int, int, int *t, int *tint ) { ++scene.solveCalls; *t = 3000; *tint = 30; }, &preset );
        ASSERT_FALSE( r.refinePatchAcquired );
        ASSERT_TRUE( r.source != QStringLiteral("rendered-neutral-patch") );
        ASSERT_TRUE( r.temperature >= 4800 && r.temperature <= 10000 );
        ASSERT_TRUE( r.tint >= -35 && r.tint <= 10 );
    }
    // (b) the solver answers a balance at which the deck renders CAST (9990 K / -20 on a 6540 K deck): the
    // picture at the solution is checked, found worse than where the patch was found, and the answer is refused.
    {
        DeckScene scene;
        const LookAssistStats day = daylightWithPrior( 4200, 0 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = scene.renderer();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
            request, [&]( int, int, int *t, int *tint ) { *t = 9990; *tint = -20; }, &preset );
        ASSERT_FALSE( r.refinePatchAcquired );
        ASSERT_TRUE( r.temperature != 9990 );
    }
    // (c) the solver answers a balance whose picture still has a valid, near-neutral patch -- but a MORE cast
    // one (chroma ~9) than the patch it was solved from (chroma ~2): only the comparison with where the patch
    // was found can refuse it. The picture is the judge, not the solver's say-so.
    {
        DeckScene scene;
        const LookAssistStats day = daylightWithPrior( 6400, -15 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = scene.renderer();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
            request, [&]( int, int, int *t, int *tint ) { *t = 5850; *tint = -20; }, &preset );
        ASSERT_FALSE( r.refinePatchAcquired );
        ASSERT_TRUE( r.refineStartPatchChroma > 0.0 );
        ASSERT_TRUE( r.refineFinalPatchChroma > r.refineStartPatchChroma + 0.75 );
        ASSERT_TRUE( r.temperature != 5850 );
    }
}

TEST(LookAssistScene, TheRefinementRunsOnlyWhereItIsMeantTo)
{
    DeckScene scene;
    // No renderer: the as-shot prior stands exactly as before, nothing is rendered.
    {
        const LookAssistStats day = daylightWithPrior( 7000, 0 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_FALSE( r.refineAttempted );
        ASSERT_EQ( 0, scene.renders );
        ASSERT_TRUE( r.source == QStringLiteral("as-shot-prior") );
        ASSERT_EQ( 7000, r.temperature );
    }
    // Night / not corroborated daylight: master's behaviour, nothing rendered.
    {
        const LookAssistStats night = fixtureRawStats();
        LookAssistWhiteBalanceRequest request = noPatchRequest( &night, scene );
        request.scene = LookAssistScene::Night;
        request.renderBalance = scene.renderer();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Night, night );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_FALSE( r.refineAttempted );
        ASSERT_EQ( 0, scene.renders );
        LookAssistStats uncorroborated = withEv( fixtureRawStats(), 100, 465, 560 );   // exposure says daylight, picture does not
        request.stats = &uncorroborated;
        request.scene = LookAssistScene::Night;
        preset = presetForLookAssistScene( LookAssistScene::Night, uncorroborated );
        resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_EQ( 0, scene.renders );
    }
    // A trusted patch was found and accepted: that decision stands, nothing is rendered.
    {
        const LookAssistStats day = daylightWithPrior( 7000, 0 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.solvedOnProcessedPicture = true;
        request.patch.valid = true; request.patch.luma = 205.0; request.patch.chroma = 12.0; request.patch.blueAmberAxis = 12.0;
        request.renderBalance = scene.renderer();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
            request, []( int, int, int *t, int *tint ) { *t = 9990; *tint = -35; }, &preset );
        ASSERT_TRUE( r.autoValid );
        ASSERT_FALSE( r.refineAttempted );
        ASSERT_EQ( 0, scene.renders );
        ASSERT_EQ( 9990, r.temperature );
    }
    // A renderer that fails: the prior stands.
    {
        const LookAssistStats day = daylightWithPrior( 7000, 0 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = []( double, int, int, LookAssistRenderedPicture * ) { return false; };
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_FALSE( r.refineAttempted );
        ASSERT_TRUE( r.source == QStringLiteral("as-shot-prior") );
        ASSERT_EQ( 7000, r.temperature );
    }
}

TEST(LookAssistScene, TheNarrowingSwitchIsTheOnlyGateInFrontOfTheRefinement)
{
    // The hub's pre-committed narrowing exit: one constant. This pins that it is on and that nothing else
    // gates the refinement (the analysis uses it in one predicate; the GUI only to route daylight to sync).
    ASSERT_TRUE( kLookAssistRefineDaylightWithoutPatch );
    const QString analysis = readRepoFile( QStringLiteral("src/batch/LookAssistAnalysis.cpp") );
    ASSERT_EQ( 1, analysis.count( QStringLiteral("kLookAssistRefineDaylightWithoutPatch") ) );
    const QString window = readRepoFile( QStringLiteral("platform/qt/MainWindow.cpp") );
    ASSERT_EQ( 1, window.count( QStringLiteral("kLookAssistRefineDaylightWithoutPatch") ) );
}

TEST(LookAssistScene, EveryConsumerReachesTheRefinementThroughTheOneDecision)
{
    // GUI sync and headless hand the shared decision a renderer (one definition, ReceiptApplier); the GUI's
    // async worker never renders -- a corroborated daylight scene is sent down the synchronous path BEFORE the
    // async dispatch, so sync and async cannot disagree. (Measured: the worker's isolated render is a different
    // picture from the live one, 25 against 62 on the same look.)
    const QString applier = readRepoFile( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    const QString window = readRepoFile( QStringLiteral("platform/qt/MainWindow.cpp") );
    ASSERT_FALSE( applier.isEmpty() );
    ASSERT_FALSE( window.isEmpty() );
    ASSERT_TRUE( applier.contains( QStringLiteral("wbRequest.renderBalance = lookAssistBalanceRenderer(") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("wbRequest.renderBalance = ReceiptApplier::lookAssistBalanceRenderer(") ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("wbRequest.renderBalance =") ) );   // sync only
    const int guard = window.indexOf( QStringLiteral("const bool daylightNeedsLivePicture") );
    const int dispatch = window.indexOf( QStringLiteral("if( !s_syncMode && !daylightNeedsLivePicture )") );
    ASSERT_TRUE( guard > 0 );
    ASSERT_TRUE( dispatch > guard );
    const int worker = window.indexOf( QStringLiteral("std::thread([this,"), dispatch );
    const int workerEnd = window.indexOf( QStringLiteral("}).detach();"), worker );
    ASSERT_TRUE( worker > dispatch && workerEnd > worker );
    ASSERT_FALSE( window.mid( worker, workerEnd - worker ).contains( QStringLiteral("renderBalance") ) );
    ASSERT_FALSE( window.mid( worker, workerEnd - worker ).contains( QStringLiteral("lookAssistBalanceRenderer") ) );
}
