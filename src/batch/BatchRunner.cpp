#include "BatchRunner.h"
#include "CdngSequenceExport.h"
#include "BatchContext.h"
#include "BatchLogger.h"
#include "ReceiptLoader.h"
#include "ReceiptApplier.h"
#include "WorkerThreadCount.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QThread>
#include <QElapsedTimer>
#include <QSettings>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QRegularExpression>

#include <csignal>
#include <cstdlib>

#include "../../platform/qt/ExportCodecIds.h"
#include "../../platform/qt/StretchFactors.h"
#include "../../platform/qt/ReceiptSettings.h"
/* Header-only, GUI-free process seam. This is the SAME machinery the GUI drives its
 * rendered exports through (MainWindow::startExportPipe), which is why the headless
 * runner needs no MainWindow helper and forks no ffmpeg-invocation logic. */
#include "../../platform/qt/ExportProcess.h"
#include "avir/avir.h"
#include "avir/avirthreadpool.h"

static QString batchCdngCodecName(int offset)
{
    switch(offset)
    {
        case 1:  return QStringLiteral("lossless");
        case 2:  return QStringLiteral("fast-pass");
        default: return QStringLiteral("uncompressed");
    }
}

static int normalizedBatchCdngCodecOffset(int offset)
{
    return (offset >= 0 && offset <= 2) ? offset : 0;
}

static BatchRenderedVideoSourceAudioPlan renderedVideoSourceAudioPlanFromOpenMlv(
    mlvObject_t *mlvObject,
    const QString &clipPath)
{
    const bool sourceAudioPresent = doesMlvHaveAudio( mlvObject );
    return batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
        clipPath,
        sourceAudioPresent,
        sourceAudioPresent ? static_cast<int>(getMlvAudioChannels( mlvObject )) : 0,
        sourceAudioPresent ? static_cast<int>(getMlvSampleRate( mlvObject )) : 0,
        sourceAudioPresent ? static_cast<int>(getMlvAudioBitsPerSample( mlvObject )) : 0,
        sourceAudioPresent
            ? static_cast<qulonglong>(getMlvAudioSize( mlvObject ))
            : 0);
}

/* ---------------------------------------------------------------------------
 * Rendered-video (E4-1) support
 * ------------------------------------------------------------------------- */

/* Cancellation. A headless export has no Abort button, so the console signals ARE the
 * cancel channel. The handler does the only thing a signal handler may safely do -- set a
 * flag -- and the frame loop polls it. Combined with the atomic-write temporary this
 * means a cancelled run leaves NO file at the caller's output path, rather than a
 * truncated one that looks finished. */
static volatile std::sig_atomic_t g_batchRenderCancelRequested = 0;

static void batchRenderCancelSignalHandler(int /*signalNumber*/)
{
    g_batchRenderCancelRequested = 1;
}

namespace
{

/* Installs the cancel handlers for the lifetime of one rendered export and restores the
 * previous dispositions afterwards, so a batch run of several clips cannot leave a stale
 * handler behind and CDNG runs in the same process are unaffected. */
class BatchRenderCancelScope
{
public:
    BatchRenderCancelScope()
    {
        g_batchRenderCancelRequested = 0;
        m_previousInt  = std::signal( SIGINT,  batchRenderCancelSignalHandler );
        m_previousTerm = std::signal( SIGTERM, batchRenderCancelSignalHandler );
    }

    ~BatchRenderCancelScope()
    {
        if( m_previousInt  != SIG_ERR ) std::signal( SIGINT,  m_previousInt );
        if( m_previousTerm != SIG_ERR ) std::signal( SIGTERM, m_previousTerm );
    }

    BatchRenderCancelScope(const BatchRenderCancelScope &) = delete;
    BatchRenderCancelScope &operator=(const BatchRenderCancelScope &) = delete;

