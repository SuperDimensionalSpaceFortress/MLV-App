// UPSTREAM-SYNC-20260920-1 increment 1 (port of upstream e25c1fa0, masc4ii: "fix rcd and dcb debayer for export"):
// wiring test. A receipt whose debayer is ReceiptSettings::RCD or ::DCB must make every debayered export use that
// algorithm; before the port, three export switches had no case for either and fell to `default`, keeping a stale
// debayer. MainWindow.cpp needs a full GUI build (not linked into console_tests), so this reads the sources as text
// (as test_playback_conform_wiring.cpp does) and pins, per site:
//   (a) the body is found;
//   (b) "case ReceiptSettings::RCD:" calls setMlvUseRcdDebayer( before the next case label;
//   (c) the same for DCB / setMlvUseDcbDebayer(;
//   (d) every enumerator of ReceiptSettings::DebayerAlgorithm (read from ReceiptSettings.h) has a case label.
// One TEST per site and check, so reverting a single site turns exactly that site's tests red.
// Sites: MainWindow::startExportPipe, MainWindow::startExportAVFoundation (Q_OS_MACX only, so this test is its sole
// proof on Windows) and BatchRunner::exportRenderedVideoFile (fork-only, beyond upstream).
// CDNG / MLV exports write Bayer data and never debayer; they are deliberately not covered.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTextStream>

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
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

QString functionBody(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}

QString startExportPipeBody()
{
    return functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
        QStringLiteral("void MainWindow::startExportPipe(QString fileName)"),
        QStringLiteral("void MainWindow::startExportCdng(QString fileName)"));
}

QString startExportAVFoundationBody()
{
    return functionBody(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")),
        QStringLiteral("void MainWindow::startExportAVFoundation(QString fileName)"),
        QStringLiteral("int MainWindow::askToSaveCurrentSession()"));
}

QString batchRenderedExportBody()
{
    return functionBody(readRepoFile(QStringLiteral("src/batch/BatchRunner.cpp")),
        QStringLiteral("int BatchRunner::exportRenderedVideoFile("),
        QStringLiteral("int BatchRunner::run(const QString &inputPath, const QString &outputPath)"));
}

// The text between "case ReceiptSettings::<name>:" and the next case or default label (or the body end). Empty when
// the label is missing or appears more than once (an ambiguous site must not pass by matching the wrong switch).
QString caseArm(const QString & body, const QString & name)
{
    const QString label = QStringLiteral("case ReceiptSettings::%1:").arg(name);
    if (countOccurrences(body, label) != 1) return QString();
    const int start = body.indexOf(label) + label.length();
    int end = body.length();
    const int nextCase = body.indexOf(QStringLiteral("case "), start);
    if (nextCase >= 0 && nextCase < end) end = nextCase;
    const int nextDefault = body.indexOf(QStringLiteral("default:"), start);
    if (nextDefault >= 0 && nextDefault < end) end = nextDefault;
    return body.mid(start, end - start);
}

bool armCalls(const QString & body, const QString & name, const QString & setter)
{
    const QString arm = caseArm(body, name);
    return !arm.isEmpty() && arm.contains(setter);
}

// The enumerators of ReceiptSettings::DebayerAlgorithm, read from the header (never hard-coded).
QStringList debayerEnumerators()
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/ReceiptSettings.h"));
    const QRegularExpression enumRe(QStringLiteral("enum\\s+DebayerAlgorithm\\s*\\{([^}]*)\\}"));
    const QRegularExpressionMatch match = enumRe.match(header);
    QStringList names;
    if (!match.hasMatch()) return names;
    for (const QString & item : match.captured(1).split(QLatin1Char(','))) {
        const QString name = item.section(QLatin1Char('='), 0, 0).trimmed();
        if (!name.isEmpty()) names.append(name);
    }
    return names;
}

QStringList missingEnumeratorCases(const QString & body)
{
    QStringList missing;
    for (const QString & name : debayerEnumerators()) {
        if (!body.contains(QStringLiteral("case ReceiptSettings::%1:").arg(name))) missing.append(name);
    }
    return missing;
}

} // namespace

