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

namespace
{

bool openLargeDualIso(MlvPipelineFixture & fixture)
{
    QString error;
    return fixture.openClipFile(repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv")), &error);
}

} // namespace

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
