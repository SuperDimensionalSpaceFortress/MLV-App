/*!
 * \file test_upstream_sync_hardening.cpp
 * \brief The upstream-sync (PR #132) hardening exercised through the PRODUCTION
 *        entry points - openMlvClip() and the playback frame reader - rather
 *        than through the exported bounds helpers alone. Each test is written
 *        so that reverting the production wiring it guards turns it red.
 *
 * Every clip below is bytes this file constructs and writes into a
 * QTemporaryDir. No footage is read and nothing is checked in. The synthetic
 * files deliberately do not carry a real clip extension: load_all_chunks()
 * then skips its M00/M01 sequence probe, which is what a single-chunk fixture
 * wants.
 *
 * The pipeline suite defines ENABLE_JPEG2K (see pipeline_tests.pro) so that
 * video_mlv.c compiles its real JPEG2000 frame-read branch. Only the
 * third-party codec entry points are replaced, by ojph_decoder_test_stub.cpp;
 * nothing under src/ is stubbed.
 */

#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "ojph_decoder_test_stub.h"

extern "C" {
#include "../../src/mlv/video_mlv.h"
}

#include <QByteArray>
#include <QDir>
#include <QFile>
#include <QString>
#include <QTemporaryDir>

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>

namespace {

const int kSyntheticWidth = 8;
const int kSyntheticHeight = 8;
const int kSyntheticBitdepth = 14;
/* mlvRawFrameInputCapacity() admits an encoded frame of at most
 * pixels * 3 + 200 bytes; the packed RAW frame is pixels * bitdepth / 8. */
const uint32_t kSyntheticPackedSize = 112u;     /* 64 * 14 / 8 */
const uint32_t kSyntheticEncodedCeiling = 392u; /* 64 * 3 + 200 */

QByteArray syntheticMlviBlock(uint16_t videoClass, uint32_t videoFrameCount)
{
    mlv_file_hdr_t header;
    std::memset(&header, 0, sizeof header);
    std::memcpy(header.fileMagic, "MLVI", 4);
    header.blockSize = static_cast<uint32_t>(sizeof(mlv_file_hdr_t));
    std::memcpy(header.versionString, "v2.0", 4);
    header.fileNum = 0;
    header.fileCount = 1;
    header.videoClass = videoClass;
    header.audioClass = 0;
    header.videoFrameCount = videoFrameCount;
    header.audioFrameCount = 0;
    header.sourceFpsNom = 25000;
    header.sourceFpsDenom = 1000;
    return QByteArray(reinterpret_cast<const char *>(&header), sizeof header);
}

QByteArray syntheticRawiBlock(uint16_t xRes, uint16_t yRes, int32_t bitsPerPixel)
{
    mlv_rawi_hdr_t rawi;
    std::memset(&rawi, 0, sizeof rawi);
    std::memcpy(rawi.blockType, "RAWI", 4);
    rawi.blockSize = static_cast<uint32_t>(sizeof(mlv_rawi_hdr_t));
    rawi.timestamp = 1000;
    rawi.xRes = xRes;
    rawi.yRes = yRes;
    rawi.raw_info.api_version = 1;
    rawi.raw_info.width = xRes;
    rawi.raw_info.height = yRes;
    rawi.raw_info.pitch = (xRes * bitsPerPixel) / 8;
    rawi.raw_info.frame_size = (xRes * yRes * bitsPerPixel) / 8;
    rawi.raw_info.bits_per_pixel = bitsPerPixel;
    rawi.raw_info.black_level = 512;
    rawi.raw_info.white_level = 15000;
    rawi.raw_info.active_area.y1 = 0;
    rawi.raw_info.active_area.x1 = 0;
    rawi.raw_info.active_area.y2 = yRes;
    rawi.raw_info.active_area.x2 = xRes;
    rawi.raw_info.cfa_pattern = 0x02010100;
    rawi.raw_info.calibration_illuminant1 = 21;
    return QByteArray(reinterpret_cast<const char *>(&rawi), sizeof rawi);
}

QByteArray syntheticVidfBlock(const QByteArray & payload, uint32_t frameNumber)
{
    mlv_vidf_hdr_t vidf;
    std::memset(&vidf, 0, sizeof vidf);
    std::memcpy(vidf.blockType, "VIDF", 4);
    vidf.blockSize = static_cast<uint32_t>(sizeof(mlv_vidf_hdr_t))
        + static_cast<uint32_t>(payload.size());
    vidf.timestamp = 2000;
    vidf.frameNumber = frameNumber;
    vidf.frameSpace = 0;
    QByteArray block(reinterpret_cast<const char *>(&vidf), sizeof vidf);
    block.append(payload);
    return block;
}

/* A CURV block whose declared blockSize is exactly the bytes written, so the
 * only thing the parser can object to is the size arithmetic itself. */
QByteArray syntheticCurvBlock(uint32_t blockSize)
{
    mlv_curv_hdr_t curv;
    std::memset(&curv, 0, sizeof curv);
    std::memcpy(curv.blockType, "CURV", 4);
    curv.blockSize = blockSize;
    curv.timestamp = 1500;
    QByteArray block(reinterpret_cast<const char *>(&curv), sizeof curv);
    if (blockSize > sizeof(mlv_curv_hdr_t))
    {
        const int payload = static_cast<int>(blockSize - sizeof(mlv_curv_hdr_t));
        block.append(QByteArray(payload, '\x01'));
    }
    return block;
}

/* version(u32) plus an offset/size u32 pair per quarter-resolution channel. */
QByteArray syntheticJpeg2kPayload(uint32_t frameSize,
                                  const uint32_t offsets[4],
                                  const uint32_t sizes[4],
                                  uint32_t layoutVersion = 1u)
{
    QByteArray payload(static_cast<int>(frameSize), '\x5a');
    uint32_t table[9];
    table[0] = layoutVersion;
    for (int c = 0; c < 4; ++c)
    {
        table[1 + 2 * c] = offsets[c];
        table[2 + 2 * c] = sizes[c];
    }
    std::memcpy(payload.data(), table, sizeof table);
    return payload;
}

bool writeSyntheticClip(const QString & path, const QByteArray & bytes)
{
    QFile file(path);
    if (!file.open(QIODevice::WriteOnly)) return false;
    const qint64 written = file.write(bytes);
    file.close();
    return written == static_cast<qint64>(bytes.size());
}

/* Owns an mlvObject_t opened from a synthetic clip through the real
 * openMlvClip(), and keeps the error code and message that open produced. */
class SyntheticClip
{
public:
    SyntheticClip(const QString & path, int open_mode)
        : m_video(initMlvObject())
        , m_err(MLV_ERR_NONE)
    {
        std::memset(m_message, 0, sizeof m_message);
        QByteArray native = QDir::toNativeSeparators(path).toLocal8Bit();
        m_err = openMlvClip(m_video, native.data(), open_mode, m_message);
    }

