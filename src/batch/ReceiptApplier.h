#ifndef RECEIPTAPPLIER_H
#define RECEIPTAPPLIER_H

/* C API types — mlvObject_t and processingObject_t are anonymous typedefs,
 * so we must include the full header (forward declaration won't work). */
#include "../../src/mlv_include.h"
#include "LookAssistAnalysis.h"

#include <vector>

class ReceiptSettings;

/* LOOK-ASSIST-M16-CAST-6 (ReceiptApplier::DualIsoTraceLookAssist): what the run's own white-balance decision was made
 * from, so the dual-ISO trace can make the same decision on each arm's render (LA): the run's raw statistics, scene and
 * flavor, its base balance and the exposure (receipt units) its colour pictures were rendered at. valid = false: LA is
 * the temperature / tint passed in. */
struct ReceiptApplierDualIsoTraceLookAssist
{
    bool valid = false;
    lookassist::LookAssistStats stats;
    lookassist::LookAssistScene scene = lookassist::LookAssistScene::Shade;
    lookassist::LookAssistFlavor flavor = lookassist::LookAssistFlavor::Classic;
    int baseTemperature = 6000;
    int baseTint = 0;
    int analysisExposure = lookassist::LookAssistWhiteBalanceRequest::kLookAssistNoAnalysisExposure;
};

/* Applies parsed ReceiptSettings to the runtime mlvObject_t / processingObject_t
 * using the same C API calls the GUI's setSliders() triggers through its
 * signal chain.  This is the standalone batch-mode equivalent.
 *
 * Also provides a FINGERPRINT printer that reads back the actual runtime
 * state from the objects — proving settings reached the pipeline. */
class ReceiptApplier
{
public:
    /* Apply all CDNG-relevant receipt settings to the MLV pipeline.
     * receipt is non-const because the GUI logic mutates certain fields
     * (e.g. dualIsoForced, dualIso) during application. */
    static void applyToMlv(ReceiptSettings *receipt,
                            mlvObject_t *mlvObject,
                            processingObject_t *processingObject);

    /* Read back actual runtime state from mlvObject/processingObject and
     * print a structured [BATCH] FINGERPRINT line via BatchLogger.
     * Can be called even when no receipt was loaded (prints defaults). */
    static void printFingerprint(mlvObject_t *mlvObject,
                                 processingObject_t *processingObject);

    /* Batch/headless equivalent of Auto Look Assist.
     * Generates fresh clip-local DNG defaults from the currently opened MLV
     * instead of reusing a GUI receipt baseline captured from another clip.
     * masterScenePass is internal: a corroborated daylight clip that neither an accepted patch nor a
     * verified surface backs re-runs itself with the recorded-exposure daylight hypothesis off, i.e. exactly
     * as master analysed it. Callers leave it false. */
    static bool applyHeadlessLookAssist(ReceiptSettings *receipt,
                                        mlvObject_t *mlvObject,
                                        processingObject_t *processingObject,
                                        uint32_t analysisFrame,
                                        bool masterScenePass = false);

    /* The clip's recorded (as-shot) white balance as the app's temperature / tint controls
     * (tint in receipt units), decoded by WBAL.wb_mode: kelvin only in WB_KELVIN, the wbgain_* neutral
     * only in WB_CUSTOM (DNG sequences), presets mapped to their kelvin, default 6000 K. The ONE
     * decoder: MainWindow::setWhiteBalanceFromMlv calls it, and Look Assist (GUI and headless) uses it
     * as the fallback prior when no neutral patch can be trusted. */
    static bool asShotWhiteBalanceControls(mlvObject_t *mlvObject,
                                           int *temperature,
                                           int *tint);

    /* The dual-ISO recovery ISO Look Assist credits against the aperture-bounded EV100: the second ISO llrawproc
     * decoded from the DISO block at clip open (its isoValue is an encoding, not an ISO), and only for a clip whose
     * DISO block is VALID; 0 otherwise (a forced dual ISO has no recorded recovery ISO). */
    static int lookAssistRecoveryIso(mlvObject_t *mlvObject);

    /* The downscaled PROCESSED thumbnail (same source and path as
     * get_area_average_downscale_thumnail) rendered at an explicit exposure, through a private clone of
     * the live processing object -- nothing shared is mutated. Look Assist judges colour on a daylight
     * clip at the exposure it is about to apply, so the neutral-patch search sees the picture the user
     * will see whatever exposure the receipt currently holds. False when it could not be rendered. */
    static bool processedThumbnailAtExposure(mlvObject_t *mlvObject,
                                             int frameIndex,
                                             int downscaleFactor,
                                             int cpuCores,
                                             double exposureStops,
                                             unsigned char *outBuffer);

