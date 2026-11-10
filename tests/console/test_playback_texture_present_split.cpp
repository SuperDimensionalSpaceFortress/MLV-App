// HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 K1: the session split of the GUI-thread texture present
// (playback_smoke.texture_present_split) and the per-slip texture fields. Pure: no GUI, no clock.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackDecodeRenderOverlap.h"
#include "../../platform/qt/PlaybackSlipHistogram.h"
#include "../../platform/qt/PlaybackStrideSampler.h"
#include "../../platform/qt/PlaybackTexturePresentSplit.h"

#include <QFile>
#include <QString>

#include <atomic>
#include <string>
#include <thread>

using playback_texture_split::Aggregator;
using playback_texture_split::Part;
using playback_texture_split::PartSums;
using playback_texture_split::Path;
using playback_texture_split::PathAggregator;
using playback_texture_split::Sample;

namespace
{

Sample makeSample( double kernel, double handoff, double draw, Path path = Path::Window )
{
    Sample s;
    s.path = path;
    s.at( Part::Context ) = 0.1;
    s.at( Part::Setup ) = 1.0;
    s.at( Part::SetupLut ) = 0.2;
    s.at( Part::SetupBlur ) = 0.5;
    s.at( Part::AmazeKernel ) = kernel;
    s.at( Part::AmazeHandoff ) = handoff;
    s.at( Part::AmazeWall ) = kernel + handoff + 0.25;
    s.at( Part::ReconWall ) = 0.3;
    s.at( Part::Paint ) = 0.4;
    s.at( Part::Swap ) = 0.06;
    s.at( Part::Post ) = 0.5;
    s.at( Part::Wall ) = kernel + handoff + 2.0;
    s.at( Part::HostGap ) = 0.7;
    s.at( Part::DrawTotal ) = draw;
    s.painted = true;
    return s;
}

// the value after "key=" in a space-separated line, or "" when absent
std::string field( const std::string &line, const std::string &key )
{
    const std::string needle = " " + key + "=";
    const size_t at = line.find( needle );
    if( at == std::string::npos ) return std::string();
    const size_t start = at + needle.size();
    const size_t end = line.find( ' ', start );
    return line.substr( start, end == std::string::npos ? std::string::npos : end - start );
}

std::string readRepoFile( const QString &relativePath )
{
    const QString path = repo_file_path( relativePath );
    ASSERT_FALSE( path.isEmpty() );
    QFile file( path );
    ASSERT_TRUE( file.open( QIODevice::ReadOnly ) );
    return file.readAll().toStdString();
}

} // namespace

TEST(PlaybackTexturePresentSplit, FirstPresentIsExcludedAndReportedApart)
{
    PathAggregator agg;
    Sample first = makeSample( 50.0, 10.0, 80.0 );
    first.at( Part::Wall ) = 123.5;
    agg.note( first );
    ASSERT_TRUE( agg.any() );
    ASSERT_EQ( agg.frames(), 0 );
    ASSERT_NEAR( agg.firstWallMs(), 123.5, 1e-9 );
    agg.note( makeSample( 4.0, 1.0, 9.0 ) );
    ASSERT_EQ( agg.frames(), 1 );
    ASSERT_NEAR( agg.avg( Part::AmazeKernel ), 4.0, 1e-9 );
    ASSERT_NEAR( agg.avg( Part::DrawTotal ), 9.0, 1e-9 );
}

TEST(PlaybackTexturePresentSplit, AveragesAndNearestRankP95)
{
    PathAggregator agg;
    agg.note( makeSample( 0.0, 0.0, 0.0 ) );   // first: excluded
    for( int i = 1; i <= 100; ++i ) agg.note( makeSample( static_cast<double>( i ), 1.0, 10.0 + i ) );
    ASSERT_EQ( agg.frames(), 100 );
    ASSERT_NEAR( agg.avg( Part::AmazeKernel ), 50.5, 1e-9 );
    ASSERT_NEAR( agg.p95( Part::AmazeKernel ), 95.0, 1e-9 );   // nearest rank: ceil(0.95 x 100) = 95th value
    ASSERT_NEAR( agg.median( Part::DrawTotal ), 60.0, 1e-9 );   // ceil(0.5 x 100) = 50th value = 10 + 50
    ASSERT_NEAR( agg.avg( Part::AmazeHandoff ), 1.0, 1e-9 );
}

