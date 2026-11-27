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
 *   (3) the MainWindow.cpp wiring, read as text (MainWindow.cpp is not linked into console_tests);
 *   (4) self-tests of the source-text helpers the wiring pins rely on (stripComments: splicing, literals, raw strings).
 * Every test is named with the prefix EveryFrameTargetGate (minitest's filter takes ONE prefix).
 */

#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/PlaybackDueWake.h"

#include <QFile>
#include <QString>
#include <QStringList>
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
// Scope (fix 1, adversary A5 on 5370a8e6): the model covers the every-frame branch's advance up to the last frame. It does NOT
// model the outer last-frame branch (a poll at the cut-out stops or wraps before the last target is accepted: adversary A2,
// pre-existing) or the display's paint coalescing after an accept (adversary A3, pre-existing); "presented" here means accepted
// by drawFrameReady. Both are named follow-ups of this card, not claims of these tests.
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
    // fix 2 (adversary A1 on 06031667): with notifyModel, frameReady runs only on a pending notification (one per publish), as
    // the frameReady signal does; kickOnHold is MainWindow's queued drawFrameReady for a held target already published Ready
    bool notifyModel = false;
    bool kickOnHold = true;
    int notifications = 0;

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
            if( kickOnHold && ready.count( target ) ) ++notifications;
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
        if( notifyModel )
        {
            if( notifications == 0 ) return false;
            --notifications;
        }
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

// r2 (adversary A4 on 06031667): a dead guard (an `if( false )` in front of a statement, a dead block around it, an `#if 0`)
// left the substring and order assertions true. These helpers let a test pin the comment-free code exactly and pin the path of
// blocks that encloses a statement, so a dead guard anywhere on that path changes a pinned value.
// The code with its // and /* */ comments removed (string and char literals kept), so neither a guard nor a pin hides in a comment.
// r7 (Grok CoS G7, adversary A3): the stripper follows the compiler's lexing, so a pin cannot be fooled by text the compiler reads
// differently:
//   - translation phase 2 line splicing: a backslash right before a newline (optionally with spaces or tabs between them, as GCC,
//     Clang and C++23 accept; also before CR LF) joins the lines, so `// note \` + newline + `code();` is ONE comment, and a splice
//     inside `/` `/`, `/` `*` or `*` `/` still opens or closes a comment;
//   - string and char literals (any encoding prefix), with escaped quotes and escaped backslashes, so a `//` or `/*` inside a literal
//     is literal text and the code after the literal on the same line survives;
//   - raw string literals `R"delim(...)delim"` (prefixes R, LR, uR, UR, u8R): their body is copied verbatim and, as the standard
//     says, splicing is undone inside them;
//   - digit separators (`1'000'000`), which are not char literals.
// Splices in code outside comments are copied unchanged, so the code text the exact pins compare is the source text.
// Not handled: header-names (`#include <a//b.h>`), trigraphs (removed in C++17), and a macro that expands to a comment or to code
// (the stripper does not preprocess). No pin depends on these: MainWindow.cpp has no splice, raw string, digit separator or
// header-name with `//` or `/*` (the EveryFrameTargetGate.StripComments* tests below prove each handled case on fixtures).
int skipLineSplices( const QString & s, int i )
{
    const int n = s.size();
    while( i < n && s.at( i ) == QLatin1Char( '\\' ) )
    {
        int k = i + 1;
        while( k < n && ( s.at( k ) == QLatin1Char( ' ' ) || s.at( k ) == QLatin1Char( '\t' ) ) ) ++k;
        if( k < n && s.at( k ) == QLatin1Char( '\r' ) && k + 1 < n && s.at( k + 1 ) == QLatin1Char( '\n' ) ) ++k;
        if( k >= n || s.at( k ) != QLatin1Char( '\n' ) ) break;
        i = k + 1;
    }
    return i;
}

bool isIdentifierChar( QChar c )
{
    return c.isLetterOrNumber() || c == QLatin1Char( '_' );
}

// skipLineSplices scanned backwards: the position before the splices that end right at \a i.
int skipLineSplicesBackward( const QString & s, int i )
{
    while( i > 0 && s.at( i - 1 ) == QLatin1Char( '\n' ) )
    {
        int k = i - 1;
        if( k > 0 && s.at( k - 1 ) == QLatin1Char( '\r' ) ) --k;
        while( k > 0 && ( s.at( k - 1 ) == QLatin1Char( ' ' ) || s.at( k - 1 ) == QLatin1Char( '\t' ) ) ) --k;
        if( k == 0 || s.at( k - 1 ) != QLatin1Char( '\\' ) ) break;
        i = k - 1;
    }
    return i;
}

// The identifier or pp-number that ends right before \a i (empty when \a i follows punctuation or whitespace), read after
// splicing (r7, adversary A1 on db994303): `R\` + newline + `"x(` is the raw-string prefix R, and `1\` + newline + `'000` is a
// digit separator, exactly as the compiler reads them.
QString tokenBefore( const QString & s, int i )
{
    QString token;
    int k = skipLineSplicesBackward( s, i );
    while( k > 0 && ( isIdentifierChar( s.at( k - 1 ) ) || s.at( k - 1 ) == QLatin1Char( '.' ) ) )
    {
        token.prepend( s.at( k - 1 ) );
        k = skipLineSplicesBackward( s, k - 1 );
    }
    return token;
}

QString stripComments( const QString & s )
{
    QString out;
    out.reserve( s.size() );
    const int n = s.size();
    int i = 0;
    while( i < n )
    {
        const QChar c = s.at( i );
        if( c == QLatin1Char( '\\' ) && skipLineSplices( s, i ) > i )
        {
            const int end = skipLineSplices( s, i );   // a splice in code: copied unchanged
            out.append( s.mid( i, end - i ) );
            i = end;
            continue;
        }
        if( c == QLatin1Char( '"' ) )
        {
            const QString prefix = tokenBefore( s, i );
            const bool raw = prefix == QLatin1String( "R" ) || prefix == QLatin1String( "LR" ) || prefix == QLatin1String( "uR" )
                             || prefix == QLatin1String( "UR" ) || prefix == QLatin1String( "u8R" );
            if( raw )
            {
                const int open = s.indexOf( QLatin1Char( '(' ), i + 1 );
                const QString delimiter = open > i ? s.mid( i + 1, open - i - 1 ) : QString();
                bool delimiterValid = open > i && delimiter.size() <= 16;
                for( const QChar d : delimiter )
                    if( d.isSpace() || d == QLatin1Char( ')' ) || d == QLatin1Char( '\\' ) || d == QLatin1Char( '"' ) )
                        delimiterValid = false;
                if( delimiterValid )
                {
                    const QString closer = QLatin1Char( ')' ) + s.mid( i + 1, open - i - 1 ) + QLatin1Char( '"' );
                    const int close = s.indexOf( closer, open + 1 );
                    const int end = close < 0 ? n : close + closer.length();
                    out.append( s.mid( i, end - i ) );
                    i = end;
                    continue;
                }
            }
        }
        if( c == QLatin1Char( '\'' ) )
        {
            const QString token = tokenBefore( s, i );
            const bool digitSeparator = !token.isEmpty()
                                        && ( token.at( 0 ).isDigit()
                                             || ( token.at( 0 ) == QLatin1Char( '.' ) && token.size() > 1 && token.at( 1 ).isDigit() ) );
            if( digitSeparator )
            {
                out.append( c );
                ++i;
                continue;
            }
        }
        if( c == QLatin1Char( '"' ) || c == QLatin1Char( '\'' ) )
        {
            out.append( c );
            ++i;
            while( i < n && s.at( i ) != c && s.at( i ) != QLatin1Char( '\n' ) )
            {
                if( s.at( i ) == QLatin1Char( '\\' ) && skipLineSplices( s, i ) > i )
                {
                    const int end = skipLineSplices( s, i );   // a splice continues the literal
                    out.append( s.mid( i, end - i ) );
                    i = end;
                    continue;
                }
                if( s.at( i ) == QLatin1Char( '\\' ) && i + 1 < n )
                {
                    out.append( s.at( i ) );
                    out.append( s.at( i + 1 ) );
                    i += 2;
                    continue;
                }
                out.append( s.at( i ) );
                ++i;
            }
            if( i < n && s.at( i ) == c )
            {
                out.append( c );
                ++i;
            }
            continue;
        }
        if( c == QLatin1Char( '/' ) )
        {
            const int second = skipLineSplices( s, i + 1 );
            if( second < n && s.at( second ) == QLatin1Char( '/' ) )
            {
                // a // comment runs to the first newline that is not spliced away
                int k = second + 1;
                while( k < n )
                {
                    const int afterSplices = skipLineSplices( s, k );
                    if( afterSplices > k )
                    {
                        k = afterSplices;
                        continue;
                    }
                    if( s.at( k ) == QLatin1Char( '\n' ) ) break;
                    ++k;
                }
                i = k;   // the newline itself is code (kept)
                continue;
            }
            if( second < n && s.at( second ) == QLatin1Char( '*' ) )
            {
                int k = second + 1;
                int end = n;
                while( k < n )
                {
                    if( s.at( k ) == QLatin1Char( '*' ) )
                    {
                        const int slash = skipLineSplices( s, k + 1 );
                        if( slash < n && s.at( slash ) == QLatin1Char( '/' ) )
                        {
                            end = slash + 1;
                            break;
                        }
                    }
                    ++k;
                }
                i = end;
                out.append( QLatin1Char( ' ' ) );
                continue;
            }
        }
        out.append( c );
        ++i;
    }
    return out;
}

// The headers of the blocks that enclose \a pos in comment-free \a code, outermost first. A header is the simplified text from
// the previous ';', '{' or '}' up to the block's '{', so an unbraced guard in front of a braced statement is part of it. An
// "else" header carries the chain it belongs to ("if( A ) / else"), so a guard in front of the chain's "if" is on the path too.
QStringList enclosingBlockHeaders( const QString & code, int pos )
{
    struct OpenBlock
    {
        QString header;
        QString full;
    };
    std::vector<OpenBlock> open;
    std::vector<QStringList> chain( 1 );   // per depth: the if/else chain closed at that depth since its last ';'
    const QString elseWord = QStringLiteral( "else" );
    const QString link = QStringLiteral( " / " );
    int lastSeparator = -1;
    QChar inLiteral;   // null outside a string or char literal
    for( int i = 0; i < pos && i < code.size(); ++i )
    {
        const QChar c = code.at( i );
        if( !inLiteral.isNull() )
        {
            if( c == QLatin1Char( '\\' ) )
            {
                ++i;
                continue;
            }
            if( c == inLiteral || c == QLatin1Char( '\n' ) ) inLiteral = QChar();
            continue;
        }
        if( c == QLatin1Char( '"' ) || c == QLatin1Char( '\'' ) )
            inLiteral = c;
        else if( c == QLatin1Char( '{' ) )
        {
            const QString header = code.mid( lastSeparator + 1, i - lastSeparator - 1 ).simplified();
            QStringList full = chain.back();
            full << header;
            open.push_back( { header, header.startsWith( elseWord ) ? full.join( link ) : header } );
            chain.emplace_back();
            lastSeparator = i;
        }
        else if( c == QLatin1Char( '}' ) )
        {
            if( !open.empty() )
            {
                const OpenBlock closed = open.back();
                open.pop_back();
                chain.pop_back();
                chain.back() = closed.header.startsWith( elseWord ) ? closed.full.split( link ) : QStringList{ closed.header };
            }
            lastSeparator = i;
        }
        else if( c == QLatin1Char( ';' ) )
        {
            chain.back().clear();
            lastSeparator = i;
        }
    }
    QStringList headers;
    for( const OpenBlock & block : open ) headers << block.full;
    return headers;
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

// fix 1 (adversary A1 on 5370a8e6): 24 fps paced CUDA, Drop Frame off, accepted 5, target 6 Ready -- the paced tick must not ask the
// render thread (its mutex can be held for a whole speculative publish). The verdict consults "outstanding" only where it depends
// on it, and wherever it does not, the verdict is true whatever outstanding says, so the caller's short-circuit changes nothing.
TEST( EveryFrameTargetGate, OnlyAnUnpacedUnacceptedTargetConsultsTheRenderThread )
{
    ASSERT_FALSE( playbackEveryFrameTickConsultsOutstanding( false, false, 5, 6 ) );   // paced: the A1 input
    ASSERT_FALSE( playbackEveryFrameTickConsultsOutstanding( false, true, 5, 6 ) );
    ASSERT_FALSE( playbackEveryFrameTickConsultsOutstanding( true, true, 5, 6 ) );     // drop-frame
    ASSERT_FALSE( playbackEveryFrameTickConsultsOutstanding( true, false, -1, 6 ) );   // nothing accepted yet
    ASSERT_FALSE( playbackEveryFrameTickConsultsOutstanding( true, false, 6, 6 ) );    // target accepted
    ASSERT_TRUE( playbackEveryFrameTickConsultsOutstanding( true, false, 5, 6 ) );
    for( int unpaced = 0; unpaced < 2; ++unpaced )
        for( int drop = 0; drop < 2; ++drop )
            for( long long accepted = -1; accepted <= 7; ++accepted )
                for( int target = 0; target <= 7; ++target )
                {
                    if( playbackEveryFrameTickConsultsOutstanding( unpaced != 0, drop != 0, accepted, target ) ) continue;
                    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( unpaced != 0, drop != 0, accepted, target, true ) );
                    ASSERT_TRUE( playbackEveryFrameTickMayAdvance( unpaced != 0, drop != 0, accepted, target, false ) );
                }
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

// fix 2 (adversary A1 on 06031667): frames 6 and 7 (the cut-out) were published Ready and their frameReady callbacks handled while
// the slider was still on 5, so no callback is left and no new lookahead is requested. Without the held tick's queued
// drawFrameReady the hold waits forever; with it every frame is taken, in order.
TEST( EveryFrameTargetGate, AHeldTargetWhoseCallbackWasAlreadyHandledIsStillTaken )
{
    for( int kick = 0; kick < 2; ++kick )
    {
        // last = 8 so frame 7 is taken by the every-frame branch (the cut-out's own branch is adversary A2, outside this model);
        // 8 is requested but never published here
        EveryFrameModel model( 1, 8 );
        model.notifyModel = true;
        model.kickOnHold = kick != 0;
        model.requested.clear();
        model.target = 5;
        model.accepted = 5;
        model.presented = { 1, 2, 3, 4, 5 };
        model.ready = { 6, 7 };
        model.notifications = 0;
        for( int step = 0; step < 100 && model.presented.back() != 7; ++step )
        {
            model.pollTick();
            model.frameReady();
        }
        if( kick == 0 )
        {
            ASSERT_EQ( 5, model.presented.back() );   // the stall the adversary traced: held at 6 with 6 Ready and no callback
            ASSERT_EQ( 6, model.target );
            ASSERT_TRUE( model.heldTicks > 50 );
        }
        else
        {
            ASSERT_TRUE( ( model.presented == std::vector<int>{ 1, 2, 3, 4, 5, 6, 7 } ) );
            ASSERT_TRUE( model.maxLeadPastAccepted <= 1 );
        }
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
    // the hold block: from the unpaced flag to grantWholeFrame
    const int unpacedAt = body.indexOf( QStringLiteral( "const bool everyFrameUnpaced = !( playbackPaceGuardFps() > 0.0 );" ), dropOff );
    ASSERT_TRUE( unpacedAt > dropOff );
    ASSERT_TRUE( unpacedAt < gate );
    const QString span = body.mid( gate, grant - gate );
    const int holdEnd = span.indexOf( QStringLiteral( "return;" ) );
    ASSERT_TRUE( holdEnd > 0 );
    const QString call = span.left( holdEnd + 7 ).simplified();   // the hold statement alone, up to its return
    ASSERT_TRUE( call.startsWith( QStringLiteral(
        "playbackEveryFrameTickMayAdvance( everyFrameUnpaced, false, m_playbackEveryFrameAcceptedFrame, everyFrameTarget, " ) ) );
    // fix 1 (adversary A1 on 5370a8e6): the render thread is asked only through the && short-circuit, after the same pure
    // consults-check the verdict uses, so a paced tick never takes the render mutex
    const QString outstandingArg = QStringLiteral(
        "playbackEveryFrameTickConsultsOutstanding( everyFrameUnpaced, false, m_playbackEveryFrameAcceptedFrame, everyFrameTarget ) "
        "&& playbackTargetPresentOutstanding( everyFrameTarget ) ) ) {" );
    ASSERT_TRUE( call.contains( outstandingArg ) );
    // fix 2 (adversary A1 on 06031667): the held tick asks drawFrameReady to take a target already published Ready (its frameReady
    // may have been handled while the slider was on the frame before), then returns
    const QString kick = call.mid( call.indexOf( outstandingArg ) + outstandingArg.length() );
    ASSERT_TRUE( kick.contains( QStringLiteral( "if( m_pRenderThread && m_pRenderThread->hasReadyFrameForPlaybackTarget( "
                                                "static_cast<uint32_t>( everyFrameTarget ), " ) ) );
    ASSERT_EQ( 1, countOccurrences( kick, QStringLiteral( "invokeMethod" ) ) );
    ASSERT_TRUE( kick.endsWith( QStringLiteral(
        "QMetaObject::invokeMethod( this, \"drawFrameReady\", Qt::QueuedConnection ); return;" ) ) );
    ASSERT_EQ( 1, countOccurrences( body, QStringLiteral( "playbackTargetPresentOutstanding(" ) ) );
    // fix 1 (adversary A4 on 5370a8e6): the polarity -- outstanding holds, never its negation
    ASSERT_FALSE( call.contains( QStringLiteral( "!playbackTargetPresentOutstanding" ) ) );
    ASSERT_FALSE( call.contains( QStringLiteral( "! playbackTargetPresentOutstanding" ) ) );
    ASSERT_TRUE( call.startsWith( QStringLiteral( "playbackEveryFrameTickMayAdvance(" ) ) );
    const QString before = body.mid( dropOff, gate - dropOff ).simplified();
    ASSERT_TRUE( before.endsWith( QStringLiteral( "if( !" ) ) );   // the hold returns when the verdict is false
}

// r2 (adversary A4 on 06031667): `if( false )` inserted right in front of the hold kept every assertion above true, while an
// unpaced tick with accepted 5 and target 6 published would then reach the grant and advance. The every-frame branch's code
// (comments removed) is pinned exactly from its marker to the grant, which covers a dead guard in front of the hold, a dead
// block, an `#if 0`, and a dead guard on the Ready kick or on the drop-off block; and the path of blocks that encloses the hold
// is pinned, which covers a dead guard further out (in front of the outer last-frame if/else chain, or a dead block around it).
TEST( EveryFrameTargetGate, NoDeadGuardEnclosesTheHoldOrItsReadyKick )
{
    const QString body = playbackHandlingBody( mainWindowSource() );
    ASSERT_FALSE( body.isEmpty() );
    const int branch = body.indexOf( kEveryFrameBranchMarker );
    ASSERT_TRUE( branch >= 0 );
    const QString grantCall = QStringLiteral( "m_playbackPaceGuard.grantWholeFrame(" );
    const int grant = body.indexOf( grantCall, branch );
    ASSERT_TRUE( grant > branch );
    const QString holdSpan = stripComments( body.mid( branch, grant + grantCall.length() - branch ) ).simplified();
    ASSERT_EQ( std::string(
                   "if( !ui->actionDropFrameMode->isChecked() ) { "
                   "const int everyFrameTarget = ui->horizontalSliderPosition->value(); "
                   "const bool everyFrameUnpaced = !( playbackPaceGuardFps() > 0.0 ); "
                   "if( !playbackEveryFrameTickMayAdvance( everyFrameUnpaced, false, m_playbackEveryFrameAcceptedFrame, everyFrameTarget, "
                   "playbackEveryFrameTickConsultsOutstanding( everyFrameUnpaced, false, m_playbackEveryFrameAcceptedFrame, everyFrameTarget ) "
                   "&& playbackTargetPresentOutstanding( everyFrameTarget ) ) ) { "
                   "if( m_pRenderThread && m_pRenderThread->hasReadyFrameForPlaybackTarget( static_cast<uint32_t>( everyFrameTarget ), "
                   "m_playbackPresentationGeneration.load( std::memory_order_acquire ) ) ) "
                   "QMetaObject::invokeMethod( this, \"drawFrameReady\", Qt::QueuedConnection ); return; } "
                   "if( !m_playbackPaceGuard.grantWholeFrame(" ),
               holdSpan.toStdString() );

    const QString code = stripComments( body );
    const int hold = code.indexOf( QStringLiteral( "if( !playbackEveryFrameTickMayAdvance(" ) );
    ASSERT_TRUE( hold > 0 );
    ASSERT_EQ( std::string(
                   "void MainWindow::playbackHandling(int timeDiff)"
                   " | if( ui->actionPlay->isChecked() )"
                   " | if( playback_frame_range::outerLastFrameBranchHandlesTick( ui->horizontalSliderPosition->value(), "
                   "ui->spinBoxCutOut->value(), ui->actionDropFrameMode->isChecked(), ui->actionLoop->isChecked(), "
                   "playbackConformActive() ) ) / else"
                   " | if( !ui->actionDropFrameMode->isChecked() )" ),
               enclosingBlockHeaders( code, hold ).join( QStringLiteral( " | " ) ).toStdString() );
}

// r2 (adversary A4 sibling on 06031667): the reset proximity check (< 200 characters after the Play-start line) accepted the reset
// behind a dead guard. The reset is the statement right after the Play-start line, directly in resetPlaybackDueWakeCounters.
TEST( EveryFrameTargetGate, TheAcceptedFrameResetIsLiveAtPlayStart )
{
    const QString code = stripComments( functionBody( mainWindowSource(),
                                                      QStringLiteral( "void MainWindow::resetPlaybackDueWakeCounters( void )" ),
                                                      QStringLiteral( "\n}\n" ) ) );
    ASSERT_FALSE( code.isEmpty() );
    const QString presented = QStringLiteral( "m_playbackDueWakePresentedFrame = -1;" );
    const QString reset = QStringLiteral( "m_playbackEveryFrameAcceptedFrame = -1;" );
    ASSERT_EQ( 1, countOccurrences( code, reset ) );
    const int resetAt = code.indexOf( reset );
    const int presentedAt = code.lastIndexOf( presented, resetAt );
    ASSERT_TRUE( presentedAt > 0 );
    ASSERT_TRUE( code.mid( presentedAt + presented.length(), resetAt - presentedAt - presented.length() ).trimmed().isEmpty() );
    ASSERT_EQ( std::string( "void MainWindow::resetPlaybackDueWakeCounters( void )" ),
               enclosingBlockHeaders( code, resetAt ).join( QStringLiteral( " | " ) ).toStdString() );
}

// r2 (adversary A4 siblings on 06031667): the substring checks of the mode-neutral check and of the conform delegate accepted a
// dead guard on the Ready check, and a dead delegate followed by `return false;`. Both bodies are pinned exactly, comments removed
// (this also covers the same substring checks in test_playback_conform_wiring.cpp).
TEST( EveryFrameTargetGate, TheModeNeutralChecksAndTheConformDelegateAreLive )
{
    const QString source = mainWindowSource();
    const QString neutralSignature = QStringLiteral( "bool MainWindow::playbackTargetPresentOutstanding( int target )" );
    // r4 (merge of fork/master after #905): PLAYBACK-STALE-POSITION-FOLD-CONTRACT-1 r2 gave the conform checks the tick's
    // corrected start (\a position) in place of m_newPosDropMode; the delegate to the mode-neutral check is unchanged.
    const QString conformSignature = QStringLiteral( "bool MainWindow::conformTargetPresentOutstanding( double position )" );
    const QString neutral = stripComments( functionBody( source, neutralSignature, conformSignature ) ).simplified();
    ASSERT_EQ( std::string(
                   "bool MainWindow::playbackTargetPresentOutstanding( int target ) { "
                   "if( !m_pRenderThread || target < 0 ) return false; "
                   "if( renderThreadBusyForPlaybackAdvance( target ) ) return true; "
                   "const uint64_t generation = m_playbackPresentationGeneration.load( std::memory_order_acquire ); "
                   "if( m_pRenderThread->hasReadyFrameForPlaybackTarget( static_cast<uint32_t>( target ), generation ) ) return true; "
                   "return m_pRenderThread->hasPlaybackLookaheadRequest( static_cast<uint32_t>( target ), generation ); }" ),
               neutral.toStdString() );
    const QString conform = stripComments( functionBody( source, conformSignature,
                                                         QStringLiteral( "bool MainWindow::conformAcceptedTargetPresentPending( double position )" ) ) )
                                .simplified();
    ASSERT_EQ( std::string(
                   "bool MainWindow::conformTargetPresentOutstanding( double position ) { "
                   "if( !m_pRenderThread || !playbackConformActive() ) return false; "
                   "const int target = static_cast<int>( position ); "
                   "if( target < 0 || m_lastPresentedPlaybackFrame == static_cast<long long>( target ) ) return false; "
                   "return playbackTargetPresentOutstanding( target ); }" ),
               conform.toStdString() );
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
    // r6 (hosted run 46063672035, job 140723964877): a body runs up to the NEXT signature, so it carries that function's leading
    // comment block. Since the r4 merge of fork/master, the comment above conformTargetPresentOutstanding names m_newPosDropMode
    // (PLAYBACK-STALE-POSITION-FOLD-CONTRACT-1 r2), which failed the negative check below on a comment, not on code. Both bodies
    // are read with comments removed, as TheModeNeutralChecksAndTheConformDelegateAreLive does; every assertion is unchanged.
    const QString conform = stripComments( functionBody( source,
                                          QStringLiteral( "bool MainWindow::conformTargetPresentOutstanding( double position )" ),
                                          QStringLiteral( "bool MainWindow::conformAcceptedTargetPresentPending( double position )" ) ) );
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

    const QString neutral = stripComments( functionBody( source,
                                          QStringLiteral( "bool MainWindow::playbackTargetPresentOutstanding( int target )" ),
                                          QStringLiteral( "bool MainWindow::conformTargetPresentOutstanding( double position )" ) ) );
    ASSERT_FALSE( neutral.isEmpty() );
    // fix 1 (adversary A4 sibling): each check with its polarity -- each one found means a present is coming
    ASSERT_TRUE( neutral.contains( QStringLiteral( "if( renderThreadBusyForPlaybackAdvance( target ) ) return true;" ) ) );
    ASSERT_TRUE( neutral.contains( QStringLiteral(
        "if( m_pRenderThread->hasReadyFrameForPlaybackTarget( static_cast<uint32_t>( target ), generation ) ) return true;" ) ) );
    ASSERT_TRUE( neutral.contains( QStringLiteral(
        "return m_pRenderThread->hasPlaybackLookaheadRequest( static_cast<uint32_t>( target ), generation );" ) ) );
    ASSERT_TRUE( neutral.contains( QStringLiteral( "if( !m_pRenderThread || target < 0 ) return false;" ) ) );
    ASSERT_FALSE( neutral.contains( QStringLiteral( "playbackConformActive" ) ) );
    ASSERT_FALSE( neutral.contains( QStringLiteral( "m_newPosDropMode" ) ) );

    const QString header = readRepoFile( QStringLiteral( "platform/qt/MainWindow.h" ) );
    ASSERT_EQ( 1, countOccurrences( header, QStringLiteral( "bool playbackTargetPresentOutstanding( int target );" ) ) );
}

// ---- (4) self-tests of the source-text helpers (r7, Grok CoS G6b/G6c/G7) --------------------------------------------------
// The wiring pins above read MainWindow.cpp through functionBody + stripComments. These fixtures prove the pipeline on the cases a
// pin relies on: a comment cannot fail a negative check, real code always fails it, and text the compiler reads as a comment (a
// spliced // comment) is never counted as code, while code after a literal that holds "//" is never dropped.

// G6b REGRESSION: a comment directly above the next signature names the forbidden token (the 5295d637 hosted red, in miniature).
TEST( EveryFrameTargetGate, StripCommentsNegativeCheckPassesOnACommentAboveTheNextSignature )
{
    const QString fixture = QStringLiteral(
        "bool A::neutral( int target )\n"
        "{\n"
        "    if( target < 0 ) return false;\n"
        "    return check( target );\n"
        "}\n"
        "\n"
        "// the conform check below takes the tick's start, not m_newPosDropMode\n"
        "/* nor m_newPosDropMode in a block comment */\n"
        "bool A::conform( double position )\n"
        "{\n"
        "    return neutral( static_cast<int>( position ) );\n"
        "}\n" );
    const QString raw = functionBody( fixture, QStringLiteral( "bool A::neutral( int target )" ),
                                      QStringLiteral( "bool A::conform( double position )" ) );
    ASSERT_FALSE( raw.isEmpty() );
    ASSERT_TRUE( raw.contains( QStringLiteral( "m_newPosDropMode" ) ) );   // the fixture carries the trap the raw read fell into
    const QString neutral = stripComments( raw );
    ASSERT_FALSE( neutral.contains( QStringLiteral( "m_newPosDropMode" ) ) );
    ASSERT_TRUE( neutral.contains( QStringLiteral( "return check( target );" ) ) );
    ASSERT_TRUE( neutral.contains( QStringLiteral( "if( target < 0 ) return false;" ) ) );
}

// G6c ADVERSARIAL: the forbidden token in real code inside the body is still found after the strip, wherever it sits on its line.
TEST( EveryFrameTargetGate, StripCommentsNegativeCheckFailsOnTheForbiddenTokenInRealCode )
{
    const QStringList bodies = {
        QStringLiteral( "    if( m_newPosDropMode < 0 ) return false;\n" ),
        QStringLiteral( "    /* a block comment first */ const double p = m_newPosDropMode;\n" ),
        QStringLiteral( "    log( \"a // b\" ); use( m_newPosDropMode );\n" ),
        QStringLiteral( "    log( \"q\\\" // still the string\" ); use( m_newPosDropMode );\n" ),
        QStringLiteral( "    const char c = '\"'; use( m_newPosDropMode );\n" ),
        QStringLiteral( "    const char c = '\\''; use( m_newPosDropMode );\n" ),
        QStringLiteral( "    const int n = 1'000'000; use( m_newPosDropMode );\n" ),
        QStringLiteral( "    const char * r = R\"x(a \" // )\" b)x\"; use( m_newPosDropMode );\n" ),
        QStringLiteral( "    // a comment line\n    use( m_newPosDropMode );\n" ),
        QStringLiteral( "    /* a block\n       comment */\n    use( m_newPosDropMode );\n" ),
    };
    for( const QString & body : bodies )
    {
        const QString fixture = QStringLiteral( "bool A::neutral( int target )\n{\n" ) + body
                                + QStringLiteral( "    return true;\n}\n\nbool A::conform( double position )\n{\n}\n" );
        const QString neutral = stripComments( functionBody( fixture, QStringLiteral( "bool A::neutral( int target )" ),
                                                             QStringLiteral( "bool A::conform( double position )" ) ) );
        ASSERT_FALSE( neutral.isEmpty() );
        ASSERT_TRUE( neutral.contains( QStringLiteral( "m_newPosDropMode" ) ) );
        ASSERT_TRUE( neutral.contains( QStringLiteral( "return true;" ) ) );
    }
}

// G7 SPLICE: a backslash right before a newline continues a // comment (translation phase 2), so the next line is comment, not
// code. The old stripper ended the comment at the first newline and kept `if( stop ) return;` as live code.
TEST( EveryFrameTargetGate, StripCommentsTreatsASplicedLineCommentAsComment )
{
    const QStringList spliced = {
        QStringLiteral( "void f()\n{\n    // disabled early-out \\\n    if( stop ) return;\n    run();\n}\n" ),
        QStringLiteral( "void f()\n{\n    // trailing blanks after the backslash \\  \t\n    if( stop ) return;\n    run();\n}\n" ),
        QStringLiteral( "void f()\n{\n    // CR LF line end \\\r\n    if( stop ) return;\n    run();\n}\n" ),
        QStringLiteral( "void f()\n{\n    // an escaped-looking backslash pair still splices \\\\\n    if( stop ) return;\n    run();\n}\n" ),
        QStringLiteral( "void f()\n{\n    // two splices \\\n    one(); \\\n    if( stop ) return;\n    run();\n}\n" ),
        QStringLiteral( "void f()\n{\n    /\\\n/ the opener is spliced\n    /\\\n/ if( stop ) return;\n    run();\n}\n" ),
        QStringLiteral( "void f()\n{\n    /* the closer *\\\n is not here, so if( stop ) return; is comment */\n    run();\n}\n" ),
    };
    for( const QString & fixture : spliced )
    {
        const QString code = stripComments( fixture );
        ASSERT_FALSE( code.contains( QStringLiteral( "if( stop ) return;" ) ) );
        ASSERT_FALSE( code.contains( QStringLiteral( "one();" ) ) );
        ASSERT_TRUE( code.contains( QStringLiteral( "run();" ) ) );
        ASSERT_TRUE( code.contains( QStringLiteral( "void f()" ) ) );
    }
    // a spliced block-comment closer does close the comment, so the code after it is live
    const QString closed = stripComments( QStringLiteral( "/* comment *\\\n/ live();\n" ) );
    ASSERT_TRUE( closed.contains( QStringLiteral( "live();" ) ) );
    // a splice in code (a macro continuation) is copied unchanged, so the exact pins compare source text
    const QString macro = QStringLiteral( "#define M( x ) \\\n    ( x )\nlive();\n" );
    ASSERT_EQ( macro.toStdString(), stripComments( macro ).toStdString() );
}

// G7 LITERALS: "//" or "/*" inside a string, char or raw string literal is literal text; the code after it on the same line
// survives the strip and a real comment after it is still removed.
TEST( EveryFrameTargetGate, StripCommentsKeepsCodeAfterALiteralThatHoldsACommentOpener )
{
    struct Case
    {
        QString input;
        QString expected;
    };
    const std::vector<Case> cases = {
        { QStringLiteral( "log( \"a // b\" ); forbidden_code(); // gone\n" ),
          QStringLiteral( "log( \"a // b\" ); forbidden_code(); \n" ) },
        { QStringLiteral( "log( \"a /* b\" ); forbidden_code(); /* gone */\n" ),
          QStringLiteral( "log( \"a /* b\" ); forbidden_code();  \n" ) },
        { QStringLiteral( "log( \"q\\\" // still the string\" ); forbidden_code(); // gone\n" ),
          QStringLiteral( "log( \"q\\\" // still the string\" ); forbidden_code(); \n" ) },
        { QStringLiteral( "log( \"\\\\\" ); forbidden_code(); // gone\n" ),
          QStringLiteral( "log( \"\\\\\" ); forbidden_code(); \n" ) },
        { QStringLiteral( "c = '\"'; forbidden_code(); // gone\n" ),
          QStringLiteral( "c = '\"'; forbidden_code(); \n" ) },
        { QStringLiteral( "c = '\\''; forbidden_code(); // gone\n" ),
          QStringLiteral( "c = '\\''; forbidden_code(); \n" ) },
        { QStringLiteral( "s = u8\"// x\"; w = L'/'; forbidden_code(); // gone\n" ),
          QStringLiteral( "s = u8\"// x\"; w = L'/'; forbidden_code(); \n" ) },
        { QStringLiteral( "r = R\"x(a \" // )\" b)x\"; forbidden_code(); // gone\n" ),
          QStringLiteral( "r = R\"x(a \" // )\" b)x\"; forbidden_code(); \n" ) },
        { QStringLiteral( "r = u8R\"(/* \\\n */)\"; forbidden_code(); // gone\n" ),
          QStringLiteral( "r = u8R\"(/* \\\n */)\"; forbidden_code(); \n" ) },
        { QStringLiteral( "n = 1'000'000; forbidden_code(); // gone '\n" ),
          QStringLiteral( "n = 1'000'000; forbidden_code(); \n" ) },
        { QStringLiteral( "x = 0x1'F; forbidden_code(); // gone\n" ),
          QStringLiteral( "x = 0x1'F; forbidden_code(); \n" ) },
        { QStringLiteral( "a = b / c; forbidden_code(); // gone\n" ),
          QStringLiteral( "a = b / c; forbidden_code(); \n" ) },
        // adversary A1 on db994303: a splice between a literal's prefix and its quote (or inside a pp-number) is read after splicing
        { QStringLiteral( "log(R\\\n\"x(a \" // b)x\"); forbidden_code(); // gone\n" ),
          QStringLiteral( "log(R\\\n\"x(a \" // b)x\"); forbidden_code(); \n" ) },
        { QStringLiteral( "log(u8\\\nR\\\r\n\"(a \" // b)\"); forbidden_code(); // gone\n" ),
          QStringLiteral( "log(u8\\\nR\\\r\n\"(a \" // b)\"); forbidden_code(); \n" ) },
        { QStringLiteral( "n = 1\\  \n'000; forbidden_code(); // gone '\n" ),
          QStringLiteral( "n = 1\\  \n'000; forbidden_code(); \n" ) },
        // a blank before the splice ends the token: `R "x(` is the identifier R and an ordinary string, so `// b)x"` is in it
        { QStringLiteral( "log(R \\\n\"x(a // b)x\"); forbidden_code(); // gone\n" ),
          QStringLiteral( "log(R \\\n\"x(a // b)x\"); forbidden_code(); \n" ) },
    };
    for( const Case & item : cases )
    {
        const QString code = stripComments( item.input );
        ASSERT_EQ( item.expected.toStdString(), code.toStdString() );
        ASSERT_TRUE( code.contains( QStringLiteral( "forbidden_code();" ) ) );
        ASSERT_FALSE( code.contains( QStringLiteral( "gone" ) ) );
    }
    // text without comments is returned unchanged
    const QString plain = QStringLiteral( "if( a ) { b(); }\nreturn c;\n" );
    ASSERT_EQ( plain.toStdString(), stripComments( plain ).toStdString() );
}
