#include "ReceiptApplier.h"
#include "BatchContext.h"
#include "BatchLogger.h"
#include "LookAssistAnalysis.h"
#include "ReceiptSafety.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../platform/qt/DualIsoPatternMapping.h"
#include "../mlv/llrawproc/dualiso.h"

#include <QByteArray>
#include <QElapsedTimer>
#include <QFileInfo>
#include <QtGlobal>

#include <algorithm>
#include <cmath>
#include <cstring>

namespace
{

using namespace lookassist;

static uint16_t headlessAutoCorrectRawBlackLevel( mlvObject_t *mlvObject )
{
    int factor = 1;
    switch( getMlvBitdepth( mlvObject ) )
    {
        case 10: factor = 16;
            break;
        case 12: factor = 4;
            break;
        default:
            break;
    }
    if( getMlvOriginalBlackLevel( mlvObject ) >= (1700 / factor)
     && getMlvOriginalBlackLevel( mlvObject ) <= (2200 / factor) )
        return getMlvOriginalBlackLevel( mlvObject );

    if( getMlvCameraModel( mlvObject ) == 0x80000218
     || getMlvCameraModel( mlvObject ) == 0x80000261 )
        return 1792 / factor;

    return 2048 / factor;
}

static bool headlessDualIsoEnabled( ReceiptSettings *receipt, mlvObject_t *mlvObject )
{
    return receipt
        && mlvObject
        && llrpGetDualIsoValidity( mlvObject ) != DISO_INVALID
        && receipt->dualIso() > 0;
}

static uint16_t headlessAutoCorrectRawWhiteLevel( ReceiptSettings *receipt, mlvObject_t *mlvObject )
{
    if( !mlvObject ) return 0;

    const int originalWhite = getMlvOriginalWhiteLevel( mlvObject );
    int white = originalWhite;

    if( !receipt || !receipt->rawFixesEnabled() )
        return static_cast<uint16_t>( qBound( 0, white, 65535 ) );

    const bool restrictedLossless =
        ( mlvObject->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92 )
        && originalWhite > 0
        && originalWhite < 15000;

    if( restrictedLossless && headlessDualIsoEnabled( receipt, mlvObject ) )
    {
        white = originalWhite;
    }

    return static_cast<uint16_t>( qBound( 0, white, 65535 ) );
}

static uint16_t headlessRestrictedLosslessDualIsoOutputWhiteLevel( ReceiptSettings *receipt,
                                                                   mlvObject_t *mlvObject )
{
    if( !mlvObject ) return 0;

    const int originalWhite = getMlvOriginalWhiteLevel( mlvObject );
    const bool restrictedLossless =
        ( mlvObject->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92 )
        && originalWhite > 0
        && originalWhite < 15000;

    if( !restrictedLossless || !headlessDualIsoEnabled( receipt, mlvObject ) )
        return static_cast<uint16_t>( qBound( 0, originalWhite, 65535 ) );

    const int publishedWhite =
        mlvObject->llrawproc ? mlvObject->llrawproc->dng_white_level : 0;
    if( publishedWhite > originalWhite && publishedWhite <= 16383 )
    {
        return static_cast<uint16_t>( publishedWhite );
    }

    const int black = headlessAutoCorrectRawBlackLevel( mlvObject );
    const int range = qMax( 1, originalWhite - black );
    const int bitDepth = qBound( 1, (int)std::ceil( std::log2( (double)range ) ), 14 );
    const int shift = qMax( 0, 14 - bitDepth );
    const int scaledWhite = range * ( 1 << shift );
    return static_cast<uint16_t>( qBound( 0, qMax( originalWhite, scaledWhite ), 16383 ) );
}

static void captureHeadlessLookAssistBaseline( ReceiptSettings *receipt,
                                               mlvObject_t *mlvObject )
{
    if( !receipt ) return;

    receipt->setLookAssistBaselineExposure( receipt->exposure() );
    receipt->setLookAssistBaselineContrast( receipt->contrast() );
    receipt->setLookAssistBaselinePivot( receipt->pivot() );
    receipt->setLookAssistBaselineTemperature( receipt->temperature() == -1
                                             ? 6000
                                             : receipt->temperature() );
    receipt->setLookAssistBaselineTint( receipt->tint() );
    receipt->setLookAssistBaselineVibrance( receipt->vibrance() );
    receipt->setLookAssistBaselineShadows( receipt->shadows() );
    receipt->setLookAssistBaselineHighlights( receipt->highlights() );
    receipt->setLookAssistBaselineStretchX( receipt->stretchFactorX() > 0
                                          ? receipt->stretchFactorX()
                                          : 1.0 );
    receipt->setLookAssistBaselineStretchY( receipt->stretchFactorY() > 0
                                          ? receipt->stretchFactorY()
                                          : 1.0 );

    if( receipt->rawBlack() != -1 )
        receipt->setLookAssistBaselineRawBlack( receipt->rawBlack() );
    else if( mlvObject )
        receipt->setLookAssistBaselineRawBlack( (int)getMlvOriginalBlackLevel( mlvObject ) * 10 );
    else
        receipt->setLookAssistBaselineRawBlack( -1 );

    if( receipt->rawWhite() != -1 )
        receipt->setLookAssistBaselineRawWhite( receipt->rawWhite() );
    else if( mlvObject )
        receipt->setLookAssistBaselineRawWhite( (int)getMlvOriginalWhiteLevel( mlvObject ) );
    else
        receipt->setLookAssistBaselineRawWhite( -1 );

    receipt->setLookAssistBaselineChromaSmooth( receipt->chromaSmooth() );
    receipt->setLookAssistBaselineValid( true );
}

static void restoreHeadlessLookAssistBaseline( ReceiptSettings *receipt )
{
    if( !receipt || !receipt->lookAssistBaselineValid() ) return;

    receipt->setExposure( receipt->lookAssistBaselineExposure() );
    receipt->setContrast( receipt->lookAssistBaselineContrast() );
    receipt->setPivot( receipt->lookAssistBaselinePivot() );
    receipt->setTemperature( receipt->lookAssistBaselineTemperature() );
    receipt->setTint( receipt->lookAssistBaselineTint() );
    receipt->setVibrance( receipt->lookAssistBaselineVibrance() );
    receipt->setShadows( receipt->lookAssistBaselineShadows() );
    receipt->setHighlights( receipt->lookAssistBaselineHighlights() );
    receipt->setRawBlack( receipt->lookAssistBaselineRawBlack() );
    receipt->setRawWhite( receipt->lookAssistBaselineRawWhite() );
    receipt->setChromaSmooth( qBound( 0, receipt->lookAssistBaselineChromaSmooth(), 3 ) );
}

static void applyHeadlessRawLevelsAutoFix( ReceiptSettings *receipt,
                                           mlvObject_t *mlvObject,
                                           processingObject_t *processingObject )
{
    if( !receipt || !mlvObject || !processingObject ) return;

    const uint16_t black = headlessAutoCorrectRawBlackLevel( mlvObject );
    const uint16_t white = headlessAutoCorrectRawWhiteLevel( receipt, mlvObject );

    receipt->setRawBlack( black * 10 );
    receipt->setRawWhite( white );

    setMlvBlackLevel( mlvObject, black );
    processingSetBlackLevel( processingObject, black, getMlvBitdepth( mlvObject ) );
    setMlvWhiteLevel( mlvObject, white );
    processingSetWhiteLevel( processingObject, white, getMlvBitdepth( mlvObject ) );
    llrpResetFpmStatus( mlvObject );
    llrpResetBpmStatus( mlvObject );
    llrpResetDngBWLevels( mlvObject );
}

}

/* -----------------------------------------------------------------------
 * applyToMlv()
 *
 * Replicates the NET EFFECT of MainWindow::setSliders() on the
 * mlvObject_t / processingObject_t, bypassing the GUI signal chain.
 *
 * The code below is a faithful extraction of every C API call that
 * setSliders() triggers through its setToolButton*() → toolButton*Changed()
 * signal chain, plus the direct struct assignments for dual ISO.
 *
 * ORDERING matches setSliders() exactly.
 * ----------------------------------------------------------------------- */

