#ifndef _video_mlv_
#define _video_mlv_

#include <stddef.h>

#include "raw.h"
#include "mlv.h"
#include "../processing/raw_processing.h"

/* mlvObject_t */
#include "mlv_object.h"

/* Usefull macros */
#include "macros.h"

/* All functions in one */
mlvObject_t * initMlvObjectWithClip(char * mlvPath, int preview, int * err, char * error_message);
mlvObject_t * initMlvObjectWithMcrawClip(char * mlvPath, int preview, int * err, char * error_message);
/* Open a folder of CinemaDNG frames as a clip (per-frame .dng source). */
mlvObject_t * initMlvObjectWithDngFolder(char * dirPath, int preview, int * err, char * error_message);

/* Initialises an MLV object. That's all you need to know */
mlvObject_t * initMlvObject();

/* Prints everything you'll ever need to know */
void printMlvInfo(mlvObject_t * video);

/* Reads an MLV file in to a video object(mlvObject_t struct)
 * only puts frame indexes and metadata in to the mlvObject_t, 
 * no debayering or processing */
int openMlvClip(mlvObject_t * video, char * mlvPath, int open_mode, char * error_message);
int openMcrawClip(mlvObject_t * video, char * mcrawPath, int open_mode, char * error_message);
int openDngFolderClip(mlvObject_t * video, char * dirPath, int open_mode, char * error_message);

/* Validate hostile frame metadata before allocating/reading compressed or
 * packed RAW bytes. Exposed for sizing regression tests. */
int mlvRawFrameInputCapacity(int width, int height, int bitdepth,
                             uint32_t encoded_size,
                             size_t * packed_size,
                             size_t * allocation_size);

/* The linearisation LUT is indexed by a full 16-bit sample, so it is always
 * allocated at this entry count and a CURV block may never claim more. */
#define MLV_LINEARISE_LUT_ENTRIES 65536u

/* Bound the file-controlled CURV payload to that allocation before any read.
 * Returns 0 for a block that is short, empty, oversized, or whose payload is
 * not a whole number of uint16 samples. Exposed for sizing regression tests. */
int mlvCurvLutEntryCount(uint32_t block_size, uint32_t * lut_entries);

/* Fixed prefix of the bayer JPEG2000 VIDF payload: layout version plus an
 * offset/size pair per quarter-resolution channel. */
#define MLV_JPEG2K_BAYER_HEADER_BYTES 36u

/* Validate the file-controlled JPEG2000 bayer channel table against the frame
 * bytes actually read and against the RAWI geometry, before any pointer
 * arithmetic or decode. Returns the quarter-frame dimensions every channel must
 * decode to. Exposed for sizing regression tests. */
int mlvJpeg2kBayerLayoutIsValid(size_t frame_size, int width, int height,
                                const uint32_t * offsets,
                                const uint32_t * sizes,
                                uint32_t * quarter_width,
                                uint32_t * quarter_height);

/* Validate CinemaDNG folder metadata before it is narrowed into RAWI's
 * uint16 geometry or used by legacy int-indexed processing paths. */
int mlvDngSequenceGeometryIsRepresentable(uint32_t width, uint32_t height,
                                          uint32_t bits_per_sample,
                                          size_t * pixel_count);

/* The current shared debayer pipeline consumes RGGB samples only. */
int mlvDngCfaPatternIsSupported(uint32_t cfa_pattern);

/* Convert three positive AsShotNeutral rationals into bounded MLV WB gains. */
int mlvDngAsShotNeutralToWbGains(const int32_t neutral[6], uint32_t gains[3]);

/* Convert DNG DefaultScale X/Y rationals to the exact small-integer sampling
 * ratio consumed by RAWC/getMlvAspectRatio(). */
int mlvDngDefaultScaleToSampling(const int32_t scale[4],
                                 uint32_t * sampling_x,
                                 uint32_t * sampling_y);

