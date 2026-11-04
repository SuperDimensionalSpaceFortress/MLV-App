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
#include "../../platform/qt/VisualCaptureSourceGuard.h"

#include <QByteArray>
#include <QTemporaryDir>
#include <QThread>
#include <cstdio>

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
// r7 pass 1 A1: clip A's frame 615 with byte frame_offset+7167 edited FF -> FE after the captures (adversary's in-memory hashes:
// 08688fb28ee97125 unedited, 08688eb28ee96f72 edited)
const quint64 kEditedAHash = 0x08688eb28ee96f72ull;
quint64 clipBHash(int frame) { return 0xb000000000000000ull + static_cast<quint64>(2048 + frame); }

struct ClipHarness
{
    ClipLifecycleBarrier barrier;
    FakeClip a{ 1 };
    FakeClip b{ 2 };
    FakeClip *open = &a;
    int hashCalls = 0;
    int openBDuringHashCall = -1;   // when >= 0: the hash call with this ordinal opens B first (a clip change while hashing)
    bool sourceUnchanged = true;    // what the capture run's SourceWriteGuard reports
    int editSourceDuringHashCall = -1;   // when >= 0: the hash call with this ordinal reads bytes edited since the captures

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
            [this](bool * unchanged) { *unchanged = sourceUnchanged; },
            [this](int displayFrame, quint64 * hash, bool * ok) {
                if (hashCalls == openBDuringHashCall) openB();
                if (hashCalls == editSourceDuringHashCall) sourceUnchanged = false;
                ++hashCalls;
                *ok = true;
                *hash = open != &a ? clipBHash(displayFrame) : (sourceUnchanged ? kStaticAHash : kEditedAHash);
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
    // r7 pass 1 A1: the clip's bytes are held from the same point, and the binding asks the guard before and after each hash
    ASSERT_TRUE(source.contains(QStringLiteral("#include \"VisualCaptureSourceGuard.h\"")));
    // r8 (fable r7 key A1): the guard must match the stamp taken when the capture's clip was opened, never a stamp of its own
    const int guardAt = lf.indexOf(QStringLiteral(
        "const visual_capture_source_difference::SourceWriteGuard visualSourceGuard(\n"
        "            m_pMlvObject ? m_pMlvObject->file : nullptr, m_pMlvObject ? m_pMlvObject->filenum : 0,\n"
        "            visual_capture_source_difference::sameClip( visualOpenStampClip, visualCaptureClip ) ? m_clipOpenSourceStamp : QByteArray() );"));
    ASSERT_TRUE(guardAt > startAt);
    const QString stampClip = lf.mid(startAt, guardAt - startAt);
    ASSERT_TRUE(stampClip.contains(QStringLiteral("visualOpenStampClip.generation = m_clipOpenSourceStampGeneration;")));
    ASSERT_TRUE(stampClip.contains(QStringLiteral("visualOpenStampClip.object = m_clipOpenSourceStampObject;")));
    ASSERT_TRUE(lf.indexOf(QStringLiteral("processEvents(")) > guardAt);
    // r8 pass 1 A1: the ids also need the guard that has held this clip since its open
    // r10 (adversary r9 pass 3 A1): and only on a build whose guard excludes writers at all (Windows)
    ASSERT_TRUE(lf.mid(bindAt, 600).contains(QStringLiteral(
        "[&]( bool *unchanged ) { *unchanged = visual_capture_source_difference::SourceWriteGuard::excludesExistingWriters()\n"
        "                && visualSourceGuard.unchanged() && visualOpenGuard && visualOpenGuard->unchanged(); },")));
    ASSERT_TRUE(lf.contains(QStringLiteral(
        "visual_capture_source_difference::sameClip( visualOpenStampClip, visualCaptureClip )\n"
        "                ? m_clipOpenSourceGuard : std::shared_ptr<visual_capture_source_difference::SourceWriteGuard>();")));
}

// r8 (fable r7 key A1): every clip open stamps the new clip's files the moment it becomes m_pMlvObject, before anything can decode
// a frame of it (setMlvProcessing and the first render come after), and the stamp names that clip.
TEST(VisualCaptureSourceDifferenceWiring, EveryClipOpenStampsItsFilesBeforeAnyFrameIsDecoded)
{
    const QString source = QString(readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp")))
        .replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
    const QString assign = QStringLiteral(
        "    m_pMlvObject = new_MlvObject;\n"
        "    m_clipLifecycleBarrier.reopen();\n"
        "    recordClipOpenSourceStamp();\n");
    ASSERT_EQ(2, source.count(assign));
    ASSERT_EQ(2, source.count(QStringLiteral("m_pMlvObject = new_MlvObject;")));
    const int openMlvAt = source.indexOf(QStringLiteral("int MainWindow::openMlv( QString fileName )"));
    ASSERT_TRUE(openMlvAt >= 0);
    const int stampAt = source.indexOf(assign, openMlvAt);
    const int processingAt = source.indexOf(QStringLiteral("setMlvProcessing( m_pMlvObject, m_pProcessingObject );"), openMlvAt);
    ASSERT_TRUE(stampAt > openMlvAt && processingAt > stampAt);
    const int defAt = source.indexOf(QStringLiteral("void MainWindow::recordClipOpenSourceStamp()"));
    ASSERT_TRUE(defAt >= 0);
    const int defEnd = source.indexOf(QStringLiteral("\n}\n"), defAt);
    ASSERT_TRUE(defEnd > defAt);
    const QString def = source.mid(defAt, defEnd - defAt);
    ASSERT_TRUE(def.contains(QStringLiteral(
        "visual_capture_source_difference::SourceWriteGuard::openStamp( m_pMlvObject->file, m_pMlvObject->filenum )")));
    ASSERT_TRUE(def.contains(QStringLiteral(
        "m_clipOpenSourceStampGeneration = static_cast<quint64>( m_clipLifecycleBarrier.generation() );")));
    ASSERT_TRUE(def.contains(QStringLiteral("m_clipOpenSourceStampObject = static_cast<const void *>( m_pMlvObject );")));
    // r8 pass 1 A1: a visual-capture run HOLDS the clip from that point (share-read handles), released when the clip is freed
    // r10 (adversary r9 pass 3 A1): and only where the guard excludes writers (Windows)
    ASSERT_TRUE(def.contains(QStringLiteral(
        "if( visual_capture_source_difference::SourceWriteGuard::excludesExistingWriters()\n"
        "        && m_holdClipSourceFromOpen && m_pMlvObject && !m_clipOpenSourceStamp.isEmpty() )")));
    ASSERT_TRUE(def.contains(QStringLiteral("m_clipOpenSourceGuard = std::make_shared<visual_capture_source_difference::SourceWriteGuard>(")));
    ASSERT_TRUE(source.contains(QStringLiteral("m_holdClipSourceFromOpen = !options.visualCaptureDir.isEmpty();")));
    const int smokeAt = source.indexOf(QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"));
    const int holdAt = source.indexOf(QStringLiteral("m_holdClipSourceFromOpen = !options.visualCaptureDir.isEmpty();"));
    const int smokeOpenAt = source.indexOf(QStringLiteral("openMlvSet( QStringList() <<"), smokeAt);
    ASSERT_TRUE(smokeAt >= 0 && holdAt > smokeAt && smokeOpenAt > holdAt);
    const int freeAt = source.indexOf(QStringLiteral("bool MainWindow::freeActiveMlvObjectAfterLifecycleBarrier( const char *reason )"));
    const int freeCall = source.indexOf(QStringLiteral("freeMlvObject( m_pMlvObject );"), freeAt);
    const int releaseAt = source.indexOf(QStringLiteral("m_clipOpenSourceGuard.reset();"), freeAt);
    ASSERT_TRUE(freeAt >= 0 && releaseAt > freeAt && freeCall > releaseAt);
}

// r7 (adversary r7 pass 1 A1 on 1a9748b6): an unchanged clip token accepted a reread of EDITED bytes. The json's input: a static
// clip A captured at [600,608,615,623,630,638], then byte frame_offset+7167 of frame 615 changed FF -> FE and flushed before the
// deferred hashing, headers, lengths, object and generation preserved. 1a9748b6 gave the earlier identical captures differing ids
// ("differ" over a static display -> FROZEN_DISPLAY, counted=false).
TEST(VisualCaptureSourceDifference, AnEditOfTheClipsBytesAfterTheCapturesLendsNoSourceId)
{
    ClipHarness clips;
    const vcsd::ClipToken captureClip = clips.now();
    QVector<vcsd::CaptureClip> captures;
    for (int label : kA2Labels) captures.append(playCapture(label, clips.now()));
    clips.sourceUnchanged = false;   // the edit lands after the captures, before the hashing; the clip token is unchanged
    ASSERT_TRUE(vcsd::sameClip(clips.now(), captureClip));
    const vcsd::BoundSourceIds bound = clips.bind(captures, captureClip);
    assertNoSourceId(bound);
    ASSERT_EQ(0, clips.hashCalls);   // nothing is read from bytes that are not the captures' bytes
}

TEST(VisualCaptureSourceDifference, AnEditWhileHashingDiscardsThatHashAndEveryLaterOne)
{
    ClipHarness clips;
    const vcsd::ClipToken captureClip = clips.now();
    QVector<vcsd::CaptureClip> captures;
    for (int label : kA2Labels) captures.append(playCapture(label, clips.now()));
    clips.editSourceDuringHashCall = 2;   // the third distinct frame (615) is read after the edit
    const vcsd::BoundSourceIds bound = clips.bind(captures, captureClip);
    ASSERT_TRUE(bound.valid.at(0));
    ASSERT_TRUE(bound.valid.at(1));
    for (int k = 2; k < 6; ++k) ASSERT_FALSE(bound.valid.at(k));
    for (int k = 0; k < 6; ++k) ASSERT_TRUE(!bound.valid.at(k) || bound.hash.at(k) == kStaticAHash);
    ASSERT_TRUE(vcsd::decide(bound.playIdentities) == vcsd::Verdict::Unknown);
}

namespace
{

QString writeSourceFile(const QTemporaryDir & dir)
{
    const QString path = dir.filePath(QStringLiteral("source.bin"));
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::WriteOnly));
    QByteArray payload;
    for (int i = 0; i < 64; ++i) payload += QByteArray(56, '\0') + QByteArray(56, '\xff');
    ASSERT_EQ(7168, payload.size());
    ASSERT_EQ(qint64(7168), file.write(payload));
    file.close();
    return path;
}

} // namespace

TEST(VisualCaptureSourceGuard, NoWriterChangesTheBytesWhileTheGuardHoldsThem)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = writeSourceFile(dir);
    FILE * clipFile = std::fopen(QFile::encodeName(path).constData(), "rb");   // the clip's own read handle, as the MLV reader opens it
    ASSERT_TRUE(clipFile != nullptr);
    const QByteArray opened = vcsd::SourceWriteGuard::openStamp(&clipFile, 1);   // r8: as MainWindow stamps a clip it opens
    {
        const vcsd::SourceWriteGuard guard(&clipFile, 1, opened);
        if (!vcsd::SourceWriteGuard::excludesExistingWriters()) {   // r10: a guard that excludes no writer is never held
            ASSERT_FALSE(guard.held());
            ASSERT_FALSE(guard.unchanged());
            std::fclose(clipFile);
            return;
        }
        ASSERT_TRUE(guard.held());
        ASSERT_TRUE(guard.unchanged());
        // the json's edit: byte 7167 FF -> FE, flushed
        QFile writer(path);
        const bool writerOpened = writer.open(QIODevice::ReadWrite);
        if (writerOpened) {
            ASSERT_TRUE(writer.seek(7167));
            ASSERT_EQ(qint64(1), writer.write("\xfe", 1));
            ASSERT_TRUE(writer.flush());
            writer.close();
            ASSERT_FALSE(guard.unchanged());   // a platform that cannot refuse the writer still sees the bytes moved
        }
#ifdef Q_OS_WIN
        ASSERT_FALSE(writerOpened);   // Windows: the guard's handle shares read only, so no writer can open the clip
        ASSERT_TRUE(guard.unchanged());
#endif
    }
    // released: the clip is writable again (the guard holds the files only for the capture run)
    QFile writer(path);
    ASSERT_TRUE(writer.open(QIODevice::ReadWrite));
    writer.close();
    std::fclose(clipFile);
}

