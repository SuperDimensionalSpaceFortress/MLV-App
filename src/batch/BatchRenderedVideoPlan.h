#ifndef BATCHRENDEREDVIDEOPLAN_H
#define BATCHRENDEREDVIDEOPLAN_H

#include "BatchTypes.h"

#include <QString>
#include <QStringList>

#include <functional>

enum class BatchRenderedVideoCodec : int
{
    Unspecified = 0,
    H264 = 1,
    H265 = 2,
    ProRes = 3
};

enum class BatchRenderedVideoContainer : int
{
    Unspecified = 0,
    Mov = 1,
    Mp4 = 2,
    Mkv = 3
};

struct BatchExportFormatRequest
{
    BatchExportFormat format = BatchExportFormat::Cdng;
    BatchRenderedVideoCodec renderedCodec = BatchRenderedVideoCodec::Unspecified;
    BatchRenderedVideoContainer renderedContainer = BatchRenderedVideoContainer::Unspecified;
};

struct BatchRenderedVideoTarget
{
    BatchRenderedVideoCodec codec = BatchRenderedVideoCodec::Unspecified;
    BatchRenderedVideoContainer container = BatchRenderedVideoContainer::Unspecified;
    QString extension;
    bool complete = false;
};

enum class BatchRenderedVideoEncoderProfile : int
{
    Unspecified = 0,
    H264 = 1,
    H265_8 = 2,
    ProRes422HQ = 3
};

enum class BatchRenderedVideoEncoderOption : int
{
    Unspecified = 0,
    H264HighMov = 1,
    H264HighMp4 = 2,
    H264HighMkv = 3,
    H265HighMov = 4,
    H265HighMp4 = 5,
    H265HighMkv = 6,
    ProResFfmpegKostya = 7
};

struct BatchRenderedVideoEncoderPreset
{
    BatchRenderedVideoEncoderProfile profile =
        BatchRenderedVideoEncoderProfile::Unspecified;
    BatchRenderedVideoEncoderOption option =
        BatchRenderedVideoEncoderOption::Unspecified;
    int guiCodecProfile = -1;
    int guiCodecOption = -1;
    QString extension;
    bool ready = false;
};

struct BatchRenderedVideoFfmpegVideoPlan
{
    QString encoder;
    QString preset;
    QString qualityFlag;
    int qualityValue = -1;
    QString pixelFormat;
    QString videoTag;
    QString videoArguments;
    QString reason;
    bool ready = false;
};

struct BatchRenderedVideoFfmpegFilterPlan
{
    QString source = QStringLiteral("gui-base-color-scale");
    QString colorScaleFilter;
    QString filterArguments;
    QString reason;
    bool baseColorScaleReady = false;
    bool optionalFiltersOwned = false;
    bool moireeFilterOwned = false;
    bool hdrBlendOwned = false;
    bool stabilizationOwned = false;
    bool ready = false;
};

