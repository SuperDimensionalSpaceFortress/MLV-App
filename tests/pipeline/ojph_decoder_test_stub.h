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
 *
 * PLAYBACK-SOURCE-FRAME-IDENTITY-1 r9: the JPEG2K source identity is the
 * decoded picture, which only a real codestream through the real decoder can
 * pin, so OpenJPH is now linked too, its wrapper renamed to ojph_real_*
 * (ojph_real_codec.cpp). A test opts in with OjphRealCodecScope; the stub
 * remains the default.
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
    /* PLAYBACK-SOURCE-FRAME-IDENTITY-1 r9: when set, every ojph_* entry point forwards to the REAL OpenJPH codec
     * (tests/pipeline/ojph_real_codec.cpp, the product wrapper under ojph_real_* names) and the fields above are
     * ignored; the counters still count. Off by default, so every other test keeps the stub. */
    bool real_codec = false;
};

/* r9: the real codec for the lifetime of the scope (installs a real_codec plan). r10 (adversary r9 pass 3 A2): on exit it
 * restores the plan it replaced, not the default stub plan, so a nested scope leaves the outer one on the real codec. */
struct OjphRealCodecScope {
    OjphRealCodecScope();
    ~OjphRealCodecScope();
    OjphRealCodecScope(const OjphRealCodecScope &) = delete;
    OjphRealCodecScope & operator=(const OjphRealCodecScope &) = delete;
private:
    OjphStubPlan m_previous;
};

/* Plan that decodes one quarter-resolution bayer channel successfully. */
OjphStubPlan ojphStubQuarterFramePlan(uint32_t quarterWidth, uint32_t quarterHeight);

/* Install a plan and zero every counter. */
void ojphStubReset(const OjphStubPlan & plan);

int ojphStubDecoderNewCount();
int ojphStubProbeCount();
int ojphStubDecodeCount();

#endif // MLV_APP_OJPH_DECODER_TEST_STUB_H
