// EXPORT-PRORES-LOOK-ASSIST-1: wiring test. A rendered (ProRes / ffmpeg) batch export must run headless Look Assist the
// way the CDNG export does, and must then push the grade Look Assist wrote into the receipt into the processing object:
// Look Assist itself pushes only white balance and raw levels, and a rendered frame shows only processing-object state
// (CDNG carries only exposure, levels and white balance, as DNG metadata). BatchRunner.cpp needs
// the GUI build (not linked into console_tests), so this reads the source as text, with comments stripped, as
// test_export_receipt_debayer_wiring.cpp does. One TEST per check, so reverting one piece turns exactly that test red:
//   (1) exportRenderedVideoFile calls ReceiptApplier::applyHeadlessLookAssist( exactly once;
//   (2) after ReceiptApplier::applyToMlv( and before the debayer switch and getMlvProcessedFrame16(;
//   (3) with lookAssistAnalysisFrameIndex( cutIn, cutIn ) in its argument list (the clip-wide anchor CDNG analyses; a
//       constant anchor does not pass, adversary A8 on 76653777);
//   (4) applyLookAssistGradeToProcessing( follows it, before getMlvProcessedFrame16(;
//   (5) exportSingleFile (CDNG) is unchanged: one applyHeadlessLookAssist( with
//       lookAssistAnalysisFrameIndex( cutIn, effectiveCutIn ) and no applyLookAssistGradeToProcessing(.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QTextStream>

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    if (path.isEmpty()) return QString();
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) return QString();
    QTextStream stream(&file);
    return stream.readAll();
}

int countOccurrences(const QString & haystack, const QString & needle)
{
    int count = 0;
    int from = 0;
    while (true) {
        const int at = haystack.indexOf(needle, from);
        if (at < 0) break;
        ++count;
        from = at + needle.length();
    }
    return count;
}

// Backslash-continued lines are spliced first, as the preprocessor does, so a "// ... \" comment swallows its next line.
QString stripComments(QString text)
{
    text.remove(QRegularExpression(QStringLiteral("\\\\[ \\t]*\\n")));
    text.remove(QRegularExpression(QStringLiteral("/\\*.*?\\*/"), QRegularExpression::DotMatchesEverythingOption));
    text.remove(QRegularExpression(QStringLiteral("//[^\\n]*")));
    return text;
}

// From signature up to nextSignature, or to the end of the file when nextSignature is empty. Empty when not found.
QString functionBody(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    if (at < 0) return QString();
    if (nextSignature.isEmpty()) return source.mid(at);
    const int next = source.indexOf(nextSignature, at);
    if (next <= at) return QString();
    return source.mid(at, next - at);
}

QString batchRunnerSource()
{
    return stripComments(readRepoFile(QStringLiteral("src/batch/BatchRunner.cpp")));
}

QString renderedBody()
{
    return functionBody(batchRunnerSource(),
        QStringLiteral("int BatchRunner::exportRenderedVideoFile("),
        QStringLiteral("int BatchRunner::run(const QString &inputPath, const QString &outputPath)"));
}

QString cdngBody()
{
    return functionBody(batchRunnerSource(),
        QStringLiteral("ProcessResult BatchRunner::exportSingleFile("),
        QString());
}

const QString kLookAssistCall = QStringLiteral("ReceiptApplier::applyHeadlessLookAssist(");
const QString kGradePush = QStringLiteral("applyLookAssistGradeToProcessing(");

// The argument list of the first call at `callAt` (the text between its opening parenthesis and the matching close).
QString callArguments(const QString & body, int callAt)
{
    const int open = body.indexOf(QLatin1Char('('), callAt);
    if (open < 0) return QString();
    int depth = 0;
    for (int i = open; i < body.length(); ++i) {
        if (body.at(i) == QLatin1Char('(')) ++depth;
        else if (body.at(i) == QLatin1Char(')') && --depth == 0) return body.mid(open + 1, i - open - 1);
    }
    return QString();
}

QString collapseSpaces(QString text)
{
    return text.remove(QRegularExpression(QStringLiteral("\\s+")));
}

} // namespace

