#ifndef RECEIPTAPPLIER_H
#define RECEIPTAPPLIER_H

/* C API types — mlvObject_t and processingObject_t are anonymous typedefs,
 * so we must include the full header (forward declaration won't work). */
#include "../../src/mlv_include.h"

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
     * instead of reusing a GUI receipt baseline captured from another clip. */
    static bool applyHeadlessLookAssist(ReceiptSettings *receipt,
                                        mlvObject_t *mlvObject,
                                        processingObject_t *processingObject,
                                        uint32_t analysisFrame);

    /* The clip's recorded (as-shot) white balance as the app's temperature / tint controls
     * (tint in receipt units): the WBAL neutral gains when present, else its kelvin. Used by
     * Look Assist (GUI and headless) as the fallback when no neutral patch can be trusted. */
    static bool asShotWhiteBalanceControls(mlvObject_t *mlvObject,
                                           int *temperature,
                                           int *tint);

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

private:
    ReceiptApplier() = delete; /* Pure static — no instances */
};

#endif // RECEIPTAPPLIER_H
