#include "../common/minitest.h"
#include "../common/hash_helpers.h"
#include "../common/repo_paths.h"
#include "../common/test_artifacts.h"

#include "../../src/batch/ReceiptLoader.h"
#include "../../platform/qt/ReceiptSettings.h"

#include <QDir>
#include <QFile>
#include <QTextStream>
#include <QTemporaryDir>
#include <QString>

static QString repo_receipt_path(const QString & name)
{
    return repo_file_path(QStringLiteral("receipts/%1").arg(name));
}

static QString receipt_summary(ReceiptSettings & receipt)
{
    QString summary;
    summary += QStringLiteral("pivot=%1;").arg(receipt.pivot());
    summary += QStringLiteral("rawFixes=%1;").arg(receipt.rawFixesEnabled());
    summary += QStringLiteral("lookAssist=%1;").arg(receipt.lookAssistEnabled());
    summary += QStringLiteral("lookAssistBase=%1;").arg(receipt.lookAssistBaselineValid());
    summary += QStringLiteral("lookAssistStretchX=%1;").arg(receipt.lookAssistBaselineStretchX());
    summary += QStringLiteral("lookAssistStretchY=%1;").arg(receipt.lookAssistBaselineStretchY());
    summary += QStringLiteral("focusPixels=%1;").arg(receipt.focusPixels());
    summary += QStringLiteral("dualIso=%1;").arg(receipt.dualIso());
    summary += QStringLiteral("alias=%1;").arg(receipt.dualIsoAliasMap());
    summary += QStringLiteral("fullres=%1;").arg(receipt.dualIsoFrBlending());
    summary += QStringLiteral("rawBlack=%1;").arg(receipt.rawBlack());
    summary += QStringLiteral("rawWhite=%1;").arg(receipt.rawWhite());
    summary += QStringLiteral("cutOut=%1;").arg(receipt.cutOut());
    summary += QStringLiteral("debayer=%1;").arg(receipt.debayer());
    return summary;
}

TEST(ReceiptLoader, LoadsFastProxyReceipt)
{
    ReceiptSettings receipt;
    QString error_message;

    ASSERT_TRUE(ReceiptLoader::loadFromFile(repo_receipt_path(QStringLiteral("FastProxy.marxml")),
                                            &receipt,
                                            &error_message));
    ASSERT_FALSE(receipt.wasNeverLoaded());
    ASSERT_EQ(75, receipt.pivot());
    ASSERT_TRUE(receipt.rawFixesEnabled());
    ASSERT_EQ(1, receipt.focusPixels());
    ASSERT_EQ(143u, receipt.cutOut());
    ASSERT_EQ(static_cast<unsigned int>(2), static_cast<unsigned int>(receipt.debayer()));
    ASSERT_EQ(20470, receipt.rawBlack());
    ASSERT_EQ(2840, receipt.rawWhite());
    ASSERT_TRUE(receipt.lookAssistEnabled());
    ASSERT_FALSE(receipt.lookAssistBaselineValid());

    test_artifacts::record("receipt.fast_proxy",
                           sha256_qstring(receipt_summary(receipt)));
}

TEST(ReceiptLoader, LoadsFastProxyRcdReceipt)
{
    ReceiptSettings receipt;
    QString error_message;

    ASSERT_TRUE(ReceiptLoader::loadFromFile(repo_receipt_path(QStringLiteral("FastProxyRCD.marxml")),
                                            &receipt,
                                            &error_message));
    ASSERT_EQ(static_cast<unsigned int>(7), static_cast<unsigned int>(receipt.debayer()));
    ASSERT_EQ(1, receipt.dualIsoAliasMap());
    ASSERT_EQ(1, receipt.dualIsoFrBlending());

    test_artifacts::record("receipt.fast_proxy_rcd",
                           sha256_qstring(receipt_summary(receipt)));
}

