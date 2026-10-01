// LOOK-ASSIST-SCENE-CLASSIFY-1: every tracked fixture clip, through the real thumbnail path and the
// real headless Look Assist (the CPU path; the GUI and its CUDA/GL display path call the same shared
// module -- see console test LookAssistScene.CpuAndCudaShareOneClassifier).
//
// Both tracked clips are the same daylight pool scene (ISO 100, 1/2150 s, f/5.6: EV100 16). Their
// RAW thumbnail is a flat floor at the sensor black offset, which is what made them read as NIGHT.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/batch/LookAssistAnalysis.h"
#include "../../src/batch/ReceiptApplier.h"

#include <QString>
#include <vector>

using namespace lookassist;

namespace
{

struct FixtureClip
{
    const char *file;
    int lastFrame;
};

const FixtureClip kTrackedFixtureClips[] = {
    { "tests/fixtures/clips/tiny_dual_iso.mlv", 1 },
    { "tests/fixtures/clips/large_dual_iso.mlv", 15 },
};

LookAssistStats rawThumbnailStats( mlvObject_t *video, int frame )
{
    const int rawW = video->RAWI.xRes;
    const int rawH = video->RAWI.yRes;
    int downscale = 6;
    if( rawW > 4000 || rawH > 2500 ) downscale = 12;
    else if( rawW > 2800 || rawH > 1900 ) downscale = 10;
    else if( rawW > 1800 || rawH > 1200 ) downscale = 8;
    const int w = rawW / downscale;
    const int h = rawH / downscale;
    std::vector<unsigned char> thumbnail( static_cast<size_t>( w ) * h * 3 );
    get_area_average_downscale_raw_thumnail( video, frame, downscale, thumbnail.data() );
    LookAssistStats stats = analyzeLookAssistThumbnail( thumbnail.data(), w, h );
    lookAssistSetSceneEv100( &stats,
                             video->EXPO.isoValue,
                             static_cast<double>( video->EXPO.shutterValue ),
                             video->LENS.aperture );
    return stats;
}

} // namespace

TEST(LookAssistFixtureScene, EveryTrackedFixtureClipReadsAsDaylightNotNight)
{
    for( const FixtureClip &clip : kTrackedFixtureClips )
    {
        MlvPipelineFixture fixture;
        QString error_message;
        ASSERT_TRUE( fixture.openClipFile( repo_file_path( QString::fromLatin1( clip.file ) ), &error_message ) );
        ASSERT_TRUE( fixture.applyReceipt( &error_message ) );

        for( int frame = 0; frame <= clip.lastFrame; frame += 5 )
        {
            const LookAssistStats stats = rawThumbnailStats( fixture.video(), frame );
            // Regression guard: the display statistics alone ARE night-like (flat dark floor) ...
            ASSERT_TRUE( stats.median < 60.0 );
            ASSERT_TRUE( stats.dynamicRange <= 24.0 );
            // ... but the recorded exposure is full daylight, and that decides.
            ASSERT_TRUE( stats.hasSceneEv100 );
            ASSERT_TRUE( stats.sceneEv100 > 15.0 && stats.sceneEv100 < 17.0 );
            const LookAssistScene scene = classifyLookAssistScene( stats );
            ASSERT_TRUE( scene != LookAssistScene::Night );
            ASSERT_TRUE( scene != LookAssistScene::ArtificialLights );
            ASSERT_TRUE( scene == LookAssistScene::Shade );
        }
    }
}

TEST(LookAssistFixtureScene, HeadlessLookAssistKeepsDaylightWhiteBalanceNeutralOnEveryFixtureFrame)
{
    for( const FixtureClip &clip : kTrackedFixtureClips )
    {
        for( int frame = 0; frame <= clip.lastFrame; frame += 5 )
        {
            MlvPipelineFixture fixture;
            QString error_message;
            ASSERT_TRUE( fixture.openClipFile( repo_file_path( QString::fromLatin1( clip.file ) ), &error_message ) );
            ASSERT_TRUE( fixture.applyReceipt( &error_message ) );

            ReceiptSettings &receipt = fixture.receipt();
            receipt.setLookAssistEnabled( true );
            receipt.setLookAssistBaselineValid( false );
            receipt.setExposure( 0 );
            receipt.setTemperature( -1 );
            receipt.setTint( 0 );
            ASSERT_TRUE( ReceiptApplier::applyHeadlessLookAssist(
                &receipt, fixture.video(), fixture.processing(), static_cast<uint32_t>( frame ) ) );

            // Neutral: the solved white balance stays on the daylight locus (the defect was 8594 K,
            // tint -23 -- a lavender cast). Tint in receipt units = tenths.
            ASSERT_TRUE( receipt.temperature() >= 4800 );
            ASSERT_TRUE( receipt.temperature() <= 7500 );
            ASSERT_TRUE( receipt.tint() >= -10 );
            ASSERT_TRUE( receipt.tint() <= 10 );
            // Not the night rescue (+174): a daylight lift, bounded by the Shade preset.
            ASSERT_TRUE( receipt.exposure() > 0 );
            ASSERT_TRUE( receipt.exposure() <= 180 );
            ASSERT_NEAR( static_cast<double>( receipt.temperature() ), fixture.processing()->kelvin, 0.0001 );
            ASSERT_NEAR( receipt.tint() / 10.0, fixture.processing()->wb_tint, 0.0001 );
        }
    }
}
