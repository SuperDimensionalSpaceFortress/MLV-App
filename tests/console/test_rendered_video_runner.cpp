#include "../common/minitest.h"

#include <string>
#include "../../src/batch/BatchRenderedVideoPlan.h"
/* GUI-free: BatchRunner.h includes only QString + the batch type/plan headers, so the static stretch
 * helpers are reachable without linking BatchRunner.cpp. */
#include "../../src/batch/BatchRunner.h"

/* Pins the pure, GUI-free pieces of the E4-1 rendered-video runner.
 *
 * Everything here is reachable without linking BatchRunner.cpp (which pulls in
 * MainWindow.h and the whole GUI), because the pure plan helpers live in
 * BatchRenderedVideoPlan.cpp. The runner's process orchestration is proven end-to-end instead, by
 * running the real binary against tests/fixtures/clips/tiny_dual_iso.mlv. */

/* ---------------------------------------------------------------------------
 * ffmpeg stream-dump parsing -- the aspect check the roadmap's blocking proof names.
 *
 * The two dumps below are VERBATIM ffmpeg n4.4 output captured from this repo's own
 * shipped binary (platform/qt/FFmpeg/ffmpegWin64.zip), not hand-written approximations.
 * That matters: the value of these tests is that they fail if ffmpeg's format changes.
 * ------------------------------------------------------------------------- */

/* Real output for a file THIS runner produced (square pixels, so no [SAR] bracket). */
static QString squarePixelDump()
{
    return QStringLiteral(
        "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'C:/mlvtmp/e4-1-proof/out/aspect-check.mp4':\n"
        "  Metadata:\n"
        "    encoder         : Lavf58.76.100\n"
        "  Duration: 00:00:00.08, start: 0.000000, bitrate: 265739 kb/s\n"
        "  Stream #0:0(und): Video: h264 (High) (avc1 / 0x31637661), yuv420p(tv, bt709), 5424x2268, 267513 kb/s, 23.98 fps, 23.98 tbr, 11988 tbn, 47.95 tbc (default)\n"
        "    Metadata:\n"
        "      handler_name    : VideoHandler\n" );
}

/* Real output for a deliberately NON-square file, which is how a genuine aspect defect
 * would present: the picture would be re-stretched on playback. */
static QString nonSquarePixelDump()
{
    return QStringLiteral(
        "Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'C:/mlvtmp/e4-1-proof/out/sar-nonsquare.mp4':\n"
        "  Duration: 00:00:01.00, start: 0.000000, bitrate: 108 kb/s\n"
        "  Stream #0:0(und): Video: h264 (High) (avc1 / 0x31637661), yuv420p, 720x576 [SAR 16:15 DAR 4:3], 104 kb/s, 25 fps, 25 tbr, 12800 tbn, 50 tbc (default)\n" );
}

TEST( RenderedVideoRunner, FfmpegDumpYieldsCodecContainerAndGeometry )
{
    const BatchRenderedVideoFfmpegDumpFacts facts =
        batchRenderedVideoFfmpegDumpFacts( squarePixelDump() );

    ASSERT_TRUE( facts.parsed );
    ASSERT_EQ( std::string("h264"), (facts.codecName).toStdString() );
    ASSERT_EQ( std::string("mov,mp4,m4a,3gp,3g2,mj2"), (facts.formatNames).toStdString() );
    ASSERT_EQ( 5424, facts.width );
    ASSERT_EQ( 2268, facts.height );
}

/* The load-bearing case: no [SAR] bracket must be read as SQUARE, and must be labelled
 * as an inference rather than as something ffmpeg asserted. */
TEST( RenderedVideoRunner, AbsentSarBracketMeansSquarePixelsAndIsLabelledImplied )
{
    const BatchRenderedVideoFfmpegDumpFacts facts =
        batchRenderedVideoFfmpegDumpFacts( squarePixelDump() );

    ASSERT_EQ( std::string("1:1(implied)"), (facts.sampleAspectRatio).toStdString() );
    ASSERT_TRUE( batchRenderedVideoSampleAspectIsSquare( facts.sampleAspectRatio ) );
    /* 5424:2268 reduced by gcd 12. A wrong gcd would still "look reasonable", so the
     * exact reduced value is pinned rather than merely a non-empty string. */
    ASSERT_EQ( std::string("452:189"), (facts.displayAspectRatio).toStdString() );
}

/* The teeth. If this check cannot fail, it is not a check -- and a parser that has only
 * ever been run against good input is untested. */