TEST(ExportRenderedLookAssistWiring, StripCommentsDropsACommentedOutCall)
{
    const QString block = QStringLiteral("/* ReceiptApplier::applyHeadlessLookAssist( r, m, p, 0 ); */");
    const QString line = QStringLiteral("// ReceiptApplier::applyHeadlessLookAssist( r, m, p, 0 );\n");
    const QString spliced = QStringLiteral("// off \\\n ReceiptApplier::applyHeadlessLookAssist( r, m, p, 0 );\n");
    ASSERT_EQ(0, countOccurrences(stripComments(block), kLookAssistCall));
    ASSERT_EQ(0, countOccurrences(stripComments(line), kLookAssistCall));
    ASSERT_EQ(0, countOccurrences(stripComments(spliced), kLookAssistCall));
}

TEST(ExportRenderedLookAssistWiring, BothBodiesAreFound)
{
    ASSERT_FALSE(renderedBody().isEmpty());
    ASSERT_FALSE(cdngBody().isEmpty());
}

// (1)
TEST(ExportRenderedLookAssistWiring, RenderedExportCallsHeadlessLookAssistExactlyOnce)
{
    ASSERT_EQ(1, countOccurrences(renderedBody(), kLookAssistCall));
}

// (2)
TEST(ExportRenderedLookAssistWiring, RenderedLookAssistRunsAfterApplyToMlvAndBeforeDebayerAndRender)
{
    const QString body = renderedBody();
    const int lookAssist = body.indexOf(kLookAssistCall);
    const int applyToMlv = body.indexOf(QStringLiteral("ReceiptApplier::applyToMlv("));
    const int debayer = body.indexOf(QStringLiteral("switch( receipt->debayer() )"));
    const int render = body.indexOf(QStringLiteral("getMlvProcessedFrame16("));
    ASSERT_TRUE(lookAssist >= 0);
    ASSERT_TRUE(applyToMlv >= 0);
    ASSERT_TRUE(debayer >= 0);
    ASSERT_TRUE(render >= 0);
    ASSERT_TRUE(applyToMlv < lookAssist);
    ASSERT_TRUE(lookAssist < debayer);
    ASSERT_TRUE(lookAssist < render);
}

// (3)
TEST(ExportRenderedLookAssistWiring, RenderedLookAssistAnalysesTheClipWideAnchorFrame)
{
    const QString body = renderedBody();
    const int lookAssist = body.indexOf(kLookAssistCall);
    ASSERT_TRUE(lookAssist >= 0);
    ASSERT_TRUE(collapseSpaces(callArguments(body, lookAssist))
                    .contains(QStringLiteral("lookAssistAnalysisFrameIndex(cutIn,cutIn)")));
}

// ---- adversary A10 on 76653777: the curve setters survive a failed allocation ----------------------------------

namespace
{

QString cFunctionBody(const QString & signature)
{
    const QString source = stripComments(readRepoFile(QStringLiteral("src/processing/raw_processing.c")));
    const int at = source.indexOf(signature);
    if (at < 0) return QString();
    const int next = source.indexOf(QStringLiteral("\n}"), at);
    return next < 0 ? QString() : source.mid(at, next - at);
}

// The allocations' null check sits before the first write through either buffer.
bool checksAllocationsBeforeUse(const QString & body)
{
    const int alloc = body.indexOf(QStringLiteral("float *pYout = (float*)malloc("));
    const int check = body.indexOf(QStringLiteral("if( !pXout || !pYout )"));
    const int firstUse = body.indexOf(QStringLiteral("pXout[i] ="));
    return alloc >= 0 && check > alloc && firstUse > check;
}

} // namespace

TEST(ExportRenderedLookAssistWiring, GradationCurveSetterChecksItsAllocations)
{
    ASSERT_TRUE(checksAllocationsBeforeUse(cFunctionBody(QStringLiteral("int processingSetGCurve("))));
}

// Adversary A9 on 16d54744: the cancel scope is installed before the analysis, once.
TEST(ExportRenderedLookAssistWiring, CancelScopeCoversTheAnalysis)
{
    const QString body = renderedBody();
    const int scope = body.indexOf(QStringLiteral("BatchRenderCancelScope cancelScope;"));
    ASSERT_EQ(1, countOccurrences(body, QStringLiteral("BatchRenderCancelScope cancelScope;")));
    ASSERT_TRUE(scope >= 0);
    ASSERT_TRUE(scope < body.indexOf(kLookAssistCall));
}

