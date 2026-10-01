#ifndef LOOKASSISTANALYSIS_H
#define LOOKASSISTANALYSIS_H

#include <QString>

#include <functional>

/* Look Assist scene analysis, classification and preset math -- the ONE implementation.
 *
 * Consumers: the GUI's MainWindow (whose output drives both the CPU and the CUDA/GL display
 * paths, because Look Assist only ever writes the eight receipt sliders) and the headless
 * ReceiptApplier. Nothing here depends on the render backend, so CPU and CUDA cannot diverge
 * on the scene class or the white-balance decision. Pure functions of their arguments; no I/O,
 * no globals, safe on a worker thread. */
namespace lookassist
{

enum class LookAssistScene
{
    Night,
    ArtificialLights,
    Shade,
    BrightSun
};

struct LookAssistStats
{
    double median = 0.0;
    double p05 = 0.0;
    double p95 = 0.0;
    double p99 = 0.0;
    double clipLow = 0.0;
    double clipHigh = 0.0;
    double dynamicRange = 0.0;
    double medianR = 0.0;
    double medianG = 0.0;
    double medianB = 0.0;
    double balanceR = 0.0;
    double balanceG = 0.0;
    double balanceB = 0.0;
    int balanceSamples = 0;
    double visibleMeanR = 0.0;
    double visibleMeanG = 0.0;
    double visibleMeanB = 0.0;
    int visibleSamples = 0;
    double greenArtifactRatio = 0.0;
    double greenArtifactMeanAxis = 0.0;
    int greenArtifactSamples = 0;
    // Scene-referred brightness from the camera's recorded exposure (ISO 100 equivalent EV).
    // Display statistics alone cannot tell an under-exposed daylight clip from a night scene.
    bool hasSceneEv100 = false;
    double sceneEv100 = 0.0;
    // Fraction of pixels in the mid-tone band (luma 40..215): "a lit picture", not "a dark field
    // with a small bright region".
    double midtoneFraction = 0.0;
    // The RENDERED picture, at the exposure the daylight verdict would apply, is a lit daylight picture (set only by
    // resolveLookAssistScene). The recorded exposure alone is NOT proof of daylight: a night moon
    // shot at ISO 200, 1/500 s, f/7.1 records EV100 13.6 over a black sky.
    bool daylightPictureEvidence = false;
    // The clip's recorded (as-shot) white balance, mapped to the app's temperature / tint controls
    // (tint in receipt units). Only a PRIOR: used when no neutral patch can be trusted.
    bool hasAsShotWb = false;
    int asShotTemperature = 6000;
    int asShotTint = 0;
};

struct LookAssistPreset
{
    int exposure = 0;
    int contrast = 0;
    int pivot = 75;
    int shadows = 0;
    int highlights = 0;
    int vibrance = 0;
    int temperatureDelta = 0;
    int tintDelta = 0;
};

struct LookAssistAutoWhiteBalancePatch
{
    bool valid = false;
    int thumbnailX = -1;
    int thumbnailY = -1;
    int rawX = -1;
    int rawY = -1;
    double luma = 0.0;
    double chroma = 0.0;
    double greenAxis = 0.0;
    double blueAmberAxis = 0.0;
    double score = -1.0e9;
};

QString lookAssistSceneName( LookAssistScene scene );

/* rgb = interleaved 8-bit RGB thumbnail of width*height pixels. */
LookAssistStats analyzeLookAssistThumbnail( const unsigned char *rgb, int width, int height );

/* EV at ISO 100 = log2( N^2 / t ) - log2( ISO / 100 ), from the MLV EXPO/LENS blocks
 * (iso, shutter in microseconds, f-number * 100). False when any value is missing/zero. */
bool lookAssistSceneEv100( double isoValue, double shutterMicroseconds, double apertureTimes100, double *ev100 );
void lookAssistSetSceneEv100( LookAssistStats *stats,
                              double isoValue,
                              double shutterMicroseconds,
                              double apertureTimes100 );

/* The recorded exposure is bright enough for daylight (open shade ~12, overcast ~13, sun ~15; lit
 * interiors <= ~10). NECESSARY, never sufficient: see lookAssistSceneIsDaylight. */
bool lookAssistExposureIsDaylightBright( const LookAssistStats &stats );

/* Daylight = bright recorded exposure AND the rendered picture agrees. */
bool lookAssistSceneIsDaylight( const LookAssistStats &stats );

/* Daylight and not a scene class that excludes it. Every daylight-only rule below keys on this. */
bool lookAssistIsDaylightScene( const LookAssistStats &stats, LookAssistScene scene );

/* The legacy verdict is Night / ArtificialLights, the RAW thumbnail is a flat floor (so it cannot
 * speak), and the exposure is daylight-bright: the picture has to be consulted. */
bool lookAssistDaylightNeedsPictureEvidence( const LookAssistStats &stats, LookAssistScene legacyScene );

/* The picture rendered at the exposure the daylight verdict would apply (the Shade preset's lift) is a
 * lit daylight picture: mostly mid-tones around a mid median, not a dark field with a small bright
 * region. The tracked fixture renders median 77-147 with 95-99 % mid-tones at its lift; a moon over a
 * black sky stays almost entirely below luma 40 whatever the lift. */
bool lookAssistPictureCorroboratesDaylight( const LookAssistStats &processedAtPlannedExposure );

/* Without picture evidence the classification is exactly the legacy one. */
LookAssistScene classifyLookAssistScene( const LookAssistStats &stats );

/* Render the processed thumbnail at an absolute exposure (stops) and return its statistics. */
typedef std::function<bool( double exposureStops, LookAssistStats *processedStats )> LookAssistRenderFn;

/* classifyLookAssistScene plus the one picture check: when the exposure says daylight but the flat
 * RAW thumbnail cannot confirm it, the processed picture is rendered at the exposure the daylight
 * verdict would apply and must corroborate; otherwise the legacy verdict (night rescue included) stands. Sets
 * stats->daylightPictureEvidence. The same call for GUI and headless, CPU and CUDA. */
LookAssistScene resolveLookAssistScene( LookAssistStats *stats, const LookAssistRenderFn &renderProcessed );

void lookAssistSetAsShotWhiteBalance( LookAssistStats *stats, bool valid, int temperature, int tint );

/* Colour-temperature / tint window a solved white balance is clamped into (clamped, never rejected).
 * Daylight scenes (by the recorded exposure) cannot be tungsten: below 4800 K or past +10 tint a
 * "neutral patch" is not neutral. The warm end is the slider's own 10000 K: open shade under a blue
 * sky is legitimately 7500-10000 K, and the tracked daylight fixture's neutral deck solves at
 * 9990 K / tint -35 (a 7500 K ceiling left the deck at Lab chroma 17 against 4.8 at the solution).
 * Other scenes keep the full range, exactly as before. Tint is in receipt units (tenths). */
struct LookAssistWhiteBalanceBounds
{
    int minTemperature = 2000;
    int maxTemperature = 10000;
    int minTint = -100;
    int maxTint = 100;
};
LookAssistWhiteBalanceBounds lookAssistWhiteBalanceBounds( const LookAssistStats &stats, LookAssistScene scene );
void lookAssistClampWhiteBalance( const LookAssistWhiteBalanceBounds &bounds, int *temperature, int *tint );

/* A daylight clip whose white balance was solved from a neutral patch of the RENDERED picture applies
 * that solution undamped (clamped into the daylight bounds). The generic damping hedges a patch that
 * is not quite neutral by pulling only part of the way, which left the daylight deck lavender
 * (8594 K / -23 against the solved 9990 K / -35). Non-daylight scenes and raw-thumbnail patches keep
 * the damping exactly as before. */
bool lookAssistDaylightSolveIsUndamped( const LookAssistStats &stats, LookAssistScene scene, bool solvedOnProcessedPicture );

/* The as-shot white balance as the fallback when no neutral patch can be trusted (none found, or the
 * solution was rejected). Daylight-by-metadata scenes only; clamped into the daylight bounds. */
bool lookAssistAsShotPrior( const LookAssistStats &stats, LookAssistScene scene, int *temperature, int *tint );

/* The RAW thumbnail is a flat floor (dual-ISO/raw preview lift): unusable for colour, any scene. */
bool lookAssistIsFlatFloorRawThumbnail( const LookAssistStats &stats );
/* Flat floor AND night: the night-only exposure/shadow rescue. Stays night-only. */
bool lookAssistIsFloorLiftedNightThumbnail( LookAssistScene scene, const LookAssistStats &stats );
/* Colour must come from the rendered (processed) picture. Scene-independent by design. */
bool lookAssistShouldAnalyzeProcessedColor( LookAssistScene scene, const LookAssistStats &stats );
bool lookAssistIsFlatNoiseFloorThumbnail( LookAssistScene scene, const LookAssistStats &stats );
int lookAssistExposureForTarget( double sourceValue, double targetValue, int fallback );
bool lookAssistHasNeutralBalanceSamples( const LookAssistStats &stats );
int lookAssistAutoTintCap( LookAssistScene scene, bool processedFloorLiftedBalance );

LookAssistAutoWhiteBalancePatch findLookAssistAutoWhiteBalancePatch( const unsigned char *rgb,
                                                                     int width,
                                                                     int height,
                                                                     int downscaleFactor,
                                                                     int rawWidth,
                                                                     int rawHeight );

bool lookAssistAutoWhiteBalanceSolutionIsStable( const LookAssistAutoWhiteBalancePatch &patch,
                                                 int baseTemperature,
                                                 int baseTint,
                                                 int candidateTemperature,
                                                 int candidateTint,
                                                 bool daylightSolve = false );

double lookAssistAutoWhiteBalanceDampingFactor( const LookAssistAutoWhiteBalancePatch &patch,
                                                int baseTemperature,
                                                int baseTint,
                                                int candidateTemperature,
                                                int candidateTint,
                                                LookAssistScene scene );

/* A daylight solve skips the generic two-axis-swing rejection, so the PATCH itself must be a surface
 * that can be neutral under daylight: near-neutral (chroma <= 9 % of luma, at least 10) and not on the
 * blue sky / water locus (|B-R| <= 20). A pale-blue patch solved to neutral drags the picture to the
 * warm / green rail (10000 K / tint -35). The tracked deck patch is chroma 12-13 at luma 200-209,
 * B-R +12..13. */
bool lookAssistDaylightPatchIsNeutralEnough( const LookAssistAutoWhiteBalancePatch &patch );

/* The slider ranges the receipt controls live in (MainWindow.ui; a test pins the equality). */
static const int kLookAssistTemperatureMin = 2000;
static const int kLookAssistTemperatureMax = 10000;
static const int kLookAssistTintMin = -100;
static const int kLookAssistTintMax = 100;

/* ---- The ONE white-balance decision: solve -> stability -> damping -> as-shot prior -> clamp. ----
 * GUI sync, GUI async and the headless applier all call this and nothing else; none of them
 * contains the sequence. The caller supplies only what differs between them: the solver (the live
 * one on the UI thread, the isolated one on the worker) and the control ranges. */
struct LookAssistWhiteBalanceRequest
{
    const LookAssistStats *stats = nullptr;
    LookAssistScene scene = LookAssistScene::Shade;
    LookAssistAutoWhiteBalancePatch patch;
    bool solvedOnProcessedPicture = false;
    int baseTemperature = 6000;
    int baseTint = 0;
    int minTemperature = kLookAssistTemperatureMin;
    int maxTemperature = kLookAssistTemperatureMax;
    int minTint = kLookAssistTintMin;
    int maxTint = kLookAssistTintMax;
};

struct LookAssistWhiteBalanceResolution
{
    bool autoValid = false;
    QString source = QStringLiteral("none");
    QString decision = QStringLiteral("none");
    double damping = 1.0;
    int solvedTemperature = 0;      // after damping (what was accepted); 0 when there was no patch
    int solvedTint = 0;
    int candidateTemperature = 0;   // the raw solver answer after the control clamp; 0 when no patch
    int candidateTint = 0;
    int temperature = 6000;         // final, clamped into the scene's window
    int tint = 0;
};

typedef std::function<void( int rawX, int rawY, int *temperature, int *tint )> LookAssistWhiteBalanceSolveFn;

/* preset->temperatureDelta / tintDelta are read (the colour-balance default) and rewritten to
 * final - base, so the preset always describes what was applied. */
LookAssistWhiteBalanceResolution resolveLookAssistWhiteBalance( const LookAssistWhiteBalanceRequest &request,
                                                                const LookAssistWhiteBalanceSolveFn &solve,
                                                                LookAssistPreset *preset );

int lookAssistDisplayTargetMedianForScene( LookAssistScene scene );

LookAssistPreset presetForLookAssistScene( LookAssistScene scene,
                                           const LookAssistStats &stats,
                                           const LookAssistStats *colorStats = nullptr,
                                           const LookAssistStats *displayStats = nullptr );

} // namespace lookassist

#endif // LOOKASSISTANALYSIS_H