    ~SyntheticClip()
    {
        if (m_video) freeMlvObject(m_video);
    }

    SyntheticClip(const SyntheticClip &) = delete;
    SyntheticClip & operator=(const SyntheticClip &) = delete;

    int err() const { return m_err; }
    const char * message() const { return m_message; }
    mlvObject_t * video() const { return m_video; }

    /* The MLV headers are #pragma pack(1), and a reference may not be bound to
     * a packed field, so every assertion reads a plain copy. */
    uint32_t indexedFrameSize(std::size_t index) const
    {
        return m_video->video_index[index].frame_size;
    }
    uint32_t vidfFrameNumber() const { return m_video->VIDF.frameNumber; }
    void setVidfFrameNumber(uint32_t value) { m_video->VIDF.frameNumber = value; }

private:
    mlvObject_t * m_video;
    int m_err;
    char m_message[256];
};

/* MLVI + RAWI + VIDF, in that order, so a preview open reaches RAWI before it
 * stops at the first video frame. */
QByteArray syntheticJpeg2kClip(const QByteArray & framePayload)
{
    QByteArray clip = syntheticMlviBlock(
        static_cast<uint16_t>(MLV_VIDEO_CLASS_RAW | MLV_VIDEO_CLASS_FLAG_JPEG2K), 1u);
    clip.append(syntheticRawiBlock(static_cast<uint16_t>(kSyntheticWidth),
                                   static_cast<uint16_t>(kSyntheticHeight),
                                   kSyntheticBitdepth));
    clip.append(syntheticVidfBlock(framePayload, 0u));
    return clip;
}

std::string readSourceFile(const QString & relative_path)
{
    QFile file(repo_file_path(relative_path));
    if (!file.open(QIODevice::ReadOnly)) return std::string();
    const QByteArray bytes = file.readAll();
    return std::string(bytes.constData(), static_cast<std::size_t>(bytes.size()));
}

} // namespace

