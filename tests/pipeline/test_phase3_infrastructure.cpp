/*!
 * \file test_phase3_infrastructure.cpp
 * \brief Phase 3 infrastructure tests: rollback gates, diagnostics, and
 *        deterministic debug helpers. These tests intentionally do not
 *        enable parallel playback behavior.
 */

#include "../common/minitest.h"

#include "../../platform/qt/Phase3Breadcrumbs.h"
#include "../../platform/qt/Phase3Checksums.h"
#include "../../platform/qt/Phase3Mode.h"
#include "../../platform/qt/Phase3StageTelemetry.h"
#include "../../src/batch/WorkerThreadCount.h"
#include "../../src/batch/RawAspectStretchPolicy.h"
#include "../../src/debug/ForceSingleThread.h"
#include "../../src/debug/FrameChecksum.h"
#include "../../src/debug/StageTiming.h"
#include "../../src/debug/StageTimingCsvSink.h"
#include "../../src/librtprocess/src/include/array2D.h"
#include "../../src/librtprocess/src/include/librtprocess.h"
#include "../../src/ca_correct/CA_correct_RT.h"
#include "../../src/dng/dng.h"
#include "../../src/dng/dng_reader.h"
#include "../../src/dng/dng_tag_codes.h"
#include "../../src/dng/dng_tag_types.h"
extern "C" {
#include "../../src/mlv/video_mlv.h"
#include "../../platform/mlv_blender/MLVBlender.h"
}

// These kernels must reject hostile extents without dereferencing their image inputs.

#include <QByteArray>
#include <QDir>
#include <QFile>
#include <QTemporaryDir>
#include <QtGlobal>
#include <QStringList>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <limits>
#include <omp.h>
#include <string>
#include <vector>

namespace {

class EnvGuard {
public:
    explicit EnvGuard(const char * name)
        : m_name(name),
          m_wasSet(qEnvironmentVariableIsSet(name)),
          m_value(qgetenv(name))
    {}

    ~EnvGuard()
    {
        if (m_wasSet)
        {
            qputenv(m_name, m_value);
        }
        else
        {
            qunsetenv(m_name);
        }
    }

private:
    const char * m_name;
    bool m_wasSet;
    QByteArray m_value;
};

struct ThrowingElement {
    static bool failConstruction;
    int value = 0;

    ThrowingElement()
    {
        if (failConstruction)
        {
            throw std::bad_alloc();
        }
    }
};

bool ThrowingElement::failConstruction = false;

std::string readTextFile(const QString & path)
{
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    return QString::fromUtf8(file.readAll()).toStdString();
}

void clearPhase3KillSwitches()
{
    qunsetenv("MLVAPP_DISABLE_PHASE3");
    qunsetenv("MLVAPP_DISABLE_PHASE3_3A");
    qunsetenv("MLVAPP_DISABLE_PHASE3_3B");
    qunsetenv("MLVAPP_DISABLE_PHASE3_3C");
    qunsetenv("MLVAPP_DISABLE_PHASE3_3D");
    phase3SetLiveFallbackActive(false);
}

} // namespace

TEST(SecuritySizing, Array2DRejectsOverflowBeforeAllocation)
{
    bool rejected = false;
    try
    {
        array2D<uint64_t> impossible(std::numeric_limits<int>::max(),
                                     std::numeric_limits<int>::max(),
                                     ARRAY2D_CLEAR_DATA);
        (void)impossible;
    }
    catch (const std::bad_array_new_length &)
    {
        rejected = true;
    }
    catch (const std::bad_alloc &)
    {
        ::minitest::fail(__FILE__, __LINE__,
                         "array2D rejects overflow before attempting allocation");
    }
    ASSERT_TRUE(rejected);
}

TEST(SecuritySizing, ResizeAndImageKernelsRejectHostileDimensionsBeforeIndexing)
{
    array2D<uint64_t> stable(2, 2, ARRAY2D_CLEAR_DATA);
    stable[0][0] = UINT64_C(0x123456789abcdef0);
    bool resizeRejected = false;
    try
    {
        stable(std::numeric_limits<int>::max(),
               std::numeric_limits<int>::max(),
               ARRAY2D_CLEAR_DATA);
    }
    catch (const std::bad_array_new_length &)
    {
        resizeRejected = true;
    }
    catch (const std::bad_alloc &)
    {
        ::minitest::fail(__FILE__, __LINE__,
                         "array2D resize rejects overflow before allocation");
    }
    ASSERT_TRUE(resizeRejected);
    ASSERT_EQ(2, stable.width());
    ASSERT_EQ(2, stable.height());
    ASSERT_EQ(UINT64_C(0x123456789abcdef0), stable[0][0]);

    array2D<ThrowingElement> throwing(2, 2);
    throwing[0][0].value = 73;
    ThrowingElement::failConstruction = true;
    bool allocationRejected = false;
    try
    {
        throwing(3, 3, ARRAY2D_LOCK_DATA);
    }
    catch (const std::bad_alloc &)
    {
        allocationRejected = true;
    }
    ThrowingElement::failConstruction = false;
    ASSERT_TRUE(allocationRejected);
    ASSERT_EQ(2, throwing.width());
    ASSERT_EQ(2, throwing.height());
    ASSERT_EQ(73, throwing[0][0].value);
    ASSERT_FALSE(throwing.is_locked());
    throwing(3, 3);
    ASSERT_EQ(3, throwing.width());
    ASSERT_EQ(3, throwing.height());

    const unsigned bayerCfa[2][2] = {{0u, 1u}, {1u, 2u}};
    const unsigned fourColorCfa[2][2] = {{0u, 1u}, {3u, 2u}};
    const auto progress = [](double) { return false; };
    ASSERT_EQ(RP_MEMORY_ERROR,
              igv_demosaic(std::numeric_limits<int>::max(),
                           std::numeric_limits<int>::max(),
                           nullptr, nullptr, nullptr, nullptr, bayerCfa, progress));
    ASSERT_EQ(RP_MEMORY_ERROR,
              lmmse_demosaic(std::numeric_limits<int>::max(),
                             std::numeric_limits<int>::max(),
                             nullptr, nullptr, nullptr, nullptr, bayerCfa, progress, 1));
    ASSERT_EQ(RP_MEMORY_ERROR,
              vng4_demosaic(std::numeric_limits<int>::max(),
                            std::numeric_limits<int>::max(),
                            nullptr, nullptr, nullptr, nullptr, fourColorCfa, progress));
    ASSERT_EQ(RP_MEMORY_ERROR,
              vng4_demosaic(50000, 6000,
                            nullptr, nullptr, nullptr, nullptr, fourColorCfa, progress));

    double fitParams[2][2][16] = {};
    ASSERT_EQ(RP_MEMORY_ERROR,
              CA_correct(std::numeric_limits<int>::min(), 0,
                         std::numeric_limits<int>::max(), 1,
                         false, 1, 0.0, 0.0, false,
                         nullptr, nullptr, bayerCfa, progress,
                         fitParams, false));
    ASSERT_EQ(RP_MEMORY_ERROR,
              CA_correct(std::numeric_limits<int>::min(), 0,
                         std::numeric_limits<int>::max(), 1,
                         false, 1, 0.0, 0.0, true,
                         nullptr, reinterpret_cast<float **>(static_cast<uintptr_t>(1)), bayerCfa, progress,
                         fitParams, false));

    CA_correct_RT(nullptr, 0, 0,
                  std::numeric_limits<int>::max(),
                  std::numeric_limits<int>::max(),
                  0, 0,
                  std::numeric_limits<int>::max(),
                  std::numeric_limits<int>::max(),
                  0, 0.0f, 0.0f);
}

