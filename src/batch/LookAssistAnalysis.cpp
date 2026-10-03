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

LookAssistDaylightGate lookAssistDaylightGate( const LookAssistStats &stats, LookAssistScene legacyScene )
{
    if( !lookAssistExposureIsDaylightBright( stats ) ) return LookAssistDaylightGate::Exposure;
    if( !lookAssistIsFlatFloorRawThumbnail( stats ) ) return LookAssistDaylightGate::FlatFloor;
    if( legacyScene != LookAssistScene::Night && legacyScene != LookAssistScene::ArtificialLights )
        return LookAssistDaylightGate::Legacy;
    return LookAssistDaylightGate::Open;
}

bool lookAssistDaylightNeedsPictureEvidence( const LookAssistStats &stats, LookAssistScene legacyScene )
{
    return !stats.daylightPictureEvidence
        && lookAssistDaylightGate( stats, legacyScene ) == LookAssistDaylightGate::Open;
}

QString lookAssistDaylightGateName( const LookAssistStats &resolved, bool pictureEvidenceAsked )
{
    if( resolved.daylightPictureEvidence ) return QStringLiteral("pass");
    // No evidence was granted, so the stats classify exactly as the legacy verdict did.
    switch( lookAssistDaylightGate( resolved, classifyLookAssistScene( resolved ) ) )
    {
    case LookAssistDaylightGate::Exposure:
        return QStringLiteral("exposure");
    case LookAssistDaylightGate::FlatFloor:
        return QStringLiteral("flatfloor");
    case LookAssistDaylightGate::Legacy:
        return QStringLiteral("legacy");
    case LookAssistDaylightGate::Open:
        break;
    }
    return pictureEvidenceAsked ? QStringLiteral("picture") : QStringLiteral("n/a");
}

void lookAssistTraceWalkSteps( LookAssistDecisionTrace *trace, bool stepsAdjustedBalance )
{
    if( trace && stepsAdjustedBalance ) trace->postWalkBranch = LookAssistPostWalkBranch::Steps;
}

void lookAssistTraceWalkCleanup( LookAssistDecisionTrace *trace )
{
    if( trace ) trace->postWalkBranch = LookAssistPostWalkBranch::Cleanup;
}

void lookAssistTraceWalkRecovery( LookAssistDecisionTrace *trace, bool candidateAdopted,
                                  int temperatureDelta, int tintDelta )
{
    if( !trace || !candidateAdopted ) return;   // table entered, nothing adopted: the earlier branch and its pair stand
    trace->postWalkBranch = LookAssistPostWalkBranch::Recovery;
    trace->recoveryTemperatureDelta = temperatureDelta;
    trace->recoveryTintDelta = tintDelta;
}

QString lookAssistDecisionLogFields( const LookAssistStats &resolved, const LookAssistDecisionTrace &trace )
{
    QString branch = QStringLiteral("none");
    switch( trace.postWalkBranch )
    {
    case LookAssistPostWalkBranch::None:
        break;
    case LookAssistPostWalkBranch::Steps:
        branch = QStringLiteral("steps");
        break;
    case LookAssistPostWalkBranch::Cleanup:
        branch = QStringLiteral("cleanup");
        break;
    case LookAssistPostWalkBranch::Recovery:
        branch = QStringLiteral("recovery");
        break;
    }
    const bool recovery = trace.postWalkBranch == LookAssistPostWalkBranch::Recovery;
    return QStringLiteral("has_ev100=%1 ev100=%2 daylight_gate=%3 post_walk_ran=%4 post_walk_branch=%5 "
                          "post_walk_recovery=%6 display_meter_ran=%7 playback_scale=%8")
        .arg( resolved.hasSceneEv100 ? 1 : 0 )
        .arg( resolved.hasSceneEv100 ? QString::number( floor( resolved.sceneEv100 * 1000.0 ) / 1000.0, 'f', 3 ) : QStringLiteral("NA") )
        .arg( lookAssistDaylightGateName( resolved, trace.pictureEvidenceAsked ) )
        .arg( trace.postWalkRan ? 1 : 0 )
        .arg( branch )
        .arg( recovery ? QStringLiteral("%1/%2").arg( trace.recoveryTemperatureDelta ).arg( trace.recoveryTintDelta )
                       : QStringLiteral("NA") )
        .arg( trace.displayMeterRan ? 1 : 0 )
        .arg( trace.playbackScaleFactor > 0 ? QString::number( trace.playbackScaleFactor ) : QStringLiteral("NA") );
}