void ReceiptApplier::applyToMlv(ReceiptSettings *receipt,
                                 mlvObject_t *mlvObject,
                                 processingObject_t *processingObject)
{
    /* ---- Raw fixes enable/disable ---- */
    llrpSetFixRawMode( mlvObject, (int)receipt->rawFixesEnabled() );

    /* ---- Focus pixels ----
     * -1 = auto-detect (first load), else use receipt value */
    if( receipt->focusPixels() == -1 )
    {
        llrpSetFocusPixelMode( mlvObject, llrpDetectFocusDotFixMode( mlvObject ) );
    }
    else
    {
        llrpSetFocusPixelMode( mlvObject, receipt->focusPixels() );
    }
    llrpResetFpmStatus( mlvObject );
    llrpResetBpmStatus( mlvObject );

    /* ---- Focus pixels interpolation method ---- */
    llrpSetFocusPixelInterpolationMethod( mlvObject, receipt->fpiMethod() );

    /* ---- Bad pixels ---- */
    llrpSetBadPixelMode( mlvObject, receipt->badPixels() );
    llrpResetBpmStatus( mlvObject );

    /* ---- Bad pixels search method ---- */
    llrpSetBadPixelSearchMethod( mlvObject, receipt->bpsMethod() );
    llrpResetBpmStatus( mlvObject );

    /* ---- Bad pixels interpolation method ---- */
    llrpSetBadPixelInterpolationMethod( mlvObject, receipt->bpiMethod() );

    /* ---- Chroma smooth ----
     * Receipt stores toolButton index 0-3 which maps 1:1 to
     * CS_OFF=0, CS_2x2=1, CS_3x3=2, CS_5x5=3 enum values. */
    llrpSetChromaSmoothMode( mlvObject, receipt->chromaSmooth() );

    /* ---- Pattern noise ---- */
    llrpSetPatternNoiseMode( mlvObject, receipt->patternNoise() );

    /* ---- Upside down ---- */
    processingSetTransformation( processingObject, receipt->upsideDown() );

    /* ---- Vertical stripes ----
     * -1 = auto-detect: enable for Canon 5D3 (cameraModel 0x80000285) */
    if( receipt->verticalStripes() == -1 )
    {
        if( getMlvCameraModel( mlvObject ) == 0x80000285 )
            llrpSetVerticalStripeMode( mlvObject, 1 );
        else
            llrpSetVerticalStripeMode( mlvObject, 0 );
    }
    else
    {
        llrpSetVerticalStripeMode( mlvObject, receipt->verticalStripes() );
    }
    llrpComputeStripesOn( mlvObject );
    llrpResetFpmStatus( mlvObject );
    llrpResetBpmStatus( mlvObject );

    /* ==== Dual ISO — complex logic faithfully extracted from setSliders() ==== */

    receiptSanitizeClipLocalDualIsoState( receipt, mlvObject );

    /* Step 1: Resolve dualIsoForced (-1 = uninitialized) */
    if( receipt->dualIsoForced() == -1 )
    {
        receipt->setDualIsoForced( llrpGetDualIsoValidity( mlvObject ) );
    }
    else if( receipt->dualIsoForced() == DISO_FORCED
             && llrpGetDualIsoValidity( mlvObject ) == DISO_VALID )
    {
        receipt->setDualIsoForced( DISO_VALID );
    }
    else if( receipt->dualIsoForced() == DISO_VALID
             && llrpGetDualIsoValidity( mlvObject ) != DISO_VALID )
    {
        receipt->setDualIsoForced( DISO_FORCED );
    }

    /* Step 2: If forced, set validity flag */
    if( receipt->dualIsoForced() == DISO_FORCED )
    {
        llrpSetDualIsoValidity( mlvObject, 1 );
    }

    /* Step 3: Reset diso_auto_correction sign (matches GUI logic) */
    if( mlvObject->llrawproc->diso_auto_correction > 0 )
    {
        mlvObject->llrawproc->diso_auto_correction =
            -mlvObject->llrawproc->diso_auto_correction;
    }

    /* Step 4: Handle auto-corrected vs non-auto-corrected dual ISO */
    const int requestedDualIsoMode = receipt->dualIso();
    if( !receipt->dualIsoAutoCorrected() )
    {
        if( requestedDualIsoMode != 2 && receipt->dualIsoForced() == DISO_VALID )
        {
            /* Enable dual ISO if the two ISO levels actually differ */
            if( mlvObject->llrawproc->diso1 != mlvObject->llrawproc->diso2 )
            {
                receipt->setDualIso( 1 );
            }
            else
            {
                receipt->setDualIso( 0 );
            }
        }
        else if( requestedDualIsoMode != 2 )
        {
            receipt->setDualIso( 0 );
        }

        if( receipt->dualIsoForced() == DISO_VALID || requestedDualIsoMode == 2 )
        {
            mlvObject->llrawproc->diso_pattern = 0;
            mlvObject->llrawproc->diso_auto_correction = -1;
            mlvObject->llrawproc->diso_ev_correction = 1;
            mlvObject->llrawproc->diso_black_delta = -1;
        }
        else
        {
            /* Not VALID — set pattern/ev/black to zeroed defaults
             * (mirrors the GUI setting combobox=0, sliders=0) */
            mlvObject->llrawproc->diso_pattern = 0;
            mlvObject->llrawproc->diso_ev_correction = 0;
            mlvObject->llrawproc->diso_black_delta = 0;
        }

        /* Forced overrides — after the above branches */
        if( receipt->dualIsoForced() == DISO_FORCED )
        {
            mlvObject->llrawproc->diso_pattern = 0;
            mlvObject->llrawproc->diso_auto_correction = -2;
            mlvObject->llrawproc->diso_ev_correction = 1;
            mlvObject->llrawproc->diso_black_delta = -1;
        }
    }
    else
    {
        /* Auto-corrected: apply receipt values through the same UI-to-core
         * mapping used by on_DualIsoPatternComboBox_currentIndexChanged,
         *  on_horizontalSliderDualIsoEvCorrection_valueChanged,
         *  on_horizontalSliderDualIsoBlackDelta_valueChanged) */
        mlvObject->llrawproc->diso_pattern =
            dualIsoCorePatternFromUiIndex( receipt->dualIsoPattern() );

        /* EV correction: receipt stores the slider int value.
         * Slider value 1 is special (triggers auto-correct toggle),
         * other values are divided by 200.0 for the actual EV offset. */
        int evSliderVal = receipt->dualIsoEvCorrection();
        if( evSliderVal != 1 )
        {
            mlvObject->llrawproc->diso_ev_correction = evSliderVal / 200.0;
        }
        else
        {
            /* Value 1 = toggle auto correction sign */
            mlvObject->llrawproc->diso_auto_correction =
                -mlvObject->llrawproc->diso_auto_correction;
        }

        /* Black delta: -1 is special (triggers auto-correct toggle),
         * other values are used directly. */
        int bdSliderVal = receipt->dualIsoBlackDelta();
        if( bdSliderVal != -1 )
        {
            mlvObject->llrawproc->diso_black_delta = bdSliderVal;
        }
        else
        {
            mlvObject->llrawproc->diso_auto_correction =
                -mlvObject->llrawproc->diso_auto_correction;
        }
    }

    /* Step 5: Set dual ISO mode and reset levels */
    llrpSetDualIsoMode( mlvObject, receipt->dualIso() );
    processingSetBlackAndWhiteLevel( mlvObject->processing,
                                     getMlvBlackLevel( mlvObject ),
                                     getMlvWhiteLevel( mlvObject ),
                                     getMlvBitdepth( mlvObject ) );
    llrpResetDngBWLevels( mlvObject );

    /* Step 6: Dual ISO interpolation / alias map / fullres blending */
    llrpSetDualIsoInterpolationMethod( mlvObject, receipt->dualIsoInterpolation() );
    llrpSetDualIsoAliasMapMode( mlvObject, receipt->dualIsoAliasMap() );
    llrpSetDualIsoFullResBlendingMode( mlvObject, receipt->dualIsoFrBlending() );

    /* ---- Deflicker target ---- */
    llrpSetDeflickerTarget( mlvObject, receipt->deflickerTarget() );

    /* ---- Dark frame ----
     * Load external dark frame file first (if valid), then set mode. */
    {
        QString dfName = receipt->darkFrameFileName();
        if( QFileInfo( dfName ).exists()
            && dfName.endsWith( QStringLiteral(".MLV"), Qt::CaseInsensitive ) )
        {
#ifdef Q_OS_UNIX
            QByteArray dfBytes = dfName.toUtf8();
#else
            QByteArray dfBytes = dfName.toLatin1();
#endif
            char errorMessage[256] = { 0 };
            int ret = llrpValidateExtDarkFrame( mlvObject, dfBytes.data(), errorMessage );
            if( !ret )
            {
                llrpInitDarkFrameExtFileName( mlvObject, dfBytes.data() );
                if( errorMessage[0] )
                {
                    BatchLogger::err( QStringLiteral("[BATCH] WARNING dark frame: %1\n")
                                          .arg( QString(errorMessage) ) );
                }
            }
            else
            {
                BatchLogger::err( QStringLiteral("[BATCH] WARNING dark frame rejected: %1\n")
                                      .arg( QString(errorMessage) ) );
                llrpFreeDarkFrameExtFileName( mlvObject );
            }
        }
        else
        {
            llrpFreeDarkFrameExtFileName( mlvObject );
        }

        /* Set dark frame mode (0=off, 1=ext, 2=int).
         * If ext/int requested but no dark frame available, force off. */
        int dfMode = receipt->darkFrameEnabled();
        if( dfMode == -1 )
        {
            /* Auto-detect: use internal dark frame if available */
            if( llrpGetDarkFrameIntStatus( mlvObject ) )
                dfMode = 2;
            else
                dfMode = 0;
        }
        if( dfMode > 0 && !llrpGetDarkFrameExtStatus( mlvObject )
                       && !llrpGetDarkFrameIntStatus( mlvObject ) )
        {
            dfMode = 0;
        }
        llrpSetDarkFrameMode( mlvObject, dfMode );

        /* Dark frame affects dual ISO correction — match GUI behavior */
        if( mlvObject->llrawproc->diso_auto_correction > 0 )
        {
            mlvObject->llrawproc->diso_auto_correction =
                -mlvObject->llrawproc->diso_auto_correction;
            mlvObject->llrawproc->diso_black_delta = -1;
        }
        llrpResetBpmStatus( mlvObject );
        llrpComputeStripesOn( mlvObject );
    }

    /* ---- Raw black / white levels ----
     * -1 = use file defaults (do not override). */
    if( receipt->rawWhite() != -1 )
    {
        int wl = receipt->rawWhite();
        /* Clamp: white must be above black */
        int bl = (receipt->rawBlack() != -1) ? (int)(receipt->rawBlack() / 10.0)
                                             : getMlvBlackLevel( mlvObject );
        if( wl <= bl + 1 ) wl = bl + 2;

        setMlvWhiteLevel( mlvObject, wl );
        processingSetWhiteLevel( processingObject, wl, getMlvBitdepth( mlvObject ) );
        llrpResetFpmStatus( mlvObject );
        llrpResetBpmStatus( mlvObject );
    }

    if( receipt->rawBlack() != -1 )
    {
        double rawBlack = receipt->rawBlack() / 10.0;
        /* Clamp: black must be below white */
        if( rawBlack >= getMlvWhiteLevel( mlvObject ) - 1 )
            rawBlack = getMlvWhiteLevel( mlvObject ) - 2;

        setMlvBlackLevel( mlvObject, rawBlack );
        processingSetBlackLevel( processingObject, rawBlack, getMlvBitdepth( mlvObject ) );
        llrpResetFpmStatus( mlvObject );
        llrpResetBpmStatus( mlvObject );
    }

    /* ---- AgX tonemap ----
     * Phase E7: previously the AgX gate in processing_can_use_basic_matrix_fast_path
     * returned false for AgX-on receipts, so the indirect path was taken regardless
     * of what the receipt requested -- which masked the fact that ReceiptApplier
     * never propagated <agx>0/1</agx> to the processing object. Now that AgX
     * receipts can use the direct8 fast path, the AgX flag must reach the
     * processing object verbatim, otherwise the runtime default of AgX=1 (set
     * in initProcessingObject) sticks regardless of what the user requested. */
    if( receipt->agx() )
    {
        processingEnableAgX( processingObject );
    }
    else
    {
        processingDisableAgX( processingObject );
    }

    /* Final cache reset — ensures all settings take effect on next frame read */
    resetMlvCache( mlvObject );
    resetMlvCachedFrame( mlvObject );
}

int ReceiptApplier::lookAssistRecoveryIso(mlvObject_t *mlvObject)
{
    if( !mlvObject || !mlvObject->llrawproc || llrpGetDualIsoValidity( mlvObject ) != DISO_VALID ) return 0;
    return mlvObject->llrawproc->diso2;
}