TEST(SecuritySizing, DngCompressorRejectsExpansionBeyondCallerCapacity)
{
    uint16_t input[16] = {};
    uint16_t output[8];
    for (uint16_t & value : output)
    {
        value = UINT16_C(0xa55a);
    }
    size_t outputSize = sizeof(output);
    ASSERT_TRUE(dng_compress_image(output,
                                   1,
                                   input,
                                   &outputSize,
                                   4,
                                   4,
                                   14) != 0);
    ASSERT_EQ(static_cast<size_t>(0), outputSize);
    for (const uint16_t value : output)
    {
        ASSERT_EQ(UINT16_C(0xa55a), value);
    }

    size_t capacity = 0;
    ASSERT_FALSE(dng_lj92_output_capacity(4, 3, &capacity));
    ASSERT_FALSE(dng_lj92_output_capacity(50000, 40000, &capacity));

    outputSize = sizeof(output);
    ASSERT_TRUE(dng_compress_image(output, sizeof(output), input, &outputSize,
                                   4, 3, 14) != 0);
    ASSERT_EQ(static_cast<size_t>(0), outputSize);

    uint16_t decoded[4] = {UINT16_C(0xa55a), UINT16_C(0xa55a),
                           UINT16_C(0xa55a), UINT16_C(0xa55a)};
    ASSERT_TRUE(dng_decompress_image(decoded, sizeof(uint16_t), output, sizeof(output),
                                     2, 2, 14) != 0);
    for (const uint16_t value : decoded)
    {
        ASSERT_EQ(UINT16_C(0xa55a), value);
    }

    struct ExactPackedPayload
    {
        uint16_t words[14];
        uint16_t canary;
    } packedPayload = {{}, UINT16_C(0x6b3d)};
    uint16_t * packed = packedPayload.words;
    uint16_t unpacked[16] = {};
    for (size_t i = 0; i < 16; ++i) input[i] = static_cast<uint16_t>(i * 701u);
    dng_pack_image_bits(packed, input, 4, 4, 14, 0);
    ASSERT_EQ(UINT16_C(0x6b3d), packedPayload.canary);
    dng_unpack_image_bits(unpacked, packed, 4, 4, 14);
    for (size_t i = 0; i < 16; ++i)
    {
        ASSERT_EQ(input[i], unpacked[i]);
    }

    uint16_t sixteenBitPacked[16] = {};
    uint16_t sixteenBitUnpacked[16] = {};
    dng_pack_image_bits(sixteenBitPacked, input, 4, 4, 16, 0);
    dng_unpack_image_bits(sixteenBitUnpacked, sixteenBitPacked, 4, 4, 16);
    for (size_t i = 0; i < 16; ++i)
    {
        ASSERT_EQ(input[i], sixteenBitUnpacked[i]);
    }

    ASSERT_TRUE(saveDngFrame(nullptr, nullptr, 0, nullptr, nullptr) != 0);
}

TEST(SecuritySizing, RawInputMetadataCannotSelectAnOversizedRead)
{
    size_t packedSize = 0;
    size_t allocationSize = 0;
    ASSERT_TRUE(mlvRawFrameInputCapacity(4, 4, 14, 0,
                                         &packedSize, &allocationSize));
    ASSERT_EQ(static_cast<size_t>(28), packedSize);
    ASSERT_EQ(static_cast<size_t>(32), allocationSize);
    ASSERT_TRUE(mlvRawFrameInputCapacity(4, 4, 14, 200,
                                         &packedSize, &allocationSize));
    ASSERT_EQ(static_cast<size_t>(204), allocationSize);
    ASSERT_FALSE(mlvRawFrameInputCapacity(4, 4, 14, 249,
                                          &packedSize, &allocationSize));
    ASSERT_FALSE(mlvRawFrameInputCapacity(INT_MAX, INT_MAX, 14, 1,
                                          &packedSize, &allocationSize));
    ASSERT_FALSE(mlvRawFrameInputCapacity(1, 1, 14, 0,
                                          &packedSize, &allocationSize));
    ASSERT_FALSE(mlvRawFrameInputCapacity(2, 4, 14, 0,
                                          &packedSize, &allocationSize));
    ASSERT_FALSE(dng_lj92_output_capacity(32768, 4, &allocationSize));
    ASSERT_TRUE(dng_lj92_output_capacity(32767, 4, &allocationSize));

    dng_frame_info_t info = {};
    info.valid = 1;
    info.strip_offset = 12;
    info.strip_byte_count = 20;
    ASSERT_TRUE(dng_reader_strip_allocation_size(&info, 64, &allocationSize));
    ASSERT_EQ(static_cast<size_t>(24), allocationSize);

    info.strip_byte_count = UINT64_MAX;
    ASSERT_FALSE(dng_reader_strip_allocation_size(&info, UINT64_MAX,
                                                   &allocationSize));
    info.strip_byte_count = 60;
    ASSERT_FALSE(dng_reader_strip_allocation_size(&info, 64, &allocationSize));

    uint8_t tinyStrip[2] = {};
    uint16_t tinyOutput[1] = {};
    info.width = static_cast<uint32_t>(INT_MAX);
    info.height = static_cast<uint32_t>(INT_MAX);
    info.bits_per_sample = 16;
    info.compression = 1;
    ASSERT_EQ(1, dng_reader_decode_strip(&info, tinyStrip, sizeof(tinyStrip),
                                         tinyOutput));
    info.width = 1;
    info.height = 1;
    info.bits_per_sample = 14;
    ASSERT_EQ(1, dng_reader_decode_strip(&info, tinyStrip, sizeof(tinyStrip),
                                         tinyOutput));
}

TEST(SecuritySizing, Jpeg2000FrameSizeCannotExceedTheAdmittedCapacity)
{
    /* The JPEG2000 VIDF branch reads frame_size bytes into the buffer sized by
     * mlvRawFrameInputCapacity(), so a JPEG2000 clip must be sized from its
     * encoded frame size exactly as an LJ92 clip is. 8x8 at 14bpp packs to 112
     * bytes and admits at most 8*8*3+200 = 392 encoded bytes. */
    const int width = 8;
    const int height = 8;
    const int bitdepth = 14;
    size_t packedSize = 0;
    size_t allocationSize = 0;

    ASSERT_TRUE(mlvRawFrameInputCapacity(width, height, bitdepth, 392,
                                         &packedSize, &allocationSize));
    ASSERT_EQ(static_cast<size_t>(112), packedSize);
    ASSERT_EQ(static_cast<size_t>(396), allocationSize);
    /* The read is bounded by allocationSize - 4; the admitted frame must fit. */
    ASSERT_TRUE(allocationSize - 4u >= static_cast<size_t>(392));

    /* A frame_size beyond the codec-independent ceiling is rejected outright,
     * so no allocation and no read of that size ever happens. */
    ASSERT_FALSE(mlvRawFrameInputCapacity(width, height, bitdepth, 393,
                                          &packedSize, &allocationSize));
    ASSERT_FALSE(mlvRawFrameInputCapacity(width, height, bitdepth, 0xffffffffu,
                                          &packedSize, &allocationSize));

    /* Sizing a JPEG2000 frame from the packed RAW size alone - the pre-fix
     * behaviour - admits only a 112-byte buffer, while a 392-byte frame's
     * channel table addresses bytes well past it. The layout validator is the
     * second gate: it refuses any channel that is not wholly inside the bytes
     * actually read, so a short buffer can never be indexed out of bounds. */
    ASSERT_TRUE(mlvRawFrameInputCapacity(width, height, bitdepth, 0,
                                         &packedSize, &allocationSize));
    ASSERT_EQ(static_cast<size_t>(116), allocationSize);
    const uint32_t offsets[4] = {36u, 125u, 214u, 303u};
    const uint32_t sizes[4] = {89u, 89u, 89u, 89u};
    uint32_t quarterWidth = 0;
    uint32_t quarterHeight = 0;
    ASSERT_TRUE(mlvJpeg2kBayerLayoutIsValid(392, width, height,
                                            offsets, sizes,
                                            &quarterWidth, &quarterHeight));
    ASSERT_EQ(static_cast<uint32_t>(4), quarterWidth);
    ASSERT_EQ(static_cast<uint32_t>(4), quarterHeight);
    quarterWidth = 0;
    quarterHeight = 0;
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(allocationSize - 4u, width, height,
                                             offsets, sizes,
                                             &quarterWidth, &quarterHeight));
    ASSERT_EQ(static_cast<uint32_t>(0), quarterWidth);
    ASSERT_EQ(static_cast<uint32_t>(0), quarterHeight);
}

