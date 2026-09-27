/*!
 * \file main.cpp
 * \author masc4ii
 * \copyright 2017
 * \brief The main... the start of the horror
 */

#include "MainWindow.h"
#include "MyApplication.h"
#include "CrashForensics.h"
#include "Phase3Mode.h"
#include "../../src/batch/BatchContext.h"
#include "../../src/batch/BatchRunner.h"
#include "../../src/batch/BatchLogger.h"
#include "../../src/batch/MlvTrim.h"
#include "../../src/batch/EnvFlags.h"
#include "debug/ForceSingleThread.h"

#include <QByteArray>
#include <QCommandLineParser>
#include <QCoreApplication>
#include <QDateTime>
#include <QDir>
#include <QFileInfo>
#include <QSurfaceFormat>
#include <QTextStream>
#include <cstring>

#ifdef Q_OS_WIN
extern "C" {
__declspec(dllexport) unsigned long NvOptimusEnablement = 0x00000001;
__declspec(dllexport) int AmdPowerXpressRequestHighPerformance = 1;
}
#endif

/* Raw argv scan for "--batch" BEFORE QApplication is constructed.
 * QCommandLineParser needs QApplication, but we need to know the
 * mode early to skip MainWindow creation entirely in batch mode. */
static bool hasBatchFlag(int argc, char *argv[])
{
    for (int i = 1; i < argc; ++i)
    {
        if (std::strcmp(argv[i], "--batch") == 0) return true;
    }
    return false;
}

static bool hasTrimMlvFlag(int argc, char *argv[])
{
    for (int i = 1; i < argc; ++i)
    {
        if (std::strcmp(argv[i], "--trim-mlv") == 0) return true;
    }
    return false;
}

static bool hasPlaybackProfileFlag(int argc, char *argv[])
{
    for (int i = 1; i < argc; ++i)
    {
        if (std::strcmp(argv[i], "--profile-playback") == 0) return true;
    }
    return false;
}

static bool hasGuiPlaybackSmokeFlag(int argc, char *argv[])
{
    for (int i = 1; i < argc; ++i)
    {
        if (std::strcmp(argv[i], "--gui-smoke-playback") == 0) return true;
    }
    return false;
}

static bool envFlagEnabled(const char *name)
{
    if (!name) return false;
    return mlvappEnvFlagEnabled(qgetenv(name));
}

static bool argvOptionMatches(const char *arg, const char *option)
{
    if (!arg || !option) return false;
    if (std::strcmp(arg, option) == 0) return true;

    const size_t optionLength = std::strlen(option);
    return std::strncmp(arg, option, optionLength) == 0
        && arg[optionLength] == '=';
}

static bool hasGpuRelatedFlag(int argc, char *argv[])
{
    for (int i = 1; i < argc; ++i)
    {
        if (argvOptionMatches(argv[i], "--gpu-viewport")) return true;
        if (argvOptionMatches(argv[i], "--gpu-preview-processing")) return true;
        if (argvOptionMatches(argv[i], "--gpu-bilinear-debayer")) return true;
        if (argvOptionMatches(argv[i], "--gpu-amaze-debayer")) return true;
        if (argvOptionMatches(argv[i], "--gpu-amaze-texture-present")) return true;
        if (argvOptionMatches(argv[i], "--enable-phase3-quality-modes")) return true;
    }
    return false;
}

static bool shouldPreferDesktopOpenGl(int argc,
                                      char *argv[],
                                      bool batch,
                                      bool trim_mlv,
                                      bool profile_playback)
{
#ifdef Q_OS_WIN
    if (!qEnvironmentVariableIsEmpty("QT_OPENGL")) return false;
    if (batch || trim_mlv) return false;
    if (profile_playback) return true;
    if (hasGpuRelatedFlag(argc, argv)) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GL_VIEWPORT")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_PROCESSING")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_DEBAYER")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT")) return true;
#else
    Q_UNUSED(argc)
    Q_UNUSED(argv)
    Q_UNUSED(batch)
    Q_UNUSED(trim_mlv)
    Q_UNUSED(profile_playback)
#endif
    return false;
}

static bool shouldShareOpenGlContexts(int argc,
                                      char *argv[],
                                      bool batch,
                                      bool trim_mlv)
{
    if (batch || trim_mlv) return false;
    if (hasGpuRelatedFlag(argc, argv)) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GL_VIEWPORT")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_PROCESSING")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_DEBAYER")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT")) return true;
    if (qEnvironmentVariableIsSet("MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT")) return true;
    return false;
}

/* Bug A on-screen present A/B: truthy-value env flag (1/true/yes/on). Used to gate
 * the global-level present toggles (skip AA_ShareOpenGLContexts, set a global
 * default QSurfaceFormat) that pair with the per-widget toggles in
 * GpuDisplayViewport.cpp. Inert unless explicitly set. */
static bool viewportAbEnvFlag(const char *name)
{
    if (!qEnvironmentVariableIsSet(name)) return false;
    const QByteArray v = qgetenv(name).trimmed().toLower();
    if (v.isEmpty()) return false;
    return v == "1" || v == "true" || v == "yes" || v == "on";
}

static MainWindow::PlaybackProfileScope parsePlaybackProfileScope(const QString & value, bool * ok)
{
    if (ok) *ok = true;

    const QString normalized = value.trimmed().toLower();
    if (normalized.isEmpty() || normalized == QStringLiteral("none")) return MainWindow::PlaybackProfileScope::None;
    if (normalized == QStringLiteral("histogram")) return MainWindow::PlaybackProfileScope::Histogram;
    if (normalized == QStringLiteral("waveform")) return MainWindow::PlaybackProfileScope::Waveform;
    if (normalized == QStringLiteral("parade")) return MainWindow::PlaybackProfileScope::Parade;
    if (normalized == QStringLiteral("vectorscope")) return MainWindow::PlaybackProfileScope::Vectorscope;

    if (ok) *ok = false;
    return MainWindow::PlaybackProfileScope::None;
}

