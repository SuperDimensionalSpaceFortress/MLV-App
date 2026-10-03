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
#include <QStringList>
#include <QTextStream>

#include <cmath>
#include <cstdio>
#include <memory>
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
    // result is MASTER's balance (its colour-balance default, inside the daylight window), NOT the rail.
    LookAssistStats day = daylightFixture();
    lookAssistSetAsShotWhiteBalance( &day, true, 6000, 0 );
    LookAssistWhiteBalanceRequest request;
    request.stats = &day; request.scene = LookAssistScene::Shade; request.patch = blue;
    request.solvedOnProcessedPicture = true; request.baseTemperature = 6000; request.baseTint = 0;
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistPreset master = preset;
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
        request, []( int, int, int *t, int *tint ) { *t = 9990; *tint = -35; }, &preset );
    ASSERT_FALSE( r.autoValid );
    ASSERT_TRUE( r.legacyBalance );
    ASSERT_TRUE( r.decision == QStringLiteral("legacy") );
    ASSERT_TRUE( r.source == QStringLiteral("master-balance") );
    const int masterT = qBound( 4800, 6000 + master.temperatureDelta, 10000 );
    const int masterN = qBound( -35, master.tintDelta, 10 );
    ASSERT_EQ( masterT, r.temperature );
    ASSERT_EQ( masterN, r.tint );
    ASSERT_EQ( master.temperatureDelta, preset.temperatureDelta );   // the preset is master's, untouched
    ASSERT_EQ( master.tintDelta, preset.tintDelta );
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

    // The deck itself, with nothing to verify it by (no renderer): an undamped daylight solve is believed only for
    // a surface verified at the as-shot balance and at the solution (LOOK-ASSIST-SCENE-CLASSIFY-3), so this
    // clip takes master's path -- the accepted-undamped case is pinned below through the real renderer shape.
    LookAssistWhiteBalanceRequest deck;
    deck.stats = &day; deck.scene = LookAssistScene::Shade; deck.solvedOnProcessedPicture = true;
    deck.patch.valid = true; deck.patch.luma = 205.0; deck.patch.chroma = 12.0; deck.patch.blueAmberAxis = 12.0;
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
        deck, []( int, int, int *t, int *tint ) { *t = 9990; *tint = -35; }, &preset );
    ASSERT_FALSE( r.autoValid );
    ASSERT_TRUE( r.legacyBalance );
    ASSERT_TRUE( r.initialPatchRefused );
    ASSERT_TRUE( r.temperature != 9990 );
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


// ---- LOOK-ASSIST-SCENE-CLASSIFY-2: corroborated daylight WITHOUT a trusted patch ----
//
// PR #221 r2 left such a clip on the as-shot prior (6000 K / tint 0 in the real app's profile-settle state:
// deck chroma 18.9 against master's 13.5). The refinement renders the picture at the white balance under test,
// steps it until it has neutral samples, runs the SAME patch search + solver + guards on it, and verifies by
// rendering at the solution. Round 2 (fable's blocker): every guard judges what the SURFACE IS, so a candidate
// surface must pass the near-neutral / not-blue-locus guard on the unstepped BASE picture too, and the
// verification measures that SAME surface. With nothing acquired the balance is MASTER's, not the prior.
//
// These tests drive resolveLookAssistWhiteBalance end to end over a synthetic scene whose rendered colours follow
// the white balance the way a real picture does -- EVERY surface, the water and sky included, moves with it (the
// r1 model painted the water a fixed 70/110/170, which hid the blocker). The renderer is the one the real
// consumers pass (GUI sync and headless both hand over renderBalance).
namespace
{

struct DeckScene
{
    int neutralTemperature = 6540;     // the white balance at which the deck renders neutral
    int neutralTint = -20;   // not on the -35 rail: a mid-tone near-neutral patch solved onto the rail is distrusted by design
    bool hasDeck = true;
    bool hasBlueSurface = false;   // a pale-blue pool: physically B-R +32 at the neutral white balance
    int width = 80;
    int height = 60;
    int downscale = 4;
    mutable int renders = 0;
    mutable int solveCalls = 0;
    mutable int solveRawX = -1;
    mutable int solveRawY = -1;
    // How a cast moves with the white balance: blue-amber per mired, green per tint unit. It moves EVERY surface.
    double blueAmberPerMired = 0.5;
    double greenPerTint = -0.15;

    bool onDeck( int x, int y ) const { return hasDeck && x >= 10 && x < 50 && y >= 30 && y < 45; }
    bool onBlue( int x, int y ) const { return hasBlueSurface && x >= 55 && x < 75 && y >= 5 && y < 25; }

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
                    // The surface's own colour (physical, the same at every white balance) ...
                    double r = 70.0, g = 110.0, b = 170.0;                          // water / sky: blue
                    if( onDeck( x, y ) )      { r = 140.0; g = 140.0; b = 140.0; }  // concrete: neutral
                    else if( onBlue( x, y ) ) { r = 164.0; g = 180.0; b = 196.0; }  // pale-blue pool: B-R +32
                    // ... and the cast the white balance under test puts on it.
                    p[0] = clamp( r - blueAmber / 2.0 );
                    p[1] = clamp( g + green );
                    p[2] = clamp( b + blueAmber / 2.0 );
                }
            out->stats = analyzeLookAssistThumbnail( out->rgb.data(), width, height );
            return true;
        };
    }

    // The solver answers the white balance that makes the surface it is asked about neutral.
    LookAssistWhiteBalanceSolveFn solver( int wrongTemperature = 3000, int wrongTint = 30 ) const
    {
        return [this, wrongTemperature, wrongTint]( int rawX, int rawY, int *t, int *tint )
        {
            ++solveCalls;
            solveRawX = rawX;
            solveRawY = rawY;
            const int x = rawX / downscale;
            const int y = rawY / downscale;
            if( onDeck( x, y ) )      { *t = neutralTemperature; *tint = neutralTint; }
            else if( onBlue( x, y ) ) { *t = 12000; *tint = -60; }   // a pale-blue surface "neutralised": the rail
            else                      { *t = wrongTemperature; *tint = wrongTint; }
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

// What master applies for this clip: the colour-balance default of the preset, clamped like the receipt.
int masterTemperature( const LookAssistStats &stats, int baseTemperature )
{
    const LookAssistPreset master = presetForLookAssistScene( LookAssistScene::Shade, stats );
    return qBound( 4800, baseTemperature + master.temperatureDelta, 10000 );
}

int masterTint( const LookAssistStats &stats, int baseTint )
{
    const LookAssistPreset master = presetForLookAssistScene( LookAssistScene::Shade, stats );
    return qBound( -35, baseTint + master.tintDelta, 10 );
}

// A picture painted from a function of the pixel: the renderers of the hand-built scenes below.
template<class F>
void paintPicture( LookAssistRenderedPicture *out, int width, int height, int downscale, F colourAt )
{
    out->width = width;
    out->height = height;
    out->downscaleFactor = downscale;
    out->rgb.assign( static_cast<size_t>( width ) * height * 3, 0 );
    for( int y = 0; y < height; ++y )
        for( int x = 0; x < width; ++x )
        {
            const std::vector<int> c = colourAt( x, y );
            for( int k = 0; k < 3; ++k )
                out->rgb[( static_cast<size_t>( y ) * width + x ) * 3 + k] = static_cast<unsigned char>( qBound( 0, c[k], 255 ) );
        }
    out->stats = analyzeLookAssistThumbnail( out->rgb.data(), width, height );
}

// The request a consumer builds when the picture rendered at the EXISTING processing white balance has a neutral
// patch: the shared patch search ran on that picture, the renderer is the one the real consumers pass.
LookAssistWhiteBalanceRequest initialPatchRequest( const LookAssistStats *stats, const DeckScene &scene,
                                                   int existingTemperature, int existingTint )
{
    LookAssistWhiteBalanceRequest request = noPatchRequest( stats, scene );
    request.solvedOnProcessedPicture = true;
    request.renderBalance = scene.renderer();
    LookAssistRenderedPicture existing;
    scene.renderer()( 0.0, existingTemperature, existingTint, &existing );
    request.patch = findLookAssistAutoWhiteBalancePatch( existing.rgb.data(), existing.width, existing.height,
                                                         existing.downscaleFactor, request.rawWidth, request.rawHeight );
    return request;
}

// What the consumers' MASTER PASS computes for the same input: the clip is re-analysed with the recorded-exposure
// daylight hypothesis off (the scene is then master's own verdict, so the solve is the damped one).
LookAssistWhiteBalanceResolution masterPassResolution( const LookAssistWhiteBalanceRequest &daylightRequest,
                                                       const LookAssistStats &daylightStats,
                                                       const LookAssistWhiteBalanceSolveFn &solver,
                                                       LookAssistPreset *preset )
{
    LookAssistStats master = daylightStats;
    master.daylightPictureEvidence = false;
    const LookAssistScene masterScene = classifyLookAssistScene( master );
    LookAssistWhiteBalanceRequest request = daylightRequest;
    request.stats = &master;
    request.scene = masterScene;
    *preset = presetForLookAssistScene( masterScene, master );
    return resolveLookAssistWhiteBalance( request, solver, preset );
}

void expectMastersBalance( const LookAssistWhiteBalanceResolution &r, const LookAssistStats &stats, int baseTemperature, int baseTint )
{
    ASSERT_FALSE( r.autoValid );
    ASSERT_FALSE( r.refined );
    ASSERT_FALSE( r.refinePatchAcquired );
    ASSERT_TRUE( r.legacyBalance );
    ASSERT_TRUE( r.source == QStringLiteral("master-balance") );
    ASSERT_TRUE( r.decision == QStringLiteral("legacy") );
    ASSERT_EQ( masterTemperature( stats, baseTemperature ), r.temperature );
    ASSERT_EQ( masterTint( stats, baseTint ), r.tint );
}

} // namespace

TEST(LookAssistScene, ADaylightSurfaceThatIsNeutralAtBaseIsAcquiredAndVerified)
{
    // The as-shot prior is close to the deck's balance: the deck is a near-neutral surface in the BASE picture
    // already, so it is acquired from it (no probing), solved, and verified at the solution.
    DeckScene scene;
    const LookAssistStats day = daylightWithPrior( 6300, -15 );
    LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
    request.renderBalance = scene.renderer();
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );

    ASSERT_TRUE( r.refineAttempted );
    ASSERT_TRUE( r.refined );
    ASSERT_TRUE( r.refinePatchAcquired );
    ASSERT_FALSE( r.legacyBalance );
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
    ASSERT_EQ( 2, scene.renders );                // the base picture and the verification: no probe was needed
    ASSERT_TRUE( r.refineFinalPatchChroma <= 1.5 );          // that same deck at the result is neutral
    ASSERT_TRUE( r.refineStartPatchChroma >= r.refineFinalPatchChroma );
    ASSERT_FALSE( r.refineRefusedAtBase );
}

TEST(LookAssistScene, ASurfaceThatIsBlueAtBaseIsNeverAcquiredHoweverNeutralItLooksAfterStepping)
{
    // fable's hand-traced case (PR #222 key r1), now RUN, through the renderer the real consumers pass and a
    // scene in which everything moves with the white balance. The surface is physically neutral only at 9800 K;
    // at the as-shot 6000 K it renders 124/137/156 (B-R +32, past the guard's 20). Stepping warms the picture
    // and after one probe (7895 K) the same surface renders 134/139/146 -- near-neutral, passing the guard on
    // the STEPPED picture. It must not be believed: it is blue at BASE.
    DeckScene scene;
    scene.neutralTemperature = 9800;
    const LookAssistStats day = daylightWithPrior( 6000, 0 );
    LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
    request.renderBalance = scene.renderer();
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    LookAssistRenderedPicture base;
    scene.renderer()( 0.0, 6000, 0, &base );
    ASSERT_EQ( 124, base.rgb[( 35 * scene.width + 20 ) * 3 + 0] );   // the hand trace: 124/137/156 at 6000 K
    ASSERT_EQ( 137, base.rgb[( 35 * scene.width + 20 ) * 3 + 1] );
    ASSERT_EQ( 156, base.rgb[( 35 * scene.width + 20 ) * 3 + 2] );
    LookAssistRenderedPicture stepped;
    scene.renderer()( 0.0, 7895, -12, &stepped );
    const LookAssistAutoWhiteBalancePatch steppedPatch = findLookAssistAutoWhiteBalancePatch(
        stepped.rgb.data(), stepped.width, stepped.height, stepped.downscaleFactor,
        request.rawWidth, request.rawHeight );
    ASSERT_TRUE( steppedPatch.valid );
    ASSERT_TRUE( lookAssistDaylightPatchIsNeutralEnough( steppedPatch ) );   // what the r1 refinement believed

    scene.renders = 0;
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
    ASSERT_TRUE( r.refineAttempted );
    ASSERT_TRUE( r.refineRefusedAtBase );
    ASSERT_TRUE( r.refineBaseSurfaceBlueAmber > 20.0 );     // measured at BASE: past the guard
    ASSERT_EQ( 0, scene.solveCalls );                       // the solver is never asked about it
    ASSERT_TRUE( scene.renders <= 3 );                      // and the walk stops rendering once it is refused
    expectMastersBalance( r, day, 6000, 0 );
    ASSERT_TRUE( r.temperature < 9800 );
}

TEST(LookAssistScene, APaleBlueSurfaceWithNoNeutralOneIsNotWalkedToTheRail)
{
    // No neutral surface at all: water, sky and a pale-blue pool (B-R +32 whatever the white balance). Stepping
    // towards the warm end of the window makes the pool near-neutral; its solver answer is the 10000 K / -35 rail.
    // The pool is blue at base, so it is never acquired and the clip keeps master's balance.
    for( int priorTemperature : { 5200, 6000, 7000 } )
    {
        DeckScene scene;
        scene.hasDeck = false;
        scene.hasBlueSurface = true;
        const LookAssistStats day = daylightWithPrior( priorTemperature, 0 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = scene.renderer();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_TRUE( r.refineAttempted );
        ASSERT_EQ( 0, scene.solveCalls );
        expectMastersBalance( r, day, 6000, 0 );
    }
}

TEST(LookAssistScene, TheVerificationMeasuresTheSameSurfaceNotTheBestPatchAnywhere)
{
    // sol H2. At the solution a DIFFERENT surface (B) is the best patch and is perfectly neutral, while the
    // surface that was acquired and solved (A) has gone to a cast of 18. Comparing independently selected best
    // patches accepts that (0 <= 2 + slack); measuring A itself refuses it.
    const int W = 80, H = 60, D = 4;
    auto surfaceA = []( int x, int y ) { return x >= 10 && x < 50 && y >= 30 && y < 45; };
    auto surfaceB = []( int x, int y ) { return x >= 55 && x < 75 && y >= 30 && y < 45; };
    auto picture = [&]( bool atSolution, LookAssistRenderedPicture *out )
    {
        paintPicture( out, W, H, D, [&]( int x, int y ) -> std::vector<int> {
            if( surfaceA( x, y ) ) return atSolution ? std::vector<int>{ 130, 140, 148 } : std::vector<int>{ 140, 140, 142 };
            if( surfaceB( x, y ) ) return atSolution ? std::vector<int>{ 145, 145, 145 } : std::vector<int>{ 120, 124, 128 };
            return std::vector<int>{ 70, 110, 170 };
        } );
    };
    LookAssistRenderBalanceFn renderer = [&]( double, int t, int tint, LookAssistRenderedPicture *out ) -> bool
    {
        if( t == 6000 && tint == 0 ) picture( false, out );               // the base picture
        else if( t == 7500 && tint == -5 ) picture( true, out );          // the picture at the solver's answer
        else paintPicture( out, W, H, D, []( int, int ) { return std::vector<int>{ 70, 110, 170 }; } );   // a probe
        return true;
    };
    // The old comparison would have accepted: B is the best patch of the solution picture and is neutral.
    LookAssistRenderedPicture solution;
    picture( true, &solution );
    const LookAssistAutoWhiteBalancePatch best = findLookAssistAutoWhiteBalancePatch(
        solution.rgb.data(), W, H, D, W * D, H * D );
    ASSERT_TRUE( best.valid );
    ASSERT_TRUE( surfaceB( best.thumbnailX, best.thumbnailY ) );
    ASSERT_TRUE( lookAssistDaylightPatchIsNeutralEnough( best ) );

    const LookAssistStats day = daylightWithPrior( 6000, 0 );
    DeckScene geometry;
    LookAssistWhiteBalanceRequest request = noPatchRequest( &day, geometry );
    request.renderBalance = renderer;
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    int solveCalls = 0;
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
        request, [&]( int, int, int *t, int *tint ) { ++solveCalls; *t = 7500; *tint = -5; }, &preset );
    ASSERT_TRUE( solveCalls >= 1 );                          // surface A WAS acquired at base and solved ...
    ASSERT_TRUE( r.refineStartPatchChroma <= 3.0 );
    ASSERT_TRUE( r.refineFinalPatchChroma >= 17.0 );         // ... and measured itself at the solution: cast 18
    expectMastersBalance( r, day, 6000, 0 );                 // refused: master's balance
}

