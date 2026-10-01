#ifndef LOOKASSISTANALYSIS_H
#define LOOKASSISTANALYSIS_H

#include <QString>

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

/* Daylight (open shade ~12, overcast ~13, sun ~15) versus anything dimmer (lit interiors <= ~10). */
bool lookAssistSceneIsDaylightByMetadata( const LookAssistStats &stats );

LookAssistScene classifyLookAssistScene( const LookAssistStats &stats );

/* Colour-temperature / tint window a solved white balance must stay inside. Daylight scenes live
 * on the daylight locus; a "neutral patch" that solves outside it (pale water, a tinted wall)
 * is not neutral. Other scenes keep the full range, exactly as before. Tint is in receipt units
 * (tenths of the processing tint). */
struct LookAssistWhiteBalanceBounds
{
    int minTemperature = 2000;
    int maxTemperature = 10000;
    int minTint = -100;
    int maxTint = 100;
};
LookAssistWhiteBalanceBounds lookAssistWhiteBalanceBounds( const LookAssistStats &stats, LookAssistScene scene );
void lookAssistClampWhiteBalance( const LookAssistWhiteBalanceBounds &bounds, int *temperature, int *tint );

bool lookAssistIsFloorLiftedNightThumbnail( LookAssistScene scene, const LookAssistStats &stats );
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
                                                 int candidateTint );

double lookAssistAutoWhiteBalanceDampingFactor( const LookAssistAutoWhiteBalancePatch &patch,
                                                int baseTemperature,
                                                int baseTint,
                                                int candidateTemperature,
                                                int candidateTint,
                                                LookAssistScene scene );

int lookAssistDisplayTargetMedianForScene( LookAssistScene scene );

LookAssistPreset presetForLookAssistScene( LookAssistScene scene,
                                           const LookAssistStats &stats,
                                           const LookAssistStats *colorStats = nullptr,
                                           const LookAssistStats *displayStats = nullptr );

} // namespace lookassist

#endif // LOOKASSISTANALYSIS_H