// Adversary A6 on 16d54744: the spline helper turns an exception into an error return.
TEST(ExportRenderedLookAssistWiring, SplineHelperCatchesExceptions)
{
    const QString helper = stripComments(readRepoFile(QStringLiteral("src/processing/interpolation/spline_helper.cpp")));
    const int body = helper.indexOf(QStringLiteral("int spline1dccc("));
    const int tryAt = helper.indexOf(QStringLiteral("try"), body);
    const int setPoints = helper.indexOf(QStringLiteral("s.set_points("), body);
    const int catchAt = helper.indexOf(QStringLiteral("catch( ... )"), body);
    ASSERT_TRUE(body >= 0 && tryAt > body && setPoints > tryAt && catchAt > setPoints);
}

// Adversary A1 / A3 on 16d54744: the raw analysis thumbnail is zeroed before any allocation can fail, and the debayer
// buffer starts zeroed.
TEST(ExportRenderedLookAssistWiring, RawAnalysisThumbnailStartsBlack)
{
    const QString source = stripComments(readRepoFile(QStringLiteral("src/mlv/video_mlv_misc.c")));
    const int fn = source.indexOf(QStringLiteral("void get_area_average_downscale_raw_thumnail("));
    ASSERT_TRUE(fn >= 0);
    const int end = source.indexOf(QStringLiteral("\n}"), fn);
    const QString body = source.mid(fn, end - fn);
    const int zero = body.indexOf(QStringLiteral("memset(out_buffer, 0,"));
    const int firstAlloc = body.indexOf(QStringLiteral("malloc("));
    ASSERT_TRUE(zero >= 0 && firstAlloc > zero);
    ASSERT_TRUE(body.contains(QStringLiteral("uint16_t *debayered_raw_frame = (uint16_t *) calloc(")));
}

TEST(ExportRenderedLookAssistWiring, HueVsCurveSetterChecksItsAllocations)
{
    ASSERT_TRUE(checksAllocationsBeforeUse(cFunctionBody(QStringLiteral("void processingSetHueVsCurves("))));
}

// (4)
TEST(ExportRenderedLookAssistWiring, RenderedGradeIsPushedToProcessingBeforeTheRender)
{
    const QString body = renderedBody();
    const int lookAssist = body.indexOf(kLookAssistCall);
    const int grade = body.indexOf(kGradePush);
    const int render = body.indexOf(QStringLiteral("getMlvProcessedFrame16("));
    ASSERT_TRUE(lookAssist >= 0);
    ASSERT_TRUE(grade >= 0);
    ASSERT_TRUE(render >= 0);
    ASSERT_TRUE(lookAssist < grade);
    ASSERT_TRUE(grade < render);
}

// ---- r2 hardening (sol r1 / opus key3 r1 on ed129ee1): a dead branch or a zeroed anchor does not pass -------------