/* Return non-zero only for exact RAWC aspect ratios representable by the
 * application's horizontal/vertical stretch controls in every output path. */
int mlvDngAspectRatioIsSupported(uint32_t sampling_x, uint32_t sampling_y);

/* Add BaselineExposure and BaselineExposureOffset without signed overflow,
 * normalize the result, and retain an exact signed rational for re-export. */
int mlvDngBaselineExposureToBias(const int32_t baseline[2],
                                 const int32_t offset[2],
                                 int32_t bias[2]);

/* return error codes of and open modes of openMlvClip() */
enum mlv_err { MLV_ERR_NONE, MLV_ERR_OPEN, MLV_ERR_IO, MLV_ERR_CORRUPTED, MLV_ERR_INVALID };
enum open_mode { MLV_OPEN_FULL, MLV_OPEN_MAPP, MLV_OPEN_PREVIEW };

/* Functions for saving cut or averaged MLV */
int saveMlvHeaders(mlvObject_t * video, FILE * output_mlv, int export_audio, int export_mode, uint32_t frame_start, uint32_t frame_end, const char * version, char * error_message);
int saveMlvAVFrame(mlvObject_t * video, FILE * output_mlv, int export_audio, int export_mode, uint32_t frame_start, uint32_t frame_end, uint32_t frame_index, uint64_t * avg_buf, char * error_message);
enum export_mode { MLV_FAST_PASS, MLV_LJ92, MLV_DECOMPRESS, MLV_AVERAGED_FRAME, MLV_DF_INT, MLV_JP2K_LOW, MLV_JP2K_MED, MLV_JP2K_HIGH, MLV_JP2K_VERYHIGH, MLV_JP2K_VISULOSSLESS };
/* from darkframe.c */
extern int df_init(mlvObject_t * video);

/* Frees all memory and closes file.
 *
 * Lifetime contract: the caller must stop and join foreground render/UI/API
 * users before calling this function.  freeMlvObject fences the asynchronous
 * workers owned by the object (including cache workers and admitted cache
 * topology operations); cache lifecycle admission is not a general external
 * reference count for arbitrary concurrent render calls. */
void freeMlvObject(mlvObject_t * video);

/* To enable and disable caching */
void disableMlvCaching(mlvObject_t * video);
void enableMlvCaching(mlvObject_t * video);
/* Reset cache, to recache all frames, clears simgle frame cache too */
void resetMlvCache(mlvObject_t * video);
void invalidateMlvProcessedPreviewCache(mlvObject_t * video);
/* Invalidates processed8 slots and barriers publication by the asynchronous
 * prefetch worker. Foreground render lifecycle serialization remains the
 * caller's responsibility, as it was for invalidateMlvProcessedPreviewCache. */
void mlvInvalidateProcessed8PrefetchCache(mlvObject_t * video);
void mlvCancelPreviewPrefetch(mlvObject_t * video);
/* For setting how much can be cached - "MegaBytes" == MebiBytes (thanks dmilligan) */
void setMlvRawCacheLimitMegaBytes(mlvObject_t * video, uint64_t megaByteLimit);
void setMlvRawCacheLimitFrames(mlvObject_t * video, uint64_t frameLimit);

/* Links processing settings() with an MLV object */
void setMlvProcessing(mlvObject_t * video, processingObject_t * processing);
/* Function for WB Picker */
void findMlvWhiteBalance(mlvObject_t * video, uint64_t frameIndex, int posX, int posY, int *wbTemp, int *wbTint, int mode);
void findMlvWhiteBalanceIsolated(mlvObject_t * video, uint64_t frameIndex, int posX, int posY, int *wbTemp, int *wbTint, int mode);

/* Functions for getting processed MLV frames - uses the 'processing' module,
 * Avalible in 8 and 16 bit! Neither is faster as processing for both is done in 16 bit,
 * only use more than one thread in threads argument for speedier preview, not for export
 * as it may have minor artifacts (though I haven't found them yet) */