bool ReceiptApplier::asShotWhiteBalanceControls(mlvObject_t *mlvObject, int *temperature, int *tint)
{
    if( !mlvObject || !temperature || !tint ) return false;

    // The ONE mode-aware decoder of WBAL (MainWindow::setWhiteBalanceFromMlv calls this; there is no
    // second copy). The header's contract: kelvin is valid only in WB_KELVIN and the wbgain_* neutral
    // only in WB_CUSTOM, so each field is read only in the mode that populates it. A stale custom-WB
    // slot under another mode is never consulted.
    *tint = 0;
    switch( getMlvWbMode( mlvObject ) )
    {
    case 6: // Custom: fit the retained neutral to the receipt controls (DNG sequences only)
    {
        *temperature = 6000;
        if( ( mlvObject->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_DNGSEQ ) == 0 ) break;
        const double neutral[3] = {
            static_cast<double>( getMlvWbRgain( mlvObject ) ) / 1024.0,
            static_cast<double>( getMlvWbGgain( mlvObject ) ) / 1024.0,
            static_cast<double>( getMlvWbBgain( mlvObject ) ) / 1024.0
        };
        int fittedTemperature = 6000;
        int fittedTint = 0;
        if( processingWhiteBalanceControlsForAsShotNeutral( neutral, &fittedTemperature, &fittedTint ) )
        {
            *temperature = fittedTemperature;
            *tint = fittedTint;
        }
        break;
    }
    case 1:  *temperature = 5200; break; // Sunny
    case 8:  *temperature = 7000; break; // Shade
    case 2:  *temperature = 6000; break; // Cloudy
    case 3:  *temperature = 3200; break; // Tungsten
    case 4:  *temperature = 4000; break; // Fluorescent
    case 5:  *temperature = 6000; break; // Flash
    case 9:  *temperature = static_cast<int>( getMlvWbKelvin( mlvObject ) ); break; // Kelvin
    case 0:  // Auto: the app default
    default: *temperature = 6000; break;
    }
    return true;
}

bool ReceiptApplier::processedThumbnailAtExposure(mlvObject_t *mlvObject,
                                                  int frameIndex,
                                                  int downscaleFactor,
                                                  int cpuCores,
                                                  double exposureStops,
                                                  unsigned char *outBuffer)
{
    if( !mlvObject || !mlvObject->processing || !outBuffer || downscaleFactor <= 0 ) return false;

    processingObject_t *clone = processingCloneForAnalysis( mlvObject->processing );
    if( !clone ) return false;
    processingSetExposureStops( clone, exposureStops );
    const int rendered = get_area_average_downscale_thumnail_with_processing(
        mlvObject, frameIndex, downscaleFactor, qMax( 1, cpuCores ), clone, nullptr, outBuffer );
    processingFreeClone( clone );
    return rendered != 0;
}

bool ReceiptApplier::lookAssistDisplayMeter(mlvObject_t *mlvObject,
                                            int analysisFrame,
                                            int downscaleFactor,
                                            int cpuCores,
                                            LookAssistStats *out,
                                            int *validSamples)
{
    if( validSamples ) *validSamples = 0;
    if( !mlvObject || !mlvObject->processing || !out || downscaleFactor <= 0 ) return false;
    const int width = mlvObject->RAWI.xRes / downscaleFactor;
    const int height = mlvObject->RAWI.yRes / downscaleFactor;
    if( width <= 0 || height <= 0 ) return false;

    processingObject_t *displayClone = processingCloneForAnalysis( mlvObject->processing );
    if( !displayClone ) return false;

    mlv_processed_thumbnail_settings_t displaySettings;
    memset( &displaySettings, 0, sizeof( displaySettings ) );
    // DISPLAY_LEVELS (LOOK-ASSIST-ANALYSIS-TRUE-LEVELS-1): the meter's median IS the exposure answer, so it reads the
    // picture the display shows, not the judgement calibration (on HQ dual-ISO restricted-lossless clips 1.8 EV
    // brighter: the tracked fixtures metered median 88 / 86 where the display shows 33 / 31, and got exposure 13 / 16
    // instead of 154 / 163).
    displaySettings.flags = MLV_PROCESSED_THUMBNAIL_APPLY_EXPOSURE
                          | MLV_PROCESSED_THUMBNAIL_APPLY_SIMPLE_CONTRAST
                          | MLV_PROCESSED_THUMBNAIL_APPLY_SHADOWS
                          | MLV_PROCESSED_THUMBNAIL_APPLY_HIGHLIGHTS
                          | MLV_PROCESSED_THUMBNAIL_APPLY_VIBRANCE
                          | MLV_PROCESSED_THUMBNAIL_DISPLAY_LEVELS;

    const int totalFrames = static_cast<int>( getMlvFrames( mlvObject ) );
    const double samplePcts[3] = { 0.15, 0.5, 0.85 };
    QByteArray displayThumb;
    displayThumb.resize( width * height * 3 );
    double medianSamples[3];
    double p95Samples[3];
    double p99Samples[3];
    int valid = 0;

    for( int s = 0; s < 3; ++s )
    {
        int sampleFrame = analysisFrame;
        if( totalFrames > 1 )
        {
            sampleFrame = static_cast<int>( samplePcts[s] * ( totalFrames - 1 ) );
            sampleFrame = qBound( 0, sampleFrame, totalFrames - 1 );
        }

        if( get_area_average_downscale_thumnail_with_processing_cachefree(
                mlvObject,
                sampleFrame,
                downscaleFactor,
                qMax( 1, cpuCores ),
                displayClone,
                &displaySettings,
                reinterpret_cast<unsigned char *>( displayThumb.data() ) ) )
        {
            const LookAssistStats sampleStats = analyzeLookAssistThumbnail(
                reinterpret_cast<const unsigned char *>( displayThumb.constData() ),
                width,
                height );
            if( sampleStats.median > 0.0 )
            {
                medianSamples[valid] = sampleStats.median;
                p95Samples[valid] = sampleStats.p95;
                p99Samples[valid] = sampleStats.p99;
                ++valid;
            }
        }

        if( totalFrames <= 1 ) break;
    }
    processingFreeClone( displayClone );

    if( validSamples ) *validSamples = valid;
    if( valid <= 0 ) return false;

    std::sort( medianSamples, medianSamples + valid );
    std::sort( p95Samples, p95Samples + valid );
    std::sort( p99Samples, p99Samples + valid );
    LookAssistStats result;
    result.median = medianSamples[valid / 2];
    result.p95 = p95Samples[valid / 2];
    result.p99 = p99Samples[valid / 2];
    result.p05 = result.median;
    *out = result;
    return true;
}

bool ReceiptApplier::processedThumbnailAtBalance(mlvObject_t *mlvObject,
                                                 int frameIndex,
                                                 int downscaleFactor,
                                                 int cpuCores,
                                                 double exposureStops,
                                                 int temperature,
                                                 int tint,
                                                 bool isolated,
                                                 unsigned char *outBuffer,
                                                 bool displayLevels)
{
    if( !mlvObject || !mlvObject->processing || !outBuffer || downscaleFactor <= 0 ) return false;

    processingObject_t *clone = processingCloneForAnalysis( mlvObject->processing );
    if( !clone ) return false;

    // Only the two things the analysis varies: the planned exposure in stops (as processedThumbnailAtExposure()
    // and every other Look Assist analysis render take it -- the patch search, the daylight corroboration,
    // so a picture rendered here is comparable with them) and the white balance under test. Everything else
    // is the live processing state, exactly as for those renders.
    mlv_processed_thumbnail_settings_t settings;
    memset( &settings, 0, sizeof( settings ) );
    settings.flags = MLV_PROCESSED_THUMBNAIL_APPLY_WHITE_BALANCE
                   | MLV_PROCESSED_THUMBNAIL_APPLY_EXPOSURE;
    if( displayLevels ) settings.flags |= MLV_PROCESSED_THUMBNAIL_DISPLAY_LEVELS;
    settings.white_balance_kelvin = temperature;
    settings.white_balance_tint = tint / 10.0;
    settings.exposure_stops = exposureStops;

    int rendered = 0;
    if( isolated )
    {
        // Cache-free AND read-only on llrawproc: the raw read cannot prepare, search or version the shared pixel maps,
        // reset the force-search state or claim the stripe one-shot (it works on a per-thread shadow instead).
        const int previousReadOnly = llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( 1 );
        rendered = get_area_average_downscale_thumnail_with_processing_cachefree(
            mlvObject, frameIndex, downscaleFactor, qMax( 1, cpuCores ), clone, &settings, outBuffer );
        llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( previousReadOnly );
    }
    else
    {
        rendered = get_area_average_downscale_thumnail_with_processing(
            mlvObject, frameIndex, downscaleFactor, qMax( 1, cpuCores ), clone, &settings, outBuffer );
    }
    processingFreeClone( clone );
    return rendered != 0;
}

LookAssistRenderBalanceFn ReceiptApplier::lookAssistMeasureOnlyRenderer(mlvObject_t *mlvObject,
                                                                       int frameIndex,
                                                                       int downscaleFactor,
                                                                       int thumbWidth,
                                                                       int thumbHeight,
                                                                       int cpuCores)
{
    return lookAssistBalanceRenderer( mlvObject, frameIndex, downscaleFactor, thumbWidth, thumbHeight, cpuCores, true );
}

LookAssistRenderBalanceFn ReceiptApplier::lookAssistMeasureOnlyDisplayRenderer(mlvObject_t *mlvObject,
                                                                              int frameIndex,
                                                                              int downscaleFactor,
                                                                              int thumbWidth,
                                                                              int thumbHeight,
                                                                              int cpuCores)
{
    return lookAssistBalanceRenderer( mlvObject, frameIndex, downscaleFactor, thumbWidth, thumbHeight, cpuCores, true,
                                      true );
}

LookAssistRenderBalanceFn ReceiptApplier::lookAssistBalanceRenderer(mlvObject_t *mlvObject,
                                                              int frameIndex,
                                                              int downscaleFactor,
                                                              int thumbWidth,
                                                              int thumbHeight,
                                                              int cpuCores,
                                                              bool isolated,
                                                              bool displayLevels)
{
    return [=]( double exposureStops, int temperature, int tint, LookAssistRenderedPicture *out ) -> bool
    {
        if( !out || thumbWidth <= 0 || thumbHeight <= 0 ) return false;
        out->rgb.assign( static_cast<size_t>( thumbWidth ) * static_cast<size_t>( thumbHeight ) * 3u, 0 );
        if( !processedThumbnailAtBalance( mlvObject, frameIndex, downscaleFactor, cpuCores, exposureStops,
                                          temperature, tint, isolated, out->rgb.data(), displayLevels ) )
            return false;
        out->width = thumbWidth;
        out->height = thumbHeight;
        out->downscaleFactor = downscaleFactor;
        out->stats = analyzeLookAssistThumbnail( out->rgb.data(), thumbWidth, thumbHeight );
        return out->stats.median > 0.0;
    };
}

// LOOK-ASSIST-M16-CAST-3: one isolated, read-only raw read of frameIndex with the dual-ISO match seeded as asked
// (llrpSetIsolatedAnalysisDualIsoMatchForCurrentThread). stopAfterMatch skips the reconstruction: only the match's
// numbers are wanted. False when no match ran.
static bool lookAssistProbeDualIsoMatch(mlvObject_t *mlvObject, int frameIndex, int mode, double evCorrection,
                                        int blackDelta, bool stopAfterMatch, dualiso_match_probe_t *out)
{
    const size_t pixels = static_cast<size_t>( mlvObject->RAWI.xRes ) * static_cast<size_t>( mlvObject->RAWI.yRes );
    if( pixels == 0 ) return false;
    std::vector<uint16_t> frame( pixels );
    const int previousReadOnly = llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( 1 );
    const int previousMode = llrpSetIsolatedAnalysisDualIsoMatchForCurrentThread( mode, evCorrection, blackDelta );
    dualiso_match_probe_reset( stopAfterMatch ? 1 : 0 );
    int bitShift = 0;
    getMlvRawFrameProcessedUint16Direct( mlvObject, static_cast<uint64_t>( frameIndex ), frame.data(), &bitShift );
    const bool ran = dualiso_match_probe_get( out ) != 0;
    dualiso_match_probe_reset( 0 );
    llrpSetIsolatedAnalysisDualIsoMatchForCurrentThread( previousMode, 1.0, -1 );
    llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( previousReadOnly );
    return ran;
}

