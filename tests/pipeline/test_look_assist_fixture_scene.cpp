// LOOK-ASSIST-SCENE-CLASSIFY-1: every tracked fixture clip, through the real thumbnail path and the
// real headless Look Assist (the CPU path; the GUI and its CUDA/GL display path call the same shared
// module -- see console test LookAssistScene.CpuAndCudaShareOneClassifier).
//
// Both tracked clips are the same daylight pool scene (ISO 100, 1/2150 s, f/5.6: EV100 16). Their
// RAW thumbnail is a flat floor at the sensor black offset, which is what made them read as NIGHT.
// The test is on the PICTURE as well as the verdict: a correct class with a wrong balance is a
// regression (r1: scene right, deck cast chroma 11.5 -> 22.4).
#include "../common/hash_helpers.h"
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
#include <cmath>
#include <cstdio>
#include <cstring>
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

// The processed picture at an absolute exposure, as statistics: the same call the app's and the
// headless applier's render callback make (ReceiptApplier::processedThumbnailAtExposure).
bool processedPictureStats( mlvObject_t *video, int frame, double stops, LookAssistStats *out )
{
    const int rawW = video->RAWI.xRes;
    const int rawH = video->RAWI.yRes;
    int downscale = 6;
    if( rawW > 4000 || rawH > 2500 ) downscale = 12;
    else if( rawW > 2800 || rawH > 1900 ) downscale = 10;
    else if( rawW > 1800 || rawH > 1200 ) downscale = 8;
    const int colorDownscale = std::max( 3, downscale / 3 );
    const int w = rawW / colorDownscale;
    const int h = rawH / colorDownscale;
    std::vector<unsigned char> thumbnail( static_cast<size_t>( w ) * h * 3 );
    if( !ReceiptApplier::processedThumbnailAtExposure( video, frame, colorDownscale, 1, stops, thumbnail.data() ) )
        return false;
    *out = analyzeLookAssistThumbnail( thumbnail.data(), w, h );
    return true;
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
            ASSERT_TRUE( stats.hasSceneEv100 );
            ASSERT_TRUE( stats.sceneEv100 > 15.0 && stats.sceneEv100 < 17.0 );
            // ... and the recorded exposure ALONE does not call it daylight (a night moon records the
            // same kind of EV): the legacy verdict stands until the rendered picture agrees.
            ASSERT_TRUE( classifyLookAssistScene( stats ) == LookAssistScene::Night );
            ASSERT_TRUE( lookAssistDaylightNeedsPictureEvidence( stats, LookAssistScene::Night ) );

            // The rendered picture at the lift the daylight verdict would apply is a lit picture (measured
            // headless: median 140-147, 95-99 % mid-tones; real app, CPU render: median ~116; the check
            // needs >= 60 % mid-tones and median 55..190): real margin on both sides.
            LookAssistStats hypothesis = stats;
            hypothesis.daylightPictureEvidence = true;
            const double plannedStops = presetForLookAssistScene( LookAssistScene::Shade, hypothesis ).exposure / 100.0;
            LookAssistStats picture;
            ASSERT_TRUE( processedPictureStats( fixture.video(), frame, plannedStops, &picture ) );
            ASSERT_TRUE( picture.midtoneFraction >= 0.85 );
            ASSERT_TRUE( picture.median >= 100.0 && picture.median <= 175.0 );
            ASSERT_TRUE( lookAssistPictureCorroboratesDaylight( picture ) );
            // ... while the same picture UNLIFTED is not what the check judges (it is the lift that
            // separates a dim day from a dark field): at the camera's exposure it is much darker.
            LookAssistStats unlifted;
            ASSERT_TRUE( processedPictureStats( fixture.video(), frame, 0.0, &unlifted ) );
            ASSERT_TRUE( unlifted.median < picture.median );

            LookAssistStats resolved = stats;
            const LookAssistScene scene = resolveLookAssistScene(
                &resolved, [&]( double stops, LookAssistStats *out ) {
                    return processedPictureStats( fixture.video(), frame, stops, out ); } );
            ASSERT_TRUE( scene != LookAssistScene::Night );
            ASSERT_TRUE( scene != LookAssistScene::ArtificialLights );
            ASSERT_TRUE( scene == LookAssistScene::Shade );
            ASSERT_TRUE( resolved.daylightPictureEvidence );
            const LookAssistScene sceneAgain = resolveLookAssistScene(
                &resolved, [&]( double stops, LookAssistStats *out ) {
                    return processedPictureStats( fixture.video(), frame, stops, out ); } );
            ASSERT_TRUE( sceneAgain == scene );   // idempotent: re-resolving a resolved stats object
            // A flat RAW thumbnail is unusable for colour in ANY scene: the rendered picture is read.
            ASSERT_TRUE( lookAssistShouldAnalyzeProcessedColor( scene, resolved ) );
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

TEST(LookAssistFixtureScene, HeadlessBalancesDaylightFromTheRenderedPictureWhenNoPatchIsTrusted)
{
    // PR #221 r2's blocker, through the real headless consumer: corroborated daylight, NO trusted neutral
    // patch (the analysis picture is rendered at a white balance far from the scene's, so < 1 % of it is
    // neutral and the patch search falls back to the flat raw thumbnail). r2 kept the as-shot prior
    // (6000 K / tint 0: deck chroma 12 here, 18.9 in the real app); master's night path reached 6480 K /
    // tint -19. The shared render-based refinement walks the RENDERED picture to a balance that has neutral
    // samples, takes the patch from it, solves, and verifies at the solution.
    for( const FixtureClip &clip : kTrackedFixtureClips )
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
        // The stale blue balance the processing object holds when Look Assist runs (the analysis render
        // inherits it): this is what leaves the picture with no neutral samples.
        processingSetWhiteBalance( fixture.processing(), 3000, 0.0 );

        QTemporaryDir temporary_dir;
        const QString log_path = temporary_dir.filePath( QStringLiteral("look_assist.log") );
        BatchLogger::init( log_path );
        const bool applied = ReceiptApplier::applyHeadlessLookAssist(
            &receipt, fixture.video(), fixture.processing(), 0 );
        BatchLogger::shutdown();
        ASSERT_TRUE( applied );
        QFile log_file( log_path );
        ASSERT_TRUE( log_file.open( QIODevice::ReadOnly | QIODevice::Text ) );
        const QByteArray log = log_file.readAll();

        ASSERT_TRUE( log.contains( "scene=shade" ) );
        // Not the prior: the balance was found on a RENDERED picture, by the same patch solve.
        ASSERT_FALSE( log.contains( "autoWbSource=as-shot-prior" ) );
        ASSERT_TRUE( log.contains( "autoWbSource=rendered-neutral-patch" ) );
        ASSERT_TRUE( log.contains( "autoWbValid=true" ) );
        ASSERT_TRUE( log.contains( "autoWbDamping=1.000" ) );
        ASSERT_FALSE( log.contains( "refineRenders=0 " ) );   // the refinement ran (and rendered)
        ASSERT_TRUE( receipt.temperature() >= 4800 && receipt.temperature() <= 10000 );
        ASSERT_TRUE( receipt.tint() >= -35 && receipt.tint() <= 10 );
        ASSERT_TRUE( receipt.temperature() != 6000 || receipt.tint() != 0 );   // not the base balance

        // THE PICTURE, per state (headless render, the deck while it is in view): the applied look is no more
        // cast than the as-shot prior r2 left standing, than master's night-path result, and within 6.
        const int width = fixture.width();
        const int height = fixture.height();
        const int exposure = receipt.exposure();
        const double applied_cast = deckCastChroma( renderWith( fixture, 0, receipt.temperature(), receipt.tint(), exposure ), width, height );
        const double prior_cast = deckCastChroma( renderWith( fixture, 0, 6000, 0, exposure ), width, height );
        const double master_cast = deckCastChroma( renderWith( fixture, 0, 6480, -19, 174 ), width, height );
        ASSERT_TRUE( applied_cast < prior_cast );
        ASSERT_TRUE( applied_cast <= master_cast );
        ASSERT_TRUE( applied_cast <= 6.0 );
    }
}