TEST(SecuritySizing, Jpeg2000ChannelTableRejectsOutOfBoundsOffsets)
{
    const int width = 8;
    const int height = 8;
    const size_t frameSize = MLV_JPEG2K_BAYER_HEADER_BYTES + 40u;
    const uint32_t validOffsets[4] = {36u, 46u, 56u, 66u};
    const uint32_t validSizes[4] = {10u, 10u, 10u, 10u};
    uint32_t quarterWidth = 0;
    uint32_t quarterHeight = 0;

    ASSERT_TRUE(mlvJpeg2kBayerLayoutIsValid(frameSize, width, height,
                                            validOffsets, validSizes,
                                            &quarterWidth, &quarterHeight));
    ASSERT_EQ(static_cast<uint32_t>(4), quarterWidth);
    ASSERT_EQ(static_cast<uint32_t>(4), quarterHeight);

    /* A channel that starts inside the fixed header. */
    uint32_t offsets[4] = {35u, 46u, 56u, 66u};
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, width, height,
                                             offsets, validSizes,
                                             &quarterWidth, &quarterHeight));

    /* A channel that starts past the end of the frame. */
    offsets[0] = 36u;
    offsets[3] = static_cast<uint32_t>(frameSize) + 1u;
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, width, height,
                                             offsets, validSizes,
                                             &quarterWidth, &quarterHeight));

    /* A channel that starts inside the frame but runs past its end. */
    offsets[3] = 66u;
    uint32_t sizes[4] = {10u, 10u, 10u, 11u};
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, width, height,
                                             offsets, sizes,
                                             &quarterWidth, &quarterHeight));

    /* Offset/size arithmetic must not wrap. */
    sizes[3] = 0xffffffffu;
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, width, height,
                                             offsets, sizes,
                                             &quarterWidth, &quarterHeight));
    sizes[3] = 10u;
    offsets[3] = 0xffffffffu;
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, width, height,
                                             offsets, sizes,
                                             &quarterWidth, &quarterHeight));

    /* An empty channel carries no samples for its quarter of the frame. */
    offsets[3] = 66u;
    sizes[2] = 0u;
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, width, height,
                                             offsets, sizes,
                                             &quarterWidth, &quarterHeight));

    /* A frame too short to even hold the channel table. */
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(MLV_JPEG2K_BAYER_HEADER_BYTES - 1u,
                                             width, height,
                                             validOffsets, validSizes,
                                             &quarterWidth, &quarterHeight));

    /* Geometry the 2x2 channel split is not defined for. */
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, 9, height,
                                             validOffsets, validSizes,
                                             &quarterWidth, &quarterHeight));
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, width, 9,
                                             validOffsets, validSizes,
                                             &quarterWidth, &quarterHeight));
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, 2, 2,
                                             validOffsets, validSizes,
                                             &quarterWidth, &quarterHeight));
    ASSERT_FALSE(mlvJpeg2kBayerLayoutIsValid(frameSize, width, height,
                                             nullptr, validSizes,
                                             &quarterWidth, &quarterHeight));
}

TEST(SecuritySizing, CurvBlockCannotOverrunTheLinearisationTable)
{
    const uint32_t headerBytes = static_cast<uint32_t>(sizeof(mlv_curv_hdr_t));
    uint32_t lutEntries = 0xdeadbeefu;

    /* A full-range curve is exactly the allocation. */
    ASSERT_TRUE(mlvCurvLutEntryCount(
        headerBytes + MLV_LINEARISE_LUT_ENTRIES * 2u, &lutEntries));
    ASSERT_EQ(MLV_LINEARISE_LUT_ENTRIES, lutEntries);

    /* One entry past the allocation is rejected, not truncated. */
    lutEntries = 0xdeadbeefu;
    ASSERT_FALSE(mlvCurvLutEntryCount(
        headerBytes + (MLV_LINEARISE_LUT_ENTRIES + 1u) * 2u, &lutEntries));
    ASSERT_EQ(static_cast<uint32_t>(0), lutEntries);

    /* So is a wildly oversized block, with no size arithmetic wrapping. */
    ASSERT_FALSE(mlvCurvLutEntryCount(0xffffffffu, &lutEntries));
    ASSERT_EQ(static_cast<uint32_t>(0), lutEntries);

    /* A short or empty block carries no curve at all. */
    ASSERT_FALSE(mlvCurvLutEntryCount(0u, &lutEntries));
    ASSERT_FALSE(mlvCurvLutEntryCount(headerBytes - 1u, &lutEntries));
    ASSERT_FALSE(mlvCurvLutEntryCount(headerBytes, &lutEntries));
    ASSERT_FALSE(mlvCurvLutEntryCount(headerBytes + 1u, &lutEntries));

    /* The payload is an array of uint16 samples: every odd size is rejected
     * outright, never divided down to the entry count below it. The +3 case is
     * the one that used to be accepted as a single entry with a stray byte. */
    lutEntries = 0xdeadbeefu;
    ASSERT_FALSE(mlvCurvLutEntryCount(headerBytes + 3u, &lutEntries));
    ASSERT_EQ(static_cast<uint32_t>(0), lutEntries);
    ASSERT_FALSE(mlvCurvLutEntryCount(headerBytes + 5u, &lutEntries));
    ASSERT_FALSE(mlvCurvLutEntryCount(headerBytes + 8191u, &lutEntries));
    ASSERT_FALSE(mlvCurvLutEntryCount(
        headerBytes + MLV_LINEARISE_LUT_ENTRIES * 2u - 1u, &lutEntries));
    ASSERT_EQ(static_cast<uint32_t>(0), lutEntries);
    /* One odd byte past the allocation stays rejected for both reasons. */
    ASSERT_FALSE(mlvCurvLutEntryCount(
        headerBytes + MLV_LINEARISE_LUT_ENTRIES * 2u + 1u, &lutEntries));

    /* Partial curves stay admissible; the table is zero-padded past them. */
    ASSERT_TRUE(mlvCurvLutEntryCount(headerBytes + 2u, &lutEntries));
    ASSERT_EQ(static_cast<uint32_t>(1), lutEntries);
    ASSERT_TRUE(mlvCurvLutEntryCount(headerBytes + 8192u, &lutEntries));
    ASSERT_EQ(static_cast<uint32_t>(4096), lutEntries);

    ASSERT_FALSE(mlvCurvLutEntryCount(headerBytes + 2u, nullptr));
}