/* Item 2(a): a CURV block the reader cannot honour must fail openMlvClip(),
 * not be truncated into a curve that silently mis-linearises every frame. */
TEST(UpstreamSyncHardening, MalformedCurvBlockFailsTheRealClipOpen)
{
    QTemporaryDir temporary;
    ASSERT_TRUE(temporary.isValid());

    const uint32_t curvHeader = static_cast<uint32_t>(sizeof(mlv_curv_hdr_t));

    auto buildClip = [&](uint32_t curvBlockSize) {
        QByteArray clip = syntheticMlviBlock(MLV_VIDEO_CLASS_RAW, 1u);
        clip.append(syntheticRawiBlock(static_cast<uint16_t>(kSyntheticWidth),
                                       static_cast<uint16_t>(kSyntheticHeight),
                                       kSyntheticBitdepth));
        clip.append(syntheticCurvBlock(curvBlockSize));
        clip.append(syntheticVidfBlock(
            QByteArray(static_cast<int>(kSyntheticPackedSize), '\x00'), 0u));
        return clip;
    };

    /* Control: a curve the table can hold opens cleanly, so the failures below
     * are attributable to the CURV size and not to the synthetic container. */
    const QString wellFormedPath = temporary.filePath("curv-well-formed.testclip");
    ASSERT_TRUE(writeSyntheticClip(wellFormedPath, buildClip(curvHeader + 512u)));
    {
        SyntheticClip clip(wellFormedPath, MLV_OPEN_FULL);
        ASSERT_EQ(static_cast<int>(MLV_ERR_NONE), clip.err());
        ASSERT_TRUE(clip.video()->linearise_lut != nullptr);
    }

    /* More entries than the linearisation table is allocated for. */
    const QString oversizedPath = temporary.filePath("curv-oversized.testclip");
    ASSERT_TRUE(writeSyntheticClip(
        oversizedPath, buildClip(curvHeader + (MLV_LINEARISE_LUT_ENTRIES + 1u) * 2u)));
    {
        SyntheticClip clip(oversizedPath, MLV_OPEN_FULL);
        ASSERT_EQ(static_cast<int>(MLV_ERR_CORRUPTED), clip.err());
        ASSERT_TRUE(std::string(clip.message()).find("CURV") != std::string::npos);
    }

    /* A payload that is not a whole number of uint16 samples. */
    const QString oddPath = temporary.filePath("curv-odd-payload.testclip");
    ASSERT_TRUE(writeSyntheticClip(oddPath, buildClip(curvHeader + 3u)));
    {
        SyntheticClip clip(oddPath, MLV_OPEN_FULL);
        ASSERT_EQ(static_cast<int>(MLV_ERR_CORRUPTED), clip.err());
        ASSERT_TRUE(std::string(clip.message()).find("CURV") != std::string::npos);
    }

    /* An odd payload one byte past the allocation is rejected for both reasons
     * at once, and still never truncated. */
    const QString oddOversizedPath = temporary.filePath("curv-odd-oversized.testclip");
    ASSERT_TRUE(writeSyntheticClip(
        oddOversizedPath, buildClip(curvHeader + MLV_LINEARISE_LUT_ENTRIES * 2u + 1u)));
    {
        SyntheticClip clip(oddOversizedPath, MLV_OPEN_FULL);
        ASSERT_EQ(static_cast<int>(MLV_ERR_CORRUPTED), clip.err());
    }
}

/* Item 2(b): the JPEG2000 class must be sized by the same encoded-frame
 * capacity gate LJ92 gets, before any frame byte is read. */
