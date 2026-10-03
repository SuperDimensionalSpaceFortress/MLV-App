#ifndef LOOKASSISTANALYSIS_H
#define LOOKASSISTANALYSIS_H

#include <QString>

#include <functional>
#include <vector>

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
    // No aperture recorded (a manual lens: LENS.aperture = 0) but ISO and shutter are: EV100 at f/1.0, a LOWER BOUND of
    // the scene's EV100 (a real aperture only raises it). Never set together with hasSceneEv100, which keeps its meaning.
    bool hasSceneEv100Bound = false;
    double sceneEv100Bound = 0.0;
    // Dual ISO: stops from the recorded ISO up to the more sensitive recovery ISO (decoded DISO), 0 when there is none.
    double sceneRecoveryStops = 0.0;
    // Fraction of pixels in the mid-tone band (luma 40..215): "a lit picture", not "a dark field
    // with a small bright region".
    double midtoneFraction = 0.0;
    // The RENDERED picture, at the exposure the daylight verdict would apply, is a lit daylight picture (set only by
    // resolveLookAssistScene). The recorded exposure alone is NOT proof of daylight: a night moon
    // shot at ISO 200, 1/500 s, f/7.1 records EV100 13.6 over a black sky.
    bool daylightPictureEvidence = false;
    // A window-lit interior: a flat-floor "night" verdict with no recorded exposure whose own neutral-patch solve
    // is daylight and verifies on the picture (set only by resolveLookAssistWindowLitInterior). Daylight evidence
    // in its own right; see lookAssistSceneIsDaylight.
    bool windowLitInteriorEvidence = false;
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
/* recoveryIsoValue = the decoded dual-ISO recovery ISO (ReceiptApplier::lookAssistRecoveryIso; 0 = none). With no aperture but ISO and shutter recorded, the stats
 * get the f/1.0 lower bound instead (hasSceneEv100Bound); the recorded EV100 is computed exactly as before. */
void lookAssistSetSceneEv100( LookAssistStats *stats,
                              double isoValue,
                              double shutterMicroseconds,
                              double apertureTimes100,
                              double recoveryIsoValue = 0.0 );

/* ---- The aperture lower bound (LOOK-ASSIST-M16-NOT-NIGHT-1) ----
 * Night scenes are EV100 ~0-5, lit night interiors ~5-7. A clip whose f/1.0 bound is at least 7 is therefore not
 * night whatever lens was on it. The bound uses the RECORDED ISO, the same ISO the recorded EV100 uses for every other
 * clip (dual ISO included). Dual ISO: the recovery rows are the same light with more gain, so a scene exposed for them
 * could be up to the recovery stops darker; the bound minus those stops must still be above the dark-night band (>= 5).
 * M16-1243: ISO 100, 1/1357 s -> bound 10.4; a 4-stop recovery would leave 6.4, still >= 5. */
static const double kLookAssistNotNightEv100 = 7.0;
static const double kLookAssistDarkNightEv100 = 5.0;
bool lookAssistExposureBoundExcludesNight( const LookAssistStats &stats );

/* The recorded exposure is bright enough for daylight (open shade ~12, overcast ~13, sun ~15; lit
 * interiors <= ~10). NECESSARY, never sufficient: see lookAssistSceneIsDaylight. */
bool lookAssistExposureIsDaylightBright( const LookAssistStats &stats );

/* Daylight = bright recorded exposure AND the rendered picture agrees, or a verified window-lit interior. */
bool lookAssistSceneIsDaylight( const LookAssistStats &stats );

/* Daylight and not a scene class that excludes it. Every daylight-only rule below keys on this. */
bool lookAssistIsDaylightScene( const LookAssistStats &stats, LookAssistScene scene );

/* The legacy verdict is Night / ArtificialLights, the RAW thumbnail is a flat floor (so it cannot
 * speak), and the exposure is daylight-bright: the picture has to be consulted. */
bool lookAssistDaylightNeedsPictureEvidence( const LookAssistStats &stats, LookAssistScene legacyScene );

