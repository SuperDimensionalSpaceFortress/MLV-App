// LOOK-ASSIST-ANALYSIS-RENDER-RACE-1: a Look Assist analysis render clones the live processing object.
// On an HQ Dual ISO restricted-range lossless clip (both tracked fixtures, like the owner's M16 footage:
// LJ92, black 2047, white 6000) the recon rescales the data to a ~63000 16-bit domain, and the live
// object only takes those levels once a render SYNCS them -- the classic cores per frame, the GPU route
// on the main thread at render dispatch. Whether that sync had happened when the analysis ran was GUI
// timing, and it changed the analysed picture ~2.6x (M16: processed median 10 / p95 144 vs 2 / 74),
// which flipped the colour source and the verdict on some runs.
//
// These tests inject that state (mlvSyncProcessingDualIsoBlackWhiteLevels, or a classic display render,
// exactly what a render does to the live object) between two analysis renders of the same frame and
// require the analysed picture to be byte-identical, through every analysis render primitive.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include "../../src/batch/LookAssistAnalysis.h"
#include "../../src/batch/ReceiptApplier.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <vector>

using namespace lookassist;

namespace
{

const char *const kDualIsoFixtureClips[] = {
    "tests/fixtures/clips/tiny_dual_iso.mlv",
    "tests/fixtures/clips/large_dual_iso.mlv",
};

// The RAW-thumbnail downscale: the display meter renders at it (even, so it takes the cache-free SCALED
// source); the colour renders use a third of it (3, which no source scale divides: the full-resolution one).
int thumbnailDownscaleFor( mlvObject_t *video )
{
    const int rawW = video->RAWI.xRes;
    const int rawH = video->RAWI.yRes;
    int downscale = 6;
    if( rawW > 4000 || rawH > 2500 ) downscale = 12;
    else if( rawW > 2800 || rawH > 1900 ) downscale = 10;
    else if( rawW > 1800 || rawH > 1200 ) downscale = 8;
    return downscale;
}

int colorDownscaleFor( mlvObject_t *video )
{
    return std::max( 3, thumbnailDownscaleFor( video ) / 3 );
}

// Every picture a Look Assist analysis takes of one frame, through each primitive the GUI and the headless
// applier use: the planned-exposure render (scene evidence), the live-state colour render (night colour),
// the balance renderer both cache-backed and isolated (patch verification / surface search), and the
// cache-free render at the colour downscale (full-resolution source) and at the display meter's (scaled).
struct AnalysisPictures
{
    std::vector<unsigned char> atExposure;
    std::vector<unsigned char> liveColour;
    std::vector<unsigned char> atBalance;
    std::vector<unsigned char> atBalanceIsolated;
    std::vector<unsigned char> cacheFree;
    std::vector<unsigned char> displayMeter;
};

bool renderAnalysisPictures( mlvObject_t *video, int frame, AnalysisPictures *out )
{
    const int cd = colorDownscaleFor( video );
    const size_t bytes = static_cast<size_t>( video->RAWI.xRes / cd ) * ( video->RAWI.yRes / cd ) * 3;
    out->atExposure.assign( bytes, 0 );
    out->liveColour.assign( bytes, 0 );
    out->atBalance.assign( bytes, 0 );
    out->atBalanceIsolated.assign( bytes, 0 );
    out->cacheFree.assign( bytes, 0 );
    if( !ReceiptApplier::processedThumbnailAtExposure( video, frame, cd, 1, 1.0, out->atExposure.data() ) )
        return false;
    get_area_average_downscale_thumnail( video, frame, cd, 1, out->liveColour.data() );
    if( !ReceiptApplier::processedThumbnailAtBalance( video, frame, cd, 1, 0.5, 6600, 0, false, out->atBalance.data() ) )
        return false;
    if( !ReceiptApplier::processedThumbnailAtBalance( video, frame, cd, 1, 0.5, 6600, 0, true,
                                                      out->atBalanceIsolated.data() ) )
        return false;
    const int md = thumbnailDownscaleFor( video );
    out->displayMeter.assign( static_cast<size_t>( video->RAWI.xRes / md ) * ( video->RAWI.yRes / md ) * 3, 0 );
    // One fresh clone of the live object per render, as each caller takes it.
    processingObject_t *clone = processingCloneForAnalysis( video->processing );
    if( !clone ) return false;
    const int rendered = get_area_average_downscale_thumnail_with_processing_cachefree(
        video, frame, cd, 1, clone, nullptr, out->cacheFree.data() );
    processingFreeClone( clone );
    processingObject_t *meterClone = processingCloneForAnalysis( video->processing );
    if( !meterClone ) return false;
    const int meterRendered = get_area_average_downscale_thumnail_with_processing_cachefree(
        video, frame, md, 1, meterClone, nullptr, out->displayMeter.data() );
    processingFreeClone( meterClone );
    return rendered != 0 && meterRendered != 0;
}

void assertSamePictures( const AnalysisPictures &a, const AnalysisPictures &b )
{
    ASSERT_TRUE( a.atExposure == b.atExposure );
    ASSERT_TRUE( a.liveColour == b.liveColour );
    ASSERT_TRUE( a.atBalance == b.atBalance );
    ASSERT_TRUE( a.atBalanceIsolated == b.atBalanceIsolated );
    ASSERT_TRUE( a.cacheFree == b.cacheFree );
    ASSERT_TRUE( a.displayMeter == b.displayMeter );
}

bool openFixture( MlvPipelineFixture &fixture, const char *clip )
{
    QString error_message;
    return fixture.openClipFile( repo_file_path( QString::fromLatin1( clip ) ), &error_message )
        && fixture.applyReceipt( &error_message );
}

} // namespace