namespace
{

// The receipt Look Assist leaves, as one comparable line.
QString receiptLine( ReceiptSettings &r )
{
    return QStringLiteral("exp=%1 contrast=%2 pivot=%3 temp=%4 tint=%5 vibrance=%6 shadows=%7 highlights=%8 chromaSmooth=%9")
        .arg( r.exposure() ).arg( r.contrast() ).arg( r.pivot() ).arg( r.temperature() ).arg( r.tint() )
        .arg( r.vibrance() ).arg( r.shadows() ).arg( r.highlights() ).arg( r.chromaSmooth() );
}

// The last "LOOK_ASSIST applied" line of a run, with only the masterScenePass flag taken out. Beyond the receipt it carries
// everything master's analysis measured on the picture it rendered (balanceRGB, balanceSamples, p05, the patch fields), so two
// runs that agree here analysed the same picture, not only reached the same receipt.
QString appliedLineWithoutPassFlag( const QByteArray &log )
{
    QString line;
    for( const QByteArray &candidate : log.split( '\n' ) )
        if( candidate.contains( "LOOK_ASSIST applied" ) ) line = QString::fromUtf8( candidate );
    line.remove( QStringLiteral(" masterScenePass=true") );
    line.remove( QStringLiteral(" masterScenePass=false") );
    // LOOK-ASSIST-DIAG-LOGGING-1: the appended decision trace records HOW each run reached its verdict (the recorded
    // exposure, whether a picture was asked for), which legitimately differs between the runs compared here. The
    // analysis they compare is everything before it.
    const int trace = line.indexOf( QStringLiteral(" has_ev100=") );
    if( trace >= 0 ) line.truncate( trace );
    return line;
}

// Runs the real headless Look Assist on a tracked clip as the no-trusted-patch state leaves it. noExposureMetadata
// removes the recorded exposure, so nothing can call the clip daylight: that run IS master's analysis.
// staleWhiteBalance: the processing object holds a 3000 K balance (no patch is found); false = the app default (the
// picture has a trusted neutral patch, the initial-patch state).
// existingTemperature / existingTint (receipt units; -1 = unset, the 6000 K / 0 base) are the balance the picture is
// rendered at when Look Assist runs; asShotKelvin > 0 records that kelvin as the clip's own as-shot balance.
// masterPassOnly: call master's single pass directly (masterScenePass = true), so the daylight pass is never entered.
// processingEntry / processingEnd: the live processing object's balance (kelvin / stored render tint) on entry and at
// the end, so the state a run STARTS from is asserted, not assumed.
// entryKelvin > 0: the processing object holds that kelvin and entryUiTint (the UI's tint units, as the slider passes it
// to processingSetWhiteBalance after dividing by 10) on entry, instead of the default / the stale 3000 K.
bool runHeadlessLookAssist( const char *clipFile, bool noExposureMetadata, QString *receipt, QByteArray *log,
                            bool staleWhiteBalance = true, int existingTemperature = -1, int existingTint = 0,
                            int asShotKelvin = 0, bool masterPassOnly = false,
                            QString *processingEntry = nullptr, QString *processingEnd = nullptr,
                            int entryKelvin = 0, int entryUiTint = 0 )
{
    MlvPipelineFixture fixture;
    QString error_message;
    if( !fixture.openClipFile( repo_file_path( QString::fromLatin1( clipFile ) ), &error_message ) ) return false;
    if( !fixture.applyReceipt( &error_message ) ) return false;
    if( noExposureMetadata )
    {
        fixture.video()->EXPO.isoValue = 0;
        fixture.video()->EXPO.shutterValue = 0;
        fixture.video()->LENS.aperture = 0;
    }
    ReceiptSettings &r = fixture.receipt();
    r.setLookAssistEnabled( true );
    r.setLookAssistBaselineValid( false );
    r.setExposure( 0 );
    r.setTemperature( existingTemperature );
    r.setTint( existingTint );
    if( asShotKelvin > 0 )
    {
        fixture.video()->WBAL.wb_mode = 9;   // WB_KELVIN: the kelvin field is the as-shot balance
        fixture.video()->WBAL.kelvin = asShotKelvin;
    }
    if( staleWhiteBalance ) processingSetWhiteBalance( fixture.processing(), 3000, 0.0 );
    if( entryKelvin > 0 ) processingSetWhiteBalance( fixture.processing(), entryKelvin, entryUiTint / 10.0 );

    QTemporaryDir temporary_dir;
    const QString log_path = temporary_dir.filePath( QStringLiteral("look_assist.log") );
    BatchLogger::init( log_path );
    const auto balance = [&]() {
        return QStringLiteral("%1/%2").arg( processingGetWhiteBalanceKelvin( fixture.processing() ), 0, 'f', 3 )
                                      .arg( processingGetWhiteBalanceTint( fixture.processing() ), 0, 'f', 9 );
    };
    if( processingEntry ) *processingEntry = balance();
    const bool applied = ReceiptApplier::applyHeadlessLookAssist( &r, fixture.video(), fixture.processing(), 0,
                                                                 masterPassOnly );
    if( processingEnd ) *processingEnd = balance();
    BatchLogger::shutdown();
    QFile log_file( log_path );
    if( !applied || !log_file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return false;
    *log = log_file.readAll();
    *receipt = receiptLine( r );
    return true;
}

struct ScopedEnv
{
    explicit ScopedEnv( const char *name, const char *value ) : m_name( name ) { qputenv( m_name, value ); }
    ~ScopedEnv() { qunsetenv( m_name ); }
    const char *m_name;
};

} // namespace

TEST(LookAssistFixtureScene, HeadlessWithTheRefinementOffIsMastersAnalysisOnTheFixtureStates)
{
    // sol H4 / fable: with the narrowing switch off a corroborated daylight clip that has no trusted patch must
    // get MASTER's behaviour -- not the as-shot prior, not the daylight preset with master's balance -- on every
    // tracked clip. Master's analysis is what the SAME clip gets when nothing can call it daylight, so the two
    // receipts must be equal field for field.
    for( const FixtureClip &clip : kTrackedFixtureClips )
    {
        QString master, off;
        QByteArray masterLog, offLog;
        ASSERT_TRUE( runHeadlessLookAssist( clip.file, true, &master, &masterLog ) );
        {
            ScopedEnv switchOff( "MLVAPP_LOOK_ASSIST_REFINE_DAYLIGHT", "0" );
            ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &off, &offLog ) );
        }
        ASSERT_TRUE( masterLog.contains( "scene=night" ) );
        ASSERT_TRUE( masterLog.contains( "masterScenePass=false" ) );
        ASSERT_TRUE( offLog.contains( "scene=night" ) );              // master's verdict, not "shade"
        ASSERT_TRUE( offLog.contains( "masterScenePass=true" ) );     // reached by the fallback, not by luck
        ASSERT_FALSE( offLog.contains( "autoWbSource=as-shot-prior" ) );
        ASSERT_FALSE( offLog.contains( "autoWbSource=rendered-neutral-patch" ) );
        ASSERT_TRUE( master == off );
    }
}