namespace
{

// Index just past the ')' closing the call at callAt; -1 when unbalanced.
int callEnd(const QString & body, int callAt)
{
    const int open = body.indexOf(QLatin1Char('('), callAt);
    if (open < 0) return -1;
    int depth = 0;
    for (int i = open; i < body.length(); ++i) {
        if (body.at(i) == QLatin1Char('(')) ++depth;
        else if (body.at(i) == QLatin1Char(')') && --depth == 0) return i + 1;
    }
    return -1;
}

// r3 (r2 advisory A11 on 40efaff7): the statement at `pos` runs whenever the function body runs: it sits at the body's
// own brace depth, and the previous non-space character ends a statement or a block (';', '{', '}'), so neither an
// enclosing block nor an un-braced if (...) / else prefix guards it. The body starts at its first '{'.
bool atTopLevelStatement(const QString & body, int pos)
{
    const int open = body.indexOf(QLatin1Char('{'));
    if (open < 0 || pos <= open) return false;
    int depth = 0;
    for (int i = open; i < pos; ++i) {
        if (body.at(i) == QLatin1Char('{')) ++depth;
        else if (body.at(i) == QLatin1Char('}')) --depth;
    }
    if (depth != 1) return false;
    int i = pos - 1;
    while (i >= 0 && body.at(i).isSpace()) --i;
    return i >= 0 && (body.at(i) == QLatin1Char(';') || body.at(i) == QLatin1Char('{') || body.at(i) == QLatin1Char('}'));
}

// The analysis runs unconditionally on exactly the CDNG anchor, and its grade push is guarded by its verdict alone.
bool analysisIsWiredUnconditionally(const QString & body)
{
    const int apply = body.indexOf(QStringLiteral("ReceiptApplier::applyToMlv("));
    const int lookAssist = body.indexOf(kLookAssistCall);
    const int grade = body.indexOf(kGradePush);
    if (apply < 0 || lookAssist <= apply || grade <= lookAssist) return false;
    if (!atTopLevelStatement(body, apply)) return false;
    const int applyEnd = callEnd(body, apply);
    const int lookAssistEnd = callEnd(body, lookAssist);
    if (applyEnd < 0 || lookAssistEnd < 0) return false;
    return collapseSpaces(body.mid(applyEnd, lookAssist - applyEnd))
               == QStringLiteral(";BatchRenderCancelScopecancelScope;constboollookAssistApplied=")
        && collapseSpaces(callArguments(body, lookAssist))
               == QStringLiteral("receipt,mlvObject,processingObject,CdngSequenceExport::lookAssistAnalysisFrameIndex(cutIn,cutIn)")
        && collapseSpaces(body.mid(lookAssistEnd, grade - lookAssistEnd))
               == QStringLiteral(";if(lookAssistApplied){ReceiptApplier::");
}

const QString kCancelCheck = QStringLiteral("BatchRenderCancelScope::cancelled()");

// The `if` that tests the cancel flag at `checkAt` (the flag call inside its condition) runs unconditionally.
bool cancelGuardRunsAlways(const QString & body, int checkAt)
{
    const int ifAt = body.lastIndexOf(QStringLiteral("if"), checkAt);
    return ifAt >= 0 && collapseSpaces(body.mid(ifAt, checkAt - ifAt)) == QStringLiteral("if(")
        && atTopLevelStatement(body, ifAt);
}

// r2 B3 (sol r1 on ed129ee1): between the analysis and the frame loop the cancel flag is checked before the plan check
// and before the encoder spawn, and every return there is the typed cancellation behind such a check or a failure exit
// through renderedPreEncodeExit (r3, r2 adversary A8: it samples the flag on both sides of the failure line). Both
// guards run unconditionally (r3, r2 advisory A11).
bool preEncodeExitsReportACancel(const QString & body)
{
    const int lookAssist = body.indexOf(kLookAssistCall);
    const int loop = body.indexOf(QStringLiteral("for( uint32_t frameIndex"));
    if (lookAssist < 0 || loop <= lookAssist) return false;
    const QString window = body.mid(lookAssist, loop - lookAssist);
    const int planCheck = window.indexOf(QStringLiteral("if( !plan.runnable )"));
    const int renderStart = window.indexOf(QStringLiteral("[BATCH] RENDER_START"));
    const int spawn = window.indexOf(QStringLiteral("pipeline.start("));
    if (planCheck < 0 || renderStart < 0 || spawn <= renderStart) return false;
    const int firstCheck = window.indexOf(kCancelCheck);
    if (firstCheck < 0 || firstCheck > planCheck) return false;
    if (!cancelGuardRunsAlways(body, lookAssist + firstCheck)) return false;
    const int spawnCheck = window.indexOf(kCancelCheck, renderStart);
    if (spawnCheck < 0 || spawnCheck > spawn) return false;
    if (!cancelGuardRunsAlways(body, lookAssist + spawnCheck)) return false;
    int returns = 0;
    for (int at = window.indexOf(QStringLiteral("return ")); at >= 0; at = window.indexOf(QStringLiteral("return "), at + 1)) {
        ++returns;
        if (window.mid(at).startsWith(QStringLiteral("return renderedPreEncodeExit("))) continue;
        if (!window.mid(at).startsWith(QStringLiteral("return renderedCancelledBeforeEncode("))) return false;
        // The typed cancellation is the body of an if on the flag: directly, or in the block that if opens.
        const QString ifFlag = QStringLiteral("if(BatchRenderCancelScope::cancelled())");
        const int statement = qMax(window.lastIndexOf(QLatin1Char(';'), at), window.lastIndexOf(QLatin1Char('}'), at));
        if (collapseSpaces(window.mid(statement + 1, at - statement - 1)) == ifFlag) continue;
        const int branch = window.lastIndexOf(QLatin1Char('{'), at);
        if (branch < 0 || window.mid(branch, at - branch).contains(QLatin1Char('}'))) return false;
        const int beforeBranch = qMax(window.lastIndexOf(QLatin1Char(';'), branch), window.lastIndexOf(QLatin1Char('}'), branch));
        if (collapseSpaces(window.mid(beforeBranch + 1, branch - beforeBranch - 1)) != ifFlag) return false;
    }
    return returns > 0;
}

// The body of a free function in BatchRunner.cpp, from its signature to the closing brace at column 0.
QString batchRunnerFunction(const QString & signature)
{
    const QString source = batchRunnerSource();
    const int at = source.indexOf(signature);
    if (at < 0) return QString();
    const int end = source.indexOf(QStringLiteral("\n}"), at);
    return end < 0 ? QString() : source.mid(at, end - at + 2);
}

// r3 (r2 adversary A8 on 40efaff7): the failure exit samples the flag before its line and again after it, and only then
// returns the failure code; both samples run unconditionally.
bool preEncodeExitSamplesAroundTheLine(const QString & helper)
{
    const int first = helper.indexOf(kCancelCheck);
    const int line = helper.indexOf(QStringLiteral("BatchLogger::err( failureLine );"));
    const int second = helper.indexOf(kCancelCheck, line);
    const int failure = helper.indexOf(QStringLiteral("return failureCode;"));
    return first >= 0 && line > first && second > line && failure > second
        && cancelGuardRunsAlways(helper, first) && cancelGuardRunsAlways(helper, second)
        && atTopLevelStatement(helper, line) && atTopLevelStatement(helper, failure)
        && helper.indexOf(QStringLiteral("return renderedCancelledBeforeEncode("), first) < line
        && helper.indexOf(QStringLiteral("return renderedCancelledBeforeEncode("), second) < failure
        && helper.indexOf(QStringLiteral("return renderedCancelledBeforeEncode("), second) > second;
}

} // namespace