TEST(ReceiptLoader, LoadsLookAssistReceiptState)
{
    QTemporaryDir tempDir;
    ASSERT_TRUE(tempDir.isValid());

    const QString receiptPath = tempDir.filePath(QStringLiteral("lookassist.marxml"));
    QFile file(receiptPath);
    ASSERT_TRUE(file.open(QIODevice::WriteOnly | QIODevice::Text));

    QTextStream out(&file);
    out << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
    out << "<receipt version=\"4\" mlvapp=\"test\">\n";
    out << "  <exposure>34</exposure>\n";
    out << "  <contrast>12</contrast>\n";
    out << "  <pivot>58</pivot>\n";
    out << "  <rawFixesEnabled>1</rawFixesEnabled>\n";
    out << "  <rawBlack>20470</rawBlack>\n";
    out << "  <rawWhite>2840</rawWhite>\n";
    out << "  <lookAssistEnabled>0</lookAssistEnabled>\n";
    out << "  <lookAssistBaselineValid>1</lookAssistBaselineValid>\n";
    out << "  <lookAssistBaselineExposure>10</lookAssistBaselineExposure>\n";
    out << "  <lookAssistBaselineContrast>-4</lookAssistBaselineContrast>\n";
    out << "  <lookAssistBaselinePivot>47</lookAssistBaselinePivot>\n";
    out << "  <lookAssistBaselineTemperature>5200</lookAssistBaselineTemperature>\n";
    out << "  <lookAssistBaselineTint>-7</lookAssistBaselineTint>\n";
    out << "  <lookAssistBaselineVibrance>5</lookAssistBaselineVibrance>\n";
    out << "  <lookAssistBaselineShadows>18</lookAssistBaselineShadows>\n";
    out << "  <lookAssistBaselineHighlights>-12</lookAssistBaselineHighlights>\n";
    out << "  <lookAssistBaselineRawBlack>20490</lookAssistBaselineRawBlack>\n";
    out << "  <lookAssistBaselineRawWhite>3021</lookAssistBaselineRawWhite>\n";
    out << "  <lookAssistBaselineStretchX>1</lookAssistBaselineStretchX>\n";
    out << "  <lookAssistBaselineStretchY>0.3333</lookAssistBaselineStretchY>\n";
    out << "</receipt>\n";
    file.close();

    ReceiptSettings receipt;
    QString error_message;
    ASSERT_TRUE(ReceiptLoader::loadFromFile(receiptPath, &receipt, &error_message));
    ASSERT_TRUE(error_message.isEmpty());
    ASSERT_FALSE(receipt.lookAssistEnabled());
    ASSERT_TRUE(receipt.lookAssistBaselineValid());
    ASSERT_EQ(10, receipt.lookAssistBaselineExposure());
    ASSERT_EQ(-4, receipt.lookAssistBaselineContrast());
    ASSERT_EQ(47, receipt.lookAssistBaselinePivot());
    ASSERT_EQ(5200, receipt.lookAssistBaselineTemperature());
    ASSERT_EQ(-7, receipt.lookAssistBaselineTint());
    ASSERT_EQ(5, receipt.lookAssistBaselineVibrance());
    ASSERT_EQ(18, receipt.lookAssistBaselineShadows());
    ASSERT_EQ(-12, receipt.lookAssistBaselineHighlights());
    ASSERT_EQ(20490, receipt.lookAssistBaselineRawBlack());
    ASSERT_EQ(3021, receipt.lookAssistBaselineRawWhite());
    ASSERT_NEAR(1.0, receipt.lookAssistBaselineStretchX(), 0.0001);
    ASSERT_NEAR(0.3333, receipt.lookAssistBaselineStretchY(), 0.0001);
}

// EXPORT-PRORES-LOOK-ASSIST-1, adversary A1 on 76653777: a compact receipt (no whitespace between elements) loses no
// element. Before the fix the walk stepped past <lookAssistEnabled> and stopped at its end element.
static bool loadReceiptText(const QByteArray & text, ReceiptSettings * receipt)
{
    QTemporaryDir tempDir;
    if (!tempDir.isValid()) return false;
    const QString receiptPath = tempDir.filePath(QStringLiteral("compact.marxml"));
    QFile file(receiptPath);
    if (!file.open(QIODevice::WriteOnly)) return false;
    file.write(text);
    file.close();
    QString error_message;
    return ReceiptLoader::loadFromFile(receiptPath, receipt, &error_message) && error_message.isEmpty();
}

