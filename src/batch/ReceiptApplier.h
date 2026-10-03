#ifndef RECEIPTAPPLIER_H
#define RECEIPTAPPLIER_H

/* C API types — mlvObject_t and processingObject_t are anonymous typedefs,
 * so we must include the full header (forward declaration won't work). */
#include "../../src/mlv_include.h"
#include "LookAssistAnalysis.h"

class ReceiptSettings;

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

    /* The same processed thumbnail at an explicit exposure AND white balance (temperature in K, tint in
     * receipt units), through a private clone: the exposure at the PLANNED stops (the preset's, without the
     * display offset the live viewport adds) and the white balance under test, nothing else varied. isolated =
     * the cache-free render a detached worker would use; no production caller passes it (a daylight scene
     * never reaches the async worker), the cache-backed render is what the GUI sync path and the headless
     * applier use for every other Look Assist thumbnail. The ONE renderer behind the daylight refinement
     * for GUI sync (async is routed to sync) and headless. */
    static bool processedThumbnailAtBalance(mlvObject_t *mlvObject,
                                            int frameIndex,
                                            int downscaleFactor,
                                            int cpuCores,
                                            double exposureStops,
                                            int temperature,
                                            int tint,
                                            bool isolated,
                                            unsigned char *outBuffer);

    /* processedThumbnailAtBalance + analyzeLookAssistThumbnail as the callback the shared white-balance
     * resolution asks about the picture. */
    static lookassist::LookAssistRenderBalanceFn lookAssistBalanceRenderer(mlvObject_t *mlvObject,
                                                                     int frameIndex,
                                                                     int downscaleFactor,
                                                                     int thumbWidth,
                                                                     int thumbHeight,
                                                                     int cpuCores,
                                                                     bool isolated);

private:
    ReceiptApplier() = delete; /* Pure static — no instances */
};

#endif // RECEIPTAPPLIER_H
