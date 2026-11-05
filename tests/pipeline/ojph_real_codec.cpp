// PLAYBACK-SOURCE-FRAME-IDENTITY-1 r9 (r8 adversary pass 2 A1 = Dot B2): the REAL OpenJPH codec inside the pipeline test
// binary, beside the link-seam stub (ojph_decoder_test_stub.cpp). The JPEG2K source identity is the DECODED picture, and
// that a COM comment OpenJPH skips does not change it can only be shown with a real codestream through the real decoder.
//
// The product wrapper (src/mlv/OpenJPH/ojph_wrapper.cpp) is compiled here verbatim, with its eleven extern "C" entry points
// renamed to ojph_real_*, so the stub keeps the ojph_* names every existing test relies on and forwards to these only while a
// test installs a real-codec plan (OjphStubPlan::real_codec). Nothing in src/ is changed or stubbed.
#define ojph_encoder_new ojph_real_encoder_new
#define ojph_encoder_set_image ojph_real_encoder_set_image
#define ojph_encoder_set_lossless ojph_real_encoder_set_lossless
#define ojph_encoder_set_decompositions ojph_real_encoder_set_decompositions
#define ojph_encoder_set_quantization ojph_real_encoder_set_quantization
#define ojph_encoder_encode_into ojph_real_encoder_encode_into
#define ojph_encoder_free ojph_real_encoder_free
#define ojph_decoder_new ojph_real_decoder_new
#define ojph_decoder_probe ojph_real_decoder_probe
#define ojph_decoder_decode_into ojph_real_decoder_decode_into
#define ojph_decoder_free ojph_real_decoder_free
#include "../../src/mlv/OpenJPH/ojph_wrapper.cpp"
