// PLAYBACK-SOURCE-FRAME-IDENTITY-1: pins getMlvFrameSourceHash64, the source-frame identity the
// --visual-capture-dir writer puts in visual.json (source_id per play frame, and the
// source_difference verdict across them). It hashes the bytes the decoder reads for a frame
// (the encoded VIDF payload at video_index[frame].frame_offset) without decoding, so it must be
// stable for one frame, differ between two frames of a moving clip, and refuse (nonzero, no
// identity) every input it cannot answer for, which makes the writer omit the field.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "mlv_pipeline_fixture.h"

#include <cstdint>
#include <cstdlib>
#include <vector>

extern "C" {
#include "../../src/mlv/liblj92/lj92.h"
}

namespace
{

bool openLargeDualIso(MlvPipelineFixture & fixture)
{
    QString error;
    return fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error);
}

// r6 (adversary A1 on a9ae667a): a 64x64 14-bit LJ92 payload of a two-level picture, encoded by liblj92 itself.
std::vector<uint8_t> encodeLj92(uint16_t left, uint16_t right)
{
    std::vector<uint16_t> picture(64 * 64);
    for (int y = 0; y < 64; ++y)
        for (int x = 0; x < 64; ++x)
            picture[y * 64 + x] = x < 32 ? left : right;
    uint8_t * encoded = nullptr;
    int encodedLength = 0;
    if (lj92_encode(picture.data(), 64, 64, 14, 64 * 64, 0, nullptr, 0, &encoded, &encodedLength) != LJ92_ERROR_NONE
        || !encoded)
        return {};
    std::vector<uint8_t> out(encoded, encoded + encodedLength);
    free(encoded);
    return out;
}

// The payload with `segment` inserted right after SOI (FF D8).
std::vector<uint8_t> withSegmentAfterSoi(const std::vector<uint8_t> & payload, const std::vector<uint8_t> & segment)
{
    std::vector<uint8_t> out(payload.begin(), payload.begin() + 2);
    out.insert(out.end(), segment.begin(), segment.end());
    out.insert(out.end(), payload.begin() + 2, payload.end());
    return out;
}

// Decodes a COPY: liblj92's parseHuff writes into the buffer it parses.
bool decodeLj92(std::vector<uint8_t> payload, std::vector<uint16_t> * picture)
{
    lj92 decoder;
    int width = 0, height = 0, bitdepth = 0, components = 0;
    if (lj92_open(&decoder, payload.data(), static_cast<int>(payload.size()),
                  &width, &height, &bitdepth, &components) != LJ92_ERROR_NONE)
        return false;
    picture->assign(static_cast<size_t>(width) * height * components, 0);
    const int ret = lj92_decode(decoder, picture->data(), width * components, 0, nullptr, 0);
    lj92_close(decoder);
    return ret == LJ92_ERROR_NONE;
}

uint64_t lj92Hash(const std::vector<uint8_t> & payload, int * status)
{
    uint64_t hash = 0;
    *status = mlvLj92FrameSourceHash64(payload.data(), payload.size(), &hash);
    return hash;
}

} // namespace

TEST(VisualCaptureSourceHash, Lj92BytesTheDecoderSkipsAreNotHashed)
{
    const std::vector<uint8_t> plain = encodeLj92(2048, 12288);
    ASSERT_TRUE(plain.size() > 16);
    std::vector<uint16_t> plainPicture;
    ASSERT_TRUE(decodeLj92(plain, &plainPicture));
    int status = -1;
    const uint64_t plainHash = lj92Hash(plain, &status);
    ASSERT_EQ(0, status);

    // The adversary's input: a COM segment whose text differs per frame; then an APP1 segment; then bytes after EOI.
    const std::vector<std::vector<uint8_t>> variants = {
        withSegmentAfterSoi(plain, {0xFF, 0xFE, 0x00, 0x03, 0x41}),
        withSegmentAfterSoi(plain, {0xFF, 0xFE, 0x00, 0x03, 0x42}),
        withSegmentAfterSoi(plain, {0xFF, 0xE1, 0x00, 0x04, 0x12, 0x34}),
        [&plain]() { std::vector<uint8_t> v = plain; v.push_back(0x00); v.push_back(0x7F); return v; }(),
    };
    for (const std::vector<uint8_t> & variant : variants)
    {
        std::vector<uint16_t> picture;
        ASSERT_TRUE(decodeLj92(variant, &picture));
        ASSERT_TRUE(picture == plainPicture); // the decoder reads the same picture...
        ASSERT_TRUE(lj92Hash(variant, &status) == plainHash); // ...so the identity is the same
        ASSERT_EQ(0, status);
    }
}