TEST(ExportRenderedLookAssistWiring, RenderedAnalysisIsWiredUnconditionally)
{
    const QString body = renderedBody();
    ASSERT_TRUE(analysisIsWiredUnconditionally(body));
    // The mutations the r1 reviewers showed passing the older checks now fail.
    QString deadBranch = body;
    deadBranch.replace(QStringLiteral("const bool lookAssistApplied ="), QStringLiteral("if (false) {} const bool lookAssistApplied ="));
    ASSERT_FALSE(analysisIsWiredUnconditionally(deadBranch));
    QString zeroAnchor = body;
    zeroAnchor.replace(QStringLiteral("lookAssistAnalysisFrameIndex( cutIn, cutIn )"),
                       QStringLiteral("lookAssistAnalysisFrameIndex( cutIn, cutIn ) * 0u"));
    ASSERT_FALSE(analysisIsWiredUnconditionally(zeroAnchor));
    QString deadPush = body;
    deadPush.replace(QStringLiteral("if( lookAssistApplied )"), QStringLiteral("if( lookAssistApplied && false )"));
    ASSERT_FALSE(analysisIsWiredUnconditionally(deadPush));
    // r3 (r2 advisory A11 on 40efaff7): applyToMlv, the analysis and the grade enclosed together, braced or not.
    QString deadAll = body;
    deadAll.replace(QStringLiteral("ReceiptApplier::applyToMlv("), QStringLiteral("if (false) { ReceiptApplier::applyToMlv("));
    deadAll.replace(QStringLiteral("switch( receipt->debayer() )"), QStringLiteral("} switch( receipt->debayer() )"));
    ASSERT_FALSE(analysisIsWiredUnconditionally(deadAll));
    QString deadPrefix = body;
    deadPrefix.replace(QStringLiteral("ReceiptApplier::applyToMlv("), QStringLiteral("if (false) ReceiptApplier::applyToMlv("));
    ASSERT_FALSE(analysisIsWiredUnconditionally(deadPrefix));
}

