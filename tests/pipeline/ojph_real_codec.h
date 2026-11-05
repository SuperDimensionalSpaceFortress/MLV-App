#ifndef MLV_APP_OJPH_REAL_CODEC_H
#define MLV_APP_OJPH_REAL_CODEC_H

#include <cstddef>
#include <cstdint>

// PLAYBACK-SOURCE-FRAME-IDENTITY-1 r9: the product OpenJPH wrapper's entry points under the ojph_real_* names
// (tests/pipeline/ojph_real_codec.cpp compiles src/mlv/OpenJPH/ojph_wrapper.cpp with them renamed). Same signatures as
// src/mlv/OpenJPH/ojph_wrapper.h.
extern "C" {
void * ojph_real_encoder_new();
void ojph_real_encoder_set_image(void * e, uint32_t w, uint32_t h, uint32_t num_comps, uint32_t bit_depth, int is_signed);
void ojph_real_encoder_set_lossless(void * e, int lossless);
void ojph_real_encoder_set_decompositions(void * e, uint32_t n);
void ojph_real_encoder_set_quantization(void * e, float q);
size_t ojph_real_encoder_encode_into(void * e, const int32_t * pixels, uint8_t * out_buf, size_t out_buf_size);
void ojph_real_encoder_free(void * e);

void * ojph_real_decoder_new();
int ojph_real_decoder_probe(void * d, const uint8_t * data, size_t size, uint32_t * w, uint32_t * h,
                            uint32_t * num_comps, uint32_t * bit_depth, int * is_signed);
size_t ojph_real_decoder_decode_into(void * d, const uint8_t * data, size_t size, int32_t * out_pixels,
                                     size_t out_pixels_cap, uint32_t * out_w, uint32_t * out_h,
                                     uint32_t * out_num_comps);
void ojph_real_decoder_free(void * d);
}

#endif // MLV_APP_OJPH_REAL_CODEC_H