TEST(PlaybackTexturePresentSplit, PercentileEdges)
{
    ASSERT_NEAR( playback_texture_split::percentile( {}, 0.95 ), 0.0, 1e-12 );
    ASSERT_NEAR( playback_texture_split::percentile( { 7.0 }, 0.95 ), 7.0, 1e-12 );
    ASSERT_NEAR( playback_texture_split::percentile( { 3.0, 1.0, 2.0 }, 0.5 ), 2.0, 1e-12 );
    ASSERT_NEAR( playback_texture_split::percentile( { 3.0, 1.0, 2.0 }, 1.0 ), 3.0, 1e-12 );
}

TEST(PlaybackTexturePresentSplit, TailWindowCountsOnlyKnownWindows)
{
    PathAggregator agg;
    agg.note( makeSample( 1.0, 1.0, 5.0 ) );
    Sample known = makeSample( 1.0, 1.0, 5.0 );
    known.tailWindowKnown = true;
    known.at( Part::TailWindow ) = 12.0;
    Sample unknown = makeSample( 1.0, 1.0, 5.0 );
    unknown.at( Part::TailWindow ) = 999.0;   // not a measurement: never counted
    agg.note( known );
    agg.note( unknown );
    ASSERT_EQ( agg.frames(), 2 );
    ASSERT_EQ( agg.tailWindowKnown(), 1 );
    ASSERT_NEAR( agg.avg( Part::TailWindow ), 12.0, 1e-9 );
    ASSERT_NEAR( agg.p95( Part::TailWindow ), 12.0, 1e-9 );
}

TEST(PlaybackTexturePresentSplit, CountsPaintedReconInFlightAndAheadCandidates)
{
    PathAggregator agg;
    agg.note( makeSample( 1.0, 1.0, 5.0 ) );
    Sample a = makeSample( 1.0, 1.0, 5.0 );
    a.reconInFlight = true;
    a.aheadCandidateReady = true;
    Sample b = makeSample( 1.0, 1.0, 5.0 );
    b.painted = false;
    agg.note( a );
    agg.note( b );
    ASSERT_EQ( agg.painted(), 1 );
    ASSERT_EQ( agg.reconInFlight(), 1 );
    ASSERT_EQ( agg.aheadCandidateReady(), 1 );
}

TEST(PlaybackTexturePresentSplit, PathsAreAggregatedApart)
{
    Aggregator agg;
    agg.note( makeSample( 1.0, 1.0, 5.0, Path::Window ) );
    agg.note( makeSample( 2.0, 1.0, 5.0, Path::Window ) );
    agg.note( makeSample( 9.0, 1.0, 5.0, Path::Viewport ) );
    ASSERT_EQ( agg.path( Path::Window ).frames(), 1 );
    ASSERT_NEAR( agg.path( Path::Window ).avg( Part::AmazeKernel ), 2.0, 1e-9 );
    ASSERT_TRUE( agg.path( Path::Viewport ).any() );
    ASSERT_EQ( agg.path( Path::Viewport ).frames(), 0 );
    agg.reset();
    ASSERT_FALSE( agg.path( Path::Window ).any() );
}