TEST(VisualCaptureSourceGuard, AGuardThatCannotBeTakenNeverSaysUnchanged)
{
    const vcsd::SourceWriteGuard none(nullptr, 0, QByteArray());
    ASSERT_FALSE(none.held());
    ASSERT_FALSE(none.unchanged());
    FILE * missing = nullptr;
    ASSERT_TRUE(vcsd::SourceWriteGuard::openStamp(&missing, 1).isEmpty());
    const vcsd::SourceWriteGuard nullFile(&missing, 1, vcsd::SourceWriteGuard::openStamp(&missing, 1));
    ASSERT_FALSE(nullFile.unchanged());
#ifdef Q_OS_WIN
    // a writer that already has the clip open: the guard cannot deny it, so it is not held
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = writeSourceFile(dir);
    QFile writer(path);
    ASSERT_TRUE(writer.open(QIODevice::ReadWrite));
    FILE * clipFile = std::fopen(QFile::encodeName(path).constData(), "rb");
    ASSERT_TRUE(clipFile != nullptr);
    {
        const vcsd::SourceWriteGuard guard(&clipFile, 1, vcsd::SourceWriteGuard::openStamp(&clipFile, 1));
        ASSERT_FALSE(guard.held());
        ASSERT_FALSE(guard.unchanged());
    }
    std::fclose(clipFile);
    writer.close();
#endif
}

