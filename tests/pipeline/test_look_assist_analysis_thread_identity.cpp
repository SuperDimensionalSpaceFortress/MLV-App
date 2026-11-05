// LOOK-ASSIST-CLIP-OPEN-GUI-BLOCK-1: the heavy calls of Look Assist's analysis moved from the GUI thread to a worker, and
// the look has to stay the same. Each primitive the apply wraps runs on the caller and on the worker, with a fresh clip
// object per arm and the same call order, and has to give the same bytes and the same solved balance. A changed
// exposure has to change the bytes, so an arm that renders nothing cannot pass.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include "../../platform/qt/LookAssistAnalysisPump.h"
#include "../../src/batch/LookAssistAnalysis.h"
#include "../../src/batch/ReceiptApplier.h"

#include <algorithm>
#include <atomic>
#include <cstdio>
#include <thread>
#include <vector>

#ifdef _OPENMP
#include <omp.h>
#endif

using namespace lookassist;
using look_assist_pump::AnalysisPump;
using look_assist_pump::AnalysisWorker;

namespace
{

const char *const kDualIsoFixtureClips[] = {
    "tests/fixtures/clips/tiny_dual_iso.mlv",
    "tests/fixtures/clips/large_dual_iso.mlv",
};

constexpr int kFrame = 0;

// The downscale the apply takes from the RAW size.
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

struct ArmResult
{
    std::vector<unsigned char> rawThumb;
    std::vector<unsigned char> atExposure;
    std::vector<unsigned char> atOtherExposure;
    std::vector<unsigned char> liveColour;
    std::vector<unsigned char> balance;
    std::vector<unsigned char> measureOnly;
    std::vector<unsigned char> measureOnlyDisplay;
    std::vector<unsigned char> afterStateChange;
    bool renderedAtExposure = false;
    bool renderedBalance = false;
    bool renderedMeasureOnly = false;
    bool renderedMeasureOnlyDisplay = false;
    bool meterValid = false;
    int meterSamples = 0;
    double meterMedian = 0.0;
    double meterP05 = 0.0;
    double meterP95 = 0.0;
    double meterP99 = 0.0;
    int solved[5][2] = {};
    std::vector<std::thread::id> threads;
};

// The apply's calls in the apply's order. Everything that changes state runs here, between the calls, as in the apply.
bool runArm( const char *clip, AnalysisPump &pump, int cores, ArmResult *out )
{
    MlvPipelineFixture fixture;
    QString error;
    if( !fixture.openClipFile( repo_file_path( QString::fromLatin1( clip ) ), &error ) || !fixture.applyReceipt( &error ) )
    {
        std::printf( "[identity] cannot open %s: %s\n", clip, qPrintable( error ) );
        return false;
    }
    mlvObject_t *video = fixture.video();
    const int md = thumbnailDownscaleFor( video );
    const int cd = std::max( 3, md / 3 );
    const int thumbWidth = video->RAWI.xRes / md;
    const int thumbHeight = video->RAWI.yRes / md;
    const int colorWidth = video->RAWI.xRes / cd;
    const int colorHeight = video->RAWI.yRes / cd;
    const size_t colorBytes = static_cast<size_t>( colorWidth ) * colorHeight * 3;
    const auto noteThread = [out]() { out->threads.push_back( std::this_thread::get_id() ); };

    out->rawThumb.assign( static_cast<size_t>( thumbWidth ) * thumbHeight * 3, 0 );
    pump.call( [&]()
    {
        noteThread();
        get_area_average_downscale_raw_thumnail( video, kFrame, md, out->rawThumb.data() );
    } );

    out->atExposure.assign( colorBytes, 0 );
    out->atOtherExposure.assign( colorBytes, 0 );
    pump.call( [&]()
    {
        noteThread();
        out->renderedAtExposure = ReceiptApplier::processedThumbnailAtExposure( video, kFrame, cd, cores, 1.0, out->atExposure.data() );
    } );
    pump.call( [&]()
    {
        noteThread();
        ReceiptApplier::processedThumbnailAtExposure( video, kFrame, cd, cores, 2.5, out->atOtherExposure.data() );
    } );

    out->liveColour.assign( colorBytes, 0 );
    pump.call( [&]()
    {
        noteThread();
        get_area_average_downscale_thumnail( video, kFrame, cd, cores, out->liveColour.data() );
    } );

    LookAssistStats meter;
    pump.call( [&]()
    {
        noteThread();
        out->meterValid = ReceiptApplier::lookAssistDisplayMeter( video, kFrame, md, cores, &meter, &out->meterSamples );
    } );
    out->meterMedian = meter.median;
    out->meterP05 = meter.p05;
    out->meterP95 = meter.p95;
    out->meterP99 = meter.p99;

    const int w = getMlvWidth( video );
    const int h = getMlvHeight( video );
    const int patches[5][2] = { { w / 2, h / 2 }, { w / 4, h / 4 }, { 3 * w / 4, h / 4 },
                                { w / 4, 3 * h / 4 }, { 3 * w / 4, 3 * h / 4 } };
    for( int i = 0; i < 5; ++i )
    {
        pump.call( [&]()
        {
            noteThread();
            findMlvWhiteBalanceAtAnalysisLevels( video, kFrame, patches[i][0], patches[i][1],
                                                 &out->solved[i][0], &out->solved[i][1], 0 );
        } );
    }

    // The three renderers the shared decisions call, made and held on this thread as the apply makes them.
    const LookAssistRenderBalanceFn balance = ReceiptApplier::lookAssistBalanceRenderer(
        video, kFrame, cd, colorWidth, colorHeight, cores, false );
    const LookAssistRenderBalanceFn measureOnly = ReceiptApplier::lookAssistMeasureOnlyRenderer(
        video, kFrame, cd, colorWidth, colorHeight, cores );
    const LookAssistRenderBalanceFn measureOnlyDisplay = ReceiptApplier::lookAssistMeasureOnlyDisplayRenderer(
        video, kFrame, cd, colorWidth, colorHeight, cores );
    LookAssistRenderedPicture picture;
    pump.call( [&]()
    {
        noteThread();
        out->renderedBalance = balance( 0.5, 6600, 0, &picture );
    } );
    out->balance = picture.rgb;
    pump.call( [&]()
    {
        noteThread();
        out->renderedMeasureOnly = measureOnly( 0.5, 6000, 0, &picture );
    } );
    out->measureOnly = picture.rgb;
    pump.call( [&]()
    {
        noteThread();
        out->renderedMeasureOnlyDisplay = measureOnlyDisplay( 0.5, 6000, 0, &picture );
    } );
    out->measureOnlyDisplay = picture.rgb;

    // The apply's post-balance render: a state change on this thread, then the render on the worker.
    processingSetWhiteBalance( video->processing, 7000.0, 10.0 );
    resetMlvCachedFrame( video );
    out->afterStateChange.assign( colorBytes, 0 );
    pump.call( [&]()
    {
        noteThread();
        get_area_average_downscale_thumnail( video, kFrame, cd, cores, out->afterStateChange.data() );
    } );
    return true;
}

void assertSameArm( const ArmResult &caller, const ArmResult &worker )
{
    ASSERT_TRUE( caller.rawThumb == worker.rawThumb );
    ASSERT_TRUE( caller.atExposure == worker.atExposure );
    ASSERT_TRUE( caller.atOtherExposure == worker.atOtherExposure );
    ASSERT_TRUE( caller.liveColour == worker.liveColour );
    ASSERT_TRUE( caller.balance == worker.balance );
    ASSERT_TRUE( caller.measureOnly == worker.measureOnly );
    ASSERT_TRUE( caller.measureOnlyDisplay == worker.measureOnlyDisplay );
    ASSERT_TRUE( caller.afterStateChange == worker.afterStateChange );
    ASSERT_EQ( caller.renderedAtExposure, worker.renderedAtExposure );
    ASSERT_EQ( caller.renderedBalance, worker.renderedBalance );
    ASSERT_EQ( caller.renderedMeasureOnly, worker.renderedMeasureOnly );
    ASSERT_EQ( caller.renderedMeasureOnlyDisplay, worker.renderedMeasureOnlyDisplay );
    ASSERT_EQ( caller.meterValid, worker.meterValid );
    ASSERT_EQ( caller.meterSamples, worker.meterSamples );
    ASSERT_TRUE( caller.meterMedian == worker.meterMedian );
    ASSERT_TRUE( caller.meterP05 == worker.meterP05 );
    ASSERT_TRUE( caller.meterP95 == worker.meterP95 );
    ASSERT_TRUE( caller.meterP99 == worker.meterP99 );
    for( int i = 0; i < 5; ++i )
    {
        ASSERT_EQ( caller.solved[i][0], worker.solved[i][0] );
        ASSERT_EQ( caller.solved[i][1], worker.solved[i][1] );
    }
}

void runIdentity( int cores )
{
#ifdef _OPENMP
    const int restoreThreads = omp_get_max_threads();
    omp_set_num_threads( cores );
#endif
    for( const char *clip : kDualIsoFixtureClips )
    {
        ArmResult caller;
        ArmResult worker;
        AnalysisPump inlinePump;
        ASSERT_TRUE( runArm( clip, inlinePump, cores, &caller ) );

        AnalysisWorker thread;
        AnalysisPump pump;
        pump.beginSession( &thread );
        ASSERT_TRUE( runArm( clip, pump, cores, &worker ) );
        pump.endSession();

        // Each arm really ran where it says.
        ASSERT_TRUE( !caller.threads.empty() );
        ASSERT_EQ( caller.threads.size(), worker.threads.size() );
        for( const std::thread::id id : caller.threads ) ASSERT_TRUE( id == std::this_thread::get_id() );
        for( const std::thread::id id : worker.threads )
        {
            ASSERT_TRUE( id != std::this_thread::get_id() );
            ASSERT_TRUE( id == worker.threads[0] );
        }

        // The arms rendered something the check can see.
        ASSERT_TRUE( caller.renderedAtExposure );
        ASSERT_TRUE( caller.renderedBalance );
        ASSERT_TRUE( caller.renderedMeasureOnly );
        ASSERT_TRUE( caller.renderedMeasureOnlyDisplay );
        ASSERT_TRUE( caller.meterValid );
        ASSERT_TRUE( !caller.balance.empty() );
        ASSERT_TRUE( !caller.measureOnly.empty() );
        ASSERT_TRUE( !caller.measureOnlyDisplay.empty() );

        // Negative control: the same comparison on a changed exposure and a changed balance goes red.
        ASSERT_TRUE( worker.atExposure != worker.atOtherExposure );
        ASSERT_TRUE( worker.liveColour != worker.afterStateChange );

        assertSameArm( caller, worker );
        std::printf( "[identity] %s cores=%d: %d calls on each thread, bytes and balance identical\n",
                     clip, cores, static_cast<int>( worker.threads.size() ) );
    }
#ifdef _OPENMP
    omp_set_num_threads( restoreThreads );
#endif
}

} // namespace