TEST(UpstreamSyncHardening, Jpeg2000FrameSizeIsGatedByTheAdmittedCapacity)
{
    QTemporaryDir temporary;
    ASSERT_TRUE(temporary.isValid());

    const OjphStubPlan plan = ojphStubQuarterFramePlan(
        static_cast<uint32_t>(kSyntheticWidth / 2),
        static_cast<uint32_t>(kSyntheticHeight / 2));

    const uint32_t oversized = kSyntheticEncodedCeiling + 1u;
    const uint32_t bigOffsets[4] = {36u, 126u, 216u, 306u};
    const uint32_t bigSizes[4] = {90u, 90u, 90u, 87u};
    const QString path = temporary.filePath("jpeg2k-oversized-frame.testclip");
    ASSERT_TRUE(writeSyntheticClip(
        path,
        syntheticJpeg2kClip(syntheticJpeg2kPayload(oversized, bigOffsets, bigSizes))));

    SyntheticClip clip(path, MLV_OPEN_PREVIEW);
    ASSERT_EQ(static_cast<int>(MLV_ERR_NONE), clip.err());
    ASSERT_EQ(static_cast<uint64_t>(1), static_cast<uint64_t>(clip.video()->frames));
    ASSERT_EQ(oversized, clip.indexedFrameSize(0));
    ojphStubReset(plan);

    /* The capacity gate runs before the VIDF header is re-read for this frame,
     * so an untouched sentinel proves the rejection happened AT the gate and
     * not downstream of a wrongly-sized allocation. */
    const uint32_t kSentinel = 0xa5a5a5a5u;
    clip.setVidfFrameNumber(kSentinel);

    std::vector<uint16_t> frame(
        static_cast<std::size_t>(kSyntheticWidth) * kSyntheticHeight,
        static_cast<uint16_t>(0));
    ASSERT_NE(0, getMlvRawFrameUint16(clip.video(), 0, frame.data()));
    ASSERT_EQ(kSentinel, clip.vidfFrameNumber());
    ASSERT_EQ(0, ojphStubDecoderNewCount());

    /* Positive control: an encoded frame larger than the packed RAW frame but
     * inside the ceiling must still be admitted. This is the half that fails if
     * the JPEG2000 class stops contributing to compressed_input - the
     * allocation falls back to the packed size and real frames are refused. */
    const uint32_t admitted = 200u;
    ASSERT_TRUE(admitted > kSyntheticPackedSize);
    ASSERT_TRUE(admitted <= kSyntheticEncodedCeiling);
    const uint32_t okOffsets[4] = {36u, 77u, 118u, 159u};
    const uint32_t okSizes[4] = {41u, 41u, 41u, 41u};
    const QString admittedPath = temporary.filePath("jpeg2k-admitted-frame.testclip");
    ASSERT_TRUE(writeSyntheticClip(
        admittedPath,
        syntheticJpeg2kClip(syntheticJpeg2kPayload(admitted, okOffsets, okSizes))));

    SyntheticClip admittedClip(admittedPath, MLV_OPEN_PREVIEW);
    ASSERT_EQ(static_cast<int>(MLV_ERR_NONE), admittedClip.err());
    ASSERT_EQ(admitted, admittedClip.indexedFrameSize(0));
    ojphStubReset(plan);
    std::fill(frame.begin(), frame.end(), static_cast<uint16_t>(0));
#ifdef ENABLE_JPEG2K
    ASSERT_EQ(0, getMlvRawFrameUint16(admittedClip.video(), 0, frame.data()));
    ASSERT_EQ(4, ojphStubDecoderNewCount());
    for (std::size_t i = 0; i < frame.size(); ++i)
        ASSERT_EQ(static_cast<uint16_t>(1234), frame[i]);
#else
    /* Without the codec the frame still cannot be produced, but the capacity
     * gate must not be what refused it: the VIDF header is reached either way. */
    admittedClip.setVidfFrameNumber(kSentinel);
    ASSERT_NE(0, getMlvRawFrameUint16(admittedClip.video(), 0, frame.data()));
    ASSERT_NE(kSentinel, admittedClip.vidfFrameNumber());
#endif
}