struct BatchRenderedVideoOptionalFilterPlan
{
    QString source = QStringLiteral("optional-filter-ownership-contract");
    QString baseFilterArguments;
    QString reason;
    bool baseFilterContractReady = false;
    bool optionalFiltersRequested = false;
    bool optionalFilterGraphOwned = false;
    bool moireeFilterOwned = false;
    bool hdrBlendOwned = false;
    bool stabilizationOwned = false;
    bool optionalFilterOrderOwned = false;
    bool optionalFilterParityOwned = false;
    bool optionalFilterExecutionReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoSourceAudioPlan
{
    QString source = QStringLiteral("video-only-undiscovered");
    QString clipPath;
    QString audioState = QStringLiteral("unknown");
    QString reason;
    int sampleRate = 0;
    int channels = 0;
    int bitsPerSample = 0;
    qulonglong audioBytes = 0;
    bool discoveryOwned = false;
    bool discoveryAttempted = false;
    bool sourceAudioKnown = false;
    bool sourceAudioPresent = false;
    bool extractionOwned = false;
    bool muxInputOwned = false;
    bool syncValidationOwned = false;
    bool videoOnlyFallbackReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoSourceAudioExtractionPlan
{
    QString source = QStringLiteral("source-audio-extraction-prerequisite-contract");
    QString clipPath;
    QString extractionFormat = QStringLiteral("wav-pcm-s16le");
    QString plannedAudioPath;
    QString reason;
    bool sourceAudioContractReady = false;
    bool sourceAudioKnown = false;
    bool sourceAudioPresent = false;
    bool sourceAudioDiscoveryOwned = false;
    bool extractionPathPlanned = false;
    bool extractionPathReady = false;
    bool sampleFormatReady = false;
    bool sampleRateReady = false;
    bool channelLayoutReady = false;
    bool extractionProcessOwned = false;
    bool tempFileOwned = false;
    bool cleanupOwned = false;
    bool extractionReady = false;
    bool videoOnlyFallbackReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoSourceAudioExtractionExecutionPlan
{
    QString source = QStringLiteral("source-audio-extraction-execution-contract");
    QString plannedAudioPath;
    QString extractionFormat = QStringLiteral("wav-pcm-s16le");
    QString reason;
    bool extractionPrerequisiteContractReady = false;
    bool sourceAudioKnown = false;
    bool sourceAudioPresent = false;
    bool extractionPathPlanned = false;
    bool extractionPathReady = false;
    bool sampleReadPlanned = false;
    bool wavWritePlanned = false;
    bool tempFileLifecyclePlanned = false;
    bool sampleReadOwned = false;
    bool wavHeaderWriteOwned = false;
    bool wavSampleWriteOwned = false;
    bool tempFileOpenOwned = false;
    bool tempFileFinalizeOwned = false;
    bool cleanupOwned = false;
    bool extractionProcessOwned = false;
    bool tempFileLifecycleReady = false;
    bool extractionExecutionReady = false;
    bool videoOnlyFallbackReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoFfmpegAudioInputHandoffPlan
{
    QString source = QStringLiteral("ffmpeg-audio-input-handoff-contract");
    QString plannedAudioPath;
    QString plannedInputArguments;
    QString activeAudioArguments = QStringLiteral("-an");
    QString reason;
    bool extractionExecutionContractReady = false;
    bool sourceAudioKnown = false;
    bool sourceAudioPresent = false;
    bool extractionPathPlanned = false;
    bool extractionPathReady = false;
    bool extractionExecutionReady = false;
    bool tempFileLifecycleReady = false;
    bool cleanupOwned = false;
    bool audioInputPlanned = false;
    bool inputArgumentsPlanned = false;
    bool extractionOutputOwned = false;
    bool audioInputOwnershipPlanned = false;
    bool audioInputArgumentHandoffPlanned = false;
    bool audioInputOwned = false;
    bool audioInputArgumentHandoffOwned = false;
    bool audioInputHandoffReady = false;
    bool videoOnlyFallbackReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoFfmpegAudioInputPlan
{
    QString source = QStringLiteral("ffmpeg-audio-input-contract");
    QString plannedAudioPath;
    QString plannedInputArguments;
    QString activeAudioArguments = QStringLiteral("-an");
    QString reason;
    bool sourceAudioExtractionContractReady = false;
    bool sourceAudioKnown = false;
    bool sourceAudioPresent = false;
    bool extractionPathPlanned = false;
    bool extractionPathReady = false;
    bool extractionReady = false;
    bool tempFileOwned = false;
    bool cleanupOwned = false;
    bool audioInputPlanned = false;
    bool audioInputOwned = false;
    bool audioInputReady = false;
    bool videoOnlyFallbackReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoAudioMuxPrerequisitesPlan
{
    QString source = QStringLiteral("audio-mux-prerequisite-contract");
    QString inputState = QStringLiteral("rendered-source-audio-discovery");
    QString outputState =
        QStringLiteral("ffmpeg-audio-input-or-video-only-fallback");
    QString reason;
    bool sourceAudioContractReady = false;
    bool sourceAudioExtractionContractReady = false;
    bool sourceAudioInputContractReady = false;
    bool sourceAudioDiscoveryOwned = false;
    bool sourceAudioExtractionOwned = false;
    bool audioInputOwned = false;
    bool audioMuxOwned = false;
    bool audioSyncValidationOwned = false;
    bool videoOnlyFallbackReady = false;
    bool muxReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoAudioMuxExecutionPlan
{
    QString source = QStringLiteral("audio-mux-execution-contract");
    QString inputState =
        QStringLiteral("ffmpeg-audio-input-or-video-only-fallback");
    QString outputState =
        QStringLiteral("synced-rendered-audio-or-video-only-fallback");
    QString reason;
    bool sourceAudioContractReady = false;
    bool audioInputHandoffContractReady = false;
    bool audioMuxPrerequisitesContractReady = false;
    bool sourceAudioKnown = false;
    bool sourceAudioPresent = false;
    bool sourceAudioDiscoveryOwned = false;
    bool sourceAudioExtractionOwned = false;
    bool audioInputHandoffReady = false;
    bool tempAudioInputOwned = false;
    bool audioMuxPlanned = false;
    bool audioMuxArgumentHandoffPlanned = false;
    bool audioSyncValidationPlanned = false;
    bool audioMuxOwned = false;
    bool audioMuxArgumentHandoffOwned = false;
    bool audioSyncValidationOwned = false;
    bool audioMuxReady = false;
    bool audioSyncValidationReady = false;
    bool muxExecutionReady = false;
    bool videoOnlyFallbackReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoFfmpegAudioPlan
{
    QString source = QStringLiteral("video-only-to-mux-transition-contract");
    QString audioArguments = QStringLiteral("-an");
    QString muxTransitionArguments;
    QString reason;
    bool videoOnlyCommandReady = false;
    bool sourceAudioDiscoveryOwned = false;
    bool sourceAudioExtractionOwned = false;
    bool audioInputContractReady = false;
    bool audioInputOwned = false;
    bool audioMuxExecutionContractReady = false;
    bool audioMuxPlanned = false;
    bool audioMuxArgumentHandoffPlanned = false;
    bool audioSyncValidationPlanned = false;
    bool audioMuxOwned = false;
    bool audioMuxArgumentHandoffOwned = false;
    bool audioSyncOwned = false;
    bool audioMuxExecutionReady = false;
    bool muxedAudioCommandPlanned = false;
    bool muxedAudioCommandReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoFfmpegFramePlan
{
    int sourceWidth = 0;
    int sourceHeight = 0;
    int outputWidth = 0;
    int outputHeight = 0;
    double frameRate = 0.0;
    QString frameRateArgument;
    QString frameSizeArgument;
    QString reason;
    bool resizeEnabled = false;
    bool resizeHeightLocked = false;
    bool stretchApplied = false;
    bool codecDimensionAdjusted = false;
    bool scaled = false;
    bool ready = false;
};

struct BatchRenderedVideoReceiptApplicationPlan
{
    QString source = QStringLiteral("receipt-application-input-output-contract");
    QString inputState = QStringLiteral("open-mlv-runtime-plus-batch-receipt");
    QString outputState = QStringLiteral("receipt-applied-mlv-processing-state");
    QString reason;
    bool sourceMetadataReady = false;
    bool frameGeometryReady = false;
    bool inputContractReady = false;
    bool outputContractReady = false;
    bool applyToMlvOwned = false;
    bool processingObjectMutationOwned = false;
    bool cacheInvalidationOwned = false;
    bool cutStretchStateOwned = false;
    bool lookAssistApplicationOwned = false;
    bool receiptValidationOwned = false;
    bool applicationReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoFrameProcessingPlan
{
    QString source = QStringLiteral("headless-rendered-frame-contract");
    QString rawFramePixelFormat = QStringLiteral("rgb48");
    QString outputSize;
    QString reason;
    bool sourceMetadataReady = false;
    bool frameGeometryReady = false;
    bool receiptApplicationContractReady = false;
    bool debayerContractReady = false;
    bool previewProcessingContractReady = false;
    bool resizeProcessingContractReady = false;
    bool rgb48FrameBufferContractReady = false;
    bool frameIterationContractReady = false;
    bool receiptApplicationOwned = false;
    bool debayerOwned = false;
    bool previewProcessingOwned = false;
    bool resizeProcessingOwned = false;
    bool rgb48FrameBufferOwned = false;
    bool frameIterationOwned = false;
    bool processingParityValidationOwned = false;
    bool processingParityReady = false;
    bool frameProcessingReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoFfmpegBinaryPlan
{
    QString source = QStringLiteral("default-executable-name");
    QString requestedExecutable = QStringLiteral("ffmpeg");
    QString resolvedExecutable = QStringLiteral("ffmpeg");
    QString reason;
    bool pathSearchOwned = false;
    bool pathSearchAttempted = false;
    bool foundOnPath = false;
    /* Default request only: the application directory is searched before PATH. A hit there
     * sets source "app-dir" and foundInAppDir, and leaves foundOnPath false. */
    bool appDirSearchAttempted = false;
    bool foundInAppDir = false;
    bool commandExecutableReady = true;
};

struct BatchRenderedVideoMediaProbeBinaryPlan
{
    QString source = QStringLiteral("default-executable-name");
    QString requestedExecutable = QStringLiteral("ffprobe");
    QString resolvedExecutable = QStringLiteral("ffprobe");
    QString reason;
    bool pathSearchOwned = false;
    bool pathSearchAttempted = false;
    bool foundOnPath = false;
    bool appDirSearchAttempted = false;
    bool foundInAppDir = false;
    bool commandExecutableReady = true;
};

/* Resolves an executable name. An EMPTY searchDirs list means "search PATH"; otherwise only
 * the given directories are searched (QStandardPaths::findExecutable semantics). Returns an
 * empty string when nothing is found. Injected so the lookup order is testable without the
 * real PATH or the real application directory. */
using BatchRenderedVideoExecutableResolver =
    std::function<QString(const QString & name, const QStringList & searchDirs)>;

struct BatchRenderedVideoMediaProbeCommandPlan
{
    QString source = QStringLiteral("output-verification-probe-command-contract");
    QString executable = QStringLiteral("ffprobe");
    QString expectedOutputPath;
    QString arguments;
    QString commandLine;
    QString reason;
    bool outputVerificationContractReady = false;
    bool mediaProbeExecutableReady = false;
    bool commandPlanned = false;
    bool commandReady = false;
    bool executionOwned = false;
    bool executionReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoMediaProbeJsonPlan
{
    QString source = QStringLiteral("output-verification-probe-json-contract");
    QString expectedOutputPath;
    QString commandLine;
    QString reason;
    bool mediaProbeCommandContractReady = false;
    bool mediaProbeCommandExecutionOwned = false;
    bool mediaProbeCommandExecutionReady = false;
    bool stdoutCapturePlanned = false;
    bool stderrCapturePlanned = false;
    bool exitCodeValidationPlanned = false;
    bool timeoutPlanned = false;
    bool errorReportingPlanned = false;
    bool jsonDocumentPlanned = false;
    bool stdoutCaptureOwned = false;
    bool stderrCaptureOwned = false;
    bool exitCodeValidationOwned = false;
    bool timeoutOwned = false;
    bool rawJsonOutputOwned = false;
    bool jsonParseOwned = false;
    bool rawJsonReady = false;
    bool jsonParseReady = false;
    bool errorFree = false;
    bool contractReady = false;
};

struct BatchRenderedVideoMediaProbeResultPlan
{
    QString source = QStringLiteral("output-verification-probe-result-contract");
    QString expectedOutputPath;
    QString expectedCodec;
    QString expectedContainer;
    int expectedFrameCount = 0;
    double expectedDurationSeconds = 0.0;
    QString parsedCodec;
    QString parsedContainer;
    int parsedFrameCount = 0;
    double parsedDurationSeconds = 0.0;
    QString mediaProbeJsonSource =
        QStringLiteral("output-verification-probe-json-contract");
    QString reason;
    bool outputVerificationContractReady = false;
    bool mediaProbeCommandContractReady = false;
    bool mediaProbeJsonContractReady = false;
    bool mediaProbeCommandExecutionOwned = false;
    bool mediaProbeCommandExecutionReady = false;
    bool rawJsonReady = false;
    bool jsonParseReady = false;
    bool jsonErrorFree = false;
    bool resultParsingPlanned = false;
    bool resultSchemaReady = false;
    bool codecContainerFieldsPlanned = false;
    bool frameCountFieldPlanned = false;
    bool durationFieldPlanned = false;
    bool mediaProbeResultOwned = false;
    bool jsonParseOwned = false;
    bool codecContainerResultReady = false;
    bool frameCountResultReady = false;
    bool durationResultReady = false;
    bool resultReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoMediaProbeValidationPlan
{
    QString source = QStringLiteral("output-verification-probe-validation-contract");
    QString expectedOutputPath;
    QString expectedCodec;
    QString expectedContainer;
    int expectedFrameCount = 0;
    double expectedDurationSeconds = 0.0;
    QString parsedCodec;
    QString parsedContainer;
    int parsedFrameCount = 0;
    double parsedDurationSeconds = 0.0;
    QString mediaProbeResultSource =
        QStringLiteral("output-verification-probe-result-contract");
    QString reason;
    bool mediaProbeResultContractReady = false;
    bool parsedResultReady = false;
    bool parsedResultIngestPlanned = false;
    bool parsedResultIngestOwned = false;
    bool parsedResultIngestReady = false;
    bool codecContainerComparisonPlanned = false;
    bool codecContainerComparisonReady = false;
    bool codecContainerMatches = false;
    bool frameCountComparisonPlanned = false;
    bool frameCountComparisonReady = false;
    bool frameCountMatches = false;
    bool durationComparisonPlanned = false;
    bool durationComparisonReady = false;
    bool durationMatches = false;
    bool validationOwned = false;
    bool validationReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoFfmpegCommandPlan
{
    QString source = QStringLiteral("gui-rawvideo-pipe");
    QString executable = QStringLiteral("ffmpeg");
    QString rawInputPixelFormat = QStringLiteral("rgb48");
    QString rawInputArguments;
    QString colorTagSource = QStringLiteral("rec709-default");
    int colorTag = 1;
    QString colorArguments;
    /* Retained verbatim from the parts they were built from, so the runner can
     * re-assemble a byte-identical argument string against a different output path
     * (the atomic-write temporary) without re-deriving any encoder/filter decision.
     * See batchRenderedVideoFfmpegArgumentsWithOutputPath. */
    QString videoArguments;
    QString filterArguments;
    QString arguments;
    QString commandLine;
    QString reason;
    bool rawVideoPipeInputReady = false;
    QString audioArguments;
    QString audioTransitionSource;
    QString audioTransitionArguments;
    bool audioContractReady = false;
    bool audioMuxExecutionContractReady = false;
    bool audioMuxPlanned = false;
    bool audioMuxArgumentHandoffPlanned = false;
    bool audioSyncValidationPlanned = false;
    bool audioMuxExecutionReady = false;
    bool muxedAudioCommandPlanned = false;
    bool muxedAudioCommandReady = false;
    bool audioInputOwned = false;
    bool executionOwned = false;
    bool outputVerificationOwned = false;
    bool ready = false;
};

struct BatchRenderedVideoFfmpegExecutionPlan
{
    QString source = QStringLiteral("command-contract");
    QString executable;
    QString commandLine;
    QString reason;
    bool commandReady = false;
    QString audioArguments;
    QString audioTransitionSource;
    QString audioTransitionArguments;
    bool audioContractReady = false;
    bool audioMuxExecutionContractReady = false;
    bool audioMuxPlanned = false;
    bool audioMuxArgumentHandoffPlanned = false;
    bool audioSyncValidationPlanned = false;
    bool audioMuxExecutionReady = false;
    bool muxedAudioCommandPlanned = false;
    bool muxedAudioCommandReady = false;
    bool audioInputOwned = false;
    bool processLaunchOwned = false;
    bool stdinPipeOwned = false;
    bool rawFrameFeedOwned = false;
    bool stderrCaptureOwned = false;
    bool exitCodeValidationOwned = false;
    bool timeoutOwned = false;
    bool cleanupOwned = false;
    bool executionReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoSourceMetadata
{
    int width = 0;
    int height = 0;
    double frameRate = 0.0;
    int frameCount = 0;
    double durationSeconds = 0.0;
    double stretchFactorX = STRETCH_H_100;
    double stretchFactorY = STRETCH_V_100;
    QString reason;
    bool frameCountReady = false;
    bool ready = false;
};

struct BatchRenderedVideoRenderSettings
{
    QString source = QStringLiteral("batch-defaults");
    bool resizeEnabled = false;
    int resizeWidth = 0;
    int resizeHeight = 0;
    bool resizeHeightLocked = false;
    bool explicitHeadlessSettings = false;
    bool guiSettingsOwned = false;
    QString reason;
    bool ready = true;
};

struct BatchRenderedVideoOutputPlan
{
    QString outputPath;
    QString reason;
    int inputClipCount = 1;
    bool explicitFileOutput = false;
    bool ready = false;
};

struct BatchRenderedVideoOutputVerificationPlan
{
    QString source = QStringLiteral("planned-output-contract");
    QString expectedOutputPath;
    QString expectedExtension;
    QString expectedCodec;
    QString expectedContainer;
    int expectedFrameCount = 0;
    double expectedDurationSeconds = 0.0;
    QString reason;
    qulonglong nonEmptyMinimumBytes = 1;
    bool outputPathReady = false;
    bool extensionMatchesTarget = false;
    bool fileExistenceCheckPlanned = false;
    bool nonEmptyCheckPlanned = false;
    bool filesystemInspectionOwned = false;
    bool fileExistenceCheckReady = false;
    bool nonEmptyCheckReady = false;
    bool codecContainerCheckPlanned = false;
    bool codecContainerExpectationReady = false;
    bool codecContainerValidationReady = false;
    bool frameCountCheckPlanned = false;
    bool frameCountExpectationReady = false;
    bool frameCountValidationReady = false;
    bool fileExistenceCheckOwned = false;
    bool nonEmptyCheckOwned = false;
    bool mediaProbeOwned = false;
    bool codecContainerCheckOwned = false;
    bool frameCountCheckOwned = false;
    bool receiptOrHashOwned = false;
    bool verificationExecutionOwned = false;
    bool contractReady = false;
};

struct BatchRenderedVideoOutputVerificationExecutionPlan
{
    QString source = QStringLiteral("post-ffmpeg-output-verification-contract");
    QString expectedOutputPath;
    QString expectedExtension;
    QString expectedCodec;
    QString expectedContainer;
    int expectedFrameCount = 0;
    double expectedDurationSeconds = 0.0;
    QString mediaProbeExecutable = QStringLiteral("ffprobe");
    QString mediaProbeBinarySource =
        QStringLiteral("default-executable-name");
    QString mediaProbeRequestedExecutable = QStringLiteral("ffprobe");
    QString mediaProbeResolvedExecutable = QStringLiteral("ffprobe");
    QString mediaProbeBinaryReason;
    QString mediaProbeCommandSource =
        QStringLiteral("output-verification-probe-command-contract");
    QString mediaProbeCommandArguments;
    QString mediaProbeCommandLine;
    QString mediaProbeCommandReason;
    QString mediaProbeResultSource =
        QStringLiteral("output-verification-probe-result-contract");
    QString mediaProbeJsonSource =
        QStringLiteral("output-verification-probe-json-contract");
    QString mediaProbeValidationSource =
        QStringLiteral("output-verification-probe-validation-contract");
    QString mediaProbeJsonReason;
    QString mediaProbeResultParsedCodec;
    QString mediaProbeResultParsedContainer;
    int mediaProbeResultParsedFrameCount = 0;
    double mediaProbeResultParsedDurationSeconds = 0.0;
    QString mediaProbeResultReason;
    QString mediaProbeValidationReason;
    QString reason;
    bool outputVerificationContractReady = false;
    bool ffmpegExecutionContractReady = false;
    bool mediaProbePathSearchOwned = false;
    bool mediaProbePathSearchAttempted = false;
    bool mediaProbeFoundOnPath = false;
    bool mediaProbeCommandReady = true;
    bool mediaProbeCommandPlanned = false;
    bool mediaProbeInvocationCommandReady = false;
    bool mediaProbeCommandExecutionOwned = false;
    bool mediaProbeCommandExecutionReady = false;
    bool mediaProbeCommandContractReady = false;
    bool mediaProbeJsonContractReady = false;
    bool mediaProbeJsonStdoutCapturePlanned = false;
    bool mediaProbeJsonStderrCapturePlanned = false;
    bool mediaProbeJsonExitCodeValidationPlanned = false;
    bool mediaProbeJsonTimeoutPlanned = false;
    bool mediaProbeJsonErrorReportingPlanned = false;
    bool mediaProbeJsonDocumentPlanned = false;
    bool mediaProbeJsonStdoutCaptureOwned = false;
    bool mediaProbeJsonStderrCaptureOwned = false;
    bool mediaProbeJsonExitCodeValidationOwned = false;
    bool mediaProbeJsonTimeoutOwned = false;
    bool mediaProbeJsonRawOutputOwned = false;
    bool mediaProbeJsonParseOwned = false;
    bool mediaProbeJsonRawOutputReady = false;
    bool mediaProbeJsonParseReady = false;
    bool mediaProbeJsonErrorFree = false;
    bool mediaProbeJsonReady = false;
    bool mediaProbeResultContractReady = false;
    bool mediaProbeResultParsingPlanned = false;
    bool mediaProbeResultSchemaReady = false;
    bool mediaProbeResultCodecContainerFieldsPlanned = false;
    bool mediaProbeResultFrameCountFieldPlanned = false;
    bool mediaProbeResultDurationFieldPlanned = false;
    bool mediaProbeResultOwned = false;
    bool mediaProbeResultJsonParseOwned = false;
    bool mediaProbeResultCodecContainerReady = false;
    bool mediaProbeResultFrameCountReady = false;
    bool mediaProbeResultDurationReady = false;
    bool mediaProbeResultReady = false;
    bool mediaProbeValidationContractReady = false;
    bool mediaProbeValidationParsedResultReady = false;
    bool mediaProbeValidationParsedResultIngestPlanned = false;
    bool mediaProbeValidationParsedResultIngestOwned = false;
    bool mediaProbeValidationParsedResultIngestReady = false;
    bool mediaProbeValidationCodecContainerComparisonPlanned = false;
    bool mediaProbeValidationCodecContainerComparisonReady = false;
    bool mediaProbeValidationCodecContainerMatches = false;
    bool mediaProbeValidationFrameCountComparisonPlanned = false;
    bool mediaProbeValidationFrameCountComparisonReady = false;
    bool mediaProbeValidationFrameCountMatches = false;
    bool mediaProbeValidationDurationComparisonPlanned = false;
    bool mediaProbeValidationDurationComparisonReady = false;
    bool mediaProbeValidationDurationMatches = false;
    bool mediaProbeValidationOwned = false;
    bool mediaProbeValidationReady = false;
    QString ffmpegAudioArguments;
    QString ffmpegAudioTransitionSource;
    QString ffmpegAudioTransitionArguments;
    bool ffmpegAudioContractReady = false;
    bool ffmpegAudioMuxExecutionContractReady = false;
    bool ffmpegAudioMuxPlanned = false;
    bool ffmpegAudioMuxArgumentHandoffPlanned = false;
    bool ffmpegAudioSyncValidationPlanned = false;
    bool ffmpegAudioMuxExecutionReady = false;
    bool ffmpegMuxedAudioCommandPlanned = false;
    bool ffmpegMuxedAudioCommandReady = false;
    bool ffmpegAudioInputOwned = false;
    qulonglong nonEmptyMinimumBytes = 1;
    bool fileExistenceCheckPlanned = false;
    bool nonEmptyCheckPlanned = false;
    bool filesystemInspectionOwned = false;
    bool fileExistenceCheckReady = false;
    bool nonEmptyCheckReady = false;
    bool codecContainerCheckPlanned = false;
    bool codecContainerExpectationReady = false;
    bool codecContainerValidationReady = false;
    bool frameCountCheckPlanned = false;
    bool frameCountExpectationReady = false;
    bool frameCountValidationReady = false;
    bool fileExistenceCheckOwned = false;
    bool nonEmptyCheckOwned = false;
    bool mediaProbeExecutionOwned = false;
    bool codecContainerValidationOwned = false;
    bool frameCountValidationOwned = false;
    QString receiptHashValidationSource =
        QStringLiteral("output-verification-receipt-hash-validation-contract");
    QString receiptHashValidationReason;
    bool receiptHashValidationContractReady = false;
    bool receiptHashValidationPlanned = false;
    bool receiptComparisonPlanned = false;
    bool outputHashComparisonPlanned = false;
    bool receiptReadOwned = false;
    bool outputHashReadOwned = false;
    bool receiptComparisonOwned = false;
    bool outputHashComparisonOwned = false;
    bool receiptComparisonReady = false;
    bool outputHashComparisonReady = false;
    bool receiptHashValidationOwned = false;
    bool receiptHashValidationReady = false;
    bool verificationExecutionReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoReceiptHashValidationPlan
{
    QString source =
        QStringLiteral("output-verification-receipt-hash-validation-contract");
    QString expectedOutputPath;
    QString expectedExtension;
    QString expectedCodec;
    QString expectedContainer;
    int expectedFrameCount = 0;
    double expectedDurationSeconds = 0.0;
    QString reason;
    bool outputVerificationExecutionContractReady = false;
    bool validationPlanned = false;
    bool receiptComparisonPlanned = false;
    bool outputHashComparisonPlanned = false;
    bool receiptReadOwned = false;
    bool outputHashReadOwned = false;
    bool receiptComparisonOwned = false;
    bool outputHashComparisonOwned = false;
    bool receiptComparisonReady = false;
    bool outputHashComparisonReady = false;
    bool receiptHashValidationOwned = false;
    bool receiptHashValidationReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoOutputVerificationDecisionPlan
{
    QString source =
        QStringLiteral("output-verification-decision-contract");
    QString expectedOutputPath;
    QString expectedExtension;
    QString expectedCodec;
    QString expectedContainer;
    int expectedFrameCount = 0;
    double expectedDurationSeconds = 0.0;
    QString reason;
    bool outputVerificationExecutionContractReady = false;
    bool fileExistenceCheckPlanned = false;
    bool nonEmptyCheckPlanned = false;
    bool fileChecksPlanned = false;
    bool filesystemInspectionOwned = false;
    bool fileExistenceCheckOwned = false;
    bool nonEmptyCheckOwned = false;
    bool fileChecksOwned = false;
    bool fileExistenceCheckReady = false;
    bool nonEmptyCheckReady = false;
    bool fileChecksReady = false;
    bool mediaProbeValidationContractReady = false;
    bool mediaProbeValidationPlanned = false;
    bool mediaProbeValidationOwned = false;
    bool mediaProbeValidationReady = false;
    bool codecContainerComparisonPlanned = false;
    bool codecContainerComparisonReady = false;
    bool frameCountComparisonPlanned = false;
    bool frameCountComparisonReady = false;
    bool durationComparisonPlanned = false;
    bool durationComparisonReady = false;
    QString receiptHashValidationSource =
        QStringLiteral("output-verification-receipt-hash-validation-contract");
    QString receiptHashValidationReason;
    bool receiptHashValidationContractReady = false;
    bool receiptHashValidationPlanned = false;
    bool receiptComparisonPlanned = false;
    bool outputHashComparisonPlanned = false;
    bool receiptReadOwned = false;
    bool outputHashReadOwned = false;
    bool receiptComparisonOwned = false;
    bool outputHashComparisonOwned = false;
    bool receiptComparisonReady = false;
    bool outputHashComparisonReady = false;
    bool receiptHashValidationOwned = false;
    bool receiptHashValidationReady = false;
    bool verificationExecutionReady = false;
    bool verificationDecisionReady = false;
    bool outputAccepted = false;
    bool contractReady = false;
};

struct BatchRenderedVideoOutputVerificationResultPlan
{
    QString source =
        QStringLiteral("output-verification-result-report-contract");
    QString expectedOutputPath;
    QString expectedExtension;
    QString expectedCodec;
    QString expectedContainer;
    int expectedFrameCount = 0;
    double expectedDurationSeconds = 0.0;
    QString resultState = QStringLiteral("blocked");
    QString failureReason;
    QString reason;
    bool outputVerificationDecisionContractReady = false;
    bool fileChecksReady = false;
    bool mediaProbeValidationReady = false;
    bool receiptHashValidationReady = false;
    bool verificationExecutionReady = false;
    bool verificationDecisionReady = false;
    bool outputAccepted = false;
    bool resultReportPlanned = false;
    bool failureReportPlanned = false;
    bool acceptedOutputReportPlanned = false;
    bool resultReportOwned = false;
    bool failureReportOwned = false;
    bool acceptedOutputReportOwned = false;
    bool resultReportReady = false;
    bool failureReportReady = false;
    bool acceptedOutputReportReady = false;
    bool outputVerificationReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoOutputVerificationReportPlan
{
    QString source =
        QStringLiteral("output-verification-report-artifact-contract");
    QString expectedOutputPath;
    QString expectedExtension;
    QString expectedCodec;
    QString expectedContainer;
    int expectedFrameCount = 0;
    double expectedDurationSeconds = 0.0;
    QString resultState = QStringLiteral("blocked");
    QString failureReason;
    QString reportPath;
    QString reportFormat = QStringLiteral("json");
    QString reportSchema =
        QStringLiteral("rendered-output-verification-report.v1");
    QString reportKind = QStringLiteral("failure");
    QString reason;
    bool outputVerificationResultContractReady = false;
    bool resultReportPlanned = false;
    bool failureReportPlanned = false;
    bool acceptedOutputReportPlanned = false;
    bool outputAccepted = false;
    bool reportArtifactPlanned = false;
    bool reportPathPlanned = false;
    bool reportPathReady = false;
    bool reportFormatReady = false;
    bool reportSchemaReady = false;
    bool reportContentPlanned = false;
    bool failureReportContentPlanned = false;
    bool acceptedReportContentPlanned = false;
    bool reportFileCreationPlanned = false;
    bool reportWritePlanned = false;
    bool reportFileCreationOwned = false;
    bool reportWriteOwned = false;
    bool reportFileReady = false;
    bool reportWriteReady = false;
    bool reportReady = false;
    bool outputVerificationReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoOutputVerificationReportContentPlan
{
    QString source =
        QStringLiteral("output-verification-report-content-schema-contract");
    QString expectedOutputPath;
    QString resultState = QStringLiteral("blocked");
    QString failureReason;
    QString reportPath;
    QString reportFormat = QStringLiteral("json");
    QString reportSchema =
        QStringLiteral("rendered-output-verification-report.v1");
    QString reportKind = QStringLiteral("failure");
    QStringList topLevelKeys;
    QStringList expectedOutputKeys;
    QStringList verificationCheckKeys;
    QStringList artifactKeys;
    QStringList plannedErrorCategories;
    QString activeErrorCategory;
    QString reason;
    bool outputVerificationReportContractReady = false;
    bool reportPathReady = false;
    bool reportFormatReady = false;
    bool reportSchemaReady = false;
    bool reportKindReady = false;
    bool reportContentPlanned = false;
    bool failureReportContentPlanned = false;
    bool acceptedReportContentPlanned = false;
    bool topLevelKeysPlanned = false;
    bool expectedOutputKeysPlanned = false;
    bool verificationCheckKeysPlanned = false;
    bool artifactKeysPlanned = false;
    bool errorCategoriesPlanned = false;
    bool activeErrorCategoryPlanned = false;
    bool schemaDetailContractReady = false;
    bool jsonSerializationPlanned = false;
    bool jsonSerializationOwned = false;
    bool reportWritePlanned = false;
    bool reportWriteOwned = false;
    bool reportReady = false;
    bool outputVerificationReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoOutputVerificationReportWriterPlan
{
    QString source =
        QStringLiteral("output-verification-report-writer-preflight-contract");
    QString serializationInput =
        QStringLiteral("output-verification-report-content-schema-contract");
    QString expectedOutputPath;
    QString reportPath;
    QString temporaryReportPath;
    QString reportFormat = QStringLiteral("json");
    QString reportSchema =
        QStringLiteral("rendered-output-verification-report.v1");
    QString reportEncoding = QStringLiteral("utf-8");
    QString writeMode = QStringLiteral("atomic-json-sidecar");
    QString reportKind = QStringLiteral("failure");
    QString activeErrorCategory;
    QString failureReason;
    QString reason;
    bool outputVerificationReportContentContractReady = false;
    bool schemaDetailContractReady = false;
    bool reportPathReady = false;
    bool temporaryReportPathPlanned = false;
    bool temporaryReportPathReady = false;
    bool reportFormatReady = false;
    bool reportSchemaReady = false;
    bool reportEncodingReady = false;
    bool writeModeReady = false;
    bool writerPreflightPlanned = false;
    bool serializationInputReady = false;
    bool serializationPreflightPlanned = false;
    bool parentDirectoryPreflightPlanned = false;
    bool fileCreationPreflightPlanned = false;
    bool writePreflightPlanned = false;
    bool cleanupPreflightPlanned = false;
    bool jsonSerializationPlanned = false;
    bool jsonSerializationOwned = false;
    bool jsonSerializationReady = false;
    bool parentDirectoryOwned = false;
    bool reportFileCreationPlanned = false;
    bool reportFileCreationOwned = false;
    bool reportWritePlanned = false;
    bool reportWriteOwned = false;
    bool reportFileReady = false;
    bool reportWriteReady = false;
    bool reportReady = false;
    bool outputVerificationReady = false;
    bool contractReady = false;
};

struct BatchRenderedVideoRunnerPrerequisites
{
    bool processingParityReady = false;
    bool frameProcessingReady = false;
    bool audioMuxReady = false;
    bool ffmpegExecutionReady = false;
    bool outputVerificationReady = false;
    bool headlessRunnerReady = false;
    /* The five capabilities a VIDEO-ONLY rendered export actually needs. audioMuxReady is
     * deliberately NOT one of them -- see batchRenderedVideoRunnerPrerequisitesForCurrentBuild. */
    bool videoOnlyRunnerReady = false;
    /* Non-empty when the runner IS ready but ships a narrower capability than the full
     * plan models. Unlike `reason` this is not a blocker: it is surfaced to the operator
     * so a silently-dropped audio track can never look like a complete export. */
    QString limitation;
    QString reason = QStringLiteral("rendered processing parity, frame processing, ffmpeg execution, output verification, and headless rendered-export runner are not implemented");
    bool ready = false;
};

struct BatchRenderedVideoJobPlan
{
    BatchExportFormatRequest request;
    BatchRenderedVideoTarget target;
    BatchRenderedVideoEncoderPreset encoderPreset;
    BatchRenderedVideoFfmpegVideoPlan ffmpegVideoPlan;
    BatchRenderedVideoFfmpegFilterPlan ffmpegFilterPlan;
    BatchRenderedVideoOptionalFilterPlan optionalFilterPlan;
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan;
    BatchRenderedVideoSourceAudioExtractionPlan sourceAudioExtractionPlan;
    BatchRenderedVideoSourceAudioExtractionExecutionPlan sourceAudioExtractionExecutionPlan;
    BatchRenderedVideoFfmpegAudioInputHandoffPlan ffmpegAudioInputHandoffPlan;
    BatchRenderedVideoFfmpegAudioInputPlan ffmpegAudioInputPlan;
    BatchRenderedVideoAudioMuxPrerequisitesPlan audioMuxPrerequisitesPlan;
    BatchRenderedVideoAudioMuxExecutionPlan audioMuxExecutionPlan;
    BatchRenderedVideoFfmpegAudioPlan ffmpegAudioPlan;
    BatchRenderedVideoSourceMetadata sourceMetadata;
    BatchRenderedVideoRenderSettings renderSettings;
    BatchRenderedVideoFfmpegFramePlan ffmpegFramePlan;
    BatchRenderedVideoReceiptApplicationPlan receiptApplicationPlan;
    BatchRenderedVideoFrameProcessingPlan frameProcessingPlan;
    BatchRenderedVideoFfmpegBinaryPlan ffmpegBinaryPlan;
    BatchRenderedVideoFfmpegCommandPlan ffmpegCommandPlan;
    BatchRenderedVideoFfmpegExecutionPlan ffmpegExecutionPlan;
    BatchRenderedVideoOutputPlan outputPlan;
    BatchRenderedVideoOutputVerificationPlan outputVerificationPlan;
    BatchRenderedVideoMediaProbeBinaryPlan mediaProbeBinaryPlan;
    BatchRenderedVideoMediaProbeCommandPlan mediaProbeCommandPlan;
    BatchRenderedVideoMediaProbeJsonPlan mediaProbeJsonPlan;
    BatchRenderedVideoMediaProbeResultPlan mediaProbeResultPlan;
    BatchRenderedVideoMediaProbeValidationPlan mediaProbeValidationPlan;
    BatchRenderedVideoOutputVerificationExecutionPlan outputVerificationExecutionPlan;
    BatchRenderedVideoReceiptHashValidationPlan receiptHashValidationPlan;
    BatchRenderedVideoOutputVerificationDecisionPlan outputVerificationDecisionPlan;
    BatchRenderedVideoOutputVerificationResultPlan outputVerificationResultPlan;
    BatchRenderedVideoOutputVerificationReportPlan outputVerificationReportPlan;
    BatchRenderedVideoOutputVerificationReportContentPlan outputVerificationReportContentPlan;
    BatchRenderedVideoOutputVerificationReportWriterPlan outputVerificationReportWriterPlan;
    BatchRenderedVideoRunnerPrerequisites runnerPrerequisites;
    bool requestValid = false;
    bool targetReady = false;
    bool encoderReady = false;
    bool ffmpegVideoReady = false;
    bool ffmpegFilterReady = false;
    bool optionalFilterContractReady = false;
    bool sourceAudioContractReady = false;
    bool sourceAudioExtractionContractReady = false;
    bool sourceAudioExtractionExecutionContractReady = false;
    bool ffmpegAudioInputHandoffContractReady = false;
    bool ffmpegAudioInputContractReady = false;
    bool audioMuxPrerequisitesContractReady = false;
    bool audioMuxExecutionContractReady = false;
    bool ffmpegAudioContractReady = false;
    bool metadataAttempted = false;
    bool metadataReady = false;
    bool ffmpegFrameReady = false;
    bool receiptApplicationContractReady = false;
    bool frameProcessingContractReady = false;
    bool ffmpegBinaryCommandReady = false;
    bool ffmpegCommandReady = false;
    bool ffmpegExecutionContractReady = false;
    bool outputReady = false;
    bool outputVerificationContractReady = false;
    bool mediaProbeCommandReady = false;
    bool mediaProbeCommandContractReady = false;
    bool mediaProbeJsonContractReady = false;
    bool mediaProbeResultContractReady = false;
    bool mediaProbeValidationContractReady = false;
    bool outputVerificationExecutionContractReady = false;
    bool receiptHashValidationContractReady = false;
    bool outputVerificationDecisionContractReady = false;
    bool outputVerificationResultContractReady = false;
    bool outputVerificationReportContractReady = false;
    bool outputVerificationReportContentContractReady = false;
    bool outputVerificationReportWriterContractReady = false;
    bool preflightReady = false;
    bool runnable = false;
};

BatchRenderedVideoCodec batchRenderedVideoCodecFromString(const QString & value, bool * ok = nullptr)
;

BatchRenderedVideoContainer batchRenderedVideoContainerFromString(const QString & value, bool * ok = nullptr)
;

BatchExportFormatRequest batchExportFormatRequestFromString(const QString & value)
;

BatchExportFormat batchExportFormatFromString(const QString & value)
;

inline const char * batchExportFormatName(BatchExportFormat format)
{
    switch( format )
    {
        case BatchExportFormat::Cdng:
            return "cdng";
        case BatchExportFormat::RenderedVideo:
            return "rendered-video";
        case BatchExportFormat::Unknown:
            break;
    }
    return "unknown";
}

inline const char * batchRenderedVideoCodecName(BatchRenderedVideoCodec codec)
{
    switch( codec )
    {
        case BatchRenderedVideoCodec::Unspecified:
            return "unspecified";
        case BatchRenderedVideoCodec::H264:
            return "h264";
        case BatchRenderedVideoCodec::H265:
            return "h265";
        case BatchRenderedVideoCodec::ProRes:
            return "prores";
    }
    return "unspecified";
}

inline const char * batchRenderedVideoContainerName(BatchRenderedVideoContainer container)
{
    switch( container )
    {
        case BatchRenderedVideoContainer::Unspecified:
            return "unspecified";
        case BatchRenderedVideoContainer::Mov:
            return "mov";
        case BatchRenderedVideoContainer::Mp4:
            return "mp4";
        case BatchRenderedVideoContainer::Mkv:
            return "mkv";
    }
    return "unspecified";
}

inline const char * batchRenderedVideoContainerExtension(BatchRenderedVideoContainer container)
{
    switch( container )
    {
        case BatchRenderedVideoContainer::Mov:
            return ".mov";
        case BatchRenderedVideoContainer::Mp4:
            return ".mp4";
        case BatchRenderedVideoContainer::Mkv:
            return ".mkv";
        case BatchRenderedVideoContainer::Unspecified:
            break;
    }
    return "";
}

inline const char * batchRenderedVideoMediaProbeCodecName(BatchRenderedVideoCodec codec)
{
    switch( codec )
    {
        case BatchRenderedVideoCodec::H265:
            return "hevc";
        case BatchRenderedVideoCodec::H264:
        case BatchRenderedVideoCodec::ProRes:
        case BatchRenderedVideoCodec::Unspecified:
            break;
    }
    return batchRenderedVideoCodecName(codec);
}

inline const char * batchRenderedVideoMediaProbeContainerName(BatchRenderedVideoContainer container)
{
    switch( container )
    {
        case BatchRenderedVideoContainer::Mkv:
            return "matroska";
        case BatchRenderedVideoContainer::Mov:
        case BatchRenderedVideoContainer::Mp4:
        case BatchRenderedVideoContainer::Unspecified:
            break;
    }
    return batchRenderedVideoContainerName(container);
}

inline const char * batchRenderedVideoEncoderProfileName(
    BatchRenderedVideoEncoderProfile profile)
{
    switch( profile )
    {
        case BatchRenderedVideoEncoderProfile::Unspecified:
            return "unspecified";
        case BatchRenderedVideoEncoderProfile::H264:
            return "h264";
        case BatchRenderedVideoEncoderProfile::H265_8:
            return "h265-8";
        case BatchRenderedVideoEncoderProfile::ProRes422HQ:
            return "prores422hq";
    }
    return "unspecified";
}

inline const char * batchRenderedVideoEncoderOptionName(
    BatchRenderedVideoEncoderOption option)
{
    switch( option )
    {
        case BatchRenderedVideoEncoderOption::Unspecified:
            return "unspecified";
        case BatchRenderedVideoEncoderOption::H264HighMov:
            return "ffmpeg-mov-high";
        case BatchRenderedVideoEncoderOption::H264HighMp4:
            return "ffmpeg-mp4-high";
        case BatchRenderedVideoEncoderOption::H264HighMkv:
            return "ffmpeg-mkv-high";
        case BatchRenderedVideoEncoderOption::H265HighMov:
            return "ffmpeg-mov-high";
        case BatchRenderedVideoEncoderOption::H265HighMp4:
            return "ffmpeg-mp4-high";
        case BatchRenderedVideoEncoderOption::H265HighMkv:
            return "ffmpeg-mkv-high";
        case BatchRenderedVideoEncoderOption::ProResFfmpegKostya:
            return "ffmpeg-kostya";
    }
    return "unspecified";
}

QString batchExportFormatRequestSummary(const BatchExportFormatRequest & request)
;

bool batchRenderedVideoRequestShapeValid(const BatchExportFormatRequest & request)
;

QString batchRenderedVideoRequestShapeError(const BatchExportFormatRequest & request)
;

BatchRenderedVideoTarget batchRenderedVideoTargetFromRequest(
    const BatchExportFormatRequest & request)
;

BatchRenderedVideoEncoderPreset batchRenderedVideoEncoderPresetFromTarget(
    const BatchRenderedVideoTarget & target)
;

BatchRenderedVideoEncoderPreset batchRenderedVideoEncoderPresetFromRequest(
    const BatchExportFormatRequest & request)
;

BatchRenderedVideoFfmpegVideoPlan
batchRenderedVideoFfmpegVideoPlanFromEncoderPreset(
    const BatchRenderedVideoEncoderPreset & preset)
;

BatchRenderedVideoFfmpegVideoPlan
batchRenderedVideoFfmpegVideoPlanFromRequest(
    const BatchExportFormatRequest & request)
;

BatchRenderedVideoFfmpegFilterPlan
batchRenderedVideoFfmpegFilterPlanForCurrentBuild()
;

BatchRenderedVideoOptionalFilterPlan
batchRenderedVideoOptionalFilterPlanFromFilterPlan(
    const BatchRenderedVideoFfmpegFilterPlan & filterPlan)
;

BatchRenderedVideoSourceAudioPlan
batchRenderedVideoSourceAudioPlanForCurrentBuild(
    const QString & clipPath = QString())
;

BatchRenderedVideoSourceAudioPlan
batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
    const QString & clipPath,
    bool sourceAudioPresent,
    int channels,
    int sampleRate,
    int bitsPerSample,
    qulonglong audioBytes)
;

QString batchRenderedVideoSourceAudioExtractionPathFromOutputPath(
    const QString & outputPath)
;

BatchRenderedVideoSourceAudioExtractionPlan
batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const QString & plannedAudioPath)
;

BatchRenderedVideoSourceAudioExtractionPlan
batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan)
;

BatchRenderedVideoSourceAudioExtractionExecutionPlan
batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan)
;

BatchRenderedVideoFfmpegAudioInputHandoffPlan
batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
    const BatchRenderedVideoSourceAudioExtractionExecutionPlan & executionPlan)
;

BatchRenderedVideoFfmpegAudioInputPlan
batchRenderedVideoFfmpegAudioInputPlanFromHandoff(
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan,
    const BatchRenderedVideoFfmpegAudioInputHandoffPlan & handoffPlan)
;

BatchRenderedVideoFfmpegAudioInputPlan
batchRenderedVideoFfmpegAudioInputPlanFromExtraction(
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan,
    const BatchRenderedVideoSourceAudioExtractionExecutionPlan & executionPlan)
;

BatchRenderedVideoFfmpegAudioInputPlan
batchRenderedVideoFfmpegAudioInputPlanFromExtraction(
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan)
;

BatchRenderedVideoAudioMuxPrerequisitesPlan
batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan,
    const BatchRenderedVideoFfmpegAudioInputPlan & audioInputPlan)
;

BatchRenderedVideoAudioMuxPrerequisitesPlan
batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan)
;

BatchRenderedVideoAudioMuxPrerequisitesPlan
batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan)
;

BatchRenderedVideoAudioMuxExecutionPlan
batchRenderedVideoAudioMuxExecutionPlanFromContracts(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoFfmpegAudioInputHandoffPlan & handoffPlan,
    const BatchRenderedVideoAudioMuxPrerequisitesPlan & prerequisitesPlan)
;

BatchRenderedVideoFfmpegAudioPlan
batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoAudioMuxPrerequisitesPlan & audioMuxPlan,
    const BatchRenderedVideoAudioMuxExecutionPlan & audioMuxExecutionPlan)
;

BatchRenderedVideoFfmpegAudioPlan
batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoAudioMuxPrerequisitesPlan & audioMuxPlan)
;

BatchRenderedVideoFfmpegAudioPlan
batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan)
;

BatchRenderedVideoFfmpegAudioPlan
batchRenderedVideoFfmpegAudioPlanForCurrentBuild()
;

bool batchRenderedVideoEncoderProfileRequiresEvenDimensions(
    BatchRenderedVideoEncoderProfile profile)
;

QString batchRenderedVideoFfmpegFrameRateArgument(double frameRate)
;

BatchRenderedVideoFfmpegFramePlan
batchRenderedVideoFfmpegFramePlanFromGuiState(
    int sourceWidth,
    int sourceHeight,
    double frameRate,
    double stretchFactorX,
    double stretchFactorY,
    bool resizeEnabled,
    int resizeWidth,
    int resizeHeight,
    bool resizeHeightLocked,
    BatchRenderedVideoEncoderProfile encoderProfile)
;

BatchRenderedVideoSourceMetadata batchRenderedVideoSourceMetadata(
    int width,
    int height,
    double frameRate,
    double stretchFactorX,
    double stretchFactorY,
    int frameCount = 0)
;

BatchRenderedVideoRenderSettings batchRenderedVideoDefaultRenderSettings()
;

BatchRenderedVideoRenderSettings
batchRenderedVideoRenderSettingsFromExplicitResize(
    bool resizeEnabled,
    int resizeWidth,
    int resizeHeight,
    bool resizeHeightLocked)
;

BatchRenderedVideoFfmpegFramePlan
batchRenderedVideoFfmpegFramePlanFromMetadata(
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoRenderSettings & settings,
    BatchRenderedVideoEncoderProfile encoderProfile)
;

BatchRenderedVideoReceiptApplicationPlan
batchRenderedVideoReceiptApplicationPlanFromContracts(
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoFfmpegFramePlan & framePlan)
;

BatchRenderedVideoFrameProcessingPlan
batchRenderedVideoFrameProcessingPlanFromFramePlan(
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoFfmpegFramePlan & framePlan,
    const BatchRenderedVideoReceiptApplicationPlan & receiptPlan)
;

BatchRenderedVideoFrameProcessingPlan
batchRenderedVideoFrameProcessingPlanFromFramePlan(
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoFfmpegFramePlan & framePlan)
;

BatchRenderedVideoOutputPlan batchRenderedVideoOutputPlanFromPaths(
    const QString & inputPath,
    const QString & outputPath,
    const BatchRenderedVideoTarget & target,
    int inputClipCount)
;

BatchRenderedVideoOutputPlan batchRenderedVideoOutputPlanFromPaths(
    const QString & inputPath,
    const QString & outputPath,
    const BatchRenderedVideoTarget & target)
;

BatchRenderedVideoOutputVerificationPlan
batchRenderedVideoOutputVerificationPlanFromOutput(
    const BatchRenderedVideoOutputPlan & outputPlan,
    const BatchRenderedVideoTarget & target)
;

BatchRenderedVideoOutputVerificationPlan
batchRenderedVideoOutputVerificationPlanFromOutput(
    const BatchRenderedVideoOutputPlan & outputPlan,
    const BatchRenderedVideoTarget & target,
    const BatchRenderedVideoSourceMetadata & sourceMetadata)
;

BatchRenderedVideoFfmpegBinaryPlan
batchRenderedVideoFfmpegBinaryPlanFromRequestedName(
    const QString & requestedExecutable = QStringLiteral("ffmpeg"))
;

BatchRenderedVideoFfmpegBinaryPlan
batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(
    const QString & requestedExecutable,
    const QString & resolvedExecutable)
;

/* "ffmpeg" -> "ffmpeg.exe" on Windows, unchanged elsewhere: the exact packaged file name the
 * application-directory lookup asks for. */
QString batchRenderedVideoAppDirExecutableName(
    const QString & bareName)
;

/* QStandardPaths::findExecutable; for an app-dir search (non-empty searchDirs) a hit must
 * also be a non-empty regular file. A PATH search (empty searchDirs) is findExecutable as is. */
BatchRenderedVideoExecutableResolver batchRenderedVideoSystemExecutableResolver()
;

/* Pure lookup. Only the DEFAULT request (no request at all: empty or blank) searches
 * applicationDirectory first, for the exact packaged file name, and then falls back to ONE
 * plain PATH lookup (resolver(name, {})). Any other request -- including the bare name
 * "ffmpeg" passed explicitly -- is resolved on PATH alone, exactly as
 * ...FromResolvedPath(request, resolver(request, {})) always did. */
BatchRenderedVideoFfmpegBinaryPlan
batchRenderedVideoFfmpegBinaryPlanFromLookup(
    const QString & requestedExecutable,
    const QString & applicationDirectory,
    const BatchRenderedVideoExecutableResolver & resolver)
;

/* No argument = the default request (app directory, then PATH). */
BatchRenderedVideoFfmpegBinaryPlan
batchRenderedVideoFfmpegBinaryPlanFromCurrentEnvironment(
    const QString & requestedExecutable = QString())
;

/* Preflight: a PATH hit is trusted; an explicit selection must exist (QFileInfo::exists, as
 * before this card); an app-dir hit must still be a non-empty regular file that can start (this
 * account can open it for execute on Windows, may execute it elsewhere). */
bool batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(
    const BatchRenderedVideoFfmpegBinaryPlan & plan)
;

/* Where the lookup actually searched: "app-dir", "app-dir,PATH" or "PATH". */
QString batchRenderedVideoFfmpegBinaryPlanSearchedLocations(
    const BatchRenderedVideoFfmpegBinaryPlan & plan)
;

/* The optional ffprobe used to enrich output verification: the probe plan's PATH hit (as
 * before this card), or its app-dir hit while that is still a non-empty regular file that can
 * start; else such an ffprobe beside the resolved ffmpeg (that directory may be the app dir, so
 * the same rule applies). Empty = not available. */
QString batchRenderedVideoOptionalMediaProbeExecutable(
    const BatchRenderedVideoMediaProbeBinaryPlan & probePlan,
    const BatchRenderedVideoFfmpegBinaryPlan & ffmpegPlan)
;

BatchRenderedVideoMediaProbeBinaryPlan
batchRenderedVideoMediaProbeBinaryPlanFromRequestedName(
    const QString & requestedExecutable = QStringLiteral("ffprobe"))
;

BatchRenderedVideoMediaProbeBinaryPlan
batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
    const QString & requestedExecutable,
    const QString & resolvedExecutable)
;

/* Same rule as ...FfmpegBinaryPlanFromLookup, for the default bare name "ffprobe". */
BatchRenderedVideoMediaProbeBinaryPlan
batchRenderedVideoMediaProbeBinaryPlanFromLookup(
    const QString & requestedExecutable,
    const QString & applicationDirectory,
    const BatchRenderedVideoExecutableResolver & resolver)
;

BatchRenderedVideoMediaProbeBinaryPlan
batchRenderedVideoMediaProbeBinaryPlanFromCurrentEnvironment(
    const QString & requestedExecutable = QString())
;

QString batchRenderedVideoCommandExecutableForDisplay(
    const QString & executable)
;

BatchRenderedVideoMediaProbeCommandPlan
batchRenderedVideoMediaProbeCommandPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan)
;

BatchRenderedVideoMediaProbeJsonPlan
batchRenderedVideoMediaProbeJsonPlanFromCommand(
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan)
;

BatchRenderedVideoMediaProbeResultPlan
batchRenderedVideoMediaProbeResultPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan,
    const BatchRenderedVideoMediaProbeJsonPlan & mediaProbeJsonPlan)
;

BatchRenderedVideoMediaProbeResultPlan
batchRenderedVideoMediaProbeResultPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan)
;

BatchRenderedVideoMediaProbeValidationPlan
batchRenderedVideoMediaProbeValidationPlanFromResult(
    const BatchRenderedVideoMediaProbeResultPlan & mediaProbeResultPlan)
;

BatchRenderedVideoFfmpegCommandPlan
batchRenderedVideoFfmpegCommandPlanFromParts(
    const BatchRenderedVideoFfmpegFramePlan & framePlan,
    const BatchRenderedVideoFfmpegFilterPlan & filterPlan,
    const BatchRenderedVideoFfmpegAudioPlan & audioPlan,
    const BatchRenderedVideoFfmpegVideoPlan & videoPlan,
    const BatchRenderedVideoOutputPlan & outputPlan,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan)
;

/* The rendered runner must never leave a half-encoded file at the path a caller was told
 * to expect, so it encodes to this sibling path and renames on success. Sibling (not
 * %TEMP%) because the rename has to stay on one volume to be atomic, and the marker is
 * fixed rather than random so an interrupted run leaves an obviously-partial artefact a
 * human or an orchestrator can identify and delete.
 *
 * THE MARKER GOES BEFORE THE EXTENSION, NOT AFTER IT: `clip.mp4` -> `clip.mlvapp-partial.mp4`.
 * ffmpeg selects its muxer from the output file extension, so a trailing marker makes it
 * fail with "Unable to find a suitable output format" -- observed exactly that way on the
 * first end-to-end run of this path. A path with no suffix at all just gets the marker
 * appended. Returns an empty string for empty input so callers fail closed instead of
 * writing to a bare marker file. */
/* Codec / container / geometry / aspect, parsed out of an `ffmpeg -i <file>` stream dump.
 *
 * WHY THIS IS HERE RATHER THAN IN BatchRunner.cpp. The roadmap's E4-1 blocking proof
 * requires the export to check "frame count/duration/dimensions/ASPECT". The first three
 * are read back from a decoded frame, but aspect was reachable only through ffprobe --
 * and ffprobe is NOT shipped with MLVApp (platform/qt/FFmpeg/ffmpegWin64.zip contains
 * exactly one entry, ffmpeg.exe), so on a stock install the aspect check silently
 * degraded to `sar=unchecked`. Parsing ffmpeg's own dump closes that against the binary
 * the export ALREADY REQUIRES. It lives in this header, GUI-free and inline, so
 * tests/console/test_rendered_video_runner.cpp can pin the regexes directly instead of
 * linking the GUI-dependent BatchRunner.cpp -- the same reason
 * BatchRunner::effectiveStretchFactorY is header-inline.
 *
 * Text parsed, exactly as libavformat emits it:
 *   Input #0, mov,mp4,m4a,3gp,3g2,mj2, from 'C:/out/clip.mp4':
 *     Stream #0:0(und): Video: h264 (High) (avc1 / ...), yuv420p(tv, bt709), 5424x2268, ...
 * and, for non-square pixels ONLY:
 *     ... , 720x576 [SAR 16:15 DAR 4:3], ...
 *
 * THE ABSENCE OF THE [SAR ...] BRACKET IS ITSELF THE SQUARE-PIXEL SIGNAL, not missing
 * data: avcodec_string() prints it only when the sample aspect ratio is set AND differs
 * from 1:1. So absence is reported as `1:1(implied)` -- never as plain `1:1`, because the
 * log must not claim ffmpeg asserted a value it merely declined to contradict. */
struct BatchRenderedVideoFfmpegDumpFacts
{
    bool parsed = false;
    QString codecName;
    QString formatNames;   /* comma-separated, e.g. "mov,mp4,m4a,3gp,3g2,mj2" */
    int width = 0;
    int height = 0;
    QString sampleAspectRatio;
    QString displayAspectRatio;
};

BatchRenderedVideoFfmpegDumpFacts
batchRenderedVideoFfmpegDumpFacts(const QString & dumpText)
;

/* The three spellings that all mean SQUARE PIXELS and must all pass an aspect check:
 * ffprobe's "1:1", ffprobe's "0:1" (its rendering of an UNSET sample aspect ratio -- not
 * a degenerate ratio), and this runner's "1:1(implied)" from a dump with no [SAR] bracket.
 * An empty string means the probe reported nothing at all, which is also not a defect. */
bool batchRenderedVideoSampleAspectIsSquare(const QString & sampleAspectRatio)
;

QString batchRenderedVideoPartialOutputPath(const QString & outputPath)
;

/* Re-assemble the ffmpeg argument string against a different output path.
 *
 * This exists ONLY so the runner can aim a completed command plan at the atomic-write
 * temporary. It reuses the SAME stored parts the plan was built from and the SAME
 * concatenation order, so the only difference from commandPlan.arguments is the output
 * token -- no encoder, colour, filter or audio decision is re-derived here. Any drift
 * between this and batchRenderedVideoFfmpegCommandPlanFromParts is a defect, and
 * tests/console/test_rendered_video_runner.cpp pins the two together.
 *
 * Returns an empty string when the plan is not ready or the path is empty, so a caller
 * that forgets to check gets an unrunnable invocation rather than a truncated command. */
QString batchRenderedVideoFfmpegArgumentsWithOutputPath(
    const BatchRenderedVideoFfmpegCommandPlan & commandPlan,
    const QString & outputPath)
;

/* Re-aim the media-probe (ffprobe) argument string at a different path.
 *
 * [B2], LANE-4 review of 29af0972..7144cfe9. `BatchRenderedVideoMediaProbeCommandPlan`
 * bakes `expectedOutputPath` -- the FINAL path -- into its `arguments` at plan-build time,
 * and the runner used to execute that string verbatim. After [B1] moved verification ahead
 * of the publish, that meant the ffprobe branch pointed at the file from the PREVIOUS
 * export: on any re-run where a file already existed at the final path and ffprobe was
 * resolvable, the probe parsed the OLD file, `facts.parsed` became true, the partial-path
 * ffmpeg-dump fallback was skipped, and the codec / container / SAR checks were evaluated
 * against a file that was not the one being verified. **That is worse in kind than [B1]:
 * [B1] left behind a file that FAILED verification; [B2] let a file PASS a verification
 * that never examined it.** First runs have no pre-existing output, so the branch simply
 * fell through to the dump -- which is why neither the suite nor the end-to-end proof
 * could see it.
 *
 * `expectedOutputPath` is deliberately NOT changed at plan-build time. It is threaded
 * through a dozen plan structs and reported in the verification report, the decision plan
 * and ~14 summary tokens, where it correctly names the artefact the caller was promised.
 * The path actually being INSPECTED is a property of the moment of use, so it is decided
 * here -- the same split, for the same reason, as
 * batchRenderedVideoFfmpegArgumentsWithOutputPath above.
 *
 * Returns an empty string when the plan is not ready or the path is empty, so a caller
 * that forgets to check gets an unrunnable invocation rather than one silently aimed at
 * the wrong file. */
QString batchRenderedVideoMediaProbeArgumentsWithPath(
    const BatchRenderedVideoMediaProbeCommandPlan & commandPlan,
    const QString & probePath)
;

BatchRenderedVideoFfmpegCommandPlan
batchRenderedVideoFfmpegCommandPlanFromParts(
    const BatchRenderedVideoFfmpegFramePlan & framePlan,
    const BatchRenderedVideoFfmpegFilterPlan & filterPlan,
    const BatchRenderedVideoFfmpegVideoPlan & videoPlan,
    const BatchRenderedVideoOutputPlan & outputPlan)
;

BatchRenderedVideoFfmpegExecutionPlan
batchRenderedVideoFfmpegExecutionPlanFromCommand(
    const BatchRenderedVideoFfmpegCommandPlan & commandPlan)
;

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoFfmpegExecutionPlan & ffmpegExecutionPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan,
    const BatchRenderedVideoMediaProbeJsonPlan & mediaProbeJsonPlan,
    const BatchRenderedVideoMediaProbeResultPlan & mediaProbeResultPlan,
    const BatchRenderedVideoMediaProbeValidationPlan & mediaProbeValidationPlan)
;

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoFfmpegExecutionPlan & ffmpegExecutionPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan,
    const BatchRenderedVideoMediaProbeJsonPlan & mediaProbeJsonPlan)
;

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoFfmpegExecutionPlan & ffmpegExecutionPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan)
;

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoFfmpegExecutionPlan & ffmpegExecutionPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan =
        batchRenderedVideoMediaProbeBinaryPlanFromRequestedName())
;

BatchRenderedVideoReceiptHashValidationPlan
batchRenderedVideoReceiptHashValidationPlanFromExecution(
    const BatchRenderedVideoOutputVerificationExecutionPlan & executionPlan)
;

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanWithReceiptHashValidation(
    const BatchRenderedVideoOutputVerificationExecutionPlan & executionPlan,
    const BatchRenderedVideoReceiptHashValidationPlan & receiptHashPlan)
;

BatchRenderedVideoOutputVerificationDecisionPlan
batchRenderedVideoOutputVerificationDecisionPlanFromExecution(
    const BatchRenderedVideoOutputVerificationExecutionPlan & executionPlan)
;

QString batchRenderedVideoOutputVerificationResultFailureReason(
    const BatchRenderedVideoOutputVerificationDecisionPlan & decisionPlan)
;

BatchRenderedVideoOutputVerificationResultPlan
batchRenderedVideoOutputVerificationResultPlanFromDecision(
    const BatchRenderedVideoOutputVerificationDecisionPlan & decisionPlan)
;

QString batchRenderedVideoOutputVerificationReportPathFromOutputPath(
    const QString & outputPath)
;

BatchRenderedVideoOutputVerificationReportPlan
batchRenderedVideoOutputVerificationReportPlanFromResult(
    const BatchRenderedVideoOutputVerificationResultPlan & resultPlan)
;

QString batchRenderedVideoOutputVerificationReportContentFailureCategory(
    const QString & failureReason)
;

BatchRenderedVideoOutputVerificationReportContentPlan
batchRenderedVideoOutputVerificationReportContentPlanFromReport(
    const BatchRenderedVideoOutputVerificationReportPlan & reportPlan)
;

QString batchRenderedVideoOutputVerificationReportTemporaryPath(
    const QString & reportPath)
;

BatchRenderedVideoOutputVerificationReportWriterPlan
batchRenderedVideoOutputVerificationReportWriterPlanFromContent(
    const BatchRenderedVideoOutputVerificationReportContentPlan & contentPlan)
;

BatchRenderedVideoRunnerPrerequisites
batchRenderedVideoRunnerPrerequisitesForCurrentBuild()
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    int inputClipCount,
    const BatchRenderedVideoRenderSettings & settings,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan =
        batchRenderedVideoMediaProbeBinaryPlanFromRequestedName())
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    int inputClipCount,
    const BatchRenderedVideoRenderSettings & settings =
        batchRenderedVideoDefaultRenderSettings())
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    int inputClipCount,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan)
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request)
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    const BatchRenderedVideoRenderSettings & settings)
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    const BatchRenderedVideoRenderSettings & settings,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan)
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    const BatchRenderedVideoRenderSettings & settings,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan)
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanWithSourceAudio(
    const BatchRenderedVideoJobPlan & preflightPlan,
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan)
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanWithMetadata(
    const BatchRenderedVideoJobPlan & preflightPlan,
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoRenderSettings & settings)
;

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanWithMetadata(
    const BatchRenderedVideoJobPlan & preflightPlan,
    const BatchRenderedVideoSourceMetadata & metadata)