// ---- the enum the parity checks read -------------------------------------------------------

TEST(ExportReceiptDebayerWiring, DebayerEnumIsReadFromTheHeaderAndNamesRcdAndDcb)
{
    const QStringList names = debayerEnumerators();
    ASSERT_TRUE(names.size() >= 9);
    ASSERT_TRUE(names.contains(QStringLiteral("None")));
    ASSERT_TRUE(names.contains(QStringLiteral("AHD")));
    ASSERT_TRUE(names.contains(QStringLiteral("RCD")));
    ASSERT_TRUE(names.contains(QStringLiteral("DCB")));
}

// ---- MainWindow::startExportPipe ------------------------------------------------------------

TEST(ExportReceiptDebayerWiring, PipeBodyIsFound)
{
    ASSERT_FALSE(startExportPipeBody().isEmpty());
}

TEST(ExportReceiptDebayerWiring, PipeRcdReceiptUsesRcdDebayer)
{
    ASSERT_TRUE(armCalls(startExportPipeBody(), QStringLiteral("RCD"), QStringLiteral("setMlvUseRcdDebayer(")));
}

TEST(ExportReceiptDebayerWiring, PipeDcbReceiptUsesDcbDebayer)
{
    ASSERT_TRUE(armCalls(startExportPipeBody(), QStringLiteral("DCB"), QStringLiteral("setMlvUseDcbDebayer(")));
}

TEST(ExportReceiptDebayerWiring, PipeSwitchCoversEveryDebayerEnumerator)
{
    const QString body = startExportPipeBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_EQ(std::string(), missingEnumeratorCases(body).join(QLatin1Char(',')).toStdString());
}

// ---- MainWindow::startExportAVFoundation (Q_OS_MACX; cannot compile on Windows) ----------------

TEST(ExportReceiptDebayerWiring, AVFoundationBodyIsFound)
{
    ASSERT_FALSE(startExportAVFoundationBody().isEmpty());
}

TEST(ExportReceiptDebayerWiring, AVFoundationRcdReceiptUsesRcdDebayer)
{
    ASSERT_TRUE(armCalls(startExportAVFoundationBody(), QStringLiteral("RCD"), QStringLiteral("setMlvUseRcdDebayer(")));
}

TEST(ExportReceiptDebayerWiring, AVFoundationDcbReceiptUsesDcbDebayer)
{
    ASSERT_TRUE(armCalls(startExportAVFoundationBody(), QStringLiteral("DCB"), QStringLiteral("setMlvUseDcbDebayer(")));
}

TEST(ExportReceiptDebayerWiring, AVFoundationSwitchCoversEveryDebayerEnumerator)
{
    const QString body = startExportAVFoundationBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_EQ(std::string(), missingEnumeratorCases(body).join(QLatin1Char(',')).toStdString());
}

// ---- BatchRunner::exportRenderedVideoFile (fork-only, beyond upstream) -------------------------

TEST(ExportReceiptDebayerWiring, BatchRenderedExportBodyIsFound)
{
    ASSERT_FALSE(batchRenderedExportBody().isEmpty());
}

TEST(ExportReceiptDebayerWiring, BatchRenderedExportRcdReceiptUsesRcdDebayer)
{
    ASSERT_TRUE(armCalls(batchRenderedExportBody(), QStringLiteral("RCD"), QStringLiteral("setMlvUseRcdDebayer(")));
}

TEST(ExportReceiptDebayerWiring, BatchRenderedExportDcbReceiptUsesDcbDebayer)
{
    ASSERT_TRUE(armCalls(batchRenderedExportBody(), QStringLiteral("DCB"), QStringLiteral("setMlvUseDcbDebayer(")));
}

TEST(ExportReceiptDebayerWiring, BatchRenderedExportSwitchCoversEveryDebayerEnumerator)
{
    const QString body = batchRenderedExportBody();
    ASSERT_FALSE(body.isEmpty());
    ASSERT_EQ(std::string(), missingEnumeratorCases(body).join(QLatin1Char(',')).toStdString());
}