TEST(ExportRenderedLookAssistWiring, ACancelDuringTheAnalysisIsTheTypedCancellation)
{
    ASSERT_TRUE(preEncodeExitsReportACancel(renderedBody()));
}

TEST(ExportRenderedLookAssistWiring, ACancelCheckMissingFromAPreEncodeExitIsCaught)
{
    const QString plain = QStringLiteral(
        "int f() { ReceiptApplier::applyHeadlessLookAssist( r ); if( BatchRenderCancelScope::cancelled() ) { return renderedCancelledBeforeEncode( b ); }"
        " if( !plan.runnable ) { return renderedPreEncodeExit( 2, l, b ); }"
        " \"[BATCH] RENDER_START\"; if( BatchRenderCancelScope::cancelled() ) { return renderedCancelledBeforeEncode( b ); }"
        " if( !pipeline.start( i ) ) { return renderedPreEncodeExit( 4, l, b ); }"
        " for( uint32_t frameIndex = 0; ; ) {} }");
    ASSERT_TRUE(preEncodeExitsReportACancel(plain));
    QString unchecked = plain;
    unchecked.replace(QStringLiteral("{ return renderedPreEncodeExit( 4, l, b ); }"), QStringLiteral("{ return 4; }"));
    ASSERT_FALSE(preEncodeExitsReportACancel(unchecked));
    QString lateCheck = plain;
    lateCheck.replace(QStringLiteral("ReceiptApplier::applyHeadlessLookAssist( r ); if( BatchRenderCancelScope::cancelled() ) { return renderedCancelledBeforeEncode( b ); }"),
                      QStringLiteral("ReceiptApplier::applyHeadlessLookAssist( r );"));
    ASSERT_FALSE(preEncodeExitsReportACancel(lateCheck));
    // r3 (r2 advisory A11): a guard that never runs does not pass, braced or not.
    QString deadGuard = plain;
    deadGuard.replace(QStringLiteral("( r ); if( BatchRenderCancelScope::cancelled() )"),
                      QStringLiteral("( r ); if (false) if( BatchRenderCancelScope::cancelled() )"));
    ASSERT_FALSE(preEncodeExitsReportACancel(deadGuard));
    QString deadSpawnGuard = plain;
    deadSpawnGuard.replace(QStringLiteral("\"[BATCH] RENDER_START\"; if( BatchRenderCancelScope::cancelled() ) { return renderedCancelledBeforeEncode( b ); }"),
                           QStringLiteral("\"[BATCH] RENDER_START\"; if (false) { if( BatchRenderCancelScope::cancelled() ) { return renderedCancelledBeforeEncode( b ); } }"));
    ASSERT_FALSE(preEncodeExitsReportACancel(deadSpawnGuard));
}

// r3 (r2 adversary A8 on 40efaff7): a cancel recorded while the failure line is written is still the typed cancellation.
TEST(ExportRenderedLookAssistWiring, APreEncodeFailureSamplesTheFlagAfterItsLine)
{
    const QString helper = batchRunnerFunction(QStringLiteral("int renderedPreEncodeExit("));
    ASSERT_FALSE(helper.isEmpty());
    ASSERT_TRUE(preEncodeExitSamplesAroundTheLine(helper));
    // Every failure exit between the analysis and the frame loop goes through it (four: plan, invocation, spawn, buffers).
    ASSERT_EQ(4, countOccurrences(renderedBody(), QStringLiteral("return renderedPreEncodeExit(")));
    // The predicate catches the r2 shape (one sample, before the line) and a dead second sample.
    QString oneSample = helper;
    oneSample.replace(QStringLiteral("BatchLogger::err( failureLine );"), QStringLiteral("BatchLogger::err( failureLine ); return failureCode;"));
    const int tail = oneSample.indexOf(QStringLiteral("return failureCode;")) + QStringLiteral("return failureCode;").size();
    oneSample = oneSample.left(tail) + QStringLiteral("\n}");
    ASSERT_FALSE(preEncodeExitSamplesAroundTheLine(oneSample));
    QString deadSecond = helper;
    const int line = deadSecond.indexOf(QStringLiteral("BatchLogger::err( failureLine );"));
    const int secondIf = deadSecond.indexOf(QStringLiteral("if("), line);
    ASSERT_TRUE(secondIf > line);
    deadSecond.insert(secondIf, QStringLiteral("if (false) "));
    ASSERT_FALSE(preEncodeExitSamplesAroundTheLine(deadSecond));
}