// r8 (fable r7 key A1, adversary r7 A1 on 09973e83): the guard was stamped at the START of the capture block, after the measured
// Play had decoded frames the caches serve by memcpy with no file read (raw prefetch ring video_mlv.c:1709, raw RGB cache
// :6808/:6813, processed caches :2636/:7809/:7829). The adversary's input: frame 22 decoded (prefetch READY), then byte
// frame_offset+53 edited A0 -> 80, flushed and closed, all BEFORE the guard. 09973e83's guard stamped the edited file, said
// unchanged(), and the deferred hash read the edited bytes while the cache showed the old picture: two ids under one picture,
// "differ", FROZEN_DISPLAY counted=false. Expected: that frame carries no source id. The guard now matches the stamp of the clip's
// files taken when the clip was opened, before any decode.
namespace
{

// The clip opened, one frame decoded into a "cache" (its bytes as read then), and then the clip's bytes edited -- all before the
// guard. Returns the cached bytes of the frame so the caller can show the cache and the disk now disagree.
QByteArray decodeThenEditBeforeTheGuard(const QString & path, FILE * clipFile)
{
    QByteArray cached(7168, '\0');
    ASSERT_EQ(0, std::fseek(clipFile, 0, SEEK_SET));
    ASSERT_EQ(size_t(7168), std::fread(cached.data(), 1, 7168, clipFile));
    QFile writer(path);
    ASSERT_TRUE(writer.open(QIODevice::ReadWrite));   // no guard yet: the reader's "rb" handle shares writes
    ASSERT_TRUE(writer.seek(53));
    ASSERT_EQ(qint64(1), writer.write("\x80", 1));
    ASSERT_TRUE(writer.flush());
    writer.close();
    return cached;
}

} // namespace

