#include "ojph_decoder_test_stub.h"

#include <atomic>

namespace {

OjphStubPlan & plan()
{
    static OjphStubPlan value;
    return value;
}

std::atomic<int> & decoderNewCount()
{
    static std::atomic<int> value{0};
    return value;
}

std::atomic<int> & probeCount()
{
    static std::atomic<int> value{0};
    return value;
}

std::atomic<int> & decodeCount()
{
    static std::atomic<int> value{0};
    return value;
}

/* Distinct non-null handle so the production branch's null check is exercised
 * the same way it is against the real codec. */
int g_handle = 0;

} // namespace

OjphStubPlan ojphStubQuarterFramePlan(uint32_t quarterWidth, uint32_t quarterHeight)
{
    OjphStubPlan value;
    value.probe_result = 0;
    value.probe_width = quarterWidth;
    value.probe_height = quarterHeight;
    value.probe_components = 1;
    value.probe_bit_depth = 12;
    value.decode_pixels = 0;
    value.fill_value = 1234;
    return value;
}

void ojphStubReset(const OjphStubPlan & new_plan)
{
    plan() = new_plan;
    decoderNewCount().store(0);
    probeCount().store(0);
    decodeCount().store(0);
}

int ojphStubDecoderNewCount() { return decoderNewCount().load(); }
int ojphStubProbeCount() { return probeCount().load(); }
int ojphStubDecodeCount() { return decodeCount().load(); }

#ifdef ENABLE_JPEG2K

#include "../../src/mlv/OpenJPH/ojph_wrapper.h"

extern "C" {

void * ojph_decoder_new()
{
    decoderNewCount().fetch_add(1);
    return &g_handle;
}

int ojph_decoder_probe(void * d, const uint8_t * data, size_t size,
                       uint32_t * w, uint32_t * h,
                       uint32_t * num_comps, uint32_t * bit_depth,
                       int * is_signed)
{
    probeCount().fetch_add(1);
    if (!d || !data || size == 0) return -1;
    const OjphStubPlan & p = plan();
    if (w) *w = p.probe_width;
    if (h) *h = p.probe_height;
    if (num_comps) *num_comps = p.probe_components;
    if (bit_depth) *bit_depth = p.probe_bit_depth;
    if (is_signed) *is_signed = 0;
    return p.probe_result;
}

size_t ojph_decoder_decode_into(void * d, const uint8_t * data, size_t size,
                                int32_t * out_pixels, size_t out_pixels_cap,
                                uint32_t * out_w, uint32_t * out_h,
                                uint32_t * out_num_comps)
{
    decodeCount().fetch_add(1);
    if (!d || !data || size == 0 || !out_pixels || out_pixels_cap == 0) return 0;
    const OjphStubPlan & p = plan();
    const size_t produced = p.decode_pixels ? p.decode_pixels : out_pixels_cap;
    /* Never write past what the production branch offered: a stub that
     * overran would hide the very bound this suite is protecting. */
    const size_t written = produced < out_pixels_cap ? produced : out_pixels_cap;
    for (size_t i = 0; i < written; ++i) out_pixels[i] = p.fill_value;
    if (out_w) *out_w = p.probe_width;
    if (out_h) *out_h = p.probe_height;
    if (out_num_comps) *out_num_comps = p.probe_components;
    return produced;
}

void ojph_decoder_free(void *) {}

/* The JPEG2000 export branch is compiled too; it is not under test, so the
 * encoder seam simply refuses. */
void * ojph_encoder_new() { return nullptr; }
void ojph_encoder_set_image(void *, uint32_t, uint32_t, uint32_t, uint32_t, int) {}
void ojph_encoder_set_lossless(void *, int) {}
void ojph_encoder_set_decompositions(void *, uint32_t) {}
void ojph_encoder_set_quantization(void *, float) {}
size_t ojph_encoder_encode_into(void *, const int32_t *, uint8_t *, size_t) { return 0; }
void ojph_encoder_free(void *) {}

} // extern "C"

#endif // ENABLE_JPEG2K