/* The first of the three NON-picture conjuncts of lookAssistDaylightNeedsPictureEvidence that fails, in the
 * order they are tried (recorded exposure daylight-bright, RAW thumbnail a flat floor, legacy verdict
 * Night / ArtificialLights); Open when all three hold and only the rendered picture is left to decide.
 * lookAssistDaylightNeedsPictureEvidence is built on this, so the log and the decision cannot disagree. */
enum class LookAssistDaylightGate
{
    Exposure,
    FlatFloor,
    Legacy,
    Open
};
LookAssistDaylightGate lookAssistDaylightGate( const LookAssistStats &stats, LookAssistScene legacyScene );

/* Observation only (LOOK-ASSIST-DIAG-LOGGING-1): which branch of the GUI's night post-balance walk produced the
 * final balance. None = the walk made no change (or did not run). A branch is recorded only when it CHANGED the
 * balance: the recovery table is Recovery only when it adopted a candidate, never merely because it was entered. */
enum class LookAssistPostWalkBranch
{
    None,
    Steps,
    Cleanup,
    Recovery
};

/* What a consumer knows about HOW the verdict and balance were reached, handed to
 * lookAssistDecisionLogFields. Defaults are the headless applier's: no picture asked, no walk, no display meter. */
struct LookAssistDecisionTrace
{
    bool pictureEvidenceAsked = false;                       // a render callback was given to resolveLookAssistScene
    bool postWalkRan = false;                                // the night post-balance walk was entered
    LookAssistPostWalkBranch postWalkBranch = LookAssistPostWalkBranch::None;
    int recoveryTemperatureDelta = 0;                        // the pair the recovery table ADOPTED (Recovery only)
    int recoveryTintDelta = 0;
    bool displayMeterRan = false;                            // the display-space exposure meter produced samples
    int playbackScaleFactor = 0;                             // effective playback scale (0 = not applicable)
    QString surfaceSearch = QStringLiteral("not-run");       // the verified-surface balance search (LookAssistWindowLitCheck)
    int surfaceSearchTemperature = 0;                        // the balance it found (converged only)
    int surfaceSearchTint = 0;
};

/* The walk's branch bookkeeping, kept pure so the GUI walk and the unit tests run the very same code. Call in walk
 * order. Each records its branch only when that branch changed the balance; a branch that ran and changed nothing
 * leaves the earlier branch (and its pair) standing. */
void lookAssistTraceWalkSteps( LookAssistDecisionTrace *trace, bool stepsAdjustedBalance );
void lookAssistTraceWalkCleanup( LookAssistDecisionTrace *trace );   // call where the cleanup raised the tint
void lookAssistTraceWalkRecovery( LookAssistDecisionTrace *trace, bool candidateAdopted,
                                  int temperatureDelta, int tintDelta );

/* daylight_gate value: window | exposure | flatfloor | legacy | picture | pass | n/a, for the stats AFTER
 * resolveLookAssistScene (and resolveLookAssistWindowLitInterior). window = a verified window-lit interior (see there);
 * pass = the rendered picture corroborated daylight; picture = every other conjunct held
 * and the picture did not (or could not) say so; n/a = every other conjunct held and no picture was asked for
 * (the master pass). An earlier failing conjunct is reported whether or not a picture was asked for. */
QString lookAssistDaylightGateName( const LookAssistStats &resolved, bool pictureEvidenceAsked );

/* The fields appended to the end of the Look Assist result log lines, space separated, no leading space:
 *   has_ev100=0|1 ev100=(3 decimals, truncated not rounded so it never reads 11 below the gate's 11.0, or NA) daylight_gate=(see above) post_walk_ran=0|1
 *   post_walk_branch=(none, steps, cleanup or recovery) post_walk_recovery=(temp delta, slash, tint delta; NA unless
 *   recovery) display_meter_ran=0|1 playback_scale=(n or NA)
 *   ev100_bound=(the f/1.0 lower bound, 3 decimals truncated, or NA) ev100_source=(recorded, aperture_bound or none)
 *   surface_search=(not-run, converged, not-converged or unverifiable) surface_search_balance=(temp/tint, or NA)
 * Pure; reads nothing it was not given. */