TEST(LookAssistScene, AnAcquisitionIsRefusedWhenItsSolutionLeavesTheSurfaceCastOrOutsideTheWindow)
{
    // (a) the solver answers a tungsten balance (3000 K / +30) for the deck: clamped into the window, and the
    // verification render (the deck is NOT neutral there) refuses it.
    {
        DeckScene scene;
        const LookAssistStats day = daylightWithPrior( 6300, -15 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = scene.renderer();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
            request, [&]( int, int, int *t, int *tint ) { ++scene.solveCalls; *t = 3000; *tint = 30; }, &preset );
        ASSERT_FALSE( r.refinePatchAcquired );
        ASSERT_TRUE( r.source != QStringLiteral("rendered-neutral-patch") );
        ASSERT_TRUE( r.temperature >= 4800 && r.temperature <= 10000 );
        ASSERT_TRUE( r.tint >= -35 && r.tint <= 10 );
        expectMastersBalance( r, day, 6000, 0 );
    }
    // (b) the solver answers a balance at which the deck renders CAST (9990 K / -20 on a 6540 K deck).
    {
        DeckScene scene;
        const LookAssistStats day = daylightWithPrior( 6300, -15 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = scene.renderer();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
            request, [&]( int, int, int *t, int *tint ) { *t = 9990; *tint = -20; }, &preset );
        ASSERT_FALSE( r.refinePatchAcquired );
        ASSERT_TRUE( r.temperature != 9990 );
        expectMastersBalance( r, day, 6000, 0 );
    }
    // (c) the solver answers a balance whose picture still has the deck near-neutral -- but MORE cast (chroma ~9)
    // than where it was found (chroma ~2): only the comparison with where the surface was found can refuse it.
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
        expectMastersBalance( r, day, 6000, 0 );
    }
}

TEST(LookAssistScene, WhateverTheSceneNothingAcquiredMeansMastersBalance)
{
    // The class rule: never worse than master. Over scenes with no neutral surface, only a blue one, a deck that
    // is blue at base, and an all-water frame, over a range of as-shot priors, every clip that acquires nothing
    // lands exactly on master's balance (inside the daylight window), never on the prior and never on a rail.
    for( int priorTemperature : { 4200, 5000, 6000, 7000 } )
        for( int priorTint : { -10, 0, 8 } )
            for( int variant = 0; variant < 3; ++variant )
            {
                DeckScene scene;
                if( variant == 0 ) scene.hasDeck = false;                              // all water
                if( variant == 1 ) { scene.hasDeck = false; scene.hasBlueSurface = true; }   // water + pale-blue pool
                if( variant == 2 ) scene.neutralTemperature = 9800;                    // a deck blue at the as-shot
                const LookAssistStats day = daylightWithPrior( priorTemperature, priorTint );
                LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
                request.renderBalance = scene.renderer();
                LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
                const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
                ASSERT_TRUE( r.refineAttempted );
                ASSERT_TRUE( r.refineRenders <= 9 );
                ASSERT_EQ( 0, scene.solveCalls );
                expectMastersBalance( r, day, 6000, 0 );
                ASSERT_TRUE( r.temperature >= 4800 && r.temperature <= 10000 );
            }
}

TEST(LookAssistScene, TheRefinementRunsOnlyWhereItIsMeantTo)
{
    DeckScene scene;
    // No renderer: nothing can be refined and nothing is rendered; the balance is master's (never the prior).
    {
        const LookAssistStats day = daylightWithPrior( 7000, 0 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_FALSE( r.refineAttempted );
        ASSERT_EQ( 0, scene.renders );
        expectMastersBalance( r, day, 6000, 0 );
    }
    // Night / not corroborated daylight: master's behaviour, nothing rendered, no legacy flag (it never left master).
    {
        const LookAssistStats night = fixtureRawStats();
        LookAssistWhiteBalanceRequest request = noPatchRequest( &night, scene );
        request.scene = LookAssistScene::Night;
        request.renderBalance = scene.renderer();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Night, night );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_FALSE( r.refineAttempted );
        ASSERT_FALSE( r.legacyBalance );
        ASSERT_EQ( 0, scene.renders );
        LookAssistStats uncorroborated = withEv( fixtureRawStats(), 100, 465, 560 );   // exposure says daylight, picture does not
        request.stats = &uncorroborated;
        request.scene = LookAssistScene::Night;
        preset = presetForLookAssistScene( LookAssistScene::Night, uncorroborated );
        resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_EQ( 0, scene.renders );
    }
    // A trusted patch was found and accepted: the refinement (which looks for a patch) does not run, and the
    // patch is verified on its own surface (LOOK-ASSIST-SCENE-CLASSIFY-3): the base picture and the solution.
    {
        const LookAssistStats day = daylightWithPrior( 6300, -15 );
        LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, scene, 6300, -15 );
        ASSERT_TRUE( request.patch.valid );
        scene.renders = 0;
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_TRUE( r.autoValid );
        ASSERT_FALSE( r.legacyBalance );
        ASSERT_FALSE( r.refineAttempted );
        ASSERT_EQ( 2, scene.renders );   // the as-shot base picture and the verification at the solution
        ASSERT_EQ( 6540, r.temperature );
    }
    // A renderer that fails: master's balance.
    {
        const LookAssistStats day = daylightWithPrior( 7000, 0 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = []( double, int, int, LookAssistRenderedPicture * ) { return false; };
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_FALSE( r.refineAttempted );
        expectMastersBalance( r, day, 6000, 0 );
    }
}

TEST(LookAssistScene, TheNarrowingSwitchRestoresMastersBalance)
{
    // sol H4 / fable: switching the refinement off must give MASTER's balance, not the as-shot prior. A scene in
    // which the refinement WOULD acquire the deck (6540 K / -20): off, nothing is rendered or solved and the
    // receipt is what master computes (its colour-balance default; the GUI then runs master's post-balance walk).
    for( int priorTemperature : { 4200, 6000, 6300, 7000 } )
    {
        DeckScene scene;
        const LookAssistStats day = daylightWithPrior( priorTemperature, 0 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = scene.renderer();
        request.refineWithoutPatch = false;
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistPreset master = preset;
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_FALSE( r.refineAttempted );
        ASSERT_EQ( 0, scene.renders );
        ASSERT_EQ( 0, scene.solveCalls );
        expectMastersBalance( r, day, 6000, 0 );
        ASSERT_EQ( master.temperatureDelta, preset.temperatureDelta );
        ASSERT_EQ( master.tintDelta, preset.tintDelta );
    }
    // On: the same clip (prior near the deck) is acquired, so the switch is what decides.
    {
        DeckScene scene;
        const LookAssistStats day = daylightWithPrior( 6300, -15 );
        LookAssistWhiteBalanceRequest request = noPatchRequest( &day, scene );
        request.renderBalance = scene.renderer();
        ASSERT_TRUE( request.refineWithoutPatch == lookAssistRefineDaylightWithoutPatchEnabled() );
        request.refineWithoutPatch = true;
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_TRUE( r.refinePatchAcquired );
        ASSERT_EQ( 6540, r.temperature );
    }
}

// ---- LOOK-ASSIST-SCENE-CLASSIFY-3: the INITIAL patch is judged on the surface too ----
//
// sol, PR #222 r2: the consumers find the initial patch on a picture rendered at the EXISTING processing white
// balance, so a surface that is blue at the as-shot balance but near-neutral under an existing / custom balance
// was accepted undamped (9800 K / -20) with no base guard and no same-surface verification, where master's damped
// solve gives 8470 K / -13. The same two guards the refinement applies now stand in front of that branch; a patch
// that fails either, or cannot be verified, is not believed and the clip takes MASTER's path.
TEST(LookAssistScene, ASurfaceThatIsBlueAtTheAsShotBalanceIsNotAcceptedUndampedFromTheInitialPatch)
{
    // sol's repro, RUN through the renderer the real consumers pass, in the scene where every surface (water and sky
    // too) moves with the white balance. Physically neutral only at 9800 K: 124/137/156 at the as-shot 6000 K
    // (B-R +32), 134/139/146 at the existing 7895 K / -12 balance (chroma 12, B-R +12: it passes the guard THERE).
    DeckScene scene;
    scene.neutralTemperature = 9800;
    const LookAssistStats day = daylightWithPrior( 6000, 0 );
    LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, scene, 7895, -12 );
    ASSERT_TRUE( request.patch.valid );
    ASSERT_TRUE( scene.onDeck( request.patch.thumbnailX, request.patch.thumbnailY ) );
    ASSERT_TRUE( lookAssistDaylightPatchIsNeutralEnough( request.patch ) );          // what the initial branch believed
    ASSERT_TRUE( lookAssistAutoWhiteBalanceSolutionIsStable( request.patch, 6000, 0, 9800, -20, true ) );

    scene.renders = 0;
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
    // Not 9800 K / -20, and not accepted: refused at the as-shot base picture, no second chance from the refinement.
    ASSERT_FALSE( r.autoValid );
    ASSERT_TRUE( r.initialPatchChecked );
    ASSERT_TRUE( r.initialPatchRefused );
    ASSERT_TRUE( r.initialPatchRefusedAtBase );
    ASSERT_TRUE( r.initialPatchBaseBlueAmber > 20.0 );        // measured at BASE: 124/137/156
    ASSERT_FALSE( r.refineAttempted );
    ASSERT_EQ( 1, scene.renders );                             // the base picture only; nothing walked or verified
    ASSERT_EQ( 9800, r.candidateTemperature );                 // reported, never applied
    ASSERT_TRUE( r.temperature != 9800 );
    expectMastersBalance( r, day, 6000, 0 );                   // legacyBalance: the consumers re-run master's analysis

    // That master pass: the damped solve of the SAME patch, which is what master applied (sol: 8470 K / -13).
    LookAssistPreset masterPreset;
    const LookAssistWhiteBalanceResolution master = masterPassResolution( request, day, scene.solver(), &masterPreset );
    ASSERT_TRUE( master.autoValid );
    ASSERT_TRUE( master.decision == QStringLiteral("accepted-damped") );
    ASSERT_NEAR( 0.65, master.damping, 1e-9 );
    ASSERT_EQ( 8470, master.temperature );
    ASSERT_EQ( -13, master.tint );
    ASSERT_FALSE( master.legacyBalance );
}

TEST(LookAssistScene, AnInitialPatchThatIsNeutralAtTheAsShotBalanceAndAtTheSolutionKeepsTheUndampedSolve)
{
    // The state that IMPROVES on master: a real neutral surface (the deck) found on the initial picture is
    // near-neutral in the as-shot base picture, solved to its neutral balance, and verified on that same surface.
    DeckScene scene;
    const LookAssistStats day = daylightWithPrior( 6300, -15 );
    LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, scene, 7000, -5 );
    ASSERT_TRUE( request.patch.valid );
    scene.renders = 0;
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
    ASSERT_TRUE( r.autoValid );
    ASSERT_TRUE( r.initialPatchChecked );
    ASSERT_FALSE( r.initialPatchRefused );
    ASSERT_FALSE( r.legacyBalance );
    ASSERT_FALSE( r.refineAttempted );
    ASSERT_TRUE( r.source == QStringLiteral("processed-neutral-patch") );
    ASSERT_TRUE( r.decision == QStringLiteral("accepted") );
    ASSERT_NEAR( 1.0, r.damping, 1e-9 );
    ASSERT_EQ( 6540, r.temperature );
    ASSERT_EQ( -20, r.tint );
    ASSERT_EQ( 2, scene.renders );                              // the base picture and the verification
    ASSERT_EQ( r.temperature, 6000 + preset.temperatureDelta );
    ASSERT_TRUE( r.initialPatchFinalChroma <= 1.5 );            // that same deck at the solution is neutral
    ASSERT_TRUE( r.initialPatchBaseChroma <= 10.0 );
}

TEST(LookAssistScene, AnInitialPatchWhoseSolutionLeavesTheSameSurfaceCastIsRefused)
{
    // The base surface is fine, but the solver's answer renders the SAME surface with a cast (a deck of 6540 K
    // solved to 9990 K / -20): the same-surface verification refuses it -- not the base guard.
    DeckScene scene;
    const LookAssistStats day = daylightWithPrior( 6300, -15 );
    LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, scene, 7000, -5 );
    ASSERT_TRUE( request.patch.valid );
    LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
    const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance(
        request, []( int, int, int *t, int *tint ) { *t = 9990; *tint = -20; }, &preset );
    ASSERT_FALSE( r.autoValid );
    ASSERT_TRUE( r.initialPatchRefused );
    ASSERT_FALSE( r.initialPatchRefusedAtBase );
    ASSERT_TRUE( r.initialPatchFinalChroma > request.patch.chroma + 0.75 );   // the deck, at the solution, is cast
    ASSERT_FALSE( r.refineAttempted );
    ASSERT_TRUE( r.temperature != 9990 );
    expectMastersBalance( r, day, 6000, 0 );
}