void getMlvProcessedFrame8(mlvObject_t * video, uint64_t frameIndex, uint8_t * outputFrame, int threads);
void getMlvProcessedFrame16(mlvObject_t * video, uint64_t frameIndex, uint16_t * outputFrame, int threads);

/* Phase 4A: scale-aware processed-frame getters.
 *
 * The scaleFactor contract is:
 *   - must be 1, 2, 4, or 8
 *   - must be a power of two (Phase 4B will reject non-power-of-two values
 *     to preserve the 2x2 Bayer block alignment)
 *   - if scaleFactor is invalid, the call is treated as scaleFactor == 1
 *
 * Phase 4A note: the rendering pipeline still produces full-resolution
 * output regardless of scaleFactor. The parameter is observed only by:
 *   1. the processed8/16 cache key (so a scale=1 result cannot satisfy a
 *      scale=2 lookup, and vice versa), and
 *   2. mlvFrameOutputDimensions() reporting (always returns full res in
 *      Phase 4A).
 *
 * Phase 4B will add the fused-downsample-and-debayer that actually
 * produces a smaller buffer for scaleFactor > 1. Callers wiring through
 * the scaled API today are forward-compatible: their output buffer must be
 * sized to mlvFrameOutputDimensions(), and once Phase 4B lands the same
 * call site automatically gets the smaller render. */
/* Fast/subset playback-preview level sync (see video_mlv.c): the subset path
 * renders via the GPU-preview config instead of the classic cores, so it must
 * mirror their per-frame dual-ISO black/white level sync before the config is
 * built. Check first (no mutation), then sync under a render-idle guard. */
int mlvProcessingDualIsoBlackWhiteLevelsOutOfSync(mlvObject_t * video);
void mlvSyncProcessingDualIsoBlackWhiteLevels(mlvObject_t * video);

void getMlvProcessedFrame8Scaled(mlvObject_t * video,
                                 uint64_t frameIndex,
                                 uint8_t * outputFrame,
                                 int threads,
                                 int scaleFactor);
/* Render-thread-only playback helpers: both REQUIRE an active playback-preview
 * envelope (RenderFrameThread's PlaybackPreviewModeGuard) -- they inherit the
 * preview mode + preview scale factor and manage only the aggressive sub-flag,
 * unlike getMlvProcessedFrame8Scaled which establishes the envelope itself.
 * See the full contract note at their definitions in video_mlv.c. Not
 * general-purpose; calling outside that envelope mis-gates the direct8 path. */
int getMlvProcessedFrame8ScaledFromRaw16(mlvObject_t * video,
                                         uint64_t frameIndex,
                                         const uint16_t * decodedRawFrame,
                                         uint8_t * outputFrame,
                                         int threads,
                                         int scaleFactor);
int getMlvProcessedFrame8ScaledFromReconnedRaw16(mlvObject_t * video,
                                                 uint64_t frameIndex,
                                                 const uint16_t * reconnedRawFrame,
                                                 uint8_t * outputFrame,
                                                 int threads,
                                                 int scaleFactor,
                                                 int allowScale1StateDebayer);
void getMlvProcessedFrame16Scaled(mlvObject_t * video,
                                  uint64_t frameIndex,
                                  uint16_t * outputFrame,
                                  int threads,
                                  int scaleFactor);

/* Reports the (width,height) a Phase-4-aware caller should size the output
 * buffer for. In Phase 4A this always returns the full sensor dimensions
 * (the pipeline still renders at scale=1 internally). In Phase 4B it will
 * return W/scaleFactor and H/scaleFactor when scaleFactor > 1. For x8,
 * trailing partial 8x8 edge blocks are ignored in preview, so dimensions are
 * floor(W/8) and floor(H/8).
 * scaleFactor must be 1, 2, 4, or 8; invalid values are treated as 1. */
void mlvFrameOutputDimensions(mlvObject_t * video,
                              int scaleFactor,
                              int * outWidth,
                              int * outHeight);
