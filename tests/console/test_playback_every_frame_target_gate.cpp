/*
 * PLAYBACK-UNPACED-EVERY-TARGET-PAINTED-CUDA-1: unpaced, drop-frame-off (every-frame) Play must paint every source frame.
 *
 * Bachelor CUDA receipt a877bb17 (m16-1243-everyframe-visual-unpaced-fullscreen-s1, Look Assist ON) never painted source
 * frames 6 and 57: unpaced, grantWholeFrame always grants, and the 8 ms poll tick advanced the slider past a lookahead
 * slot that was Ready but not yet accepted by drawFrameReady. The target-aware acquire then skipped it and the prune
 * released it (passed over, not undecoded). The due wake, the empty-frameReady path and the lookahead-drop path already
 * obey the every-frame rule; the poll tick did not.
 *
 * These tests pin:
 *   (1) the truth table of playbackEveryFrameTickMayAdvance (platform/qt/PlaybackDueWake.h);
 *   (2) a deterministic event-sequence model of the venue's order of events (accept -> early advance -> lookahead slot
 *       Ready unaccepted -> poll tick -> accept ...), plus a seeded random mix: every frame presents once, in order;
 *   (3) the MainWindow.cpp wiring, read as text (MainWindow.cpp is not linked into console_tests).
 * Every test is named with the prefix EveryFrameTargetGate (minitest's filter takes ONE prefix).
 */

#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackDueWake.h"

#include <QFile>
#include <QString>
#include <QTextStream>

#include <algorithm>
#include <cstdint>
#include <random>
#include <set>
#include <vector>

namespace
{

// ---- (2) event-sequence model -------------------------------------------------------------------------------------
// target    = the slider (the engine target, every-frame mode)
// accepted  = the frame drawFrameReady last accepted in this Play (m_playbackEveryFrameAcceptedFrame)
// requested = render requests issued and not yet published; ready = published slots not yet accepted
// tick()    = playbackHandling's every-frame branch (the poll tick, or drawFrameReady's early advance)
// frameReady() = the target-aware acquire: it takes only the slot for the current target and the prune releases every
//                slot below the target (passed over, never painted)
struct EveryFrameModel
{
    int first = 1;
    int last = 480;
    int target = 1;
    long long accepted = -1;
    bool lookahead = true;          // the CUDA texture route: N+1 renders while N presents
    std::set<int> requested;
    std::set<int> ready;
    std::vector<int> presented;
    int maxLeadPastAccepted = 0;
    int heldTicks = 0;

    EveryFrameModel( int firstFrame, int lastFrame ) : first( firstFrame ), last( lastFrame ), target( firstFrame )
    {
        issueRequests();
    }

    void issueRequests()
    {
        if( !ready.count( target ) ) requested.insert( target );
        if( lookahead && target + 1 <= last && !ready.count( target + 1 ) ) requested.insert( target + 1 );
    }

    bool presentOutstanding( int frame ) const
    {
        return requested.count( frame ) > 0 || ready.count( frame ) > 0;
    }

    void tick()
    {
        if( target >= last ) return;   // the outer last-frame branch handles the end (Loop off: stop)
        const bool outstanding = accepted != static_cast<long long>( target ) && presentOutstanding( target );
        if( !playbackEveryFrameTickMayAdvance( true, false, accepted, target, outstanding ) )
        {
            ++heldTicks;
            return;
        }
        ++target;
        issueRequests();
        if( accepted >= 0 )
            maxLeadPastAccepted = std::max( maxLeadPastAccepted, static_cast<int>( target - accepted ) );
    }

    // the 8 ms poll tick: timerFrameEvent's only check is m_frameStillDrawing, which counts a request in flight for the
    // target but not a published Ready slot (RenderFrameThread's busy check ignores Ready slots)
    void pollTick()
    {
        if( requested.count( target ) ) return;
        tick();
    }

    void renderCompleteAll()
    {
        for( int frame : requested ) ready.insert( frame );
        requested.clear();
    }

    void renderCompleteOne()
    {
        if( requested.empty() ) return;
        const int frame = *requested.begin();
        requested.erase( requested.begin() );
        ready.insert( frame );
    }