TEST( RenderedVideoRunner, NonSquareSarIsParsedAndRejected )
{
    const BatchRenderedVideoFfmpegDumpFacts facts =
        batchRenderedVideoFfmpegDumpFacts( nonSquarePixelDump() );

    ASSERT_TRUE( facts.parsed );
    ASSERT_EQ( 720, facts.width );
    ASSERT_EQ( 576, facts.height );
    ASSERT_EQ( std::string("16:15"), (facts.sampleAspectRatio).toStdString() );
    ASSERT_EQ( std::string("4:3"), (facts.displayAspectRatio).toStdString() );
    ASSERT_FALSE( batchRenderedVideoSampleAspectIsSquare( facts.sampleAspectRatio ) );
}

/* Fail closed: unparsable text must report NOTHING rather than a confident default,
 * because `parsed` is what gates the codec/container/aspect failures downstream. */
TEST( RenderedVideoRunner, UnparsableDumpClaimsNothing )
{
    for( const QString & text : QStringList{ QString(),
                                             QStringLiteral("ffmpeg version n4.4\n"),
                                             QStringLiteral("Input #0, mov,mp4, from 'x':\n  Stream #0:0: Audio: aac, 48000 Hz\n") } )
    {
        const BatchRenderedVideoFfmpegDumpFacts facts =
            batchRenderedVideoFfmpegDumpFacts( text );
        ASSERT_FALSE( facts.parsed );
        ASSERT_EQ( 0, facts.width );
        ASSERT_TRUE( facts.sampleAspectRatio.isEmpty() );
    }
}

TEST( RenderedVideoRunner, EmptySampleAspectIsTreatedAsSquare )
{
    /* ffprobe renders an UNSET sample aspect ratio as "0:1"; that is not a degenerate
     * ratio and must not be reported as an aspect defect. */
    ASSERT_TRUE( batchRenderedVideoSampleAspectIsSquare( QString() ) );
    ASSERT_TRUE( batchRenderedVideoSampleAspectIsSquare( QStringLiteral("1:1") ) );
    ASSERT_TRUE( batchRenderedVideoSampleAspectIsSquare( QStringLiteral("0:1") ) );
    ASSERT_FALSE( batchRenderedVideoSampleAspectIsSquare( QStringLiteral("16:15") ) );
    ASSERT_FALSE( batchRenderedVideoSampleAspectIsSquare( QStringLiteral("64:45") ) );
}

/* ---------------------------------------------------------------------------
 * Atomic-write temporary path.
 * ------------------------------------------------------------------------- */

/* THE MARKER MUST GO BEFORE THE EXTENSION. ffmpeg selects its muxer from the output
 * file extension, so a trailing marker makes it fail with "Unable to find a suitable
 * output format" -- observed exactly that way on the first end-to-end run of this path,
 * and this test is what stops it coming back. */
TEST( RenderedVideoRunner, PartialOutputPathKeepsTheExtensionLast )
{
    ASSERT_EQ( std::string("C:/out/clip.mlvapp-partial.mp4"), (batchRenderedVideoPartialOutputPath( QStringLiteral("C:/out/clip.mp4") )).toStdString() );
    /* A dotted directory or basename must not confuse the suffix split. */
    ASSERT_EQ( std::string("C:/my.dir/clip.v2.mlvapp-partial.mov"), (batchRenderedVideoPartialOutputPath( QStringLiteral("C:/my.dir/clip.v2.mov") )).toStdString() );
    /* No extension at all -> marker simply appended. */
    ASSERT_EQ( std::string("C:/out/clip.mlvapp-partial"), (batchRenderedVideoPartialOutputPath( QStringLiteral("C:/out/clip") )).toStdString() );
}

/* Fail closed rather than writing to a bare marker file. */
TEST( RenderedVideoRunner, PartialOutputPathRefusesEmptyInput )
{
    ASSERT_TRUE( batchRenderedVideoPartialOutputPath( QString() ).isEmpty() );
    ASSERT_TRUE( batchRenderedVideoPartialOutputPath( QStringLiteral("   ") ).isEmpty() );
}

/* ---------------------------------------------------------------------------
 * Re-aiming the ffmpeg command at the atomic-write temporary.
 * ------------------------------------------------------------------------- */