double getMlvLastRawUint16Milliseconds(void);
double getMlvLastRawUint16DiskReadMilliseconds(void);
double getMlvLastRawUint16DecompressMilliseconds(void);
double getMlvLastRawUint16DecompressPrepareMilliseconds(void);
double getMlvLastRawUint16DecompressExecuteMilliseconds(void);
int getMlvLastRawUint16Lj92Pred6SplitActive(void);
int getMlvLastRawUint16Lj92Pred6SplitRequested(void);
int getMlvLastRawUint16Lj92GenericSplitActive(void);
int getMlvLastRawUint16Lj92GenericSplitRequested(void);
int getMlvLastRawUint16Lj92Pred1FastPathActive(void);
int getMlvLastRawUint16Lj92Pred1FastPathMeasurementRequested(void);
int getMlvLastRawUint16Lj92Pred1FastPathMeasurementActive(void);
int getMlvLastRawUint16Lj92Pred1FastPathEligible(void);
int getMlvLastRawUint16Lj92ScanComponentCount(void);
int getMlvLastRawUint16Lj92WriteLength(void);
int getMlvLastRawUint16Lj92ExpectedWriteLength(void);
int getMlvLastRawUint16Lj92SkipLength(void);
int getMlvLastRawUint16Lj92LinearizeActive(void);
int getMlvLastRawUint16Lj92ComponentCount(void);
int getMlvLastRawUint16Lj92Predictor(void);
double getMlvLastRawUint16Lj92Pred6TotalMilliseconds(void);
double getMlvLastRawUint16Lj92Pred6BitstreamMilliseconds(void);
double getMlvLastRawUint16Lj92Pred6PredictorMilliseconds(void);
double getMlvLastRawUint16Lj92GenericTotalMilliseconds(void);
double getMlvLastRawUint16Lj92GenericBitstreamMilliseconds(void);
double getMlvLastRawUint16Lj92GenericPredictorMilliseconds(void);
double getMlvLastRawUint16Lj92Pred1FastPathTotalMilliseconds(void);
double getMlvLastRawUint16Lj92Pred1FastPathBitstreamMilliseconds(void);
double getMlvLastRawUint16Lj92Pred1FastPathPredictorMilliseconds(void);
double getMlvLastRawUint16UnpackMilliseconds(void);
double getMlvLastRawUint16CopyMilliseconds(void);
int getMlvLastRawUint16PrefetchHit(void);
uint32_t mlvRawUint16PrefetchLookaheadForTesting(const mlvObject_t * video);
int mlvRawUint16PrefetchAllowedForTesting(const mlvObject_t * video);
uint64_t getMlvRawUint16PrefetchDecodeFailures(mlvObject_t * video);
double getMlvLastLlrawprocMilliseconds(void);
double getMlvLastRawFloatConvertMilliseconds(void);
double getMlvLastDebayeredFrameMilliseconds(void);
void resetMlvLastDebayerStageMilliseconds(void);
double getMlvLastDebayerWbPrepareMilliseconds(void);
double getMlvLastDebayerCaMilliseconds(void);
double getMlvLastDebayerKernelMilliseconds(void);
double getMlvLastDebayerWbUndoMilliseconds(void);
double getMlvLastProcessingMilliseconds(void);
double getMlvLastProcessed16TotalMilliseconds(void);
double getMlvLastProcessed16For8BitMilliseconds(void);
double getMlvLastProcessed16To8BitMilliseconds(void);
double getMlvLastProcessed16CacheStoreMilliseconds(void);
double getMlvLastProcessed8TotalMilliseconds(void);
double getMlvLastProcessed8CacheStoreMilliseconds(void);
int getMlvLastProcessed8DirectPathActive(void);
int getMlvLastProcessed8CacheHit(void);
int getMlvLastProcessed8CacheHitScaleFactor(void);
int getMlvLastProcessed8PrefetchHit(void);
int getMlvProcessed8PrefetchEnabledForTesting(const mlvObject_t * video);
int mlvWaitForProcessed8PrefetchIdleForTesting(mlvObject_t * video, uint32_t timeout_ms);
void mlvSetProcessed8PrefetchPauseBeforeStoreForTesting(mlvObject_t * video, int enabled);
int mlvWaitForProcessed8PrefetchPausedBeforeStoreForTesting(mlvObject_t * video,
                                                            uint32_t timeout_ms);