TEST(SecuritySizing, DngOffsetsAndFolderGeometryFailClosedBeforeNarrowing)
{
    ASSERT_FALSE(dng_reader_range_fits(UINT32_MAX, 2, 16));
    ASSERT_TRUE(dng_reader_range_fits(14, 2, 16));

    QTemporaryDir temporary;
    ASSERT_TRUE(temporary.isValid());
    const QString hostilePath = temporary.filePath("hostile-ifd-offset.dng");
    QByteArray header(16, '\0');
    header[0] = 'I';
    header[1] = 'I';
    header[2] = 42;
    header[4] = static_cast<char>(0xff);
    header[5] = static_cast<char>(0xff);
    header[6] = static_cast<char>(0xff);
    header[7] = static_cast<char>(0xff);
    QFile hostile(hostilePath);
    ASSERT_TRUE(hostile.open(QIODevice::WriteOnly));
    ASSERT_EQ(header.size(), hostile.write(header));
    hostile.close();
    dng_frame_info_t parsed{};
    ASSERT_NE(0, dng_reader_parse_file(hostilePath.toUtf8().constData(), &parsed));

    size_t pixels = 0;
    ASSERT_TRUE(mlvDngSequenceGeometryIsRepresentable(4096, 2160, 14, &pixels));
    ASSERT_EQ(static_cast<size_t>(4096 * 2160), pixels);
    ASSERT_FALSE(mlvDngSequenceGeometryIsRepresentable(65540, 4, 14, &pixels));
    ASSERT_FALSE(mlvDngSequenceGeometryIsRepresentable(4, 65540, 14, &pixels));
    ASSERT_FALSE(mlvDngSequenceGeometryIsRepresentable(4, 4, 17, &pixels));
    ASSERT_FALSE(mlvDngSequenceGeometryIsRepresentable(2, 4, 14, &pixels));
    ASSERT_FALSE(mlvDngSequenceGeometryIsRepresentable(40000, 40000, 14, &pixels));
    /* Compressed DNG geometry need not be aligned like packed uncompressed
     * strips; the open/decode path applies the packed-only constraint. */
    ASSERT_TRUE(mlvDngSequenceGeometryIsRepresentable(3, 3, 14, &pixels));
    ASSERT_TRUE(mlvDngCfaPatternIsSupported(0x02010100u));
    ASSERT_FALSE(mlvDngCfaPatternIsSupported(0x01000201u));
    ASSERT_FALSE(mlvDngCfaPatternIsSupported(0x00010102u));
    ASSERT_FALSE(mlvDngCfaPatternIsSupported(0x01020001u));
    ASSERT_FALSE(mlvDngCfaPatternIsSupported(0xffffffffu));

    dng_frame_info_t expected{};
    expected.valid = 1;
    expected.width = 4;
    expected.height = 4;
    expected.bits_per_sample = 14;
    expected.cfa_pattern = 0x02010100u;
    expected.black_level = 512;
    expected.white_level = 15000;
    expected.active_area[2] = 4;
    expected.active_area[3] = 4;
    dng_frame_info_t candidate = expected;
    ASSERT_TRUE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate.cfa_pattern ^= 1u;
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate = expected;
    candidate.white_level--;
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));

    expected.has_as_shot_neutral = 1;
    expected.as_shot_neutral[0] = 2;
    expected.as_shot_neutral[1] = 1;
    expected.as_shot_neutral[2] = 1;
    expected.as_shot_neutral[3] = 1;
    expected.as_shot_neutral[4] = 1;
    expected.as_shot_neutral[5] = 2;
    candidate = expected;
    ASSERT_TRUE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate.as_shot_neutral[4] = 2;
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));

    expected.has_color_matrix1 = 1;
    expected.color_matrix1[0] = 1;
    expected.color_matrix1[1] = 1;
    candidate = expected;
    ASSERT_TRUE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate.color_matrix1[0] = 2;
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));

    expected.has_default_scale = 1;
    expected.default_scale[0] = 1;
    expected.default_scale[1] = 1;
    expected.default_scale[2] = 5;
    expected.default_scale[3] = 3;
    expected.has_frame_rate = 1;
    expected.frame_rate[0] = 24000;
    expected.frame_rate[1] = 1001;
    expected.has_iso = 1;
    expected.iso = 800;
    std::strcpy(expected.camera_model, "metadata-anchor");
    std::strcpy(expected.unique_camera_model, "metadata-anchor-unique");
    expected.has_active_area = 1;
    expected.has_default_crop_origin = 1;
    expected.default_crop_origin[0] = 1;
    expected.default_crop_origin[1] = 1;
    expected.has_default_crop_size = 1;
    expected.default_crop_size[0] = 3;
    expected.default_crop_size[1] = 3;
    expected.has_baseline_exposure = 1;
    expected.baseline_exposure[0] = 5;
    expected.baseline_exposure[1] = 4;
    expected.has_baseline_exposure_offset = 1;
    expected.baseline_exposure_offset[0] = 0;
    expected.baseline_exposure_offset[1] = 1;
    candidate = expected;
    ASSERT_TRUE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate.default_scale[2] = 4;
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate = expected;
    candidate.frame_rate[0] = 25;
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate = expected;
    candidate.iso = 1600;
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate = expected;
    std::strcpy(candidate.camera_model, "other-camera");
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate = expected;
    std::strcpy(candidate.unique_camera_model, "other-unique-camera");
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate = expected;
    candidate.default_crop_size[0] = 2;
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));
    candidate = expected;
    candidate.baseline_exposure[0] = 6;
    ASSERT_FALSE(dng_reader_processing_metadata_matches(&expected, &candidate));

    const int32_t neutral[] = { 1, 2, 1, 1, 2, 1 };
    uint32_t gains[3] = {};
    ASSERT_TRUE(mlvDngAsShotNeutralToWbGains(neutral, gains));
    ASSERT_EQ(512u, gains[0]);
    ASSERT_EQ(1024u, gains[1]);
    ASSERT_EQ(2048u, gains[2]);
    const int32_t writerRoundTrip[] = { 2048, 1024, 1024, 1024, 512, 1024 };
    ASSERT_TRUE(mlvDngAsShotNeutralToWbGains(writerRoundTrip, gains));
    ASSERT_EQ(2048u, gains[0]);
    ASSERT_EQ(1024u, gains[1]);
    ASSERT_EQ(512u, gains[2]);
    const int32_t zeroDenominator[] = { 1, 2, 1, 0, 2, 1 };
    ASSERT_FALSE(mlvDngAsShotNeutralToWbGains(zeroDenominator, gains));
    const int32_t negativeNumerator[] = { -1, 2, 1, 1, 2, 1 };
    ASSERT_FALSE(mlvDngAsShotNeutralToWbGains(negativeNumerator, gains));

    const int32_t squeezedScale[] = { 1, 1, 5, 3 };
    uint32_t samplingX = 0;
    uint32_t samplingY = 0;
    ASSERT_TRUE(mlvDngDefaultScaleToSampling(squeezedScale,
                                             &samplingX, &samplingY));
    ASSERT_EQ(3u, samplingX);
    ASSERT_EQ(5u, samplingY);
    const int32_t equivalentScale[] = { 2, 3, 10, 9 };
    ASSERT_TRUE(mlvDngDefaultScaleToSampling(equivalentScale,
                                             &samplingX, &samplingY));
    ASSERT_EQ(3u, samplingX);
    ASSERT_EQ(5u, samplingY);
    const int32_t unrepresentableScale[] = { 1, 1, 256, 1 };
    ASSERT_FALSE(mlvDngDefaultScaleToSampling(unrepresentableScale,
                                              &samplingX, &samplingY));
    ASSERT_TRUE(mlvDngAspectRatioIsSupported(3u, 5u));
    ASSERT_TRUE(mlvDngAspectRatioIsSupported(5u, 4u));
    ASSERT_TRUE(mlvDngAspectRatioIsSupported(3u, 4u));
    ASSERT_FALSE(mlvDngAspectRatioIsSupported(7u, 11u));
    const RawAspectStretchSelection horizontal125 =
        rawAspectStretchSelectionForRatio(4.0 / 5.0);
    ASSERT_TRUE(horizontal125.valid);
    ASSERT_EQ(1, horizontal125.horizontalIndex);
    ASSERT_EQ(0, horizontal125.verticalIndex);
    const RawAspectStretchSelection combined =
        rawAspectStretchSelectionForRatio(4.0 / 3.0);
    ASSERT_TRUE(combined.valid);
    ASSERT_EQ(1, combined.horizontalIndex);
    ASSERT_EQ(1, combined.verticalIndex);
    const RawAspectRenderedDimensions v033Combined =
        rawAspectRenderedDimensions(100, 100, STRETCH_H_125, STRETCH_V_033, 65535);
    ASSERT_TRUE(v033Combined.valid);
    ASSERT_EQ(375, v033Combined.width);
    ASSERT_EQ(100, v033Combined.height);
    const RawAspectRenderedDimensions truncated =
        rawAspectRenderedDimensions(3, 3, STRETCH_H_125, STRETCH_V_100, 65535);
    ASSERT_TRUE(truncated.valid);
    ASSERT_EQ(3, truncated.width);
    ASSERT_EQ(3, truncated.height);
    ASSERT_FALSE(rawAspectRenderedDimensions(65535, 100,
                                              STRETCH_H_125, STRETCH_V_033,
                                              65535).valid);

    const int32_t baseline[] = { 5, 4 };
    const int32_t baselineOffset[] = { -1, 4 };
    int32_t bias[2] = {};
    ASSERT_TRUE(mlvDngBaselineExposureToBias(baseline, baselineOffset, bias));
    ASSERT_EQ(1, bias[0]);
    ASSERT_EQ(1, bias[1]);
    const int32_t invalidOffset[] = { 1, 0 };
    ASSERT_FALSE(mlvDngBaselineExposureToBias(baseline, invalidOffset, bias));

    double kelvinMultipliers[3] = {};
    get_kelvin_multipliers_rgb(5200.0, kelvinMultipliers);
    const double kelvinNeutral[] = {
        1.0 / kelvinMultipliers[0],
        1.0 / kelvinMultipliers[1],
        1.0 / kelvinMultipliers[2]
    };
    int fittedTemperature = 0;
    int fittedTint = 999;
    ASSERT_TRUE(processingWhiteBalanceControlsForAsShotNeutral(
        kelvinNeutral, &fittedTemperature, &fittedTint));
    ASSERT_EQ(5200, fittedTemperature);
    ASSERT_EQ(0, fittedTint);
    double tintedMultipliers[3] = {};
    get_kelvin_multipliers_rgb(6500.0, tintedMultipliers);
    const double effectiveTint = std::pow(0.4, 1.75) * 10.0;
    tintedMultipliers[2] += effectiveTint / 11.0;
    tintedMultipliers[0] += effectiveTint / 19.0;
    const double tintedLowest = std::min(tintedMultipliers[0],
        std::min(tintedMultipliers[1], tintedMultipliers[2]));
    for (double & multiplier : tintedMultipliers) multiplier /= tintedLowest;
    const double tintedNeutral[] = {
        1.0 / tintedMultipliers[0],
        1.0 / tintedMultipliers[1],
        1.0 / tintedMultipliers[2]
    };
    ASSERT_TRUE(processingWhiteBalanceControlsForAsShotNeutral(
        tintedNeutral, &fittedTemperature, &fittedTint));
    ASSERT_EQ(6500, fittedTemperature);
    ASSERT_EQ(40, fittedTint);
    processingObject_t * renderedWhiteBalance = initProcessingObject();
    ASSERT_TRUE(renderedWhiteBalance != nullptr);
    processingSetWhiteBalance(renderedWhiteBalance, 6500.0, 4.0);
    for(int channel = 0; channel < 3; ++channel)
        ASSERT_NEAR(tintedMultipliers[channel],
                    renderedWhiteBalance->wb_multipliers[channel], 1e-12);
    freeProcessingObject(renderedWhiteBalance);
    const double invalidNeutral[] = { 1.0, 0.0, 1.0 };
    ASSERT_FALSE(processingWhiteBalanceControlsForAsShotNeutral(
        invalidNeutral, &fittedTemperature, &fittedTint));
    const double outOfModelNeutral[] = { 1.0, 1.0 / 1024.0, 1.0 };
    ASSERT_FALSE(processingWhiteBalanceControlsForAsShotNeutral(
        outOfModelNeutral, &fittedTemperature, &fittedTint));
}