QString lookAssistDecisionLogFields( const LookAssistStats &resolved, const LookAssistDecisionTrace &trace );

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
 * Daylight scenes (recorded exposure AND the rendered picture agree) cannot be tungsten: below 4800 K or past +10 tint a
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

/* The as-shot white balance of a corroborated daylight clip, clamped into the daylight bounds. It is only the
 * START of the render-based refinement (the unstepped BASE picture is rendered there; see
 * LookAssistWhiteBalanceRequest::renderBalance), never an answer: with no verified surface the balance is
 * master's (LookAssistWhiteBalanceResolution::legacyBalance). */
bool lookAssistAsShotPrior( const LookAssistStats &stats, LookAssistScene scene, int *temperature, int *tint );

/* The RAW thumbnail is a flat floor (dual-ISO/raw preview lift): unusable for colour, any scene. */
bool lookAssistIsFlatFloorRawThumbnail( const LookAssistStats &stats );
/* Flat floor AND night: the night-only exposure/shadow rescue. Stays night-only. */
bool lookAssistIsFloorLiftedNightThumbnail( LookAssistScene scene, const LookAssistStats &stats );
/* Colour must come from the rendered (processed) picture: the RAW thumbnail is a flat floor AND the scene
 * is night (the rescue) or a corroborated daylight picture; every other scene keeps the raw-thumbnail balance. */
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

/* How far the rendered picture is from neutral: the post-balance score the sync path has always used
 * to keep or revert a refinement step (green axis, half the blue-amber axis, the visible-green axis,
 * green-artifact area). Lower is better. ONE definition for every consumer. */
double lookAssistBalanceScore( const LookAssistStats &rendered );

/* A rendered picture: the 8-bit RGB thumbnail of the planned look at a white balance, and its statistics.
 * downscaleFactor is the thumbnail's factor against the RAW frame (the neutral-patch search needs it). */
struct LookAssistRenderedPicture
{
    LookAssistStats stats;
    std::vector<unsigned char> rgb;
    int width = 0;
    int height = 0;
    int downscaleFactor = 1;
};

/* Render the planned look at a white balance (exposure and white balance only, at the preset's stops
 * without the display offset) on a private clone. temperature in K, tint in receipt units (tenths).
 * Supplied by the consumer that owns the live picture (GUI sync on the UI thread, headless); the daylight
 * refinement only ever asks it questions about the picture, always at the exposure the look is about to
 * apply. The thumbnail geometry is the same for every call, so a pixel is the same surface in every render. */
typedef std::function<bool( double exposureStops, int temperature, int tint, LookAssistRenderedPicture *picture )> LookAssistRenderBalanceFn;

/* The narrowing switch (hub ruling): false = a corroborated daylight scene with no trusted patch gets MASTER's
 * balance (its colour-balance default plus, in the GUI, its legacy post-balance walk) and renders nothing
 * extra; an initial patch is then not verifiable either, so that clip is master's too (master's damped solve).
 * Flip it to land only the states proven no worse than master. */
static const bool kLookAssistRefineDaylightWithoutPatch = true;

/* The switch as every consumer reads it: the constant, which the environment variable
 * MLVAPP_LOOK_ASSIST_REFINE_DAYLIGHT=0 may also turn OFF (never on). The narrowing exit is then measurable in
 * the real app and in the headless applier without a rebuild. LookAssistWhiteBalanceRequest takes its default
 * from here, so a test can drive both settings per request. */
bool lookAssistRefineDaylightWithoutPatchEnabled();