TEST(VisualCaptureSourceGuard, AnEditAfterTheClipOpenedButBeforeTheGuardLeavesTheCapturesWithoutASourceId)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = writeSourceFile(dir);
    QThread::msleep(1100);   // the edit's write time must be a later tick than the file's creation on any file system
    FILE * clipFile = std::fopen(QFile::encodeName(path).constData(), "rb");
    ASSERT_TRUE(clipFile != nullptr);
    const QByteArray opened = vcsd::SourceWriteGuard::openStamp(&clipFile, 1);   // MainWindow::recordClipOpenSourceStamp
    ASSERT_FALSE(opened.isEmpty());
    const QByteArray cached = decodeThenEditBeforeTheGuard(path, clipFile);
    {
        QFile disk(path);
        ASSERT_TRUE(disk.open(QIODevice::ReadOnly));
        ASSERT_TRUE(disk.readAll() != cached);   // the cached picture's bytes are not the bytes on disk any more
    }
    {
        const vcsd::SourceWriteGuard guard(&clipFile, 1, opened);   // the capture block's guard, after the edit
        ASSERT_FALSE(guard.held());
        ASSERT_FALSE(guard.unchanged());
        // through the binding: no capture gets a source id and nothing is hashed, so no "differ" over identical pictures
        ClipHarness clips;
        const vcsd::ClipToken captureClip = clips.now();
        QVector<vcsd::CaptureClip> captures;
        for (int label : kA2Labels) captures.append(playCapture(label, clips.now()));
        clips.sourceUnchanged = guard.unchanged();
        const vcsd::BoundSourceIds bound = clips.bind(captures, captureClip);
        assertNoSourceId(bound);
        ASSERT_EQ(0, clips.hashCalls);
    }
    std::fclose(clipFile);
}