TEST(LookAssistFixtureScene, HeadlessTakesMastersPathForAnInitialPatchItCannotVerify)
{
    // LOOK-ASSIST-SCENE-CLASSIFY-3, through the real headless consumer on the tracked clips, in the state where the
    // picture HAS a trusted neutral patch (the app's default white balance). With the narrowing switch off the
    // initial patch cannot be verified, so the clip takes MASTER's path: the consumer re-runs the analysis with the
    // recorded-exposure daylight hypothesis off, and the receipt equals -- field for field -- what the same clip
    // gets when nothing can call it daylight (master's own analysis). With the switch on, the same patch is
    // verified on its own surface (base picture and solution) and keeps the undamped daylight solve.
    for( const FixtureClip &clip : kTrackedFixtureClips )
    {
        QString master, off, on;
        QByteArray masterLog, offLog, onLog;
        ASSERT_TRUE( runHeadlessLookAssist( clip.file, true, &master, &masterLog, false ) );
        {
            ScopedEnv switchOff( "MLVAPP_LOOK_ASSIST_REFINE_DAYLIGHT", "0" );
            ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &off, &offLog, false ) );
        }
        ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &on, &onLog, false ) );

        ASSERT_TRUE( masterLog.contains( "scene=night" ) );
        ASSERT_TRUE( offLog.contains( "daylight_fallback_to_master" ) );
        ASSERT_TRUE( offLog.contains( "reason=initial_patch_unverified" ) );
        ASSERT_TRUE( offLog.contains( "masterScenePass=true" ) );
        ASSERT_TRUE( offLog.contains( "scene=night" ) );                       // master's verdict, not "shade"
        ASSERT_FALSE( offLog.contains( "scene=shade" ) );
        ASSERT_TRUE( master == off );                                           // master's receipt, field for field

        // Switch on: the same clip, the same patch, verified on its own surface -> the daylight solve stands.
        ASSERT_TRUE( onLog.contains( "scene=shade" ) );
        ASSERT_TRUE( onLog.contains( "autoWbSource=processed-neutral-patch" ) );
        ASSERT_TRUE( onLog.contains( "initialPatchChecked=true initialPatchRefused=false" ) );
        ASSERT_FALSE( onLog.contains( "daylight_fallback_to_master" ) );
    }
}

