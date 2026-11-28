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
    // r8 (Grok CoS G9): the "one per publish" claim is asserted inside the model. Every publish goes through publish(), which posts
    // one notification when notifyModel is set; the held tick's kick posts its own; and checkNotificationLedger() runs after every
    // step, so a publish that posts none (or two), a kick that is not counted, or a callback that is lost fails at the step itself.
    int publishes = 0;
    int publishNotifications = 0;
    int kickNotifications = 0;
    int consumedNotifications = 0;
    int acceptsOnNotification = 0;   // a consumed notification that accepted the target's slot
    int emptyCallbacks = 0;          // a consumed notification whose target had no Ready slot: it takes nothing, never another frame

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
            if( kickOnHold && ready.count( target ) )
            {
                ++notifications;
                ++kickNotifications;
            }
            checkNotificationLedger();
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

    // the notification ledger (r8, Grok CoS G9): with notifyModel, every publish posted exactly one notification, every pending
    // notification came from a publish or a kick and was not yet consumed, and every consumed one either accepted the target's slot
    // or took nothing
    void checkNotificationLedger() const
    {
        if( !notifyModel ) return;
        ASSERT_EQ( publishes, publishNotifications );
        ASSERT_EQ( publishNotifications + kickNotifications - consumedNotifications, notifications );
        ASSERT_EQ( consumedNotifications, acceptsOnNotification + emptyCallbacks );
        ASSERT_TRUE( notifications >= 0 );
    }

    // a render completes and publishes its slot Ready; the frameReady signal fires once for it
    void publish( int frame )
    {
        ready.insert( frame );
        ++publishes;
        if( notifyModel )
        {
            ++notifications;
            ++publishNotifications;
        }
        checkNotificationLedger();
    }

    void renderCompleteAll()
    {
        const std::set<int> completed = requested;
        requested.clear();
        for( int frame : completed ) publish( frame );
    }

    void renderCompleteOne()
    {
        if( requested.empty() ) return;
        const int frame = *requested.begin();
        requested.erase( requested.begin() );
        publish( frame );
    }

    bool frameReady()
    {
        if( notifyModel )
        {
            if( notifications == 0 ) return false;
            --notifications;
            ++consumedNotifications;
        }
        // prune: a slot below the target is released unpainted
        while( !ready.empty() && *ready.begin() < target ) ready.erase( ready.begin() );
        if( !ready.count( target ) )
        {
            if( notifyModel ) ++emptyCallbacks;
            checkNotificationLedger();
            return false;
        }
        ready.erase( target );
        presented.push_back( target );
        accepted = target;
        if( notifyModel ) ++acceptsOnNotification;
        tick();   // drawFrameReady's early advance runs right after the accept
        checkNotificationLedger();
        return true;
    }

    // the event loop drains every queued frameReady notification (the publish callbacks and the held tick's kick)
    void drainNotifications()
    {
        while( notifyModel && notifications > 0 ) frameReady();
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
    if( needle.isEmpty() ) return 0;   // r7 (adversary A3 on cca976f5): an empty needle matched at 0 forever
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

QString stripComments( const QString & s, std::vector<char> * codeAt );

// r8 (adversary pass 10 A2 on fbf06187): the signature and the next signature count only where they start in code. A
// `// bool A::conform( double position )` line inside the body (or the text in a literal) used to end the body early, so a negative
// check after it read a truncated body and passed on a live reference below the comment.
int indexOfInCode( const QString & source, const QString & needle, int from, const std::vector<char> & codeAt )
{
    int at = source.indexOf( needle, from );
    while( at >= 0 && !codeAt[static_cast<size_t>( at )] ) at = source.indexOf( needle, at + 1 );
    return at;
}

QString functionBody( const QString & source, const QString & signature, const QString & nextSignature )
{
    std::vector<char> codeAt;
    stripComments( source, &codeAt );
    const int at = indexOfInCode( source, signature, 0, codeAt );
    const int next = at < 0 ? -1 : indexOfInCode( source, nextSignature, at, codeAt );
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
//   - digit separators, which are not char literals: a `'` inside a pp-number followed by a digit or nondigit, however many the
//     number has (`1'000'000`, `0xA'B'C`, `0b1'0`, `1e+1'0`; r8, Grok CoS G11 / adversary pass 8 A1), while `1'/'` is the
//     pp-number 1 and the char literal '/'.
// Splices in code outside comments are copied unchanged, so the code text the exact pins compare is the source text.
// Not handled: header-names (`#include <a//b.h>`), trigraphs (removed in C++17), a universal-character-name (`é`) inside an
// identifier or pp-number in front of a quote (the token stops at its backslash), a bare CR (a CR not followed by LF) as a line
// end (stripComments' `//` scan ends only at `\n`, so `// note` + bare CR + `use( x );` + LF reads the use as comment; the
// splice helpers and literalEnd also see only LF and CR LF; adversary pass 11 A1), and a macro that expands to a comment or to
// code (the stripper does not preprocess). No pin depends on these: MainWindow.cpp has no bare CR, and no splice, raw string,
// digit separator, universal-character-name or header-name with `//` or `/*` (the EveryFrameTargetGate.StripComments* tests below
// prove each handled case on fixtures). Also not handled, in the model above: frame numbers near the top of the int range
// (EveryFrameModel's `target + 1` lookahead request and the `last - first + 1` frame count in
// assertEveryFramePresentedOnceInOrder overflow signed int when `last` is INT_MAX; adversary pass 11 A5). No test reaches it: every
// model range here is small and fixed. Also not handled, in functionBody above: an empty signature or next signature (an empty
// needle matches at every offset, including the end of the source, one past indexOfInCode's code mask, so
// `functionBody( QString(), QString(), QString() )` reads codeAt[0] of an empty mask; adversary pass 12 A3). Every caller passes a
// non-empty literal signature into a non-empty source.
// r9 re-check (Grok CoS G12), done after the last adversary pass (pass 12 on 03ff5c86), against the advisories still OPEN on
// this card: three are in these test helpers, all minor and introduced, all listed above as not handled and owned by
// EVERY-FRAME-TARGET-GATE-WIRING-SPLICE-HARDEN-1: the bare CR in stripComments (pass 11 A1 = pass 12 A1), the int range in
// EveryFrameModel (pass 11 A5 = pass 12 A2) and the empty signature in functionBody (pass 12 A3). Fixed here earlier: pass 8
// A1, pass 9 A1 (a char literal with a user-defined-literal suffix in front of a quote), pass 9 A2 (the `<%` / `%>` digraph
// braces in enclosingBlockHeaders), pass 10 A1 (a raw-string prefix after a char literal: the floor) and pass 10 A2 (a next
// signature inside a comment or literal in functionBody). The rest are product-path gaps in MainWindow.cpp /
// GpuDisplayWindow.cpp / RenderFrameThread.cpp, outside these helpers: pass 8 A2 / pass 9 A3 / pass 10 A3, pass 6 A3, pass 7 A5
// / pass 9 A4 / pass 10 A4, pass 11 A2 / A3 / A4, and pass 12 A4 / A5 (= pass 11 A2 / A3).
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
// r8 (Grok CoS G11, adversary pass 8 A1 on 257ad2a9): the token is found by lexing FORWARD over the longest run of characters that
// can belong to a pp-number ([lex.ppnumber]: identifier characters, '.', `'` followed by a digit or nondigit, and a sign after
// e/E/p/P) ending at \a i, read after splicing. A pp-number with several separators (`0xA'B'C`, `1'000'000`, `0b1'0`) or a
// signed exponent (`1e+1'0`) is therefore one token. The old backward walk stopped at the earlier separator and returned `B`, so
// the second `'` of `0xA'B'C` opened a char literal that ran to the newline and kept a // comment as code.
// r8 (adversary pass 9 A1 / pass 10 A1): the run never reads back past \a floor, the index just past the last literal the forward
// scanner (lexSpans, enclosingBlockHeaders) passed. In `'1'_e+'x'` and `'\0'+R"x(...)x"` the run would otherwise cross the char
// literal before it ('1' with its suffix _e, or '\0') and read its quote as a digit separator or its digits as a pp-number.
QString tokenBefore( const QString & s, int i, int floor = 0 )
{
    QString run;
    int k = skipLineSplicesBackward( s, i );
    while( k > floor )
    {
        const QChar c = s.at( k - 1 );
        if( !isIdentifierChar( c ) && c != QLatin1Char( '.' ) && c != QLatin1Char( '\'' ) && c != QLatin1Char( '+' )
            && c != QLatin1Char( '-' ) )
            break;
        run.prepend( c );
        k = std::max( floor, skipLineSplicesBackward( s, k - 1 ) );
    }
    // the run starts after a character no token here can contain, so a token starts there; lex it forward to its last token
    enum class Kind
    {
        Other,
        Identifier,
        PpNumber
    };
    Kind kind = Kind::Other;
    int start = 0;
    for( int j = 0; j < run.size(); ++j )
    {
        const QChar c = run.at( j );
        if( kind == Kind::PpNumber )
        {
            if( isIdentifierChar( c ) || c == QLatin1Char( '.' ) ) continue;
            if( c == QLatin1Char( '\'' ) && j + 1 < run.size() && isIdentifierChar( run.at( j + 1 ) ) ) continue;
            if( ( c == QLatin1Char( '+' ) || c == QLatin1Char( '-' ) ) && QStringLiteral( "eEpP" ).contains( run.at( j - 1 ) ) )
                continue;
        }
        else if( kind == Kind::Identifier && isIdentifierChar( c ) )
            continue;
        // (a `'` that does not continue a pp-number is punctuation here: the run never holds a literal's characters, because it
        // stops at \a floor, the end of the last literal the forward scanner passed -- r8, adversary pass 9 A1 and pass 10 A1)
        start = j;
        if( c.isDigit() || ( c == QLatin1Char( '.' ) && j + 1 < run.size() && run.at( j + 1 ).isDigit() ) ) kind = Kind::PpNumber;
        else if( isIdentifierChar( c ) ) kind = Kind::Identifier;
        else kind = Kind::Other;   // '.', `'`, '+' or '-' as punctuation (a `'` here opened a literal: what follows is not a prefix)
    }
    return kind == Kind::Other ? QString() : run.mid( start );
}

// When a string or char literal (any prefix) or a raw string literal opens at \a i (a '"' or '\'' in code), the index just past
// it; -1 when the '\'' is a digit separator (`1'000`), which is code. Shared by stripComments and enclosingBlockHeaders, so both
// read literals the way the compiler does (r7, adversary A1/A2 on cca976f5 and their sibling in enclosingBlockHeaders):
//   - a raw string `R"d(...)d"` runs to its own closer, with no splicing and no escapes inside; the delimiter d is up to 16
//     characters other than space, parentheses, backslash and control characters (so `"` is allowed: `R""(a)""`);
//   - in an ordinary literal, phase-2 splices come first: a backslash followed by a splice escapes the character after the splice
//     (`"a\\` + newline + `b"` is `"a\b"`), and a splice continues the literal; an unspliced newline ends an unterminated one.
// \a floor: the index just past the last literal the caller's forward scan passed (r8, adversary pass 10 A1; see tokenBefore).
int literalEnd( const QString & s, int i, int floor = 0 )
{
    const int n = s.size();
    const QChar quote = s.at( i );
    if( quote == QLatin1Char( '\'' ) )
    {
        // a separator continues a pp-number only as `' digit` or `' nondigit` (r8): `1'/'` is the pp-number 1 and the literal '/'
        const QString token = tokenBefore( s, i, floor );
        const int after = skipLineSplices( s, i + 1 );
        const bool digitSeparator = !token.isEmpty()
                                    && ( token.at( 0 ).isDigit()
                                         || ( token.at( 0 ) == QLatin1Char( '.' ) && token.size() > 1 && token.at( 1 ).isDigit() ) )
                                    && after < n && isIdentifierChar( s.at( after ) );
        if( digitSeparator ) return -1;
    }
    else
    {
        const QString prefix = tokenBefore( s, i, floor );
        const bool raw = prefix == QLatin1String( "R" ) || prefix == QLatin1String( "LR" ) || prefix == QLatin1String( "uR" )
                         || prefix == QLatin1String( "UR" ) || prefix == QLatin1String( "u8R" );
        if( raw )
        {
            const int open = s.indexOf( QLatin1Char( '(' ), i + 1 );
            const QString delimiter = open > i ? s.mid( i + 1, open - i - 1 ) : QString();
            bool delimiterValid = open > i && delimiter.size() <= 16;
            for( const QChar d : delimiter )
                if( d.isSpace() || d.category() == QChar::Other_Control || d == QLatin1Char( ')' ) || d == QLatin1Char( '\\' ) )
                    delimiterValid = false;
            if( delimiterValid )
            {
                const QString closer = QLatin1Char( ')' ) + delimiter + QLatin1Char( '"' );
                const int close = s.indexOf( closer, open + 1 );
                return close < 0 ? n : close + closer.length();
            }
        }
    }
    int k = i + 1;
    while( k < n )
    {
        const int afterSplices = skipLineSplices( s, k );
        if( afterSplices > k )
        {
            k = afterSplices;   // a splice continues the literal
            continue;
        }
        const QChar d = s.at( k );
        if( d == quote ) return k + 1;
        if( d == QLatin1Char( '\n' ) ) return k;   // unterminated: the newline is code
        if( d == QLatin1Char( '\\' ) )
        {
            k = skipLineSplices( s, k + 1 );   // the escaped character is the one after any splices
            if( k < n ) ++k;
            continue;
        }
        ++k;
    }
    return n;
}

// \a codeAt (r8, adversary pass 10 A2): when given, set to one entry per character of \a s, 1 where the character is code and 0 where
// it is inside a comment or a string/char/raw literal, by the same lexing (functionBody uses it to skip a signature in a comment).
QString stripComments( const QString & s, std::vector<char> * codeAt = nullptr )
{
    QString out;
    out.reserve( s.size() );
    const int n = s.size();
    if( codeAt ) codeAt->assign( static_cast<size_t>( n ), 1 );
    const auto notCode = [codeAt]( int from, int to ) {
        if( codeAt )
            for( int p = from; p < to; ++p ) ( *codeAt )[static_cast<size_t>( p )] = 0;
    };
    int floor = 0;   // just past the last literal (r8, adversary pass 10 A1)
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
        if( c == QLatin1Char( '"' ) || c == QLatin1Char( '\'' ) )
        {
            const int end = literalEnd( s, i, floor );   // -1: a digit separator, which is code
            if( end >= 0 )
            {
                out.append( s.mid( i, end - i ) );   // the literal, verbatim (its splices included)
                notCode( i, end );
                i = end;
                floor = end;
                continue;
            }
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
                notCode( i, k );
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
                notCode( i, end );
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
    int floor = 0;   // just past the last literal (r8, adversary pass 10 A1 sibling)
    for( int i = 0; i < pos && i < code.size(); ++i )
    {
        const QChar c = code.at( i );
        // r7 sibling of adversary A1/A2 on cca976f5: literals are skipped with literalEnd (raw strings, digit separators, splices),
        // so a brace inside `R"x({")x"` or after `1'000` is read as the compiler reads it.
        if( c == QLatin1Char( '"' ) || c == QLatin1Char( '\'' ) )
        {
            const int end = literalEnd( code, i, floor );
            if( end >= 0 )
            {
                floor = end;
                i = end - 1;   // the loop's ++i lands just past the literal
                continue;
            }
        }
        // r8 (adversary pass 9 A2 on db196611): the digraphs `<%` and `%>` ([lex.digraph], read after splicing) are `{` and `}`,
        // so `if( false ) <% ... %>` around a pinned statement is on the path of blocks too. Maximal munch always reads `<%` and `%>`
        // as digraphs (the `<::` exception is for `<:` only).
        int digraphEnd = -1;
        if( ( c == QLatin1Char( '<' ) || c == QLatin1Char( '%' ) ) )
        {
            const int second = skipLineSplices( code, i + 1 );
            if( second < code.size()
                && ( ( c == QLatin1Char( '<' ) && code.at( second ) == QLatin1Char( '%' ) )
                     || ( c == QLatin1Char( '%' ) && code.at( second ) == QLatin1Char( '>' ) ) ) )
                digraphEnd = second;
        }
        const bool opens = c == QLatin1Char( '{' ) || ( digraphEnd >= 0 && c == QLatin1Char( '<' ) );
        const bool closes = c == QLatin1Char( '}' ) || ( digraphEnd >= 0 && c == QLatin1Char( '%' ) );
        if( opens )
        {
            const QString header = code.mid( lastSeparator + 1, i - lastSeparator - 1 ).simplified();
            QStringList full = chain.back();
            full << header;
            open.push_back( { header, header.startsWith( elseWord ) ? full.join( link ) : header } );
            chain.emplace_back();
            if( digraphEnd >= 0 ) i = digraphEnd;   // the loop's ++i lands just past the digraph
            lastSeparator = i;
        }
        else if( closes )
        {
            if( digraphEnd >= 0 ) i = digraphEnd;
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

// r8 (Grok CoS G9): the notify model through NORMAL publishes, each posting its own frameReady notification, with the poll tick's
// hold in between. The lookahead slot N+1 is published (one notification) while the slider is on N; the held tick then kicks for a
// target that is already Ready, so two notifications are pending for one target. The first takes the target; the surplus one finds
// the next target not yet Ready and takes nothing. Every frame is presented exactly once, in order, each accept consumed exactly
// one notification, and the model's ledger (one notification per publish) holds after every step.
TEST( EveryFrameTargetGate, NotifyModelPublishCallbacksAndTheHeldKickTakeEveryFrameOnceInOrder )
{
    EveryFrameModel model( 1, 480 );
    model.notifyModel = true;
    model.kickOnHold = true;
    int doubleNotifiedHolds = 0;   // a hold whose kick joined a publish notification still pending for the same Ready target
    for( int step = 0; step < 480 * 10 && !model.finished(); ++step )
    {
        model.renderCompleteAll();   // publishes the target (or the lookahead slot N+1): one notification each
        if( model.accepted >= 0 )    // before this Play's first accept the predicate is open by design (startup unchanged)
        {
            const int pendingBefore = model.notifications;
            const int kicksBefore = model.kickNotifications;
            model.pollTick();
            if( model.kickNotifications > kicksBefore && pendingBefore > 0 ) ++doubleNotifiedHolds;
        }
        model.drainNotifications();
    }
    ASSERT_TRUE( model.finished() );
    assertEveryFramePresentedOnceInOrder( model );
    model.checkNotificationLedger();
    ASSERT_EQ( static_cast<int>( model.presented.size() ), model.acceptsOnNotification );   // no double take, no take without one
    ASSERT_EQ( model.publishes, model.publishNotifications );
    ASSERT_TRUE( model.publishes >= static_cast<int>( model.presented.size() ) );
    ASSERT_TRUE( model.heldTicks > 0 );            // the hold was exercised
    ASSERT_TRUE( model.kickNotifications > 0 );    // and so was its kick
    ASSERT_TRUE( doubleNotifiedHolds > 0 );        // a publish callback and the kick were pending for the same target
    ASSERT_TRUE( model.emptyCallbacks > 0 );       // the surplus callback took nothing (not the next frame, not a second copy)
    ASSERT_EQ( 0, model.notifications );
}

// r8 (Grok CoS G9): the same notify model under a seeded random mix of publishes (one or all), single frameReady callbacks and poll
// ticks; the ledger is checked inside the model at every step.
TEST( EveryFrameTargetGate, NotifyModelSeededRandomEventMixTakesEveryFrameOnceInOrder )
{
    for( uint32_t seed = 1; seed <= 32; ++seed )
    {
        std::mt19937 rng( seed );
        EveryFrameModel model( 1, 480 );
        model.notifyModel = true;
        model.kickOnHold = true;
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
                if( model.accepted >= 0 ) model.pollTick();
                break;
            }
        }
        ASSERT_TRUE( model.finished() );
        assertEveryFramePresentedOnceInOrder( model );
        model.checkNotificationLedger();
        ASSERT_EQ( static_cast<int>( model.presented.size() ), model.acceptsOnNotification );
        ASSERT_EQ( model.publishes, model.publishNotifications );
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
        // adversary A1 on cca976f5: '"' is a valid raw-string delimiter character
        { QStringLiteral( "log(R\"\"(a // b)\"\"); forbidden_code(); // gone\n" ),
          QStringLiteral( "log(R\"\"(a // b)\"\"); forbidden_code(); \n" ) },
        // adversary A2 on cca976f5: `"a\\` + newline + `b // text"` is `"a\b // text"` after splicing (the second backslash splices)
        { QStringLiteral( "log(\"a\\\\\nb // text\"); forbidden_code(); // gone\n" ),
          QStringLiteral( "log(\"a\\\\\nb // text\"); forbidden_code(); \n" ) },
        { QStringLiteral( "c = '\\\\\n''; forbidden_code(); // gone\n" ),
          QStringLiteral( "c = '\\\\\n''; forbidden_code(); \n" ) },
        { QStringLiteral( "log(\"a \\\n// still the string\"); forbidden_code(); // gone\n" ),
          QStringLiteral( "log(\"a \\\n// still the string\"); forbidden_code(); \n" ) },
        // r8 (Grok CoS G11, adversary pass 8 A1 on 257ad2a9): a pp-number with several separators is one token, so its second `'` is
        // not a char opener and the comment after it is still removed
        { QStringLiteral( "n = 0xA'B'C; forbidden_code(); // gone m_newPosDropMode\n" ),
          QStringLiteral( "n = 0xA'B'C; forbidden_code(); \n" ) },
        { QStringLiteral( "n = 0b1'0'1; forbidden_code(); // gone '\n" ), QStringLiteral( "n = 0b1'0'1; forbidden_code(); \n" ) },
        { QStringLiteral( "d = 1e+1'0; e = .5'0; forbidden_code(); // gone\n" ),
          QStringLiteral( "d = 1e+1'0; e = .5'0; forbidden_code(); \n" ) },
        { QStringLiteral( "n = 0xA'\\\nB'C; forbidden_code(); // gone\n" ), QStringLiteral( "n = 0xA'\\\nB'C; forbidden_code(); \n" ) },
        // `' digit` / `' nondigit` only: `1'/'` is the pp-number 1 and the char literal '/', whose `/` opens no comment
        { QStringLiteral( "t = 1'/'; forbidden_code(); // gone\n" ), QStringLiteral( "t = 1'/'; forbidden_code(); \n" ) },
        // a sign joins a pp-number only after its e/E/p/P: `e-'0'` is the identifier e, '-', and the literal '0'
        { QStringLiteral( "v = e-'/'; forbidden_code(); // gone\n" ), QStringLiteral( "v = e-'/'; forbidden_code(); \n" ) },
        // adversary pass 9 A1 on db196611: `'1'_e` is a char literal with a user-defined-literal suffix, not the digits of a
        // pp-number, so the `'` after `+` opens the literal '/'
        { QStringLiteral( "auto n = '1'_e+'/'; forbidden_code(); // gone\n" ),
          QStringLiteral( "auto n = '1'_e+'/'; forbidden_code(); \n" ) },
        // adversary pass 10 A1 on fbf06187: the run stops at the end of '\0', so R is the raw-string prefix
        { QStringLiteral( "auto p = '\\0'+R\"x(a \" // b)x\"; forbidden_code(); // gone\n" ),
          QStringLiteral( "auto p = '\\0'+R\"x(a \" // b)x\"; forbidden_code(); \n" ) },
        { QStringLiteral( "auto p = '0'+1'0; q = '\\''+0xA'B; forbidden_code(); // gone '\n" ),
          QStringLiteral( "auto p = '0'+1'0; q = '\\''+0xA'B; forbidden_code(); \n" ) },
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

// r7 sibling sweep (adversary A1/A2/A3 on cca976f5): enclosingBlockHeaders skips literals with the same literalEnd, so a brace in
// a raw string, after a digit separator or in a spliced literal does not change the path of blocks; countOccurrences of an empty
// needle is 0 (it used to loop forever).
TEST( EveryFrameTargetGate, StripCommentsSiblingHelpersLexLiteralsLikeTheCompiler )
{
    struct Case
    {
        QString code;
        std::string headers;
    };
    const std::vector<Case> cases = {
        { QStringLiteral( "void f() { const char * r = R\"x({\")x\"; if( a ) { target(); } }" ), "void f() | if( a )" },
        { QStringLiteral( "void f() { const char * r = R\"\"(})\"\"; if( a ) { target(); } }" ), "void f() | if( a )" },
        { QStringLiteral( "void f() { const int n = 1'000; if( a ) { target(); } }" ), "void f() | if( a )" },
        { QStringLiteral( "void f() { log( \"a\\\\\n}\" ); if( a ) { target(); } }" ), "void f() | if( a )" },
        { QStringLiteral( "void f() { const char c = '{'; if( a ) { target(); } }" ), "void f() | if( a )" },
        { QStringLiteral( "void f() { log( \"q\\\"}\" ); if( a ) { target(); } }" ), "void f() | if( a )" },
        // r8 sibling of G11 (adversary pass 8 A1 sibling :435): enclosingBlockHeaders reads `0xA'B'C` as one pp-number too
        { QStringLiteral( "void f() { const int n = 0xA'B'C; if( a ) { target(); } }\n" ), "void f() | if( a )" },
        { QStringLiteral( "void f() { const char c = 1'{'; if( a ) { target(); } }" ), "void f() | if( a )" },
        // adversary pass 9 A1 on db196611: a char literal with a user-defined-literal suffix in front of another char literal
        { QStringLiteral( "void f() { const auto n = '1'_e+'{'; if( a ) { target(); } }\n" ), "void f() | if( a )" },
        // adversary pass 9 A2 on db196611: the digraphs <% and %> are braces (also when spliced), so a dead guard written with them
        // is on the path, and a closed digraph block leaves it
        { QStringLiteral( "void f() { x = 1; if( false ) <% if( a ) { target(); } %> }" ), "void f() | if( false ) | if( a )" },
        { QStringLiteral( "void f() <% if( a ) <% target(); %> %>" ), "void f() | if( a )" },
        { QStringLiteral( "void f() { if( false ) <\\\n% if( a ) { target(); } %\\\n> }" ), "void f() | if( false ) | if( a )" },
        { QStringLiteral( "void f() { if( false ) <% x(); %> if( a ) { target(); } }" ), "void f() | if( a )" },
        // adversary pass 10 A1 sibling on fbf06187
        { QStringLiteral( "void f() { auto p = '\\0'+R\"x(\"})x\"; if( a ) { target(); } }" ), "void f() | if( a )" },
    };
    for( const Case & item : cases )
    {
        const int at = item.code.indexOf( QStringLiteral( "target();" ) );
        ASSERT_TRUE( at > 0 );
        ASSERT_EQ( item.headers, enclosingBlockHeaders( item.code, at ).join( QStringLiteral( " | " ) ).toStdString() );
    }
    ASSERT_EQ( 0, countOccurrences( QStringLiteral( "x" ), QString() ) );
    ASSERT_EQ( 2, countOccurrences( QStringLiteral( "a1'0a" ), QStringLiteral( "a" ) ) );
}

// r8 (adversary pass 10 A2 on fbf06187): functionBody takes the signature and the next signature only where they are code, so a
// comment or a literal naming the next signature inside the body does not truncate it before a live reference.
TEST( EveryFrameTargetGate, StripCommentsFunctionBodySkipsASignatureInACommentOrLiteral )
{
    const QStringList decoys = {
        QStringLiteral( "    // bool A::conform( double position )\n" ),
        QStringLiteral( "    /* bool A::conform( double position ) */\n" ),
        QStringLiteral( "    log( \"bool A::conform( double position )\" );\n" ),
        QStringLiteral( "    // a spliced comment \\\nbool A::conform( double position )\n" ),
    };
    for( const QString & decoy : decoys )
    {
        const QString fixture = QStringLiteral( "// bool A::neutral( int target ) named in a comment first\n"
                                                "bool A::neutral( int target )\n{\n" )
                                + decoy
                                + QStringLiteral( "    use( m_newPosDropMode );\n    return true;\n}\n"
                                                  "bool A::conform( double position ) {}\n" );
        const QString body = functionBody( fixture, QStringLiteral( "bool A::neutral( int target )" ),
                                           QStringLiteral( "bool A::conform( double position )" ) );
        ASSERT_TRUE( body.startsWith( QStringLiteral( "bool A::neutral( int target )\n{" ) ) );
        ASSERT_TRUE( stripComments( body ).contains( QStringLiteral( "use( m_newPosDropMode );" ) ) );
        ASSERT_TRUE( body.contains( QStringLiteral( "return true;\n}\n" ) ) );
    }
}

// r8 (Grok CoS G11; FIX-ROUND-CLASS-SWEEP-1 item 4, a lexer defect gets a generated-input test against an exact oracle): seeded
// random pp-numbers with any number of digit separators (decimal, hex, binary, octal, a leading '.', signed exponents, suffixes,
// optional line splices between their characters), and seeded char literals after an identifier or encoding prefix. The oracle is
// the construction itself: the code before the comment is known exactly, so stripComments must return it unchanged with the
// comment removed, and enclosingBlockHeaders must see exactly the two blocks the fixture opens.
TEST( EveryFrameTargetGate, StripCommentsGeneratedPpNumbersAndCharLiteralsMatchTheConstruction )
{
    const QString decimalDigits = QStringLiteral( "0123456789" );
    const QString hexDigits = QStringLiteral( "0123456789abcdefABCDEF" );
    const QString splice = QStringLiteral( "\\\n" );
    for( uint32_t seed = 1; seed <= 400; ++seed )
    {
        std::mt19937 rng( seed );
        const auto pick = [&rng]( const QString & from ) { return from.at( static_cast<int>( rng() % static_cast<uint32_t>( from.size() ) ) ); };
        // a digit sequence of 1..4 groups joined by separators
        const auto groups = [&]( const QString & digits ) {
            QString text;
            const int count = 1 + static_cast<int>( rng() % 4u );
            for( int g = 0; g < count; ++g )
            {
                if( g > 0 ) text += QLatin1Char( '\'' );
                const int width = 1 + static_cast<int>( rng() % 3u );
                for( int w = 0; w < width; ++w ) text += pick( digits );
            }
            return text;
        };
        QString number;
        switch( rng() % 5u )
        {
        case 0: number = QStringLiteral( "0x" ) + groups( hexDigits ); break;
        case 1: number = QStringLiteral( "0b" ) + groups( QStringLiteral( "01" ) ); break;
        case 2: number = QStringLiteral( "0" ) + groups( QStringLiteral( "01234567" ) ); break;
        case 3:
            number = groups( decimalDigits ) + QLatin1Char( '.' ) + groups( decimalDigits ) + pick( QStringLiteral( "eE" ) )
                     + pick( QStringLiteral( "+-" ) ) + groups( decimalDigits );
            break;
        default: number = QLatin1Char( '.' ) + pick( decimalDigits ) + groups( decimalDigits ); break;
        }
        if( rng() % 2u ) number += pick( QStringLiteral( "uUlLfF" ) );
        // optional splices between characters of the number (never before its first character)
        QString spliced;
        for( int c = 0; c < number.size(); ++c )
        {
            if( c > 0 && rng() % 6u == 0 ) spliced += splice;
            spliced += number.at( c );
        }
        const QString prefix = QStringList{ QString(), QStringLiteral( "L" ), QStringLiteral( "u" ), QStringLiteral( "U" ),
                                            QStringLiteral( "u8" ) }.at( static_cast<int>( rng() % 5u ) );
        const QString literal = prefix + QLatin1Char( '\'' ) + pick( QStringLiteral( "/{}*'" ) ) + QLatin1Char( '\'' );
        QString fixedLiteral = literal.endsWith( QStringLiteral( "'''" ) ) ? prefix + QStringLiteral( "'\\''" ) : literal;
        // adversary pass 9 A1 on db196611: optionally a digit char literal with a user-defined-literal suffix in front of it
        if( rng() % 2u ) fixedLiteral = QLatin1Char( '\'' ) + pick( decimalDigits ) + QStringLiteral( "'_e+" ) + fixedLiteral;
        const QString code = QStringLiteral( "n = " ) + spliced + QStringLiteral( "; c = " ) + fixedLiteral
                             + QStringLiteral( "; forbidden_code(); " );
        const QString input = code + QStringLiteral( "// gone ' \" /*\n" );
        ASSERT_EQ( ( code + QStringLiteral( "\n" ) ).toStdString(), stripComments( input ).toStdString() );

        const QString blocks = QStringLiteral( "void f() { const auto n = " ) + spliced + QStringLiteral( "; const auto c = " )
                               + fixedLiteral + QStringLiteral( "; if( a ) { target(); } }\n" );
        const int at = blocks.indexOf( QStringLiteral( "target();" ) );
        ASSERT_TRUE( at > 0 );
        ASSERT_EQ( std::string( "void f() | if( a )" ),
                   enclosingBlockHeaders( blocks, at ).join( QStringLiteral( " | " ) ).toStdString() );
    }
}