TEST(SecuritySizing, DngIfdContractsRejectMissingOrMalformedCriticalMetadata)
{
    QTemporaryDir temporary;
    ASSERT_TRUE(temporary.isValid());

    auto put16 = [](QByteArray & bytes, int offset, uint16_t value) {
        bytes[offset] = static_cast<char>(value & 0xffu);
        bytes[offset + 1] = static_cast<char>((value >> 8) & 0xffu);
    };
    auto put32 = [](QByteArray & bytes, int offset, uint32_t value) {
        for (int shift = 0; shift < 32; shift += 8)
            bytes[offset + shift / 8] = static_cast<char>((value >> shift) & 0xffu);
    };
    auto makeDng = [&](bool withNeutral) {
        const uint16_t entryCount = withNeutral ? 13u : 12u;
        QByteArray bytes(420, '\0');
        bytes[0] = 'I';
        bytes[1] = 'I';
        put16(bytes, 2, 42);
        put32(bytes, 4, 8);
        put16(bytes, 8, entryCount);
        auto entry = [&](int index, uint16_t tag, uint16_t type,
                         uint32_t count, uint32_t value) {
            const int offset = 10 + index * 12;
            put16(bytes, offset, tag);
            put16(bytes, offset + 2, type);
            put32(bytes, offset + 4, count);
            put32(bytes, offset + 8, value);
        };
        entry(0, tcImageWidth, ttLong, 1, 4);
        entry(1, tcImageLength, ttLong, 1, 4);
        entry(2, tcBitsPerSample, ttShort, 1, 14);
        entry(3, tcCompression, ttShort, 1, DNG_READER_COMPRESSION_NONE);
        entry(4, tcPhotometricInterpretation, ttShort, 1, 32803);
        entry(5, tcStripOffsets, ttLong, 1, 384);
        entry(6, tcSamplesPerPixel, ttShort, 1, 1);
        entry(7, tcRowsPerStrip, ttShort, 1, 4);
        entry(8, tcStripByteCounts, ttLong, 1, 28);
        entry(9, tcPlanarConfiguration, ttShort, 1, 1);
        entry(10, tcCFARepeatPatternDim, ttShort, 2, 0x00020002u);
        entry(11, tcCFAPattern, ttByte, 4, 0x02010100u);
        if (withNeutral)
        {
            entry(12, tcAsShotNeutral, ttRational, 3, 256);
            put32(bytes, 256, 1); put32(bytes, 260, 1);
            put32(bytes, 264, 1); put32(bytes, 268, 0);
            put32(bytes, 272, 1); put32(bytes, 276, 1);
        }
        return bytes;
    };
    auto parseBytes = [&](const QString & name, const QByteArray & bytes,
                          dng_frame_info_t * parsed) {
        QFile file(temporary.filePath(name));
        if (!file.open(QIODevice::WriteOnly)) return -1;
        if (file.write(bytes) != bytes.size()) return -1;
        file.close();
        return dng_reader_parse_file(file.fileName().toUtf8().constData(), parsed);
    };

    dng_frame_info_t parsed{};
    QByteArray valid = makeDng(false);
    ASSERT_EQ(0, parseBytes("valid.dng", valid, &parsed));
    ASSERT_EQ(14u, parsed.bits_per_sample);

    QTemporaryDir missingUniqueDirectory;
    ASSERT_TRUE(missingUniqueDirectory.isValid());
    QFile missingUniqueFile(missingUniqueDirectory.filePath("000000.dng"));
    ASSERT_TRUE(missingUniqueFile.open(QIODevice::WriteOnly));
    ASSERT_EQ(valid.size(), missingUniqueFile.write(valid));
    missingUniqueFile.close();
    QByteArray missingUniquePath = missingUniqueDirectory.path().toLocal8Bit();
    int missingUniqueError = MLV_ERR_NONE;
    char missingUniqueMessage[256] = {};
    mlvObject_t * missingUniqueObject = initMlvObjectWithDngFolder(
        missingUniquePath.data(), MLV_OPEN_FULL,
        &missingUniqueError, missingUniqueMessage);
    ASSERT_TRUE(missingUniqueObject != nullptr);
    ASSERT_EQ(MLV_ERR_INVALID, missingUniqueError);
    ASSERT_TRUE(std::strstr(missingUniqueMessage, "UniqueCameraModel") != nullptr);
    freeMlvObject(missingUniqueObject);

    QByteArray missingBits = valid;
    put16(missingBits, 10 + 2 * 12, 65000);
    ASSERT_NE(0, parseBytes("missing-bits.dng", missingBits, &parsed));

    QByteArray missingCompression = valid;
    put16(missingCompression, 10 + 3 * 12, 65000);
    ASSERT_NE(0, parseBytes("missing-compression.dng", missingCompression, &parsed));

    QByteArray duplicateCompression = valid;
    put16(duplicateCompression, 10 + 4 * 12, tcCompression);
    ASSERT_NE(0, parseBytes("duplicate-compression.dng", duplicateCompression, &parsed));

    QByteArray wrongBitsCount = valid;
    put32(wrongBitsCount, 10 + 2 * 12 + 4, 2);
    ASSERT_NE(0, parseBytes("wrong-bits-count.dng", wrongBitsCount, &parsed));

    QByteArray multipleStrips = valid;
    put32(multipleStrips, 10 + 5 * 12 + 4, 2);
    ASSERT_NE(0, parseBytes("multiple-strips.dng", multipleStrips, &parsed));

    QByteArray wrongPhotometric = valid;
    put32(wrongPhotometric, 10 + 4 * 12 + 8, 2);
    ASSERT_NE(0, parseBytes("wrong-photometric.dng", wrongPhotometric, &parsed));

    QByteArray wrongSamples = valid;
    put32(wrongSamples, 10 + 6 * 12 + 8, 3);
    ASSERT_NE(0, parseBytes("wrong-samples.dng", wrongSamples, &parsed));

    QByteArray partialRows = valid;
    put32(partialRows, 10 + 7 * 12 + 8, 2);
    ASSERT_NE(0, parseBytes("partial-rows.dng", partialRows, &parsed));

    QByteArray wrongRepeat = valid;
    put32(wrongRepeat, 10 + 10 * 12 + 8, 0x00030002u);
    ASSERT_NE(0, parseBytes("wrong-cfa-repeat.dng", wrongRepeat, &parsed));

    QByteArray invalidNeutral = makeDng(true);
    ASSERT_NE(0, parseBytes("zero-neutral-denominator.dng", invalidNeutral, &parsed));

    auto withExtraEntry = [&](uint16_t tag, uint16_t type,
                              uint32_t count, uint32_t value) {
        QByteArray bytes = makeDng(false);
        put16(bytes, 8, 13u);
        const int offset = 10 + 12 * 12;
        put16(bytes, offset, tag);
        put16(bytes, offset + 2, type);
        put32(bytes, offset + 4, count);
        put32(bytes, offset + 8, value);
        return bytes;
    };

    QByteArray embeddedModel = withExtraEntry(tcModel, ttAscii, 6, 256);
    embeddedModel.replace(256, 6, QByteArray("A\0BAD\0", 6));
    ASSERT_NE(0, parseBytes("embedded-nul-model.dng", embeddedModel, &parsed));

    QByteArray emptyModel = withExtraEntry(tcModel, ttAscii, 1, 0);
    ASSERT_NE(0, parseBytes("empty-model.dng", emptyModel, &parsed));

    QByteArray embeddedUnique = withExtraEntry(tcUniqueCameraModel, ttAscii, 6, 256);
    embeddedUnique.replace(256, 6, QByteArray("U\0BAD\0", 6));
    ASSERT_NE(0, parseBytes("embedded-nul-unique.dng", embeddedUnique, &parsed));

    QByteArray emptyUnique = withExtraEntry(tcUniqueCameraModel, ttAscii, 1, 0);
    ASSERT_NE(0, parseBytes("empty-unique.dng", emptyUnique, &parsed));

    QByteArray shortUnique = withExtraEntry(tcUniqueCameraModel, ttAscii, 6, 256);
    shortUnique.replace(256, 6, QByteArray("Short\0", 6));
    QTemporaryDir shortFallbackDirectory;
    ASSERT_TRUE(shortFallbackDirectory.isValid());
    QFile shortFallbackFile(shortFallbackDirectory.filePath("000000.dng"));
    ASSERT_TRUE(shortFallbackFile.open(QIODevice::WriteOnly));
    ASSERT_EQ(shortUnique.size(), shortFallbackFile.write(shortUnique));
    shortFallbackFile.close();
    QByteArray shortFallbackPath = shortFallbackDirectory.path().toLocal8Bit();
    int shortFallbackError = MLV_ERR_NONE;
    char shortFallbackMessage[256] = {};
    mlvObject_t * shortFallbackObject = initMlvObjectWithDngFolder(
        shortFallbackPath.data(), MLV_OPEN_FULL,
        &shortFallbackError, shortFallbackMessage);
    ASSERT_TRUE(shortFallbackObject != nullptr);
    ASSERT_EQ(MLV_ERR_NONE, shortFallbackError);
    ASSERT_EQ(std::string("Short"),
              std::string(reinterpret_cast<const char *>(
                  shortFallbackObject->IDNT.cameraName)));
    ASSERT_TRUE(shortFallbackObject->camid.cameraName[UNIQ] != nullptr);
    ASSERT_EQ(std::string("Short"),
              std::string(shortFallbackObject->camid.cameraName[UNIQ]));

    QTemporaryDir shortFallbackReexportDirectory;
    ASSERT_TRUE(shortFallbackReexportDirectory.isValid());
    int32_t shortFallbackPar[4] = { 1, 1, 1, 1 };
    dngObject_t * shortFallbackDng = initDngObject(
        shortFallbackObject, UNCOMPRESSED_RAW, 24.0, shortFallbackPar);
    ASSERT_TRUE(shortFallbackDng != nullptr);
    const QString shortFallbackReexportPath =
        shortFallbackReexportDirectory.filePath("reexport.dng");
    QByteArray shortFallbackReexportBytes =
        shortFallbackReexportPath.toLocal8Bit();
    ASSERT_EQ(0, saveDngFrame(shortFallbackObject, shortFallbackDng, 0,
                              shortFallbackReexportBytes.data(), nullptr));
    freeDngObject(shortFallbackDng);
    dng_frame_info_t shortFallbackReexportInfo{};
    ASSERT_EQ(0, dng_reader_parse_file(
        shortFallbackReexportBytes.constData(), &shortFallbackReexportInfo));
    ASSERT_EQ(std::string("Short"),
              std::string(shortFallbackReexportInfo.camera_model));
    ASSERT_EQ(std::string("Short"),
              std::string(shortFallbackReexportInfo.unique_camera_model));
    freeMlvObject(shortFallbackObject);

    QByteArray longUnique = withExtraEntry(tcUniqueCameraModel, ttAscii, 40, 256);
    longUnique.replace(256, 40, QByteArray(39, 'L') + QByteArray(1, '\0'));
    QTemporaryDir longFallbackDirectory;
    ASSERT_TRUE(longFallbackDirectory.isValid());
    QFile longFallbackFile(longFallbackDirectory.filePath("000000.dng"));
    ASSERT_TRUE(longFallbackFile.open(QIODevice::WriteOnly));
    ASSERT_EQ(longUnique.size(), longFallbackFile.write(longUnique));
    longFallbackFile.close();
    QByteArray longFallbackPath = longFallbackDirectory.path().toLocal8Bit();
    int longFallbackError = MLV_ERR_NONE;
    char longFallbackMessage[256] = {};
    mlvObject_t * longFallbackObject = initMlvObjectWithDngFolder(
        longFallbackPath.data(), MLV_OPEN_FULL,
        &longFallbackError, longFallbackMessage);
    ASSERT_TRUE(longFallbackObject != nullptr);
    ASSERT_EQ(MLV_ERR_INVALID, longFallbackError);
    ASSERT_TRUE(std::strstr(longFallbackMessage, "too long") != nullptr);
    freeMlvObject(longFallbackObject);

    QByteArray invalidActive = withExtraEntry(tcActiveArea, ttLong, 4, 256);
    put32(invalidActive, 256, 3); put32(invalidActive, 260, 0);
    put32(invalidActive, 264, 2); put32(invalidActive, 268, 4);
    ASSERT_NE(0, parseBytes("reversed-active-area.dng", invalidActive, &parsed));

    QByteArray zeroBaseline = withExtraEntry(tcBaselineExposure, ttSRational, 1, 256);
    put32(zeroBaseline, 256, 1); put32(zeroBaseline, 260, 0);
    ASSERT_NE(0, parseBytes("zero-baseline-denominator.dng", zeroBaseline, &parsed));

    QByteArray cropOriginOnly = withExtraEntry(tcDefaultCropOrigin, ttShort, 2, 0);
    ASSERT_NE(0, parseBytes("crop-origin-without-size.dng", cropOriginOnly, &parsed));
}