TEST(LookAssistAnalysisRenderRace, TheAnalysedPictureDoesNotDependOnWhetherARenderSyncedTheLevels)
{
    for( const char *clip : kDualIsoFixtureClips )
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFixture( fixture, clip ) );
        mlvObject_t *video = fixture.video();
        ASSERT_TRUE( llrpHQDualIso( video ) != 0 );

        // Clip-open state: no render has synced the live levels yet (the usual GUI timing; always the
        // headless applier's state).
        const int clipWhite = video->processing->white_level;
        AnalysisPictures beforeSync;
        ASSERT_TRUE( renderAnalysisPictures( video, 0, &beforeSync ) );
        ASSERT_EQ( clipWhite, video->processing->white_level ); // analysis never mutates the live object

        // Inject the race: a render was dispatched first and synced the live levels to the recon output.
        mlvSyncProcessingDualIsoBlackWhiteLevels( video );
        const int syncedWhite = video->processing->white_level;
        ASSERT_TRUE( syncedWhite > clipWhite * 2 ); // the injected state really is the other one (23832 -> 62805)

        AnalysisPictures afterSync;
        ASSERT_TRUE( renderAnalysisPictures( video, 0, &afterSync ) );
        ASSERT_EQ( syncedWhite, video->processing->white_level );
        assertSamePictures( beforeSync, afterSync );

        // A classic display render (which syncs on its own) in between changes nothing either.
        resetMlvCache( video );
        resetMlvCachedFrame( video );
        (void)fixture.renderFrame8( 0 );
        AnalysisPictures afterDisplay;
        ASSERT_TRUE( renderAnalysisPictures( video, 0, &afterDisplay ) );
        assertSamePictures( beforeSync, afterDisplay );
    }
}