TEST(LookAssistFixtureScene, HeadlessRefusesABlueAtAsShotInitialPatchThroughTheRealRenderer)
{
    // sol H2, PR #224 r1: the refusal of a patch that is near-neutral under the EXISTING balance but blue at the clip's
    // as-shot balance, through ReceiptApplier's actual renderer (not the console suite's synthetic DeckScene).
    // The clip records a 4800 K as-shot balance, so the as-shot prior renders the deck blue (the daylight window's
    // floor); the daylight pass finds its initial patch on the picture rendered at the daylight exposure with the
    // processing object's own balance (the default 6000 K / 0; the receipt's 7895 K / -12 is the base the solve starts
    // from), where the same deck is near-neutral and passes the patch search. Switch ON: the initial patch must be judged on the as-shot surface,
    // refused there, and the clip must take MASTER's result for the same state -- not the undamped solve (the
    // candidate, 6360 K / -35 here).
    // Master's result for this state: the receipt's 7895 K / -12 is NOT the picture master renders (the processing
    // object holds the default 6000 K / 0 and master renders at that), and that picture has no trusted patch, so master
    // finds nothing and leaves the receipt's balance alone (night, no decision). The fallback starts from that same
    // processing state, so it lands there; HeadlessFallbackStartsFromMastersProcessingState compares it with a fresh
    // master-only run field for field.
    for( const FixtureClip &clip : kTrackedFixtureClips )
    {
        QString on, off, control;
        QByteArray onLog, offLog, controlLog;
        ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &on, &onLog, false, 7895, -12, 4800 ) );
        {
            ScopedEnv switchOff( "MLVAPP_LOOK_ASSIST_REFINE_DAYLIGHT", "0" );
            ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &off, &offLog, false, 7895, -12, 4800 ) );
        }
        // Control: the same existing balance with the as-shot balance at the daylight default (6000 K), where the same
        // surface is verified -> the daylight solve stands. The refusal above is the as-shot blue, nothing else.
        ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &control, &controlLog, false, 7895, -12, 6000 ) );

        ASSERT_TRUE( onLog.contains( "daylight_fallback_to_master" ) );
        ASSERT_TRUE( onLog.contains( "reason=initial_patch_unverified refusedAtBase=true" ) );
        ASSERT_TRUE( onLog.contains( "masterScenePass=true" ) );
        ASSERT_TRUE( onLog.contains( "scene=night" ) );                        // master's verdict
        ASSERT_FALSE( onLog.contains( "scene=shade" ) );
        ASSERT_TRUE( onLog.contains( "autoWbSource=none autoWbDecision=none" ) );   // master: no patch on its picture
        ASSERT_TRUE( onLog.contains( "patchValid=false" ) );
        ASSERT_FALSE( on.contains( "temp=6360 " ) );                           // not the undamped candidate
        ASSERT_FALSE( on.contains( "temp=6897 " ) );                           // not a damped solve of a patch master never sees
        ASSERT_TRUE( on.contains( "temp=7895 tint=-12 " ) );                   // master's value: the receipt's balance, untouched
        ASSERT_TRUE( on == off );                                              // the switch off lands on the same receipt

        ASSERT_FALSE( controlLog.contains( "daylight_fallback_to_master" ) );
        ASSERT_TRUE( controlLog.contains( "scene=shade" ) );
        ASSERT_TRUE( controlLog.contains( "initialPatchChecked=true initialPatchRefused=false" ) );
    }
}

TEST(LookAssistFixtureScene, HeadlessFallbackStartsFromMastersProcessingState)
{
    // fable r2, PR #224: the daylight pass writes the receipt's base balance into the live processing object before it
    // falls back, so master's pass used to render its picture at the RECEIPT's balance. Master renders it at the
    // balance the object holds on entry (BatchRunner creates it at 6000 K / 0 and applyToMlv never sets one).
    // State: receipt 7895 K / -12, processing object at its default 6000 K / 0 (staleWhiteBalance = false).
    //
    // Master's behaviour here is produced two ways, on a FRESH object each, and neither enters the daylight pass:
    //  - the no-metadata run: nothing can call the clip daylight, so master's single pass is the only pass there is;
    //  - the direct call with masterScenePass = true: the same code the fallback re-enters, from a clean start.
    // The fallback result (switch off: always falls back; switch on with a 4800 K as-shot balance: the initial patch is
    // refused at the as-shot base) must equal both field for field, including the balance left in the object.
    struct Arm { const char *name; bool switchOff; int asShotKelvin; };
    const Arm arms[] = { { "switch-off", true, 0 }, { "switch-on-blue-as-shot", false, 4800 } };
    for( const FixtureClip &clip : kTrackedFixtureClips )
    {
        for( const Arm &arm : arms )
        {
            QString metadataFree, direct, fallback;
            QByteArray metadataFreeLog, directLog, fallbackLog;
            QString entryA, endA, entryB, endB, entryF, endF;
            ASSERT_TRUE( runHeadlessLookAssist( clip.file, true, &metadataFree, &metadataFreeLog, false, 7895, -12,
                                                arm.asShotKelvin, false, &entryA, &endA ) );
            ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &direct, &directLog, false, 7895, -12,
                                                arm.asShotKelvin, true, &entryB, &endB ) );
            {
                ScopedEnv switchOff( "MLVAPP_LOOK_ASSIST_REFINE_DAYLIGHT", arm.switchOff ? "0" : "1" );
                ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &fallback, &fallbackLog, false, 7895, -12,
                                                    arm.asShotKelvin, false, &entryF, &endF ) );
            }

            // The starting state is the default 6000 K / 0 in all three, and it is not the receipt's balance.
            ASSERT_TRUE( entryA == QStringLiteral("6000.000/0.000000000") );
            ASSERT_TRUE( entryB == entryA );
            ASSERT_TRUE( entryF == entryA );

            // Which path each run took.
            ASSERT_TRUE( metadataFreeLog.contains( "masterScenePass=false" ) );
            ASSERT_FALSE( metadataFreeLog.contains( "daylight_fallback_to_master" ) );
            ASSERT_TRUE( directLog.contains( "masterScenePass=true" ) );
            ASSERT_FALSE( directLog.contains( "daylight_fallback_to_master" ) );
            ASSERT_TRUE( fallbackLog.contains( "daylight_fallback_to_master" ) );
            ASSERT_TRUE( fallbackLog.contains( "masterScenePass=true" ) );

            // Master finds no patch on the 6000 K / 0 picture and leaves the receipt's balance alone.
            ASSERT_TRUE( metadataFreeLog.contains( "patchValid=false" ) );
            ASSERT_TRUE( metadataFree.contains( "temp=7895 tint=-12 " ) );

            // The fallback IS master's result: receipt and live balance, field for field.
            ASSERT_TRUE( fallback == metadataFree );
            ASSERT_TRUE( fallback == direct );
            ASSERT_TRUE( endF == endA );
            ASSERT_TRUE( endF == endB );
            ASSERT_TRUE( appliedLineWithoutPassFlag( fallbackLog ) == appliedLineWithoutPassFlag( metadataFreeLog ) );
            ASSERT_TRUE( appliedLineWithoutPassFlag( fallbackLog ) == appliedLineWithoutPassFlag( directLog ) );
            ASSERT_TRUE( fallbackLog.contains( "patchValid=false" ) );
            (void)arm.name;
        }
    }
}