TEST(PlaybackTexturePresentSplit, FormatLineCarriesEveryPartAndTheGateInputs)
{
    PathAggregator agg;
    agg.note( makeSample( 1.0, 1.0, 5.0 ) );
    agg.note( makeSample( 4.0, 2.0, 9.0 ) );
    agg.note( makeSample( 6.0, 2.0, 11.0 ) );
    PartSums guiLate;
    Sample slip = makeSample( 8.0, 3.0, 17.0 );
    slip.reconInFlight = true;
    guiLate.add( slip );
    PartSums all = guiLate;
    all.add( makeSample( 2.0, 1.0, 13.0 ) );
    const std::string line = playback_texture_split::formatLine( 7, Path::Window, agg, guiLate, all );
    ASSERT_EQ( line.rfind( "playback_smoke.texture_present_split session=7 path=window frames=2 ", 0 ), 0u );
    ASSERT_EQ( field( line, "avg_amaze_kernel_ms" ), std::string( "5.000" ) );
    ASSERT_EQ( field( line, "avg_amaze_handoff_ms" ), std::string( "2.000" ) );
    ASSERT_EQ( field( line, "avg_draw_total_ms" ), std::string( "10.000" ) );
    ASSERT_EQ( field( line, "median_draw_total_ms" ), std::string( "9.000" ) );
    ASSERT_EQ( field( line, "p95_draw_total_ms" ), std::string( "11.000" ) );
    ASSERT_EQ( field( line, "slip_events" ), std::string( "1" ) );
    ASSERT_EQ( field( line, "slip_recon_in_flight" ), std::string( "1" ) );
    ASSERT_EQ( field( line, "slip_avg_amaze_kernel_ms" ), std::string( "8.000" ) );
    ASSERT_EQ( field( line, "slip_avg_draw_total_ms" ), std::string( "17.000" ) );
    ASSERT_EQ( field( line, "any_slip_events" ), std::string( "2" ) );
    ASSERT_EQ( field( line, "any_slip_avg_draw_total_ms" ), std::string( "15.000" ) );
    ASSERT_EQ( field( line, "ahead_candidate_ready_at_present_return" ), std::string( "0" ) );
    for( int i = 0; i < playback_texture_split::kPartCount; ++i )
    {
        const std::string name = playback_texture_split::partName( static_cast<Part>( i ) );
        ASSERT_FALSE( field( line, "avg_" + name + "_ms" ).empty() );
        ASSERT_FALSE( field( line, "p95_" + name + "_ms" ).empty() );
        ASSERT_FALSE( field( line, "slip_avg_" + name + "_ms" ).empty() );
    }
    // no line break and no empty field: one LIGHT line
    ASSERT_EQ( line.find( '\n' ), std::string::npos );
    ASSERT_EQ( line.find( "  " ), std::string::npos );
}

TEST(PlaybackTexturePresentSplit, SlipFieldsKnownAndUnknown)
{
    Sample s = makeSample( 8.25, 3.5, 17.0, Path::Viewport );
    s.reconInFlight = true;
    const std::string known = playback_texture_split::formatSlipFields( true, s );
    ASSERT_EQ( field( known, "amaze_kernel_ms" ), std::string( "8.250" ) );
    ASSERT_EQ( field( known, "amaze_handoff_ms" ), std::string( "3.500" ) );
    ASSERT_EQ( field( known, "present_setup_ms" ), std::string( "1.000" ) );
    ASSERT_EQ( field( known, "present_paint_ms" ), std::string( "0.400" ) );
    ASSERT_EQ( field( known, "present_swap_ms" ), std::string( "0.060" ) );
    ASSERT_EQ( field( known, "recon_in_flight" ), std::string( "1" ) );
    ASSERT_EQ( field( known, "present_path" ), std::string( "viewport" ) );
    const std::string unknown = playback_texture_split::formatSlipFields( false, s );
    ASSERT_EQ( field( unknown, "amaze_kernel_ms" ), std::string( "0.000" ) );
    ASSERT_EQ( field( unknown, "recon_in_flight" ), std::string( "-1" ) );
    ASSERT_EQ( field( unknown, "present_path" ), std::string( "none" ) );
}

