// HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 K1: the session split of the GUI-thread texture present
// (playback_smoke.texture_present_split) and the per-slip texture fields. Pure: no GUI, no clock.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackDecodeRenderOverlap.h"
#include "../../platform/qt/PlaybackSlipHistogram.h"
#include "../../platform/qt/PlaybackTexturePresentSplit.h"

#include <QFile>
#include <QString>

#include <string>

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

// Source pins: MainWindow feeds the split from values it already has, only in smoke sessions, and prints it.
TEST(PlaybackTexturePresentSplit, MainWindowWiring)
{
    const std::string mw = readRepoFile( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
    ASSERT_FALSE( mw.empty() );
    ASSERT_NE( mw.find( "const bool textureSplitSampled = m_playbackSmokeActive && m_pRenderThread;" ),
               std::string::npos );
    ASSERT_NE( mw.find( "m_playbackTextureSplit.reset();" ), std::string::npos );
    ASSERT_NE( mw.find( "m_playbackTextureSplit.note( tx );" ), std::string::npos );
    ASSERT_NE( mw.find( "playback_texture_split::formatLine(" ), std::string::npos );
    ASSERT_NE( mw.find( "playback_texture_split::formatSlipFields( r.textureKnown, r.texture )" ), std::string::npos );
}