TEST(VisualCaptureSourceGuard, AClipUntouchedSinceItOpenedKeepsTheGuardAndItsSourceIds)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = writeSourceFile(dir);
    FILE * clipFile = std::fopen(QFile::encodeName(path).constData(), "rb");
    ASSERT_TRUE(clipFile != nullptr);
    const QByteArray opened = vcsd::SourceWriteGuard::openStamp(&clipFile, 1);
    QByteArray cached(7168, '\0');
    ASSERT_EQ(size_t(7168), std::fread(cached.data(), 1, 7168, clipFile));   // a decode before the guard, nothing edited
    {
        const vcsd::SourceWriteGuard guard(&clipFile, 1, opened);
        if (!vcsd::SourceWriteGuard::excludesExistingWriters()) {   // r10: a guard that excludes no writer is never held
            ASSERT_FALSE(guard.held());
            ASSERT_FALSE(guard.unchanged());
            std::fclose(clipFile);
            return;
        }
        ASSERT_TRUE(guard.held());
        ASSERT_TRUE(guard.unchanged());
        ClipHarness clips;
        const vcsd::ClipToken captureClip = clips.now();
        QVector<vcsd::CaptureClip> captures;
        for (int label : kA2Labels) captures.append(playCapture(label, clips.now()));
        clips.sourceUnchanged = guard.unchanged();
        const vcsd::BoundSourceIds bound = clips.bind(captures, captureClip);
        for (bool valid : bound.valid) ASSERT_TRUE(valid);
        ASSERT_TRUE(vcsd::decide(bound.playIdentities) == vcsd::Verdict::Identical);
    }
    std::fclose(clipFile);
}