/* Phase 4B-v2/v3/v4 telemetry — for parity tests and diagnostics. Returns the
 * path taken on the most recent v2 entry on the calling thread:
 *   8 = v4 x8 full-XY pre-recon (Y-cropped if necessary)
 *   4 = x2 full-XY pre-recon (Y-cropped if necessary)
 *   3 = v3 full-XY pre-recon (Y-cropped if necessary)
 *   2 = v2 X-only pre-recon fallback
 *   0 = v2 entry not invoked / rejected before path selection. */
int mlv_phase4bv2_last_path_taken(void);
const char * mlv_phase4bv2_last_fallback_reason(void);
/* Number of source rows cropped from the bottom edge by the v3/v4 Y-crop
 * wrapper on the most recent v2 entry on the calling thread. 0 if no
 * Y-cropped path was taken or if the clip was already path-aligned. */
int mlv_phase4bv3_last_y_crop_rows(void);

/* Playback preview contract selected by the GUI/profile environment.
 * Sharp/Smooth is 0; Aggressive Performance is non-zero. */
void mlvSetPlaybackAggressivePreviewMode(int enabled);
int mlvPlaybackAggressivePreviewMode(void);

/* Round-3 item 1: GUI-selected playback preview resolution (proxy level).
 * -1 = Auto (per-scale tuned default), 0 = Full (no preview proxy),
 * 1 = Half (one internal halving per axis), 2 = Quarter (two halvings where
 * a core exists; falls back to Half otherwise). The MLVAPP_DISABLE_* env
 * kill switches keep precedence over this value. */
void mlvSetPlaybackProxyLevel(int level);
int mlvPlaybackProxyLevel(void);

/* Fast-mode x4 HQ escape hatch: when enabled, scale=4 Dual ISO playback may
 * use the same pre-recon fast-HQ gate as Aggressive preview without forcing
 * the preview rowscale override. */
void mlvSetPlaybackFastX4HqPathMode(int enabled);
int mlvPlaybackFastX4HqPathMode(void);

/* Test-only hook: clear cached Phase 4B env-var values so subsequent calls
 * re-read getenv(). Used by parity tests that flip the kill switches or HQ
 * fast-path override mid-process. */
void mlv_phase4bv_reset_env_cache_for_testing(void);

/* Unpacks the bits of a frame to get a bayer B&W image (without black level correction)
 * Needs memory to return to, sized: sizeof(float) * getMlvHeight(urvid) * getMlvWidth(urvid)
 * Output values will be in range 0-65535 (16 bit), float is only because AMAzE uses it */
int getMlvRawFrameUint16(mlvObject_t * video, uint64_t frameIndex, uint16_t * unpackedFrame);
int getMlvRawFrameProcessedUint16Direct(mlvObject_t * video,
                                        uint64_t frameIndex,
                                        uint16_t * outputFrame,
                                        int * bit_shift);
int getMlvRawFrameProcessedUint16DirectWithChromaSmooth(mlvObject_t * video,
                                                        uint64_t frameIndex,
                                                        uint16_t * outputFrame,
                                                        int * bit_shift,
                                                        int chroma_smooth_method);
int getMlvRawFrameProcessedUint16(mlvObject_t * video,
                                  uint64_t frameIndex,
                                  uint16_t * outputFrame,
                                  int * bit_shift);
void getMlvRawFrameFloat(mlvObject_t * video, uint64_t frameIndex, float * outputFrame);

