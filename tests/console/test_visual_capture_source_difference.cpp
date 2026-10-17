// PLAYBACK-SOURCE-FRAME-IDENTITY-1: pins visual.json's "source_difference" field, which the
// dual-venue runner (DualVenueRunner.psm1 Get-DvVisualVerdict) reads to tell a FROZEN display
// (the source frames differ but one picture is shown) from a static clip (the source frames
// are identical). The runner accepts only the scalar lowercase JSON strings "differ" and
// "identical"; anything else, absence included, is unknown. "identical" waives the frozen
// gate, so the app writes it only on positive evidence: >= 2 play frames, every one with a
// source identity, on >= 2 distinct display frames (a repeated index is a stuck clock, not a
// static clip). The policy is visual_capture_source_difference (header-only); the wiring
// test reads MainWindow.cpp as text (it is not linked into console_tests), like
// test_contact_sheet_capture_wiring.cpp, and pins that the source hashes are read after the
// tail's Play has stopped and the verdict is applied before visual.json is written.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/VisualCaptureSourceDifference.h"

#include <QFile>
#include <QJsonObject>
#include <QJsonValue>
#include <QString>
#include <QTextStream>
#include <QVector>

#include <string>

// minitest streams its operands, which a QString cannot do; compare as UTF-8 std::string.
#define ASSERT_QSTR_EQ(EXPECTED, ACTUAL) \
    ASSERT_EQ(std::string((EXPECTED).toStdString()), std::string((ACTUAL).toStdString()))

namespace
{

namespace vcsd = visual_capture_source_difference;

vcsd::PlayIdentity identity(int displayFrame, quint64 hash, bool valid = true)
{
    vcsd::PlayIdentity id;
    id.displayFrame = displayFrame;
    id.valid = valid;
    id.hash = hash;
    return id;
}

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString sliceBetween(const QString & source, const QString & fromMarker, const QString & toMarker)
{
    const int at = source.indexOf(fromMarker);
    if (at < 0) return QString();
    const int next = source.indexOf(toMarker, at);
    if (next <= at) return QString();
    return source.mid(at, next - at);
}

void assertOmitted(const QVector<vcsd::PlayIdentity> & play)
{
    ASSERT_TRUE(vcsd::decide(play) == vcsd::Verdict::Unknown);
    QJsonObject visualJson;
    visualJson.insert(QStringLiteral("source_difference"), QStringLiteral("differ"));
    vcsd::apply(visualJson, vcsd::decide(play));
    ASSERT_FALSE(visualJson.contains(QStringLiteral("source_difference")));
}

} // namespace

TEST(VisualCaptureSourceDifference, DistinctFramesWithDifferentHashesWriteTheStringDiffer)
{
    const QVector<vcsd::PlayIdentity> play{ identity(10, 0x1111), identity(14, 0x2222) };
    ASSERT_TRUE(vcsd::decide(play) == vcsd::Verdict::Differ);
    QJsonObject visualJson;
    vcsd::apply(visualJson, vcsd::decide(play));
    ASSERT_TRUE(visualJson.contains(QStringLiteral("source_difference")));
    const QJsonValue value = visualJson.value(QStringLiteral("source_difference"));
    ASSERT_TRUE(value.isString());
    ASSERT_QSTR_EQ(QStringLiteral("differ"), value.toString());
}

TEST(VisualCaptureSourceDifference, AnyDifferingHashAmongManyIsDiffer)
{
    // Equal hashes on a repeated index next to one moved frame are still a difference.
    const QVector<vcsd::PlayIdentity> play{ identity(3, 7), identity(3, 7), identity(5, 7), identity(9, 8) };
    ASSERT_TRUE(vcsd::decide(play) == vcsd::Verdict::Differ);
}