/* Item 2(c): the file-controlled channel table must be validated against the
 * bytes actually read before any of it is handed to a decoder. */
TEST(UpstreamSyncHardening, Jpeg2000ChannelTableIsRejectedBeforeAnyDecode)
{
    QTemporaryDir temporary;
    ASSERT_TRUE(temporary.isValid());

    const uint32_t frameSize = 200u;
    const OjphStubPlan plan = ojphStubQuarterFramePlan(
        static_cast<uint32_t>(kSyntheticWidth / 2),
        static_cast<uint32_t>(kSyntheticHeight / 2));

    auto rejects = [&](const char * name,
                       const uint32_t offsets[4],
                       const uint32_t sizes[4]) {
        const QString path = temporary.filePath(QString::fromLatin1(name));
        ASSERT_TRUE(writeSyntheticClip(
            path,
            syntheticJpeg2kClip(syntheticJpeg2kPayload(frameSize, offsets, sizes))));
        SyntheticClip clip(path, MLV_OPEN_PREVIEW);
        ASSERT_EQ(static_cast<int>(MLV_ERR_NONE), clip.err());
        ojphStubReset(plan);
        std::vector<uint16_t> frame(
            static_cast<std::size_t>(kSyntheticWidth) * kSyntheticHeight,
            static_cast<uint16_t>(7));
        ASSERT_NE(0, getMlvRawFrameUint16(clip.video(), 0, frame.data()));
        /* Nothing was decoded, so no pointer derived from the hostile table was
         * ever dereferenced. */
        ASSERT_EQ(0, ojphStubDecoderNewCount());
        ASSERT_EQ(0, ojphStubProbeCount());
        ASSERT_EQ(0, ojphStubDecodeCount());
    };

    /* A channel that starts inside the frame but runs past the bytes read. */
    {
        const uint32_t offsets[4] = {36u, 77u, 118u, 180u};
        const uint32_t sizes[4] = {41u, 41u, 41u, 41u};
        rejects("jpeg2k-channel-past-frame.testclip", offsets, sizes);
    }
    /* A channel that starts past the end of the frame entirely. */
    {
        const uint32_t offsets[4] = {36u, 77u, 118u, frameSize + 1u};
        const uint32_t sizes[4] = {41u, 41u, 41u, 1u};
        rejects("jpeg2k-channel-offset-past-frame.testclip", offsets, sizes);
    }
    /* Offset/size arithmetic must not wrap into an in-range answer. */
    {
        const uint32_t offsets[4] = {36u, 77u, 118u, 0xffffffffu};
        const uint32_t sizes[4] = {41u, 41u, 41u, 41u};
        rejects("jpeg2k-channel-offset-wrap.testclip", offsets, sizes);
    }
    {
        const uint32_t offsets[4] = {36u, 77u, 118u, 159u};
        const uint32_t sizes[4] = {41u, 41u, 41u, 0xffffffffu};
        rejects("jpeg2k-channel-size-wrap.testclip", offsets, sizes);
    }
    /* A channel that starts inside the fixed header that describes it. */
    {
        const uint32_t offsets[4] = {12u, 77u, 118u, 159u};
        const uint32_t sizes[4] = {41u, 41u, 41u, 41u};
        rejects("jpeg2k-channel-inside-header.testclip", offsets, sizes);
    }
    /* An empty channel cannot fill its quarter of the frame. */
    {
        const uint32_t offsets[4] = {36u, 77u, 118u, 159u};
        const uint32_t sizes[4] = {41u, 0u, 41u, 41u};
        rejects("jpeg2k-channel-empty.testclip", offsets, sizes);
    }

#ifdef ENABLE_JPEG2K
    /* Control: the same clip shape with an in-bounds table does reach the
     * decoder, so the counters above measure rejection rather than a path that
     * never decodes anything. */
    {
        const uint32_t offsets[4] = {36u, 77u, 118u, 159u};
        const uint32_t sizes[4] = {41u, 41u, 41u, 41u};
        const QString path = temporary.filePath("jpeg2k-channel-valid.testclip");
        ASSERT_TRUE(writeSyntheticClip(
            path,
            syntheticJpeg2kClip(syntheticJpeg2kPayload(frameSize, offsets, sizes))));
        SyntheticClip clip(path, MLV_OPEN_PREVIEW);
        ASSERT_EQ(static_cast<int>(MLV_ERR_NONE), clip.err());
        ojphStubReset(plan);
        std::vector<uint16_t> frame(
            static_cast<std::size_t>(kSyntheticWidth) * kSyntheticHeight,
            static_cast<uint16_t>(7));
        ASSERT_EQ(0, getMlvRawFrameUint16(clip.video(), 0, frame.data()));
        ASSERT_EQ(4, ojphStubDecoderNewCount());
        ASSERT_EQ(4, ojphStubDecodeCount());
    }

    /* An unsupported layout version is refused before the table is trusted. */
    {
        const uint32_t offsets[4] = {36u, 77u, 118u, 159u};
        const uint32_t sizes[4] = {41u, 41u, 41u, 41u};
        const QString path = temporary.filePath("jpeg2k-bad-version.testclip");
        ASSERT_TRUE(writeSyntheticClip(
            path,
            syntheticJpeg2kClip(
                syntheticJpeg2kPayload(frameSize, offsets, sizes, 2u))));
        SyntheticClip clip(path, MLV_OPEN_PREVIEW);
        ASSERT_EQ(static_cast<int>(MLV_ERR_NONE), clip.err());
        ojphStubReset(plan);
        std::vector<uint16_t> frame(
            static_cast<std::size_t>(kSyntheticWidth) * kSyntheticHeight,
            static_cast<uint16_t>(7));
        ASSERT_NE(0, getMlvRawFrameUint16(clip.video(), 0, frame.data()));
        ASSERT_EQ(0, ojphStubDecoderNewCount());
    }
#endif
}

