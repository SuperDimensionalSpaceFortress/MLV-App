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
#include "../../platform/qt/ClipLifecycleBarrier.h"

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

TEST(VisualCaptureSourceDifference, OnlyTheForcedAndUnforcedPlayKindsCarryASourceId)
{
    ASSERT_TRUE(vcsd::isPlayCaptureKind(QStringLiteral("play")));
    ASSERT_TRUE(vcsd::isPlayCaptureKind(QStringLiteral("play_unbound")));
    ASSERT_FALSE(vcsd::isPlayCaptureKind(QStringLiteral("stop")));
    ASSERT_FALSE(vcsd::isPlayCaptureKind(QStringLiteral("stop_unbound")));
    ASSERT_FALSE(vcsd::isPlayCaptureKind(QStringLiteral("paused")));
    ASSERT_FALSE(vcsd::isPlayCaptureKind(QStringLiteral("paused_unbound")));
    ASSERT_FALSE(vcsd::isPlayCaptureKind(QStringLiteral("Play")));
    ASSERT_FALSE(vcsd::isPlayCaptureKind(QString()));
}

// Master merge (2026-11-02): the capture block now takes forced "play" and unforced "play_unbound" captures, and the
// runner judges the frozen check on the unforced ones when present -- so both kinds are hashed and carry a source_id.
// The kind predicate lives in the header: test_dual_venue_evidence pins that the capture block holds no `return`.
TEST(VisualCaptureSourceDifferenceWiring, BothPlayCaptureKindsCarryASourceId)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString header = readRepoFile(QStringLiteral("platform/qt/VisualCaptureSourceDifference.h"));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "return kind == QStringLiteral(\"play\") || kind == QStringLiteral(\"play_unbound\");")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "if( !visual_capture_source_difference::isPlayCaptureKind( grab.kind ) ) continue;")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "if( visual_capture_source_difference::isPlayCaptureKind( grab.kind ) && grab.displayFrame >= 0 )")));
    ASSERT_FALSE(source.contains(QStringLiteral("if( grab.kind != QStringLiteral(\"play\") ) continue;")));
}

// r7 (adversary r6 pass 3 A2 on 3663c8a3): the capture path pumps events after the play captures (the post-stop drain at the
// old MainWindow.cpp:10698, the paused settle) and a QFileOpenEvent handled there replaces m_pMlvObject before the deferred
// source hashing. 3663c8a3 then hashed "the open clip at display_frame", so clip B's ids were attached to clip A's captures:
// static clip A read "differ" and the runner said FROZEN_DISPLAY, counted=false, for a healthy display. The ids are now bound
// to the clip each capture was taken from (generation of the clip-lifecycle barrier + the open object), fail closed.
namespace
{

struct FakeClip { int name = 0; };

// The json's input: a static 1000-frame clip A (every payload the same picture) and a second clip B of the same geometry
// whose frame i is a different picture per frame (encodeLj92(2048 + i, 12288)); six unforced play captures labelled 600..638.
const int kA2Labels[6] = { 600, 608, 615, 623, 630, 638 };
const quint64 kStaticAHash = 0xa0a0a0a0a0a0a0a0ull;
quint64 clipBHash(int frame) { return 0xb000000000000000ull + static_cast<quint64>(2048 + frame); }

struct ClipHarness
{
    ClipLifecycleBarrier barrier;
    FakeClip a{ 1 };
    FakeClip b{ 2 };
    FakeClip *open = &a;
    int hashCalls = 0;
    int openBDuringHashCall = -1;   // when >= 0: the hash call with this ordinal opens B first (a clip change while hashing)