TEST(SecuritySizing, BlenderRejectsEmptyCropAndBindsExportRangeAndSampleMaximum)
{
    MLVBlender_mlv_t lane{};
    lane.mlv = reinterpret_cast<mlvObject_t *>(static_cast<uintptr_t>(1));
    lane.width = 8;
    lane.height = 4;
    lane.crop_left = 8;
    lane.visible = 1;

    MLVBlender_t blender{};
    blender.num_mlvs = 1;
    blender.mlvs = &lane;
    blender.blended_output = static_cast<float *>(malloc(sizeof(float)));
    ASSERT_TRUE(blender.blended_output != nullptr);
    blender.output_width = 1;
    blender.output_height = 1;
    MLVBlenderBlend(&blender, 0);
    ASSERT_TRUE(blender.blended_output == nullptr);
    ASSERT_EQ(0, blender.output_width);
    ASSERT_EQ(0, blender.output_height);

    uint32_t first = UINT32_MAX;
    uint32_t last = UINT32_MAX;
    ASSERT_TRUE(MLVBlenderInclusiveFrameRange(1, &first, &last));
    ASSERT_EQ(0u, first);
    ASSERT_EQ(0u, last);
    ASSERT_TRUE(MLVBlenderInclusiveFrameRange(17, &first, &last));
    ASSERT_EQ(0u, first);
    ASSERT_EQ(16u, last);
    ASSERT_FALSE(MLVBlenderInclusiveFrameRange(0, &first, &last));

    ASSERT_EQ(static_cast<uint16_t>(16383), MLVBlenderQuantize14(1.0f, 512));
    ASSERT_EQ(static_cast<uint16_t>(16383), MLVBlenderQuantize14(2.0f, 512));
    ASSERT_EQ(static_cast<uint16_t>(512), MLVBlenderQuantize14(0.0f, 512));
    ASSERT_EQ(static_cast<uint16_t>(512),
              MLVBlenderQuantize14(std::numeric_limits<float>::quiet_NaN(), 512));
}