static BatchRenderedVideoFfmpegCommandPlan readyCommandPlan()
{
    BatchRenderedVideoFfmpegCommandPlan plan;
    plan.ready = true;
    plan.rawInputArguments = QStringLiteral("-r 23.976 -y -f rawvideo -s 5424x2268 -pix_fmt rgb48 -i -");
    plan.videoArguments    = QStringLiteral("-c:v libx264 -preset medium -crf 14 -pix_fmt yuv420p");
    plan.colorArguments    = QStringLiteral("-color_primaries bt709 -color_trc bt709 -colorspace bt709");
    plan.filterArguments   = QStringLiteral("-vf scale=in_color_matrix=bt601:out_color_matrix=bt709");
    plan.audioArguments    = QString();
    return plan;
}

/* The comment on batchRenderedVideoFfmpegArgumentsWithOutputPath claims the ONLY
 * difference from the assembled plan is the output token. This asserts that claim
 * literally: rebuild the expected string from the same parts and compare. */
TEST( RenderedVideoRunner, ArgumentsWithOutputPathDifferOnlyInTheOutputToken )
{
    const BatchRenderedVideoFfmpegCommandPlan plan = readyCommandPlan();
    const QString retargeted = batchRenderedVideoFfmpegArgumentsWithOutputPath(
        plan, QStringLiteral("C:/out/clip.mlvapp-partial.mp4") );

    const QString expected = QStringLiteral("%1 %2 %3 %4 %5 \"%6\"")
        .arg(plan.rawInputArguments)
        .arg(plan.videoArguments)
        .arg(plan.colorArguments)
        .arg(plan.filterArguments)
        .arg(plan.audioArguments)
        .arg(QStringLiteral("C:/out/clip.mlvapp-partial.mp4"));

    ASSERT_EQ( expected.toStdString(), retargeted.toStdString() );
    /* Every encoder/colour/filter decision survived the re-aim. */
    ASSERT_TRUE( retargeted.contains( QStringLiteral("-c:v libx264") ) );
    ASSERT_TRUE( retargeted.contains( QStringLiteral("-crf 14") ) );
    ASSERT_TRUE( retargeted.contains( QStringLiteral("out_color_matrix=bt709") ) );
}

/* An unrunnable invocation is the correct answer for a caller that forgot to check --
 * far better than a truncated command that ffmpeg would partially honour. */
TEST( RenderedVideoRunner, ArgumentsWithOutputPathRefuseUnreadyPlanOrEmptyPath )
{
    BatchRenderedVideoFfmpegCommandPlan notReady = readyCommandPlan();
    notReady.ready = false;
    ASSERT_TRUE( batchRenderedVideoFfmpegArgumentsWithOutputPath(
        notReady, QStringLiteral("C:/out/clip.mp4") ).isEmpty() );

    ASSERT_TRUE( batchRenderedVideoFfmpegArgumentsWithOutputPath(
        readyCommandPlan(), QString() ).isEmpty() );
}

/* ---------------------------------------------------------------------------
 * Re-aiming the MEDIA PROBE at the file actually under verification -- [B2].
 *
 * The plan bakes the FINAL output path into its probe arguments at plan-build time. After
 * verification moved ahead of the publish ([B1]), executing that string verbatim meant the
 * ffprobe branch inspected the PREVIOUS export on any re-run, and because a successful
 * parse suppresses the ffmpeg-dump fallback, the codec / container / SAR checks passed
 * against a file that was never the one being verified.
 *
 * This is the level the defect actually lives at, so it is the level that is pinned. An
 * end-to-end test would need both a pre-existing output AND a resolvable ffprobe, which is
 * not reachable from this suite -- MLVApp ships no ffprobe and the resolver searches the
 * application directory, PATH and the ffmpeg sibling directory, none of which a console test
 * controls.
 * ------------------------------------------------------------------------- */

/* [R-1], LANE-4 review of 29af0972..6d52f0ae, folded into this block per fable SEQ 1217 [2].
 *
 * This fixture is built by the REAL builder, not hand-written. The earlier version
 * hand-wrote `plan.arguments` as a literal, which made the drift test below vacuous: the
 * format string is duplicated between batchRenderedVideoMediaProbeCommandPlanFromContracts
 * and batchRenderedVideoMediaProbeArgumentsWithPath, and that test is the ONLY thing
 * holding them together -- so if the production builder gained a flag, production would
 * drift and the test would still compare the re-aimed string against a stale literal and
 * pass. Building the fixture through the real chain means the drift test now fails when
 * the two actually diverge, which is the whole reason it exists. */