    // what MainWindow does on a FileOpen: free the open clip behind the barrier, then open the new one
    void openB()
    {
        barrier.beginMutation();
        open = &b;
        barrier.reopen();
    }
    vcsd::ClipToken now() const { return vcsd::clipToken(barrier, open); }
    vcsd::BoundSourceIds bind(const QVector<vcsd::CaptureClip> & captures, const vcsd::ClipToken & captureClip)
    {
        return vcsd::bindSourceIds(captures, captureClip, barrier, open,
            [this](int displayFrame, quint64 * hash, bool * ok) {
                if (hashCalls++ == openBDuringHashCall) openB();
                *ok = true;
                *hash = open == &a ? kStaticAHash : clipBHash(displayFrame);
            });
    }
};

vcsd::CaptureClip playCapture(int displayFrame, const vcsd::ClipToken & clip)
{
    vcsd::CaptureClip capture;
    capture.kind = QStringLiteral("play_unbound");
    capture.displayFrame = displayFrame;
    capture.clip = clip;
    return capture;
}

void assertNoSourceId(const vcsd::BoundSourceIds & bound)
{
    for (bool valid : bound.valid) ASSERT_FALSE(valid);
    ASSERT_TRUE(vcsd::decide(bound.playIdentities) == vcsd::Verdict::Unknown);
    QJsonObject visualJson;
    vcsd::apply(visualJson, vcsd::decide(bound.playIdentities));
    ASSERT_FALSE(visualJson.contains(QStringLiteral("source_difference")));
}

} // namespace

TEST(VisualCaptureSourceDifference, AFileOpenBeforeTheDeferredHashLendsNoSourceIdOfTheNewClip)
{
    ClipHarness clips;
    const vcsd::ClipToken captureClip = clips.now();
    QVector<vcsd::CaptureClip> captures;
    for (int label : kA2Labels) captures.append(playCapture(label, clips.now()));
    // control: with A still open the static clip reads identical (the frozen gate is waived on the app's word, correctly)
    ASSERT_TRUE(vcsd::decide(clips.bind(captures, captureClip).playIdentities) == vcsd::Verdict::Identical);
    // the processEvents after Play stops delivers a QFileOpenEvent for clip B before the hashing
    clips.openB();
    ASSERT_FALSE(vcsd::sameClip(clips.now(), captureClip));
    const vcsd::BoundSourceIds bound = clips.bind(captures, captureClip);
    ASSERT_EQ(6, bound.valid.size());
    ASSERT_EQ(6, bound.playIdentities.size());
    assertNoSourceId(bound);   // 3663c8a3: six of B's ids on A's pictures, "differ"
    ASSERT_TRUE(bound.clipChangedCaptures > 0);
}

TEST(VisualCaptureSourceDifference, AFileOpenBetweenTwoCapturesLendsNoSourceIdToEither)
{
    ClipHarness clips;
    const vcsd::ClipToken captureClip = clips.now();
    QVector<vcsd::CaptureClip> captures;
    for (int k = 0; k < 6; ++k) {
        if (k == 3) clips.openB();   // a tail wait between play captures 2 and 3 handles the FileOpen
        captures.append(playCapture(kA2Labels[k], clips.now()));
    }
    assertNoSourceId(clips.bind(captures, captureClip));
}

TEST(VisualCaptureSourceDifference, AClipChangeWhileHashingDiscardsThatHashAndEveryLaterOne)
{
    ClipHarness clips;
    const vcsd::ClipToken captureClip = clips.now();
    QVector<vcsd::CaptureClip> captures;
    for (int label : kA2Labels) captures.append(playCapture(label, clips.now()));
    clips.openBDuringHashCall = 2;
    const vcsd::BoundSourceIds bound = clips.bind(captures, captureClip);
    ASSERT_TRUE(bound.valid.at(0));
    ASSERT_TRUE(bound.valid.at(1));
    ASSERT_EQ(kStaticAHash, bound.hash.at(0));
    ASSERT_EQ(kStaticAHash, bound.hash.at(1));
    for (int k = 2; k < 6; ++k) ASSERT_FALSE(bound.valid.at(k));
    ASSERT_TRUE(vcsd::decide(bound.playIdentities) == vcsd::Verdict::Unknown);
}

TEST(VisualCaptureSourceDifference, AClipThatStaysOpenKeepsEveryIdentityAndOnlyPlayKindsAreHashed)
{
    ClipHarness clips;
    const vcsd::ClipToken captureClip = clips.now();
    QVector<vcsd::CaptureClip> captures;
    captures.append(playCapture(10, clips.now()));
    vcsd::CaptureClip stop = playCapture(11, clips.now());
    stop.kind = QStringLiteral("stop");
    captures.append(stop);
    captures.append(playCapture(10, clips.now()));
    captures.append(playCapture(12, clips.now()));
    const vcsd::BoundSourceIds bound = clips.bind(captures, captureClip);
    ASSERT_EQ(4, bound.valid.size());
    ASSERT_EQ(3, bound.playIdentities.size());
    ASSERT_TRUE(bound.valid.at(0));
    ASSERT_FALSE(bound.valid.at(1));   // not a play kind: no source_id
    ASSERT_TRUE(bound.valid.at(2));
    ASSERT_TRUE(bound.valid.at(3));
    ASSERT_EQ(2, clips.hashCalls);     // one hash per distinct display frame
    ASSERT_EQ(0, bound.clipChangedCaptures);
}

