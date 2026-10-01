// Look Assist scene analysis, classification and preset math.
//
// ONE implementation shared by every Look Assist consumer (the GUI's MainWindow, which feeds
// both the CPU and the CUDA/GL display paths, and the headless ReceiptApplier). It was
// previously duplicated byte-for-byte in both files; a fix to one copy silently missed the
// other. Nothing in here may depend on the render backend.

#include "LookAssistAnalysis.h"

#include <QtGlobal>
#include <math.h>

namespace lookassist
{

QString lookAssistSceneName( LookAssistScene scene )
{
    switch( scene )
    {
    case LookAssistScene::Night:
        return QStringLiteral("night");
    case LookAssistScene::ArtificialLights:
        return QStringLiteral("artificial-lights");
    case LookAssistScene::Shade:
        return QStringLiteral("shade");
    case LookAssistScene::BrightSun:
        return QStringLiteral("bright-sun");
    }
    return QStringLiteral("unknown");
}

static double lookAssistPercentile( const int *histogram, int totalSamples, double fraction )
{
    if( !histogram || totalSamples <= 0 ) return 0.0;

    const int target = qBound( 1, (int)ceil( fraction * totalSamples ), totalSamples );
    int cumulative = 0;
    for( int i = 0; i < 256; ++i )
    {
        cumulative += histogram[i];
        if( cumulative >= target ) return (double)i;
    }
    return 255.0;
}

LookAssistStats analyzeLookAssistThumbnail( const unsigned char *rgb, int width, int height )
{
    LookAssistStats stats;
    if( !rgb || width <= 0 || height <= 0 ) return stats;

    int histogram[256] = { 0 };
    int histogramR[256] = { 0 };
    int histogramG[256] = { 0 };
    int histogramB[256] = { 0 };
    int balanceHistogramR[256] = { 0 };
    int balanceHistogramG[256] = { 0 };
    int balanceHistogramB[256] = { 0 };
    const int totalSamples = width * height;
    int midtoneSamples = 0;
    double visibleRTotal = 0.0;
    double visibleGTotal = 0.0;
    double visibleBTotal = 0.0;
    double greenArtifactAxisTotal = 0.0;

    for( int i = 0; i < totalSamples; ++i )
    {
        const int base = i * 3;
        const int r = rgb[base + 0];
        const int g = rgb[base + 1];
        const int b = rgb[base + 2];
        const int luma = qBound( 0, ( 54 * r + 183 * g + 19 * b ) >> 8, 255 );
        histogram[luma]++;
        if( luma >= 40 && luma <= 215 ) ++midtoneSamples;
        histogramR[r]++;
        histogramG[g]++;
        histogramB[b]++;

        const int maxChannel = qMax( r, qMax( g, b ) );
        const int minChannel = qMin( r, qMin( g, b ) );
        const int saturationProxy = maxChannel - minChannel;
        const double greenAxis = (double)g - ( ( (double)r + (double)b ) * 0.5 );
        if( luma >= 12 )
        {
            visibleRTotal += (double)r;
            visibleGTotal += (double)g;
            visibleBTotal += (double)b;
            stats.visibleSamples++;
        }
        if( luma >= 12
         && g >= 30
         && greenAxis >= 25.0 )
        {
            greenArtifactAxisTotal += greenAxis;
            stats.greenArtifactSamples++;
        }
        if( luma >= 20
         && luma <= 230
         && saturationProxy <= qMax( 14, luma / 5 ) )
        {
            balanceHistogramR[r]++;
            balanceHistogramG[g]++;
            balanceHistogramB[b]++;
            stats.balanceSamples++;
        }
    }

    stats.midtoneFraction = (double)midtoneSamples / (double)totalSamples;
    stats.median = lookAssistPercentile( histogram, totalSamples, 0.50 );
    stats.p05 = lookAssistPercentile( histogram, totalSamples, 0.05 );
    stats.p95 = lookAssistPercentile( histogram, totalSamples, 0.95 );
    stats.p99 = lookAssistPercentile( histogram, totalSamples, 0.99 );
    stats.dynamicRange = stats.p95 - stats.p05;
    stats.medianR = lookAssistPercentile( histogramR, totalSamples, 0.50 );
    stats.medianG = lookAssistPercentile( histogramG, totalSamples, 0.50 );
    stats.medianB = lookAssistPercentile( histogramB, totalSamples, 0.50 );

    if( stats.balanceSamples >= qMax( 32, totalSamples / 100 ) )
    {
        stats.balanceR = lookAssistPercentile( balanceHistogramR, stats.balanceSamples, 0.50 );
        stats.balanceG = lookAssistPercentile( balanceHistogramG, stats.balanceSamples, 0.50 );
        stats.balanceB = lookAssistPercentile( balanceHistogramB, stats.balanceSamples, 0.50 );
    }
    else
    {
        stats.balanceR = stats.medianR;
        stats.balanceG = stats.medianG;
        stats.balanceB = stats.medianB;
    }
    if( stats.visibleSamples > 0 )
    {
        stats.visibleMeanR = visibleRTotal / (double)stats.visibleSamples;
        stats.visibleMeanG = visibleGTotal / (double)stats.visibleSamples;
        stats.visibleMeanB = visibleBTotal / (double)stats.visibleSamples;
        stats.greenArtifactRatio =
            (double)stats.greenArtifactSamples / (double)stats.visibleSamples;
    }
    if( stats.greenArtifactSamples > 0 )
    {
        stats.greenArtifactMeanAxis =
            greenArtifactAxisTotal / (double)stats.greenArtifactSamples;
    }

    int clipLow = histogram[0] + histogram[1] + histogram[2] + histogram[3];
    int clipHigh = histogram[252] + histogram[253] + histogram[254] + histogram[255];
    stats.clipLow = (double)clipLow / (double)totalSamples;
    stats.clipHigh = (double)clipHigh / (double)totalSamples;
    return stats;
}

bool lookAssistSceneEv100( double isoValue, double shutterMicroseconds, double apertureTimes100, double *ev100 )
{
    if( !ev100 || isoValue <= 0.0 || shutterMicroseconds <= 0.0 || apertureTimes100 <= 0.0 )
        return false;
    const double fNumber = apertureTimes100 / 100.0;
    const double shutterSeconds = shutterMicroseconds / 1000000.0;
    *ev100 = log( fNumber * fNumber / shutterSeconds ) / log( 2.0 )
           - log( isoValue / 100.0 ) / log( 2.0 );
    return true;
}

void lookAssistSetSceneEv100( LookAssistStats *stats,
                              double isoValue,
                              double shutterMicroseconds,
                              double apertureTimes100 )
{
    if( !stats ) return;
    double ev100 = 0.0;
    stats->hasSceneEv100 = lookAssistSceneEv100( isoValue, shutterMicroseconds, apertureTimes100, &ev100 );
    stats->sceneEv100 = stats->hasSceneEv100 ? ev100 : 0.0;
}

// Open shade is ~EV100 12; a window-lit interior tops out near 10. 11 splits them with margin.
static const double kLookAssistDaylightEv100 = 11.0;

bool lookAssistExposureIsDaylightBright( const LookAssistStats &stats )
{
    return stats.hasSceneEv100 && stats.sceneEv100 >= kLookAssistDaylightEv100;
}

bool lookAssistSceneIsDaylight( const LookAssistStats &stats )
{
    return lookAssistExposureIsDaylightBright( stats ) && stats.daylightPictureEvidence;
}

void lookAssistSetAsShotWhiteBalance( LookAssistStats *stats, bool valid, int temperature, int tint )
{
    if( !stats ) return;
    stats->hasAsShotWb = valid;
    stats->asShotTemperature = valid ? temperature : 6000;
    stats->asShotTint = valid ? tint : 0;
}

bool lookAssistIsDaylightScene( const LookAssistStats &stats, LookAssistScene scene )
{
    return scene != LookAssistScene::Night
        && scene != LookAssistScene::ArtificialLights
        && lookAssistSceneIsDaylight( stats );
}

LookAssistWhiteBalanceBounds lookAssistWhiteBalanceBounds( const LookAssistStats &stats, LookAssistScene scene )
{
    LookAssistWhiteBalanceBounds bounds;
    if( lookAssistIsDaylightScene( stats, scene ) )
    {
        bounds.minTemperature = 4800;
        bounds.maxTemperature = 10000;
        bounds.minTint = -35;
        bounds.maxTint = 10;
    }
    return bounds;
}

bool lookAssistDaylightSolveIsUndamped( const LookAssistStats &stats, LookAssistScene scene, bool solvedOnProcessedPicture )
{
    return solvedOnProcessedPicture && lookAssistIsDaylightScene( stats, scene );
}

bool lookAssistAsShotPrior( const LookAssistStats &stats, LookAssistScene scene, int *temperature, int *tint )
{
    if( !temperature || !tint || !stats.hasAsShotWb || !lookAssistIsDaylightScene( stats, scene ) )
        return false;
    int priorTemperature = stats.asShotTemperature;
    int priorTint = stats.asShotTint;
    lookAssistClampWhiteBalance( lookAssistWhiteBalanceBounds( stats, scene ), &priorTemperature, &priorTint );
    *temperature = priorTemperature;
    *tint = priorTint;
    return true;
}

void lookAssistClampWhiteBalance( const LookAssistWhiteBalanceBounds &bounds, int *temperature, int *tint )
{
    if( temperature ) *temperature = qBound( bounds.minTemperature, *temperature, bounds.maxTemperature );
    if( tint ) *tint = qBound( bounds.minTint, *tint, bounds.maxTint );
}

LookAssistScene classifyLookAssistScene( const LookAssistStats &stats )
{
    if( stats.p95 >= 220.0 || stats.clipHigh > 0.015 )
        return LookAssistScene::BrightSun;

    // The recorded exposure says daylight AND the rendered picture agrees (resolveLookAssistScene):
    // a dark RAW thumbnail is then under-exposure / a raw floor, not night. The exposure alone
    // never gets here -- a night moon records a daylight EV over a black sky.
    if( lookAssistSceneIsDaylight( stats ) )
        return LookAssistScene::Shade;

    if( stats.median < 60.0 )
    {
        if( stats.clipHigh > 0.006 || stats.p99 >= 236.0 || stats.p95 >= 185.0 )
            return LookAssistScene::ArtificialLights;
        return LookAssistScene::Night;
    }

    return LookAssistScene::Shade;
}

bool lookAssistDaylightNeedsPictureEvidence( const LookAssistStats &stats, LookAssistScene legacyScene )
{
    return !stats.daylightPictureEvidence
        && lookAssistExposureIsDaylightBright( stats )
        && lookAssistIsFlatFloorRawThumbnail( stats )
        && ( legacyScene == LookAssistScene::Night || legacyScene == LookAssistScene::ArtificialLights );
}

bool lookAssistPictureCorroboratesDaylight( const LookAssistStats &processedAtCameraExposure )
{
    // Camera settings that say "bright" over a picture that is mostly dark is a bright SUBJECT in a
    // dark scene (moon, lit stage, car lights), not daylight. A daylight exposure renders a lit
    // picture: most pixels in the mid-tones around a mid median.
    return processedAtCameraExposure.midtoneFraction >= 0.60
        && processedAtCameraExposure.median >= 60.0
        && processedAtCameraExposure.median <= 190.0;
}

LookAssistScene resolveLookAssistScene( LookAssistStats *stats, const LookAssistRenderFn &renderProcessed )
{
    if( !stats ) return LookAssistScene::Night;
    stats->daylightPictureEvidence = false;
    LookAssistScene scene = classifyLookAssistScene( *stats );
    if( renderProcessed && lookAssistDaylightNeedsPictureEvidence( *stats, scene ) )
    {
        LookAssistStats processed;
        if( renderProcessed( 0.0, &processed ) && lookAssistPictureCorroboratesDaylight( processed ) )
        {
            stats->daylightPictureEvidence = true;
            scene = classifyLookAssistScene( *stats );
        }
    }
    return scene;
}

bool lookAssistIsFlatFloorRawThumbnail( const LookAssistStats &stats )
{
    // Settled Dual ISO/raw preview paths can lift near-black thumbnails to a
    // flat floor around 32. That thumbnail carries no usable colour or tonal
    // information whatever the scene is; this test is about the PICTURE, not the scene.
    return stats.median >= 24.0 &&
           stats.p05 >= 18.0 &&
           stats.p95 <= 70.0 &&
           stats.dynamicRange <= 24.0;
}

bool lookAssistIsFloorLiftedNightThumbnail( LookAssistScene scene, const LookAssistStats &stats )
{
    // The night-only rescue: a flat floor in a scene that really is night.
    return scene == LookAssistScene::Night && lookAssistIsFlatFloorRawThumbnail( stats );
}

bool lookAssistShouldAnalyzeProcessedColor( LookAssistScene scene, const LookAssistStats &stats )
{
    // Colour has to be read from the RENDERED picture when the RAW thumbnail is a flat floor AND the
    // scene is one whose floor-lifted balance is understood: night (the rescue, unchanged) or a
    // daylight picture (corroborated by the render). This used to ride on the night-only test above,
    // so the day the classifier correctly stopped calling the daylight fixture "night" no white
    // balance was solved on rendered pixels at all and the base 6000 K stood (deck cast chroma
    // 11.5 -> 22.4). Flat-floor artificial-lights / bright-sun clips WITHOUT daylight evidence stay
    // exactly as on master (raw-thumbnail balance, no processed analysis, no auto chroma smoothing):
    // none of the night-only fail-closed guards cover them, so the gate is not widened to them.
    return lookAssistIsFlatFloorRawThumbnail( stats )
        && ( scene == LookAssistScene::Night || lookAssistIsDaylightScene( stats, scene ) );
}

bool lookAssistIsFlatNoiseFloorThumbnail( LookAssistScene scene, const LookAssistStats &stats )
{
    return scene == LookAssistScene::Night
        && stats.median <= 34.0
        && stats.p05 <= 34.0
        && stats.p95 <= 34.0
        && stats.p99 <= 34.0
        && ( stats.p99 - stats.p05 ) <= 2.0;
}

int lookAssistExposureForTarget( double sourceValue, double targetValue, int fallback )
{
    if( sourceValue <= 1.0 || targetValue <= 1.0 ) return fallback;
    return (int)qRound( log( targetValue / sourceValue ) / log( 2.0 ) * 100.0 );
}

bool lookAssistHasNeutralBalanceSamples( const LookAssistStats &stats )
{
    return stats.balanceSamples >= 32
        && stats.balanceR > 0.0
        && stats.balanceG > 0.0
        && stats.balanceB > 0.0;
}

int lookAssistAutoTintCap( LookAssistScene scene, bool processedFloorLiftedBalance )
{
    (void)processedFloorLiftedBalance;
    if( scene == LookAssistScene::BrightSun ) return 8;
    return 22;
}

LookAssistAutoWhiteBalancePatch findLookAssistAutoWhiteBalancePatch(
        const unsigned char *rgb,
        int width,
        int height,
        int downscaleFactor,
        int rawWidth,
        int rawHeight )
{
    LookAssistAutoWhiteBalancePatch best;
    if( !rgb
     || width <= 0
     || height <= 0
     || downscaleFactor <= 0
     || rawWidth <= 0
     || rawHeight <= 0 )
    {
        return best;
    }

    const int edgeMarginX = qMax( 1, width / 80 );
    const int edgeMarginY = qMax( 1, height / 80 );
    for( int y = edgeMarginY; y < height - edgeMarginY; ++y )
    {
        for( int x = edgeMarginX; x < width - edgeMarginX; ++x )
        {
            const int base = ( y * width + x ) * 3;
            const int r = rgb[base + 0];
            const int g = rgb[base + 1];
            const int b = rgb[base + 2];
            const int maxChannel = qMax( r, qMax( g, b ) );
            const int minChannel = qMin( r, qMin( g, b ) );
            const double chroma = (double)( maxChannel - minChannel );
            const double luma = ( 54.0 * r + 183.0 * g + 19.0 * b ) / 256.0;
            if( luma < 70.0 || luma > 220.0 ) continue;
            if( chroma > qMax( 10.0, luma * 0.16 ) ) continue;

            const double greenAxis = (double)g - ( ( (double)r + (double)b ) * 0.5 );
            const double blueAmberAxis = (double)b - (double)r;
            if( greenAxis > 14.0 ) continue;
            if( fabs( blueAmberAxis ) > 30.0 ) continue;

            const double score =
                luma * 0.75
                - chroma * 1.6
                - qMax( 0.0, greenAxis ) * 2.8
                - fabs( blueAmberAxis ) * 0.4;
            if( !best.valid || score > best.score )
            {
                best.valid = true;
                best.thumbnailX = x;
                best.thumbnailY = y;
                best.rawX = qBound( 0, x * downscaleFactor + downscaleFactor / 2, rawWidth - 1 );
                best.rawY = qBound( 0, y * downscaleFactor + downscaleFactor / 2, rawHeight - 1 );
                best.luma = luma;
                best.chroma = chroma;
                best.greenAxis = greenAxis;
                best.blueAmberAxis = blueAmberAxis;
                best.score = score;
            }
        }
    }
    return best;
}

bool lookAssistDaylightPatchIsNeutralEnough( const LookAssistAutoWhiteBalancePatch &patch )
{
    return patch.valid
        && patch.chroma <= qMax( 10.0, patch.luma * 0.09 )
        && fabs( patch.blueAmberAxis ) <= 20.0;
}

bool lookAssistAutoWhiteBalanceSolutionIsStable(
        const LookAssistAutoWhiteBalancePatch &patch,
        int baseTemperature,
        int baseTint,
        int candidateTemperature,
        int candidateTint,
        bool daylightSolve )
{
    if( !patch.valid ) return false;
    // A daylight solve skips the swing rejection below, so it must come from a patch that can be
    // neutral under daylight, not a pale-blue sky / water surface.
    if( daylightSolve && !lookAssistDaylightPatchIsNeutralEnough( patch ) ) return false;

    const int temperatureDelta = candidateTemperature - baseTemperature;
    const int tintDelta = candidateTint - baseTint;
    const bool extremeGreenCorrection =
        candidateTint <= -34
        && temperatureDelta <= -1200
        && patch.luma >= 205.0
        && patch.chroma >= 12.0
        && fabs( patch.blueAmberAxis ) >= 14.0;
    if( extremeGreenCorrection )
    {
        return false;
    }

    const bool hardGreenClampFromBrightNeutralPatch =
        candidateTint <= -34
        && patch.luma >= 210.0
        && patch.chroma <= 6.0
        && qAbs( temperatureDelta ) <= 1000;
    if( hardGreenClampFromBrightNeutralPatch )
    {
        return false;
    }

    const bool hardGreenClampFromLowChromaMidtonePatch =
        candidateTint <= -34
        && patch.luma >= 70.0
        && patch.luma <= 160.0
        && patch.chroma <= 8.0
        && qAbs( temperatureDelta ) <= 1200;
    if( hardGreenClampFromLowChromaMidtonePatch )
    {
        return false;
    }

    // A large two-axis move off a bright, slightly coloured patch is distrusted -- except for a
    // daylight solve from the rendered picture, which is clamped into the daylight bounds instead.
    // Here the rule was a coin flip on the thumbnail's brightness: the daylight fixture's patch
    // (chroma 12-13, luma 199.97 -> 208.9 with the exposure normalisation) sat on its edge and the
    // same correct solution (9990 K / tint -35, deck chroma 4.8) was accepted or rejected.
    const bool implausibleDualAxisSwing =
        !daylightSolve
        && fabs( static_cast<double>( tintDelta ) ) >= 34.0
        && qAbs( temperatureDelta ) >= 1800
        && patch.chroma >= 12.0
        && patch.luma >= 200.0;
    return !implausibleDualAxisSwing;
}

double lookAssistAutoWhiteBalanceDampingFactor(
        const LookAssistAutoWhiteBalancePatch &patch,
        int baseTemperature,
        int baseTint,
        int candidateTemperature,
        int candidateTint,
        LookAssistScene scene )
{
    if( !patch.valid ) return 1.0;

    const int temperatureDelta = candidateTemperature - baseTemperature;
    const int tintDelta = candidateTint - baseTint;
    double factor = 1.0;

    if( patch.chroma >= 14.0 && qAbs( temperatureDelta ) >= 900 )
    {
        factor = qMin( factor, 0.70 );
    }
    if( patch.chroma >= 10.0 && qAbs( temperatureDelta ) >= 1200 )
    {
        factor = qMin( factor, 0.65 );
    }
    if( patch.chroma >= 10.0 && qAbs( tintDelta ) >= 24 )
    {
        factor = qMin( factor, 0.75 );
    }
    if( scene == LookAssistScene::Night
     && patch.luma < 150.0
     && qAbs( temperatureDelta ) >= 1000 )
    {
        factor = qMin( factor, 0.70 );
    }
    return factor;
}

LookAssistWhiteBalanceResolution resolveLookAssistWhiteBalance( const LookAssistWhiteBalanceRequest &request,
                                                                const LookAssistWhiteBalanceSolveFn &solve,
                                                                LookAssistPreset *preset )
{
    LookAssistWhiteBalanceResolution out;
    if( !request.stats || !preset ) return out;
    const LookAssistStats &stats = *request.stats;
    const LookAssistAutoWhiteBalancePatch &patch = request.patch;
    const int baseTemperature = request.baseTemperature;
    const int baseTint = request.baseTint;
    const bool undamped = lookAssistDaylightSolveIsUndamped( stats, request.scene, request.solvedOnProcessedPicture );

    int solvedTemperature = baseTemperature;
    int solvedTint = baseTint;
    if( patch.valid )
    {
        out.source = request.solvedOnProcessedPicture ? QStringLiteral("processed-neutral-patch")
                                                      : QStringLiteral("raw-neutral-patch");
        out.decision = QStringLiteral("candidate");
        if( solve ) solve( patch.rawX, patch.rawY, &solvedTemperature, &solvedTint );
        solvedTemperature = qBound( request.minTemperature, solvedTemperature, request.maxTemperature );
        solvedTint = qBound( request.minTint, solvedTint, request.maxTint );
        solvedTint = qBound( -35, solvedTint, 18 );   // the solver's own rails, unchanged
        out.candidateTemperature = solvedTemperature;
        out.candidateTint = solvedTint;
        if( lookAssistAutoWhiteBalanceSolutionIsStable( patch, baseTemperature, baseTint,
                                                        solvedTemperature, solvedTint, undamped ) )
        {
            out.damping = undamped
                ? 1.0
                : lookAssistAutoWhiteBalanceDampingFactor( patch, baseTemperature, baseTint,
                                                           solvedTemperature, solvedTint, request.scene );
            if( out.damping < 0.999 )
            {
                solvedTemperature = qBound( request.minTemperature,
                                            baseTemperature + qRound( ( solvedTemperature - baseTemperature ) * out.damping ),
                                            request.maxTemperature );
                solvedTint = qBound( request.minTint,
                                     baseTint + qRound( ( solvedTint - baseTint ) * out.damping ),
                                     request.maxTint );
                out.decision = QStringLiteral("accepted-damped");
            }
            else
            {
                out.decision = QStringLiteral("accepted");
            }
            preset->temperatureDelta = solvedTemperature - baseTemperature;
            preset->tintDelta = solvedTint - baseTint;
            out.autoValid = true;
        }
        else
        {
            out.source = QStringLiteral("rejected-extreme-color-cast");
            out.decision = QStringLiteral("rejected-unstable");
        }
        out.solvedTemperature = solvedTemperature;
        out.solvedTint = solvedTint;
    }
    if( !out.autoValid )
    {
        // No neutral patch we can trust: the clip's recorded white balance (mode-aware) is the
        // prior, daylight only. Everything else keeps the colour-balance default in the preset.
        int priorTemperature = baseTemperature;
        int priorTint = baseTint;
        if( lookAssistAsShotPrior( stats, request.scene, &priorTemperature, &priorTint ) )
        {
            out.source = QStringLiteral("as-shot-prior");
            out.decision = QStringLiteral("prior");
            preset->temperatureDelta = priorTemperature - baseTemperature;
            preset->tintDelta = priorTint - baseTint;
        }
    }

    int temperature = qBound( request.minTemperature, baseTemperature + preset->temperatureDelta, request.maxTemperature );
    int tint = qBound( request.minTint, baseTint + preset->tintDelta, request.maxTint );
    lookAssistClampWhiteBalance( lookAssistWhiteBalanceBounds( stats, request.scene ), &temperature, &tint );
    preset->temperatureDelta = temperature - baseTemperature;
    preset->tintDelta = tint - baseTint;
    out.temperature = temperature;
    out.tint = tint;
    return out;
}

int lookAssistDisplayTargetMedianForScene( LookAssistScene scene )
{
    switch( scene )
    {
    case LookAssistScene::Night:            return 64;
    case LookAssistScene::ArtificialLights: return 82;
    case LookAssistScene::Shade:            return 96;
    case LookAssistScene::BrightSun:        return 110;
    }
    return 88;
}

LookAssistPreset presetForLookAssistScene( LookAssistScene scene,
                                           const LookAssistStats &stats,
                                           const LookAssistStats *colorStats,
                                           const LookAssistStats *displayStats )
{
    LookAssistPreset preset;
    int targetMedian = 110;

    switch( scene )
    {
    case LookAssistScene::Night:
        targetMedian = 94;
        preset.contrast = 8;
        preset.pivot = 46;
        preset.shadows = 28;
        preset.highlights = -18;
        preset.vibrance = 3;
        break;
    case LookAssistScene::ArtificialLights:
        targetMedian = 96;
        preset.contrast = 10;
        preset.pivot = 50;
        preset.shadows = 10;
        preset.highlights = -24;
        preset.vibrance = 2;
        break;
    case LookAssistScene::Shade:
        targetMedian = 112;
        preset.contrast = 9;
        preset.pivot = 55;
        preset.shadows = 12;
        preset.highlights = -12;
        preset.vibrance = 5;
        break;
    case LookAssistScene::BrightSun:
        targetMedian = 118;
        preset.contrast = 6;
        preset.pivot = 60;
        preset.shadows = 4;
        preset.highlights = -30;
        preset.vibrance = 0;
        break;
    }

    const bool floorLiftedNightThumbnail =
        lookAssistIsFloorLiftedNightThumbnail( scene, stats );
    const bool flatNoiseFloorThumbnail =
        lookAssistIsFlatNoiseFloorThumbnail( scene, stats );
    const double sourceMedian = floorLiftedNightThumbnail
        ? qMax( 2.0, ( stats.median - stats.p05 ) + 2.0 )
        : qMax( 1.0, stats.median );
    int exposure = lookAssistExposureForTarget( sourceMedian, targetMedian, 0 );
    int maxExposure = 180;
    int minExposure = -140;
    double p95Ceiling = 172.0;
    double p99Ceiling = 218.0;
    if( scene == LookAssistScene::Night )
    {
        maxExposure = ( stats.p99 < 55.0 ) ? 380 : 260;
        if( flatNoiseFloorThumbnail )
            maxExposure = qMin( maxExposure, 170 );
        minExposure = -40;
        p95Ceiling = floorLiftedNightThumbnail ? 124.0 : 142.0;
        p99Ceiling = floorLiftedNightThumbnail ? 160.0 : 188.0;
    }
    else if( scene == LookAssistScene::ArtificialLights )
    {
        maxExposure = 220;
        minExposure = -120;
        p95Ceiling = 150.0;
        p99Ceiling = 194.0;
    }
    else if( scene == LookAssistScene::BrightSun )
    {
        maxExposure = 0;
        minExposure = -180;
        p95Ceiling = 146.0;
        p99Ceiling = 184.0;
    }

    int highlightCap = maxExposure;
    highlightCap = qMin( highlightCap, lookAssistExposureForTarget( stats.p95, p95Ceiling, highlightCap ) );
    highlightCap = qMin( highlightCap, lookAssistExposureForTarget( stats.p99, p99Ceiling, highlightCap ) );
    if( stats.clipHigh > 0.002 )
        highlightCap = qMin( highlightCap, 0 );
    exposure = qMin( exposure, highlightCap );

    exposure = qBound( minExposure, exposure, maxExposure );
    if( scene == LookAssistScene::BrightSun )
        exposure = qMin( exposure, 0 );
    if( scene == LookAssistScene::Night )
        exposure = qMax( exposure, 0 );

    if( displayStats != nullptr && displayStats->median > 0.0 )
    {
        const int displayTarget = lookAssistDisplayTargetMedianForScene( scene );
        int displayExposure = lookAssistExposureForTarget(
            qMax( 1.0, displayStats->median ), (double)displayTarget, 0 );
        const int displayCap = lookAssistExposureForTarget(
            qMax( 1.0, displayStats->p99 ), 440.0, 400 );
        displayExposure = qMin( displayExposure, displayCap );
        exposure = qBound( -120, displayExposure, 380 );
    }

    preset.exposure = exposure;

    if( stats.dynamicRange < 100.0 ) preset.contrast += 6;
    else if( stats.dynamicRange < 130.0 ) preset.contrast += 3;
    else if( stats.dynamicRange > 180.0 ) preset.contrast -= 4;

    if( stats.p05 < 18.0 ) preset.shadows += 8;
    if( stats.p05 < 12.0 ) preset.shadows += 6;
    if( floorLiftedNightThumbnail ) preset.shadows = qMax( preset.shadows, 32 );
    if( stats.clipHigh > 0.010 ) preset.highlights -= 8;
    if( stats.clipHigh > 0.020 ) preset.highlights -= 8;
    const double exposureScale = pow( 2.0, exposure / 100.0 );
    const double projectedP95 = stats.p95 * exposureScale;
    const double projectedP99 = stats.p99 * exposureScale;
    if( projectedP95 > p95Ceiling - 2.0 ) preset.highlights -= 8;
    if( projectedP99 > p99Ceiling - 2.0 ) preset.highlights -= 8;

    if( scene == LookAssistScene::BrightSun )
    {
        preset.shadows = qMin( preset.shadows, 6 );
        preset.vibrance = qMin( preset.vibrance, 2 );
    }

    const LookAssistStats &balanceStats = colorStats ? *colorStats : stats;
    const bool processedFloorLiftedBalance = colorStats && floorLiftedNightThumbnail;
    const bool lowSignalFloorLiftedBalance =
        processedFloorLiftedBalance &&
        balanceStats.median > 0.0 &&
        balanceStats.median < 32.0;
    const double magentaGreenAxis = balanceStats.balanceG - ( ( balanceStats.balanceR + balanceStats.balanceB ) * 0.5 );
    const double blueAmberAxis = balanceStats.balanceB - balanceStats.balanceR;
    const bool hasNeutralBalance = lookAssistHasNeutralBalanceSamples( balanceStats );
    const int tintCap = lookAssistAutoTintCap( scene, processedFloorLiftedBalance );
    const int tempCap = ( scene == LookAssistScene::BrightSun )
                      ? 250
                      : ( processedFloorLiftedBalance ? 420 : 500 );
    const double tintThreshold = processedFloorLiftedBalance ? 6.0 : 10.0;
    const double tintGain = processedFloorLiftedBalance ? 0.55 : 0.65;
    const double tempThreshold = processedFloorLiftedBalance ? 6.0 : 14.0;
    const double tempGain = processedFloorLiftedBalance ? 16.0 : 18.0;

    if( hasNeutralBalance && fabs( magentaGreenAxis ) >= tintThreshold )
    {
        // Positive tint counteracts green casts; negative tint counteracts magenta casts.
        preset.tintDelta = qBound( -tintCap, (int)qRound( magentaGreenAxis * tintGain ), tintCap );
    }

    if( hasNeutralBalance && fabs( blueAmberAxis ) >= tempThreshold )
    {
        // Positive temperature warms blue-heavy clips; negative temperature cools amber-heavy clips.
        preset.temperatureDelta = qBound( -tempCap, (int)qRound( blueAmberAxis * tempGain ), tempCap );
    }

    if( hasNeutralBalance && lowSignalFloorLiftedBalance )
    {
        if( magentaGreenAxis > -4.0 )
            preset.tintDelta = qMax( preset.tintDelta, 4 );
        if( blueAmberAxis <= -6.0 )
        {
            const int warmCastTemperatureDelta =
                qBound( -360,
                        (int)qRound( blueAmberAxis * 22.0 ),
                        -96 );
            preset.temperatureDelta =
                qMin( preset.temperatureDelta, warmCastTemperatureDelta );
        }
    }
    if( hasNeutralBalance
     && processedFloorLiftedBalance
     && magentaGreenAxis > 2.0
     && balanceStats.greenArtifactRatio >= 0.004
     && balanceStats.greenArtifactMeanAxis >= 25.0 )
    {
        const int artifactTintNudge =
            qBound( 0,
                    (int)qRound( balanceStats.greenArtifactMeanAxis * 0.18
                               + balanceStats.greenArtifactRatio * 120.0 ),
                    qMin( 6, tintCap ) );
        preset.tintDelta = qBound( -tintCap,
                                   preset.tintDelta + artifactTintNudge,
                                   tintCap );
    }

    preset.contrast = qBound( -100, preset.contrast, 100 );
    preset.pivot = qBound( 0, preset.pivot, 100 );
    preset.shadows = qBound( -100, preset.shadows, 100 );
    preset.highlights = qBound( -100, preset.highlights, 100 );
    preset.vibrance = qBound( -100, preset.vibrance, 100 );
    return preset;
}

} // namespace lookassist