// The slip histogram carries each slip present's parts and sums them per path and class over ALL slips.
TEST(PlaybackTexturePresentSplit, SlipHistogramSumsGuiLateSlipParts)
{
    const double fps = 60000.0 / 1001.0;
    const double period = 1000.0 / fps;
    playback_slip::SlipHistogram h;
    h.reset( 0, fps );
    const double t0 = 1000.0;
    int timeline = 0;
    const auto move = [&]( int pos, double ms )
    {
        h.noteTimelineMove( playback_slip::AdvancePath::DropTick, pos, ms, pos - timeline, 0.0 );
        timeline = pos;
    };
    const auto present = [&]( int frame, double ms, double readyMs, double drawMs, bool texture, double kernel )
    {
        playback_slip::PresentSample s;
        s.displayFrame = frame;
        s.presentMs = ms;
        s.readyKnown = true;
        s.readyMs = readyMs;
        s.drawMs = drawMs;
        s.timelinePosition = timeline;
        s.lookaheadCovered = true;
        s.textureKnown = texture;
        s.texture = makeSample( kernel, 1.0, drawMs );
        h.notePresent( s );
    };
    // frames 0..3 on time
    for( int k = 0; k < 4; ++k )
    {
        move( k + 1, t0 + k * period );
        present( k, t0 + k * period + 1.0, t0 + k * period - 5.0, 8.0, true, 4.0 );
    }
    // frame 4's deadline passes while frame 5 (ready early) is drawn for longer than a period: a gui_late slip
    move( 5, t0 + 4 * period );
    move( 6, t0 + 5 * period );
    present( 5, t0 + 5 * period + 2.0, t0 + 3 * period, 20.0, true, 12.0 );
    // frame 7 skipped with no texture parts: counted in no texture sum
    move( 7, t0 + 6 * period );
    move( 8, t0 + 7 * period );
    present( 7, t0 + 7 * period + 2.0, t0 + 5 * period, 20.0, false, 0.0 );
    const playback_slip::Summary sum = h.finish( t0 + 8 * period, timeline );
    ASSERT_EQ( sum.classFrames[static_cast<int>( playback_slip::SlipClass::GuiLate )], 2 );
    const PartSums &gl = sum.textureGuiLateSlips[static_cast<size_t>( Path::Window )];
    ASSERT_EQ( gl.events, 1 );
    ASSERT_NEAR( gl.avg( Part::AmazeKernel ), 12.0, 1e-9 );
    ASSERT_NEAR( gl.avg( Part::DrawTotal ), 20.0, 1e-9 );
    ASSERT_EQ( sum.textureAllSlips[static_cast<size_t>( Path::Window )].events, 1 );
    ASSERT_EQ( sum.textureGuiLateSlips[static_cast<size_t>( Path::Viewport )].events, 0 );
    ASSERT_EQ( sum.slipLines.size(), 2u );
    ASSERT_TRUE( sum.slipLines[0].textureKnown );
    ASSERT_NEAR( sum.slipLines[0].texture.at( Part::AmazeKernel ), 12.0, 1e-9 );
    ASSERT_FALSE( sum.slipLines[1].textureKnown );
}

TEST(PlaybackTexturePresentSplit, ReconActivityCountsAroundAPresent)
{
    playback_overlap::OverlapMeter meter;   // never armed: the activity is counted anyway
    const playback_overlap::ReconActivity idle0 = meter.reconActivity();
    const playback_overlap::ReconActivity idle1 = meter.reconActivity();
    ASSERT_FALSE( playback_overlap::reconRanBetween( idle0, idle1 ) );
    // a recon starts and ends between the two samples
    meter.upstreamBegin( 1.0, false, playback_overlap::UpstreamStage::Recon );
    meter.upstreamEnd( 2.0, playback_overlap::UpstreamStage::Recon );
    ASSERT_TRUE( playback_overlap::reconRanBetween( idle1, meter.reconActivity() ) );
    // a decode is not a recon
    const playback_overlap::ReconActivity before = meter.reconActivity();
    meter.upstreamBegin( 3.0, false, playback_overlap::UpstreamStage::Decode );
    ASSERT_FALSE( playback_overlap::reconRanBetween( before, meter.reconActivity() ) );
    meter.upstreamEnd( 4.0, playback_overlap::UpstreamStage::Decode );
    // a recon running across both samples; a reset while it runs loses neither its start nor its end
    meter.upstreamBegin( 5.0, false, playback_overlap::UpstreamStage::Recon );
    const playback_overlap::ReconActivity running = meter.reconActivity();
    ASSERT_EQ( running.active, 1 );
    meter.reset( 6.0 );
    ASSERT_TRUE( playback_overlap::reconRanBetween( running, meter.reconActivity() ) );
    meter.upstreamEnd( 7.0, playback_overlap::UpstreamStage::Recon );
    ASSERT_EQ( meter.reconActivity().active, 0 );
    meter.upstreamEnd( 8.0, playback_overlap::UpstreamStage::Recon );   // an unmatched end never goes below 0
    ASSERT_EQ( meter.reconActivity().active, 0 );
}