/* Gets a debayered 16 bit frame */
void getMlvRawFrameDebayered(mlvObject_t * video, uint64_t frameIndex, uint16_t * outputFrame);

/* For processing only, no use to average library user ;) Camera RGB -> sRGB */
void getMlvCameraTosRGBMatrix(mlvObject_t * video, double * outputMatrix); /* Still havent had any success here */

/* Gets image aspect ratio according to RAWC block info, calculating from binnin + skipping values */
float getMlvAspectRatio(mlvObject_t * video);

/* Set imaginary lossless bit depth value */
void setMlvLosslessBpp(mlvObject_t * video);

/******************************** 
 ********* PRIVATE AREA *********
 ********************************/

/* Add as many of these as you want :) */
void an_mlv_cache_thread(mlvObject_t * video);

/* Test-only fault injection for cache-worker plane allocation. */
void mlvCacheSetAllocationFailureForTesting(int enabled);
void mlvCacheSetRawAcquisitionFailureForTesting(int enabled);

/* Test-only barrier for validating the async launch/count lifetime contract. */
void mlvCacheSetWorkerStartPauseForTesting(int enabled);
void mlvCacheSetWorkerBeforePublishPauseForTesting(int enabled);
int mlvCacheWorkerBeforePublishPausedForTesting(void);
void mlvCacheSetLifecyclePauseForTesting(int enabled);
int mlvCacheLifecyclePausedForTesting(void);

/* Test-only ordinal fault injection for cache resize allocations. */
void mlvCacheSetResizeAllocationFailureForTesting(int allocation_index);

/* Marks all frames as not cached */
void mark_mlv_uncached(mlvObject_t * video);

/* Clears cache by freeing then reallocating (RAM usage down until frames written) */
void clear_mlv_cache(mlvObject_t * video);

/* Cache window helpers */
int mlv_frame_in_cache_window(mlvObject_t * video, uint64_t frameIndex);
uint64_t mlv_cache_slot_for_frame(mlvObject_t * video, uint64_t frameIndex);
void mlv_cache_ensure_window(mlvObject_t * video, uint64_t frameIndex);
void mlv_cache_request_playback_preroll(mlvObject_t * video,
                                        uint64_t currentFrame,
                                        uint64_t lastFrameInclusive,
                                        uint64_t lookaheadFrames);

/* Returns 1 on success, or 0 if all are cached */
int find_mlv_frame_to_cache(mlvObject_t * video, uint64_t *index); /* Outputs to *index */

/* Adds one thread; query the active total through mlvCacheWorkerCount(). */
void add_mlv_cache_thread(mlvObject_t * video);
int mlvCacheWorkerCount(mlvObject_t * video);
int mlvCacheShouldStop(mlvObject_t * video);
void mlvCacheSetStop(mlvObject_t * video, int stop);

/* OLD DEPRACTEDFSDJKHJKLAJSKDLJ KLSDJKL AJSD LKSAJDLKSAJDLK DKJS */
void cache_mlv_frames(mlvObject_t * video);

/* Gets a debayered frame; how is it different from getMlvRawFrameDebayered?... it doesn't get it from cache ever
 * also you must allocate it some temporary memory because that's how it works and you shouldnt be looking anyway */
void get_mlv_raw_frame_debayered(mlvObject_t * video,
                                  uint64_t frame_index,
                                  float * temp_memory,
                                  uint16_t * output_frame,
                                  int debayer_type ); /* Debayer type: 0=bilinear 1=amaze */
void get_mlv_raw_frame_debayered_isolated_analysis(mlvObject_t * video,
                                                   uint64_t frame_index,
                                                   float * temp_memory,
                                                   uint16_t * output_frame,
                                                   int debayer_type ); /* Debayer type: 0=bilinear 1=amaze */
/* Cache-free isolated-analysis source for reduced thumbnails. This decodes and
 * runs llrawproc at full resolution through the direct path, then emits a
 * reduced RGB16 frame via Bayer->RGB block averaging. Returns 1 on success. */
