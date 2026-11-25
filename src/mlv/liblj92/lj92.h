/*
lj92.h
(c) Andrew Baldwin 2014

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
*/

#ifndef LJ92_H
#define LJ92_H

enum LJ92_ERRORS {
    LJ92_ERROR_NONE = 0,
    LJ92_ERROR_CORRUPT = -1,
    LJ92_ERROR_NO_MEMORY = -2,
    LJ92_ERROR_BAD_HANDLE = -3,
    LJ92_ERROR_TOO_WIDE = -4,
    LJ92_ERROR_ENCODER = -5
};

typedef struct _ljp* lj92;

/* Parse a lossless JPEG (1992) structure returning
 * - a handle that can be used to decode the data
 * - width/height/bitdepth of the data
 * Returns status code.
 * If status == LJ92_ERROR_NONE, handle must be closed with lj92_close
 */
int lj92_open(lj92* lj, // Return handle here
              uint8_t* data,int datalen, // The encoded data
              int* width,int* height,int* bitdepth,int* components); // Width, height, bitdepth and components

/* Release a decoder object */
void lj92_close(lj92 lj);

/*
 * Decode previously opened lossless JPEG (1992) into a 2D tile of memory
 * Starting at target, write writeLength 16bit values, then skip 16bit skipLength value before writing again
 * If linearize is not NULL, use table at linearize to convert data values from output value to target value
 * Data is only correct if LJ92_ERROR_NONE is returned
 *
 * Precondition (no aliasing): the compressed input passed to lj92_open and the target buffer must not share
 * backing memory, whether through the same mapping or through two distinct mappings of the same pages.
 * The decoder's address-range overlap check sees only same-mapping overlap (it then keeps the legacy reader); it is
 * a defence, not part of this contract. Behaviour is undefined when the two alias through distinct mappings.
 */
int lj92_decode(lj92 lj,
                uint16_t* target, int writeLength, int skipLength, // The image is written to target as a tile
                uint16_t* linearize, int linearizeLength); // If not null, linearize the data using this table

int lj92_get_last_pred6_split_active(void);
int lj92_get_last_pred6_split_requested(void);
int lj92_get_last_generic_split_active(void);
int lj92_get_last_generic_split_requested(void);
int lj92_get_last_pred1_fast_path_active(void);
int lj92_get_last_pred1_fast_path_measurement_requested(void);
int lj92_get_last_pred1_fast_path_measurement_active(void);
int lj92_get_last_pred1_fast_path_eligible(void);
/* Largest SSSS (Huffman symbol value) in the table of the last lj92_decode on this thread; -1 before one. */
int lj92_get_last_huff_max_ssss(void);
/* Test/bench only (per calling thread): non-zero forces the legacy 16-bit-pair bit reader on the predictor-1 fast
 * path so one binary can A/B both; zero (default) takes the u64 reader whenever the Huffman table allows it. */
void lj92_set_pred1_legacy_reader(int force_legacy);
/* 1 when the last lj92_decode on this thread used the u64 accumulator reader, else 0. */
int lj92_get_last_pred1_u64_reader_active(void);
int lj92_get_last_scan_component_count(void);
int lj92_get_last_write_length(void);
int lj92_get_last_expected_write_length(void);
int lj92_get_last_skip_length(void);
int lj92_get_last_linearize_active(void);
int lj92_get_last_component_count(void);
int lj92_get_last_predictor(void);
double lj92_get_last_pred6_total_ms(void);
double lj92_get_last_pred6_bitstream_ms(void);
double lj92_get_last_pred6_predictor_ms(void);
double lj92_get_last_generic_total_ms(void);
double lj92_get_last_generic_bitstream_ms(void);
double lj92_get_last_generic_predictor_ms(void);
double lj92_get_last_pred1_fast_path_total_ms(void);
double lj92_get_last_pred1_fast_path_bitstream_ms(void);
double lj92_get_last_pred1_fast_path_predictor_ms(void);

/*
 * Encode a grayscale image supplied as 16bit values within the given bitdepth
 * Read from tile in the image
 * Apply delinearization if given
 * Return the encoded lossless JPEG stream
 */
int lj92_encode(uint16_t* image, int width, int height, int bitdepth,
                int readLength, int skipLength,
                uint16_t* delinearize,int delinearizeLength,
                uint8_t** encoded, int* encodedLength);
#endif