// Adversary A2 on 2f1ec397: a begin is ONE atomic step (a start and an active recon at once), so no reader sees a
// start counted with its recon not yet active, and the adversary's interleaving (before=(0,0), worker half way, a
// whole recon during the present, after=(1,0)) cannot occur: the half-way state is not representable.
TEST(PlaybackTexturePresentSplit, ReconActivityBeginIsOneAtomicStep)
{
    using namespace playback_overlap;
    const ReconActivity afterBegin = unpackReconActivity( packReconActivity( 0, 0 ) + kReconActivityBeginDelta );
    ASSERT_EQ( 1ull, afterBegin.starts );
    ASSERT_EQ( 1, afterBegin.active );
    const ReconActivity afterEnd = unpackReconActivity( packReconActivity( 1, 1 ) - 1 );
    ASSERT_EQ( 1ull, afterEnd.starts );
    ASSERT_EQ( 0, afterEnd.active );
    const ReconActivity big = unpackReconActivity( packReconActivity( 123456789ull, 3 ) );
    ASSERT_EQ( 123456789ull, big.starts );
    ASSERT_EQ( 3, big.active );
    // the adversary's input: a recon that starts and ends between the two samples is seen
    ASSERT_TRUE( reconRanBetween( unpackReconActivity( packReconActivity( 0, 0 ) ),
                                  unpackReconActivity( packReconActivity( 1, 0 ) ) ) );
    // and the meter itself goes through those states only
    OverlapMeter meter;
    meter.upstreamBegin( 1.0, false, UpstreamStage::Recon );
    ASSERT_EQ( 1ull, meter.reconActivity().starts );
    ASSERT_EQ( 1, meter.reconActivity().active );
}

// A2, observed: one worker begins and ends recons while a reader samples. Every sample must be a state the worker
// passed through: completed = starts - active never decreases and active is 0 or 1. With the old two-word counter a
// sample between the two increments read (starts=n+1, active=0) and the next one (n+1, 1): completed went back.
TEST(PlaybackTexturePresentSplit, ReconActivitySamplesAreConsistentUnderConcurrency)
{
    playback_overlap::OverlapMeter meter;
    std::atomic<bool> stop{ false };
    std::thread worker( [&]()
    {
        for( int i = 0; i < 200000 && !stop.load(); ++i )
        {
            meter.upstreamBegin( 0.0, false, playback_overlap::UpstreamStage::Recon );
            meter.upstreamEnd( 0.0, playback_overlap::UpstreamStage::Recon );
        }
        stop.store( true );
    } );
    long long lastCompleted = 0;
    bool consistent = true;
    while( !stop.load() )
    {
        const playback_overlap::ReconActivity a = meter.reconActivity();
        const long long completed = static_cast<long long>( a.starts ) - a.active;
        if( a.active < 0 || a.active > 1 || completed < lastCompleted ) consistent = false;
        lastCompleted = completed;
    }
    worker.join();
    ASSERT_TRUE( consistent );
    ASSERT_EQ( 200000ull, meter.reconActivity().starts );
    ASSERT_EQ( 0, meter.reconActivity().active );
}