TEST(Phase3Mode, DefaultKillSwitchStateKeepsModesAvailable)
{
    EnvGuard disableAll("MLVAPP_DISABLE_PHASE3");
    EnvGuard disable3A("MLVAPP_DISABLE_PHASE3_3A");
    EnvGuard disable3B("MLVAPP_DISABLE_PHASE3_3B");
    EnvGuard disable3C("MLVAPP_DISABLE_PHASE3_3C");
    EnvGuard disable3D("MLVAPP_DISABLE_PHASE3_3D");
    clearPhase3KillSwitches();

    phase3ReloadKillSwitchesForTest();

    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::Disabled));
    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::DecodeAheadOnly));
    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::DecodeRecon));
    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::DecodeReconProcess));
    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::Full));
}

TEST(Phase3Mode, GlobalKillSwitchDisablesEveryExperimentalMode)
{
    EnvGuard disableAll("MLVAPP_DISABLE_PHASE3");
    qputenv("MLVAPP_DISABLE_PHASE3", QByteArrayLiteral("1"));

    phase3ReloadKillSwitchesForTest();

    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::Disabled));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeAheadOnly));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeRecon));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeReconProcess));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::Full));
}

TEST(Phase3Mode, Hierarchical3AKillSwitchDisablesEveryPhase3Mode)
{
    EnvGuard disableAll("MLVAPP_DISABLE_PHASE3");
    EnvGuard disable3A("MLVAPP_DISABLE_PHASE3_3A");
    EnvGuard disable3B("MLVAPP_DISABLE_PHASE3_3B");
    EnvGuard disable3C("MLVAPP_DISABLE_PHASE3_3C");
    EnvGuard disable3D("MLVAPP_DISABLE_PHASE3_3D");
    clearPhase3KillSwitches();
    qputenv("MLVAPP_DISABLE_PHASE3_3A", QByteArrayLiteral("1"));

    phase3ReloadKillSwitchesForTest();

    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeAheadOnly));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeRecon));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeReconProcess));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::Full));
}

TEST(Phase3Mode, Hierarchical3BKillSwitchDisablesItsSubphaseAndLater)
{
    EnvGuard disableAll("MLVAPP_DISABLE_PHASE3");
    EnvGuard disable3A("MLVAPP_DISABLE_PHASE3_3A");
    EnvGuard disable3B("MLVAPP_DISABLE_PHASE3_3B");
    EnvGuard disable3C("MLVAPP_DISABLE_PHASE3_3C");
    EnvGuard disable3D("MLVAPP_DISABLE_PHASE3_3D");
    clearPhase3KillSwitches();
    qputenv("MLVAPP_DISABLE_PHASE3_3B", QByteArrayLiteral("1"));

    phase3ReloadKillSwitchesForTest();

    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::DecodeAheadOnly));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeRecon));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeReconProcess));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::Full));
}

TEST(Phase3Mode, Hierarchical3CKillSwitchDisablesItsSubphaseAndLater)
{
    EnvGuard disableAll("MLVAPP_DISABLE_PHASE3");
    EnvGuard disable3A("MLVAPP_DISABLE_PHASE3_3A");
    EnvGuard disable3B("MLVAPP_DISABLE_PHASE3_3B");
    EnvGuard disable3C("MLVAPP_DISABLE_PHASE3_3C");
    EnvGuard disable3D("MLVAPP_DISABLE_PHASE3_3D");
    clearPhase3KillSwitches();
    qputenv("MLVAPP_DISABLE_PHASE3_3C", QByteArrayLiteral("1"));

    phase3ReloadKillSwitchesForTest();

    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::DecodeAheadOnly));
    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::DecodeRecon));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeReconProcess));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::Full));
}

TEST(Phase3Mode, LiveFallbackUsesAtomicStateNotEnvironmentMutation)
{
    EnvGuard disableAll("MLVAPP_DISABLE_PHASE3");
    clearPhase3KillSwitches();
    phase3ReloadKillSwitchesForTest();

    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::Full));
    phase3SetLiveFallbackActive(true);
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::DecodeAheadOnly));
    ASSERT_TRUE(phase3KillSwitchActive(Phase3Mode::Full));
    phase3SetLiveFallbackActive(false);
    ASSERT_FALSE(phase3KillSwitchActive(Phase3Mode::Full));
}

TEST(Phase3_STM, ForceSingleThreadEnvOverridesWorkerAndOpenMpCounts)
{
    EnvGuard forceSingle("MLVAPP_FORCE_SINGLETHREAD");
    EnvGuard forceThreads("MLVAPP_FORCE_THREADS");
    EnvGuard ompThreads("OMP_NUM_THREADS");
    EnvGuard ompDynamic("OMP_DYNAMIC");
    qputenv("MLVAPP_FORCE_SINGLETHREAD", QByteArrayLiteral("1"));
    qputenv("MLVAPP_FORCE_THREADS", QByteArrayLiteral("8"));
    mlvapp_force_singlethread_reset_for_test();

    mlvapp_force_singlethread_init();

    ASSERT_EQ(1, mlvapp_is_forced_singlethread());
    ASSERT_EQ(1, mlvappEffectiveWorkerThreadCount());
    ASSERT_EQ(1, omp_get_max_threads());
    ASSERT_EQ(std::string("1"), std::string(qgetenv("OMP_NUM_THREADS").constData()));
}

TEST(Phase3_TEL, StreamingSinkWritesPerEventCsvRows)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("stages.csv"));

    ASSERT_EQ(1, stage_timing_csv_sink_open(path.toLocal8Bit().constData()));
    ASSERT_TRUE(stage_timing_csv_sink_enabled() != 0);
    stage_timing_csv_sink_write_event(17, 123, 2, MLV_STAGE_DECODE, "enter", 1000, 4, 9);
    stage_timing_csv_sink_write_event(17, 123, 2, MLV_STAGE_DECODE, "leave", 2000, 4, 9);
    stage_timing_csv_sink_close();

    const std::string text = readTextFile(path);
    ASSERT_TRUE(text.find("frame_idx,request_serial,slot,stage,event,ns,phase3_mode,clip_generation") != std::string::npos);
    ASSERT_TRUE(text.find("17,123,2,decode,enter,1000,4,9") != std::string::npos);
    ASSERT_TRUE(text.find("17,123,2,decode,leave,2000,4,9") != std::string::npos);
}

TEST(Phase3_TEL, ScaffoldSequenceWritesEightOrderedStageEvents)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("stages.csv"));

    ASSERT_EQ(1, stage_timing_csv_sink_open(path.toLocal8Bit().constData()));
    const char * stages[] = {
        MLV_STAGE_DECODE,
        MLV_STAGE_DECODE,
        MLV_STAGE_RECON,
        MLV_STAGE_RECON,
        MLV_STAGE_PROCESS,
        MLV_STAGE_PROCESS,
        MLV_STAGE_DISPLAY,
        MLV_STAGE_DISPLAY
    };
    const char * events[] = {
        "enter",
        "leave",
        "enter",
        "leave",
        "enter",
        "leave",
        "enter",
        "leave"
    };
    for( uint64_t i = 0; i < 8; ++i )
    {
        stage_timing_csv_sink_write_event( 21,
                                           0x100000005ull,
                                           3,
                                           stages[i],
                                           events[i],
                                           1000 + i,
                                           static_cast<uint8_t>( Phase3Mode::Full ),
                                           11 );
    }
    stage_timing_csv_sink_close();

    const QString text = QString::fromStdString(readTextFile(path)).trimmed();
    const QStringList lines = text.split(QLatin1Char('\n'), Qt::SkipEmptyParts);
    ASSERT_EQ(9, lines.size());
    ASSERT_EQ(std::string("frame_idx,request_serial,slot,stage,event,ns,phase3_mode,clip_generation"),
              lines[0].trimmed().toStdString());
    for( int i = 0; i < 8; ++i )
    {
        const QStringList cells = lines[i + 1].trimmed().split(QLatin1Char(','));
        ASSERT_EQ(8, cells.size());
        ASSERT_EQ(std::string("21"), cells[0].toStdString());
        ASSERT_EQ(std::string("4294967301"), cells[1].toStdString());
        ASSERT_EQ(std::string("3"), cells[2].toStdString());
        ASSERT_EQ(std::string(stages[i]), cells[3].toStdString());
        ASSERT_EQ(std::string(events[i]), cells[4].toStdString());
        ASSERT_EQ(1000 + i, cells[5].toInt());
        ASSERT_EQ(std::string("4"), cells[6].toStdString());
        ASSERT_EQ(std::string("11"), cells[7].toStdString());
    }
}