bool lookAssistPictureCorroboratesDaylight( const LookAssistStats &processedAtPlannedExposure )
{
    // The Shade preset lifts a flat RAW thumbnail towards a mid-grey picture (target median 112). If the
    // exposure is right that the scene is daylight, the RENDERED picture at that lift is a lit picture:
    // most pixels in the mid-tones, median around the target. Camera settings that say "bright" over a
    // picture that stays dark after the lift is a bright SUBJECT in a dark scene (moon, lit stage, car
    // lights): its sky does not scale with the lift. The camera's own exposure is not a usable judge (the
    // app's default render of the tracked daylight fixture is median 31 there). Measured at the lift on
    // the tracked fixture: median 77 / 98.7 % mid-tones in the app's profile render, ~116 in the playback
    // render, 140-147 headless -- so the floor sits well under all three and well over a dark field.
    return processedAtPlannedExposure.midtoneFraction >= 0.60
        && processedAtPlannedExposure.median >= 55.0
        && processedAtPlannedExposure.median <= 190.0;
}

LookAssistScene resolveLookAssistScene( LookAssistStats *stats, const LookAssistRenderFn &renderProcessed )
{
    if( !stats ) return LookAssistScene::Night;
    stats->daylightPictureEvidence = false;
    LookAssistScene scene = classifyLookAssistScene( *stats );
    if( renderProcessed && lookAssistDaylightNeedsPictureEvidence( *stats, scene ) )
    {
        // Judge the daylight hypothesis at the exposure it would apply (the Shade preset's lift).
        LookAssistStats hypothesis = *stats;
        hypothesis.daylightPictureEvidence = true;
        const double plannedStops = presetForLookAssistScene( classifyLookAssistScene( hypothesis ), hypothesis ).exposure / 100.0;
        LookAssistStats processed;
        if( renderProcessed( plannedStops, &processed ) && lookAssistPictureCorroboratesDaylight( processed ) )
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

double lookAssistBalanceScore( const LookAssistStats &rendered )
{
    const double greenAxis =
        rendered.balanceG - ( ( rendered.balanceR + rendered.balanceB ) * 0.5 );
    const double blueAmberAxis = rendered.balanceB - rendered.balanceR;
    const double visibleGreenAxis =
        rendered.visibleMeanG - ( ( rendered.visibleMeanR + rendered.visibleMeanB ) * 0.5 );
    return fabs( greenAxis )
        + ( fabs( blueAmberAxis ) * 0.5 )
        + ( fabs( visibleGreenAxis ) * 0.7 )
        + ( rendered.greenArtifactRatio * 700.0 )
        + ( qMax( 0.0, rendered.greenArtifactMeanAxis - 22.0 ) * 0.7 );
}

// The tail of the decision: the final temperature / tint, clamped into the control range and the scene's
// window, and the preset rewritten to describe exactly what is applied.
static void lookAssistFinalizeWhiteBalance( const LookAssistWhiteBalanceRequest &request,
                                            LookAssistPreset *preset,
                                            LookAssistWhiteBalanceResolution *out )
{
    int temperature = qBound( request.minTemperature, request.baseTemperature + preset->temperatureDelta, request.maxTemperature );
    int tint = qBound( request.minTint, request.baseTint + preset->tintDelta, request.maxTint );
    lookAssistClampWhiteBalance( lookAssistWhiteBalanceBounds( *request.stats, request.scene ), &temperature, &tint );
    preset->temperatureDelta = temperature - request.baseTemperature;
    preset->tintDelta = tint - request.baseTint;
    out->temperature = temperature;
    out->tint = tint;
}

// ---- Render-based daylight refinement ------------------------------------------------------------------
//
// Corroborated daylight, no trusted neutral patch. Why not simply neutralise the rendered picture's
// median? It includes blue water and sky: on the tracked fixtures the picture median reaches B-R ~ 0 near
// 10000 K while the concrete deck (the surface that is actually neutral) is then yellow (CIELAB chroma 10.8,
// against 3.8 at the deck-neutral 6500 K / tint -35). The shared neutral-patch solver IS self-consistent with
// the render of the state it runs in, so the refinement reuses it: the patch search needs neutral samples
// (>= 1 % of the picture), which a picture rendered at a white balance far from the scene's has none of
// (that is the whole reason no patch was found). So step the RENDERED picture until it has neutral samples,
// run the same patch search + solver + stability guard on it, and VERIFY by rendering at the solution.
//
// What the guards judge is what the SURFACE IS, never how it looks after we have re-balanced: a surface
// that is neutral only because the walk stepped the white balance until it was (a pale-blue sky / water
// surface walked to grey is the textbook case) must not be believed. So a candidate surface is identified by
// its pixel region (the thumbnail geometry is the same in every render) and must pass the near-neutral /
// not-blue-locus guard on the BASE picture, rendered at the unstepped as-shot prior, too: a surface that is
// blue-locus at base is never acquired, however neutral it looks after stepping. The verification measures
// that SAME surface at the solution, not the best patch anywhere. When nothing is acquired the caller falls
// back to MASTER's balance (resolveLookAssistWhiteBalance); the probes only ever steer the search.
static const int    kRefineMaxProbes           = 7;      // probe renders after the start picture
static const double kRefineDeadBand            = 2.0;    // |axis| below this is neutral enough
static const double kRefineMinImprovement      = 0.25;   // score points a probe must gain to count
static const double kRefineInitialMiredSlope   = 0.8;    // blue-amber axis units per mired
static const double kRefineInitialTintSlope    = -0.3;   // green axis units per receipt tint unit
static const double kRefineMaxMiredStep        = 40.0;
static const double kRefineMaxTintStep         = 12.0;
static const double kRefineVerifyChromaSlack   = 0.75;   // the deck at the result may be this much more cast than where found

static double lookAssistMired( int kelvin ) { return 1.0e6 / (double)qMax( 1, kelvin ); }
static int lookAssistKelvinFromMired( double mired ) { return qRound( 1.0e6 / qMax( 1.0, mired ) ); }

static bool lookAssistPictureHasNeutralSamples( const LookAssistRenderedPicture &picture )
{
    return picture.stats.median > 0.0
        && picture.width > 0
        && picture.height > 0
        && picture.rgb.size() >= (size_t)picture.width * (size_t)picture.height * 3u
        && picture.stats.balanceSamples >= qMax( 32, ( picture.width * picture.height ) / 100 );
}

// The surface at pixel (x, y) of a rendered picture, measured exactly as the patch search measures a pixel
// (luma, chroma = max - min, blue-amber, green axis; the thumbnail is already an area average of the frame).
// This is how a surface found in a STEPPED picture is looked at again in the base picture and in the
// verification picture: the thumbnail geometry is identical in every render, so the pixel is the same surface.
static LookAssistAutoWhiteBalancePatch lookAssistSurfaceAt( const LookAssistRenderedPicture &picture, int x, int y )
{
    LookAssistAutoWhiteBalancePatch surface;
    if( picture.width <= 0 || picture.height <= 0
     || picture.rgb.size() < (size_t)picture.width * (size_t)picture.height * 3u
     || x < 0 || y < 0 || x >= picture.width || y >= picture.height )
        return surface;
    const unsigned char *p = &picture.rgb[( (size_t)y * (size_t)picture.width + (size_t)x ) * 3u];
    const double r = p[0];
    const double g = p[1];
    const double b = p[2];
    surface.valid = true;
    surface.thumbnailX = x;
    surface.thumbnailY = y;
    surface.luma = ( 54.0 * r + 183.0 * g + 19.0 * b ) / 256.0;
    surface.chroma = qMax( r, qMax( g, b ) ) - qMin( r, qMin( g, b ) );
    surface.greenAxis = g - ( r + b ) * 0.5;
    surface.blueAmberAxis = b - r;
    return surface;
}

// Same thumbnail geometry: pixel (x, y) is the same surface in both pictures.
static bool lookAssistSamePictureGeometry( const LookAssistRenderedPicture &a, const LookAssistRenderedPicture &b )
{
    return a.width > 0 && a.width == b.width && a.height == b.height && a.downscaleFactor == b.downscaleFactor
        && a.rgb.size() >= (size_t)a.width * (size_t)a.height * 3u
        && b.rgb.size() >= (size_t)b.width * (size_t)b.height * 3u;
}

static void refineDaylightFromRenderedPicture( const LookAssistWhiteBalanceRequest &request,
                                               const LookAssistWhiteBalanceSolveFn &solve,
                                               LookAssistPreset *preset,
                                               LookAssistWhiteBalanceResolution *out )
{
    const LookAssistStats &stats = *request.stats;
    LookAssistWhiteBalanceBounds window = lookAssistWhiteBalanceBounds( stats, request.scene );
    window.minTemperature = qMax( window.minTemperature, request.minTemperature );
    window.maxTemperature = qMin( window.maxTemperature, request.maxTemperature );
    window.minTint = qMax( window.minTint, request.minTint );
    window.maxTint = qMin( window.maxTint, request.maxTint );
    if( window.minTemperature > window.maxTemperature || window.minTint > window.maxTint ) return;

    // the planned exposure every picture is rendered at
    const double exposureStops = ( request.analysisExposure != LookAssistWhiteBalanceRequest::kLookAssistNoAnalysisExposure
                                   ? request.analysisExposure : preset->exposure ) / 100.0;
    const int startTemperature = qBound( window.minTemperature, request.baseTemperature + preset->temperatureDelta, window.maxTemperature );
    const int startTint = qBound( window.minTint, request.baseTint + preset->tintDelta, window.maxTint );

    struct Point
    {
        int temperature = 0;
        int tint = 0;
        double score = 0.0;
        double blueAmber = 0.0;
        double green = 0.0;
    };
    auto pointOf = []( int temperature, int tint, const LookAssistStats &s ) -> Point
    {
        Point p;
        p.temperature = temperature;
        p.tint = tint;
        p.score = lookAssistBalanceScore( s );
        p.blueAmber = s.balanceB - s.balanceR;
        p.green = s.balanceG - ( ( s.balanceR + s.balanceB ) * 0.5 );
        return p;
    };

    LookAssistRenderedPicture picture;   // the picture currently held, rendered at (pictureTemperature, pictureTint)
    if( !request.renderBalance( exposureStops, startTemperature, startTint, &picture ) || picture.stats.median <= 0.0 ) return;
    out->refineAttempted = true;
    out->refineRenders = 1;
    out->refineStartTemperature = startTemperature;
    out->refineStartTint = startTint;

    // The BASE picture: the unstepped render at the as-shot prior. Every candidate surface is judged here too.
    const LookAssistRenderedPicture basePicture = picture;
    bool refusedAtBase = false;   // the best surface of a stepped picture is not neutral-enough AT BASE: stop walking

    const Point start = pointOf( startTemperature, startTint, picture.stats );
    Point best = start;       // best score seen (steers the probe steps; never an answer by itself)
    out->refineStartScore = start.score;
    out->refineScore = start.score;
    out->refineBlueAmber = start.blueAmber;
    out->refineGreen = start.green;

    // The held picture has neutral samples: search it for the patch, solve, guard, and verify by rendering
    // at the solution.
    auto acquirePatch = [&]() -> bool
    {
        if( !solve || request.rawWidth <= 0 || request.rawHeight <= 0 || !lookAssistPictureHasNeutralSamples( picture ) )
            return false;
        const LookAssistAutoWhiteBalancePatch patch = findLookAssistAutoWhiteBalancePatch(
            picture.rgb.data(), picture.width, picture.height, picture.downscaleFactor,
            request.rawWidth, request.rawHeight );
        if( !patch.valid || !lookAssistDaylightPatchIsNeutralEnough( patch ) ) return false;

        // What the surface IS: the same pixels in the unstepped base picture must be near-neutral and off the
        // blue sky / water locus as well. A surface that only looks neutral after the walk is never acquired.
        if( !lookAssistSamePictureGeometry( basePicture, picture ) ) { refusedAtBase = true; return false; }
        const LookAssistAutoWhiteBalancePatch baseSurface = lookAssistSurfaceAt( basePicture, patch.thumbnailX, patch.thumbnailY );
        out->refineBaseSurfaceChroma = baseSurface.chroma;
        out->refineBaseSurfaceBlueAmber = baseSurface.blueAmberAxis;
        if( !lookAssistDaylightPatchIsNeutralEnough( baseSurface ) ) { refusedAtBase = true; out->refineRefusedAtBase = true; return false; }
        const LookAssistAutoWhiteBalancePatch foundSurface = lookAssistSurfaceAt( picture, patch.thumbnailX, patch.thumbnailY );

        int solvedTemperature = request.baseTemperature;
        int solvedTint = request.baseTint;
        solve( patch.rawX, patch.rawY, &solvedTemperature, &solvedTint );
        solvedTemperature = qBound( request.minTemperature, solvedTemperature, request.maxTemperature );
        solvedTint = qBound( request.minTint, solvedTint, request.maxTint );
        solvedTint = qBound( -35, solvedTint, 18 );   // the solver's own rails, unchanged
        out->candidateTemperature = solvedTemperature;
        out->candidateTint = solvedTint;
        if( !lookAssistAutoWhiteBalanceSolutionIsStable( patch, request.baseTemperature, request.baseTint,
                                                         solvedTemperature, solvedTint, true ) )
            return false;
        solvedTemperature = qBound( window.minTemperature, solvedTemperature, window.maxTemperature );
        solvedTint = qBound( window.minTint, solvedTint, window.maxTint );

        LookAssistRenderedPicture verify;
        if( !request.renderBalance( exposureStops, solvedTemperature, solvedTint, &verify ) ) return false;
        ++out->refineRenders;
        // Verify the SAME acquired surface at the solution (not the best patch anywhere): it must be
        // near-neutral there and no more cast than where it was found.
        if( !lookAssistSamePictureGeometry( picture, verify ) ) return false;
        const LookAssistAutoWhiteBalancePatch solutionSurface = lookAssistSurfaceAt( verify, patch.thumbnailX, patch.thumbnailY );
        out->refineStartPatchChroma = foundSurface.chroma;
        out->refineFinalPatchChroma = solutionSurface.valid ? solutionSurface.chroma : 0.0;
        if( !solutionSurface.valid
         || !lookAssistDaylightPatchIsNeutralEnough( solutionSurface )
         || solutionSurface.chroma > foundSurface.chroma + kRefineVerifyChromaSlack )
            return false;

        out->autoValid = true;
        out->refined = true;
        out->refinePatchAcquired = true;
        out->source = QStringLiteral("rendered-neutral-patch");
        out->decision = QStringLiteral("accepted");
        out->damping = 1.0;
        out->solvedTemperature = solvedTemperature;
        out->solvedTint = solvedTint;
        out->refineScore = lookAssistBalanceScore( verify.stats );
        out->refineBlueAmber = verify.stats.balanceB - verify.stats.balanceR;
        out->refineGreen = verify.stats.balanceG - ( ( verify.stats.balanceR + verify.stats.balanceB ) * 0.5 );
        preset->temperatureDelta = solvedTemperature - request.baseTemperature;
        preset->tintDelta = solvedTint - request.baseTint;
        return true;
    };

    double miredSlope = kRefineInitialMiredSlope;
    double tintSlope = kRefineInitialTintSlope;
    double damp = 1.0;
    int failures = 0;
    for( int probes = 0;; ++probes )
    {
        if( acquirePatch() ) return;
        if( refusedAtBase || probes >= kRefineMaxProbes ) break;   // a surface refused at base stays refused: stop rendering

        const bool moveTemperature = fabs( best.blueAmber ) >= kRefineDeadBand;
        const bool moveTint = fabs( best.green ) >= kRefineDeadBand;
        if( !moveTemperature && !moveTint ) break;
        const double dMired = moveTemperature
            ? qBound( -kRefineMaxMiredStep, ( -best.blueAmber / miredSlope ) * damp, kRefineMaxMiredStep )
            : 0.0;
        const double dTint = moveTint
            ? qBound( -kRefineMaxTintStep, ( -best.green / tintSlope ) * damp, kRefineMaxTintStep )
            : 0.0;
        const int nextTemperature = qBound( window.minTemperature,
                                            lookAssistKelvinFromMired( lookAssistMired( best.temperature ) + dMired ),
                                            window.maxTemperature );
        const int nextTint = qBound( window.minTint, best.tint + qRound( dTint ), window.maxTint );
        if( nextTemperature == best.temperature && nextTint == best.tint ) break;

        LookAssistRenderedPicture next;
        if( !request.renderBalance( exposureStops, nextTemperature, nextTint, &next ) || next.stats.median <= 0.0 ) break;
        ++out->refineRenders;
        const Point candidate = pointOf( nextTemperature, nextTint, next.stats );
        picture = std::move( next );

        // The measurement is information whether or not the probe is kept: refit the slopes from it.
        const double dMiredSeen = lookAssistMired( nextTemperature ) - lookAssistMired( best.temperature );
        if( fabs( dMiredSeen ) >= 3.0 )
        {
            const double seen = ( candidate.blueAmber - best.blueAmber ) / dMiredSeen;
            if( seen > 0.1 && seen < 6.0 ) miredSlope = seen;
        }
        const int dTintSeen = nextTint - best.tint;
        if( qAbs( dTintSeen ) >= 2 )
        {
            const double seen = ( candidate.green - best.green ) / (double)dTintSeen;
            if( seen < -0.03 && seen > -3.0 ) tintSlope = seen;
        }

        if( candidate.score < best.score - kRefineMinImprovement )
        {
            best = candidate;
            failures = 0;
        }
        else
        {
            damp *= 0.5;
            ++failures;
        }
        if( failures >= 3 ) break;
    }
    // No verified surface along the walk: nothing is acquired and the caller applies MASTER's balance. The
    // probes steered the search only; none of them is an answer.
}

// The INITIAL patch of a corroborated daylight clip: the consumer found it on the picture rendered at the EXISTING
// processing white balance, so on its own that picture says nothing about what the surface IS -- a surface that is
// blue at the camera's as-shot balance can be made near-neutral by an existing / custom balance, and would then be
// solved undamped to the daylight rail (sol, PR #222 r2: 9800 K / -20 where master's damped solve gives 8470 K / -13).
// So it is judged exactly as the refinement judges a surface it found: near-neutral and off the blue locus in the
// picture rendered at the AS-SHOT prior (clamped into the daylight window), and -- the SAME pixel -- still
// near-neutral at the solution and no more cast than where it was found. Anything else, including "cannot be
// asked" (no renderer, the narrowing switch off, no as-shot balance, a patch that is not a pixel of that picture),
// is NOT believed: the clip takes MASTER's path. Returns true only for a verified surface.
static bool initialDaylightPatchIsVerified( const LookAssistWhiteBalanceRequest &request,
                                            const LookAssistPreset &preset,
                                            int solvedTemperature,
                                            int solvedTint,
                                            LookAssistWhiteBalanceResolution *out )
{
    out->initialPatchChecked = true;
    const LookAssistAutoWhiteBalancePatch &patch = request.patch;
    if( !request.refineWithoutPatch || !request.renderBalance || !request.stats
     || request.rawWidth <= 0 || request.rawHeight <= 0 )
        return false;

    LookAssistWhiteBalanceBounds window = lookAssistWhiteBalanceBounds( *request.stats, request.scene );
    window.minTemperature = qMax( window.minTemperature, request.minTemperature );
    window.maxTemperature = qMin( window.maxTemperature, request.maxTemperature );
    window.minTint = qMax( window.minTint, request.minTint );
    window.maxTint = qMin( window.maxTint, request.maxTint );
    if( window.minTemperature > window.maxTemperature || window.minTint > window.maxTint ) return false;

    int priorTemperature = request.baseTemperature;
    int priorTint = request.baseTint;
    if( !lookAssistAsShotPrior( *request.stats, request.scene, &priorTemperature, &priorTint ) ) return false;
    priorTemperature = qBound( window.minTemperature, priorTemperature, window.maxTemperature );
    priorTint = qBound( window.minTint, priorTint, window.maxTint );

    // the planned exposure the consumer's picture was rendered at
    const double exposureStops = ( request.analysisExposure != LookAssistWhiteBalanceRequest::kLookAssistNoAnalysisExposure
                                   ? request.analysisExposure : preset.exposure ) / 100.0;
    LookAssistRenderedPicture base;
    if( !request.renderBalance( exposureStops, priorTemperature, priorTint, &base ) || base.stats.median <= 0.0 ) return false;
    // The patch must be a pixel of this very picture: the thumbnail geometry is the one the patch was found in.
    if( base.width <= 0 || base.height <= 0 || base.downscaleFactor <= 0
     || base.rgb.size() < (size_t)base.width * (size_t)base.height * 3u
     || patch.thumbnailX < 0 || patch.thumbnailX >= base.width
     || patch.thumbnailY < 0 || patch.thumbnailY >= base.height
     || patch.rawX != qBound( 0, patch.thumbnailX * base.downscaleFactor + base.downscaleFactor / 2, request.rawWidth - 1 )
     || patch.rawY != qBound( 0, patch.thumbnailY * base.downscaleFactor + base.downscaleFactor / 2, request.rawHeight - 1 ) )
        return false;

    const LookAssistAutoWhiteBalancePatch baseSurface = lookAssistSurfaceAt( base, patch.thumbnailX, patch.thumbnailY );
    out->initialPatchBaseChroma = baseSurface.chroma;
    out->initialPatchBaseBlueAmber = baseSurface.blueAmberAxis;
    if( !lookAssistDaylightPatchIsNeutralEnough( baseSurface ) ) { out->initialPatchRefusedAtBase = true; return false; }

    // The balance that would be applied: the solution inside the daylight window.
    const int appliedTemperature = qBound( window.minTemperature, solvedTemperature, window.maxTemperature );
    const int appliedTint = qBound( window.minTint, solvedTint, window.maxTint );
    LookAssistRenderedPicture verify;
    if( !request.renderBalance( exposureStops, appliedTemperature, appliedTint, &verify ) || !lookAssistSamePictureGeometry( base, verify ) )
        return false;
    const LookAssistAutoWhiteBalancePatch solutionSurface = lookAssistSurfaceAt( verify, patch.thumbnailX, patch.thumbnailY );
    out->initialPatchFinalChroma = solutionSurface.valid ? solutionSurface.chroma : 0.0;
    return solutionSurface.valid
        && lookAssistDaylightPatchIsNeutralEnough( solutionSurface )
        && solutionSurface.chroma <= patch.chroma + kRefineVerifyChromaSlack;
}

LookAssistWhiteBalanceResolution resolveLookAssistWhiteBalance( const LookAssistWhiteBalanceRequest &request,
                                                                const LookAssistWhiteBalanceSolveFn &solve,
                                                                LookAssistPreset *preset )
{
    LookAssistWhiteBalanceResolution out;
    if( !request.stats || !preset ) return out;
    const LookAssistStats &stats = *request.stats;
    const LookAssistAutoWhiteBalancePatch &patch = request.patch;
    // The colour-balance default the preset arrives with is MASTER's balance for a clip with no trusted patch
    // (presetForLookAssistScene's statistics-based deltas); the fallback below restores exactly it.
    const int masterTemperatureDelta = preset->temperatureDelta;
    const int masterTintDelta = preset->tintDelta;
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
        const bool stable = lookAssistAutoWhiteBalanceSolutionIsStable( patch, baseTemperature, baseTint,
                                                                         solvedTemperature, solvedTint, undamped );
        // An undamped daylight solve is believed only for a surface verified at the as-shot balance and at the
        // solution; otherwise nothing is accepted here and the clip takes master's path (damped solve, legacy walk).
        const bool refused = stable && undamped
                          && !initialDaylightPatchIsVerified( request, *preset, solvedTemperature, solvedTint, &out );
        if( refused )
        {
            out.initialPatchRefused = true;
            out.source = QStringLiteral("rejected-unverified-surface");
            out.decision = QStringLiteral("rejected-unverified");
        }
        else if( stable )
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
    // Corroborated daylight and no trusted patch. Only a surface VERIFIED on the rendered picture (see the
    // refinement) may move the white balance; with none -- or with the narrowing switch off, or no renderer
    // to ask -- the balance is MASTER's: its colour-balance default, followed (GUI) by its legacy post-balance
    // walk. Never the as-shot prior alone: that was measured worse than master's look in the real app.
    const bool daylightWithoutPatch = !out.autoValid && lookAssistIsDaylightScene( stats, request.scene );
    // An initial patch that was refused is master's path outright: the refinement is not a second chance for it.
    const bool canRefine = daylightWithoutPatch
                        && !out.initialPatchRefused
                        && lookAssistDaylightNeedsRenderedRefinement( stats, request.scene, out, request.refineWithoutPatch )
                        && static_cast<bool>( request.renderBalance );
    if( canRefine )
    {
        // The as-shot prior is the START of the refinement: the unstepped base picture is rendered there.
        int priorTemperature = baseTemperature;
        int priorTint = baseTint;
        if( lookAssistAsShotPrior( stats, request.scene, &priorTemperature, &priorTint ) )
        {
            preset->temperatureDelta = priorTemperature - baseTemperature;
            preset->tintDelta = priorTint - baseTint;
        }
        lookAssistFinalizeWhiteBalance( request, preset, &out );
        refineLookAssistDaylightWhiteBalance( request, solve, preset, &out );
    }
    if( daylightWithoutPatch && !out.autoValid )
    {
        preset->temperatureDelta = masterTemperatureDelta;
        preset->tintDelta = masterTintDelta;
        out.legacyBalance = true;
        out.source = QStringLiteral("master-balance");
        out.decision = QStringLiteral("legacy");
    }

    lookAssistFinalizeWhiteBalance( request, preset, &out );
    return out;
}

bool lookAssistRefineDaylightWithoutPatchEnabled()
{
    return kLookAssistRefineDaylightWithoutPatch
        && qEnvironmentVariable( "MLVAPP_LOOK_ASSIST_REFINE_DAYLIGHT" ) != QLatin1String( "0" );
}

bool lookAssistDaylightNeedsRenderedRefinement( const LookAssistStats &stats,
                                                LookAssistScene scene,
                                                const LookAssistWhiteBalanceResolution &resolution,
                                                bool refineEnabled )
{
    return refineEnabled
        && !resolution.autoValid
        && lookAssistIsDaylightScene( stats, scene );
}

void refineLookAssistDaylightWhiteBalance( const LookAssistWhiteBalanceRequest &request,
                                           const LookAssistWhiteBalanceSolveFn &solve,
                                           LookAssistPreset *preset,
                                           LookAssistWhiteBalanceResolution *resolution )
{
    if( !request.stats || !preset || !resolution || !request.renderBalance ) return;
    if( resolution->initialPatchRefused ) return;
    if( !lookAssistDaylightNeedsRenderedRefinement( *request.stats, request.scene, *resolution, request.refineWithoutPatch ) ) return;
    refineDaylightFromRenderedPicture( request, solve, preset, resolution );
    lookAssistFinalizeWhiteBalance( request, preset, resolution );
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