static BatchRenderedVideoMediaProbeCommandPlan readyProbePlan()
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoTarget target =
        batchRenderedVideoTargetFromRequest(request);
    BatchRenderedVideoOutputPlan outputPlan =
        batchRenderedVideoOutputPlanFromPaths(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders"),
            target);
    BatchRenderedVideoOutputVerificationPlan verificationPlan =
        batchRenderedVideoOutputVerificationPlanFromOutput( outputPlan, target );
    return batchRenderedVideoMediaProbeCommandPlanFromContracts(
        verificationPlan,
        batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
            QStringLiteral("ffprobe"),
            QStringLiteral("C:/tools/ffprobe.exe") ) );
}

/* The fixture is only useful if the real chain actually produced a ready plan -- a builder
 * change that made it unready would otherwise turn every assertion below into a vacuous
 * pass against an empty string. */
TEST( RenderedVideoRunner, ProbePlanFixtureIsBuiltByTheRealBuilderAndIsReady )
{
    const BatchRenderedVideoMediaProbeCommandPlan plan = readyProbePlan();
    ASSERT_TRUE( plan.commandReady );
    ASSERT_FALSE( plan.arguments.isEmpty() );
    ASSERT_FALSE( plan.expectedOutputPath.isEmpty() );
    /* The builder derives this from the output plan; pinning it keeps the path-bearing
     * assertions below meaningful rather than accidentally true. */
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"), plan.expectedOutputPath.toStdString() );
}

TEST( RenderedVideoRunner, MediaProbeArgumentsNameTheProbedFileNotThePlannedOutput )
{
    const BatchRenderedVideoMediaProbeCommandPlan plan = readyProbePlan();
    /* Derived from the plan's own final path via the production helper, so this stays
     * correct if the builder's output naming ever changes. */
    const QString partial = batchRenderedVideoPartialOutputPath( plan.expectedOutputPath );
    const QString args = batchRenderedVideoMediaProbeArgumentsWithPath( plan, partial );

    /* The whole point: the FINAL path must NOT appear, or the probe reads the wrong file.
     * Checked as a whole token, because the final path is a SUBSTRING of the partial path
     * only if the marker were appended -- it is not, the marker goes before the extension,
     * so an exact-token absence check is the right test. */
    ASSERT_TRUE( args.contains( partial ) );
    ASSERT_FALSE( args.endsWith( plan.expectedOutputPath ) );
    ASSERT_EQ( std::string("-v error -show_format -show_streams -of json C:/renders/M16-1327.mlvapp-partial.mp4"),
               args.toStdString() );
    /* And the plan's own declared output path is left alone -- it correctly names the
     * artefact the caller was promised, and the verification report depends on it. */
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"), plan.expectedOutputPath.toStdString() );
}

/* Only the probed path may differ from the plan's own argument string. A drifting flag set
 * would silently change what ffprobe reports and therefore what is checked. */
TEST( RenderedVideoRunner, MediaProbeArgumentsDifferFromThePlanOnlyInThePath )
{
    const BatchRenderedVideoMediaProbeCommandPlan plan = readyProbePlan();
    const QString rebuilt =
        batchRenderedVideoMediaProbeArgumentsWithPath( plan, plan.expectedOutputPath );
    ASSERT_EQ( plan.arguments.toStdString(), rebuilt.toStdString() );
}

/* A path containing spaces must be quoted, or splitCommand() would tear it into several
 * arguments and ffprobe would read neither file. */
TEST( RenderedVideoRunner, MediaProbeArgumentsQuoteASpacedPath )
{
    const QString spaced = QStringLiteral("C:/my renders/clip.mlvapp-partial.mp4");
    const QString args = batchRenderedVideoMediaProbeArgumentsWithPath( readyProbePlan(), spaced );
    ASSERT_EQ( std::string("-v error -show_format -show_streams -of json \"C:/my renders/clip.mlvapp-partial.mp4\""),
               args.toStdString() );
}

/* Fail closed: an unrunnable invocation is far better than one silently aimed at the
 * wrong file, because the caller skips the branch and the dump fallback still checks the
 * right one. */
TEST( RenderedVideoRunner, MediaProbeArgumentsRefuseUnreadyPlanOrEmptyPath )
{
    BatchRenderedVideoMediaProbeCommandPlan notReady = readyProbePlan();
    notReady.commandReady = false;
    ASSERT_TRUE( batchRenderedVideoMediaProbeArgumentsWithPath(
        notReady, QStringLiteral("C:/renders/clip.mlvapp-partial.mp4") ).isEmpty() );

    ASSERT_TRUE( batchRenderedVideoMediaProbeArgumentsWithPath(
        readyProbePlan(), QString() ).isEmpty() );
    ASSERT_TRUE( batchRenderedVideoMediaProbeArgumentsWithPath(
        readyProbePlan(), QStringLiteral("   ") ).isEmpty() );
}