/* ---- The ONE white-balance decision: solve -> stability -> damping -> refine -> master fallback -> clamp. ----
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
    // RAW frame size (the neutral-patch search maps thumbnail pixels back to RAW coordinates).
    int rawWidth = 0;
    int rawHeight = 0;
    // When set, a corroborated daylight scene WITHOUT a trusted neutral patch is balanced from the
    // RENDERED picture: the picture is stepped until it has neutral samples, the same patch search and
    // solver run on it, every candidate surface is also judged on the unstepped base picture, and the result
    // is verified on that same surface at the solution. Unset (or nothing acquired) = master's balance.
    LookAssistRenderBalanceFn renderBalance;
    // The narrowing switch, per request (default: lookAssistRefineDaylightWithoutPatchEnabled()).
    bool refineWithoutPatch = lookAssistRefineDaylightWithoutPatchEnabled();
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
    // Corroborated daylight with no trusted patch and no verified surface: the balance is MASTER's (the
    // colour-balance default the preset arrived with). The GUI then runs master's legacy post-balance walk.
    bool legacyBalance = false;
    // The render-based refinement (corroborated daylight, no trusted patch). attempted = it ran;
    // refined = it moved the white balance, always to a verified neutral-patch solution found on a rendered
    // picture (patchAcquired); with none, the balance is master's (legacyBalance) and refined is false.
    bool refineAttempted = false;
    bool refineRefusedAtBase = false;       // the best surface of a stepped picture failed the guard at BASE
    double refineBaseSurfaceChroma = 0.0;   // that surface's chroma / blue-amber in the unstepped base picture
    double refineBaseSurfaceBlueAmber = 0.0;
    bool refined = false;
    bool refinePatchAcquired = false;
    int refineRenders = 0;
    int refineStartTemperature = 0;
    int refineStartTint = 0;
    double refineStartScore = 0.0;
    double refineScore = 0.0;       // balance score of the picture at the final white balance
    double refineBlueAmber = 0.0;   // final measured B-R of the neutral samples
    double refineGreen = 0.0;       // final measured G-(R+B)/2
    double refineStartPatchChroma = 0.0;   // chroma of the acquired surface where it was found
    double refineFinalPatchChroma = 0.0;   // chroma of that SAME surface in the picture rendered at the result
    // The INITIAL patch of a corroborated daylight clip (the one the consumer found on the picture rendered at the
    // existing processing white balance), judged on the surface itself: near-neutral and off the blue locus in the
    // picture rendered at the AS-SHOT prior, and still near-neutral -- no more cast than where it was found -- when
    // rendered at the solution. checked = the judgement ran; refused = the patch is NOT believed (it failed either
    // test, or could not be verified at all) and the clip takes MASTER's path (legacyBalance), never the refinement.
    bool initialPatchChecked = false;
    bool initialPatchRefused = false;
    bool initialPatchRefusedAtBase = false;      // refused by the as-shot base picture (not by the verification)
    double initialPatchBaseChroma = 0.0;         // that surface's chroma / blue-amber in the as-shot base picture
    double initialPatchBaseBlueAmber = 0.0;
    double initialPatchFinalChroma = 0.0;        // that SAME surface's chroma in the picture rendered at the solution
};

typedef std::function<void( int rawX, int rawY, int *temperature, int *tint )> LookAssistWhiteBalanceSolveFn;

/* preset->temperatureDelta / tintDelta are read (the colour-balance default) and rewritten to
 * final - base, so the preset always describes what was applied. When request.renderBalance is set the
 * render-based daylight refinement below runs inside this call (GUI sync and headless). */
LookAssistWhiteBalanceResolution resolveLookAssistWhiteBalance( const LookAssistWhiteBalanceRequest &request,
                                                                const LookAssistWhiteBalanceSolveFn &solve,
                                                                LookAssistPreset *preset );

/* The render-based daylight refinement (called by resolveLookAssistWhiteBalance; public so a test can drive
 * it directly). Does nothing unless request.renderBalance is set, the scene is corroborated daylight and
 * `resolution` holds no trusted patch. `resolution` and `preset` are updated in place, including the final
 * clamp; when it acquires nothing they hold the walk's start and resolveLookAssistWhiteBalance applies
 * master's balance instead. It renders the LIVE picture, so a detached worker must not run it (the GUI
 * sends daylight scenes down its synchronous path for that reason). */
void refineLookAssistDaylightWhiteBalance( const LookAssistWhiteBalanceRequest &request,
                                           const LookAssistWhiteBalanceSolveFn &solve,
                                           LookAssistPreset *preset,
                                           LookAssistWhiteBalanceResolution *resolution );

