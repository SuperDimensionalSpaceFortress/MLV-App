// LJ92-PRED1-FAST-PATH-1: pipeline_tests entry for the predictor-1 bit-reader equality self-test.
//
// The self-test lives in tests/console/lj92_pred1_reader_tool.cpp (the same file also builds the standalone bench / scaling
// tool). Including it here, with its main() left out, runs the whole self-test in the suite CI runs: the legacy and the u64
// reader must return identical codes and identical samples on every in-tree fixture frame, on lj92-conformant synthetic
// streams, on 0xFF-stuffed / marker / bit-flipped / truncated entropy, and the u64 reader must be taken on every eligible
// layout and refused on a table naming an SSSS above 16.
//
// It sits in pipeline_tests (not console_tests) because only pipeline_tests links src/mlv/liblj92/lj92.c.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#define LJ92_PRED1_READER_TOOL_NO_MAIN 1
#include "../console/lj92_pred1_reader_tool.cpp"

TEST(Lj92Pred1Reader, U64ReaderMatchesLegacyReaderOnFixturesAndSyntheticStreams)
{
    const QString root = find_repo_root();
    ASSERT_TRUE(!root.isEmpty());
    ASSERT_EQ(run_selftest(root.toStdString(), 20261120u), 0);
}

// LJ92-PRED1-FAST-PATH-1 r12 (adversary r11 A1, introduced major): the layout getters describe the LAST decode on the calling
// thread, so a decode that fails before the layout reset (an encoded frame_size the input-capacity check refuses; the raw-frame
// malloc failure and the DNG-folder return sit on the same side of the reset) used to leave the previous frame's predictor,
// precision and category maximum in place for a worker to snapshot as the failed frame's. A layout is valid only for the decode
// that produced it: after a failed decode every getter reads its reset sentinel.
#include "mlv_pipeline_fixture.h"

TEST(Lj92LayoutSnapshot, AFailedDecodeReportsTheResetSentinelsNotThePreviousFramesLayout)
{
    MlvPipelineFixture fixture;
    QString error_message;
    ASSERT_TRUE(fixture.openTinyDualIso(&error_message));
    mlvObject_t *video = fixture.video();
    ASSERT_TRUE(video != nullptr);
    ASSERT_TRUE((video->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92) != 0);
    ASSERT_TRUE(getMlvFrames(video) >= 2);

    const size_t words = static_cast<size_t>(fixture.width()) * static_cast<size_t>(fixture.height());
    std::vector<uint16_t> frame(words);
    ASSERT_EQ(0, getMlvRawFrameUint16Uncached(video, 0, frame.data()));
    ASSERT_TRUE(getMlvLastRawUint16Lj92Predictor() >= 0);
    ASSERT_TRUE(getMlvLastRawUint16Lj92Precision() > 0);
    ASSERT_TRUE(getMlvLastRawUint16Lj92MaxSsss() >= 0);
    ASSERT_TRUE(getMlvLastRawUint16Lj92ComponentCount() > 0);

    const uint32_t original = video->video_index[1].frame_size;
    video->video_index[1].frame_size = 0x7FFFFFFFu;   // above pixels * 3 + 200: mlvRawFrameInputCapacity refuses it
    const int status = getMlvRawFrameUint16Uncached(video, 1, frame.data());
    video->video_index[1].frame_size = original;

    ASSERT_TRUE(status != 0);
    ASSERT_EQ(-1, getMlvLastRawUint16Lj92Predictor());
    ASSERT_EQ(0, getMlvLastRawUint16Lj92Precision());
    ASSERT_EQ(-1, getMlvLastRawUint16Lj92MaxSsss());
    ASSERT_EQ(0, getMlvLastRawUint16Lj92ComponentCount());
}