// CAST-3's PRIMARY metrics over a fixed mask: DBM = -median(G-(R+B)/2) (positive = magenta), PSH = the mask's share
// with G-(R+B)/2 <= -8, R-G >= 8 and B-G >= 8.
static void lookAssistDarkBandMagenta(const std::vector<unsigned char> &rgb, const std::vector<size_t> &mask,
                                      double *dbm, double *psh)
{
    *dbm = 0.0;
    *psh = 0.0;
    if( mask.empty() ) return;
    std::vector<double> axis;
    axis.reserve( mask.size() );
    size_t purple = 0;
    for( size_t i : mask )
    {
        const int r = rgb[i * 3], g = rgb[i * 3 + 1], b = rgb[i * 3 + 2];
        const double greenAxis = g - ( r + b ) / 2.0;
        axis.push_back( greenAxis );
        if( greenAxis <= -8.0 && r - g >= 8 && b - g >= 8 ) ++purple;
    }
    std::nth_element( axis.begin(), axis.begin() + axis.size() / 2, axis.end() );
    *dbm = -axis[axis.size() / 2];
    *psh = 100.0 * static_cast<double>( purple ) / static_cast<double>( mask.size() );
}

// The CAST-3 mask: pixels with luma (54R+183G+19B)>>8 in 20..120.
static std::vector<size_t> lookAssistLumaBandMask(const std::vector<unsigned char> &rgb, size_t pixels)
{
    std::vector<size_t> mask;
    for( size_t i = 0; i < pixels; ++i )
    {
        const int luma = ( 54 * rgb[i * 3] + 183 * rgb[i * 3 + 1] + 19 * rgb[i * 3 + 2] ) >> 8;
        if( luma >= 20 && luma <= 120 ) mask.push_back( i );
    }
    return mask;
}

// LOOK-ASSIST-M16-CAST-4 BLK: the mask's share whose G-(R+B)/2 deviates by >= 12 from the median over the 9x9 pixels
// around it (twice the axis, so the arithmetic stays integer).
static double lookAssistBlotchShare(const std::vector<unsigned char> &rgb, int width, int height,
                                    const std::vector<size_t> &mask)
{
    if( mask.empty() || width <= 0 || height <= 0 ) return 0.0;
    std::vector<int> axis( static_cast<size_t>( width ) * static_cast<size_t>( height ) );
    for( size_t i = 0; i < axis.size(); ++i )
        axis[i] = 2 * rgb[i * 3 + 1] - rgb[i * 3] - rgb[i * 3 + 2];
    std::vector<int> window;
    window.reserve( 81 );
    size_t blotch = 0;
    for( size_t i : mask )
    {
        const int x = static_cast<int>( i % static_cast<size_t>( width ) );
        const int y = static_cast<int>( i / static_cast<size_t>( width ) );
        window.clear();
        for( int yy = qMax( 0, y - 4 ); yy <= qMin( height - 1, y + 4 ); ++yy )
            for( int xx = qMax( 0, x - 4 ); xx <= qMin( width - 1, x + 4 ); ++xx )
                window.push_back( axis[static_cast<size_t>( yy ) * static_cast<size_t>( width ) + xx] );
        std::nth_element( window.begin(), window.begin() + window.size() / 2, window.end() );
        if( std::abs( axis[i] - window[window.size() / 2] ) >= 24 ) ++blotch;
    }
    return 100.0 * static_cast<double>( blotch ) / static_cast<double>( mask.size() );
}

// LOOK-ASSIST-M16-CAST-4 Step 1: what the HQ recon sees on frameIndex. No reconstruction: the probe stops it right after
// recording (the isolated read's failure path restores the frame).
static bool lookAssistProbeDualIsoLevels(mlvObject_t *mlvObject, int frameIndex, dualiso_levels_probe_t *out)
{
    const size_t pixels = static_cast<size_t>( mlvObject->RAWI.xRes ) * static_cast<size_t>( mlvObject->RAWI.yRes );
    if( pixels == 0 ) return false;
    std::vector<uint16_t> frame( pixels );
    const int previousReadOnly = llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( 1 );
    dualiso_levels_probe_reset( 1 );
    int bitShift = 0;
    getMlvRawFrameProcessedUint16Direct( mlvObject, static_cast<uint64_t>( frameIndex ), frame.data(), &bitShift );
    const bool ran = dualiso_levels_probe_get( out ) != 0;
    dualiso_levels_probe_reset( 0 );
    llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( previousReadOnly );
    return ran;
}

// One CAST-4 arm: an isolated, read-only, display-level render of frameIndex, with the arms set for this render only.
// defaultGrade is Look Assist off: contrast, shadows, highlights and vibrance at 0 (the display meter's grade).
static bool lookAssistDisoArmRender(mlvObject_t *mlvObject, int frameIndex, int downscaleFactor, double exposureStops,
                                    int temperature, int tint, bool defaultGrade, int mode, int whiteBright,
                                    double darkNoiseScale, const int *darkBlackOffset, std::vector<unsigned char> *rgb)
{
    llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( mode, whiteBright, darkNoiseScale, darkBlackOffset );
    bool rendered = false;
    if( !defaultGrade )
    {
        rendered = ReceiptApplier::processedThumbnailAtBalance( mlvObject, frameIndex, downscaleFactor, 1, exposureStops,
                                                                temperature, tint, true, rgb->data(), true );
    }
    else if( processingObject_t *clone = processingCloneForAnalysis( mlvObject->processing ) )
    {
        mlv_processed_thumbnail_settings_t settings;
        memset( &settings, 0, sizeof( settings ) );
        settings.flags = MLV_PROCESSED_THUMBNAIL_APPLY_WHITE_BALANCE | MLV_PROCESSED_THUMBNAIL_APPLY_EXPOSURE
                       | MLV_PROCESSED_THUMBNAIL_APPLY_SIMPLE_CONTRAST | MLV_PROCESSED_THUMBNAIL_APPLY_SHADOWS
                       | MLV_PROCESSED_THUMBNAIL_APPLY_HIGHLIGHTS | MLV_PROCESSED_THUMBNAIL_APPLY_VIBRANCE
                       | MLV_PROCESSED_THUMBNAIL_DISPLAY_LEVELS;
        settings.white_balance_kelvin = temperature;
        settings.white_balance_tint = tint / 10.0;
        settings.exposure_stops = exposureStops;
        const int previousReadOnly = llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( 1 );
        rendered = get_area_average_downscale_thumnail_with_processing_cachefree(
            mlvObject, frameIndex, downscaleFactor, 1, clone, &settings, rgb->data() ) != 0;
        llrpSetIsolatedAnalysisSharedStateReadOnlyForCurrentThread( previousReadOnly );
        processingFreeClone( clone );
    }
    llrpSetIsolatedAnalysisDualIsoArmsForCurrentThread( -1, 0, 0.0, nullptr );
    return rendered;
}

// LOOK-ASSIST-M16-CAST-4: the pinned exposure / balance every arm renders at except A1 (as-shot WB) and A5 (Look
// Assist off). Look Assist's own decision varies run to run (6724 vs 6686 on the same build), so it is never used.
static const double kLookAssistDisoArmExposure = 3.80;
static const int kLookAssistDisoArmTemperature = 6724;
static const int kLookAssistDisoArmTint = 0;

