#include "ReceiptApplier.h"
#include "BatchContext.h"
#include "BatchLogger.h"
#include "LookAssistAnalysis.h"
#include "ReceiptSafety.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../platform/qt/DualIsoPatternMapping.h"

#include <QByteArray>
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
    displaySettings.flags = MLV_PROCESSED_THUMBNAIL_APPLY_EXPOSURE
                          | MLV_PROCESSED_THUMBNAIL_APPLY_SIMPLE_CONTRAST
                          | MLV_PROCESSED_THUMBNAIL_APPLY_SHADOWS
                          | MLV_PROCESSED_THUMBNAIL_APPLY_HIGHLIGHTS
                          | MLV_PROCESSED_THUMBNAIL_APPLY_VIBRANCE;

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
                                                 unsigned char *outBuffer)
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
    settings.white_balance_kelvin = temperature;
    settings.white_balance_tint = tint / 10.0;
    settings.exposure_stops = exposureStops;

    const int rendered = isolated
        ? get_area_average_downscale_thumnail_with_processing_cachefree(
              mlvObject, frameIndex, downscaleFactor, qMax( 1, cpuCores ), clone, &settings, outBuffer )
        : get_area_average_downscale_thumnail_with_processing(
              mlvObject, frameIndex, downscaleFactor, qMax( 1, cpuCores ), clone, &settings, outBuffer );
    processingFreeClone( clone );
    return rendered != 0;
}

LookAssistRenderBalanceFn ReceiptApplier::lookAssistBalanceRenderer(mlvObject_t *mlvObject,
                                                              int frameIndex,
                                                              int downscaleFactor,
                                                              int thumbWidth,
                                                              int thumbHeight,
                                                              int cpuCores,
                                                              bool isolated)
{
    return [=]( double exposureStops, int temperature, int tint, LookAssistRenderedPicture *out ) -> bool
    {
        if( !out || thumbWidth <= 0 || thumbHeight <= 0 ) return false;
        out->rgb.assign( static_cast<size_t>( thumbWidth ) * static_cast<size_t>( thumbHeight ) * 3u, 0 );
        if( !processedThumbnailAtBalance( mlvObject, frameIndex, downscaleFactor, cpuCores, exposureStops,
                                          temperature, tint, isolated, out->rgb.data() ) )
            return false;
        out->width = thumbWidth;
        out->height = thumbHeight;
        out->downscaleFactor = downscaleFactor;
        out->stats = analyzeLookAssistThumbnail( out->rgb.data(), thumbWidth, thumbHeight );
        return out->stats.median > 0.0;
    };
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
                             mlvObject->LENS.aperture );
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
    const LookAssistScene scene = resolveLookAssistScene(
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
            const double presetStops = presetForLookAssistScene( scene, stats ).exposure / 100.0;
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
        displayStatsValid ? &displayStats : nullptr );
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
        wbRequest.analysisExposure = presetForLookAssistScene( scene, stats ).exposure;
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
            findMlvWhiteBalance( mlvObject, static_cast<uint64_t>( frameIndex ), rawX, rawY,
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
    const bool autoWhiteBalanceValid = wb.autoValid;
    const QString autoWhiteBalanceSource = wb.source;
    const QString autoWhiteBalanceDecision = wb.decision;
    const double autoWhiteBalanceDamping = wb.damping;
    const int autoWhiteBalanceCandidateTemperature = wb.candidateTemperature;
    const int autoWhiteBalanceCandidateTint = wb.candidateTint;
    const int temperature = wb.temperature;
    const int tint = wb.tint;

    receipt->setExposure( preset.exposure );
    receipt->setContrast( preset.contrast );
    receipt->setPivot( preset.pivot );
    receipt->setTemperature( temperature );
    receipt->setTint( tint );
    receipt->setVibrance( preset.vibrance );
    receipt->setShadows( preset.shadows );
    receipt->setHighlights( preset.highlights );
    receipt->setLookAssistBaselineValid( true );

    processingSetWhiteBalance( processingObject, temperature, tint / 10.0 );
    resetMlvCache( mlvObject );
    resetMlvCachedFrame( mlvObject );

    BatchLogger::out( QStringLiteral(
        "[BATCH] LOOK_ASSIST applied frame=%1 scene=%2 median=%3 p95=%4 p99=%5 exposure=%6 temperature=%7 tint=%8 autoWbValid=%9 autoWbSource=%10 autoWbDecision=%11 autoWbDamping=%12 autoWbCandidateTemp=%13 autoWbCandidateTint=%14 chromaSmoothAuto=%15 rawBlack=%16 rawWhite=%17 p05=%18 clipHigh=%19 balanceRGB=%20/%21/%22 balanceSamples=%23 patchValid=%24 patchLuma=%25 patchChroma=%26 patchBlueAmber=%27 patchGreenAxis=%28 refineRenders=%29 refineStartScore=%30 refineScore=%31 refineBlueAmber=%32 refineGreen=%33 masterScenePass=%34 initialPatchChecked=%35 initialPatchRefused=%36 initialPatchBaseChroma=%37 initialPatchFinalChroma=%38 %39\n" )
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
        .arg( lookAssistDecisionLogFields( stats, decisionTrace ) ) );

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