TEST(Phase3Telemetry, BatchWriterRemainsAvailableForOfflineReports)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("stages.csv"));

    std::vector<Phase3StageTelemetry::Event> events;
    events.push_back({17, 123, 2, "decode", "start", 1000, Phase3Mode::Full, 9});
    events.push_back({17, 123, 2, "decode", "end", 2000, Phase3Mode::Full, 9});

    QString error;
    ASSERT_TRUE(Phase3StageTelemetry::writeCsv(path, events, &error));
    const std::string text = readTextFile(path);

    ASSERT_TRUE(text.find(Phase3StageTelemetry::csvHeader()) != std::string::npos);
    ASSERT_TRUE(text.find("17,123,2,decode,start,1000,full,9") != std::string::npos);
    ASSERT_TRUE(text.find("17,123,2,decode,end,2000,full,9") != std::string::npos);
}

TEST(Phase3_CHK, Xxhash64KnownVectorsAreSharedByQtAndCApis)
{
    ASSERT_EQ(0xef46db3751d8e999ull, Phase3Checksums::xxhash64("", 0));
    ASSERT_EQ(0xef46db3751d8e999ull, frame_checksum_compute("", 0));
    const char hello[] = "hello";
    ASSERT_EQ(0x26c7827d889f6da3ull,
              Phase3Checksums::xxhash64(hello, std::strlen(hello)));
    ASSERT_EQ(0x26c7827d889f6da3ull,
              frame_checksum_compute(hello, std::strlen(hello)));
}

TEST(Phase3_CHK, FrameChecksumRingVerifiesNewestRecords)
{
    frame_checksum_reset_for_test();
    for (uint32_t i = 0; i < 300; ++i)
    {
        frame_checksum_log_record(i, 0x1000ull + i);
    }

    int found = 0;
    ASSERT_EQ(0ull, frame_checksum_log_lookup(0, &found));
    ASSERT_EQ(0, found);
    ASSERT_EQ(0x112bull, frame_checksum_log_lookup(299, &found));
    ASSERT_EQ(1, found);
    ASSERT_EQ(0, frame_checksum_verify(299, 0x112bull));
    ASSERT_NE(0, frame_checksum_verify(299, 0x9999ull));
}

TEST(Phase3_CHK, EnvGateControlsRuntimeChecksumVerification)
{
    EnvGuard enabled("MLVAPP_PHASE3_CHECK_FRAME_CHECKSUMS");
    qputenv("MLVAPP_PHASE3_CHECK_FRAME_CHECKSUMS", QByteArrayLiteral("1"));
    frame_checksum_reset_for_test();
    ASSERT_EQ(1, frame_checksum_enabled());

    qputenv("MLVAPP_PHASE3_CHECK_FRAME_CHECKSUMS", QByteArrayLiteral("0"));
    frame_checksum_reset_for_test();
    ASSERT_EQ(0, frame_checksum_enabled());
}

TEST(Phase3Checksums, WritesChecksumCsvWithPlanSchema)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QString path = dir.filePath(QStringLiteral("checksums.csv"));

    std::vector<Phase3Checksums::Record> records;
    records.push_back({5, 99, Phase3Mode::DecodeReconProcess, 4, 1920, 1080, 0x1234ull});

    QString error;
    ASSERT_TRUE(Phase3Checksums::writeCsv(path, records, &error));
    const std::string text = readTextFile(path);

    ASSERT_TRUE(text.find("frame_idx,request_serial,phase3_mode,scale_factor,width,height,checksum_hex")
                != std::string::npos);
    ASSERT_TRUE(text.find("5,99,decode_recon_process,4,1920,1080,0000000000001234")
                != std::string::npos);
}

TEST(Phase3_CRA, RingWrapKeepsNewestBreadcrumbsInOrder)
{
    Phase3Breadcrumbs::resetForTest();
    for (uint32_t i = 0; i < 300; ++i)
    {
        Phase3Breadcrumbs::push(static_cast<uint8_t>(i % 4),
                                1,
                                2,
                                i,
                                i,
                                i + 1000,
                                static_cast<uint8_t>(Phase3Mode::Full),
                                "test");
    }

    const std::vector<Phase3Breadcrumbs::Breadcrumb> crumbs =
        Phase3Breadcrumbs::getBreadcrumbsForTest();
    ASSERT_EQ(static_cast<std::size_t>(256), crumbs.size());
    ASSERT_EQ(44ull, crumbs.front().timestamp_ns);
    ASSERT_EQ(44u, crumbs.front().frame_idx);
    ASSERT_EQ(299ull, crumbs.back().timestamp_ns);
    ASSERT_EQ(299u, crumbs.back().frame_idx);
    ASSERT_EQ(1299u, crumbs.back().request_serial);
    ASSERT_EQ(1u, crumbs.back().from_state);
    ASSERT_EQ(2u, crumbs.back().to_state);
    ASSERT_EQ(static_cast<uint8_t>(Phase3Mode::Full), crumbs.back().phase3_mode);
    ASSERT_EQ(std::string("test"), std::string(crumbs.back().context));
}

TEST(Phase3_CRA, BreadcrumbPreservesFullRequestSerial)
{
    Phase3Breadcrumbs::resetForTest();
    const uint64_t requestSerial = 0x100000123ull;
    Phase3Breadcrumbs::push(2,
                            3,
                            4,
                            55,
                            77,
                            requestSerial,
                            static_cast<uint8_t>(Phase3Mode::Full),
                            "serial64");

    const std::vector<Phase3Breadcrumbs::Breadcrumb> crumbs =
        Phase3Breadcrumbs::getBreadcrumbsForTest();
    ASSERT_EQ(static_cast<std::size_t>(1), crumbs.size());
    ASSERT_EQ(requestSerial, crumbs.front().request_serial);
}

TEST(Phase3_CRA, DumpToFileIsCrashSafeText)
{
    Phase3Breadcrumbs::resetForTest();
    Phase3Breadcrumbs::push(1,
                            7,
                            8,
                            77,
                            42,
                            420,
                            static_cast<uint8_t>(Phase3Mode::DecodeRecon),
                            "dump");

    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const QByteArray path =
        QDir::toNativeSeparators(dir.filePath(QStringLiteral("breadcrumbs.txt"))).toLocal8Bit();
    FILE * file = std::fopen(path.constData(), "wb+");
    ASSERT_TRUE(file != nullptr);
    Phase3Breadcrumbs::dumpToFile(file);
    std::rewind(file);
    char buffer[512] = {};
    const std::size_t read = std::fread(buffer, 1, sizeof(buffer) - 1, file);
    std::fclose(file);

    ASSERT_TRUE(read > 0);
    const std::string text(buffer);
    ASSERT_TRUE(text.find("Phase 3 Breadcrumbs") != std::string::npos);
    ASSERT_TRUE(text.find("frame=42") != std::string::npos);
    ASSERT_TRUE(text.find("request=420") != std::string::npos);
    ASSERT_TRUE(text.find("from=7") != std::string::npos);
    ASSERT_TRUE(text.find("to=8") != std::string::npos);
    ASSERT_TRUE(text.find("context=dump") != std::string::npos);
}