// r8 pass 1 A1 on d9836349: a stamp alone is forgeable -- edit a byte, then restore the last-write FILETIME with SetFileTime: same
// identity, size and write time, so a stamp-only check passes over edited bytes. A visual-capture run therefore holds the clip from
// its open (MainWindow::recordClipOpenSourceStamp): while held, the edit itself cannot happen, so there is nothing to hide.
TEST(VisualCaptureSourceGuard, AClipHeldFromItsOpenCannotBeEditedSoNoWriteTimeCanHideAnEdit)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = writeSourceFile(dir);
    FILE * clipFile = std::fopen(QFile::encodeName(path).constData(), "rb");
    ASSERT_TRUE(clipFile != nullptr);
    const QByteArray opened = vcsd::SourceWriteGuard::openStamp(&clipFile, 1);
    {
        const vcsd::SourceWriteGuard heldFromOpen(&clipFile, 1, opened);   // taken at the open, before any decode
        if (!vcsd::SourceWriteGuard::excludesExistingWriters()) {   // r10: a guard that excludes no writer is never held
            ASSERT_FALSE(heldFromOpen.held());
            ASSERT_FALSE(heldFromOpen.unchanged());
            std::fclose(clipFile);
            return;
        }
        ASSERT_TRUE(heldFromOpen.held());
        QFile writer(path);
        const bool writerOpened = writer.open(QIODevice::ReadWrite);
#ifdef Q_OS_WIN
        ASSERT_FALSE(writerOpened);   // the adversary's edit (frame_offset+53 0x20 -> 0x21) cannot be written at all
#endif
        if (writerOpened) {
            // a platform that cannot refuse the writer: the edit moves ctime, which no utimes() can restore
            ASSERT_TRUE(writer.seek(53));
            ASSERT_EQ(qint64(1), writer.write("\x21", 1));
            writer.close();
            ASSERT_FALSE(heldFromOpen.unchanged());
        }
        // the capture's own guard taken later still shares read with the open guard
        const vcsd::SourceWriteGuard captureGuard(&clipFile, 1, opened);
#ifdef Q_OS_WIN
        ASSERT_TRUE(captureGuard.held());
        ASSERT_TRUE(captureGuard.unchanged() && heldFromOpen.unchanged());
#endif
    }
    std::fclose(clipFile);
}

TEST(VisualCaptureSourceGuard, AnOpenStampThatIsEmptyOrAnotherFilesIsNeverHeld)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = writeSourceFile(dir);
    const QString otherPath = dir.filePath(QStringLiteral("other.bin"));
    {
        QFile other(otherPath);
        ASSERT_TRUE(other.open(QIODevice::WriteOnly));
        ASSERT_EQ(qint64(4), other.write("abcd", 4));
    }
    FILE * clipFile = std::fopen(QFile::encodeName(path).constData(), "rb");
    FILE * otherFile = std::fopen(QFile::encodeName(otherPath).constData(), "rb");
    ASSERT_TRUE(clipFile != nullptr && otherFile != nullptr);
    {
        const vcsd::SourceWriteGuard unknown(&clipFile, 1, QByteArray());   // the open state is unknown: fail closed
        ASSERT_FALSE(unknown.held());
        ASSERT_FALSE(unknown.unchanged());
        const vcsd::SourceWriteGuard other(&clipFile, 1, vcsd::SourceWriteGuard::openStamp(&otherFile, 1));
        ASSERT_FALSE(other.held());
        ASSERT_FALSE(other.unchanged());
    }
    std::fclose(otherFile);
    std::fclose(clipFile);
}