QString ReceiptApplier::lookAssistDualIsoMatchTrace(mlvObject_t *mlvObject,
                                                    int judgementFrame,
                                                    int downscaleFactor,
                                                    double exposureStops,
                                                    int temperature,
                                                    int tint,
                                                    const DualIsoTraceImageSink &imageSink)
{
    // Diagnostic only, and slow (about four minutes of isolated full-resolution renders on a 5K clip): off unless asked for.
    if( qEnvironmentVariableIntValue( "MLVAPP_LOOK_ASSIST_DISO_MATCH_TRACE" ) == 0 ) return QString();
    if( !mlvObject || !mlvObject->llrawproc || downscaleFactor <= 0 ) return QString();
    const llrawprocObject_t *llr = mlvObject->llrawproc;
    if( llr->dual_iso != 1 || llr->diso_validity == DISO_INVALID || llr->diso1 == llr->diso2 ) return QString();
    const int totalFrames = static_cast<int>( getMlvFrames( mlvObject ) );
    if( totalFrames <= 0 ) return QString();

    QString trace = QStringLiteral( "dual_iso=%1 diso=%2/%3 interp=%4 alias_map=%5 fullres=%6 chroma_smooth=%7 "
                                    "auto_correction=%8 ev_correction=%9 black_delta=%10 raw_black=%11 raw_white=%12" )
        .arg( llr->dual_iso ).arg( llr->diso1 ).arg( llr->diso2 ).arg( llr->diso_averaging )
        .arg( llr->diso_alias_map ).arg( llr->diso_frblending ).arg( llr->chroma_smooth )
        .arg( llr->diso_auto_correction ).arg( llr->diso_ev_correction, 0, 'f', 3 ).arg( llr->diso_black_delta )
        .arg( getMlvBlackLevel( mlvObject ) ).arg( getMlvWhiteLevel( mlvObject ) );

    // The nominal match the clip renders with (what the reconstruction actually used, levels included).
    dualiso_match_probe_t nominal;
    memset( &nominal, 0, sizeof( nominal ) );
    lookAssistProbeDualIsoMatch( mlvObject, judgementFrame, LLRP_ANALYSIS_DISO_MATCH_SEED, 1.0, -1, true, &nominal );
    trace += QStringLiteral( " match_black20=%1 match_white20=%2 nominal_rc=%3 nominal_ev=%4 nominal_bd=%5" )
        .arg( nominal.black ).arg( nominal.white ).arg( nominal.rc )
        .arg( nominal.ev, 0, 'f', 3 ).arg( nominal.black_delta / 64.0, 0, 'f', 2 );

    // The measured match on the judgement frame, then on the six evenly spaced frames.
    std::vector<int> frames;
    frames.push_back( judgementFrame );
    for( int k = 0; k < 6; ++k )
        frames.push_back( qRound( k * ( totalFrames - 1 ) / 5.0 ) );
    std::vector<double> evs;
    std::vector<double> deltas;
    dualiso_match_probe_t judged;
    memset( &judged, 0, sizeof( judged ) );
    QString perFrame;
    for( size_t f = 0; f < frames.size(); ++f )
    {
        dualiso_match_probe_t measured;
        memset( &measured, 0, sizeof( measured ) );
        lookAssistProbeDualIsoMatch( mlvObject, frames[f], LLRP_ANALYSIS_DISO_MATCH_MEASURED, 1.0, -1, true, &measured );
        if( f == 0 ) judged = measured;
        else if( measured.rc > 0 )
        {
            evs.push_back( measured.ev );
            deltas.push_back( measured.black_delta / 64.0 );
        }
        perFrame += QStringLiteral( "%1%2:%3:%4:%5" ).arg( f == 0 ? QString() : QStringLiteral( "," ) )
            .arg( frames[f] ).arg( measured.rc ).arg( measured.ev, 0, 'f', 3 )
            .arg( measured.black_delta / 64.0, 0, 'f', 2 );
    }
    trace += QStringLiteral( " measured_rc=%1 measured_ev=%2 measured_bd=%3 frames=%4" )
        .arg( judged.rc ).arg( judged.ev, 0, 'f', 3 ).arg( judged.black_delta / 64.0, 0, 'f', 2 ).arg( perFrame );
    if( !evs.empty() )
    {
        std::vector<double> sortedEv = evs;
        std::vector<double> sortedBd = deltas;
        std::sort( sortedEv.begin(), sortedEv.end() );
        std::sort( sortedBd.begin(), sortedBd.end() );
        trace += QStringLiteral( " sheet_ok=%1 median_ev=%2 median_bd=%3 spread_ev=%4 spread_bd=%5" )
            .arg( sortedEv.size() )
            .arg( sortedEv[sortedEv.size() / 2], 0, 'f', 3 ).arg( sortedBd[sortedBd.size() / 2], 0, 'f', 2 )
            .arg( sortedEv.back() - sortedEv.front(), 0, 'f', 3 ).arg( sortedBd.back() - sortedBd.front(), 0, 'f', 2 );
    }
    // LOOK-ASSIST-M16-CAST-4: lever (d) is retired, so CAST-3's match variants and recon options no longer render
    // (their numbers are in CAST-3's run); the levels probe and the CAST-4 arms do.
    if( nominal.rc <= 0 ) return trace + QStringLiteral( " arms=none" );
    QElapsedTimer armsClock;
    armsClock.start();

    // LOOK-ASSIST-M16-CAST-4 Step 1 (no render): the levels the HQ recon sees on the judgement frame, per field and CFA
    // channel, after restricted-range scaling. scale_restricted_range's own arithmetic (llrawproc.c) says where the
    // RAWI white lands after scaling, against the white the recon states.
    const int rawBlack = getMlvBlackLevel( mlvObject );
    const int rawWhite = getMlvWhiteLevel( mlvObject );
    const bool restricted = ( mlvObject->MLVI.videoClass & MLV_VIDEO_CLASS_FLAG_LJ92 ) && rawWhite < 15000
                         && rawWhite > rawBlack;
    double scaleRatio = 1.0;
    if( restricted )
    {
        const int span = rawWhite - rawBlack;
        const int bd = qBound( 0, static_cast<int>( std::ceil( std::log2( static_cast<double>( span ) ) ) ), 14 );
        const int addBit = ( llr->diso1 != llr->diso2 && qMax( llr->diso1, llr->diso2 ) >= 6400 ) ? 1 : 0;
        const double actualSpan = static_cast<double>( ( 1u << static_cast<unsigned>( bd + addBit ) ) - 1u );
        const double scaledWhite = static_cast<double>( span ) * static_cast<double>( 1u << static_cast<unsigned>( 14 - bd ) );
        scaleRatio = ( scaledWhite - rawBlack ) / actualSpan;
    }
    dualiso_levels_probe_t levels;
    memset( &levels, 0, sizeof( levels ) );
    const bool haveLevels = lookAssistProbeDualIsoLevels( mlvObject, judgementFrame, &levels );
    const char *channelNames[4] = { "R", "G1", "G2", "B" };
    int brightClip = 0;
    int darkTop = 0;
    bool darkLevelsOff = false;
    int darkBlackOffset[4] = { 0, 0, 0, 0 };
    QString darkFields;
    QString brightFields;
    QString darkFloorPre;
    QString darkBlackPre;
    for( int c = 0; c < 4 && haveLevels; ++c )
    {
        darkFields += QStringLiteral( "%1%2:%3/%4/%5/%6" ).arg( c ? QStringLiteral( "|" ) : QString() )
            .arg( QLatin1String( channelNames[c] ) ).arg( levels.p001[0][c] ).arg( levels.p1[0][c] )
            .arg( levels.p9999[0][c] ).arg( levels.max[0][c] );
        brightFields += QStringLiteral( "%1%2:%3/%4/%5/%6" ).arg( c ? QStringLiteral( "|" ) : QString() )
            .arg( QLatin1String( channelNames[c] ) ).arg( levels.p001[1][c] ).arg( levels.p1[1][c] )
            .arg( levels.p9999[1][c] ).arg( levels.max[1][c] );
        brightClip = qMax( brightClip, levels.p9999[1][c] );
        darkTop = qMax( darkTop, levels.p9999[0][c] );
        // The floor in pre-scale codes, and the black a Gaussian low tail implies (p0.1 = mu - 3.090 sigma, p1 = mu -
        // 2.326 sigma): no optical black reaches this recon (active_area.x1 = 0), so the tail is the only witness.
        const double floorPre = ( levels.p001[0][c] - levels.black ) / scaleRatio + levels.black;
        const double sigma = qMax( 0.0, static_cast<double>( levels.p1[0][c] - levels.p001[0][c] ) / 0.764 );
        const double mu = levels.p1[0][c] + 2.326 * sigma;
        const double blackPre = ( mu - levels.black ) / scaleRatio + levels.black;
        if( std::fabs( floorPre - rawBlack ) >= 0.5 || std::fabs( blackPre - rawBlack ) >= 0.5 ) darkLevelsOff = true;
        darkBlackOffset[c] = qRound( mu - levels.black );
        darkFloorPre += QStringLiteral( "%1%2:%3" ).arg( c ? QStringLiteral( "/" ) : QString() )
            .arg( QLatin1String( channelNames[c] ) ).arg( floorPre, 0, 'f', 2 );
        darkBlackPre += QStringLiteral( "%1%2:%3" ).arg( c ? QStringLiteral( "/" ) : QString() )
            .arg( QLatin1String( channelNames[c] ) ).arg( blackPre, 0, 'f', 2 );
    }
    const int whiteHalf = levels.white / 2;
    const double brightClipVsHalfEv = ( brightClip > levels.black && whiteHalf > levels.black )
        ? std::log2( static_cast<double>( brightClip - levels.black ) / static_cast<double>( whiteHalf - levels.black ) )
        : 0.0;
    const double darkClipEv = ( darkTop > levels.black && levels.white > levels.black )
        ? std::log2( static_cast<double>( darkTop - levels.black ) / static_cast<double>( levels.white - levels.black ) )
        : 0.0;
    trace += QStringLiteral( " diso_levels valid=%1 black=%2 white=%3 restricted=%4 ratio=%5 rawi_white_scaled=%6 "
                             "white_half=%7 bright_used=%8 is_bright=%9%10%11%12 active=%13,%14,%15,%16" )
        .arg( haveLevels ? 1 : 0 ).arg( levels.black ).arg( levels.white ).arg( restricted ? 1 : 0 )
        .arg( scaleRatio, 0, 'f', 4 ).arg( ( rawWhite - rawBlack ) * scaleRatio + rawBlack, 0, 'f', 1 )
        .arg( whiteHalf ).arg( levels.white_bright_used )
        .arg( levels.is_bright[0] ).arg( levels.is_bright[1] ).arg( levels.is_bright[2] ).arg( levels.is_bright[3] )
        .arg( levels.active_x1 ).arg( levels.active_y1 ).arg( levels.active_x2 ).arg( levels.active_y2 );
    trace += QStringLiteral( " noise_samples=%1 noise_std=%2/%3/%4/%5 dark_noise=%6 bright_noise=%7" )
        .arg( levels.has_noise_samples ).arg( levels.noise_std[0], 0, 'f', 2 ).arg( levels.noise_std[1], 0, 'f', 2 )
        .arg( levels.noise_std[2], 0, 'f', 2 ).arg( levels.noise_std[3], 0, 'f', 2 )
        .arg( levels.dark_noise, 0, 'f', 2 ).arg( levels.bright_noise, 0, 'f', 2 );
    trace += QStringLiteral( " dark=%1 bright=%2 bright_clip=%3 bright_clip_vs_half_ev=%4 dark_clip_ev=%5 "
                             "dark_floor_pre=%6 dark_black_pre=%7 levels_off=%8" )
        .arg( darkFields ).arg( brightFields ).arg( brightClip ).arg( brightClipVsHalfEv, 0, 'f', 3 )
        .arg( darkClipEv, 0, 'f', 3 ).arg( darkFloorPre ).arg( darkBlackPre ).arg( darkLevelsOff ? 1 : 0 );

    // LOOK-ASSIST-M16-CAST-4 Step 2: the arms, on fixed frame indices, at the pinned exposure / balance.
    const int width = mlvObject->RAWI.xRes / downscaleFactor;
    const int height = mlvObject->RAWI.yRes / downscaleFactor;
    if( width <= 0 || height <= 0 ) return trace + QStringLiteral( " arms=none" );
    const size_t pixels = static_cast<size_t>( width ) * static_cast<size_t>( height );
    int asShotTemperature = 6000;
    int asShotTint = 0;
    const bool hasAsShot = asShotWhiteBalanceControls( mlvObject, &asShotTemperature, &asShotTint );
    const double pinExposure = kLookAssistDisoArmExposure;
    const int pinTemperature = kLookAssistDisoArmTemperature;
    const int pinTint = kLookAssistDisoArmTint;
    const int lastFrame = totalFrames - 1;
    const int armFrames[3] = { judgementFrame, qMin( 374, lastFrame ), qMin( 749, lastFrame ) };
    trace += QStringLiteral( " arms_pin=ev%1/%2/%3 la=ev%4/%5/%6 as_shot=%7/%8/%9 arm_frames=%10,%11,%12" )
        .arg( pinExposure, 0, 'f', 2 ).arg( pinTemperature ).arg( pinTint )
        .arg( exposureStops, 0, 'f', 2 ).arg( temperature ).arg( tint )
        .arg( hasAsShot ? 1 : 0 ).arg( asShotTemperature ).arg( asShotTint )
        .arg( armFrames[0] ).arg( armFrames[1] ).arg( armFrames[2] );
    struct Arm { const char *name; int mode; int whiteBright; double darkNoiseScale; bool darkBlack; bool asShotWb;
                 bool lookAssistOff; };
    const int a6White = haveLevels && brightClip > levels.black ? qMin( brightClip, levels.white ) : 0;
    const Arm arms[8] = {
        { "R0",  -1, 0,       0.0, false, false, false },
        { "A1",  -1, 0,       0.0, false, true,  false },
        { "A3",   2, 0,       0.0, false, false, false },
        { "A6",  -1, a6White, 0.0, false, false, false },
        { "A7a", -1, 0,       0.5, false, false, false },
        { "A7b", -1, 0,       2.0, false, false, false },
        { "A8",  -1, 0,       0.0, true,  false, false },
        { "A5",  -1, 0,       0.0, false, true,  true } };
    trace += QStringLiteral( " a6_white=%1 a8=%2" ).arg( a6White )
        .arg( darkLevelsOff ? QStringLiteral( "%1/%2/%3/%4" ).arg( darkBlackOffset[0] ).arg( darkBlackOffset[1] )
                                  .arg( darkBlackOffset[2] ).arg( darkBlackOffset[3] )
                            : QStringLiteral( "skipped" ) );
    for( int rf : armFrames )
    {
        trace += QStringLiteral( " arms@%1:" ).arg( rf );
        std::vector<size_t> mask;
        for( const Arm &a : arms )
        {
            if( std::strcmp( a.name, "A6" ) == 0 && a.whiteBright <= 0 ) { trace += QStringLiteral( " A6=skipped" ); continue; }
            if( a.darkBlack && !darkLevelsOff ) continue;
            const bool reference = std::strcmp( a.name, "R0" ) == 0;
            if( !reference && mask.empty() ) { trace += QStringLiteral( " %1=no_mask" ).arg( QLatin1String( a.name ) ); continue; }
            std::vector<unsigned char> rgb( pixels * 3u, 0 );
            const bool rendered = lookAssistDisoArmRender(
                mlvObject, rf, downscaleFactor,
                a.lookAssistOff ? 0.0 : pinExposure,
                a.asShotWb ? asShotTemperature : pinTemperature,
                a.asShotWb ? asShotTint : pinTint,
                a.lookAssistOff, a.mode, a.whiteBright, a.darkNoiseScale,
                a.darkBlack ? darkBlackOffset : nullptr, &rgb );
            if( !rendered )
            {
                trace += QStringLiteral( " %1=unrendered" ).arg( QLatin1String( a.name ) );
                continue;
            }
            const std::vector<size_t> own = lookAssistLumaBandMask( rgb, pixels );
            if( reference ) mask = own;
            // Every arm but A5 is scored over R0's mask; A5 (another exposure) only over its own, and never graded.
            const std::vector<size_t> &scored = a.lookAssistOff ? own : mask;
            double dbm = 0.0;
            double psh = 0.0;
            lookAssistDarkBandMagenta( rgb, scored, &dbm, &psh );
            const double blk = lookAssistBlotchShare( rgb, width, height, scored );
            trace += QStringLiteral( " %1=dbm%2/psh%3/blk%4/m%5/own%6" ).arg( QLatin1String( a.name ) )
                .arg( dbm, 0, 'f', 1 ).arg( psh, 0, 'f', 2 ).arg( blk, 0, 'f', 2 )
                .arg( scored.size() ).arg( own.size() );
            if( imageSink )
                imageSink( QStringLiteral( "f%1-%2" ).arg( rf ).arg( QLatin1String( a.name ) ), width, height, rgb.data() );
        }
    }
    trace += QStringLiteral( " arms_ms=%1" ).arg( armsClock.elapsed() );
    return trace;
}

