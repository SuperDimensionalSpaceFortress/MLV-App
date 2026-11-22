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

    // With no plain element the walk's value stands: an extension opt-out base honoured stays honoured.
    ReceiptSettings extensionOff;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\" xmlns:x=\"urn:metadata\">\n<x:lookAssistEnabled>0</x:lookAssistEnabled>\n<cutIn>4</cutIn>\n"
        "</receipt>\n"), &extensionOff));
    ASSERT_FALSE(extensionOff.lookAssistEnabled());
    ASSERT_EQ(4u, extensionOff.cutIn());
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

// r3 (r2 adversary A1 on 40efaff7): a nested element named like the receipt, which the walk steps past and whose end
// stops the walk, does not hide the opt-out after it. r2 compared the stop element's name with the receipt's.
TEST(ReceiptLoader, ANestedReceiptElementDoesNotDefeatTheOptOut)
{
    ReceiptSettings receipt;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\"><cutIn>1</cutIn><receipt/><lookAssistEnabled>0</lookAssistEnabled></receipt>"), &receipt));
    ASSERT_EQ(1u, receipt.cutIn());
    ASSERT_FALSE(receipt.lookAssistEnabled());
}

// r3 (r2 adversary A2 on 40efaff7): a malformed earlier lookAssistEnabled (a child element inside it) does not end
// the scan before a later scalar opt-out on a receipt the walk accepts.
TEST(ReceiptLoader, AMalformedEarlierOptOutDoesNotHideALaterOne)
{
    ReceiptSettings receipt;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled><value/></lookAssistEnabled>"
        "<lookAssistEnabled>0</lookAssistEnabled></receipt>"), &receipt));
    ASSERT_FALSE(receipt.lookAssistEnabled());
}

// r4 (sol r3 B1 on 875d5e40): a lookAssistEnabled with no integer in it (a child element, an empty element, garbage
// text) is no value, never a valid 0: Look Assist keeps its documented default (on). The compact receipt makes the walk
// stop at </cutOut>, so the recovery scan is what reads the malformed element; the indented receipt is the walk's own
// handler reading it.
TEST(ReceiptLoader, AMalformedOptOutNeverSwitchesLookAssistOff)
{
    ReceiptSettings defaults;
    ASSERT_TRUE(defaults.lookAssistEnabled());
    const char *const compact[] = {
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled><value/></lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled></lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled/></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled>garbage</lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled>0x</lookAssistEnabled></receipt>",
    };
    for (const char *text : compact)
    {
        ReceiptSettings receipt;
        ASSERT_TRUE(loadReceiptText(QByteArray(text), &receipt));
        ASSERT_TRUE(receipt.lookAssistEnabled());
    }
    const char *const indented[] = {
        "<receipt version=\"4\">\n  <lookAssistEnabled></lookAssistEnabled>\n  <cutIn>2</cutIn>\n</receipt>\n",
        "<receipt version=\"4\">\n  <lookAssistEnabled/>\n  <cutIn>2</cutIn>\n</receipt>\n",
        "<receipt version=\"4\">\n  <lookAssistEnabled>garbage</lookAssistEnabled>\n  <cutIn>2</cutIn>\n</receipt>\n",
    };
    for (const char *text : indented)
    {
        ReceiptSettings receipt;
        ASSERT_TRUE(loadReceiptText(QByteArray(text), &receipt));
        ASSERT_TRUE(receipt.lookAssistEnabled());
        ASSERT_EQ(2u, receipt.cutIn());
    }
    // A child element inside the indented handler's element is the typed XML parse failure, as on base.
    ReceiptSettings nested;
    ASSERT_FALSE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\">\n  <lookAssistEnabled><value/></lookAssistEnabled>\n</receipt>\n"), &nested));
}