// r10 (adversary r9 pass 3 A1 on 1c438b0c; hub ruling HUB-TICK 20261104T0536Z (a)). The json's input: a Linux uncompressed MLV
// whose writer took a MAP_SHARED writable mapping BEFORE the open stamp, then stored into one captured frame after the captures
// and before the deferred hash, with no flush, resize or remap. dup() excludes no writer, and that store can leave size, mtime
// and ctime unchanged, so 1c438b0c's POSIX guard held, said unchanged(), and the hash of edited bytes was bound to the earlier
// capture: identical captured frames got differing ids. A guard that cannot exclude an EXISTING writer now binds no source id
// at all; only the Windows guard (share-read reopen, which fails while any writer holds the file) is ever held. This test takes
// the non-Windows path on any build through the constructor's seam (platformExcludesWriters = false); the next test pins that
// the real non-Windows compile takes it too.
TEST(VisualCaptureSourceGuard, AGuardThatCannotExcludeAnExistingWriterBindsNoSourceId)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = writeSourceFile(dir);
    FILE * clipFile = std::fopen(QFile::encodeName(path).constData(), "rb");
    ASSERT_TRUE(clipFile != nullptr);
    const QByteArray opened = vcsd::SourceWriteGuard::openStamp(&clipFile, 1);
    ASSERT_FALSE(opened.isEmpty());
    {
        // the non-Windows path: the open guard and the capture guard, both on files nothing has written (the mapped store is
        // invisible to the stamp, so the stamp still matches: the guard must refuse on what it cannot exclude, not on the stamp)
        const vcsd::SourceWriteGuard openGuard(&clipFile, 1, opened, false);
        const vcsd::SourceWriteGuard captureGuard(&clipFile, 1, opened, false);
        ASSERT_TRUE(vcsd::SourceWriteGuard::openStamp(&clipFile, 1) == opened);
        ASSERT_FALSE(openGuard.held());
        ASSERT_FALSE(openGuard.unchanged());
        ASSERT_FALSE(captureGuard.held());
        ASSERT_FALSE(captureGuard.unchanged());
        // through the binding, as MainWindow asks it: no capture gets a source id, nothing is hashed, the verdict is unknown
        ClipHarness clips;
        const vcsd::ClipToken captureClip = clips.now();
        QVector<vcsd::CaptureClip> captures;
        for (int label : kA2Labels) captures.append(playCapture(label, clips.now()));
        clips.sourceUnchanged = vcsd::SourceWriteGuard::excludesExistingWriters()
            && captureGuard.unchanged() && openGuard.unchanged();
        clips.editSourceDuringHashCall = 2;   // the json's store lands while hashing: no hash may be read at all
        const vcsd::BoundSourceIds bound = clips.bind(captures, captureClip);
        assertNoSourceId(bound);
        ASSERT_EQ(0, clips.hashCalls);
    }
    if (vcsd::SourceWriteGuard::excludesExistingWriters())
    {
        // control (Windows): the same files, through the default constructor, are held and keep their ids
        const vcsd::SourceWriteGuard guard(&clipFile, 1, opened);
        ASSERT_TRUE(guard.held());
        ASSERT_TRUE(guard.unchanged());
    }
    std::fclose(clipFile);
}

// r10: the seam above is the non-Windows path only if the real non-Windows compile takes it. Pinned on the header's text, because
// this suite builds on Windows only: excludesExistingWriters() is false outside Q_OS_WIN, the constructor holds nothing then (even
// when a caller passes true), and unchanged() asks it too. MainWindow's two sites ask it as well (wiring tests above).
TEST(VisualCaptureSourceGuard, EveryNonWindowsBuildsGuardExcludesNoWriterSoItIsNeverHeld)
{
    const QString header = QString(readRepoFile(QStringLiteral("platform/qt/VisualCaptureSourceGuard.h")))
        .replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "    static constexpr bool excludesExistingWriters()\n"
        "    {\n"
        "#ifdef Q_OS_WIN\n"
        "        return true;\n"
        "#else\n"
        "        return false;\n"
        "#endif\n"
        "    }\n")));
    ASSERT_EQ(1, header.count(QStringLiteral("bool excludesExistingWriters()")));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "        bool held = excludesExistingWriters() && platformExcludesWriters && files != nullptr && count > 0;\n")));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "    bool unchanged() const { return excludesExistingWriters() && m_held && stamp() == m_stamp; }\n")));
    // m_held is assigned once, from `held`
    ASSERT_EQ(1, header.count(QStringLiteral("        m_held = ")));
    ASSERT_TRUE(header.contains(QStringLiteral("        m_held = held && ")));
#ifdef Q_OS_WIN
    ASSERT_TRUE(vcsd::SourceWriteGuard::excludesExistingWriters());
#else
    ASSERT_FALSE(vcsd::SourceWriteGuard::excludesExistingWriters());
#endif
}