TEST(LookAssistFixtureScene, HeadlessFallbackStartsFromMastersProcessingStateAtANonZeroEntryTint)
{
    // sol r1, PR #226 (closes LOOK-ASSIST-ENTRY-TINT-RESTORE-COVERAGE-1): the same equality, with the processing object
    // entering at 6000 K and a non-zero UI tint instead of 6000 K / 0, so the tint half of the restore is pinned, in a state
    // where the daylight pass's live solver RUNS before it falls back (the receipt-less arm: the processed picture has a
    // trusted neutral patch, and the solver ends at a different tint). The tints are sol's -12 (stored -0.244662371) and -50
    // (stored -2.973017788). The picture master analyses on these fixtures barely depends on the entry tint, so the
    // equality below alone cannot see the restore (a first version stayed green with it removed); the readback of the
    // state the recursion starts from is what pins it.
    // Master's behaviour is the direct masterScenePass call on a fresh object (nothing enters the daylight pass); the
    // fallback equals it: receipt, live balance (kelvin and stored tint to nine decimals) and the analysis line master
    // measured on its picture. One fixture and one arm only: this test shares a hosted shard with a 240 s bound; the
    // other fixture runs the 6000 K / 0 equality above, and the solver restore is pinned on both fixtures below.
    const FixtureClip &clip = kTrackedFixtureClips[0];
    const int entryTints[] = { -12, -50 };
    for( const int entryTint : entryTints )
    {
        QString direct, fallback;
        QByteArray directLog, fallbackLog;
        QString entryB, endB, entryF, endF;
        ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &direct, &directLog, false, -1, 0, 0, true,
                                            &entryB, &endB, 6000, entryTint ) );
        {
            ScopedEnv switchOff( "MLVAPP_LOOK_ASSIST_REFINE_DAYLIGHT", "0" );
            ASSERT_TRUE( runHeadlessLookAssist( clip.file, false, &fallback, &fallbackLog, false, -1, 0, 0, false,
                                                &entryF, &endF, 6000, entryTint ) );
        }

        // The entry balance is the UI's tint as the slider stores it.
        ASSERT_TRUE( entryB == QStringLiteral("6000.000/%1").arg( -std::pow( std::fabs( entryTint / 100.0 ), 1.75 ) * 10.0, 0, 'f', 9 ) );
        if( entryTint == -12 ) ASSERT_TRUE( entryB == QStringLiteral("6000.000/-0.244662371") );
        if( entryTint == -50 ) ASSERT_TRUE( entryB == QStringLiteral("6000.000/-2.973017788") );
        ASSERT_TRUE( entryF == entryB );

        ASSERT_TRUE( directLog.contains( "masterScenePass=true" ) );
        ASSERT_FALSE( directLog.contains( "daylight_fallback_to_master" ) );
        ASSERT_TRUE( fallbackLog.contains( "daylight_fallback_to_master" ) );
        ASSERT_TRUE( fallbackLog.contains( "masterScenePass=true" ) );

        // What master's pass starts from, read back from the object at the moment of the recursion: the entry balance
        // exactly, and no cached debayered frame (the daylight pass's solver left one behind: before = 1).
        const QStringList entryParts = entryB.split( QLatin1Char('/') );
        ASSERT_TRUE( fallbackLog.contains( QStringLiteral("daylight_fallback_state kelvin=%1 renderTint=%2 ")
                                               .arg( entryParts[0], entryParts[1] ).toUtf8() ) );
        ASSERT_TRUE( fallbackLog.contains( "cachedFrameBefore=1" ) );
        ASSERT_TRUE( fallbackLog.contains( "cachedFrameAfter=0" ) );

        ASSERT_TRUE( fallback == direct );
        ASSERT_TRUE( endF == endB );
        ASSERT_TRUE( appliedLineWithoutPassFlag( fallbackLog ) == appliedLineWithoutPassFlag( directLog ) );
    }
}

TEST(LookAssistFixtureScene, TheLiveWhiteBalanceSolverPutsTheStoredBalanceBackExactly)
{
    // sol r1, PR #226: processingFindWhiteBalance searches by setting candidate balances on the live object and then puts
    // the balance it found back. It passes the STORED render tint to the setter, which converts any tint that differs from
    // the stored one as a receipt tint -- so the object used to leave the search at a different tint than it entered
    // with (UI -12: stored -0.244662371 became -0.015135353), and the GUI's fallback into master's pass inherited it.
    // State: 6000 K / UI tint -12; the solver runs on a trusted patch and ends elsewhere. Every value master's pass reads
    // -- kelvin, stored tint, the multipliers, the matrices -- must equal a fresh object that never ran the solver.
    // The solver walks 7700 x 201 candidates (about 5 s) whatever the clip is, so each fixture costs one search.
    for( const FixtureClip &clip : kTrackedFixtureClips )
    {
        MlvPipelineFixture solved, reference;
        QString error_message;
        ASSERT_TRUE( solved.openClipFile( repo_file_path( QString::fromLatin1( clip.file ) ), &error_message ) );
        ASSERT_TRUE( solved.applyReceipt( &error_message ) );
        ASSERT_TRUE( reference.openClipFile( repo_file_path( QString::fromLatin1( clip.file ) ), &error_message ) );
        ASSERT_TRUE( reference.applyReceipt( &error_message ) );
        processingSetWhiteBalance( solved.processing(), 6000, -12 / 10.0 );
        processingSetWhiteBalance( reference.processing(), 6000, -12 / 10.0 );

        const processingObject_t *s = solved.processing();
        const processingObject_t *r = reference.processing();
        ASSERT_TRUE( std::fabs( s->wb_tint - ( -0.244662371 ) ) < 1e-9 );
        ASSERT_TRUE( s->wb_tint == r->wb_tint );

        int solvedTemperature = 0, solvedTint = 0;
        findMlvWhiteBalance( solved.video(), 0, solved.width() / 2, solved.height() / 2,
                             &solvedTemperature, &solvedTint, 0 );
        // The solve ended somewhere other than the entry balance, so a restore that is wrong has something to get wrong.
        ASSERT_TRUE( solvedTemperature != 6000 || solvedTint != -12 );

        ASSERT_TRUE( s->kelvin == r->kelvin );
        ASSERT_TRUE( s->wb_tint == r->wb_tint );
        for( int c = 0; c < 3; ++c ) ASSERT_TRUE( s->wb_multipliers[c] == r->wb_multipliers[c] );
        for( int m : { 0, 4, 8 } )
            ASSERT_TRUE( std::memcmp( s->pre_calc_matrix[m], r->pre_calc_matrix[m], 65536 * sizeof( int32_t ) ) == 0 );
        // sol's executed arithmetic: the blue multiplier and the blue matrix value at 20000.
        ASSERT_TRUE( std::fabs( s->wb_multipliers[2] - 1.523314852 ) < 1e-8 );
        ASSERT_EQ( 30466, s->pre_calc_matrix[8][20000] );
    }
}