TEST(VisualCaptureSourceDifferenceWiring, TheCaptureRunBindsItsSourceIdsToTheClipItStartedOn)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString block = sliceBetween(source,
        QStringLiteral("PLAYBACK-VISUAL-INSPECTION-1 (--visual-capture-dir"),
        QStringLiteral("gui_smoke.visual_capture\")"));
    ASSERT_FALSE(block.isEmpty());
    const QString lf = QString(block).replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
    const int startAt = lf.indexOf(QStringLiteral(
        "const visual_capture_source_difference::ClipToken visualCaptureClip =\n"
        "            visual_capture_source_difference::clipToken( m_clipLifecycleBarrier, m_pMlvObject );"));
    ASSERT_TRUE(startAt >= 0);
    // named before ANY event is pumped on the capture path
    ASSERT_TRUE(lf.indexOf(QStringLiteral("processEvents(")) > startAt);
    ASSERT_TRUE(lf.indexOf(QStringLiteral("finishPlaybackSmokeTelemetry( \"visual-capture\" );")) > startAt);
    // every capture records its own clip
    const int grabAt = lf.indexOf(QStringLiteral("auto grabVisual = [&]"));
    const int grabEnd = lf.indexOf(QStringLiteral("visualGrabs.append( grab );"), grabAt);
    ASSERT_TRUE(grabAt >= 0 && grabEnd > grabAt);
    ASSERT_TRUE(lf.mid(grabAt, grabEnd - grabAt).contains(QStringLiteral(
        "grab.clip = visual_capture_source_difference::clipToken( m_clipLifecycleBarrier, m_pMlvObject );")));
    // the ONE source read goes through the clip binding; no second read can attach another clip's identity
    ASSERT_EQ(1, lf.count(QStringLiteral("getMlvFrameSourceHash64(")));
    const int bindAt = lf.indexOf(QStringLiteral("visual_capture_source_difference::bindSourceIds("));
    ASSERT_TRUE(bindAt >= 0);
    ASSERT_TRUE(lf.indexOf(QStringLiteral("getMlvFrameSourceHash64(")) > bindAt);
    ASSERT_TRUE(lf.mid(bindAt, 200).contains(QStringLiteral("visualCaptureClips, visualCaptureClip, m_clipLifecycleBarrier, m_pMlvObject,")));
    // the source_id written comes from that binding, not from a per-frame lookup on the open clip
    ASSERT_TRUE(lf.contains(QStringLiteral("visual_capture_source_difference::sourceIdHex( visualSourceIds.hash.at( playAt ) )")));
    ASSERT_TRUE(lf.contains(QStringLiteral("visual_capture_source_difference::decide( visualSourceIds.playIdentities );")));
    // the paused settle is never sought in, or credited to, a clip opened since
    const int settleAt = lf.indexOf(QStringLiteral("seekAndSettleLoadedClip( visualStopFrame"));
    const int settleFrom = lf.lastIndexOf(QStringLiteral("const bool visualSettled"), settleAt);
    ASSERT_TRUE(settleAt >= 0 && settleFrom >= 0);
    const QString settleGuard = lf.mid(settleFrom, settleAt - settleFrom);
    ASSERT_TRUE(settleGuard.contains(QStringLiteral("sameClip( visualGrabs.last().clip, visualCaptureClip )")));
    ASSERT_TRUE(settleGuard.contains(QStringLiteral("clipToken( m_clipLifecycleBarrier, m_pMlvObject ), visualCaptureClip )")));
    ASSERT_TRUE(lf.contains(QStringLiteral(
        "grab.settled && visual_capture_source_difference::sameClip( grab.clip, visualCaptureClip ) );")));
}
