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
#include <cstdio>
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
TEST(LookAssistAnalysisRenderRace, AFractionalRawBlackGivesTheSamePictureBeforeAndAfterASync)
{
    for( const char *clip : kDualIsoFixtureClips )
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
// (findMlvWhiteBalanceAtAnalysisLevels) must give one answer in both states, at five patches on both fixtures,
// and match the live solver in the clip-level state (the headless applier's, unchanged).
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
            ASSERT_EQ( liveBefore[i][0], before[i][0] );
            ASSERT_EQ( liveBefore[i][1], before[i][1] );
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
        }
    }
    ASSERT_TRUE( liveSolverMoved > 0 ); // the injected state really moves an unprotected solve
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