TEST(LookAssistFixtureScene, HeadlessKeepsTheLegacyVerdictWhenTheExposureCannotSayDaylight)
{
    // The same flat-floor fixture, but the recorded exposure is an ND-filtered daylight shot
    // (ISO 100, 1/50 s, f/2.8 = EV100 8.6) or absent: nothing can call it daylight, so the verdict is
    // the one master produced -- night, with the night rescue -- and the daylight machinery stays out.
    struct Exposure { const char *name; int iso; int shutterUs; int apertureX100; };
    const Exposure exposures[] = { { "nd-filter", 100, 20000, 280 }, { "no-metadata", 0, 0, 0 } };
    for( const Exposure &e : exposures )
    {
        MlvPipelineFixture fixture;
        QString error_message;
        ASSERT_TRUE( fixture.openClipFile( repo_file_path( QStringLiteral("tests/fixtures/clips/tiny_dual_iso.mlv") ), &error_message ) );
        ASSERT_TRUE( fixture.applyReceipt( &error_message ) );
        fixture.video()->EXPO.isoValue = e.iso;
        fixture.video()->EXPO.shutterValue = e.shutterUs;
        fixture.video()->LENS.aperture = e.apertureX100;

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
            &receipt, fixture.video(), fixture.processing(), 0 );
        BatchLogger::shutdown();
        ASSERT_TRUE( applied );
        QFile log_file( log_path );
        ASSERT_TRUE( log_file.open( QIODevice::ReadOnly | QIODevice::Text ) );
        const QByteArray log = log_file.readAll();
        ASSERT_TRUE( log.contains( "scene=night" ) );
        ASSERT_FALSE( log.contains( "autoWbSource=as-shot-prior" ) );
        // The night rescue is on (the daylight window is not): a daylight-bound solve would stay >= 4800 K.
        ASSERT_TRUE( log.contains( "autoWbDamping=" ) );
        (void)e.name;
    }
}

TEST(LookAssistFixtureScene, AsShotWhiteBalanceDecoderHonoursTheWbMode)
{
    // WBAL: kelvin is valid only in WB_KELVIN, the wbgain_* neutral only in WB_CUSTOM (mlv.h). The
    // decoder is the app's own (MainWindow::setWhiteBalanceFromMlv delegates to it). A populated
    // custom-WB slot under another mode must never leak into the answer.
    MlvPipelineFixture fixture;
    QString error_message;
    ASSERT_TRUE( fixture.openClipFile( repo_file_path( QStringLiteral("tests/fixtures/clips/tiny_dual_iso.mlv") ), &error_message ) );
    ASSERT_TRUE( fixture.applyReceipt( &error_message ) );
    mlvObject_t *video = fixture.video();

    // The clip's own populated custom-WB slot: it fits a real colour temperature (~5270 K / -27 on
    // this fixture), which is exactly the wrong answer for any mode that is not WB_CUSTOM.
    video->WBAL.kelvin = 7000;
    const double neutral[3] = { static_cast<double>( video->WBAL.wbgain_r ) / 1024.0,
                                static_cast<double>( video->WBAL.wbgain_g ) / 1024.0,
                                static_cast<double>( video->WBAL.wbgain_b ) / 1024.0 };
    int fitTemperature = 0, fitTint = 0;
    ASSERT_TRUE( processingWhiteBalanceControlsForAsShotNeutral( neutral, &fitTemperature, &fitTint ) );
    ASSERT_TRUE( fitTemperature != 7000 && fitTemperature != 6000 );   // distinguishable from every other answer

    struct Mode { uint32_t mode; int temperature; };
    const Mode modes[] = {
        { 0, 6000 },   // WB_AUTO: the app default (never the slot, never kelvin)
        { 1, 5200 },   // WB_SUNNY
        { 2, 6000 },   // WB_CLOUDY
        { 3, 3200 },   // WB_TUNGSTEN
        { 4, 4000 },   // WB_FLUORESCENT
        { 5, 6000 },   // WB_FLASH
        { 8, 7000 },   // WB_SHADE: preset, NOT the populated kelvin field (7000 here by coincidence)
        { 9, 7000 },   // WB_KELVIN: the kelvin field, NOT the populated custom slot
        { 77, 6000 },  // unknown mode: default
    };
    for( const Mode &m : modes )
    {
        video->WBAL.wb_mode = m.mode;
        video->WBAL.kelvin = ( m.mode == 8 ) ? 4321 : 7000;   // shade must ignore even a different kelvin
        int temperature = 0, tint = 99;
        ASSERT_TRUE( ReceiptApplier::asShotWhiteBalanceControls( video, &temperature, &tint ) );
        ASSERT_EQ( m.temperature, temperature );
        ASSERT_EQ( 0, tint );
    }
    // WB_KELVIN with a different kelvin: follows the field, still ignores the slot.
    video->WBAL.wb_mode = 9;
    video->WBAL.kelvin = 4800;
    int temperature = 0, tint = 99;
    ASSERT_TRUE( ReceiptApplier::asShotWhiteBalanceControls( video, &temperature, &tint ) );
    ASSERT_EQ( 4800, temperature );
    ASSERT_EQ( 0, tint );

    // WB_CUSTOM: the app fits the retained neutral only for DNG sequences; a native MLV keeps the
    // default (this is what MainWindow::setWhiteBalanceFromMlv always did).
    video->WBAL.wb_mode = 6;
    const uint32_t savedClass = video->MLVI.videoClass;
    video->MLVI.videoClass = savedClass & ~static_cast<uint32_t>( MLV_VIDEO_CLASS_FLAG_DNGSEQ );
    ASSERT_TRUE( ReceiptApplier::asShotWhiteBalanceControls( video, &temperature, &tint ) );
    ASSERT_EQ( 6000, temperature );
    ASSERT_EQ( 0, tint );
    video->MLVI.videoClass = savedClass | static_cast<uint32_t>( MLV_VIDEO_CLASS_FLAG_DNGSEQ );
    ASSERT_TRUE( ReceiptApplier::asShotWhiteBalanceControls( video, &temperature, &tint ) );
    ASSERT_EQ( fitTemperature, temperature );
    ASSERT_EQ( fitTint, tint );
    video->MLVI.videoClass = savedClass;

    // Null guards.
    ASSERT_FALSE( ReceiptApplier::asShotWhiteBalanceControls( nullptr, &temperature, &tint ) );
    ASSERT_FALSE( ReceiptApplier::asShotWhiteBalanceControls( video, nullptr, &tint ) );
}