TEST(ReceiptLoader, ACompactReceiptLosesNoOptOut)
{
    ReceiptSettings receipt;
    ASSERT_TRUE(receipt.lookAssistEnabled());   // the default the skipped element used to leave
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\"><cutIn>1</cutIn><lookAssistEnabled>0</lookAssistEnabled></receipt>"), &receipt));
    ASSERT_EQ(1u, receipt.cutIn());
    ASSERT_FALSE(receipt.lookAssistEnabled());
}

// r2 B1 (sol r1 on ed129ee1): every field other than the Look Assist opt-out is read token for token as on base
// 1a65d592, whose walk reads a compact receipt's first element and stops at the end of the next one. Reading the rest
// changed the rendered frame range with Look Assist off (base exports 1..clip end for this receipt, not 2..2).
TEST(ReceiptLoader, ACompactOffReceiptKeepsTheBaseFrameRange)
{
    ReceiptSettings defaults;
    ReceiptSettings receipt;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\"><lookAssistEnabled>0</lookAssistEnabled><cutIn>2</cutIn><cutOut>2</cutOut></receipt>"),
        &receipt));
    ASSERT_FALSE(receipt.lookAssistEnabled());
    ASSERT_EQ(defaults.cutIn(), receipt.cutIn());
    ASSERT_EQ(defaults.cutOut(), receipt.cutOut());

    ReceiptSettings flavored;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\"><lookAssistEnabled>1</lookAssistEnabled><lookAssistFlavor>film</lookAssistFlavor>"
        "<unknownFutureTag>7</unknownFutureTag><cutIn>3</cutIn><cutOut>9</cutOut></receipt>"), &flavored));
    ASSERT_TRUE(flavored.lookAssistEnabled());
    ASSERT_EQ(defaults.lookAssistFlavor().toStdString(), flavored.lookAssistFlavor().toStdString());
    ASSERT_EQ(defaults.cutIn(), flavored.cutIn());
    ASSERT_EQ(defaults.cutOut(), flavored.cutOut());
}

// r2 B2a (sol r1 on ed129ee1): a compact namespaced extension between cutIn and the plain opt-out hides nothing.
TEST(ReceiptLoader, AnExtensionBeforeACompactOptOutDoesNotHideIt)
{
    ReceiptSettings receipt;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\" xmlns:x=\"urn:metadata\"><cutIn>1</cutIn><x:lookAssistEnabled>1</x:lookAssistEnabled>"
        "<lookAssistEnabled>0</lookAssistEnabled></receipt>"), &receipt));
    ASSERT_FALSE(receipt.lookAssistEnabled());
}

// r2 B2b (sol r1 on ed129ee1): an indented extension after the plain opt-out does not overwrite it.
TEST(ReceiptLoader, AnIndentedExtensionAfterTheOptOutDoesNotOverwriteIt)
{
    ReceiptSettings receipt;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\" xmlns:x=\"urn:metadata\">\n<lookAssistEnabled>0</lookAssistEnabled>\n"
        "<x:lookAssistEnabled>1</x:lookAssistEnabled>\n</receipt>\n"), &receipt));
    ASSERT_FALSE(receipt.lookAssistEnabled());

    // The last plain element still wins when it opts back in, as on base; CRLF reads the same.
    ReceiptSettings optIn;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\">\r\n<lookAssistEnabled>0</lookAssistEnabled>\r\n<lookAssistEnabled>1</lookAssistEnabled>\r\n"
        "</receipt>\r\n"), &optIn));
    ASSERT_TRUE(optIn.lookAssistEnabled());
}

// Adversary A5 on 16d54744: a compact namespaced extension tag with a known local name does not overwrite the field.
TEST(ReceiptLoader, ACompactNamespacedTagDoesNotOverwriteAField)
{
    ReceiptSettings receipt;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\" xmlns:x=\"urn:metadata\"><lookAssistEnabled>0</lookAssistEnabled>"
        "<x:lookAssistEnabled>1</x:lookAssistEnabled></receipt>"), &receipt));
    ASSERT_FALSE(receipt.lookAssistEnabled());
}