;

QString batchRenderedVideoJobPlanFirstBlocker(
    const BatchRenderedVideoJobPlan & plan)
;

QString batchRenderedVideoTargetSummary(const BatchRenderedVideoTarget & target)
;

QString batchRenderedVideoTargetSummary(const BatchExportFormatRequest & request)
;

QString batchRenderedVideoEncoderPresetSummary(
    const BatchRenderedVideoEncoderPreset & preset)
;

QString batchRenderedVideoEncoderPresetSummary(
    const BatchExportFormatRequest & request)
;

QString batchRenderedVideoFfmpegVideoPlanSummary(
    const BatchRenderedVideoFfmpegVideoPlan & plan)
;

QString batchRenderedVideoFfmpegVideoPlanSummary(
    const BatchExportFormatRequest & request)
;

QString batchRenderedVideoFfmpegFilterPlanSummary(
    const BatchRenderedVideoFfmpegFilterPlan & plan)
;

QString batchRenderedVideoOptionalFilterPlanSummary(
    const BatchRenderedVideoOptionalFilterPlan & plan)
;

QString batchRenderedVideoSourceAudioPlanSummary(
    const BatchRenderedVideoSourceAudioPlan & plan)
;

QString batchRenderedVideoSourceAudioExtractionPlanSummary(
    const BatchRenderedVideoSourceAudioExtractionPlan & plan)