/* Item 2(d): both compressed MLV export branches must decode through the
 * codec-neutral reader. isMlvCompressed() admits JPEG2000 as well as LJ92, so
 * calling the LJ92-only dng_decompress_image() there fails (or corrupts) every
 * JPEG2000 clip. This is a SOURCE CONTRACT rather than a behavioural test; the
 * round report records why. */
TEST(UpstreamSyncHardening, CompressedExportBranchesUseTheCodecNeutralReader)
{
    const std::string source = readSourceFile(QStringLiteral("src/mlv/video_mlv.c"));
    ASSERT_TRUE(source.size() > 100000u);

    /* The LJ92-only decompressor must not be reachable from this file at all. */
    ASSERT_TRUE(source.find("dng_decompress_image") == std::string::npos);

    const std::string decompressAnchor =
        "(export_mode == MLV_DECOMPRESS) && isMlvCompressed(video)";
    const std::size_t decompressAt = source.find(decompressAnchor);
    ASSERT_TRUE(decompressAt != std::string::npos);
    ASSERT_TRUE(source.find(decompressAnchor, decompressAt + 1) == std::string::npos);
    const std::string decompressBranch = source.substr(decompressAt, 2400);
    ASSERT_TRUE(decompressBranch.find(
        "getMlvRawFrameUint16(video, frame_index, frame_buf_unpacked)")
        != std::string::npos);

    const std::string averagedAnchor = "else if(export_mode == MLV_AVERAGED_FRAME)";
    const std::size_t averagedAt = source.find(averagedAnchor);
    ASSERT_TRUE(averagedAt != std::string::npos);
    ASSERT_TRUE(source.find(averagedAnchor, averagedAt + 1) == std::string::npos);
    /* The averaged branch decompresses inside its isMlvCompressed() arm. */
    const std::string averagedBranch = source.substr(averagedAt, 1600);
    ASSERT_TRUE(averagedBranch.find("isMlvCompressed(video)") != std::string::npos);
    ASSERT_TRUE(averagedBranch.find(
        "getMlvRawFrameUint16(video, frame_index, frame_buf_unpacked)")
        != std::string::npos);

    /* Both arms belong to the same export loop, ahead of its pass-through
     * fallback, so neither match can have come from another export mode. */
    ASSERT_TRUE(averagedAt < decompressAt);
}