// A fractional raw black (Raw Fix on, black control 2047.9): the control writes the fraction into the
// processing object (8191.6 at 14 bit) but the integer into RAWI, which is all the recon reads. A sync
// replaces the fraction with the recon's levels, so the analysis black must come from RAWI in both states
// or the level table -- and the analysed picture -- again depends on whether a render synced first.
// The tiny fixture only (same 2047/6000 shape as the large one): this shard shares a 240 s CI bound.
TEST(LookAssistAnalysisRenderRace, AFractionalRawBlackGivesTheSamePictureBeforeAndAfterASync)
{
    for( const char *clip : { kDualIsoFixtureClips[0] } )
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFixture( fixture, clip ) );
        mlvObject_t *video = fixture.video();
        ASSERT_TRUE( llrpHQDualIso( video ) != 0 );

        // Exactly what the raw-black slider does (MainWindow::on_horizontalSliderRawBlack_valueChanged).
        const double rawBlack = getMlvBlackLevel( video ) + 0.9;
        setMlvBlackLevel( video, rawBlack );
        processingSetBlackLevel( video->processing, rawBlack, getMlvBitdepth( video ) );
        llrpResetFpmStatus( video );
        llrpResetBpmStatus( video );
        resetMlvCache( video );
        resetMlvCachedFrame( video );
        const float fractionalBlack = video->processing->black_level;
        ASSERT_TRUE( fractionalBlack != static_cast<float>( static_cast<int>( fractionalBlack ) ) );

        AnalysisPictures beforeSync;
        ASSERT_TRUE( renderAnalysisPictures( video, 0, &beforeSync ) );
        ASSERT_TRUE( video->processing->black_level == fractionalBlack ); // the live object keeps the fraction

        mlvSyncProcessingDualIsoBlackWhiteLevels( video );
        ASSERT_TRUE( video->processing->black_level != fractionalBlack );

        AnalysisPictures afterSync;
        ASSERT_TRUE( renderAnalysisPictures( video, 0, &afterSync ) );
        assertSamePictures( beforeSync, afterSync );
    }
}

// The neutral-patch white-balance solve. The live solver (findMlvWhiteBalance, the manual picker) runs on the
// LIVE object, whose levels a sync changes ~2.6x, and that moves its answer where a channel reaches the 16-bit
// clamp (tiny_dual_iso, patch 1: 10000/-37 at clip levels, 9990/-37 synced). Look Assist's solve
// (findMlvWhiteBalanceAtAnalysisLevels) must give one answer in both states, at five patches on both fixtures.
// LOOK-ASSIST-ANALYSIS-TRUE-LEVELS-1 changed WHICH answer: it is the live solver's in the SYNCED state (the display's
// levels; TheWhiteBalanceSolveUsesTheDisplayLevels pins that), no longer the clip-level state's, so the per-patch
// equality with the live solver moved from the clip-level state to the synced one below.
TEST(LookAssistAnalysisRenderRace, TheWhiteBalanceSolveDoesNotDependOnWhetherARenderSyncedTheLevels)
{
    int liveSolverMoved = 0;
    for( const char *clip : kDualIsoFixtureClips )
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFixture( fixture, clip ) );
        mlvObject_t *video = fixture.video();
        ASSERT_TRUE( llrpHQDualIso( video ) != 0 );
        const int w = getMlvWidth( video );
        const int h = getMlvHeight( video );
        const int patches[5][2] = { { w / 2, h / 2 }, { w / 4, h / 4 }, { 3 * w / 4, h / 4 },
                                    { w / 4, 3 * h / 4 }, { 3 * w / 4, 3 * h / 4 } };

        int before[5][2] = {};
        int liveBefore[5][2] = {};
        for( int i = 0; i < 5; ++i )
        {
            findMlvWhiteBalanceAtAnalysisLevels( video, 0, patches[i][0], patches[i][1], &before[i][0], &before[i][1], 0 );
            findMlvWhiteBalance( video, 0, patches[i][0], patches[i][1], &liveBefore[i][0], &liveBefore[i][1], 0 );
        }

        const int clipWhite = video->processing->white_level;
        mlvSyncProcessingDualIsoBlackWhiteLevels( video );
        const int syncedWhite = video->processing->white_level;
        ASSERT_TRUE( syncedWhite > clipWhite * 2 );

        for( int i = 0; i < 5; ++i )
        {
            int temperature = 0, tint = 0;
            findMlvWhiteBalanceAtAnalysisLevels( video, 0, patches[i][0], patches[i][1], &temperature, &tint, 0 );
            ASSERT_EQ( syncedWhite, video->processing->white_level ); // the live object is not touched
            int liveTemperature = 0, liveTint = 0;
            findMlvWhiteBalance( video, 0, patches[i][0], patches[i][1], &liveTemperature, &liveTint, 0 );
            std::printf( "[wb-solve] %s patch %d: analysis %d/%d -> %d/%d, live %d/%d -> %d/%d\n", clip, i,
                         before[i][0], before[i][1], temperature, tint,
                         liveBefore[i][0], liveBefore[i][1], liveTemperature, liveTint );
            if( liveTemperature != liveBefore[i][0] || liveTint != liveBefore[i][1] ) ++liveSolverMoved;
            ASSERT_EQ( before[i][0], temperature );
            ASSERT_EQ( before[i][1], tint );
            ASSERT_EQ( liveTemperature, temperature );   // the synced (display) state's answer
            ASSERT_EQ( liveTint, tint );
        }
    }
    ASSERT_TRUE( liveSolverMoved > 0 ); // the injected state really moves an unprotected solve
}