// r4 (r4 adversary A1 on 07462a6a): text around a child element (mixed content) is no scalar either. The scan read
// 0<value/> as 0 (the child skipped, the text kept): it switched Look Assist off, and a later 1<value/> hid a valid
// opt-out before it.
TEST(ReceiptLoader, AMixedContentOptOutIsNoValue)
{
    const char *const on[] = {
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled>0<value/></lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled><value/>0</lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled>0<value>1</value></lookAssistEnabled></receipt>",
    };
    for (const char *text : on)
    {
        ReceiptSettings receipt;
        ASSERT_TRUE(loadReceiptText(QByteArray(text), &receipt));
        ASSERT_TRUE(receipt.lookAssistEnabled());
    }
    ReceiptSettings off;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled>0</lookAssistEnabled>"
        "<lookAssistEnabled>1<value/></lookAssistEnabled></receipt>"), &off));
    ASSERT_FALSE(off.lookAssistEnabled());
}

// r4: a valid scalar keeps its meaning next to a malformed one, in either order and either layout; surrounding
// whitespace is still a value.
TEST(ReceiptLoader, AValidOptOutSurvivesAMalformedNeighbour)
{
    const char *const off[] = {
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled>0</lookAssistEnabled>"
        "<lookAssistEnabled>garbage</lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled/>"
        "<lookAssistEnabled>0</lookAssistEnabled></receipt>",
        "<receipt version=\"4\">\n<lookAssistEnabled>0</lookAssistEnabled>\n<lookAssistEnabled>garbage</lookAssistEnabled>\n"
        "</receipt>\n",
        "<receipt version=\"4\"><cutIn>1</cutIn><cutOut>1</cutOut><lookAssistEnabled> 0 </lookAssistEnabled></receipt>",
    };
    for (const char *text : off)
    {
        ReceiptSettings receipt;
        ASSERT_TRUE(loadReceiptText(QByteArray(text), &receipt));
        ASSERT_FALSE(receipt.lookAssistEnabled());
    }
}

// r6 (sol r5 B1 on feabee0a): a flag element nested inside another element is no value either. The compact walk steps
// past the outer element and its handler used to read the inner zero, which the scan (direct children only) could not
// undo; a later well-formed direct element is honoured by the scan's last-wins rule, toward off only.
TEST(ReceiptLoader, ANestedFlagIsNoValue)
{
    const char *const on[] = {
        "<receipt version=\"4\"><cutIn>1</cutIn><lookAssistEnabled><lookAssistEnabled>0</lookAssistEnabled>"
        "</lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><lookAssistEnabled><lookAssistEnabled>0</lookAssistEnabled>"
        "</lookAssistEnabled><lookAssistEnabled>1</lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><unknownFutureTag><lookAssistEnabled>0</lookAssistEnabled>"
        "</unknownFutureTag></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><unknownFutureTag><lookAssistEnabled>0</lookAssistEnabled>"
        "</unknownFutureTag><lookAssistEnabled>1</lookAssistEnabled></receipt>",
    };
    for (const char *text : on)
    {
        ReceiptSettings receipt;
        ASSERT_TRUE(loadReceiptText(QByteArray(text), &receipt));
        ASSERT_EQ(1u, receipt.cutIn());
        ASSERT_TRUE(receipt.lookAssistEnabled());
    }
    const char *const off[] = {
        "<receipt version=\"4\"><cutIn>1</cutIn><lookAssistEnabled><lookAssistEnabled>0</lookAssistEnabled>"
        "</lookAssistEnabled><lookAssistEnabled>0</lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><cutIn>1</cutIn><unknownFutureTag><lookAssistEnabled>1</lookAssistEnabled>"
        "</unknownFutureTag><lookAssistEnabled>0</lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><lookAssistEnabled>0</lookAssistEnabled><lookAssistEnabled>"
        "<lookAssistEnabled>1</lookAssistEnabled></lookAssistEnabled></receipt>",
    };
    for (const char *text : off)
    {
        ReceiptSettings receipt;
        ASSERT_TRUE(loadReceiptText(QByteArray(text), &receipt));
        ASSERT_FALSE(receipt.lookAssistEnabled());
    }
    // Indented, the handler reaches the outer element itself: a child element in it is the typed XML parse failure, as
    // on base.
    ReceiptSettings indented;
    ASSERT_FALSE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\">\n  <lookAssistEnabled><lookAssistEnabled>0</lookAssistEnabled></lookAssistEnabled>\n"
        "</receipt>\n"), &indented));
}

