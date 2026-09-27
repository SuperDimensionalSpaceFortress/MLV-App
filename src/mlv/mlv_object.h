#ifndef _video_object_
#define _video_object_

/* For MLV headers */
#include "mlv.h"
/* TO have processingObject_t */
#include "../processing/processing_object.h"
#include "llrawproc/llrawproc_object.h"

/* I guess this has to happen for pthread_t */
#include "pthread.h"

#include "camid/camera_id.h"

/* cache states */
#define MLV_FRAME_NOT_CACHED 0
#define MLV_FRAME_IS_CACHED 1
#define MLV_FRAME_BEING_CACHED 2
#define MLV_PROCESSED_8BIT_CACHE_SLOTS 8 /* original known-good. A 2026-06-10 bump to 16 appeared to collapse prefetch hits, but that A/B was THERMALLY CONFOUNDED - a cache-8 control run on the same heat-soaked machine degraded identically (99%->56% hit). Cache size NOT proven to matter; re-test only from a cooled machine before changing. */
#define MLV_PROCESSED_16BIT_CACHE_SLOTS 2
#define MLV_RAW_UINT16_PREFETCH_SLOTS 4

/* Struct of index of video and audio frames for quick access */
typedef struct
{
    uint16_t frame_type;     /* VIDF = 1, AUDF = 2, VERS = 3 */
    uint16_t chunk_num;      /* MLV chunk number */
    uint32_t frame_number;   /* Unique frame number */
    uint32_t frame_size;     /* Size of frame data */
    uint64_t frame_offset;   /* Offset to the start of frame data */
    uint64_t frame_time;     /* Time of frame from the start of recording in microseconds */
    uint64_t block_offset;   /* Offset to the start of the block header */
} frame_index_t;

/* MLV App map file header (.MAPP) */
#define MAPP_VERSION 3
typedef struct {
    uint8_t     fileMagic[4];  /* MAPP */
    uint64_t    mapp_size;     /* total MAPP file size */
    uint8_t     mapp_version;  /* MAPP structure version */
    uint32_t    block_num;     /* total block count */
    uint32_t    video_frames;  /* total video frames */
    uint32_t    audio_frames;  /* total audio frames */
    uint32_t    vers_blocks;   /* total VERS blocks */
    uint64_t    audio_size;    /* total size of audio data in bytes */
    uint64_t    df_offset;     /* offset to the dark frame location */
} mapp_header_t;