    static bool cancelled() { return g_batchRenderCancelRequested != 0; }

private:
    void (*m_previousInt)(int)  = SIG_ERR;
    void (*m_previousTerm)(int) = SIG_ERR;
};

/* -------- Output verification --------------------------------------------
 *
 * Verification runs on the ffmpeg binary the export ALREADY REQUIRES, not on ffprobe.
 * That is a deliberate choice, not a shortcut:
 *
 *   - ffprobe is NOT shipped with MLVApp. platform/qt/FFmpeg/ffmpegWin64.zip contains
 *     exactly one entry, ffmpeg.exe. Requiring ffprobe would make rendered export refuse
 *     on a stock install, which is a worse product than one that verifies with what it
 *     already has.
 *   - A full DECODE pass is a stronger playability claim than reading container metadata.
 *     ffprobe reading a plausible header does not prove the bitstream decodes; `-f null`
 *     decodes every packet and fails on a truncated or corrupt file.
 *   - The frame count and duration it reports are read back out of the encoded file, so
 *     they are independent of what the writer believed it wrote.
 *
 * ffprobe is still used when it happens to be resolvable, purely to ADD codec, container
 * and sample-aspect-ratio facts. Its absence downgrades the report, never the gate. */

struct DecodeProbeFacts
{
    bool ran = false;
    bool decoded = false;      /* every packet decoded, exit status 0, empty stderr */
    int frameCount = 0;
    double durationSeconds = 0.0;
    QString diagnostics;
};

/* `-progress pipe:1` emits stable key=value lines regardless of loglevel, which is why
 * this parses that stream rather than ffmpeg's human-readable stderr banner. */
DecodeProbeFacts decodeProbe(const QString &ffmpegExecutable, const QString &path)
{
    DecodeProbeFacts facts;
    QProcess process;
    process.start( ffmpegExecutable,
                   QStringList{ QStringLiteral("-hide_banner"),
                                QStringLiteral("-v"), QStringLiteral("error"),
                                QStringLiteral("-i"), path,
                                QStringLiteral("-progress"), QStringLiteral("pipe:1"),
                                QStringLiteral("-f"), QStringLiteral("null"),
                                QStringLiteral("-") } );
    if( !process.waitForStarted( 10000 ) || !process.waitForFinished( 600000 ) )
    {
        facts.diagnostics = QStringLiteral("decode probe did not run to completion");
        return facts;
    }
    facts.ran = true;

    const QString progress = QString::fromLocal8Bit( process.readAllStandardOutput() );
    const QString stderrText =
        QString::fromLocal8Bit( process.readAllStandardError() ).trimmed();

    const QStringList lines = progress.split( QLatin1Char('\n'), Qt::SkipEmptyParts );
    for( const QString &rawLine : lines )
    {
        const QString line = rawLine.trimmed();
        if( line.startsWith( QStringLiteral("frame=") ) )
        {
            /* progress reports cumulatively; the LAST value is the total. */
            facts.frameCount = line.mid( 6 ).trimmed().toInt();
        }
        else if( line.startsWith( QStringLiteral("out_time_us=") ) )
        {
            const qint64 micros = line.mid( 12 ).trimmed().toLongLong();
            if( micros > 0 )
                facts.durationSeconds = static_cast<double>( micros ) / 1000000.0;
        }
    }

    facts.decoded = process.exitStatus() == QProcess::NormalExit
                 && process.exitCode() == 0
                 && stderrText.isEmpty();
    facts.diagnostics = stderrText;
    return facts;
}

/* Decoded pixel dimensions, read back out of the file: decode exactly one frame to raw
 * rgb24 and count the bytes. Independent of any header field, and of what the writer
 * thought the geometry was. Returns false when the frame could not be decoded. */
bool decodedFrameDimensionsMatch(const QString &ffmpegExecutable,
                                 const QString &path,
                                 int expectedWidth,
                                 int expectedHeight,
                                 qint64 *actualBytes)
{
    QProcess process;
    process.start( ffmpegExecutable,
                   QStringList{ QStringLiteral("-hide_banner"),
                                QStringLiteral("-v"), QStringLiteral("error"),
                                QStringLiteral("-i"), path,
                                QStringLiteral("-frames:v"), QStringLiteral("1"),
                                QStringLiteral("-f"), QStringLiteral("rawvideo"),
                                QStringLiteral("-pix_fmt"), QStringLiteral("rgb24"),
                                QStringLiteral("-") } );
    if( !process.waitForStarted( 10000 ) || !process.waitForFinished( 120000 ) )
    {
        if( actualBytes ) *actualBytes = -1;
        return false;
    }

    const QByteArray raw = process.readAllStandardOutput();
    if( actualBytes ) *actualBytes = raw.size();
    if( process.exitCode() != 0 )
        return false;

    const qint64 expectedBytes = static_cast<qint64>(expectedWidth)
                               * static_cast<qint64>(expectedHeight) * 3;
    return raw.size() == expectedBytes;
}

struct MediaProbeFacts
{
    bool parsed = false;
    QString codecName;
    QString formatNames;   /* ffprobe reports a comma-separated list, e.g. "mov,mp4,..." */
    int width = 0;
    int height = 0;
    QString sampleAspectRatio;
    QString displayAspectRatio;
    /* Which probe produced these facts, so the RENDER_VERIFIED line never leaves an
     * operator guessing whether the aspect check was authoritative or derived. */
    QString source;
};

MediaProbeFacts mediaProbeFactsFromJson(const QByteArray &json)
{
    MediaProbeFacts facts;
    QJsonParseError err{};
    const QJsonDocument doc = QJsonDocument::fromJson( json, &err );
    if( err.error != QJsonParseError::NoError || !doc.isObject() )
        return facts;

    const QJsonObject root = doc.object();
    facts.formatNames = root.value( QStringLiteral("format") ).toObject()
                            .value( QStringLiteral("format_name") ).toString();

    const QJsonArray streams = root.value( QStringLiteral("streams") ).toArray();
    for( const QJsonValue &value : streams )
    {
        const QJsonObject stream = value.toObject();
        if( stream.value( QStringLiteral("codec_type") ).toString()
                != QStringLiteral("video") )
            continue;

        facts.codecName = stream.value( QStringLiteral("codec_name") ).toString();
        facts.width  = stream.value( QStringLiteral("width") ).toInt();
        facts.height = stream.value( QStringLiteral("height") ).toInt();
        facts.sampleAspectRatio =
            stream.value( QStringLiteral("sample_aspect_ratio") ).toString();
        facts.displayAspectRatio =
            stream.value( QStringLiteral("display_aspect_ratio") ).toString();
        break; /* first video stream is the one we wrote */
    }

    facts.parsed = true;
    facts.source = QStringLiteral("ffprobe-json");
    return facts;
}

/* Same facts, read from `ffmpeg -i <file>` instead of ffprobe.
 *
 * The PARSING lives in BatchRenderedVideoPlan.cpp (batchRenderedVideoFfmpegDumpFacts) so the console
 * tests can pin the regexes without linking this GUI-dependent translation unit; this
 * wrapper only adapts the result into MediaProbeFacts and stamps the probe source.
 *
 * `ffmpeg -i <file>` with no output file ALWAYS exits non-zero ("At least one output file
 * must be specified") AFTER printing the stream dump, so the caller must read stderr
 * regardless of exit status -- gating on exitCode()==0 would discard every result. */
MediaProbeFacts mediaProbeFactsFromFfmpegDump(const QByteArray &dump)
{
    const BatchRenderedVideoFfmpegDumpFacts parsed =
        batchRenderedVideoFfmpegDumpFacts( QString::fromUtf8( dump ) );

    MediaProbeFacts facts;
    if( !parsed.parsed )
        return facts;

    facts.parsed             = true;
    facts.codecName          = parsed.codecName;
    facts.formatNames        = parsed.formatNames;
    facts.width              = parsed.width;
    facts.height             = parsed.height;
    facts.sampleAspectRatio  = parsed.sampleAspectRatio;
    facts.displayAspectRatio = parsed.displayAspectRatio;
    facts.source             = QStringLiteral("ffmpeg-dump");
    return facts;
}

/* Optional enrichment only. Uses the plan's hit (application directory, then PATH), then
 * looks beside the resolved ffmpeg (the two ship together in every standard distribution).
 * Empty string means "not available", and the caller degrades its REPORT, never its verdict. */
QString resolveOptionalMediaProbe(
    const BatchRenderedVideoMediaProbeBinaryPlan &probePlan,
    const BatchRenderedVideoFfmpegBinaryPlan &ffmpegPlan)
{
    if( ( probePlan.foundInAppDir || probePlan.foundOnPath )
     && !probePlan.resolvedExecutable.isEmpty() )
        return probePlan.resolvedExecutable;

    const QString siblingDir = QFileInfo( ffmpegPlan.resolvedExecutable ).absolutePath();
    if( siblingDir.isEmpty() )
        return QString();

    for( const QString &candidate : QStringList{ siblingDir + QStringLiteral("/ffprobe.exe"),
                                                 siblingDir + QStringLiteral("/ffprobe") } )
    {
        const QFileInfo info( candidate );
        if( info.exists() && info.isFile() )
            return QDir::cleanPath( info.absoluteFilePath() );
    }
    return QString();
}

} /* anonymous namespace */