TEST(ReceiptLoader, AnIndentedReceiptReadsAsBefore)
{
    ReceiptSettings receipt;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\">\n  <cutIn>2</cutIn>\n  <lookAssistEnabled>0</lookAssistEnabled>\n  <cutOut>5</cutOut>\n</receipt>\n"),
        &receipt));
    ASSERT_EQ(2u, receipt.cutIn());
    ASSERT_FALSE(receipt.lookAssistEnabled());
    ASSERT_EQ(5u, receipt.cutOut());
}

// The GUI's copy of the reader (MainWindow::readXmlElementsFromFile, not linked here) keeps the base walk and applies
// the same opt-out scan after it (r2 B1/B2).
TEST(ReceiptLoader, TheGuiReaderUsesTheSameWalk)
{
    QFile file(repo_file_path(QStringLiteral("platform/qt/MainWindow.cpp")));
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QString source = QString::fromUtf8(file.readAll());
    const int at = source.indexOf(QStringLiteral("void MainWindow::readXmlElementsFromFile("));
    ASSERT_TRUE(at >= 0);
    const int end = source.indexOf(QStringLiteral("\n}"), at);
    ASSERT_TRUE(end > at);
    const QString body = source.mid(at, end - at);
    const int offset = body.indexOf(QStringLiteral("const qint64 receiptElementOffset = Rxml->characterOffset();"));
    const int walk = body.indexOf(QStringLiteral(
        "    while( !Rxml->atEnd() && !Rxml->isEndElement() )\n    {\n        Rxml->readNext();\n\n"
        "        if( Rxml->isStartElement() && Rxml->name() == QString( \"exposure\" ) )"));
    const int scan = body.indexOf(QStringLiteral(
        "if( ReceiptXmlWalk::plainLookAssistOptOut( Rxml, receiptElementOffset ) ) receipt->setLookAssistEnabled( false );"));
    ASSERT_TRUE(offset >= 0);
    ASSERT_TRUE(walk > offset);
    ASSERT_TRUE(scan > walk);
    ASSERT_TRUE(scan > body.lastIndexOf(QStringLiteral("Rxml->readElementText();")));
    ASSERT_EQ(-1, body.indexOf(QStringLiteral("walk.next(")));
    ASSERT_EQ(-1, body.indexOf(QStringLiteral("walk.settle(")));
}

// The batch reader carries the same two pieces (its behaviour is pinned by the tests above).
TEST(ReceiptLoader, TheBatchReaderKeepsTheBaseWalk)
{
    QFile file(repo_file_path(QStringLiteral("src/batch/ReceiptLoader.cpp")));
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QString source = QString::fromUtf8(file.readAll());
    ASSERT_EQ(-1, source.indexOf(QStringLiteral("walk.next(")));
    ASSERT_EQ(-1, source.indexOf(QStringLiteral("walk.settle(")));
    ASSERT_TRUE(source.indexOf(QStringLiteral(
        "if( ReceiptXmlWalk::plainLookAssistOptOut( Rxml, receiptElementOffset ) ) receipt->setLookAssistEnabled( false );")) >= 0);
}

TEST(ReceiptLoader, MissingReceiptReturnsUsefulError)
{
    ReceiptSettings receipt;
    QString error_message;

    ASSERT_FALSE(ReceiptLoader::loadFromFile(repo_receipt_path(QStringLiteral("missing.marxml")),
                                             &receipt,
                                             &error_message));
    ASSERT_TRUE(!error_message.isEmpty());
}

TEST(ReceiptLoader, LoadsDualIsoPreviewReceiptMode)
{
    ReceiptSettings receipt;
    QString error_message;

    ASSERT_TRUE(ReceiptLoader::loadFromFile(repo_file_path(QStringLiteral("tests/fixtures/receipts/large_dual_iso_preview.marxml")),
                                            &receipt,
                                            &error_message));
    ASSERT_EQ(2, receipt.dualIso());
    ASSERT_EQ(1, receipt.dualIsoAutoCorrected());
    ASSERT_EQ(1, receipt.dualIsoInterpolation());
    ASSERT_EQ(0, receipt.dualIsoAliasMap());
    ASSERT_EQ(0, receipt.dualIsoFrBlending());
}