TEST(VisualCaptureSourceHash, Lj92APictureTheDecoderReadsDifferentlyHashesDifferently)
{
    int status = -1;
    const uint64_t first = lj92Hash(encodeLj92(2048, 12288), &status);
    ASSERT_EQ(0, status);
    const uint64_t second = lj92Hash(encodeLj92(2048, 12289), &status);
    ASSERT_EQ(0, status);
    ASSERT_TRUE(first != second);
}

TEST(VisualCaptureSourceHash, Lj92APayloadTheDecoderRefusesHasNoIdentity)
{
    const std::vector<uint8_t> plain = encodeLj92(2048, 12288);
    ASSERT_TRUE(plain.size() > 16);
    int status = 0;
    std::vector<uint8_t> noSoi = plain;
    noSoi[1] = 0xD9;
    lj92Hash(noSoi, &status);
    ASSERT_NE(0, status);
    lj92Hash({0xFF, 0xD8, 0xFF, 0xD9, 0x00}, &status); // EOI before any scan
    ASSERT_NE(0, status);
    lj92Hash(withSegmentAfterSoi(plain, {0xFF, 0xFE, 0x00, 0x01}), &status); // a segment length under 2
    ASSERT_NE(0, status);
    uint64_t hash = 0;
    ASSERT_NE(0, mlvLj92FrameSourceHash64(nullptr, 8, &hash));
    ASSERT_NE(0, mlvLj92FrameSourceHash64(plain.data(), plain.size(), nullptr));
}

TEST(VisualCaptureSourceHash, OneFrameHashesTheSameTwice)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    uint64_t first = 0;
    uint64_t second = 0;
    ASSERT_EQ(0, getMlvFrameSourceHash64(fixture.video(), 0, &first));
    ASSERT_EQ(0, getMlvFrameSourceHash64(fixture.video(), 0, &second));
    ASSERT_TRUE(first == second);
    // FNV-1a 64's offset basis is the hash of zero bytes: a real read moved it.
    ASSERT_TRUE(first != 0xcbf29ce484222325ull);
}

TEST(VisualCaptureSourceHash, TwoFramesOfTheClipHashDifferently)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    const uint64_t frames = getMlvFrames(fixture.video());
    ASSERT_TRUE(frames > 1);
    uint64_t firstFrame = 0;
    uint64_t lastFrame = 0;
    ASSERT_EQ(0, getMlvFrameSourceHash64(fixture.video(), 0, &firstFrame));
    ASSERT_EQ(0, getMlvFrameSourceHash64(fixture.video(), frames - 1, &lastFrame));
    ASSERT_TRUE(firstFrame != lastFrame);
}

TEST(VisualCaptureSourceHash, AnIndexPastTheLastFrameHasNoIdentity)
{
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    uint64_t hash = 0x5a5a5a5aull;
    ASSERT_NE(0, getMlvFrameSourceHash64(fixture.video(), getMlvFrames(fixture.video()), &hash));
}

TEST(VisualCaptureSourceHash, NullInputsHaveNoIdentity)
{
    uint64_t hash = 0;
    ASSERT_NE(0, getMlvFrameSourceHash64(nullptr, 0, &hash));
    MlvPipelineFixture fixture;
    ASSERT_TRUE(openLargeDualIso(fixture));
    ASSERT_NE(0, getMlvFrameSourceHash64(fixture.video(), 0, nullptr));
}