int BatchRunner::exportRenderedVideoFile(
    const QString &mlvPath,
    ReceiptSettings *receipt,
    const BatchRenderedVideoJobPlan &preflightPlan,
    const BatchRenderedVideoRenderSettings &renderSettings)
{
    QElapsedTimer clipTimer;
    clipTimer.start();

    const QString baseName = QFileInfo(mlvPath).completeBaseName();

    /* ---- Open the clip ------------------------------------------------- */
    int mlvErr = MLV_ERR_NONE;
    char mlvErrMsg[256] = { 0 };

#ifdef Q_OS_UNIX
    mlvObject_t *mlvObject = initMlvObjectWithClip(
        mlvPath.toUtf8().data(), MLV_OPEN_FULL, &mlvErr, mlvErrMsg );
#else
    mlvObject_t *mlvObject = initMlvObjectWithClip(
        mlvPath.toLatin1().data(), MLV_OPEN_FULL, &mlvErr, mlvErrMsg );
#endif

    if( mlvErr )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: Cannot open MLV for rendered export: %1. %2\n")
            .arg(mlvPath, QString(mlvErrMsg)));
        if( mlvObject ) freeMlvObject( mlvObject );
        return 3;
    }

    processingObject_t *processingObject = initProcessingObject();
    setMlvProcessing( mlvObject, processingObject );
    disableMlvCaching( mlvObject );
    setMlvCpuCores( mlvObject, mlvappEffectiveWorkerThreadCount() );

    const uint32_t totalFrames = getMlvFrames( mlvObject );
    BatchLogger::out(QStringLiteral("[BATCH] FILE %1 frames=%2\n")
        .arg( baseName ).arg( totalFrames ));

    /* ---- Frame range, identical rules to the CDNG runner ---------------- */
    uint32_t cutIn  = receipt->cutIn();
    uint32_t cutOut = receipt->cutOut();
    if( cutIn == 0 )  cutIn  = 1;
    if( cutOut == 0 || cutOut > totalFrames ) cutOut = totalFrames;

    if( cutIn > totalFrames || cutIn > cutOut )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: Receipt cut-in %1 is past clip end (frames=%2, cut-out=%3)\n")
            .arg( cutIn ).arg( totalFrames ).arg( cutOut ));
        freeMlvObject( mlvObject );
        freeProcessingObject( processingObject );
        return 4;
    }

    const uint32_t unclampedCutOut = cutOut;
    cutOut = CdngSequenceExport::cutOutClampedForMaxFrames(
        cutIn, cutOut, BatchContext::maxFrames() );
    if( cutOut != unclampedCutOut )
    {
        BatchLogger::out(QStringLiteral("[BATCH] MAX_FRAMES %1 max=%2 cutIn=%3 cutOut=%4 originalCutOut=%5\n")
            .arg( baseName ).arg( BatchContext::maxFrames() )
            .arg( cutIn ).arg( cutOut ).arg( unclampedCutOut ));
    }

    /* ---- Receipt -> pipeline, the same call the CDNG runner makes ------- */
    ReceiptApplier::applyToMlv( receipt, mlvObject, processingObject );

    /* Debayer. ReceiptApplier does not set it because CDNG writes Bayer data and never
     * debayers; a rendered export does, so this mirrors the receipt-driven branch of
     * MainWindow::startExportPipe verbatim. (The GUI's m_exportDebayerMode override has
     * no CLI surface, so the receipt IS the headless authority.) */
    switch( receipt->debayer() )
    {
        case ReceiptSettings::None:     setMlvUseNoneDebayer( mlvObject );      break;
        case ReceiptSettings::Simple:   setMlvUseSimpleDebayer( mlvObject );    break;
        case ReceiptSettings::Bilinear: setMlvDontAlwaysUseAmaze( mlvObject );  break;
        case ReceiptSettings::LMMSE:    setMlvUseLmmseDebayer( mlvObject );     break;
        case ReceiptSettings::IGV:      setMlvUseIgvDebayer( mlvObject );       break;
        case ReceiptSettings::AMaZE:    setMlvAlwaysUseAmaze( mlvObject );      break;
        case ReceiptSettings::AHD:      setMlvUseAhdDebayer( mlvObject );       break;
        case ReceiptSettings::RCD:      setMlvUseRcdDebayer( mlvObject );       break;
        case ReceiptSettings::DCB:      setMlvUseDcbDebayer( mlvObject );       break;
        default:                                                                break;
    }

    ReceiptApplier::printFingerprint( mlvObject, processingObject );

    /* ---- Complete the plan against the clip's real geometry ------------- */
    double stretchX = STRETCH_H_100;
    double stretchY = STRETCH_V_100;
    CdngSequenceExport::effectiveStretchFactors( receipt->stretchFactorX(),
                                          receipt->stretchFactorY(),
                                          getMlvAspectRatio( mlvObject ),
                                          &stretchX, &stretchY );

    const BatchRenderedVideoSourceMetadata metadata =
        CdngSequenceExport::renderedVideoSourceMetadataFromClipState(
            static_cast<int>(getMlvWidth( mlvObject )),
            static_cast<int>(getMlvHeight( mlvObject )),
            getMlvFramerate( mlvObject ),
            stretchX,
            stretchY,
            static_cast<int>(totalFrames) );

    const BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        renderedVideoSourceAudioPlanFromOpenMlv( mlvObject, mlvPath );
    const BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanWithSourceAudio( preflightPlan, sourceAudioPlan ),
            metadata,
            renderSettings );

    if( !plan.runnable )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner rendered-video job is not runnable. clip=%1 %2.\n")
            .arg(baseName)
            .arg(batchRenderedVideoJobPlanSummary(plan)));
        freeMlvObject( mlvObject );
        freeProcessingObject( processingObject );
        return 2;
    }

    if( !plan.runnerPrerequisites.limitation.isEmpty()
     && sourceAudioPlan.sourceAudioPresent )
    {
        BatchLogger::out(QStringLiteral("[BATCH] RENDER_AUDIO_DROPPED %1 reason=%2\n")
            .arg(baseName)
            .arg(plan.runnerPrerequisites.limitation));
    }

    /* ---- Atomic write target -------------------------------------------- */
    const QString finalPath = plan.outputPlan.outputPath;
    const QString partialPath = batchRenderedVideoPartialOutputPath( finalPath );
    const QString arguments =
        batchRenderedVideoFfmpegArgumentsWithOutputPath( plan.ffmpegCommandPlan,
                                                         partialPath );
    if( partialPath.isEmpty() || arguments.isEmpty() )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner could not build the rendered-video ffmpeg invocation. clip=%1 output=%2.\n")
            .arg(baseName, finalPath));
        freeMlvObject( mlvObject );
        freeProcessingObject( processingObject );
        return 2;
    }

    QDir().mkpath( QFileInfo( finalPath ).absolutePath() );
    QFile::remove( partialPath ); /* a previous cancelled run may have left one */

    const uint32_t framesToExport = cutOut - cutIn + 1;
    const int srcWidth  = static_cast<int>(getMlvWidth( mlvObject ));
    const int srcHeight = static_cast<int>(getMlvHeight( mlvObject ));
    const int outWidth  = plan.ffmpegFramePlan.outputWidth;
    const int outHeight = plan.ffmpegFramePlan.outputHeight;
    const bool scaled   = plan.ffmpegFramePlan.scaled;

    BatchLogger::out(QStringLiteral("[BATCH] RENDER_START %1 codec=%2 container=%3 source=%4x%5 output=%6x%7 scaled=%8 fps=%9 frames=%10 cutIn=%11 cutOut=%12 out=%13\n")
        .arg(baseName)
        .arg(plan.outputVerificationPlan.expectedCodec)
        .arg(plan.outputVerificationPlan.expectedContainer)
        .arg(srcWidth).arg(srcHeight)
        .arg(outWidth).arg(outHeight)
        .arg(scaled ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.ffmpegFramePlan.frameRateArgument)
        .arg(framesToExport).arg(cutIn).arg(cutOut)
        .arg(finalPath));
    if( BatchContext::isVerbose() )
    {
        BatchLogger::out(QStringLiteral("[BATCH] RENDER_COMMAND %1 %2\n")
            .arg(batchRenderedVideoCommandExecutableForDisplay(
                     plan.ffmpegCommandPlan.executable))
            .arg(arguments));
    }

    /* ---- Encode ---------------------------------------------------------- */
    BatchRenderCancelScope cancelScope;

    export_process::StreamingPipeline pipeline;
    const export_process::Invocation invocation =
        export_process::invocationFromTemplate( plan.ffmpegCommandPlan.executable,
                                                arguments );

    if( !pipeline.start( QVector<export_process::Invocation>{ invocation } ) )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner could not start ffmpeg. clip=%1 executable=%2. %3\n")
            .arg(baseName, plan.ffmpegCommandPlan.executable,
                 pipeline.diagnostics().trimmed()));
        freeMlvObject( mlvObject );
        freeProcessingObject( processingObject );
        return 4;
    }

    const size_t sourcePixels = static_cast<size_t>(srcWidth)
                              * static_cast<size_t>(srcHeight) * 3u;
    const size_t scaledPixels = static_cast<size_t>(outWidth)
                              * static_cast<size_t>(outHeight) * 3u;

    uint16_t *frameBuffer =
        static_cast<uint16_t *>( malloc( sourcePixels * sizeof(uint16_t) ) );
    uint16_t *scaledBuffer = scaled
        ? static_cast<uint16_t *>( malloc( scaledPixels * sizeof(uint16_t) ) )
        : nullptr;

    if( frameBuffer == nullptr || ( scaled && scaledBuffer == nullptr ) )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner could not allocate rendered-video frame buffers. clip=%1 source=%2x%3 output=%4x%5.\n")
            .arg(baseName).arg(srcWidth).arg(srcHeight).arg(outWidth).arg(outHeight));
        pipeline.cancel();
        free( frameBuffer );
        free( scaledBuffer );
        QFile::remove( partialPath );
        freeMlvObject( mlvObject );
        freeProcessingObject( processingObject );
        return 4;
    }

    const int workerThreads = mlvappEffectiveWorkerThreadCount();
    bool writeFailed = false;
    uint32_t framesWritten = 0;

    /* Same iteration the GUI runs (MainWindow::startExportPipe): 0-based frame indices
     * over the 1-based receipt cut range. */
    for( uint32_t frameIndex = cutIn - 1; frameIndex < cutOut; frameIndex++ )
    {
        if( BatchRenderCancelScope::cancelled() ) break;

        getMlvProcessedFrame16( mlvObject, frameIndex, frameBuffer, workerThreads );

        if( scaled )
        {
            avir_scale_thread_pool scaling_pool;
            avir::CImageResizerVars vars; vars.ThreadPool = &scaling_pool;
            avir::CImageResizerParamsUltra roptions;
            avir::CImageResizer<> image_resizer( 16, 0, roptions );
            image_resizer.resizeImage( frameBuffer,
                                       srcWidth, srcHeight, 0,
                                       scaledBuffer,
                                       outWidth, outHeight,
                                       3, 0, &vars );
        }

        const uint16_t *pixels = scaled ? scaledBuffer : frameBuffer;
        const size_t pixelCount = scaled ? scaledPixels : sourcePixels;
        if( !pipeline.writeAll( reinterpret_cast<const char *>( pixels ),
                                static_cast<qint64>( pixelCount * sizeof(uint16_t) ) ) )
        {
            writeFailed = true;
            break;
        }

        framesWritten++;

        /* Progress on a bounded cadence so a long clip stays legible in a log file
         * without emitting one line per frame. */
        if( BatchContext::isVerbose()
         || framesWritten == 1
         || framesWritten == framesToExport
         || ( framesWritten % 25 ) == 0 )
        {
            BatchLogger::out(QStringLiteral("[BATCH] RENDER_PROGRESS %1 frames=%2/%3 elapsed=%4\n")
                .arg(baseName).arg(framesWritten).arg(framesToExport)
                .arg(clipTimer.elapsed() / 1000.0, 0, 'f', 1));
        }
    }

    const bool cancelled = BatchRenderCancelScope::cancelled();
    bool encoded = false;
    if( cancelled || writeFailed )
        pipeline.cancel();
    else
        encoded = pipeline.finish();

    const QString diagnostics = pipeline.diagnostics().trimmed();

    free( frameBuffer );
    free( scaledBuffer );
    freeMlvObject( mlvObject );
    freeProcessingObject( processingObject );

    if( cancelled )
    {
        QFile::remove( partialPath );
        BatchLogger::err(QStringLiteral("[BATCH] RENDER_CANCELLED %1 frames=%2/%3 partial_removed=%4\n")
            .arg(baseName).arg(framesWritten).arg(framesToExport).arg(partialPath));
        return 4;
    }

    if( writeFailed || !encoded )
    {
        QFile::remove( partialPath );
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner rendered-video encode failed. clip=%1 frames=%2/%3 write_failed=%4 ffmpeg=%5\n")
            .arg(baseName).arg(framesWritten).arg(framesToExport)
            .arg(writeFailed ? QStringLiteral("true") : QStringLiteral("false"))
            .arg(diagnostics.isEmpty() ? QStringLiteral("(no diagnostics)") : diagnostics));
        return 4;
    }

    if( framesWritten != framesToExport )
    {
        QFile::remove( partialPath );
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner rendered-video wrote %1 of %2 frames. clip=%3\n")
            .arg(framesWritten).arg(framesToExport).arg(baseName));
        return 4;
    }

    /* ---- Verify, BEFORE publishing --------------------------------------- */
    /* [B1], LANE-4 review of e9226e1c..51f28e8e: verification used to run AFTER the
     * rename, so a file that FAILED its own checks was left sitting at the path the
     * caller was told to expect -- and because publishing removes any pre-existing
     * output first, a failed RE-RUN destroyed a good file and left the bad one in its
     * place. Five of the six failure paths in this function already removed their
     * artefact; the verification path was the one that did not.
     *
     * Verifying the partial file costs nothing: decodeProbe, decodedFrameDimensionsMatch,
     * the `ffmpeg -i` dump and QFileInfo::size() all take a path, and the partial marker
     * sits BEFORE the extension (`clip.mlvapp-partial.mp4`), so ffmpeg selects the same
     * demuxer it would for the final name. The publish below is then reached only by an
     * output that has passed every check. */
    const QString ffmpegExecutable = plan.ffmpegCommandPlan.executable;
    QStringList failures;

    const qint64 outputBytes = QFileInfo( partialPath ).size();
    if( outputBytes < static_cast<qint64>( plan.outputVerificationPlan.nonEmptyMinimumBytes ) )
        failures << QStringLiteral("output-bytes=%1").arg(outputBytes);

    /* (a) Playability, frame count and duration, read back by decoding the file. */
    const DecodeProbeFacts decoded = decodeProbe( ffmpegExecutable, partialPath );
    if( !decoded.ran )
    {
        failures << QStringLiteral("decode-probe did-not-run (%1)").arg(decoded.diagnostics);
    }
    else
    {
        if( !decoded.decoded )
        {
            failures << QStringLiteral("decode-probe failed: %1")
                .arg(decoded.diagnostics.isEmpty()
                        ? QStringLiteral("(no diagnostics)") : decoded.diagnostics);
        }
        if( decoded.frameCount != static_cast<int>(framesToExport) )
        {
            failures << QStringLiteral("frame-count expected=%1 decoded=%2")
                .arg(framesToExport).arg(decoded.frameCount);
        }

        const double expectedDuration =
            metadata.frameRate > 0.0
                ? static_cast<double>(framesToExport) / metadata.frameRate
                : 0.0;
        /* One frame of tolerance: container timebases round, and a single-frame
         * difference is a representation artefact, not a lost or duplicated picture --
         * the frame-count check above is what actually guards content. */
        const double durationTolerance =
            metadata.frameRate > 0.0 ? ( 1.0 / metadata.frameRate ) : 0.5;
        if( expectedDuration > 0.0
         && qAbs( decoded.durationSeconds - expectedDuration ) > durationTolerance )
        {
            failures << QStringLiteral("duration expected=%1s decoded=%2s tolerance=%3s")
                .arg(expectedDuration, 0, 'f', 3)
                .arg(decoded.durationSeconds, 0, 'f', 3)
                .arg(durationTolerance, 0, 'f', 3);
        }
    }

    /* (b) Geometry, read back from a decoded frame rather than from a header field.
     * The frame plan bakes the receipt's stretch into the pixel dimensions and emits
     * square pixels, so a correct decoded frame size IS the aspect check. */
    qint64 decodedFrameBytes = 0;
    if( !decodedFrameDimensionsMatch( ffmpegExecutable, partialPath,
                                      outWidth, outHeight, &decodedFrameBytes ) )
    {
        failures << QStringLiteral("decoded-frame-bytes expected=%1 (%2x%3 rgb24) actual=%4")
            .arg(static_cast<qint64>(outWidth) * outHeight * 3)
            .arg(outWidth).arg(outHeight).arg(decodedFrameBytes);
    }

    /* (c) Codec / container / aspect. ffprobe is preferred when it is resolvable because
     * its JSON is unambiguous, but it is NOT shipped with MLVApp, so the ffmpeg stream
     * dump is the fallback that makes this check actually run on a stock install rather
     * than degrade to `unchecked`. Either way the facts are advisory about WHICH probe
     * spoke and binding about what it said. */
    QString probeExecutable =
        resolveOptionalMediaProbe( plan.mediaProbeBinaryPlan, plan.ffmpegBinaryPlan );
    MediaProbeFacts facts;
    /* [B2]: the plan bakes the FINAL path into mediaProbeCommandPlan.arguments, so running
     * that string verbatim would probe the previous export on any re-run. Re-aim it at the
     * file actually under verification. An empty result means the probe plan is not ready,
     * in which case this branch is skipped entirely and the ffmpeg-dump fallback below
     * still checks the right file. */
    const QString probeArguments =
        batchRenderedVideoMediaProbeArgumentsWithPath( plan.mediaProbeCommandPlan,
                                                       partialPath );
    if( !probeExecutable.isEmpty() && !probeArguments.isEmpty() )
    {
        QProcess probe;
        probe.start( probeExecutable, QProcess::splitCommand( probeArguments ) );
        if( probe.waitForStarted( 10000 ) && probe.waitForFinished( 60000 )
         && probe.exitCode() == 0 )
        {
            facts = mediaProbeFactsFromJson( probe.readAllStandardOutput() );
        }
    }
    if( !facts.parsed && !ffmpegExecutable.isEmpty() )
    {
        QProcess dump;
        dump.start( ffmpegExecutable,
                    QStringList{ QStringLiteral("-hide_banner"),
                                 QStringLiteral("-i"), partialPath } );
        /* Deliberately NOT gated on exit status: `ffmpeg -i` with no output file always
         * exits non-zero after printing the dump this parses. */
        if( dump.waitForStarted( 10000 ) && dump.waitForFinished( 60000 ) )
        {
            facts = mediaProbeFactsFromFfmpegDump( dump.readAllStandardError() );
            if( facts.parsed )
                probeExecutable = ffmpegExecutable;
        }
    }
    if( facts.parsed )
    {
        if( facts.codecName != plan.outputVerificationPlan.expectedCodec )
        {
            failures << QStringLiteral("codec expected=%1 actual=%2")
                .arg(plan.outputVerificationPlan.expectedCodec, facts.codecName);
        }
        if( !facts.formatNames.split( QLatin1Char(',') )
                 .contains( plan.outputVerificationPlan.expectedContainer ) )
        {
            failures << QStringLiteral("container expected=%1 actual=%2")
                .arg(plan.outputVerificationPlan.expectedContainer, facts.formatNames);
        }
        if( facts.width != outWidth || facts.height != outHeight )
        {
            failures << QStringLiteral("coded-dimensions expected=%1x%2 actual=%3x%4")
                .arg(outWidth).arg(outHeight).arg(facts.width).arg(facts.height);
        }
        /* A non-1:1 SAR would silently re-stretch the image on playback, undoing the
         * receipt's stretch. This is exactly the aspect defect the check exists for.
         * The square-pixel spellings live in BatchRenderedVideoPlan.cpp so the product check and the
         * console test share ONE definition and cannot drift apart. */
        if( !batchRenderedVideoSampleAspectIsSquare( facts.sampleAspectRatio ) )
        {
            failures << QStringLiteral("sample-aspect-ratio expected=1:1 actual=%1")
                .arg(facts.sampleAspectRatio);
        }
    }

    if( !failures.isEmpty() )
    {
        /* [B1]: discard the artefact instead of publishing it. Nothing is written to
         * finalPath on this path, so a pre-existing good output at that path also
         * SURVIVES a failed re-run -- which the previous publish-then-verify order
         * destroyed before it knew whether the replacement was sound. */
        QFile::remove( partialPath );
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner rendered-video output verification failed. clip=%1 out=%2 failures=[%3]\n")
            .arg(baseName, finalPath, failures.join( QStringLiteral("; ") )));
        return 4;
    }

    /* ---- Publish atomically, now that the output has passed every check ---- */
    QFile::remove( finalPath ); /* rename() will not overwrite on Windows */
    if( !QFile::rename( partialPath, finalPath ) )
    {
        QFile::remove( partialPath );
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner could not publish the rendered-video output. clip=%1 from=%2 to=%3\n")
            .arg(baseName, partialPath, finalPath));
        return 4;
    }

    BatchLogger::out(QStringLiteral("[BATCH] RENDER_VERIFIED %1 decoded_frames=%2 decoded_duration=%3 decoded_frame_bytes=%4 dimensions=%5x%6 bytes=%7 container_probe=%8 probe_source=%13 codec=%9 container=%10 sar=%11 dar=%12\n")
        .arg(baseName)
        .arg(decoded.frameCount)
        .arg(decoded.durationSeconds, 0, 'f', 3)
        .arg(decodedFrameBytes)
        .arg(outWidth).arg(outHeight)
        .arg(outputBytes)
        .arg(facts.parsed ? probeExecutable : QStringLiteral("unavailable"))
        .arg(facts.parsed ? facts.codecName : QStringLiteral("unchecked"))
        .arg(facts.parsed ? facts.formatNames : QStringLiteral("unchecked"))
        .arg(facts.parsed
                ? ( facts.sampleAspectRatio.isEmpty() ? QStringLiteral("unset") : facts.sampleAspectRatio )
                : QStringLiteral("unchecked"))
        .arg(facts.parsed
                ? ( facts.displayAspectRatio.isEmpty() ? QStringLiteral("unset") : facts.displayAspectRatio )
                : QStringLiteral("unchecked"))
        .arg(facts.parsed ? facts.source : QStringLiteral("none")));

    BatchLogger::out(QStringLiteral("[BATCH] DONE %1 exported=%2 skipped=0 elapsed=%3\n")
        .arg(baseName).arg(framesWritten)
        .arg(clipTimer.elapsed() / 1000.0, 0, 'f', 1));

    return 0;
}

