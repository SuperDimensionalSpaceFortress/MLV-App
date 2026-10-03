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

int colorDownscaleFor( mlvObject_t *video )
{
    const int rawW = video->RAWI.xRes;
    const int rawH = video->RAWI.yRes;
    int downscale = 6;
    if( rawW > 4000 || rawH > 2500 ) downscale = 12;
    else if( rawW > 2800 || rawH > 1900 ) downscale = 10;
    else if( rawW > 1800 || rawH > 1200 ) downscale = 8;
    return std::max( 3, downscale / 3 );
}

// Every picture a Look Assist analysis takes of one frame, through each primitive the GUI and the headless
// applier use: the planned-exposure render (scene evidence), the live-state colour render (night colour),
// the balance renderer both cache-backed and isolated (patch verification / surface search), and the
// display meter's cache-free render.
struct AnalysisPictures
{
    std::vector<unsigned char> atExposure;
    std::vector<unsigned char> liveColour;
    std::vector<unsigned char> atBalance;
    std::vector<unsigned char> atBalanceIsolated;
    std::vector<unsigned char> cacheFree;
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
    processingObject_t *clone = processingCloneForAnalysis( video->processing );
    if( !clone ) return false;
    const int rendered = get_area_average_downscale_thumnail_with_processing_cachefree(
        video, frame, cd, 1, clone, nullptr, out->cacheFree.data() );
    processingFreeClone( clone );
    return rendered != 0;
}

void assertSamePictures( const AnalysisPictures &a, const AnalysisPictures &b )
{
    ASSERT_TRUE( a.atExposure == b.atExposure );
    ASSERT_TRUE( a.liveColour == b.liveColour );
    ASSERT_TRUE( a.atBalance == b.atBalance );
    ASSERT_TRUE( a.atBalanceIsolated == b.atBalanceIsolated );
    ASSERT_TRUE( a.cacheFree == b.cacheFree );
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