// r3 (r2 adversary A9 on 40efaff7): a RENDER_CANCELLED receipt names the partial as removed only when QFile::remove
// removed it.
TEST(ExportRenderedLookAssistWiring, PartialRemovedReflectsTheRemoval)
{
    const QString helper = batchRunnerFunction(QStringLiteral("QString removeRenderedPartial("));
    ASSERT_FALSE(helper.isEmpty());
    ASSERT_TRUE(helper.contains(QStringLiteral("if( QFile::remove( partialPath ) ) return partialPath;")));
    ASSERT_EQ(1, countOccurrences(helper, QStringLiteral("return partialPath;")));
    const QString body = renderedBody();
    // Both cancelled exits that removed a partial (frame buffers before the loop, the loop's own cancel) name only what
    // the removal answered.
    ASSERT_EQ(2, countOccurrences(body, QStringLiteral("const QString partialRemoved = removeRenderedPartial( partialPath );")));
    ASSERT_EQ(0, countOccurrences(body, QStringLiteral("renderedCancelledBeforeEncode( baseName, framesToExport, partialPath )")));
    ASSERT_EQ(0, countOccurrences(collapseSpaces(body), QStringLiteral(".arg(framesToExport).arg(partialPath)")));
}

namespace
{

// The number of .arg( calls that directly follow another .arg( call's closing parenthesis (a chain). String and char
// literals are skipped, so a ')' inside "(no diagnostics)" does not end an argument list.
int chainedArgCalls(const QString & source)
{
    const QString call = QStringLiteral(".arg(");
    int chained = 0;
    for (int at = source.indexOf(call); at >= 0; at = source.indexOf(call, at + call.size())) {
        int depth = 0;
        int i = at + call.size() - 1;   // the '('
        for (; i < source.size(); ++i) {
            const QChar c = source.at(i);
            if (c == QLatin1Char('"') || c == QLatin1Char('\'')) {
                for (++i; i < source.size() && source.at(i) != c; ++i)
                    if (source.at(i) == QLatin1Char('\\')) ++i;
                continue;
            }
            if (c == QLatin1Char('(')) ++depth;
            else if (c == QLatin1Char(')') && --depth == 0) break;
        }
        int next = i + 1;
        while (next < source.size() && source.at(next).isSpace()) ++next;
        if (source.mid(next, call.size()) == call) ++chained;
    }
    return chained;
}

} // namespace

// r5 (r4 adversary A2 on 07462a6a): a chain of .arg( calls substitutes again into what an earlier call inserted, so a
// clip whose base name itself holds a %N placeholder lost its name and its counts in the RENDER_CANCELLED record. Every
// BatchRunner line fills all of its placeholders in one pass, so a name or path is printed as it is.
TEST(ExportRenderedLookAssistWiring, NoBatchLineSubstitutesIntoAnEarlierValue)
{
    // Why: the chain re-substitutes; one multi-arg call does not.
    const QString name = QStringLiteral("take%1_b");
    ASSERT_TRUE(QStringLiteral("%1 frames=0/%2 partial_removed=%3").arg(name).arg(7).arg(QStringLiteral("none"))
                != QStringLiteral("take%1_b frames=0/7 partial_removed=none"));
    ASSERT_TRUE(QStringLiteral("%1 frames=0/%2 partial_removed=%3").arg(name, QString::number(7), QStringLiteral("none"))
                == QStringLiteral("take%1_b frames=0/7 partial_removed=none"));
    // The predicate sees a chain, across lines and past a ')' inside a string, and nothing else.
    ASSERT_EQ(1, chainedArgCalls(QStringLiteral("f(QStringLiteral(\"%1 %2\").arg(a)\n    .arg(b));")));
    ASSERT_EQ(1, chainedArgCalls(QStringLiteral("f(QStringLiteral(\"%1 %2\").arg(x ? QStringLiteral(\"(none)\") : a).arg(b));")));
    ASSERT_EQ(0, chainedArgCalls(QStringLiteral("f(QStringLiteral(\"%1 %2\").arg(a, QString::number(b)));")));
    ASSERT_EQ(0, chainedArgCalls(QStringLiteral("f(QStringLiteral(\"%1\").arg(a)); g(QStringLiteral(\"%1\").arg(b));")));
    // BatchRunner.cpp has none.
    const QString source = batchRunnerSource();
    ASSERT_FALSE(source.isEmpty());
    ASSERT_EQ(0, chainedArgCalls(source));
}

