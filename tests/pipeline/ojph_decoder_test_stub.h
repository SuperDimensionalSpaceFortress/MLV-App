#ifndef MLV_APP_OJPH_DECODER_TEST_STUB_H
#define MLV_APP_OJPH_DECODER_TEST_STUB_H

#include <cstddef>
#include <cstdint>

/*!
 * \file ojph_decoder_test_stub.h
 * \brief Link seam standing in for the third-party OpenJPH codec inside the
 *        pipeline test binary.
 *
 * The pipeline suite defines ENABLE_JPEG2K so that video_mlv.c compiles its
 * real JPEG2000 frame-read branch - the capacity gate, the layout-version
 * check, the channel-table validation and the scatter are the production lines
 * under test. Linking OpenJPH itself would drag its whole source tree into the
 * test build and would still require a real JPEG2000 encoder to produce a
 * decodable payload, so only the codec entry points are replaced here. Nothing
 * in src/ is stubbed.
 *
 * The counters exist so a test can prove that a malformed frame is rejected
 * *before* any byte of it reaches a decoder.
 */

struct OjphStubPlan {
    int probe_result = 0;
    uint32_t probe_width = 0;
    uint32_t probe_height = 0;
    uint32_t probe_components = 1;
    uint32_t probe_bit_depth = 12;
    /* 0 means "fill the whole capacity the caller offered and report it". */
    size_t decode_pixels = 0;
    int32_t fill_value = 1234;
};

/* Plan that decodes one quarter-resolution bayer channel successfully. */
OjphStubPlan ojphStubQuarterFramePlan(uint32_t quarterWidth, uint32_t quarterHeight);

/* Install a plan and zero every counter. */
void ojphStubReset(const OjphStubPlan & plan);

int ojphStubDecoderNewCount();
int ojphStubProbeCount();
int ojphStubDecodeCount();

#endif // MLV_APP_OJPH_DECODER_TEST_STUB_H