/* True when refineLookAssistDaylightWhiteBalance has work to do for this resolution (corroborated
 * daylight, no trusted patch, and the narrowing switch `refineEnabled` is on). */
bool lookAssistDaylightNeedsRenderedRefinement( const LookAssistStats &stats,
                                                LookAssistScene scene,
                                                const LookAssistWhiteBalanceResolution &resolution,
                                                bool refineEnabled );

/* ---- Window-lit interior (LOOK-ASSIST-WINDOW-LIT-INTERIOR-1) ----
 * A dual-ISO clip of a dark room with daylight windows: its RAW thumbnail is a flat floor (so the legacy classifier
 * calls it night), it carries no recorded exposure (so the daylight gate cannot open), and the night post-balance walk
 * then overrode its ACCEPTED daylight-locus neutral solve with a blue-magenta recovery pair. The night verdict is kept
 * unless the clip's own solve, on the processed picture, says daylight and verifies:
 *   - candidate: legacy Night, flat-floor RAW thumbnail, no recorded exposure (a clip WITH EV100 keeps today's gate);
 *   - the balance was solved on the processed picture and accepted undamped;
 *   - the patch is window-bright (luma >= 150) and neutral enough under daylight (lookAssistDaylightPatchIsNeutralEnough);
 *   - the solve is bluer than the 6000 K base and on the daylight locus: >= 7000 K and inside the daylight window. Every
 *     night light source (tungsten, sodium, warm or neutral LED, moonlight) solves warmer than the base;
 *   - verified: the same surface, rendered at the patch picture's exposure, is near-neutral at the base balance and at
 *     the solution and no more cast there (the daylight initial-patch guard, with the base balance as the start).
 * Then stats->windowLitInteriorEvidence is set, the scene becomes the daylight class (Shade), the preset is that
 * scene's (same inputs) and the accepted balance stands, clamped into the daylight window (a no-op by the gate).
 * By colour alone (no exposure bound) the check stays MEASURE-ONLY: consumers run it on copies and log it, because on the
 * owner clip the accepted solve did not verify and a colour rule cannot tell that clip from a cool-white LED night.
 *
 * LIVE with the aperture bound (LOOK-ASSIST-M16-NOT-NIGHT-1, `applies`): when lookAssistExposureBoundExcludesNight holds,
 * the exposure, not the colour, rules night out, so the >= 7000 K locus conjunct is not asked. Instead:
 *   - the patch picture is a lit picture: at least 5 % of it at luma >= 100 (p95), which a bright subject on a black
 *     field (a moon, a lamp) is not;
 *   - the balance APPLIED is the solve if its surface verifies, otherwise the balance the verified-surface search finds
 *     for that same surface (searchLookAssistNeutralSurfaceBalance); no verified balance = no evidence (today's night);
 *   - that balance is daylight: inside the daylight window and no warmer than 6000 K. Every night light source (sodium,
 *     tungsten, warm / neutral LED to ~5600 K, moonlight ~4100 K) neutralises warmer than that.
 * The class is the daylight Shade class: "not night, under the daylight exposure of 11" is an interior lit by daylight
 * through windows (its own verified surface says the light is daylight), the Shade preset is the one built for a lit
 * picture without sun, and its window keeps the balance on the daylight locus. A new class would need its own preset,
 * bounds and receipt handling for no gain; and the night post-balance walk does not run for it (daylight rule). */
static const double kLookAssistWindowLitMinPatchLuma = 150.0;
static const int    kLookAssistWindowLitMinTemperature = 7000;
static const int    kLookAssistNotNightMinTemperature = 6000;
static const double kLookAssistLitPictureMinP95 = 100.0;

/* The night verdict could be a window-lit interior: legacy Night from a flat-floor RAW thumbnail, no recorded exposure. */
bool lookAssistWindowLitInteriorCandidate( const LookAssistStats &stats, LookAssistScene scene );

/* A candidate whose aperture bound rules night out: the check may change the verdict (consumers route it to the
 * synchronous path, as daylight, because it renders the live picture). */