;

QString batchRenderedVideoSourceAudioExtractionExecutionPlanSummary(
    const BatchRenderedVideoSourceAudioExtractionExecutionPlan & plan)
;

QString batchRenderedVideoFfmpegAudioInputHandoffPlanSummary(
    const BatchRenderedVideoFfmpegAudioInputHandoffPlan & plan)
;

QString batchRenderedVideoFfmpegAudioInputPlanSummary(
    const BatchRenderedVideoFfmpegAudioInputPlan & plan)
;

QString batchRenderedVideoAudioMuxPrerequisitesPlanSummary(
    const BatchRenderedVideoAudioMuxPrerequisitesPlan & plan)
;

QString batchRenderedVideoAudioMuxExecutionPlanSummary(
    const BatchRenderedVideoAudioMuxExecutionPlan & plan)
;

QString batchRenderedVideoFfmpegAudioPlanSummary(
    const BatchRenderedVideoFfmpegAudioPlan & plan)
;

QString batchRenderedVideoFfmpegFramePlanSummary(
    const BatchRenderedVideoFfmpegFramePlan & plan)
;

QString batchRenderedVideoReceiptApplicationPlanSummary(
    const BatchRenderedVideoReceiptApplicationPlan & plan)
;

QString batchRenderedVideoFrameProcessingPlanSummary(
    const BatchRenderedVideoFrameProcessingPlan & plan)