/* ---------------------------------------------------------------------------
 * Runner prerequisites.
 * ------------------------------------------------------------------------- */

/* The gate that kept E4-1 unimplemented was a hardcoded `false`. These assertions are
 * what make flipping it back a test failure rather than a silent regression. */
TEST( RenderedVideoRunner, VideoOnlyPrerequisitesAreReadyAndAudioGapIsExplicit )
{
    const BatchRenderedVideoRunnerPrerequisites prerequisites =
        batchRenderedVideoRunnerPrerequisitesForCurrentBuild();

    ASSERT_TRUE( prerequisites.processingParityReady );
    ASSERT_TRUE( prerequisites.frameProcessingReady );
    ASSERT_TRUE( prerequisites.ffmpegExecutionReady );
    ASSERT_TRUE( prerequisites.outputVerificationReady );
    ASSERT_TRUE( prerequisites.headlessRunnerReady );
    ASSERT_TRUE( prerequisites.videoOnlyRunnerReady );
    ASSERT_TRUE( prerequisites.ready );

    /* Audio muxing is deferred to E4-2, and the deferral must stay LEGIBLE: a reader of
     * the summary has to be able to see the gap rather than infer it from silence. */
    ASSERT_FALSE( prerequisites.audioMuxReady );
    ASSERT_FALSE( prerequisites.limitation.isEmpty() );
    ASSERT_TRUE( prerequisites.reason.isEmpty() );

    const QString summary =
        batchRenderedVideoRunnerPrerequisitesSummary( prerequisites );
    ASSERT_TRUE( summary.contains( QStringLiteral("runner-ready=true") ) );
    ASSERT_TRUE( summary.contains( QStringLiteral("runner-audio-mux-ready=false") ) );
    ASSERT_FALSE( summary.contains( QStringLiteral("runner-limitation=none") ) );
}

/* Stretch defaults shared by the CDNG and rendered runners. A receipt that was never
 * loaded leaves stretch at -1, and a headless export that skips the GUI's fill-in emits
 * squeezed output for binned modes (5D3 1080p is binning_x=3/binning_y=1). */
TEST( RenderedVideoRunner, UnsetStretchFallsBackToTheClipAspectBands )
{
    /* An explicit receipt value always wins. */
    ASSERT_NEAR( 2.0, BatchRunner::effectiveStretchFactorX( 2.0 ), 1e-9 );
    ASSERT_NEAR( 1.75, BatchRunner::effectiveStretchFactorY( 1.75, 3.0f ), 1e-9 );

    /* Unset (-1) falls back to the band the clip's RAWC aspect lands in. */
    ASSERT_NEAR( STRETCH_H_100, BatchRunner::effectiveStretchFactorX( -1.0 ), 1e-9 );
    ASSERT_NEAR( STRETCH_V_100, BatchRunner::effectiveStretchFactorY( -1.0, 1.0f ), 1e-9 );
    ASSERT_NEAR( STRETCH_V_167, BatchRunner::effectiveStretchFactorY( -1.0, 1.67f ), 1e-9 );
    ASSERT_NEAR( STRETCH_V_300, BatchRunner::effectiveStretchFactorY( -1.0, 3.0f ), 1e-9 );

    /* No RAWC info at all is treated as square, not as an out-of-band value. */
    ASSERT_NEAR( STRETCH_V_100, BatchRunner::effectiveStretchFactorY( -1.0, 0.0f ), 1e-9 );

    double stretchX = 0.0;
    double stretchY = 0.0;
    BatchRunner::effectiveStretchFactors( 1.0, 1.0, 0.8f,
                                          &stretchX, &stretchY );
    ASSERT_NEAR( STRETCH_H_125, stretchX, 1e-9 );
    ASSERT_NEAR( STRETCH_V_100, stretchY, 1e-9 );
    BatchRunner::effectiveStretchFactors( -1.0, -1.0, 4.0f / 3.0f,
                                          &stretchX, &stretchY );
    ASSERT_NEAR( STRETCH_H_125, stretchX, 1e-9 );
    ASSERT_NEAR( STRETCH_V_167, stretchY, 1e-9 );
    BatchRunner::effectiveStretchFactors( 2.0, 1.75, 0.8f,
                                          &stretchX, &stretchY );
    ASSERT_NEAR( 2.0, stretchX, 1e-9 );
    ASSERT_NEAR( 1.75, stretchY, 1e-9 );
}