bool lookAssistNotNightByExposureBoundCandidate( const LookAssistStats &stats, LookAssistScene scene );

/* ---- The verified-surface balance search ----
 * The processed-patch solver can overshoot (M16-1243: 9930 K / -33 leaves its own patch amber, chroma 6 -> 20). Given the
 * surface (pixel x, y of every render, all of one geometry) measured at two balances, find the balance at which THAT
 * surface renders neutral (|B-R| and |G-(R+B)/2| both under 2) inside `window`, by secant steps in mired and tint whose
 * slopes are refitted from every render. Bounded: at most kLookAssistSurfaceSearchMaxRenders renders. Never guesses: a
 * failed render or another geometry is "unverifiable", no neutral balance within the budget or the window is
 * "not-converged"; only "converged" carries a balance. */
static const int kLookAssistSurfaceSearchMaxRenders = 6;
struct LookAssistSurfaceProbe
{
    int temperature = 0;
    int tint = 0;
    LookAssistAutoWhiteBalancePatch surface;   // the surface measured in the picture rendered at (temperature, tint)
};
struct LookAssistSurfaceSearch
{
    QString result = QStringLiteral("not-run");   // not-run | converged | not-converged | unverifiable
    bool converged = false;
    int temperature = 0;
    int tint = 0;
    int renders = 0;
    LookAssistAutoWhiteBalancePatch surface;      // the surface at the best balance seen
};
LookAssistSurfaceSearch searchLookAssistNeutralSurfaceBalance( const LookAssistRenderBalanceFn &renderBalance,
                                                               double exposureStops,
                                                               const LookAssistRenderedPicture &geometry,
                                                               int x,
                                                               int y,
                                                               const LookAssistWhiteBalanceBounds &window,
                                                               const LookAssistSurfaceProbe &first,
                                                               const LookAssistSurfaceProbe &second );

struct LookAssistWindowLitCheck
{
    bool candidate = false;
    bool evidence = false;
    bool exposureBound = false;   // the aperture bound rules night out: the live rule
    bool applies = false;         // evidence on the live rule: consumers adopt stats, scene and preset
    QString reason = QStringLiteral("not-candidate");   // the first conjunct that failed, or "pass"
    double baseSurfaceChroma = 0.0;
    double baseSurfaceBlueAmber = 0.0;
    double solutionSurfaceChroma = 0.0;
    double solutionSurfaceBlueAmber = 0.0;
    LookAssistSurfaceSearch search;   // run only on the live rule, when the solve does not verify
    int appliedTemperature = 0;       // the balance the evidence applies (0 without evidence)
    int appliedTint = 0;
};

/* The trace fields for the search, from the check (consumers call this; the formatter prints them). */
void lookAssistTraceSurfaceSearch( LookAssistDecisionTrace *trace, const LookAssistWindowLitCheck &check );

/* Runs after resolveLookAssistWhiteBalance on the same request. patchPictureExposureStops = the exposure (stops) of
 * the processed picture the patch was found in, so the verification renders the same picture. On evidence it updates
 * stats (request.stats must point at it), scene and preset as described above; otherwise it changes nothing.
 * colorStats = the patch picture's statistics (the live rule's lit-picture conjunct reads its p95). */
LookAssistWindowLitCheck resolveLookAssistWindowLitInterior( const LookAssistWhiteBalanceRequest &request,
                                                             const LookAssistWhiteBalanceResolution &wb,
                                                             double patchPictureExposureStops,
                                                             LookAssistStats *stats,
                                                             LookAssistScene *scene,
                                                             LookAssistPreset *preset,
                                                             const LookAssistStats *colorStats = nullptr,
                                                             const LookAssistStats *displayStats = nullptr );

int lookAssistDisplayTargetMedianForScene( LookAssistScene scene );

LookAssistPreset presetForLookAssistScene( LookAssistScene scene,
                                           const LookAssistStats &stats,
                                           const LookAssistStats *colorStats = nullptr,
                                           const LookAssistStats *displayStats = nullptr );

} // namespace lookassist

#endif // LOOKASSISTANALYSIS_H