int BatchRunner::run(const QString &inputPath, const QString &outputPath)
{
    QElapsedTimer totalTimer;
    totalTimer.start();

    const BatchExportFormatRequest exportRequest =
        BatchContext::exportFormatRequest();
    const BatchRenderedVideoRenderSettings renderSettings =
        BatchContext::renderedVideoRenderSettings();
    const QString requestedFfmpegExecutable =
        BatchContext::renderedVideoFfmpegExecutable();
    const bool renderedVideoRequested =
        exportRequest.format == BatchExportFormat::RenderedVideo;
    const BatchRenderedVideoFfmpegBinaryPlan ffmpegBinaryPlan =
        renderedVideoRequested
            ? batchRenderedVideoFfmpegBinaryPlanFromCurrentEnvironment(
                requestedFfmpegExecutable)
            : BatchRenderedVideoFfmpegBinaryPlan();
    const BatchRenderedVideoMediaProbeBinaryPlan mediaProbeBinaryPlan =
        renderedVideoRequested
            ? batchRenderedVideoMediaProbeBinaryPlanFromCurrentEnvironment()
            : BatchRenderedVideoMediaProbeBinaryPlan();
    if( exportRequest.format == BatchExportFormat::RenderedVideo )
    {
        const BatchRenderedVideoJobPlan renderedPlan =
            batchRenderedVideoJobPlanFromRequest(
                inputPath,
                outputPath,
                exportRequest,
                renderSettings,
                ffmpegBinaryPlan,
                mediaProbeBinaryPlan);
        if( !renderedPlan.requestValid )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner invalid rendered-video request. %1. %2.\n")
                .arg(batchExportFormatRequestSummary(renderedPlan.request))
                .arg(batchRenderedVideoRequestShapeError(renderedPlan.request)));
            return 2;
        }
        if( !renderedPlan.targetReady )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner rendered-video target is incomplete. %1. %2. Choose a rendered codec or container before runner work.\n")
                .arg(batchExportFormatRequestSummary(renderedPlan.request))
                .arg(batchRenderedVideoTargetSummary(renderedPlan.target)));
            return 2;
        }
        if( !renderedPlan.encoderReady )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner rendered-video encoder preset is unavailable. %1. %2. %3.\n")
                .arg(batchExportFormatRequestSummary(renderedPlan.request))
                .arg(batchRenderedVideoTargetSummary(renderedPlan.target))
                .arg(batchRenderedVideoEncoderPresetSummary(renderedPlan.encoderPreset)));
            return 2;
        }
        if( !renderedPlan.outputReady )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner rendered-video output path is invalid. %1. %2. %3. Choose an output directory or an explicit rendered file path ending in %4.\n")
                .arg(batchExportFormatRequestSummary(renderedPlan.request))
                .arg(batchRenderedVideoTargetSummary(renderedPlan.target))
                .arg(batchRenderedVideoOutputPlanSummary(renderedPlan.outputPlan))
                .arg(renderedPlan.target.extension));
            return 2;
        }
    }
    if( !renderedVideoRequested && exportRequest.format != BatchExportFormat::Cdng )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner unsupported export format. %1. Supported now: cdng.\n")
            .arg(batchExportFormatRequestSummary(exportRequest)));
        return 2;
    }

    /* --- Receipt resolution (4-way priority) ---
     * 1. --receipt <file>       → explicit, exit 5 on failure
     * 2. --default-receipt      → GUI default, exit 5 if not configured/missing
     * 3. auto-detect            → GUI default if enabled in QSettings, warn on missing
     * 4. none                   → use defaults */
    QString receiptPath = BatchContext::receiptPath();
    bool useDefault     = BatchContext::useDefaultReceipt();
    ReceiptSettings receipt;  /* default-constructed */

    if( !receiptPath.isEmpty() )
    {
        /* Priority 1: explicit --receipt <file> (wins over --default-receipt) */
        QString errMsg;
        if( !ReceiptLoader::loadFromFile(receiptPath, &receipt, &errMsg) )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: %1\n").arg(errMsg));
            return 5;
        }
        BatchLogger::out(QStringLiteral("[BATCH] RECEIPT source=explicit path=%1\n").arg(receiptPath));
        ReceiptLoader::printCdngSettings(&receipt);
    }
    else if( useDefault )
    {
        /* Priority 2: --default-receipt flag — read GUI's QSettings */
        QSettings set( QSettings::UserScope,
                       QStringLiteral("magiclantern.MLVApp"),
                       QStringLiteral("MLVApp") );
        QString defaultPath = set.value( QStringLiteral("defaultReceiptFileName"),
                                         QDir::homePath() ).toString();
        bool defaultEnabled = set.value( QStringLiteral("defaultReceiptEnabled"),
                                         false ).toBool();

        if( !defaultEnabled || defaultPath.isEmpty() || defaultPath == QDir::homePath() )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: --default-receipt requested but no default receipt configured in GUI\n"));
            return 5;
        }

        QString errMsg;
        if( !ReceiptLoader::loadFromFile(defaultPath, &receipt, &errMsg) )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: %1\n").arg(errMsg));
            return 5;
        }
        BatchLogger::out(QStringLiteral("[BATCH] RECEIPT source=default path=%1\n").arg(defaultPath));
        ReceiptLoader::printCdngSettings(&receipt);
    }
    else
    {
        /* Priority 3: auto-detect — check GUI QSettings silently */
        QSettings set( QSettings::UserScope,
                       QStringLiteral("magiclantern.MLVApp"),
                       QStringLiteral("MLVApp") );
        bool defaultEnabled = set.value( QStringLiteral("defaultReceiptEnabled"),
                                         false ).toBool();

        if( defaultEnabled )
        {
            QString defaultPath = set.value( QStringLiteral("defaultReceiptFileName"),
                                             QDir::homePath() ).toString();

            if( !defaultPath.isEmpty() && defaultPath != QDir::homePath()
                && QFileInfo::exists(defaultPath) )
            {
                QString errMsg;
                if( ReceiptLoader::loadFromFile(defaultPath, &receipt, &errMsg) )
                {
                    BatchLogger::out(QStringLiteral("[BATCH] RECEIPT source=auto-default path=%1\n").arg(defaultPath));
                    ReceiptLoader::printCdngSettings(&receipt);
                }
                else
                {
                    BatchLogger::err(QStringLiteral("[BATCH] WARNING default receipt failed to parse: %1 (using defaults)\n").arg(errMsg));
                    BatchLogger::out(QStringLiteral("[BATCH] RECEIPT source=none (using defaults)\n"));
                }
            }
            else
            {
                BatchLogger::err(QStringLiteral("[BATCH] WARNING default receipt missing: %1 (using defaults)\n").arg(defaultPath));
                BatchLogger::out(QStringLiteral("[BATCH] RECEIPT source=none (using defaults)\n"));
            }
        }
        else
        {
            /* Priority 4: no receipt at all */
            BatchLogger::out(QStringLiteral("[BATCH] RECEIPT source=none (using defaults)\n"));
        }
    }

    /* Collect list of .mlv files to process */
    QStringList mlvFiles;
    QFileInfo inputInfo(inputPath);

    if( inputInfo.isFile() )
    {
        if( inputPath.endsWith( QStringLiteral(".mlv"), Qt::CaseInsensitive ) )
            mlvFiles << inputPath;
        else
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: Input is not an MLV file: %1\n").arg(inputPath));
            return 3;
        }
    }
    else if( inputInfo.isDir() )
    {
        QDir dir(inputPath);
        QStringList filters;
        filters << QStringLiteral("*.mlv") << QStringLiteral("*.MLV");
        QFileInfoList entries = dir.entryInfoList( filters, QDir::Files, QDir::Name );
        for( const QFileInfo &fi : entries )
            mlvFiles << fi.absoluteFilePath();

        if( mlvFiles.isEmpty() )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: No MLV files found in: %1\n").arg(inputPath));
            return 3;
        }
    }
    else
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: Input path does not exist: %1\n").arg(inputPath));
        return 3;
    }

    if( renderedVideoRequested )
    {
        const QString mlvPath = mlvFiles.first();
        const QString baseName = QFileInfo(mlvPath).completeBaseName();
        const BatchRenderedVideoJobPlan basePlan =
            batchRenderedVideoJobPlanFromRequest(
                mlvPath,
                outputPath,
                exportRequest,
                mlvFiles.size(),
                renderSettings,
                ffmpegBinaryPlan,
                mediaProbeBinaryPlan);

        if( !basePlan.preflightReady )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner rendered-video input/output preflight failed. clip=%1 planned-clips=%2 %3.\n")
                .arg(baseName)
                .arg(mlvFiles.size())
                .arg(batchRenderedVideoJobPlanSummary(basePlan)));
            return 2;
        }

        /* The binary plan treats a non-empty NAME as command-ready even when nothing was
         * found, so an --rendered-ffmpeg typo would otherwise survive preflight and only
         * surface as a failed spawn (exit 4, "export failure"). It is a usage error:
         * report it as one, name what was searched, and exit 2 before touching the clip. */
        if( !batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight( ffmpegBinaryPlan ) )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: rendered-video export cannot find ffmpeg. requested=%1 resolved=%2 searched=%3. Pass --rendered-ffmpeg <path-to-ffmpeg> or put ffmpeg on PATH.\n")
                .arg(ffmpegBinaryPlan.requestedExecutable)
                .arg(ffmpegBinaryPlan.resolvedExecutable)
                .arg(ffmpegBinaryPlan.appDirSearchAttempted
                    ? QStringLiteral("app-dir,PATH")
                    : QStringLiteral("PATH")));
            return 2;
        }

        /* E4-1 is deliberately ONE clip to ONE output path. A folder of clips would need
         * per-clip output naming, a queue and restart semantics, all of which the roadmap
         * defers to E4-2 -- so refuse rather than silently exporting only the first. */
        if( mlvFiles.size() != 1 )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: BatchRunner rendered-video export takes exactly one clip. found=%1 input=%2. Point --input at a single .mlv file.\n")
                .arg(mlvFiles.size()).arg(inputPath));
            return 2;
        }

        BatchLogger::out(QStringLiteral("[BATCH] START input=%1 output=%2 export-format=%3 rendered-codec=%4 rendered-container=%5 ffmpeg=%6\n")
                   .arg( inputPath )
                   .arg( basePlan.outputPlan.outputPath )
                   .arg( batchExportFormatName( exportRequest.format ) )
                   .arg( batchRenderedVideoCodecName( exportRequest.renderedCodec ) )
                   .arg( batchRenderedVideoContainerName( exportRequest.renderedContainer ) )
                   .arg( ffmpegBinaryPlan.resolvedExecutable ));

        const int renderedExit = BatchRunner::exportRenderedVideoFile(
            mlvPath, &receipt, basePlan, renderSettings );

        const double totalElapsedRendered = totalTimer.elapsed() / 1000.0;
        BatchLogger::out(QStringLiteral("[BATCH] COMPLETE files=1 succeeded=%1 failed=%2 total_elapsed=%3\n")
                   .arg( renderedExit == 0 ? 1 : 0 )
                   .arg( renderedExit == 0 ? 0 : 1 )
                   .arg( totalElapsedRendered, 0, 'f', 1 ));
        return renderedExit;
    }

    /* Ensure output directory exists */
    QDir outDir(outputPath);
    if( !outDir.exists() )
    {
        if( !outDir.mkpath( QStringLiteral(".") ) )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: Cannot create output directory: %1\n").arg(outputPath));
            return 3;
        }
    }

    const uint32_t maxFrames = BatchContext::maxFrames();
    const int cdngCodecOffset =
        normalizedBatchCdngCodecOffset( BatchContext::cdngCodecOffset() );
    BatchLogger::out(QStringLiteral("[BATCH] START input=%1 output=%2 skip-errors=%3 resume=%4 max-frames=%5 cdng-codec=%6 export-format=%7 rendered-codec=%8 rendered-container=%9\n")
               .arg( inputPath )
               .arg( outputPath )
               .arg( BatchContext::skipErrors() ? QStringLiteral("true")
                                                 : QStringLiteral("false") )
               .arg( BatchContext::resumeEnabled() ? QStringLiteral("true")
                                                    : QStringLiteral("false") )
               .arg( maxFrames > 0 ? QString::number( maxFrames )
                                    : QStringLiteral("all") )
               .arg( batchCdngCodecName( cdngCodecOffset ) )
               .arg( batchExportFormatName( exportRequest.format ) )
               .arg( batchRenderedVideoCodecName( exportRequest.renderedCodec ) )
               .arg( batchRenderedVideoContainerName( exportRequest.renderedContainer ) ));

    int succeeded = 0;
    int failed = 0;

    for( const QString &mlvPath : mlvFiles )
    {
        /* ReceiptApplier mutates clip-local runtime decisions (Dual ISO state,
         * raw levels, and headless Look Assist). Keep those derivations scoped
         * to the MLV being exported so batch directories get one look per clip. */
        ReceiptSettings clipReceipt = receipt;
        ProcessResult res = exportSingleFile( mlvPath, outputPath, &clipReceipt );

        QString baseName = QFileInfo(mlvPath).completeBaseName();
        if( res.success )
        {
            BatchLogger::out(QStringLiteral("[BATCH] DONE %1 exported=%2 skipped=%3 elapsed=%4\n")
                       .arg( baseName )
                       .arg( res.framesExported )
                       .arg( res.framesSkipped )
                       .arg( res.elapsedSeconds, 0, 'f', 1 ));
            succeeded++;
        }
        else
        {
            BatchLogger::out(QStringLiteral("[BATCH] FAIL %1 error=%2 exported=%3 skipped=%4 elapsed=%5\n")
                       .arg( baseName,
                              res.errorMessage )
                       .arg( res.framesExported )
                       .arg( res.framesSkipped )
                       .arg( res.elapsedSeconds, 0, 'f', 1 ));
            failed++;

            /* Without --skip-errors, abort the entire batch on first failure */
            if( !BatchContext::skipErrors() )
            {
                BatchLogger::out(QStringLiteral("[BATCH] COMPLETE files=%1 succeeded=%2 failed=%3 total_elapsed=%4\n")
                           .arg( mlvFiles.size() ).arg( succeeded ).arg( failed )
                           .arg( totalTimer.elapsed() / 1000.0, 0, 'f', 1 ));
                return 4;
            }
        }
    }

    double totalElapsed = totalTimer.elapsed() / 1000.0;
    BatchLogger::out(QStringLiteral("[BATCH] COMPLETE files=%1 succeeded=%2 failed=%3 total_elapsed=%4\n")
               .arg( mlvFiles.size() ).arg( succeeded ).arg( failed )
               .arg( totalElapsed, 0, 'f', 1 ));

    if( failed > 0 ) return 1;
    return 0;
}