/* Struct for MLV handling */
typedef struct {

    /* Amount of MLV chunks (.MLV, .M00, .M01, ...) */
    int filenum;
    uint64_t block_num; /* How many file blocks in MLV file */

    /* 0=no, 1=yes, mlv file open */
    int is_active;

    /* MLV/Lite file(s) */
    FILE ** file;
    char * path;
    pthread_mutex_t * main_file_mutex; /* One for each file */
    pthread_mutex_t g_mutexFind; /* 'g' mutexes should prevent pink frames */
    pthread_mutex_t g_mutexCount;
    /* Serializes stop -> quiesce -> cache topology mutation -> restart. */
    pthread_mutex_t g_mutexCacheLifecycle;
    int cache_lifecycle_users;
    int cache_closing;

    /* For access to MLV headers */
    mlv_file_hdr_t    MLVI;
    mlv_rawi_hdr_t    RAWI;
    mlv_rawc_hdr_t    RAWC;
    mlv_idnt_hdr_t    IDNT;
    mlv_expo_hdr_t    EXPO;
    mlv_lens_hdr_t    LENS;
    mlv_elns_hdr_t    ELNS;
    mlv_rtci_hdr_t    RTCI;
    mlv_wbal_hdr_t    WBAL;
    mlv_wavi_hdr_t    WAVI;
    mlv_diso_hdr_t    DISO;
    mlv_info_hdr_t    INFO;
    mlv_styl_hdr_t    STYL;
    mlv_vers_hdr_t    VERS;
    mlv_dark_hdr_t    DARK;
    mlv_vidf_hdr_t    VIDF; /* One of many VIDFs(don't know if they're different) */
    mlv_audf_hdr_t    AUDF; /* Last AUDF header read */

    char INFO_STRING[256]; /* String stored in INFO block */

    /* camid for cameras not listed in database */
    camera_id_t       camid;

    /* Dark frame info */
    uint64_t dark_frame_offset;

    /* Black and white level copy for resseting to the original */
    uint16_t original_black_level;
    uint16_t original_white_level;

    /* CURV block lookup table for linearising encoded values */
    uint16_t * linearise_lut;

    /* Video info */
    double      real_frame_rate; /* ...Because framerate is not explicitly stored in the file */
    double      frame_rate;      /* User may want to override it */
    uint32_t    frames;          /* Number of frames */
    uint32_t    frame_size;      /* NOT counting compression factor */
    int         compression_type;  /* mcraw compression type. legacy/binned: 6, default: 7 */
    frame_index_t * video_index;

    /* CinemaDNG folder source: opaque pointer to a dng_sequence_t (defined in
     * src/dng/dng_reader.h). Non-NULL only when this object was opened via
     * openDngFolderClip(); the per-frame decode path then reads .dng files
     * instead of an MLV/mcraw container. Kept void* so this widely-included
     * header does not pull in dng_reader.h. */
    void *      dng_sequence;

    /* Audio info */
    uint32_t    audios;          /* Number of audio blocks */
    frame_index_t * audio_index;

    /* Audio buffer pointer and size */
    uint8_t * audio_data;        /* Audio buffer pointer */
    uint64_t  audio_size;        /* Aligned usable audio size */
    uint64_t  audio_buffer_size; /* Full audio buffer size to be freed */

    /* Version info */
    uint32_t    vers_blocks;     /* Number of audio blocks */
    frame_index_t * vers_index;

    /* Image processing object pointer (it is to be made separately) */
    processingObject_t * processing;
    llrawprocObject_t * llrawproc;
    pthread_mutex_t llrawproc_mutex;
    pthread_mutex_t llrawproc_worker_mutex;
    llrawprocWorkerState_t * llrawproc_workers;
    uint32_t llrawproc_worker_capacity;

    /* Restricted lossless raw data bit depth */
    int lossless_bpp;

    /************************************************************
     *** CACHE AREA - used by getMlvProcessedFrame and things ***
     ************************************************************/

    /* 0 = no, 1 = (yes... cache threads are alive right now) */
    int is_caching;
    int cache_thread_count; /* Total active cache threads */
    uint64_t cache_next; /* Like a cache request (any non-zero frame) */
    /* Will be set to 1 for cache threads to stop (probably only by freeMlvObject) */
    int stop_caching;

    /* Decides whether or not AMaZE *has* to be used or not, normally disabled for smooth playback */
    int use_amaze;

    /* CA correction */
    //uint8_t ca_auto; /* off=0, on=1 */
    float ca_red;    /* Range -5..5 */
    float ca_blue;   /* Range -5..5 */

    /* Basically how much we can cache(can be set by MB or frames or bytes) */
    uint64_t cache_limit_bytes;
    uint64_t cache_limit_frames;
    uint64_t cache_limit_mb; /* How many MB of frames can be cached... 
     * Debayered frames are cached with 16 bit channel bitdepth (48bpp) */

    /* Windowed cache start frame and generation used to invalidate in-flight work when it shifts */
    uint64_t cache_start_frame;
    uint32_t cache_generation;

    uint8_t * cached_frames; /* Basically an array with as many elements as frames, cache states are defined above */
    uint16_t ** rgb_raw_frames; /* Pointers to 16bit cached RGB frames */

    /* A single cached frame, speeds up when asking for the same (non-cached) frame over and over again */
    int current_cached_frame_active;
    uint64_t current_cached_frame; int times_requested;
    uint16_t * rgb_raw_current_frame;
    uint64_t rgb_raw_current_frame_words;

    /* Reusable hot-path buffers to avoid per-frame malloc/free churn during playback */
    float * raw_debayer_temp_frame;
    uint64_t raw_debayer_temp_frame_pixels;
    uint16_t * rgb_processed_temp_frame;
    uint64_t rgb_processed_temp_frame_words;

    /* A single processed frame cache for the exact frame + thread-count path */
    int current_processed_frame_active;
    uint64_t current_processed_frame;
    int current_processed_frame_threads;
    uint64_t current_processed_frame_signature;
    uint16_t * rgb_processed_current_frame;
    uint64_t rgb_processed_current_frame_words;
    uint16_t * rgb_processed_frame_cache_16bit;
    uint64_t rgb_processed_frame_cache_16bit_words;
    uint8_t processed_16bit_cache_active[MLV_PROCESSED_16BIT_CACHE_SLOTS];
    uint64_t processed_16bit_cache_frame[MLV_PROCESSED_16BIT_CACHE_SLOTS];
    int processed_16bit_cache_threads[MLV_PROCESSED_16BIT_CACHE_SLOTS];
    uint64_t processed_16bit_cache_signature[MLV_PROCESSED_16BIT_CACHE_SLOTS];
    /* Phase 4A: per-slot playback scale factor (1, 2, 4, or 8). The field
     * is compared in slot-match logic so different preview scales cannot
     * collide. */
    int processed_16bit_cache_scale[MLV_PROCESSED_16BIT_CACHE_SLOTS];
    uint32_t processed_16bit_cache_next_slot;
    /* Phase 4B: rgb_frame_size in uint16 words that the slot offsets in
     * rgb_processed_frame_cache_16bit are laid out for. When the per-call
     * size differs (e.g. switching between scale=1 and scale=2), the
     * cache must be reset. */
    uint64_t processed_16bit_cache_unit_words;
    int current_processed_frame_8bit_active;
    uint64_t current_processed_frame_8bit;
    int current_processed_frame_8bit_threads;
    uint64_t current_processed_frame_8bit_signature;
    uint8_t * rgb_processed_current_frame_8bit;
    uint64_t rgb_processed_current_frame_8bit_bytes;
    uint8_t processed_8bit_cache_active[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    uint64_t processed_8bit_cache_frame[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    int processed_8bit_cache_threads[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    uint64_t processed_8bit_cache_signature[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    /* Phase 4A: per-slot playback scale factor (1, 2, 4, or 8). The field is
     * compared in slot-match logic and folded into the state signature so a
     * scale=1 entry never satisfies a reduced-scale lookup. */
    int processed_8bit_cache_scale[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    int processed_8bit_cache_phase4b_path[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    int processed_8bit_cache_phase4b_y_crop_rows[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    uint8_t processed_8bit_cache_state[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    uint8_t processed_8bit_cache_prefetched[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    uint32_t processed_8bit_cache_generation[MLV_PROCESSED_8BIT_CACHE_SLOTS];
    uint32_t processed_8bit_cache_next_slot;
    /* Phase 4B: rgb_frame_size that the slot offsets were laid out for.
     * When the per-call frame size differs from this (e.g. switching
     * between scale=1 and scale=2), the cache must be reset because the
     * slot offsets in the backing buffer are stale. */
    uint64_t processed_8bit_cache_unit_size;
    /* Phase 4A: most recently requested playback scale factor (1, 2, 4, or 8). */
    int playback_scale_factor_active;
    pthread_mutex_t processed8_prefetch_mutex;
    pthread_cond_t processed8_prefetch_cond;
    pthread_t processed8_prefetch_thread;
    processingObject_t * processed8_prefetch_processing;
    int processed8_prefetch_thread_started;
    int processed8_prefetch_stop;
    int processed8_prefetch_request_pending;
    int processed8_prefetch_worker_busy;
    uint64_t processed8_prefetch_request_frame;
    int processed8_prefetch_request_threads;
    uint64_t processed8_prefetch_last_request_frame;
    int processed8_prefetch_last_request_threads;
    uint64_t processed8_prefetch_last_state_signature;
    uint32_t processed8_prefetch_generation;
    /* Set by cache invalidation so the next request refreshes the worker's
     * private processing snapshot even when the state signature is unchanged
     * (some manually-invalidated state is intentionally absent from it). */
    int processed8_prefetch_snapshot_dirty;
    /* Deterministic cache-generation race seam. Production leaves both zero;
     * tests may pause a completed worker render immediately before publish. */
    int processed8_prefetch_test_pause_before_store;
    int processed8_prefetch_test_paused_before_store;
    int processed8_prefetch_test_idle_waiters;
    /* Phase E6: scaleFactor accompanying the most recent note_request. The
     * worker uses this to record the slot's `scale` lane (so render-thread
     * lookups at the same scale match) and to set
     * playback_scale_factor_active before the worker's render call (so the
     * direct8 path runs the same downsample / scaled debayer as the live
     * path would). The state signature already encodes scale via Phase 4A,
     * but the slot-match comparator additionally checks the raw scale lane,
     * so both must agree. */
    int processed8_prefetch_request_scale;
    int processed8_prefetch_last_request_scale;

    /* Massive block of memory for all frames that will be cached, pointers in rgb_raw_frames will point within here, 
     * using one big block block to try and avoid fragmentation (I feel that may be one of the causes of growth) */
    uint16_t * cache_memory_block;

    /* Small raw uint16 decode-ahead ring for playback */
    pthread_mutex_t raw_uint16_prefetch_mutex;
    pthread_cond_t raw_uint16_prefetch_cond;
    pthread_t raw_uint16_prefetch_thread;
    int raw_uint16_prefetch_thread_started;
    int raw_uint16_prefetch_stop;
    int raw_uint16_prefetch_request_pending;
    int raw_uint16_prefetch_worker_busy;
    uint64_t raw_uint16_prefetch_request_frame;
    uint64_t raw_uint16_prefetch_last_request_frame;
    uint32_t raw_uint16_prefetch_request_stride;
    uint32_t raw_uint16_prefetch_generation;
    uint16_t * raw_uint16_prefetch_cache;
    uint64_t raw_uint16_prefetch_cache_words;
    uint64_t raw_uint16_prefetch_slot_words;
    uint8_t raw_uint16_prefetch_slot_state[MLV_RAW_UINT16_PREFETCH_SLOTS];
    uint64_t raw_uint16_prefetch_slot_frame[MLV_RAW_UINT16_PREFETCH_SLOTS];
    uint32_t raw_uint16_prefetch_slot_generation[MLV_RAW_UINT16_PREFETCH_SLOTS];
    uint32_t raw_uint16_prefetch_next_slot;
    uint64_t raw_uint16_prefetch_decode_failures;

    /* How many cores, will not neccesarily determine number of threads made in any case, but helps */
    int cpu_cores; /* Default 4 */


} mlvObject_t;

#endif