namespace
{

// LOOK-ASSIST-DIAG-LOGGING-1: the decision log is observation only. One tracked-fixture run, reduced to the three things
// the owner sees: the verdict, the receipt, and the picture (sha256 of the 8-bit frame rendered with that receipt).
struct IdentityRun
{
    QString scene;
    QString receipt;
    std::string pictureSha256;
    QByteArray log;
};

struct IdentityCase
{
    const char *name;
    const char *clip;
    bool overrideExposure;   // replace the recorded exposure (ISO, shutter in microseconds, aperture * 100)
    int iso;
    int shutterUs;
    int apertureX100;
    int renderFrame;         // the frame rendered with the resulting receipt (Look Assist always analyses frame 0)
};

bool runIdentityCase( const IdentityCase &c, IdentityRun *out )
{
    MlvPipelineFixture fixture;
    QString error_message;
    if( !fixture.openClipFile( repo_file_path( QString::fromLatin1( c.clip ) ), &error_message ) ) return false;
    if( !fixture.applyReceipt( &error_message ) ) return false;
    if( c.overrideExposure )
    {
        fixture.video()->EXPO.isoValue = c.iso;
        fixture.video()->EXPO.shutterValue = c.shutterUs;
        fixture.video()->LENS.aperture = c.apertureX100;
    }
    ReceiptSettings &r = fixture.receipt();
    r.setLookAssistEnabled( true );
    r.setLookAssistBaselineValid( false );
    r.setExposure( 0 );
    r.setTemperature( -1 );
    r.setTint( 0 );

    QTemporaryDir temporary_dir;
    const QString log_path = temporary_dir.filePath( QStringLiteral("look_assist.log") );
    BatchLogger::init( log_path );
    const bool applied = ReceiptApplier::applyHeadlessLookAssist( &r, fixture.video(), fixture.processing(), 0 );
    BatchLogger::shutdown();
    QFile log_file( log_path );
    if( !applied || !log_file.open( QIODevice::ReadOnly | QIODevice::Text ) ) return false;
    out->log = log_file.readAll();
    out->receipt = receiptLine( r );
    const QRegularExpressionMatch m =
        QRegularExpression( QStringLiteral("LOOK_ASSIST applied frame=\\d+ scene=(\\w+)") )
            .match( QString::fromUtf8( out->log ) );
    out->scene = m.hasMatch() ? m.captured( 1 ) : QString();

    // The picture: the receipt pushed into the pipeline exactly as the export path does, then one frame rendered.
    if( !fixture.applyReceipt( &error_message ) ) return false;
    const std::vector<uint8_t> frame = fixture.renderFrame8( static_cast<uint64_t>( c.renderFrame ) );
    out->pictureSha256 = sha256_bytes( frame.data(), frame.size() );
    return !frame.empty();
}

const IdentityCase kIdentityCases[] = {
    { "tiny-daylight",     "tests/fixtures/clips/tiny_dual_iso.mlv",  false, 0,   0,     0,   1 },
    { "large-daylight",    "tests/fixtures/clips/large_dual_iso.mlv", false, 0,   0,     0,   10 },
    { "tiny-no-metadata",  "tests/fixtures/clips/tiny_dual_iso.mlv",  true,  0,   0,     0,   1 },
    { "tiny-nd-filter",    "tests/fixtures/clips/tiny_dual_iso.mlv",  true,  100, 20000, 280, 1 },
};

// Pinned from master b5751928, BEFORE the decision log existed (see the PR). Same receipt, same verdict, same picture.
struct IdentityPin { const char *scene; const char *receipt; const char *pictureSha256; };
const IdentityPin kIdentityPins[] = {
    { "shade", "exp=160 contrast=15 pivot=55 temp=6540 tint=-35 vibrance=5 shadows=12 highlights=-12 chromaSmooth=1",
      "9a16525a28dc92ed96fe5940ccceaeec1ecd0aca5e1a74360ba9709fbf7a3e30" },
    { "shade", "exp=160 contrast=15 pivot=55 temp=6540 tint=-35 vibrance=5 shadows=12 highlights=-12 chromaSmooth=1",
      "f36fb58ce3680f57bd06e9d538db1e08bf74fad1963c70353049c69954f9f099" },
    { "night", "exp=174 contrast=14 pivot=46 temp=6000 tint=0 vibrance=3 shadows=32 highlights=-26 chromaSmooth=1",
      "4e9d6211cc6328216538224b3f9fe5be4c4f16e83343219d49984f473f49c83d" },
    { "night", "exp=174 contrast=14 pivot=46 temp=6000 tint=0 vibrance=3 shadows=32 highlights=-26 chromaSmooth=1",
      "4e9d6211cc6328216538224b3f9fe5be4c4f16e83343219d49984f473f49c83d" },
};

} // namespace