// r6 (adversary A1 on 624f97d7): the last plain direct element wins on or off in every layout. A direct 0, a nested 1
// the compact walk stopped in, then a direct 1 stayed off; so did a compact 0 then 1 (indented reads on), and an
// extension 0 after a plain 1 overwrote it.
TEST(ReceiptLoader, TheLastDirectFlagWinsInEveryLayout)
{
    const char *const on[] = {
        "<receipt version=\"4\"><lookAssistEnabled>0</lookAssistEnabled><unknownFutureTag><lookAssistEnabled>1"
        "</lookAssistEnabled></unknownFutureTag><lookAssistEnabled>1</lookAssistEnabled></receipt>",
        "<receipt version=\"4\"><lookAssistEnabled>0</lookAssistEnabled><lookAssistEnabled>1</lookAssistEnabled></receipt>",
        "<receipt version=\"4\" xmlns:x=\"urn:metadata\">\n<lookAssistEnabled>1</lookAssistEnabled>\n"
        "<x:lookAssistEnabled>0</x:lookAssistEnabled>\n</receipt>\n",
    };
    for (const char *text : on)
    {
        ReceiptSettings receipt;
        ASSERT_TRUE(loadReceiptText(QByteArray(text), &receipt));
        ASSERT_TRUE(receipt.lookAssistEnabled());
    }
    ReceiptSettings off;
    ASSERT_TRUE(loadReceiptText(QByteArrayLiteral(
        "<receipt version=\"4\"><lookAssistEnabled>1</lookAssistEnabled><lookAssistEnabled>0</lookAssistEnabled></receipt>"),
        &off));
    ASSERT_FALSE(off.lookAssistEnabled());
}

// The GUI's copy of the reader (MainWindow::readXmlElementsFromFile, not linked here) keeps the base walk and applies
// the same opt-out scan after it (r2 B1/B2), observing every pass at the loop bottom (r3 A1).
static const QString kOptOutScanCall = QStringLiteral(
    "        optOutWatch.endOfPass( Rxml );\n    }\n\n");
static const QString kOptOutApply = QStringLiteral(
    "    bool lookAssistDirect = true;\n"
    "    if( optOutWatch.lastDirectValue( Rxml, &lookAssistDirect ) )\n"
    "        receipt->setLookAssistEnabled( lookAssistDirect );");
// r4 (sol r3 B1): both handlers read the value through the scan's own rule, so an element with no integer in it keeps
// the current value in the walk exactly as in the scan. r6 (sol r5 B1): both handlers take a value only from a direct
// child of the receipt, so the GUI reader (no console seam) is pinned to the batch reader's guarded handler.
static const QString kOptOutHandler = QStringLiteral(
    "            optOutWatch.sawLookAssistEnabled( Rxml );\n"
    "            bool lookAssistOn = true;\n"
    "            const QString lookAssistText = Rxml->readElementText();\n"
    "            if( optOutWatch.atDirectChild()\n"
    "             && ReceiptXmlWalk::lookAssistEnabledValue( lookAssistText, &lookAssistOn ) )\n"
    "                receipt->setLookAssistEnabled( lookAssistOn );");

// r3 (r2 adversary A3 on 40efaff7): the opt-out scan re-reads the bytes the walk parsed, never the file, so a file
// saved between two reads cannot mix two versions. Every caller of the two element readers parses a QBuffer filled
// by one read of the file, and the helper opens no file.
static int countOf(const QString & text, const QString & needle)
{
    int n = 0;
    for (int at = text.indexOf(needle); at >= 0; at = text.indexOf(needle, at + needle.size())) ++n;
    return n;
}