TEST(LookAssistScene, AnInitialPatchThatCannotBeVerifiedIsMastersNotBelieved)
{
    DeckScene scene;
    const LookAssistStats day = daylightWithPrior( 6300, -15 );
    // (a) no renderer to ask
    {
        LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, scene, 7000, -5 );
        request.renderBalance = LookAssistRenderBalanceFn();
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_TRUE( r.initialPatchRefused );
        ASSERT_FALSE( r.autoValid );
        expectMastersBalance( r, day, 6000, 0 );
    }
    // (b) the narrowing switch is off: master's analysis everywhere, an initial patch included
    {
        LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, scene, 7000, -5 );
        request.refineWithoutPatch = false;
        scene.renders = 0;
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_TRUE( r.initialPatchRefused );
        ASSERT_EQ( 0, scene.renders );
        expectMastersBalance( r, day, 6000, 0 );
    }
    // (c) a renderer that fails
    {
        LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, scene, 7000, -5 );
        request.renderBalance = []( double, int, int, LookAssistRenderedPicture * ) { return false; };
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_TRUE( r.initialPatchRefused );
        expectMastersBalance( r, day, 6000, 0 );
    }
    // (d) no recorded as-shot balance: there is no as-shot base picture to judge the surface in
    {
        const LookAssistStats noPrior = [] { LookAssistStats s = daylightFixture(); lookAssistSetAsShotWhiteBalance( &s, false, 6000, 0 ); return s; }();
        LookAssistWhiteBalanceRequest request = initialPatchRequest( &noPrior, scene, 7000, -5 );
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, noPrior );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_TRUE( r.initialPatchRefused );
        expectMastersBalance( r, noPrior, 6000, 0 );
    }
    // (e) the patch is not a pixel of the picture the renderer returns (a different thumbnail geometry)
    {
        LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, scene, 7000, -5 );
        request.patch.thumbnailX = scene.width + 3;
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_TRUE( r.initialPatchRefused );
        expectMastersBalance( r, day, 6000, 0 );
        request.patch.thumbnailX = 0;   // inside the picture, but rawX does not map from it
        request.patch.rawX = 5;
        preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        ASSERT_TRUE( resolveLookAssistWhiteBalance( request, scene.solver(), &preset ).initialPatchRefused );
    }
}

TEST(LookAssistScene, TheInitialPatchGateLeavesEveryOtherPathAsItWas)
{
    DeckScene scene;
    // A RAW-thumbnail patch (not solved on the processed picture) is damped as before: nothing is rendered for it.
    {
        const LookAssistStats day = daylightWithPrior( 6300, -15 );
        LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, scene, 7000, -5 );
        request.solvedOnProcessedPicture = false;
        scene.renders = 0;
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_FALSE( r.initialPatchChecked );
        ASSERT_EQ( 0, scene.renders );
        ASSERT_TRUE( r.autoValid );
        ASSERT_TRUE( r.source == QStringLiteral("raw-neutral-patch") );
    }
    // Night / not corroborated daylight: master's own damped solve, no guard, nothing rendered.
    {
        LookAssistStats master = daylightWithPrior( 6300, -15 );
        master.daylightPictureEvidence = false;
        const LookAssistScene masterScene = classifyLookAssistScene( master );
        LookAssistWhiteBalanceRequest request = initialPatchRequest( &master, scene, 7000, -5 );
        request.scene = masterScene;
        scene.renders = 0;
        LookAssistPreset preset = presetForLookAssistScene( masterScene, master );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, scene.solver(), &preset );
        ASSERT_FALSE( r.initialPatchChecked );
        ASSERT_EQ( 0, scene.renders );
        ASSERT_TRUE( r.autoValid );
        ASSERT_TRUE( r.source == QStringLiteral("processed-neutral-patch") );
        ASSERT_TRUE( r.damping < 0.999 || r.decision == QStringLiteral("accepted") );
    }
    // A patch the existing guards already reject (a pale-blue surface) still goes on to the refinement, as in #222.
    {
        DeckScene pool;
        pool.hasDeck = false;
        pool.hasBlueSurface = true;
        const LookAssistStats day = daylightWithPrior( 6000, 0 );
        LookAssistWhiteBalanceRequest request = initialPatchRequest( &day, pool, 12000, -60 );
        request.patch.valid = true;   // whatever the search found, it is on the pale-blue surface
        request.patch.thumbnailX = 60; request.patch.thumbnailY = 10;
        request.patch.rawX = 60 * pool.downscale + pool.downscale / 2;
        request.patch.rawY = 10 * pool.downscale + pool.downscale / 2;
        request.patch.luma = 180.0; request.patch.chroma = 32.0; request.patch.blueAmberAxis = 32.0;
        LookAssistPreset preset = presetForLookAssistScene( LookAssistScene::Shade, day );
        const LookAssistWhiteBalanceResolution r = resolveLookAssistWhiteBalance( request, pool.solver(), &preset );
        ASSERT_FALSE( r.initialPatchChecked );   // never "stable": the existing guard answered first
        ASSERT_TRUE( r.refineAttempted );
        ASSERT_FALSE( r.initialPatchRefused );
    }
}