TEST(LookAssistFixtureScene, DecisionLoggingChangesNeitherThePictureNorTheReceiptNorTheVerdict)
{
    const size_t caseCount = sizeof( kIdentityCases ) / sizeof( kIdentityCases[0] );
    std::vector<IdentityRun> runs( caseCount );
    for( size_t i = 0; i < caseCount; ++i )
    {
        ASSERT_TRUE( runIdentityCase( kIdentityCases[i], &runs[i] ) );
        std::fprintf( stderr, "IDENTITY-PIN %s | scene=%s | receipt=%s | sha256=%s\n", kIdentityCases[i].name,
                      qPrintable( runs[i].scene ), qPrintable( runs[i].receipt ), runs[i].pictureSha256.c_str() );
    }
    for( size_t i = 0; i < caseCount; ++i )
    {
        ASSERT_TRUE( runs[i].scene == QString::fromLatin1( kIdentityPins[i].scene ) );
        ASSERT_TRUE( runs[i].receipt == QString::fromLatin1( kIdentityPins[i].receipt ) );
        ASSERT_TRUE( runs[i].pictureSha256 == kIdentityPins[i].pictureSha256 );
    }
}

namespace
{

// The decision fields at the end of the last "LOOK_ASSIST applied" line of a run ("" when absent).
QString appliedLineDecisionTail( const QByteArray &log )
{
    QString line;
    for( const QByteArray &candidate : log.split( '\n' ) )
        if( candidate.contains( "LOOK_ASSIST applied" ) ) line = QString::fromUtf8( candidate ).trimmed();
    const int at = line.indexOf( QStringLiteral(" has_ev100=") );
    return at < 0 ? QString() : line.mid( at + 1 );
}

} // namespace

TEST(LookAssistFixtureScene, HeadlessAppliedLineSaysWhyItChoseItsScene)
{
    // The three decisions the M16 night diagnosis could not see, through the real headless Look Assist on the tracked
    // fixtures: the recorded exposure, which daylight conjunct decided, and that the headless path runs no night walk
    // and no display meter. The expected tail is the WHOLE field set, so dropping or renaming any field fails here.
    struct Expect { const char *name; const char *tail; };
    const Expect expected[] = {
        // (a) tracked daylight: EV100 16, the rendered picture corroborates
        { "tiny-daylight",
          "^has_ev100=1 ev100=16\\.\\d\\d\\d daylight_gate=pass post_walk_ran=0 post_walk_branch=none post_walk_recovery=NA "
          "display_meter_ran=0 playback_scale=NA flavor=classic$" },
        { "large-daylight",
          "^has_ev100=1 ev100=16\\.\\d\\d\\d daylight_gate=pass post_walk_ran=0 post_walk_branch=none post_walk_recovery=NA "
          "display_meter_ran=0 playback_scale=NA flavor=classic$" },
        // (b) no metadata: nothing to record, and the first conjunct is what failed
        { "tiny-no-metadata",
          "^has_ev100=0 ev100=NA daylight_gate=exposure post_walk_ran=0 post_walk_branch=none post_walk_recovery=NA "
          "display_meter_ran=0 playback_scale=NA flavor=classic$" },
        // (c) a flat-floor NIGHT verdict (ND filter: EV100 8.6 over the same flat floor): metadata present, gate exposure
        { "tiny-nd-filter",
          "^has_ev100=1 ev100=8\\.\\d\\d\\d daylight_gate=exposure post_walk_ran=0 post_walk_branch=none "
          "post_walk_recovery=NA display_meter_ran=0 playback_scale=NA flavor=classic$" },
    };
    const size_t caseCount = sizeof( kIdentityCases ) / sizeof( kIdentityCases[0] );
    ASSERT_EQ( caseCount, sizeof( expected ) / sizeof( expected[0] ) );
    for( size_t i = 0; i < caseCount; ++i )
    {
        ASSERT_TRUE( QString::fromLatin1( expected[i].name ) == QString::fromLatin1( kIdentityCases[i].name ) );
        IdentityRun run;
        ASSERT_TRUE( runIdentityCase( kIdentityCases[i], &run ) );
        const QString tail = appliedLineDecisionTail( run.log );
        ASSERT_FALSE( tail.isEmpty() );
        ASSERT_TRUE( QRegularExpression( QString::fromLatin1( expected[i].tail ) ).match( tail ).hasMatch() );
        // Appended only: the line still starts the way it always did and still carries its last original field.
        const QString line = QString::fromUtf8( run.log );
        ASSERT_TRUE( line.contains( QStringLiteral("LOOK_ASSIST applied frame=0 scene=") ) );
        ASSERT_TRUE( line.contains( QStringLiteral("initialPatchFinalChroma=") ) );
        ASSERT_TRUE( line.indexOf( QStringLiteral("initialPatchFinalChroma=") ) < line.indexOf( QStringLiteral(" has_ev100=") ) );
    }

    // The master pass asks for no picture: with every other conjunct holding, the gate is n/a rather than "picture".
    QString receipt;
    QByteArray log;
    ASSERT_TRUE( runHeadlessLookAssist( "tests/fixtures/clips/tiny_dual_iso.mlv", false, &receipt, &log, true, -1, 0, 0, true ) );
    ASSERT_TRUE( log.contains( "masterScenePass=true" ) );
    ASSERT_TRUE( appliedLineDecisionTail( log ).contains( QStringLiteral("daylight_gate=n/a ") ) );
}