TEST(LookAssistAnalysisThreadIdentity, EachPrimitiveGivesTheSameBytesOnTheWorkerAtOneThread)
{
    runIdentity( 1 );
}

TEST(LookAssistAnalysisThreadIdentity, EachPrimitiveGivesTheSameBytesOnTheWorkerAtFourThreads)
{
    runIdentity( 4 );
}

#ifdef _OPENMP
// omp_set_num_threads binds to the calling thread, so a new thread does not inherit it, and a region of a heavy call would
// be a different size on the worker. Red if AnalysisPump::call stops carrying the caller's setting across.
TEST(LookAssistAnalysisThreadIdentity, TheWorkerTakesTheCallersOpenMpSettings)
{
    const int restoreThreads = omp_get_max_threads();
    const int restoreDynamic = omp_get_dynamic();
    AnalysisWorker thread;
    AnalysisPump pump;

    // The hazard is real: a job posted straight to the worker, past AnalysisPump::call, does not see the caller's setting.
    omp_set_num_threads( 7 );
    std::atomic<int> bareThreads{ 0 };
    thread.post( [&bareThreads]() { bareThreads = omp_get_max_threads(); } );
    while( bareThreads.load() == 0 ) std::this_thread::yield();
    std::printf( "[identity] caller asks for 7 threads, a bare worker thread reads %d\n", bareThreads.load() );
    ASSERT_TRUE( bareThreads.load() != 7 );

    pump.beginSession( &thread );
    for( const int threads : { 3, 1, 2 } )
    {
        omp_set_num_threads( threads );
        omp_set_dynamic( threads == 1 ? 1 : 0 );
        int seenThreads = 0;
        int seenDynamic = -1;
        int regionSize = 0;
        pump.call( [&]()
        {
            seenThreads = omp_get_max_threads();
            seenDynamic = omp_get_dynamic();
            #pragma omp parallel
            {
                #pragma omp single
                regionSize = omp_get_num_threads();
            }
        } );
        ASSERT_EQ( threads, seenThreads );
        ASSERT_EQ( omp_get_dynamic(), seenDynamic );
        if( threads != 1 )
        {
            ASSERT_EQ( threads, regionSize );
        }
    }
    pump.endSession();
    omp_set_num_threads( restoreThreads );
    omp_set_dynamic( restoreDynamic );
}
#endif // _OPENMP