    bool frameReady()
    {
        // prune: a slot below the target is released unpainted
        while( !ready.empty() && *ready.begin() < target ) ready.erase( ready.begin() );
        if( !ready.count( target ) ) return false;
        ready.erase( target );
        presented.push_back( target );
        accepted = target;
        tick();   // drawFrameReady's early advance runs right after the accept
        return true;
    }

    bool finished() const { return !presented.empty() && presented.back() == last; }
};

void assertEveryFramePresentedOnceInOrder( const EveryFrameModel & model )
{
    const int expected = model.last - model.first + 1;
    int gaps = 0;
    int repeats = 0;
    int outOfOrder = 0;
    for( size_t i = 1; i < model.presented.size(); ++i )
    {
        const int step = model.presented[i] - model.presented[i - 1];
        if( step > 1 ) ++gaps;
        else if( step == 0 ) ++repeats;
        else if( step < 0 ) ++outOfOrder;
    }
    ASSERT_EQ( 0, gaps );
    ASSERT_EQ( 0, repeats );
    ASSERT_EQ( 0, outOfOrder );
    ASSERT_EQ( static_cast<size_t>( expected ), model.presented.size() );
    ASSERT_EQ( model.first, model.presented.front() );
    ASSERT_EQ( model.last, model.presented.back() );
    ASSERT_TRUE( model.maxLeadPastAccepted <= 1 );
}

// ---- (3) wiring helpers ---------------------------------------------------------------------------------------------
QString readRepoFile( const QString & relativePath )
{
    const QString path = repo_file_path( relativePath );
    ASSERT_FALSE( path.isEmpty() );
    QFile file( path );
    ASSERT_TRUE( file.open( QIODevice::ReadOnly | QIODevice::Text ) );
    QTextStream stream( &file );
    return stream.readAll();
}

int countOccurrences( const QString & haystack, const QString & needle )
{
    int count = 0;
    int from = 0;
    while( true )
    {
        const int at = haystack.indexOf( needle, from );
        if( at < 0 ) break;
        ++count;
        from = at + needle.length();
    }
    return count;
}

QString functionBody( const QString & source, const QString & signature, const QString & nextSignature )
{
    const int at = source.indexOf( signature );
    const int next = source.indexOf( nextSignature, at );
    if( at < 0 || next <= at ) return QString();
    return source.mid( at, next - at );
}

QString mainWindowSource()
{
    return readRepoFile( QStringLiteral( "platform/qt/MainWindow.cpp" ) );
}

QString playbackHandlingBody( const QString & source )
{
    return functionBody( source,
                         QStringLiteral( "void MainWindow::playbackHandling(int timeDiff)" ),
                         QStringLiteral( "void MainWindow::showPerformanceProfilingDialog" ) );
}

const QString kEveryFrameBranchMarker = QStringLiteral( "//Normal mode: next frame" );
const QString kDropFrameBranchMarker = QStringLiteral( "//Drop Frame Mode: calc picture for actual time" );
const QString kGateCall = QStringLiteral( "playbackEveryFrameTickMayAdvance(" );

} // namespace

// ---- (1) truth table ------------------------------------------------------------------------------------------------
TEST( EveryFrameTargetGate, UnpacedHoldsWhileTheTargetIsUnacceptedAndItsPresentIsComing )
{
    ASSERT_FALSE( playbackEveryFrameTickMayAdvance( true, false, 5, 6, true ) );
}

TEST( EveryFrameTargetGate, UnpacedAdvancesOnceTheTargetIsAccepted )
{
    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( true, false, 6, 6, true ) );
    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( true, false, 6, 6, false ) );
}

TEST( EveryFrameTargetGate, UnpacedAdvancesWhenNoPresentOfTheTargetIsComing )
{
    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( true, false, 5, 6, false ) );
}

TEST( EveryFrameTargetGate, StartupWithNothingAcceptedIsUnchanged )
{
    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( true, false, -1, 0, true ) );
    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( true, false, -1, 6, true ) );
}

TEST( EveryFrameTargetGate, DropFrameModeIsUnchanged )
{
    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( true, true, 5, 6, true ) );
}

TEST( EveryFrameTargetGate, PacedPlayIsUnchanged )
{
    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( false, false, 5, 6, true ) );
    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( false, true, 5, 6, true ) );
}

