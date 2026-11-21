#include "BatchRenderedVideoPlan.h"

#include <QCoreApplication>
#include <QDir>
#include <QFileInfo>
#include <QLocale>
#include <QRegularExpression>
#include <QStandardPaths>

#include <cmath>

QString batchRenderedVideoJobPlanSummary(
    const BatchRenderedVideoJobPlan & plan)
{
    const QString blocker = batchRenderedVideoJobPlanFirstBlocker(plan);
    return QStringLiteral("%1 %2 %3 %4 %5 %6 %7 %8 %9 %10 %11 %12 %13 %14 %15 %16 %17 %18 %19 %20 %21 %22 %23 %24 %25 %26 %27 %28 %29 %30 %31 %32 %33 %34 %35 %36 %37 preflight-ready=%38 runnable=%39 first-blocker=%40")
        .arg(batchExportFormatRequestSummary(plan.request))
        .arg(batchRenderedVideoTargetSummary(plan.target))
        .arg(batchRenderedVideoEncoderPresetSummary(plan.encoderPreset))
        .arg(batchRenderedVideoFfmpegVideoPlanSummary(plan.ffmpegVideoPlan))
        .arg(batchRenderedVideoFfmpegFilterPlanSummary(plan.ffmpegFilterPlan))
        .arg(batchRenderedVideoOptionalFilterPlanSummary(
            plan.optionalFilterPlan))
        .arg(batchRenderedVideoSourceAudioPlanSummary(plan.sourceAudioPlan))
        .arg(batchRenderedVideoSourceAudioExtractionPlanSummary(
            plan.sourceAudioExtractionPlan))
        .arg(batchRenderedVideoSourceAudioExtractionExecutionPlanSummary(
            plan.sourceAudioExtractionExecutionPlan))
        .arg(batchRenderedVideoFfmpegAudioInputHandoffPlanSummary(
            plan.ffmpegAudioInputHandoffPlan))
        .arg(batchRenderedVideoFfmpegAudioInputPlanSummary(
            plan.ffmpegAudioInputPlan))
        .arg(batchRenderedVideoAudioMuxPrerequisitesPlanSummary(
            plan.audioMuxPrerequisitesPlan))
        .arg(batchRenderedVideoAudioMuxExecutionPlanSummary(
            plan.audioMuxExecutionPlan))
        .arg(batchRenderedVideoFfmpegAudioPlanSummary(plan.ffmpegAudioPlan))
        .arg(batchRenderedVideoFfmpegBinaryPlanSummary(plan.ffmpegBinaryPlan))
        .arg(batchRenderedVideoMediaProbeBinaryPlanSummary(
            plan.mediaProbeBinaryPlan))
        .arg(batchRenderedVideoMediaProbeCommandPlanSummary(
            plan.mediaProbeCommandPlan))
        .arg(batchRenderedVideoMediaProbeJsonPlanSummary(
            plan.mediaProbeJsonPlan))
        .arg(batchRenderedVideoMediaProbeResultPlanSummary(
            plan.mediaProbeResultPlan))
        .arg(batchRenderedVideoMediaProbeValidationPlanSummary(
            plan.mediaProbeValidationPlan))
        .arg(batchRenderedVideoSourceMetadataSummary(plan))
        .arg(batchRenderedVideoRenderSettingsSummary(plan.renderSettings))
        .arg(batchRenderedVideoFfmpegFramePlanSummary(plan.ffmpegFramePlan))
        .arg(batchRenderedVideoReceiptApplicationPlanSummary(
            plan.receiptApplicationPlan))
        .arg(batchRenderedVideoFrameProcessingPlanSummary(
            plan.frameProcessingPlan))
        .arg(batchRenderedVideoFfmpegCommandPlanSummary(plan.ffmpegCommandPlan))
        .arg(batchRenderedVideoFfmpegExecutionPlanSummary(
            plan.ffmpegExecutionPlan))
        .arg(batchRenderedVideoOutputPlanSummary(plan.outputPlan))
        .arg(batchRenderedVideoOutputVerificationPlanSummary(
            plan.outputVerificationPlan))
        .arg(batchRenderedVideoOutputVerificationExecutionPlanSummary(
            plan.outputVerificationExecutionPlan))
        .arg(batchRenderedVideoReceiptHashValidationPlanSummary(
            plan.receiptHashValidationPlan))
        .arg(batchRenderedVideoOutputVerificationDecisionPlanSummary(
            plan.outputVerificationDecisionPlan))
        .arg(batchRenderedVideoOutputVerificationResultPlanSummary(
            plan.outputVerificationResultPlan))
        .arg(batchRenderedVideoOutputVerificationReportPlanSummary(
            plan.outputVerificationReportPlan))
        .arg(batchRenderedVideoOutputVerificationReportContentPlanSummary(
            plan.outputVerificationReportContentPlan))
        .arg(batchRenderedVideoOutputVerificationReportWriterPlanSummary(
            plan.outputVerificationReportWriterPlan))
        .arg(batchRenderedVideoRunnerPrerequisitesSummary(plan.runnerPrerequisites))
        .arg(plan.preflightReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.runnable ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(blocker.isEmpty()
            ? QStringLiteral("none")
            : blocker);
}

QString batchRenderedVideoRunnerPrerequisitesSummary(
    const BatchRenderedVideoRunnerPrerequisites & prerequisites)
{
    return QStringLiteral("runner-processing-parity-ready=%1 runner-frame-processing-ready=%2 runner-audio-mux-ready=%3 runner-ffmpeg-execution-ready=%4 runner-output-verification-ready=%5 runner-headless-export-ready=%6 runner-video-only-ready=%7 runner-ready=%8 runner-limitation=%9 runner-reason=%10")
        .arg(prerequisites.processingParityReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(prerequisites.frameProcessingReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(prerequisites.audioMuxReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(prerequisites.ffmpegExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(prerequisites.outputVerificationReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(prerequisites.headlessRunnerReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(prerequisites.videoOnlyRunnerReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(prerequisites.ready ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(prerequisites.limitation.isEmpty() ? QStringLiteral("none") : prerequisites.limitation)
        .arg(prerequisites.reason.isEmpty() ? QStringLiteral("none") : prerequisites.reason);
}

QString batchRenderedVideoOutputPlanSummary(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request)
{
    return batchRenderedVideoOutputPlanSummary(
        batchRenderedVideoOutputPlanFromPaths(
            inputPath,
            outputPath,
            batchRenderedVideoTargetFromRequest(request)));
}

QString batchRenderedVideoOutputVerificationReportWriterPlanSummary(
    const BatchRenderedVideoOutputVerificationReportWriterPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-report-writer-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-report-writer-input=%1").arg(plan.serializationInput.isEmpty() ? QStringLiteral("unspecified") : plan.serializationInput)
        << QStringLiteral("output-verification-report-writer-output-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-report-writer-report-path=%1").arg(plan.reportPath.isEmpty() ? QStringLiteral("unspecified") : plan.reportPath)
        << QStringLiteral("output-verification-report-writer-temp-path=%1").arg(plan.temporaryReportPath.isEmpty() ? QStringLiteral("unspecified") : plan.temporaryReportPath)
        << QStringLiteral("output-verification-report-writer-format=%1").arg(plan.reportFormat.isEmpty() ? QStringLiteral("unspecified") : plan.reportFormat)
        << QStringLiteral("output-verification-report-writer-schema=%1").arg(plan.reportSchema.isEmpty() ? QStringLiteral("unspecified") : plan.reportSchema)
        << QStringLiteral("output-verification-report-writer-encoding=%1").arg(plan.reportEncoding.isEmpty() ? QStringLiteral("unspecified") : plan.reportEncoding)
        << QStringLiteral("output-verification-report-writer-mode=%1").arg(plan.writeMode.isEmpty() ? QStringLiteral("unspecified") : plan.writeMode)
        << QStringLiteral("output-verification-report-writer-kind=%1").arg(plan.reportKind.isEmpty() ? QStringLiteral("unspecified") : plan.reportKind)
        << QStringLiteral("output-verification-report-writer-active-error-category=%1").arg(plan.activeErrorCategory.isEmpty() ? QStringLiteral("unspecified") : plan.activeErrorCategory)
        << QStringLiteral("output-verification-report-writer-content-contract-ready=%1").arg(plan.outputVerificationReportContentContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-schema-detail-ready=%1").arg(plan.schemaDetailContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-path-ready=%1").arg(plan.reportPathReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-temp-path-planned=%1").arg(plan.temporaryReportPathPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-temp-path-ready=%1").arg(plan.temporaryReportPathReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-format-ready=%1").arg(plan.reportFormatReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-schema-ready=%1").arg(plan.reportSchemaReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-encoding-ready=%1").arg(plan.reportEncodingReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-mode-ready=%1").arg(plan.writeModeReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-preflight-planned=%1").arg(plan.writerPreflightPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-serialization-input-ready=%1").arg(plan.serializationInputReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-serialization-preflight-planned=%1").arg(plan.serializationPreflightPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-parent-dir-preflight-planned=%1").arg(plan.parentDirectoryPreflightPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-file-create-preflight-planned=%1").arg(plan.fileCreationPreflightPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-write-preflight-planned=%1").arg(plan.writePreflightPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-cleanup-preflight-planned=%1").arg(plan.cleanupPreflightPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-json-serialization-planned=%1").arg(plan.jsonSerializationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-json-serialization-owned=%1").arg(plan.jsonSerializationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-json-serialization-ready=%1").arg(plan.jsonSerializationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-parent-dir-owned=%1").arg(plan.parentDirectoryOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-file-create-planned=%1").arg(plan.reportFileCreationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-file-create-owned=%1").arg(plan.reportFileCreationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-write-planned=%1").arg(plan.reportWritePlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-write-owned=%1").arg(plan.reportWriteOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-file-ready=%1").arg(plan.reportFileReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-write-ready=%1").arg(plan.reportWriteReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-report-ready=%1").arg(plan.reportReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-output-verification-ready=%1").arg(plan.outputVerificationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-failure-reason=%1").arg(plan.failureReason.isEmpty() ? QStringLiteral("none") : plan.failureReason)
        << QStringLiteral("output-verification-report-writer-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-writer-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoOutputVerificationReportContentPlanSummary(
    const BatchRenderedVideoOutputVerificationReportContentPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-report-content-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-report-content-output-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-report-content-result-state=%1").arg(plan.resultState.isEmpty() ? QStringLiteral("unspecified") : plan.resultState)
        << QStringLiteral("output-verification-report-content-kind=%1").arg(plan.reportKind.isEmpty() ? QStringLiteral("unspecified") : plan.reportKind)
        << QStringLiteral("output-verification-report-content-path=%1").arg(plan.reportPath.isEmpty() ? QStringLiteral("unspecified") : plan.reportPath)
        << QStringLiteral("output-verification-report-content-format=%1").arg(plan.reportFormat.isEmpty() ? QStringLiteral("unspecified") : plan.reportFormat)
        << QStringLiteral("output-verification-report-content-schema=%1").arg(plan.reportSchema.isEmpty() ? QStringLiteral("unspecified") : plan.reportSchema)
        << QStringLiteral("output-verification-report-content-top-level-keys=%1").arg(batchRenderedVideoReportContentListSummary(plan.topLevelKeys))
        << QStringLiteral("output-verification-report-content-expected-keys=%1").arg(batchRenderedVideoReportContentListSummary(plan.expectedOutputKeys))
        << QStringLiteral("output-verification-report-content-check-keys=%1").arg(batchRenderedVideoReportContentListSummary(plan.verificationCheckKeys))
        << QStringLiteral("output-verification-report-content-artifact-keys=%1").arg(batchRenderedVideoReportContentListSummary(plan.artifactKeys))
        << QStringLiteral("output-verification-report-content-error-categories=%1").arg(batchRenderedVideoReportContentListSummary(plan.plannedErrorCategories))
        << QStringLiteral("output-verification-report-content-active-error-category=%1").arg(plan.activeErrorCategory.isEmpty() ? QStringLiteral("unspecified") : plan.activeErrorCategory)
        << QStringLiteral("output-verification-report-content-report-contract-ready=%1").arg(plan.outputVerificationReportContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-path-ready=%1").arg(plan.reportPathReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-format-ready=%1").arg(plan.reportFormatReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-schema-ready=%1").arg(plan.reportSchemaReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-kind-ready=%1").arg(plan.reportKindReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-planned=%1").arg(plan.reportContentPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-failure-content-planned=%1").arg(plan.failureReportContentPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-accepted-content-planned=%1").arg(plan.acceptedReportContentPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-top-level-keys-planned=%1").arg(plan.topLevelKeysPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-expected-keys-planned=%1").arg(plan.expectedOutputKeysPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-check-keys-planned=%1").arg(plan.verificationCheckKeysPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-artifact-keys-planned=%1").arg(plan.artifactKeysPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-error-categories-planned=%1").arg(plan.errorCategoriesPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-active-error-category-planned=%1").arg(plan.activeErrorCategoryPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-schema-detail-ready=%1").arg(plan.schemaDetailContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-json-serialization-planned=%1").arg(plan.jsonSerializationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-json-serialization-owned=%1").arg(plan.jsonSerializationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-write-planned=%1").arg(plan.reportWritePlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-write-owned=%1").arg(plan.reportWriteOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-report-ready=%1").arg(plan.reportReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-output-verification-ready=%1").arg(plan.outputVerificationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-failure-reason=%1").arg(plan.failureReason.isEmpty() ? QStringLiteral("none") : plan.failureReason)
        << QStringLiteral("output-verification-report-content-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoReportContentListSummary(
    const QStringList & values)
{
    return values.isEmpty()
        ? QStringLiteral("none")
        : values.join(QLatin1Char('|'));
}

QString batchRenderedVideoOutputVerificationReportPlanSummary(
    const BatchRenderedVideoOutputVerificationReportPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-report-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-report-output-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-report-output-extension=%1").arg(plan.expectedExtension.isEmpty() ? QStringLiteral("unspecified") : plan.expectedExtension)
        << QStringLiteral("output-verification-report-expected-codec=%1").arg(plan.expectedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.expectedCodec)
        << QStringLiteral("output-verification-report-expected-container=%1").arg(plan.expectedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.expectedContainer)
        << QStringLiteral("output-verification-report-expected-frame-count=%1").arg(plan.expectedFrameCount)
        << QStringLiteral("output-verification-report-expected-duration-seconds=%1").arg(QLocale::c().toString(plan.expectedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-report-result-state=%1").arg(plan.resultState.isEmpty() ? QStringLiteral("unspecified") : plan.resultState)
        << QStringLiteral("output-verification-report-kind=%1").arg(plan.reportKind.isEmpty() ? QStringLiteral("unspecified") : plan.reportKind)
        << QStringLiteral("output-verification-report-path=%1").arg(plan.reportPath.isEmpty() ? QStringLiteral("unspecified") : plan.reportPath)
        << QStringLiteral("output-verification-report-format=%1").arg(plan.reportFormat.isEmpty() ? QStringLiteral("unspecified") : plan.reportFormat)
        << QStringLiteral("output-verification-report-schema=%1").arg(plan.reportSchema.isEmpty() ? QStringLiteral("unspecified") : plan.reportSchema)
        << QStringLiteral("output-verification-report-result-contract-ready=%1").arg(plan.outputVerificationResultContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-result-report-planned=%1").arg(plan.resultReportPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-failure-report-planned=%1").arg(plan.failureReportPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-accepted-report-planned=%1").arg(plan.acceptedOutputReportPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-output-accepted=%1").arg(plan.outputAccepted ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-artifact-planned=%1").arg(plan.reportArtifactPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-path-planned=%1").arg(plan.reportPathPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-path-ready=%1").arg(plan.reportPathReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-format-ready=%1").arg(plan.reportFormatReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-schema-ready=%1").arg(plan.reportSchemaReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-content-planned=%1").arg(plan.reportContentPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-failure-content-planned=%1").arg(plan.failureReportContentPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-accepted-content-planned=%1").arg(plan.acceptedReportContentPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-file-create-planned=%1").arg(plan.reportFileCreationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-write-planned=%1").arg(plan.reportWritePlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-file-create-owned=%1").arg(plan.reportFileCreationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-write-owned=%1").arg(plan.reportWriteOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-file-ready=%1").arg(plan.reportFileReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-write-ready=%1").arg(plan.reportWriteReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-ready=%1").arg(plan.reportReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-output-verification-ready=%1").arg(plan.outputVerificationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-failure-reason=%1").arg(plan.failureReason.isEmpty() ? QStringLiteral("none") : plan.failureReason)
        << QStringLiteral("output-verification-report-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-report-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoOutputVerificationResultPlanSummary(
    const BatchRenderedVideoOutputVerificationResultPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-result-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-result-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-result-extension=%1").arg(plan.expectedExtension.isEmpty() ? QStringLiteral("unspecified") : plan.expectedExtension)
        << QStringLiteral("output-verification-result-expected-codec=%1").arg(plan.expectedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.expectedCodec)
        << QStringLiteral("output-verification-result-expected-container=%1").arg(plan.expectedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.expectedContainer)
        << QStringLiteral("output-verification-result-expected-frame-count=%1").arg(plan.expectedFrameCount)
        << QStringLiteral("output-verification-result-expected-duration-seconds=%1").arg(QLocale::c().toString(plan.expectedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-result-state=%1").arg(plan.resultState.isEmpty() ? QStringLiteral("unspecified") : plan.resultState)
        << QStringLiteral("output-verification-result-decision-contract-ready=%1").arg(plan.outputVerificationDecisionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-file-checks-ready=%1").arg(plan.fileChecksReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-probe-validation-ready=%1").arg(plan.mediaProbeValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-receipt-hash-ready=%1").arg(plan.receiptHashValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-verification-exec-ready=%1").arg(plan.verificationExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-decision-ready=%1").arg(plan.verificationDecisionReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-accepted=%1").arg(plan.outputAccepted ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-report-planned=%1").arg(plan.resultReportPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-failure-report-planned=%1").arg(plan.failureReportPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-accepted-report-planned=%1").arg(plan.acceptedOutputReportPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-report-owned=%1").arg(plan.resultReportOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-failure-report-owned=%1").arg(plan.failureReportOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-accepted-report-owned=%1").arg(plan.acceptedOutputReportOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-report-ready=%1").arg(plan.resultReportReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-failure-report-ready=%1").arg(plan.failureReportReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-accepted-report-ready=%1").arg(plan.acceptedOutputReportReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-output-verification-ready=%1").arg(plan.outputVerificationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-failure-reason=%1").arg(plan.failureReason.isEmpty() ? QStringLiteral("none") : plan.failureReason)
        << QStringLiteral("output-verification-result-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-result-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoOutputVerificationDecisionPlanSummary(
    const BatchRenderedVideoOutputVerificationDecisionPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-decision-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-decision-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-decision-extension=%1").arg(plan.expectedExtension.isEmpty() ? QStringLiteral("unspecified") : plan.expectedExtension)
        << QStringLiteral("output-verification-decision-expected-codec=%1").arg(plan.expectedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.expectedCodec)
        << QStringLiteral("output-verification-decision-expected-container=%1").arg(plan.expectedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.expectedContainer)
        << QStringLiteral("output-verification-decision-expected-frame-count=%1").arg(plan.expectedFrameCount)
        << QStringLiteral("output-verification-decision-expected-duration-seconds=%1").arg(QLocale::c().toString(plan.expectedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-decision-exec-contract-ready=%1").arg(plan.outputVerificationExecutionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-file-exists-planned=%1").arg(plan.fileExistenceCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-nonempty-planned=%1").arg(plan.nonEmptyCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-file-checks-planned=%1").arg(plan.fileChecksPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-filesystem-inspection-owned=%1").arg(plan.filesystemInspectionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-file-exists-owned=%1").arg(plan.fileExistenceCheckOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-nonempty-owned=%1").arg(plan.nonEmptyCheckOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-file-checks-owned=%1").arg(plan.fileChecksOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-file-exists-ready=%1").arg(plan.fileExistenceCheckReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-nonempty-ready=%1").arg(plan.nonEmptyCheckReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-file-checks-ready=%1").arg(plan.fileChecksReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-probe-validation-contract-ready=%1").arg(plan.mediaProbeValidationContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-probe-validation-planned=%1").arg(plan.mediaProbeValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-probe-validation-owned=%1").arg(plan.mediaProbeValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-probe-validation-ready=%1").arg(plan.mediaProbeValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-codec-container-planned=%1").arg(plan.codecContainerComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-codec-container-ready=%1").arg(plan.codecContainerComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-frame-count-planned=%1").arg(plan.frameCountComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-frame-count-ready=%1").arg(plan.frameCountComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-duration-planned=%1").arg(plan.durationComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-duration-ready=%1").arg(plan.durationComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-receipt-hash-source=%1").arg(plan.receiptHashValidationSource.isEmpty() ? QStringLiteral("unspecified") : plan.receiptHashValidationSource)
        << QStringLiteral("output-verification-decision-receipt-hash-contract-ready=%1").arg(plan.receiptHashValidationContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-receipt-hash-planned=%1").arg(plan.receiptHashValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-receipt-compare-planned=%1").arg(plan.receiptComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-output-hash-compare-planned=%1").arg(plan.outputHashComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-receipt-read-owned=%1").arg(plan.receiptReadOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-output-hash-read-owned=%1").arg(plan.outputHashReadOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-receipt-compare-owned=%1").arg(plan.receiptComparisonOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-output-hash-compare-owned=%1").arg(plan.outputHashComparisonOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-receipt-compare-ready=%1").arg(plan.receiptComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-output-hash-compare-ready=%1").arg(plan.outputHashComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-receipt-hash-owned=%1").arg(plan.receiptHashValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-receipt-hash-ready=%1").arg(plan.receiptHashValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-verification-exec-ready=%1").arg(plan.verificationExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-ready=%1").arg(plan.verificationDecisionReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-accepted=%1").arg(plan.outputAccepted ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-decision-receipt-hash-reason=%1").arg(plan.receiptHashValidationReason.isEmpty() ? QStringLiteral("none") : plan.receiptHashValidationReason)
        << QStringLiteral("output-verification-decision-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoReceiptHashValidationPlanSummary(
    const BatchRenderedVideoReceiptHashValidationPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-receipt-hash-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-receipt-hash-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-receipt-hash-extension=%1").arg(plan.expectedExtension.isEmpty() ? QStringLiteral("unspecified") : plan.expectedExtension)
        << QStringLiteral("output-verification-receipt-hash-expected-codec=%1").arg(plan.expectedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.expectedCodec)
        << QStringLiteral("output-verification-receipt-hash-expected-container=%1").arg(plan.expectedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.expectedContainer)
        << QStringLiteral("output-verification-receipt-hash-expected-frame-count=%1").arg(plan.expectedFrameCount)
        << QStringLiteral("output-verification-receipt-hash-expected-duration-seconds=%1").arg(QLocale::c().toString(plan.expectedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-receipt-hash-exec-contract-ready=%1").arg(plan.outputVerificationExecutionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-validation-planned=%1").arg(plan.validationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-receipt-compare-planned=%1").arg(plan.receiptComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-output-hash-planned=%1").arg(plan.outputHashComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-receipt-read-owned=%1").arg(plan.receiptReadOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-output-hash-read-owned=%1").arg(plan.outputHashReadOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-receipt-compare-owned=%1").arg(plan.receiptComparisonOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-output-hash-compare-owned=%1").arg(plan.outputHashComparisonOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-receipt-compare-ready=%1").arg(plan.receiptComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-output-hash-ready=%1").arg(plan.outputHashComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-owned=%1").arg(plan.receiptHashValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-ready=%1").arg(plan.receiptHashValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoOutputVerificationExecutionPlanSummary(
    const BatchRenderedVideoOutputVerificationExecutionPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-exec-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-exec-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-exec-extension=%1").arg(plan.expectedExtension.isEmpty() ? QStringLiteral("unspecified") : plan.expectedExtension)
        << QStringLiteral("output-verification-exec-expected-codec=%1").arg(plan.expectedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.expectedCodec)
        << QStringLiteral("output-verification-exec-expected-container=%1").arg(plan.expectedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.expectedContainer)
        << QStringLiteral("output-verification-exec-expected-frame-count=%1").arg(plan.expectedFrameCount)
        << QStringLiteral("output-verification-exec-expected-duration-seconds=%1").arg(QLocale::c().toString(plan.expectedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-exec-probe=%1").arg(plan.mediaProbeExecutable.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeExecutable)
        << QStringLiteral("output-verification-exec-probe-binary-source=%1").arg(plan.mediaProbeBinarySource.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeBinarySource)
        << QStringLiteral("output-verification-exec-probe-binary-request=%1").arg(plan.mediaProbeRequestedExecutable.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeRequestedExecutable)
        << QStringLiteral("output-verification-exec-probe-binary-resolved=%1").arg(plan.mediaProbeResolvedExecutable.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeResolvedExecutable)
        << QStringLiteral("output-verification-exec-probe-binary-path-search-owned=%1").arg(plan.mediaProbePathSearchOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-binary-path-search-attempted=%1").arg(plan.mediaProbePathSearchAttempted ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-binary-found=%1").arg(plan.mediaProbeFoundOnPath ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-binary-command-ready=%1").arg(plan.mediaProbeCommandReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-binary-reason=%1").arg(plan.mediaProbeBinaryReason.isEmpty() ? QStringLiteral("none") : plan.mediaProbeBinaryReason)
        << QStringLiteral("output-verification-exec-output-contract-ready=%1").arg(plan.outputVerificationContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-contract-ready=%1").arg(plan.ffmpegExecutionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-audio-args=%1").arg(plan.ffmpegAudioArguments.isEmpty() ? QStringLiteral("unspecified") : plan.ffmpegAudioArguments)
        << QStringLiteral("output-verification-exec-ffmpeg-audio-transition-source=%1").arg(plan.ffmpegAudioTransitionSource.isEmpty() ? QStringLiteral("unspecified") : plan.ffmpegAudioTransitionSource)
        << QStringLiteral("output-verification-exec-ffmpeg-audio-transition-args=%1").arg(plan.ffmpegAudioTransitionArguments.isEmpty() ? QStringLiteral("unspecified") : plan.ffmpegAudioTransitionArguments)
        << QStringLiteral("output-verification-exec-ffmpeg-audio-contract-ready=%1").arg(plan.ffmpegAudioContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-audio-mux-exec-contract-ready=%1").arg(plan.ffmpegAudioMuxExecutionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-audio-mux-planned=%1").arg(plan.ffmpegAudioMuxPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-audio-mux-args-handoff-planned=%1").arg(plan.ffmpegAudioMuxArgumentHandoffPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-audio-sync-planned=%1").arg(plan.ffmpegAudioSyncValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-audio-mux-exec-ready=%1").arg(plan.ffmpegAudioMuxExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-audio-mux-command-planned=%1").arg(plan.ffmpegMuxedAudioCommandPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-audio-mux-command-ready=%1").arg(plan.ffmpegMuxedAudioCommandReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ffmpeg-audio-owned=%1").arg(plan.ffmpegAudioInputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-file-exists-planned=%1").arg(plan.fileExistenceCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-nonempty-planned=%1").arg(plan.nonEmptyCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-nonempty-min-bytes=%1").arg(plan.nonEmptyMinimumBytes)
        << QStringLiteral("output-verification-exec-filesystem-inspection-owned=%1").arg(plan.filesystemInspectionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-file-exists-ready=%1").arg(plan.fileExistenceCheckReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-nonempty-ready=%1").arg(plan.nonEmptyCheckReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-codec-container-planned=%1").arg(plan.codecContainerCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-codec-container-input-ready=%1").arg(plan.codecContainerExpectationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-codec-container-validation-ready=%1").arg(plan.codecContainerValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-frame-count-planned=%1").arg(plan.frameCountCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-frame-count-input-ready=%1").arg(plan.frameCountExpectationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-frame-count-validation-ready=%1").arg(plan.frameCountValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-file-exists-owned=%1").arg(plan.fileExistenceCheckOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-nonempty-owned=%1").arg(plan.nonEmptyCheckOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-owned=%1").arg(plan.mediaProbeExecutionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-codec-container-owned=%1").arg(plan.codecContainerValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-frame-count-owned=%1").arg(plan.frameCountValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-receipt-hash-source=%1").arg(plan.receiptHashValidationSource.isEmpty() ? QStringLiteral("unspecified") : plan.receiptHashValidationSource)
        << QStringLiteral("output-verification-exec-receipt-hash-contract-ready=%1").arg(plan.receiptHashValidationContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-receipt-hash-planned=%1").arg(plan.receiptHashValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-receipt-compare-planned=%1").arg(plan.receiptComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-output-hash-compare-planned=%1").arg(plan.outputHashComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-receipt-read-owned=%1").arg(plan.receiptReadOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-output-hash-read-owned=%1").arg(plan.outputHashReadOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-receipt-compare-owned=%1").arg(plan.receiptComparisonOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-output-hash-compare-owned=%1").arg(plan.outputHashComparisonOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-receipt-compare-ready=%1").arg(plan.receiptComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-output-hash-compare-ready=%1").arg(plan.outputHashComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-receipt-hash-owned=%1").arg(plan.receiptHashValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-receipt-hash-ready=%1").arg(plan.receiptHashValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-ready=%1").arg(plan.verificationExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-command-source=%1").arg(plan.mediaProbeCommandSource.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeCommandSource)
        << QStringLiteral("output-verification-exec-probe-command-args=%1").arg(plan.mediaProbeCommandArguments.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeCommandArguments)
        << QStringLiteral("output-verification-exec-probe-command-line=%1").arg(plan.mediaProbeCommandLine.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeCommandLine)
        << QStringLiteral("output-verification-exec-probe-command-planned=%1").arg(plan.mediaProbeCommandPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-command-ready=%1").arg(plan.mediaProbeInvocationCommandReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-command-owned=%1").arg(plan.mediaProbeCommandExecutionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-command-exec-ready=%1").arg(plan.mediaProbeCommandExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-command-contract-ready=%1").arg(plan.mediaProbeCommandContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-command-reason=%1").arg(plan.mediaProbeCommandReason.isEmpty() ? QStringLiteral("none") : plan.mediaProbeCommandReason)
        << QStringLiteral("output-verification-exec-probe-json-source=%1").arg(plan.mediaProbeJsonSource.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeJsonSource)
        << QStringLiteral("output-verification-exec-probe-json-stdout-planned=%1").arg(plan.mediaProbeJsonStdoutCapturePlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-stderr-planned=%1").arg(plan.mediaProbeJsonStderrCapturePlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-exit-code-planned=%1").arg(plan.mediaProbeJsonExitCodeValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-timeout-planned=%1").arg(plan.mediaProbeJsonTimeoutPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-error-reporting-planned=%1").arg(plan.mediaProbeJsonErrorReportingPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-document-planned=%1").arg(plan.mediaProbeJsonDocumentPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-stdout-owned=%1").arg(plan.mediaProbeJsonStdoutCaptureOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-stderr-owned=%1").arg(plan.mediaProbeJsonStderrCaptureOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-exit-code-owned=%1").arg(plan.mediaProbeJsonExitCodeValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-timeout-owned=%1").arg(plan.mediaProbeJsonTimeoutOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-raw-owned=%1").arg(plan.mediaProbeJsonRawOutputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-parse-owned=%1").arg(plan.mediaProbeJsonParseOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-raw-ready=%1").arg(plan.mediaProbeJsonRawOutputReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-parse-ready=%1").arg(plan.mediaProbeJsonParseReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-error-free=%1").arg(plan.mediaProbeJsonErrorFree ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-ready=%1").arg(plan.mediaProbeJsonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-contract-ready=%1").arg(plan.mediaProbeJsonContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-json-reason=%1").arg(plan.mediaProbeJsonReason.isEmpty() ? QStringLiteral("none") : plan.mediaProbeJsonReason)
        << QStringLiteral("output-verification-exec-probe-result-source=%1").arg(plan.mediaProbeResultSource.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeResultSource)
        << QStringLiteral("output-verification-exec-probe-result-parsed-codec=%1").arg(plan.mediaProbeResultParsedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeResultParsedCodec)
        << QStringLiteral("output-verification-exec-probe-result-parsed-container=%1").arg(plan.mediaProbeResultParsedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeResultParsedContainer)
        << QStringLiteral("output-verification-exec-probe-result-parsed-frame-count=%1").arg(plan.mediaProbeResultParsedFrameCount)
        << QStringLiteral("output-verification-exec-probe-result-parsed-duration-seconds=%1").arg(QLocale::c().toString(plan.mediaProbeResultParsedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-exec-probe-result-parse-planned=%1").arg(plan.mediaProbeResultParsingPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-schema-ready=%1").arg(plan.mediaProbeResultSchemaReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-codec-container-fields-planned=%1").arg(plan.mediaProbeResultCodecContainerFieldsPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-frame-count-field-planned=%1").arg(plan.mediaProbeResultFrameCountFieldPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-duration-field-planned=%1").arg(plan.mediaProbeResultDurationFieldPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-owned=%1").arg(plan.mediaProbeResultOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-json-parse-owned=%1").arg(plan.mediaProbeResultJsonParseOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-codec-container-ready=%1").arg(plan.mediaProbeResultCodecContainerReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-frame-count-ready=%1").arg(plan.mediaProbeResultFrameCountReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-duration-ready=%1").arg(plan.mediaProbeResultDurationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-ready=%1").arg(plan.mediaProbeResultReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-contract-ready=%1").arg(plan.mediaProbeResultContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-result-reason=%1").arg(plan.mediaProbeResultReason.isEmpty() ? QStringLiteral("none") : plan.mediaProbeResultReason)
        << QStringLiteral("output-verification-exec-probe-validation-source=%1").arg(plan.mediaProbeValidationSource.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeValidationSource)
        << QStringLiteral("output-verification-exec-probe-validation-result-contract-ready=%1").arg(plan.mediaProbeResultContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-parsed-result-ready=%1").arg(plan.mediaProbeValidationParsedResultReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-ingest-planned=%1").arg(plan.mediaProbeValidationParsedResultIngestPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-ingest-owned=%1").arg(plan.mediaProbeValidationParsedResultIngestOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-ingest-ready=%1").arg(plan.mediaProbeValidationParsedResultIngestReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-codec-container-planned=%1").arg(plan.mediaProbeValidationCodecContainerComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-codec-container-ready=%1").arg(plan.mediaProbeValidationCodecContainerComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-codec-container-matches=%1").arg(plan.mediaProbeValidationCodecContainerMatches ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-frame-count-planned=%1").arg(plan.mediaProbeValidationFrameCountComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-frame-count-ready=%1").arg(plan.mediaProbeValidationFrameCountComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-frame-count-matches=%1").arg(plan.mediaProbeValidationFrameCountMatches ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-duration-planned=%1").arg(plan.mediaProbeValidationDurationComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-duration-ready=%1").arg(plan.mediaProbeValidationDurationComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-duration-matches=%1").arg(plan.mediaProbeValidationDurationMatches ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-owned=%1").arg(plan.mediaProbeValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-ready=%1").arg(plan.mediaProbeValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-contract-ready=%1").arg(plan.mediaProbeValidationContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-exec-probe-validation-reason=%1").arg(plan.mediaProbeValidationReason.isEmpty() ? QStringLiteral("none") : plan.mediaProbeValidationReason)
        << QStringLiteral("output-verification-exec-receipt-hash-reason=%1").arg(plan.receiptHashValidationReason.isEmpty() ? QStringLiteral("none") : plan.receiptHashValidationReason)
        << QStringLiteral("output-verification-exec-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoOutputVerificationPlanSummary(
    const BatchRenderedVideoOutputVerificationPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-extension=%1").arg(plan.expectedExtension.isEmpty() ? QStringLiteral("unspecified") : plan.expectedExtension)
        << QStringLiteral("output-verification-expected-codec=%1").arg(plan.expectedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.expectedCodec)
        << QStringLiteral("output-verification-expected-container=%1").arg(plan.expectedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.expectedContainer)
        << QStringLiteral("output-verification-expected-frame-count=%1").arg(plan.expectedFrameCount)
        << QStringLiteral("output-verification-expected-duration-seconds=%1").arg(QLocale::c().toString(plan.expectedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-path-ready=%1").arg(plan.outputPathReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-extension-match=%1").arg(plan.extensionMatchesTarget ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-file-exists-planned=%1").arg(plan.fileExistenceCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-nonempty-planned=%1").arg(plan.nonEmptyCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-nonempty-min-bytes=%1").arg(plan.nonEmptyMinimumBytes)
        << QStringLiteral("output-verification-filesystem-inspection-owned=%1").arg(plan.filesystemInspectionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-file-exists-ready=%1").arg(plan.fileExistenceCheckReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-nonempty-ready=%1").arg(plan.nonEmptyCheckReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-codec-container-planned=%1").arg(plan.codecContainerCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-codec-container-input-ready=%1").arg(plan.codecContainerExpectationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-codec-container-validation-ready=%1").arg(plan.codecContainerValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-frame-count-planned=%1").arg(plan.frameCountCheckPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-frame-count-input-ready=%1").arg(plan.frameCountExpectationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-frame-count-validation-ready=%1").arg(plan.frameCountValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-file-exists-owned=%1").arg(plan.fileExistenceCheckOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-nonempty-owned=%1").arg(plan.nonEmptyCheckOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-owned=%1").arg(plan.mediaProbeOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-codec-container-owned=%1").arg(plan.codecContainerCheckOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-frame-count-owned=%1").arg(plan.frameCountCheckOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-receipt-hash-owned=%1").arg(plan.receiptOrHashOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-execution-owned=%1").arg(plan.verificationExecutionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoOutputPlanSummary(
    const BatchRenderedVideoOutputPlan & plan)
{
    return QStringLiteral("rendered-output=%1 rendered-output-explicit-file=%2 rendered-output-input-clips=%3 rendered-output-ready=%4 rendered-output-reason=%5")
        .arg(plan.outputPath.isEmpty() ? QStringLiteral("unspecified") : plan.outputPath)
        .arg(plan.explicitFileOutput ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.inputClipCount)
        .arg(plan.ready ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoRenderSettingsSummary(
    const BatchRenderedVideoRenderSettings & settings)
{
    return QStringLiteral("render-settings-source=%1 render-settings-explicit-headless=%2 render-settings-gui-owned=%3 render-settings-ready=%4 render-settings-reason=%5 render-settings-resize=%6 render-settings-resize-width=%7 render-settings-resize-height=%8 render-settings-resize-height-locked=%9")
        .arg(settings.source.isEmpty() ? QStringLiteral("unspecified") : settings.source)
        .arg(settings.explicitHeadlessSettings ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(settings.guiSettingsOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(settings.ready ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(settings.reason.isEmpty() ? QStringLiteral("none") : settings.reason)
        .arg(settings.resizeEnabled ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(settings.resizeWidth)
        .arg(settings.resizeHeight)
        .arg(settings.resizeHeightLocked ? QStringLiteral("true") : QStringLiteral("false"));
}

QString batchRenderedVideoSourceMetadataSummary(
    const BatchRenderedVideoJobPlan & plan)
{
    return QStringLiteral("%1 source-metadata-attempted=%2")
        .arg(batchRenderedVideoSourceMetadataSummary(plan.sourceMetadata))
        .arg(plan.metadataAttempted ? QStringLiteral("true") : QStringLiteral("false"));
}

QString batchRenderedVideoSourceMetadataSummary(
    const BatchRenderedVideoSourceMetadata & metadata)
{
    return QStringLiteral("source-metadata=%1x%2 source-fps=%3 source-frame-count=%4 source-duration-seconds=%5 source-stretch-x=%6 source-stretch-y=%7 source-frame-count-ready=%8 source-metadata-ready=%9 source-metadata-reason=%10")
        .arg(metadata.width)
        .arg(metadata.height)
        .arg(metadata.frameRate)
        .arg(metadata.frameCount)
        .arg(QLocale::c().toString(metadata.durationSeconds, 'f', 6))
        .arg(metadata.stretchFactorX)
        .arg(metadata.stretchFactorY)
        .arg(metadata.frameCountReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(metadata.ready ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(metadata.reason.isEmpty() ? QStringLiteral("none") : metadata.reason);
}

QString batchRenderedVideoMediaProbeValidationPlanSummary(
    const BatchRenderedVideoMediaProbeValidationPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-probe-validation-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-probe-validation-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-probe-validation-expected-codec=%1").arg(plan.expectedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.expectedCodec)
        << QStringLiteral("output-verification-probe-validation-expected-container=%1").arg(plan.expectedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.expectedContainer)
        << QStringLiteral("output-verification-probe-validation-expected-frame-count=%1").arg(plan.expectedFrameCount)
        << QStringLiteral("output-verification-probe-validation-expected-duration-seconds=%1").arg(QLocale::c().toString(plan.expectedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-probe-validation-parsed-codec=%1").arg(plan.parsedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.parsedCodec)
        << QStringLiteral("output-verification-probe-validation-parsed-container=%1").arg(plan.parsedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.parsedContainer)
        << QStringLiteral("output-verification-probe-validation-parsed-frame-count=%1").arg(plan.parsedFrameCount)
        << QStringLiteral("output-verification-probe-validation-parsed-duration-seconds=%1").arg(QLocale::c().toString(plan.parsedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-probe-validation-result-source=%1").arg(plan.mediaProbeResultSource.isEmpty() ? QStringLiteral("unspecified") : plan.mediaProbeResultSource)
        << QStringLiteral("output-verification-probe-validation-result-contract-ready=%1").arg(plan.mediaProbeResultContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-parsed-result-ready=%1").arg(plan.parsedResultReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-ingest-planned=%1").arg(plan.parsedResultIngestPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-ingest-owned=%1").arg(plan.parsedResultIngestOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-ingest-ready=%1").arg(plan.parsedResultIngestReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-codec-container-planned=%1").arg(plan.codecContainerComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-codec-container-ready=%1").arg(plan.codecContainerComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-codec-container-matches=%1").arg(plan.codecContainerMatches ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-frame-count-planned=%1").arg(plan.frameCountComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-frame-count-ready=%1").arg(plan.frameCountComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-frame-count-matches=%1").arg(plan.frameCountMatches ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-duration-planned=%1").arg(plan.durationComparisonPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-duration-ready=%1").arg(plan.durationComparisonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-duration-matches=%1").arg(plan.durationMatches ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-owned=%1").arg(plan.validationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-ready=%1").arg(plan.validationReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-validation-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoMediaProbeResultPlanSummary(
    const BatchRenderedVideoMediaProbeResultPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-probe-result-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-probe-result-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-probe-result-expected-codec=%1").arg(plan.expectedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.expectedCodec)
        << QStringLiteral("output-verification-probe-result-expected-container=%1").arg(plan.expectedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.expectedContainer)
        << QStringLiteral("output-verification-probe-result-expected-frame-count=%1").arg(plan.expectedFrameCount)
        << QStringLiteral("output-verification-probe-result-expected-duration-seconds=%1").arg(QLocale::c().toString(plan.expectedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-probe-result-parsed-codec=%1").arg(plan.parsedCodec.isEmpty() ? QStringLiteral("unspecified") : plan.parsedCodec)
        << QStringLiteral("output-verification-probe-result-parsed-container=%1").arg(plan.parsedContainer.isEmpty() ? QStringLiteral("unspecified") : plan.parsedContainer)
        << QStringLiteral("output-verification-probe-result-parsed-frame-count=%1").arg(plan.parsedFrameCount)
        << QStringLiteral("output-verification-probe-result-parsed-duration-seconds=%1").arg(QLocale::c().toString(plan.parsedDurationSeconds, 'f', 6))
        << QStringLiteral("output-verification-probe-result-output-contract-ready=%1").arg(plan.outputVerificationContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-command-contract-ready=%1").arg(plan.mediaProbeCommandContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-command-owned=%1").arg(plan.mediaProbeCommandExecutionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-command-exec-ready=%1").arg(plan.mediaProbeCommandExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-parse-planned=%1").arg(plan.resultParsingPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-schema-ready=%1").arg(plan.resultSchemaReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-codec-container-fields-planned=%1").arg(plan.codecContainerFieldsPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-frame-count-field-planned=%1").arg(plan.frameCountFieldPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-duration-field-planned=%1").arg(plan.durationFieldPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-owned=%1").arg(plan.mediaProbeResultOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-json-parse-owned=%1").arg(plan.jsonParseOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-codec-container-ready=%1").arg(plan.codecContainerResultReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-frame-count-ready=%1").arg(plan.frameCountResultReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-duration-ready=%1").arg(plan.durationResultReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-ready=%1").arg(plan.resultReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-result-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoMediaProbeJsonPlanSummary(
    const BatchRenderedVideoMediaProbeJsonPlan & plan)
{
    QStringList tokens;
    tokens
        << QStringLiteral("output-verification-probe-json-source=%1").arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        << QStringLiteral("output-verification-probe-json-path=%1").arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        << QStringLiteral("output-verification-probe-json-command-line=%1").arg(plan.commandLine.isEmpty() ? QStringLiteral("unspecified") : plan.commandLine)
        << QStringLiteral("output-verification-probe-json-command-contract-ready=%1").arg(plan.mediaProbeCommandContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-command-owned=%1").arg(plan.mediaProbeCommandExecutionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-command-exec-ready=%1").arg(plan.mediaProbeCommandExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-stdout-planned=%1").arg(plan.stdoutCapturePlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-stderr-planned=%1").arg(plan.stderrCapturePlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-exit-code-planned=%1").arg(plan.exitCodeValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-timeout-planned=%1").arg(plan.timeoutPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-error-reporting-planned=%1").arg(plan.errorReportingPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-document-planned=%1").arg(plan.jsonDocumentPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-stdout-owned=%1").arg(plan.stdoutCaptureOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-stderr-owned=%1").arg(plan.stderrCaptureOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-exit-code-owned=%1").arg(plan.exitCodeValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-timeout-owned=%1").arg(plan.timeoutOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-raw-owned=%1").arg(plan.rawJsonOutputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-parse-owned=%1").arg(plan.jsonParseOwned ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-raw-ready=%1").arg(plan.rawJsonReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-parse-ready=%1").arg(plan.jsonParseReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-error-free=%1").arg(plan.errorFree ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-contract-ready=%1").arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        << QStringLiteral("output-verification-probe-json-reason=%1").arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
    return tokens.join(QLatin1Char(' '));
}

QString batchRenderedVideoMediaProbeCommandPlanSummary(
    const BatchRenderedVideoMediaProbeCommandPlan & plan)
{
    return QStringLiteral("output-verification-probe-command-source=%1 output-verification-probe-command-executable=%2 output-verification-probe-command-path=%3 output-verification-probe-command-args=%4 output-verification-probe-command-line=%5 output-verification-probe-command-output-contract-ready=%6 output-verification-probe-command-executable-ready=%7 output-verification-probe-command-planned=%8 output-verification-probe-command-ready=%9 output-verification-probe-command-owned=%10 output-verification-probe-command-exec-ready=%11 output-verification-probe-command-contract-ready=%12 output-verification-probe-command-reason=%13")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.executable.isEmpty() ? QStringLiteral("unspecified") : plan.executable)
        .arg(plan.expectedOutputPath.isEmpty() ? QStringLiteral("unspecified") : plan.expectedOutputPath)
        .arg(plan.arguments.isEmpty() ? QStringLiteral("unspecified") : plan.arguments)
        .arg(plan.commandLine.isEmpty() ? QStringLiteral("unspecified") : plan.commandLine)
        .arg(plan.outputVerificationContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.mediaProbeExecutableReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.commandPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.commandReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.executionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.executionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoMediaProbeBinaryPlanSummary(
    const BatchRenderedVideoMediaProbeBinaryPlan & plan)
{
    return QStringLiteral("output-verification-probe-binary-source=%1 output-verification-probe-binary-request=%2 output-verification-probe-binary-resolved=%3 output-verification-probe-binary-path-search-owned=%4 output-verification-probe-binary-path-search-attempted=%5 output-verification-probe-binary-found=%6 output-verification-probe-binary-command-ready=%7 output-verification-probe-binary-reason=%8")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.requestedExecutable.isEmpty() ? QStringLiteral("unspecified") : plan.requestedExecutable)
        .arg(plan.resolvedExecutable.isEmpty() ? QStringLiteral("unspecified") : plan.resolvedExecutable)
        .arg(plan.pathSearchOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.pathSearchAttempted ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.foundOnPath ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.commandExecutableReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFfmpegBinaryPlanSummary(
    const BatchRenderedVideoFfmpegBinaryPlan & plan)
{
    return QStringLiteral("ffmpeg-binary-source=%1 ffmpeg-binary-request=%2 ffmpeg-binary-resolved=%3 ffmpeg-binary-path-search-owned=%4 ffmpeg-binary-path-search-attempted=%5 ffmpeg-binary-found=%6 ffmpeg-binary-command-ready=%7 ffmpeg-binary-reason=%8")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.requestedExecutable.isEmpty() ? QStringLiteral("unspecified") : plan.requestedExecutable)
        .arg(plan.resolvedExecutable.isEmpty() ? QStringLiteral("unspecified") : plan.resolvedExecutable)
        .arg(plan.pathSearchOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.pathSearchAttempted ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.foundOnPath ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.commandExecutableReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFfmpegExecutionPlanSummary(
    const BatchRenderedVideoFfmpegExecutionPlan & plan)
{
    return QStringLiteral("ffmpeg-execution-source=%1 ffmpeg-execution-exe=%2 ffmpeg-execution-command-ready=%3 ffmpeg-execution-audio-args=%4 ffmpeg-execution-audio-transition-source=%5 ffmpeg-execution-audio-transition-args=%6 ffmpeg-execution-audio-contract-ready=%7 ffmpeg-execution-audio-mux-exec-contract-ready=%8 ffmpeg-execution-audio-mux-planned=%9 ffmpeg-execution-audio-mux-args-handoff-planned=%10 ffmpeg-execution-audio-sync-planned=%11 ffmpeg-execution-audio-mux-exec-ready=%12 ffmpeg-execution-audio-mux-command-planned=%13 ffmpeg-execution-audio-mux-command-ready=%14 ffmpeg-execution-audio-owned=%15 ffmpeg-execution-process-launch-owned=%16 ffmpeg-execution-stdin-pipe-owned=%17 ffmpeg-execution-raw-frame-feed-owned=%18 ffmpeg-execution-stderr-capture-owned=%19 ffmpeg-execution-exit-code-owned=%20 ffmpeg-execution-timeout-owned=%21 ffmpeg-execution-cleanup-owned=%22 ffmpeg-execution-ready=%23 ffmpeg-execution-contract-ready=%24 ffmpeg-execution-reason=%25")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.executable.isEmpty() ? QStringLiteral("unspecified") : plan.executable)
        .arg(plan.commandReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioArguments.isEmpty()
            ? QStringLiteral("unspecified")
            : plan.audioArguments)
        .arg(plan.audioTransitionSource.isEmpty()
            ? QStringLiteral("unspecified")
            : plan.audioTransitionSource)
        .arg(plan.audioTransitionArguments.isEmpty()
            ? QStringLiteral("unspecified")
            : plan.audioTransitionArguments)
        .arg(plan.audioContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxExecutionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxArgumentHandoffPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioSyncValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.muxedAudioCommandPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.muxedAudioCommandReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.processLaunchOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.stdinPipeOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.rawFrameFeedOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.stderrCaptureOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.exitCodeValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.timeoutOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.cleanupOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.executionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFfmpegCommandPlanSummary(
    const BatchRenderedVideoFfmpegCommandPlan & plan)
{
    return QStringLiteral("ffmpeg-command-source=%1 ffmpeg-command-exe=%2 ffmpeg-command-raw-pix-fmt=%3 ffmpeg-command-raw-input=%4 ffmpeg-command-color-source=%5 ffmpeg-command-color-tag=%6 ffmpeg-command-color-args=%7 ffmpeg-command-audio-args=%8 ffmpeg-command-audio-transition-source=%9 ffmpeg-command-audio-transition-args=%10 ffmpeg-command-audio-contract-ready=%11 ffmpeg-command-audio-mux-exec-contract-ready=%12 ffmpeg-command-audio-mux-planned=%13 ffmpeg-command-audio-mux-args-handoff-planned=%14 ffmpeg-command-audio-sync-planned=%15 ffmpeg-command-audio-mux-exec-ready=%16 ffmpeg-command-audio-mux-command-planned=%17 ffmpeg-command-audio-mux-command-ready=%18 ffmpeg-command-audio-owned=%19 ffmpeg-command-execution-owned=%20 ffmpeg-command-output-verification-owned=%21 ffmpeg-command-args=%22 ffmpeg-command-ready=%23 ffmpeg-command-reason=%24")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.executable.isEmpty() ? QStringLiteral("unspecified") : plan.executable)
        .arg(plan.rawInputPixelFormat.isEmpty() ? QStringLiteral("unspecified") : plan.rawInputPixelFormat)
        .arg(plan.rawInputArguments.isEmpty() ? QStringLiteral("unspecified") : plan.rawInputArguments)
        .arg(plan.colorTagSource.isEmpty() ? QStringLiteral("unspecified") : plan.colorTagSource)
        .arg(plan.colorTag)
        .arg(plan.colorArguments.isEmpty() ? QStringLiteral("unspecified") : plan.colorArguments)
        .arg(plan.audioArguments.isEmpty() ? QStringLiteral("unspecified") : plan.audioArguments)
        .arg(plan.audioTransitionSource.isEmpty()
            ? QStringLiteral("unspecified")
            : plan.audioTransitionSource)
        .arg(plan.audioTransitionArguments.isEmpty()
            ? QStringLiteral("unspecified")
            : plan.audioTransitionArguments)
        .arg(plan.audioContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxExecutionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxArgumentHandoffPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioSyncValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.muxedAudioCommandPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.muxedAudioCommandReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.executionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.outputVerificationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.arguments.isEmpty() ? QStringLiteral("unspecified") : plan.arguments)
        .arg(plan.ready ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFrameProcessingPlanSummary(
    const BatchRenderedVideoFrameProcessingPlan & plan)
{
    return QStringLiteral("render-processing-source=%1 render-processing-pix-fmt=%2 render-processing-output-size=%3 render-processing-metadata-ready=%4 render-processing-frame-geometry-ready=%5 render-processing-receipt-contract-ready=%6 render-processing-debayer-contract-ready=%7 render-processing-preview-contract-ready=%8 render-processing-resize-contract-ready=%9 render-processing-rgb48-buffer-contract-ready=%10 render-processing-frame-iteration-contract-ready=%11 render-processing-receipt-owned=%12 render-processing-debayer-owned=%13 render-processing-preview-owned=%14 render-processing-resize-owned=%15 render-processing-rgb48-buffer-owned=%16 render-processing-frame-iteration-owned=%17 render-processing-parity-validation-owned=%18 render-processing-parity-ready=%19 render-processing-frame-ready=%20 render-processing-contract-ready=%21 render-processing-reason=%22")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.rawFramePixelFormat.isEmpty() ? QStringLiteral("unspecified") : plan.rawFramePixelFormat)
        .arg(plan.outputSize.isEmpty() ? QStringLiteral("unspecified") : plan.outputSize)
        .arg(plan.sourceMetadataReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.frameGeometryReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.receiptApplicationContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.debayerContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.previewProcessingContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.resizeProcessingContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.rgb48FrameBufferContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.frameIterationContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.receiptApplicationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.debayerOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.previewProcessingOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.resizeProcessingOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.rgb48FrameBufferOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.frameIterationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.processingParityValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.processingParityReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.frameProcessingReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoReceiptApplicationPlanSummary(
    const BatchRenderedVideoReceiptApplicationPlan & plan)
{
    return QStringLiteral("receipt-application-source=%1 receipt-application-input=%2 receipt-application-output=%3 receipt-application-metadata-ready=%4 receipt-application-frame-geometry-ready=%5 receipt-application-input-contract-ready=%6 receipt-application-output-contract-ready=%7 receipt-application-apply-owned=%8 receipt-application-processing-object-owned=%9 receipt-application-cache-invalidation-owned=%10 receipt-application-cut-stretch-owned=%11 receipt-application-look-assist-owned=%12 receipt-application-validation-owned=%13 receipt-application-ready=%14 receipt-application-contract-ready=%15 receipt-application-reason=%16")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.inputState.isEmpty() ? QStringLiteral("unspecified") : plan.inputState)
        .arg(plan.outputState.isEmpty() ? QStringLiteral("unspecified") : plan.outputState)
        .arg(plan.sourceMetadataReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.frameGeometryReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.inputContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.outputContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.applyToMlvOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.processingObjectMutationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.cacheInvalidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.cutStretchStateOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.lookAssistApplicationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.receiptValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.applicationReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFfmpegFramePlanSummary(
    const BatchRenderedVideoFfmpegFramePlan & plan)
{
    return QStringLiteral("ffmpeg-frame-source=%1x%2 ffmpeg-frame-size=%3 ffmpeg-frame-rate=%4 ffmpeg-frame-resize=%5 ffmpeg-frame-resize-height-locked=%6 ffmpeg-frame-stretch=%7 ffmpeg-frame-codec-dimension-adjusted=%8 ffmpeg-frame-scaled=%9 ffmpeg-frame-ready=%10 ffmpeg-frame-reason=%11")
        .arg(plan.sourceWidth)
        .arg(plan.sourceHeight)
        .arg(plan.frameSizeArgument.isEmpty() ? QStringLiteral("unspecified") : plan.frameSizeArgument)
        .arg(plan.frameRateArgument.isEmpty() ? QStringLiteral("unspecified") : plan.frameRateArgument)
        .arg(plan.resizeEnabled ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.resizeHeightLocked ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.stretchApplied ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.codecDimensionAdjusted ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.scaled ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.ready ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFfmpegAudioPlanSummary(
    const BatchRenderedVideoFfmpegAudioPlan & plan)
{
    return QStringLiteral("ffmpeg-audio-source=%1 ffmpeg-audio-args=%2 ffmpeg-audio-mux-transition-args=%3 ffmpeg-audio-video-only-ready=%4 ffmpeg-audio-source-discovery-owned=%5 ffmpeg-audio-extraction-owned=%6 ffmpeg-audio-input-contract-ready=%7 ffmpeg-audio-input-owned=%8 ffmpeg-audio-mux-exec-contract-ready=%9 ffmpeg-audio-mux-planned=%10 ffmpeg-audio-mux-args-handoff-planned=%11 ffmpeg-audio-sync-planned=%12 ffmpeg-audio-mux-owned=%13 ffmpeg-audio-mux-args-handoff-owned=%14 ffmpeg-audio-sync-owned=%15 ffmpeg-audio-mux-exec-ready=%16 ffmpeg-audio-mux-command-planned=%17 ffmpeg-audio-mux-command-ready=%18 ffmpeg-audio-contract-ready=%19 ffmpeg-audio-reason=%20")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.audioArguments.isEmpty() ? QStringLiteral("unspecified") : plan.audioArguments)
        .arg(plan.muxTransitionArguments.isEmpty() ? QStringLiteral("unspecified") : plan.muxTransitionArguments)
        .arg(plan.videoOnlyCommandReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioDiscoveryOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioExtractionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxExecutionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxArgumentHandoffPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioSyncValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxArgumentHandoffOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioSyncOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.muxedAudioCommandPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.muxedAudioCommandReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoAudioMuxExecutionPlanSummary(
    const BatchRenderedVideoAudioMuxExecutionPlan & plan)
{
    return QStringLiteral("audio-mux-exec-source=%1 audio-mux-exec-input=%2 audio-mux-exec-output=%3 audio-mux-exec-source-contract-ready=%4 audio-mux-exec-handoff-contract-ready=%5 audio-mux-exec-prereq-contract-ready=%6 audio-mux-exec-known=%7 audio-mux-exec-present=%8 audio-mux-exec-discovery-owned=%9 audio-mux-exec-extraction-owned=%10 audio-mux-exec-handoff-ready=%11 audio-mux-exec-temp-input-owned=%12 audio-mux-exec-mux-planned=%13 audio-mux-exec-args-handoff-planned=%14 audio-mux-exec-sync-planned=%15 audio-mux-exec-mux-owned=%16 audio-mux-exec-args-handoff-owned=%17 audio-mux-exec-sync-owned=%18 audio-mux-exec-mux-ready=%19 audio-mux-exec-sync-ready=%20 audio-mux-exec-ready=%21 audio-mux-exec-video-only-ready=%22 audio-mux-exec-contract-ready=%23 audio-mux-exec-reason=%24")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.inputState.isEmpty() ? QStringLiteral("unspecified") : plan.inputState)
        .arg(plan.outputState.isEmpty() ? QStringLiteral("unspecified") : plan.outputState)
        .arg(plan.sourceAudioContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputHandoffContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxPrerequisitesContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioKnown ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioPresent ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioDiscoveryOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioExtractionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputHandoffReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.tempAudioInputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxArgumentHandoffPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioSyncValidationPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxArgumentHandoffOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioSyncValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioSyncValidationReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.muxExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.videoOnlyFallbackReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoAudioMuxPrerequisitesPlanSummary(
    const BatchRenderedVideoAudioMuxPrerequisitesPlan & plan)
{
    return QStringLiteral("audio-mux-source=%1 audio-mux-input=%2 audio-mux-output=%3 audio-mux-source-contract-ready=%4 audio-mux-extraction-contract-ready=%5 audio-mux-audio-input-contract-ready=%6 audio-mux-video-only-ready=%7 audio-mux-source-discovery-owned=%8 audio-mux-extraction-owned=%9 audio-mux-input-owned=%10 audio-mux-mux-owned=%11 audio-mux-sync-owned=%12 audio-mux-ready=%13 audio-mux-contract-ready=%14 audio-mux-reason=%15")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.inputState.isEmpty() ? QStringLiteral("unspecified") : plan.inputState)
        .arg(plan.outputState.isEmpty() ? QStringLiteral("unspecified") : plan.outputState)
        .arg(plan.sourceAudioContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioExtractionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioInputContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.videoOnlyFallbackReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioDiscoveryOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioExtractionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioMuxOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioSyncValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.muxReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFfmpegAudioInputPlanSummary(
    const BatchRenderedVideoFfmpegAudioInputPlan & plan)
{
    return QStringLiteral("ffmpeg-audio-input-source=%1 ffmpeg-audio-input-path=%2 ffmpeg-audio-input-planned-args=%3 ffmpeg-audio-input-active-args=%4 ffmpeg-audio-input-extraction-contract-ready=%5 ffmpeg-audio-input-known=%6 ffmpeg-audio-input-present=%7 ffmpeg-audio-input-path-planned=%8 ffmpeg-audio-input-path-ready=%9 ffmpeg-audio-input-extraction-ready=%10 ffmpeg-audio-input-temp-file-owned=%11 ffmpeg-audio-input-cleanup-owned=%12 ffmpeg-audio-input-planned=%13 ffmpeg-audio-input-owned=%14 ffmpeg-audio-input-ready=%15 ffmpeg-audio-input-video-only-ready=%16 ffmpeg-audio-input-contract-ready=%17 ffmpeg-audio-input-reason=%18")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.plannedAudioPath.isEmpty() ? QStringLiteral("unspecified") : plan.plannedAudioPath)
        .arg(plan.plannedInputArguments.isEmpty() ? QStringLiteral("unspecified") : plan.plannedInputArguments)
        .arg(plan.activeAudioArguments.isEmpty() ? QStringLiteral("unspecified") : plan.activeAudioArguments)
        .arg(plan.sourceAudioExtractionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioKnown ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioPresent ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionPathPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionPathReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.tempFileOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.cleanupOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.videoOnlyFallbackReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFfmpegAudioInputHandoffPlanSummary(
    const BatchRenderedVideoFfmpegAudioInputHandoffPlan & plan)
{
    return QStringLiteral("ffmpeg-audio-input-handoff-source=%1 ffmpeg-audio-input-handoff-path=%2 ffmpeg-audio-input-handoff-planned-args=%3 ffmpeg-audio-input-handoff-active-args=%4 ffmpeg-audio-input-handoff-extraction-exec-contract-ready=%5 ffmpeg-audio-input-handoff-known=%6 ffmpeg-audio-input-handoff-present=%7 ffmpeg-audio-input-handoff-path-planned=%8 ffmpeg-audio-input-handoff-path-ready=%9 ffmpeg-audio-input-handoff-extraction-ready=%10 ffmpeg-audio-input-handoff-temp-lifecycle-ready=%11 ffmpeg-audio-input-handoff-cleanup-owned=%12 ffmpeg-audio-input-handoff-planned=%13 ffmpeg-audio-input-handoff-args-planned=%14 ffmpeg-audio-input-handoff-output-owned=%15 ffmpeg-audio-input-handoff-ownership-planned=%16 ffmpeg-audio-input-handoff-args-handoff-planned=%17 ffmpeg-audio-input-handoff-owned=%18 ffmpeg-audio-input-handoff-args-handoff-owned=%19 ffmpeg-audio-input-handoff-ready=%20 ffmpeg-audio-input-handoff-video-only-ready=%21 ffmpeg-audio-input-handoff-contract-ready=%22 ffmpeg-audio-input-handoff-reason=%23")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.plannedAudioPath.isEmpty() ? QStringLiteral("unspecified") : plan.plannedAudioPath)
        .arg(plan.plannedInputArguments.isEmpty() ? QStringLiteral("unspecified") : plan.plannedInputArguments)
        .arg(plan.activeAudioArguments.isEmpty() ? QStringLiteral("unspecified") : plan.activeAudioArguments)
        .arg(plan.extractionExecutionContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioKnown ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioPresent ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionPathPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionPathReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.tempFileLifecycleReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.cleanupOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.inputArgumentsPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionOutputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputOwnershipPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputArgumentHandoffPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputArgumentHandoffOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.audioInputHandoffReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.videoOnlyFallbackReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoSourceAudioExtractionExecutionPlanSummary(
    const BatchRenderedVideoSourceAudioExtractionExecutionPlan & plan)
{
    return QStringLiteral("source-audio-extraction-exec-source=%1 source-audio-extraction-exec-path=%2 source-audio-extraction-exec-format=%3 source-audio-extraction-exec-prereq-contract-ready=%4 source-audio-extraction-exec-known=%5 source-audio-extraction-exec-present=%6 source-audio-extraction-exec-path-planned=%7 source-audio-extraction-exec-path-ready=%8 source-audio-extraction-exec-sample-read-planned=%9 source-audio-extraction-exec-wav-write-planned=%10 source-audio-extraction-exec-temp-lifecycle-planned=%11 source-audio-extraction-exec-sample-read-owned=%12 source-audio-extraction-exec-wav-header-owned=%13 source-audio-extraction-exec-wav-sample-owned=%14 source-audio-extraction-exec-temp-open-owned=%15 source-audio-extraction-exec-temp-finalize-owned=%16 source-audio-extraction-exec-cleanup-owned=%17 source-audio-extraction-exec-process-owned=%18 source-audio-extraction-exec-temp-lifecycle-ready=%19 source-audio-extraction-exec-ready=%20 source-audio-extraction-exec-video-only-ready=%21 source-audio-extraction-exec-contract-ready=%22 source-audio-extraction-exec-reason=%23")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.plannedAudioPath.isEmpty() ? QStringLiteral("unspecified") : plan.plannedAudioPath)
        .arg(plan.extractionFormat.isEmpty() ? QStringLiteral("unspecified") : plan.extractionFormat)
        .arg(plan.extractionPrerequisiteContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioKnown ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioPresent ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionPathPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionPathReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sampleReadPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.wavWritePlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.tempFileLifecyclePlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sampleReadOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.wavHeaderWriteOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.wavSampleWriteOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.tempFileOpenOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.tempFileFinalizeOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.cleanupOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionProcessOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.tempFileLifecycleReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.videoOnlyFallbackReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoSourceAudioExtractionPlanSummary(
    const BatchRenderedVideoSourceAudioExtractionPlan & plan)
{
    return QStringLiteral("source-audio-extraction-source=%1 source-audio-extraction-clip=%2 source-audio-extraction-format=%3 source-audio-extraction-path=%4 source-audio-extraction-source-contract-ready=%5 source-audio-extraction-known=%6 source-audio-extraction-present=%7 source-audio-extraction-discovery-owned=%8 source-audio-extraction-path-planned=%9 source-audio-extraction-path-ready=%10 source-audio-extraction-format-ready=%11 source-audio-extraction-sample-rate-ready=%12 source-audio-extraction-channel-layout-ready=%13 source-audio-extraction-process-owned=%14 source-audio-extraction-temp-file-owned=%15 source-audio-extraction-cleanup-owned=%16 source-audio-extraction-ready=%17 source-audio-extraction-video-only-ready=%18 source-audio-extraction-contract-ready=%19 source-audio-extraction-reason=%20")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.clipPath.isEmpty() ? QStringLiteral("unspecified") : plan.clipPath)
        .arg(plan.extractionFormat.isEmpty() ? QStringLiteral("unspecified") : plan.extractionFormat)
        .arg(plan.plannedAudioPath.isEmpty() ? QStringLiteral("unspecified") : plan.plannedAudioPath)
        .arg(plan.sourceAudioContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioKnown ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioPresent ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioDiscoveryOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionPathPlanned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionPathReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sampleFormatReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sampleRateReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.channelLayoutReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionProcessOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.tempFileOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.cleanupOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.videoOnlyFallbackReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoSourceAudioPlanSummary(
    const BatchRenderedVideoSourceAudioPlan & plan)
{
    return QStringLiteral("source-audio-source=%1 source-audio-clip=%2 source-audio-state=%3 source-audio-sample-rate=%4 source-audio-channels=%5 source-audio-bits=%6 source-audio-bytes=%7 source-audio-discovery-owned=%8 source-audio-discovery-attempted=%9 source-audio-known=%10 source-audio-present=%11 source-audio-extraction-owned=%12 source-audio-mux-input-owned=%13 source-audio-sync-validation-owned=%14 source-audio-video-only-ready=%15 source-audio-contract-ready=%16 source-audio-reason=%17")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.clipPath.isEmpty() ? QStringLiteral("unspecified") : plan.clipPath)
        .arg(plan.audioState.isEmpty() ? QStringLiteral("unspecified") : plan.audioState)
        .arg(plan.sampleRate)
        .arg(plan.channels)
        .arg(plan.bitsPerSample)
        .arg(plan.audioBytes)
        .arg(plan.discoveryOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.discoveryAttempted ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioKnown ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.sourceAudioPresent ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.extractionOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.muxInputOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.syncValidationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.videoOnlyFallbackReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoOptionalFilterPlanSummary(
    const BatchRenderedVideoOptionalFilterPlan & plan)
{
    return QStringLiteral("optional-filter-source=%1 optional-filter-base-contract-ready=%2 optional-filter-base-args=%3 optional-filter-requested=%4 optional-filter-graph-owned=%5 optional-filter-moiree-owned=%6 optional-filter-hdr-owned=%7 optional-filter-stabilization-owned=%8 optional-filter-order-owned=%9 optional-filter-parity-owned=%10 optional-filter-exec-ready=%11 optional-filter-contract-ready=%12 optional-filter-reason=%13")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.baseFilterContractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.baseFilterArguments.isEmpty() ? QStringLiteral("unspecified") : plan.baseFilterArguments)
        .arg(plan.optionalFiltersRequested ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.optionalFilterGraphOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.moireeFilterOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.hdrBlendOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.stabilizationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.optionalFilterOrderOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.optionalFilterParityOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.optionalFilterExecutionReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.contractReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFfmpegFilterPlanSummary(
    const BatchRenderedVideoFfmpegFilterPlan & plan)
{
    return QStringLiteral("ffmpeg-filter-source=%1 ffmpeg-filter-base-color-scale-ready=%2 ffmpeg-filter-optional-owned=%3 ffmpeg-filter-moiree-owned=%4 ffmpeg-filter-hdr-owned=%5 ffmpeg-filter-stabilization-owned=%6 ffmpeg-filter-color-scale=%7 ffmpeg-filter-args=%8 ffmpeg-filter-ready=%9 ffmpeg-filter-reason=%10")
        .arg(plan.source.isEmpty() ? QStringLiteral("unspecified") : plan.source)
        .arg(plan.baseColorScaleReady ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.optionalFiltersOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.moireeFilterOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.hdrBlendOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.stabilizationOwned ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.colorScaleFilter.isEmpty() ? QStringLiteral("unspecified") : plan.colorScaleFilter)
        .arg(plan.filterArguments.isEmpty() ? QStringLiteral("unspecified") : plan.filterArguments)
        .arg(plan.ready ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoFfmpegVideoPlanSummary(
    const BatchExportFormatRequest & request)
{
    return batchRenderedVideoFfmpegVideoPlanSummary(
        batchRenderedVideoFfmpegVideoPlanFromRequest(request));
}

QString batchRenderedVideoFfmpegVideoPlanSummary(
    const BatchRenderedVideoFfmpegVideoPlan & plan)
{
    return QStringLiteral("ffmpeg-video-encoder=%1 ffmpeg-video-preset=%2 ffmpeg-video-quality=%3:%4 ffmpeg-video-pix-fmt=%5 ffmpeg-video-tag=%6 ffmpeg-video-args=%7 ffmpeg-video-ready=%8 ffmpeg-video-reason=%9")
        .arg(plan.encoder.isEmpty() ? QStringLiteral("unspecified") : plan.encoder)
        .arg(plan.preset.isEmpty() ? QStringLiteral("none") : plan.preset)
        .arg(plan.qualityFlag.isEmpty() ? QStringLiteral("unspecified") : plan.qualityFlag)
        .arg(plan.qualityValue)
        .arg(plan.pixelFormat.isEmpty() ? QStringLiteral("unspecified") : plan.pixelFormat)
        .arg(plan.videoTag.isEmpty() ? QStringLiteral("none") : plan.videoTag)
        .arg(plan.videoArguments.isEmpty() ? QStringLiteral("unspecified") : plan.videoArguments)
        .arg(plan.ready ? QStringLiteral("true") : QStringLiteral("false"))
        .arg(plan.reason.isEmpty() ? QStringLiteral("none") : plan.reason);
}

QString batchRenderedVideoEncoderPresetSummary(
    const BatchExportFormatRequest & request)
{
    return batchRenderedVideoEncoderPresetSummary(
        batchRenderedVideoEncoderPresetFromRequest(request));
}

QString batchRenderedVideoEncoderPresetSummary(
    const BatchRenderedVideoEncoderPreset & preset)
{
    return QStringLiteral("encoder-profile=%1 encoder-option=%2 gui-codec-profile=%3 gui-codec-option=%4 encoder-extension=%5 encoder-ready=%6")
        .arg(batchRenderedVideoEncoderProfileName(preset.profile))
        .arg(batchRenderedVideoEncoderOptionName(preset.option))
        .arg(preset.guiCodecProfile)
        .arg(preset.guiCodecOption)
        .arg(preset.extension.isEmpty() ? QStringLiteral("unspecified") : preset.extension)
        .arg(preset.ready ? QStringLiteral("true") : QStringLiteral("false"));
}

QString batchRenderedVideoTargetSummary(const BatchExportFormatRequest & request)
{
    return batchRenderedVideoTargetSummary(
        batchRenderedVideoTargetFromRequest(request));
}

QString batchRenderedVideoTargetSummary(const BatchRenderedVideoTarget & target)
{
    return QStringLiteral("target-codec=%1 target-container=%2 target-extension=%3 target-complete=%4")
        .arg(batchRenderedVideoCodecName(target.codec))
        .arg(batchRenderedVideoContainerName(target.container))
        .arg(target.extension.isEmpty() ? QStringLiteral("unspecified") : target.extension)
        .arg(target.complete ? QStringLiteral("true") : QStringLiteral("false"));
}

QString batchRenderedVideoJobPlanFirstBlocker(
    const BatchRenderedVideoJobPlan & plan)
{
    if( plan.request.format != BatchExportFormat::RenderedVideo )
        return QStringLiteral("not a rendered-video request");
    if( !plan.requestValid )
        return batchRenderedVideoRequestShapeError(plan.request);
    if( !plan.targetReady )
        return QStringLiteral("rendered target incomplete");
    if( !plan.encoderReady )
        return QStringLiteral("rendered encoder preset unavailable");
    if( !plan.ffmpegVideoReady )
    {
        return plan.ffmpegVideoPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg video plan unavailable")
            : plan.ffmpegVideoPlan.reason;
    }
    if( !plan.ffmpegFilterReady )
    {
        return plan.ffmpegFilterPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg filter plan unavailable")
            : plan.ffmpegFilterPlan.reason;
    }
    if( !plan.optionalFilterContractReady )
    {
        return plan.optionalFilterPlan.reason.isEmpty()
            ? QStringLiteral("rendered optional-filter contract unavailable")
            : plan.optionalFilterPlan.reason;
    }
    if( !plan.sourceAudioContractReady )
    {
        return plan.sourceAudioPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio contract unavailable")
            : plan.sourceAudioPlan.reason;
    }
    if( !plan.sourceAudioExtractionContractReady )
    {
        return plan.sourceAudioExtractionPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio extraction prerequisite contract unavailable")
            : plan.sourceAudioExtractionPlan.reason;
    }
    if( !plan.sourceAudioExtractionExecutionContractReady )
    {
        return plan.sourceAudioExtractionExecutionPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio extraction execution contract unavailable")
            : plan.sourceAudioExtractionExecutionPlan.reason;
    }
    if( !plan.ffmpegAudioInputHandoffContractReady )
    {
        return plan.ffmpegAudioInputHandoffPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg audio input handoff contract unavailable")
            : plan.ffmpegAudioInputHandoffPlan.reason;
    }
    if( !plan.ffmpegAudioInputContractReady )
    {
        return plan.ffmpegAudioInputPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg audio input contract unavailable")
            : plan.ffmpegAudioInputPlan.reason;
    }
    if( !plan.audioMuxPrerequisitesContractReady )
    {
        return plan.audioMuxPrerequisitesPlan.reason.isEmpty()
            ? QStringLiteral("rendered audio mux prerequisite contract unavailable")
            : plan.audioMuxPrerequisitesPlan.reason;
    }
    if( !plan.audioMuxExecutionContractReady )
    {
        return plan.audioMuxExecutionPlan.reason.isEmpty()
            ? QStringLiteral("rendered audio mux execution contract unavailable")
            : plan.audioMuxExecutionPlan.reason;
    }
    if( !plan.ffmpegAudioContractReady )
    {
        return plan.ffmpegAudioPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg audio contract unavailable")
            : plan.ffmpegAudioPlan.reason;
    }
    if( !plan.ffmpegBinaryCommandReady )
    {
        return plan.ffmpegBinaryPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg executable unavailable")
            : plan.ffmpegBinaryPlan.reason;
    }
    if( !plan.mediaProbeCommandReady )
    {
        return plan.mediaProbeBinaryPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification media probe executable unavailable")
            : plan.mediaProbeBinaryPlan.reason;
    }
    if( !plan.mediaProbeCommandContractReady )
    {
        return plan.mediaProbeCommandPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification media probe command contract unavailable")
            : plan.mediaProbeCommandPlan.reason;
    }
    if( !plan.mediaProbeJsonContractReady )
    {
        return plan.mediaProbeJsonPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification media probe JSON contract unavailable")
            : plan.mediaProbeJsonPlan.reason;
    }
    if( !plan.mediaProbeResultContractReady )
    {
        return plan.mediaProbeResultPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification media probe result contract unavailable")
            : plan.mediaProbeResultPlan.reason;
    }
    if( !plan.mediaProbeValidationContractReady )
    {
        return plan.mediaProbeValidationPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification media probe validation contract unavailable")
            : plan.mediaProbeValidationPlan.reason;
    }
    if( !plan.renderSettings.ready )
    {
        return plan.renderSettings.reason.isEmpty()
            ? QStringLiteral("rendered render settings unavailable")
            : plan.renderSettings.reason;
    }
    if( !plan.outputReady )
    {
        return plan.outputPlan.reason.isEmpty()
            ? QStringLiteral("rendered output path invalid")
            : plan.outputPlan.reason;
    }
    if( !plan.outputVerificationContractReady )
    {
        return plan.outputVerificationPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification contract unavailable")
            : plan.outputVerificationPlan.reason;
    }
    if( plan.metadataAttempted && !plan.metadataReady )
    {
        return plan.sourceMetadata.reason.isEmpty()
            ? QStringLiteral("rendered source metadata unavailable")
            : plan.sourceMetadata.reason;
    }
    if( plan.metadataAttempted && !plan.ffmpegFrameReady )
    {
        return plan.ffmpegFramePlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg frame plan unavailable")
            : plan.ffmpegFramePlan.reason;
    }
    if( plan.metadataAttempted && !plan.receiptApplicationContractReady )
    {
        return plan.receiptApplicationPlan.reason.isEmpty()
            ? QStringLiteral("rendered receipt application contract unavailable")
            : plan.receiptApplicationPlan.reason;
    }
    if( plan.metadataAttempted && !plan.frameProcessingContractReady )
    {
        return plan.frameProcessingPlan.reason.isEmpty()
            ? QStringLiteral("rendered frame-processing contract unavailable")
            : plan.frameProcessingPlan.reason;
    }
    if( plan.metadataAttempted && !plan.ffmpegCommandReady )
    {
        return plan.ffmpegCommandPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg command plan unavailable")
            : plan.ffmpegCommandPlan.reason;
    }
    if( plan.metadataAttempted && !plan.ffmpegExecutionContractReady )
    {
        return plan.ffmpegExecutionPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg execution contract unavailable")
            : plan.ffmpegExecutionPlan.reason;
    }
    if( plan.metadataAttempted && !plan.outputVerificationExecutionContractReady )
    {
        return plan.outputVerificationExecutionPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification execution contract unavailable")
            : plan.outputVerificationExecutionPlan.reason;
    }
    if( plan.metadataAttempted && !plan.receiptHashValidationContractReady )
    {
        return plan.receiptHashValidationPlan.reason.isEmpty()
            ? QStringLiteral("rendered receipt/hash validation contract unavailable")
            : plan.receiptHashValidationPlan.reason;
    }
    if( plan.metadataAttempted && !plan.outputVerificationDecisionContractReady )
    {
        return plan.outputVerificationDecisionPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification decision contract unavailable")
            : plan.outputVerificationDecisionPlan.reason;
    }
    if( plan.metadataAttempted && !plan.outputVerificationResultContractReady )
    {
        return plan.outputVerificationResultPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification result report contract unavailable")
            : plan.outputVerificationResultPlan.reason;
    }
    if( plan.metadataAttempted && !plan.outputVerificationReportContractReady )
    {
        return plan.outputVerificationReportPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification report artifact contract unavailable")
            : plan.outputVerificationReportPlan.reason;
    }
    if( plan.metadataAttempted
     && !plan.outputVerificationReportContentContractReady )
    {
        return plan.outputVerificationReportContentPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification report content contract unavailable")
            : plan.outputVerificationReportContentPlan.reason;
    }
    if( plan.metadataAttempted
     && !plan.outputVerificationReportWriterContractReady )
    {
        return plan.outputVerificationReportWriterPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification report writer contract unavailable")
            : plan.outputVerificationReportWriterPlan.reason;
    }
    if( !plan.runnerPrerequisites.ready )
        return plan.runnerPrerequisites.reason;
    return QString();
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanWithMetadata(
    const BatchRenderedVideoJobPlan & preflightPlan,
    const BatchRenderedVideoSourceMetadata & metadata)
{
    return batchRenderedVideoJobPlanWithMetadata(
        preflightPlan,
        metadata,
        preflightPlan.renderSettings);
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanWithMetadata(
    const BatchRenderedVideoJobPlan & preflightPlan,
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoRenderSettings & settings)
{
    BatchRenderedVideoJobPlan plan = preflightPlan;
    plan.sourceMetadata = metadata;
    plan.renderSettings = settings;
    plan.metadataAttempted = true;
    plan.metadataReady = metadata.ready;
    if( plan.encoderReady )
    {
        plan.ffmpegFramePlan =
            batchRenderedVideoFfmpegFramePlanFromMetadata(
                metadata,
                settings,
                plan.encoderPreset.profile);
    }
    else
    {
        plan.ffmpegFramePlan.reason =
            QStringLiteral("rendered encoder preset incomplete");
    }
    plan.ffmpegFrameReady = plan.ffmpegFramePlan.ready;
    plan.receiptApplicationPlan =
        batchRenderedVideoReceiptApplicationPlanFromContracts(
            metadata,
            plan.ffmpegFramePlan);
    plan.receiptApplicationContractReady =
        plan.receiptApplicationPlan.contractReady;
    plan.frameProcessingPlan =
        batchRenderedVideoFrameProcessingPlanFromFramePlan(
            metadata,
            plan.ffmpegFramePlan,
            plan.receiptApplicationPlan);
    plan.frameProcessingContractReady =
        plan.frameProcessingPlan.contractReady;
    plan.optionalFilterPlan =
        batchRenderedVideoOptionalFilterPlanFromFilterPlan(
            plan.ffmpegFilterPlan);
    plan.optionalFilterContractReady =
        plan.optionalFilterPlan.contractReady;
    plan.ffmpegCommandPlan =
        batchRenderedVideoFfmpegCommandPlanFromParts(
            plan.ffmpegFramePlan,
            plan.ffmpegFilterPlan,
            plan.ffmpegAudioPlan,
            plan.ffmpegVideoPlan,
            plan.outputPlan,
            plan.ffmpegBinaryPlan);
    plan.ffmpegCommandReady = plan.ffmpegCommandPlan.ready;
    plan.ffmpegExecutionPlan =
        batchRenderedVideoFfmpegExecutionPlanFromCommand(
            plan.ffmpegCommandPlan);
    plan.ffmpegExecutionContractReady =
        plan.ffmpegExecutionPlan.contractReady;
    plan.outputVerificationPlan =
        batchRenderedVideoOutputVerificationPlanFromOutput(
            plan.outputPlan,
            plan.target,
            metadata);
    plan.outputVerificationContractReady =
        plan.outputVerificationPlan.contractReady;
    plan.mediaProbeCommandPlan =
        batchRenderedVideoMediaProbeCommandPlanFromContracts(
            plan.outputVerificationPlan,
            plan.mediaProbeBinaryPlan);
    plan.mediaProbeCommandReady =
        plan.mediaProbeBinaryPlan.commandExecutableReady;
    plan.mediaProbeCommandContractReady =
        plan.mediaProbeCommandPlan.contractReady;
    plan.mediaProbeJsonPlan =
        batchRenderedVideoMediaProbeJsonPlanFromCommand(
            plan.mediaProbeCommandPlan);
    plan.mediaProbeJsonContractReady =
        plan.mediaProbeJsonPlan.contractReady;
    plan.mediaProbeResultPlan =
        batchRenderedVideoMediaProbeResultPlanFromContracts(
            plan.outputVerificationPlan,
            plan.mediaProbeCommandPlan,
            plan.mediaProbeJsonPlan);
    plan.mediaProbeResultContractReady =
        plan.mediaProbeResultPlan.contractReady;
    plan.mediaProbeValidationPlan =
        batchRenderedVideoMediaProbeValidationPlanFromResult(
            plan.mediaProbeResultPlan);
    plan.mediaProbeValidationContractReady =
        plan.mediaProbeValidationPlan.contractReady;
    plan.outputVerificationExecutionPlan =
        batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
            plan.outputVerificationPlan,
            plan.ffmpegExecutionPlan,
            plan.mediaProbeBinaryPlan,
            plan.mediaProbeCommandPlan,
            plan.mediaProbeJsonPlan,
            plan.mediaProbeResultPlan,
            plan.mediaProbeValidationPlan);
    plan.receiptHashValidationPlan =
        batchRenderedVideoReceiptHashValidationPlanFromExecution(
            plan.outputVerificationExecutionPlan);
    plan.outputVerificationExecutionPlan =
        batchRenderedVideoOutputVerificationExecutionPlanWithReceiptHashValidation(
            plan.outputVerificationExecutionPlan,
            plan.receiptHashValidationPlan);
    plan.outputVerificationExecutionContractReady =
        plan.outputVerificationExecutionPlan.contractReady;
    plan.receiptHashValidationContractReady =
        plan.receiptHashValidationPlan.contractReady;
    plan.outputVerificationDecisionPlan =
        batchRenderedVideoOutputVerificationDecisionPlanFromExecution(
            plan.outputVerificationExecutionPlan);
    plan.outputVerificationDecisionContractReady =
        plan.outputVerificationDecisionPlan.contractReady;
    plan.outputVerificationResultPlan =
        batchRenderedVideoOutputVerificationResultPlanFromDecision(
            plan.outputVerificationDecisionPlan);
    plan.outputVerificationResultContractReady =
        plan.outputVerificationResultPlan.contractReady;
    plan.outputVerificationReportPlan =
        batchRenderedVideoOutputVerificationReportPlanFromResult(
            plan.outputVerificationResultPlan);
    plan.outputVerificationReportContractReady =
        plan.outputVerificationReportPlan.contractReady;
    plan.outputVerificationReportContentPlan =
        batchRenderedVideoOutputVerificationReportContentPlanFromReport(
            plan.outputVerificationReportPlan);
    plan.outputVerificationReportContentContractReady =
        plan.outputVerificationReportContentPlan.contractReady;
    plan.outputVerificationReportWriterPlan =
        batchRenderedVideoOutputVerificationReportWriterPlanFromContent(
            plan.outputVerificationReportContentPlan);
    plan.outputVerificationReportWriterContractReady =
        plan.outputVerificationReportWriterPlan.contractReady;
    plan.preflightReady = plan.requestValid
                       && plan.targetReady
                       && plan.encoderReady
                       && plan.ffmpegVideoReady
                       && plan.ffmpegFilterReady
                       && plan.optionalFilterContractReady
                       && plan.sourceAudioContractReady
                       && plan.sourceAudioExtractionContractReady
                       && plan.sourceAudioExtractionExecutionContractReady
                       && plan.ffmpegAudioInputHandoffContractReady
                       && plan.ffmpegAudioInputContractReady
                       && plan.audioMuxPrerequisitesContractReady
                       && plan.audioMuxExecutionContractReady
                       && plan.ffmpegAudioContractReady
                       && plan.ffmpegBinaryCommandReady
                       && plan.mediaProbeCommandReady
                       && plan.mediaProbeCommandContractReady
                       && plan.mediaProbeJsonContractReady
                       && plan.mediaProbeResultContractReady
                       && plan.mediaProbeValidationContractReady
                       && plan.renderSettings.ready
                       && plan.metadataReady
                       && plan.ffmpegFrameReady
                       && plan.receiptApplicationContractReady
                       && plan.frameProcessingContractReady
                       && plan.ffmpegCommandReady
                       && plan.ffmpegExecutionContractReady
                       && plan.outputReady
                       && plan.outputVerificationContractReady
                       && plan.outputVerificationExecutionContractReady
                       && plan.receiptHashValidationContractReady
                       && plan.outputVerificationDecisionContractReady
                       && plan.outputVerificationResultContractReady
                       && plan.outputVerificationReportContractReady
                       && plan.outputVerificationReportContentContractReady
                       && plan.outputVerificationReportWriterContractReady;
    plan.runnable = plan.preflightReady && plan.runnerPrerequisites.ready;
    return plan;
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanWithSourceAudio(
    const BatchRenderedVideoJobPlan & preflightPlan,
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan)
{
    BatchRenderedVideoJobPlan plan = preflightPlan;
    plan.sourceAudioPlan = sourceAudioPlan;
    plan.sourceAudioExtractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            plan.sourceAudioPlan,
            batchRenderedVideoSourceAudioExtractionPathFromOutputPath(
                plan.outputPlan.outputPath));
    plan.sourceAudioExtractionExecutionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            plan.sourceAudioExtractionPlan);
    plan.ffmpegAudioInputHandoffPlan =
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            plan.sourceAudioExtractionExecutionPlan);
    plan.ffmpegAudioInputPlan =
        batchRenderedVideoFfmpegAudioInputPlanFromHandoff(
            plan.sourceAudioExtractionPlan,
            plan.ffmpegAudioInputHandoffPlan);
    plan.audioMuxPrerequisitesPlan =
        batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
            plan.sourceAudioPlan,
            plan.sourceAudioExtractionPlan,
            plan.ffmpegAudioInputPlan);
    plan.audioMuxExecutionPlan =
        batchRenderedVideoAudioMuxExecutionPlanFromContracts(
            plan.sourceAudioPlan,
            plan.ffmpegAudioInputHandoffPlan,
            plan.audioMuxPrerequisitesPlan);
    plan.ffmpegAudioPlan =
        batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
            plan.sourceAudioPlan,
            plan.audioMuxPrerequisitesPlan,
            plan.audioMuxExecutionPlan);
    plan.sourceAudioContractReady =
        plan.sourceAudioPlan.contractReady;
    plan.sourceAudioExtractionContractReady =
        plan.sourceAudioExtractionPlan.contractReady;
    plan.sourceAudioExtractionExecutionContractReady =
        plan.sourceAudioExtractionExecutionPlan.contractReady;
    plan.ffmpegAudioInputHandoffContractReady =
        plan.ffmpegAudioInputHandoffPlan.contractReady;
    plan.ffmpegAudioInputContractReady =
        plan.ffmpegAudioInputPlan.contractReady;
    plan.audioMuxPrerequisitesContractReady =
        plan.audioMuxPrerequisitesPlan.contractReady;
    plan.audioMuxExecutionContractReady =
        plan.audioMuxExecutionPlan.contractReady;
    plan.ffmpegAudioContractReady =
        plan.ffmpegAudioPlan.contractReady;

    if( plan.metadataAttempted )
    {
        plan.ffmpegCommandPlan =
            batchRenderedVideoFfmpegCommandPlanFromParts(
                plan.ffmpegFramePlan,
                plan.ffmpegFilterPlan,
                plan.ffmpegAudioPlan,
                plan.ffmpegVideoPlan,
                plan.outputPlan,
                plan.ffmpegBinaryPlan);
        plan.ffmpegCommandReady = plan.ffmpegCommandPlan.ready;
        plan.ffmpegExecutionPlan =
            batchRenderedVideoFfmpegExecutionPlanFromCommand(
                plan.ffmpegCommandPlan);
        plan.ffmpegExecutionContractReady =
            plan.ffmpegExecutionPlan.contractReady;
        plan.mediaProbeJsonPlan =
            batchRenderedVideoMediaProbeJsonPlanFromCommand(
                plan.mediaProbeCommandPlan);
        plan.mediaProbeJsonContractReady =
            plan.mediaProbeJsonPlan.contractReady;
        plan.mediaProbeResultPlan =
            batchRenderedVideoMediaProbeResultPlanFromContracts(
                plan.outputVerificationPlan,
                plan.mediaProbeCommandPlan,
                plan.mediaProbeJsonPlan);
        plan.mediaProbeResultContractReady =
            plan.mediaProbeResultPlan.contractReady;
        plan.mediaProbeValidationPlan =
            batchRenderedVideoMediaProbeValidationPlanFromResult(
                plan.mediaProbeResultPlan);
        plan.mediaProbeValidationContractReady =
            plan.mediaProbeValidationPlan.contractReady;
        plan.outputVerificationExecutionPlan =
            batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
                plan.outputVerificationPlan,
                plan.ffmpegExecutionPlan,
                plan.mediaProbeBinaryPlan,
                plan.mediaProbeCommandPlan,
                plan.mediaProbeJsonPlan,
                plan.mediaProbeResultPlan,
                plan.mediaProbeValidationPlan);
        plan.receiptHashValidationPlan =
            batchRenderedVideoReceiptHashValidationPlanFromExecution(
                plan.outputVerificationExecutionPlan);
        plan.outputVerificationExecutionPlan =
            batchRenderedVideoOutputVerificationExecutionPlanWithReceiptHashValidation(
                plan.outputVerificationExecutionPlan,
                plan.receiptHashValidationPlan);
        plan.outputVerificationExecutionContractReady =
            plan.outputVerificationExecutionPlan.contractReady;
        plan.receiptHashValidationContractReady =
            plan.receiptHashValidationPlan.contractReady;
        plan.outputVerificationDecisionPlan =
            batchRenderedVideoOutputVerificationDecisionPlanFromExecution(
                plan.outputVerificationExecutionPlan);
        plan.outputVerificationDecisionContractReady =
            plan.outputVerificationDecisionPlan.contractReady;
        plan.outputVerificationResultPlan =
            batchRenderedVideoOutputVerificationResultPlanFromDecision(
                plan.outputVerificationDecisionPlan);
        plan.outputVerificationResultContractReady =
            plan.outputVerificationResultPlan.contractReady;
        plan.outputVerificationReportPlan =
            batchRenderedVideoOutputVerificationReportPlanFromResult(
                plan.outputVerificationResultPlan);
        plan.outputVerificationReportContractReady =
            plan.outputVerificationReportPlan.contractReady;
        plan.outputVerificationReportContentPlan =
            batchRenderedVideoOutputVerificationReportContentPlanFromReport(
                plan.outputVerificationReportPlan);
        plan.outputVerificationReportContentContractReady =
            plan.outputVerificationReportContentPlan.contractReady;
        plan.outputVerificationReportWriterPlan =
            batchRenderedVideoOutputVerificationReportWriterPlanFromContent(
                plan.outputVerificationReportContentPlan);
        plan.outputVerificationReportWriterContractReady =
            plan.outputVerificationReportWriterPlan.contractReady;
    }

    plan.preflightReady = plan.requestValid
                       && plan.targetReady
                       && plan.encoderReady
                       && plan.ffmpegVideoReady
                       && plan.ffmpegFilterReady
                       && plan.optionalFilterContractReady
                       && plan.sourceAudioContractReady
                       && plan.sourceAudioExtractionContractReady
                       && plan.sourceAudioExtractionExecutionContractReady
                       && plan.ffmpegAudioInputHandoffContractReady
                       && plan.ffmpegAudioInputContractReady
                       && plan.audioMuxPrerequisitesContractReady
                       && plan.audioMuxExecutionContractReady
                       && plan.ffmpegAudioContractReady
                       && plan.ffmpegBinaryCommandReady
                       && plan.mediaProbeCommandReady
                       && plan.mediaProbeCommandContractReady
                       && plan.mediaProbeJsonContractReady
                       && plan.mediaProbeResultContractReady
                       && plan.mediaProbeValidationContractReady
                       && plan.renderSettings.ready
                       && plan.outputReady
                       && plan.outputVerificationContractReady;
    if( plan.metadataAttempted )
    {
        plan.preflightReady = plan.preflightReady
                           && plan.metadataReady
                           && plan.ffmpegFrameReady
                           && plan.receiptApplicationContractReady
                           && plan.frameProcessingContractReady
                           && plan.ffmpegCommandReady
                           && plan.ffmpegExecutionContractReady
                           && plan.mediaProbeJsonContractReady
                           && plan.mediaProbeResultContractReady
                           && plan.mediaProbeValidationContractReady
                           && plan.outputVerificationExecutionContractReady
                           && plan.receiptHashValidationContractReady
                           && plan.outputVerificationDecisionContractReady
                           && plan.outputVerificationResultContractReady
                           && plan.outputVerificationReportContractReady
                           && plan.outputVerificationReportContentContractReady
                           && plan.outputVerificationReportWriterContractReady;
    }
    plan.runnable = plan.preflightReady && plan.runnerPrerequisites.ready;
    return plan;
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    const BatchRenderedVideoRenderSettings & settings,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan)
{
    return batchRenderedVideoJobPlanFromRequest(
        inputPath,
        outputPath,
        request,
        1,
        settings,
        binaryPlan,
        mediaProbeBinaryPlan);
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    const BatchRenderedVideoRenderSettings & settings,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan)
{
    return batchRenderedVideoJobPlanFromRequest(
        inputPath,
        outputPath,
        request,
        1,
        settings,
        binaryPlan);
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    const BatchRenderedVideoRenderSettings & settings)
{
    return batchRenderedVideoJobPlanFromRequest(
        inputPath,
        outputPath,
        request,
        1,
        settings);
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request)
{
    return batchRenderedVideoJobPlanFromRequest(
        inputPath,
        outputPath,
        request,
        1);
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    int inputClipCount,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan)
{
    return batchRenderedVideoJobPlanFromRequest(
        inputPath,
        outputPath,
        request,
        inputClipCount,
        batchRenderedVideoDefaultRenderSettings(),
        binaryPlan);
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    int inputClipCount,
    const BatchRenderedVideoRenderSettings & settings)
{
    return batchRenderedVideoJobPlanFromRequest(
        inputPath,
        outputPath,
        request,
        inputClipCount,
        settings,
        batchRenderedVideoFfmpegBinaryPlanFromRequestedName());
}

BatchRenderedVideoJobPlan batchRenderedVideoJobPlanFromRequest(
    const QString & inputPath,
    const QString & outputPath,
    const BatchExportFormatRequest & request,
    int inputClipCount,
    const BatchRenderedVideoRenderSettings & settings,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan)
{
    BatchRenderedVideoJobPlan plan;
    plan.request = request;
    plan.renderSettings = settings;
    plan.ffmpegBinaryPlan = binaryPlan;
    plan.mediaProbeBinaryPlan = mediaProbeBinaryPlan;
    plan.requestValid = request.format == BatchExportFormat::RenderedVideo
                     && batchRenderedVideoRequestShapeValid(request);

    if( request.format == BatchExportFormat::RenderedVideo )
    {
        plan.target = batchRenderedVideoTargetFromRequest(request);
        plan.encoderPreset =
            batchRenderedVideoEncoderPresetFromTarget(plan.target);
        plan.ffmpegVideoPlan =
            batchRenderedVideoFfmpegVideoPlanFromEncoderPreset(plan.encoderPreset);
        plan.ffmpegFilterPlan =
            batchRenderedVideoFfmpegFilterPlanForCurrentBuild();
        plan.optionalFilterPlan =
            batchRenderedVideoOptionalFilterPlanFromFilterPlan(
                plan.ffmpegFilterPlan);
        plan.outputPlan =
            batchRenderedVideoOutputPlanFromPaths(
                inputPath,
                outputPath,
                plan.target,
                inputClipCount);
        plan.outputVerificationPlan =
            batchRenderedVideoOutputVerificationPlanFromOutput(
                plan.outputPlan,
                plan.target);
        plan.mediaProbeCommandPlan =
            batchRenderedVideoMediaProbeCommandPlanFromContracts(
                plan.outputVerificationPlan,
                plan.mediaProbeBinaryPlan);
        plan.mediaProbeJsonPlan =
            batchRenderedVideoMediaProbeJsonPlanFromCommand(
                plan.mediaProbeCommandPlan);
        plan.mediaProbeResultPlan =
            batchRenderedVideoMediaProbeResultPlanFromContracts(
                plan.outputVerificationPlan,
                plan.mediaProbeCommandPlan,
                plan.mediaProbeJsonPlan);
        plan.mediaProbeValidationPlan =
            batchRenderedVideoMediaProbeValidationPlanFromResult(
                plan.mediaProbeResultPlan);
        plan.outputVerificationExecutionPlan =
            batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
                plan.outputVerificationPlan,
                plan.ffmpegExecutionPlan,
                plan.mediaProbeBinaryPlan,
                plan.mediaProbeCommandPlan,
                plan.mediaProbeJsonPlan,
                plan.mediaProbeResultPlan,
                plan.mediaProbeValidationPlan);
        plan.receiptHashValidationPlan =
            batchRenderedVideoReceiptHashValidationPlanFromExecution(
                plan.outputVerificationExecutionPlan);
        plan.outputVerificationExecutionPlan =
            batchRenderedVideoOutputVerificationExecutionPlanWithReceiptHashValidation(
                plan.outputVerificationExecutionPlan,
                plan.receiptHashValidationPlan);
        plan.outputVerificationDecisionPlan =
            batchRenderedVideoOutputVerificationDecisionPlanFromExecution(
                plan.outputVerificationExecutionPlan);
        plan.outputVerificationResultPlan =
            batchRenderedVideoOutputVerificationResultPlanFromDecision(
                plan.outputVerificationDecisionPlan);
        plan.outputVerificationReportPlan =
            batchRenderedVideoOutputVerificationReportPlanFromResult(
                plan.outputVerificationResultPlan);
        plan.outputVerificationReportContentPlan =
            batchRenderedVideoOutputVerificationReportContentPlanFromReport(
                plan.outputVerificationReportPlan);
        plan.outputVerificationReportWriterPlan =
            batchRenderedVideoOutputVerificationReportWriterPlanFromContent(
                plan.outputVerificationReportContentPlan);
        plan.sourceAudioPlan =
            batchRenderedVideoSourceAudioPlanForCurrentBuild(inputPath);
        plan.sourceAudioExtractionPlan =
            batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
                plan.sourceAudioPlan,
                batchRenderedVideoSourceAudioExtractionPathFromOutputPath(
                    plan.outputPlan.outputPath));
        plan.sourceAudioExtractionExecutionPlan =
            batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
                plan.sourceAudioExtractionPlan);
        plan.ffmpegAudioInputHandoffPlan =
            batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
                plan.sourceAudioExtractionExecutionPlan);
        plan.ffmpegAudioInputPlan =
            batchRenderedVideoFfmpegAudioInputPlanFromHandoff(
                plan.sourceAudioExtractionPlan,
                plan.ffmpegAudioInputHandoffPlan);
        plan.audioMuxPrerequisitesPlan =
            batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
                plan.sourceAudioPlan,
                plan.sourceAudioExtractionPlan,
                plan.ffmpegAudioInputPlan);
        plan.audioMuxExecutionPlan =
            batchRenderedVideoAudioMuxExecutionPlanFromContracts(
                plan.sourceAudioPlan,
                plan.ffmpegAudioInputHandoffPlan,
                plan.audioMuxPrerequisitesPlan);
        plan.ffmpegAudioPlan =
            batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
                plan.sourceAudioPlan,
                plan.audioMuxPrerequisitesPlan,
                plan.audioMuxExecutionPlan);
        plan.receiptApplicationPlan.reason =
            QStringLiteral("rendered source metadata unavailable");
    }
    else
    {
        plan.outputPlan.reason = QStringLiteral("not a rendered-video request");
        plan.sourceAudioPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.sourceAudioExtractionPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.sourceAudioExtractionExecutionPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.ffmpegAudioInputHandoffPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.ffmpegAudioInputPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.audioMuxPrerequisitesPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.audioMuxExecutionPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.ffmpegAudioPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.optionalFilterPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.receiptApplicationPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.frameProcessingPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.outputVerificationPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.mediaProbeJsonPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.mediaProbeResultPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.mediaProbeValidationPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.outputVerificationExecutionPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.receiptHashValidationPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.outputVerificationDecisionPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.outputVerificationResultPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.outputVerificationReportPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.outputVerificationReportContentPlan.reason =
            QStringLiteral("not a rendered-video request");
        plan.outputVerificationReportWriterPlan.reason =
            QStringLiteral("not a rendered-video request");
    }

    plan.runnerPrerequisites =
        batchRenderedVideoRunnerPrerequisitesForCurrentBuild();
    plan.targetReady = plan.target.complete;
    plan.encoderReady = plan.encoderPreset.ready;
    plan.ffmpegVideoReady = plan.ffmpegVideoPlan.ready;
    plan.ffmpegFilterReady = plan.ffmpegFilterPlan.ready;
    plan.optionalFilterContractReady =
        plan.optionalFilterPlan.contractReady;
    plan.sourceAudioContractReady =
        plan.sourceAudioPlan.contractReady;
    plan.sourceAudioExtractionContractReady =
        plan.sourceAudioExtractionPlan.contractReady;
    plan.sourceAudioExtractionExecutionContractReady =
        plan.sourceAudioExtractionExecutionPlan.contractReady;
    plan.ffmpegAudioInputHandoffContractReady =
        plan.ffmpegAudioInputHandoffPlan.contractReady;
    plan.ffmpegAudioInputContractReady =
        plan.ffmpegAudioInputPlan.contractReady;
    plan.audioMuxPrerequisitesContractReady =
        plan.audioMuxPrerequisitesPlan.contractReady;
    plan.audioMuxExecutionContractReady =
        plan.audioMuxExecutionPlan.contractReady;
    plan.ffmpegAudioContractReady =
        plan.ffmpegAudioPlan.contractReady;
    plan.ffmpegBinaryCommandReady =
        plan.ffmpegBinaryPlan.commandExecutableReady;
    plan.mediaProbeCommandReady =
        plan.mediaProbeBinaryPlan.commandExecutableReady;
    plan.mediaProbeCommandContractReady =
        plan.mediaProbeCommandPlan.contractReady;
    plan.mediaProbeJsonContractReady =
        plan.mediaProbeJsonPlan.contractReady;
    plan.mediaProbeResultContractReady =
        plan.mediaProbeResultPlan.contractReady;
    plan.mediaProbeValidationContractReady =
        plan.mediaProbeValidationPlan.contractReady;
    plan.outputReady = plan.outputPlan.ready;
    plan.outputVerificationContractReady =
        plan.outputVerificationPlan.contractReady;
    plan.outputVerificationExecutionContractReady =
        plan.outputVerificationExecutionPlan.contractReady;
    plan.receiptHashValidationContractReady =
        plan.receiptHashValidationPlan.contractReady;
    plan.outputVerificationDecisionContractReady =
        plan.outputVerificationDecisionPlan.contractReady;
    plan.outputVerificationResultContractReady =
        plan.outputVerificationResultPlan.contractReady;
    plan.outputVerificationReportContractReady =
        plan.outputVerificationReportPlan.contractReady;
    plan.outputVerificationReportContentContractReady =
        plan.outputVerificationReportContentPlan.contractReady;
    plan.outputVerificationReportWriterContractReady =
        plan.outputVerificationReportWriterPlan.contractReady;
    plan.preflightReady = plan.requestValid
                       && plan.targetReady
                       && plan.encoderReady
                       && plan.ffmpegVideoReady
                       && plan.ffmpegFilterReady
                       && plan.optionalFilterContractReady
                       && plan.sourceAudioContractReady
                       && plan.sourceAudioExtractionContractReady
                       && plan.sourceAudioExtractionExecutionContractReady
                       && plan.ffmpegAudioInputHandoffContractReady
                       && plan.ffmpegAudioInputContractReady
                       && plan.audioMuxPrerequisitesContractReady
                       && plan.audioMuxExecutionContractReady
                       && plan.ffmpegAudioContractReady
                       && plan.ffmpegBinaryCommandReady
                       && plan.mediaProbeCommandReady
                       && plan.mediaProbeCommandContractReady
                       && plan.mediaProbeJsonContractReady
                       && plan.mediaProbeResultContractReady
                       && plan.mediaProbeValidationContractReady
                       && plan.renderSettings.ready
                       && plan.outputReady
                       && plan.outputVerificationContractReady;
    plan.runnable = plan.preflightReady && plan.runnerPrerequisites.ready;
    return plan;
}

BatchRenderedVideoRunnerPrerequisites
batchRenderedVideoRunnerPrerequisitesForCurrentBuild()
{
    BatchRenderedVideoRunnerPrerequisites prerequisites;

    /* Receipt/aspect/colour parity: BatchRunner applies the receipt through the same
     * ReceiptApplier the CDNG runner uses, sets the debayer mode from the receipt exactly
     * as MainWindow::startExportPipe does, and takes every geometry decision from
     * batchRenderedVideoFfmpegFramePlanFromGuiState, which mirrors the GUI branch by
     * construction. Nothing is re-derived on the batch side. */
    prerequisites.processingParityReady = true;

    /* Frame processing: getMlvProcessedFrame16 into an RGB48 buffer plus the same
     * avir::CImageResizer resize the GUI performs when the frame plan reports `scaled`. */
    prerequisites.frameProcessingReady = true;

    /* Ffmpeg execution: export_process::StreamingPipeline, the same header-only seam the
     * GUI drives its rendered exports through. */
    prerequisites.ffmpegExecutionReady = true;

    /* Output verification: the encoded file is probed and its codec, container, geometry,
     * frame count and duration are checked against the plan before success is reported. */
    prerequisites.outputVerificationReady = true;

    /* Headless runner: BatchRunner::exportRenderedVideoFile drives all of the above with
     * no widget, dialog or event loop. */
    prerequisites.headlessRunnerReady = true;

    /* NOT IMPLEMENTED, and deliberately so. The roadmap's E4-1 card is a walking skeleton
     * -- "add no new framework until one clip reaches one MP4", "H.264 first". Source audio
     * is DISCOVERED (the source-audio plan is populated from the open MLV) but is not
     * extracted or muxed, so the emitted file is video-only. This flag stays false so the
     * gap is legible in every plan summary rather than implied by silence, and the runner
     * emits a [BATCH] RENDER_AUDIO_DROPPED line whenever a clip that HAS audio is exported.
     * Muxing belongs to E4-2 ("receipts, queues, progress, and supported codecs"). */
    prerequisites.audioMuxReady = false;

    /* `ready` intentionally does NOT include audioMuxReady. Requiring it would make the
     * five implemented capabilities unreachable and keep the product goal blocked on a
     * capability its own card defers. A video-only H.264 MP4 is the deliverable. */
    prerequisites.videoOnlyRunnerReady = prerequisites.processingParityReady
                                      && prerequisites.frameProcessingReady
                                      && prerequisites.ffmpegExecutionReady
                                      && prerequisites.outputVerificationReady
                                      && prerequisites.headlessRunnerReady;
    prerequisites.ready = prerequisites.videoOnlyRunnerReady;

    if( !prerequisites.ready )
    {
        prerequisites.reason = QStringLiteral("rendered processing parity, frame processing, ffmpeg execution, output verification, and headless rendered-export runner are not implemented");
    }
    else
    {
        prerequisites.reason.clear();
        if( !prerequisites.audioMuxReady )
        {
            prerequisites.limitation = QStringLiteral("rendered export is video-only; source audio is discovered but not muxed (E4-2)");
        }
    }
    return prerequisites;
}

BatchRenderedVideoOutputVerificationReportWriterPlan
batchRenderedVideoOutputVerificationReportWriterPlanFromContent(
    const BatchRenderedVideoOutputVerificationReportContentPlan & contentPlan)
{
    BatchRenderedVideoOutputVerificationReportWriterPlan plan;
    plan.expectedOutputPath = contentPlan.expectedOutputPath;
    plan.reportPath = contentPlan.reportPath;
    plan.reportFormat = contentPlan.reportFormat;
    plan.reportSchema = contentPlan.reportSchema;
    plan.reportKind = contentPlan.reportKind;
    plan.activeErrorCategory = contentPlan.activeErrorCategory;
    plan.failureReason = contentPlan.failureReason;
    plan.outputVerificationReportContentContractReady =
        contentPlan.contractReady;
    plan.schemaDetailContractReady =
        contentPlan.schemaDetailContractReady;
    plan.reportPathReady = contentPlan.reportPathReady;
    plan.reportFormatReady = contentPlan.reportFormatReady
                          && contentPlan.reportFormat
                             == QStringLiteral("json");
    plan.reportSchemaReady = contentPlan.reportSchemaReady
                          && contentPlan.reportSchema
                             == plan.reportSchema;
    plan.reportEncodingReady = plan.reportEncoding == QStringLiteral("utf-8");
    plan.writeModeReady = plan.writeMode
                       == QStringLiteral("atomic-json-sidecar");

    if( !contentPlan.contractReady )
    {
        plan.reason = contentPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification report writer contract unavailable")
            : contentPlan.reason;
        return plan;
    }

    plan.temporaryReportPath =
        batchRenderedVideoOutputVerificationReportTemporaryPath(
            contentPlan.reportPath);
    plan.temporaryReportPathPlanned = true;
    plan.temporaryReportPathReady =
        !plan.temporaryReportPath.isEmpty()
     && plan.temporaryReportPath != plan.reportPath;
    plan.writerPreflightPlanned = true;
    plan.serializationInputReady =
        contentPlan.schemaDetailContractReady
     && !contentPlan.topLevelKeys.isEmpty()
     && !contentPlan.expectedOutputKeys.isEmpty()
     && !contentPlan.verificationCheckKeys.isEmpty()
     && !contentPlan.artifactKeys.isEmpty()
     && !contentPlan.plannedErrorCategories.isEmpty()
     && !contentPlan.activeErrorCategory.isEmpty();
    plan.serializationPreflightPlanned = plan.serializationInputReady;
    plan.parentDirectoryPreflightPlanned = plan.reportPathReady;
    plan.fileCreationPreflightPlanned = plan.reportPathReady;
    plan.writePreflightPlanned = plan.serializationInputReady
                              && plan.temporaryReportPathReady;
    plan.cleanupPreflightPlanned = plan.writePreflightPlanned;
    plan.reportFileReady = plan.reportFileCreationOwned
                        && plan.reportFileCreationPlanned;
    plan.reportWriteReady = plan.reportWriteOwned
                         && plan.reportWritePlanned;
    plan.reportReady = plan.jsonSerializationReady
                    && plan.reportFileReady
                    && plan.reportWriteReady;
    plan.outputVerificationReady = contentPlan.outputVerificationReady
                                && plan.reportReady;
    plan.contractReady =
        plan.outputVerificationReportContentContractReady
     && plan.schemaDetailContractReady
     && plan.reportPathReady
     && plan.temporaryReportPathReady
     && plan.reportFormatReady
     && plan.reportSchemaReady
     && plan.reportEncodingReady
     && plan.writeModeReady
     && plan.writerPreflightPlanned
     && plan.serializationInputReady
     && plan.serializationPreflightPlanned
     && plan.parentDirectoryPreflightPlanned
     && plan.fileCreationPreflightPlanned
     && plan.writePreflightPlanned
     && plan.cleanupPreflightPlanned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered output verification report writer contract unavailable");
    }
    return plan;
}

QString batchRenderedVideoOutputVerificationReportTemporaryPath(
    const QString & reportPath)
{
    const QString cleaned = QDir::cleanPath(reportPath.trimmed());
    if( cleaned.isEmpty() ) return QString();
    return cleaned + QStringLiteral(".tmp");
}

BatchRenderedVideoOutputVerificationReportContentPlan
batchRenderedVideoOutputVerificationReportContentPlanFromReport(
    const BatchRenderedVideoOutputVerificationReportPlan & reportPlan)
{
    BatchRenderedVideoOutputVerificationReportContentPlan plan;
    plan.expectedOutputPath = reportPlan.expectedOutputPath;
    plan.resultState = reportPlan.resultState;
    plan.failureReason = reportPlan.failureReason;
    plan.reportPath = reportPlan.reportPath;
    plan.reportFormat = reportPlan.reportFormat;
    plan.reportSchema = reportPlan.reportSchema;
    plan.reportKind = reportPlan.reportKind;
    plan.outputVerificationReportContractReady = reportPlan.contractReady;
    plan.reportPathReady = reportPlan.reportPathReady;
    plan.reportFormatReady = reportPlan.reportFormatReady;
    plan.reportSchemaReady = reportPlan.reportSchemaReady;
    plan.reportKindReady = !reportPlan.reportKind.isEmpty();
    plan.reportContentPlanned = reportPlan.reportContentPlanned;
    plan.failureReportContentPlanned =
        reportPlan.failureReportContentPlanned;
    plan.acceptedReportContentPlanned =
        reportPlan.acceptedReportContentPlanned;
    plan.reportWritePlanned = reportPlan.reportWritePlanned;
    plan.reportWriteOwned = reportPlan.reportWriteOwned;
    plan.reportReady = reportPlan.reportReady;
    plan.outputVerificationReady = reportPlan.outputVerificationReady;

    if( !reportPlan.contractReady )
    {
        plan.reason = reportPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification report content contract unavailable")
            : reportPlan.reason;
        return plan;
    }

    plan.topLevelKeys = QStringList()
        << QStringLiteral("schema")
        << QStringLiteral("kind")
        << QStringLiteral("state")
        << QStringLiteral("expected")
        << QStringLiteral("checks")
        << QStringLiteral("failure")
        << QStringLiteral("artifacts");
    plan.expectedOutputKeys = QStringList()
        << QStringLiteral("path")
        << QStringLiteral("extension")
        << QStringLiteral("codec")
        << QStringLiteral("container")
        << QStringLiteral("frame_count")
        << QStringLiteral("duration_seconds");
    plan.verificationCheckKeys = QStringList()
        << QStringLiteral("file_exists")
        << QStringLiteral("file_non_empty")
        << QStringLiteral("ffprobe_json")
        << QStringLiteral("codec_container")
        << QStringLiteral("frame_count")
        << QStringLiteral("duration")
        << QStringLiteral("receipt_hash");
    plan.artifactKeys = QStringList()
        << QStringLiteral("rendered_output")
        << QStringLiteral("verification_report")
        << QStringLiteral("ffprobe_command")
        << QStringLiteral("receipt_sidecar");
    plan.plannedErrorCategories = QStringList()
        << QStringLiteral("runner-prerequisite")
        << QStringLiteral("file-check")
        << QStringLiteral("media-probe-execution")
        << QStringLiteral("media-probe-json")
        << QStringLiteral("codec-container-mismatch")
        << QStringLiteral("frame-count-mismatch")
        << QStringLiteral("duration-mismatch")
        << QStringLiteral("receipt-hash-mismatch")
        << QStringLiteral("report-write");
    plan.activeErrorCategory = reportPlan.outputAccepted
        ? QStringLiteral("none")
        : batchRenderedVideoOutputVerificationReportContentFailureCategory(
            reportPlan.failureReason);

    plan.topLevelKeysPlanned = !plan.topLevelKeys.isEmpty();
    plan.expectedOutputKeysPlanned = !plan.expectedOutputKeys.isEmpty();
    plan.verificationCheckKeysPlanned =
        !plan.verificationCheckKeys.isEmpty();
    plan.artifactKeysPlanned = !plan.artifactKeys.isEmpty();
    plan.errorCategoriesPlanned = !plan.plannedErrorCategories.isEmpty();
    plan.activeErrorCategoryPlanned = !plan.activeErrorCategory.isEmpty();
    plan.schemaDetailContractReady =
        plan.topLevelKeysPlanned
     && plan.expectedOutputKeysPlanned
     && plan.verificationCheckKeysPlanned
     && plan.artifactKeysPlanned
     && plan.errorCategoriesPlanned
     && plan.activeErrorCategoryPlanned;
    plan.contractReady =
        plan.outputVerificationReportContractReady
     && plan.reportPathReady
     && plan.reportFormatReady
     && plan.reportSchemaReady
     && plan.reportKindReady
     && plan.reportContentPlanned
     && plan.schemaDetailContractReady
     && (plan.failureReportContentPlanned
      || plan.acceptedReportContentPlanned);
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered output verification report content contract unavailable");
    }
    return plan;
}

QString batchRenderedVideoOutputVerificationReportContentFailureCategory(
    const QString & failureReason)
{
    const QString cleaned = failureReason.trimmed().toLower();
    if( cleaned.isEmpty() )
    {
        return QStringLiteral("unspecified-failure");
    }
    if( cleaned.contains(QStringLiteral("verification execution")) )
    {
        return QStringLiteral("output-verification-execution-not-ready");
    }
    if( cleaned.contains(QStringLiteral("file")) )
    {
        return QStringLiteral("file-check-failure");
    }
    if( cleaned.contains(QStringLiteral("probe")) )
    {
        return QStringLiteral("media-probe-failure");
    }
    if( cleaned.contains(QStringLiteral("receipt"))
     || cleaned.contains(QStringLiteral("hash")) )
    {
        return QStringLiteral("receipt-hash-failure");
    }
    return QStringLiteral("rendered-output-verification-failure");
}

BatchRenderedVideoOutputVerificationReportPlan
batchRenderedVideoOutputVerificationReportPlanFromResult(
    const BatchRenderedVideoOutputVerificationResultPlan & resultPlan)
{
    BatchRenderedVideoOutputVerificationReportPlan plan;
    plan.expectedOutputPath = resultPlan.expectedOutputPath;
    plan.expectedExtension = resultPlan.expectedExtension;
    plan.expectedCodec = resultPlan.expectedCodec;
    plan.expectedContainer = resultPlan.expectedContainer;
    plan.expectedFrameCount = resultPlan.expectedFrameCount;
    plan.expectedDurationSeconds = resultPlan.expectedDurationSeconds;
    plan.resultState = resultPlan.resultState;
    plan.failureReason = resultPlan.failureReason;
    plan.outputVerificationResultContractReady = resultPlan.contractReady;
    plan.resultReportPlanned = resultPlan.resultReportPlanned;
    plan.failureReportPlanned = resultPlan.failureReportPlanned;
    plan.acceptedOutputReportPlanned =
        resultPlan.acceptedOutputReportPlanned;
    plan.outputAccepted = resultPlan.outputAccepted;
    plan.reportKind = resultPlan.outputAccepted
        ? QStringLiteral("accepted-output")
        : QStringLiteral("failure");

    if( !resultPlan.contractReady )
    {
        plan.reason = resultPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification report artifact contract unavailable")
            : resultPlan.reason;
        return plan;
    }

    plan.reportPath =
        batchRenderedVideoOutputVerificationReportPathFromOutputPath(
            resultPlan.expectedOutputPath);
    plan.reportArtifactPlanned = resultPlan.resultReportPlanned;
    plan.reportPathPlanned = plan.reportArtifactPlanned;
    plan.reportPathReady = plan.reportPathPlanned
                        && !plan.reportPath.isEmpty();
    plan.reportFormatReady = plan.reportFormat == QStringLiteral("json");
    plan.reportSchemaReady = !plan.reportSchema.isEmpty();
    plan.reportContentPlanned = resultPlan.resultReportPlanned;
    plan.failureReportContentPlanned =
        resultPlan.failureReportPlanned;
    plan.acceptedReportContentPlanned =
        resultPlan.acceptedOutputReportPlanned;
    plan.reportFileReady = plan.reportFileCreationOwned
                        && plan.reportFileCreationPlanned;
    plan.reportWriteReady = plan.reportWriteOwned
                         && plan.reportWritePlanned;
    plan.reportReady = plan.reportFileReady
                    && plan.reportWriteReady;
    plan.outputVerificationReady = resultPlan.outputVerificationReady
                                && plan.reportReady;
    plan.contractReady = plan.outputVerificationResultContractReady
                      && plan.reportArtifactPlanned
                      && plan.reportPathReady
                      && plan.reportFormatReady
                      && plan.reportSchemaReady
                      && plan.reportContentPlanned
                      && (plan.failureReportContentPlanned
                       || plan.acceptedReportContentPlanned);
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered output verification report artifact contract unavailable");
    }
    return plan;
}

QString batchRenderedVideoOutputVerificationReportPathFromOutputPath(
    const QString & outputPath)
{
    const QString cleaned = QDir::cleanPath(outputPath.trimmed());
    if( cleaned.isEmpty() ) return QString();

    const QFileInfo outputInfo(cleaned);
    const QString baseName = outputInfo.completeBaseName().isEmpty()
        ? outputInfo.fileName()
        : outputInfo.completeBaseName();
    if( baseName.isEmpty() ) return QString();

    return QDir::cleanPath(outputInfo.dir().filePath(
        baseName + QStringLiteral(".verification-report.json")));
}

BatchRenderedVideoOutputVerificationResultPlan
batchRenderedVideoOutputVerificationResultPlanFromDecision(
    const BatchRenderedVideoOutputVerificationDecisionPlan & decisionPlan)
{
    BatchRenderedVideoOutputVerificationResultPlan plan;
    plan.expectedOutputPath = decisionPlan.expectedOutputPath;
    plan.expectedExtension = decisionPlan.expectedExtension;
    plan.expectedCodec = decisionPlan.expectedCodec;
    plan.expectedContainer = decisionPlan.expectedContainer;
    plan.expectedFrameCount = decisionPlan.expectedFrameCount;
    plan.expectedDurationSeconds = decisionPlan.expectedDurationSeconds;
    plan.outputVerificationDecisionContractReady =
        decisionPlan.contractReady;
    plan.fileChecksReady = decisionPlan.fileChecksReady;
    plan.mediaProbeValidationReady =
        decisionPlan.mediaProbeValidationReady;
    plan.receiptHashValidationReady =
        decisionPlan.receiptHashValidationReady;
    plan.verificationExecutionReady =
        decisionPlan.verificationExecutionReady;
    plan.verificationDecisionReady =
        decisionPlan.verificationDecisionReady;
    plan.outputAccepted = decisionPlan.outputAccepted;
    plan.resultState = decisionPlan.outputAccepted
        ? QStringLiteral("accepted")
        : QStringLiteral("blocked");

    if( !decisionPlan.contractReady )
    {
        plan.reason = decisionPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification result report contract unavailable")
            : decisionPlan.reason;
        return plan;
    }

    plan.resultReportPlanned = true;
    plan.failureReportPlanned = !decisionPlan.outputAccepted;
    plan.acceptedOutputReportPlanned = decisionPlan.outputAccepted;
    plan.failureReason = decisionPlan.outputAccepted
        ? QString()
        : batchRenderedVideoOutputVerificationResultFailureReason(decisionPlan);
    plan.failureReportReady = plan.failureReportOwned
                            && plan.failureReportPlanned;
    plan.acceptedOutputReportReady = plan.acceptedOutputReportOwned
                                   && plan.acceptedOutputReportPlanned;
    plan.resultReportReady = plan.resultReportOwned
                           && (plan.failureReportReady
                            || plan.acceptedOutputReportReady);
    plan.outputVerificationReady = plan.outputAccepted
                                && plan.resultReportReady;
    plan.contractReady = !plan.expectedOutputPath.isEmpty()
                      && !plan.expectedExtension.isEmpty()
                      && !plan.expectedCodec.isEmpty()
                      && !plan.expectedContainer.isEmpty()
                      && plan.outputVerificationDecisionContractReady
                      && plan.resultReportPlanned
                      && (plan.failureReportPlanned
                       || plan.acceptedOutputReportPlanned);
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered output verification result report contract unavailable");
    }
    return plan;
}

QString batchRenderedVideoOutputVerificationResultFailureReason(
    const BatchRenderedVideoOutputVerificationDecisionPlan & decisionPlan)
{
    if( !decisionPlan.verificationExecutionReady )
        return QStringLiteral("rendered output verification execution is not ready");
    if( !decisionPlan.fileChecksReady )
        return QStringLiteral("rendered output file checks are not ready");
    if( !decisionPlan.mediaProbeValidationReady )
        return QStringLiteral("rendered output probe validation is not ready");
    if( !decisionPlan.receiptHashValidationReady )
        return QStringLiteral("rendered receipt/hash validation is not ready");
    if( !decisionPlan.verificationDecisionReady )
        return QStringLiteral("rendered output verification decision is not ready");
    return QStringLiteral("rendered output is not accepted");
}

BatchRenderedVideoOutputVerificationDecisionPlan
batchRenderedVideoOutputVerificationDecisionPlanFromExecution(
    const BatchRenderedVideoOutputVerificationExecutionPlan & executionPlan)
{
    BatchRenderedVideoOutputVerificationDecisionPlan plan;
    plan.expectedOutputPath = executionPlan.expectedOutputPath;
    plan.expectedExtension = executionPlan.expectedExtension;
    plan.expectedCodec = executionPlan.expectedCodec;
    plan.expectedContainer = executionPlan.expectedContainer;
    plan.expectedFrameCount = executionPlan.expectedFrameCount;
    plan.expectedDurationSeconds = executionPlan.expectedDurationSeconds;
    plan.outputVerificationExecutionContractReady =
        executionPlan.contractReady;
    plan.fileExistenceCheckPlanned =
        executionPlan.fileExistenceCheckPlanned;
    plan.nonEmptyCheckPlanned = executionPlan.nonEmptyCheckPlanned;
    plan.fileChecksPlanned = plan.fileExistenceCheckPlanned
                          && plan.nonEmptyCheckPlanned;
    plan.filesystemInspectionOwned =
        executionPlan.filesystemInspectionOwned;
    plan.fileExistenceCheckOwned =
        executionPlan.fileExistenceCheckOwned;
    plan.nonEmptyCheckOwned = executionPlan.nonEmptyCheckOwned;
    plan.fileChecksOwned = plan.fileExistenceCheckOwned
                        && plan.nonEmptyCheckOwned;
    plan.fileExistenceCheckReady =
        executionPlan.fileExistenceCheckReady;
    plan.nonEmptyCheckReady = executionPlan.nonEmptyCheckReady;
    plan.fileChecksReady = plan.fileChecksOwned
                        && plan.fileExistenceCheckReady
                        && plan.nonEmptyCheckReady;
    plan.mediaProbeValidationContractReady =
        executionPlan.mediaProbeValidationContractReady;
    plan.codecContainerComparisonPlanned =
        executionPlan.mediaProbeValidationCodecContainerComparisonPlanned;
    plan.codecContainerComparisonReady =
        executionPlan.mediaProbeValidationCodecContainerComparisonReady;
    plan.frameCountComparisonPlanned =
        executionPlan.mediaProbeValidationFrameCountComparisonPlanned;
    plan.frameCountComparisonReady =
        executionPlan.mediaProbeValidationFrameCountComparisonReady;
    plan.durationComparisonPlanned =
        executionPlan.mediaProbeValidationDurationComparisonPlanned;
    plan.durationComparisonReady =
        executionPlan.mediaProbeValidationDurationComparisonReady;
    plan.receiptHashValidationSource =
        executionPlan.receiptHashValidationSource;
    plan.receiptHashValidationReason =
        executionPlan.receiptHashValidationReason;
    plan.receiptHashValidationContractReady =
        executionPlan.receiptHashValidationContractReady;
    plan.mediaProbeValidationPlanned =
        executionPlan.mediaProbeValidationParsedResultIngestPlanned
     && plan.codecContainerComparisonPlanned;
    plan.mediaProbeValidationOwned =
        executionPlan.mediaProbeValidationOwned;
    plan.mediaProbeValidationReady =
        executionPlan.mediaProbeValidationReady
     && plan.mediaProbeValidationOwned;
    plan.receiptHashValidationPlanned =
        executionPlan.receiptHashValidationPlanned;
    plan.receiptComparisonPlanned =
        executionPlan.receiptComparisonPlanned;
    plan.outputHashComparisonPlanned =
        executionPlan.outputHashComparisonPlanned;
    plan.receiptReadOwned =
        executionPlan.receiptReadOwned;
    plan.outputHashReadOwned =
        executionPlan.outputHashReadOwned;
    plan.receiptComparisonOwned =
        executionPlan.receiptComparisonOwned;
    plan.outputHashComparisonOwned =
        executionPlan.outputHashComparisonOwned;
    plan.receiptComparisonReady =
        executionPlan.receiptComparisonReady;
    plan.outputHashComparisonReady =
        executionPlan.outputHashComparisonReady;
    plan.receiptHashValidationOwned =
        executionPlan.receiptHashValidationOwned;
    plan.receiptHashValidationReady =
        executionPlan.receiptHashValidationReady;
    plan.verificationExecutionReady =
        executionPlan.verificationExecutionReady;
    plan.verificationDecisionReady = plan.fileChecksReady
                                  && plan.mediaProbeValidationReady
                                  && plan.receiptHashValidationReady
                                  && plan.verificationExecutionReady;
    plan.outputAccepted = plan.verificationDecisionReady;
    plan.contractReady = executionPlan.contractReady
                      && plan.fileChecksPlanned
                      && plan.mediaProbeValidationContractReady
                      && plan.mediaProbeValidationPlanned
                      && plan.receiptHashValidationContractReady
                      && plan.receiptHashValidationPlanned;
    if( !plan.contractReady )
    {
        plan.reason = executionPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification decision contract unavailable")
            : executionPlan.reason;
    }
    return plan;
}

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanWithReceiptHashValidation(
    const BatchRenderedVideoOutputVerificationExecutionPlan & executionPlan,
    const BatchRenderedVideoReceiptHashValidationPlan & receiptHashPlan)
{
    BatchRenderedVideoOutputVerificationExecutionPlan plan = executionPlan;
    plan.receiptHashValidationSource = receiptHashPlan.source;
    plan.receiptHashValidationReason = receiptHashPlan.reason;
    plan.receiptHashValidationContractReady = receiptHashPlan.contractReady;
    plan.receiptHashValidationPlanned =
        receiptHashPlan.validationPlanned;
    plan.receiptComparisonPlanned =
        receiptHashPlan.receiptComparisonPlanned;
    plan.outputHashComparisonPlanned =
        receiptHashPlan.outputHashComparisonPlanned;
    plan.receiptReadOwned = receiptHashPlan.receiptReadOwned;
    plan.outputHashReadOwned = receiptHashPlan.outputHashReadOwned;
    plan.receiptComparisonOwned =
        receiptHashPlan.receiptComparisonOwned;
    plan.outputHashComparisonOwned =
        receiptHashPlan.outputHashComparisonOwned;
    plan.receiptComparisonReady =
        receiptHashPlan.receiptComparisonReady;
    plan.outputHashComparisonReady =
        receiptHashPlan.outputHashComparisonReady;
    plan.receiptHashValidationOwned =
        receiptHashPlan.receiptHashValidationOwned;
    plan.receiptHashValidationReady =
        receiptHashPlan.receiptHashValidationReady;
    plan.verificationExecutionReady = plan.fileExistenceCheckOwned
                                   && plan.nonEmptyCheckOwned
                                   && plan.mediaProbeExecutionOwned
                                   && plan.codecContainerValidationOwned
                                   && plan.frameCountValidationOwned
                                   && plan.receiptHashValidationReady;
    return plan;
}

BatchRenderedVideoReceiptHashValidationPlan
batchRenderedVideoReceiptHashValidationPlanFromExecution(
    const BatchRenderedVideoOutputVerificationExecutionPlan & executionPlan)
{
    BatchRenderedVideoReceiptHashValidationPlan plan;
    plan.expectedOutputPath = executionPlan.expectedOutputPath;
    plan.expectedExtension = executionPlan.expectedExtension;
    plan.expectedCodec = executionPlan.expectedCodec;
    plan.expectedContainer = executionPlan.expectedContainer;
    plan.expectedFrameCount = executionPlan.expectedFrameCount;
    plan.expectedDurationSeconds =
        executionPlan.expectedDurationSeconds;
    plan.outputVerificationExecutionContractReady =
        executionPlan.contractReady;

    if( !executionPlan.contractReady )
    {
        plan.reason = executionPlan.reason.isEmpty()
            ? QStringLiteral("rendered receipt/hash validation contract unavailable")
            : executionPlan.reason;
        return plan;
    }

    plan.validationPlanned = true;
    plan.receiptComparisonPlanned = true;
    plan.outputHashComparisonPlanned = true;
    plan.receiptHashValidationOwned =
        executionPlan.receiptHashValidationOwned;
    plan.receiptHashValidationReady =
        plan.receiptHashValidationOwned
     && plan.receiptComparisonReady
     && plan.outputHashComparisonReady;
    plan.contractReady = !plan.expectedOutputPath.isEmpty()
                      && !plan.expectedExtension.isEmpty()
                      && !plan.expectedCodec.isEmpty()
                      && !plan.expectedContainer.isEmpty()
                      && plan.outputVerificationExecutionContractReady
                      && plan.validationPlanned
                      && plan.receiptComparisonPlanned
                      && plan.outputHashComparisonPlanned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered receipt/hash validation contract unavailable");
    }
    return plan;
}

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoFfmpegExecutionPlan & ffmpegExecutionPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan)
{
    return batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
        outputVerificationPlan,
        ffmpegExecutionPlan,
        mediaProbeBinaryPlan,
        batchRenderedVideoMediaProbeCommandPlanFromContracts(
            outputVerificationPlan,
            mediaProbeBinaryPlan));
}

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoFfmpegExecutionPlan & ffmpegExecutionPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan)
{
    return batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
        outputVerificationPlan,
        ffmpegExecutionPlan,
        mediaProbeBinaryPlan,
        mediaProbeCommandPlan,
        batchRenderedVideoMediaProbeJsonPlanFromCommand(
            mediaProbeCommandPlan));
}

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoFfmpegExecutionPlan & ffmpegExecutionPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan,
    const BatchRenderedVideoMediaProbeJsonPlan & mediaProbeJsonPlan)
{
    const BatchRenderedVideoMediaProbeResultPlan mediaProbeResultPlan =
        batchRenderedVideoMediaProbeResultPlanFromContracts(
            outputVerificationPlan,
            mediaProbeCommandPlan,
            mediaProbeJsonPlan);
    return batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
        outputVerificationPlan,
        ffmpegExecutionPlan,
        mediaProbeBinaryPlan,
        mediaProbeCommandPlan,
        mediaProbeJsonPlan,
        mediaProbeResultPlan,
        batchRenderedVideoMediaProbeValidationPlanFromResult(
            mediaProbeResultPlan));
}

BatchRenderedVideoOutputVerificationExecutionPlan
batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoFfmpegExecutionPlan & ffmpegExecutionPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan,
    const BatchRenderedVideoMediaProbeJsonPlan & mediaProbeJsonPlan,
    const BatchRenderedVideoMediaProbeResultPlan & mediaProbeResultPlan,
    const BatchRenderedVideoMediaProbeValidationPlan & mediaProbeValidationPlan)
{
    BatchRenderedVideoOutputVerificationExecutionPlan plan;
    plan.expectedOutputPath = outputVerificationPlan.expectedOutputPath;
    plan.expectedExtension = outputVerificationPlan.expectedExtension;
    plan.expectedCodec = outputVerificationPlan.expectedCodec;
    plan.expectedContainer = outputVerificationPlan.expectedContainer;
    plan.expectedFrameCount = outputVerificationPlan.expectedFrameCount;
    plan.expectedDurationSeconds =
        outputVerificationPlan.expectedDurationSeconds;
    plan.mediaProbeBinarySource = mediaProbeBinaryPlan.source;
    plan.mediaProbeRequestedExecutable =
        mediaProbeBinaryPlan.requestedExecutable;
    plan.mediaProbeResolvedExecutable =
        mediaProbeBinaryPlan.resolvedExecutable;
    plan.mediaProbeBinaryReason = mediaProbeBinaryPlan.reason;
    plan.mediaProbePathSearchOwned =
        mediaProbeBinaryPlan.pathSearchOwned;
    plan.mediaProbePathSearchAttempted =
        mediaProbeBinaryPlan.pathSearchAttempted;
    plan.mediaProbeFoundOnPath = mediaProbeBinaryPlan.foundOnPath;
    plan.mediaProbeCommandReady =
        mediaProbeBinaryPlan.commandExecutableReady;
    plan.mediaProbeExecutable =
        mediaProbeBinaryPlan.resolvedExecutable.isEmpty()
            ? mediaProbeBinaryPlan.requestedExecutable
            : mediaProbeBinaryPlan.resolvedExecutable;
    plan.mediaProbeCommandSource = mediaProbeCommandPlan.source;
    plan.mediaProbeCommandArguments = mediaProbeCommandPlan.arguments;
    plan.mediaProbeCommandLine = mediaProbeCommandPlan.commandLine;
    plan.mediaProbeCommandReason = mediaProbeCommandPlan.reason;
    plan.mediaProbeCommandPlanned = mediaProbeCommandPlan.commandPlanned;
    plan.mediaProbeInvocationCommandReady =
        mediaProbeCommandPlan.commandReady;
    plan.mediaProbeCommandExecutionOwned =
        mediaProbeCommandPlan.executionOwned;
    plan.mediaProbeCommandExecutionReady =
        mediaProbeCommandPlan.executionReady;
    plan.mediaProbeCommandContractReady =
        mediaProbeCommandPlan.contractReady;
    plan.mediaProbeJsonSource = mediaProbeJsonPlan.source;
    plan.mediaProbeJsonReason = mediaProbeJsonPlan.reason;
    plan.mediaProbeJsonContractReady =
        mediaProbeJsonPlan.contractReady;
    plan.mediaProbeJsonStdoutCapturePlanned =
        mediaProbeJsonPlan.stdoutCapturePlanned;
    plan.mediaProbeJsonStderrCapturePlanned =
        mediaProbeJsonPlan.stderrCapturePlanned;
    plan.mediaProbeJsonExitCodeValidationPlanned =
        mediaProbeJsonPlan.exitCodeValidationPlanned;
    plan.mediaProbeJsonTimeoutPlanned =
        mediaProbeJsonPlan.timeoutPlanned;
    plan.mediaProbeJsonErrorReportingPlanned =
        mediaProbeJsonPlan.errorReportingPlanned;
    plan.mediaProbeJsonDocumentPlanned =
        mediaProbeJsonPlan.jsonDocumentPlanned;
    plan.mediaProbeJsonStdoutCaptureOwned =
        mediaProbeJsonPlan.stdoutCaptureOwned;
    plan.mediaProbeJsonStderrCaptureOwned =
        mediaProbeJsonPlan.stderrCaptureOwned;
    plan.mediaProbeJsonExitCodeValidationOwned =
        mediaProbeJsonPlan.exitCodeValidationOwned;
    plan.mediaProbeJsonTimeoutOwned =
        mediaProbeJsonPlan.timeoutOwned;
    plan.mediaProbeJsonRawOutputOwned =
        mediaProbeJsonPlan.rawJsonOutputOwned;
    plan.mediaProbeJsonParseOwned =
        mediaProbeJsonPlan.jsonParseOwned;
    plan.mediaProbeJsonRawOutputReady =
        mediaProbeJsonPlan.rawJsonReady;
    plan.mediaProbeJsonParseReady =
        mediaProbeJsonPlan.jsonParseReady;
    plan.mediaProbeJsonErrorFree =
        mediaProbeJsonPlan.errorFree;
    plan.mediaProbeJsonReady =
        mediaProbeJsonPlan.rawJsonReady
     && mediaProbeJsonPlan.jsonParseReady
     && mediaProbeJsonPlan.errorFree;
    plan.mediaProbeResultSource = mediaProbeResultPlan.source;
    plan.mediaProbeResultParsedCodec =
        mediaProbeResultPlan.parsedCodec;
    plan.mediaProbeResultParsedContainer =
        mediaProbeResultPlan.parsedContainer;
    plan.mediaProbeResultParsedFrameCount =
        mediaProbeResultPlan.parsedFrameCount;
    plan.mediaProbeResultParsedDurationSeconds =
        mediaProbeResultPlan.parsedDurationSeconds;
    plan.mediaProbeResultReason = mediaProbeResultPlan.reason;
    plan.mediaProbeResultContractReady =
        mediaProbeResultPlan.contractReady;
    plan.mediaProbeResultParsingPlanned =
        mediaProbeResultPlan.resultParsingPlanned;
    plan.mediaProbeResultSchemaReady =
        mediaProbeResultPlan.resultSchemaReady;
    plan.mediaProbeResultCodecContainerFieldsPlanned =
        mediaProbeResultPlan.codecContainerFieldsPlanned;
    plan.mediaProbeResultFrameCountFieldPlanned =
        mediaProbeResultPlan.frameCountFieldPlanned;
    plan.mediaProbeResultDurationFieldPlanned =
        mediaProbeResultPlan.durationFieldPlanned;
    plan.mediaProbeResultOwned =
        mediaProbeResultPlan.mediaProbeResultOwned;
    plan.mediaProbeResultJsonParseOwned =
        mediaProbeResultPlan.jsonParseOwned;
    plan.mediaProbeResultCodecContainerReady =
        mediaProbeResultPlan.codecContainerResultReady;
    plan.mediaProbeResultFrameCountReady =
        mediaProbeResultPlan.frameCountResultReady;
    plan.mediaProbeResultDurationReady =
        mediaProbeResultPlan.durationResultReady;
    plan.mediaProbeResultReady =
        mediaProbeResultPlan.resultReady;
    plan.mediaProbeValidationSource = mediaProbeValidationPlan.source;
    plan.mediaProbeValidationReason = mediaProbeValidationPlan.reason;
    plan.mediaProbeValidationContractReady =
        mediaProbeValidationPlan.contractReady;
    plan.mediaProbeValidationParsedResultReady =
        mediaProbeValidationPlan.parsedResultReady;
    plan.mediaProbeValidationParsedResultIngestPlanned =
        mediaProbeValidationPlan.parsedResultIngestPlanned;
    plan.mediaProbeValidationParsedResultIngestOwned =
        mediaProbeValidationPlan.parsedResultIngestOwned;
    plan.mediaProbeValidationParsedResultIngestReady =
        mediaProbeValidationPlan.parsedResultIngestReady;
    plan.mediaProbeValidationCodecContainerComparisonPlanned =
        mediaProbeValidationPlan.codecContainerComparisonPlanned;
    plan.mediaProbeValidationCodecContainerComparisonReady =
        mediaProbeValidationPlan.codecContainerComparisonReady;
    plan.mediaProbeValidationCodecContainerMatches =
        mediaProbeValidationPlan.codecContainerMatches;
    plan.mediaProbeValidationFrameCountComparisonPlanned =
        mediaProbeValidationPlan.frameCountComparisonPlanned;
    plan.mediaProbeValidationFrameCountComparisonReady =
        mediaProbeValidationPlan.frameCountComparisonReady;
    plan.mediaProbeValidationFrameCountMatches =
        mediaProbeValidationPlan.frameCountMatches;
    plan.mediaProbeValidationDurationComparisonPlanned =
        mediaProbeValidationPlan.durationComparisonPlanned;
    plan.mediaProbeValidationDurationComparisonReady =
        mediaProbeValidationPlan.durationComparisonReady;
    plan.mediaProbeValidationDurationMatches =
        mediaProbeValidationPlan.durationMatches;
    plan.mediaProbeValidationOwned =
        mediaProbeValidationPlan.validationOwned;
    plan.mediaProbeValidationReady =
        mediaProbeValidationPlan.validationReady;
    plan.outputVerificationContractReady =
        outputVerificationPlan.contractReady;
    plan.ffmpegExecutionContractReady = ffmpegExecutionPlan.contractReady;
    plan.ffmpegAudioArguments = ffmpegExecutionPlan.audioArguments;
    plan.ffmpegAudioTransitionSource =
        ffmpegExecutionPlan.audioTransitionSource;
    plan.ffmpegAudioTransitionArguments =
        ffmpegExecutionPlan.audioTransitionArguments;
    plan.ffmpegAudioContractReady =
        ffmpegExecutionPlan.audioContractReady;
    plan.ffmpegAudioMuxExecutionContractReady =
        ffmpegExecutionPlan.audioMuxExecutionContractReady;
    plan.ffmpegAudioMuxPlanned = ffmpegExecutionPlan.audioMuxPlanned;
    plan.ffmpegAudioMuxArgumentHandoffPlanned =
        ffmpegExecutionPlan.audioMuxArgumentHandoffPlanned;
    plan.ffmpegAudioSyncValidationPlanned =
        ffmpegExecutionPlan.audioSyncValidationPlanned;
    plan.ffmpegAudioMuxExecutionReady =
        ffmpegExecutionPlan.audioMuxExecutionReady;
    plan.ffmpegMuxedAudioCommandPlanned =
        ffmpegExecutionPlan.muxedAudioCommandPlanned;
    plan.ffmpegMuxedAudioCommandReady =
        ffmpegExecutionPlan.muxedAudioCommandReady;
    plan.ffmpegAudioInputOwned = ffmpegExecutionPlan.audioInputOwned;
    plan.nonEmptyMinimumBytes = outputVerificationPlan.nonEmptyMinimumBytes;
    plan.fileExistenceCheckPlanned =
        outputVerificationPlan.fileExistenceCheckPlanned;
    plan.nonEmptyCheckPlanned = outputVerificationPlan.nonEmptyCheckPlanned;
    plan.filesystemInspectionOwned =
        outputVerificationPlan.filesystemInspectionOwned;
    plan.fileExistenceCheckReady =
        outputVerificationPlan.fileExistenceCheckReady;
    plan.nonEmptyCheckReady = outputVerificationPlan.nonEmptyCheckReady;
    plan.codecContainerCheckPlanned =
        outputVerificationPlan.codecContainerCheckPlanned;
    plan.codecContainerExpectationReady =
        outputVerificationPlan.codecContainerExpectationReady;
    plan.codecContainerValidationReady =
        outputVerificationPlan.codecContainerValidationReady
     && plan.codecContainerValidationOwned;
    plan.frameCountCheckPlanned =
        outputVerificationPlan.frameCountCheckPlanned;
    plan.frameCountExpectationReady =
        outputVerificationPlan.frameCountExpectationReady;
    plan.frameCountValidationReady =
        outputVerificationPlan.frameCountValidationReady
     && plan.frameCountValidationOwned;
    plan.fileExistenceCheckOwned =
        outputVerificationPlan.fileExistenceCheckOwned;
    plan.nonEmptyCheckOwned = outputVerificationPlan.nonEmptyCheckOwned;

    if( !outputVerificationPlan.contractReady )
    {
        plan.reason = outputVerificationPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification contract unavailable")
            : outputVerificationPlan.reason;
        return plan;
    }
    if( !ffmpegExecutionPlan.contractReady )
    {
        plan.reason = ffmpegExecutionPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg execution contract unavailable")
            : ffmpegExecutionPlan.reason;
        return plan;
    }

    plan.contractReady = !plan.expectedOutputPath.isEmpty()
                      && !plan.expectedExtension.isEmpty()
                      && !plan.expectedCodec.isEmpty()
                      && !plan.expectedContainer.isEmpty()
                      && !plan.mediaProbeExecutable.isEmpty()
                      && plan.mediaProbeCommandReady
                      && plan.mediaProbeCommandContractReady
                      && plan.mediaProbeJsonContractReady
                      && plan.mediaProbeResultContractReady
                      && plan.mediaProbeValidationContractReady
                      && plan.codecContainerExpectationReady
                      && plan.outputVerificationContractReady
                      && plan.ffmpegExecutionContractReady;
    plan.verificationExecutionReady = plan.fileExistenceCheckOwned
                                   && plan.nonEmptyCheckOwned
                                   && plan.mediaProbeExecutionOwned
                                   && plan.codecContainerValidationOwned
                                   && plan.frameCountValidationOwned
                                   && plan.receiptHashValidationReady;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered output verification execution contract unavailable");
    }
    return plan;
}

BatchRenderedVideoFfmpegExecutionPlan
batchRenderedVideoFfmpegExecutionPlanFromCommand(
    const BatchRenderedVideoFfmpegCommandPlan & commandPlan)
{
    BatchRenderedVideoFfmpegExecutionPlan plan;
    plan.executable = commandPlan.executable;
    plan.commandLine = commandPlan.commandLine;
    plan.commandReady = commandPlan.ready;
    plan.audioArguments = commandPlan.audioArguments;
    plan.audioTransitionSource = commandPlan.audioTransitionSource;
    plan.audioTransitionArguments = commandPlan.audioTransitionArguments;
    plan.audioContractReady = commandPlan.audioContractReady;
    plan.audioMuxExecutionContractReady =
        commandPlan.audioMuxExecutionContractReady;
    plan.audioMuxPlanned = commandPlan.audioMuxPlanned;
    plan.audioMuxArgumentHandoffPlanned =
        commandPlan.audioMuxArgumentHandoffPlanned;
    plan.audioSyncValidationPlanned = commandPlan.audioSyncValidationPlanned;
    plan.audioMuxExecutionReady = commandPlan.audioMuxExecutionReady;
    plan.muxedAudioCommandPlanned = commandPlan.muxedAudioCommandPlanned;
    plan.muxedAudioCommandReady = commandPlan.muxedAudioCommandReady;
    plan.audioInputOwned = commandPlan.audioInputOwned;

    if( !commandPlan.ready )
    {
        plan.reason = commandPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg command plan unavailable")
            : commandPlan.reason;
        return plan;
    }

    plan.contractReady = !plan.executable.isEmpty()
                      && !plan.commandLine.isEmpty();
    plan.executionReady = plan.processLaunchOwned
                       && plan.stdinPipeOwned
                       && plan.rawFrameFeedOwned
                       && plan.stderrCaptureOwned
                       && plan.exitCodeValidationOwned
                       && plan.timeoutOwned
                       && plan.cleanupOwned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered ffmpeg execution contract unavailable");
    }
    return plan;
}

BatchRenderedVideoFfmpegCommandPlan
batchRenderedVideoFfmpegCommandPlanFromParts(
    const BatchRenderedVideoFfmpegFramePlan & framePlan,
    const BatchRenderedVideoFfmpegFilterPlan & filterPlan,
    const BatchRenderedVideoFfmpegVideoPlan & videoPlan,
    const BatchRenderedVideoOutputPlan & outputPlan)
{
    return batchRenderedVideoFfmpegCommandPlanFromParts(
        framePlan,
        filterPlan,
        batchRenderedVideoFfmpegAudioPlanForCurrentBuild(),
        videoPlan,
        outputPlan,
        batchRenderedVideoFfmpegBinaryPlanFromRequestedName());
}

QString batchRenderedVideoMediaProbeArgumentsWithPath(
    const BatchRenderedVideoMediaProbeCommandPlan & commandPlan,
    const QString & probePath)
{
    const QString trimmed = probePath.trimmed();
    if( !commandPlan.commandReady || trimmed.isEmpty() )
        return QString();

    /* Same format string and same quoting helper as
     * batchRenderedVideoMediaProbeCommandPlanFromContracts, so the only difference from
     * commandPlan.arguments is which file is named. */
    return QStringLiteral("-v error -show_format -show_streams -of json %1")
        .arg(batchRenderedVideoCommandExecutableForDisplay(trimmed));
}

QString batchRenderedVideoFfmpegArgumentsWithOutputPath(
    const BatchRenderedVideoFfmpegCommandPlan & commandPlan,
    const QString & outputPath)
{
    const QString trimmed = outputPath.trimmed();
    if( !commandPlan.ready || trimmed.isEmpty() )
        return QString();

    return QStringLiteral("%1 %2 %3 %4 %5 \"%6\"")
        .arg(commandPlan.rawInputArguments)
        .arg(commandPlan.videoArguments)
        .arg(commandPlan.colorArguments)
        .arg(commandPlan.filterArguments)
        .arg(commandPlan.audioArguments)
        .arg(trimmed);
}

QString batchRenderedVideoPartialOutputPath(const QString & outputPath)
{
    const QString trimmed = outputPath.trimmed();
    if( trimmed.isEmpty() )
        return QString();

    const QString marker = QStringLiteral(".mlvapp-partial");
    const QFileInfo info( trimmed );
    const QString suffix = info.suffix();
    if( suffix.isEmpty() )
        return trimmed + marker;

    /* Strip exactly the final ".<suffix>" and re-attach it after the marker, so the
     * extension ffmpeg reads is unchanged. */
    return trimmed.left( trimmed.length() - suffix.length() - 1 )
         + marker + QStringLiteral(".") + suffix;
}

bool batchRenderedVideoSampleAspectIsSquare(const QString & sampleAspectRatio)
{
    return sampleAspectRatio.isEmpty()
        || sampleAspectRatio == QStringLiteral("1:1")
        || sampleAspectRatio == QStringLiteral("0:1")
        || sampleAspectRatio == QStringLiteral("1:1(implied)");
}

BatchRenderedVideoFfmpegDumpFacts
batchRenderedVideoFfmpegDumpFacts(const QString & dumpText)
{
    BatchRenderedVideoFfmpegDumpFacts facts;

    const QRegularExpressionMatch inputMatch =
        QRegularExpression( QStringLiteral( R"(^Input #\d+,\s*(.+?),\s*from )" ),
                            QRegularExpression::MultilineOption ).match( dumpText );
    if( !inputMatch.hasMatch() )
        return facts;          /* no stream dump at all -> report nothing, claim nothing */
    facts.formatNames = inputMatch.captured( 1 ).trimmed();

    /* First VIDEO stream only: the one the rendered runner wrote. */
    const QRegularExpressionMatch videoMatch =
        QRegularExpression( QStringLiteral( R"(^\s*Stream #\d+:\d+.*?:\s*Video:\s*([A-Za-z0-9_]+))" ),
                            QRegularExpression::MultilineOption ).match( dumpText );
    if( !videoMatch.hasMatch() )
        return facts;
    facts.codecName = videoMatch.captured( 1 );

    /* Geometry and the optional SAR/DAR bracket are read from THAT stream's line only, so
     * a second stream's numbers can never be attributed to the video stream. */
    const int lineStart = videoMatch.capturedStart( 0 );
    int lineEnd = dumpText.indexOf( QLatin1Char('\n'), lineStart );
    if( lineEnd < 0 ) lineEnd = dumpText.length();
    const QString streamLine = dumpText.mid( lineStart, lineEnd - lineStart );

    const QRegularExpressionMatch dimMatch =
        QRegularExpression( QStringLiteral( R"(,\s*(\d+)x(\d+)\b)" ) ).match( streamLine );
    if( dimMatch.hasMatch() )
    {
        facts.width  = dimMatch.captured( 1 ).toInt();
        facts.height = dimMatch.captured( 2 ).toInt();
    }

    const QRegularExpressionMatch sarMatch =
        QRegularExpression( QStringLiteral( R"(\[SAR (\d+:\d+) DAR (\d+:\d+)\])" ) )
            .match( streamLine );
    if( sarMatch.hasMatch() )
    {
        facts.sampleAspectRatio  = sarMatch.captured( 1 );
        facts.displayAspectRatio = sarMatch.captured( 2 );
    }
    else
    {
        facts.sampleAspectRatio = QStringLiteral("1:1(implied)");
        if( facts.width > 0 && facts.height > 0 )
        {
            int divisor = facts.width;
            int remainder = facts.height;
            while( remainder != 0 )
            {
                const int next = divisor % remainder;
                divisor = remainder;
                remainder = next;
            }
            facts.displayAspectRatio = QStringLiteral("%1:%2")
                .arg( facts.width / divisor ).arg( facts.height / divisor );
        }
    }

    facts.parsed = true;
    return facts;
}

BatchRenderedVideoFfmpegCommandPlan
batchRenderedVideoFfmpegCommandPlanFromParts(
    const BatchRenderedVideoFfmpegFramePlan & framePlan,
    const BatchRenderedVideoFfmpegFilterPlan & filterPlan,
    const BatchRenderedVideoFfmpegAudioPlan & audioPlan,
    const BatchRenderedVideoFfmpegVideoPlan & videoPlan,
    const BatchRenderedVideoOutputPlan & outputPlan,
    const BatchRenderedVideoFfmpegBinaryPlan & binaryPlan)
{
    BatchRenderedVideoFfmpegCommandPlan plan;
    plan.executable = binaryPlan.resolvedExecutable;
    if( !binaryPlan.commandExecutableReady )
    {
        plan.reason = binaryPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg executable unavailable")
            : binaryPlan.reason;
        return plan;
    }
    if( !framePlan.ready )
    {
        plan.reason = framePlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg frame plan unavailable")
            : framePlan.reason;
        return plan;
    }
    if( !filterPlan.ready )
    {
        plan.reason = filterPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg filter plan unavailable")
            : filterPlan.reason;
        return plan;
    }
    if( !audioPlan.contractReady )
    {
        plan.reason = audioPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg audio contract unavailable")
            : audioPlan.reason;
        return plan;
    }
    if( !videoPlan.ready )
    {
        plan.reason = videoPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg video plan unavailable")
            : videoPlan.reason;
        return plan;
    }
    if( !outputPlan.ready )
    {
        plan.reason = outputPlan.reason.isEmpty()
            ? QStringLiteral("rendered output path invalid")
            : outputPlan.reason;
        return plan;
    }

    plan.rawInputArguments =
        QStringLiteral("-r %1 -y -f rawvideo -s %2 -pix_fmt %3 -i -")
            .arg(framePlan.frameRateArgument)
            .arg(framePlan.frameSizeArgument)
            .arg(plan.rawInputPixelFormat);
    plan.rawVideoPipeInputReady =
        !plan.rawInputArguments.isEmpty()
     && !framePlan.frameRateArgument.isEmpty()
     && !framePlan.frameSizeArgument.isEmpty();
    plan.colorArguments =
        QStringLiteral("-color_primaries %1 -color_trc %1 -colorspace bt709")
            .arg(plan.colorTag);
    plan.audioArguments = audioPlan.audioArguments;
    plan.audioTransitionSource = audioPlan.source;
    plan.audioTransitionArguments = audioPlan.muxTransitionArguments;
    plan.audioContractReady = audioPlan.contractReady;
    plan.audioMuxExecutionContractReady = audioPlan.audioMuxExecutionContractReady;
    plan.audioMuxPlanned = audioPlan.audioMuxPlanned;
    plan.audioMuxArgumentHandoffPlanned =
        audioPlan.audioMuxArgumentHandoffPlanned;
    plan.audioSyncValidationPlanned = audioPlan.audioSyncValidationPlanned;
    plan.audioMuxExecutionReady = audioPlan.audioMuxExecutionReady;
    plan.muxedAudioCommandPlanned = audioPlan.muxedAudioCommandPlanned;
    plan.muxedAudioCommandReady = audioPlan.muxedAudioCommandReady;
    plan.audioInputOwned = audioPlan.audioInputOwned;

    plan.videoArguments = videoPlan.videoArguments;
    plan.filterArguments = filterPlan.filterArguments;

    plan.arguments =
        QStringLiteral("%1 %2 %3 %4 %5 \"%6\"")
            .arg(plan.rawInputArguments)
            .arg(videoPlan.videoArguments)
            .arg(plan.colorArguments)
            .arg(filterPlan.filterArguments)
            .arg(plan.audioArguments)
            .arg(outputPlan.outputPath);
    plan.commandLine =
        QStringLiteral("%1 %2")
            .arg(batchRenderedVideoCommandExecutableForDisplay(plan.executable),
                 plan.arguments);
    plan.ready = !plan.executable.isEmpty()
              && plan.rawVideoPipeInputReady
              && !videoPlan.videoArguments.isEmpty()
              && !filterPlan.filterArguments.isEmpty()
              && !plan.colorArguments.isEmpty()
              && !plan.audioArguments.isEmpty()
              && !outputPlan.outputPath.isEmpty()
              && !plan.arguments.isEmpty()
              && !plan.commandLine.isEmpty();
    if( !plan.ready )
        plan.reason = QStringLiteral("rendered ffmpeg command plan unavailable");
    return plan;
}

BatchRenderedVideoMediaProbeValidationPlan
batchRenderedVideoMediaProbeValidationPlanFromResult(
    const BatchRenderedVideoMediaProbeResultPlan & mediaProbeResultPlan)
{
    BatchRenderedVideoMediaProbeValidationPlan plan;
    plan.expectedOutputPath = mediaProbeResultPlan.expectedOutputPath;
    plan.expectedCodec = mediaProbeResultPlan.expectedCodec;
    plan.expectedContainer = mediaProbeResultPlan.expectedContainer;
    plan.expectedFrameCount = mediaProbeResultPlan.expectedFrameCount;
    plan.expectedDurationSeconds =
        mediaProbeResultPlan.expectedDurationSeconds;
    plan.parsedCodec = mediaProbeResultPlan.parsedCodec;
    plan.parsedContainer = mediaProbeResultPlan.parsedContainer;
    plan.parsedFrameCount = mediaProbeResultPlan.parsedFrameCount;
    plan.parsedDurationSeconds =
        mediaProbeResultPlan.parsedDurationSeconds;
    plan.mediaProbeResultSource = mediaProbeResultPlan.source;
    plan.mediaProbeResultContractReady =
        mediaProbeResultPlan.contractReady;
    plan.parsedResultReady = mediaProbeResultPlan.resultReady;

    if( !mediaProbeResultPlan.contractReady )
    {
        plan.reason = mediaProbeResultPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification probe result contract unavailable")
            : mediaProbeResultPlan.reason;
        return plan;
    }

    plan.parsedResultIngestPlanned =
        mediaProbeResultPlan.resultParsingPlanned
     && mediaProbeResultPlan.resultSchemaReady;
    plan.parsedResultIngestReady =
        plan.parsedResultIngestOwned
     && mediaProbeResultPlan.resultReady;
    plan.codecContainerComparisonPlanned =
        mediaProbeResultPlan.codecContainerFieldsPlanned;
    plan.frameCountComparisonPlanned =
        mediaProbeResultPlan.frameCountFieldPlanned;
    plan.durationComparisonPlanned =
        mediaProbeResultPlan.durationFieldPlanned;
    plan.codecContainerComparisonReady =
        plan.parsedResultIngestReady
     && mediaProbeResultPlan.codecContainerResultReady;
    plan.frameCountComparisonReady =
        plan.parsedResultIngestReady
     && mediaProbeResultPlan.frameCountResultReady;
    plan.durationComparisonReady =
        plan.parsedResultIngestReady
     && mediaProbeResultPlan.durationResultReady;
    plan.codecContainerMatches =
        plan.codecContainerComparisonReady
     && plan.parsedCodec == plan.expectedCodec
     && plan.parsedContainer == plan.expectedContainer;
    plan.frameCountMatches =
        plan.frameCountComparisonReady
     && plan.parsedFrameCount == plan.expectedFrameCount;
    plan.durationMatches =
        plan.durationComparisonReady
     && std::fabs(plan.parsedDurationSeconds
                - plan.expectedDurationSeconds) < 0.000001;
    plan.validationReady =
        plan.validationOwned
     && plan.parsedResultIngestReady
     && plan.codecContainerComparisonReady
     && (!plan.frameCountComparisonPlanned
         || plan.frameCountComparisonReady)
     && (!plan.durationComparisonPlanned
         || plan.durationComparisonReady);
    plan.contractReady = plan.mediaProbeResultContractReady
                      && plan.parsedResultIngestPlanned
                      && plan.codecContainerComparisonPlanned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered output verification probe validation contract unavailable");
    }
    return plan;
}

BatchRenderedVideoMediaProbeResultPlan
batchRenderedVideoMediaProbeResultPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan)
{
    return batchRenderedVideoMediaProbeResultPlanFromContracts(
        outputVerificationPlan,
        mediaProbeCommandPlan,
        batchRenderedVideoMediaProbeJsonPlanFromCommand(
            mediaProbeCommandPlan));
}

BatchRenderedVideoMediaProbeResultPlan
batchRenderedVideoMediaProbeResultPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan,
    const BatchRenderedVideoMediaProbeJsonPlan & mediaProbeJsonPlan)
{
    BatchRenderedVideoMediaProbeResultPlan plan;
    plan.expectedOutputPath = outputVerificationPlan.expectedOutputPath;
    plan.expectedCodec = outputVerificationPlan.expectedCodec;
    plan.expectedContainer = outputVerificationPlan.expectedContainer;
    plan.expectedFrameCount = outputVerificationPlan.expectedFrameCount;
    plan.expectedDurationSeconds =
        outputVerificationPlan.expectedDurationSeconds;
    plan.outputVerificationContractReady =
        outputVerificationPlan.contractReady;
    plan.mediaProbeCommandContractReady =
        mediaProbeCommandPlan.contractReady;
    plan.mediaProbeJsonSource = mediaProbeJsonPlan.source;
    plan.mediaProbeJsonContractReady =
        mediaProbeJsonPlan.contractReady;
    plan.mediaProbeCommandExecutionOwned =
        mediaProbeCommandPlan.executionOwned;
    plan.mediaProbeCommandExecutionReady =
        mediaProbeCommandPlan.executionReady;
    plan.rawJsonReady = mediaProbeJsonPlan.rawJsonReady;
    plan.jsonParseReady = mediaProbeJsonPlan.jsonParseReady;
    plan.jsonErrorFree = mediaProbeJsonPlan.errorFree;
    plan.jsonParseOwned = mediaProbeJsonPlan.jsonParseOwned;

    if( !outputVerificationPlan.contractReady )
    {
        plan.reason = outputVerificationPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification contract unavailable")
            : outputVerificationPlan.reason;
        return plan;
    }
    if( !mediaProbeCommandPlan.contractReady )
    {
        plan.reason = mediaProbeCommandPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification probe command contract unavailable")
            : mediaProbeCommandPlan.reason;
        return plan;
    }
    if( !mediaProbeJsonPlan.contractReady )
    {
        plan.reason = mediaProbeJsonPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification probe JSON contract unavailable")
            : mediaProbeJsonPlan.reason;
        return plan;
    }

    plan.codecContainerFieldsPlanned =
        outputVerificationPlan.codecContainerCheckPlanned
     && outputVerificationPlan.codecContainerExpectationReady;
    plan.frameCountFieldPlanned =
        outputVerificationPlan.frameCountCheckPlanned
     && outputVerificationPlan.frameCountExpectationReady;
    plan.durationFieldPlanned =
        plan.frameCountFieldPlanned
     && outputVerificationPlan.expectedDurationSeconds > 0.0;
    plan.resultParsingPlanned =
        mediaProbeJsonPlan.jsonDocumentPlanned
     && !mediaProbeCommandPlan.commandLine.isEmpty();
    plan.resultSchemaReady = plan.codecContainerFieldsPlanned
                          || plan.frameCountFieldPlanned
                          || plan.durationFieldPlanned;
    plan.contractReady = plan.outputVerificationContractReady
                      && plan.mediaProbeCommandContractReady
                      && plan.mediaProbeJsonContractReady
                      && plan.resultParsingPlanned
                      && plan.resultSchemaReady;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered output verification probe result contract unavailable");
    }
    return plan;
}

BatchRenderedVideoMediaProbeJsonPlan
batchRenderedVideoMediaProbeJsonPlanFromCommand(
    const BatchRenderedVideoMediaProbeCommandPlan & mediaProbeCommandPlan)
{
    BatchRenderedVideoMediaProbeJsonPlan plan;
    plan.expectedOutputPath = mediaProbeCommandPlan.expectedOutputPath;
    plan.commandLine = mediaProbeCommandPlan.commandLine;
    plan.mediaProbeCommandContractReady =
        mediaProbeCommandPlan.contractReady;
    plan.mediaProbeCommandExecutionOwned =
        mediaProbeCommandPlan.executionOwned;
    plan.mediaProbeCommandExecutionReady =
        mediaProbeCommandPlan.executionReady;

    if( !mediaProbeCommandPlan.contractReady )
    {
        plan.reason = mediaProbeCommandPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification probe command contract unavailable")
            : mediaProbeCommandPlan.reason;
        return plan;
    }

    plan.stdoutCapturePlanned = mediaProbeCommandPlan.commandPlanned;
    plan.stderrCapturePlanned = mediaProbeCommandPlan.commandPlanned;
    plan.exitCodeValidationPlanned = mediaProbeCommandPlan.commandPlanned;
    plan.timeoutPlanned = mediaProbeCommandPlan.commandPlanned;
    plan.errorReportingPlanned = mediaProbeCommandPlan.commandPlanned;
    plan.jsonDocumentPlanned =
        mediaProbeCommandPlan.commandPlanned
     && mediaProbeCommandPlan.commandReady;
    plan.contractReady = plan.mediaProbeCommandContractReady
                      && plan.stdoutCapturePlanned
                      && plan.stderrCapturePlanned
                      && plan.exitCodeValidationPlanned
                      && plan.timeoutPlanned
                      && plan.errorReportingPlanned
                      && plan.jsonDocumentPlanned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered output verification probe JSON contract unavailable");
    }
    return plan;
}

BatchRenderedVideoMediaProbeCommandPlan
batchRenderedVideoMediaProbeCommandPlanFromContracts(
    const BatchRenderedVideoOutputVerificationPlan & outputVerificationPlan,
    const BatchRenderedVideoMediaProbeBinaryPlan & mediaProbeBinaryPlan)
{
    BatchRenderedVideoMediaProbeCommandPlan plan;
    plan.expectedOutputPath = outputVerificationPlan.expectedOutputPath;
    plan.executable = mediaProbeBinaryPlan.resolvedExecutable.isEmpty()
        ? mediaProbeBinaryPlan.requestedExecutable
        : mediaProbeBinaryPlan.resolvedExecutable;
    plan.outputVerificationContractReady =
        outputVerificationPlan.contractReady;
    plan.mediaProbeExecutableReady =
        mediaProbeBinaryPlan.commandExecutableReady;

    if( !outputVerificationPlan.contractReady )
    {
        plan.reason = outputVerificationPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification contract unavailable")
            : outputVerificationPlan.reason;
        return plan;
    }
    if( !plan.mediaProbeExecutableReady || plan.executable.isEmpty() )
    {
        plan.reason = mediaProbeBinaryPlan.reason.isEmpty()
            ? QStringLiteral("rendered output verification media probe executable unavailable")
            : mediaProbeBinaryPlan.reason;
        return plan;
    }

    plan.arguments =
        QStringLiteral("-v error -show_format -show_streams -of json %1")
            .arg(batchRenderedVideoCommandExecutableForDisplay(
                plan.expectedOutputPath));
    plan.commandLine = QStringLiteral("%1 %2")
        .arg(batchRenderedVideoCommandExecutableForDisplay(plan.executable))
        .arg(plan.arguments);
    plan.commandPlanned = true;
    plan.commandReady = !plan.commandLine.isEmpty();
    plan.contractReady = plan.outputVerificationContractReady
                      && plan.mediaProbeExecutableReady
                      && plan.commandReady;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered output verification probe command contract unavailable");
    }
    return plan;
}

QString batchRenderedVideoCommandExecutableForDisplay(
    const QString & executable)
{
    if( executable.contains(QLatin1Char(' '))
     || executable.contains(QLatin1Char('\t')) )
    {
        return QStringLiteral("\"%1\"").arg(executable);
    }
    return executable;
}

namespace
{

/* BATCH-FFMPEG-DEFAULT-APPDIR-LOOKUP-1. The packaged ffmpeg.exe/ffprobe.exe sit beside
 * MLVApp.exe, and QStandardPaths::findExecutable(name) searches PATH only. So a DEFAULT
 * request (no --rendered-ffmpeg) looks in the application directory first and falls back to
 * PATH; an explicit request keeps the PATH-only lookup byte for byte. */
template <typename Plan, typename FromResolvedPath>
Plan batchRenderedVideoBinaryPlanFromLookup(
    const QString & requestedExecutable,
    const QString & defaultName,
    const QString & applicationDirectory,
    const BatchRenderedVideoExecutableResolver & resolver,
    FromResolvedPath fromResolvedPath)
{
    const QString trimmed = requestedExecutable.trimmed();
    const QString requested = trimmed.isEmpty() ? defaultName : trimmed;
    const auto resolve = [&resolver](const QString & name, const QStringList & dirs)
    {
        return resolver ? resolver(name, dirs) : QString();
    };

    const QString appDir = applicationDirectory.trimmed();
    if( requested != defaultName || appDir.isEmpty() )
        return fromResolvedPath(requested, resolve(requested, QStringList()));

    const QString cleanAppDir = QDir::cleanPath(appDir);
    const QString appDirHit =
        resolve(requested, QStringList{ cleanAppDir }).trimmed();
    if( !appDirHit.isEmpty() )
    {
        Plan plan = fromResolvedPath(requested, appDirHit);
        plan.source = QStringLiteral("app-dir");
        plan.pathSearchAttempted = false;
        plan.foundOnPath = false;
        plan.appDirSearchAttempted = true;
        plan.foundInAppDir = true;
        return plan;
    }

    Plan plan = fromResolvedPath(requested, resolve(requested, QStringList()));
    plan.appDirSearchAttempted = true;
    if( !plan.foundOnPath )
    {
        plan.reason =
            QStringLiteral("%1 executable not found in the application directory (%2) or on PATH")
                .arg(defaultName, cleanAppDir);
    }
    return plan;
}

BatchRenderedVideoExecutableResolver batchRenderedVideoSystemExecutableResolver()
{
    return [](const QString & name, const QStringList & searchDirs)
    {
        return QStandardPaths::findExecutable(name, searchDirs);
    };
}

/* Read here, in the *FromCurrentEnvironment wrappers only. No instance (a console test)
 * means no application directory: the lookup is then PATH-only, as before. */
QString batchRenderedVideoCurrentApplicationDirectory()
{
    return QCoreApplication::instance()
        ? QCoreApplication::applicationDirPath()
        : QString();
}

} /* anonymous namespace */

BatchRenderedVideoMediaProbeBinaryPlan
batchRenderedVideoMediaProbeBinaryPlanFromLookup(
    const QString & requestedExecutable,
    const QString & applicationDirectory,
    const BatchRenderedVideoExecutableResolver & resolver)
{
    return batchRenderedVideoBinaryPlanFromLookup<BatchRenderedVideoMediaProbeBinaryPlan>(
        requestedExecutable,
        QStringLiteral("ffprobe"),
        applicationDirectory,
        resolver,
        [](const QString & requested, const QString & resolved)
        {
            return batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
                requested, resolved);
        });
}

BatchRenderedVideoFfmpegBinaryPlan
batchRenderedVideoFfmpegBinaryPlanFromLookup(
    const QString & requestedExecutable,
    const QString & applicationDirectory,
    const BatchRenderedVideoExecutableResolver & resolver)
{
    return batchRenderedVideoBinaryPlanFromLookup<BatchRenderedVideoFfmpegBinaryPlan>(
        requestedExecutable,
        QStringLiteral("ffmpeg"),
        applicationDirectory,
        resolver,
        [](const QString & requested, const QString & resolved)
        {
            return batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(
                requested, resolved);
        });
}

BatchRenderedVideoMediaProbeBinaryPlan
batchRenderedVideoMediaProbeBinaryPlanFromCurrentEnvironment(
    const QString & requestedExecutable)
{
    return batchRenderedVideoMediaProbeBinaryPlanFromLookup(
        requestedExecutable,
        batchRenderedVideoCurrentApplicationDirectory(),
        batchRenderedVideoSystemExecutableResolver());
}

BatchRenderedVideoMediaProbeBinaryPlan
batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
    const QString & requestedExecutable,
    const QString & resolvedExecutable)
{
    BatchRenderedVideoMediaProbeBinaryPlan plan =
        batchRenderedVideoMediaProbeBinaryPlanFromRequestedName(
            requestedExecutable);
    plan.source = QStringLiteral("path-search");
    plan.pathSearchOwned = true;
    plan.pathSearchAttempted = true;

    const QString resolved = resolvedExecutable.trimmed();
    if( !resolved.isEmpty() )
    {
        plan.resolvedExecutable = QDir::cleanPath(resolved);
        plan.foundOnPath = true;
        plan.commandExecutableReady = true;
        plan.reason.clear();
    }
    else
    {
        plan.foundOnPath = false;
        plan.commandExecutableReady = !plan.requestedExecutable.isEmpty();
        plan.reason = QStringLiteral("ffprobe executable not found on PATH");
    }
    return plan;
}

BatchRenderedVideoMediaProbeBinaryPlan
batchRenderedVideoMediaProbeBinaryPlanFromRequestedName(
    const QString & requestedExecutable)
{
    BatchRenderedVideoMediaProbeBinaryPlan plan;
    const QString requested = requestedExecutable.trimmed();
    plan.requestedExecutable = requested.isEmpty()
        ? QStringLiteral("ffprobe")
        : requested;
    if( !requested.isEmpty() && requested != QStringLiteral("ffprobe") )
        plan.source = QStringLiteral("requested-executable");
    plan.resolvedExecutable = plan.requestedExecutable;
    plan.commandExecutableReady = !plan.resolvedExecutable.isEmpty();
    if( !plan.commandExecutableReady )
    {
        plan.reason = QStringLiteral("ffprobe executable name is empty");
    }
    return plan;
}

BatchRenderedVideoFfmpegBinaryPlan
batchRenderedVideoFfmpegBinaryPlanFromCurrentEnvironment(
    const QString & requestedExecutable)
{
    return batchRenderedVideoFfmpegBinaryPlanFromLookup(
        requestedExecutable,
        batchRenderedVideoCurrentApplicationDirectory(),
        batchRenderedVideoSystemExecutableResolver());
}

BatchRenderedVideoFfmpegBinaryPlan
batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(
    const QString & requestedExecutable,
    const QString & resolvedExecutable)
{
    BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromRequestedName(
            requestedExecutable);
    plan.source = QStringLiteral("path-search");
    plan.pathSearchOwned = true;
    plan.pathSearchAttempted = true;

    const QString resolved = resolvedExecutable.trimmed();
    if( !resolved.isEmpty() )
    {
        plan.resolvedExecutable = QDir::cleanPath(resolved);
        plan.foundOnPath = true;
        plan.commandExecutableReady = true;
        plan.reason.clear();
    }
    else
    {
        plan.foundOnPath = false;
        plan.commandExecutableReady = !plan.requestedExecutable.isEmpty();
        plan.reason = QStringLiteral("ffmpeg executable not found on PATH");
    }
    return plan;
}

BatchRenderedVideoFfmpegBinaryPlan
batchRenderedVideoFfmpegBinaryPlanFromRequestedName(
    const QString & requestedExecutable)
{
    BatchRenderedVideoFfmpegBinaryPlan plan;
    const QString requested = requestedExecutable.trimmed();
    plan.requestedExecutable = requested.isEmpty()
        ? QStringLiteral("ffmpeg")
        : requested;
    if( !requested.isEmpty() && requested != QStringLiteral("ffmpeg") )
        plan.source = QStringLiteral("requested-executable");
    plan.resolvedExecutable = plan.requestedExecutable;
    plan.commandExecutableReady = !plan.resolvedExecutable.isEmpty();
    if( !plan.commandExecutableReady )
    {
        plan.reason = QStringLiteral("ffmpeg executable name is empty");
    }
    return plan;
}

BatchRenderedVideoOutputVerificationPlan
batchRenderedVideoOutputVerificationPlanFromOutput(
    const BatchRenderedVideoOutputPlan & outputPlan,
    const BatchRenderedVideoTarget & target,
    const BatchRenderedVideoSourceMetadata & sourceMetadata)
{
    BatchRenderedVideoOutputVerificationPlan plan =
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target);
    plan.expectedFrameCount =
        sourceMetadata.frameCountReady ? sourceMetadata.frameCount : 0;
    plan.expectedDurationSeconds =
        sourceMetadata.frameCountReady ? sourceMetadata.durationSeconds : 0.0;
    plan.frameCountExpectationReady =
        plan.expectedFrameCount > 0
     && plan.expectedDurationSeconds > 0.0;
    plan.frameCountCheckPlanned =
        plan.contractReady && plan.frameCountExpectationReady;
    return plan;
}

BatchRenderedVideoOutputVerificationPlan
batchRenderedVideoOutputVerificationPlanFromOutput(
    const BatchRenderedVideoOutputPlan & outputPlan,
    const BatchRenderedVideoTarget & target)
{
    BatchRenderedVideoOutputVerificationPlan plan;
    plan.expectedOutputPath = outputPlan.outputPath;
    plan.expectedExtension = target.extension;
    plan.expectedCodec =
        QString::fromLatin1(batchRenderedVideoMediaProbeCodecName(target.codec));
    plan.expectedContainer =
        QString::fromLatin1(batchRenderedVideoMediaProbeContainerName(
            target.container));

    if( !outputPlan.ready )
    {
        plan.reason = outputPlan.reason.isEmpty()
            ? QStringLiteral("rendered output path invalid")
            : outputPlan.reason;
        return plan;
    }
    if( plan.expectedExtension.isEmpty() )
    {
        plan.reason = QStringLiteral("rendered target extension unavailable");
        return plan;
    }

    plan.outputPathReady = !plan.expectedOutputPath.isEmpty();
    plan.codecContainerExpectationReady =
        !plan.expectedCodec.isEmpty()
     && plan.expectedCodec != QStringLiteral("unspecified")
     && !plan.expectedContainer.isEmpty()
     && plan.expectedContainer != QStringLiteral("unspecified");
    const QFileInfo outputInfo(plan.expectedOutputPath);
    plan.extensionMatchesTarget =
        outputInfo.suffix().toLower()
            == plan.expectedExtension.mid(1).toLower();
    plan.contractReady = plan.outputPathReady
                      && plan.extensionMatchesTarget
                      && plan.codecContainerExpectationReady;
    if( plan.contractReady )
    {
        plan.fileExistenceCheckPlanned = true;
        plan.nonEmptyCheckPlanned = true;
        plan.codecContainerCheckPlanned = true;
    }
    if( !plan.contractReady )
    {
        plan.reason = QStringLiteral("rendered output verification contract unavailable");
    }
    return plan;
}

BatchRenderedVideoOutputPlan batchRenderedVideoOutputPlanFromPaths(
    const QString & inputPath,
    const QString & outputPath,
    const BatchRenderedVideoTarget & target)
{
    return batchRenderedVideoOutputPlanFromPaths(
        inputPath,
        outputPath,
        target,
        1);
}

BatchRenderedVideoOutputPlan batchRenderedVideoOutputPlanFromPaths(
    const QString & inputPath,
    const QString & outputPath,
    const BatchRenderedVideoTarget & target,
    int inputClipCount)
{
    BatchRenderedVideoOutputPlan plan;
    plan.inputClipCount = inputClipCount;
    if( !target.complete || target.extension.isEmpty() )
    {
        plan.reason = QStringLiteral("rendered target incomplete");
        return plan;
    }

    if( inputClipCount < 1 )
    {
        plan.reason = QStringLiteral("rendered input clip count invalid");
        return plan;
    }

    const QString inputTrimmed = inputPath.trimmed();
    if( inputTrimmed.isEmpty() )
    {
        plan.reason = QStringLiteral("input path is empty");
        return plan;
    }

    const QString outputTrimmed = outputPath.trimmed();
    if( outputTrimmed.isEmpty() )
    {
        plan.reason = QStringLiteral("output path is empty");
        return plan;
    }

    const QFileInfo inputInfo(inputTrimmed);
    const QString baseName = inputInfo.completeBaseName();
    if( baseName.isEmpty() )
    {
        plan.reason = QStringLiteral("input base name is empty");
        return plan;
    }

    const QString extension = target.extension.startsWith(QLatin1Char('.'))
        ? target.extension
        : QStringLiteral(".") + target.extension;
    const bool explicitDirectory =
        outputTrimmed.endsWith(QLatin1Char('/'))
     || outputTrimmed.endsWith(QLatin1Char('\\'));
    const QFileInfo outputInfo(outputTrimmed);
    const bool explicitFileOutput =
        !explicitDirectory && !outputInfo.suffix().isEmpty();
    plan.explicitFileOutput = explicitFileOutput;

    if( !explicitFileOutput )
    {
        plan.outputPath = QDir::cleanPath(
            QDir(outputTrimmed).filePath(baseName + extension));
        plan.ready = true;
        return plan;
    }

    if( outputInfo.suffix().toLower() != extension.mid(1).toLower() )
    {
        plan.reason = QStringLiteral("output path extension does not match target extension");
        return plan;
    }

    if( inputClipCount > 1 )
    {
        plan.reason = QStringLiteral("explicit rendered output file requires a single input clip");
        return plan;
    }

    plan.outputPath = QDir::cleanPath(outputTrimmed);
    plan.ready = true;
    return plan;
}

BatchRenderedVideoFrameProcessingPlan
batchRenderedVideoFrameProcessingPlanFromFramePlan(
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoFfmpegFramePlan & framePlan)
{
    return batchRenderedVideoFrameProcessingPlanFromFramePlan(
        metadata,
        framePlan,
        batchRenderedVideoReceiptApplicationPlanFromContracts(
            metadata,
            framePlan));
}

BatchRenderedVideoFrameProcessingPlan
batchRenderedVideoFrameProcessingPlanFromFramePlan(
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoFfmpegFramePlan & framePlan,
    const BatchRenderedVideoReceiptApplicationPlan & receiptPlan)
{
    BatchRenderedVideoFrameProcessingPlan plan;
    plan.sourceMetadataReady = metadata.ready;
    plan.frameGeometryReady = framePlan.ready;
    plan.outputSize = framePlan.frameSizeArgument;

    if( !metadata.ready )
    {
        plan.reason = metadata.reason.isEmpty()
            ? QStringLiteral("rendered source metadata unavailable")
            : metadata.reason;
        return plan;
    }
    if( !framePlan.ready )
    {
        plan.reason = framePlan.reason.isEmpty()
            ? QStringLiteral("rendered frame geometry unavailable")
            : framePlan.reason;
        return plan;
    }

    plan.contractReady = !plan.rawFramePixelFormat.isEmpty()
                      && !plan.outputSize.isEmpty()
                      && plan.sourceMetadataReady
                      && plan.frameGeometryReady
                      && receiptPlan.contractReady;
    if( plan.contractReady )
    {
        plan.receiptApplicationContractReady = receiptPlan.contractReady;
        plan.debayerContractReady = true;
        plan.previewProcessingContractReady = true;
        plan.resizeProcessingContractReady = true;
        plan.rgb48FrameBufferContractReady = true;
        plan.frameIterationContractReady = true;
    }
    plan.receiptApplicationOwned = receiptPlan.applicationReady;
    plan.processingParityReady = plan.receiptApplicationOwned
                              && plan.debayerOwned
                              && plan.previewProcessingOwned
                              && plan.resizeProcessingOwned
                              && plan.processingParityValidationOwned;
    plan.frameProcessingReady = plan.processingParityReady
                             && plan.rgb48FrameBufferOwned
                             && plan.frameIterationOwned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered frame-processing contract unavailable");
    }
    return plan;
}

BatchRenderedVideoReceiptApplicationPlan
batchRenderedVideoReceiptApplicationPlanFromContracts(
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoFfmpegFramePlan & framePlan)
{
    BatchRenderedVideoReceiptApplicationPlan plan;
    plan.sourceMetadataReady = metadata.ready;
    plan.frameGeometryReady = framePlan.ready;

    if( !metadata.ready )
    {
        plan.reason = metadata.reason.isEmpty()
            ? QStringLiteral("rendered source metadata unavailable")
            : metadata.reason;
        return plan;
    }
    if( !framePlan.ready )
    {
        plan.reason = framePlan.reason.isEmpty()
            ? QStringLiteral("rendered frame geometry unavailable")
            : framePlan.reason;
        return plan;
    }

    plan.inputContractReady = !plan.inputState.isEmpty()
                           && plan.sourceMetadataReady
                           && plan.frameGeometryReady;
    plan.outputContractReady = !plan.outputState.isEmpty()
                            && plan.inputContractReady;
    plan.contractReady = plan.inputContractReady
                      && plan.outputContractReady;
    plan.applicationReady = plan.applyToMlvOwned
                         && plan.processingObjectMutationOwned
                         && plan.cacheInvalidationOwned
                         && plan.cutStretchStateOwned
                         && plan.lookAssistApplicationOwned
                         && plan.receiptValidationOwned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered receipt application contract unavailable");
    }
    return plan;
}

BatchRenderedVideoFfmpegFramePlan
batchRenderedVideoFfmpegFramePlanFromMetadata(
    const BatchRenderedVideoSourceMetadata & metadata,
    const BatchRenderedVideoRenderSettings & settings,
    BatchRenderedVideoEncoderProfile encoderProfile)
{
    if( !metadata.ready )
    {
        BatchRenderedVideoFfmpegFramePlan plan;
        plan.sourceWidth = metadata.width;
        plan.sourceHeight = metadata.height;
        plan.frameRate = metadata.frameRate;
        plan.resizeEnabled = settings.resizeEnabled;
        plan.resizeHeightLocked = settings.resizeHeightLocked;
        plan.reason = metadata.reason.isEmpty()
            ? QStringLiteral("rendered source metadata unavailable")
            : metadata.reason;
        return plan;
    }

    if( !settings.ready )
    {
        BatchRenderedVideoFfmpegFramePlan plan;
        plan.sourceWidth = metadata.width;
        plan.sourceHeight = metadata.height;
        plan.frameRate = metadata.frameRate;
        plan.resizeEnabled = settings.resizeEnabled;
        plan.resizeHeightLocked = settings.resizeHeightLocked;
        plan.reason = settings.reason.isEmpty()
            ? QStringLiteral("rendered render settings unavailable")
            : settings.reason;
        return plan;
    }

    return batchRenderedVideoFfmpegFramePlanFromGuiState(
        metadata.width,
        metadata.height,
        metadata.frameRate,
        metadata.stretchFactorX,
        metadata.stretchFactorY,
        settings.resizeEnabled,
        settings.resizeWidth,
        settings.resizeHeight,
        settings.resizeHeightLocked,
        encoderProfile);
}

BatchRenderedVideoRenderSettings
batchRenderedVideoRenderSettingsFromExplicitResize(
    bool resizeEnabled,
    int resizeWidth,
    int resizeHeight,
    bool resizeHeightLocked)
{
    BatchRenderedVideoRenderSettings settings;
    settings.source = QStringLiteral("explicit-headless");
    settings.resizeEnabled = resizeEnabled;
    settings.resizeWidth = resizeWidth;
    settings.resizeHeight = resizeHeight;
    settings.resizeHeightLocked = resizeHeightLocked;
    settings.explicitHeadlessSettings = true;
    settings.guiSettingsOwned = false;
    if( resizeEnabled
     && (resizeWidth <= 0 || (!resizeHeightLocked && resizeHeight <= 0)) )
    {
        settings.ready = false;
        settings.reason = QStringLiteral("rendered resize dimensions invalid");
    }
    return settings;
}

BatchRenderedVideoRenderSettings batchRenderedVideoDefaultRenderSettings()
{
    return BatchRenderedVideoRenderSettings();
}

BatchRenderedVideoSourceMetadata batchRenderedVideoSourceMetadata(
    int width,
    int height,
    double frameRate,
    double stretchFactorX,
    double stretchFactorY,
    int frameCount)
{
    BatchRenderedVideoSourceMetadata metadata;
    metadata.width = width;
    metadata.height = height;
    metadata.frameRate = frameRate;
    metadata.frameCount = frameCount > 0 ? frameCount : 0;
    metadata.stretchFactorX = stretchFactorX;
    metadata.stretchFactorY = stretchFactorY;

    if( width <= 0 || height <= 0 )
    {
        metadata.reason = QStringLiteral("rendered source dimensions invalid");
        return metadata;
    }
    if( !(frameRate > 0.0) )
    {
        metadata.reason = QStringLiteral("rendered frame rate invalid");
        return metadata;
    }
    if( !(stretchFactorX > 0.0) || !(stretchFactorY > 0.0) )
    {
        metadata.reason = QStringLiteral("rendered stretch factors invalid");
        return metadata;
    }

    metadata.frameCountReady = metadata.frameCount > 0;
    if( metadata.frameCountReady )
        metadata.durationSeconds =
            static_cast<double>(metadata.frameCount) / frameRate;
    metadata.ready = true;
    return metadata;
}

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
{
    BatchRenderedVideoFfmpegFramePlan plan;
    plan.sourceWidth = sourceWidth;
    plan.sourceHeight = sourceHeight;
    plan.frameRate = frameRate;
    plan.resizeEnabled = resizeEnabled;
    plan.resizeHeightLocked = resizeHeightLocked;

    if( sourceWidth <= 0 || sourceHeight <= 0 )
    {
        plan.reason = QStringLiteral("rendered source dimensions invalid");
        return plan;
    }
    if( !(frameRate > 0.0) )
    {
        plan.reason = QStringLiteral("rendered frame rate invalid");
        return plan;
    }
    if( !(stretchFactorX > 0.0) || !(stretchFactorY > 0.0) )
    {
        plan.reason = QStringLiteral("rendered stretch factors invalid");
        return plan;
    }

    int width = sourceWidth;
    int height = sourceHeight;

    if( resizeEnabled )
    {
        if( resizeWidth <= 0 || (!resizeHeightLocked && resizeHeight <= 0) )
        {
            plan.reason = QStringLiteral("rendered resize dimensions invalid");
            return plan;
        }

        if( resizeHeightLocked )
        {
            height = static_cast<int>(
                static_cast<double>(resizeWidth)
                / static_cast<double>(sourceWidth)
                / stretchFactorX
                * stretchFactorY
                * static_cast<double>(sourceHeight)
                + 0.5);
        }
        else
        {
            height = resizeHeight;
        }
        width = resizeWidth;
        plan.scaled = true;
    }
    else if( stretchFactorX != STRETCH_H_100
          || stretchFactorY != STRETCH_V_100 )
    {
        const RawAspectRenderedDimensions dimensions =
            rawAspectRenderedDimensions(sourceWidth,
                                        sourceHeight,
                                        stretchFactorX,
                                        stretchFactorY,
                                        std::numeric_limits<int>::max());
        if( !dimensions.valid )
        {
            plan.reason = QStringLiteral("rendered stretch dimensions invalid or too large");
            return plan;
        }
        width = dimensions.width;
        height = dimensions.height;
        plan.stretchApplied = true;
        plan.scaled = true;
    }

    if( batchRenderedVideoEncoderProfileRequiresEvenDimensions(encoderProfile) )
    {
        if( (width % 2) != 0 )
        {
            if( width == std::numeric_limits<int>::max() )
            {
                plan.reason = QStringLiteral("rendered output dimensions too large to align safely");
                return plan;
            }
            width += width % 2;
            plan.codecDimensionAdjusted = true;
            plan.scaled = true;
        }
        if( (height % 2) != 0 )
        {
            if( height == std::numeric_limits<int>::max() )
            {
                plan.reason = QStringLiteral("rendered output dimensions too large to align safely");
                return plan;
            }
            height += height % 2;
            plan.codecDimensionAdjusted = true;
            plan.scaled = true;
        }
    }

    if( width <= 0 || height <= 0 )
    {
        plan.reason = QStringLiteral("rendered output dimensions invalid");
        return plan;
    }

    plan.outputWidth = width;
    plan.outputHeight = height;
    plan.frameRateArgument =
        batchRenderedVideoFfmpegFrameRateArgument(frameRate);
    plan.frameSizeArgument =
        QStringLiteral("%1x%2").arg(width).arg(height);
    plan.ready = !plan.frameRateArgument.isEmpty()
              && !plan.frameSizeArgument.isEmpty();
    if( !plan.ready )
        plan.reason = QStringLiteral("rendered ffmpeg frame plan unavailable");
    return plan;
}

QString batchRenderedVideoFfmpegFrameRateArgument(double frameRate)
{
    QLocale locale = QLocale(QLocale::English, QLocale::UnitedKingdom);
    locale.setNumberOptions(QLocale::OmitGroupSeparator);
    return locale.toString(frameRate);
}

bool batchRenderedVideoEncoderProfileRequiresEvenDimensions(
    BatchRenderedVideoEncoderProfile profile)
{
    return profile == BatchRenderedVideoEncoderProfile::H264
        || profile == BatchRenderedVideoEncoderProfile::H265_8;
}

BatchRenderedVideoFfmpegAudioPlan
batchRenderedVideoFfmpegAudioPlanForCurrentBuild()
{
    const BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild();
    return batchRenderedVideoFfmpegAudioPlanForCurrentBuild(sourceAudioPlan);
}

BatchRenderedVideoFfmpegAudioPlan
batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan)
{
    return batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
        sourceAudioPlan,
        batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
            sourceAudioPlan));
}

BatchRenderedVideoFfmpegAudioPlan
batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoAudioMuxPrerequisitesPlan & audioMuxPlan)
{
    const BatchRenderedVideoSourceAudioExtractionPlan extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan);
    const BatchRenderedVideoSourceAudioExtractionExecutionPlan executionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    const BatchRenderedVideoFfmpegAudioInputHandoffPlan handoffPlan =
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            executionPlan);
    return batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
        sourceAudioPlan,
        audioMuxPlan,
        batchRenderedVideoAudioMuxExecutionPlanFromContracts(
            sourceAudioPlan,
            handoffPlan,
            audioMuxPlan));
}

BatchRenderedVideoFfmpegAudioPlan
batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoAudioMuxPrerequisitesPlan & audioMuxPlan,
    const BatchRenderedVideoAudioMuxExecutionPlan & audioMuxExecutionPlan)
{
    BatchRenderedVideoFfmpegAudioPlan plan;
    plan.sourceAudioDiscoveryOwned =
        audioMuxExecutionPlan.sourceAudioDiscoveryOwned;
    plan.sourceAudioExtractionOwned =
        audioMuxExecutionPlan.sourceAudioExtractionOwned;
    plan.audioInputContractReady = audioMuxPlan.sourceAudioInputContractReady;
    plan.audioInputOwned = audioMuxPlan.audioInputOwned;
    plan.audioMuxExecutionContractReady =
        audioMuxExecutionPlan.contractReady;
    plan.audioMuxPlanned = audioMuxExecutionPlan.audioMuxPlanned;
    plan.audioMuxArgumentHandoffPlanned =
        audioMuxExecutionPlan.audioMuxArgumentHandoffPlanned;
    plan.audioSyncValidationPlanned =
        audioMuxExecutionPlan.audioSyncValidationPlanned;
    plan.audioMuxOwned = audioMuxExecutionPlan.audioMuxOwned;
    plan.audioMuxArgumentHandoffOwned =
        audioMuxExecutionPlan.audioMuxArgumentHandoffOwned;
    plan.audioSyncOwned = audioMuxExecutionPlan.audioSyncValidationOwned;
    plan.audioMuxExecutionReady = audioMuxExecutionPlan.muxExecutionReady;
    plan.muxedAudioCommandPlanned = plan.audioMuxPlanned
                                  && plan.audioMuxArgumentHandoffPlanned
                                  && plan.audioSyncValidationPlanned;
    if( plan.muxedAudioCommandPlanned )
    {
        plan.muxTransitionArguments =
            QStringLiteral("deferred-until-audio-mux-execution-owned");
    }
    plan.muxedAudioCommandReady = plan.muxedAudioCommandPlanned
                               && plan.audioMuxExecutionReady
                               && plan.audioInputOwned
                               && plan.audioMuxOwned
                               && plan.audioMuxArgumentHandoffOwned
                               && plan.audioSyncOwned;
    plan.videoOnlyCommandReady = !plan.audioArguments.isEmpty()
                              && audioMuxPlan.videoOnlyFallbackReady
                              && audioMuxExecutionPlan.videoOnlyFallbackReady;

    if( !sourceAudioPlan.contractReady )
    {
        plan.reason = sourceAudioPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio contract unavailable")
            : sourceAudioPlan.reason;
        return plan;
    }
    if( !audioMuxPlan.contractReady )
    {
        plan.reason = audioMuxPlan.reason.isEmpty()
            ? QStringLiteral("rendered audio mux prerequisite contract unavailable")
            : audioMuxPlan.reason;
        return plan;
    }
    if( !audioMuxExecutionPlan.contractReady )
    {
        plan.reason = audioMuxExecutionPlan.reason.isEmpty()
            ? QStringLiteral("rendered audio mux execution contract unavailable")
            : audioMuxExecutionPlan.reason;
        return plan;
    }

    plan.contractReady = plan.videoOnlyCommandReady
                      && audioMuxPlan.contractReady
                      && audioMuxExecutionPlan.contractReady
                      && (!plan.audioMuxPlanned
                       || plan.muxedAudioCommandPlanned);
    if( !plan.contractReady )
        plan.reason = QStringLiteral("rendered ffmpeg audio contract unavailable");
    return plan;
}

BatchRenderedVideoAudioMuxExecutionPlan
batchRenderedVideoAudioMuxExecutionPlanFromContracts(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoFfmpegAudioInputHandoffPlan & handoffPlan,
    const BatchRenderedVideoAudioMuxPrerequisitesPlan & prerequisitesPlan)
{
    BatchRenderedVideoAudioMuxExecutionPlan plan;
    plan.sourceAudioContractReady = sourceAudioPlan.contractReady;
    plan.audioInputHandoffContractReady = handoffPlan.contractReady;
    plan.audioMuxPrerequisitesContractReady = prerequisitesPlan.contractReady;
    plan.sourceAudioKnown = sourceAudioPlan.sourceAudioKnown;
    plan.sourceAudioPresent = sourceAudioPlan.sourceAudioPresent;
    plan.sourceAudioDiscoveryOwned =
        prerequisitesPlan.sourceAudioDiscoveryOwned;
    plan.sourceAudioExtractionOwned =
        prerequisitesPlan.sourceAudioExtractionOwned;
    plan.audioInputHandoffReady = handoffPlan.audioInputHandoffReady;
    plan.tempAudioInputOwned = handoffPlan.audioInputOwned
                            && handoffPlan.audioInputArgumentHandoffOwned;
    plan.videoOnlyFallbackReady = prerequisitesPlan.videoOnlyFallbackReady;
    plan.audioMuxPlanned = plan.sourceAudioKnown && plan.sourceAudioPresent;
    plan.audioMuxArgumentHandoffPlanned =
        plan.audioMuxPlanned && handoffPlan.inputArgumentsPlanned;
    plan.audioSyncValidationPlanned = plan.audioMuxPlanned;

    if( !sourceAudioPlan.contractReady )
    {
        plan.reason = sourceAudioPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio contract unavailable")
            : sourceAudioPlan.reason;
        return plan;
    }
    if( !handoffPlan.contractReady )
    {
        plan.reason = handoffPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg audio input handoff contract unavailable")
            : handoffPlan.reason;
        return plan;
    }
    if( !prerequisitesPlan.contractReady )
    {
        plan.reason = prerequisitesPlan.reason.isEmpty()
            ? QStringLiteral("rendered audio mux prerequisite contract unavailable")
            : prerequisitesPlan.reason;
        return plan;
    }

    plan.audioMuxReady = plan.audioInputHandoffReady
                      && plan.tempAudioInputOwned
                      && plan.audioMuxOwned
                      && plan.audioMuxArgumentHandoffOwned;
    plan.audioSyncValidationReady = plan.audioMuxReady
                                 && plan.audioSyncValidationOwned;
    plan.muxExecutionReady = plan.audioMuxReady
                           && plan.audioSyncValidationReady;
    plan.contractReady = plan.sourceAudioContractReady
                      && plan.audioInputHandoffContractReady
                      && plan.audioMuxPrerequisitesContractReady
                      && plan.videoOnlyFallbackReady
                      && (!plan.audioMuxPlanned
                       || (plan.audioMuxArgumentHandoffPlanned
                        && plan.audioSyncValidationPlanned));
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered audio mux execution contract unavailable");
    }
    return plan;
}

BatchRenderedVideoAudioMuxPrerequisitesPlan
batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan)
{
    return batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
        sourceAudioPlan,
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan));
}

BatchRenderedVideoAudioMuxPrerequisitesPlan
batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan)
{
    return batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
        sourceAudioPlan,
        extractionPlan,
        batchRenderedVideoFfmpegAudioInputPlanFromExtraction(
            extractionPlan));
}

BatchRenderedVideoAudioMuxPrerequisitesPlan
batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan,
    const BatchRenderedVideoFfmpegAudioInputPlan & audioInputPlan)
{
    BatchRenderedVideoAudioMuxPrerequisitesPlan plan;
    plan.sourceAudioContractReady = sourceAudioPlan.contractReady;
    plan.sourceAudioExtractionContractReady = extractionPlan.contractReady;
    plan.sourceAudioInputContractReady = audioInputPlan.contractReady;
    plan.sourceAudioDiscoveryOwned = sourceAudioPlan.discoveryOwned;
    plan.sourceAudioExtractionOwned = extractionPlan.extractionProcessOwned;
    plan.audioInputOwned = audioInputPlan.audioInputOwned;
    plan.audioSyncValidationOwned = sourceAudioPlan.syncValidationOwned;
    plan.videoOnlyFallbackReady = sourceAudioPlan.videoOnlyFallbackReady;

    if( !sourceAudioPlan.contractReady )
    {
        plan.reason = sourceAudioPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio contract unavailable")
            : sourceAudioPlan.reason;
        return plan;
    }
    if( !extractionPlan.contractReady )
    {
        plan.reason = extractionPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio extraction prerequisite contract unavailable")
            : extractionPlan.reason;
        return plan;
    }
    if( !audioInputPlan.contractReady )
    {
        plan.reason = audioInputPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg audio input contract unavailable")
            : audioInputPlan.reason;
        return plan;
    }

    plan.contractReady = plan.sourceAudioContractReady
                      && plan.sourceAudioExtractionContractReady
                      && plan.sourceAudioInputContractReady
                      && plan.videoOnlyFallbackReady;
    plan.muxReady = plan.sourceAudioDiscoveryOwned
                 && plan.sourceAudioExtractionOwned
                 && plan.audioInputOwned
                 && plan.audioMuxOwned
                 && plan.audioSyncValidationOwned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered audio mux prerequisite contract unavailable");
    }
    return plan;
}

BatchRenderedVideoFfmpegAudioInputPlan
batchRenderedVideoFfmpegAudioInputPlanFromExtraction(
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan)
{
    return batchRenderedVideoFfmpegAudioInputPlanFromExtraction(
        extractionPlan,
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan));
}

BatchRenderedVideoFfmpegAudioInputPlan
batchRenderedVideoFfmpegAudioInputPlanFromExtraction(
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan,
    const BatchRenderedVideoSourceAudioExtractionExecutionPlan & executionPlan)
{
    return batchRenderedVideoFfmpegAudioInputPlanFromHandoff(
        extractionPlan,
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            executionPlan));
}

BatchRenderedVideoFfmpegAudioInputPlan
batchRenderedVideoFfmpegAudioInputPlanFromHandoff(
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan,
    const BatchRenderedVideoFfmpegAudioInputHandoffPlan & handoffPlan)
{
    BatchRenderedVideoFfmpegAudioInputPlan plan;
    plan.plannedAudioPath = handoffPlan.plannedAudioPath;
    plan.plannedInputArguments = handoffPlan.plannedInputArguments;
    plan.activeAudioArguments = handoffPlan.activeAudioArguments;
    plan.sourceAudioExtractionContractReady = extractionPlan.contractReady;
    plan.sourceAudioKnown = handoffPlan.sourceAudioKnown;
    plan.sourceAudioPresent = handoffPlan.sourceAudioPresent;
    plan.extractionPathPlanned = handoffPlan.extractionPathPlanned;
    plan.extractionPathReady = handoffPlan.extractionPathReady;
    plan.extractionReady = handoffPlan.extractionExecutionReady;
    plan.tempFileOwned = handoffPlan.tempFileLifecycleReady;
    plan.cleanupOwned = handoffPlan.cleanupOwned;
    plan.videoOnlyFallbackReady = handoffPlan.videoOnlyFallbackReady;
    plan.audioInputPlanned = handoffPlan.audioInputPlanned;
    plan.audioInputOwned = handoffPlan.audioInputOwned
                        && handoffPlan.audioInputArgumentHandoffOwned;
    plan.audioInputReady = handoffPlan.audioInputHandoffReady;

    if( !extractionPlan.contractReady )
    {
        plan.reason = extractionPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio extraction prerequisite contract unavailable")
            : extractionPlan.reason;
        return plan;
    }
    if( !handoffPlan.contractReady )
    {
        plan.reason = handoffPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg audio input handoff contract unavailable")
            : handoffPlan.reason;
        return plan;
    }

    plan.contractReady = plan.sourceAudioExtractionContractReady
                      && handoffPlan.contractReady
                      && plan.videoOnlyFallbackReady
                      && (!plan.audioInputPlanned
                       || !plan.plannedInputArguments.isEmpty());
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered ffmpeg audio input contract unavailable");
    }
    return plan;
}

BatchRenderedVideoFfmpegAudioInputHandoffPlan
batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
    const BatchRenderedVideoSourceAudioExtractionExecutionPlan & executionPlan)
{
    BatchRenderedVideoFfmpegAudioInputHandoffPlan plan;
    plan.plannedAudioPath = executionPlan.plannedAudioPath;
    plan.extractionExecutionContractReady = executionPlan.contractReady;
    plan.sourceAudioKnown = executionPlan.sourceAudioKnown;
    plan.sourceAudioPresent = executionPlan.sourceAudioPresent;
    plan.extractionPathPlanned = executionPlan.extractionPathPlanned;
    plan.extractionPathReady = executionPlan.extractionPathReady;
    plan.extractionExecutionReady = executionPlan.extractionExecutionReady;
    plan.tempFileLifecycleReady = executionPlan.tempFileLifecycleReady;
    plan.cleanupOwned = executionPlan.cleanupOwned;
    plan.videoOnlyFallbackReady = executionPlan.videoOnlyFallbackReady;
    plan.audioInputPlanned = plan.sourceAudioKnown
                          && plan.sourceAudioPresent;
    if( plan.audioInputPlanned && !plan.plannedAudioPath.isEmpty() )
    {
        plan.plannedInputArguments =
            QStringLiteral("-i \"%1\"").arg(plan.plannedAudioPath);
    }
    plan.inputArgumentsPlanned = plan.audioInputPlanned
                              && !plan.plannedInputArguments.isEmpty();
    plan.audioInputOwnershipPlanned = plan.inputArgumentsPlanned;
    plan.audioInputArgumentHandoffPlanned = plan.inputArgumentsPlanned;

    if( !executionPlan.contractReady )
    {
        plan.reason = executionPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio extraction execution contract unavailable")
            : executionPlan.reason;
        return plan;
    }

    plan.contractReady = plan.extractionExecutionContractReady
                      && plan.videoOnlyFallbackReady
                      && (!plan.audioInputPlanned
                       || (plan.inputArgumentsPlanned
                        && plan.audioInputOwnershipPlanned
                        && plan.audioInputArgumentHandoffPlanned));
    plan.audioInputHandoffReady = plan.extractionOutputOwned
                               && plan.audioInputOwned
                               && plan.audioInputArgumentHandoffOwned
                               && plan.extractionExecutionReady
                               && plan.tempFileLifecycleReady;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered ffmpeg audio input handoff contract unavailable");
    }
    return plan;
}

BatchRenderedVideoSourceAudioExtractionExecutionPlan
batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
    const BatchRenderedVideoSourceAudioExtractionPlan & extractionPlan)
{
    BatchRenderedVideoSourceAudioExtractionExecutionPlan plan;
    plan.plannedAudioPath = extractionPlan.plannedAudioPath;
    plan.extractionFormat = extractionPlan.extractionFormat;
    plan.extractionPrerequisiteContractReady = extractionPlan.contractReady;
    plan.sourceAudioKnown = extractionPlan.sourceAudioKnown;
    plan.sourceAudioPresent = extractionPlan.sourceAudioPresent;
    plan.extractionPathPlanned = extractionPlan.extractionPathPlanned;
    plan.extractionPathReady = extractionPlan.extractionPathReady;
    plan.videoOnlyFallbackReady = extractionPlan.videoOnlyFallbackReady;
    plan.sampleReadPlanned = plan.sourceAudioKnown
                          && plan.sourceAudioPresent;
    plan.wavWritePlanned = plan.sampleReadPlanned;
    plan.tempFileLifecyclePlanned = plan.sampleReadPlanned
                                 && plan.extractionPathReady;

    if( !extractionPlan.contractReady )
    {
        plan.reason = extractionPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio extraction prerequisite contract unavailable")
            : extractionPlan.reason;
        return plan;
    }

    plan.contractReady = plan.extractionPrerequisiteContractReady
                      && plan.videoOnlyFallbackReady
                      && (!plan.sampleReadPlanned
                       || (plan.wavWritePlanned
                        && plan.tempFileLifecyclePlanned));
    plan.tempFileLifecycleReady = plan.tempFileOpenOwned
                               && plan.tempFileFinalizeOwned
                               && plan.cleanupOwned;
    plan.extractionExecutionReady = plan.extractionProcessOwned
                                 && plan.sampleReadOwned
                                 && plan.wavHeaderWriteOwned
                                 && plan.wavSampleWriteOwned
                                 && plan.tempFileLifecycleReady;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered source audio extraction execution contract unavailable");
    }
    return plan;
}

BatchRenderedVideoSourceAudioExtractionPlan
batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan)
{
    return batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
        sourceAudioPlan,
        QString());
}

BatchRenderedVideoSourceAudioExtractionPlan
batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
    const BatchRenderedVideoSourceAudioPlan & sourceAudioPlan,
    const QString & plannedAudioPath)
{
    BatchRenderedVideoSourceAudioExtractionPlan plan;
    plan.clipPath = sourceAudioPlan.clipPath;
    plan.plannedAudioPath = QDir::cleanPath(plannedAudioPath.trimmed());
    plan.sourceAudioContractReady = sourceAudioPlan.contractReady;
    plan.sourceAudioKnown = sourceAudioPlan.sourceAudioKnown;
    plan.sourceAudioPresent = sourceAudioPlan.sourceAudioPresent;
    plan.sourceAudioDiscoveryOwned = sourceAudioPlan.discoveryOwned;
    plan.videoOnlyFallbackReady = sourceAudioPlan.videoOnlyFallbackReady;
    plan.extractionPathPlanned =
        plan.sourceAudioKnown && plan.sourceAudioPresent;
    plan.extractionPathReady =
        !plan.extractionPathPlanned || !plan.plannedAudioPath.isEmpty();
    plan.sampleFormatReady =
        !plan.sourceAudioPresent || sourceAudioPlan.bitsPerSample > 0;
    plan.sampleRateReady =
        !plan.sourceAudioPresent || sourceAudioPlan.sampleRate > 0;
    plan.channelLayoutReady =
        !plan.sourceAudioPresent || sourceAudioPlan.channels > 0;

    if( !sourceAudioPlan.contractReady )
    {
        plan.reason = sourceAudioPlan.reason.isEmpty()
            ? QStringLiteral("rendered source audio contract unavailable")
            : sourceAudioPlan.reason;
        return plan;
    }

    plan.contractReady = plan.sourceAudioContractReady
                      && plan.videoOnlyFallbackReady
                      && plan.extractionPathReady
                      && plan.sampleFormatReady
                      && plan.sampleRateReady
                      && plan.channelLayoutReady;
    plan.extractionReady = plan.extractionProcessOwned
                        && plan.tempFileOwned
                        && plan.cleanupOwned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered source audio extraction prerequisite contract unavailable");
    }
    return plan;
}

QString batchRenderedVideoSourceAudioExtractionPathFromOutputPath(
    const QString & outputPath)
{
    const QString cleaned = QDir::cleanPath(outputPath.trimmed());
    if( cleaned.isEmpty() ) return QString();

    const QFileInfo outputInfo(cleaned);
    const QString baseName = outputInfo.completeBaseName().isEmpty()
        ? outputInfo.fileName()
        : outputInfo.completeBaseName();
    if( baseName.isEmpty() ) return QString();

    return QDir::cleanPath(
        outputInfo.dir().filePath(baseName + QStringLiteral(".source-audio.wav")));
}

BatchRenderedVideoSourceAudioPlan
batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
    const QString & clipPath,
    bool sourceAudioPresent,
    int channels,
    int sampleRate,
    int bitsPerSample,
    qulonglong audioBytes)
{
    BatchRenderedVideoSourceAudioPlan plan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild(clipPath);
    plan.source = QStringLiteral("open-mlv-audio-metadata");
    plan.audioState = sourceAudioPresent
        ? QStringLiteral("present")
        : QStringLiteral("absent");
    plan.discoveryOwned = true;
    plan.discoveryAttempted = true;
    plan.sourceAudioKnown = true;
    plan.sourceAudioPresent = sourceAudioPresent;
    plan.channels = sourceAudioPresent ? channels : 0;
    plan.sampleRate = sourceAudioPresent ? sampleRate : 0;
    plan.bitsPerSample = sourceAudioPresent ? bitsPerSample : 0;
    plan.audioBytes = sourceAudioPresent ? audioBytes : 0;
    plan.videoOnlyFallbackReady = true;

    if( sourceAudioPresent
     && (plan.channels <= 0
      || plan.sampleRate <= 0
      || plan.bitsPerSample <= 0) )
    {
        plan.reason = QStringLiteral("rendered source audio metadata invalid");
        plan.contractReady = false;
        return plan;
    }

    plan.contractReady = plan.sourceAudioKnown
                      && plan.videoOnlyFallbackReady;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered source audio discovery contract unavailable");
    }
    return plan;
}

BatchRenderedVideoSourceAudioPlan
batchRenderedVideoSourceAudioPlanForCurrentBuild(
    const QString & clipPath)
{
    BatchRenderedVideoSourceAudioPlan plan;
    const QString normalizedPath =
        QDir::fromNativeSeparators(clipPath.trimmed());
    if( !normalizedPath.isEmpty() )
        plan.clipPath = QDir::cleanPath(normalizedPath);
    plan.videoOnlyFallbackReady = true;
    plan.contractReady = plan.videoOnlyFallbackReady;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered source audio contract unavailable");
    }
    return plan;
}

BatchRenderedVideoOptionalFilterPlan
batchRenderedVideoOptionalFilterPlanFromFilterPlan(
    const BatchRenderedVideoFfmpegFilterPlan & filterPlan)
{
    BatchRenderedVideoOptionalFilterPlan plan;
    plan.baseFilterArguments = filterPlan.filterArguments;
    plan.baseFilterContractReady = filterPlan.ready;
    plan.optionalFilterGraphOwned = filterPlan.optionalFiltersOwned;
    plan.moireeFilterOwned = filterPlan.moireeFilterOwned;
    plan.hdrBlendOwned = filterPlan.hdrBlendOwned;
    plan.stabilizationOwned = filterPlan.stabilizationOwned;

    if( !filterPlan.ready )
    {
        plan.reason = filterPlan.reason.isEmpty()
            ? QStringLiteral("rendered ffmpeg filter plan unavailable")
            : filterPlan.reason;
        return plan;
    }

    plan.contractReady = plan.baseFilterContractReady
                      && !plan.baseFilterArguments.isEmpty();
    plan.optionalFilterExecutionReady =
        plan.optionalFiltersRequested
     && plan.optionalFilterGraphOwned
     && plan.moireeFilterOwned
     && plan.hdrBlendOwned
     && plan.stabilizationOwned
     && plan.optionalFilterOrderOwned
     && plan.optionalFilterParityOwned;
    if( !plan.contractReady )
    {
        plan.reason =
            QStringLiteral("rendered optional-filter contract unavailable");
    }
    return plan;
}

BatchRenderedVideoFfmpegFilterPlan
batchRenderedVideoFfmpegFilterPlanForCurrentBuild()
{
    BatchRenderedVideoFfmpegFilterPlan plan;
    plan.colorScaleFilter =
        QStringLiteral("scale=in_color_matrix=bt601:out_color_matrix=bt709");
    plan.filterArguments = QStringLiteral("-vf %1").arg(plan.colorScaleFilter);
    plan.baseColorScaleReady = !plan.colorScaleFilter.isEmpty();
    plan.ready = plan.baseColorScaleReady && !plan.filterArguments.isEmpty();
    if( !plan.ready )
        plan.reason = QStringLiteral("rendered ffmpeg filter plan unavailable");
    return plan;
}

BatchRenderedVideoFfmpegVideoPlan
batchRenderedVideoFfmpegVideoPlanFromRequest(
    const BatchExportFormatRequest & request)
{
    return batchRenderedVideoFfmpegVideoPlanFromEncoderPreset(
        batchRenderedVideoEncoderPresetFromRequest(request));
}

BatchRenderedVideoFfmpegVideoPlan
batchRenderedVideoFfmpegVideoPlanFromEncoderPreset(
    const BatchRenderedVideoEncoderPreset & preset)
{
    BatchRenderedVideoFfmpegVideoPlan plan;
    if( !preset.ready )
    {
        plan.reason = QStringLiteral("rendered encoder preset incomplete");
        return plan;
    }

    switch( preset.profile )
    {
        case BatchRenderedVideoEncoderProfile::H264:
            plan.encoder = QStringLiteral("libx264");
            plan.preset = QStringLiteral("medium");
            plan.qualityFlag = QStringLiteral("-crf");
            plan.qualityValue = 14;
            plan.pixelFormat = QStringLiteral("yuv420p");
            break;
        case BatchRenderedVideoEncoderProfile::H265_8:
            plan.encoder = QStringLiteral("libx265");
            plan.preset = QStringLiteral("medium");
            plan.qualityFlag = QStringLiteral("-crf");
            plan.qualityValue = 18;
            plan.pixelFormat = QStringLiteral("yuv420p");
            plan.videoTag = QStringLiteral("hvc1");
            break;
        case BatchRenderedVideoEncoderProfile::ProRes422HQ:
            plan.encoder = QStringLiteral("prores_ks");
            plan.qualityFlag = QStringLiteral("-profile:v");
            plan.qualityValue = CODEC_PRORES422HQ;
            plan.pixelFormat = QStringLiteral("yuv422p10");
            break;
        case BatchRenderedVideoEncoderProfile::Unspecified:
            break;
    }

    plan.ready = !plan.encoder.isEmpty()
              && !plan.qualityFlag.isEmpty()
              && plan.qualityValue >= 0
              && !plan.pixelFormat.isEmpty();
    if( !plan.ready )
    {
        plan.reason = QStringLiteral("rendered ffmpeg video plan unavailable");
        return plan;
    }

    plan.videoArguments = QStringLiteral("-c:v %1 ").arg(plan.encoder);
    if( !plan.preset.isEmpty() )
        plan.videoArguments += QStringLiteral("-preset %1 ").arg(plan.preset);
    plan.videoArguments += QStringLiteral("%1 %2 ").arg(plan.qualityFlag).arg(plan.qualityValue);
    if( !plan.videoTag.isEmpty() )
        plan.videoArguments += QStringLiteral("-tag:v %1 ").arg(plan.videoTag);
    plan.videoArguments += QStringLiteral("-pix_fmt %1").arg(plan.pixelFormat);
    return plan;
}

BatchRenderedVideoEncoderPreset batchRenderedVideoEncoderPresetFromRequest(
    const BatchExportFormatRequest & request)
{
    return batchRenderedVideoEncoderPresetFromTarget(
        batchRenderedVideoTargetFromRequest(request));
}

BatchRenderedVideoEncoderPreset batchRenderedVideoEncoderPresetFromTarget(
    const BatchRenderedVideoTarget & target)
{
    BatchRenderedVideoEncoderPreset preset;
    if( !target.complete )
        return preset;

    preset.extension = target.extension;
    switch( target.codec )
    {
        case BatchRenderedVideoCodec::H264:
            preset.profile = BatchRenderedVideoEncoderProfile::H264;
            preset.guiCodecProfile = CODEC_H264;
            switch( target.container )
            {
                case BatchRenderedVideoContainer::Mov:
                    preset.option = BatchRenderedVideoEncoderOption::H264HighMov;
                    preset.guiCodecOption = CODEC_H264_H_MOV;
                    break;
                case BatchRenderedVideoContainer::Mp4:
                    preset.option = BatchRenderedVideoEncoderOption::H264HighMp4;
                    preset.guiCodecOption = CODEC_H264_H_MP4;
                    break;
                case BatchRenderedVideoContainer::Mkv:
                    preset.option = BatchRenderedVideoEncoderOption::H264HighMkv;
                    preset.guiCodecOption = CODEC_H264_H_MKV;
                    break;
                case BatchRenderedVideoContainer::Unspecified:
                    break;
            }
            break;
        case BatchRenderedVideoCodec::H265:
            preset.profile = BatchRenderedVideoEncoderProfile::H265_8;
            preset.guiCodecProfile = CODEC_H265_8;
            switch( target.container )
            {
                case BatchRenderedVideoContainer::Mov:
                    preset.option = BatchRenderedVideoEncoderOption::H265HighMov;
                    preset.guiCodecOption = CODEC_H265_H_MOV;
                    break;
                case BatchRenderedVideoContainer::Mp4:
                    preset.option = BatchRenderedVideoEncoderOption::H265HighMp4;
                    preset.guiCodecOption = CODEC_H265_H_MP4;
                    break;
                case BatchRenderedVideoContainer::Mkv:
                    preset.option = BatchRenderedVideoEncoderOption::H265HighMkv;
                    preset.guiCodecOption = CODEC_H265_H_MKV;
                    break;
                case BatchRenderedVideoContainer::Unspecified:
                    break;
            }
            break;
        case BatchRenderedVideoCodec::ProRes:
            if( target.container == BatchRenderedVideoContainer::Mov )
            {
                preset.profile = BatchRenderedVideoEncoderProfile::ProRes422HQ;
                preset.option =
                    BatchRenderedVideoEncoderOption::ProResFfmpegKostya;
                preset.guiCodecProfile = CODEC_PRORES422HQ;
                preset.guiCodecOption = CODEC_PRORES_OPTION_KS;
            }
            break;
        case BatchRenderedVideoCodec::Unspecified:
            break;
    }

    preset.ready =
        preset.profile != BatchRenderedVideoEncoderProfile::Unspecified
     && preset.option != BatchRenderedVideoEncoderOption::Unspecified
     && preset.guiCodecProfile >= 0
     && preset.guiCodecOption >= 0
     && !preset.extension.isEmpty();
    if( !preset.ready )
    {
        preset.extension.clear();
        preset.guiCodecProfile = -1;
        preset.guiCodecOption = -1;
    }
    return preset;
}

BatchRenderedVideoTarget batchRenderedVideoTargetFromRequest(
    const BatchExportFormatRequest & request)
{
    BatchRenderedVideoTarget target;
    if( request.format != BatchExportFormat::RenderedVideo
     || !batchRenderedVideoRequestShapeValid(request) )
    {
        return target;
    }

    target.codec = request.renderedCodec;
    target.container = request.renderedContainer;

    if( target.codec == BatchRenderedVideoCodec::Unspecified )
    {
        switch( target.container )
        {
            case BatchRenderedVideoContainer::Mov:
                target.codec = BatchRenderedVideoCodec::ProRes;
                break;
            case BatchRenderedVideoContainer::Mp4:
            case BatchRenderedVideoContainer::Mkv:
                target.codec = BatchRenderedVideoCodec::H264;
                break;
            case BatchRenderedVideoContainer::Unspecified:
                break;
        }
    }

    if( target.container == BatchRenderedVideoContainer::Unspecified )
    {
        switch( target.codec )
        {
            case BatchRenderedVideoCodec::H264:
            case BatchRenderedVideoCodec::H265:
                target.container = BatchRenderedVideoContainer::Mp4;
                break;
            case BatchRenderedVideoCodec::ProRes:
                target.container = BatchRenderedVideoContainer::Mov;
                break;
            case BatchRenderedVideoCodec::Unspecified:
                break;
        }
    }

    target.extension =
        QString::fromLatin1(batchRenderedVideoContainerExtension(target.container));
    target.complete = target.codec != BatchRenderedVideoCodec::Unspecified
                   && target.container != BatchRenderedVideoContainer::Unspecified
                   && !target.extension.isEmpty();
    return target;
}

QString batchRenderedVideoRequestShapeError(const BatchExportFormatRequest & request)
{
    if( batchRenderedVideoRequestShapeValid(request) )
        return QString();

    if( request.renderedCodec == BatchRenderedVideoCodec::ProRes )
        return QStringLiteral("codec=prores requires container=mov or unspecified");

    return QStringLiteral("unsupported rendered-video codec/container combination");
}

bool batchRenderedVideoRequestShapeValid(const BatchExportFormatRequest & request)
{
    if( request.format != BatchExportFormat::RenderedVideo )
        return true;

    if( request.renderedCodec == BatchRenderedVideoCodec::ProRes
     && request.renderedContainer != BatchRenderedVideoContainer::Unspecified
     && request.renderedContainer != BatchRenderedVideoContainer::Mov )
    {
        return false;
    }

    return true;
}

QString batchExportFormatRequestSummary(const BatchExportFormatRequest & request)
{
    return QStringLiteral("request=%1 codec=%2 container=%3")
        .arg(batchExportFormatName(request.format))
        .arg(batchRenderedVideoCodecName(request.renderedCodec))
        .arg(batchRenderedVideoContainerName(request.renderedContainer));
}

BatchExportFormat batchExportFormatFromString(const QString & value)
{
    return batchExportFormatRequestFromString(value).format;
}

BatchExportFormatRequest batchExportFormatRequestFromString(const QString & value)
{
    BatchExportFormatRequest request;
    const QString normalized = value.trimmed().toLower();
    if( normalized.isEmpty()
     || normalized == QStringLiteral("cdng")
     || normalized == QStringLiteral("dng")
     || normalized == QStringLiteral("cinemadng")
     || normalized == QStringLiteral("cinema-dng")
     || normalized == QStringLiteral("cinema_dng") )
    {
        request.format = BatchExportFormat::Cdng;
        return request;
    }
    if( normalized == QStringLiteral("rendered")
     || normalized == QStringLiteral("rendered-video")
     || normalized == QStringLiteral("rendered_video")
     || normalized == QStringLiteral("video") )
    {
        request.format = BatchExportFormat::RenderedVideo;
        return request;
    }
    if( normalized == QStringLiteral("mov") )
    {
        request.format = BatchExportFormat::RenderedVideo;
        request.renderedContainer = BatchRenderedVideoContainer::Mov;
        return request;
    }
    if( normalized == QStringLiteral("mp4") )
    {
        request.format = BatchExportFormat::RenderedVideo;
        request.renderedContainer = BatchRenderedVideoContainer::Mp4;
        return request;
    }
    if( normalized == QStringLiteral("mkv") )
    {
        request.format = BatchExportFormat::RenderedVideo;
        request.renderedContainer = BatchRenderedVideoContainer::Mkv;
        return request;
    }
    if( normalized == QStringLiteral("h264")
     || normalized == QStringLiteral("h264-mp4") )
    {
        request.format = BatchExportFormat::RenderedVideo;
        request.renderedCodec = BatchRenderedVideoCodec::H264;
        if( normalized.endsWith(QStringLiteral("-mp4")) )
            request.renderedContainer = BatchRenderedVideoContainer::Mp4;
        return request;
    }
    if( normalized == QStringLiteral("h265")
     || normalized == QStringLiteral("hevc")
     || normalized == QStringLiteral("h265-mp4")
     || normalized == QStringLiteral("hevc-mp4") )
    {
        request.format = BatchExportFormat::RenderedVideo;
        request.renderedCodec = BatchRenderedVideoCodec::H265;
        if( normalized.endsWith(QStringLiteral("-mp4")) )
            request.renderedContainer = BatchRenderedVideoContainer::Mp4;
        return request;
    }
    if( normalized == QStringLiteral("prores")
     || normalized == QStringLiteral("prores-mov") )
    {
        request.format = BatchExportFormat::RenderedVideo;
        request.renderedCodec = BatchRenderedVideoCodec::ProRes;
        request.renderedContainer = BatchRenderedVideoContainer::Mov;
        return request;
    }

    request.format = BatchExportFormat::Unknown;
    return request;
}

BatchRenderedVideoContainer batchRenderedVideoContainerFromString(const QString & value, bool * ok)
{
    const QString normalized = value.trimmed().toLower();
    if( normalized.isEmpty()
     || normalized == QStringLiteral("unspecified")
     || normalized == QStringLiteral("default")
     || normalized == QStringLiteral("auto") )
    {
        if( ok ) *ok = true;
        return BatchRenderedVideoContainer::Unspecified;
    }
    if( normalized == QStringLiteral("mov")
     || normalized == QStringLiteral("quicktime") )
    {
        if( ok ) *ok = true;
        return BatchRenderedVideoContainer::Mov;
    }
    if( normalized == QStringLiteral("mp4") )
    {
        if( ok ) *ok = true;
        return BatchRenderedVideoContainer::Mp4;
    }
    if( normalized == QStringLiteral("mkv")
     || normalized == QStringLiteral("matroska") )
    {
        if( ok ) *ok = true;
        return BatchRenderedVideoContainer::Mkv;
    }

    if( ok ) *ok = false;
    return BatchRenderedVideoContainer::Unspecified;
}

BatchRenderedVideoCodec batchRenderedVideoCodecFromString(const QString & value, bool * ok)
{
    const QString normalized = value.trimmed().toLower();
    if( normalized.isEmpty()
     || normalized == QStringLiteral("unspecified")
     || normalized == QStringLiteral("default")
     || normalized == QStringLiteral("auto") )
    {
        if( ok ) *ok = true;
        return BatchRenderedVideoCodec::Unspecified;
    }
    if( normalized == QStringLiteral("h264")
     || normalized == QStringLiteral("h.264")
     || normalized == QStringLiteral("avc") )
    {
        if( ok ) *ok = true;
        return BatchRenderedVideoCodec::H264;
    }
    if( normalized == QStringLiteral("h265")
     || normalized == QStringLiteral("h.265")
     || normalized == QStringLiteral("hevc") )
    {
        if( ok ) *ok = true;
        return BatchRenderedVideoCodec::H265;
    }
    if( normalized == QStringLiteral("prores")
     || normalized == QStringLiteral("pro-res") )
    {
        if( ok ) *ok = true;
        return BatchRenderedVideoCodec::ProRes;
    }

    if( ok ) *ok = false;
    return BatchRenderedVideoCodec::Unspecified;
}