    /* The display-space exposure meter: the robust (median of three) display-referred statistics of the clip,
     * rendered at 15 / 50 / 85 % of its frames through the cache-free processed-thumbnail primitive with the
     * live exposure / contrast / shadows / highlights / vibrance, at the analysis downscale the caller took
     * its RAW thumbnail at. presetForLookAssistScene aims its exposure at them. The ONE meter: the GUI
     * (every playback scale) and the headless applier call this and nothing else, and nothing here reads
     * the playback scale, so the same clip gets the same statistics wherever it is analysed. out->median > 0
     * only when at least one frame rendered; false (out untouched) otherwise. validSamples, when given,
     * receives how many of the three frames rendered. */
    static bool lookAssistDisplayMeter(mlvObject_t *mlvObject,
                                       int analysisFrame,
                                       int downscaleFactor,
                                       int cpuCores,
                                       lookassist::LookAssistStats *out,
                                       int *validSamples = nullptr);

    /* The same processed thumbnail at an explicit exposure AND white balance (temperature in K, tint in
     * receipt units), through a private clone: the exposure at the PLANNED stops (the preset's, without the
     * display offset the live viewport adds) and the white balance under test, nothing else varied. isolated =
     * the cache-free render whose raw read is ALSO read-only on llrawproc (pixel maps, their versions and
     * status, force search, stripe one-shot go to a per-thread shadow); only measure-only analysis passes it
     * (lookAssistMeasureOnlyRenderer). The cache-backed render is what the GUI sync path and the headless
     * applier use for every Look Assist thumbnail that decides something. The ONE renderer behind the
     * daylight refinement for GUI sync (async is routed to sync) and headless. */
    static bool processedThumbnailAtBalance(mlvObject_t *mlvObject,
                                            int frameIndex,
                                            int downscaleFactor,
                                            int cpuCores,
                                            double exposureStops,
                                            int temperature,
                                            int tint,
                                            bool isolated,
                                            unsigned char *outBuffer,
                                            bool displayLevels = false);

    /* processedThumbnailAtBalance + analyzeLookAssistThumbnail as the callback the shared white-balance
     * resolution asks about the picture. */
    static lookassist::LookAssistRenderBalanceFn lookAssistBalanceRenderer(mlvObject_t *mlvObject,
                                                                     int frameIndex,
                                                                     int downscaleFactor,
                                                                     int thumbWidth,
                                                                     int thumbHeight,
                                                                     int cpuCores,
                                                                     bool isolated,
                                                                     bool displayLevels = false);

    /* The renderer for a MEASURE-ONLY check (the window-lit verification in both consumers): the isolated,
     * read-only render above, so measuring cannot change any shared processing or low-level raw state. */
    static lookassist::LookAssistRenderBalanceFn lookAssistMeasureOnlyRenderer(mlvObject_t *mlvObject,
                                                                         int frameIndex,
                                                                         int downscaleFactor,
                                                                         int thumbWidth,
                                                                         int thumbHeight,
                                                                         int cpuCores);

    /* The same measure-only render at the DISPLAY's levels (MLV_PROCESSED_THUMBNAIL_DISPLAY_LEVELS): the window-lit
     * check's last question, whether the balance it applies is neutral where the display shows the surface
     * (LOOK-ASSIST-M16-CAST-1). Identical to the render above outside HQ dual-ISO. */
    static lookassist::LookAssistRenderBalanceFn lookAssistMeasureOnlyDisplayRenderer(mlvObject_t *mlvObject,
                                                                                int frameIndex,
                                                                                int downscaleFactor,
                                                                                int thumbWidth,
                                                                                int thumbHeight,
                                                                                int cpuCores);