// ---- (2) simulation -------------------------------------------------------------------------------------------------
TEST( EveryFrameTargetGate, VenueOrderPollTickBetweenEveryAcceptAndTheNextFrameReadyPaintsEveryFrame )
{
    // the venue's order: accept N -> early advance (target N+1) -> slot N+1 Ready unaccepted -> poll tick -> accept N+1 ...
    EveryFrameModel model( 1, 480 );
    for( int step = 0; step < 480 * 10 && !model.finished(); ++step )
    {
        model.renderCompleteAll();
        model.frameReady();
        model.renderCompleteAll();
        model.pollTick();   // the 8 ms poll tick
    }
    ASSERT_TRUE( model.finished() );
    assertEveryFramePresentedOnceInOrder( model );
    ASSERT_TRUE( model.heldTicks > 0 );   // the gate was exercised, not bypassed
}

TEST( EveryFrameTargetGate, SeededRandomEventMixPaintsEveryFrameOnceInOrder )
{
    for( uint32_t seed = 1; seed <= 32; ++seed )
    {
        std::mt19937 rng( seed );
        EveryFrameModel model( 1, 480 );
        int steps = 0;
        while( !model.finished() && steps < 200000 )
        {
            ++steps;
            switch( rng() % 4u )
            {
            case 0: model.renderCompleteOne(); break;
            case 1: model.renderCompleteAll(); break;
            case 2: model.frameReady(); break;
            default:
                // the poll tick. Before this Play's first accept the predicate is open by design (startup unchanged),
                // so the model starts ticking at the first accept; that window is outside this card.
                if( model.accepted >= 0 ) model.pollTick();
                break;
            }
        }
        ASSERT_TRUE( model.finished() );
        assertEveryFramePresentedOnceInOrder( model );
    }
}

// ---- (3) wiring -----------------------------------------------------------------------------------------------------
TEST( EveryFrameTargetGate, TheHoldSitsInTheDropFrameOffBranchBeforeGrantWholeFrameAndPassesUnpaced )
{
    const QString body = playbackHandlingBody( mainWindowSource() );
    ASSERT_FALSE( body.isEmpty() );
    const int branch = body.indexOf( kEveryFrameBranchMarker );
    ASSERT_TRUE( branch >= 0 );
    const int dropOff = body.indexOf( QStringLiteral( "if( !ui->actionDropFrameMode->isChecked() )" ), branch );
    ASSERT_TRUE( dropOff > branch );
    const int gate = body.indexOf( kGateCall, dropOff );
    const int grant = body.indexOf( QStringLiteral( "m_playbackPaceGuard.grantWholeFrame(" ), dropOff );
    const int dropBranch = body.indexOf( kDropFrameBranchMarker, dropOff );
    ASSERT_TRUE( gate > dropOff );
    ASSERT_TRUE( grant > gate );
    ASSERT_TRUE( dropBranch > grant );
    ASSERT_EQ( 1, countOccurrences( body, kGateCall ) );
    const QString call = body.mid( gate, grant - gate );
    ASSERT_TRUE( call.contains( QStringLiteral( "!( playbackPaceGuardFps() > 0.0 )" ) ) );
    ASSERT_TRUE( call.contains( QStringLiteral( "m_playbackEveryFrameAcceptedFrame" ) ) );
    ASSERT_TRUE( call.contains( QStringLiteral( "playbackTargetPresentOutstanding( everyFrameTarget )" ) ) );
    ASSERT_TRUE( call.contains( QStringLiteral( ") ) return;" ) ) );
}

TEST( EveryFrameTargetGate, TheDropFrameBranchHasNoGateCall )
{
    const QString body = playbackHandlingBody( mainWindowSource() );
    ASSERT_FALSE( body.isEmpty() );
    const int dropBranch = body.indexOf( kDropFrameBranchMarker );
    ASSERT_TRUE( dropBranch >= 0 );
    ASSERT_FALSE( body.mid( dropBranch ).contains( kGateCall ) );
    // the gate is in playbackHandling once, so it is nowhere else in the drop-frame engine either
    ASSERT_EQ( 1, countOccurrences( mainWindowSource(), kGateCall ) );
}