// Adversary A3 on 2f1ec397: the AMaZE presenter ran and failed, and a fallback presented the frame. The failed
// attempt's setup keys must not enter the split.
TEST(PlaybackTexturePresentSplit, FailedAmazePresenterWithFallbackIsNotSampled)
{
    ASSERT_FALSE( playback_texture_split::sampleEligible( true, false ) );   // setup keys left, AMaZE presenter failed
    ASSERT_FALSE( playback_texture_split::sampleEligible( false, true ) );
    ASSERT_FALSE( playback_texture_split::sampleEligible( false, false ) );
    ASSERT_TRUE( playback_texture_split::sampleEligible( true, true ) );
}

// Adversary A4 on 2f1ec397: past the sampler cap the percentiles describe the whole session, not its prefix.
TEST(PlaybackTexturePresentSplit, PercentilesCoverTheWholeSessionPastTheCap)
{
    PathAggregator agg;
    agg.note( makeSample( 1.0, 1.0, 1.0 ) );   // excluded first present
    for( int i = 0; i < 100000; ++i ) agg.note( makeSample( 1.0, 1.0, 1.0 ) );
    for( int i = 0; i < 100001; ++i ) agg.note( makeSample( 1.0, 1.0, 10.0 ) );
    ASSERT_EQ( 200001, agg.frames() );
    ASSERT_NEAR( 10.0, agg.median( Part::DrawTotal ), 1e-9 );
    ASSERT_NEAR( 10.0, agg.p95( Part::DrawTotal ), 1e-9 );
    ASSERT_TRUE( agg.percentileStride() > 1ull );
}

TEST(PlaybackTexturePresentSplit, StrideSamplerKeepsEveryValueBelowTheCapAndAUniformSampleAbove)
{
    playback_sampling::StrideSampler s( 8 );
    for( int i = 0; i < 7; ++i ) s.add( i );
    ASSERT_EQ( size_t( 7 ), s.size() );   // below the cap: every value, in order
    ASSERT_EQ( 1ull, s.stride() );
    s.add( 7 );                            // reaches the cap: keeps 0,2,4,6 and strides 2
    ASSERT_EQ( size_t( 4 ), s.size() );
    ASSERT_EQ( 2ull, s.stride() );
    for( int i = 8; i < 100; ++i ) s.add( i );
    ASSERT_TRUE( s.size() < s.cap() );
    ASSERT_EQ( 100ull, s.seen() );
    // every kept value is a multiple of the stride, starting at 0, with none skipped in between
    for( size_t k = 0; k < s.size(); ++k )
        ASSERT_NEAR( static_cast<double>( k * s.stride() ), s.values()[k], 1e-12 );
    // the last kept value is within one stride of the session's end: the sample spans the whole session
    ASSERT_TRUE( s.values().back() + static_cast<double>( s.stride() ) >= 99.0 );
}

// A4 siblings: the slip histogram's upstream-stage medians follow the whole session too.
TEST(PlaybackTexturePresentSplit, SlipStageMediansCoverTheWholeSessionPastTheCap)
{
    const double fps = 60000.0 / 1001.0;
    const double period = 1000.0 / fps;
    playback_slip::SlipHistogram h;
    h.reset( 0, fps );
    const int prefix = playback_slip::kMaxStageSamples;   // the first cap's worth is cheap: the old prefix median is 1
    const int n = 3 * playback_slip::kMaxStageSamples + 3;  // two thirds of the session are not: its median is 10
    int timeline = 0;
    double t = 1000.0;
    for( int k = 0; k < n; ++k )
    {
        h.noteTimelineMove( playback_slip::AdvancePath::DropTick, k + 1, t, 1.0, 0.0 );
        timeline = k + 1;
        playback_slip::PresentSample s;
        s.displayFrame = k;
        s.presentMs = t + 1.0;
        s.readyKnown = true;
        s.readyMs = t - 1.0;
        s.decodeMs = k < prefix ? 1.0 : 10.0;
        s.drawMs = 5.0;
        s.timelinePosition = timeline;
        s.lookaheadCovered = true;
        // one upstream-late slip at the very end: its decode (12) is over 2x the median only if the median is the
        // PREFIX's (1.0); against the whole session's median (10.0) it is not
        if( k == n - 1 )
        {
            h.noteTimelineMove( playback_slip::AdvancePath::DropTick, k + 2, t + period, 1.0, 0.0 );
            timeline = k + 2;
            s.displayFrame = k + 1;
            s.presentMs = t + period + 1.0;
            s.readyMs = t + period + 0.5;
            s.decodeMs = 12.0;
            s.timelinePosition = timeline;
        }
        h.notePresent( s );
        t += period;
    }
    const playback_slip::Summary sum = h.finish( t, timeline );
    ASSERT_EQ( 1LL, sum.classFrames[static_cast<int>( playback_slip::SlipClass::UpstreamLate )] );
    ASSERT_EQ( 0LL, sum.upstreamStageFrames[static_cast<int>( playback_slip::UpstreamStage::Decode )] );
    ASSERT_EQ( 1LL, sum.upstreamStageFrames[static_cast<int>( playback_slip::UpstreamStage::None )] );
}

