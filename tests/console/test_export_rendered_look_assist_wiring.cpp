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
    ASSERT_TRUE(checksAllocationsBeforeUse(cFunctionBody(QStringLiteral("void processingSetGCurve("))));
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

// The analysis runs unconditionally on exactly the CDNG anchor, and its grade push is guarded by its verdict alone.
bool analysisIsWiredUnconditionally(const QString & body)
{
    const int apply = body.indexOf(QStringLiteral("ReceiptApplier::applyToMlv("));
    const int lookAssist = body.indexOf(kLookAssistCall);
    const int grade = body.indexOf(kGradePush);
    if (apply < 0 || lookAssist <= apply || grade <= lookAssist) return false;
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

// r2 B3 (sol r1 on ed129ee1): between the analysis and the frame loop the cancel flag is checked before the plan check
// and before the encoder spawn, and every return there either is the typed cancellation or sits in a branch that
// checks the flag first.
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
    if (window.mid(renderStart, spawn - renderStart).indexOf(kCancelCheck) < 0) return false;
    int returns = 0;
    for (int at = window.indexOf(QStringLiteral("return ")); at >= 0; at = window.indexOf(QStringLiteral("return "), at + 1)) {
        ++returns;
        if (window.mid(at).startsWith(QStringLiteral("return renderedCancelledBeforeEncode("))) continue;
        const int branch = window.lastIndexOf(QLatin1Char('{'), at);
        if (branch < 0 || window.mid(branch, at - branch).indexOf(kCancelCheck) < 0) return false;
    }
    return returns > 0;
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
}

TEST(ExportRenderedLookAssistWiring, ACancelDuringTheAnalysisIsTheTypedCancellation)
{
    ASSERT_TRUE(preEncodeExitsReportACancel(renderedBody()));
}

TEST(ExportRenderedLookAssistWiring, ACancelCheckMissingFromAPreEncodeExitIsCaught)
{
    const QString plain = QStringLiteral(
        "ReceiptApplier::applyHeadlessLookAssist( r ); if( BatchRenderCancelScope::cancelled() ) { return renderedCancelledBeforeEncode( b ); }"
        " if( !plan.runnable ) { if( BatchRenderCancelScope::cancelled() ) return renderedCancelledBeforeEncode( b ); return 2; }"
        " \"[BATCH] RENDER_START\"; if( BatchRenderCancelScope::cancelled() ) { return renderedCancelledBeforeEncode( b ); }"
        " if( !pipeline.start( i ) ) { if( BatchRenderCancelScope::cancelled() ) return renderedCancelledBeforeEncode( b ); return 4; }"
        " for( uint32_t frameIndex = 0; ; ) {}");
    ASSERT_TRUE(preEncodeExitsReportACancel(plain));
    QString unchecked = plain;
    unchecked.replace(QStringLiteral("{ if( BatchRenderCancelScope::cancelled() ) return renderedCancelledBeforeEncode( b ); return 4; }"),
                      QStringLiteral("{ return 4; }"));
    ASSERT_FALSE(preEncodeExitsReportACancel(unchecked));
    QString lateCheck = plain;
    lateCheck.replace(QStringLiteral("ReceiptApplier::applyHeadlessLookAssist( r ); if( BatchRenderCancelScope::cancelled() ) { return renderedCancelledBeforeEncode( b ); }"),
                      QStringLiteral("ReceiptApplier::applyHeadlessLookAssist( r );"));
    ASSERT_FALSE(preEncodeExitsReportACancel(lateCheck));
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