;

QString batchRenderedVideoFfmpegCommandPlanSummary(
    const BatchRenderedVideoFfmpegCommandPlan & plan)
;

QString batchRenderedVideoFfmpegExecutionPlanSummary(
    const BatchRenderedVideoFfmpegExecutionPlan & plan)
;

QString batchRenderedVideoFfmpegBinaryPlanSummary(
    const BatchRenderedVideoFfmpegBinaryPlan & plan)
;

QString batchRenderedVideoMediaProbeBinaryPlanSummary(
    const BatchRenderedVideoMediaProbeBinaryPlan & plan)
;

QString batchRenderedVideoMediaProbeCommandPlanSummary(
    const BatchRenderedVideoMediaProbeCommandPlan & plan)
;

QString batchRenderedVideoMediaProbeJsonPlanSummary(
    const BatchRenderedVideoMediaProbeJsonPlan & plan)
;

QString batchRenderedVideoMediaProbeResultPlanSummary(
    const BatchRenderedVideoMediaProbeResultPlan & plan)
;

QString batchRenderedVideoMediaProbeValidationPlanSummary(
    const BatchRenderedVideoMediaProbeValidationPlan & plan)
;

QString batchRenderedVideoSourceMetadataSummary(
    const BatchRenderedVideoSourceMetadata & metadata)