bool ReceiptApplier::applyHeadlessLookAssist(ReceiptSettings *receipt,
                                             mlvObject_t *mlvObject,
                                             processingObject_t *processingObject,
                                             uint32_t analysisFrame,
                                             bool masterScenePass)
{
    static const bool s_noLookAssist =
        qEnvironmentVariableIntValue( "MLVAPP_NO_LOOK_ASSIST" ) != 0;
    if( s_noLookAssist || !receipt || !mlvObject || !processingObject || !receipt->lookAssistEnabled() )
    {
        if( receipt )
            receipt->setLookAssistBaselineValid( false );
        BatchLogger::out( QStringLiteral(
            "[BATCH] LOOK_ASSIST skip env_disabled=%1 receipt=%2 mlv=%3 enabled=%4\n" )
            .arg( s_noLookAssist ? QStringLiteral("true") : QStringLiteral("false") )
            .arg( receipt ? QStringLiteral("true") : QStringLiteral("false") )
            .arg( mlvObject ? QStringLiteral("true") : QStringLiteral("false") )
            .arg( receipt && receipt->lookAssistEnabled()
                  ? QStringLiteral("true")
                  : QStringLiteral("false") ) );
        return false;
    }

    const int totalFrames = static_cast<int>( getMlvFrames( mlvObject ) );
    if( totalFrames <= 0 )
    {
        receipt->setLookAssistBaselineValid( false );
        BatchLogger::err( QStringLiteral("[BATCH] WARNING LOOK_ASSIST skipped: clip has no frames\n") );
        return false;
    }
    const int frameIndex = qBound( 0,
                                   static_cast<int>( analysisFrame ),
                                   totalFrames - 1 );

    if( receipt->lookAssistBaselineValid() )
        restoreHeadlessLookAssistBaseline( receipt );
    else
        captureHeadlessLookAssistBaseline( receipt, mlvObject );

    applyHeadlessRawLevelsAutoFix( receipt, mlvObject, processingObject );

    // The flavor: MLVAPP_LOOK_ASSIST_FLAVOR (runs) over the receipt's lookAssistFlavor element; neither = Classic.
    // It shapes the sliders only: the scene verdict and the white-balance decision below are the same for both.
    const LookAssistFlavorSelection flavorSelection = lookAssistSelectFlavor(
        lookAssistFlavorEnvironmentValue(), receipt->lookAssistFlavor(), QString() );
    const LookAssistFlavor flavor = flavorSelection.flavor;
    if( flavorSelection.unknownValue && !masterScenePass )
        BatchLogger::err( QStringLiteral(
            "[BATCH] WARNING LOOK_ASSIST unknown flavor '%1' from %2; using classic\n" )
            .arg( flavorSelection.rejectedValue )
            .arg( flavorSelection.source ) );

    const int raw_w = mlvObject->RAWI.xRes;
    const int raw_h = mlvObject->RAWI.yRes;
    if( raw_w <= 0 || raw_h <= 0 )
    {
        receipt->setLookAssistBaselineValid( false );
        BatchLogger::err( QStringLiteral(
            "[BATCH] WARNING LOOK_ASSIST skipped: invalid raw size width=%1 height=%2\n" )
            .arg( raw_w )
            .arg( raw_h ) );
        return false;
    }

    int downscaleFactor = 8;
    if( raw_w > 4000 || raw_h > 2500 ) downscaleFactor = 12;
    else if( raw_w > 2800 || raw_h > 1900 ) downscaleFactor = 10;
    else if( raw_w > 1800 || raw_h > 1200 ) downscaleFactor = 8;
    else downscaleFactor = 6;

    const int width = raw_w / downscaleFactor;
    const int height = raw_h / downscaleFactor;
    if( width <= 0 || height <= 0 )
    {
        receipt->setLookAssistBaselineValid( false );
        BatchLogger::err( QStringLiteral(
            "[BATCH] WARNING LOOK_ASSIST skipped: invalid thumbnail size downscale=%1 width=%2 height=%3\n" )
            .arg( downscaleFactor )
            .arg( width )
            .arg( height ) );
        return false;
    }

    QByteArray thumbnail;
    thumbnail.resize( width * height * 3 );
    get_area_average_downscale_raw_thumnail(
        mlvObject,
        frameIndex,
        downscaleFactor,
        reinterpret_cast<unsigned char *>( thumbnail.data() ) );

    LookAssistStats stats = analyzeLookAssistThumbnail(
        reinterpret_cast<const unsigned char *>( thumbnail.constData() ),
        width,
        height );
    if( stats.median <= 0.0 )
    {
        receipt->setLookAssistBaselineValid( false );
        BatchLogger::err( QStringLiteral(
            "[BATCH] WARNING LOOK_ASSIST skipped: empty analysis stats frame=%1 width=%2 height=%3\n" )
            .arg( frameIndex )
            .arg( width )
            .arg( height ) );
        return false;
    }

    lookAssistSetSceneEv100( &stats,
                             mlvObject->EXPO.isoValue,
                             static_cast<double>( mlvObject->EXPO.shutterValue ),
                             mlvObject->LENS.aperture,
                             lookAssistRecoveryIso( mlvObject ) );
    {
        int asShotTemperature = 6000;
        int asShotTint = 0;
        const bool hasAsShot = asShotWhiteBalanceControls( mlvObject, &asShotTemperature, &asShotTint );
        lookAssistSetAsShotWhiteBalance( &stats, hasAsShot, asShotTemperature, asShotTint );
    }
    // Colour comes from the rendered picture whenever the RAW thumbnail is a flat floor. The same
    // rendered picture also decides whether the recorded exposure may call the scene daylight.
    const int colorDownscaleFactor = lookAssistIsFlatFloorRawThumbnail( stats )
                                   ? qMax( 3, downscaleFactor / 3 )
                                   : downscaleFactor;
    const int colorWidth = raw_w / colorDownscaleFactor;
    const int colorHeight = raw_h / colorDownscaleFactor;
    QByteArray processedThumbnail;
    auto renderProcessed = [&]( double exposureStops, LookAssistStats *out ) -> bool
    {
        if( colorWidth <= 0 || colorHeight <= 0 ) return false;
        processedThumbnail.resize( colorWidth * colorHeight * 3 );
        unsigned char *processedOut = reinterpret_cast<unsigned char *>( processedThumbnail.data() );
        if( !processedThumbnailAtExposure( mlvObject, frameIndex, colorDownscaleFactor, 1, exposureStops, processedOut ) )
            return false;
        *out = analyzeLookAssistThumbnail( reinterpret_cast<const unsigned char *>( processedThumbnail.constData() ),
                                           colorWidth, colorHeight );
        return true;
    };
    // The master pass gives the recorded exposure no say: no picture evidence is asked for, so the scene is
    // the one master classified (daylight needs the evidence).
    // Not const: the window-lit check below may make a night verdict the daylight class (the GUI does the same).
    LookAssistScene scene = resolveLookAssistScene(
        &stats, masterScenePass ? LookAssistRenderFn() : LookAssistRenderFn( renderProcessed ) );
    // Observation only: how the verdict was reached, appended to the "applied" line. The headless applier has no
    // night post-balance walk and no display meter, so those stay at their defaults.
    LookAssistDecisionTrace decisionTrace;
    decisionTrace.pictureEvidenceAsked = !masterScenePass;
    const bool processedColorWanted = lookAssistShouldAnalyzeProcessedColor( scene, stats );
    const bool canAnalyzeProcessedColor =
        processedColorWanted && colorWidth > 0 && colorHeight > 0;
    bool chromaSmoothAutoApplied = false;
    if( canAnalyzeProcessedColor
     && receipt->chromaSmooth() == 0
     && headlessRestrictedLosslessDualIsoOutputWhiteLevel( receipt, mlvObject )
        > getMlvOriginalWhiteLevel( mlvObject ) )
    {
        receipt->setChromaSmooth( 1 );
        llrpSetChromaSmoothMode( mlvObject, 1 );
        llrpResetFpmStatus( mlvObject );
        llrpResetBpmStatus( mlvObject );
        chromaSmoothAutoApplied = true;
    }

    LookAssistStats processedColorStats;
    bool useProcessedColorStats = false;
    if( canAnalyzeProcessedColor )
    {
        processedThumbnail.resize( colorWidth * colorHeight * 3 );
        unsigned char *processedOut = reinterpret_cast<unsigned char *>( processedThumbnail.data() );
        bool renderedAtPresetExposure = false;
        if( scene != LookAssistScene::Night )
        {
            // Daylight: judge colour at the scene's own lift (the night
            // path keeps the receipt's current exposure, exactly as before).
            const double presetStops =
                presetForLookAssistScene( scene, stats, nullptr, nullptr, flavor ).exposure / 100.0;
            renderedAtPresetExposure = processedThumbnailAtExposure(
                mlvObject, frameIndex, colorDownscaleFactor, 1, presetStops, processedOut );
        }
        if( !renderedAtPresetExposure )
        {
            get_area_average_downscale_thumnail(
                mlvObject,
                frameIndex,
                colorDownscaleFactor,
                1,
                processedOut );
        }
        processedColorStats = analyzeLookAssistThumbnail(
            reinterpret_cast<const unsigned char *>( processedThumbnail.constData() ),
            colorWidth,
            colorHeight );
        const int minColorBalanceSamples = qMax( 32, ( colorWidth * colorHeight ) / 100 );
        useProcessedColorStats =
            processedColorStats.median > 0.0 &&
            processedColorStats.balanceSamples >= minColorBalanceSamples;
    }

    // The same display-space exposure meter the GUI runs, at every playback scale: batch export lands on the
    // exposure the app would.
    LookAssistStats displayStats;
    int displayMeterSamples = 0;
    const bool displayStatsValid = lookAssistDisplayMeter(
        mlvObject, frameIndex, downscaleFactor, 1, &displayStats, &displayMeterSamples );
    if( displayStatsValid )
    {
        decisionTrace.displayMeterRan = true;   // batch has no playback, so playback_scale stays NA
        BatchLogger::out( QStringLiteral(
            "[BATCH] LOOK_ASSIST display_meter samples=%1 robust_median=%2 robust_p95=%3 robust_p99=%4 frame=%5\n" )
            .arg( displayMeterSamples )
            .arg( displayStats.median, 0, 'f', 1 )
            .arg( displayStats.p95, 0, 'f', 1 )
            .arg( displayStats.p99, 0, 'f', 1 )
            .arg( frameIndex ) );
    }

    LookAssistPreset preset = presetForLookAssistScene(
        scene,
        stats,
        useProcessedColorStats ? &processedColorStats : nullptr,
        displayStatsValid ? &displayStats : nullptr,
        flavor );
    const int baseTemperature = receipt->temperature() == -1
                              ? 6000
                              : qBound( 2000, receipt->temperature(), 10000 );
    const int baseTint = qBound( -100, receipt->tint(), 100 );
    // The balance the live processing object held on entry. This is the first write to it; a fallback to master's pass
    // puts it back, so that pass renders its picture at the balance master renders it at, not at the receipt's.
    const double entryKelvin = processingGetWhiteBalanceKelvin( processingObject );
    const double entryRenderTint = processingGetWhiteBalanceTint( processingObject );
    processingSetWhiteBalance( processingObject, baseTemperature, baseTint / 10.0 );

    const unsigned char *autoWbThumbnail =
        useProcessedColorStats
        ? reinterpret_cast<const unsigned char *>( processedThumbnail.constData() )
        : reinterpret_cast<const unsigned char *>( thumbnail.constData() );
    const int autoWbWidth = useProcessedColorStats ? colorWidth : width;
    const int autoWbHeight = useProcessedColorStats ? colorHeight : height;
    const int autoWbDownscaleFactor = useProcessedColorStats
                                    ? colorDownscaleFactor
                                    : downscaleFactor;
    const LookAssistAutoWhiteBalancePatch autoWbPatch =
        findLookAssistAutoWhiteBalancePatch(
            autoWbThumbnail,
            autoWbWidth,
            autoWbHeight,
            autoWbDownscaleFactor,
            raw_w,
            raw_h );

    // The one white-balance decision, shared with the GUI (sync and async): solve -> stability ->
    // damping -> as-shot prior -> clamp. Nothing here re-implements any step.
    LookAssistWhiteBalanceRequest wbRequest;
    wbRequest.stats = &stats;
    // The colour pictures were rendered at the scene's own lift; the daylight patch gates judge them there, and the
    // metered exposure is only what gets applied.
    if( displayStatsValid )
        wbRequest.analysisExposure = presetForLookAssistScene( scene, stats, nullptr, nullptr, flavor ).exposure;
    wbRequest.scene = scene;
    wbRequest.patch = autoWbPatch;
    wbRequest.solvedOnProcessedPicture = useProcessedColorStats;
    wbRequest.baseTemperature = baseTemperature;
    wbRequest.baseTint = baseTint;
    // Corroborated daylight without a trusted patch is balanced from the rendered picture (same shared
    // refinement as the GUI; the render is the same cache-backed one, single-threaded here).
    wbRequest.rawWidth = raw_w;
    wbRequest.rawHeight = raw_h;
    wbRequest.renderBalance = lookAssistBalanceRenderer( mlvObject, frameIndex, colorDownscaleFactor,
                                                   colorWidth, colorHeight, 1, false );
    // [Jun-9 WB RESTORE] live solver (was findMlvWhiteBalanceIsolated); same signature.
    // Batch CLI is single-threaded so the live solver is safe here.
    const LookAssistWhiteBalanceResolution wb = resolveLookAssistWhiteBalance(
        wbRequest,
        [&]( int rawX, int rawY, int *solvedTemperature, int *solvedTint )
        {
            findMlvWhiteBalanceAtAnalysisLevels( mlvObject, static_cast<uint64_t>( frameIndex ), rawX, rawY,
                                                 solvedTemperature, solvedTint, 0 );
        },
        &preset );
    if( wb.legacyBalance && !masterScenePass )
    {
        BatchLogger::out( QStringLiteral(
            "[BATCH] LOOK_ASSIST daylight_fallback_to_master frame=%1 reason=%2 refusedAtBase=%3 initialPatchBaseChroma=%4 initialPatchFinalChroma=%5\n" )
            .arg( frameIndex )
            .arg( wb.initialPatchRefused ? QStringLiteral("initial_patch_unverified") : QStringLiteral("no_verified_surface") )
            .arg( ( wb.refineRefusedAtBase || wb.initialPatchRefusedAtBase ) ? QStringLiteral("true") : QStringLiteral("false") )
            .arg( wb.initialPatchBaseChroma, 0, 'f', 1 )
            .arg( wb.initialPatchFinalChroma, 0, 'f', 1 ) );
        // Corroborated daylight that neither an accepted patch nor a VERIFIED surface backs: the verdict is not
        // trusted, so the clip gets MASTER's analysis from the top (its scene verdict, exposure, colour source
        // and balance), never the as-shot prior. Undo the auto chroma smoothing the daylight verdict switched
        // on; the baseline restore at the top of the next pass puts the receipt back.
        if( chromaSmoothAutoApplied )
        {
            const int chromaSmoothBefore = qBound( 0, receipt->lookAssistBaselineChromaSmooth(), 3 );
            receipt->setChromaSmooth( chromaSmoothBefore );
            llrpSetChromaSmoothMode( mlvObject, chromaSmoothBefore );
            llrpResetFpmStatus( mlvObject );
            llrpResetBpmStatus( mlvObject );
        }
        // Put back the white balance the base write above changed. The setter keeps the stored (render) tint when it is
        // passed back unchanged, so the stored tint is set first; the multipliers and matrices rebuild from it.
        processingObject->wb_tint = entryRenderTint;
        processingSetWhiteBalance( processingObject, entryKelvin, entryRenderTint );
        // The daylight pass's solver and analysis renders leave the single cached debayered frame behind; master's pass
        // debayers its own.
        const int cachedFrameBefore = mlvObject->current_cached_frame_active;
        resetMlvCachedFrame( mlvObject );
        // What master's pass starts from, read back from the object (the tests pin it against the entry state).
        BatchLogger::out( QStringLiteral(
            "[BATCH] LOOK_ASSIST daylight_fallback_state kelvin=%1 renderTint=%2 cachedFrameBefore=%3 cachedFrameAfter=%4\n" )
            .arg( processingGetWhiteBalanceKelvin( processingObject ), 0, 'f', 3 )
            .arg( processingGetWhiteBalanceTint( processingObject ), 0, 'f', 9 )
            .arg( cachedFrameBefore )
            .arg( mlvObject->current_cached_frame_active ) );
        return applyHeadlessLookAssist( receipt, mlvObject, processingObject, analysisFrame, true );
    }
    // The window-lit check runs on copies (the GUI does the same; see there): by colour alone it only logs; it APPLIES
    // when the aperture-bounded exposure rules night out and a verified surface backs the balance. Never in master's pass.
    // Its verification and surface-search renders go through the isolated read-only renderer, so no shared state moves.
    LookAssistStats windowLitStats = stats;
    LookAssistScene windowLitScene = scene;
    LookAssistPreset windowLitPreset = preset;
    LookAssistWhiteBalanceRequest windowLitRequest = wbRequest;
    windowLitRequest.stats = &windowLitStats;
    windowLitRequest.renderBalance = lookAssistMeasureOnlyRenderer( mlvObject, frameIndex, colorDownscaleFactor,
                                                                    colorWidth, colorHeight, 1 );
    windowLitRequest.renderDisplayBalance = lookAssistMeasureOnlyDisplayRenderer( mlvObject, frameIndex, colorDownscaleFactor,
                                                                                  colorWidth, colorHeight, 1 );
    const LookAssistWindowLitCheck windowLit = resolveLookAssistWindowLitInterior(
        windowLitRequest, wb, mlvObject->processing->exposure_stops, &windowLitStats, &windowLitScene, &windowLitPreset,
        useProcessedColorStats ? &processedColorStats : nullptr,
        displayStatsValid ? &displayStats : nullptr );
    const bool windowLitApplied = windowLit.applies && !masterScenePass;
    if( windowLitApplied )
    {
        stats = windowLitStats;
        scene = windowLitScene;
        preset = windowLitPreset;
    }
    lookAssistTraceSurfaceSearch( &decisionTrace, windowLit );
    if( windowLit.candidate )
    {
        BatchLogger::out( QStringLiteral(
            "[BATCH] LOOK_ASSIST window_lit_interior frame=%1 wouldReclassify=%2 reason=%3 scene=%4 baseSurfaceChroma=%5 "
            "baseSurfaceBlueAmber=%6 solutionSurfaceChroma=%7 solutionSurfaceBlueAmber=%8 expoIso=%9 expoShutterUs=%10 "
            "lensApertureX100=%11 applied=%12 exposureBound=%13 recoveryIso=%14 surfaceSearch=%15 searchRenders=%16 "
            "searchBalance=%17/%18 appliedBalance=%19/%20 gateSurfaceBlueAmber=%21 displaySurface=%22 "
            "displaySurfaceBlueAmber=%23 displaySurfaceGreen=%24 displaySearch=%25 displaySearchRenders=%26 "
            "displaySearchBalance=%27/%28 displayDecision=%29 displayBaseBlueAmber=%30 displayBaseGreen=%31 "
            "displayRoomSamples=%32 displayRoomCast=%33/%34\n" )
            .arg( frameIndex )
            .arg( windowLit.evidence ? QStringLiteral("true") : QStringLiteral("false") )
            .arg( windowLit.reason )
            .arg( lookAssistSceneName( scene ) )
            .arg( windowLit.baseSurfaceChroma, 0, 'f', 1 )
            .arg( windowLit.baseSurfaceBlueAmber, 0, 'f', 1 )
            .arg( windowLit.solutionSurfaceChroma, 0, 'f', 1 )
            .arg( windowLit.solutionSurfaceBlueAmber, 0, 'f', 1 )
            .arg( static_cast<qulonglong>( mlvObject->EXPO.isoValue ) )
            .arg( static_cast<qulonglong>( mlvObject->EXPO.shutterValue ) )
            .arg( static_cast<qulonglong>( mlvObject->LENS.aperture ) )
            .arg( windowLitApplied ? QStringLiteral("true") : QStringLiteral("false") )
            .arg( windowLit.exposureBound ? QStringLiteral("true") : QStringLiteral("false") )
            .arg( lookAssistRecoveryIso( mlvObject ) )
            .arg( windowLit.search.result )
            .arg( windowLit.search.renders )
            .arg( windowLit.search.temperature )
            .arg( windowLit.search.tint )
            .arg( windowLit.appliedTemperature )
            .arg( windowLit.appliedTint )
            .arg( windowLit.gateSurfaceBlueAmber, 0, 'f', 1 )
            .arg( windowLit.displayRendered ? QStringLiteral("true") : QStringLiteral("false") )
            .arg( windowLit.displaySurfaceBlueAmber, 0, 'f', 1 )
            .arg( windowLit.displaySurfaceGreen, 0, 'f', 1 )
            .arg( windowLit.displaySearch.result )
            .arg( windowLit.displaySearch.renders )
            .arg( windowLit.displaySearch.temperature )
            .arg( windowLit.displaySearch.tint )
            .arg( windowLit.displayDecision )
            .arg( windowLit.displayBaseBlueAmber, 0, 'f', 1 )
            .arg( windowLit.displayBaseGreen, 0, 'f', 1 )
            .arg( windowLit.displayRoomSamples )
            .arg( windowLit.displayRoomCastBefore, 0, 'f', 1 )
            .arg( windowLit.displayRoomCastAfter, 0, 'f', 1 ) );
    }
    const bool autoWhiteBalanceValid = wb.autoValid;
    const QString autoWhiteBalanceSource = wb.source;
    const QString autoWhiteBalanceDecision = wb.decision;
    const double autoWhiteBalanceDamping = wb.damping;
    const int autoWhiteBalanceCandidateTemperature = wb.candidateTemperature;
    const int autoWhiteBalanceCandidateTint = wb.candidateTint;
    // The applied window-lit evidence's verified balance, or the one white-balance decision's.
    const int temperature = windowLitApplied ? windowLit.appliedTemperature : wb.temperature;
    const int tint = windowLitApplied ? windowLit.appliedTint : wb.tint;

    receipt->setExposure( preset.exposure );
    receipt->setContrast( preset.contrast );
    receipt->setPivot( preset.pivot );
    receipt->setTemperature( temperature );
    receipt->setTint( tint );
    receipt->setVibrance( preset.vibrance );
    receipt->setShadows( preset.shadows );
    receipt->setHighlights( preset.highlights );
    receipt->setLookAssistFlavor( lookAssistFlavorName( flavor ) );
    receipt->setLookAssistBaselineValid( true );

    processingSetWhiteBalance( processingObject, temperature, tint / 10.0 );
    resetMlvCache( mlvObject );
    resetMlvCachedFrame( mlvObject );

    BatchLogger::out( QStringLiteral(
        "[BATCH] LOOK_ASSIST applied frame=%1 scene=%2 median=%3 p95=%4 p99=%5 exposure=%6 temperature=%7 tint=%8 autoWbValid=%9 autoWbSource=%10 autoWbDecision=%11 autoWbDamping=%12 autoWbCandidateTemp=%13 autoWbCandidateTint=%14 chromaSmoothAuto=%15 rawBlack=%16 rawWhite=%17 p05=%18 clipHigh=%19 balanceRGB=%20/%21/%22 balanceSamples=%23 patchValid=%24 patchLuma=%25 patchChroma=%26 patchBlueAmber=%27 patchGreenAxis=%28 refineRenders=%29 refineStartScore=%30 refineScore=%31 refineBlueAmber=%32 refineGreen=%33 masterScenePass=%34 initialPatchChecked=%35 initialPatchRefused=%36 initialPatchBaseChroma=%37 initialPatchFinalChroma=%38 %39 flavor=%40\n" )
        .arg( frameIndex )
        .arg( lookAssistSceneName( scene ) )
        .arg( stats.median, 0, 'f', 2 )
        .arg( stats.p95, 0, 'f', 2 )
        .arg( stats.p99, 0, 'f', 2 )
        .arg( preset.exposure )
        .arg( temperature )
        .arg( tint )
        .arg( autoWhiteBalanceValid ? QStringLiteral("true") : QStringLiteral("false") )
        .arg( autoWhiteBalanceSource )
        .arg( autoWhiteBalanceDecision )
        .arg( autoWhiteBalanceDamping, 0, 'f', 3 )
        .arg( autoWhiteBalanceCandidateTemperature )
        .arg( autoWhiteBalanceCandidateTint )
        .arg( chromaSmoothAutoApplied ? QStringLiteral("true") : QStringLiteral("false") )
        .arg( getMlvBlackLevel( mlvObject ) )
        .arg( getMlvWhiteLevel( mlvObject ) )
        .arg( stats.p05, 0, 'f', 2 )
        .arg( stats.clipHigh, 0, 'f', 4 )
        .arg( stats.balanceR, 0, 'f', 1 )
        .arg( stats.balanceG, 0, 'f', 1 )
        .arg( stats.balanceB, 0, 'f', 1 )
        .arg( stats.balanceSamples )
        .arg( autoWbPatch.valid ? QStringLiteral("true") : QStringLiteral("false") )
        .arg( autoWbPatch.luma, 0, 'f', 1 )
        .arg( autoWbPatch.chroma, 0, 'f', 1 )
        .arg( autoWbPatch.blueAmberAxis, 0, 'f', 1 )
        .arg( autoWbPatch.greenAxis, 0, 'f', 1 )
        .arg( wb.refineRenders )
        .arg( wb.refineStartScore, 0, 'f', 2 )
        .arg( wb.refineScore, 0, 'f', 2 )
        .arg( wb.refineBlueAmber, 0, 'f', 1 )
        .arg( wb.refineGreen, 0, 'f', 1 )
        .arg( masterScenePass ? QStringLiteral("true") : QStringLiteral("false") )
        .arg( wb.initialPatchChecked ? QStringLiteral("true") : QStringLiteral("false") )
        .arg( wb.initialPatchRefused ? QStringLiteral("true") : QStringLiteral("false") )
        .arg( wb.initialPatchBaseChroma, 0, 'f', 1 )
        .arg( wb.initialPatchFinalChroma, 0, 'f', 1 )
        .arg( lookAssistDecisionLogFields( stats, decisionTrace ) )
        .arg( lookAssistFlavorName( flavor ) ) );

    const QString disoMatch = lookAssistDualIsoMatchTrace( mlvObject, frameIndex, colorDownscaleFactor,
                                                           preset.exposure / 100.0, temperature, tint );
    if( !disoMatch.isEmpty() )
        BatchLogger::out( QStringLiteral( "[BATCH] LOOK_ASSIST diso_match frame=%1 %2\n" )
                              .arg( frameIndex ).arg( disoMatch ) );

    return true;
}