ProcessResult BatchRunner::exportSingleFile(const QString &mlvPath,
                                            const QString &outputRoot,
                                            ReceiptSettings *receipt)
{
    ProcessResult result;
    QString baseName = QFileInfo(mlvPath).completeBaseName();

    /* Open MLV file using the C API — same as MainWindow::openMlv() */
    int mlvErr = MLV_ERR_NONE;
    char mlvErrMsg[256] = { 0 };

#ifdef Q_OS_UNIX
    mlvObject_t *mlvObject = initMlvObjectWithClip(
        mlvPath.toUtf8().data(), MLV_OPEN_FULL, &mlvErr, mlvErrMsg );
#else
    mlvObject_t *mlvObject = initMlvObjectWithClip(
        mlvPath.toLatin1().data(), MLV_OPEN_FULL, &mlvErr, mlvErrMsg );
#endif

    if( mlvErr )
    {
        result.success = false;
        result.errorMessage = QStringLiteral("Cannot open MLV: %1").arg( QString(mlvErrMsg) );
        if( mlvObject ) freeMlvObject( mlvObject );
        return result;
    }

    /* Create processing object with default settings */
    processingObject_t *processingObject = initProcessingObject();
    setMlvProcessing( mlvObject, processingObject );
    disableMlvCaching( mlvObject );
    setMlvCpuCores( mlvObject, mlvappEffectiveWorkerThreadCount() );

    uint32_t totalFrames = getMlvFrames( mlvObject );
    BatchLogger::out(QStringLiteral("[BATCH] FILE %1 frames=%2\n").arg( baseName ).arg( totalFrames ));

    /* ---- Derive export parameters from receipt ----
     * cutIn/cutOut: use receipt values if set, else export all frames.
     * stretchX/Y: use receipt values if set, else no stretch.
     * NOTE: These must be computed BEFORE resume logic (which adjusts cutIn)
     * and BEFORE applyToMlv (which may mutate receipt fields). */
    uint32_t cutIn  = receipt->cutIn();
    uint32_t cutOut = receipt->cutOut();
    if( cutIn == 0 )  cutIn  = 1;
    if( cutOut == 0 || cutOut > totalFrames ) cutOut = totalFrames;

    /* Hardening (audit #1): a receipt cut-in past the clip end (or past cut-out) makes the export
     * loop run zero times yet report success -> the orchestrator would see exit 0 with zero DNGs
     * written. Fail explicitly so run() emits [BATCH] FAIL and returns a non-zero exit code. */
    if( cutIn > totalFrames || cutIn > cutOut )
    {
        result.success = false;
        result.errorMessage = QStringLiteral("Receipt cut-in %1 is past clip end (frames=%2, cut-out=%3)")
                    .arg( cutIn ).arg( totalFrames ).arg( cutOut );
        freeMlvObject( mlvObject );
        freeProcessingObject( processingObject );
        return result;
    }

    const uint32_t unclampedCutOut = cutOut;
    cutOut = CdngSequenceExport::cutOutClampedForMaxFrames(
        cutIn, cutOut, BatchContext::maxFrames() );
    if( cutOut != unclampedCutOut )
    {
        BatchLogger::out(QStringLiteral("[BATCH] MAX_FRAMES %1 max=%2 cutIn=%3 cutOut=%4 originalCutOut=%5\n")
                   .arg( baseName )
                   .arg( BatchContext::maxFrames() )
                   .arg( cutIn )
                   .arg( cutOut )
                   .arg( unclampedCutOut ));
    }

    uint32_t effectiveCutIn = cutIn;  /* may be adjusted by resume */

    /* -1 = uninitialized (never loaded). The vertical case mirrors the GUI first-load
     * auto de-squeeze; without it headless exports emit picAR={1,1,1,1} -> manual_ar=1 in
     * the DNG writer -> the writer's own RAWC de-squeeze (dng.c) is skipped -> squeezed
     * CDNGs for binned modes (e.g. 5D3 1080p: binning_x=3/binning_y=1 needs a 3:1
     * stretch). The rendered-video runner needs the identical answer, so both call the
     * one definition in BatchRunner.h rather than each carrying a copy of the bands. */
    double stretchX = STRETCH_H_100;
    double stretchY = STRETCH_V_100;
    CdngSequenceExport::effectiveStretchFactors( receipt->stretchFactorX(),
                                          receipt->stretchFactorY(),
                                          getMlvAspectRatio( mlvObject ),
                                          &stretchX, &stretchY );

    /* ---- Resume logic (--resume flag) ----
     * Scan the output subfolder for existing DNG files.  If the clip is
     * already fully exported, skip it entirely (exit 0, no file deletion).
     * If partially exported, advance cutIn past the last completed frame. */
    if( BatchContext::resumeEnabled() )
    {
        QString subFolder = outputRoot + QStringLiteral("/") + baseName;
        QDir subDir(subFolder);

        if( subDir.exists() )
        {
            /* Scan for clipBaseName_NNNNNN.dng files */
            QStringList dngFilter;
            dngFilter << baseName + QStringLiteral("_*.dng");
            QFileInfoList dngFiles = subDir.entryInfoList( dngFilter, QDir::Files );

            if( !dngFiles.isEmpty() )
            {
                /* Find the highest numeric suffix among existing DNG files.
                 * Filename pattern: clipBaseName_NNNNNN.dng
                 * We extract NNNNNN from each matching file. */
                uint32_t highestSuffix = 0;
                int validCount = 0;
                QString prefix = baseName + QStringLiteral("_");

                for( const QFileInfo &fi : dngFiles )
                {
                    QString fn = fi.completeBaseName(); /* clipBaseName_NNNNNN */
                    if( !fn.startsWith(prefix) ) continue;
                    QString numStr = fn.mid( prefix.length() );
                    bool ok = false;
                    uint32_t num = numStr.toUInt( &ok );
                    if( ok )
                    {
                        validCount++;
                        if( num > highestSuffix ) highestSuffix = num;
                    }
                }

                if( validCount > 0 )
                {
                    /* Map suffix → frame_index by scanning video_index[].
                     * Do NOT assume suffix == frame_index.  The MLV file's
                     * VIDF blocks store arbitrary frame_number values. */
                    int resumeFrameIndex = -1;
                    for( uint32_t fi = 0; fi < totalFrames; fi++ )
                    {
                        if( getMlvFrameNumber( mlvObject, fi ) == highestSuffix )
                        {
                            resumeFrameIndex = (int)fi;
                            break;
                        }
                    }

                    if( resumeFrameIndex >= 0 )
                    {
                        /* Convert to 1-based cutIn: next frame after last completed */
                        uint32_t newCutIn = (uint32_t)resumeFrameIndex + 2; /* +1 for 0→1-based, +1 for next */

                        if( newCutIn > cutOut )
                        {
                            /* Already complete — do NOT delete files, just log and skip */
                            BatchLogger::out(QStringLiteral("[BATCH] RESUME %1 already_complete existing=%2 highestSuffix=%3\n")
                                       .arg( baseName ).arg( validCount ).arg( highestSuffix ));
                            result.success = true;
                            result.framesExported = 0;
                            result.framesSkipped = cutOut - cutIn + 1;
                            result.elapsedSeconds = 0.0;
                            freeMlvObject( mlvObject );
                            freeProcessingObject( processingObject );
                            return result;
                        }

                        /* Clamp: never go below the receipt's original cutIn */
                        if( newCutIn > cutIn )
                        {
                            effectiveCutIn = newCutIn;
                            BatchLogger::out(QStringLiteral("[BATCH] RESUME %1 advancing cutIn=%2 (was %3) existing=%4 highestSuffix=%5\n")
                                       .arg( baseName ).arg( effectiveCutIn ).arg( cutIn )
                                       .arg( validCount ).arg( highestSuffix ));
                        }
                        else
                        {
                            BatchLogger::out(QStringLiteral("[BATCH] RESUME %1 no_advance highestSuffix=%2 maps_to_frame=%3 below_cutIn=%4\n")
                                       .arg( baseName ).arg( highestSuffix )
                                       .arg( resumeFrameIndex ).arg( cutIn ));
                        }
                    }
                    else
                    {
                        /* Suffix not found in video_index — files may be from a different clip.
                         * Play it safe: export from original cutIn (overwrite). */
                        BatchLogger::out(QStringLiteral("[BATCH] RESUME %1 suffix_not_found highestSuffix=%2 exporting_from=%3\n")
                                   .arg( baseName ).arg( highestSuffix ).arg( cutIn ));
                    }
                }
            }
            else
            {
                BatchLogger::out(QStringLiteral("[BATCH] RESUME %1 no_existing_frames\n").arg( baseName ));
            }
        }
        else
        {
            BatchLogger::out(QStringLiteral("[BATCH] RESUME %1 no_output_folder\n").arg( baseName ));
        }
    }

    /* ---- Apply receipt settings to the MLV pipeline ----
     * This calls the same C API functions the GUI's setSliders() triggers.
     * Works with both default-constructed receipts (no-op for most settings)
     * and receipts loaded from .marxml files.
     * Must happen AFTER resume logic (which needs raw mlvObject state for
     * video_index scanning) but BEFORE the export call. */
    ReceiptApplier::applyToMlv( receipt, mlvObject, processingObject );

    /* Analyze the ORIGINAL cut-in frame, NOT effectiveCutIn: --resume advances
     * effectiveCutIn past already-exported frames, and Look Assist defaults are
     * clip-wide, so a resumed run must analyze the same anchor as the first run
     * or the remaining DNGs would get different BaselineExposure / AsShotNeutral
     * / raw-level defaults. See CdngSequenceExport::lookAssistAnalysisFrameIndex. */
    const bool lookAssistApplied = ReceiptApplier::applyHeadlessLookAssist(
        receipt,
        mlvObject,
        processingObject,
        CdngSequenceExport::lookAssistAnalysisFrameIndex( cutIn, effectiveCutIn ) );

    /* Print runtime FINGERPRINT — proves settings reached the pipeline */
    ReceiptApplier::printFingerprint( mlvObject, processingObject );

    const int cdngCodecProfile =
        CODEC_CDNG + normalizedBatchCdngCodecOffset( BatchContext::cdngCodecOffset() );

    /* Export CDNG sequence */
    result = CdngSequenceExport::exportCdngSequence(
        mlvObject,
        outputRoot,
        baseName,
        cdngCodecProfile,     /* selected headless CDNG codec */
        CODEC_CNDG_DEFAULT,   /* standard folder/file naming */
        effectiveCutIn,       /* from receipt, possibly advanced by --resume */
        cutOut,               /* from receipt or totalFrames */
        stretchX,             /* from receipt or STRETCH_H_100 */
        stretchY,             /* from receipt or STRETCH_V_100 */
        ( effectiveCutIn == cutIn ), /* audit #2: export audio only on a full (non-resume) pass.
                                      * On --resume, effectiveCutIn is advanced past already-exported
                                      * frames; writeMlvAudioToWaveCut would fopen("wb")-truncate the
                                      * already-complete <clip>.wav and rewrite it with tail-only audio
                                      * (audio is written before frames, so the run-1 WAV is complete
                                      * whenever resume triggers). Skip it to preserve the full WAV. */
        receipt->rawFixesEnabled(), /* from receipt */
        nullptr,
        lookAssistApplied,
        receipt->exposure()
    );

    /* Clean up */
    freeMlvObject( mlvObject );
    freeProcessingObject( processingObject );

    return result;
}