;

QString batchRenderedVideoSourceMetadataSummary(
    const BatchRenderedVideoJobPlan & plan)
;

QString batchRenderedVideoRenderSettingsSummary(
    const BatchRenderedVideoRenderSettings & settings)
;

QString batchRenderedVideoOutputPlanSummary(
    const BatchRenderedVideoOutputPlan & plan)
;

QString batchRenderedVideoOutputVerificationPlanSummary(
    const BatchRenderedVideoOutputVerificationPlan & plan)
;

QString batchRenderedVideoOutputVerificationExecutionPlanSummary(
    const BatchRenderedVideoOutputVerificationExecutionPlan & plan)
;

QString batchRenderedVideoReceiptHashValidationPlanSummary(
    const BatchRenderedVideoReceiptHashValidationPlan & plan)
;

QString batchRenderedVideoOutputVerificationDecisionPlanSummary(
    const BatchRenderedVideoOutputVerificationDecisionPlan & plan)
;

QString batchRenderedVideoOutputVerificationResultPlanSummary(
    const BatchRenderedVideoOutputVerificationResultPlan & plan)
;

QString batchRenderedVideoOutputVerificationReportPlanSummary(
    const BatchRenderedVideoOutputVerificationReportPlan & plan)
;

QString batchRenderedVideoReportContentListSummary(
    const QStringList & values)
;

QString batchRenderedVideoOutputVerificationReportContentPlanSummary(
    const BatchRenderedVideoOutputVerificationReportContentPlan & plan)
;

QString batchRenderedVideoOutputVerificationReportWriterPlanSummary(
    const BatchRenderedVideoOutputVerificationReportWriterPlan & plan)
;

QString batchRenderedVideoOutputPlanSummary(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request)
;

QString batchRenderedVideoRunnerPrerequisitesSummary(
    const BatchRenderedVideoRunnerPrerequisites & prerequisites)
;

QString batchRenderedVideoJobPlanSummary(
    const BatchRenderedVideoJobPlan & plan)
;


#endif // BATCHRENDEREDVIDEOPLAN_H