namespace
{

// The picture the DISPLAY shows: the classic display render of the frame through the live object (which syncs the
// levels to the recon output on its own), at `stops` of exposure, as luma statistics.
LookAssistStats displayedPictureStats( MlvPipelineFixture &fixture, int frame, double stops )
{
    processingSetExposureStops( fixture.processing(), stops );
    resetMlvCache( fixture.video() );
    resetMlvCachedFrame( fixture.video() );
    const std::vector<uint8_t> rgb = fixture.renderFrame8( static_cast<uint64_t>( frame ) );
    return analyzeLookAssistThumbnail( rgb.data(), fixture.width(), fixture.height() );
}

bool sameMedian( double analysed, double displayed )
{
    // An analysis thumbnail is an area average, the display a full-size render: the medians of one picture agree to a
    // few code values (measured: 1, at 0, 1 and 1.8 EV). The clip-level picture is 2.7x off at 0 EV (85 vs 30).
    return std::fabs( analysed - displayed ) <= std::max( 4.0, displayed * 0.10 );
}

} // namespace

// LOOK-ASSIST-ANALYSIS-TRUE-LEVELS-1, on both HQ dual-ISO restricted-range lossless fixtures, in the headless state (no
// render has synced the live levels):
//  - the DISPLAY METER render (MLV_PROCESSED_THUMBNAIL_DISPLAY_LEVELS) is the picture the display shows at the same
//    exposure. Its median is the exposure answer: at #253's clip levels it read 85 where the display shows 30, so the
//    display-space meter set the exposure log2(85/30) = 1.5 EV too low (RED on master);
//  - every JUDGEMENT render is the display's picture at its exposure plus the stated calibration, log2(display range /
//    clip range) = 1.82 EV here: the brighter picture every Look Assist threshold is calibrated on (not some other
//    level mapping), and really brighter than the display at the same exposure.
TEST(LookAssistAnalysisRenderRace, TheMeterReadsTheDisplayedPictureAndJudgementsItsStatedCalibration)
{
    for( const char *clip : kDualIsoFixtureClips )
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFixture( fixture, clip ) );
        mlvObject_t *video = fixture.video();
        ASSERT_TRUE( llrpHQDualIso( video ) != 0 );
        const int cd = colorDownscaleFor( video );
        const int w = video->RAWI.xRes / cd;
        const int h = video->RAWI.yRes / cd;
        const int md = thumbnailDownscaleFor( video );
        const int mw = video->RAWI.xRes / md;
        const int mh = video->RAWI.yRes / md;
        const int kelvin = static_cast<int>( processingGetWhiteBalanceKelvin( video->processing ) );
        const float clipBlack = video->processing->black_level;
        const int clipWhite = video->processing->white_level;

        for( const double stops : { 0.0, 1.0, 1.8 } )
        {
            // Analysis first, in the headless state: the live object still holds the clip levels.
            processingSetBlackAndWhiteLevel( video->processing, getMlvBlackLevel( video ), getMlvWhiteLevel( video ),
                                             getMlvBitdepth( video ) );
            ASSERT_EQ( clipWhite, video->processing->white_level );
            std::vector<unsigned char> atExposure( static_cast<size_t>( w ) * h * 3 );
            std::vector<unsigned char> atBalance( atExposure.size() );
            std::vector<unsigned char> meter( static_cast<size_t>( mw ) * mh * 3 );
            // The meter first: on the first pass it is the clip's first raw read, so no render has published the recon's
            // levels yet and only the levels its own (isolated, never publishing) run recorded can be the display's.
            mlv_processed_thumbnail_settings_t meterSettings;
            std::memset( &meterSettings, 0, sizeof( meterSettings ) );
            meterSettings.flags = MLV_PROCESSED_THUMBNAIL_APPLY_EXPOSURE | MLV_PROCESSED_THUMBNAIL_DISPLAY_LEVELS;
            meterSettings.exposure_stops = stops;
            processingObject_t *meterClone = processingCloneForAnalysis( video->processing );
            ASSERT_TRUE( meterClone != nullptr );
            ASSERT_TRUE( get_area_average_downscale_thumnail_with_processing_cachefree(
                             video, 0, md, 1, meterClone, &meterSettings, meter.data() ) != 0 );
            processingFreeClone( meterClone );
            ASSERT_TRUE( ReceiptApplier::processedThumbnailAtExposure( video, 0, cd, 1, stops, atExposure.data() ) );
            ASSERT_TRUE( ReceiptApplier::processedThumbnailAtBalance( video, 0, cd, 1, stops, kelvin, 0, true, atBalance.data() ) );
            ASSERT_EQ( clipWhite, video->processing->white_level );   // the analysis never syncs the live object

            const LookAssistStats judged = analyzeLookAssistThumbnail( atExposure.data(), w, h );
            const LookAssistStats balanced = analyzeLookAssistThumbnail( atBalance.data(), w, h );
            const LookAssistStats metered = analyzeLookAssistThumbnail( meter.data(), mw, mh );
            const LookAssistStats displayed = displayedPictureStats( fixture, 0, stops );
            // The calibration, from the two level states the processing object itself holds (16-bit domain).
            const double calibrationStops = std::log2( ( video->processing->white_level - video->processing->black_level )
                                                     / static_cast<double>( clipWhite - clipBlack ) );
            ASSERT_TRUE( calibrationStops > 1.7 && calibrationStops < 1.95 );
            const LookAssistStats displayedCalibrated = displayedPictureStats( fixture, 0, stops + calibrationStops );
            std::printf( "[true-levels] %s %.1f EV: display %.0f/%.0f, meter %.0f/%.0f | judgement %.0f/%.0f, at-balance "
                         "%.0f/%.0f, display at +%.2f EV %.0f/%.0f\n", clip, stops, displayed.median, displayed.p95,
                         metered.median, metered.p95, judged.median, judged.p95, balanced.median, balanced.p95,
                         calibrationStops, displayedCalibrated.median, displayedCalibrated.p95 );
            ASSERT_TRUE( sameMedian( metered.median, displayed.median ) );
            ASSERT_TRUE( sameMedian( judged.median, displayedCalibrated.median ) );
            ASSERT_TRUE( sameMedian( balanced.median, displayedCalibrated.median ) );
            ASSERT_TRUE( judged.median > displayed.median * 1.6 );
        }
    }
}

