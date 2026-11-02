/*
 * Copyright (C) 2017 bouncyball
 *
 * This program is free software; you can redistribute it and/or
 * modify it under the terms of the GNU General Public License
 * as published by the Free Software Foundation; either version 2
 * of the License, or (at your option) any later version.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with this program; if not, write to the
 * Free Software Foundation, Inc.,
 * 51 Franklin Street, Fifth Floor,
 * Boston, MA  02110-1301, USA.
 */

#ifndef _dng_h
#define _dng_h

#include <sys/types.h>
#include "../mlv/mlv_object.h"

#ifdef __cplusplus
extern "C" {
#endif

/* raw state definitions */
#define UNCOMPRESSED_RAW 0
#define COMPRESSED_RAW 1
#define UNCOMPRESSED_ORIG 2
#define COMPRESSED_ORIG 3

typedef struct
{
    int enabled;
    int black_level_enabled;
    int black_level;
    int white_level_enabled;
    int white_level;
    int baseline_exposure_enabled;
    int32_t baseline_exposure[2];
    int as_shot_neutral_enabled;
    int32_t as_shot_neutral[6];
} dngExportOverrides_t;

/* dngObject struct consists of DNG header and image buffers and their sizes */
typedef struct
{
    double fps_float;               // User defined fps
    int32_t par[4];                 // User defined verical and horizontal stretch (AR)

    int raw_input_state;            // 0 - uncompressed, 1 - losless
    int raw_output_state;           // 0 - uncompressed, 1 - losless, 2 - pass uncompressed, 3 - pass losless

    size_t header_size;             // dng header size
    size_t image_size;              // raw image buffer size
    size_t image_capacity;          // allocated image buffer capacity in bytes
    size_t image_size_unpacked;     // bit unpacked working buffer size

    uint8_t * header_buf;           // pointer to header buffer
    uint16_t * image_buf;           // pointer to image buffer
    uint16_t * image_buf2;          // pointer to image buffer for temporary decompression
    uint16_t * image_buf_unpacked;  // pointer to bit packed image buffer
    dngExportOverrides_t overrides; // optional metadata defaults for export
    int defer_payload_compress;      // opt-in async writer compresses this payload
    int payload_compress_width;
    int payload_compress_height;
    uint32_t payload_compress_bpp;
    uint64_t payload_compress_input_bytes;

} dngObject_t;

typedef struct
{
    uint32_t frame_index;
    int raw_input_state;
    int raw_output_state;
    int compression_deferred;
    int compression_width;
    int compression_height;
    uint32_t compression_bpp;
    uint64_t compression_input_bytes;
    size_t header_size;
    size_t image_size;
    size_t image_capacity;
    uint8_t * header_buf;
    uint8_t * image_buf;
} dngFramePayload_t;

typedef struct dngPayloadWriter dngPayloadWriter_t;

/* routines to unpack, pack, decompress or compress raw data */
void dng_unpack_image_bits(uint16_t * input_buffer, uint16_t * output_buffer, int width, int height, uint32_t bpp);
void dng_pack_image_bits(uint16_t * input_buffer, uint16_t * output_buffer, int width, int height, uint32_t bpp, int big_endian);
int dng_compress_image(uint16_t * output_buffer, size_t output_buffer_capacity, uint16_t * input_buffer, size_t * output_buffer_size, int width, int height, uint32_t bpp);
int dng_lj92_output_capacity(int width, int height, size_t * output_capacity);
int dng_decompress_image(uint16_t * output_buffer, size_t output_buffer_capacity, uint16_t * input_buffer, size_t input_buffer_size, int width, int height, uint32_t bpp);

/* routines to initialize, save and free DNG exporting struct */
dngObject_t * initDngObject(mlvObject_t * mlv_data, int raw_state, double fps, int32_t par[4]);
void setDngExportOverrides(dngObject_t * dng_data, const dngExportOverrides_t * overrides);
dngFramePayload_t * buildDngFramePayload(mlvObject_t * mlv_data, dngObject_t * dng_data, uint32_t frame_index, const char *props_filename);
int writeDngFramePayload(dngFramePayload_t * payload, const char * dng_filename);
void freeDngFramePayload(dngFramePayload_t * payload);
dngPayloadWriter_t * createDngPayloadWriter(void);
int finishDngPayloadWriter(dngPayloadWriter_t * writer);
int saveDngFrameViaAsyncPayloadWriter(dngPayloadWriter_t * writer, mlvObject_t * mlv_data, dngObject_t * dng_data, uint32_t frame_index, char * dng_filename, const char *props_filename);
int saveDngFrameViaPayload(mlvObject_t * mlv_data, dngObject_t * dng_data, uint32_t frame_index, char * dng_filename, const char *props_filename);
int saveDngFrame(mlvObject_t * mlv_data, dngObject_t * dng_data, uint32_t frame_index, char * dng_filename, const char *props_filename);
void freeDngObject(dngObject_t * dng_data);
void resetDngExportProfilerForTesting(void);

/* Test-only: how the async writer's debug hold (MLVAPP_CDNG_EXPORT_ASYNC_WRITER_DEBUG_HOLD_UNTIL_FULL_MS,
 * profiler-gated) ended. dngPayloadWriterDebugWaitHoldResult waits up to wait_ms for a PENDING hold
 * to end and returns its outcome (PENDING if wait_ms ran out, OFF if the hold is not armed). It waits
 * on the producer condition, so call it from the producer thread, before finishDngPayloadWriter. */
#define DNG_PAYLOAD_WRITER_DEBUG_HOLD_OFF 0
#define DNG_PAYLOAD_WRITER_DEBUG_HOLD_PENDING 1
#define DNG_PAYLOAD_WRITER_DEBUG_HOLD_FILLED 2
#define DNG_PAYLOAD_WRITER_DEBUG_HOLD_STOPPED 3
#define DNG_PAYLOAD_WRITER_DEBUG_HOLD_TIMED_OUT 4
int dngPayloadWriterDebugWaitHoldResult(dngPayloadWriter_t * writer, unsigned wait_ms);

#ifdef __cplusplus
}
#endif

#endif