// ---- ReceiptApplier (linked into console_tests, but its curve and level paths need a clip or a fault) -----------------

namespace
{

QString receiptApplierSource()
{
    return stripComments(readRepoFile(QStringLiteral("src/batch/ReceiptApplier.cpp")));
}

} // namespace

// r3 (r2 adversary A6 on 40efaff7): master's pass starts from the levels this pass found on entry, so a skip in it gives
// those back; the re-run stays a tail call (test_look_assist_scene.cpp pins it).
TEST(ExportRenderedLookAssistWiring, MastersPassStartsFromTheEntryLevels)
{
    const QString source = receiptApplierSource();
    const QString tail = QStringLiteral("return applyHeadlessLookAssist( receipt, mlvObject, processingObject, analysisFrame, true );");
    ASSERT_EQ(1, countOccurrences(source, tail));
    const int tailAt = source.indexOf(tail);
    const int state = source.lastIndexOf(QStringLiteral("daylight_fallback_state"), tailAt);
    ASSERT_TRUE(state > 0);
    const int stateEnd = source.indexOf(QLatin1Char(';'), state);
    ASSERT_TRUE(collapseSpaces(source.mid(stateEnd + 1, tailAt - stateEnd - 1))
                == QStringLiteral("restoreHeadlessRawLevels(levelsBeforeAutoFix,receipt,mlvObject,processingObject);"));
}

// r3 (r2 adversary A7 on 40efaff7): every C++ allocation of the curve push happens inside one try, closed by a
// std::bad_alloc catch, before the first table moves.
TEST(ExportRenderedLookAssistWiring, TheCurvePushAllocatesBeforeAnyTableMoves)
{
    const QString source = receiptApplierSource();
    const int fn = source.indexOf(QStringLiteral("bool ReceiptApplier::applyLookAssistGradeToProcessing("));
    ASSERT_TRUE(fn >= 0);
    const int end = source.indexOf(QStringLiteral("\n}"), fn);
    const QString body = source.mid(fn, end - fn);
    const int tryAt = body.indexOf(QStringLiteral("try"));
    const int curve = body.indexOf(QStringLiteral("receipt->gradationCurve()"));
    const int parse = body.indexOf(QStringLiteral("lookAssistParseGradationCurve("));
    const int lastPush = body.lastIndexOf(QStringLiteral("push_back("));
    const int catchAt = body.indexOf(QStringLiteral("catch( const std::bad_alloc & )"));
    const int firstSet = body.indexOf(QStringLiteral("processingSetGCurve("));
    ASSERT_TRUE(tryAt >= 0 && curve > tryAt && parse > curve && lastPush > parse && catchAt > lastPush);
    ASSERT_TRUE(firstSet > catchAt);
    ASSERT_TRUE(atTopLevelStatement(body, tryAt));
    // The setter's status restores all four tables (A5; the pipeline test pins the behaviour).
    ASSERT_TRUE(body.indexOf(QStringLiteral("!= 0"), firstSet) > firstSet);
    ASSERT_EQ(-1, body.indexOf(QStringLiteral("BatchLogger::")));   // the warning goes through the noexcept helper
}

// (5)
TEST(ExportRenderedLookAssistWiring, CdngExportLookAssistIsUnchanged)
{
    const QString body = cdngBody();
    ASSERT_EQ(1, countOccurrences(body, kLookAssistCall));
    const QString args = collapseSpaces(callArguments(body, body.indexOf(kLookAssistCall)));
    ASSERT_TRUE(args.contains(QStringLiteral("lookAssistAnalysisFrameIndex(cutIn,effectiveCutIn)")));
    ASSERT_EQ(0, countOccurrences(body, kGradePush));
}