TEST(ReceiptLoader, TheOptOutScanReadsTheWalksOwnBytes)
{
    QFile header(repo_file_path(QStringLiteral("src/batch/ReceiptXmlWalk.h")));
    ASSERT_TRUE(header.open(QIODevice::ReadOnly | QIODevice::Text));
    const QString helper = QString::fromUtf8(header.readAll());
    ASSERT_EQ(-1, helper.indexOf(QStringLiteral("QFile file")));
    ASSERT_EQ(-1, helper.indexOf(QStringLiteral(".open(")));
    ASSERT_TRUE(helper.indexOf(QStringLiteral("qobject_cast<QBuffer *>( reader->device() )")) >= 0);
    // r4 (r4 adversary A1): the scan reads the element token by token, so a child inside it is seen (readElementText
    // with SkipChildElements kept the text around a skipped child); A2's later scalar is still reached (pinned above).
    ASSERT_EQ(-1, helper.indexOf(QStringLiteral("readElementText(")));
    ASSERT_TRUE(helper.indexOf(QStringLiteral("!childElement && lookAssistEnabledValue( text, &on )")) >= 0);
    // r4 (sol r3 B1): the scan reads a child's value with the handlers' rule, never a bare toInt() of any text.
    ASSERT_TRUE(helper.indexOf(QStringLiteral("lookAssistEnabledValue( text, &on )")) >= 0);
    ASSERT_EQ(-1, helper.indexOf(QStringLiteral("text.toInt() != 0")));

    QFile loader(repo_file_path(QStringLiteral("src/batch/ReceiptLoader.cpp")));
    ASSERT_TRUE(loader.open(QIODevice::ReadOnly | QIODevice::Text));
    const QString batch = QString::fromUtf8(loader.readAll());
    ASSERT_EQ(1, countOf(batch, QStringLiteral("Rxml.setDevice(&receiptBuffer);")));
    ASSERT_EQ(0, countOf(batch, QStringLiteral("Rxml.setDevice(&file);")));

    QFile gui(repo_file_path(QStringLiteral("platform/qt/MainWindow.cpp")));
    ASSERT_TRUE(gui.open(QIODevice::ReadOnly | QIODevice::Text));
    const QString window = QString::fromUtf8(gui.readAll());
    // Session load, default receipt and import receipt: the three callers of readXmlElementsFromFile.
    ASSERT_EQ(3, countOf(window, QStringLiteral("readXmlElementsFromFile( &Rxml,")));
    ASSERT_EQ(1, countOf(window, QStringLiteral("Rxml.setDevice(&sessionBuffer);")));
    ASSERT_EQ(2, countOf(window, QStringLiteral("Rxml.setDevice(&receiptBuffer);")));
    ASSERT_EQ(0, countOf(window, QStringLiteral("Rxml.setDevice(&file);")));
}

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
    const int offset = body.indexOf(QStringLiteral("ReceiptXmlWalk::OptOutWatch optOutWatch( Rxml );"));
    const int walk = body.indexOf(QStringLiteral(
        "    while( !Rxml->atEnd() && !Rxml->isEndElement() )\n    {\n        Rxml->readNext();\n\n"
        "        if( Rxml->isStartElement() && Rxml->name() == QString( \"exposure\" ) )"));
    const int scan = body.indexOf(kOptOutScanCall);
    const int apply = body.indexOf(kOptOutApply);
    ASSERT_TRUE(body.indexOf(kOptOutHandler) >= 0);
    ASSERT_TRUE(offset >= 0);
    ASSERT_TRUE(walk > offset);
    ASSERT_TRUE(scan > walk);
    ASSERT_TRUE(scan > body.lastIndexOf(QStringLiteral("Rxml->readElementText();")));
    ASSERT_TRUE(apply > scan);
    ASSERT_EQ(-1, body.indexOf(QStringLiteral("continue;")));
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
    const int scan = source.indexOf(kOptOutScanCall);
    ASSERT_TRUE(scan >= 0);
    ASSERT_TRUE(source.indexOf(kOptOutApply) > scan);
    ASSERT_TRUE(source.indexOf(kOptOutHandler) >= 0);
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