    /* LOOK-ASSIST-M16-CAST-3 step 1, MEASURE-ONLY (changes no applied value): for an HQ dual-ISO clip, the
     * existing histogram exposure match (auto -2) run through isolated, read-only renders on the judgement frame
     * and on six evenly spaced frames, against the nominal match (-1) the clip renders with. On the judgement frame
     * it also renders four display-level variants at the given exposure / balance (nominal; measured ev + black
     * delta; nominal ev + measured black delta; measured ev + nominal black delta) and scores each with the CAST-3
     * dark-band magenta metrics over one mask fixed on the nominal render. Returns the trace fields (empty when
     * the clip is not HQ dual-ISO with two different ISOs). Both consumers log the same line. Off (empty, nothing
     * rendered) unless MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE=1: it costs minutes of renders on a 5K clip.
     * LOOK-ASSIST-M16-CAST-4 replaced the CAST-3 renders (lever (d) is retired) with a levels probe (diso_levels, no
     * render) and the CAST-4 arms on frames {judgement, 374, 749} at the PINNED exposure / balance 3.80 EV / 6724 / 0
     * (never the run's Look Assist output, which only gets logged): R0 today's HQ recon (fixes the mask), A1 as-shot
     * WB, A3 the preview recon, A6 the measured bright clip, A7a/A7b dark noise x0.5/x2, A8 (only when the dark-field
     * floor is off) the measured dark-field black, all scored DBM/PSH/BLK over R0's mask; A5 Look Assist off (exposure
     * 0, as-shot WB, default grade) on its own mask, never graded. imageSink, when given, receives every arm render. */
    using DualIsoTraceImageSink = std::function<void(const QString &name, int width, int height,
                                                     const unsigned char *rgb)>;
    using DualIsoTraceLookAssist = ReceiptApplierDualIsoTraceLookAssist;
    static QString lookAssistDualIsoMatchTrace(mlvObject_t *mlvObject,
                                               int judgementFrame,
                                               int downscaleFactor,
                                               double exposureStops,
                                               int temperature,
                                               int tint,
                                               const DualIsoTraceImageSink &imageSink = DualIsoTraceImageSink(),
                                               const DualIsoTraceLookAssist &lookAssist = DualIsoTraceLookAssist());

    /* LOOK-ASSIST-M16-CAST-6 SEAM / BLOCK (measure-only, white balance cannot move them): on a 16-bit RGGB Bayer
     * capture (black16 subtracted; quad = (x&~1, y&~1), R, G = (Gr+Gb)/2, B all > 0) and a same-size switch map (a quad
     * is flagged when any of its pixels is). box = display pixels [x0, y0, x1, y1) at the given factor (NULL or empty
     * after clamping = the whole frame). SEAM = |median_in - median_out| of log2(R/G) plus the same of log2(B/G), over
     * the box's quads within 16 quads (chessboard) of a map boundary quad; BLOCK = IQR(log2 R/G) + IQR(log2 B/G) over
     * the box's flagged quads; blockTileMedian = the median BLOCK over the frame's 32x32-quad tiles with >= 256 flagged
     * quads. -1 = not enough quads (50 per side / 50 flagged). */
    struct DisoQuadChroma
    {
        double seam = -1.0;
        double block = -1.0;
        double blockTileMedian = -1.0;
        long long ringIn = 0;
        long long ringOut = 0;
        long long flagged = 0;
        int tiles = 0;
    };
    static DisoQuadChroma lookAssistDisoQuadChroma(const uint16_t *bayer, int width, int height, int black16,
                                                   const unsigned char *map, int mapWidth, int mapHeight, int factor,
                                                   const int *box);

    /* LOOK-ASSIST-M16-CAST-5 P1 (measure-only): the coincidence of a display render with a raw-resolution switch map
     * (1 = flagged). Quads are the 2x2 CFA cells (x&~1, y&~1), flagged when any of their pixels is; display pixel
     * (dx, dy) averages raw [factor*dx, factor*dx+factor) x [factor*dy, ...) and is flagged when any quad that block
     * touches is. Over the render's HI mask (luma 121..235): c = the flagged share of its LAV pixels (R-G >= 8 and B-G
     * >= 8), r = the flagged share of the rest. displayFlags, when given, receives the per-display-pixel flags. */
    struct DisoProvenance
    {
        double c;
        double r;
        long long lav;
        long long lavFlagged;
        long long other;
        long long otherFlagged;
        long long rawFlagged;
        long long displayFlagged;
    };
    static DisoProvenance lookAssistDisoProvenance(const unsigned char *rgb, int width, int height,
                                                   const unsigned char *rawMap, int rawWidth, int rawHeight,
                                                   int factor, std::vector<unsigned char> *displayFlags = nullptr);

private:
    ReceiptApplier() = delete; /* Pure static — no instances */
};

#endif // RECEIPTAPPLIER_H
