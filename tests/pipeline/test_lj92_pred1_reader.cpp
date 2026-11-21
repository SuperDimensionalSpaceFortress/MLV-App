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