// A1 on 2f1ec397 (GpuDisplayViewport needs a GL build, so its source is pinned): the viewport AMaZE presenter records
// its AMaZE-only parts like GpuDisplayWindow, and MainWindow samples only the AMaZE presenter's own presents (A3).
TEST(PlaybackTexturePresentSplit, ViewportRecordsAmazePartsAndMainWindowGatesOnTheAmazePresenter)
{
    const std::string vp = readRepoFile( QStringLiteral( "platform/qt/GpuDisplayViewport.cpp" ) );
    ASSERT_NE( std::string::npos, vp.find( "        setupTiming.amaze_kernel_ms = amazeTiming.kernelMs;" ) );
    ASSERT_NE( std::string::npos, vp.find( "        setupTiming.amaze_download_ms = amazeTiming.downloadMs;" ) );
    const std::string mw = readRepoFile( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_NE( std::string::npos, mw.find( "telemetryBoolValue( timing, \"gpu_playback_recon_amaze_texture_present_active\" ) ) )" ) );
}

// Fix 2, adversary A1 on 3390ee51: Loop on, cut 0..719, presenting 719 while frame 0's lookahead recon already holds
// its retained Bayer16. Both slots hold a token: another frame's is ready (the old "published frame > presented
// frame" test said 0 > 719, no).
TEST(PlaybackTexturePresentSplit, AheadCandidateCountsALoopWrappedLookahead)
{
    ASSERT_TRUE( playback_texture_split::aheadCandidateReady( 2, true ) );
    ASSERT_TRUE( playback_texture_split::aheadCandidateReady( 1, false ) );
}

// Fix 2, adversary A2 on 3390ee51: frame 101's recon published token 17 and then rejected and released it; the slot
// count went +1 and -1, so only the presented frame's own token is held: no candidate (the old marker kept 101).
TEST(PlaybackTexturePresentSplit, AheadCandidateIgnoresAReleasedPublication)
{
    int held = 1;   // the presented frame 100's own slot
    ++held;         // frame 101 publishes token 17 into its slot
    --held;         // ...which the final validation rejects and releases
    ASSERT_FALSE( playback_texture_split::aheadCandidateReady( held, true ) );
    ASSERT_FALSE( playback_texture_split::aheadCandidateReady( 0, false ) );
}

// The slot count is conserved: every RenderFrameThread site that releases a slot's retained token also takes it off the
// count, and the one publish site adds it (a new release site without the decrement reds here).
TEST(PlaybackTexturePresentSplit, RetainedSlotCountTracksEveryPublishAndRelease)
{
    const std::string rft = readRepoFile( QStringLiteral( "platform/qt/RenderFrameThread.cpp" ) );
    const auto count = [&rft]( const std::string &needle )
    {
        size_t n = 0;
        for( size_t at = rft.find( needle ); at != std::string::npos; at = rft.find( needle, at + needle.size() ) ) ++n;
        return n;
    };
    const size_t releases = count( "llrpGpuPlaybackReconReleaseRetainedDeviceBayer16(" );
    ASSERT_EQ( size_t( 3 ), releases );
    ASSERT_EQ( releases, count( "m_retainedDeviceBayer16SlotsHeld.fetch_sub( 1, std::memory_order_acq_rel );" ) );
    ASSERT_EQ( size_t( 1 ), count( "m_retainedDeviceBayer16SlotsHeld.fetch_add( 1, std::memory_order_acq_rel );" ) );
    const std::string mw = readRepoFile( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_NE( std::string::npos, mw.find( "m_pRenderThread->retainedDeviceBayer16SlotsHeld()," ) );
    ASSERT_NE( std::string::npos, mw.find( "task.gpuPlaybackReconTextureRetainedDeviceToken != 0 ) );" ) );
}

// Fix 2, adversary A3 on 3390ee51: a fallback present (no texture parts), then the path's FIRST texture present skips a
// frame as gui_late. The split excludes that first present, so the slip sums must not carry it either.
TEST(PlaybackTexturePresentSplit, ExcludedFirstTexturePresentIsInNoSlipSum)
{
    const double fps = 60.0;
    playback_slip::SlipHistogram h;
    h.reset( 0, fps );
    Aggregator split;
    int timeline = 0;
    const auto move = [&]( int pos, double ms )
    {
        h.noteTimelineMove( playback_slip::AdvancePath::DropTick, pos, ms, pos - timeline, 0.0 );
        timeline = pos;
    };
    move( 1, 1000.0 );
    playback_slip::PresentSample fallback;
    fallback.displayFrame = 0;
    fallback.presentMs = 1001.0;
    fallback.readyKnown = true;
    fallback.readyMs = 999.0;
    fallback.drawMs = 5.0;
    fallback.timelinePosition = timeline;
    fallback.textureKnown = false;
    h.notePresent( fallback );
    move( 2, 1016.667 );
    move( 3, 1033.333 );
    playback_slip::PresentSample first;
    first.displayFrame = 2;
    first.presentMs = 1081.0;
    first.readyKnown = true;
    first.readyMs = 1001.0;
    first.drawMs = 80.0;
    first.timelinePosition = timeline;
    first.texture = makeSample( 50.0, 1.0, 80.0 );
    first.texture.at( Part::Wall ) = 123.5;
    first.textureKnown = split.note( first.texture );   // as MainWindow does
    h.notePresent( first );
    const playback_slip::Summary sum = h.finish( 1100.0, timeline );
    ASSERT_EQ( 1LL, sum.classFrames[static_cast<int>( playback_slip::SlipClass::GuiLate )] );
    ASSERT_FALSE( first.textureKnown );
    ASSERT_EQ( 0, split.path( Path::Window ).frames() );
    ASSERT_NEAR( 123.5, split.path( Path::Window ).firstWallMs(), 1e-9 );
    ASSERT_EQ( 0, sum.textureGuiLateSlips[static_cast<size_t>( Path::Window )].events );
    ASSERT_EQ( 0, sum.textureAllSlips[static_cast<size_t>( Path::Window )].events );
}

// Source pins: MainWindow feeds the split from values it already has, only in smoke sessions, and prints it.
TEST(PlaybackTexturePresentSplit, MainWindowWiring)
{
    const std::string mw = readRepoFile( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_FALSE( mw.empty() );
    ASSERT_NE( mw.find( "const bool textureSplitSampled = m_playbackSmokeActive && m_pRenderThread;" ),
               std::string::npos );
    ASSERT_NE( mw.find( "m_playbackTextureSplit.reset();" ), std::string::npos );
    ASSERT_NE( mw.find( "slipSample.textureKnown = m_playbackTextureSplit.note( tx );" ), std::string::npos );
    ASSERT_NE( mw.find( "playback_texture_split::formatLine(" ), std::string::npos );
    ASSERT_NE( mw.find( "playback_texture_split::formatSlipFields( r.textureKnown, r.texture )" ), std::string::npos );
}