TEST( EveryFrameTargetGate, TheAcceptedFrameIsResetAtPlayStartAndSetRightAfterTheAccept )
{
    const QString source = mainWindowSource();
    const QString header = readRepoFile( QStringLiteral( "platform/qt/MainWindow.h" ) );
    ASSERT_EQ( 1, countOccurrences( header, QStringLiteral( "long long m_playbackEveryFrameAcceptedFrame = -1;" ) ) );

    const QString playStart = QStringLiteral( "m_playbackDueWakePresentedFrame = -1;   // nothing presented in this Play yet" );
    const int playStartAt = source.indexOf( playStart );
    ASSERT_TRUE( playStartAt >= 0 );
    const int resetAt = source.indexOf( QStringLiteral( "m_playbackEveryFrameAcceptedFrame = -1;" ), playStartAt );
    ASSERT_TRUE( resetAt > playStartAt );
    ASSERT_TRUE( resetAt - ( playStartAt + playStart.length() ) < 200 );

    const QString accept = QStringLiteral( "m_lastPresentedPlaybackFrame = static_cast<long long>( display_frame );" );
    ASSERT_EQ( 1, countOccurrences( source, accept ) );
    const int acceptAt = source.indexOf( accept );
    const int nextLineStart = source.indexOf( QLatin1Char( '\n' ), acceptAt ) + 1;
    const int nextLineEnd = source.indexOf( QLatin1Char( '\n' ), nextLineStart );
    const QString nextLine = source.mid( nextLineStart, nextLineEnd - nextLineStart ).trimmed();
    ASSERT_EQ( QStringLiteral( "if( ui->actionPlay->isChecked() ) m_playbackEveryFrameAcceptedFrame = static_cast<long long>( display_frame );" ).toStdString(),
               nextLine.toStdString() );

    // written only there: the Play-start reset and the accept
    ASSERT_EQ( 2, countOccurrences( source, QStringLiteral( "m_playbackEveryFrameAcceptedFrame = " ) ) );
}

TEST( EveryFrameTargetGate, ConformStillReturnsFalseWithoutConformAndDelegatesToTheModeNeutralCheck )
{
    const QString source = mainWindowSource();
    const QString conform = functionBody( source,
                                          QStringLiteral( "bool MainWindow::conformTargetPresentOutstanding()" ),
                                          QStringLiteral( "bool MainWindow::conformAcceptedTargetPresentPending()" ) );
    ASSERT_FALSE( conform.isEmpty() );
    const int noConform = conform.indexOf( QStringLiteral( "if( !m_pRenderThread || !playbackConformActive() ) return false;" ) );
    const int earlyOut = conform.indexOf(
        QStringLiteral( "if( target < 0 || m_lastPresentedPlaybackFrame == static_cast<long long>( target ) ) return false;" ) );
    const int delegate = conform.indexOf( QStringLiteral( "return playbackTargetPresentOutstanding( target );" ) );
    ASSERT_TRUE( noConform >= 0 );
    ASSERT_TRUE( earlyOut > noConform );
    ASSERT_TRUE( delegate > earlyOut );
    ASSERT_FALSE( conform.contains( QStringLiteral( "hasReadyFrameForPlaybackTarget" ) ) );
    ASSERT_FALSE( conform.contains( QStringLiteral( "hasPlaybackLookaheadRequest" ) ) );

    const QString neutral = functionBody( source,
                                          QStringLiteral( "bool MainWindow::playbackTargetPresentOutstanding( int target )" ),
                                          QStringLiteral( "bool MainWindow::conformTargetPresentOutstanding()" ) );
    ASSERT_FALSE( neutral.isEmpty() );
    ASSERT_TRUE( neutral.contains( QStringLiteral( "renderThreadBusyForPlaybackAdvance( target )" ) ) );
    ASSERT_TRUE( neutral.contains( QStringLiteral( "hasReadyFrameForPlaybackTarget(" ) ) );
    ASSERT_TRUE( neutral.contains( QStringLiteral( "hasPlaybackLookaheadRequest(" ) ) );
    ASSERT_FALSE( neutral.contains( QStringLiteral( "playbackConformActive" ) ) );
    ASSERT_FALSE( neutral.contains( QStringLiteral( "m_newPosDropMode" ) ) );

    const QString header = readRepoFile( QStringLiteral( "platform/qt/MainWindow.h" ) );
    ASSERT_EQ( 1, countOccurrences( header, QStringLiteral( "bool playbackTargetPresentOutstanding( int target );" ) ) );
}
