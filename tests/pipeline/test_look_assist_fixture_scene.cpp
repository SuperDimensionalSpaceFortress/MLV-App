// LOOK-ASSIST-SCENE-CLASSIFY-1: every tracked fixture clip, through the real thumbnail path and the
// real headless Look Assist (the CPU path; the GUI and its CUDA/GL display path call the same shared
// module -- see console test LookAssistScene.CpuAndCudaShareOneClassifier).
//
// Both tracked clips are the same daylight pool scene (ISO 100, 1/2150 s, f/5.6: EV100 16). Their
// RAW thumbnail is a flat floor at the sensor black offset, which is what made them read as NIGHT.
// The test is on the PICTURE as well as the verdict: a correct class with a wrong balance is a
// regression (r1: scene right, deck cast chroma 11.5 -> 22.4).
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/batch/BatchLogger.h"
#include "../../src/batch/LookAssistAnalysis.h"
#include "../../src/batch/ReceiptApplier.h"

#include <QFile>
#include <QString>
#include <QTemporaryDir>
#include <cmath>
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

// CIELAB chroma of the mean colour of a fixed region of the frame: the concrete pool deck, lower
// left (rows 65-98 %, columns 2-25 %). A physically near-neutral surface, so an ESTIMATE of cast,
// not a calibrated grey. Same region and maths as the real-app sheet metrics (tools/profiling).
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
            // A flat RAW thumbnail is unusable for colour in ANY scene: the rendered picture is read.
            ASSERT_TRUE( lookAssistShouldAnalyzeProcessedColor( scene, stats ) );
        }
    }
}

TEST(LookAssistFixtureScene, HeadlessLookAssistSolvesDaylightWhiteBalanceFromTheRenderedPicture)
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

            QTemporaryDir temporary_dir;
            const QString log_path = temporary_dir.filePath( QStringLiteral("look_assist.log") );
            BatchLogger::init( log_path );
            const bool applied = ReceiptApplier::applyHeadlessLookAssist(
                &receipt, fixture.video(), fixture.processing(), static_cast<uint32_t>( frame ) );
            BatchLogger::shutdown();
            ASSERT_TRUE( applied );

            QFile log_file( log_path );
            ASSERT_TRUE( log_file.open( QIODevice::ReadOnly | QIODevice::Text ) );
            const QByteArray log = log_file.readAll();
            // The verdict AND the source of the balance: the scene is shade, and the white balance
            // came from a neutral patch of the RENDERED picture (r1 left it on the base: source=none).
            ASSERT_TRUE( log.contains( "scene=shade" ) );
            ASSERT_TRUE( log.contains( "autoWbValid=true" ) );
            ASSERT_TRUE( log.contains( "autoWbSource=processed-neutral-patch" ) );
            ASSERT_TRUE( log.contains( "autoWbDamping=1.000" ) );   // daylight solve applied undamped

            // Inside the daylight bounds, and moved off the base: a blue deck needs warming.
            ASSERT_TRUE( receipt.temperature() >= 4800 );
            ASSERT_TRUE( receipt.temperature() <= 10000 );
            ASSERT_TRUE( receipt.tint() >= -35 );
            ASSERT_TRUE( receipt.tint() <= 10 );
            ASSERT_TRUE( receipt.temperature() > 6000 );
            // Not the night rescue (+174): a daylight lift, bounded by the Shade preset.
            ASSERT_TRUE( receipt.exposure() > 0 );
            ASSERT_TRUE( receipt.exposure() <= 180 );
            // The solved balance reached the pipeline (tint is stored through the non-linear render curve).
            ASSERT_NEAR( static_cast<double>( receipt.temperature() ), fixture.processing()->kelvin, 0.0001 );
            ASSERT_TRUE( fixture.processing()->wb_tint < 0.0 );

            // The PICTURE: the deck (a near-neutral surface) is closer to neutral than at the base
            // balance (6000 K, tint 0) and than the damped solve master produced (8594 K, tint -23).
            // Only while the deck is in view: from about frame 6 of the large clip a child stands in
            // the measured region, and that is skin and swimwear, not concrete.
            if( frame > 5 ) continue;
            const int width = fixture.width();
            const int height = fixture.height();
            const int applied_temperature = receipt.temperature();
            const int applied_tint = receipt.tint();
            const double base_cast =
                deckCastChroma( renderWith( fixture, frame, 6000, 0, receipt.exposure() ), width, height );
            const double master_cast =
                deckCastChroma( renderWith( fixture, frame, 8594, -23, receipt.exposure() ), width, height );
            const double applied_cast =
                deckCastChroma( renderWith( fixture, frame, applied_temperature, applied_tint, receipt.exposure() ), width, height );
            ASSERT_TRUE( applied_cast < base_cast );
            ASSERT_TRUE( applied_cast < master_cast );
            // Measured here (headless render): applied 3.5-4.4 vs master 8.5 and base 11.8-12.0.
            // Real-app sheet (GUI render, cam matrix on): applied 4.8 vs master 11.5 and r1 22.4.
            ASSERT_TRUE( applied_cast <= 6.0 );
        }
    }
}