/* -----------------------------------------------------------------------
 * printFingerprint()
 *
 * Reads ACTUAL runtime state from the mlvObject/processingObject and
 * prints a structured line.  This proves settings reached the pipeline,
 * not just the ReceiptSettings parser.
 * ----------------------------------------------------------------------- */

void ReceiptApplier::printFingerprint(mlvObject_t *mlvObject,
                                       processingObject_t * /* processingObject */)
{
    llrawprocObject_t *llr = mlvObject->llrawproc;

    BatchLogger::out( QStringLiteral(
        "[BATCH] FINGERPRINT"
        " fixRaw=%1"
        " focusPixels=%2"
        " fpiMethod=%3"
        " badPixels=%4"
        " bpsMethod=%5"
        " bpiMethod=%6"
        " chromaSmooth=%7"
        " patternNoise=%8"
        " verticalStripes=%9"
        " deflickerTarget=%10"
        " dualIso=%11"
        " disoValidity=%12"
        " disoPattern=%13"
        " disoAutoCorr=%14"
        " disoEvCorr=%15"
        " disoBlackDelta=%16"
        " disoAveraging=%17"
        " disoAliasMap=%18"
        " disoFrBlending=%19"
        " darkFrame=%20"
        " rawBlack=%21"
        " rawWhite=%22"
        "\n")
        .arg( llr->fix_raw )
        .arg( llr->focus_pixels )
        .arg( llr->fpi_method )
        .arg( llr->bad_pixels )
        .arg( llr->bps_method )
        .arg( llr->bpi_method )
        .arg( llr->chroma_smooth )
        .arg( llr->pattern_noise )
        .arg( llr->vertical_stripes )
        .arg( llr->deflicker_target )
        .arg( llr->dual_iso )
        .arg( llr->diso_validity )
        .arg( llr->diso_pattern )
        .arg( llr->diso_auto_correction )
        .arg( llr->diso_ev_correction, 0, 'f', 4 )
        .arg( llr->diso_black_delta )
        .arg( llr->diso_averaging )
        .arg( llr->diso_alias_map )
        .arg( llr->diso_frblending )
        .arg( llr->dark_frame )
        .arg( getMlvBlackLevel( mlvObject ) )
        .arg( getMlvWhiteLevel( mlvObject ) )
    );
}