static MainWindow::PlaybackProfileDebayerRequest parsePlaybackProfileDebayerRequest(
    const QString & value,
    bool * ok)
{
    if (ok) *ok = true;

    const QString normalized = value.trimmed().toLower();
    if (normalized.isEmpty() || normalized == QStringLiteral("auto"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::Auto;
    }
    if (normalized == QStringLiteral("receipt"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::Receipt;
    }
    if (normalized == QStringLiteral("none"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::None;
    }
    if (normalized == QStringLiteral("simple"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::Simple;
    }
    if (normalized == QStringLiteral("bilinear"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::Bilinear;
    }
    if (normalized == QStringLiteral("lmmse"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::LMMSE;
    }
    if (normalized == QStringLiteral("igv"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::IGV;
    }
    if (normalized == QStringLiteral("amaze"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::AMaZE;
    }
    if (normalized == QStringLiteral("ahd"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::AHD;
    }
    if (normalized == QStringLiteral("rcd"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::RCD;
    }
    if (normalized == QStringLiteral("dcb"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::DCB;
    }
    if (normalized == QStringLiteral("amaze-cached"))
    {
        return MainWindow::PlaybackProfileDebayerRequest::AmazeCached;
    }

    if (ok) *ok = false;
    return MainWindow::PlaybackProfileDebayerRequest::Auto;
}

static MainWindow::PlaybackProfileProcessingRequest parsePlaybackProfileProcessingRequest(
    const QString & value,
    bool * ok)
{
    if (ok) *ok = true;

    const QString normalized = value.trimmed().toLower();
    if (normalized.isEmpty() || normalized == QStringLiteral("auto"))
    {
        return MainWindow::PlaybackProfileProcessingRequest::Auto;
    }
    if (normalized == QStringLiteral("receipt"))
    {
        return MainWindow::PlaybackProfileProcessingRequest::Receipt;
    }
    if (normalized == QStringLiteral("subset"))
    {
        return MainWindow::PlaybackProfileProcessingRequest::Subset;
    }

    if (ok) *ok = false;
    return MainWindow::PlaybackProfileProcessingRequest::Auto;
}

static GpuPreviewProcessingBackendRequest parsePlaybackProfileGpuPreviewProcessingBackend(
    const QString & value,
    bool * ok)
{
    if (ok) *ok = true;

    const QString normalized = value.trimmed().toLower();
    if (normalized.isEmpty() || normalized == QStringLiteral("auto"))
    {
        return GpuPreviewProcessingBackendRequest::Auto;
    }
    if (normalized == QStringLiteral("cpu"))
    {
        return GpuPreviewProcessingBackendRequest::Cpu;
    }
    if (normalized == QStringLiteral("gpu"))
    {
        return GpuPreviewProcessingBackendRequest::Gpu;
    }

    if (ok) *ok = false;
    return GpuPreviewProcessingBackendRequest::Auto;
}

static GpuBilinearDebayerBackendRequest parsePlaybackProfileGpuBilinearDebayerBackend(
    const QString & value,
    bool * ok)
{
    if (ok) *ok = true;

    const QString normalized = value.trimmed().toLower();
    if (normalized.isEmpty() || normalized == QStringLiteral("auto"))
    {
        return GpuBilinearDebayerBackendRequest::Auto;
    }
    if (normalized == QStringLiteral("cpu"))
    {
        return GpuBilinearDebayerBackendRequest::Cpu;
    }
    if (normalized == QStringLiteral("gpu"))
    {
        return GpuBilinearDebayerBackendRequest::Gpu;
    }

    if (ok) *ok = false;
    return GpuBilinearDebayerBackendRequest::Auto;
}

static GpuAmazeDebayerBackendRequest parsePlaybackProfileGpuAmazeDebayerBackend(
    const QString & value,
    bool * ok)
{
    if (ok) *ok = true;

    const QString normalized = value.trimmed().toLower();
    if (normalized.isEmpty() || normalized == QStringLiteral("auto"))
    {
        return GpuAmazeDebayerBackendRequest::Auto;
    }
    if (normalized == QStringLiteral("cpu"))
    {
        return GpuAmazeDebayerBackendRequest::Cpu;
    }
    if (normalized == QStringLiteral("gpu"))
    {
        return GpuAmazeDebayerBackendRequest::Gpu;
    }

    if (ok) *ok = false;
    return GpuAmazeDebayerBackendRequest::Auto;
}

static int parseBatchCdngCodecOffset(const QString & value, bool * ok)
{
    if (ok) *ok = true;

    const QString normalized = value.trimmed().toLower();
    if (normalized.isEmpty()
     || normalized == QStringLiteral("uncompressed")
     || normalized == QStringLiteral("default"))
    {
        return 0;
    }
    if (normalized == QStringLiteral("lossless")
     || normalized == QStringLiteral("compressed"))
    {
        return 1;
    }
    if (normalized == QStringLiteral("fast")
     || normalized == QStringLiteral("fast-pass")
     || normalized == QStringLiteral("fastpass"))
    {
        return 2;
    }

    if (ok) *ok = false;
    return 0;
}

static int parsePositiveBatchInteger(const QString & value, bool * ok)
{
    bool parsed = false;
    const int result = value.trimmed().toInt(&parsed);
    if( ok ) *ok = parsed && result > 0;
    return parsed ? result : 0;
}

static int runBatch(QCoreApplication &app)
{
    QCommandLineParser parser;
    parser.setApplicationDescription(
        QStringLiteral("MLVApp batch mode — headless Cinema DNG export"));

    /* Do NOT call parser.addHelpOption() — on Windows/Qt it triggers a
     * QMessageBox.  We handle -h/--help manually below. */
    QCommandLineOption helpOpt(
        QStringList() << QStringLiteral("h") << QStringLiteral("help"),
        QStringLiteral("Show this help text and exit."));
    parser.addOption(helpOpt);

    /* --batch is already consumed by hasBatchFlag(); add it here so
     * QCommandLineParser doesn't complain about an unknown option. */
    QCommandLineOption batchOpt(
        QStringLiteral("batch"),
        QStringLiteral("Run in headless batch export mode."));
    parser.addOption(batchOpt);

    QCommandLineOption inputOpt(
        QStringList() << QStringLiteral("i") << QStringLiteral("input"),
        QStringLiteral("Input MLV file or folder path."),
        QStringLiteral("path"));
    parser.addOption(inputOpt);

    QCommandLineOption outputOpt(
        QStringList() << QStringLiteral("o") << QStringLiteral("output"),
        QStringLiteral("Output directory for exported DNG sequences."),
        QStringLiteral("path"));
    parser.addOption(outputOpt);

    QCommandLineOption skipErrorsOpt(
        QStringLiteral("skip-errors"),
        QStringLiteral("Skip corrupt frames instead of aborting."));
    parser.addOption(skipErrorsOpt);

    QCommandLineOption logOpt(
        QStringLiteral("log"),
        QStringLiteral("Mirror log output to file."),
        QStringLiteral("file"));
    parser.addOption(logOpt);

    QCommandLineOption verboseOpt(
        QStringLiteral("verbose"),
        QStringLiteral("Enable detailed per-frame logging."));
    parser.addOption(verboseOpt);

    QCommandLineOption receiptOpt(
        QStringList() << QStringLiteral("r") << QStringLiteral("receipt"),
        QStringLiteral("Apply .marxml receipt settings to export."),
        QStringLiteral("file"));
    parser.addOption(receiptOpt);

    QCommandLineOption defaultReceiptOpt(
        QStringLiteral("default-receipt"),
        QStringLiteral("Use the GUI-configured default receipt."));
    parser.addOption(defaultReceiptOpt);

    QCommandLineOption resumeOpt(
        QStringLiteral("resume"),
        QStringLiteral("Skip clips whose DNG output already matches expected frame count."));
    parser.addOption(resumeOpt);

    QCommandLineOption maxFramesOpt(
        QStringLiteral("max-frames"),
        QStringLiteral("Limit each batch export to at most this many frames after receipt cut-in/cut-out resolution."),
        QStringLiteral("count"));
    parser.addOption(maxFramesOpt);

    QCommandLineOption exportFormatOpt(
        QStringLiteral("export-format"),
        QStringLiteral("Batch export format: cdng (Cinema DNG frame sequence) or rendered (encoded video). Both are supported."),
        QStringLiteral("format"),
        QStringLiteral("cdng"));
    parser.addOption(exportFormatOpt);

    QCommandLineOption renderedCodecOpt(
        QStringLiteral("rendered-codec"),
        QStringLiteral("Rendered-video codec: h264, h265/hevc, or prores. Requires --export-format rendered."),
        QStringLiteral("codec"));
    parser.addOption(renderedCodecOpt);

    QCommandLineOption renderedContainerOpt(
        QStringLiteral("rendered-container"),
        QStringLiteral("Rendered-video container: mov, mp4, or mkv. Requires --export-format rendered."),
        QStringLiteral("container"));
    parser.addOption(renderedContainerOpt);

    QCommandLineOption renderedResizeWidthOpt(
        QStringLiteral("rendered-resize-width"),
        QStringLiteral("Rendered-video resize width in pixels. Requires --export-format rendered."),
        QStringLiteral("pixels"));
    parser.addOption(renderedResizeWidthOpt);

    QCommandLineOption renderedResizeHeightOpt(
        QStringLiteral("rendered-resize-height"),
        QStringLiteral("Rendered-video resize height in pixels, unless --rendered-resize-height-locked derives it from source geometry and stretch."),
        QStringLiteral("pixels"));
    parser.addOption(renderedResizeHeightOpt);

    QCommandLineOption renderedResizeHeightLockedOpt(
        QStringLiteral("rendered-resize-height-locked"),
        QStringLiteral("Derive planned rendered-video resize height from source geometry, stretch, and --rendered-resize-width."));
    parser.addOption(renderedResizeHeightLockedOpt);

    QCommandLineOption renderedFfmpegOpt(
        QStringLiteral("rendered-ffmpeg"),
        QStringLiteral("ffmpeg executable path or command name used for rendered export. Defaults to 'ffmpeg' on PATH; rendered export fails with an actionable error if it cannot be resolved."),
        QStringLiteral("path-or-name"));
    parser.addOption(renderedFfmpegOpt);

    QCommandLineOption cdngCodecOpt(
        QStringLiteral("cdng-codec"),
        QStringLiteral("CDNG codec for batch export: uncompressed, lossless, or fast-pass. Default: uncompressed."),
        QStringLiteral("mode"));
    parser.addOption(cdngCodecOpt);

    parser.process(app);

    /* Init log file mirror as early as possible so that --help and
     * missing-arg errors are captured in the log file too. */
    QString logPath = parser.value(logOpt);
    BatchLogger::init(logPath);

    /* --help: print to stdout (+ log), exit 0.  No QMessageBox. */
    if( parser.isSet(helpOpt) )
    {
        BatchLogger::out(parser.helpText() + QStringLiteral("\n"));
        BatchLogger::shutdown();
        return 0;
    }

    /* --input and --output are required in batch mode */
    if( !parser.isSet(inputOpt) || !parser.isSet(outputOpt) )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: --input and --output are required.\n\n"));
        BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
        BatchLogger::shutdown();
        return 2;
    }

    QString inputPath   = parser.value(inputOpt);
    QString outputPath  = parser.value(outputOpt);
    bool skipErrors     = parser.isSet(skipErrorsOpt);
    bool verbose        = parser.isSet(verboseOpt);
    QString receiptPath     = parser.value(receiptOpt);
    bool useDefaultReceipt  = parser.isSet(defaultReceiptOpt);

    bool resume         = parser.isSet(resumeOpt);
    uint maxFrames      = 0;
    int cdngCodecOffset = 0;
    const QString exportFormatRaw = parser.value(exportFormatOpt);
    BatchExportFormatRequest exportRequest =
        batchExportFormatRequestFromString(exportFormatRaw);
    const bool renderedCodecSet = parser.isSet(renderedCodecOpt);
    const bool renderedContainerSet = parser.isSet(renderedContainerOpt);
    const bool renderedResizeWidthSet = parser.isSet(renderedResizeWidthOpt);
    const bool renderedResizeHeightSet = parser.isSet(renderedResizeHeightOpt);
    const bool renderedResizeHeightLockedSet =
        parser.isSet(renderedResizeHeightLockedOpt);
    const bool renderedFfmpegSet = parser.isSet(renderedFfmpegOpt);
    const bool renderedResizeOptionSet = renderedResizeWidthSet
                                      || renderedResizeHeightSet
                                      || renderedResizeHeightLockedSet;
    const bool renderedOptionSet = renderedCodecSet
                                || renderedContainerSet
                                || renderedResizeOptionSet
                                || renderedFfmpegSet;
    BatchRenderedVideoRenderSettings renderedSettings =
        batchRenderedVideoDefaultRenderSettings();
    QString renderedFfmpegExecutable;
    if( renderedOptionSet
     && parser.isSet(exportFormatOpt)
     && exportRequest.format == BatchExportFormat::Cdng )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: rendered-video options require --export-format rendered-video or a rendered-video alias; omit them for cdng.\n\n"));
        BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
        BatchLogger::shutdown();
        return 2;
    }
    if( renderedOptionSet && exportRequest.format == BatchExportFormat::Cdng )
        exportRequest.format = BatchExportFormat::RenderedVideo;
    if( renderedCodecSet )
    {
        bool ok = false;
        exportRequest.renderedCodec =
            batchRenderedVideoCodecFromString(parser.value(renderedCodecOpt), &ok);
        if( !ok )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: --rendered-codec must be one of: h264, h265, hevc, prores.\n\n"));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
    }
    if( renderedContainerSet )
    {
        bool ok = false;
        exportRequest.renderedContainer =
            batchRenderedVideoContainerFromString(parser.value(renderedContainerOpt), &ok);
        if( !ok )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: --rendered-container must be one of: mov, mp4, mkv.\n\n"));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
    }
    if( renderedResizeOptionSet )
    {
        if( !renderedResizeWidthSet )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: --rendered-resize-width is required when using rendered resize options.\n\n"));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }

        bool ok = false;
        const int resizeWidth =
            parsePositiveBatchInteger(parser.value(renderedResizeWidthOpt), &ok);
        if( !ok )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: --rendered-resize-width must be a positive integer.\n\n"));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }

        int resizeHeight = 0;
        if( renderedResizeHeightSet )
        {
            resizeHeight =
                parsePositiveBatchInteger(parser.value(renderedResizeHeightOpt), &ok);
            if( !ok )
            {
                BatchLogger::err(QStringLiteral("[BATCH] ERROR: --rendered-resize-height must be a positive integer.\n\n"));
                BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
                BatchLogger::shutdown();
                return 2;
            }
        }
        else if( !renderedResizeHeightLockedSet )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: --rendered-resize-height is required unless --rendered-resize-height-locked is set.\n\n"));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }

        renderedSettings = batchRenderedVideoRenderSettingsFromExplicitResize(
            true,
            resizeWidth,
            resizeHeight,
            renderedResizeHeightLockedSet);
        if( !renderedSettings.ready )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: rendered-video resize settings are invalid. %1.\n\n")
                .arg(batchRenderedVideoRenderSettingsSummary(renderedSettings)));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
    }
    if( renderedFfmpegSet )
    {
        renderedFfmpegExecutable = parser.value(renderedFfmpegOpt).trimmed();
        if( renderedFfmpegExecutable.isEmpty() )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: --rendered-ffmpeg must not be empty.\n\n"));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
    }
    const BatchExportFormat exportFormat =
        exportRequest.format;
    if( exportFormat == BatchExportFormat::Unknown )
    {
        BatchLogger::err(QStringLiteral("[BATCH] ERROR: --export-format must be cdng. Rendered-video E4 is not implemented yet.\n\n"));
        BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
        BatchLogger::shutdown();
        return 2;
    }
    if( exportFormat == BatchExportFormat::RenderedVideo )
    {
        const BatchRenderedVideoFfmpegBinaryPlan ffmpegBinaryPlan =
            batchRenderedVideoFfmpegBinaryPlanFromRequestedName(
                renderedFfmpegExecutable);
        const BatchRenderedVideoJobPlan renderedPlan =
            batchRenderedVideoJobPlanFromRequest(
                inputPath,
                outputPath,
                exportRequest,
                renderedSettings,
                ffmpegBinaryPlan);
        if( !renderedPlan.requestValid )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: rendered-video request is invalid. %1. %2.\n\n")
                .arg(batchExportFormatRequestSummary(renderedPlan.request))
                .arg(batchRenderedVideoRequestShapeError(renderedPlan.request)));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
        if( !renderedPlan.targetReady )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: rendered-video target is incomplete. %1. %2. Choose a rendered codec or container, for example --export-format h264, --export-format mp4, or --export-format prores.\n\n")
                .arg(batchExportFormatRequestSummary(renderedPlan.request))
                .arg(batchRenderedVideoTargetSummary(renderedPlan.target)));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
        if( !renderedPlan.encoderReady )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: rendered-video encoder preset is unavailable. %1. %2. %3.\n\n")
                .arg(batchExportFormatRequestSummary(renderedPlan.request))
                .arg(batchRenderedVideoTargetSummary(renderedPlan.target))
                .arg(batchRenderedVideoEncoderPresetSummary(renderedPlan.encoderPreset)));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
        if( !renderedPlan.outputReady )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: rendered-video output path is invalid. %1. %2. %3. Choose an output directory or an explicit rendered file path ending in %4.\n\n")
                .arg(batchExportFormatRequestSummary(renderedPlan.request))
                .arg(batchRenderedVideoTargetSummary(renderedPlan.target))
                .arg(batchRenderedVideoOutputPlanSummary(renderedPlan.outputPlan))
                .arg(renderedPlan.target.extension));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
        /* Statically complete rendered-video requests continue into
         * BatchRunner, which can inspect clip metadata and then fail closed
         * before any rendered frames or output directories are produced. */
    }
    if( parser.isSet(maxFramesOpt) )
    {
        bool ok = false;
        maxFrames = parser.value(maxFramesOpt).toUInt(&ok);
        if( !ok || maxFrames == 0 )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: --max-frames must be a positive integer.\n\n"));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
    }
    if( parser.isSet(cdngCodecOpt) && exportFormat == BatchExportFormat::Cdng )
    {
        bool ok = false;
        cdngCodecOffset = parseBatchCdngCodecOffset(parser.value(cdngCodecOpt), &ok);
        if( !ok )
        {
            BatchLogger::err(QStringLiteral("[BATCH] ERROR: --cdng-codec must be one of: uncompressed, lossless, fast-pass.\n\n"));
            BatchLogger::err(parser.helpText() + QStringLiteral("\n"));
            BatchLogger::shutdown();
            return 2;
        }
    }

    /* Store in BatchContext for global access */
    BatchContext::setBatchMode(true);
    BatchContext::setSkipErrors(skipErrors);
    BatchContext::setVerbose(verbose);
    BatchContext::setLogPath(logPath);
    BatchContext::setReceiptPath(receiptPath);
    BatchContext::setUseDefaultReceipt(useDefaultReceipt);
    BatchContext::setResumeEnabled(resume);
    BatchContext::setMaxFrames(static_cast<uint32_t>(maxFrames));
    BatchContext::setCdngCodecOffset(cdngCodecOffset);
    BatchContext::setExportFormatRequest(exportRequest);
    BatchContext::setRenderedVideoRenderSettings(renderedSettings);
    BatchContext::setRenderedVideoFfmpegExecutable(renderedFfmpegExecutable);

    int exitCode = BatchRunner::run(inputPath, outputPath);
    BatchLogger::shutdown();
    return exitCode;
}

static int runPlaybackProfile(QApplication &app)
{
    QCommandLineParser parser;
    parser.setApplicationDescription(
        QStringLiteral("MLVApp headless playback profiler - steps frames through the real Qt playback path and writes JSON timings."));

    QCommandLineOption helpOpt(
        QStringList() << QStringLiteral("h") << QStringLiteral("help"),
        QStringLiteral("Show this help text and exit."));
    parser.addOption(helpOpt);

    parser.addOption(QCommandLineOption(
        QStringLiteral("profile-playback"),
        QStringLiteral("Run in headless playback profiling mode.")));

    const QCommandLineOption inputOpt(
        QStringList() << QStringLiteral("i") << QStringLiteral("input"),
        QStringLiteral("Input MLV file path."),
        QStringLiteral("path"));
    parser.addOption(inputOpt);

    const QCommandLineOption outputOpt(
        QStringList() << QStringLiteral("o") << QStringLiteral("output"),
        QStringLiteral("Output JSON file path."),
        QStringLiteral("path"));
    parser.addOption(outputOpt);

    const QCommandLineOption receiptOpt(
        QStringList() << QStringLiteral("r") << QStringLiteral("receipt"),
        QStringLiteral("Optional .marxml receipt to apply before profiling."),
        QStringLiteral("file"));
    parser.addOption(receiptOpt);

    const QCommandLineOption framesOpt(
        QStringLiteral("frames"),
        QStringLiteral("Number of frames to step through."),
        QStringLiteral("count"),
        QStringLiteral("16"));
    parser.addOption(framesOpt);

    const QCommandLineOption startFrameOpt(
        QStringLiteral("start-frame"),
        QStringLiteral("Zero-based first frame to render."),
        QStringLiteral("frame"),
        QStringLiteral("0"));
    parser.addOption(startFrameOpt);

    const QCommandLineOption frameStepOpt(
        QStringLiteral("frame-step"),
        QStringLiteral("Frame increment between profiled samples."),
        QStringLiteral("count"),
        QStringLiteral("1"));
    parser.addOption(frameStepOpt);

    const QCommandLineOption scopeOpt(
        QStringLiteral("scope"),
        QStringLiteral("Optional live scope during profiling: none, histogram, waveform, parade, vectorscope."),
        QStringLiteral("mode"),
        QStringLiteral("none"));
    parser.addOption(scopeOpt);

    const QCommandLineOption playbackDebayerOpt(
        QStringLiteral("playback-debayer"),
        QStringLiteral("Playback debayer policy for profiling: auto, receipt, none, simple, bilinear, lmmse, igv, amaze, ahd, rcd, dcb, amaze-cached."),
        QStringLiteral("mode"),
        QStringLiteral("auto"));
    parser.addOption(playbackDebayerOpt);

    const QCommandLineOption playbackProcessingOpt(
        QStringLiteral("playback-processing"),
        QStringLiteral("Playback processing policy for profiling: auto, receipt, subset."),
        QStringLiteral("mode"),
        QStringLiteral("auto"));
    parser.addOption(playbackProcessingOpt);

    const QCommandLineOption zebrasOpt(
        QStringLiteral("zebras"),
        QStringLiteral("Enable zebra overlay during profiling."));
    parser.addOption(zebrasOpt);

    const QCommandLineOption rawCacheOpt(
        QStringLiteral("raw-cache-mb"),
        QStringLiteral("Enable raw caching with this many MiB."),
        QStringLiteral("mebibytes"),
        QStringLiteral("0"));
    parser.addOption(rawCacheOpt);

    const QCommandLineOption cacheCpuOpt(
        QStringLiteral("cache-cpu-cores"),
        QStringLiteral("Cache worker cores when raw caching is enabled."),
        QStringLiteral("count"),
        QStringLiteral("1"));
    parser.addOption(cacheCpuOpt);

    const QCommandLineOption threadsOpt(
        QStringLiteral("threads"),
        QStringLiteral("Force MLVApp worker thread count via MLVAPP_FORCE_THREADS, or use auto to leave the worker count unforced."),
        QStringLiteral("count"),
        QStringLiteral("1"));
    parser.addOption(threadsOpt);

    const QCommandLineOption fastOpenOpt(
        QStringLiteral("fast-open"),
        QStringLiteral("Use the preview/open-for-preview path instead of a full open."));
    parser.addOption(fastOpenOpt);

    const QCommandLineOption gpuViewportOpt(
        QStringLiteral("gpu-viewport"),
        QStringLiteral("Enable the experimental OpenGL viewport path while profiling."));
    parser.addOption(gpuViewportOpt);

    const QCommandLineOption gpuPreviewProcessingOpt(
        QStringLiteral("gpu-preview-processing"),
        QStringLiteral("Preview-processing backend selection for the 16-bit GPU viewport path: auto, cpu, gpu. Selecting gpu implies --gpu-viewport."),
        QStringLiteral("backend"),
        QStringLiteral("auto"));
    parser.addOption(gpuPreviewProcessingOpt);

    const QCommandLineOption gpuBilinearDebayerOpt(
        QStringLiteral("gpu-bilinear-debayer"),
        QStringLiteral("Experimental bilinear debayer backend selection for the GPU preview-processing path: auto, cpu, gpu. Selecting gpu implies --gpu-viewport."),
        QStringLiteral("backend"),
        QStringLiteral("auto"));
    parser.addOption(gpuBilinearDebayerOpt);

    const QCommandLineOption gpuAmazeDebayerOpt(
        QStringLiteral("gpu-amaze-debayer"),
        QStringLiteral("Experimental AMaZE debayer backend selection for the GPU preview-processing path: auto, cpu, gpu. Selecting gpu implies --gpu-viewport and --gpu-preview-processing gpu, and falls back to CPU AMaZE if the CUDA backend is unavailable."),
        QStringLiteral("backend"),
        QStringLiteral("auto"));
    parser.addOption(gpuAmazeDebayerOpt);

    const QCommandLineOption showWindowOpt(
        QStringLiteral("show-window"),
        QStringLiteral("Show the main window while profiling instead of keeping it hidden."));
    parser.addOption(showWindowOpt);

    const QCommandLineOption waitForPaintOpt(
        QStringLiteral("wait-for-paint"),
        QStringLiteral("After each frameReady(), wait for the graphics viewport to receive a paint event and record paint latency. Implies --show-window."));
    parser.addOption(waitForPaintOpt);

    const QCommandLineOption exercisePlayActionOpt(
        QStringLiteral("exercise-play-action"),
        QStringLiteral("Toggle the real Play QAction and fail if frameReady/slider advance does not happen."));
    parser.addOption(exercisePlayActionOpt);

    const QCommandLineOption exerciseLookAssistToggleOpt(
        QStringLiteral("exercise-look-assist-toggle"),
        QStringLiteral("When Look Assist is enabled, click it off/on and verify the re-applied look matches the load-time look."));
    parser.addOption(exerciseLookAssistToggleOpt);

    const QCommandLineOption exerciseLookAssistSettleOpt(
        QStringLiteral("exercise-look-assist-settle"),
        QStringLiteral("When Look Assist is enabled, render the start frame and wait for Look Assist diagnostics before writing the profile JSON."));
    parser.addOption(exerciseLookAssistSettleOpt);

    const QCommandLineOption exerciseScaleFactorToggleOpt(
        QStringLiteral("exercise-scale-toggle"),
        QStringLiteral("Render at a playback scale, switch to x1 on the loaded clip, and verify the settled frame updates safely."));
    parser.addOption(exerciseScaleFactorToggleOpt);

    const QCommandLineOption exerciseScaleFactorToggleFromOpt(
        QStringLiteral("exercise-scale-toggle-from"),
        QStringLiteral("Starting playback scale for --exercise-scale-toggle. Supported values: 2, 4, or 8."),
        QStringLiteral("scale"),
        QStringLiteral("2"));
    parser.addOption(exerciseScaleFactorToggleFromOpt);

    const QCommandLineOption stageLogOpt(
        QStringLiteral("stage-log"),
        QStringLiteral("Optional stage timing log path. Also enables MLVAPP_STAGE_TIMING."),
        QStringLiteral("file"));
    parser.addOption(stageLogOpt);

    parser.process(app);

    QTextStream out(stdout);
    QTextStream err(stderr);

    if (parser.isSet(helpOpt))
    {
        out << parser.helpText() << "\n";
        return 0;
    }

    if (!parser.isSet(inputOpt) || !parser.isSet(outputOpt))
    {
        err << "[PROFILE] ERROR: --input and --output are required.\n\n";
        err << parser.helpText() << "\n";
        return 2;
    }

    bool ok = false;
    const int frameCount = parser.value(framesOpt).toInt(&ok);
    if (!ok || frameCount <= 0)
    {
        err << "[PROFILE] ERROR: --frames must be greater than 0.\n";
        return 2;
    }

    const int startFrame = parser.value(startFrameOpt).toInt(&ok);
    if (!ok || startFrame < 0)
    {
        err << "[PROFILE] ERROR: --start-frame must be 0 or greater.\n";
        return 2;
    }

    const int frameStep = parser.value(frameStepOpt).toInt(&ok);
    if (!ok || frameStep <= 0)
    {
        err << "[PROFILE] ERROR: --frame-step must be greater than 0.\n";
        return 2;
    }

    const uint64_t rawCacheMB = parser.value(rawCacheOpt).toULongLong(&ok);
    if (!ok)
    {
        err << "[PROFILE] ERROR: invalid --raw-cache-mb value.\n";
        return 2;
    }

    const int cacheCpuCores = parser.value(cacheCpuOpt).toInt(&ok);
    if (!ok || cacheCpuCores <= 0)
    {
        err << "[PROFILE] ERROR: --cache-cpu-cores must be greater than 0.\n";
        return 2;
    }

    const QString threadsValue = parser.value(threadsOpt).trimmed();
    const bool autoThreads = threadsValue.compare(QStringLiteral("auto"), Qt::CaseInsensitive) == 0
        || threadsValue == QStringLiteral("0");
    int forcedThreads = 1;
    if (!autoThreads)
    {
        forcedThreads = threadsValue.toInt(&ok);
        if (!ok || forcedThreads <= 0)
        {
            err << "[PROFILE] ERROR: --threads must be greater than 0 or set to auto.\n";
            return 2;
        }
    }

    bool scopeOk = false;
    const MainWindow::PlaybackProfileScope scope =
        parsePlaybackProfileScope(parser.value(scopeOpt), &scopeOk);
    if (!scopeOk)
    {
        err << "[PROFILE] ERROR: --scope must be one of none, histogram, waveform, parade, vectorscope.\n";
        return 2;
    }

    bool playbackDebayerOk = false;
    const MainWindow::PlaybackProfileDebayerRequest playbackDebayer =
        parsePlaybackProfileDebayerRequest(
            parser.value(playbackDebayerOpt),
            &playbackDebayerOk);
    if (!playbackDebayerOk)
    {
        err << "[PROFILE] ERROR: --playback-debayer must be one of auto, receipt, none, simple, bilinear, lmmse, igv, amaze, ahd, rcd, dcb, amaze-cached.\n";
        return 2;
    }

    bool playbackProcessingOk = false;
    const MainWindow::PlaybackProfileProcessingRequest playbackProcessing =
        parsePlaybackProfileProcessingRequest(
            parser.value(playbackProcessingOpt),
            &playbackProcessingOk);
    if (!playbackProcessingOk)
    {
        err << "[PROFILE] ERROR: --playback-processing must be one of auto, receipt, subset.\n";
        return 2;
    }

    bool gpuPreviewProcessingOk = false;
    GpuPreviewProcessingBackendRequest gpuPreviewProcessingBackend =
        parsePlaybackProfileGpuPreviewProcessingBackend(
            parser.value(gpuPreviewProcessingOpt),
            &gpuPreviewProcessingOk);
    if (!gpuPreviewProcessingOk)
    {
        err << "[PROFILE] ERROR: --gpu-preview-processing must be one of auto, cpu, gpu.\n";
        return 2;
    }

    bool gpuBilinearDebayerOk = false;
    const GpuBilinearDebayerBackendRequest gpuBilinearDebayerBackend =
        parsePlaybackProfileGpuBilinearDebayerBackend(
            parser.value(gpuBilinearDebayerOpt),
            &gpuBilinearDebayerOk);
    if (!gpuBilinearDebayerOk)
    {
        err << "[PROFILE] ERROR: --gpu-bilinear-debayer must be one of auto, cpu, gpu.\n";
        return 2;
    }

    bool gpuAmazeDebayerOk = false;
    const GpuAmazeDebayerBackendRequest gpuAmazeDebayerBackend =
        parsePlaybackProfileGpuAmazeDebayerBackend(
            parser.value(gpuAmazeDebayerOpt),
            &gpuAmazeDebayerOk);
    if (!gpuAmazeDebayerOk)
    {
        err << "[PROFILE] ERROR: --gpu-amaze-debayer must be one of auto, cpu, gpu.\n";
        return 2;
    }
    if (gpuAmazeDebayerBackend == GpuAmazeDebayerBackendRequest::Gpu)
    {
        if (gpuPreviewProcessingBackend == GpuPreviewProcessingBackendRequest::Cpu)
        {
            err << "[PROFILE] ERROR: --gpu-amaze-debayer gpu requires --gpu-preview-processing gpu or auto.\n";
            return 2;
        }
        gpuPreviewProcessingBackend = GpuPreviewProcessingBackendRequest::Gpu;
    }

    if (autoThreads)
    {
        qunsetenv("MLVAPP_FORCE_THREADS");
    }
    else
    {
        qputenv("MLVAPP_FORCE_THREADS", QByteArray::number(forcedThreads));
    }
    if (parser.isSet(gpuViewportOpt)
        || gpuPreviewProcessingBackend == GpuPreviewProcessingBackendRequest::Gpu)
    {
        qputenv("MLVAPP_EXPERIMENTAL_GL_VIEWPORT", QByteArrayLiteral("1"));
    }
    if (gpuBilinearDebayerBackend == GpuBilinearDebayerBackendRequest::Gpu)
    {
        qputenv("MLVAPP_EXPERIMENTAL_GL_VIEWPORT", QByteArrayLiteral("1"));
    }
    if (gpuAmazeDebayerBackend == GpuAmazeDebayerBackendRequest::Gpu)
    {
        qputenv("MLVAPP_EXPERIMENTAL_GL_VIEWPORT", QByteArrayLiteral("1"));
    }

    const int exerciseScaleToggleFrom = parser.value(exerciseScaleFactorToggleFromOpt).toInt(&ok);
    if (!ok || (exerciseScaleToggleFrom != 2 && exerciseScaleToggleFrom != 4 && exerciseScaleToggleFrom != 8))
    {
        err << "[PROFILE] ERROR: --exercise-scale-toggle-from must be 2, 4, or 8.\n";
        return 2;
    }

    const QString inputPath = QFileInfo(parser.value(inputOpt)).absoluteFilePath();
    const QString outputPath = QFileInfo(parser.value(outputOpt)).absoluteFilePath();
    const QString receiptPath = parser.value(receiptOpt).isEmpty()
        ? QString()
        : QFileInfo(parser.value(receiptOpt)).absoluteFilePath();
    const QString stageLogPath = parser.value(stageLogOpt).isEmpty()
        ? QString()
        : QFileInfo(parser.value(stageLogOpt)).absoluteFilePath();

    if (parser.isSet(stageLogOpt))
    {
        qputenv("MLVAPP_STAGE_TIMING", QByteArrayLiteral("1"));
        qputenv("MLVAPP_STAGE_TIMING_FILE", QDir::toNativeSeparators(stageLogPath).toLocal8Bit());
    }

    MainWindow::PlaybackProfileOptions options;
    options.inputPath = inputPath;
    options.receiptPath = receiptPath;
    options.outputPath = outputPath;
    options.startFrame = startFrame;
    options.frameCount = frameCount;
    options.frameStep = frameStep;
    options.workerThreads = forcedThreads;
    options.forceWorkerThreads = !autoThreads;
    options.rawCacheMB = rawCacheMB;
    options.cacheCpuCores = cacheCpuCores;
    options.zebras = parser.isSet(zebrasOpt);
    options.fastOpen = parser.isSet(fastOpenOpt);
    options.showWindow = parser.isSet(showWindowOpt) || parser.isSet(waitForPaintOpt);
    options.waitForPaint = parser.isSet(waitForPaintOpt);
    options.exercisePlayAction = parser.isSet(exercisePlayActionOpt);
    options.exerciseLookAssistSettle = parser.isSet(exerciseLookAssistSettleOpt);
    options.exerciseLookAssistToggle = parser.isSet(exerciseLookAssistToggleOpt);
    options.exerciseScaleFactorToggle = parser.isSet(exerciseScaleFactorToggleOpt);
    options.exerciseScaleFactorToggleFrom = exerciseScaleToggleFrom;
    options.scope = scope;
    options.playbackDebayer = playbackDebayer;
    options.playbackProcessing = playbackProcessing;
    options.gpuPreviewProcessingBackend = gpuPreviewProcessingBackend;
    options.gpuBilinearDebayerBackend = gpuBilinearDebayerBackend;
    options.gpuAmazeDebayerBackend = gpuAmazeDebayerBackend;

    QByteArray appName = QCoreApplication::applicationFilePath().toLocal8Bit();
    char *profileArgv[] = { appName.data(), nullptr };
    int profileArgc = 1;

    MainWindow window(profileArgc, profileArgv);
    window.hide();
    return window.runHeadlessPlaybackProfile(options);
}

static int runGuiPlaybackSmoke(QApplication &app)
{
    QCommandLineParser parser;
    parser.setApplicationDescription(
        QStringLiteral("MLVApp visible GUI playback smoke test - opens a clip, lets the normal GUI settle, then toggles the real Play action."));

    QCommandLineOption helpOpt(
        QStringList() << QStringLiteral("h") << QStringLiteral("help"),
        QStringLiteral("Show this help text and exit."));
    parser.addOption(helpOpt);

    parser.addOption(QCommandLineOption(
        QStringLiteral("gui-smoke-playback"),
        QStringLiteral("Run in visible GUI playback smoke mode.")));

    const QCommandLineOption inputOpt(
        QStringList() << QStringLiteral("i") << QStringLiteral("input"),
        QStringLiteral("Input MLV file path."),
        QStringLiteral("path"));
    parser.addOption(inputOpt);

    const QCommandLineOption receiptOpt(
        QStringList() << QStringLiteral("r") << QStringLiteral("receipt"),
        QStringLiteral("Optional .marxml receipt to apply before playback."),
        QStringLiteral("file"));
    parser.addOption(receiptOpt);

    const QCommandLineOption secondsOpt(
        QStringLiteral("seconds"),
        QStringLiteral("Seconds to play the clip after settling."),
        QStringLiteral("seconds"),
        QStringLiteral("8"));
    parser.addOption(secondsOpt);

    const QCommandLineOption startFrameOpt(
        QStringLiteral("start-frame"),
        QStringLiteral("Zero-based first frame to seek before GUI smoke playback."),
        QStringLiteral("frame"),
        QStringLiteral("0"));
    parser.addOption(startFrameOpt);

    const QCommandLineOption presentedFramesOpt(
        QStringLiteral("presented-frames"),
        QStringLiteral("Stop after exactly this many fresh presented frames; --seconds remains a fail-closed timeout. Zero keeps time-based behavior."),
        QStringLiteral("count"),
        QStringLiteral("0"));
    parser.addOption(presentedFramesOpt);

    const QCommandLineOption dropFrameModeOpt(
        QStringLiteral("drop-frame-mode"),
        QStringLiteral("Force timeline pacing during GUI smoke playback: persisted, on, or off."),
        QStringLiteral("mode"),
        QStringLiteral("persisted"));
    parser.addOption(dropFrameModeOpt);

    const QCommandLineOption settleOpt(
        QStringLiteral("settle-ms"),
        QStringLiteral("Milliseconds to let the GUI settle after opening the clip and before pressing Play."),
        QStringLiteral("milliseconds"),
        QStringLiteral("2500"));
    parser.addOption(settleOpt);

    const QCommandLineOption settleCpuOpt(
        QStringLiteral("settle-cpu-percent"),
        QStringLiteral("After --settle-ms, keep waiting until MLVApp CPU stays at or below this percent of total system CPU. Use a negative value to disable."),
        QStringLiteral("percent"),
        QStringLiteral("10"));
    parser.addOption(settleCpuOpt);

    const QCommandLineOption settleCpuStableOpt(
        QStringLiteral("settle-cpu-stable-ms"),
        QStringLiteral("CPU-settle duration required after --settle-ms."),
        QStringLiteral("milliseconds"),
        QStringLiteral("1000"));
    parser.addOption(settleCpuStableOpt);

    const QCommandLineOption settleCpuMaxOpt(
        QStringLiteral("settle-cpu-max-ms"),
        QStringLiteral("Maximum total settle wait before playback starts."),
        QStringLiteral("milliseconds"),
        QStringLiteral("45000"));
    parser.addOption(settleCpuMaxOpt);

    const QCommandLineOption screenshotOutputOpt(
        QStringLiteral("screenshot-output"),
        QStringLiteral("Optional PNG path for an app-internal presented playback frame screenshot."),
        QStringLiteral("path"));
    parser.addOption(screenshotOutputOpt);

    const QCommandLineOption windowScreenshotOutputOpt(
        QStringLiteral("window-screenshot-output"),
        QStringLiteral("Optional PNG path for a full GUI window screenshot including the status bar."),
        QStringLiteral("path"));
    parser.addOption(windowScreenshotOutputOpt);

    const QCommandLineOption contactSheetDirOpt(
        QStringLiteral("contact-sheet-dir"),
        QStringLiteral("Optional directory for a contact-sheet capture pass: N evenly spaced presented frames (PNG + JSON sidecar each), grabbed in an un-timed pass after the measured playback interval. Requires --contact-sheet-frames."),
        QStringLiteral("dir"));
    parser.addOption(contactSheetDirOpt);

    const QCommandLineOption contactSheetFramesOpt(
        QStringLiteral("contact-sheet-frames"),
        QStringLiteral("Number of evenly spaced frames to grab for --contact-sheet-dir. Requires --contact-sheet-dir."),
        QStringLiteral("count"),
        QStringLiteral("0"));
    parser.addOption(contactSheetFramesOpt);

    const QCommandLineOption contactSheetSeekModeOpt(
        QStringLiteral("contact-sheet-seek-mode"),
        QStringLiteral("Explicit, labelled alternative to the default contact-sheet capture pass: grabs frames by pausing/seeking after playback stops instead of during a genuine second playback pass. A seeked frame is rendered by a different, non-playback path and can show a different look; sidecars from this mode record playback_path=false. Default off."));
    parser.addOption(contactSheetSeekModeOpt);

    const QCommandLineOption scopeOpt(
        QStringLiteral("scope"),
        QStringLiteral("Force a live scope during the smoke: none, histogram, waveform, parade, vectorscope. If omitted, the user's persisted GUI state is used."),
        QStringLiteral("mode"));
    parser.addOption(scopeOpt);

    const QCommandLineOption playbackDebayerOpt(
        QStringLiteral("playback-debayer"),
        QStringLiteral("Force playback debayer during the smoke: auto, receipt, none, simple, bilinear, lmmse, igv, amaze, ahd, rcd, dcb, amaze-cached. If omitted, the user's persisted GUI state is used."),
        QStringLiteral("mode"),
        QStringLiteral("auto"));
    parser.addOption(playbackDebayerOpt);

    const QCommandLineOption playbackProcessingOpt(
        QStringLiteral("playback-processing"),
        QStringLiteral("Force playback processing during the smoke: receipt or subset. Defaults to subset to match the existing GUI smoke lane."),
        QStringLiteral("mode"),
        QStringLiteral("subset"));
    parser.addOption(playbackProcessingOpt);

    const QCommandLineOption gpuViewportOpt(
        QStringLiteral("gpu-viewport"),
        QStringLiteral("Enable the experimental OpenGL viewport path during the smoke."));
    parser.addOption(gpuViewportOpt);

    const QCommandLineOption gpuPreviewProcessingOpt(
        QStringLiteral("gpu-preview-processing"),
        QStringLiteral("Preview-processing backend selection for the 16-bit GPU viewport path: auto, cpu, gpu. Selecting gpu implies --gpu-viewport."),
        QStringLiteral("backend"),
        QStringLiteral("auto"));
    parser.addOption(gpuPreviewProcessingOpt);

    const QCommandLineOption gpuBilinearDebayerOpt(
        QStringLiteral("gpu-bilinear-debayer"),
        QStringLiteral("Experimental bilinear debayer backend selection for the GPU preview-processing path: auto, cpu, gpu. Selecting gpu implies --gpu-viewport."),
        QStringLiteral("backend"),
        QStringLiteral("auto"));
    parser.addOption(gpuBilinearDebayerOpt);

    const QCommandLineOption gpuAmazeDebayerOpt(
        QStringLiteral("gpu-amaze-debayer"),
        QStringLiteral("Experimental AMaZE debayer backend selection for the GPU preview-processing path: auto, cpu, gpu. Selecting gpu implies --gpu-viewport and --gpu-preview-processing gpu."),
        QStringLiteral("backend"),
        QStringLiteral("auto"));
    parser.addOption(gpuAmazeDebayerOpt);

    const QCommandLineOption gpuAmazeTexturePresentOpt(
        QStringLiteral("gpu-amaze-texture-present"),
        QStringLiteral("Request experimental AMaZE CUDA-to-GL texture presentation during the smoke. Requires the GPU AMaZE selector chain to become active."));
    parser.addOption(gpuAmazeTexturePresentOpt);

    const QCommandLineOption noLookAssistOpt(
        QStringLiteral("no-look-assist"),
        QStringLiteral("Disable Look Assist during this GUI smoke run."));
    parser.addOption(noLookAssistOpt);

    const QCommandLineOption loopPlaybackOpt(
        QStringLiteral("loop"),
        QStringLiteral("Loop the clip so a short clip plays continuously for the whole --seconds window (default: play once then stop)."));
    parser.addOption(loopPlaybackOpt);

    const QCommandLineOption exerciseClipLifecycleStressOpt(
        QStringLiteral("exercise-clip-lifecycle-stress"),
        QStringLiteral("During GUI smoke playback, seek, switch clips, close/unload, and reopen to validate clip lifecycle handoff."));
    parser.addOption(exerciseClipLifecycleStressOpt);

    const QCommandLineOption stressSwitchInputOpt(
        QStringLiteral("stress-switch-input"),
        QStringLiteral("Second MLV file path used by --exercise-clip-lifecycle-stress. Defaults to --input."),
        QStringLiteral("path"));
    parser.addOption(stressSwitchInputOpt);

    const QCommandLineOption stressSwitchAtMsOpt(
        QStringLiteral("stress-switch-at-ms"),
        QStringLiteral("Elapsed playback milliseconds before the lifecycle stress sequence starts."),
        QStringLiteral("milliseconds"),
        QStringLiteral("1000"));
    parser.addOption(stressSwitchAtMsOpt);

    const QCommandLineOption stressSeekFrameOpt(
        QStringLiteral("stress-seek-frame"),
        QStringLiteral("Frame requested during each lifecycle stress seek step."),
        QStringLiteral("frame"),
        QStringLiteral("8"));
    parser.addOption(stressSeekFrameOpt);

    const QCommandLineOption enablePhase3QualityModesOpt(
        QStringLiteral("enable-phase3-quality-modes"),
        QStringLiteral("Allow unattended Phase 3 quality-mode selection during this GUI smoke run."));
    parser.addOption(enablePhase3QualityModesOpt);

    const QCommandLineOption zebrasOpt(
        QStringLiteral("zebras"),
        QStringLiteral("Force zebra overlay on during the smoke."));
    parser.addOption(zebrasOpt);

    const QCommandLineOption noZebrasOpt(
        QStringLiteral("no-zebras"),
        QStringLiteral("Force zebra overlay off during the smoke."));
    parser.addOption(noZebrasOpt);

    const QCommandLineOption stageLogOpt(
        QStringLiteral("stage-log"),
        QStringLiteral("Optional stage timing log path. Also enables MLVAPP_STAGE_TIMING."),
        QStringLiteral("file"));
    parser.addOption(stageLogOpt);

    parser.process(app);

    QTextStream out(stdout);
    QTextStream err(stderr);

    if (parser.isSet(helpOpt))
    {
        out << parser.helpText() << "\n";
        return 0;
    }

    if (!parser.isSet(inputOpt))
    {
        err << "[GUI-SMOKE] ERROR: --input is required.\n\n";
        err << parser.helpText() << "\n";
        return 2;
    }

    bool ok = false;
    const double seconds = parser.value(secondsOpt).toDouble(&ok);
    if (!ok || seconds <= 0.0)
    {
        err << "[GUI-SMOKE] ERROR: --seconds must be greater than 0.\n";
        return 2;
    }

    const int settleMs = parser.value(settleOpt).toInt(&ok);
    if (!ok || settleMs < 0)
    {
        err << "[GUI-SMOKE] ERROR: --settle-ms must be 0 or greater.\n";
        return 2;
    }

    const int startFrame = parser.value(startFrameOpt).toInt(&ok);
    if (!ok || startFrame < 0)
    {
        err << "[GUI-SMOKE] ERROR: --start-frame must be 0 or greater.\n";
        return 2;
    }

    const int targetPresentedFrames =
        parser.value(presentedFramesOpt).toInt(&ok);
    if (!ok || targetPresentedFrames < 0)
    {
        err << "[GUI-SMOKE] ERROR: --presented-frames must be 0 or greater.\n";
        return 2;
    }

    const int contactSheetFrames = parser.value(contactSheetFramesOpt).toInt(&ok);
    if (!ok || contactSheetFrames < 0)
    {
        err << "[GUI-SMOKE] ERROR: --contact-sheet-frames must be 0 or greater.\n";
        return 2;
    }
    const bool contactSheetDirSet =
        parser.isSet(contactSheetDirOpt) && !parser.value(contactSheetDirOpt).isEmpty();
    if (contactSheetDirSet != (contactSheetFrames > 0))
    {
        err << "[GUI-SMOKE] ERROR: --contact-sheet-dir and --contact-sheet-frames (> 0) must be set together.\n";
        return 2;
    }

    const QString dropFrameMode =
        parser.value(dropFrameModeOpt).trimmed().toLower();
    if (dropFrameMode != QStringLiteral("persisted")
     && dropFrameMode != QStringLiteral("on")
     && dropFrameMode != QStringLiteral("off"))
    {
        err << "[GUI-SMOKE] ERROR: --drop-frame-mode must be one of persisted, on, off.\n";
        return 2;
    }

    const double settleCpuPercent = parser.value(settleCpuOpt).toDouble(&ok);
    if (!ok)
    {
        err << "[GUI-SMOKE] ERROR: --settle-cpu-percent must be numeric.\n";
        return 2;
    }

    const int settleCpuStableMs = parser.value(settleCpuStableOpt).toInt(&ok);
    if (!ok || settleCpuStableMs < 0)
    {
        err << "[GUI-SMOKE] ERROR: --settle-cpu-stable-ms must be 0 or greater.\n";
        return 2;
    }

    const int settleCpuMaxMs = parser.value(settleCpuMaxOpt).toInt(&ok);
    if (!ok || settleCpuMaxMs < settleMs)
    {
        err << "[GUI-SMOKE] ERROR: --settle-cpu-max-ms must be at least --settle-ms.\n";
        return 2;
    }

    const int stressSwitchAtMs = parser.value(stressSwitchAtMsOpt).toInt(&ok);
    if (!ok || stressSwitchAtMs < 0)
    {
        err << "[GUI-SMOKE] ERROR: --stress-switch-at-ms must be 0 or greater.\n";
        return 2;
    }

    const int stressSeekFrame = parser.value(stressSeekFrameOpt).toInt(&ok);
    if (!ok || stressSeekFrame < 0)
    {
        err << "[GUI-SMOKE] ERROR: --stress-seek-frame must be 0 or greater.\n";
        return 2;
    }

    MainWindow::PlaybackProfileScope scope = MainWindow::PlaybackProfileScope::None;
    bool scopeOk = true;
    if (parser.isSet(scopeOpt))
    {
        scope = parsePlaybackProfileScope(parser.value(scopeOpt), &scopeOk);
    }
    if (!scopeOk)
    {
        err << "[GUI-SMOKE] ERROR: --scope must be one of none, histogram, waveform, parade, vectorscope.\n";
        return 2;
    }

    bool playbackDebayerOk = false;
    const MainWindow::PlaybackProfileDebayerRequest playbackDebayer =
        parsePlaybackProfileDebayerRequest(
            parser.value(playbackDebayerOpt),
            &playbackDebayerOk);
    if (!playbackDebayerOk)
    {
        err << "[GUI-SMOKE] ERROR: --playback-debayer must be one of auto, receipt, none, simple, bilinear, lmmse, igv, amaze, ahd, rcd, dcb, amaze-cached.\n";
        return 2;
    }

    bool playbackProcessingOk = false;
    const MainWindow::PlaybackProfileProcessingRequest playbackProcessing =
        parsePlaybackProfileProcessingRequest(
            parser.value(playbackProcessingOpt),
            &playbackProcessingOk);
    if (!playbackProcessingOk)
    {
        err << "[GUI-SMOKE] ERROR: --playback-processing must be one of auto, receipt, subset.\n";
        return 2;
    }

    bool gpuPreviewProcessingOk = false;
    GpuPreviewProcessingBackendRequest gpuPreviewProcessingBackend =
        parsePlaybackProfileGpuPreviewProcessingBackend(
            parser.value(gpuPreviewProcessingOpt),
            &gpuPreviewProcessingOk);
    if (!gpuPreviewProcessingOk)
    {
        err << "[GUI-SMOKE] ERROR: --gpu-preview-processing must be one of auto, cpu, gpu.\n";
        return 2;
    }

    bool gpuBilinearDebayerOk = false;
    const GpuBilinearDebayerBackendRequest gpuBilinearDebayerBackend =
        parsePlaybackProfileGpuBilinearDebayerBackend(
            parser.value(gpuBilinearDebayerOpt),
            &gpuBilinearDebayerOk);
    if (!gpuBilinearDebayerOk)
    {
        err << "[GUI-SMOKE] ERROR: --gpu-bilinear-debayer must be one of auto, cpu, gpu.\n";
        return 2;
    }

    bool gpuAmazeDebayerOk = false;
    const GpuAmazeDebayerBackendRequest gpuAmazeDebayerBackend =
        parsePlaybackProfileGpuAmazeDebayerBackend(
            parser.value(gpuAmazeDebayerOpt),
            &gpuAmazeDebayerOk);
    if (!gpuAmazeDebayerOk)
    {
        err << "[GUI-SMOKE] ERROR: --gpu-amaze-debayer must be one of auto, cpu, gpu.\n";
        return 2;
    }
    if (gpuAmazeDebayerBackend == GpuAmazeDebayerBackendRequest::Gpu)
    {
        if (gpuPreviewProcessingBackend == GpuPreviewProcessingBackendRequest::Cpu)
        {
            err << "[GUI-SMOKE] ERROR: --gpu-amaze-debayer gpu requires --gpu-preview-processing gpu or auto.\n";
            return 2;
        }
        gpuPreviewProcessingBackend = GpuPreviewProcessingBackendRequest::Gpu;
    }

    if (parser.isSet(gpuViewportOpt)
        || gpuPreviewProcessingBackend == GpuPreviewProcessingBackendRequest::Gpu
        || gpuBilinearDebayerBackend == GpuBilinearDebayerBackendRequest::Gpu
        || gpuAmazeDebayerBackend == GpuAmazeDebayerBackendRequest::Gpu)
    {
        qputenv("MLVAPP_EXPERIMENTAL_GL_VIEWPORT", QByteArrayLiteral("1"));
    }
    if (parser.isSet(gpuAmazeTexturePresentOpt))
    {
        qputenv("MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT", QByteArrayLiteral("1"));
    }
    if (parser.isSet(enablePhase3QualityModesOpt))
    {
        qputenv("MLVAPP_PLAYBACK_PHASE3_UNATTENDED", QByteArrayLiteral("1"));
    }

    const QString stageLogPath = parser.value(stageLogOpt).isEmpty()
        ? QString()
        : QFileInfo(parser.value(stageLogOpt)).absoluteFilePath();
    if (parser.isSet(stageLogOpt))
    {
        qputenv("MLVAPP_STAGE_TIMING", QByteArrayLiteral("1"));
        qputenv("MLVAPP_STAGE_TIMING_FILE", QDir::toNativeSeparators(stageLogPath).toLocal8Bit());
    }

    if (parser.isSet(zebrasOpt) && parser.isSet(noZebrasOpt))
    {
        err << "[GUI-SMOKE] ERROR: use only one of --zebras or --no-zebras.\n";
        return 2;
    }

    MainWindow::GuiPlaybackSmokeOptions options;
    options.inputPath = QFileInfo(parser.value(inputOpt)).absoluteFilePath();
    options.receiptPath = parser.value(receiptOpt).isEmpty()
        ? QString()
        : QFileInfo(parser.value(receiptOpt)).absoluteFilePath();
    options.startFrame = startFrame;
    options.durationMs = qRound(seconds * 1000.0);
    options.targetPresentedFrames = targetPresentedFrames;
    options.settleMs = settleMs;
    options.settleCpuPercent = settleCpuPercent;
    options.settleCpuStableMs = settleCpuStableMs;
    options.settleCpuMaxMs = settleCpuMaxMs;
    options.screenshotOutputPath = parser.value(screenshotOutputOpt).isEmpty()
        ? QString()
        : QFileInfo(parser.value(screenshotOutputOpt)).absoluteFilePath();
    options.windowScreenshotOutputPath = parser.value(windowScreenshotOutputOpt).isEmpty()
        ? QString()
        : QFileInfo(parser.value(windowScreenshotOutputOpt)).absoluteFilePath();
    options.contactSheetDir = contactSheetDirSet
        ? QFileInfo(parser.value(contactSheetDirOpt)).absoluteFilePath()
        : QString();
    options.contactSheetFrames = contactSheetFrames;
    options.contactSheetSeekMode = parser.isSet(contactSheetSeekModeOpt);
    options.scope = scope;
    options.playbackDebayer = playbackDebayer;
    options.playbackProcessing = playbackProcessing;
    options.gpuPreviewProcessingBackend = gpuPreviewProcessingBackend;
    options.gpuBilinearDebayerBackend = gpuBilinearDebayerBackend;
    options.gpuAmazeDebayerBackend = gpuAmazeDebayerBackend;
    options.forceScope = parser.isSet(scopeOpt);
    options.forcePlaybackDebayer = parser.isSet(playbackDebayerOpt);
    options.disableLookAssist = parser.isSet(noLookAssistOpt);
    options.loopPlayback = parser.isSet(loopPlaybackOpt);
    options.zebras = parser.isSet(zebrasOpt);
    options.forceZebras = parser.isSet(zebrasOpt) || parser.isSet(noZebrasOpt);
    options.dropFrame = dropFrameMode == QStringLiteral("on");
    options.forceDropFrame = dropFrameMode != QStringLiteral("persisted");
    options.exerciseClipLifecycleStress =
        parser.isSet(exerciseClipLifecycleStressOpt);
    options.stressSwitchInputPath = parser.value(stressSwitchInputOpt).isEmpty()
        ? options.inputPath
        : QFileInfo(parser.value(stressSwitchInputOpt)).absoluteFilePath();
    options.stressSwitchAtMs = stressSwitchAtMs;
    options.stressSeekFrame = stressSeekFrame;

    QByteArray appName = QCoreApplication::applicationFilePath().toLocal8Bit();
    char *smokeArgv[] = { appName.data(), nullptr };
    int smokeArgc = 1;

    MainWindow window(smokeArgc, smokeArgv);
    return window.runGuiPlaybackSmoke(options);
}

int main(int argc, char *argv[])
{
    /* Crash forensics initialization runs BEFORE QApplication so the
     * qInstallMessageHandler file sink and (on Windows) minidump filter
     * are ready to capture any early failures.  Version/organization
     * names are set here so QStandardPaths::AppDataLocation resolves
     * correctly before CrashForensics::install() queries it. */
    QCoreApplication::setOrganizationName(QStringLiteral("magiclantern"));
    QCoreApplication::setApplicationName(QStringLiteral("MLVApp"));
    QCoreApplication::setApplicationVersion(
        QStringLiteral("%1.%2.%3.%4")
            .arg(VERSION_MAJOR).arg(VERSION_MINOR)
            .arg(VERSION_PATCH).arg(VERSION_BUILD));
    CrashForensics::install(argc, argv);
    CrashForensics::logStartupMetadata();
    phase3InitKillSwitches();
    mlvapp_force_singlethread_init();

    bool batch = hasBatchFlag(argc, argv);
    bool trim_mlv = hasTrimMlvFlag(argc, argv);
    bool profile_playback = hasPlaybackProfileFlag(argc, argv);
    bool gui_playback_smoke = hasGuiPlaybackSmokeFlag(argc, argv);
    const bool normal_gui =
        !batch && !trim_mlv && !profile_playback && !gui_playback_smoke;

    if (normal_gui && CrashForensics::cudaPlaybackProfilingSettingsEnabled())
    {
        CrashForensics::applyCudaPlaybackProfilingEnvironment(true);
    }

    // Bug A on-screen present A/B (software-GL diagnostic): AA_UseSoftwareOpenGL loads
    // llvmpipe, removing the dGPU cross-adapter present entirely -- a decisive test of
    // whether that present is the cause (if the panel shows under software GL, the
    // cross-adapter present is the culprit). Mutually exclusive with QT_OPENGL=desktop,
    // so when requested we skip the desktop-GL env. Needs opengl32sw.dll next to exe.
    const bool abSoftwareGl = viewportAbEnvFlag("MLVAPP_VIEWPORT_AB_SOFTWARE");
    if (abSoftwareGl)
    {
        QCoreApplication::setAttribute(Qt::AA_UseSoftwareOpenGL);
        qInfo() << "viewport_ab: AA_UseSoftwareOpenGL (llvmpipe) diagnostic present path"
                << "(QT_OPENGL=desktop skipped).";
    }
    else if (shouldPreferDesktopOpenGl(argc, argv, batch, trim_mlv, profile_playback))
    {
        qputenv("QT_OPENGL", QByteArrayLiteral("desktop"));
    }
    // Bug A on-screen present A/B (V3): probe whether shared GL contexts break the
    // Optimus cross-adapter present by skipping the attribute when requested.
    const bool abNoShareContexts = viewportAbEnvFlag("MLVAPP_VIEWPORT_AB_NOSHARE");
    const bool sharedOpenGlContexts =
        !abNoShareContexts && shouldShareOpenGlContexts(argc, argv, batch, trim_mlv);
    if (sharedOpenGlContexts)
    {
        QCoreApplication::setAttribute(Qt::AA_ShareOpenGLContexts);
    }
    // Bug A on-screen present A/B: set the GLOBAL default surface format BEFORE the
    // QApplication so the GL context AND the AA_ShareOpenGLContexts share context are
    // created from it (per-widget setFormat after pixel-format selection is too late).
    // ALPHA (MLVAPP_VIEWPORT_AB_ALPHA) is the highest-probability fix: Qt requires
    // backing-store alpha or a QOpenGLWidget's content "will not be visible" -- the
    // exact correct-FBO-but-black symptom. DEFAULTFORMAT is the no-alpha control.
    const bool abAlpha = viewportAbEnvFlag("MLVAPP_VIEWPORT_AB_ALPHA");
    if (abAlpha || viewportAbEnvFlag("MLVAPP_VIEWPORT_AB_DEFAULTFORMAT"))
    {
        QSurfaceFormat abFormat = QSurfaceFormat::defaultFormat();
        abFormat.setSwapInterval(0);
        const QByteArray swap = qgetenv("MLVAPP_VIEWPORT_AB_SWAP").trimmed().toLower();
        if (swap == "single") abFormat.setSwapBehavior(QSurfaceFormat::SingleBuffer);
        else if (swap == "triple") abFormat.setSwapBehavior(QSurfaceFormat::TripleBuffer);
        else if (swap == "double" || abAlpha) abFormat.setSwapBehavior(QSurfaceFormat::DoubleBuffer);
        if (abAlpha)
        {
            abFormat.setAlphaBufferSize(8);
            abFormat.setDepthBufferSize(24);
            abFormat.setStencilBufferSize(8);
        }
        QSurfaceFormat::setDefaultFormat(abFormat);
        qInfo().nospace() << "viewport_ab: global default QSurfaceFormat set before "
                             "QApplication (alpha=" << (abAlpha ? 1 : 0)
                          << ", swap=" << swap.constData() << ", swapInterval 0).";
    }
    if (abNoShareContexts)
    {
        qInfo() << "viewport_ab: AA_ShareOpenGLContexts skipped by request.";
    }

    MyApplication a(argc, argv);
    if (sharedOpenGlContexts)
    {
        qInfo() << "GPU OpenGL context sharing enabled for opt-in GPU presentation paths.";
    }
#if QT_VERSION < QT_VERSION_CHECK(6, 0, 0)
    a.setAttribute(Qt::AA_UseHighDpiPixmaps);
#endif
#ifdef Q_OS_WIN
    a.setAttribute(Qt::AA_Use96Dpi);
#endif

    if (normal_gui && CrashForensics::dngAsyncCompressionProfilingSettingsEnabled())
    {
        CrashForensics::applyDngAsyncCompressionProfilingEnvironment(
            true,
            CrashForensics::dngAsyncCompressionQueueDepthSettingsValue(),
            CrashForensics::dngAsyncCompressionThreadCountSettingsValue());
    }

    const bool perfFieldLogRequested =
        envFlagEnabled("MLVAPP_PERF_FIELD_LOG")
        || (normal_gui && CrashForensics::performanceFieldLogSettingsEnabled());
    if (perfFieldLogRequested)
    {
        CrashForensics::applyPerformanceFieldLogEnvironment(true);
    }
    if (profile_playback
        || gui_playback_smoke
        || envFlagEnabled("MLVAPP_EXPORT_STAGE_PROFILER")
        || perfFieldLogRequested)
    {
        CrashForensics::publishMachineFingerprintEnvironment();
    }

    if (batch)
    {
        /* Batch mode — no GUI window, but QApplication stays alive
         * because internal export code may touch widgets/fonts. */
        a.setQuitOnLastWindowClosed(false);
        return runBatch(a);
    }

    if (trim_mlv)
    {
        a.setQuitOnLastWindowClosed(false);
        return MlvTrim::run(a);
    }

    if (profile_playback)
    {
        a.setQuitOnLastWindowClosed(false);
        return runPlaybackProfile(a);
    }

    if (gui_playback_smoke)
    {
        a.setQuitOnLastWindowClosed(false);
        return runGuiPlaybackSmoke(a);
    }

    /* Normal GUI mode — unchanged */
    MainWindow w(argc, argv);
    w.show();

    return a.exec();
}