// The neutral-patch solve at the analysis levels is the solve at the DISPLAY's levels: the live solver run once a render
// has synced the live object (the state the picture on screen is in), at five patches on both fixtures. A RED-first
// guard on the WB input (at the clip levels it differs where a channel reaches the clamp: 10000/-37 vs 9990/-37).
TEST(LookAssistAnalysisRenderRace, TheWhiteBalanceSolveUsesTheDisplayLevels)
{
    int clipLevelsDiffer = 0;
    for( const char *clip : kDualIsoFixtureClips )
    {
        MlvPipelineFixture fixture;
        ASSERT_TRUE( openFixture( fixture, clip ) );
        mlvObject_t *video = fixture.video();
        const int w = getMlvWidth( video );
        const int h = getMlvHeight( video );
        const int patches[5][2] = { { w / 2, h / 2 }, { w / 4, h / 4 }, { 3 * w / 4, h / 4 },
                                    { w / 4, 3 * h / 4 }, { 3 * w / 4, 3 * h / 4 } };
        int analysis[5][2] = {};
        int atClip[5][2] = {};
        for( int i = 0; i < 5; ++i )
        {
            findMlvWhiteBalanceAtAnalysisLevels( video, 0, patches[i][0], patches[i][1], &analysis[i][0], &analysis[i][1], 0 );
            findMlvWhiteBalance( video, 0, patches[i][0], patches[i][1], &atClip[i][0], &atClip[i][1], 0 );
        }
        resetMlvCache( video );
        resetMlvCachedFrame( video );
        (void)fixture.renderFrame8( 0 );   // the display render: syncs the live levels to the recon output
        ASSERT_TRUE( video->processing->white_level > getMlvWhiteLevel( video ) * 2 );
        for( int i = 0; i < 5; ++i )
        {
            int displayTemperature = 0, displayTint = 0;
            findMlvWhiteBalance( video, 0, patches[i][0], patches[i][1], &displayTemperature, &displayTint, 0 );
            std::printf( "[true-levels-wb] %s patch %d: analysis %d/%d, live at display levels %d/%d, at clip levels %d/%d\n",
                         clip, i, analysis[i][0], analysis[i][1], displayTemperature, displayTint, atClip[i][0], atClip[i][1] );
            ASSERT_EQ( displayTemperature, analysis[i][0] );
            ASSERT_EQ( displayTint, analysis[i][1] );
            if( atClip[i][0] != displayTemperature || atClip[i][1] != displayTint ) ++clipLevelsDiffer;
        }
    }
    ASSERT_TRUE( clipLevelsDiffer > 0 );   // the levels really are an input of the solve on these fixtures
}