int get_mlv_raw_frame_debayered_isolated_analysis_scaled(mlvObject_t * video,
                                                         uint64_t frame_index,
                                                         uint16_t * output_frame,
                                                         int scale_factor,
                                                         int threads,
                                                         int * out_width,
                                                         int * out_height,
                                                         int * out_scale_factor);

/* Thumbnail Creation with a downscaled raw image sub-sampling algorithm is used. */
int create_thumbnail(mlvObject_t * video, uint8_t * thumbnail_img, int downscaled_factor, int width, int height, int threads);

/* Thumbnail Creation with full debayer, but downscaled image processing */
void get_area_average_downscale_thumnail(mlvObject_t *video, int frame_index, int downscale_factor, int cpu_cores, unsigned char *out_buffer);

enum mlv_processed_thumbnail_settings_flags
{
    MLV_PROCESSED_THUMBNAIL_APPLY_WHITE_BALANCE   = 1u << 0,
    MLV_PROCESSED_THUMBNAIL_APPLY_EXPOSURE        = 1u << 1,
    MLV_PROCESSED_THUMBNAIL_APPLY_SIMPLE_CONTRAST = 1u << 2,
    MLV_PROCESSED_THUMBNAIL_APPLY_PIVOT           = 1u << 3,
    MLV_PROCESSED_THUMBNAIL_APPLY_SHADOWS         = 1u << 4,
    MLV_PROCESSED_THUMBNAIL_APPLY_HIGHLIGHTS      = 1u << 5,
    MLV_PROCESSED_THUMBNAIL_APPLY_VIBRANCE        = 1u << 6,
    MLV_PROCESSED_THUMBNAIL_APPLY_RAW_LEVELS      = 1u << 7,
    MLV_PROCESSED_THUMBNAIL_APPLY_CHROMA_SMOOTH   = 1u << 8
};

typedef struct mlv_processed_thumbnail_settings
{
    uint32_t flags;
    double white_balance_kelvin;
    double white_balance_tint;
    double exposure_stops;
    double simple_contrast;
    double pivot;
    double shadows;
    double highlights;
    double vibrance;
    float raw_black_level;
    int raw_white_level;
    int raw_bit_depth;
    int source_chroma_smooth_method;
} mlv_processed_thumbnail_settings_t;

/* Same live/cache-backed source and downscale path as
 * get_area_average_downscale_thumnail, but renders through a caller-owned
 * processing object. Returns 1 on success.
 * The optional settings are applied only to that caller-owned processing object;
 * this function never mutates video->processing, receipts, or playback preview
 * globals. Use the cache-free sibling for detached analysis workers. */
int get_area_average_downscale_thumnail_with_processing(
    mlvObject_t *video,
    int frame_index,
    int downscale_factor,
    int cpu_cores,
    processingObject_t *analysis_processing,
    const mlv_processed_thumbnail_settings_t *settings,
    unsigned char *out_buffer);

/* Cache-free sibling for detached analysis workers. It first tries the reduced
 * direct isolated-analysis RGB16 source, then falls back to full-resolution
 * get_mlv_raw_frame_debayered_isolated_analysis with bilinear debayering. Both
 * paths use the same downscale and caller-owned processing path as
 * get_area_average_downscale_thumnail_with_processing. Returns 1 on success and
 * never mutates video->processing, receipts, playback preview globals, or the
 * playback cache. */
int get_area_average_downscale_thumnail_with_processing_cachefree(
    mlvObject_t *video,
    int frame_index,
    int downscale_factor,
    int cpu_cores,
    processingObject_t *analysis_processing,
    const mlv_processed_thumbnail_settings_t *settings,
    unsigned char *out_buffer);

/* Thumbnail Creation with full debayer and downscale, before color processing */
void get_area_average_downscale_raw_thumnail(mlvObject_t *video, int frame_index, int downscale_factor, unsigned char *out_buffer);

#endif