TEST(LookAssistScene, BothConsumersTakeMastersPassForARefusedInitialPatch)
{
    // The consumers need no second branch: a refused initial patch reports legacyBalance, which both of them
    // already turn into the master pass (the clip re-analysed with the recorded-exposure hypothesis off).
    const QString applier = readRepoFile( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    const QString window = readRepoFile( QStringLiteral("platform/qt/MainWindow.cpp") );
    ASSERT_TRUE( window.contains( QStringLiteral("if( wb.legacyBalance && !s_lookAssistMasterScenePass )") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral("if( wb.legacyBalance && !masterScenePass )") ) );
    // The decision lives once, in the shared module (the consumers never judge a patch themselves).
    const QString analysis = readRepoFile( QStringLiteral("src/batch/LookAssistAnalysis.cpp") );
    ASSERT_EQ( 1, analysis.count( QStringLiteral("initialDaylightPatchIsVerified( request,") ) );
    for( const QString &source : { applier, window } )
    {
        ASSERT_FALSE( source.contains( QStringLiteral("initialDaylightPatchIsVerified(") ) );
        ASSERT_TRUE( source.contains( QStringLiteral("resolveLookAssistWhiteBalance(") ) );
    }
    // The async worker still passes no renderer, because a corroborated daylight scene never reaches it.
    ASSERT_TRUE( window.contains( QStringLiteral("lookAssistIsDaylightScene( stats, scene ) || s_lookAssistMasterScenePass") ) );
}

TEST(LookAssistScene, TheNarrowingSwitchIsTheOnlyGateInFrontOfTheRefinement)
{
    // The hub's pre-committed narrowing exit: one constant (and an environment variable that can only turn it
    // off). This pins that it is on and that nothing else gates the refinement (the analysis reads it in one
    // function; the consumers' requests take it from there).
    ASSERT_TRUE( kLookAssistRefineDaylightWithoutPatch );
    const QString analysis = readRepoFile( QStringLiteral("src/batch/LookAssistAnalysis.cpp") );
    ASSERT_EQ( 1, analysis.count( QStringLiteral("kLookAssistRefineDaylightWithoutPatch") ) );
    ASSERT_EQ( 1, analysis.count( QStringLiteral("MLVAPP_LOOK_ASSIST_REFINE_DAYLIGHT") ) );
    const QString window = readRepoFile( QStringLiteral("platform/qt/MainWindow.cpp") );
    // The consumers never name the switch: the request carries it (default: the function).
    ASSERT_EQ( 0, window.count( QStringLiteral("kLookAssistRefineDaylightWithoutPatch") ) );
    ASSERT_EQ( 0, window.count( QStringLiteral("lookAssistRefineDaylightWithoutPatchEnabled()") ) );
    const QString applier = readRepoFile( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    ASSERT_EQ( 0, applier.count( QStringLiteral("kLookAssistRefineDaylightWithoutPatch") ) );
    // A clip that acquired nothing is re-analysed as MASTER analyses it (scene, exposure, colour source,
    // balance and -- GUI -- post-balance walk), in both consumers, and nowhere else.
    ASSERT_TRUE( window.contains( QStringLiteral("if( wb.legacyBalance && !s_lookAssistMasterScenePass )") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("s_lookAssistMasterScenePass ? LookAssistRenderFn() : LookAssistRenderFn( renderProcessed )") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral("if( wb.legacyBalance && !masterScenePass )") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral("masterScenePass ? LookAssistRenderFn() : LookAssistRenderFn( renderProcessed )") ) );
    // The GUI's post-balance walk itself is untouched: a daylight scene is not walked, the master pass is not daylight.
    ASSERT_TRUE( window.contains( QStringLiteral("!daylightScene && ( !autoWhiteBalanceValid || useProcessedColorStats )") ) );
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

// ---------------------------------------------------------------------------------------------------------------
// LOOK-ASSIST-DIAG-LOGGING-1: the decisions that change the picture leave their inputs in the log. Observation only.
// ---------------------------------------------------------------------------------------------------------------
namespace
{

// The recorded-exposure-bright flat floor of the tracked fixture, resolved with a render callback that answers `luma`
// (< 0: the callback fails). Returns the stats AFTER resolveLookAssistScene, as the consumers log them.
LookAssistStats resolvedFlatFloor( const LookAssistStats &raw, int luma, int *renders = nullptr )
{
    LookAssistStats s = raw;
    int count = 0;
    resolveLookAssistScene( &s, [&]( double, LookAssistStats *out ) {
        ++count;
        if( luma < 0 ) return false;
        *out = renderedPicture( 64, 64, [luma]( int x, int y ) { return luma < 0 ? 0 : luma + ( x + 2 * y ) % 24; } );
        return true; } );
    if( renders ) *renders = count;
    return s;
}

QString evText( double iso, double shutterUs, double apertureX100 )
{
    double ev100 = 0.0;
    lookAssistSceneEv100( iso, shutterUs, apertureX100, &ev100 );
    return QString::number( floor( ev100 * 1000.0 ) / 1000.0, 'f', 3 );
}

// The keys of a "k=v k=v ..." field string, in order.
QStringList fieldKeys( const QString &fields )
{
    QStringList keys;
    for( const QString &token : fields.split( QLatin1Char(' '), Qt::SkipEmptyParts ) )
        keys << token.section( QLatin1Char('='), 0, 0 );
    return keys;
}

const QStringList kDecisionFieldKeys = {
    QStringLiteral("has_ev100"), QStringLiteral("ev100"), QStringLiteral("daylight_gate"), QStringLiteral("post_walk_ran"),
    QStringLiteral("post_walk_branch"), QStringLiteral("post_walk_recovery"), QStringLiteral("display_meter_ran"),
    QStringLiteral("playback_scale"), QStringLiteral("ev100_bound"), QStringLiteral("ev100_source"),
    QStringLiteral("surface_search"), QStringLiteral("surface_search_balance") };

} // namespace

TEST(LookAssistScene, DecisionLogNamesTheDaylightGateAndTheExposureInputs)
{
    // (a) The tracked daylight fixture: exposure bright, flat floor, legacy night, the rendered picture corroborates.
    int renders = 0;
    const LookAssistStats daylight = resolvedFlatFloor( withEv( fixtureRawStats(), 100, 465, 560 ), 70, &renders );
    ASSERT_EQ( 1, renders );
    ASSERT_TRUE( daylight.daylightPictureEvidence );
    LookAssistDecisionTrace asked;
    asked.pictureEvidenceAsked = true;
    ASSERT_TRUE( lookAssistDecisionLogFields( daylight, asked )
                 == QStringLiteral("has_ev100=1 ev100=%1 daylight_gate=pass post_walk_ran=0 post_walk_branch=none "
                                   "post_walk_recovery=NA display_meter_ran=0 playback_scale=NA ev100_bound=NA "
                                   "ev100_source=recorded surface_search=not-run surface_search_balance=NA")
                        .arg( evText( 100, 465, 560 ) ) );
    ASSERT_TRUE( evText( 100, 465, 560 ).startsWith( QStringLiteral("16.") ) );

    // (b) No exposure block: has_ev100=0, the EV is NA, the first conjunct is what failed, and no picture was rendered.
    const LookAssistStats noMeta = resolvedFlatFloor( fixtureRawStats(), 70, &renders );
    ASSERT_EQ( 0, renders );
    ASSERT_TRUE( lookAssistDecisionLogFields( noMeta, asked )
                 == QStringLiteral("has_ev100=0 ev100=NA daylight_gate=exposure post_walk_ran=0 post_walk_branch=none "
                                   "post_walk_recovery=NA display_meter_ran=0 playback_scale=NA ev100_bound=NA "
                                   "ev100_source=none surface_search=not-run surface_search_balance=NA") );

    // ND-filter daylight (EV100 8.6): the metadata is there, it is just not daylight-bright -> the same gate.
    const LookAssistStats nd = resolvedFlatFloor( withEv( fixtureRawStats(), 100, 20000, 280 ), 70, &renders );
    ASSERT_EQ( 0, renders );
    ASSERT_TRUE( lookAssistDecisionLogFields( nd, asked ).startsWith(
        QStringLiteral("has_ev100=1 ev100=%1 daylight_gate=exposure ").arg( evText( 100, 20000, 280 ) ) ) );
    ASSERT_TRUE( evText( 100, 20000, 280 ).startsWith( QStringLiteral("8.") ) );

    // Exposure bright but the RAW thumbnail is usable (not a flat floor): the second conjunct.
    const LookAssistStats usable = resolvedFlatFloor( withEv( analyzeFrame( 64, 64, []( int x, int ) {
        return std::vector<int>{ 20 + x / 2, 20 + x / 2, 20 + x / 2 }; } ), 100, 465, 560 ), 70, &renders );
    ASSERT_EQ( 0, renders );
    ASSERT_TRUE( lookAssistDecisionLogFields( usable, asked ).contains( QStringLiteral("daylight_gate=flatfloor ") ) );

    // Exposure bright, flat floor, but the legacy verdict is neither night nor artificial lights (a bright flat floor
    // reads shade): the third conjunct.
    LookAssistStats brightFloor = fixtureRawStats();
    brightFloor.median = 64; brightFloor.p05 = 60; brightFloor.p95 = 68; brightFloor.p99 = 69;
    brightFloor.dynamicRange = 8; brightFloor.medianR = brightFloor.medianG = brightFloor.medianB = 64;
    brightFloor = withEv( brightFloor, 100, 465, 560 );
    ASSERT_TRUE( lookAssistIsFlatFloorRawThumbnail( brightFloor ) );
    ASSERT_TRUE( classifyLookAssistScene( brightFloor ) == LookAssistScene::Shade );
    const LookAssistStats legacyShade = resolvedFlatFloor( brightFloor, 70, &renders );
    ASSERT_EQ( 0, renders );
    ASSERT_TRUE( lookAssistDecisionLogFields( legacyShade, asked ).contains( QStringLiteral("daylight_gate=legacy ") ) );

    // Every non-picture conjunct held and the picture said no (a dark field), or could not say (the render failed):
    // the last conjunct. With no picture asked for (the master pass) it is n/a, not "picture".
    const LookAssistStats darkField = resolvedFlatFloor( withEv( fixtureRawStats(), 100, 465, 560 ), 8, &renders );
    ASSERT_EQ( 1, renders );
    ASSERT_FALSE( darkField.daylightPictureEvidence );
    ASSERT_TRUE( lookAssistDecisionLogFields( darkField, asked ).contains( QStringLiteral("daylight_gate=picture ") ) );
    const LookAssistStats failedRender = resolvedFlatFloor( withEv( fixtureRawStats(), 100, 465, 560 ), -1, &renders );
    ASSERT_EQ( 1, renders );
    ASSERT_TRUE( lookAssistDecisionLogFields( failedRender, asked ).contains( QStringLiteral("daylight_gate=picture ") ) );
    LookAssistStats masterPass = withEv( fixtureRawStats(), 100, 465, 560 );
    resolveLookAssistScene( &masterPass, LookAssistRenderFn() );
    LookAssistDecisionTrace notAsked;   // the default trace is the master pass / headless one
    ASSERT_TRUE( lookAssistDecisionLogFields( masterPass, notAsked ).contains( QStringLiteral("daylight_gate=n/a ") ) );
    // ... while an earlier failing conjunct is still named in the master pass.
    ASSERT_TRUE( lookAssistDecisionLogFields( noMeta, notAsked ).contains( QStringLiteral("daylight_gate=exposure ") ) );
}

TEST(LookAssistScene, DecisionLogRecordsThePostBalanceWalkAndTheDisplayMeter)
{
    // (c) A flat-floor NIGHT verdict (the M16 case: no metadata, exposure gate failed) with the GUI's walk run.
    const LookAssistStats night = resolvedFlatFloor( fixtureRawStats(), 70 );
    ASSERT_TRUE( classifyLookAssistScene( night ) == LookAssistScene::Night );
    LookAssistDecisionTrace trace;
    trace.pictureEvidenceAsked = true;
    trace.playbackScaleFactor = 4;           // the owner legs force scale 4: the display meter does not run there

    // walk entered, nothing moved
    trace.postWalkRan = true;
    ASSERT_TRUE( lookAssistDecisionLogFields( night, trace )
                 == QStringLiteral("has_ev100=0 ev100=NA daylight_gate=exposure post_walk_ran=1 post_walk_branch=none "
                                   "post_walk_recovery=NA display_meter_ran=0 playback_scale=4 ev100_bound=NA "
                                   "ev100_source=none surface_search=not-run surface_search_balance=NA") );
    // the step loop moved it
    trace.postWalkBranch = LookAssistPostWalkBranch::Steps;
    ASSERT_TRUE( lookAssistDecisionLogFields( night, trace ).contains(
        QStringLiteral("post_walk_ran=1 post_walk_branch=steps post_walk_recovery=NA ") ) );
    // the green-artifact cleanup raised the tint
    trace.postWalkBranch = LookAssistPostWalkBranch::Cleanup;
    ASSERT_TRUE( lookAssistDecisionLogFields( night, trace ).contains(
        QStringLiteral("post_walk_ran=1 post_walk_branch=cleanup post_walk_recovery=NA ") ) );
    // the warning-recovery table settled on a pair (the M16 clip: 250 / 22)
    trace.postWalkBranch = LookAssistPostWalkBranch::Recovery;
    trace.recoveryTemperatureDelta = 250;
    trace.recoveryTintDelta = 22;
    ASSERT_TRUE( lookAssistDecisionLogFields( night, trace ).contains(
        QStringLiteral("post_walk_ran=1 post_walk_branch=recovery post_walk_recovery=250/22 display_meter_ran=0 playback_scale=4") ) );
    trace.recoveryTemperatureDelta = -500;
    trace.recoveryTintDelta = -35;
    ASSERT_TRUE( lookAssistDecisionLogFields( night, trace ).contains( QStringLiteral("post_walk_recovery=-500/-35 ") ) );
    // a stale pair is never printed for another branch
    trace.postWalkBranch = LookAssistPostWalkBranch::Steps;
    ASSERT_TRUE( lookAssistDecisionLogFields( night, trace ).contains( QStringLiteral("post_walk_recovery=NA ") ) );

    // the display meter ran at scale 2
    LookAssistDecisionTrace metered;
    metered.displayMeterRan = true;
    metered.playbackScaleFactor = 2;
    ASSERT_TRUE( lookAssistDecisionLogFields( night, metered ).contains(
        QStringLiteral("display_meter_ran=1 playback_scale=2 ev100_bound=") ) );
    // ... and a scale with no meter run, or none known
    metered.displayMeterRan = false;
    metered.playbackScaleFactor = 1;
    ASSERT_TRUE( lookAssistDecisionLogFields( night, metered ).contains(
        QStringLiteral("display_meter_ran=0 playback_scale=1 ev100_bound=") ) );
}

namespace {

// The walk's bookkeeping exactly as MainWindow drives it: steps, then cleanup, then the recovery table.
// stepsMoved / cleanupRaised say whether that branch changed the balance; recoveryAdopted whether the table adopted a
// candidate (recoveryT / recoveryS is the pair the table would have adopted, or the pair left in place).
QString walkFields( bool stepsMoved, bool cleanupRaised, bool recoveryEntered, bool recoveryAdopted,
                    int recoveryT = 250, int recoveryS = 22 )
{
    LookAssistDecisionTrace trace;
    trace.postWalkRan = true;
    lookAssistTraceWalkSteps( &trace, stepsMoved );
    if( cleanupRaised ) lookAssistTraceWalkCleanup( &trace );
    if( recoveryEntered ) lookAssistTraceWalkRecovery( &trace, recoveryAdopted, recoveryT, recoveryS );
    return lookAssistDecisionLogFields( LookAssistStats(), trace );
}

} // namespace

TEST(LookAssistScene, WalkTraceRecordsOnlyTheBranchThatChangedTheBalance)
{
    // The GUI walk calls these same helpers (the wiring test pins that), so this is the walk's branch/pair logic.
    // no change at all
    ASSERT_TRUE( walkFields( false, false, false, false ).contains(
        QStringLiteral("post_walk_ran=1 post_walk_branch=none post_walk_recovery=NA ") ) );
    // steps only
    ASSERT_TRUE( walkFields( true, false, false, false ).contains(
        QStringLiteral("post_walk_branch=steps post_walk_recovery=NA ") ) );
    // steps then the green-artifact cleanup: cleanup is the last branch that changed the balance
    ASSERT_TRUE( walkFields( true, true, false, false ).contains(
        QStringLiteral("post_walk_branch=cleanup post_walk_recovery=NA ") ) );
    // cleanup without a step that moved
    ASSERT_TRUE( walkFields( false, true, false, false ).contains(
        QStringLiteral("post_walk_branch=cleanup post_walk_recovery=NA ") ) );
    // the recovery table ADOPTED a candidate: recovery and that pair, over steps and cleanup
    ASSERT_TRUE( walkFields( true, true, true, true, 250, 22 ).contains(
        QStringLiteral("post_walk_branch=recovery post_walk_recovery=250/22 ") ) );
    ASSERT_TRUE( walkFields( false, false, true, true, -500, -35 ).contains(
        QStringLiteral("post_walk_branch=recovery post_walk_recovery=-500/-35 ") ) );

    // The table was ENTERED but adopted nothing: whatever the walk had before stands, and the (steps/cleanup) pair is
    // not reported as the table's, even when it equals one of the table's own entries.
    ASSERT_TRUE( walkFields( true, false, true, false, 250, 22 ).contains(
        QStringLiteral("post_walk_branch=steps post_walk_recovery=NA ") ) );
    ASSERT_TRUE( walkFields( true, true, true, false, 250, 22 ).contains(
        QStringLiteral("post_walk_branch=cleanup post_walk_recovery=NA ") ) );
    ASSERT_TRUE( walkFields( false, false, true, false, 300, 12 ).contains(
        QStringLiteral("post_walk_branch=none post_walk_recovery=NA ") ) );
    ASSERT_FALSE( walkFields( true, true, true, false, 250, 22 ).contains( QStringLiteral("branch=recovery") ) );
    ASSERT_FALSE( walkFields( true, true, true, false, 250, 22 ).contains( QStringLiteral("recovery=250/22") ) );

    // A null trace is a no-op, not a crash (the formatter's own callers always pass one).
    lookAssistTraceWalkSteps( nullptr, true );
    lookAssistTraceWalkCleanup( nullptr );
    lookAssistTraceWalkRecovery( nullptr, true, 1, 2 );
}

TEST(LookAssistScene, Ev100IsNeverRoundedAcrossTheDaylightThreshold)
{
    // ISO 100, f/4, 1/128 s (7813 us): EV100 10.9999. Rounded to 2 or 3 decimals it would print 11.0, next to
    // daylight_gate=exposure (the gate sees 10.9999 < 11). The log truncates at three decimals instead.
    LookAssistStats justBelow = withEv( fixtureRawStats(), 100, 7813, 400 );
    ASSERT_TRUE( justBelow.hasSceneEv100 );
    ASSERT_TRUE( justBelow.sceneEv100 < 11.0 && justBelow.sceneEv100 > 10.99 );
    ASSERT_FALSE( lookAssistExposureIsDaylightBright( justBelow ) );
    LookAssistDecisionTrace trace;
    const QString below = lookAssistDecisionLogFields( justBelow, trace );
    ASSERT_TRUE( below.contains( QStringLiteral("ev100=10.999 daylight_gate=exposure ") ) );
    // At and above the threshold the printed value never reads below 11 while the gate says bright (and vice versa).
    for( double shutterUs : { 31250.0, 31249.0, 31251.0, 7813.0, 7812.0 } )
    {
        const LookAssistStats s = withEv( fixtureRawStats(), 100, shutterUs, shutterUs > 10000 ? 800 : 400 );
        const QString text = lookAssistDecisionLogFields( s, trace ).section( QLatin1Char(' '), 1, 1 );
        const bool printedAtLeast11 = text.startsWith( QStringLiteral("ev100=11.") );
        ASSERT_TRUE( printedAtLeast11 == lookAssistExposureIsDaylightBright( s ) );
    }
}

TEST(LookAssistScene, DecisionLogFieldsAreTheseTwelveInThisOrder)
{
    // Dropping, renaming or reordering any field fails here (and the exact-string tests above).
    LookAssistDecisionTrace trace;
    trace.postWalkRan = true;
    trace.postWalkBranch = LookAssistPostWalkBranch::Recovery;
    trace.displayMeterRan = true;
    trace.playbackScaleFactor = 2;
    for( const LookAssistStats &s : { fixtureRawStats(), daylightFixture() } )
    {
        const QString fields = lookAssistDecisionLogFields( s, trace );
        ASSERT_TRUE( fieldKeys( fields ) == kDecisionFieldKeys );
        ASSERT_FALSE( fields.startsWith( QLatin1Char(' ') ) );   // the caller owns the separator
        ASSERT_FALSE( fields.endsWith( QLatin1Char(' ') ) );
    }
}

TEST(LookAssistScene, TheGateHelperIsTheDecisionNotACopyOfIt)
{
    // lookAssistDaylightNeedsPictureEvidence is now built on lookAssistDaylightGate. Prove it is the same boolean as
    // the conjunction it replaced (verbatim below) over every combination of the four inputs.
    const auto previous = []( const LookAssistStats &s, LookAssistScene legacy ) {
        return !s.daylightPictureEvidence
            && lookAssistExposureIsDaylightBright( s )
            && lookAssistIsFlatFloorRawThumbnail( s )
            && ( legacy == LookAssistScene::Night || legacy == LookAssistScene::ArtificialLights ); };

    LookAssistStats usable = analyzeFrame( 64, 64, []( int x, int ) {
        return std::vector<int>{ 20 + x / 2, 20 + x / 2, 20 + x / 2 }; } );
    const LookAssistStats thumbnails[] = { fixtureRawStats(), usable };
    const double isos[] = { 0.0, 100.0, 1600.0 };
    const double shutters[] = { 0.0, 465.0, 20000.0 };
    const LookAssistScene scenes[] = { LookAssistScene::Night, LookAssistScene::ArtificialLights,
                                       LookAssistScene::Shade, LookAssistScene::BrightSun };
    int combinations = 0;
    for( const LookAssistStats &thumbnail : thumbnails )
        for( double iso : isos )
            for( double shutter : shutters )
                for( bool evidence : { false, true } )
                    for( LookAssistScene legacy : scenes )
                    {
                        LookAssistStats s = withEv( thumbnail, iso, shutter, 560 );
                        s.daylightPictureEvidence = evidence;
                        ASSERT_TRUE( lookAssistDaylightNeedsPictureEvidence( s, legacy ) == previous( s, legacy ) );
                        // The named gate agrees: Open exactly when only the picture is left to decide.
                        ASSERT_TRUE( ( lookAssistDaylightGate( s, legacy ) == LookAssistDaylightGate::Open )
                                     == ( lookAssistExposureIsDaylightBright( s ) && lookAssistIsFlatFloorRawThumbnail( s )
                                          && ( legacy == LookAssistScene::Night || legacy == LookAssistScene::ArtificialLights ) ) );
                        ++combinations;
                    }
    ASSERT_EQ( 2 * 3 * 3 * 2 * 4, combinations );
}

TEST(LookAssistScene, BothConsumersAppendTheDecisionFieldsAndChangeNothingElse)
{
    const QString applier = readRepoFile( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    const QString window = readRepoFile( QStringLiteral("platform/qt/MainWindow.cpp") );
    ASSERT_FALSE( applier.isEmpty() );
    ASSERT_FALSE( window.isEmpty() );

    // GUI: the result line keeps every existing field in its order and gains the shared fields at the very end.
    const QString guiPrefix = QStringLiteral(
        "analysis=raw scene=%1 median=%2 p05=%3 p95=%4 p99=%5 clip_low=%6 clip_high=%7 balance_samples=%8 preset_exp=%9 "
        "preset_contrast=%10 preset_pivot=%11 preset_shadows=%12 preset_highlights=%13 preset_vibrance=%14 "
        "preset_temp_delta=%15 preset_tint_delta=%16 final_temp=%17 final_tint=%18 thumb=%19x%20 downscale=%21 "
        "color_thumb=%22x%23 color_downscale=%24 frame=%25 last_serial=%26 last_frame=%27 next_serial=%28 %29\")");
    ASSERT_EQ( 1, window.count( guiPrefix ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("lookAssistDecisionLogFields( stats, decisionTrace )") ) );
    const int guiResult = window.indexOf( QStringLiteral("QStringLiteral(\"look_assist.apply.result\")") );
    ASSERT_TRUE( guiResult > 0 );
    ASSERT_TRUE( window.indexOf( QStringLiteral("lookAssistDecisionLogFields( stats, decisionTrace )"), guiResult ) > guiResult );

    // Headless: the "applied" line keeps its fields and gains the same shared fields at the end.
    ASSERT_EQ( 1, applier.count( QStringLiteral("initialPatchBaseChroma=%37 initialPatchFinalChroma=%38 %39\\n\"") ) );
    ASSERT_EQ( 1, applier.count( QStringLiteral("lookAssistDecisionLogFields( stats, decisionTrace )") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral("decisionTrace.pictureEvidenceAsked = !masterScenePass;") ) );
    // The headless applier has no walk and no display meter: it must not claim either.
    ASSERT_FALSE( applier.contains( QStringLiteral("decisionTrace.postWalk") ) );
    ASSERT_FALSE( applier.contains( QStringLiteral("decisionTrace.displayMeterRan") ) );

    // GUI: each input is recorded where it is decided, and only there.
    ASSERT_TRUE( window.contains( QStringLiteral("decisionTrace.pictureEvidenceAsked = !s_lookAssistMasterScenePass;") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("decisionTrace.playbackScaleFactor = displayMeterPlaybackScaleUi;") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("const bool useDisplayMeterExposureUi = displayMeterPlaybackScaleUi == 2;") ) );
    const int meterSamples = window.indexOf( QStringLiteral("displayStatsValidUi = true;") );
    ASSERT_TRUE( meterSamples > 0 );
    const int meterRan = window.indexOf( QStringLiteral("decisionTrace.displayMeterRan = true;"), meterSamples );
    ASSERT_TRUE( meterRan > meterSamples && meterRan - meterSamples < 120 );   // set with the samples, not elsewhere
    ASSERT_EQ( 1, window.count( QStringLiteral("decisionTrace.displayMeterRan = true;") ) );
    const int walk = window.indexOf( QStringLiteral("if( refinePostBalance )") );
    ASSERT_TRUE( walk > 0 );
    const int walkRan = window.indexOf( QStringLiteral("decisionTrace.postWalkRan = true;"), walk );
    const int walkSteps = window.indexOf( QStringLiteral("lookAssistTraceWalkSteps( &decisionTrace, adjustedPostBalance );"), walk );
    const int walkCleanup = window.indexOf( QStringLiteral("lookAssistTraceWalkCleanup( &decisionTrace );"), walk );
    const int walkRecovery = window.indexOf( QStringLiteral("lookAssistTraceWalkRecovery( &decisionTrace, recoveryAdopted,"), walk );
    ASSERT_TRUE( walkRan > walk && walkSteps > walkRan && walkCleanup > walkSteps && walkRecovery > walkCleanup );
    ASSERT_EQ( 1, window.count( QStringLiteral("decisionTrace.postWalkRan = true;") ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("lookAssistTraceWalkSteps(") ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("lookAssistTraceWalkCleanup(") ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("lookAssistTraceWalkRecovery(") ) );
    // The branch and the recovery pair are written ONLY through the helpers (whose rules the unit test below pins).
    ASSERT_FALSE( window.contains( QStringLiteral("decisionTrace.postWalkBranch") ) );
    ASSERT_FALSE( window.contains( QStringLiteral("decisionTrace.recoveryTemperatureDelta") ) );
    ASSERT_FALSE( window.contains( QStringLiteral("decisionTrace.recoveryTintDelta") ) );
    // The adoption flag is raised inside the candidate-adoption branch, once, and nowhere else: entering the table
    // is not adopting from it.
    ASSERT_EQ( 1, window.count( QStringLiteral("recoveryAdopted = true;") ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("bool recoveryAdopted = false;") ) );
    const int adoption = window.indexOf( QStringLiteral("if( candidateScore + 1.0 < bestScore )"), walk );
    const int adoptedFlag = window.indexOf( QStringLiteral("recoveryAdopted = true;"), walk );
    ASSERT_TRUE( adoption > walk && adoptedFlag > adoption && adoptedFlag - adoption < 400 );
    ASSERT_TRUE( walkRecovery > adoptedFlag );
    // The walk's own gate and its recovery table are untouched by this card.
    ASSERT_TRUE( window.contains( QStringLiteral("!daylightScene && ( !autoWhiteBalanceValid || useProcessedColorStats )") ) );
    ASSERT_TRUE( window.contains( QStringLiteral("qMakePair( 250, 22 ),") ) );
}

// ---------------------------------------------------------------------------------------------------------------
// LOOK-ASSIST-WINDOW-LIT-INTERIOR-1: a window-lit interior is not night.
// The owner clip (M16-1243): flat-floor RAW thumbnail (median 32), no recorded exposure, night verdict; its processed
// neutral patch (luma 208, chroma 6, B-R +6) solved 9930 K / -33, ACCEPTED undamped, and the night walk then replaced
// it with the 250 / +22 recovery pair (blue-magenta). Synthetic here: the RAW floor is the tracked fixture's, the
// picture is a dark room with a window whose colour follows the white balance under test.
// ---------------------------------------------------------------------------------------------------------------
namespace
{

struct WindowRoom
{
    int neutralKelvin = 9930;     // the balance at which the window surface renders exactly neutral
    int neutralTint = -33;
    int baseKelvin = 6000;        // the receipt's balance the patch picture was rendered at
    int baseTint = 0;
    double baseBlueAmber = 6.0;   // the window surface at the base balance: B-R and G-(R+B)/2 (the logged patch)
    double baseGreen = 1.0;
    int windowLuma = 210;
};

const int kRoomW = 96;
const int kRoomH = 64;
const int kRoomDownscale = 3;

// The window surface at a white balance: its casts shrink linearly (in mired / tint) to zero at the neutral balance.
std::vector<int> windowSurface( const WindowRoom &room, int kelvin, int tint )
{
    const double span = 1.0e6 / room.baseKelvin - 1.0e6 / room.neutralKelvin;
    const double moved = 1.0e6 / room.baseKelvin - 1.0e6 / kelvin;
    const double blueAmber = span == 0.0 ? room.baseBlueAmber : room.baseBlueAmber * ( 1.0 - moved / span );
    const int tintSpan = room.neutralTint - room.baseTint;
    const double green = tintSpan == 0 ? room.baseGreen
                                       : room.baseGreen * ( 1.0 - (double)( tint - room.baseTint ) / tintSpan );
    const double l = room.windowLuma;
    return { qBound( 0, (int)qRound( l - blueAmber / 2.0 - green / 3.0 ), 255 ),
             qBound( 0, (int)qRound( l + 2.0 * green / 3.0 ), 255 ),
             qBound( 0, (int)qRound( l + blueAmber / 2.0 - green / 3.0 ), 255 ) };
}

// The room: luma 6..20 everywhere, a 20 x 20 window (6.5 % of the frame) at x 70..89, y 8..27.
LookAssistRenderedPicture roomPicture( const WindowRoom &room, int kelvin, int tint )
{
    LookAssistRenderedPicture p;
    p.width = kRoomW;
    p.height = kRoomH;
    p.downscaleFactor = kRoomDownscale;
    p.rgb.resize( (size_t)kRoomW * kRoomH * 3 );
    const std::vector<int> w = windowSurface( room, kelvin, tint );
    for( int y = 0; y < kRoomH; ++y )
        for( int x = 0; x < kRoomW; ++x )
        {
            const bool inWindow = x >= 70 && x < 90 && y >= 8 && y < 28;
            const int d = 6 + ( x + 2 * y ) % 15;
            unsigned char *px = &p.rgb[( (size_t)y * kRoomW + x ) * 3];
            px[0] = (unsigned char)( inWindow ? w[0] : d );
            px[1] = (unsigned char)( inWindow ? w[1] : d );
            px[2] = (unsigned char)( inWindow ? w[2] : d + 3 );
        }
    p.stats = analyzeLookAssistThumbnail( p.rgb.data(), kRoomW, kRoomH );
    return p;
}

struct WindowLitRun
{
    LookAssistStats stats;
    LookAssistScene scene = LookAssistScene::Night;
    LookAssistStats color;
    LookAssistAutoWhiteBalancePatch patch;
    LookAssistPreset preset;
    LookAssistWhiteBalanceResolution wb;
    LookAssistWindowLitCheck check;
};

enum class Renderer { Room, None, CastAtBase, OtherGeometry, FailsAfterVerification };

// The night path exactly as the consumers run it up to the walk: scene, processed colour, patch search on the patch
// picture, the one white-balance decision with `solved` as the solver's answer, then (applyRule) the window check.
// colorP95 >= 0 replaces the patch picture's p95 (the live rule's lit-picture conjunct reads it).
WindowLitRun runWindowLit( const LookAssistStats &raw, const WindowRoom &room, int solvedKelvin, int solvedTint,
                           bool applyRule = true, bool processedSolve = true, Renderer renderer = Renderer::Room,
                           double colorP95 = -1.0 )
{
    WindowLitRun r;
    r.stats = raw;
    r.scene = resolveLookAssistScene( &r.stats, []( double, LookAssistStats *out ) {
        *out = renderedPicture( 8, 8, []( int, int ) { return 8; } ); return true; } );
    const LookAssistRenderedPicture patchPicture = roomPicture( room, room.baseKelvin, room.baseTint );
    r.color = patchPicture.stats;
    if( colorP95 >= 0.0 ) r.color.p95 = colorP95;
    r.patch = findLookAssistAutoWhiteBalancePatch( patchPicture.rgb.data(), kRoomW, kRoomH, kRoomDownscale,
                                                   kRoomW * kRoomDownscale, kRoomH * kRoomDownscale );
    r.preset = presetForLookAssistScene( r.scene, r.stats, &r.color );
    LookAssistWhiteBalanceRequest request;
    request.stats = &r.stats;
    request.scene = r.scene;
    request.patch = r.patch;
    request.solvedOnProcessedPicture = processedSolve;
    request.baseTemperature = room.baseKelvin;
    request.baseTint = room.baseTint;
    request.rawWidth = kRoomW * kRoomDownscale;
    request.rawHeight = kRoomH * kRoomDownscale;
    WindowRoom castRoom = room;
    castRoom.baseBlueAmber = 28.0;
    switch( renderer )
    {
    case Renderer::Room:
        request.renderBalance = [room]( double, int k, int t, LookAssistRenderedPicture *out ) {
            *out = roomPicture( room, k, t ); return true; };
        break;
    case Renderer::CastAtBase:
        request.renderBalance = [castRoom]( double, int k, int t, LookAssistRenderedPicture *out ) {
            *out = roomPicture( castRoom, k, t ); return true; };
        break;
    case Renderer::OtherGeometry:
        request.renderBalance = [room]( double, int k, int t, LookAssistRenderedPicture *out ) {
            *out = roomPicture( room, k, t ); out->downscaleFactor = kRoomDownscale + 1; return true; };
        break;
    case Renderer::FailsAfterVerification:
    {
        // The check's own two renders (base, solution) succeed; every render after them (the search) fails.
        auto calls = std::make_shared<int>( 0 );
        request.renderBalance = [room, calls]( double, int k, int t, LookAssistRenderedPicture *out ) {
            if( ++*calls > 2 ) return false;
            *out = roomPicture( room, k, t ); return true; };
        break;
    }
    case Renderer::None:
        break;
    }
    r.wb = resolveLookAssistWhiteBalance( request, [&]( int, int, int *t, int *s ) { *t = solvedKelvin; *s = solvedTint; },
                                          &r.preset );
    if( applyRule )
        r.check = resolveLookAssistWindowLitInterior( request, r.wb, 1.2, &r.stats, &r.scene, &r.preset, &r.color );
    return r;
}

WindowLitRun masterOf( const WindowRoom &room )
{
    return runWindowLit( fixtureRawStats(), room, 9930, -33, false );
}

bool samePreset( const LookAssistPreset &a, const LookAssistPreset &b )
{
    return a.exposure == b.exposure && a.contrast == b.contrast && a.pivot == b.pivot && a.shadows == b.shadows
        && a.highlights == b.highlights && a.vibrance == b.vibrance && a.temperatureDelta == b.temperatureDelta
        && a.tintDelta == b.tintDelta;
}

// The rule left this run exactly as master leaves it: the same verdict, stats flag, preset and balance.
bool untouched( const WindowLitRun &r, const WindowLitRun &master )
{
    return !r.check.evidence && r.scene == master.scene && !r.stats.windowLitInteriorEvidence
        && samePreset( r.preset, master.preset ) && r.wb.temperature == master.wb.temperature && r.wb.tint == master.wb.tint;
}

} // namespace

TEST(LookAssistScene, AWindowLitInteriorIsNotNightAndKeepsItsOwnNeutralSolve)
{
    const WindowRoom room;
    const WindowLitRun master = runWindowLit( fixtureRawStats(), room, 9930, -33, false );
    // Master up to the walk, as logged for the owner clip: night, a window-bright neutral patch, accepted undamped.
    ASSERT_TRUE( master.scene == LookAssistScene::Night );
    ASSERT_FALSE( master.stats.hasSceneEv100 );
    ASSERT_TRUE( lookAssistIsFloorLiftedNightThumbnail( master.scene, master.stats ) );
    ASSERT_TRUE( master.patch.valid );
    ASSERT_NEAR( 210.0, master.patch.luma, 2.0 );
    ASSERT_NEAR( 6.0, master.patch.chroma, 0.5 );
    ASSERT_TRUE( master.wb.autoValid );
    ASSERT_TRUE( master.wb.decision == QStringLiteral("accepted") );
    ASSERT_TRUE( master.wb.source == QStringLiteral("processed-neutral-patch") );
    ASSERT_EQ( 9930, master.wb.temperature );
    ASSERT_EQ( -33, master.wb.tint );
    ASSERT_FALSE( lookAssistIsDaylightScene( master.stats, master.scene ) );   // master then walks it (blue-magenta)

    const WindowLitRun r = runWindowLit( fixtureRawStats(), room, 9930, -33 );
    ASSERT_TRUE( r.check.candidate );
    ASSERT_TRUE( r.check.evidence );
    ASSERT_TRUE( r.check.reason == QStringLiteral("pass") );
    // Not night: the daylight class, by the window evidence (the exposure still cannot say daylight).
    ASSERT_TRUE( r.stats.windowLitInteriorEvidence );
    ASSERT_FALSE( r.stats.daylightPictureEvidence );
    ASSERT_TRUE( r.scene == LookAssistScene::Shade );
    ASSERT_TRUE( lookAssistSceneIsDaylight( r.stats ) );
    ASSERT_TRUE( lookAssistIsDaylightScene( r.stats, r.scene ) );
    ASSERT_FALSE( lookAssistIsFloorLiftedNightThumbnail( r.scene, r.stats ) );
    // ... so the GUI's night walk does not run (its gate, verbatim: !daylightScene && ...).
    const bool daylightScene = lookAssistIsDaylightScene( r.stats, r.scene );
    ASSERT_FALSE( !daylightScene && ( !r.wb.autoValid || true ) );
    // A neutral balance: the clip's own accepted solve, unchanged, inside the daylight window.
    ASSERT_EQ( 9930, room.baseKelvin + r.preset.temperatureDelta );
    ASSERT_EQ( -33, room.baseTint + r.preset.tintDelta );
    const LookAssistWhiteBalanceBounds bounds = lookAssistWhiteBalanceBounds( r.stats, r.scene );
    ASSERT_EQ( 4800, bounds.minTemperature );
    ASSERT_EQ( -35, bounds.minTint );
    ASSERT_EQ( 10, bounds.maxTint );
    ASSERT_TRUE( r.check.solutionSurfaceChroma <= 1.0 );                  // the window is neutral at the solution
    ASSERT_NEAR( 6.0, r.check.baseSurfaceChroma, 0.5 );
    // The tone is the daylight class's own preset on the same inputs (no night rescue), the balance the solve.
    LookAssistPreset shade = presetForLookAssistScene( LookAssistScene::Shade, r.stats, &r.color );
    shade.temperatureDelta = r.preset.temperatureDelta;
    shade.tintDelta = r.preset.tintDelta;
    ASSERT_TRUE( samePreset( shade, r.preset ) );
    ASSERT_TRUE( r.preset.shadows < master.preset.shadows );
    // The log names the evidence.
    ASSERT_TRUE( lookAssistDecisionLogFields( r.stats, LookAssistDecisionTrace() ).startsWith(
        QStringLiteral("has_ev100=0 ev100=NA daylight_gate=window post_walk_ran=0 ") ) );
    ASSERT_TRUE( lookAssistDaylightGateName( r.stats, true ) == QStringLiteral("window") );
}

TEST(LookAssistScene, ATrueNightClipKeepsItsVerdictPresetAndBalance)
{
    // The same dark room, its bright neutral surface lit by a night light source: the clip's own solve is warmer than
    // (or too close to) the 6000 K base, so it is not daylight and master's result stands bit for bit.
    struct Light { const char *name; int kelvin; int tint; double baseBlueAmber; };
    const Light lights[] = {
        { "sodium",    2100, 10, -14.0 }, { "tungsten", 3200,  4, -10.0 }, { "moonlight", 4100, 0, -6.0 },
        { "stage-led", 5600, -5,  -2.0 }, { "cool-led", 6500,  0,   2.0 }, { "just-under", 6999, -20, 3.0 } };
    for( const Light &l : lights )
    {
        WindowRoom room;
        room.neutralKelvin = l.kelvin;
        room.neutralTint = l.tint;
        room.baseBlueAmber = l.baseBlueAmber;
        const WindowLitRun master = runWindowLit( fixtureRawStats(), room, l.kelvin, l.tint, false );
        const WindowLitRun r = runWindowLit( fixtureRawStats(), room, l.kelvin, l.tint );
        std::fprintf( stderr, "WINDOW-LIT night=%s scene=%s reason=%s accepted=%d\n", l.name,
                      qPrintable( lookAssistSceneName( r.scene ) ), qPrintable( r.check.reason ), r.wb.autoValid ? 1 : 0 );
        ASSERT_TRUE( master.scene == LookAssistScene::Night );
        ASSERT_TRUE( r.check.candidate );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-daylight-locus") );
        ASSERT_TRUE( untouched( r, master ) );
        ASSERT_TRUE( lookAssistDecisionLogFields( r.stats, LookAssistDecisionTrace() ).contains(
            QStringLiteral("daylight_gate=exposure ") ) );
    }

    // A night clip WITH a recorded exposure is never a candidate, whatever its picture says: an ND-filtered day
    // (EV100 8.6, the existing pinned case) and a true night exposure (ISO 3200, 1/30 s, f/1.8).
    const WindowRoom daylightWindow;
    for( const LookAssistStats &raw : { withEv( fixtureRawStats(), 100, 20000, 280 ), withEv( fixtureRawStats(), 3200, 33333, 180 ) } )
    {
        const WindowLitRun master = runWindowLit( raw, daylightWindow, 9930, -33, false );
        const WindowLitRun r = runWindowLit( raw, daylightWindow, 9930, -33 );
        ASSERT_TRUE( master.scene == LookAssistScene::Night );
        ASSERT_FALSE( r.check.candidate );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-candidate") );
        ASSERT_TRUE( untouched( r, master ) );
    }
}

TEST(LookAssistScene, EveryWindowLitConjunctIsNeededOnItsOwn)
{
    // Each case breaks exactly ONE conjunct of the M16 run (which passes); the verdict must stay master's.
    const WindowRoom room;

    // Usable (non-flat) RAW thumbnail with a night verdict: the thumbnail speaks, so it is believed.
    const LookAssistStats usable = analyzeFrame( 64, 64, []( int x, int ) {
        return std::vector<int>{ 20 + x / 2, 20 + x / 2, 20 + x / 2 }; } );
    ASSERT_FALSE( lookAssistIsFlatFloorRawThumbnail( usable ) );
    ASSERT_TRUE( classifyLookAssistScene( usable ) == LookAssistScene::Night );
    {
        const WindowLitRun r = runWindowLit( usable, room, 9930, -33 );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-candidate") );
        ASSERT_TRUE( untouched( r, runWindowLit( usable, room, 9930, -33, false ) ) );
    }
    // A flat floor that the legacy classifier calls artificial lights (a lamp clipped in p99) is not night.
    LookAssistStats lamp = fixtureRawStats();
    lamp.p99 = 240;
    ASSERT_TRUE( lookAssistIsFlatFloorRawThumbnail( lamp ) );
    ASSERT_TRUE( classifyLookAssistScene( lamp ) == LookAssistScene::ArtificialLights );
    {
        const WindowLitRun r = runWindowLit( lamp, room, 9930, -33 );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-candidate") );
        ASSERT_TRUE( untouched( r, runWindowLit( lamp, room, 9930, -33, false ) ) );
    }
    // Solved on the RAW thumbnail, not the processed picture.
    {
        const WindowLitRun r = runWindowLit( fixtureRawStats(), room, 9930, -33, true, false );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-processed-solve") );
        ASSERT_TRUE( untouched( r, runWindowLit( fixtureRawStats(), room, 9930, -33, false, false ) ) );
    }
    // Accepted only damped (a chroma-12 patch 3930 K from base): master hedged it, so it is not believed undamped.
    {
        WindowRoom tinted = room;
        tinted.baseBlueAmber = 12.0;
        const WindowLitRun r = runWindowLit( fixtureRawStats(), tinted, 9930, -33 );
        ASSERT_TRUE( r.wb.decision == QStringLiteral("accepted-damped") );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-accepted-undamped") );
        ASSERT_TRUE( untouched( r, runWindowLit( fixtureRawStats(), tinted, 9930, -33, false ) ) );
    }
    // A patch below window brightness (a lamp-lit wall), from a 6500 K base so no damping hides it.
    {
        WindowRoom dim = room;
        dim.baseKelvin = 6500;
        dim.neutralKelvin = 7300;
        dim.neutralTint = -10;
        dim.windowLuma = 140;
        const WindowLitRun r = runWindowLit( fixtureRawStats(), dim, 7300, -10 );
        ASSERT_TRUE( r.wb.decision == QStringLiteral("accepted") );
        ASSERT_TRUE( r.check.reason == QStringLiteral("dim-patch") );
        ASSERT_TRUE( untouched( r, runWindowLit( fixtureRawStats(), dim, 7300, -10, false ) ) );
    }
    // A patch that is too coloured to be neutral under daylight (B-R +22), again with no damping to hide it.
    {
        WindowRoom blue = room;
        blue.baseKelvin = 6500;
        blue.neutralKelvin = 7300;
        blue.neutralTint = -10;
        blue.baseBlueAmber = 22.0;
        const WindowLitRun r = runWindowLit( fixtureRawStats(), blue, 7300, -10 );
        ASSERT_TRUE( r.wb.decision == QStringLiteral("accepted") );
        ASSERT_TRUE( r.check.reason == QStringLiteral("patch-not-neutral") );
        ASSERT_TRUE( untouched( r, runWindowLit( fixtureRawStats(), blue, 7300, -10, false ) ) );
    }
    // Bluer than the base but off the daylight locus on tint (+15 > +10: a green fluorescent correction).
    {
        WindowRoom green = room;
        green.neutralTint = 15;
        const WindowLitRun r = runWindowLit( fixtureRawStats(), green, 9930, 15 );
        ASSERT_TRUE( r.wb.decision == QStringLiteral("accepted") );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-daylight-locus") );
        ASSERT_TRUE( untouched( r, runWindowLit( fixtureRawStats(), green, 9930, 15, false ) ) );
    }
    // Nothing to verify the solve on.
    {
        const WindowLitRun r = runWindowLit( fixtureRawStats(), room, 9930, -33, true, true, Renderer::None );
        ASSERT_TRUE( r.check.reason == QStringLiteral("no-renderer") );
        ASSERT_TRUE( untouched( r, masterOf( room ) ) );
    }
    // The renderer's picture is not the patch picture's geometry: the surface cannot be found again.
    {
        const WindowLitRun r = runWindowLit( fixtureRawStats(), room, 9930, -33, true, true, Renderer::OtherGeometry );
        ASSERT_TRUE( r.check.reason == QStringLiteral("unverifiable") );
        ASSERT_TRUE( untouched( r, masterOf( room ) ) );
    }
    // The surface is not near-neutral at the base balance in the verification picture.
    {
        const WindowLitRun r = runWindowLit( fixtureRawStats(), room, 9930, -33, true, true, Renderer::CastAtBase );
        ASSERT_TRUE( r.check.reason == QStringLiteral("unverified-at-base") );
        ASSERT_TRUE( untouched( r, masterOf( room ) ) );
    }
    // The solver overshoots: the window is neutral at 6800 K, the solve says 9930 K and leaves it amber.
    {
        WindowRoom warmer = room;
        warmer.neutralKelvin = 6800;
        warmer.neutralTint = -33;
        const WindowLitRun r = runWindowLit( fixtureRawStats(), warmer, 9930, -33 );
        ASSERT_TRUE( r.wb.decision == QStringLiteral("accepted") );
        ASSERT_TRUE( r.check.reason == QStringLiteral("unverified-at-solution") );
        ASSERT_TRUE( r.check.solutionSurfaceChroma > r.check.baseSurfaceChroma + 0.75 );
        ASSERT_TRUE( untouched( r, runWindowLit( fixtureRawStats(), warmer, 9930, -33, false ) ) );
    }
}


TEST(LookAssistScene, BothConsumersApplyTheWindowLitCheckOnlyOnTheExposureBound)
{
    // Both consumers run the check on COPIES. They adopt its verdict, stats and preset only when it applies (the aperture
    // bound rules night out and a verified surface backs the balance) and never in master's pass; by colour alone it is
    // only logged (LOOK-ASSIST-WINDOW-LIT-INTERIOR-1).
    const QString applier = readRepoFile( QStringLiteral("src/batch/ReceiptApplier.cpp") );
    const QString window = readRepoFile( QStringLiteral("platform/qt/MainWindow.cpp") );
    ASSERT_FALSE( applier.isEmpty() );
    ASSERT_FALSE( window.isEmpty() );
    const QString copies = QStringLiteral(
        "windowLitRequest, wb, %1, &windowLitStats, &windowLitScene, &windowLitPreset," );
    const QString adopt = QStringLiteral(
        "stats = windowLitStats;\n        scene = windowLitScene;\n        preset = windowLitPreset;" );

    // GUI sync: once, after the one white-balance decision and its master-pass fallback, before the values are applied.
    ASSERT_EQ( 1, window.count( QStringLiteral("resolveLookAssistWindowLitInterior(") ) );
    ASSERT_EQ( 1, window.count( copies.arg( QStringLiteral("m_pMlvObject->processing->exposure_stops") ) ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("windowLitRequest.stats = &windowLitStats;") ) );
    const int sync = window.indexOf( QStringLiteral("// --- synchronous fallback (MLVAPP_LOOK_ASSIST_SYNC=1) ---") );
    const int syncWb = window.indexOf( QStringLiteral("const LookAssistWhiteBalanceResolution wb = resolveLookAssistWhiteBalance("), sync );
    const int masterPass = window.indexOf( QStringLiteral("applyLookAssistToReceipt( receipt, analysisFrame );"), syncWb );
    const int guiCheck = window.indexOf( QStringLiteral("resolveLookAssistWindowLitInterior("), sync );
    const int guiGate = window.indexOf( QStringLiteral("const bool windowLitApplied = windowLit.applies && !s_lookAssistMasterScenePass;"), guiCheck );
    const int guiAdopt = window.indexOf( adopt, guiGate );
    const int guiNight = window.indexOf( QStringLiteral("floorLiftedNightThumbnail = lookAssistIsFloorLiftedNightThumbnail( scene, stats );"), guiAdopt );
    const int guiTrace = window.indexOf( QStringLiteral("lookAssistTraceSurfaceSearch( &decisionTrace, windowLit );"), guiAdopt );
    const int applied = window.indexOf( QStringLiteral("applyLookAssistValues();"), syncWb );
    const int walkGate = window.indexOf( QStringLiteral("const bool daylightScene = lookAssistIsDaylightScene( stats, scene );"), syncWb );
    ASSERT_TRUE( sync > 0 && syncWb > sync && masterPass > syncWb && guiCheck > masterPass );
    ASSERT_TRUE( guiGate > guiCheck && guiAdopt > guiGate && guiNight > guiAdopt && guiTrace > guiAdopt );
    ASSERT_TRUE( applied > guiNight && walkGate > applied );   // adopted before the values and the night walk's gate
    ASSERT_EQ( 1, window.count( adopt ) );
    ASSERT_EQ( 1, window.count( QStringLiteral("windowLit.applies") ) );
    // The night walk's own gate is untouched: a daylight verdict skips it, exactly as before.
    ASSERT_TRUE( window.contains( QStringLiteral("!daylightScene && ( !autoWhiteBalanceValid || useProcessedColorStats )") ) );
    // Only a bound candidate is added to the synchronous routing (it renders the live picture); the rest is master's.
    ASSERT_TRUE( window.contains( QStringLiteral(
        "const bool daylightNeedsLivePicture = lookAssistIsDaylightScene( stats, scene ) || s_lookAssistMasterScenePass\n"
        "                                        || lookAssistNotNightByExposureBoundCandidate( stats, scene );") ) );
    ASSERT_FALSE( window.contains( QStringLiteral("lookAssistWindowLitInteriorCandidate(") ) );
    // The recovery ISO reaches the bound.
    ASSERT_TRUE( window.contains( QStringLiteral("m_pMlvObject->LENS.aperture,\n                             ReceiptApplier::lookAssistRecoveryIso( m_pMlvObject ) );") ) );

    // Headless: the same, after its decision and its master-pass fallback, before the receipt is written; the applied
    // balance is the check's verified one.
    ASSERT_EQ( 1, applier.count( QStringLiteral("resolveLookAssistWindowLitInterior(") ) );
    ASSERT_EQ( 1, applier.count( copies.arg( QStringLiteral("mlvObject->processing->exposure_stops") ) ) );
    ASSERT_EQ( 1, applier.count( QStringLiteral("windowLitRequest.stats = &windowLitStats;") ) );
    ASSERT_EQ( 1, applier.count( adopt ) );
    ASSERT_EQ( 1, applier.count( QStringLiteral("windowLit.applies") ) );
    const int hWb = applier.indexOf( QStringLiteral("const LookAssistWhiteBalanceResolution wb = resolveLookAssistWhiteBalance(") );
    const int hMaster = applier.indexOf( QStringLiteral("return applyHeadlessLookAssist( receipt, mlvObject, processingObject, analysisFrame, true );"), hWb );
    const int hCheck = applier.indexOf( QStringLiteral("resolveLookAssistWindowLitInterior("), hWb );
    const int hGate = applier.indexOf( QStringLiteral("const bool windowLitApplied = windowLit.applies && !masterScenePass;"), hCheck );
    const int hAdopt = applier.indexOf( adopt, hGate );
    const int hTemp = applier.indexOf( QStringLiteral("const int temperature = windowLitApplied ? windowLit.appliedTemperature : wb.temperature;"), hAdopt );
    const int hTint = applier.indexOf( QStringLiteral("const int tint = windowLitApplied ? windowLit.appliedTint : wb.tint;"), hAdopt );
    const int hWrite = applier.indexOf( QStringLiteral("receipt->setExposure( preset.exposure );"), hWb );
    ASSERT_TRUE( hWb > 0 && hMaster > hWb && hCheck > hMaster && hGate > hCheck && hAdopt > hGate );
    ASSERT_TRUE( hTemp > hAdopt && hTint > hTemp && hWrite > hTint );
    ASSERT_TRUE( applier.contains( QStringLiteral("lookAssistTraceSurfaceSearch( &decisionTrace, windowLit );") ) );
    ASSERT_TRUE( applier.contains( QStringLiteral("mlvObject->LENS.aperture,\n                             lookAssistRecoveryIso( mlvObject ) );") ) );
}

// ---------------------------------------------------------------------------------------------------------------
// LOOK-ASSIST-M16-NOT-NIGHT-1: aperture-bounded EV100 and a verified neutral surface.
// M16-1243 records ISO 100 and 1/1357 s (737 us) but LENS.aperture = 0 (a manual lens), so it had no EV100 at all. At
// f/1.0 that exposure is EV100 10.4, and any real aperture only raises it: provably not night (night ~0-5, lit night
// interiors ~5-7). Its processed-patch solve (9930 K / -33) overshoots: re-rendered there the patch turns amber (chroma
// 6 -> 20); the surface is neutral near 6600 K. Synthetic here, as above: the tracked RAW floor and a dark room whose
// window follows the balance under test.
// ---------------------------------------------------------------------------------------------------------------
namespace
{

LookAssistStats withBound( LookAssistStats s, double iso, double shutterUs, double recoveryIso = 0.0 )
{
    lookAssistSetSceneEv100( &s, iso, shutterUs, 0.0, recoveryIso );
    return s;
}

// The owner clip's recorded exposure: ISO 100, 737 us, no aperture.
LookAssistStats m16Raw()
{
    return withBound( fixtureRawStats(), 100, 737 );
}

// A window whose surface is neutral at (kelvin, tint) and responds like the app's picture: B-R rises 0.39 per mired
// (the M16 surface: +6 at 6000 K, -20 at 9930 K) and G-(R+B)/2 falls a third of a unit per tint unit.
WindowRoom roomNeutralAt( int kelvin, int tint )
{
    WindowRoom room;
    room.neutralKelvin = kelvin;
    room.neutralTint = tint;
    room.baseBlueAmber = 0.39 * ( 1.0e6 / room.baseKelvin - 1.0e6 / kelvin );
    room.baseGreen = ( tint - room.baseTint ) / 3.0;
    return room;
}

// M16-1243's window: neutral near 6600 K (the WINDOW-LIT lane's interpolation), a hair magenta at the 6000 K / 0 base.
WindowRoom m16Room()
{
    return roomNeutralAt( 6600, -3 );
}

// The picture at the run's final balance: what the receipt renders.
std::vector<unsigned char> finalPicture( const WindowLitRun &r, const WindowRoom &room )
{
    return roomPicture( room, room.baseKelvin + r.preset.temperatureDelta, room.baseTint + r.preset.tintDelta ).rgb;
}

// Verdict, stats flags, receipt (preset + balance) and picture are master's, bit for bit.
bool identicalToMaster( const WindowLitRun &r, const WindowLitRun &master, const WindowRoom &room )
{
    return untouched( r, master ) && !r.check.applies && r.stats.hasSceneEv100 == master.stats.hasSceneEv100
        && finalPicture( r, room ) == finalPicture( master, room );
}

LookAssistRenderBalanceFn roomRenderer( const WindowRoom &room )
{
    return [room]( double, int k, int t, LookAssistRenderedPicture *out ) { *out = roomPicture( room, k, t ); return true; };
}

// The window surface (the patch pixel of the M16 room's base picture; every room has the same geometry) in the picture
// rendered at (kelvin, tint).
LookAssistSurfaceProbe probeAt( const WindowRoom &room, int kelvin, int tint )
{
    LookAssistSurfaceProbe p;
    p.temperature = kelvin;
    p.tint = tint;
    const LookAssistAutoWhiteBalancePatch patch = findLookAssistAutoWhiteBalancePatch(
        roomPicture( m16Room(), 6000, 0 ).rgb.data(), kRoomW, kRoomH, kRoomDownscale,
        kRoomW * kRoomDownscale, kRoomH * kRoomDownscale );
    if( !patch.valid ) return p;
    const LookAssistRenderedPicture picture = roomPicture( room, kelvin, tint );
    const unsigned char *px = &picture.rgb[( (size_t)patch.thumbnailY * kRoomW + patch.thumbnailX ) * 3];
    p.surface.valid = true;
    p.surface.thumbnailX = patch.thumbnailX;
    p.surface.thumbnailY = patch.thumbnailY;
    p.surface.luma = ( 54.0 * px[0] + 183.0 * px[1] + 19.0 * px[2] ) / 256.0;
    p.surface.chroma = qMax( px[0], qMax( px[1], px[2] ) ) - qMin( px[0], qMin( px[1], px[2] ) );
    p.surface.greenAxis = px[1] - ( px[0] + px[2] ) * 0.5;
    p.surface.blueAmberAxis = (double)px[2] - (double)px[0];
    return p;
}

LookAssistWhiteBalanceBounds daylightWindow()
{
    LookAssistWhiteBalanceBounds w;
    w.minTemperature = 4800;
    w.maxTemperature = 10000;
    w.minTint = -35;
    w.maxTint = 10;
    return w;
}

int pixelChroma( const LookAssistRenderedPicture &picture, int x, int y )
{
    const unsigned char *p = &picture.rgb[( (size_t)y * picture.width + x ) * 3];
    return qMax( p[0], qMax( p[1], p[2] ) ) - qMin( p[0], qMin( p[1], p[2] ) );
}

} // namespace

TEST(LookAssistScene, TheApertureBoundIsEv100AtF1OnlyWhenTheApertureIsMissing)
{
    const LookAssistStats m16 = m16Raw();
    ASSERT_FALSE( m16.hasSceneEv100 );
    ASSERT_TRUE( m16.hasSceneEv100Bound );
    ASSERT_NEAR( 10.406, m16.sceneEv100Bound, 0.001 );
    ASSERT_NEAR( 0.0, m16.sceneRecoveryStops, 1e-12 );
    ASSERT_TRUE( lookAssistExposureBoundExcludesNight( m16 ) );
    // A bound is never a recorded exposure: it never opens the daylight gate, however high it is.
    ASSERT_FALSE( lookAssistExposureIsDaylightBright( m16 ) );
    ASSERT_FALSE( lookAssistExposureIsDaylightBright( withBound( fixtureRawStats(), 100, 100 ) ) );   // bound 13.3
    ASSERT_TRUE( lookAssistDaylightGate( m16, LookAssistScene::Night ) == LookAssistDaylightGate::Exposure );

    // It is the recorded formula at f/1.0, and every real aperture (f/1.0 and slower) gives at least the bound.
    double atF1 = 0.0;
    ASSERT_TRUE( lookAssistSceneEv100( 100, 737, 100, &atF1 ) );
    ASSERT_NEAR( atF1, m16.sceneEv100Bound, 1e-12 );
    for( double aperture : { 100.0, 120.0, 140.0, 200.0, 280.0, 560.0, 1600.0 } )
        ASSERT_TRUE( withEv( fixtureRawStats(), 100, 737, aperture ).sceneEv100 >= m16.sceneEv100Bound - 1e-12 );

    // Identity: with an aperture the recorded EV100 is exactly what it was and no bound is set, recovery ISO or not.
    for( double iso : { 100.0, 400.0, 3200.0 } )
        for( double shutter : { 465.0, 20000.0, 33333.0 } )
            for( double aperture : { 140.0, 560.0 } )
            {
                LookAssistStats s = fixtureRawStats();
                lookAssistSetSceneEv100( &s, iso, shutter, aperture, 1600.0 );
                double expected = 0.0;
                ASSERT_TRUE( lookAssistSceneEv100( iso, shutter, aperture, &expected ) );
                ASSERT_TRUE( s.hasSceneEv100 );
                ASSERT_TRUE( s.sceneEv100 == expected );
                ASSERT_FALSE( s.hasSceneEv100Bound );
                ASSERT_FALSE( lookAssistExposureBoundExcludesNight( s ) );
            }
    // Nothing recorded to bound: no ISO, or no shutter.
    ASSERT_FALSE( withBound( fixtureRawStats(), 0, 737 ).hasSceneEv100Bound );
    ASSERT_FALSE( withBound( fixtureRawStats(), 100, 0 ).hasSceneEv100Bound );
    ASSERT_FALSE( lookAssistExposureBoundExcludesNight( fixtureRawStats() ) );

    // The threshold: EV100 7 at f/1.0 is 1/128 s at ISO 100 (7812.5 us).
    ASSERT_TRUE( lookAssistExposureBoundExcludesNight( withBound( fixtureRawStats(), 100, 7812 ) ) );
    ASSERT_FALSE( lookAssistExposureBoundExcludesNight( withBound( fixtureRawStats(), 100, 7813 ) ) );
    // A night exposure without an aperture stays below it: ISO 3200 at 1/50 s is 0.6, ISO 6400 at 1/30 s is -1.1.
    ASSERT_FALSE( lookAssistExposureBoundExcludesNight( withBound( fixtureRawStats(), 3200, 20000 ) ) );
    ASSERT_FALSE( lookAssistExposureBoundExcludesNight( withBound( fixtureRawStats(), 6400, 33333 ) ) );

    // Dual ISO: the recovery rows could have been the ones exposed for, so the bound minus their stops must stay out of the
    // dark-night band (>= 5). M16 at a 4-stop recovery: 6.4 (still not night); at 6 stops: 4.4 (night not ruled out).
    const LookAssistStats m16Dual = withBound( fixtureRawStats(), 100, 737, 1600 );
    ASSERT_NEAR( 4.0, m16Dual.sceneRecoveryStops, 1e-9 );
    ASSERT_NEAR( 10.406, m16Dual.sceneEv100Bound, 0.001 );   // the bound itself is the recorded ISO's
    ASSERT_TRUE( lookAssistExposureBoundExcludesNight( m16Dual ) );
    ASSERT_FALSE( lookAssistExposureBoundExcludesNight( withBound( fixtureRawStats(), 100, 737, 6400 ) ) );
    // 1/256 s at ISO 100 is bound 8, over the 7: with a 4-stop recovery it is 4, inside the dark-night band.
    ASSERT_TRUE( lookAssistExposureBoundExcludesNight( withBound( fixtureRawStats(), 100, 3906 ) ) );
    ASSERT_FALSE( lookAssistExposureBoundExcludesNight( withBound( fixtureRawStats(), 100, 3906, 1600 ) ) );
    // A DISO ISO below the recorded one is no recovery (the recorded rows are already the more sensitive ones).
    ASSERT_NEAR( 0.0, withBound( fixtureRawStats(), 1600, 737, 100 ).sceneRecoveryStops, 1e-12 );

    // The log carries the bound and its source; a recorded EV100 says so and prints no bound.
    const QString fields = lookAssistDecisionLogFields( m16, LookAssistDecisionTrace() );
    ASSERT_TRUE( fields.startsWith( QStringLiteral("has_ev100=0 ev100=NA daylight_gate=exposure ") ) );
    ASSERT_TRUE( fields.endsWith( QStringLiteral(
        "ev100_bound=10.406 ev100_source=aperture_bound surface_search=not-run surface_search_balance=NA") ) );
    ASSERT_TRUE( lookAssistDecisionLogFields( daylightFixture(), LookAssistDecisionTrace() ).contains(
        QStringLiteral(" ev100_bound=NA ev100_source=recorded ") ) );
}

TEST(LookAssistScene, M16LeavesNightOnTheVerifiedNeutralSurfaceBalance)
{
    const WindowRoom room = m16Room();
    // Before (no bound): the colour-only check refuses exactly as the WINDOW-LIT lane logged it, the surface goes from
    // chroma 6 to 20 at the solve, and the verdict stays night.
    const WindowLitRun before = runWindowLit( fixtureRawStats(), room, 9930, -33 );
    ASSERT_TRUE( before.check.candidate );
    ASSERT_FALSE( before.check.exposureBound );
    ASSERT_TRUE( before.check.reason == QStringLiteral("unverified-at-solution") );
    ASSERT_NEAR( 6.0, before.check.baseSurfaceChroma, 1.0 );
    ASSERT_NEAR( 20.0, before.check.solutionSurfaceChroma, 2.0 );
    ASSERT_TRUE( before.check.search.result == QStringLiteral("not-run") );
    ASSERT_TRUE( before.scene == LookAssistScene::Night );

    // After: the same clip with its recorded ISO and shutter.
    const WindowLitRun master = runWindowLit( m16Raw(), room, 9930, -33, false );
    ASSERT_TRUE( master.scene == LookAssistScene::Night );   // the bound never touches the legacy verdict itself
    ASSERT_TRUE( master.wb.decision == QStringLiteral("accepted") );
    ASSERT_EQ( 9930, master.wb.temperature );
    const WindowLitRun r = runWindowLit( m16Raw(), room, 9930, -33 );
    std::fprintf( stderr, "M16-NOT-NIGHT scene=%s reason=%s search=%s renders=%d balance=%d/%d surface_chroma=%.1f\n",
                  qPrintable( lookAssistSceneName( r.scene ) ), qPrintable( r.check.reason ),
                  qPrintable( r.check.search.result ), r.check.search.renders, r.check.appliedTemperature,
                  r.check.appliedTint, r.check.search.surface.chroma );
    ASSERT_TRUE( r.check.candidate );
    ASSERT_TRUE( r.check.exposureBound );
    ASSERT_TRUE( r.check.evidence );
    ASSERT_TRUE( r.check.applies );
    ASSERT_TRUE( r.check.reason == QStringLiteral("pass") );
    // The solve did not verify; the search found the balance at which that same surface is neutral.
    ASSERT_NEAR( 20.0, r.check.solutionSurfaceChroma, 2.0 );
    ASSERT_TRUE( r.check.search.result == QStringLiteral("converged") );
    ASSERT_TRUE( r.check.search.converged );
    ASSERT_TRUE( r.check.search.renders >= 1 && r.check.search.renders <= kLookAssistSurfaceSearchMaxRenders );
    ASSERT_TRUE( std::fabs( 1.0e6 / r.check.search.temperature - 1.0e6 / 6600.0 ) <= 6.0 );   // within 6 mired
    ASSERT_TRUE( qAbs( r.check.search.tint + 3 ) <= 6 );
    ASSERT_TRUE( r.check.search.surface.chroma <= 3.0 );
    ASSERT_EQ( r.check.search.temperature, r.check.appliedTemperature );
    ASSERT_EQ( r.check.search.tint, r.check.appliedTint );
    // Not night: the daylight class, so the night walk does not run, and the applied balance is the verified one.
    ASSERT_TRUE( r.scene == LookAssistScene::Shade );
    ASSERT_TRUE( r.stats.windowLitInteriorEvidence );
    ASSERT_TRUE( lookAssistIsDaylightScene( r.stats, r.scene ) );
    ASSERT_FALSE( lookAssistIsFloorLiftedNightThumbnail( r.scene, r.stats ) );
    const bool daylightScene = lookAssistIsDaylightScene( r.stats, r.scene );
    ASSERT_FALSE( !daylightScene && ( !r.wb.autoValid || true ) );
    ASSERT_EQ( r.check.appliedTemperature, room.baseKelvin + r.preset.temperatureDelta );
    ASSERT_EQ( r.check.appliedTint, room.baseTint + r.preset.tintDelta );
    LookAssistPreset shade = presetForLookAssistScene( LookAssistScene::Shade, r.stats, &r.color );
    shade.temperatureDelta = r.preset.temperatureDelta;
    shade.tintDelta = r.preset.tintDelta;
    ASSERT_TRUE( samePreset( shade, r.preset ) );
    // The window renders neutral in the receipt's picture; master's solve left it amber.
    const int x = r.patch.thumbnailX, y = r.patch.thumbnailY;
    ASSERT_TRUE( pixelChroma( roomPicture( room, r.check.appliedTemperature, r.check.appliedTint ), x, y ) <= 3 );
    ASSERT_TRUE( pixelChroma( roomPicture( room, 9930, -33 ), x, y ) >= 15 );
    // The trace: the window gate, the bound and its source, and the search.
    LookAssistDecisionTrace trace;
    lookAssistTraceSurfaceSearch( &trace, r.check );
    const QString fields = lookAssistDecisionLogFields( r.stats, trace );
    ASSERT_TRUE( fields.startsWith( QStringLiteral("has_ev100=0 ev100=NA daylight_gate=window post_walk_ran=0 ") ) );
    ASSERT_TRUE( fields.endsWith( QStringLiteral("ev100_bound=10.406 ev100_source=aperture_bound surface_search=converged "
                                                 "surface_search_balance=%1/%2")
                                      .arg( r.check.appliedTemperature ).arg( r.check.appliedTint ) ) );

    // A solve that DOES verify on its surface is applied as it is: no search.
    const WindowLitRun direct = runWindowLit( m16Raw(), WindowRoom(), 9930, -33 );
    ASSERT_TRUE( direct.check.applies );
    ASSERT_TRUE( direct.check.search.result == QStringLiteral("not-run") );
    ASSERT_EQ( 9930, direct.check.appliedTemperature );
    ASSERT_EQ( -33, direct.check.appliedTint );
}

TEST(LookAssistScene, ANoApertureHighShutterClipLeavesNightAndASlowOneDoesNot)
{
    const WindowRoom room = m16Room();
    // ISO 400 at 1/2000 s, no aperture: bound 9.0 -> not night.
    const WindowLitRun fast = runWindowLit( withBound( fixtureRawStats(), 400, 500 ), room, 9930, -33 );
    ASSERT_TRUE( fast.check.applies );
    ASSERT_TRUE( fast.scene == LookAssistScene::Shade );
    // ISO 400 at 1/50 s (bound 3.6) and ISO 100 at 1/127.99 s (bound 6.9999, just under 7): nothing rules night out, so
    // the colour-only check only measures and the verdict, receipt and picture are master's, bit for bit.
    struct Exposure { double iso; double shutterUs; };
    for( const Exposure e : { Exposure{ 400, 20000 }, Exposure{ 100, 7813 } } )
    {
        const LookAssistStats raw = withBound( fixtureRawStats(), e.iso, e.shutterUs );
        const WindowLitRun master = runWindowLit( raw, room, 9930, -33, false );
        const WindowLitRun r = runWindowLit( raw, room, 9930, -33 );
        ASSERT_TRUE( raw.hasSceneEv100Bound );
        ASSERT_FALSE( lookAssistExposureBoundExcludesNight( raw ) );
        ASSERT_TRUE( r.scene == LookAssistScene::Night );
        ASSERT_TRUE( identicalToMaster( r, master, room ) );
    }
}

TEST(LookAssistScene, TrueNightClipsKeepVerdictReceiptAndPictureWithOrWithoutTheBound)
{
    // Night light sources lighting the room's bright neutral surface. Exposures: none recorded; no aperture at night
    // settings (ISO 3200 1/50 s, ISO 6400 1/30 s: bounds 0.6 and -1.1); a recorded night EV100 (ISO 3200, 1/30 s, f/1.8).
    // Every one keeps master's verdict, receipt and picture bit for bit.
    struct Light { const char *name; int kelvin; int tint; };
    const Light lights[] = { { "sodium", 2100, 10 }, { "tungsten", 3200, 4 }, { "moonlight", 4100, 0 },
                             { "warm-led", 3000, 2 }, { "stage-led", 5600, -5 }, { "cool-led", 6500, 0 } };
    const LookAssistStats exposures[] = { fixtureRawStats(), withBound( fixtureRawStats(), 3200, 20000 ),
                                          withBound( fixtureRawStats(), 6400, 33333 ),
                                          withEv( fixtureRawStats(), 3200, 33333, 180 ) };
    for( const Light &l : lights )
        for( const LookAssistStats &raw : exposures )
        {
            const WindowRoom room = roomNeutralAt( l.kelvin, l.tint );
            const WindowLitRun master = runWindowLit( raw, room, l.kelvin, l.tint, false );
            const WindowLitRun r = runWindowLit( raw, room, l.kelvin, l.tint );
            ASSERT_TRUE( master.scene == LookAssistScene::Night );
            ASSERT_FALSE( r.check.exposureBound );
            ASSERT_TRUE( identicalToMaster( r, master, room ) );
        }

    // Even under the M16 bound (10.4, night ruled out by the exposure), a surface whose verified neutral balance is a
    // night light source's -- warmer than 6000 K, or outside the daylight window altogether -- is not made daylight.
    for( const Light &l : lights )
    {
        if( l.kelvin >= kLookAssistNotNightMinTemperature ) continue;
        const WindowRoom room = roomNeutralAt( l.kelvin, l.tint );
        // The solver answers the light's own neutral (verified as such) or overshoots it to the daylight rail.
        for( const int solved : { l.kelvin, 9930 } )
        {
            const WindowLitRun master = runWindowLit( m16Raw(), room, solved, l.tint, false );
            const WindowLitRun r = runWindowLit( m16Raw(), room, solved, l.tint );
            std::fprintf( stderr, "M16-NOT-NIGHT night=%s solved=%d reason=%s search=%s\n", l.name, solved,
                          qPrintable( r.check.reason ), qPrintable( r.check.search.result ) );
            ASSERT_TRUE( r.check.exposureBound );
            ASSERT_FALSE( r.check.evidence );
            ASSERT_TRUE( identicalToMaster( r, master, room ) );
        }
    }
}

TEST(LookAssistScene, TheSurfaceSearchConvergesToTheNeutralBalance)
{
    // From the base (6000 K / 0) and the solver's overshoot (9930 K / -33), wherever the surface is actually neutral.
    struct Neutral { int kelvin; int tint; };
    for( const Neutral n : { Neutral{ 6600, -3 }, Neutral{ 8000, -12 }, Neutral{ 5200, 4 }, Neutral{ 9500, -30 } } )
    {
        const WindowRoom room = roomNeutralAt( n.kelvin, n.tint );
        const LookAssistSurfaceProbe base = probeAt( room, 6000, 0 );
        const LookAssistSurfaceProbe solve = probeAt( room, 9930, -33 );
        ASSERT_TRUE( base.surface.valid && solve.surface.valid );
        const LookAssistRenderedPicture geometry = roomPicture( room, 6000, 0 );
        const LookAssistSurfaceSearch s = searchLookAssistNeutralSurfaceBalance(
            roomRenderer( room ), 1.2, geometry, base.surface.thumbnailX, base.surface.thumbnailY, daylightWindow(), base, solve );
        std::fprintf( stderr, "SURFACE-SEARCH neutral=%d/%d result=%s found=%d/%d renders=%d\n", n.kelvin, n.tint,
                      qPrintable( s.result ), s.temperature, s.tint, s.renders );
        ASSERT_TRUE( s.result == QStringLiteral("converged") );
        ASSERT_TRUE( s.converged );
        ASSERT_TRUE( s.renders <= kLookAssistSurfaceSearchMaxRenders );
        ASSERT_TRUE( std::fabs( 1.0e6 / s.temperature - 1.0e6 / n.kelvin ) <= 6.0 );
        ASSERT_TRUE( qAbs( s.tint - n.tint ) <= 6 );
        ASSERT_TRUE( std::fabs( s.surface.blueAmberAxis ) < 2.0 && std::fabs( s.surface.greenAxis ) < 2.0 );
    }
    // Already neutral at a probe: that probe is the answer, with no render at all.
    const WindowRoom room = roomNeutralAt( 6000, 0 );
    const LookAssistSurfaceProbe base = probeAt( room, 6000, 0 );
    const LookAssistSurfaceSearch atBase = searchLookAssistNeutralSurfaceBalance(
        roomRenderer( room ), 1.2, roomPicture( room, 6000, 0 ), base.surface.thumbnailX, base.surface.thumbnailY,
        daylightWindow(), base, probeAt( room, 9930, -33 ) );
    ASSERT_TRUE( atBase.converged );
    ASSERT_EQ( 0, atBase.renders );
    ASSERT_EQ( 6000, atBase.temperature );
    ASSERT_EQ( 0, atBase.tint );
}

TEST(LookAssistScene, TheSurfaceSearchRefusesWhatItCannotVerify)
{
    const WindowRoom room = m16Room();
    const LookAssistSurfaceProbe base = probeAt( room, 6000, 0 );
    const LookAssistSurfaceProbe solve = probeAt( room, 9930, -33 );
    const LookAssistRenderedPicture geometry = roomPicture( room, 6000, 0 );
    const int x = base.surface.thumbnailX, y = base.surface.thumbnailY;
    // No renderer.
    ASSERT_TRUE( searchLookAssistNeutralSurfaceBalance( LookAssistRenderBalanceFn(), 1.2, geometry, x, y, daylightWindow(),
                                                        base, solve ).result == QStringLiteral("unverifiable") );
    // A render that fails.
    const LookAssistSurfaceSearch failed = searchLookAssistNeutralSurfaceBalance(
        []( double, int, int, LookAssistRenderedPicture * ) { return false; }, 1.2, geometry, x, y, daylightWindow(), base, solve );
    ASSERT_TRUE( failed.result == QStringLiteral("unverifiable") );
    ASSERT_FALSE( failed.converged );
    ASSERT_EQ( 1, failed.renders );
    // A render of another geometry: the pixel is not the same surface.
    const LookAssistSurfaceSearch other = searchLookAssistNeutralSurfaceBalance(
        [room]( double, int k, int t, LookAssistRenderedPicture *out ) {
            *out = roomPicture( room, k, t ); out->downscaleFactor = kRoomDownscale + 1; return true; },
        1.2, geometry, x, y, daylightWindow(), base, solve );
    ASSERT_TRUE( other.result == QStringLiteral("unverifiable") );
    ASSERT_FALSE( other.converged );
    // A surface that was never measured.
    LookAssistSurfaceProbe invalid = base;
    invalid.surface.valid = false;
    ASSERT_TRUE( searchLookAssistNeutralSurfaceBalance( roomRenderer( room ), 1.2, geometry, x, y, daylightWindow(),
                                                        invalid, solve ).result == QStringLiteral("unverifiable") );
    // A surface neutral only outside the window (tungsten, 3000 K): no neutral balance inside it, so no answer.
    const WindowRoom tungsten = roomNeutralAt( 3000, 0 );
    const LookAssistSurfaceSearch outside = searchLookAssistNeutralSurfaceBalance(
        roomRenderer( tungsten ), 1.2, roomPicture( tungsten, 6000, 0 ), x, y, daylightWindow(),
        probeAt( tungsten, 6000, 0 ), probeAt( tungsten, 9930, -33 ) );
    ASSERT_TRUE( outside.result == QStringLiteral("not-converged") );
    ASSERT_FALSE( outside.converged );
    ASSERT_TRUE( outside.renders <= kLookAssistSurfaceSearchMaxRenders );

    // Through the check: the search cannot verify (its renders fail), so no evidence and master's night, bit for bit.
    const WindowLitRun master = runWindowLit( m16Raw(), room, 9930, -33, false );
    const WindowLitRun r = runWindowLit( m16Raw(), room, 9930, -33, true, true, Renderer::FailsAfterVerification );
    ASSERT_TRUE( r.check.exposureBound );
    ASSERT_TRUE( r.check.search.result == QStringLiteral("unverifiable") );
    ASSERT_TRUE( r.check.reason == QStringLiteral("unverified-at-solution") );
    ASSERT_TRUE( identicalToMaster( r, master, room ) );
    LookAssistDecisionTrace trace;
    lookAssistTraceSurfaceSearch( &trace, r.check );
    ASSERT_TRUE( lookAssistDecisionLogFields( r.stats, trace ).endsWith(
        QStringLiteral("surface_search=unverifiable surface_search_balance=NA") ) );
}

TEST(LookAssistScene, EveryLiveNotNightConjunctIsNeededOnItsOwn)
{
    // Each case breaks exactly ONE conjunct of the M16 run (which applies); the verdict, receipt and picture stay master's.
    const WindowRoom room = m16Room();
    // The patch picture is not a lit picture (under 5 % of it at luma 100): a bright subject on a black field.
    {
        const WindowLitRun master = runWindowLit( m16Raw(), room, 9930, -33, false, true, Renderer::Room, 90.0 );
        const WindowLitRun r = runWindowLit( m16Raw(), room, 9930, -33, true, true, Renderer::Room, 90.0 );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-lit-picture") );
        ASSERT_TRUE( identicalToMaster( r, master, room ) );
        // ... and at the edge it holds.
        ASSERT_TRUE( runWindowLit( m16Raw(), room, 9930, -33, true, true, Renderer::Room, 100.0 ).check.applies );
    }
    // The exposure does not rule night out: a dual-ISO recovery of 4 stops over a bound of 8 leaves 4.
    {
        const LookAssistStats raw = withBound( fixtureRawStats(), 100, 3906, 1600 );
        const WindowLitRun r = runWindowLit( raw, room, 9930, -33 );
        ASSERT_FALSE( r.check.exposureBound );
        ASSERT_TRUE( r.check.reason == QStringLiteral("unverified-at-solution") );   // the colour-only check, measured
        ASSERT_TRUE( identicalToMaster( r, runWindowLit( raw, room, 9930, -33, false ), room ) );
    }
    // The verified balance is warmer than 6000 K (a neutral surface under 5800 K light): not daylight.
    {
        const WindowRoom warm = roomNeutralAt( 5800, -3 );
        const WindowLitRun r = runWindowLit( m16Raw(), warm, 9930, -33 );
        ASSERT_TRUE( r.check.search.converged );
        ASSERT_TRUE( r.check.search.temperature < kLookAssistNotNightMinTemperature );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-daylight-locus") );
        ASSERT_TRUE( identicalToMaster( r, runWindowLit( m16Raw(), warm, 9930, -33, false ), warm ) );
    }
    // The verified balance is off the daylight tint (+15: a green fluorescent correction), verified directly.
    {
        const WindowRoom green = roomNeutralAt( 7000, 15 );
        const WindowLitRun r = runWindowLit( m16Raw(), green, 7000, 15 );
        ASSERT_TRUE( r.check.reason == QStringLiteral("not-daylight-locus") );
        ASSERT_TRUE( identicalToMaster( r, runWindowLit( m16Raw(), green, 7000, 15, false ), green ) );
    }
    // The solve was found on the RAW thumbnail: the check never gets to the search.
    {
        const WindowLitRun raw = runWindowLit( m16Raw(), room, 9930, -33, true, false );
        ASSERT_TRUE( raw.check.reason == QStringLiteral("not-processed-solve") );
        ASSERT_TRUE( identicalToMaster( raw, runWindowLit( m16Raw(), room, 9930, -33, false, false ), room ) );
    }
    // A recorded aperture: never a candidate, whatever the bound would have said.
    {
        const LookAssistStats recorded = withEv( fixtureRawStats(), 100, 737, 140 );   // EV100 10.9: not daylight-bright
        const WindowLitRun r = runWindowLit( recorded, room, 9930, -33 );
        ASSERT_FALSE( r.check.candidate );
        ASSERT_TRUE( identicalToMaster( r, runWindowLit( recorded, room, 9930, -33, false ), room ) );
    }
}