// Fail closed (sol, PR #253): with no analysis clone the solve leaves the caller's balance alone; it never falls back to
// the live object, whose levels are the race. No processing object to clone is the one failure a test can force.
TEST(LookAssistAnalysisRenderRace, TheWhiteBalanceSolveFailsClosedWithoutAnAnalysisClone)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE( openFixture( fixture, kDualIsoFixtureClips[0] ) );
    mlvObject_t *video = fixture.video();
    processingObject_t *live = video->processing;
    video->processing = nullptr;
    int temperature = 1234, tint = -7;
    findMlvWhiteBalanceAtAnalysisLevels( video, 0, getMlvWidth( video ) / 2, getMlvHeight( video ) / 2, &temperature, &tint, 0 );
    int isolatedTemperature = 1234, isolatedTint = -7;
    findMlvWhiteBalanceIsolated( video, 0, getMlvWidth( video ) / 2, getMlvHeight( video ) / 2,
                                 &isolatedTemperature, &isolatedTint, 0 );
    video->processing = live;
    ASSERT_EQ( 1234, temperature );
    ASSERT_EQ( -7, tint );
    ASSERT_EQ( 1234, isolatedTemperature );
    ASSERT_EQ( -7, isolatedTint );
}

// Repeat-run stability: twenty analysis passes over the same frame, alternating the injected state, give
// one picture and one statistics record.
TEST(LookAssistAnalysisRenderRace, TwentyPassesAlternatingTheSyncStateGiveOnePicture)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE( openFixture( fixture, kDualIsoFixtureClips[0] ) );
    mlvObject_t *video = fixture.video();
    const int cd = colorDownscaleFor( video );
    const int w = video->RAWI.xRes / cd;
    const int h = video->RAWI.yRes / cd;
    const int clipBlack = getMlvBlackLevel( video );
    const int clipWhite = getMlvWhiteLevel( video );
    const int clipDepth = getMlvBitdepth( video );

    std::vector<unsigned char> first;
    LookAssistStats firstStats;
    for( int pass = 0; pass < 20; ++pass )
    {
        if( pass % 2 ) mlvSyncProcessingDualIsoBlackWhiteLevels( video );
        else processingSetBlackAndWhiteLevel( video->processing, clipBlack, clipWhite, clipDepth );
        std::vector<unsigned char> picture( static_cast<size_t>( w ) * h * 3 );
        ASSERT_TRUE( ReceiptApplier::processedThumbnailAtExposure( video, 0, cd, 1, 0.0, picture.data() ) );
        const LookAssistStats stats = analyzeLookAssistThumbnail( picture.data(), w, h );
        if( pass == 0 )
        {
            first = picture;
            firstStats = stats;
            continue;
        }
        ASSERT_TRUE( picture == first );
        ASSERT_EQ( firstStats.median, stats.median );
        ASSERT_EQ( firstStats.p95, stats.p95 );
        ASSERT_EQ( firstStats.balanceSamples, stats.balanceSamples );
    }
}