TEST(VisualCaptureSourceDifference, DistinctFramesWithEqualHashesWriteTheStringIdentical)
{
    const QVector<vcsd::PlayIdentity> play{ identity(0, 0xabcdef), identity(4, 0xabcdef), identity(8, 0xabcdef) };
    ASSERT_TRUE(vcsd::decide(play) == vcsd::Verdict::Identical);
    QJsonObject visualJson;
    vcsd::apply(visualJson, vcsd::decide(play));
    const QJsonValue value = visualJson.value(QStringLiteral("source_difference"));
    ASSERT_TRUE(value.isString());
    ASSERT_QSTR_EQ(QStringLiteral("identical"), value.toString());
}

TEST(VisualCaptureSourceDifference, AnInvalidIdentityOmitsTheField)
{
    assertOmitted({ identity(1, 5), identity(2, 6, false) });
    assertOmitted({ identity(1, 5, false), identity(2, 5) });
}

TEST(VisualCaptureSourceDifference, ANegativeDisplayFrameOmitsTheField)
{
    assertOmitted({ identity(-1, 5), identity(2, 6) });
    assertOmitted({ identity(3, 5), identity(-1, 5) });
}

TEST(VisualCaptureSourceDifference, OneRepeatedDisplayFrameIsAStuckClockNotAStaticClip)
{
    assertOmitted({ identity(7, 42), identity(7, 42), identity(7, 42) });
}

TEST(VisualCaptureSourceDifference, FewerThanTwoPlayFramesOmitTheField)
{
    assertOmitted({});
    assertOmitted({ identity(7, 42) });
}

TEST(VisualCaptureSourceDifference, SourceIdIsSixteenLowercaseHexDigits)
{
    ASSERT_QSTR_EQ(QStringLiteral("0000000000000001"), vcsd::sourceIdHex(0x1ull));
    ASSERT_QSTR_EQ(QStringLiteral("00000000deadbeef"), vcsd::sourceIdHex(0xDEADBEEFull));
    ASSERT_QSTR_EQ(QStringLiteral("ffffffffffffffff"), vcsd::sourceIdHex(~0ull));
}

TEST(VisualCaptureSourceDifferenceWiring, SourceHashesAreReadAfterPlayStopsAndAppliedBeforeVisualJsonIsWritten)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString block = sliceBetween(source,
        QStringLiteral("PLAYBACK-VISUAL-INSPECTION-1 (--visual-capture-dir"),
        QStringLiteral("gui_smoke.visual_capture\")"));
    ASSERT_FALSE(block.isEmpty());
    ASSERT_TRUE(source.contains(QStringLiteral("#include \"VisualCaptureSourceDifference.h\"")));

    const int playStopped = block.indexOf(QStringLiteral("ui->actionPlay->setChecked( false );"));
    const int firstHash = block.indexOf(QStringLiteral("getMlvFrameSourceHash64("));
    const int applyAt = block.indexOf(QStringLiteral("visual_capture_source_difference::apply("));
    const int decideAt = block.indexOf(QStringLiteral("visual_capture_source_difference::decide("));
    const int sourceIdAt = block.indexOf(QStringLiteral("QStringLiteral(\"source_id\")"));
    const int jsonWrite = block.indexOf(QStringLiteral("QStringLiteral(\"visual.json\")"));
    ASSERT_TRUE(playStopped >= 0);
    ASSERT_TRUE(firstHash >= 0);
    ASSERT_TRUE(applyAt >= 0);
    ASSERT_TRUE(decideAt >= 0);
    ASSERT_TRUE(sourceIdAt >= 0);
    ASSERT_TRUE(jsonWrite >= 0);
    // The source reads cost nothing inside the timed window or the untimed tail's play grabs.
    ASSERT_TRUE(firstHash > playStopped);
    ASSERT_TRUE(sourceIdAt > playStopped);
    // The verdict is in visual.json when it is written.
    ASSERT_TRUE(applyAt < jsonWrite);
    // No second Play stop or start was added to the capture block.
    ASSERT_EQ(1, block.count(QStringLiteral("ui->actionPlay->setChecked(")));
}
