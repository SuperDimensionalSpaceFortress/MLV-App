// EXPORT-PRORES-LOOK-ASSIST-1: wiring test. A rendered (ProRes / ffmpeg) batch export must run headless Look Assist the
// way the CDNG export does, and must then push the grade Look Assist wrote into the receipt into the processing object:
// CDNG carries the grade as DNG metadata, but a rendered frame shows only processing-object state. BatchRunner.cpp needs
// the GUI build (not linked into console_tests), so this reads the source as text, with comments stripped, as
// test_export_receipt_debayer_wiring.cpp does. One TEST per check, so reverting one piece turns exactly that test red:
//   (1) exportRenderedVideoFile calls ReceiptApplier::applyHeadlessLookAssist( exactly once;
//   (2) after ReceiptApplier::applyToMlv( and before the debayer switch and getMlvProcessedFrame16(;
//   (3) with lookAssistAnalysisFrameIndex( in its argument list (the clip-wide anchor CDNG analyses);
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
    ASSERT_TRUE(callArguments(body, lookAssist).contains(QStringLiteral("lookAssistAnalysisFrameIndex(")));
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

// (5)
TEST(ExportRenderedLookAssistWiring, CdngExportLookAssistIsUnchanged)
{
    const QString body = cdngBody();
    ASSERT_EQ(1, countOccurrences(body, kLookAssistCall));
    const QString args = collapseSpaces(callArguments(body, body.indexOf(kLookAssistCall)));
    ASSERT_TRUE(args.contains(QStringLiteral("lookAssistAnalysisFrameIndex(cutIn,effectiveCutIn)")));
    ASSERT_EQ(0, countOccurrences(body, kGradePush));
}
