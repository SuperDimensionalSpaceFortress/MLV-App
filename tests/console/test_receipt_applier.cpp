#include "../common/minitest.h"
#include "../common/hash_helpers.h"
#include "../common/repo_paths.h"
#include "../common/test_artifacts.h"

#include "../../platform/qt/ReceiptSettings.h"
#include "../../src/batch/ReceiptApplier.h"
#include "../../src/batch/ReceiptLoader.h"
#include "../../src/batch/BatchContext.h"
#include "../../src/batch/BatchLogger.h"
#include "../../src/batch/BatchRunner.h"
#include "../../platform/qt/DualIsoPatternMapping.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>
#include <QProcess>
#include <QTemporaryDir>

#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <functional>
#include <memory>
#include <string>
#include <vector>

#ifdef Q_OS_WIN
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

static QString repo_receipt_path_for_apply(const QString & name)
{
    return repo_file_path(QStringLiteral("receipts/%1").arg(name));
}

static void seed_runtime_objects(mlvObject_t * video,
                                 llrawprocObject_t * llrawproc,
                                 processingObject_t * processing)
{
    *video = {};
    *llrawproc = {};
    *processing = {};

    video->llrawproc = llrawproc;
    video->processing = processing;
    video->RAWI.raw_info.bits_per_pixel = 14;
    video->RAWI.raw_info.black_level = 2048;
    video->RAWI.raw_info.white_level = 15000;
    video->original_black_level = video->RAWI.raw_info.black_level;
    video->original_white_level = video->RAWI.raw_info.white_level;
    video->IDNT.cameraModel = 0x80000285;
    video->frames = 4;
    video->cached_frames = static_cast<uint8_t *>(std::calloc(static_cast<std::size_t>(video->frames),
                                                              sizeof(*video->cached_frames)));

    pthread_mutex_init(&video->g_mutexFind, nullptr);
}

static void destroy_runtime_objects(mlvObject_t * video)
{
    pthread_mutex_destroy(&video->g_mutexFind);
    std::free(video->cached_frames);
    video->cached_frames = nullptr;
}

TEST(ReceiptApplier, FastProxyReceiptMapsToRuntimeState)
{
    ReceiptSettings receipt;
    QString error_message;
    ASSERT_TRUE(ReceiptLoader::loadFromFile(repo_receipt_path_for_apply(QStringLiteral("FastProxy.marxml")),
                                            &receipt,
                                            &error_message));

    auto video = std::make_unique<mlvObject_t>();
    auto llrawproc = std::make_unique<llrawprocObject_t>();
    auto processing = std::make_unique<processingObject_t>();
    seed_runtime_objects(video.get(), llrawproc.get(), processing.get());

    video->current_cached_frame_active = 1;
    video->current_processed_frame_active = 1;

    ReceiptApplier::applyToMlv(&receipt, video.get(), processing.get());

    ASSERT_EQ(1, llrawproc->fix_raw);
    ASSERT_EQ(1, llrawproc->focus_pixels);
    ASSERT_EQ(0, llrawproc->dual_iso);
    ASSERT_EQ(0, llrawproc->diso_averaging);
    ASSERT_EQ(1, llrawproc->diso_alias_map);
    ASSERT_EQ(1, llrawproc->diso_frblending);
    ASSERT_EQ(2047, video->RAWI.raw_info.black_level);
    ASSERT_EQ(2840, video->RAWI.raw_info.white_level);
    ASSERT_EQ(static_cast<unsigned int>(TR_NONE), static_cast<unsigned int>(processing->transformation));
    ASSERT_EQ(0, video->current_cached_frame_active);
    ASSERT_EQ(0, video->current_processed_frame_active);

    QTemporaryDir temporary_dir;
    const QString log_path = temporary_dir.filePath(QStringLiteral("fingerprint.log"));
    BatchLogger::init(log_path);
    ReceiptApplier::printFingerprint(video.get(), processing.get());
    BatchLogger::shutdown();

    QFile fingerprint_file(log_path);
    ASSERT_TRUE(fingerprint_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QByteArray fingerprint = fingerprint_file.readAll();
    ASSERT_TRUE(fingerprint.contains("dualIso=0"));
    ASSERT_TRUE(fingerprint.contains("rawBlack=2047"));

    test_artifacts::record("receipt_applier.fast_proxy_fingerprint",
                           sha256_bytes(fingerprint.constData(),
                                        static_cast<std::size_t>(fingerprint.size())));

    destroy_runtime_objects(video.get());
}

TEST(ReceiptApplier, PreviewDualIsoModePropagatesToRuntime)
{
    ReceiptSettings receipt;
    receipt.setRawFixesEnabled(true);
    receipt.setFocusPixels(1);
    receipt.setBadPixels(0);
    receipt.setDualIsoForced(0);
    receipt.setDualIso(2);
    receipt.setDualIsoAutoCorrected(1);
    receipt.setDualIsoInterpolation(1);
    receipt.setDualIsoAliasMap(0);
    receipt.setDualIsoFrBlending(0);
    receipt.setDarkFrameEnabled(0);
    receipt.setRawBlack(-1);
    receipt.setRawWhite(-1);

    auto video = std::make_unique<mlvObject_t>();
    auto llrawproc = std::make_unique<llrawprocObject_t>();
    auto processing = std::make_unique<processingObject_t>();
    seed_runtime_objects(video.get(), llrawproc.get(), processing.get());

    ReceiptApplier::applyToMlv(&receipt, video.get(), processing.get());

    ASSERT_EQ(2, llrawproc->dual_iso);
    ASSERT_EQ(1, llrawproc->diso_averaging);
    ASSERT_EQ(0, llrawproc->diso_alias_map);
    ASSERT_EQ(0, llrawproc->diso_frblending);

    const QString runtime_summary = QStringLiteral("dualIso=%1;interp=%2;alias=%3;fullres=%4;")
        .arg(llrawproc->dual_iso)
        .arg(llrawproc->diso_averaging)
        .arg(llrawproc->diso_alias_map)
        .arg(llrawproc->diso_frblending);
    test_artifacts::record("receipt_applier.preview_dual_iso",
                           sha256_qstring(runtime_summary));

    destroy_runtime_objects(video.get());
}

TEST(ReceiptApplier, PreviewDualIsoModeSurvivesWhenAutoCorrectionIsDisabled)
{
    ReceiptSettings receipt;
    receipt.setRawFixesEnabled(true);
    receipt.setFocusPixels(1);
    receipt.setBadPixels(0);
    receipt.setDualIsoForced(DISO_FORCED);
    receipt.setDualIso(2);
    receipt.setDualIsoAutoCorrected(0);
    receipt.setDualIsoInterpolation(1);
    receipt.setDualIsoAliasMap(0);
    receipt.setDualIsoFrBlending(0);
    receipt.setDarkFrameEnabled(0);
    receipt.setRawBlack(-1);
    receipt.setRawWhite(-1);

    auto video = std::make_unique<mlvObject_t>();
    auto llrawproc = std::make_unique<llrawprocObject_t>();
    auto processing = std::make_unique<processingObject_t>();
    seed_runtime_objects(video.get(), llrawproc.get(), processing.get());

    ReceiptApplier::applyToMlv(&receipt, video.get(), processing.get());

    ASSERT_EQ(2, llrawproc->dual_iso);
    ASSERT_EQ(0, llrawproc->diso_pattern);

    destroy_runtime_objects(video.get());
}

TEST(ReceiptApplier, AutoCorrectedDualIsoPatternMapsUiIndexToCorePattern)
{
    for( int uiIndex = 0; uiIndex <= 3; ++uiIndex )
    {
        ReceiptSettings receipt;
        receipt.setRawFixesEnabled(true);
        receipt.setFocusPixels(1);
        receipt.setBadPixels(0);
        receipt.setDualIsoForced(DISO_FORCED);
        receipt.setDualIso(2);
        receipt.setDualIsoAutoCorrected(1);
        receipt.setDualIsoPattern(uiIndex);
        receipt.setDualIsoEvCorrection(0);
        receipt.setDualIsoBlackDelta(0);
        receipt.setDarkFrameEnabled(0);
        receipt.setRawBlack(-1);
        receipt.setRawWhite(-1);

        auto video = std::make_unique<mlvObject_t>();
        auto llrawproc = std::make_unique<llrawprocObject_t>();
        auto processing = std::make_unique<processingObject_t>();
        seed_runtime_objects(video.get(), llrawproc.get(), processing.get());
        llrawproc->diso_validity = DISO_INVALID;
        llrawproc->diso1 = 100;
        llrawproc->diso2 = 1600;

        ReceiptApplier::applyToMlv(&receipt, video.get(), processing.get());

        ASSERT_EQ(dualIsoCorePatternFromUiIndex(uiIndex), llrawproc->diso_pattern);

        destroy_runtime_objects(video.get());
    }
}

TEST(ReceiptApplier, DisabledLookAssistClearsStaleBaselineValid)
{
    ReceiptSettings receipt;
    receipt.setLookAssistEnabled(false);
    receipt.setLookAssistBaselineValid(true);

    auto video = std::make_unique<mlvObject_t>();
    auto llrawproc = std::make_unique<llrawprocObject_t>();
    auto processing = std::make_unique<processingObject_t>();
    seed_runtime_objects(video.get(), llrawproc.get(), processing.get());

    QTemporaryDir temporary_dir;
    const QString log_path = temporary_dir.filePath(QStringLiteral("look-assist.log"));
    BatchLogger::init(log_path);
    const bool applied =
        ReceiptApplier::applyHeadlessLookAssist(&receipt, video.get(), processing.get(), 0);
    BatchLogger::shutdown();

    ASSERT_FALSE(applied);
    ASSERT_FALSE(receipt.lookAssistBaselineValid());

    destroy_runtime_objects(video.get());
}

TEST(ReceiptApplier, ValidDualIsoClipIgnoresCopiedCorrectionState)
{
    ReceiptSettings receipt;
    receipt.setRawFixesEnabled(true);
    receipt.setFocusPixels(1);
    receipt.setBadPixels(0);
    receipt.setDualIsoForced(DISO_VALID);
    receipt.setDualIso(1);
    receipt.setDualIsoAutoCorrected(1);
    receipt.setDualIsoPattern(0);
    receipt.setDualIsoEvCorrection(1);
    receipt.setDualIsoBlackDelta(-1);
    receipt.setDualIsoInterpolation(0);
    receipt.setDualIsoAliasMap(1);
    receipt.setDualIsoFrBlending(1);
    receipt.setDualIsoWhite(64346);
    receipt.setDualIsoBlack(8188);
    receipt.setDarkFrameEnabled(0);
    receipt.setRawBlack(12340);
    receipt.setRawWhite(16200);

    auto video = std::make_unique<mlvObject_t>();
    auto llrawproc = std::make_unique<llrawprocObject_t>();
    auto processing = std::make_unique<processingObject_t>();
    seed_runtime_objects(video.get(), llrawproc.get(), processing.get());

    llrawproc->diso_validity = DISO_VALID;
    llrawproc->diso1 = 100;
    llrawproc->diso2 = 6400;
    llrawproc->diso_auto_correction = 2;
    video->MLVI.videoClass = MLV_VIDEO_CLASS_FLAG_LJ92;
    video->RAWI.raw_info.black_level = 2047;
    video->RAWI.raw_info.white_level = 6000;
    video->original_black_level = 2047;
    video->original_white_level = 6000;

    ReceiptApplier::applyToMlv(&receipt, video.get(), processing.get());

    ASSERT_EQ(DISO_VALID, receipt.dualIsoForced());
    ASSERT_EQ(1, receipt.dualIso());
    ASSERT_EQ(0, receipt.dualIsoAutoCorrected());
    ASSERT_EQ(0, receipt.dualIsoPattern());
    ASSERT_EQ(1, receipt.dualIsoEvCorrection());
    ASSERT_EQ(-1, receipt.dualIsoBlackDelta());
    ASSERT_EQ(0u, receipt.dualIsoWhite());
    ASSERT_EQ(0u, receipt.dualIsoBlack());
    ASSERT_EQ(20470, receipt.rawBlack());
    ASSERT_EQ(6000, receipt.rawWhite());

    ASSERT_EQ(1, llrawproc->dual_iso);
    ASSERT_EQ(0, llrawproc->diso_pattern);
    ASSERT_EQ(-1, llrawproc->diso_auto_correction);
    ASSERT_EQ(1.0, llrawproc->diso_ev_correction);
    ASSERT_EQ(-1, llrawproc->diso_black_delta);
    ASSERT_EQ(2047, video->RAWI.raw_info.black_level);
    ASSERT_EQ(6000, video->RAWI.raw_info.white_level);

    destroy_runtime_objects(video.get());
}

TEST(ReceiptApplier, NonDualIsoClipDoesNotInheritValidDualIsoReceipt)
{
    ReceiptSettings receipt;
    receipt.setRawFixesEnabled(true);
    receipt.setFocusPixels(1);
    receipt.setBadPixels(0);
    receipt.setDualIsoForced(DISO_VALID);
    receipt.setDualIso(1);
    receipt.setDualIsoAutoCorrected(1);
    receipt.setDualIsoPattern(3);
    receipt.setDualIsoEvCorrection(220);
    receipt.setDualIsoBlackDelta(15);
    receipt.setDualIsoInterpolation(0);
    receipt.setDualIsoAliasMap(1);
    receipt.setDualIsoFrBlending(1);
    receipt.setDarkFrameEnabled(0);
    receipt.setRawBlack(-1);
    receipt.setRawWhite(-1);

    auto video = std::make_unique<mlvObject_t>();
    auto llrawproc = std::make_unique<llrawprocObject_t>();
    auto processing = std::make_unique<processingObject_t>();
    seed_runtime_objects(video.get(), llrawproc.get(), processing.get());
    llrawproc->diso_validity = DISO_INVALID;

    ReceiptApplier::applyToMlv(&receipt, video.get(), processing.get());

    ASSERT_EQ(DISO_INVALID, receipt.dualIsoForced());
    ASSERT_EQ(0, receipt.dualIso());
    ASSERT_EQ(0, llrawproc->dual_iso);
    ASSERT_EQ(DISO_INVALID, llrawproc->diso_validity);

    destroy_runtime_objects(video.get());
}

TEST(ReceiptApplier, ApplyingReceiptInvalidatesCachedFrames)
{
    ReceiptSettings receipt;
    receipt.setRawFixesEnabled(true);
    receipt.setDualIso(1);
    receipt.setDualIsoInterpolation(0);

    auto video = std::make_unique<mlvObject_t>();
    auto llrawproc = std::make_unique<llrawprocObject_t>();
    auto processing = std::make_unique<processingObject_t>();
    seed_runtime_objects(video.get(), llrawproc.get(), processing.get());

    video->cached_frames[0] = MLV_FRAME_IS_CACHED;
    video->cached_frames[1] = MLV_FRAME_BEING_CACHED;
    video->cached_frames[2] = MLV_FRAME_IS_CACHED;
    video->current_cached_frame_active = 1;
    video->current_processed_frame_active = 1;

    ReceiptApplier::applyToMlv(&receipt, video.get(), processing.get());

    ASSERT_EQ(0, video->current_cached_frame_active);
    ASSERT_EQ(0, video->current_processed_frame_active);
    for( uint64_t index = 0; index < video->frames; ++index )
    {
        ASSERT_EQ(static_cast<unsigned int>(MLV_FRAME_NOT_CACHED),
                  static_cast<unsigned int>(video->cached_frames[index]));
    }

    destroy_runtime_objects(video.get());
}

/* Regression: headless Look Assist must analyze the ORIGINAL receipt cut-in
 * frame, not effectiveCutIn. Under --resume effectiveCutIn is advanced past the
 * frames a previous run already exported; anchoring the analysis on it would
 * give the remaining DNGs different clip-wide look defaults than the first run.
 * lookAssistAnalysisFrameIndex must therefore ignore effectiveCutIn. */
TEST(BatchRunner, LookAssistAnalysisFrameAnchorsToOriginalCutInUnderResume)
{
    /* First run: nothing exported yet, effectiveCutIn == cutIn. */
    ASSERT_EQ(0u, BatchRunner::lookAssistAnalysisFrameIndex(1, 1));
    ASSERT_EQ(9u, BatchRunner::lookAssistAnalysisFrameIndex(10, 10));

    /* Resumed run: effectiveCutIn advanced far past cutIn. The analysis anchor
     * MUST stay at cutIn - 1 (this is exactly the bug the fix prevents: pre-fix
     * these returned effectiveCutIn - 1 = 49 and 199). */
    ASSERT_EQ(0u, BatchRunner::lookAssistAnalysisFrameIndex(1, 50));
    ASSERT_EQ(9u, BatchRunner::lookAssistAnalysisFrameIndex(10, 200));

    /* Degenerate cut-in (receipt cutIn == 0, normalized to frame 0). */
    ASSERT_EQ(0u, BatchRunner::lookAssistAnalysisFrameIndex(0, 0));
    ASSERT_EQ(0u, BatchRunner::lookAssistAnalysisFrameIndex(0, 25));
}

TEST(BatchRunner, MaxFramesClampPreservesCutInAndReceiptBounds)
{
    ASSERT_EQ(10u, BatchRunner::cutOutClampedForMaxFrames(1, 10, 0));
    ASSERT_EQ(5u, BatchRunner::cutOutClampedForMaxFrames(1, 10, 5));
    ASSERT_EQ(14u, BatchRunner::cutOutClampedForMaxFrames(10, 100, 5));
    ASSERT_EQ(12u, BatchRunner::cutOutClampedForMaxFrames(10, 12, 5));
    ASSERT_EQ(8u, BatchRunner::cutOutClampedForMaxFrames(10, 8, 5));
}

TEST(BatchExportFormat, ParsesCdngAliases)
{
    ASSERT_EQ( static_cast<int>(BatchExportFormat::Cdng),
               static_cast<int>(batchExportFormatFromString(QString())) );
    ASSERT_EQ( static_cast<int>(BatchExportFormat::Cdng),
               static_cast<int>(batchExportFormatFromString(QStringLiteral("cdng"))) );
    ASSERT_EQ( static_cast<int>(BatchExportFormat::Cdng),
               static_cast<int>(batchExportFormatFromString(QStringLiteral("Cinema-DNG"))) );
    ASSERT_EQ( std::string("cdng"),
               std::string(batchExportFormatName(BatchExportFormat::Cdng)) );
}

TEST(BatchExportFormat, RecognizesRenderedVideoAsBlockedE4)
{
    ASSERT_EQ( static_cast<int>(BatchExportFormat::RenderedVideo),
               static_cast<int>(batchExportFormatFromString(QStringLiteral("rendered-video"))) );
    ASSERT_EQ( static_cast<int>(BatchExportFormat::RenderedVideo),
               static_cast<int>(batchExportFormatFromString(QStringLiteral("video"))) );
    ASSERT_EQ( static_cast<int>(BatchExportFormat::RenderedVideo),
               static_cast<int>(batchExportFormatFromString(QStringLiteral("mp4"))) );
    ASSERT_EQ( static_cast<int>(BatchExportFormat::RenderedVideo),
               static_cast<int>(batchExportFormatFromString(QStringLiteral("prores"))) );
    ASSERT_EQ( std::string("rendered-video"),
               std::string(batchExportFormatName(BatchExportFormat::RenderedVideo)) );
}

TEST(BatchExportFormat, PreservesRenderedVideoRequestIntent)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264-mp4"));
    ASSERT_EQ( static_cast<int>(BatchExportFormat::RenderedVideo),
               static_cast<int>(request.format) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::H264),
               static_cast<int>(request.renderedCodec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mp4),
               static_cast<int>(request.renderedContainer) );
    ASSERT_EQ( std::string("h264"),
               std::string(batchRenderedVideoCodecName(request.renderedCodec)) );
    ASSERT_EQ( std::string("mp4"),
               std::string(batchRenderedVideoContainerName(request.renderedContainer)) );
    ASSERT_EQ( std::string("h264"),
               std::string(batchRenderedVideoMediaProbeCodecName(request.renderedCodec)) );
    ASSERT_EQ( std::string("mp4"),
               std::string(batchRenderedVideoMediaProbeContainerName(request.renderedContainer)) );

    request = batchExportFormatRequestFromString(QStringLiteral("hevc-mp4"));
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::H265),
               static_cast<int>(request.renderedCodec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mp4),
               static_cast<int>(request.renderedContainer) );
    ASSERT_EQ( std::string("hevc"),
               std::string(batchRenderedVideoMediaProbeCodecName(request.renderedCodec)) );

    request = batchExportFormatRequestFromString(QStringLiteral("prores"));
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::ProRes),
               static_cast<int>(request.renderedCodec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mov),
               static_cast<int>(request.renderedContainer) );
    ASSERT_EQ( std::string("prores"),
               std::string(batchRenderedVideoMediaProbeCodecName(request.renderedCodec)) );
    ASSERT_EQ( std::string("mov"),
               std::string(batchRenderedVideoMediaProbeContainerName(request.renderedContainer)) );

    request = batchExportFormatRequestFromString(QStringLiteral("rendered-video"));
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::Unspecified),
               static_cast<int>(request.renderedCodec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Unspecified),
               static_cast<int>(request.renderedContainer) );
}

TEST(BatchExportFormat, ParsesRenderedVideoOptionAliases)
{
    bool ok = false;
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::H264),
               static_cast<int>(batchRenderedVideoCodecFromString(QStringLiteral("avc"), &ok)) );
    ASSERT_TRUE( ok );

    ok = false;
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::H265),
               static_cast<int>(batchRenderedVideoCodecFromString(QStringLiteral("h.265"), &ok)) );
    ASSERT_TRUE( ok );

    ok = false;
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::ProRes),
               static_cast<int>(batchRenderedVideoCodecFromString(QStringLiteral("pro-res"), &ok)) );
    ASSERT_TRUE( ok );

    ok = true;
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::Unspecified),
               static_cast<int>(batchRenderedVideoCodecFromString(QStringLiteral("dnxhr"), &ok)) );
    ASSERT_FALSE( ok );

    ok = false;
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mov),
               static_cast<int>(batchRenderedVideoContainerFromString(QStringLiteral("quicktime"), &ok)) );
    ASSERT_TRUE( ok );

    ok = false;
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mkv),
               static_cast<int>(batchRenderedVideoContainerFromString(QStringLiteral("matroska"), &ok)) );
    ASSERT_TRUE( ok );
    ASSERT_EQ( std::string("matroska"),
               std::string(batchRenderedVideoMediaProbeContainerName(
                   BatchRenderedVideoContainer::Mkv)) );

    ok = true;
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Unspecified),
               static_cast<int>(batchRenderedVideoContainerFromString(QStringLiteral("avi"), &ok)) );
    ASSERT_FALSE( ok );
}

TEST(BatchExportFormat, BatchContextPreservesExportRequestIntent)
{
    BatchExportFormatRequest request;
    request.format = BatchExportFormat::RenderedVideo;
    request.renderedCodec = BatchRenderedVideoCodec::H265;
    request.renderedContainer = BatchRenderedVideoContainer::Mkv;

    BatchContext::setExportFormatRequest(request);

    const BatchExportFormatRequest stored = BatchContext::exportFormatRequest();
    ASSERT_EQ( static_cast<int>(BatchExportFormat::RenderedVideo),
               static_cast<int>(stored.format) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::H265),
               static_cast<int>(stored.renderedCodec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mkv),
               static_cast<int>(stored.renderedContainer) );
    ASSERT_EQ( std::string("request=rendered-video codec=h265 container=mkv"),
               std::string(batchExportFormatRequestSummary(stored).toUtf8().constData()) );

    BatchContext::setExportFormatRequest(BatchExportFormatRequest());
}

TEST(BatchExportFormat, BatchContextPreservesRenderedRenderSettings)
{
    BatchRenderedVideoRenderSettings settings =
        batchRenderedVideoRenderSettingsFromExplicitResize(
            true,
            2048,
            0,
            true);

    BatchContext::setRenderedVideoRenderSettings(settings);

    const BatchRenderedVideoRenderSettings stored =
        BatchContext::renderedVideoRenderSettings();
    ASSERT_TRUE( stored.ready );
    ASSERT_TRUE( stored.explicitHeadlessSettings );
    ASSERT_FALSE( stored.guiSettingsOwned );
    ASSERT_TRUE( stored.resizeEnabled );
    ASSERT_EQ( 2048, stored.resizeWidth );
    ASSERT_EQ( 0, stored.resizeHeight );
    ASSERT_TRUE( stored.resizeHeightLocked );
    ASSERT_EQ( std::string("render-settings-source=explicit-headless render-settings-explicit-headless=true render-settings-gui-owned=false render-settings-ready=true render-settings-reason=none render-settings-resize=true render-settings-resize-width=2048 render-settings-resize-height=0 render-settings-resize-height-locked=true"),
               std::string(batchRenderedVideoRenderSettingsSummary(
                   stored).toUtf8().constData()) );

    BatchContext::setRenderedVideoRenderSettings(
        batchRenderedVideoDefaultRenderSettings());
}

TEST(BatchExportFormat, BatchContextPreservesRenderedFfmpegExecutable)
{
    BatchContext::setRenderedVideoFfmpegExecutable(
        QStringLiteral("  C:/tools/ffmpeg-custom.exe  "));

    ASSERT_EQ( std::string("C:/tools/ffmpeg-custom.exe"),
               std::string(BatchContext::renderedVideoFfmpegExecutable()
                   .toUtf8().constData()) );

    BatchContext::setRenderedVideoFfmpegExecutable(QString());
    ASSERT_TRUE( BatchContext::renderedVideoFfmpegExecutable().isEmpty() );
}

TEST(BatchExportFormat, ValidatesRenderedVideoRequestShape)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264-mp4"));
    ASSERT_TRUE( batchRenderedVideoRequestShapeValid(request) );

    request = batchExportFormatRequestFromString(QStringLiteral("prores-mov"));
    ASSERT_TRUE( batchRenderedVideoRequestShapeValid(request) );

    request = batchExportFormatRequestFromString(QStringLiteral("prores"));
    request.renderedContainer = BatchRenderedVideoContainer::Mp4;
    ASSERT_FALSE( batchRenderedVideoRequestShapeValid(request) );
    ASSERT_EQ( std::string("codec=prores requires container=mov or unspecified"),
               std::string(batchRenderedVideoRequestShapeError(request).toUtf8().constData()) );
}

TEST(BatchExportFormat, ResolvesRenderedVideoTargets)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoTarget target =
        batchRenderedVideoTargetFromRequest(request);
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::H264),
               static_cast<int>(target.codec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mp4),
               static_cast<int>(target.container) );
    ASSERT_TRUE( target.complete );
    ASSERT_EQ( std::string(".mp4"),
               std::string(target.extension.toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("mp4"));
    target = batchRenderedVideoTargetFromRequest(request);
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::H264),
               static_cast<int>(target.codec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mp4),
               static_cast<int>(target.container) );
    ASSERT_TRUE( target.complete );

    request = batchExportFormatRequestFromString(QStringLiteral("mkv"));
    target = batchRenderedVideoTargetFromRequest(request);
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::H264),
               static_cast<int>(target.codec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mkv),
               static_cast<int>(target.container) );
    ASSERT_EQ( std::string(".mkv"),
               std::string(target.extension.toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("h265"));
    target = batchRenderedVideoTargetFromRequest(request);
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::H265),
               static_cast<int>(target.codec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mp4),
               static_cast<int>(target.container) );
    ASSERT_TRUE( target.complete );

    request = batchExportFormatRequestFromString(QStringLiteral("mov"));
    target = batchRenderedVideoTargetFromRequest(request);
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::ProRes),
               static_cast<int>(target.codec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Mov),
               static_cast<int>(target.container) );
    ASSERT_EQ( std::string(".mov"),
               std::string(target.extension.toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("rendered-video"));
    target = batchRenderedVideoTargetFromRequest(request);
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoCodec::Unspecified),
               static_cast<int>(target.codec) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoContainer::Unspecified),
               static_cast<int>(target.container) );
    ASSERT_FALSE( target.complete );
    ASSERT_TRUE( target.extension.isEmpty() );
    ASSERT_EQ( std::string("target-codec=unspecified target-container=unspecified target-extension=unspecified target-complete=false"),
               std::string(batchRenderedVideoTargetSummary(request).toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("prores"));
    request.renderedContainer = BatchRenderedVideoContainer::Mp4;
    target = batchRenderedVideoTargetFromRequest(request);
    ASSERT_FALSE( target.complete );
    ASSERT_TRUE( target.extension.isEmpty() );
}

TEST(BatchExportFormat, ResolvesRenderedVideoEncoderPresets)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoEncoderPreset preset =
        batchRenderedVideoEncoderPresetFromRequest(request);
    ASSERT_TRUE( preset.ready );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderProfile::H264),
               static_cast<int>(preset.profile) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderOption::H264HighMp4),
               static_cast<int>(preset.option) );
    ASSERT_EQ( CODEC_H264, preset.guiCodecProfile );
    ASSERT_EQ( CODEC_H264_H_MP4, preset.guiCodecOption );
    ASSERT_EQ( std::string(".mp4"),
               std::string(preset.extension.toUtf8().constData()) );
    ASSERT_EQ( std::string("encoder-profile=h264 encoder-option=ffmpeg-mp4-high gui-codec-profile=9 gui-codec-option=1 encoder-extension=.mp4 encoder-ready=true"),
               std::string(batchRenderedVideoEncoderPresetSummary(request).toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("mkv"));
    preset = batchRenderedVideoEncoderPresetFromRequest(request);
    ASSERT_TRUE( preset.ready );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderProfile::H264),
               static_cast<int>(preset.profile) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderOption::H264HighMkv),
               static_cast<int>(preset.option) );
    ASSERT_EQ( CODEC_H264, preset.guiCodecProfile );
    ASSERT_EQ( CODEC_H264_H_MKV, preset.guiCodecOption );
    ASSERT_EQ( std::string(".mkv"),
               std::string(preset.extension.toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("h265"));
    preset = batchRenderedVideoEncoderPresetFromRequest(request);
    ASSERT_TRUE( preset.ready );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderProfile::H265_8),
               static_cast<int>(preset.profile) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderOption::H265HighMp4),
               static_cast<int>(preset.option) );
    ASSERT_EQ( CODEC_H265_8, preset.guiCodecProfile );
    ASSERT_EQ( CODEC_H265_H_MP4, preset.guiCodecOption );
    ASSERT_EQ( std::string(".mp4"),
               std::string(preset.extension.toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("mov"));
    preset = batchRenderedVideoEncoderPresetFromRequest(request);
    ASSERT_TRUE( preset.ready );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderProfile::ProRes422HQ),
               static_cast<int>(preset.profile) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderOption::ProResFfmpegKostya),
               static_cast<int>(preset.option) );
    ASSERT_EQ( CODEC_PRORES422HQ, preset.guiCodecProfile );
    ASSERT_EQ( CODEC_PRORES_OPTION_KS, preset.guiCodecOption );
    ASSERT_EQ( std::string(".mov"),
               std::string(preset.extension.toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("rendered-video"));
    preset = batchRenderedVideoEncoderPresetFromRequest(request);
    ASSERT_FALSE( preset.ready );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderProfile::Unspecified),
               static_cast<int>(preset.profile) );
    ASSERT_EQ( static_cast<int>(BatchRenderedVideoEncoderOption::Unspecified),
               static_cast<int>(preset.option) );
    ASSERT_EQ( -1, preset.guiCodecProfile );
    ASSERT_EQ( -1, preset.guiCodecOption );
    ASSERT_TRUE( preset.extension.isEmpty() );
}

TEST(BatchExportFormat, PlansRenderedVideoFfmpegVideoArguments)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoFfmpegVideoPlan ffmpegPlan =
        batchRenderedVideoFfmpegVideoPlanFromRequest(request);
    ASSERT_TRUE( ffmpegPlan.ready );
    ASSERT_EQ( std::string("libx264"),
               std::string(ffmpegPlan.encoder.toUtf8().constData()) );
    ASSERT_EQ( std::string("medium"),
               std::string(ffmpegPlan.preset.toUtf8().constData()) );
    ASSERT_EQ( std::string("-crf"),
               std::string(ffmpegPlan.qualityFlag.toUtf8().constData()) );
    ASSERT_EQ( 14, ffmpegPlan.qualityValue );
    ASSERT_EQ( std::string("yuv420p"),
               std::string(ffmpegPlan.pixelFormat.toUtf8().constData()) );
    ASSERT_EQ( std::string("-c:v libx264 -preset medium -crf 14 -pix_fmt yuv420p"),
               std::string(ffmpegPlan.videoArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-video-encoder=libx264 ffmpeg-video-preset=medium ffmpeg-video-quality=-crf:14 ffmpeg-video-pix-fmt=yuv420p ffmpeg-video-tag=none ffmpeg-video-args=-c:v libx264 -preset medium -crf 14 -pix_fmt yuv420p ffmpeg-video-ready=true ffmpeg-video-reason=none"),
               std::string(batchRenderedVideoFfmpegVideoPlanSummary(request).toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("h265"));
    ffmpegPlan = batchRenderedVideoFfmpegVideoPlanFromRequest(request);
    ASSERT_TRUE( ffmpegPlan.ready );
    ASSERT_EQ( std::string("libx265"),
               std::string(ffmpegPlan.encoder.toUtf8().constData()) );
    ASSERT_EQ( std::string("-crf"),
               std::string(ffmpegPlan.qualityFlag.toUtf8().constData()) );
    ASSERT_EQ( 18, ffmpegPlan.qualityValue );
    ASSERT_EQ( std::string("hvc1"),
               std::string(ffmpegPlan.videoTag.toUtf8().constData()) );
    ASSERT_EQ( std::string("-c:v libx265 -preset medium -crf 18 -tag:v hvc1 -pix_fmt yuv420p"),
               std::string(ffmpegPlan.videoArguments.toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("mov"));
    ffmpegPlan = batchRenderedVideoFfmpegVideoPlanFromRequest(request);
    ASSERT_TRUE( ffmpegPlan.ready );
    ASSERT_EQ( std::string("prores_ks"),
               std::string(ffmpegPlan.encoder.toUtf8().constData()) );
    ASSERT_EQ( std::string("-profile:v"),
               std::string(ffmpegPlan.qualityFlag.toUtf8().constData()) );
    ASSERT_EQ( CODEC_PRORES422HQ, ffmpegPlan.qualityValue );
    ASSERT_EQ( std::string("yuv422p10"),
               std::string(ffmpegPlan.pixelFormat.toUtf8().constData()) );
    ASSERT_EQ( std::string("-c:v prores_ks -profile:v 3 -pix_fmt yuv422p10"),
               std::string(ffmpegPlan.videoArguments.toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("rendered-video"));
    ffmpegPlan = batchRenderedVideoFfmpegVideoPlanFromRequest(request);
    ASSERT_FALSE( ffmpegPlan.ready );
    ASSERT_EQ( std::string("rendered encoder preset incomplete"),
               std::string(ffmpegPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoFfmpegFilterArguments)
{
    BatchRenderedVideoFfmpegFilterPlan filterPlan =
        batchRenderedVideoFfmpegFilterPlanForCurrentBuild();

    ASSERT_TRUE( filterPlan.ready );
    ASSERT_TRUE( filterPlan.baseColorScaleReady );
    ASSERT_FALSE( filterPlan.optionalFiltersOwned );
    ASSERT_FALSE( filterPlan.moireeFilterOwned );
    ASSERT_FALSE( filterPlan.hdrBlendOwned );
    ASSERT_FALSE( filterPlan.stabilizationOwned );
    ASSERT_EQ( std::string("gui-base-color-scale"),
               std::string(filterPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("scale=in_color_matrix=bt601:out_color_matrix=bt709"),
               std::string(filterPlan.colorScaleFilter.toUtf8().constData()) );
    ASSERT_EQ( std::string("-vf scale=in_color_matrix=bt601:out_color_matrix=bt709"),
               std::string(filterPlan.filterArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-filter-source=gui-base-color-scale ffmpeg-filter-base-color-scale-ready=true ffmpeg-filter-optional-owned=false ffmpeg-filter-moiree-owned=false ffmpeg-filter-hdr-owned=false ffmpeg-filter-stabilization-owned=false ffmpeg-filter-color-scale=scale=in_color_matrix=bt601:out_color_matrix=bt709 ffmpeg-filter-args=-vf scale=in_color_matrix=bt601:out_color_matrix=bt709 ffmpeg-filter-ready=true ffmpeg-filter-reason=none"),
               std::string(batchRenderedVideoFfmpegFilterPlanSummary(
                   filterPlan).toUtf8().constData()) );

    BatchRenderedVideoOptionalFilterPlan optionalFilterPlan =
        batchRenderedVideoOptionalFilterPlanFromFilterPlan(filterPlan);
    ASSERT_TRUE( optionalFilterPlan.contractReady );
    ASSERT_TRUE( optionalFilterPlan.baseFilterContractReady );
    ASSERT_FALSE( optionalFilterPlan.optionalFiltersRequested );
    ASSERT_FALSE( optionalFilterPlan.optionalFilterGraphOwned );
    ASSERT_FALSE( optionalFilterPlan.moireeFilterOwned );
    ASSERT_FALSE( optionalFilterPlan.hdrBlendOwned );
    ASSERT_FALSE( optionalFilterPlan.stabilizationOwned );
    ASSERT_FALSE( optionalFilterPlan.optionalFilterOrderOwned );
    ASSERT_FALSE( optionalFilterPlan.optionalFilterParityOwned );
    ASSERT_FALSE( optionalFilterPlan.optionalFilterExecutionReady );
    ASSERT_EQ( std::string("optional-filter-ownership-contract"),
               std::string(optionalFilterPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("-vf scale=in_color_matrix=bt601:out_color_matrix=bt709"),
               std::string(optionalFilterPlan.baseFilterArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("optional-filter-source=optional-filter-ownership-contract optional-filter-base-contract-ready=true optional-filter-base-args=-vf scale=in_color_matrix=bt601:out_color_matrix=bt709 optional-filter-requested=false optional-filter-graph-owned=false optional-filter-moiree-owned=false optional-filter-hdr-owned=false optional-filter-stabilization-owned=false optional-filter-order-owned=false optional-filter-parity-owned=false optional-filter-exec-ready=false optional-filter-contract-ready=true optional-filter-reason=none"),
               std::string(batchRenderedVideoOptionalFilterPlanSummary(
                   optionalFilterPlan).toUtf8().constData()) );

    BatchRenderedVideoFfmpegFilterPlan invalidFilterPlan;
    optionalFilterPlan =
        batchRenderedVideoOptionalFilterPlanFromFilterPlan(invalidFilterPlan);
    ASSERT_FALSE( optionalFilterPlan.contractReady );
    ASSERT_FALSE( optionalFilterPlan.baseFilterContractReady );
    ASSERT_EQ( std::string("rendered ffmpeg filter plan unavailable"),
               std::string(optionalFilterPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoSourceAudioContract)
{
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild(
            QStringLiteral("C:\\clips\\M16-1327.MLV"));

    ASSERT_TRUE( sourceAudioPlan.contractReady );
    ASSERT_TRUE( sourceAudioPlan.videoOnlyFallbackReady );
    ASSERT_FALSE( sourceAudioPlan.discoveryOwned );
    ASSERT_FALSE( sourceAudioPlan.discoveryAttempted );
    ASSERT_FALSE( sourceAudioPlan.sourceAudioKnown );
    ASSERT_FALSE( sourceAudioPlan.sourceAudioPresent );
    ASSERT_FALSE( sourceAudioPlan.extractionOwned );
    ASSERT_FALSE( sourceAudioPlan.muxInputOwned );
    ASSERT_FALSE( sourceAudioPlan.syncValidationOwned );
    ASSERT_EQ( 0, sourceAudioPlan.sampleRate );
    ASSERT_EQ( 0, sourceAudioPlan.channels );
    ASSERT_EQ( 0, sourceAudioPlan.bitsPerSample );
    ASSERT_EQ( static_cast<qulonglong>(0), sourceAudioPlan.audioBytes );
    ASSERT_EQ( std::string("video-only-undiscovered"),
               std::string(sourceAudioPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/clips/M16-1327.MLV"),
               std::string(sourceAudioPlan.clipPath.toUtf8().constData()) );
    ASSERT_EQ( std::string("unknown"),
               std::string(sourceAudioPlan.audioState.toUtf8().constData()) );
    ASSERT_EQ( std::string("source-audio-source=video-only-undiscovered source-audio-clip=C:/clips/M16-1327.MLV source-audio-state=unknown source-audio-sample-rate=0 source-audio-channels=0 source-audio-bits=0 source-audio-bytes=0 source-audio-discovery-owned=false source-audio-discovery-attempted=false source-audio-known=false source-audio-present=false source-audio-extraction-owned=false source-audio-mux-input-owned=false source-audio-sync-validation-owned=false source-audio-video-only-ready=true source-audio-contract-ready=true source-audio-reason=none"),
               std::string(batchRenderedVideoSourceAudioPlanSummary(
                   sourceAudioPlan).toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoDiscoveredSourceAudioMetadata)
{
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:\\clips\\M16-1327.MLV"),
            true,
            2,
            48000,
            16,
            123456);

    ASSERT_TRUE( sourceAudioPlan.contractReady );
    ASSERT_TRUE( sourceAudioPlan.videoOnlyFallbackReady );
    ASSERT_TRUE( sourceAudioPlan.discoveryOwned );
    ASSERT_TRUE( sourceAudioPlan.discoveryAttempted );
    ASSERT_TRUE( sourceAudioPlan.sourceAudioKnown );
    ASSERT_TRUE( sourceAudioPlan.sourceAudioPresent );
    ASSERT_FALSE( sourceAudioPlan.extractionOwned );
    ASSERT_FALSE( sourceAudioPlan.muxInputOwned );
    ASSERT_FALSE( sourceAudioPlan.syncValidationOwned );
    ASSERT_EQ( 48000, sourceAudioPlan.sampleRate );
    ASSERT_EQ( 2, sourceAudioPlan.channels );
    ASSERT_EQ( 16, sourceAudioPlan.bitsPerSample );
    ASSERT_EQ( static_cast<qulonglong>(123456), sourceAudioPlan.audioBytes );
    ASSERT_EQ( std::string("open-mlv-audio-metadata"),
               std::string(sourceAudioPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("present"),
               std::string(sourceAudioPlan.audioState.toUtf8().constData()) );
    ASSERT_EQ( std::string("source-audio-source=open-mlv-audio-metadata source-audio-clip=C:/clips/M16-1327.MLV source-audio-state=present source-audio-sample-rate=48000 source-audio-channels=2 source-audio-bits=16 source-audio-bytes=123456 source-audio-discovery-owned=true source-audio-discovery-attempted=true source-audio-known=true source-audio-present=true source-audio-extraction-owned=false source-audio-mux-input-owned=false source-audio-sync-validation-owned=false source-audio-video-only-ready=true source-audio-contract-ready=true source-audio-reason=none"),
               std::string(batchRenderedVideoSourceAudioPlanSummary(
                   sourceAudioPlan).toUtf8().constData()) );

    sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            false,
            0,
            0,
            0,
            0);
    ASSERT_TRUE( sourceAudioPlan.contractReady );
    ASSERT_TRUE( sourceAudioPlan.discoveryOwned );
    ASSERT_TRUE( sourceAudioPlan.discoveryAttempted );
    ASSERT_TRUE( sourceAudioPlan.sourceAudioKnown );
    ASSERT_FALSE( sourceAudioPlan.sourceAudioPresent );
    ASSERT_EQ( std::string("absent"),
               std::string(sourceAudioPlan.audioState.toUtf8().constData()) );

    sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            true,
            0,
            48000,
            16,
            123456);
    ASSERT_FALSE( sourceAudioPlan.contractReady );
    ASSERT_TRUE( sourceAudioPlan.videoOnlyFallbackReady );
    ASSERT_TRUE( sourceAudioPlan.discoveryOwned );
    ASSERT_TRUE( sourceAudioPlan.sourceAudioKnown );
    ASSERT_TRUE( sourceAudioPlan.sourceAudioPresent );
    ASSERT_EQ( std::string("rendered source audio metadata invalid"),
               std::string(sourceAudioPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoSourceAudioExtractionPrerequisites)
{
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild(
            QStringLiteral("C:\\clips\\M16-1327.MLV"));
    BatchRenderedVideoSourceAudioExtractionPlan extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:\\renders\\M16-1327.source-audio.wav"));

    ASSERT_TRUE( extractionPlan.contractReady );
    ASSERT_TRUE( extractionPlan.sourceAudioContractReady );
    ASSERT_FALSE( extractionPlan.sourceAudioKnown );
    ASSERT_FALSE( extractionPlan.sourceAudioPresent );
    ASSERT_FALSE( extractionPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( extractionPlan.extractionPathPlanned );
    ASSERT_TRUE( extractionPlan.extractionPathReady );
    ASSERT_TRUE( extractionPlan.sampleFormatReady );
    ASSERT_TRUE( extractionPlan.sampleRateReady );
    ASSERT_TRUE( extractionPlan.channelLayoutReady );
    ASSERT_FALSE( extractionPlan.extractionProcessOwned );
    ASSERT_FALSE( extractionPlan.tempFileOwned );
    ASSERT_FALSE( extractionPlan.cleanupOwned );
    ASSERT_FALSE( extractionPlan.extractionReady );
    ASSERT_TRUE( extractionPlan.videoOnlyFallbackReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.source-audio.wav"),
               std::string(extractionPlan.plannedAudioPath
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("source-audio-extraction-source=source-audio-extraction-prerequisite-contract source-audio-extraction-clip=C:/clips/M16-1327.MLV source-audio-extraction-format=wav-pcm-s16le source-audio-extraction-path=C:/renders/M16-1327.source-audio.wav source-audio-extraction-source-contract-ready=true source-audio-extraction-known=false source-audio-extraction-present=false source-audio-extraction-discovery-owned=false source-audio-extraction-path-planned=false source-audio-extraction-path-ready=true source-audio-extraction-format-ready=true source-audio-extraction-sample-rate-ready=true source-audio-extraction-channel-layout-ready=true source-audio-extraction-process-owned=false source-audio-extraction-temp-file-owned=false source-audio-extraction-cleanup-owned=false source-audio-extraction-ready=false source-audio-extraction-video-only-ready=true source-audio-extraction-contract-ready=true source-audio-extraction-reason=none"),
               std::string(batchRenderedVideoSourceAudioExtractionPlanSummary(
                   extractionPlan).toUtf8().constData()) );

    sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:\\clips\\M16-1327.MLV"),
            true,
            2,
            48000,
            16,
            123456);
    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:/renders/M16-1327.source-audio.wav"));
    ASSERT_TRUE( extractionPlan.contractReady );
    ASSERT_TRUE( extractionPlan.sourceAudioContractReady );
    ASSERT_TRUE( extractionPlan.sourceAudioKnown );
    ASSERT_TRUE( extractionPlan.sourceAudioPresent );
    ASSERT_TRUE( extractionPlan.sourceAudioDiscoveryOwned );
    ASSERT_TRUE( extractionPlan.extractionPathPlanned );
    ASSERT_TRUE( extractionPlan.extractionPathReady );
    ASSERT_TRUE( extractionPlan.sampleFormatReady );
    ASSERT_TRUE( extractionPlan.sampleRateReady );
    ASSERT_TRUE( extractionPlan.channelLayoutReady );
    ASSERT_FALSE( extractionPlan.extractionProcessOwned );
    ASSERT_FALSE( extractionPlan.tempFileOwned );
    ASSERT_FALSE( extractionPlan.cleanupOwned );
    ASSERT_FALSE( extractionPlan.extractionReady );
    ASSERT_TRUE( extractionPlan.videoOnlyFallbackReady );

    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QString());
    ASSERT_FALSE( extractionPlan.contractReady );
    ASSERT_TRUE( extractionPlan.extractionPathPlanned );
    ASSERT_FALSE( extractionPlan.extractionPathReady );
    ASSERT_EQ( std::string("rendered source audio extraction prerequisite contract unavailable"),
               std::string(extractionPlan.reason.toUtf8().constData()) );

    BatchRenderedVideoSourceAudioPlan invalidSourceAudioPlan;
    invalidSourceAudioPlan.reason =
        QStringLiteral("source audio contract unavailable");
    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            invalidSourceAudioPlan,
            QStringLiteral("C:/renders/M16-1327.source-audio.wav"));
    ASSERT_FALSE( extractionPlan.contractReady );
    ASSERT_FALSE( extractionPlan.sourceAudioContractReady );
    ASSERT_EQ( std::string("source audio contract unavailable"),
               std::string(extractionPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoSourceAudioExtractionExecutionContract)
{
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild(
            QStringLiteral("C:\\clips\\M16-1327.MLV"));
    BatchRenderedVideoSourceAudioExtractionPlan extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:\\renders\\M16-1327.source-audio.wav"));
    BatchRenderedVideoSourceAudioExtractionExecutionPlan executionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);

    ASSERT_TRUE( executionPlan.contractReady );
    ASSERT_TRUE( executionPlan.extractionPrerequisiteContractReady );
    ASSERT_FALSE( executionPlan.sourceAudioKnown );
    ASSERT_FALSE( executionPlan.sourceAudioPresent );
    ASSERT_FALSE( executionPlan.extractionPathPlanned );
    ASSERT_TRUE( executionPlan.extractionPathReady );
    ASSERT_FALSE( executionPlan.sampleReadPlanned );
    ASSERT_FALSE( executionPlan.wavWritePlanned );
    ASSERT_FALSE( executionPlan.tempFileLifecyclePlanned );
    ASSERT_FALSE( executionPlan.sampleReadOwned );
    ASSERT_FALSE( executionPlan.wavHeaderWriteOwned );
    ASSERT_FALSE( executionPlan.wavSampleWriteOwned );
    ASSERT_FALSE( executionPlan.tempFileOpenOwned );
    ASSERT_FALSE( executionPlan.tempFileFinalizeOwned );
    ASSERT_FALSE( executionPlan.cleanupOwned );
    ASSERT_FALSE( executionPlan.extractionProcessOwned );
    ASSERT_FALSE( executionPlan.tempFileLifecycleReady );
    ASSERT_FALSE( executionPlan.extractionExecutionReady );
    ASSERT_TRUE( executionPlan.videoOnlyFallbackReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.source-audio.wav"),
               std::string(executionPlan.plannedAudioPath
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("source-audio-extraction-exec-source=source-audio-extraction-execution-contract source-audio-extraction-exec-path=C:/renders/M16-1327.source-audio.wav source-audio-extraction-exec-format=wav-pcm-s16le source-audio-extraction-exec-prereq-contract-ready=true source-audio-extraction-exec-known=false source-audio-extraction-exec-present=false source-audio-extraction-exec-path-planned=false source-audio-extraction-exec-path-ready=true source-audio-extraction-exec-sample-read-planned=false source-audio-extraction-exec-wav-write-planned=false source-audio-extraction-exec-temp-lifecycle-planned=false source-audio-extraction-exec-sample-read-owned=false source-audio-extraction-exec-wav-header-owned=false source-audio-extraction-exec-wav-sample-owned=false source-audio-extraction-exec-temp-open-owned=false source-audio-extraction-exec-temp-finalize-owned=false source-audio-extraction-exec-cleanup-owned=false source-audio-extraction-exec-process-owned=false source-audio-extraction-exec-temp-lifecycle-ready=false source-audio-extraction-exec-ready=false source-audio-extraction-exec-video-only-ready=true source-audio-extraction-exec-contract-ready=true source-audio-extraction-exec-reason=none"),
               std::string(batchRenderedVideoSourceAudioExtractionExecutionPlanSummary(
                   executionPlan).toUtf8().constData()) );

    sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:\\clips\\M16-1327.MLV"),
            true,
            2,
            48000,
            16,
            123456);
    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:/renders/M16-1327.source-audio.wav"));
    executionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    ASSERT_TRUE( executionPlan.contractReady );
    ASSERT_TRUE( executionPlan.extractionPrerequisiteContractReady );
    ASSERT_TRUE( executionPlan.sourceAudioKnown );
    ASSERT_TRUE( executionPlan.sourceAudioPresent );
    ASSERT_TRUE( executionPlan.extractionPathPlanned );
    ASSERT_TRUE( executionPlan.extractionPathReady );
    ASSERT_TRUE( executionPlan.sampleReadPlanned );
    ASSERT_TRUE( executionPlan.wavWritePlanned );
    ASSERT_TRUE( executionPlan.tempFileLifecyclePlanned );
    ASSERT_FALSE( executionPlan.sampleReadOwned );
    ASSERT_FALSE( executionPlan.wavHeaderWriteOwned );
    ASSERT_FALSE( executionPlan.wavSampleWriteOwned );
    ASSERT_FALSE( executionPlan.tempFileOpenOwned );
    ASSERT_FALSE( executionPlan.tempFileFinalizeOwned );
    ASSERT_FALSE( executionPlan.cleanupOwned );
    ASSERT_FALSE( executionPlan.extractionProcessOwned );
    ASSERT_FALSE( executionPlan.tempFileLifecycleReady );
    ASSERT_FALSE( executionPlan.extractionExecutionReady );
    ASSERT_TRUE( executionPlan.videoOnlyFallbackReady );

    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QString());
    executionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    ASSERT_FALSE( executionPlan.contractReady );
    ASSERT_FALSE( executionPlan.extractionPrerequisiteContractReady );
    ASSERT_TRUE( executionPlan.sourceAudioKnown );
    ASSERT_TRUE( executionPlan.sourceAudioPresent );
    ASSERT_TRUE( executionPlan.extractionPathPlanned );
    ASSERT_FALSE( executionPlan.extractionPathReady );
    ASSERT_TRUE( executionPlan.sampleReadPlanned );
    ASSERT_TRUE( executionPlan.wavWritePlanned );
    ASSERT_FALSE( executionPlan.tempFileLifecyclePlanned );
    ASSERT_FALSE( executionPlan.tempFileLifecycleReady );
    ASSERT_FALSE( executionPlan.extractionExecutionReady );
    ASSERT_EQ( std::string("rendered source audio extraction prerequisite contract unavailable"),
               std::string(executionPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoFfmpegAudioInputHandoffContract)
{
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild(
            QStringLiteral("C:\\clips\\M16-1327.MLV"));
    BatchRenderedVideoSourceAudioExtractionPlan extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:\\renders\\M16-1327.source-audio.wav"));
    BatchRenderedVideoSourceAudioExtractionExecutionPlan executionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    BatchRenderedVideoFfmpegAudioInputHandoffPlan handoffPlan =
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            executionPlan);

    ASSERT_TRUE( handoffPlan.contractReady );
    ASSERT_TRUE( handoffPlan.extractionExecutionContractReady );
    ASSERT_FALSE( handoffPlan.sourceAudioKnown );
    ASSERT_FALSE( handoffPlan.sourceAudioPresent );
    ASSERT_FALSE( handoffPlan.extractionPathPlanned );
    ASSERT_TRUE( handoffPlan.extractionPathReady );
    ASSERT_FALSE( handoffPlan.extractionExecutionReady );
    ASSERT_FALSE( handoffPlan.tempFileLifecycleReady );
    ASSERT_FALSE( handoffPlan.cleanupOwned );
    ASSERT_FALSE( handoffPlan.audioInputPlanned );
    ASSERT_FALSE( handoffPlan.inputArgumentsPlanned );
    ASSERT_FALSE( handoffPlan.extractionOutputOwned );
    ASSERT_FALSE( handoffPlan.audioInputOwnershipPlanned );
    ASSERT_FALSE( handoffPlan.audioInputArgumentHandoffPlanned );
    ASSERT_FALSE( handoffPlan.audioInputOwned );
    ASSERT_FALSE( handoffPlan.audioInputArgumentHandoffOwned );
    ASSERT_FALSE( handoffPlan.audioInputHandoffReady );
    ASSERT_TRUE( handoffPlan.videoOnlyFallbackReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.source-audio.wav"),
               std::string(handoffPlan.plannedAudioPath
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(handoffPlan.activeAudioArguments
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-audio-input-handoff-source=ffmpeg-audio-input-handoff-contract ffmpeg-audio-input-handoff-path=C:/renders/M16-1327.source-audio.wav ffmpeg-audio-input-handoff-planned-args=unspecified ffmpeg-audio-input-handoff-active-args=-an ffmpeg-audio-input-handoff-extraction-exec-contract-ready=true ffmpeg-audio-input-handoff-known=false ffmpeg-audio-input-handoff-present=false ffmpeg-audio-input-handoff-path-planned=false ffmpeg-audio-input-handoff-path-ready=true ffmpeg-audio-input-handoff-extraction-ready=false ffmpeg-audio-input-handoff-temp-lifecycle-ready=false ffmpeg-audio-input-handoff-cleanup-owned=false ffmpeg-audio-input-handoff-planned=false ffmpeg-audio-input-handoff-args-planned=false ffmpeg-audio-input-handoff-output-owned=false ffmpeg-audio-input-handoff-ownership-planned=false ffmpeg-audio-input-handoff-args-handoff-planned=false ffmpeg-audio-input-handoff-owned=false ffmpeg-audio-input-handoff-args-handoff-owned=false ffmpeg-audio-input-handoff-ready=false ffmpeg-audio-input-handoff-video-only-ready=true ffmpeg-audio-input-handoff-contract-ready=true ffmpeg-audio-input-handoff-reason=none"),
               std::string(batchRenderedVideoFfmpegAudioInputHandoffPlanSummary(
                   handoffPlan).toUtf8().constData()) );

    sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:\\clips\\M16-1327.MLV"),
            true,
            2,
            48000,
            16,
            123456);
    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:/renders/M16-1327.source-audio.wav"));
    executionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    handoffPlan =
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            executionPlan);
    ASSERT_TRUE( handoffPlan.contractReady );
    ASSERT_TRUE( handoffPlan.sourceAudioKnown );
    ASSERT_TRUE( handoffPlan.sourceAudioPresent );
    ASSERT_TRUE( handoffPlan.extractionPathPlanned );
    ASSERT_TRUE( handoffPlan.extractionPathReady );
    ASSERT_FALSE( handoffPlan.extractionExecutionReady );
    ASSERT_FALSE( handoffPlan.tempFileLifecycleReady );
    ASSERT_FALSE( handoffPlan.cleanupOwned );
    ASSERT_TRUE( handoffPlan.audioInputPlanned );
    ASSERT_TRUE( handoffPlan.inputArgumentsPlanned );
    ASSERT_FALSE( handoffPlan.extractionOutputOwned );
    ASSERT_TRUE( handoffPlan.audioInputOwnershipPlanned );
    ASSERT_TRUE( handoffPlan.audioInputArgumentHandoffPlanned );
    ASSERT_FALSE( handoffPlan.audioInputOwned );
    ASSERT_FALSE( handoffPlan.audioInputArgumentHandoffOwned );
    ASSERT_FALSE( handoffPlan.audioInputHandoffReady );
    ASSERT_EQ( std::string("-i \"C:/renders/M16-1327.source-audio.wav\""),
               std::string(handoffPlan.plannedInputArguments
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(handoffPlan.activeAudioArguments
                   .toUtf8().constData()) );

    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QString());
    executionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    handoffPlan =
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            executionPlan);
    ASSERT_FALSE( handoffPlan.contractReady );
    ASSERT_FALSE( handoffPlan.extractionExecutionContractReady );
    ASSERT_TRUE( handoffPlan.sourceAudioKnown );
    ASSERT_TRUE( handoffPlan.sourceAudioPresent );
    ASSERT_TRUE( handoffPlan.audioInputPlanned );
    ASSERT_FALSE( handoffPlan.inputArgumentsPlanned );
    ASSERT_FALSE( handoffPlan.extractionPathReady );
    ASSERT_FALSE( handoffPlan.audioInputHandoffReady );
    ASSERT_EQ( std::string("rendered source audio extraction prerequisite contract unavailable"),
               std::string(handoffPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoFfmpegAudioInputContract)
{
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild(
            QStringLiteral("C:\\clips\\M16-1327.MLV"));
    BatchRenderedVideoSourceAudioExtractionPlan extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:\\renders\\M16-1327.source-audio.wav"));
    BatchRenderedVideoFfmpegAudioInputPlan inputPlan =
        batchRenderedVideoFfmpegAudioInputPlanFromExtraction(
            extractionPlan);

    ASSERT_TRUE( inputPlan.contractReady );
    ASSERT_TRUE( inputPlan.sourceAudioExtractionContractReady );
    ASSERT_FALSE( inputPlan.sourceAudioKnown );
    ASSERT_FALSE( inputPlan.sourceAudioPresent );
    ASSERT_FALSE( inputPlan.extractionPathPlanned );
    ASSERT_TRUE( inputPlan.extractionPathReady );
    ASSERT_FALSE( inputPlan.extractionReady );
    ASSERT_FALSE( inputPlan.tempFileOwned );
    ASSERT_FALSE( inputPlan.cleanupOwned );
    ASSERT_FALSE( inputPlan.audioInputPlanned );
    ASSERT_FALSE( inputPlan.audioInputOwned );
    ASSERT_FALSE( inputPlan.audioInputReady );
    ASSERT_TRUE( inputPlan.videoOnlyFallbackReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.source-audio.wav"),
               std::string(inputPlan.plannedAudioPath
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(inputPlan.activeAudioArguments
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-audio-input-source=ffmpeg-audio-input-contract ffmpeg-audio-input-path=C:/renders/M16-1327.source-audio.wav ffmpeg-audio-input-planned-args=unspecified ffmpeg-audio-input-active-args=-an ffmpeg-audio-input-extraction-contract-ready=true ffmpeg-audio-input-known=false ffmpeg-audio-input-present=false ffmpeg-audio-input-path-planned=false ffmpeg-audio-input-path-ready=true ffmpeg-audio-input-extraction-ready=false ffmpeg-audio-input-temp-file-owned=false ffmpeg-audio-input-cleanup-owned=false ffmpeg-audio-input-planned=false ffmpeg-audio-input-owned=false ffmpeg-audio-input-ready=false ffmpeg-audio-input-video-only-ready=true ffmpeg-audio-input-contract-ready=true ffmpeg-audio-input-reason=none"),
               std::string(batchRenderedVideoFfmpegAudioInputPlanSummary(
                   inputPlan).toUtf8().constData()) );

    sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:\\clips\\M16-1327.MLV"),
            true,
            2,
            48000,
            16,
            123456);
    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:/renders/M16-1327.source-audio.wav"));
    inputPlan =
        batchRenderedVideoFfmpegAudioInputPlanFromExtraction(
            extractionPlan);
    ASSERT_TRUE( inputPlan.contractReady );
    ASSERT_TRUE( inputPlan.audioInputPlanned );
    ASSERT_FALSE( inputPlan.extractionReady );
    ASSERT_FALSE( inputPlan.tempFileOwned );
    ASSERT_FALSE( inputPlan.cleanupOwned );
    ASSERT_FALSE( inputPlan.audioInputOwned );
    ASSERT_FALSE( inputPlan.audioInputReady );
    ASSERT_EQ( std::string("-i \"C:/renders/M16-1327.source-audio.wav\""),
               std::string(inputPlan.plannedInputArguments
                   .toUtf8().constData()) );

    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QString());
    inputPlan =
        batchRenderedVideoFfmpegAudioInputPlanFromExtraction(
            extractionPlan);
    ASSERT_FALSE( inputPlan.contractReady );
    ASSERT_TRUE( inputPlan.audioInputPlanned );
    ASSERT_FALSE( inputPlan.extractionPathReady );
    ASSERT_EQ( std::string("rendered source audio extraction prerequisite contract unavailable"),
               std::string(inputPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoAudioMuxPrerequisitesContract)
{
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild(
            QStringLiteral("C:\\clips\\M16-1327.MLV"));
    BatchRenderedVideoAudioMuxPrerequisitesPlan audioMuxPlan =
        batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
            sourceAudioPlan);

    ASSERT_TRUE( audioMuxPlan.contractReady );
    ASSERT_TRUE( audioMuxPlan.sourceAudioContractReady );
    ASSERT_TRUE( audioMuxPlan.sourceAudioInputContractReady );
    ASSERT_TRUE( audioMuxPlan.videoOnlyFallbackReady );
    ASSERT_FALSE( audioMuxPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( audioMuxPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( audioMuxPlan.audioInputOwned );
    ASSERT_FALSE( audioMuxPlan.audioMuxOwned );
    ASSERT_FALSE( audioMuxPlan.audioSyncValidationOwned );
    ASSERT_FALSE( audioMuxPlan.muxReady );
    ASSERT_EQ( std::string("audio-mux-prerequisite-contract"),
               std::string(audioMuxPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("rendered-source-audio-discovery"),
               std::string(audioMuxPlan.inputState.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-audio-input-or-video-only-fallback"),
               std::string(audioMuxPlan.outputState.toUtf8().constData()) );
    ASSERT_TRUE( audioMuxPlan.sourceAudioExtractionContractReady );
    ASSERT_EQ( std::string("audio-mux-source=audio-mux-prerequisite-contract audio-mux-input=rendered-source-audio-discovery audio-mux-output=ffmpeg-audio-input-or-video-only-fallback audio-mux-source-contract-ready=true audio-mux-extraction-contract-ready=true audio-mux-audio-input-contract-ready=true audio-mux-video-only-ready=true audio-mux-source-discovery-owned=false audio-mux-extraction-owned=false audio-mux-input-owned=false audio-mux-mux-owned=false audio-mux-sync-owned=false audio-mux-ready=false audio-mux-contract-ready=true audio-mux-reason=none"),
               std::string(batchRenderedVideoAudioMuxPrerequisitesPlanSummary(
                   audioMuxPlan).toUtf8().constData()) );

    sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:\\clips\\M16-1327.MLV"),
            true,
            2,
            48000,
            16,
            123456);
    BatchRenderedVideoSourceAudioExtractionPlan extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:/renders/M16-1327.source-audio.wav"));
    audioMuxPlan =
        batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
            sourceAudioPlan,
            extractionPlan);
    ASSERT_TRUE( audioMuxPlan.contractReady );
    ASSERT_TRUE( audioMuxPlan.sourceAudioContractReady );
    ASSERT_TRUE( audioMuxPlan.sourceAudioExtractionContractReady );
    ASSERT_TRUE( audioMuxPlan.sourceAudioInputContractReady );
    ASSERT_TRUE( audioMuxPlan.videoOnlyFallbackReady );
    ASSERT_TRUE( audioMuxPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( audioMuxPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( audioMuxPlan.audioInputOwned );
    ASSERT_FALSE( audioMuxPlan.audioMuxOwned );
    ASSERT_FALSE( audioMuxPlan.audioSyncValidationOwned );
    ASSERT_FALSE( audioMuxPlan.muxReady );
    ASSERT_EQ( std::string("audio-mux-source=audio-mux-prerequisite-contract audio-mux-input=rendered-source-audio-discovery audio-mux-output=ffmpeg-audio-input-or-video-only-fallback audio-mux-source-contract-ready=true audio-mux-extraction-contract-ready=true audio-mux-audio-input-contract-ready=true audio-mux-video-only-ready=true audio-mux-source-discovery-owned=true audio-mux-extraction-owned=false audio-mux-input-owned=false audio-mux-mux-owned=false audio-mux-sync-owned=false audio-mux-ready=false audio-mux-contract-ready=true audio-mux-reason=none"),
               std::string(batchRenderedVideoAudioMuxPrerequisitesPlanSummary(
                   audioMuxPlan).toUtf8().constData()) );

    BatchRenderedVideoSourceAudioPlan invalidSourceAudioPlan;
    invalidSourceAudioPlan.reason =
        QStringLiteral("source audio contract unavailable");
    audioMuxPlan =
        batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
            invalidSourceAudioPlan);
    ASSERT_FALSE( audioMuxPlan.contractReady );
    ASSERT_FALSE( audioMuxPlan.sourceAudioContractReady );
    ASSERT_FALSE( audioMuxPlan.sourceAudioExtractionContractReady );
    ASSERT_FALSE( audioMuxPlan.sourceAudioInputContractReady );
    ASSERT_FALSE( audioMuxPlan.videoOnlyFallbackReady );
    ASSERT_FALSE( audioMuxPlan.muxReady );
    ASSERT_EQ( std::string("source audio contract unavailable"),
               std::string(audioMuxPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoAudioMuxExecutionContract)
{
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild(
            QStringLiteral("C:\\clips\\M16-1327.MLV"));
    BatchRenderedVideoSourceAudioExtractionPlan extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:/renders/M16-1327.source-audio.wav"));
    BatchRenderedVideoSourceAudioExtractionExecutionPlan extractionExecutionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    BatchRenderedVideoFfmpegAudioInputHandoffPlan handoffPlan =
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            extractionExecutionPlan);
    BatchRenderedVideoFfmpegAudioInputPlan inputPlan =
        batchRenderedVideoFfmpegAudioInputPlanFromHandoff(
            extractionPlan,
            handoffPlan);
    BatchRenderedVideoAudioMuxPrerequisitesPlan prerequisitesPlan =
        batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
            sourceAudioPlan,
            extractionPlan,
            inputPlan);
    BatchRenderedVideoAudioMuxExecutionPlan executionPlan =
        batchRenderedVideoAudioMuxExecutionPlanFromContracts(
            sourceAudioPlan,
            handoffPlan,
            prerequisitesPlan);

    ASSERT_TRUE( executionPlan.contractReady );
    ASSERT_TRUE( executionPlan.sourceAudioContractReady );
    ASSERT_TRUE( executionPlan.audioInputHandoffContractReady );
    ASSERT_TRUE( executionPlan.audioMuxPrerequisitesContractReady );
    ASSERT_FALSE( executionPlan.sourceAudioKnown );
    ASSERT_FALSE( executionPlan.sourceAudioPresent );
    ASSERT_FALSE( executionPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( executionPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( executionPlan.audioInputHandoffReady );
    ASSERT_FALSE( executionPlan.tempAudioInputOwned );
    ASSERT_FALSE( executionPlan.audioMuxPlanned );
    ASSERT_FALSE( executionPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_FALSE( executionPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( executionPlan.audioMuxOwned );
    ASSERT_FALSE( executionPlan.audioMuxArgumentHandoffOwned );
    ASSERT_FALSE( executionPlan.audioSyncValidationOwned );
    ASSERT_FALSE( executionPlan.audioMuxReady );
    ASSERT_FALSE( executionPlan.audioSyncValidationReady );
    ASSERT_FALSE( executionPlan.muxExecutionReady );
    ASSERT_TRUE( executionPlan.videoOnlyFallbackReady );
    ASSERT_EQ( std::string("audio-mux-execution-contract"),
               std::string(executionPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-audio-input-or-video-only-fallback"),
               std::string(executionPlan.inputState.toUtf8().constData()) );
    ASSERT_EQ( std::string("synced-rendered-audio-or-video-only-fallback"),
               std::string(executionPlan.outputState.toUtf8().constData()) );
    ASSERT_EQ( std::string("audio-mux-exec-source=audio-mux-execution-contract audio-mux-exec-input=ffmpeg-audio-input-or-video-only-fallback audio-mux-exec-output=synced-rendered-audio-or-video-only-fallback audio-mux-exec-source-contract-ready=true audio-mux-exec-handoff-contract-ready=true audio-mux-exec-prereq-contract-ready=true audio-mux-exec-known=false audio-mux-exec-present=false audio-mux-exec-discovery-owned=false audio-mux-exec-extraction-owned=false audio-mux-exec-handoff-ready=false audio-mux-exec-temp-input-owned=false audio-mux-exec-mux-planned=false audio-mux-exec-args-handoff-planned=false audio-mux-exec-sync-planned=false audio-mux-exec-mux-owned=false audio-mux-exec-args-handoff-owned=false audio-mux-exec-sync-owned=false audio-mux-exec-mux-ready=false audio-mux-exec-sync-ready=false audio-mux-exec-ready=false audio-mux-exec-video-only-ready=true audio-mux-exec-contract-ready=true audio-mux-exec-reason=none"),
               std::string(batchRenderedVideoAudioMuxExecutionPlanSummary(
                   executionPlan).toUtf8().constData()) );

    sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:\\clips\\M16-1327.MLV"),
            true,
            2,
            48000,
            16,
            123456);
    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:/renders/M16-1327.source-audio.wav"));
    extractionExecutionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    handoffPlan =
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            extractionExecutionPlan);
    inputPlan =
        batchRenderedVideoFfmpegAudioInputPlanFromHandoff(
            extractionPlan,
            handoffPlan);
    prerequisitesPlan =
        batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
            sourceAudioPlan,
            extractionPlan,
            inputPlan);
    executionPlan =
        batchRenderedVideoAudioMuxExecutionPlanFromContracts(
            sourceAudioPlan,
            handoffPlan,
            prerequisitesPlan);
    ASSERT_TRUE( executionPlan.contractReady );
    ASSERT_TRUE( executionPlan.sourceAudioKnown );
    ASSERT_TRUE( executionPlan.sourceAudioPresent );
    ASSERT_TRUE( executionPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( executionPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( executionPlan.audioInputHandoffReady );
    ASSERT_FALSE( executionPlan.tempAudioInputOwned );
    ASSERT_TRUE( executionPlan.audioMuxPlanned );
    ASSERT_TRUE( executionPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_TRUE( executionPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( executionPlan.audioMuxOwned );
    ASSERT_FALSE( executionPlan.audioMuxArgumentHandoffOwned );
    ASSERT_FALSE( executionPlan.audioSyncValidationOwned );
    ASSERT_FALSE( executionPlan.audioMuxReady );
    ASSERT_FALSE( executionPlan.audioSyncValidationReady );
    ASSERT_FALSE( executionPlan.muxExecutionReady );
    ASSERT_EQ( std::string("audio-mux-exec-source=audio-mux-execution-contract audio-mux-exec-input=ffmpeg-audio-input-or-video-only-fallback audio-mux-exec-output=synced-rendered-audio-or-video-only-fallback audio-mux-exec-source-contract-ready=true audio-mux-exec-handoff-contract-ready=true audio-mux-exec-prereq-contract-ready=true audio-mux-exec-known=true audio-mux-exec-present=true audio-mux-exec-discovery-owned=true audio-mux-exec-extraction-owned=false audio-mux-exec-handoff-ready=false audio-mux-exec-temp-input-owned=false audio-mux-exec-mux-planned=true audio-mux-exec-args-handoff-planned=true audio-mux-exec-sync-planned=true audio-mux-exec-mux-owned=false audio-mux-exec-args-handoff-owned=false audio-mux-exec-sync-owned=false audio-mux-exec-mux-ready=false audio-mux-exec-sync-ready=false audio-mux-exec-ready=false audio-mux-exec-video-only-ready=true audio-mux-exec-contract-ready=true audio-mux-exec-reason=none"),
               std::string(batchRenderedVideoAudioMuxExecutionPlanSummary(
                   executionPlan).toUtf8().constData()) );

    BatchRenderedVideoAudioMuxPrerequisitesPlan invalidPrerequisitesPlan;
    invalidPrerequisitesPlan.reason =
        QStringLiteral("audio mux prerequisites unavailable");
    executionPlan =
        batchRenderedVideoAudioMuxExecutionPlanFromContracts(
            sourceAudioPlan,
            handoffPlan,
            invalidPrerequisitesPlan);
    ASSERT_FALSE( executionPlan.contractReady );
    ASSERT_FALSE( executionPlan.audioMuxPrerequisitesContractReady );
    ASSERT_TRUE( executionPlan.audioMuxPlanned );
    ASSERT_EQ( std::string("audio mux prerequisites unavailable"),
               std::string(executionPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoFfmpegAudioContract)
{
    BatchRenderedVideoSourceAudioPlan sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanForCurrentBuild(
            QStringLiteral("C:/clips/M16-1327.MLV"));
    BatchRenderedVideoAudioMuxPrerequisitesPlan audioMuxPlan =
        batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
            sourceAudioPlan);
    BatchRenderedVideoSourceAudioExtractionPlan extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan);
    BatchRenderedVideoSourceAudioExtractionExecutionPlan extractionExecutionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    BatchRenderedVideoFfmpegAudioInputHandoffPlan handoffPlan =
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            extractionExecutionPlan);
    BatchRenderedVideoAudioMuxExecutionPlan audioMuxExecutionPlan =
        batchRenderedVideoAudioMuxExecutionPlanFromContracts(
            sourceAudioPlan,
            handoffPlan,
            audioMuxPlan);
    BatchRenderedVideoFfmpegAudioPlan audioPlan =
        batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
            sourceAudioPlan,
            audioMuxPlan,
            audioMuxExecutionPlan);

    ASSERT_TRUE( audioMuxPlan.contractReady );
    ASSERT_TRUE( audioMuxExecutionPlan.contractReady );
    ASSERT_TRUE( audioPlan.contractReady );
    ASSERT_TRUE( audioPlan.videoOnlyCommandReady );
    ASSERT_TRUE( audioPlan.audioInputContractReady );
    ASSERT_TRUE( audioPlan.audioMuxExecutionContractReady );
    ASSERT_FALSE( audioPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( audioPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( audioPlan.audioInputOwned );
    ASSERT_FALSE( audioPlan.audioMuxPlanned );
    ASSERT_FALSE( audioPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_FALSE( audioPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( audioPlan.audioMuxOwned );
    ASSERT_FALSE( audioPlan.audioMuxArgumentHandoffOwned );
    ASSERT_FALSE( audioPlan.audioSyncOwned );
    ASSERT_FALSE( audioPlan.audioMuxExecutionReady );
    ASSERT_FALSE( audioPlan.muxedAudioCommandPlanned );
    ASSERT_FALSE( audioPlan.muxedAudioCommandReady );
    ASSERT_EQ( std::string("video-only-to-mux-transition-contract"),
               std::string(audioPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(audioPlan.audioArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string(""),
               std::string(audioPlan.muxTransitionArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-audio-source=video-only-to-mux-transition-contract ffmpeg-audio-args=-an ffmpeg-audio-mux-transition-args=unspecified ffmpeg-audio-video-only-ready=true ffmpeg-audio-source-discovery-owned=false ffmpeg-audio-extraction-owned=false ffmpeg-audio-input-contract-ready=true ffmpeg-audio-input-owned=false ffmpeg-audio-mux-exec-contract-ready=true ffmpeg-audio-mux-planned=false ffmpeg-audio-mux-args-handoff-planned=false ffmpeg-audio-sync-planned=false ffmpeg-audio-mux-owned=false ffmpeg-audio-mux-args-handoff-owned=false ffmpeg-audio-sync-owned=false ffmpeg-audio-mux-exec-ready=false ffmpeg-audio-mux-command-planned=false ffmpeg-audio-mux-command-ready=false ffmpeg-audio-contract-ready=true ffmpeg-audio-reason=none"),
               std::string(batchRenderedVideoFfmpegAudioPlanSummary(
                   audioPlan).toUtf8().constData()) );

    sourceAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            true,
            2,
            48000,
            16,
            123456);
    extractionPlan =
        batchRenderedVideoSourceAudioExtractionPlanFromSourceAudio(
            sourceAudioPlan,
            QStringLiteral("C:/renders/M16-1327.source-audio.wav"));
    extractionExecutionPlan =
        batchRenderedVideoSourceAudioExtractionExecutionPlanFromExtraction(
            extractionPlan);
    handoffPlan =
        batchRenderedVideoFfmpegAudioInputHandoffPlanFromExtractionExecution(
            extractionExecutionPlan);
    BatchRenderedVideoFfmpegAudioInputPlan audioInputPlan =
        batchRenderedVideoFfmpegAudioInputPlanFromHandoff(
            extractionPlan,
            handoffPlan);
    audioMuxPlan =
        batchRenderedVideoAudioMuxPrerequisitesPlanFromSourceAudio(
            sourceAudioPlan,
            extractionPlan,
            audioInputPlan);
    audioMuxExecutionPlan =
        batchRenderedVideoAudioMuxExecutionPlanFromContracts(
            sourceAudioPlan,
            handoffPlan,
            audioMuxPlan);
    audioPlan =
        batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
            sourceAudioPlan,
            audioMuxPlan,
            audioMuxExecutionPlan);
    ASSERT_TRUE( audioMuxPlan.contractReady );
    ASSERT_TRUE( audioMuxExecutionPlan.contractReady );
    ASSERT_TRUE( audioPlan.contractReady );
    ASSERT_TRUE( audioPlan.videoOnlyCommandReady );
    ASSERT_TRUE( audioPlan.audioInputContractReady );
    ASSERT_TRUE( audioPlan.audioMuxExecutionContractReady );
    ASSERT_TRUE( audioPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( audioPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( audioPlan.audioInputOwned );
    ASSERT_TRUE( audioPlan.audioMuxPlanned );
    ASSERT_TRUE( audioPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_TRUE( audioPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( audioPlan.audioMuxOwned );
    ASSERT_FALSE( audioPlan.audioMuxArgumentHandoffOwned );
    ASSERT_FALSE( audioPlan.audioSyncOwned );
    ASSERT_FALSE( audioPlan.audioMuxExecutionReady );
    ASSERT_TRUE( audioPlan.muxedAudioCommandPlanned );
    ASSERT_FALSE( audioPlan.muxedAudioCommandReady );
    ASSERT_EQ( std::string("-an"),
               std::string(audioPlan.audioArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("deferred-until-audio-mux-execution-owned"),
               std::string(audioPlan.muxTransitionArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-audio-source=video-only-to-mux-transition-contract ffmpeg-audio-args=-an ffmpeg-audio-mux-transition-args=deferred-until-audio-mux-execution-owned ffmpeg-audio-video-only-ready=true ffmpeg-audio-source-discovery-owned=true ffmpeg-audio-extraction-owned=false ffmpeg-audio-input-contract-ready=true ffmpeg-audio-input-owned=false ffmpeg-audio-mux-exec-contract-ready=true ffmpeg-audio-mux-planned=true ffmpeg-audio-mux-args-handoff-planned=true ffmpeg-audio-sync-planned=true ffmpeg-audio-mux-owned=false ffmpeg-audio-mux-args-handoff-owned=false ffmpeg-audio-sync-owned=false ffmpeg-audio-mux-exec-ready=false ffmpeg-audio-mux-command-planned=true ffmpeg-audio-mux-command-ready=false ffmpeg-audio-contract-ready=true ffmpeg-audio-reason=none"),
               std::string(batchRenderedVideoFfmpegAudioPlanSummary(
                   audioPlan).toUtf8().constData()) );

    BatchRenderedVideoAudioMuxPrerequisitesPlan invalidAudioMuxPlan;
    invalidAudioMuxPlan.reason =
        QStringLiteral("audio mux prerequisites unavailable");
    audioPlan =
        batchRenderedVideoFfmpegAudioPlanForCurrentBuild(
            sourceAudioPlan,
            invalidAudioMuxPlan);
    ASSERT_FALSE( audioPlan.contractReady );
    ASSERT_FALSE( audioPlan.videoOnlyCommandReady );
    ASSERT_EQ( std::string("audio mux prerequisites unavailable"),
               std::string(audioPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoFfmpegFrameGeometryFromGuiState)
{
    BatchRenderedVideoFfmpegFramePlan plan =
        batchRenderedVideoFfmpegFramePlanFromGuiState(
            5792,
            3872,
            24000.0 / 1001.0,
            STRETCH_H_100,
            STRETCH_V_100,
            false,
            0,
            0,
            false,
            BatchRenderedVideoEncoderProfile::H264);
    ASSERT_TRUE( plan.ready );
    ASSERT_EQ( 5792, plan.outputWidth );
    ASSERT_EQ( 3872, plan.outputHeight );
    ASSERT_EQ( std::string("23.976"),
               std::string(plan.frameRateArgument.toUtf8().constData()) );
    ASSERT_EQ( std::string("5792x3872"),
               std::string(plan.frameSizeArgument.toUtf8().constData()) );
    ASSERT_FALSE( plan.scaled );
    ASSERT_FALSE( plan.codecDimensionAdjusted );
    ASSERT_EQ( std::string("ffmpeg-frame-source=5792x3872 ffmpeg-frame-size=5792x3872 ffmpeg-frame-rate=23.976 ffmpeg-frame-resize=false ffmpeg-frame-resize-height-locked=false ffmpeg-frame-stretch=false ffmpeg-frame-codec-dimension-adjusted=false ffmpeg-frame-scaled=false ffmpeg-frame-ready=true ffmpeg-frame-reason=none"),
               std::string(batchRenderedVideoFfmpegFramePlanSummary(plan).toUtf8().constData()) );

    plan = batchRenderedVideoFfmpegFramePlanFromGuiState(
        1000,
        500,
        25.0,
        STRETCH_H_200,
        STRETCH_V_100,
        true,
        1920,
        0,
        true,
        BatchRenderedVideoEncoderProfile::H264);
    ASSERT_TRUE( plan.ready );
    ASSERT_EQ( 1920, plan.outputWidth );
    ASSERT_EQ( 480, plan.outputHeight );
    ASSERT_TRUE( plan.resizeEnabled );
    ASSERT_TRUE( plan.resizeHeightLocked );
    ASSERT_TRUE( plan.scaled );

    plan = batchRenderedVideoFfmpegFramePlanFromGuiState(
        1000,
        501,
        25.0,
        STRETCH_H_100,
        STRETCH_V_033,
        false,
        0,
        0,
        false,
        BatchRenderedVideoEncoderProfile::H264);
    ASSERT_TRUE( plan.ready );
    ASSERT_EQ( 3000, plan.outputWidth );
    ASSERT_EQ( 502, plan.outputHeight );
    ASSERT_TRUE( plan.stretchApplied );
    ASSERT_TRUE( plan.codecDimensionAdjusted );

    plan = batchRenderedVideoFfmpegFramePlanFromGuiState(
        100,
        100,
        25.0,
        STRETCH_H_125,
        STRETCH_V_033,
        false,
        0,
        0,
        false,
        BatchRenderedVideoEncoderProfile::ProRes422HQ);
    ASSERT_TRUE( plan.ready );
    ASSERT_EQ( 375, plan.outputWidth );
    ASSERT_EQ( 100, plan.outputHeight );

    plan = batchRenderedVideoFfmpegFramePlanFromGuiState(
        std::numeric_limits<int>::max(),
        100,
        25.0,
        STRETCH_H_125,
        STRETCH_V_033,
        false,
        0,
        0,
        false,
        BatchRenderedVideoEncoderProfile::H264);
    ASSERT_FALSE( plan.ready );
    ASSERT_EQ( std::string("rendered stretch dimensions invalid or too large"),
               std::string(plan.reason.toUtf8().constData()) );

    plan = batchRenderedVideoFfmpegFramePlanFromGuiState(
        1001,
        501,
        25.0,
        STRETCH_H_100,
        STRETCH_V_100,
        false,
        0,
        0,
        false,
        BatchRenderedVideoEncoderProfile::ProRes422HQ);
    ASSERT_TRUE( plan.ready );
    ASSERT_EQ( 1001, plan.outputWidth );
    ASSERT_EQ( 501, plan.outputHeight );
    ASSERT_FALSE( plan.codecDimensionAdjusted );

    plan = batchRenderedVideoFfmpegFramePlanFromGuiState(
        0,
        500,
        25.0,
        STRETCH_H_100,
        STRETCH_V_100,
        false,
        0,
        0,
        false,
        BatchRenderedVideoEncoderProfile::H264);
    ASSERT_FALSE( plan.ready );
    ASSERT_EQ( std::string("rendered source dimensions invalid"),
               std::string(plan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoFrameProcessingContract)
{
    BatchRenderedVideoSourceMetadata metadata =
        batchRenderedVideoSourceMetadata(
            5792,
            3872,
            24000.0 / 1001.0,
            STRETCH_H_100,
            STRETCH_V_100);
    BatchRenderedVideoFfmpegFramePlan framePlan =
        batchRenderedVideoFfmpegFramePlanFromMetadata(
            metadata,
            batchRenderedVideoDefaultRenderSettings(),
            BatchRenderedVideoEncoderProfile::H264);
    BatchRenderedVideoReceiptApplicationPlan receiptPlan =
        batchRenderedVideoReceiptApplicationPlanFromContracts(
            metadata,
            framePlan);
    ASSERT_TRUE( receiptPlan.contractReady );
    ASSERT_TRUE( receiptPlan.sourceMetadataReady );
    ASSERT_TRUE( receiptPlan.frameGeometryReady );
    ASSERT_TRUE( receiptPlan.inputContractReady );
    ASSERT_TRUE( receiptPlan.outputContractReady );
    ASSERT_FALSE( receiptPlan.applyToMlvOwned );
    ASSERT_FALSE( receiptPlan.processingObjectMutationOwned );
    ASSERT_FALSE( receiptPlan.cacheInvalidationOwned );
    ASSERT_FALSE( receiptPlan.cutStretchStateOwned );
    ASSERT_FALSE( receiptPlan.lookAssistApplicationOwned );
    ASSERT_FALSE( receiptPlan.receiptValidationOwned );
    ASSERT_FALSE( receiptPlan.applicationReady );
    ASSERT_EQ( std::string("receipt-application-input-output-contract"),
               std::string(receiptPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("open-mlv-runtime-plus-batch-receipt"),
               std::string(receiptPlan.inputState.toUtf8().constData()) );
    ASSERT_EQ( std::string("receipt-applied-mlv-processing-state"),
               std::string(receiptPlan.outputState.toUtf8().constData()) );
    ASSERT_EQ( std::string("receipt-application-source=receipt-application-input-output-contract receipt-application-input=open-mlv-runtime-plus-batch-receipt receipt-application-output=receipt-applied-mlv-processing-state receipt-application-metadata-ready=true receipt-application-frame-geometry-ready=true receipt-application-input-contract-ready=true receipt-application-output-contract-ready=true receipt-application-apply-owned=false receipt-application-processing-object-owned=false receipt-application-cache-invalidation-owned=false receipt-application-cut-stretch-owned=false receipt-application-look-assist-owned=false receipt-application-validation-owned=false receipt-application-ready=false receipt-application-contract-ready=true receipt-application-reason=none"),
               std::string(batchRenderedVideoReceiptApplicationPlanSummary(
                   receiptPlan).toUtf8().constData()) );
    BatchRenderedVideoFrameProcessingPlan processingPlan =
        batchRenderedVideoFrameProcessingPlanFromFramePlan(
            metadata,
            framePlan,
            receiptPlan);

    ASSERT_TRUE( processingPlan.contractReady );
    ASSERT_TRUE( processingPlan.sourceMetadataReady );
    ASSERT_TRUE( processingPlan.frameGeometryReady );
    ASSERT_TRUE( processingPlan.receiptApplicationContractReady );
    ASSERT_TRUE( processingPlan.debayerContractReady );
    ASSERT_TRUE( processingPlan.previewProcessingContractReady );
    ASSERT_TRUE( processingPlan.resizeProcessingContractReady );
    ASSERT_TRUE( processingPlan.rgb48FrameBufferContractReady );
    ASSERT_TRUE( processingPlan.frameIterationContractReady );
    ASSERT_FALSE( processingPlan.receiptApplicationOwned );
    ASSERT_FALSE( processingPlan.debayerOwned );
    ASSERT_FALSE( processingPlan.previewProcessingOwned );
    ASSERT_FALSE( processingPlan.resizeProcessingOwned );
    ASSERT_FALSE( processingPlan.rgb48FrameBufferOwned );
    ASSERT_FALSE( processingPlan.frameIterationOwned );
    ASSERT_FALSE( processingPlan.processingParityValidationOwned );
    ASSERT_FALSE( processingPlan.processingParityReady );
    ASSERT_FALSE( processingPlan.frameProcessingReady );
    ASSERT_EQ( std::string("rgb48"),
               std::string(processingPlan.rawFramePixelFormat.toUtf8().constData()) );
    ASSERT_EQ( std::string("5792x3872"),
               std::string(processingPlan.outputSize.toUtf8().constData()) );
    ASSERT_EQ( std::string("render-processing-source=headless-rendered-frame-contract render-processing-pix-fmt=rgb48 render-processing-output-size=5792x3872 render-processing-metadata-ready=true render-processing-frame-geometry-ready=true render-processing-receipt-contract-ready=true render-processing-debayer-contract-ready=true render-processing-preview-contract-ready=true render-processing-resize-contract-ready=true render-processing-rgb48-buffer-contract-ready=true render-processing-frame-iteration-contract-ready=true render-processing-receipt-owned=false render-processing-debayer-owned=false render-processing-preview-owned=false render-processing-resize-owned=false render-processing-rgb48-buffer-owned=false render-processing-frame-iteration-owned=false render-processing-parity-validation-owned=false render-processing-parity-ready=false render-processing-frame-ready=false render-processing-contract-ready=true render-processing-reason=none"),
               std::string(batchRenderedVideoFrameProcessingPlanSummary(
                   processingPlan).toUtf8().constData()) );

    metadata = batchRenderedVideoSourceMetadata(
        0,
        3872,
        24000.0 / 1001.0,
        STRETCH_H_100,
        STRETCH_V_100);
    receiptPlan = batchRenderedVideoReceiptApplicationPlanFromContracts(
        metadata,
        framePlan);
    processingPlan = batchRenderedVideoFrameProcessingPlanFromFramePlan(
        metadata,
        framePlan,
        receiptPlan);
    ASSERT_FALSE( receiptPlan.contractReady );
    ASSERT_FALSE( receiptPlan.sourceMetadataReady );
    ASSERT_EQ( std::string("rendered source dimensions invalid"),
               std::string(receiptPlan.reason.toUtf8().constData()) );
    ASSERT_FALSE( processingPlan.contractReady );
    ASSERT_FALSE( processingPlan.sourceMetadataReady );
    ASSERT_FALSE( processingPlan.receiptApplicationContractReady );
    ASSERT_FALSE( processingPlan.debayerContractReady );
    ASSERT_FALSE( processingPlan.previewProcessingContractReady );
    ASSERT_FALSE( processingPlan.resizeProcessingContractReady );
    ASSERT_FALSE( processingPlan.rgb48FrameBufferContractReady );
    ASSERT_FALSE( processingPlan.frameIterationContractReady );
    ASSERT_EQ( std::string("rendered source dimensions invalid"),
               std::string(processingPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoFfmpegBinaryResolution)
{
    BatchRenderedVideoFfmpegBinaryPlan defaultPlan =
        batchRenderedVideoFfmpegBinaryPlanFromRequestedName();
    ASSERT_FALSE( defaultPlan.pathSearchOwned );
    ASSERT_FALSE( defaultPlan.pathSearchAttempted );
    ASSERT_FALSE( defaultPlan.foundOnPath );
    ASSERT_TRUE( defaultPlan.commandExecutableReady );
    ASSERT_EQ( std::string("ffmpeg"),
               std::string(defaultPlan.resolvedExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-binary-source=default-executable-name ffmpeg-binary-request=ffmpeg ffmpeg-binary-resolved=ffmpeg ffmpeg-binary-path-search-owned=false ffmpeg-binary-path-search-attempted=false ffmpeg-binary-found=false ffmpeg-binary-command-ready=true ffmpeg-binary-reason=none"),
               std::string(batchRenderedVideoFfmpegBinaryPlanSummary(
                   defaultPlan).toUtf8().constData()) );

    BatchRenderedVideoFfmpegBinaryPlan requestedPlan =
        batchRenderedVideoFfmpegBinaryPlanFromRequestedName(
            QStringLiteral("C:/tools/ffmpeg-custom.exe"));
    ASSERT_FALSE( requestedPlan.pathSearchOwned );
    ASSERT_FALSE( requestedPlan.pathSearchAttempted );
    ASSERT_FALSE( requestedPlan.foundOnPath );
    ASSERT_TRUE( requestedPlan.commandExecutableReady );
    ASSERT_EQ( std::string("requested-executable"),
               std::string(requestedPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/tools/ffmpeg-custom.exe"),
               std::string(requestedPlan.requestedExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/tools/ffmpeg-custom.exe"),
               std::string(requestedPlan.resolvedExecutable.toUtf8().constData()) );

    BatchRenderedVideoFfmpegBinaryPlan foundPlan =
        batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(
            QStringLiteral("ffmpeg"),
            QStringLiteral("C:/tools/ffmpeg.exe"));
    ASSERT_TRUE( foundPlan.pathSearchOwned );
    ASSERT_TRUE( foundPlan.pathSearchAttempted );
    ASSERT_TRUE( foundPlan.foundOnPath );
    ASSERT_TRUE( foundPlan.commandExecutableReady );
    ASSERT_EQ( std::string("C:/tools/ffmpeg.exe"),
               std::string(foundPlan.resolvedExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-binary-source=path-search ffmpeg-binary-request=ffmpeg ffmpeg-binary-resolved=C:/tools/ffmpeg.exe ffmpeg-binary-path-search-owned=true ffmpeg-binary-path-search-attempted=true ffmpeg-binary-found=true ffmpeg-binary-command-ready=true ffmpeg-binary-reason=none"),
               std::string(batchRenderedVideoFfmpegBinaryPlanSummary(
                   foundPlan).toUtf8().constData()) );

    BatchRenderedVideoFfmpegBinaryPlan missingPlan =
        batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(
            QStringLiteral("ffmpeg"),
            QString());
    ASSERT_TRUE( missingPlan.pathSearchOwned );
    ASSERT_TRUE( missingPlan.pathSearchAttempted );
    ASSERT_FALSE( missingPlan.foundOnPath );
    ASSERT_TRUE( missingPlan.commandExecutableReady );
    ASSERT_EQ( std::string("ffmpeg"),
               std::string(missingPlan.resolvedExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg executable not found on PATH"),
               std::string(missingPlan.reason.toUtf8().constData()) );

    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoJobPlan basePlan =
        batchRenderedVideoJobPlanFromRequest(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders"),
            request,
            batchRenderedVideoDefaultRenderSettings(),
            foundPlan);
    BatchRenderedVideoSourceMetadata metadata =
        batchRenderedVideoSourceMetadata(
            5792,
            3872,
            24000.0 / 1001.0,
            STRETCH_H_100,
            STRETCH_V_100);
    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(basePlan, metadata);
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );
    ASSERT_TRUE( plan.ffmpegCommandReady );
    ASSERT_EQ( std::string("C:/tools/ffmpeg.exe"),
               std::string(plan.ffmpegCommandPlan.executable.toUtf8().constData()) );
    ASSERT_TRUE( std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData())
        .find("ffmpeg-binary-source=path-search ffmpeg-binary-request=ffmpeg ffmpeg-binary-resolved=C:/tools/ffmpeg.exe ffmpeg-binary-path-search-owned=true ffmpeg-binary-path-search-attempted=true ffmpeg-binary-found=true ffmpeg-binary-command-ready=true") != std::string::npos );

    basePlan = batchRenderedVideoJobPlanFromRequest(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders"),
        request,
        batchRenderedVideoDefaultRenderSettings(),
        missingPlan);
    plan = batchRenderedVideoJobPlanWithMetadata(basePlan, metadata);
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );
    ASSERT_TRUE( plan.ffmpegCommandReady );
    ASSERT_FALSE( plan.ffmpegBinaryPlan.foundOnPath );
    ASSERT_TRUE( std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData())
        .find("ffmpeg-binary-found=false ffmpeg-binary-command-ready=true ffmpeg-binary-reason=ffmpeg executable not found on PATH") != std::string::npos );

    basePlan = batchRenderedVideoJobPlanFromRequest(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders"),
        request,
        batchRenderedVideoDefaultRenderSettings(),
        requestedPlan);
    plan = batchRenderedVideoJobPlanWithMetadata(basePlan, metadata);
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );
    ASSERT_TRUE( plan.ffmpegCommandReady );
    ASSERT_EQ( std::string("C:/tools/ffmpeg-custom.exe"),
               std::string(plan.ffmpegBinaryPlan.requestedExecutable
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/tools/ffmpeg-custom.exe"),
               std::string(plan.ffmpegCommandPlan.executable
                   .toUtf8().constData()) );
    ASSERT_TRUE( std::string(batchRenderedVideoJobPlanSummary(plan)
        .toUtf8().constData())
        .find("ffmpeg-binary-source=requested-executable ffmpeg-binary-request=C:/tools/ffmpeg-custom.exe ffmpeg-binary-resolved=C:/tools/ffmpeg-custom.exe") != std::string::npos );
}

TEST(BatchExportFormat, PlansRenderedVideoMediaProbeBinaryResolution)
{
    BatchRenderedVideoMediaProbeBinaryPlan defaultPlan =
        batchRenderedVideoMediaProbeBinaryPlanFromRequestedName();
    ASSERT_FALSE( defaultPlan.pathSearchOwned );
    ASSERT_FALSE( defaultPlan.pathSearchAttempted );
    ASSERT_FALSE( defaultPlan.foundOnPath );
    ASSERT_TRUE( defaultPlan.commandExecutableReady );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(defaultPlan.resolvedExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("output-verification-probe-binary-source=default-executable-name output-verification-probe-binary-request=ffprobe output-verification-probe-binary-resolved=ffprobe output-verification-probe-binary-path-search-owned=false output-verification-probe-binary-path-search-attempted=false output-verification-probe-binary-found=false output-verification-probe-binary-command-ready=true output-verification-probe-binary-reason=none"),
               std::string(batchRenderedVideoMediaProbeBinaryPlanSummary(
                   defaultPlan).toUtf8().constData()) );

    BatchRenderedVideoMediaProbeBinaryPlan foundPlan =
        batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
            QStringLiteral("ffprobe"),
            QStringLiteral("C:/tools/ffprobe.exe"));
    ASSERT_TRUE( foundPlan.pathSearchOwned );
    ASSERT_TRUE( foundPlan.pathSearchAttempted );
    ASSERT_TRUE( foundPlan.foundOnPath );
    ASSERT_TRUE( foundPlan.commandExecutableReady );
    ASSERT_EQ( std::string("C:/tools/ffprobe.exe"),
               std::string(foundPlan.resolvedExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("output-verification-probe-binary-source=path-search output-verification-probe-binary-request=ffprobe output-verification-probe-binary-resolved=C:/tools/ffprobe.exe output-verification-probe-binary-path-search-owned=true output-verification-probe-binary-path-search-attempted=true output-verification-probe-binary-found=true output-verification-probe-binary-command-ready=true output-verification-probe-binary-reason=none"),
               std::string(batchRenderedVideoMediaProbeBinaryPlanSummary(
                   foundPlan).toUtf8().constData()) );

    BatchRenderedVideoMediaProbeBinaryPlan missingPlan =
        batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
            QStringLiteral("ffprobe"),
            QString());
    ASSERT_TRUE( missingPlan.pathSearchOwned );
    ASSERT_TRUE( missingPlan.pathSearchAttempted );
    ASSERT_FALSE( missingPlan.foundOnPath );
    ASSERT_TRUE( missingPlan.commandExecutableReady );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(missingPlan.resolvedExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffprobe executable not found on PATH"),
               std::string(missingPlan.reason.toUtf8().constData()) );

    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoSourceMetadata metadata =
        batchRenderedVideoSourceMetadata(
            5792,
            3872,
            24000.0 / 1001.0,
            STRETCH_H_100,
            STRETCH_V_100);
    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request,
                1,
                batchRenderedVideoDefaultRenderSettings(),
                batchRenderedVideoFfmpegBinaryPlanFromRequestedName(),
                foundPlan),
            metadata);
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );
    ASSERT_TRUE( plan.mediaProbeCommandReady );
    ASSERT_EQ( std::string("C:/tools/ffprobe.exe"),
               std::string(plan.outputVerificationExecutionPlan
                   .mediaProbeExecutable.toUtf8().constData()) );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.mediaProbeFoundOnPath );
    ASSERT_TRUE( std::string(batchRenderedVideoJobPlanSummary(plan)
        .toUtf8().constData())
        .find("output-verification-probe-binary-source=path-search output-verification-probe-binary-request=ffprobe output-verification-probe-binary-resolved=C:/tools/ffprobe.exe output-verification-probe-binary-path-search-owned=true output-verification-probe-binary-path-search-attempted=true output-verification-probe-binary-found=true output-verification-probe-binary-command-ready=true") != std::string::npos );
    ASSERT_TRUE( std::string(batchRenderedVideoJobPlanSummary(plan)
        .toUtf8().constData())
        .find("output-verification-exec-probe=C:/tools/ffprobe.exe output-verification-exec-probe-binary-source=path-search output-verification-exec-probe-binary-request=ffprobe output-verification-exec-probe-binary-resolved=C:/tools/ffprobe.exe") != std::string::npos );

    plan = batchRenderedVideoJobPlanWithMetadata(
        batchRenderedVideoJobPlanFromRequest(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders"),
            request,
            1,
            batchRenderedVideoDefaultRenderSettings(),
            batchRenderedVideoFfmpegBinaryPlanFromRequestedName(),
            missingPlan),
        metadata);
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );
    ASSERT_TRUE( plan.mediaProbeCommandReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbeFoundOnPath );
    ASSERT_TRUE( std::string(batchRenderedVideoJobPlanSummary(plan)
        .toUtf8().constData())
        .find("output-verification-probe-binary-found=false output-verification-probe-binary-command-ready=true output-verification-probe-binary-reason=ffprobe executable not found on PATH") != std::string::npos );
}

/* BATCH-FFMPEG-DEFAULT-APPDIR-LOOKUP-1: the DEFAULT request (no --rendered-ffmpeg at all) looks
 * in the application directory first, by the exact packaged file name, then makes one plain PATH
 * lookup; any explicit request -- even the bare default name -- keeps the PATH-only lookup. The
 * fake resolver never touches the real PATH or the real app dir: an empty search-dir list means
 * "PATH", anything else is the app-dir lookup. Every call is logged so a test can prove WHICH
 * places were searched, not only what came back. */
static BatchRenderedVideoExecutableResolver appDirLookupFakeResolver(
    const QString & appDirHit,
    const QString & pathHit,
    QStringList * calls)
{
    return [appDirHit, pathHit, calls](const QString & name,
                                       const QStringList & searchDirs) -> QString
    {
        if( calls )
            calls->append(name + QStringLiteral("@")
                + (searchDirs.isEmpty()
                    ? QStringLiteral("PATH")
                    : searchDirs.join(QLatin1Char(';'))));
        return searchDirs.isEmpty() ? pathHit : appDirHit;
    };
}

static std::string appDirLookupText(const QString & value)
{
    return std::string(value.toUtf8().constData());
}

/* The packaged file name is hard-coded here, never computed with the production helper, so a
 * helper that picks a PATHEXT sibling (.cmd, .com) reds these tests. */
static QString appDirLookupPackagedName(const QString & bareName)
{
#ifdef Q_OS_WIN
    if( bareName == QStringLiteral("ffmpeg") )
        return QStringLiteral("ffmpeg.exe");
    if( bareName == QStringLiteral("ffprobe") )
        return QStringLiteral("ffprobe.exe");
    return bareName + QStringLiteral(".exe");
#else
    return bareName;
#endif
}

static QString appDirLookupCall(const QString & bareName, const QString & dir)
{
    return appDirLookupPackagedName(bareName) + QStringLiteral("@") + dir;
}

static bool appDirLookupWriteFile(const QString & path, const QByteArray & bytes)
{
    QFile file(path);
    if( !file.open(QIODevice::WriteOnly) )
        return false;
    const bool ok = file.write(bytes) == bytes.size();
    file.close();
#ifndef Q_OS_WIN
    file.setPermissions(file.permissions() | QFileDevice::ExeOwner | QFileDevice::ExeUser);
#endif
    return ok;
}

TEST(BatchExportFormat, FfmpegDefaultLookupPrefersApplicationDirectory)
{
    const QString appDir = QStringLiteral("C:/apps/MLVApp");
    for( const QString & request : QStringList{ QString(), QStringLiteral("   ") } )
    {
        QStringList calls;
        const BatchRenderedVideoFfmpegBinaryPlan plan =
            batchRenderedVideoFfmpegBinaryPlanFromLookup(
                request,
                appDir,
                appDirLookupFakeResolver(
                    QStringLiteral("C:/apps/MLVApp/ffmpeg.exe"),
                    QStringLiteral("C:/tools/ffmpeg.exe"),
                    &calls));
        ASSERT_EQ( std::string("app-dir"), appDirLookupText(plan.source) );
        ASSERT_EQ( std::string("ffmpeg"), appDirLookupText(plan.requestedExecutable) );
        ASSERT_EQ( std::string("C:/apps/MLVApp/ffmpeg.exe"),
                   appDirLookupText(plan.resolvedExecutable) );
        ASSERT_TRUE( plan.appDirSearchAttempted );
        ASSERT_TRUE( plan.foundInAppDir );
        ASSERT_FALSE( plan.foundOnPath );
        ASSERT_FALSE( plan.pathSearchAttempted );
        ASSERT_TRUE( plan.pathSearchOwned );
        ASSERT_TRUE( plan.commandExecutableReady );
        ASSERT_TRUE( plan.reason.isEmpty() );
        /* App dir hit: PATH is never consulted. */
        ASSERT_EQ( appDirLookupText(appDirLookupCall(QStringLiteral("ffmpeg"), appDir)),
                   appDirLookupText(calls.join(QLatin1Char('|'))) );
        ASSERT_EQ( std::string("ffmpeg-binary-source=app-dir ffmpeg-binary-request=ffmpeg ffmpeg-binary-resolved=C:/apps/MLVApp/ffmpeg.exe ffmpeg-binary-path-search-owned=true ffmpeg-binary-path-search-attempted=false ffmpeg-binary-found=false ffmpeg-binary-command-ready=true ffmpeg-binary-reason=none"),
                   appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSummary(plan)) );
    }
}

/* r6 (split): the fallback is ONE plain PATH lookup, and its hit is taken as the resolver gave
 * it (no read of the file, no continuation): the plan reads exactly as the base PATH plan. */
TEST(BatchExportFormat, FfmpegDefaultLookupFallsBackToPathOnAppDirMiss)
{
    QStringList calls;
    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(),
            QStringLiteral("C:/apps/MLVApp"),
            appDirLookupFakeResolver(
                QString(),
                QStringLiteral("C:/tools/ffmpeg.exe"),
                &calls));
    ASSERT_EQ( appDirLookupText(appDirLookupCall(QStringLiteral("ffmpeg"), QStringLiteral("C:/apps/MLVApp"))
                   + QStringLiteral("|ffmpeg@PATH")),
               appDirLookupText(calls.join(QLatin1Char('|'))) );
    ASSERT_TRUE( plan.appDirSearchAttempted );
    ASSERT_FALSE( plan.foundInAppDir );
    ASSERT_TRUE( plan.foundOnPath );
    /* A PATH hit reads exactly as it did before the app-dir step existed. */
    ASSERT_EQ( appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSummary(
                   batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(
                       QStringLiteral("ffmpeg"),
                       QStringLiteral("C:/tools/ffmpeg.exe")))),
               appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSummary(plan)) );
    ASSERT_EQ( std::string("app-dir,PATH"),
               appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSearchedLocations(plan)) );
}

/* --rendered-ffmpeg ffmpeg is an EXPLICIT request and must not pick the app-dir copy over the
 * PATH one. */
TEST(BatchExportFormat, FfmpegExplicitRequestIgnoresApplicationDirectory)
{
    for( const QString & request : QStringList{
             QStringLiteral("ffmpeg"),
             QStringLiteral("  ffmpeg "),
             QStringLiteral("C:/tools/ffmpeg-custom.exe"),
             QStringLiteral("ffmpeg6"),
             QStringLiteral("ffmpeg.exe") } )
    {
        QStringList calls;
        const QString pathHit = QStringLiteral("C:/tools/ffmpeg-custom.exe");
        const BatchRenderedVideoFfmpegBinaryPlan plan =
            batchRenderedVideoFfmpegBinaryPlanFromLookup(
                request,
                QStringLiteral("C:/apps/MLVApp"),
                appDirLookupFakeResolver(
                    QStringLiteral("C:/apps/MLVApp/ffmpeg.exe"),
                    pathHit,
                    &calls));
        ASSERT_EQ( appDirLookupText(request.trimmed() + QStringLiteral("@PATH")),
                   appDirLookupText(calls.join(QLatin1Char('|'))) );
        ASSERT_FALSE( plan.appDirSearchAttempted );
        ASSERT_FALSE( plan.foundInAppDir );
        ASSERT_EQ( appDirLookupText(pathHit), appDirLookupText(plan.resolvedExecutable) );
        /* Byte for byte the plan the PATH-only lookup built before this card. */
        ASSERT_EQ( appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSummary(
                       batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(
                           request, pathHit))),
                   appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSummary(plan)) );
    }
}

TEST(BatchExportFormat, FfmpegDefaultLookupMissNamesBothPlacesSearched)
{
    QStringList calls;
    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(),
            QStringLiteral("C:/apps/MLVApp"),
            appDirLookupFakeResolver(QString(), QString(), &calls));
    ASSERT_EQ( appDirLookupText(appDirLookupCall(QStringLiteral("ffmpeg"), QStringLiteral("C:/apps/MLVApp"))
                   + QStringLiteral("|ffmpeg@PATH")),
               appDirLookupText(calls.join(QLatin1Char('|'))) );
    ASSERT_EQ( std::string("path-search"), appDirLookupText(plan.source) );
    ASSERT_TRUE( plan.appDirSearchAttempted );
    ASSERT_TRUE( plan.pathSearchAttempted );
    ASSERT_FALSE( plan.foundInAppDir );
    ASSERT_FALSE( plan.foundOnPath );
    ASSERT_EQ( std::string("ffmpeg"), appDirLookupText(plan.resolvedExecutable) );
    ASSERT_TRUE( plan.commandExecutableReady );
    ASSERT_EQ( std::string("ffmpeg executable not found in the application directory (C:/apps/MLVApp) or on PATH"),
               appDirLookupText(plan.reason) );
}

TEST(BatchExportFormat, FfmpegDefaultLookupWithoutApplicationDirectoryIsPathOnly)
{
    QStringList calls;
    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(),
            QString(),
            appDirLookupFakeResolver(
                QStringLiteral("C:/apps/MLVApp/ffmpeg.exe"), QString(), &calls));
    ASSERT_EQ( std::string("ffmpeg@PATH"),
               appDirLookupText(calls.join(QLatin1Char('|'))) );
    ASSERT_FALSE( plan.appDirSearchAttempted );
    ASSERT_EQ( std::string("ffmpeg executable not found on PATH"),
               appDirLookupText(plan.reason) );
}

/* An application directory ending in a legal whitespace-class file-name character such as
 * U+3000 is searched and reported as given. */
TEST(BatchExportFormat, FfmpegDefaultLookupNeverTrimsOsPaths)
{
    const QString appDir = QStringLiteral("C:/apps/MLVApp") + QChar(0x3000);
    const QString appDirExe = appDir + QStringLiteral("/ffmpeg.exe");
    QStringList calls;
    BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(),
            appDir,
            appDirLookupFakeResolver(appDirExe, QString(), &calls));
    ASSERT_EQ( appDirLookupText(appDirLookupCall(QStringLiteral("ffmpeg"), appDir)),
               appDirLookupText(calls.join(QLatin1Char('|'))) );
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_EQ( appDirLookupText(appDirExe), appDirLookupText(plan.resolvedExecutable) );

    calls.clear();
    plan = batchRenderedVideoFfmpegBinaryPlanFromLookup(
        QString(), appDir, appDirLookupFakeResolver(QString(), QString(), &calls));
    ASSERT_EQ( appDirLookupText(QStringLiteral("ffmpeg executable not found in the application directory (")
                   + appDir + QStringLiteral(") or on PATH")),
               appDirLookupText(plan.reason) );
}

/* With the real resolver, a stray ffmpeg.com (any PATHEXT sibling) or an empty placeholder in
 * the application directory never shadows the packaged binary and never counts as an app-dir
 * hit. */
TEST(BatchExportFormat, FfmpegDefaultLookupAppDirNeedsTheExactNonEmptyPackagedFile)
{
    QTemporaryDir dir;
    ASSERT_TRUE( dir.isValid() );
    const QString appDir = QDir::cleanPath(dir.path());
    const QString packaged = appDir + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffmpeg"));
#ifdef Q_OS_WIN
    ASSERT_TRUE( appDirLookupWriteFile(appDir + QStringLiteral("/ffmpeg.com"), QByteArray()) );
    ASSERT_TRUE( appDirLookupWriteFile(appDir + QStringLiteral("/ffmpeg.bat"), QByteArray("@echo off\r\n")) );
#endif
    ASSERT_TRUE( appDirLookupWriteFile(packaged, QByteArray("MZ-not-really-a-program")) );

    BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(), appDir, batchRenderedVideoSystemExecutableResolver());
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_EQ( appDirLookupText(packaged), appDirLookupText(plan.resolvedExecutable) );

    /* An empty packaged file is not a candidate: the app-dir step misses (PATH decides). */
    ASSERT_TRUE( appDirLookupWriteFile(packaged, QByteArray()) );
    plan = batchRenderedVideoFfmpegBinaryPlanFromLookup(
        QString(), appDir, batchRenderedVideoSystemExecutableResolver());
    ASSERT_TRUE( plan.appDirSearchAttempted );
    ASSERT_FALSE( plan.foundInAppDir );
    ASSERT_TRUE( plan.resolvedExecutable != packaged );

    const BatchRenderedVideoMediaProbeBinaryPlan probePlan =
        batchRenderedVideoMediaProbeBinaryPlanFromLookup(
            QString(), appDir, batchRenderedVideoSystemExecutableResolver());
    ASSERT_TRUE( probePlan.appDirSearchAttempted );
    ASSERT_FALSE( probePlan.foundInAppDir );
}

/* An app-dir selection that vanished after the lookup is refused at preflight (usage error,
 * exit 2), not handed to the spawn. */
TEST(BatchExportFormat, FfmpegAppDirSelectionMustStillExistAtPreflight)
{
    QTemporaryDir dir;
    ASSERT_TRUE( dir.isValid() );
    const QString appDir = QDir::cleanPath(dir.path());
    const QString packaged = appDir + QStringLiteral("/ffmpeg.exe");
    ASSERT_TRUE( appDirLookupWriteFile(packaged, QByteArray("MZ")) );

    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(), appDir,
            appDirLookupFakeResolver(packaged, QStringLiteral("C:/tools/ffmpeg.exe"), nullptr));
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_TRUE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );

    ASSERT_TRUE( QFile::rename(packaged, appDir + QStringLiteral("/ffmpeg.old")) );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );

    /* Unchanged rules: a PATH hit is trusted; an unresolved bare name is refused. */
    ASSERT_TRUE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(
        batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(
            QStringLiteral("ffmpeg"), QStringLiteral("C:/tools/ffmpeg.exe"))) );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(
        batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(
            QStringLiteral("ffmpeg"), QString())) );
}

/* An app-dir selection replaced by a DIRECTORY after the lookup is refused at preflight too. */
TEST(BatchExportFormat, FfmpegSelectionReplacedByDirectoryIsRefusedAtPreflight)
{
    QTemporaryDir dir;
    ASSERT_TRUE( dir.isValid() );
    const QString appDir = QDir::cleanPath(dir.path());
    const QString packaged = appDir + QStringLiteral("/ffmpeg.exe");
    ASSERT_TRUE( QDir(appDir).mkdir(QStringLiteral("ffmpeg.exe")) );
    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(), appDir,
            appDirLookupFakeResolver(packaged, QString(), nullptr));
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );
}

/* An app-dir ffmpeg truncated to zero bytes after the lookup selected it is refused at
 * preflight (exit 2). An explicit path keeps the base rule (QFileInfo::exists) byte for byte. */
TEST(BatchExportFormat, FfmpegAppDirSelectionTruncatedAfterLookupIsRefusedAtPreflight)
{
    QTemporaryDir dir;
    ASSERT_TRUE( dir.isValid() );
    const QString appDir = QDir::cleanPath(dir.path());
    const QString packaged = appDir + QStringLiteral("/ffmpeg.exe");
    ASSERT_TRUE( appDirLookupWriteFile(packaged, QByteArray("MZ")) );
    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(), appDir,
            appDirLookupFakeResolver(packaged, QStringLiteral("C:/tools/ffmpeg.exe"), nullptr));
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_TRUE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );

    ASSERT_TRUE( appDirLookupWriteFile(packaged, QByteArray()) );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );
    ASSERT_EQ( std::string("app-dir"),
               appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSearchedLocations(plan)) );

    ASSERT_TRUE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(
        batchRenderedVideoFfmpegBinaryPlanFromRequestedName(packaged)) );
}

/* --rendered-ffmpeg C:/Tools where C:/Tools is an existing DIRECTORY: the base rule for an
 * explicit request, preflight passes (QFileInfo::exists) and the launch fails (exit 4). A
 * missing explicit path is still refused at preflight (exit 2). */
TEST(BatchExportFormat, ExplicitFfmpegNamingAnExistingDirectoryKeepsTheBaseLaunchFailure)
{
    QTemporaryDir toolsTemp;
    ASSERT_TRUE( toolsTemp.isValid() );
    const QString toolsDir = QDir::cleanPath(toolsTemp.path());
    ASSERT_FALSE( QFileInfo::exists(toolsDir + QStringLiteral(".exe")) );
    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            toolsDir, QStringLiteral("C:/apps/MLVApp"), batchRenderedVideoSystemExecutableResolver());
    ASSERT_FALSE( plan.foundInAppDir );
    ASSERT_FALSE( plan.foundOnPath );
    ASSERT_EQ( appDirLookupText(toolsDir), appDirLookupText(plan.resolvedExecutable) );
    ASSERT_TRUE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            toolsDir + QStringLiteral("/missing-ffmpeg.exe"), QStringLiteral("C:/apps/MLVApp"),
            batchRenderedVideoSystemExecutableResolver())) );
}

/* The preflight message names only the places actually searched. */
TEST(BatchExportFormat, FfmpegSearchedLocationsNameOnlyWhatWasSearched)
{
    const QString appDir = QStringLiteral("C:/apps/MLVApp");
    ASSERT_EQ( std::string("app-dir"), appDirLookupText(
        batchRenderedVideoFfmpegBinaryPlanSearchedLocations(
            batchRenderedVideoFfmpegBinaryPlanFromLookup(
                QString(), appDir,
                appDirLookupFakeResolver(QStringLiteral("C:/apps/MLVApp/ffmpeg.exe"), QString(), nullptr)))) );
    ASSERT_EQ( std::string("app-dir,PATH"), appDirLookupText(
        batchRenderedVideoFfmpegBinaryPlanSearchedLocations(
            batchRenderedVideoFfmpegBinaryPlanFromLookup(
                QString(), appDir, appDirLookupFakeResolver(QString(), QString(), nullptr)))) );
    ASSERT_EQ( std::string("PATH"), appDirLookupText(
        batchRenderedVideoFfmpegBinaryPlanSearchedLocations(
            batchRenderedVideoFfmpegBinaryPlanFromLookup(
                QStringLiteral("ffmpeg"), appDir,
                appDirLookupFakeResolver(QString(), QString(), nullptr)))) );
}

/* r6 (split): the optional ffprobe uses a fresh app-dir hit; a stale one (removed or emptied
 * after the lookup) is skipped and the sibling step decides: a non-empty ffprobe beside the
 * resolved ffmpeg, else none. No second PATH lookup is made. */
TEST(BatchExportFormat, OptionalMediaProbeSelectionUsesAFreshAppDirHitThenTheFfmpegSibling)
{
    QTemporaryDir appTemp;
    QTemporaryDir encoderTemp;
    ASSERT_TRUE( appTemp.isValid() );
    ASSERT_TRUE( encoderTemp.isValid() );
    const QString appDir = QDir::cleanPath(appTemp.path());
    const QString encoderDir = QDir::cleanPath(encoderTemp.path());
    const QString appProbe = appDir + QStringLiteral("/ffprobe.exe");
    const QString encoderFfmpeg = encoderDir + QStringLiteral("/ffmpeg.exe");
    const QString encoderProbe = encoderDir + QStringLiteral("/ffprobe.exe");
    ASSERT_TRUE( appDirLookupWriteFile(appProbe, QByteArray("MZ")) );
    ASSERT_TRUE( appDirLookupWriteFile(encoderFfmpeg, QByteArray("MZ")) );

    QStringList calls;
    const BatchRenderedVideoMediaProbeBinaryPlan probePlan =
        batchRenderedVideoMediaProbeBinaryPlanFromLookup(
            QString(), appDir, appDirLookupFakeResolver(appProbe, QString(), &calls));
    const BatchRenderedVideoFfmpegBinaryPlan ffmpegPlan =
        batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(QStringLiteral("ffmpeg"), encoderFfmpeg);
    ASSERT_TRUE( probePlan.foundInAppDir );

    /* Fresh app-dir hit: used. */
    ASSERT_EQ( appDirLookupText(appProbe), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(probePlan, ffmpegPlan)) );

    /* Stale app-dir hit and nothing beside ffmpeg: not available. */
    ASSERT_TRUE( appDirLookupWriteFile(appProbe, QByteArray()) );
    ASSERT_EQ( std::string(), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(probePlan, ffmpegPlan)) );

    /* Stale app-dir hit, an ffprobe beside the resolved ffmpeg: that one. */
    ASSERT_TRUE( appDirLookupWriteFile(encoderProbe, QByteArray("MZ")) );
    ASSERT_EQ( appDirLookupText(encoderProbe), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(probePlan, ffmpegPlan)) );

    /* A PATH hit is used as before (base rule: no re-read). */
    const BatchRenderedVideoMediaProbeBinaryPlan pathProbePlan =
        batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
            QStringLiteral("ffprobe"), QStringLiteral("C:/tools/ffprobe.exe"));
    ASSERT_EQ( std::string("C:/tools/ffprobe.exe"), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(pathProbePlan, ffmpegPlan)) );

    /* The lookup made one app-dir call and no PATH call. */
    ASSERT_EQ( appDirLookupText(appDirLookupCall(QStringLiteral("ffprobe"), appDir)),
               appDirLookupText(calls.join(QLatin1Char('|'))) );
}

#ifdef Q_OS_WIN
/* r7 (sol r6 B3): direct Win32 calls on the literal paths. No cmd.exe (no shell parsing of '&'
 * or '%' in a temp path, no AutoRun) and no child process to wait on. Every link the R6/R7
 * tests make goes through this one helper. */
static bool appDirLookupMakeLink(const QString & link, const QString & target, bool hard)
{
    const std::wstring linkPath = QDir::toNativeSeparators(link).toStdWString();
    const std::wstring targetPath = QDir::toNativeSeparators(target).toStdWString();
    if( hard )
        return CreateHardLinkW(linkPath.c_str(), targetPath.c_str(), nullptr) != 0
            && QFileInfo::exists(link);
#ifndef SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE
#define SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE 0x2
#endif
    /* A file symlink (flag 0). Developer Mode allows it unprivileged with the flag; a Windows
     * build that predates the flag rejects it as an invalid parameter, so ask once without. */
    if( CreateSymbolicLinkW(linkPath.c_str(), targetPath.c_str(),
                            SYMBOLIC_LINK_FLAG_ALLOW_UNPRIVILEGED_CREATE) == 0 )
    {
        if( GetLastError() != ERROR_INVALID_PARAMETER
         || CreateSymbolicLinkW(linkPath.c_str(), targetPath.c_str(), 0) == 0 )
            return false;
    }
    return QFileInfo(link).isSymLink() && QFileInfo::exists(link);
}

/* r7 adversary A9: a real Windows image from this host's own Windows directory (not a
 * hard-coded C:). */
static QString appDirLookupSystemImage()
{
    const QString root = qEnvironmentVariable("SystemRoot", QStringLiteral("C:\\Windows"));
    return QDir::cleanPath(QDir::fromNativeSeparators(root)) + QStringLiteral("/System32/whoami.exe");
}

/* r7 (sol r6 B1, Windows twin of Unix 0755 -> 0644, adversary A1): replace the file's DACL so
 * this account is denied FILE_EXECUTE and allowed everything else (metadata, read, delete), so
 * the file still looks like a non-empty regular file but can no longer be started. */
static bool appDirLookupDenyExecute(const QString & path)
{
    HANDLE token = nullptr;
    if( !OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &token) )
        return false;
    DWORD size = 0;
    GetTokenInformation(token, TokenUser, nullptr, 0, &size);
    std::vector<unsigned char> user(size > 0 ? size : 1);
    const bool gotUser = size > 0
        && GetTokenInformation(token, TokenUser, user.data(), size, &size) != 0;
    CloseHandle(token);
    if( !gotUser )
        return false;
    PSID sid = reinterpret_cast<TOKEN_USER *>(user.data())->User.Sid;
    const DWORD aclSize = sizeof(ACL) + 2 * (sizeof(ACCESS_ALLOWED_ACE) + GetLengthSid(sid));
    std::vector<unsigned char> aclBytes(aclSize);
    PACL acl = reinterpret_cast<PACL>(aclBytes.data());
    SECURITY_DESCRIPTOR descriptor;
    const std::wstring nativePath = QDir::toNativeSeparators(path).toStdWString();
    return InitializeAcl(acl, aclSize, ACL_REVISION) != 0
        && AddAccessDeniedAce(acl, ACL_REVISION, FILE_EXECUTE, sid) != 0
        && AddAccessAllowedAce(acl, ACL_REVISION, FILE_ALL_ACCESS, sid) != 0
        && InitializeSecurityDescriptor(&descriptor, SECURITY_DESCRIPTOR_REVISION) != 0
        && SetSecurityDescriptorDacl(&descriptor, TRUE, acl, FALSE) != 0
        && SetFileSecurityW(nativePath.c_str(), DACL_SECURITY_INFORMATION, &descriptor) != 0;
}

/* A Windows FILE SYMLINK beside MLVApp (hard link when this host cannot make one; the line
 * printed says which) to a real non-empty exe reports QFileInfo size 0, yet it starts: the
 * app-dir lookup and preflight accept it, and the optional ffprobe beside it is found. */
TEST(BatchExportFormat, FfmpegDefaultLookupAcceptsASymlinkedLaunchableFfmpeg)
{
    QTemporaryDir appTemp;
    QTemporaryDir realTemp;
    ASSERT_TRUE( appTemp.isValid() && realTemp.isValid() );
    const QString appDir = QDir::cleanPath(appTemp.path());
    const QString real = QDir::cleanPath(realTemp.path()) + QStringLiteral("/ffmpeg-real.exe");
    ASSERT_TRUE( QFile::copy(appDirLookupSystemImage(), real) );
    const QString link = appDir + QStringLiteral("/ffmpeg.exe");
    const QString probeLink = appDir + QStringLiteral("/ffprobe.exe");
    bool hard = false;
    if( !appDirLookupMakeLink(link, real, false) )
    {
        hard = true;
        ASSERT_TRUE( appDirLookupMakeLink(link, real, true) );
    }
    ASSERT_TRUE( appDirLookupMakeLink(probeLink, real, hard) );
    const QFileInfo li(link);
    std::printf("[F1-MEASURE] link-kind=%s isFile=%d isSymLink=%d size=%lld target-size=%lld\n",
                hard ? "hardlink" : "symlink", li.isFile() ? 1 : 0, li.isSymLink() ? 1 : 0,
                static_cast<long long>(li.size()),
                static_cast<long long>(QFileInfo(real).size()));
    std::fflush(stdout);

    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(), appDir, batchRenderedVideoSystemExecutableResolver());
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_EQ( appDirLookupText(link), appDirLookupText(plan.resolvedExecutable) );
    ASSERT_TRUE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );
    const BatchRenderedVideoMediaProbeBinaryPlan noProbe =
        batchRenderedVideoMediaProbeBinaryPlanFromLookup(
            QString(), appDir, appDirLookupFakeResolver(QString(), QString(), nullptr));
    ASSERT_EQ( appDirLookupText(probeLink), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(noProbe, plan)) );
}

/* r6 adversary A1 (a9dd1d9d), literal input: the app-dir ffmpeg.exe is a file SYMLINK to a real
 * exe; after the lookup, the TARGET is truncated to zero bytes. The link itself always reports
 * size 0, so the size of the file it resolves to decides: preflight refuses it (exit 2) and a
 * fresh lookup no longer counts it as an app-dir hit (PATH decides). */
TEST(BatchExportFormat, R6AppDirSymlinkWhoseTargetWasEmptiedIsRefusedAtPreflight)
{
    QTemporaryDir appTemp;
    QTemporaryDir realTemp;
    ASSERT_TRUE( appTemp.isValid() && realTemp.isValid() );
    const QString appDir = QDir::cleanPath(appTemp.path());
    const QString real = QDir::cleanPath(realTemp.path()) + QStringLiteral("/ffmpeg-real.exe");
    ASSERT_TRUE( QFile::copy(appDirLookupSystemImage(), real) );
    const QString link = appDir + QStringLiteral("/ffmpeg.exe");
    if( !appDirLookupMakeLink(link, real, false) )
        SKIP_TEST("this host cannot create a file symlink (the input needs a symlink, not a hard link)");
    ASSERT_TRUE( QFileInfo(link).isSymLink() );

    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(), appDir, batchRenderedVideoSystemExecutableResolver());
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_TRUE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );

    QFile target(real);
    ASSERT_TRUE( target.open(QIODevice::WriteOnly | QIODevice::Truncate) );
    target.close();
    ASSERT_EQ( static_cast<qint64>(0), QFileInfo(real).size() );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanFromLookup(
        QString(), appDir, batchRenderedVideoSystemExecutableResolver()).foundInAppDir );
}
#endif

/* r6 adversary A2 (a9dd1d9d), literal input: ffmpeg and ffprobe both resolve in the app dir;
 * ffprobe.exe is truncated to zero bytes before the optional probe is chosen and there is no
 * other ffprobe. The stale app-dir hit is skipped, and the sibling step (the same directory)
 * must not take the same empty file back: one rule judges both steps. */
TEST(BatchExportFormat, R6StaleAppDirProbeIsNotTakenBackAsTheFfmpegSibling)
{
    QTemporaryDir appTemp;
    ASSERT_TRUE( appTemp.isValid() );
    const QString appDir = QDir::cleanPath(appTemp.path());
    const QString appFfmpeg = appDir + QStringLiteral("/ffmpeg.exe");
    const QString appProbe = appDir + QStringLiteral("/ffprobe.exe");
    ASSERT_TRUE( appDirLookupWriteFile(appFfmpeg, QByteArray("MZ")) );
    ASSERT_TRUE( appDirLookupWriteFile(appProbe, QByteArray("MZ")) );
    const BatchRenderedVideoFfmpegBinaryPlan ffmpegPlan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(), appDir, appDirLookupFakeResolver(appFfmpeg, QString(), nullptr));
    const BatchRenderedVideoMediaProbeBinaryPlan probePlan =
        batchRenderedVideoMediaProbeBinaryPlanFromLookup(
            QString(), appDir, appDirLookupFakeResolver(appProbe, QString(), nullptr));
    ASSERT_TRUE( ffmpegPlan.foundInAppDir );
    ASSERT_TRUE( probePlan.foundInAppDir );
    ASSERT_EQ( appDirLookupText(appProbe), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(probePlan, ffmpegPlan)) );

    ASSERT_TRUE( appDirLookupWriteFile(appProbe, QByteArray()) );
    ASSERT_EQ( std::string(), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(probePlan, ffmpegPlan)) );
}

TEST(BatchExportFormat, MediaProbeDefaultLookupMirrorsFfmpeg)
{
    const QString appDir = QStringLiteral("C:/apps/MLVApp");
    const QString appDirCall = appDirLookupCall(QStringLiteral("ffprobe"), appDir);

    QStringList calls;
    BatchRenderedVideoMediaProbeBinaryPlan plan =
        batchRenderedVideoMediaProbeBinaryPlanFromLookup(
            QString(),
            appDir,
            appDirLookupFakeResolver(
                QStringLiteral("C:/apps/MLVApp/ffprobe.exe"),
                QStringLiteral("C:/tools/ffprobe.exe"),
                &calls));
    ASSERT_EQ( appDirLookupText(appDirCall),
               appDirLookupText(calls.join(QLatin1Char('|'))) );
    ASSERT_EQ( std::string("app-dir"), appDirLookupText(plan.source) );
    ASSERT_EQ( std::string("ffprobe"), appDirLookupText(plan.requestedExecutable) );
    ASSERT_EQ( std::string("C:/apps/MLVApp/ffprobe.exe"),
               appDirLookupText(plan.resolvedExecutable) );
    ASSERT_TRUE( plan.appDirSearchAttempted );
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_FALSE( plan.foundOnPath );
    ASSERT_FALSE( plan.pathSearchAttempted );
    ASSERT_TRUE( plan.commandExecutableReady );
    ASSERT_TRUE( plan.reason.isEmpty() );

    calls.clear();
    plan = batchRenderedVideoMediaProbeBinaryPlanFromLookup(
        QString(),
        appDir,
        appDirLookupFakeResolver(
            QString(), QStringLiteral("C:/tools/ffprobe.exe"), &calls));
    ASSERT_EQ( appDirLookupText(appDirCall + QStringLiteral("|ffprobe@PATH")),
               appDirLookupText(calls.join(QLatin1Char('|'))) );
    ASSERT_TRUE( plan.foundOnPath );
    ASSERT_FALSE( plan.foundInAppDir );
    ASSERT_EQ( appDirLookupText(batchRenderedVideoMediaProbeBinaryPlanSummary(
                   batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
                       QStringLiteral("ffprobe"),
                       QStringLiteral("C:/tools/ffprobe.exe")))),
               appDirLookupText(batchRenderedVideoMediaProbeBinaryPlanSummary(plan)) );

    for( const QString & request : QStringList{
             QStringLiteral("ffprobe"),
             QStringLiteral("C:/tools/ffprobe-custom.exe") } )
    {
        calls.clear();
        plan = batchRenderedVideoMediaProbeBinaryPlanFromLookup(
            request,
            appDir,
            appDirLookupFakeResolver(
                QStringLiteral("C:/apps/MLVApp/ffprobe.exe"),
                QStringLiteral("C:/tools/ffprobe-custom.exe"),
                &calls));
        ASSERT_EQ( appDirLookupText(request + QStringLiteral("@PATH")),
                   appDirLookupText(calls.join(QLatin1Char('|'))) );
        ASSERT_FALSE( plan.appDirSearchAttempted );
        ASSERT_EQ( appDirLookupText(batchRenderedVideoMediaProbeBinaryPlanSummary(
                       batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
                           request,
                           QStringLiteral("C:/tools/ffprobe-custom.exe")))),
                   appDirLookupText(batchRenderedVideoMediaProbeBinaryPlanSummary(plan)) );
    }

    calls.clear();
    plan = batchRenderedVideoMediaProbeBinaryPlanFromLookup(
        QString(),
        appDir,
        appDirLookupFakeResolver(QString(), QString(), &calls));
    ASSERT_EQ( appDirLookupText(appDirCall + QStringLiteral("|ffprobe@PATH")),
               appDirLookupText(calls.join(QLatin1Char('|'))) );
    ASSERT_EQ( std::string("path-search"), appDirLookupText(plan.source) );
    ASSERT_FALSE( plan.foundOnPath );
    ASSERT_FALSE( plan.foundInAppDir );
    ASSERT_EQ( std::string("ffprobe executable not found in the application directory (C:/apps/MLVApp) or on PATH"),
               appDirLookupText(plan.reason) );
}

/* r6 RED pair, end to end through the *FromCurrentEnvironment wrappers BatchRunner calls (the
 * real QStandardPaths resolver, the real QCoreApplication::applicationDirPath() of this test
 * executable, and a PATH this test sets). The files written beside the test executable and the
 * PATH value are restored on every exit path (a failed ASSERT throws). */
enum class AppDirLookupCreate { Created, AlreadyPresent, Failed };

struct AppDirLookupEnvironmentFixture
{
    /* r7 (sol r6 B2): besideExe holds ONLY the files this fixture created. The create is
     * exclusive (QIODevice::NewOnly: the OS fails it when the name exists), so a file a packager
     * or a concurrent run of this executable placed is never truncated, and since only created
     * names are recorded, never removed either: the caller skips with a reason instead. */
    AppDirLookupCreate createBesideExe(const QString & path, const QByteArray & bytes)
    {
        QFile file(path);
        if( !file.open(QIODevice::WriteOnly | QIODevice::NewOnly) )
        {
            const QFileInfo info(path);
            return ( info.exists() || info.isSymLink() ) ? AppDirLookupCreate::AlreadyPresent
                                                         : AppDirLookupCreate::Failed;
        }
        besideExe << path;
        const bool ok = file.write(bytes) == bytes.size();
        file.close();
#ifndef Q_OS_WIN
        file.setPermissions(file.permissions() | QFileDevice::ExeOwner | QFileDevice::ExeUser);
#endif
        return ok ? AppDirLookupCreate::Created : AppDirLookupCreate::Failed;
    }

    QStringList besideExe;
    QByteArray savedPath;
    bool pathWasSet = false;
    bool pathChanged = false;

    ~AppDirLookupEnvironmentFixture()
    {
        if( pathChanged )
        {
            if( pathWasSet )
                qputenv("PATH", savedPath);
            else
                qunsetenv("PATH");
        }
        /* Safety net for a failed ASSERT (a destructor must not throw): a file that cannot be
         * removed is named on stderr, never left silently. */
        if( !removeBesideExe() )
        {
            for( const QString & path : besideExe )
                std::fprintf(stderr, "[R6-FIXTURE] could not remove %s\n", qPrintable(path));
        }
    }

    /* r6 adversary A3: removal is checked. Each R6 test also ASSERTs it on its normal path.
     * r7: it removes only the files createBesideExe created. */
    bool removeBesideExe()
    {
        QStringList remaining;
        for( const QString & path : besideExe )
        {
            if( QFileInfo::exists(path) && !QFile::remove(path) )
                remaining << path;
        }
        besideExe = remaining;
        return remaining.isEmpty();
    }

    void setPath(const QString & dir)
    {
        pathWasSet = qEnvironmentVariableIsSet("PATH");
        savedPath = qgetenv("PATH");
        pathChanged = true;
        qputenv("PATH", QDir::toNativeSeparators(dir).toLocal8Bit());
    }
};

static QString appDirLookupCanonical(const QString & path)
{
    const QString canonical = QFileInfo(path).canonicalFilePath();
    return canonical.isEmpty() ? QDir::cleanPath(path) : canonical;
}

/* RED on fork/master: the base resolves the default ffmpeg/ffprobe on PATH only, so the PATH
 * copy wins over the copy beside the executable. */
TEST(BatchExportFormat, R6DefaultLookupAppDirCopyWinsOverPathCopy)
{
    const QString appDir = QCoreApplication::applicationDirPath();
    ASSERT_FALSE( appDir.isEmpty() );
    const QString appFfmpeg = QDir::cleanPath(appDir) + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffmpeg"));
    const QString appProbe = QDir::cleanPath(appDir) + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffprobe"));

    QTemporaryDir toolsTemp;
    ASSERT_TRUE( toolsTemp.isValid() );
    const QString tools = QDir::cleanPath(toolsTemp.path());
    const QString pathFfmpeg = tools + QStringLiteral("/") + appDirLookupPackagedName(QStringLiteral("ffmpeg"));
    const QString pathProbe = tools + QStringLiteral("/") + appDirLookupPackagedName(QStringLiteral("ffprobe"));
    ASSERT_TRUE( appDirLookupWriteFile(pathFfmpeg, QByteArray("MZ-path-copy")) );
    ASSERT_TRUE( appDirLookupWriteFile(pathProbe, QByteArray("MZ-path-copy")) );

    /* r7 (sol r6 B2): exclusive create; a name already present (a packaged copy, or a
     * concurrent run of this executable) is never replaced or removed: the test skips. */
    AppDirLookupEnvironmentFixture fixture;
    for( const QString & besideExe : QStringList{ appFfmpeg, appProbe } )
    {
        const AppDirLookupCreate made =
            fixture.createBesideExe(besideExe, QByteArray("MZ-app-dir-copy"));
        if( made == AppDirLookupCreate::AlreadyPresent )
            SKIP_TEST("an ffmpeg or ffprobe already sits beside the test executable (a packaged copy or a concurrent run); this test never replaces or removes a file it did not create");
        ASSERT_TRUE( made == AppDirLookupCreate::Created );
    }
    fixture.setPath(tools);

    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromCurrentEnvironment();
    const BatchRenderedVideoMediaProbeBinaryPlan probePlan =
        batchRenderedVideoMediaProbeBinaryPlanFromCurrentEnvironment();
    ASSERT_EQ( appDirLookupText(appDirLookupCanonical(appFfmpeg)),
               appDirLookupText(appDirLookupCanonical(plan.resolvedExecutable)) );
    ASSERT_EQ( appDirLookupText(appDirLookupCanonical(appProbe)),
               appDirLookupText(appDirLookupCanonical(probePlan.resolvedExecutable)) );
    ASSERT_EQ( std::string("app-dir"), appDirLookupText(plan.source) );
    ASSERT_EQ( std::string("app-dir"), appDirLookupText(probePlan.source) );
    /* Both created files were removed (a name another run creates afterwards is not ours). */
    ASSERT_TRUE( fixture.removeBesideExe() );
    ASSERT_TRUE( fixture.besideExe.isEmpty() );
}

/* No copy beside the executable: the default lookup falls back to the PATH copy (the base's
 * plain findExecutable hit, read the same way), and says it searched both places.
 * r8 seam (sol r7 B3): the body takes two hooks, run after the first absence check and after
 * the lookups, so a concurrent run's create-then-remove can be staged deterministically. The
 * registered test passes no-op hooks. */
static void appDirLookupPathFallbackCheck(const std::function<void()> & afterPreCheck,
                                          const std::function<void()> & afterLookup)
{
    const QString appDir = QCoreApplication::applicationDirPath();
    ASSERT_FALSE( appDir.isEmpty() );
    const QString appFfmpeg = QDir::cleanPath(appDir) + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffmpeg"));
    const QString appProbe = QDir::cleanPath(appDir) + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffprobe"));
    if( QFileInfo::exists(appFfmpeg) || QFileInfo::exists(appProbe) )
        SKIP_TEST("an ffmpeg or ffprobe already sits beside the test executable");
    afterPreCheck();

    QTemporaryDir toolsTemp;
    ASSERT_TRUE( toolsTemp.isValid() );
    const QString tools = QDir::cleanPath(toolsTemp.path());
    const QString pathFfmpeg = tools + QStringLiteral("/") + appDirLookupPackagedName(QStringLiteral("ffmpeg"));
    const QString pathProbe = tools + QStringLiteral("/") + appDirLookupPackagedName(QStringLiteral("ffprobe"));
    ASSERT_TRUE( appDirLookupWriteFile(pathFfmpeg, QByteArray("MZ-path-copy")) );
    ASSERT_TRUE( appDirLookupWriteFile(pathProbe, QByteArray("MZ-path-copy")) );

    AppDirLookupEnvironmentFixture fixture;
    fixture.setPath(tools);
    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromCurrentEnvironment();
    const BatchRenderedVideoMediaProbeBinaryPlan probePlan =
        batchRenderedVideoMediaProbeBinaryPlanFromCurrentEnvironment();
    afterLookup();
    /* r7 (sol r6 B2): this test places nothing beside the executable, so it checks the
     * absence it relies on itself, after the lookups too, and skips (never asserts) when a
     * packager or a concurrent run put a file there meanwhile. */
    if( QFileInfo::exists(appFfmpeg) || QFileInfo::exists(appProbe)
     || QFileInfo(appFfmpeg).isSymLink() || QFileInfo(appProbe).isSymLink() )
        SKIP_TEST("an ffmpeg or ffprobe appeared beside the test executable during the lookup (a packaged copy or a concurrent run); the PATH fallback cannot be judged");
    ASSERT_TRUE( plan.foundOnPath );
    ASSERT_TRUE( probePlan.foundOnPath );
    ASSERT_EQ( appDirLookupText(appDirLookupCanonical(pathFfmpeg)),
               appDirLookupText(appDirLookupCanonical(plan.resolvedExecutable)) );
    ASSERT_EQ( appDirLookupText(appDirLookupCanonical(pathProbe)),
               appDirLookupText(appDirLookupCanonical(probePlan.resolvedExecutable)) );
    ASSERT_EQ( std::string("path-search"), appDirLookupText(plan.source) );
    ASSERT_TRUE( plan.appDirSearchAttempted );
    ASSERT_TRUE( probePlan.appDirSearchAttempted );
    ASSERT_EQ( std::string("app-dir,PATH"),
               appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSearchedLocations(plan)) );
}

TEST(BatchExportFormat, R6DefaultLookupWithoutAppDirCopyFallsBackToPath)
{
    appDirLookupPathFallbackCheck([] {}, [] {});
}

/* r7, sol r6 B1, literal input: the app-dir ffmpeg selected by the lookup can no longer be
 * started when preflight runs. On Unix its mode changed from 0755 to 0644; on Windows (adversary
 * r7 A1) this account was denied FILE_EXECUTE on it. Preflight refuses it (exit 2, the class of
 * a missing binary) instead of passing it to a launch that fails (exit 4). */
TEST(BatchExportFormat, R7AppDirSelectionThatCanNoLongerStartIsRefusedAtPreflight)
{
    QTemporaryDir dir;
    ASSERT_TRUE( dir.isValid() );
    const QString appDir = QDir::cleanPath(dir.path());
#ifdef Q_OS_WIN
    const QString packaged = appDir + QStringLiteral("/ffmpeg.exe");
    ASSERT_TRUE( QFile::copy(appDirLookupSystemImage(), packaged) );
    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(), appDir, batchRenderedVideoSystemExecutableResolver());
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_TRUE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );

    /* FILE_EXECUTE denied after the lookup; the file still reads as a non-empty regular file. */
    ASSERT_TRUE( appDirLookupDenyExecute(packaged) );
    ASSERT_TRUE( QFileInfo(packaged).isFile() );
    ASSERT_TRUE( QFileInfo(packaged).size() > 0 );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );
    ASSERT_EQ( std::string("app-dir"),
               appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSearchedLocations(plan)) );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanFromLookup(
        QString(), appDir, batchRenderedVideoSystemExecutableResolver()).foundInAppDir );
#else
    const QString packaged = appDir + QStringLiteral("/ffmpeg");
    ASSERT_TRUE( appDirLookupWriteFile(packaged, QByteArray("#!/bin/sh\nexit 0\n")) );
    ASSERT_TRUE( QFile::setPermissions(packaged,
        QFileDevice::ReadOwner | QFileDevice::WriteOwner | QFileDevice::ExeOwner
        | QFileDevice::ReadUser | QFileDevice::WriteUser | QFileDevice::ExeUser
        | QFileDevice::ReadGroup | QFileDevice::ExeGroup
        | QFileDevice::ReadOther | QFileDevice::ExeOther) );
    const BatchRenderedVideoFfmpegBinaryPlan plan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            QString(), appDir, batchRenderedVideoSystemExecutableResolver());
    ASSERT_TRUE( plan.foundInAppDir );
    ASSERT_TRUE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );

    /* 0755 -> 0644 after the lookup. */
    ASSERT_TRUE( QFile::setPermissions(packaged,
        QFileDevice::ReadOwner | QFileDevice::WriteOwner
        | QFileDevice::ReadUser | QFileDevice::WriteUser
        | QFileDevice::ReadGroup | QFileDevice::ReadOther) );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanUsableAtPreflight(plan) );
    ASSERT_EQ( std::string("app-dir"),
               appDirLookupText(batchRenderedVideoFfmpegBinaryPlanSearchedLocations(plan)) );
    ASSERT_FALSE( batchRenderedVideoFfmpegBinaryPlanFromLookup(
        QString(), appDir, batchRenderedVideoSystemExecutableResolver()).foundInAppDir );
#endif
}

/* r7, sol r6 B1 sibling sites: the optional ffprobe (the app-dir hit, step 1, and the ffprobe
 * beside the resolved ffmpeg, step 2) is judged by the same rule, so a file that cannot start
 * is never chosen and the ffmpeg-dump verification runs instead. */
TEST(BatchExportFormat, R7OptionalProbeThatCanNoLongerStartIsNotChosen)
{
    QTemporaryDir appTemp;
    QTemporaryDir encoderTemp;
    ASSERT_TRUE( appTemp.isValid() && encoderTemp.isValid() );
    const QString appDir = QDir::cleanPath(appTemp.path());
    const QString encoderDir = QDir::cleanPath(encoderTemp.path());
    const QString encoderFfmpeg = encoderDir + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffmpeg"));
    ASSERT_TRUE( appDirLookupWriteFile(encoderFfmpeg, QByteArray("MZ")) );
    const BatchRenderedVideoFfmpegBinaryPlan ffmpegPlan =
        batchRenderedVideoFfmpegBinaryPlanFromResolvedPath(QStringLiteral("ffmpeg"), encoderFfmpeg);
#ifdef Q_OS_WIN
    /* FILE_EXECUTE denied to this account on both. */
    const QString appProbe = appDir + QStringLiteral("/ffprobe.exe");
    const QString siblingProbe = encoderDir + QStringLiteral("/ffprobe");
    ASSERT_TRUE( appDirLookupWriteFile(appProbe, QByteArray("MZ")) );
    ASSERT_TRUE( appDirLookupWriteFile(siblingProbe, QByteArray("MZ")) );
    ASSERT_TRUE( appDirLookupDenyExecute(appProbe) );
    ASSERT_TRUE( appDirLookupDenyExecute(siblingProbe) );
#else
    const QString appProbe = appDir + QStringLiteral("/ffprobe");
    const QString siblingProbe = encoderDir + QStringLiteral("/ffprobe");
    ASSERT_TRUE( appDirLookupWriteFile(appProbe, QByteArray("#!/bin/sh\nexit 0\n")) );
    ASSERT_TRUE( appDirLookupWriteFile(siblingProbe, QByteArray("#!/bin/sh\nexit 0\n")) );
    const QFileDevice::Permissions noExecute =
        QFileDevice::ReadOwner | QFileDevice::WriteOwner
        | QFileDevice::ReadUser | QFileDevice::WriteUser
        | QFileDevice::ReadGroup | QFileDevice::ReadOther;
    ASSERT_TRUE( QFile::setPermissions(appProbe, noExecute) );
    ASSERT_TRUE( QFile::setPermissions(siblingProbe, noExecute) );
#endif
    const BatchRenderedVideoMediaProbeBinaryPlan probePlan =
        batchRenderedVideoMediaProbeBinaryPlanFromLookup(
            QString(), appDir, appDirLookupFakeResolver(appProbe, QString(), nullptr));
    ASSERT_TRUE( probePlan.foundInAppDir );
    ASSERT_EQ( std::string(), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(probePlan, ffmpegPlan)) );

    /* A sibling that can start is still found. */
    const QString launchable = encoderDir + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffprobe"));
    ASSERT_TRUE( appDirLookupWriteFile(launchable, QByteArray("MZ")) );
    ASSERT_EQ( appDirLookupText(launchable), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(probePlan, ffmpegPlan)) );
}

#ifdef Q_OS_WIN
/* r7 adversary A2, literal input: --rendered-ffmpeg C:/Tools/ffmpeg.exe, a working image saved
 * as C:/Tools/ffprobe (no extension), no ffprobe.exe there and none in PATH or the app dir. The
 * base took it, and it starts (QProcess hands CreateProcess the full path, which gets no .exe
 * appended; measured below with a bounded wait), so it stays the optional ffprobe. */
TEST(BatchExportFormat, R7ExtensionlessFfprobeBesideFfmpegThatStartsIsStillChosen)
{
    QTemporaryDir toolsTemp;
    ASSERT_TRUE( toolsTemp.isValid() );
    const QString tools = QDir::cleanPath(toolsTemp.path());
    const QString ffmpeg = tools + QStringLiteral("/ffmpeg.exe");
    const QString probe = tools + QStringLiteral("/ffprobe");
    ASSERT_TRUE( QFile::copy(appDirLookupSystemImage(), ffmpeg) );
    ASSERT_TRUE( QFile::copy(appDirLookupSystemImage(), probe) );

    QProcess start;
    start.start(probe, QStringList());
    const bool started = start.waitForStarted(10000);
    const bool finished = started && start.waitForFinished(10000);
    if( start.state() != QProcess::NotRunning )
    {
        start.kill();
        start.waitForFinished(5000);
    }
    std::printf("[R7-A2-MEASURE] extensionless image started=%d finished=%d exit=%d\n",
                started ? 1 : 0, finished ? 1 : 0, finished ? start.exitCode() : -1);
    std::fflush(stdout);
    ASSERT_TRUE( started );

    const BatchRenderedVideoFfmpegBinaryPlan ffmpegPlan =
        batchRenderedVideoFfmpegBinaryPlanFromLookup(
            ffmpeg, QStringLiteral("C:/apps/MLVApp"), appDirLookupFakeResolver(QString(), ffmpeg, nullptr));
    const BatchRenderedVideoMediaProbeBinaryPlan noProbe =
        batchRenderedVideoMediaProbeBinaryPlanFromLookup(
            QString(), QStringLiteral("C:/apps/MLVApp"),
            appDirLookupFakeResolver(QString(), QString(), nullptr));
    ASSERT_EQ( appDirLookupText(probe), appDirLookupText(
        batchRenderedVideoOptionalMediaProbeExecutable(noProbe, ffmpegPlan)) );
}
#endif

/* r7, sol r6 B2, literal input: a packager (or a concurrent run of this test executable) put
 * ffmpeg.exe beside console_tests.exe after the precedence test's absence check and before its
 * write. The fixture must create its files exclusively: it never truncates a file it did not
 * create, reports the name as already present (the test skips), and never removes it. */
TEST(BatchExportFormat, R7PrecedenceFixtureNeverOverwritesOrDeletesAFileItDidNotCreate)
{
    QTemporaryDir dir;
    ASSERT_TRUE( dir.isValid() );
    const QString foreign = QDir::cleanPath(dir.path()) + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffmpeg"));
    {
        QFile packager(foreign);
        ASSERT_TRUE( packager.open(QIODevice::WriteOnly) );
        ASSERT_TRUE( packager.write(QByteArray("packager-copy")) == 13 );
        packager.close();
    }
    const QString mine = QDir::cleanPath(dir.path()) + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffprobe"));
    {
        AppDirLookupEnvironmentFixture fixture;
        ASSERT_TRUE( fixture.createBesideExe(foreign, QByteArray("MZ-app-dir-copy"))
                     == AppDirLookupCreate::AlreadyPresent );
        ASSERT_TRUE( fixture.createBesideExe(mine, QByteArray("MZ-app-dir-copy"))
                     == AppDirLookupCreate::Created );
        ASSERT_TRUE( fixture.removeBesideExe() );
    }
    QFile check(foreign);
    ASSERT_TRUE( check.open(QIODevice::ReadOnly) );
    ASSERT_EQ( std::string("packager-copy"), std::string(check.readAll().constData()) );
    ASSERT_FALSE( QFileInfo::exists(mine) );
}

static bool appDirLookupReplaceClosedFile(const QString & path, const QByteArray & bytes)
{
    if( !QFile::remove(path) )
        return false;
    QFile packager(path);
    if( !packager.open(QIODevice::WriteOnly | QIODevice::NewOnly) )
        return false;
    const bool ok = packager.write(bytes) == bytes.size();
    packager.close();
    return ok;
}

static std::string appDirLookupReadAll(const QString & path)
{
    QFile file(path);
    if( !file.open(QIODevice::ReadOnly) )
        return std::string("<cannot open>");
    return file.readAll().toStdString();
}

/* r8, sol r7 B2, literal input: the fixture created ffmpeg.exe and ffprobe.exe exclusively and
 * closed them; a packager then replaced each with its own binary under the same name (the
 * ffmpeg replacement is even the same size as the fixture's bytes). Cleanup must leave both
 * replacements intact on both routes: the explicit removeBesideExe() (ffmpeg) and the
 * destructor (ffprobe). A file the fixture created and nobody touched is still removed. */
TEST(BatchExportFormat, R8PrecedenceFixtureLeavesAFileReplacedAfterItClosedIt)
{
    QTemporaryDir dir;
    ASSERT_TRUE( dir.isValid() );
    const QString root = QDir::cleanPath(dir.path());
    ASSERT_TRUE( QDir().mkpath(root + QStringLiteral("/explicit")) );
    ASSERT_TRUE( QDir().mkpath(root + QStringLiteral("/destructor")) );
    const QString ffmpeg = root + QStringLiteral("/explicit/")
        + appDirLookupPackagedName(QStringLiteral("ffmpeg"));
    const QString untouched = root + QStringLiteral("/explicit/")
        + appDirLookupPackagedName(QStringLiteral("ffprobe"));
    const QString probe = root + QStringLiteral("/destructor/")
        + appDirLookupPackagedName(QStringLiteral("ffprobe"));

    {
        AppDirLookupEnvironmentFixture fixture;
        ASSERT_TRUE( fixture.createBesideExe(ffmpeg, QByteArray("MZ-app-dir-copy"))
                     == AppDirLookupCreate::Created );
        ASSERT_TRUE( fixture.createBesideExe(untouched, QByteArray("MZ-app-dir-copy"))
                     == AppDirLookupCreate::Created );
        ASSERT_TRUE( appDirLookupReplaceClosedFile(ffmpeg, QByteArray("packager-ffmpeg")) );
        /* Nothing of the fixture's own remains: the replacement is not its file. */
        ASSERT_TRUE( fixture.removeBesideExe() );
        ASSERT_TRUE( fixture.besideExe.isEmpty() );
    }
    ASSERT_EQ( std::string("packager-ffmpeg"), appDirLookupReadAll(ffmpeg) );
    ASSERT_FALSE( QFileInfo::exists(untouched) );

    {
        AppDirLookupEnvironmentFixture fixture;
        ASSERT_TRUE( fixture.createBesideExe(probe, QByteArray("MZ-app-dir-copy"))
                     == AppDirLookupCreate::Created );
        ASSERT_TRUE( appDirLookupReplaceClosedFile(probe, QByteArray("packager-ffprobe-binary")) );
    }
    ASSERT_EQ( std::string("packager-ffprobe-binary"), appDirLookupReadAll(probe) );
}

/* r8, sol r7 B3, literal input: process B (the PATH-fallback check) sees neither file beside
 * the executable; process A (a concurrent run of the precedence test) creates both; B resolves
 * them; A removes both before B checks again. B's guard then sees absence on both sides of the
 * lookup. B must skip with a logged reason, never fail its PATH assertions. Staged through the
 * appDirLookupPathFallbackCheck hooks. */
TEST(BatchExportFormat, R8PathFallbackCheckSkipsWhenAConcurrentRunCameAndWentDuringTheLookup)
{
    const QString appDir = QCoreApplication::applicationDirPath();
    ASSERT_FALSE( appDir.isEmpty() );
    const QString appFfmpeg = QDir::cleanPath(appDir) + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffmpeg"));
    const QString appProbe = QDir::cleanPath(appDir) + QStringLiteral("/")
        + appDirLookupPackagedName(QStringLiteral("ffprobe"));
    if( QFileInfo::exists(appFfmpeg) || QFileInfo::exists(appProbe) )
        SKIP_TEST("an ffmpeg or ffprobe already sits beside the test executable; the concurrent run cannot be staged");

    AppDirLookupEnvironmentFixture concurrentRun;
    bool staged = false;
    bool removed = false;
    std::string outcome;
    try
    {
        appDirLookupPathFallbackCheck(
            [&] {
                staged = concurrentRun.createBesideExe(appFfmpeg, QByteArray("MZ-app-dir-copy"))
                             == AppDirLookupCreate::Created
                      && concurrentRun.createBesideExe(appProbe, QByteArray("MZ-app-dir-copy"))
                             == AppDirLookupCreate::Created;
            },
            [&] { removed = concurrentRun.removeBesideExe(); });
        outcome = "passed";
    }
    catch( const minitest::Skip & skip )
    {
        outcome = std::string("skipped: ") + skip.what();
    }
    catch( const minitest::Failure & failure )
    {
        outcome = std::string("failed: ") + failure.what();
    }
    std::printf("[R8-B3] staged=%d removed=%d outcome=%s\n", staged ? 1 : 0, removed ? 1 : 0,
                outcome.c_str());
    if( !staged )
        SKIP_TEST("the concurrent run's files could not be created beside the test executable");
    ASSERT_TRUE( removed );
    ASSERT_FALSE( QFileInfo::exists(appFfmpeg) );
    ASSERT_FALSE( QFileInfo::exists(appProbe) );
    ASSERT_EQ( std::string("skipped"), outcome.substr(0, outcome.find(':')) );
}

#ifdef Q_OS_WIN
/* r7, sol r6 B3, literal input: a temporary directory whose path holds a cmd metacharacter
 * (TEMP=C:\R&D\tmp). The link fixture takes the path literally: the hard link (needs no
 * privilege) is made, and a file symlink is made or refused by this host, never misparsed. */
TEST(BatchExportFormat, R7LinkFixtureTakesATempPathWithAnAmpersandLiterally)
{
    QTemporaryDir base;
    ASSERT_TRUE( base.isValid() );
    const QString dir = QDir::cleanPath(base.path()) + QStringLiteral("/R&D");
    ASSERT_TRUE( QDir().mkpath(dir) );
    const QString real = dir + QStringLiteral("/ffmpeg-real.exe");
    ASSERT_TRUE( QFile::copy(appDirLookupSystemImage(), real) );
    const QString hardLink = dir + QStringLiteral("/ffmpeg.exe");
    ASSERT_TRUE( appDirLookupMakeLink(hardLink, real, true) );
    ASSERT_FALSE( QFileInfo(hardLink).isSymLink() );
    ASSERT_EQ( QFileInfo(real).size(), QFileInfo(hardLink).size() );
    const QString symLink = dir + QStringLiteral("/ffprobe.exe");
    if( appDirLookupMakeLink(symLink, real, false) )
        ASSERT_TRUE( QFileInfo(symLink).isSymLink() );
    else
        ASSERT_FALSE( QFileInfo(symLink).exists() || QFileInfo(symLink).isSymLink() );
    /* A link into a directory that does not exist fails, and returns. */
    ASSERT_FALSE( appDirLookupMakeLink(dir + QStringLiteral("/missing/ffmpeg.exe"), real, true) );
}
#endif

TEST(BatchExportFormat, PlansRenderedVideoMediaProbeCommandShape)
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
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target);
    BatchRenderedVideoMediaProbeCommandPlan commandPlan =
        batchRenderedVideoMediaProbeCommandPlanFromContracts(
            verificationPlan,
            batchRenderedVideoMediaProbeBinaryPlanFromRequestedName());

    ASSERT_TRUE( commandPlan.contractReady );
    ASSERT_TRUE( commandPlan.outputVerificationContractReady );
    ASSERT_TRUE( commandPlan.mediaProbeExecutableReady );
    ASSERT_TRUE( commandPlan.commandPlanned );
    ASSERT_TRUE( commandPlan.commandReady );
    ASSERT_FALSE( commandPlan.executionOwned );
    ASSERT_FALSE( commandPlan.executionReady );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(commandPlan.executable.toUtf8().constData()) );
    ASSERT_EQ( std::string("-v error -show_format -show_streams -of json C:/renders/M16-1327.mp4"),
               std::string(commandPlan.arguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffprobe -v error -show_format -show_streams -of json C:/renders/M16-1327.mp4"),
               std::string(commandPlan.commandLine.toUtf8().constData()) );
    ASSERT_EQ( std::string("output-verification-probe-command-source=output-verification-probe-command-contract output-verification-probe-command-executable=ffprobe output-verification-probe-command-path=C:/renders/M16-1327.mp4 output-verification-probe-command-args=-v error -show_format -show_streams -of json C:/renders/M16-1327.mp4 output-verification-probe-command-line=ffprobe -v error -show_format -show_streams -of json C:/renders/M16-1327.mp4 output-verification-probe-command-output-contract-ready=true output-verification-probe-command-executable-ready=true output-verification-probe-command-planned=true output-verification-probe-command-ready=true output-verification-probe-command-owned=false output-verification-probe-command-exec-ready=false output-verification-probe-command-contract-ready=true output-verification-probe-command-reason=none"),
               std::string(batchRenderedVideoMediaProbeCommandPlanSummary(
                   commandPlan).toUtf8().constData()) );

    BatchRenderedVideoMediaProbeBinaryPlan foundPlan =
        batchRenderedVideoMediaProbeBinaryPlanFromResolvedPath(
            QStringLiteral("ffprobe"),
            QStringLiteral("C:/tools/ffprobe.exe"));
    commandPlan =
        batchRenderedVideoMediaProbeCommandPlanFromContracts(
            verificationPlan,
            foundPlan);
    ASSERT_TRUE( commandPlan.contractReady );
    ASSERT_EQ( std::string("C:/tools/ffprobe.exe"),
               std::string(commandPlan.executable.toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/tools/ffprobe.exe -v error -show_format -show_streams -of json C:/renders/M16-1327.mp4"),
               std::string(commandPlan.commandLine.toUtf8().constData()) );

    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request,
                1,
                batchRenderedVideoDefaultRenderSettings(),
                batchRenderedVideoFfmpegBinaryPlanFromRequestedName(),
                foundPlan),
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100));
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );
    ASSERT_TRUE( plan.mediaProbeCommandContractReady );
    ASSERT_TRUE( plan.mediaProbeResultContractReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan
        .mediaProbeCommandExecutionOwned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan
        .mediaProbeCommandExecutionReady );
    const std::string summary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( summary.find("output-verification-probe-command-executable=C:/tools/ffprobe.exe") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-json-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-json-stdout-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-json-raw-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-json-parse-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-result-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-result-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-line=C:/tools/ffprobe.exe -v error -show_format -show_streams -of json C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-json-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-json-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-result-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-result-ready=false") != std::string::npos );
}

TEST(BatchExportFormat, PlansRenderedVideoMediaProbeJsonContract)
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
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target);
    BatchRenderedVideoMediaProbeCommandPlan commandPlan =
        batchRenderedVideoMediaProbeCommandPlanFromContracts(
            verificationPlan,
            batchRenderedVideoMediaProbeBinaryPlanFromRequestedName());
    BatchRenderedVideoMediaProbeJsonPlan jsonPlan =
        batchRenderedVideoMediaProbeJsonPlanFromCommand(commandPlan);

    ASSERT_TRUE( jsonPlan.contractReady );
    ASSERT_TRUE( jsonPlan.mediaProbeCommandContractReady );
    ASSERT_FALSE( jsonPlan.mediaProbeCommandExecutionOwned );
    ASSERT_FALSE( jsonPlan.mediaProbeCommandExecutionReady );
    ASSERT_TRUE( jsonPlan.stdoutCapturePlanned );
    ASSERT_TRUE( jsonPlan.stderrCapturePlanned );
    ASSERT_TRUE( jsonPlan.exitCodeValidationPlanned );
    ASSERT_TRUE( jsonPlan.timeoutPlanned );
    ASSERT_TRUE( jsonPlan.errorReportingPlanned );
    ASSERT_TRUE( jsonPlan.jsonDocumentPlanned );
    ASSERT_FALSE( jsonPlan.stdoutCaptureOwned );
    ASSERT_FALSE( jsonPlan.stderrCaptureOwned );
    ASSERT_FALSE( jsonPlan.exitCodeValidationOwned );
    ASSERT_FALSE( jsonPlan.timeoutOwned );
    ASSERT_FALSE( jsonPlan.rawJsonOutputOwned );
    ASSERT_FALSE( jsonPlan.jsonParseOwned );
    ASSERT_FALSE( jsonPlan.rawJsonReady );
    ASSERT_FALSE( jsonPlan.jsonParseReady );
    ASSERT_FALSE( jsonPlan.errorFree );
    ASSERT_EQ( std::string("output-verification-probe-json-source=output-verification-probe-json-contract output-verification-probe-json-path=C:/renders/M16-1327.mp4 output-verification-probe-json-command-line=ffprobe -v error -show_format -show_streams -of json C:/renders/M16-1327.mp4 output-verification-probe-json-command-contract-ready=true output-verification-probe-json-command-owned=false output-verification-probe-json-command-exec-ready=false output-verification-probe-json-stdout-planned=true output-verification-probe-json-stderr-planned=true output-verification-probe-json-exit-code-planned=true output-verification-probe-json-timeout-planned=true output-verification-probe-json-error-reporting-planned=true output-verification-probe-json-document-planned=true output-verification-probe-json-stdout-owned=false output-verification-probe-json-stderr-owned=false output-verification-probe-json-exit-code-owned=false output-verification-probe-json-timeout-owned=false output-verification-probe-json-raw-owned=false output-verification-probe-json-parse-owned=false output-verification-probe-json-raw-ready=false output-verification-probe-json-parse-ready=false output-verification-probe-json-error-free=false output-verification-probe-json-contract-ready=true output-verification-probe-json-reason=none"),
               std::string(batchRenderedVideoMediaProbeJsonPlanSummary(
                   jsonPlan).toUtf8().constData()) );

    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request),
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100,
                240));
    ASSERT_TRUE( plan.mediaProbeJsonContractReady );
    ASSERT_TRUE( plan.mediaProbeJsonPlan.stdoutCapturePlanned );
    ASSERT_TRUE( plan.mediaProbeJsonPlan.errorReportingPlanned );
    ASSERT_FALSE( plan.mediaProbeJsonPlan.rawJsonOutputOwned );
    ASSERT_FALSE( plan.mediaProbeJsonPlan.jsonParseOwned );
    ASSERT_FALSE( plan.mediaProbeJsonPlan.rawJsonReady );
    ASSERT_FALSE( plan.mediaProbeJsonPlan.jsonParseReady );
    ASSERT_FALSE( plan.mediaProbeJsonPlan.errorFree );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbeJsonReady );
    const std::string summary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( summary.find("output-verification-probe-json-source=output-verification-probe-json-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-json-document-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-json-raw-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-json-parse-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-json-error-free=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-json-document-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-json-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-json-contract-ready=true") != std::string::npos );
}

TEST(BatchExportFormat, PlansRenderedVideoMediaProbeResultContract)
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
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target);
    BatchRenderedVideoMediaProbeCommandPlan commandPlan =
        batchRenderedVideoMediaProbeCommandPlanFromContracts(
            verificationPlan,
            batchRenderedVideoMediaProbeBinaryPlanFromRequestedName());
    BatchRenderedVideoMediaProbeResultPlan resultPlan =
        batchRenderedVideoMediaProbeResultPlanFromContracts(
            verificationPlan,
            commandPlan);

    ASSERT_TRUE( resultPlan.contractReady );
    ASSERT_TRUE( resultPlan.outputVerificationContractReady );
    ASSERT_TRUE( resultPlan.mediaProbeCommandContractReady );
    ASSERT_TRUE( resultPlan.mediaProbeJsonContractReady );
    ASSERT_TRUE( resultPlan.resultParsingPlanned );
    ASSERT_TRUE( resultPlan.resultSchemaReady );
    ASSERT_TRUE( resultPlan.codecContainerFieldsPlanned );
    ASSERT_FALSE( resultPlan.frameCountFieldPlanned );
    ASSERT_FALSE( resultPlan.durationFieldPlanned );
    ASSERT_FALSE( resultPlan.mediaProbeCommandExecutionOwned );
    ASSERT_FALSE( resultPlan.mediaProbeCommandExecutionReady );
    ASSERT_FALSE( resultPlan.rawJsonReady );
    ASSERT_FALSE( resultPlan.jsonParseReady );
    ASSERT_FALSE( resultPlan.jsonErrorFree );
    ASSERT_FALSE( resultPlan.mediaProbeResultOwned );
    ASSERT_FALSE( resultPlan.jsonParseOwned );
    ASSERT_FALSE( resultPlan.codecContainerResultReady );
    ASSERT_FALSE( resultPlan.frameCountResultReady );
    ASSERT_FALSE( resultPlan.durationResultReady );
    ASSERT_FALSE( resultPlan.resultReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(resultPlan.expectedOutputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string("h264"),
               std::string(resultPlan.expectedCodec.toUtf8().constData()) );
    ASSERT_EQ( std::string("mp4"),
               std::string(resultPlan.expectedContainer.toUtf8().constData()) );
    ASSERT_EQ( 0, resultPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(resultPlan.expectedDurationSeconds)
        < 0.000001 );
    ASSERT_EQ( std::string("output-verification-probe-result-source=output-verification-probe-result-contract output-verification-probe-result-path=C:/renders/M16-1327.mp4 output-verification-probe-result-expected-codec=h264 output-verification-probe-result-expected-container=mp4 output-verification-probe-result-expected-frame-count=0 output-verification-probe-result-expected-duration-seconds=0.000000 output-verification-probe-result-parsed-codec=unspecified output-verification-probe-result-parsed-container=unspecified output-verification-probe-result-parsed-frame-count=0 output-verification-probe-result-parsed-duration-seconds=0.000000 output-verification-probe-result-output-contract-ready=true output-verification-probe-result-command-contract-ready=true output-verification-probe-result-command-owned=false output-verification-probe-result-command-exec-ready=false output-verification-probe-result-parse-planned=true output-verification-probe-result-schema-ready=true output-verification-probe-result-codec-container-fields-planned=true output-verification-probe-result-frame-count-field-planned=false output-verification-probe-result-duration-field-planned=false output-verification-probe-result-owned=false output-verification-probe-result-json-parse-owned=false output-verification-probe-result-codec-container-ready=false output-verification-probe-result-frame-count-ready=false output-verification-probe-result-duration-ready=false output-verification-probe-result-ready=false output-verification-probe-result-contract-ready=true output-verification-probe-result-reason=none"),
               std::string(batchRenderedVideoMediaProbeResultPlanSummary(
                   resultPlan).toUtf8().constData()) );

    verificationPlan =
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target,
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100,
                240));
    commandPlan =
        batchRenderedVideoMediaProbeCommandPlanFromContracts(
            verificationPlan,
            batchRenderedVideoMediaProbeBinaryPlanFromRequestedName());
    resultPlan =
        batchRenderedVideoMediaProbeResultPlanFromContracts(
            verificationPlan,
            commandPlan);

    ASSERT_TRUE( resultPlan.contractReady );
    ASSERT_EQ( 240, resultPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(
        resultPlan.expectedDurationSeconds - 10.01) < 0.000001 );
    ASSERT_TRUE( resultPlan.frameCountFieldPlanned );
    ASSERT_TRUE( resultPlan.durationFieldPlanned );
    ASSERT_FALSE( resultPlan.frameCountResultReady );
    ASSERT_FALSE( resultPlan.durationResultReady );
    ASSERT_FALSE( resultPlan.resultReady );

    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request),
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100,
                240));
    ASSERT_TRUE( plan.mediaProbeResultContractReady );
    ASSERT_TRUE( plan.mediaProbeResultPlan.frameCountFieldPlanned );
    ASSERT_TRUE( plan.mediaProbeResultPlan.durationFieldPlanned );
    ASSERT_FALSE( plan.mediaProbeResultPlan.mediaProbeResultOwned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbeResultReady );
    const std::string summary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( summary.find("output-verification-probe-result-source=output-verification-probe-result-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-result-expected-frame-count=240") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-result-duration-field-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-result-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-result-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-result-frame-count-field-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-result-duration-field-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-result-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-result-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-result-contract-ready=true") != std::string::npos );
}

TEST(BatchExportFormat, PlansRenderedVideoMediaProbeValidationContract)
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
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target);
    BatchRenderedVideoMediaProbeCommandPlan commandPlan =
        batchRenderedVideoMediaProbeCommandPlanFromContracts(
            verificationPlan,
            batchRenderedVideoMediaProbeBinaryPlanFromRequestedName());
    BatchRenderedVideoMediaProbeJsonPlan jsonPlan =
        batchRenderedVideoMediaProbeJsonPlanFromCommand(commandPlan);
    BatchRenderedVideoMediaProbeResultPlan resultPlan =
        batchRenderedVideoMediaProbeResultPlanFromContracts(
            verificationPlan,
            commandPlan,
            jsonPlan);
    BatchRenderedVideoMediaProbeValidationPlan validationPlan =
        batchRenderedVideoMediaProbeValidationPlanFromResult(resultPlan);

    ASSERT_TRUE( validationPlan.contractReady );
    ASSERT_TRUE( validationPlan.mediaProbeResultContractReady );
    ASSERT_TRUE( validationPlan.parsedResultIngestPlanned );
    ASSERT_FALSE( validationPlan.parsedResultIngestOwned );
    ASSERT_FALSE( validationPlan.parsedResultIngestReady );
    ASSERT_TRUE( validationPlan.codecContainerComparisonPlanned );
    ASSERT_FALSE( validationPlan.codecContainerComparisonReady );
    ASSERT_FALSE( validationPlan.codecContainerMatches );
    ASSERT_FALSE( validationPlan.frameCountComparisonPlanned );
    ASSERT_FALSE( validationPlan.frameCountComparisonReady );
    ASSERT_FALSE( validationPlan.durationComparisonPlanned );
    ASSERT_FALSE( validationPlan.durationComparisonReady );
    ASSERT_FALSE( validationPlan.validationOwned );
    ASSERT_FALSE( validationPlan.validationReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(validationPlan.expectedOutputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string("h264"),
               std::string(validationPlan.expectedCodec.toUtf8().constData()) );
    ASSERT_EQ( std::string("mp4"),
               std::string(validationPlan.expectedContainer.toUtf8().constData()) );

    std::string validationSummary =
        std::string(batchRenderedVideoMediaProbeValidationPlanSummary(
            validationPlan).toUtf8().constData());
    ASSERT_TRUE( validationSummary.find("output-verification-probe-validation-source=output-verification-probe-validation-contract") != std::string::npos );
    ASSERT_TRUE( validationSummary.find("output-verification-probe-validation-ingest-planned=true") != std::string::npos );
    ASSERT_TRUE( validationSummary.find("output-verification-probe-validation-ingest-owned=false") != std::string::npos );
    ASSERT_TRUE( validationSummary.find("output-verification-probe-validation-codec-container-planned=true") != std::string::npos );
    ASSERT_TRUE( validationSummary.find("output-verification-probe-validation-codec-container-ready=false") != std::string::npos );
    ASSERT_TRUE( validationSummary.find("output-verification-probe-validation-frame-count-planned=false") != std::string::npos );
    ASSERT_TRUE( validationSummary.find("output-verification-probe-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( validationSummary.find("output-verification-probe-validation-contract-ready=true") != std::string::npos );

    verificationPlan =
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target,
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100,
                240));
    commandPlan =
        batchRenderedVideoMediaProbeCommandPlanFromContracts(
            verificationPlan,
            batchRenderedVideoMediaProbeBinaryPlanFromRequestedName());
    jsonPlan = batchRenderedVideoMediaProbeJsonPlanFromCommand(commandPlan);
    resultPlan =
        batchRenderedVideoMediaProbeResultPlanFromContracts(
            verificationPlan,
            commandPlan,
            jsonPlan);
    validationPlan =
        batchRenderedVideoMediaProbeValidationPlanFromResult(resultPlan);

    ASSERT_TRUE( validationPlan.contractReady );
    ASSERT_TRUE( validationPlan.frameCountComparisonPlanned );
    ASSERT_TRUE( validationPlan.durationComparisonPlanned );
    ASSERT_FALSE( validationPlan.frameCountComparisonReady );
    ASSERT_FALSE( validationPlan.durationComparisonReady );
    ASSERT_FALSE( validationPlan.frameCountMatches );
    ASSERT_FALSE( validationPlan.durationMatches );
    ASSERT_FALSE( validationPlan.validationReady );

    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request),
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100,
                240));
    ASSERT_TRUE( plan.mediaProbeValidationContractReady );
    ASSERT_TRUE( plan.mediaProbeValidationPlan.parsedResultIngestPlanned );
    ASSERT_TRUE( plan.mediaProbeValidationPlan.frameCountComparisonPlanned );
    ASSERT_TRUE( plan.mediaProbeValidationPlan.durationComparisonPlanned );
    ASSERT_FALSE( plan.mediaProbeValidationPlan.parsedResultIngestOwned );
    ASSERT_FALSE( plan.mediaProbeValidationPlan.parsedResultIngestReady );
    ASSERT_FALSE( plan.mediaProbeValidationPlan.validationOwned );
    ASSERT_FALSE( plan.mediaProbeValidationPlan.validationReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbeValidationReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.verificationExecutionReady );
    const std::string summary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( summary.find("output-verification-probe-validation-source=output-verification-probe-validation-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-validation-frame-count-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-validation-duration-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-validation-ingest-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-validation-source=output-verification-probe-validation-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-validation-frame-count-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-validation-duration-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-validation-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("runner-output-verification-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("runnable=true") != std::string::npos );
}

TEST(BatchExportFormat, PlansRenderedVideoFfmpegCommandShape)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoJobPlan basePlan =
        batchRenderedVideoJobPlanFromRequest(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders"),
            request);
    BatchRenderedVideoSourceMetadata metadata =
        batchRenderedVideoSourceMetadata(
            5792,
            3872,
            24000.0 / 1001.0,
            STRETCH_H_100,
            STRETCH_V_100);

    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(basePlan, metadata);
    ASSERT_TRUE( plan.ffmpegCommandReady );
    ASSERT_TRUE( plan.ffmpegCommandPlan.ready );
    ASSERT_TRUE( plan.ffmpegCommandPlan.rawVideoPipeInputReady );
    ASSERT_TRUE( plan.ffmpegExecutionContractReady );
    ASSERT_TRUE( plan.ffmpegExecutionPlan.contractReady );
    ASSERT_TRUE( plan.ffmpegExecutionPlan.commandReady );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.executionReady );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.processLaunchOwned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.stdinPipeOwned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.rawFrameFeedOwned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.stderrCaptureOwned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.exitCodeValidationOwned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.timeoutOwned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.cleanupOwned );
    ASSERT_EQ( std::string("-an"),
               std::string(plan.ffmpegExecutionPlan.audioArguments
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("video-only-to-mux-transition-contract"),
               std::string(plan.ffmpegExecutionPlan.audioTransitionSource
                   .toUtf8().constData()) );
    ASSERT_TRUE( plan.ffmpegExecutionPlan.audioTransitionArguments.isEmpty() );
    ASSERT_TRUE( plan.ffmpegExecutionPlan.audioContractReady );
    ASSERT_TRUE( plan.ffmpegExecutionPlan.audioMuxExecutionContractReady );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.audioMuxPlanned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.audioMuxExecutionReady );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.muxedAudioCommandPlanned );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.muxedAudioCommandReady );
    ASSERT_FALSE( plan.ffmpegExecutionPlan.audioInputOwned );
    ASSERT_EQ( std::string("-an"),
               std::string(plan.ffmpegCommandPlan.audioArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("video-only-to-mux-transition-contract"),
               std::string(plan.ffmpegCommandPlan.audioTransitionSource
                   .toUtf8().constData()) );
    ASSERT_TRUE( plan.ffmpegCommandPlan.audioTransitionArguments.isEmpty() );
    ASSERT_TRUE( plan.ffmpegCommandPlan.audioContractReady );
    ASSERT_TRUE( plan.ffmpegCommandPlan.audioMuxExecutionContractReady );
    ASSERT_FALSE( plan.ffmpegCommandPlan.audioMuxPlanned );
    ASSERT_FALSE( plan.ffmpegCommandPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_FALSE( plan.ffmpegCommandPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( plan.ffmpegCommandPlan.audioMuxExecutionReady );
    ASSERT_FALSE( plan.ffmpegCommandPlan.muxedAudioCommandPlanned );
    ASSERT_FALSE( plan.ffmpegCommandPlan.muxedAudioCommandReady );
    ASSERT_FALSE( plan.ffmpegCommandPlan.audioInputOwned );
    ASSERT_FALSE( plan.ffmpegCommandPlan.executionOwned );
    ASSERT_FALSE( plan.ffmpegCommandPlan.outputVerificationOwned );
    ASSERT_EQ( std::string("ffmpeg"),
               std::string(plan.ffmpegCommandPlan.executable.toUtf8().constData()) );
    ASSERT_EQ( std::string("gui-rawvideo-pipe"),
               std::string(plan.ffmpegCommandPlan.source.toUtf8().constData()) );
    ASSERT_EQ( std::string("rgb48"),
               std::string(plan.ffmpegCommandPlan.rawInputPixelFormat.toUtf8().constData()) );
    ASSERT_EQ( std::string("-r 23.976 -y -f rawvideo -s 5792x3872 -pix_fmt rgb48 -i -"),
               std::string(plan.ffmpegCommandPlan.rawInputArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("rec709-default"),
               std::string(plan.ffmpegCommandPlan.colorTagSource.toUtf8().constData()) );
    ASSERT_EQ( 1, plan.ffmpegCommandPlan.colorTag );
    ASSERT_EQ( std::string("-color_primaries 1 -color_trc 1 -colorspace bt709"),
               std::string(plan.ffmpegCommandPlan.colorArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("-r 23.976 -y -f rawvideo -s 5792x3872 -pix_fmt rgb48 -i - -c:v libx264 -preset medium -crf 14 -pix_fmt yuv420p -color_primaries 1 -color_trc 1 -colorspace bt709 -vf scale=in_color_matrix=bt601:out_color_matrix=bt709 -an \"C:/renders/M16-1327.mp4\""),
               std::string(plan.ffmpegCommandPlan.arguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg -r 23.976 -y -f rawvideo -s 5792x3872 -pix_fmt rgb48 -i - -c:v libx264 -preset medium -crf 14 -pix_fmt yuv420p -color_primaries 1 -color_trc 1 -colorspace bt709 -vf scale=in_color_matrix=bt601:out_color_matrix=bt709 -an \"C:/renders/M16-1327.mp4\""),
               std::string(plan.ffmpegCommandPlan.commandLine.toUtf8().constData()) );

    ASSERT_EQ( std::string("ffmpeg-command-source=gui-rawvideo-pipe ffmpeg-command-exe=ffmpeg ffmpeg-command-raw-pix-fmt=rgb48 ffmpeg-command-raw-input=-r 23.976 -y -f rawvideo -s 5792x3872 -pix_fmt rgb48 -i - ffmpeg-command-color-source=rec709-default ffmpeg-command-color-tag=1 ffmpeg-command-color-args=-color_primaries 1 -color_trc 1 -colorspace bt709 ffmpeg-command-audio-args=-an ffmpeg-command-audio-transition-source=video-only-to-mux-transition-contract ffmpeg-command-audio-transition-args=unspecified ffmpeg-command-audio-contract-ready=true ffmpeg-command-audio-mux-exec-contract-ready=true ffmpeg-command-audio-mux-planned=false ffmpeg-command-audio-mux-args-handoff-planned=false ffmpeg-command-audio-sync-planned=false ffmpeg-command-audio-mux-exec-ready=false ffmpeg-command-audio-mux-command-planned=false ffmpeg-command-audio-mux-command-ready=false ffmpeg-command-audio-owned=false ffmpeg-command-execution-owned=false ffmpeg-command-output-verification-owned=false ffmpeg-command-args=-r 23.976 -y -f rawvideo -s 5792x3872 -pix_fmt rgb48 -i - -c:v libx264 -preset medium -crf 14 -pix_fmt yuv420p -color_primaries 1 -color_trc 1 -colorspace bt709 -vf scale=in_color_matrix=bt601:out_color_matrix=bt709 -an \"C:/renders/M16-1327.mp4\" ffmpeg-command-ready=true ffmpeg-command-reason=none"),
                std::string(batchRenderedVideoFfmpegCommandPlanSummary(
                    plan.ffmpegCommandPlan).toUtf8().constData()) );
    ASSERT_EQ( std::string("ffmpeg-execution-source=command-contract ffmpeg-execution-exe=ffmpeg ffmpeg-execution-command-ready=true ffmpeg-execution-audio-args=-an ffmpeg-execution-audio-transition-source=video-only-to-mux-transition-contract ffmpeg-execution-audio-transition-args=unspecified ffmpeg-execution-audio-contract-ready=true ffmpeg-execution-audio-mux-exec-contract-ready=true ffmpeg-execution-audio-mux-planned=false ffmpeg-execution-audio-mux-args-handoff-planned=false ffmpeg-execution-audio-sync-planned=false ffmpeg-execution-audio-mux-exec-ready=false ffmpeg-execution-audio-mux-command-planned=false ffmpeg-execution-audio-mux-command-ready=false ffmpeg-execution-audio-owned=false ffmpeg-execution-process-launch-owned=false ffmpeg-execution-stdin-pipe-owned=false ffmpeg-execution-raw-frame-feed-owned=false ffmpeg-execution-stderr-capture-owned=false ffmpeg-execution-exit-code-owned=false ffmpeg-execution-timeout-owned=false ffmpeg-execution-cleanup-owned=false ffmpeg-execution-ready=false ffmpeg-execution-contract-ready=true ffmpeg-execution-reason=none"),
               std::string(batchRenderedVideoFfmpegExecutionPlanSummary(
                   plan.ffmpegExecutionPlan).toUtf8().constData()) );

    BatchRenderedVideoFfmpegCommandPlan invalidPlan =
        batchRenderedVideoFfmpegCommandPlanFromParts(
            BatchRenderedVideoFfmpegFramePlan(),
            plan.ffmpegFilterPlan,
            plan.ffmpegVideoPlan,
            plan.outputPlan);
    ASSERT_FALSE( invalidPlan.ready );
    ASSERT_EQ( std::string("rendered ffmpeg frame plan unavailable"),
               std::string(invalidPlan.reason.toUtf8().constData()) );
    BatchRenderedVideoFfmpegExecutionPlan invalidExecutionPlan =
        batchRenderedVideoFfmpegExecutionPlanFromCommand(invalidPlan);
    ASSERT_FALSE( invalidExecutionPlan.contractReady );
    ASSERT_FALSE( invalidExecutionPlan.executionReady );
    ASSERT_EQ( std::string("rendered ffmpeg frame plan unavailable"),
               std::string(invalidExecutionPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, OverlaysRenderedVideoMetadataOntoJobPlan)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoJobPlan basePlan =
        batchRenderedVideoJobPlanFromRequest(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders"),
            request);

    ASSERT_TRUE( basePlan.preflightReady );
    ASSERT_FALSE( basePlan.metadataAttempted );
    ASSERT_FALSE( basePlan.metadataReady );
    ASSERT_FALSE( basePlan.ffmpegFrameReady );
    ASSERT_FALSE( basePlan.receiptApplicationContractReady );
    ASSERT_FALSE( basePlan.receiptApplicationPlan.contractReady );
    ASSERT_TRUE( basePlan.ffmpegFilterReady );
    ASSERT_TRUE( basePlan.optionalFilterContractReady );
    ASSERT_TRUE( basePlan.optionalFilterPlan.contractReady );
    ASSERT_TRUE( basePlan.optionalFilterPlan.baseFilterContractReady );
    ASSERT_FALSE( basePlan.optionalFilterPlan.optionalFiltersRequested );
    ASSERT_FALSE( basePlan.optionalFilterPlan.optionalFilterGraphOwned );
    ASSERT_FALSE( basePlan.optionalFilterPlan.moireeFilterOwned );
    ASSERT_FALSE( basePlan.optionalFilterPlan.hdrBlendOwned );
    ASSERT_FALSE( basePlan.optionalFilterPlan.stabilizationOwned );
    ASSERT_FALSE( basePlan.optionalFilterPlan.optionalFilterOrderOwned );
    ASSERT_FALSE( basePlan.optionalFilterPlan.optionalFilterParityOwned );
    ASSERT_FALSE( basePlan.optionalFilterPlan.optionalFilterExecutionReady );
    ASSERT_TRUE( basePlan.sourceAudioContractReady );
    ASSERT_TRUE( basePlan.sourceAudioPlan.contractReady );
    ASSERT_FALSE( basePlan.sourceAudioPlan.discoveryAttempted );
    ASSERT_TRUE( basePlan.sourceAudioExtractionContractReady );
    ASSERT_TRUE( basePlan.sourceAudioExtractionPlan.contractReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.source-audio.wav"),
               std::string(basePlan.sourceAudioExtractionPlan.plannedAudioPath
                   .toUtf8().constData()) );
    ASSERT_FALSE( basePlan.sourceAudioExtractionPlan.sourceAudioKnown );
    ASSERT_FALSE( basePlan.sourceAudioExtractionPlan.sourceAudioPresent );
    ASSERT_FALSE( basePlan.sourceAudioExtractionPlan.extractionPathPlanned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionPlan.extractionProcessOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionPlan.tempFileOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionPlan.cleanupOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionPlan.extractionReady );
    ASSERT_TRUE( basePlan.sourceAudioExtractionExecutionContractReady );
    ASSERT_TRUE( basePlan.sourceAudioExtractionExecutionPlan.contractReady );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.sourceAudioKnown );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.sourceAudioPresent );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.sampleReadPlanned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.wavWritePlanned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.tempFileLifecyclePlanned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.sampleReadOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.wavHeaderWriteOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.wavSampleWriteOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.tempFileOpenOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.tempFileFinalizeOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.cleanupOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.extractionProcessOwned );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.tempFileLifecycleReady );
    ASSERT_FALSE( basePlan.sourceAudioExtractionExecutionPlan.extractionExecutionReady );
    ASSERT_TRUE( basePlan.ffmpegAudioInputHandoffContractReady );
    ASSERT_TRUE( basePlan.ffmpegAudioInputHandoffPlan.contractReady );
    ASSERT_FALSE( basePlan.ffmpegAudioInputHandoffPlan.sourceAudioKnown );
    ASSERT_FALSE( basePlan.ffmpegAudioInputHandoffPlan.sourceAudioPresent );
    ASSERT_FALSE( basePlan.ffmpegAudioInputHandoffPlan.audioInputPlanned );
    ASSERT_FALSE( basePlan.ffmpegAudioInputHandoffPlan.inputArgumentsPlanned );
    ASSERT_FALSE( basePlan.ffmpegAudioInputHandoffPlan.audioInputOwnershipPlanned );
    ASSERT_FALSE( basePlan.ffmpegAudioInputHandoffPlan.audioInputArgumentHandoffPlanned );
    ASSERT_FALSE( basePlan.ffmpegAudioInputHandoffPlan.audioInputOwned );
    ASSERT_FALSE( basePlan.ffmpegAudioInputHandoffPlan.audioInputArgumentHandoffOwned );
    ASSERT_FALSE( basePlan.ffmpegAudioInputHandoffPlan.audioInputHandoffReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.source-audio.wav"),
               std::string(basePlan.ffmpegAudioInputHandoffPlan.plannedAudioPath
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(basePlan.ffmpegAudioInputHandoffPlan.activeAudioArguments
                   .toUtf8().constData()) );
    ASSERT_TRUE( basePlan.ffmpegAudioInputContractReady );
    ASSERT_TRUE( basePlan.ffmpegAudioInputPlan.contractReady );
    ASSERT_FALSE( basePlan.ffmpegAudioInputPlan.sourceAudioKnown );
    ASSERT_FALSE( basePlan.ffmpegAudioInputPlan.sourceAudioPresent );
    ASSERT_FALSE( basePlan.ffmpegAudioInputPlan.audioInputPlanned );
    ASSERT_FALSE( basePlan.ffmpegAudioInputPlan.audioInputOwned );
    ASSERT_FALSE( basePlan.ffmpegAudioInputPlan.audioInputReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.source-audio.wav"),
               std::string(basePlan.ffmpegAudioInputPlan.plannedAudioPath
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(basePlan.ffmpegAudioInputPlan.activeAudioArguments
                   .toUtf8().constData()) );
    ASSERT_TRUE( basePlan.audioMuxPrerequisitesContractReady );
    ASSERT_TRUE( basePlan.audioMuxPrerequisitesPlan.contractReady );
    ASSERT_TRUE( basePlan.audioMuxPrerequisitesPlan.sourceAudioContractReady );
    ASSERT_TRUE( basePlan.audioMuxPrerequisitesPlan.sourceAudioExtractionContractReady );
    ASSERT_TRUE( basePlan.audioMuxPrerequisitesPlan.sourceAudioInputContractReady );
    ASSERT_TRUE( basePlan.audioMuxPrerequisitesPlan.videoOnlyFallbackReady );
    ASSERT_FALSE( basePlan.audioMuxPrerequisitesPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( basePlan.audioMuxPrerequisitesPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( basePlan.audioMuxPrerequisitesPlan.audioInputOwned );
    ASSERT_FALSE( basePlan.audioMuxPrerequisitesPlan.audioMuxOwned );
    ASSERT_FALSE( basePlan.audioMuxPrerequisitesPlan.audioSyncValidationOwned );
    ASSERT_FALSE( basePlan.audioMuxPrerequisitesPlan.muxReady );
    ASSERT_TRUE( basePlan.audioMuxExecutionContractReady );
    ASSERT_TRUE( basePlan.audioMuxExecutionPlan.contractReady );
    ASSERT_TRUE( basePlan.audioMuxExecutionPlan.sourceAudioContractReady );
    ASSERT_TRUE( basePlan.audioMuxExecutionPlan.audioInputHandoffContractReady );
    ASSERT_TRUE( basePlan.audioMuxExecutionPlan.audioMuxPrerequisitesContractReady );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.sourceAudioKnown );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.sourceAudioPresent );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.audioInputHandoffReady );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.tempAudioInputOwned );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.audioMuxPlanned );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.audioMuxOwned );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.audioMuxArgumentHandoffOwned );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.audioSyncValidationOwned );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.audioMuxReady );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.audioSyncValidationReady );
    ASSERT_FALSE( basePlan.audioMuxExecutionPlan.muxExecutionReady );
    ASSERT_TRUE( basePlan.audioMuxExecutionPlan.videoOnlyFallbackReady );
    ASSERT_TRUE( basePlan.ffmpegAudioContractReady );
    ASSERT_TRUE( basePlan.ffmpegAudioPlan.contractReady );
    ASSERT_TRUE( basePlan.ffmpegAudioPlan.audioInputContractReady );
    ASSERT_FALSE( basePlan.ffmpegAudioPlan.audioMuxOwned );
    ASSERT_FALSE( basePlan.frameProcessingContractReady );
    ASSERT_FALSE( basePlan.ffmpegExecutionContractReady );
    ASSERT_FALSE( basePlan.outputVerificationExecutionContractReady );
    ASSERT_TRUE( basePlan.mediaProbeCommandReady );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(basePlan.mediaProbeBinaryPlan.requestedExecutable
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(basePlan.mediaProbeBinaryPlan.resolvedExecutable
                   .toUtf8().constData()) );
    ASSERT_TRUE( basePlan.mediaProbeCommandContractReady );
    ASSERT_TRUE( basePlan.mediaProbeCommandPlan.commandReady );
    ASSERT_FALSE( basePlan.mediaProbeCommandPlan.executionOwned );
    ASSERT_FALSE( basePlan.mediaProbeCommandPlan.executionReady );
    ASSERT_EQ( std::string("render-settings-source=batch-defaults render-settings-explicit-headless=false render-settings-gui-owned=false render-settings-ready=true render-settings-reason=none render-settings-resize=false render-settings-resize-width=0 render-settings-resize-height=0 render-settings-resize-height-locked=false"),
               std::string(batchRenderedVideoRenderSettingsSummary(
                   basePlan.renderSettings).toUtf8().constData()) );
    /* E4-1: the runner is implemented, so a fully-planned job reports NO blocker. This
     * assertion is the one that fails first if the runner prerequisites are ever flipped
     * back off, which is why it pins the empty string rather than merely "not the old
     * text". Audio muxing remains deferred to E4-2 and is surfaced as a `limitation`,
     * not as a blocker -- see BatchRenderedVideoRunnerPrerequisites. */
    ASSERT_EQ( std::string(""),
               std::string(batchRenderedVideoJobPlanFirstBlocker(basePlan).toUtf8().constData()) );

    BatchRenderedVideoSourceMetadata metadata =
        batchRenderedVideoSourceMetadata(
            5792,
            3872,
            24000.0 / 1001.0,
            STRETCH_H_100,
            STRETCH_V_100,
            240);
    ASSERT_TRUE( metadata.ready );
    ASSERT_TRUE( metadata.frameCountReady );
    ASSERT_EQ( 240, metadata.frameCount );
    ASSERT_TRUE( std::fabs(metadata.durationSeconds - 10.01) < 0.000001 );
    ASSERT_EQ( std::string("source-metadata=5792x3872"),
               std::string(batchRenderedVideoSourceMetadataSummary(metadata)
                   .left(QStringLiteral("source-metadata=5792x3872").length())
                   .toUtf8().constData()) );

    BatchRenderedVideoRenderSettings settings =
        batchRenderedVideoRenderSettingsFromExplicitResize(
            true,
            1920,
            0,
            true);
    ASSERT_TRUE( settings.ready );
    ASSERT_TRUE( settings.explicitHeadlessSettings );
    ASSERT_FALSE( settings.guiSettingsOwned );

    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(basePlan, metadata, settings);
    ASSERT_TRUE( plan.metadataAttempted );
    ASSERT_TRUE( plan.metadataReady );
    ASSERT_TRUE( plan.ffmpegFrameReady );
    ASSERT_TRUE( plan.receiptApplicationContractReady );
    ASSERT_TRUE( plan.receiptApplicationPlan.contractReady );
    ASSERT_TRUE( plan.receiptApplicationPlan.inputContractReady );
    ASSERT_TRUE( plan.receiptApplicationPlan.outputContractReady );
    ASSERT_FALSE( plan.receiptApplicationPlan.applyToMlvOwned );
    ASSERT_FALSE( plan.receiptApplicationPlan.processingObjectMutationOwned );
    ASSERT_FALSE( plan.receiptApplicationPlan.cacheInvalidationOwned );
    ASSERT_FALSE( plan.receiptApplicationPlan.cutStretchStateOwned );
    ASSERT_FALSE( plan.receiptApplicationPlan.lookAssistApplicationOwned );
    ASSERT_FALSE( plan.receiptApplicationPlan.receiptValidationOwned );
    ASSERT_FALSE( plan.receiptApplicationPlan.applicationReady );
    ASSERT_TRUE( plan.frameProcessingContractReady );
    ASSERT_TRUE( plan.frameProcessingPlan.contractReady );
    ASSERT_TRUE( plan.frameProcessingPlan.sourceMetadataReady );
    ASSERT_TRUE( plan.frameProcessingPlan.frameGeometryReady );
    ASSERT_TRUE( plan.frameProcessingPlan.receiptApplicationContractReady );
    ASSERT_TRUE( plan.frameProcessingPlan.debayerContractReady );
    ASSERT_TRUE( plan.frameProcessingPlan.previewProcessingContractReady );
    ASSERT_TRUE( plan.frameProcessingPlan.resizeProcessingContractReady );
    ASSERT_TRUE( plan.frameProcessingPlan.rgb48FrameBufferContractReady );
    ASSERT_TRUE( plan.frameProcessingPlan.frameIterationContractReady );
    ASSERT_FALSE( plan.frameProcessingPlan.processingParityReady );
    ASSERT_FALSE( plan.frameProcessingPlan.frameProcessingReady );
    ASSERT_FALSE( plan.frameProcessingPlan.receiptApplicationOwned );
    ASSERT_FALSE( plan.frameProcessingPlan.debayerOwned );
    ASSERT_FALSE( plan.frameProcessingPlan.previewProcessingOwned );
    ASSERT_FALSE( plan.frameProcessingPlan.resizeProcessingOwned );
    ASSERT_FALSE( plan.frameProcessingPlan.rgb48FrameBufferOwned );
    ASSERT_FALSE( plan.frameProcessingPlan.frameIterationOwned );
    ASSERT_FALSE( plan.frameProcessingPlan.processingParityValidationOwned );
    ASSERT_TRUE( plan.ffmpegCommandReady );
    ASSERT_TRUE( plan.ffmpegExecutionContractReady );
    ASSERT_TRUE( plan.outputVerificationExecutionContractReady );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.contractReady );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.outputVerificationContractReady );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.ffmpegExecutionContractReady );
    ASSERT_EQ( std::string("default-executable-name"),
               std::string(plan.outputVerificationExecutionPlan
                   .mediaProbeBinarySource.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(plan.outputVerificationExecutionPlan
                   .mediaProbeRequestedExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(plan.outputVerificationExecutionPlan
                   .mediaProbeResolvedExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(plan.outputVerificationExecutionPlan
                   .mediaProbeExecutable.toUtf8().constData()) );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbePathSearchOwned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbePathSearchAttempted );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbeFoundOnPath );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.mediaProbeCommandReady );
    ASSERT_EQ( std::string("output-verification-probe-command-contract"),
               std::string(plan.outputVerificationExecutionPlan
                   .mediaProbeCommandSource.toUtf8().constData()) );
    ASSERT_EQ( std::string("-v error -show_format -show_streams -of json C:/renders/M16-1327.mp4"),
               std::string(plan.outputVerificationExecutionPlan
                   .mediaProbeCommandArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("ffprobe -v error -show_format -show_streams -of json C:/renders/M16-1327.mp4"),
               std::string(plan.outputVerificationExecutionPlan
                   .mediaProbeCommandLine.toUtf8().constData()) );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.mediaProbeCommandPlanned );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.mediaProbeInvocationCommandReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbeCommandExecutionOwned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbeCommandExecutionReady );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.mediaProbeCommandContractReady );
    ASSERT_EQ( std::string("-an"),
               std::string(plan.outputVerificationExecutionPlan
                   .ffmpegAudioArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("video-only-to-mux-transition-contract"),
               std::string(plan.outputVerificationExecutionPlan
                   .ffmpegAudioTransitionSource.toUtf8().constData()) );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan
        .ffmpegAudioTransitionArguments.isEmpty() );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.ffmpegAudioContractReady );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.ffmpegAudioMuxExecutionContractReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.ffmpegAudioMuxPlanned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.ffmpegAudioMuxArgumentHandoffPlanned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.ffmpegAudioSyncValidationPlanned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.ffmpegAudioMuxExecutionReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.ffmpegMuxedAudioCommandPlanned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.ffmpegMuxedAudioCommandReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.ffmpegAudioInputOwned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.verificationExecutionReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.fileExistenceCheckOwned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.nonEmptyCheckOwned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.mediaProbeExecutionOwned );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.codecContainerCheckPlanned );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.codecContainerExpectationReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.codecContainerValidationReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.codecContainerValidationOwned );
    ASSERT_EQ( 240, plan.outputVerificationPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(
        plan.outputVerificationPlan.expectedDurationSeconds - 10.01)
        < 0.000001 );
    ASSERT_TRUE( plan.outputVerificationPlan.frameCountCheckPlanned );
    ASSERT_TRUE( plan.outputVerificationPlan.frameCountExpectationReady );
    ASSERT_FALSE( plan.outputVerificationPlan.frameCountValidationReady );
    ASSERT_EQ( 240,
               plan.outputVerificationExecutionPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(
        plan.outputVerificationExecutionPlan.expectedDurationSeconds - 10.01)
        < 0.000001 );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan
        .frameCountCheckPlanned );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan
        .frameCountExpectationReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan
        .frameCountValidationReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.frameCountValidationOwned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.receiptHashValidationOwned );
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );
    ASSERT_EQ( 1920, plan.ffmpegFramePlan.outputWidth );
    ASSERT_EQ( 1284, plan.ffmpegFramePlan.outputHeight );
    ASSERT_EQ( std::string("23.976"),
               std::string(plan.ffmpegFramePlan.frameRateArgument.toUtf8().constData()) );
    /* E4-1: implemented, so a fully-planned job reports no blocker. */
    ASSERT_EQ( std::string(""),
               std::string(batchRenderedVideoJobPlanFirstBlocker(plan).toUtf8().constData()) );

    const std::string summary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( summary.find("source-metadata=5792x3872") != std::string::npos );
    ASSERT_TRUE( summary.find("source-frame-count=240") != std::string::npos );
    ASSERT_TRUE( summary.find("source-duration-seconds=10.010000") != std::string::npos );
    ASSERT_TRUE( summary.find("source-frame-count-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("source-metadata-attempted=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-filter-source=gui-base-color-scale") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-filter-args=-vf scale=in_color_matrix=bt601:out_color_matrix=bt709") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-filter-optional-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-source=optional-filter-ownership-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-base-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-requested=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-graph-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-moiree-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-hdr-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-stabilization-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-order-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-parity-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-source=video-only-undiscovered") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-state=unknown") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-sample-rate=0") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-channels=0") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-bits=0") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-bytes=0") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-discovery-attempted=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-known=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-video-only-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-source=source-audio-extraction-prerequisite-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-format=wav-pcm-s16le") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-known=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-present=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-path-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-process-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-temp-file-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-cleanup-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-source=source-audio-extraction-execution-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-sample-read-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-wav-write-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-temp-lifecycle-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-sample-read-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-wav-header-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-wav-sample-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-temp-open-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-temp-finalize-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-cleanup-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-process-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-temp-lifecycle-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-source=ffmpeg-audio-input-handoff-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-planned-args=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-active-args=-an") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-known=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-present=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-args-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-ownership-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-args-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-args-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-source=ffmpeg-audio-input-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-planned-args=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-active-args=-an") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-known=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-present=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-source=audio-mux-prerequisite-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-source-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-extraction-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-audio-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-video-only-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-source-discovery-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-extraction-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-input-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-mux-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-sync-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-source=audio-mux-execution-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-source-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-handoff-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-prereq-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-known=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-present=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-discovery-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-extraction-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-handoff-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-temp-input-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-mux-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-args-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-sync-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-mux-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-args-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-sync-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-mux-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-sync-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-source=video-only-to-mux-transition-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-args=-an") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-transition-args=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-args-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-sync-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-args-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-command-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-command-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-settings-source=explicit-headless render-settings-explicit-headless=true render-settings-gui-owned=false render-settings-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-settings-resize=true render-settings-resize-width=1920") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-frame-size=1920x1284") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-frame-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-source=receipt-application-input-output-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-output-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-apply-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-processing-object-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-cache-invalidation-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-cut-stretch-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-look-assist-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-validation-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-output-size=1920x1284") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-receipt-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-debayer-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-preview-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-resize-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-rgb48-buffer-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-frame-iteration-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-receipt-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-debayer-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-preview-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-rgb48-buffer-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-parity-validation-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-parity-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-frame-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-raw-input=-r 23.976 -y -f rawvideo -s 1920x1284 -pix_fmt rgb48 -i -") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-transition-source=video-only-to-mux-transition-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-transition-args=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-mux-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-mux-args-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-sync-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-mux-command-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-mux-command-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-audio-owned=false ffmpeg-command-execution-owned=false ffmpeg-command-output-verification-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-command-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-args=-an") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-transition-source=video-only-to-mux-transition-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-transition-args=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-mux-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-mux-args-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-sync-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-mux-command-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-mux-command-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-audio-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-process-launch-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-stdin-pipe-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-raw-frame-feed-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-binary-source=default-executable-name") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-binary-request=ffprobe") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-binary-resolved=ffprobe") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-binary-path-search-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-binary-path-search-attempted=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-binary-found=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-binary-command-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-expected-frame-count=240") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-expected-duration-seconds=10.010000") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-codec-container-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-codec-container-input-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-codec-container-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-frame-count-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-frame-count-input-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-frame-count-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-source=output-verification-probe-command-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-line=ffprobe -v error -show_format -show_streams -of json C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-source=post-ffmpeg-output-verification-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-expected-frame-count=240") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-expected-duration-seconds=10.010000") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe=ffprobe") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-binary-source=default-executable-name") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-binary-request=ffprobe") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-binary-resolved=ffprobe") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-binary-path-search-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-binary-path-search-attempted=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-binary-found=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-binary-command-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-source=output-verification-probe-command-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-line=ffprobe -v error -show_format -show_streams -of json C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-args=-an") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-transition-source=video-only-to-mux-transition-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-transition-args=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-mux-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-mux-args-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-sync-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-mux-command-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-mux-command-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-audio-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-codec-container-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-codec-container-input-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-codec-container-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-frame-count-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-frame-count-input-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-frame-count-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-file-exists-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-codec-container-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("preflight-ready=true runnable=true") != std::string::npos );

    BatchRenderedVideoSourceAudioPlan discoveredAudioPlan =
        batchRenderedVideoSourceAudioPlanFromDiscoveredAudio(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            true,
            2,
            48000,
            16,
            123456);
    BatchRenderedVideoJobPlan audioBasePlan =
        batchRenderedVideoJobPlanWithSourceAudio(
            basePlan,
            discoveredAudioPlan);
    ASSERT_TRUE( audioBasePlan.preflightReady );
    ASSERT_TRUE( audioBasePlan.sourceAudioPlan.discoveryOwned );
    ASSERT_TRUE( audioBasePlan.sourceAudioPlan.sourceAudioKnown );
    ASSERT_TRUE( audioBasePlan.sourceAudioPlan.sourceAudioPresent );
    ASSERT_EQ( 48000, audioBasePlan.sourceAudioPlan.sampleRate );
    ASSERT_EQ( 2, audioBasePlan.sourceAudioPlan.channels );
    ASSERT_EQ( 16, audioBasePlan.sourceAudioPlan.bitsPerSample );
    ASSERT_EQ( static_cast<qulonglong>(123456),
               audioBasePlan.sourceAudioPlan.audioBytes );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionContractReady );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionPlan.contractReady );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionPlan.sourceAudioKnown );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionPlan.sourceAudioPresent );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionPlan.sourceAudioDiscoveryOwned );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionPlan.extractionPathPlanned );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionPlan.extractionPathReady );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionPlan.extractionProcessOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionPlan.tempFileOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionPlan.cleanupOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionPlan.extractionReady );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionExecutionContractReady );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionExecutionPlan.contractReady );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionExecutionPlan.sourceAudioKnown );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionExecutionPlan.sourceAudioPresent );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionExecutionPlan.sampleReadPlanned );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionExecutionPlan.wavWritePlanned );
    ASSERT_TRUE( audioBasePlan.sourceAudioExtractionExecutionPlan.tempFileLifecyclePlanned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionExecutionPlan.sampleReadOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionExecutionPlan.wavHeaderWriteOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionExecutionPlan.wavSampleWriteOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionExecutionPlan.tempFileOpenOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionExecutionPlan.tempFileFinalizeOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionExecutionPlan.cleanupOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionExecutionPlan.extractionProcessOwned );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionExecutionPlan.tempFileLifecycleReady );
    ASSERT_FALSE( audioBasePlan.sourceAudioExtractionExecutionPlan.extractionExecutionReady );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputHandoffContractReady );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputHandoffPlan.contractReady );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputHandoffPlan.sourceAudioKnown );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputHandoffPlan.sourceAudioPresent );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputHandoffPlan.audioInputPlanned );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputHandoffPlan.inputArgumentsPlanned );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputHandoffPlan.audioInputOwnershipPlanned );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputHandoffPlan.audioInputArgumentHandoffPlanned );
    ASSERT_FALSE( audioBasePlan.ffmpegAudioInputHandoffPlan.audioInputOwned );
    ASSERT_FALSE( audioBasePlan.ffmpegAudioInputHandoffPlan.audioInputArgumentHandoffOwned );
    ASSERT_FALSE( audioBasePlan.ffmpegAudioInputHandoffPlan.audioInputHandoffReady );
    ASSERT_EQ( std::string("-i \"C:/renders/M16-1327.source-audio.wav\""),
               std::string(audioBasePlan.ffmpegAudioInputHandoffPlan.plannedInputArguments
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(audioBasePlan.ffmpegAudioInputHandoffPlan.activeAudioArguments
                   .toUtf8().constData()) );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputContractReady );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputPlan.contractReady );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioInputPlan.audioInputPlanned );
    ASSERT_FALSE( audioBasePlan.ffmpegAudioInputPlan.audioInputOwned );
    ASSERT_FALSE( audioBasePlan.ffmpegAudioInputPlan.audioInputReady );
    ASSERT_EQ( std::string("-i \"C:/renders/M16-1327.source-audio.wav\""),
               std::string(audioBasePlan.ffmpegAudioInputPlan.plannedInputArguments
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(audioBasePlan.ffmpegAudioInputPlan.activeAudioArguments
                   .toUtf8().constData()) );
    ASSERT_TRUE( audioBasePlan.audioMuxPrerequisitesPlan.sourceAudioDiscoveryOwned );
    ASSERT_TRUE( audioBasePlan.audioMuxPrerequisitesPlan.sourceAudioExtractionContractReady );
    ASSERT_TRUE( audioBasePlan.audioMuxPrerequisitesPlan.sourceAudioInputContractReady );
    ASSERT_FALSE( audioBasePlan.audioMuxPrerequisitesPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( audioBasePlan.audioMuxPrerequisitesPlan.audioInputOwned );
    ASSERT_FALSE( audioBasePlan.audioMuxPrerequisitesPlan.audioMuxOwned );
    ASSERT_FALSE( audioBasePlan.audioMuxPrerequisitesPlan.audioSyncValidationOwned );
    ASSERT_FALSE( audioBasePlan.audioMuxPrerequisitesPlan.muxReady );
    ASSERT_TRUE( audioBasePlan.audioMuxExecutionContractReady );
    ASSERT_TRUE( audioBasePlan.audioMuxExecutionPlan.contractReady );
    ASSERT_TRUE( audioBasePlan.audioMuxExecutionPlan.sourceAudioKnown );
    ASSERT_TRUE( audioBasePlan.audioMuxExecutionPlan.sourceAudioPresent );
    ASSERT_TRUE( audioBasePlan.audioMuxExecutionPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( audioBasePlan.audioMuxExecutionPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( audioBasePlan.audioMuxExecutionPlan.audioInputHandoffReady );
    ASSERT_FALSE( audioBasePlan.audioMuxExecutionPlan.tempAudioInputOwned );
    ASSERT_TRUE( audioBasePlan.audioMuxExecutionPlan.audioMuxPlanned );
    ASSERT_TRUE( audioBasePlan.audioMuxExecutionPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_TRUE( audioBasePlan.audioMuxExecutionPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( audioBasePlan.audioMuxExecutionPlan.audioMuxOwned );
    ASSERT_FALSE( audioBasePlan.audioMuxExecutionPlan.audioMuxArgumentHandoffOwned );
    ASSERT_FALSE( audioBasePlan.audioMuxExecutionPlan.audioSyncValidationOwned );
    ASSERT_FALSE( audioBasePlan.audioMuxExecutionPlan.audioMuxReady );
    ASSERT_FALSE( audioBasePlan.audioMuxExecutionPlan.audioSyncValidationReady );
    ASSERT_FALSE( audioBasePlan.audioMuxExecutionPlan.muxExecutionReady );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( audioBasePlan.ffmpegAudioPlan.sourceAudioExtractionOwned );
    ASSERT_TRUE( audioBasePlan.ffmpegAudioPlan.audioInputContractReady );
    ASSERT_FALSE( audioBasePlan.ffmpegAudioPlan.audioInputOwned );
    ASSERT_FALSE( audioBasePlan.ffmpegAudioPlan.audioMuxOwned );
    ASSERT_FALSE( audioBasePlan.ffmpegAudioPlan.audioSyncOwned );

    BatchRenderedVideoJobPlan discoveredPlan =
        batchRenderedVideoJobPlanWithMetadata(
            audioBasePlan,
            metadata,
            settings);
    ASSERT_TRUE( discoveredPlan.preflightReady );
    ASSERT_TRUE( discoveredPlan.runnable );
    ASSERT_TRUE( discoveredPlan.ffmpegCommandReady );
    ASSERT_TRUE( discoveredPlan.ffmpegExecutionContractReady );
    ASSERT_TRUE( discoveredPlan.outputVerificationExecutionContractReady );
    ASSERT_TRUE( discoveredPlan.sourceAudioPlan.discoveryOwned );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionPlan.contractReady );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionPlan.sourceAudioDiscoveryOwned );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionPlan.extractionPathPlanned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionPlan.extractionProcessOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionPlan.tempFileOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionPlan.cleanupOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionPlan.extractionReady );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionExecutionContractReady );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionExecutionPlan.contractReady );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionExecutionPlan.sourceAudioKnown );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionExecutionPlan.sourceAudioPresent );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionExecutionPlan.sampleReadPlanned );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionExecutionPlan.wavWritePlanned );
    ASSERT_TRUE( discoveredPlan.sourceAudioExtractionExecutionPlan.tempFileLifecyclePlanned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionExecutionPlan.sampleReadOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionExecutionPlan.wavHeaderWriteOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionExecutionPlan.wavSampleWriteOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionExecutionPlan.tempFileOpenOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionExecutionPlan.tempFileFinalizeOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionExecutionPlan.cleanupOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionExecutionPlan.extractionProcessOwned );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionExecutionPlan.tempFileLifecycleReady );
    ASSERT_FALSE( discoveredPlan.sourceAudioExtractionExecutionPlan.extractionExecutionReady );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputHandoffContractReady );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputHandoffPlan.contractReady );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputHandoffPlan.sourceAudioKnown );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputHandoffPlan.sourceAudioPresent );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputHandoffPlan.audioInputPlanned );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputHandoffPlan.inputArgumentsPlanned );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputHandoffPlan.audioInputOwnershipPlanned );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputHandoffPlan.audioInputArgumentHandoffPlanned );
    ASSERT_FALSE( discoveredPlan.ffmpegAudioInputHandoffPlan.audioInputOwned );
    ASSERT_FALSE( discoveredPlan.ffmpegAudioInputHandoffPlan.audioInputArgumentHandoffOwned );
    ASSERT_FALSE( discoveredPlan.ffmpegAudioInputHandoffPlan.audioInputHandoffReady );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputContractReady );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputPlan.contractReady );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioInputPlan.audioInputPlanned );
    ASSERT_FALSE( discoveredPlan.ffmpegAudioInputPlan.audioInputOwned );
    ASSERT_FALSE( discoveredPlan.ffmpegAudioInputPlan.audioInputReady );
    ASSERT_EQ( std::string("-i \"C:/renders/M16-1327.source-audio.wav\""),
               std::string(discoveredPlan.ffmpegAudioInputPlan.plannedInputArguments
                   .toUtf8().constData()) );
    ASSERT_TRUE( discoveredPlan.audioMuxPrerequisitesPlan.sourceAudioDiscoveryOwned );
    ASSERT_TRUE( discoveredPlan.audioMuxPrerequisitesPlan.sourceAudioExtractionContractReady );
    ASSERT_TRUE( discoveredPlan.audioMuxPrerequisitesPlan.sourceAudioInputContractReady );
    ASSERT_FALSE( discoveredPlan.audioMuxPrerequisitesPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( discoveredPlan.audioMuxPrerequisitesPlan.audioInputOwned );
    ASSERT_FALSE( discoveredPlan.audioMuxPrerequisitesPlan.audioMuxOwned );
    ASSERT_FALSE( discoveredPlan.audioMuxPrerequisitesPlan.audioSyncValidationOwned );
    ASSERT_FALSE( discoveredPlan.audioMuxPrerequisitesPlan.muxReady );
    ASSERT_TRUE( discoveredPlan.audioMuxExecutionContractReady );
    ASSERT_TRUE( discoveredPlan.audioMuxExecutionPlan.contractReady );
    ASSERT_TRUE( discoveredPlan.audioMuxExecutionPlan.sourceAudioKnown );
    ASSERT_TRUE( discoveredPlan.audioMuxExecutionPlan.sourceAudioPresent );
    ASSERT_TRUE( discoveredPlan.audioMuxExecutionPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( discoveredPlan.audioMuxExecutionPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( discoveredPlan.audioMuxExecutionPlan.audioInputHandoffReady );
    ASSERT_FALSE( discoveredPlan.audioMuxExecutionPlan.tempAudioInputOwned );
    ASSERT_TRUE( discoveredPlan.audioMuxExecutionPlan.audioMuxPlanned );
    ASSERT_TRUE( discoveredPlan.audioMuxExecutionPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_TRUE( discoveredPlan.audioMuxExecutionPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( discoveredPlan.audioMuxExecutionPlan.audioMuxOwned );
    ASSERT_FALSE( discoveredPlan.audioMuxExecutionPlan.audioMuxArgumentHandoffOwned );
    ASSERT_FALSE( discoveredPlan.audioMuxExecutionPlan.audioSyncValidationOwned );
    ASSERT_FALSE( discoveredPlan.audioMuxExecutionPlan.audioMuxReady );
    ASSERT_FALSE( discoveredPlan.audioMuxExecutionPlan.audioSyncValidationReady );
    ASSERT_FALSE( discoveredPlan.audioMuxExecutionPlan.muxExecutionReady );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( discoveredPlan.ffmpegAudioPlan.sourceAudioExtractionOwned );
    ASSERT_TRUE( discoveredPlan.ffmpegAudioPlan.audioInputContractReady );
    ASSERT_FALSE( discoveredPlan.ffmpegAudioPlan.audioInputOwned );
    ASSERT_FALSE( discoveredPlan.ffmpegAudioPlan.audioMuxOwned );
    ASSERT_FALSE( discoveredPlan.ffmpegAudioPlan.audioSyncOwned );
    ASSERT_EQ( std::string("-an"),
               std::string(discoveredPlan.ffmpegCommandPlan.audioArguments
                   .toUtf8().constData()) );
    const std::string discoveredSummary =
        std::string(batchRenderedVideoJobPlanSummary(
            discoveredPlan).toUtf8().constData());
    ASSERT_TRUE( discoveredSummary.find("source-audio-source=open-mlv-audio-metadata") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-state=present") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-sample-rate=48000") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-channels=2") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-bits=16") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-bytes=123456") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-discovery-owned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-mux-input-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-sync-validation-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-known=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-present=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-discovery-owned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-path-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-process-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-temp-file-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-cleanup-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-source=source-audio-extraction-execution-contract") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-known=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-present=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-sample-read-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-wav-write-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-temp-lifecycle-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-sample-read-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-wav-header-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-wav-sample-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-temp-open-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-temp-finalize-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-cleanup-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-process-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-temp-lifecycle-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("source-audio-extraction-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-source=ffmpeg-audio-input-handoff-contract") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-planned-args=-i \"C:/renders/M16-1327.source-audio.wav\"") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-active-args=-an") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-known=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-present=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-args-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-ownership-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-args-handoff-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-args-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-handoff-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-source=ffmpeg-audio-input-contract") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-planned-args=-i \"C:/renders/M16-1327.source-audio.wav\"") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-active-args=-an") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-known=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-present=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-source-discovery-owned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-extraction-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-audio-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-extraction-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-input-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-mux-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-sync-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-source=audio-mux-execution-contract") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-known=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-present=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-discovery-owned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-extraction-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-handoff-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-temp-input-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-mux-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-args-handoff-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-sync-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-mux-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-args-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-sync-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-mux-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-sync-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-source-discovery-owned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-mux-transition-args=deferred-until-audio-mux-execution-owned") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-extraction-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-input-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-mux-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-mux-args-handoff-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-sync-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-mux-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-mux-args-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-sync-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-mux-command-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-audio-mux-command-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-args=-an") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-transition-source=video-only-to-mux-transition-contract") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-transition-args=deferred-until-audio-mux-execution-owned") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-mux-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-mux-args-handoff-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-sync-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-mux-command-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-command-audio-mux-command-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-args=-an") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-transition-source=video-only-to-mux-transition-contract") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-transition-args=deferred-until-audio-mux-execution-owned") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-mux-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-mux-args-handoff-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-sync-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-mux-command-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-mux-command-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-audio-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-args=-an") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-transition-source=video-only-to-mux-transition-contract") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-transition-args=deferred-until-audio-mux-execution-owned") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-probe-binary-source=default-executable-name") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-probe-binary-command-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-codec-container-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-codec-container-input-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-codec-container-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-probe-command-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-probe-command-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-probe-command-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-probe-binary-source=default-executable-name") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-probe-binary-command-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-probe-command-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-probe-command-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-probe-command-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-mux-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-mux-args-handoff-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-sync-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-mux-command-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-mux-command-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ffmpeg-audio-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("ffmpeg-execution-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-codec-container-planned=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-codec-container-input-ready=true") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-codec-container-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-file-exists-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-probe-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-codec-container-owned=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("output-verification-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("runner-audio-mux-ready=false") != std::string::npos );
    ASSERT_TRUE( discoveredSummary.find("preflight-ready=true runnable=true") != std::string::npos );

    BatchRenderedVideoJobPlan settingsBasePlan =
        batchRenderedVideoJobPlanFromRequest(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders"),
            request,
            settings);
    ASSERT_TRUE( settingsBasePlan.preflightReady );
    ASSERT_TRUE( settingsBasePlan.renderSettings.explicitHeadlessSettings );

    BatchRenderedVideoJobPlan implicitSettingsPlan =
        batchRenderedVideoJobPlanWithMetadata(settingsBasePlan, metadata);
    ASSERT_TRUE( implicitSettingsPlan.metadataAttempted );
    ASSERT_TRUE( implicitSettingsPlan.metadataReady );
    ASSERT_TRUE( implicitSettingsPlan.ffmpegFrameReady );
    ASSERT_TRUE( implicitSettingsPlan.receiptApplicationContractReady );
    ASSERT_TRUE( implicitSettingsPlan.frameProcessingContractReady );
    ASSERT_TRUE( implicitSettingsPlan.ffmpegCommandReady );
    ASSERT_TRUE( implicitSettingsPlan.ffmpegExecutionContractReady );
    ASSERT_TRUE( implicitSettingsPlan.outputVerificationExecutionContractReady );
    ASSERT_TRUE( implicitSettingsPlan.preflightReady );
    ASSERT_TRUE( implicitSettingsPlan.runnable );
    ASSERT_EQ( 1920, implicitSettingsPlan.ffmpegFramePlan.outputWidth );
    ASSERT_EQ( 1284, implicitSettingsPlan.ffmpegFramePlan.outputHeight );
    ASSERT_TRUE( implicitSettingsPlan.renderSettings.explicitHeadlessSettings );
    const std::string implicitSettingsSummary =
        std::string(batchRenderedVideoJobPlanSummary(
            implicitSettingsPlan).toUtf8().constData());
    ASSERT_TRUE( implicitSettingsSummary.find("render-settings-source=explicit-headless render-settings-explicit-headless=true") != std::string::npos );
    ASSERT_TRUE( implicitSettingsSummary.find("ffmpeg-frame-size=1920x1284") != std::string::npos );

    BatchRenderedVideoRenderSettings invalidSettings =
        batchRenderedVideoRenderSettingsFromExplicitResize(
            true,
            0,
            1080,
            false);
    ASSERT_FALSE( invalidSettings.ready );
    plan = batchRenderedVideoJobPlanWithMetadata(
        basePlan,
        metadata,
        invalidSettings);
    ASSERT_TRUE( plan.metadataAttempted );
    ASSERT_TRUE( plan.metadataReady );
    ASSERT_FALSE( plan.ffmpegFrameReady );
    ASSERT_FALSE( plan.frameProcessingContractReady );
    ASSERT_FALSE( plan.ffmpegCommandReady );
    ASSERT_FALSE( plan.ffmpegExecutionContractReady );
    ASSERT_FALSE( plan.outputVerificationExecutionContractReady );
    ASSERT_FALSE( plan.preflightReady );
    ASSERT_EQ( std::string("rendered resize dimensions invalid"),
               std::string(batchRenderedVideoJobPlanFirstBlocker(plan).toUtf8().constData()) );

    metadata = batchRenderedVideoSourceMetadata(
        0,
        3872,
        24000.0 / 1001.0,
        STRETCH_H_100,
        STRETCH_V_100);
    plan = batchRenderedVideoJobPlanWithMetadata(basePlan, metadata, settings);
    ASSERT_TRUE( plan.metadataAttempted );
    ASSERT_FALSE( plan.metadataReady );
    ASSERT_FALSE( plan.ffmpegFrameReady );
    ASSERT_FALSE( plan.frameProcessingContractReady );
    ASSERT_FALSE( plan.ffmpegCommandReady );
    ASSERT_FALSE( plan.ffmpegExecutionContractReady );
    ASSERT_FALSE( plan.outputVerificationExecutionContractReady );
    ASSERT_FALSE( plan.preflightReady );
    ASSERT_EQ( std::string("rendered source dimensions invalid"),
               std::string(batchRenderedVideoJobPlanFirstBlocker(plan).toUtf8().constData()) );

    BatchRenderedVideoJobPlan invalidOutputPlan =
        batchRenderedVideoJobPlanFromRequest(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders/custom.mov"),
            request);
    plan = batchRenderedVideoJobPlanWithMetadata(
        invalidOutputPlan,
        metadata,
        settings);
    ASSERT_EQ( std::string("output path extension does not match target extension"),
               std::string(batchRenderedVideoJobPlanFirstBlocker(plan).toUtf8().constData()) );
}

TEST(BatchRunner, BuildsRenderedVideoMetadataFromClipState)
{
    BatchRenderedVideoSourceMetadata metadata =
        BatchRunner::renderedVideoSourceMetadataFromClipState(
            5792,
            3872,
            24000.0 / 1001.0,
            -1.0,
            0.0,
            240);
    ASSERT_TRUE( metadata.ready );
    ASSERT_EQ( 5792, metadata.width );
    ASSERT_EQ( 3872, metadata.height );
    ASSERT_EQ( 240, metadata.frameCount );
    ASSERT_TRUE( std::fabs(metadata.durationSeconds - 10.01) < 0.000001 );
    ASSERT_TRUE( metadata.frameCountReady );
    ASSERT_EQ( STRETCH_H_100, metadata.stretchFactorX );
    ASSERT_EQ( STRETCH_V_100, metadata.stretchFactorY );
    ASSERT_TRUE( std::string(batchRenderedVideoSourceMetadataSummary(metadata)
        .toUtf8().constData())
        .find("source-frame-count=240 source-duration-seconds=10.010000") != std::string::npos );

    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request),
            metadata);
    ASSERT_TRUE( plan.metadataAttempted );
    ASSERT_TRUE( plan.metadataReady );
    ASSERT_TRUE( plan.ffmpegFrameReady );
    ASSERT_TRUE( plan.frameProcessingContractReady );
    ASSERT_TRUE( plan.optionalFilterContractReady );
    ASSERT_TRUE( plan.audioMuxPrerequisitesContractReady );
    ASSERT_TRUE( plan.audioMuxPrerequisitesPlan.contractReady );
    ASSERT_FALSE( plan.audioMuxPrerequisitesPlan.muxReady );
    ASSERT_TRUE( plan.audioMuxExecutionContractReady );
    ASSERT_TRUE( plan.audioMuxExecutionPlan.contractReady );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioMuxPlanned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioMuxReady );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.muxExecutionReady );
    ASSERT_TRUE( plan.ffmpegCommandReady );
    ASSERT_TRUE( plan.ffmpegExecutionContractReady );
    ASSERT_TRUE( plan.outputVerificationExecutionContractReady );
    ASSERT_EQ( 240, plan.outputVerificationPlan.expectedFrameCount );
    ASSERT_TRUE( plan.outputVerificationPlan.frameCountCheckPlanned );
    ASSERT_TRUE( plan.outputVerificationPlan.frameCountExpectationReady );
    ASSERT_FALSE( plan.outputVerificationPlan.frameCountValidationReady );
    ASSERT_EQ( 240, plan.outputVerificationExecutionPlan.expectedFrameCount );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.frameCountCheckPlanned );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan.frameCountExpectationReady );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan.frameCountValidationReady );
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );
    /* E4-1: implemented, so a fully-planned job reports no blocker. */
    ASSERT_EQ( std::string(""),
               std::string(batchRenderedVideoJobPlanFirstBlocker(plan).toUtf8().constData()) );

    metadata = BatchRunner::renderedVideoSourceMetadataFromClipState(
        5792,
        3872,
        0.0,
        STRETCH_H_100,
        STRETCH_V_100);
    ASSERT_FALSE( metadata.ready );
    ASSERT_EQ( std::string("rendered frame rate invalid"),
               std::string(metadata.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoOutputPaths)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoTarget target =
        batchRenderedVideoTargetFromRequest(request);
    BatchRenderedVideoOutputPlan plan =
        batchRenderedVideoOutputPlanFromPaths(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders"),
            target);
    ASSERT_TRUE( plan.ready );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(plan.outputPath.toUtf8().constData()) );
    ASSERT_FALSE( plan.explicitFileOutput );
    ASSERT_EQ( 1, plan.inputClipCount );
    ASSERT_TRUE( plan.reason.isEmpty() );
    ASSERT_EQ( std::string("rendered-output=C:/renders/M16-1327.mp4 rendered-output-explicit-file=false rendered-output-input-clips=1 rendered-output-ready=true rendered-output-reason=none"),
               std::string(batchRenderedVideoOutputPlanSummary(
                   QStringLiteral("C:/clips/M16-1327.MLV"),
                   QStringLiteral("C:/renders"),
                   request).toUtf8().constData()) );

    plan = batchRenderedVideoOutputPlanFromPaths(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders/custom.mp4"),
        target);
    ASSERT_TRUE( plan.ready );
    ASSERT_EQ( std::string("C:/renders/custom.mp4"),
               std::string(plan.outputPath.toUtf8().constData()) );
    ASSERT_TRUE( plan.explicitFileOutput );
    ASSERT_EQ( 1, plan.inputClipCount );

    plan = batchRenderedVideoOutputPlanFromPaths(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders"),
        target,
        2);
    ASSERT_TRUE( plan.ready );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(plan.outputPath.toUtf8().constData()) );
    ASSERT_FALSE( plan.explicitFileOutput );
    ASSERT_EQ( 2, plan.inputClipCount );

    plan = batchRenderedVideoOutputPlanFromPaths(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders/custom.mp4"),
        target,
        2);
    ASSERT_FALSE( plan.ready );
    ASSERT_TRUE( plan.outputPath.isEmpty() );
    ASSERT_TRUE( plan.explicitFileOutput );
    ASSERT_EQ( 2, plan.inputClipCount );
    ASSERT_EQ( std::string("explicit rendered output file requires a single input clip"),
               std::string(plan.reason.toUtf8().constData()) );
    ASSERT_EQ( std::string("rendered-output=unspecified rendered-output-explicit-file=true rendered-output-input-clips=2 rendered-output-ready=false rendered-output-reason=explicit rendered output file requires a single input clip"),
               std::string(batchRenderedVideoOutputPlanSummary(plan).toUtf8().constData()) );

    plan = batchRenderedVideoOutputPlanFromPaths(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders/custom.mov"),
        target);
    ASSERT_FALSE( plan.ready );
    ASSERT_TRUE( plan.outputPath.isEmpty() );
    ASSERT_EQ( std::string("output path extension does not match target extension"),
               std::string(plan.reason.toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("rendered-video"));
    target = batchRenderedVideoTargetFromRequest(request);
    plan = batchRenderedVideoOutputPlanFromPaths(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders"),
        target);
    ASSERT_FALSE( plan.ready );
    ASSERT_TRUE( plan.outputPath.isEmpty() );
    ASSERT_EQ( std::string("rendered target incomplete"),
               std::string(plan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoOutputVerificationContract)
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
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target);
    ASSERT_TRUE( verificationPlan.contractReady );
    ASSERT_TRUE( verificationPlan.outputPathReady );
    ASSERT_TRUE( verificationPlan.extensionMatchesTarget );
    ASSERT_TRUE( verificationPlan.fileExistenceCheckPlanned );
    ASSERT_TRUE( verificationPlan.nonEmptyCheckPlanned );
    ASSERT_TRUE( verificationPlan.codecContainerCheckPlanned );
    ASSERT_TRUE( verificationPlan.codecContainerExpectationReady );
    ASSERT_EQ( 0, verificationPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(verificationPlan.expectedDurationSeconds)
        < 0.000001 );
    ASSERT_FALSE( verificationPlan.frameCountCheckPlanned );
    ASSERT_FALSE( verificationPlan.frameCountExpectationReady );
    ASSERT_FALSE( verificationPlan.frameCountValidationReady );
    ASSERT_EQ( static_cast<qulonglong>(1),
               verificationPlan.nonEmptyMinimumBytes );
    ASSERT_FALSE( verificationPlan.filesystemInspectionOwned );
    ASSERT_FALSE( verificationPlan.fileExistenceCheckReady );
    ASSERT_FALSE( verificationPlan.nonEmptyCheckReady );
    ASSERT_FALSE( verificationPlan.codecContainerValidationReady );
    ASSERT_FALSE( verificationPlan.fileExistenceCheckOwned );
    ASSERT_FALSE( verificationPlan.nonEmptyCheckOwned );
    ASSERT_FALSE( verificationPlan.mediaProbeOwned );
    ASSERT_FALSE( verificationPlan.codecContainerCheckOwned );
    ASSERT_FALSE( verificationPlan.frameCountCheckOwned );
    ASSERT_FALSE( verificationPlan.receiptOrHashOwned );
    ASSERT_FALSE( verificationPlan.verificationExecutionOwned );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(verificationPlan.expectedOutputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string(".mp4"),
               std::string(verificationPlan.expectedExtension.toUtf8().constData()) );
    ASSERT_EQ( std::string("h264"),
               std::string(verificationPlan.expectedCodec.toUtf8().constData()) );
    ASSERT_EQ( std::string("mp4"),
               std::string(verificationPlan.expectedContainer.toUtf8().constData()) );
    ASSERT_EQ( std::string("output-verification-source=planned-output-contract output-verification-path=C:/renders/M16-1327.mp4 output-verification-extension=.mp4 output-verification-expected-codec=h264 output-verification-expected-container=mp4 output-verification-expected-frame-count=0 output-verification-expected-duration-seconds=0.000000 output-verification-path-ready=true output-verification-extension-match=true output-verification-file-exists-planned=true output-verification-nonempty-planned=true output-verification-nonempty-min-bytes=1 output-verification-filesystem-inspection-owned=false output-verification-file-exists-ready=false output-verification-nonempty-ready=false output-verification-codec-container-planned=true output-verification-codec-container-input-ready=true output-verification-codec-container-validation-ready=false output-verification-frame-count-planned=false output-verification-frame-count-input-ready=false output-verification-frame-count-validation-ready=false output-verification-file-exists-owned=false output-verification-nonempty-owned=false output-verification-probe-owned=false output-verification-codec-container-owned=false output-verification-frame-count-owned=false output-verification-receipt-hash-owned=false output-verification-execution-owned=false output-verification-contract-ready=true output-verification-reason=none"),
               std::string(batchRenderedVideoOutputVerificationPlanSummary(
                   verificationPlan).toUtf8().constData()) );

    BatchRenderedVideoSourceMetadata metadata =
        batchRenderedVideoSourceMetadata(
            5792,
            3872,
            24000.0 / 1001.0,
            STRETCH_H_100,
            STRETCH_V_100,
            240);
    verificationPlan =
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target,
            metadata);
    ASSERT_TRUE( verificationPlan.contractReady );
    ASSERT_EQ( 240, verificationPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(
        verificationPlan.expectedDurationSeconds - 10.01) < 0.000001 );
    ASSERT_TRUE( verificationPlan.frameCountCheckPlanned );
    ASSERT_TRUE( verificationPlan.frameCountExpectationReady );
    ASSERT_FALSE( verificationPlan.frameCountValidationReady );

    BatchRenderedVideoJobPlan jobPlan =
        batchRenderedVideoJobPlanFromRequest(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders"),
            request);
    ASSERT_TRUE( jobPlan.outputVerificationContractReady );
    ASSERT_TRUE( jobPlan.outputVerificationPlan.contractReady );
    ASSERT_TRUE( jobPlan.preflightReady );
    ASSERT_TRUE( jobPlan.runnable );
    const std::string summary =
        std::string(batchRenderedVideoJobPlanSummary(jobPlan).toUtf8().constData());
    ASSERT_TRUE( summary.find("output-verification-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-file-exists-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-nonempty-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-nonempty-min-bytes=1") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-expected-frame-count=0") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-expected-duration-seconds=0.000000") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-filesystem-inspection-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-file-exists-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-nonempty-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-codec-container-planned=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-codec-container-input-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-codec-container-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-frame-count-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-frame-count-input-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-frame-count-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-file-exists-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-execution-owned=false") != std::string::npos );

    outputPlan = batchRenderedVideoOutputPlanFromPaths(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders/custom.mov"),
        target);
    verificationPlan =
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target);
    ASSERT_FALSE( verificationPlan.contractReady );
    ASSERT_FALSE( verificationPlan.outputPathReady );
    ASSERT_EQ( std::string("output path extension does not match target extension"),
               std::string(verificationPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoOutputVerificationExecutionContract)
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
        batchRenderedVideoOutputVerificationPlanFromOutput(
            outputPlan,
            target);
    BatchRenderedVideoSourceMetadata metadata =
        batchRenderedVideoSourceMetadata(
            5792,
            3872,
            24000.0 / 1001.0,
            STRETCH_H_100,
            STRETCH_V_100);
    BatchRenderedVideoFfmpegFramePlan framePlan =
        batchRenderedVideoFfmpegFramePlanFromMetadata(
            metadata,
            batchRenderedVideoDefaultRenderSettings(),
            BatchRenderedVideoEncoderProfile::H264);
    BatchRenderedVideoFfmpegCommandPlan commandPlan =
        batchRenderedVideoFfmpegCommandPlanFromParts(
            framePlan,
            batchRenderedVideoFfmpegFilterPlanForCurrentBuild(),
            batchRenderedVideoFfmpegVideoPlanFromEncoderPreset(
                batchRenderedVideoEncoderPresetFromTarget(target)),
            outputPlan);
    BatchRenderedVideoFfmpegExecutionPlan ffmpegExecutionPlan =
        batchRenderedVideoFfmpegExecutionPlanFromCommand(commandPlan);

    BatchRenderedVideoOutputVerificationExecutionPlan executionPlan =
        batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
            verificationPlan,
            ffmpegExecutionPlan);
    BatchRenderedVideoReceiptHashValidationPlan receiptHashPlan =
        batchRenderedVideoReceiptHashValidationPlanFromExecution(
            executionPlan);
    executionPlan =
        batchRenderedVideoOutputVerificationExecutionPlanWithReceiptHashValidation(
            executionPlan,
            receiptHashPlan);
    ASSERT_TRUE( executionPlan.contractReady );
    ASSERT_TRUE( executionPlan.outputVerificationContractReady );
    ASSERT_TRUE( executionPlan.ffmpegExecutionContractReady );
    ASSERT_EQ( std::string("-an"),
               std::string(executionPlan.ffmpegAudioArguments.toUtf8().constData()) );
    ASSERT_EQ( std::string("video-only-to-mux-transition-contract"),
               std::string(executionPlan.ffmpegAudioTransitionSource.toUtf8().constData()) );
    ASSERT_TRUE( executionPlan.ffmpegAudioTransitionArguments.isEmpty() );
    ASSERT_TRUE( executionPlan.ffmpegAudioContractReady );
    ASSERT_TRUE( executionPlan.ffmpegAudioMuxExecutionContractReady );
    ASSERT_FALSE( executionPlan.ffmpegAudioMuxPlanned );
    ASSERT_FALSE( executionPlan.ffmpegAudioMuxArgumentHandoffPlanned );
    ASSERT_FALSE( executionPlan.ffmpegAudioSyncValidationPlanned );
    ASSERT_FALSE( executionPlan.ffmpegAudioMuxExecutionReady );
    ASSERT_FALSE( executionPlan.ffmpegMuxedAudioCommandPlanned );
    ASSERT_FALSE( executionPlan.ffmpegMuxedAudioCommandReady );
    ASSERT_FALSE( executionPlan.ffmpegAudioInputOwned );
    ASSERT_TRUE( executionPlan.fileExistenceCheckPlanned );
    ASSERT_TRUE( executionPlan.nonEmptyCheckPlanned );
    ASSERT_TRUE( executionPlan.codecContainerCheckPlanned );
    ASSERT_TRUE( executionPlan.codecContainerExpectationReady );
    ASSERT_EQ( 0, executionPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(executionPlan.expectedDurationSeconds)
        < 0.000001 );
    ASSERT_FALSE( executionPlan.frameCountCheckPlanned );
    ASSERT_FALSE( executionPlan.frameCountExpectationReady );
    ASSERT_FALSE( executionPlan.frameCountValidationReady );
    ASSERT_EQ( static_cast<qulonglong>(1),
               executionPlan.nonEmptyMinimumBytes );
    ASSERT_FALSE( executionPlan.filesystemInspectionOwned );
    ASSERT_FALSE( executionPlan.fileExistenceCheckReady );
    ASSERT_FALSE( executionPlan.nonEmptyCheckReady );
    ASSERT_FALSE( executionPlan.codecContainerValidationReady );
    ASSERT_FALSE( executionPlan.fileExistenceCheckOwned );
    ASSERT_FALSE( executionPlan.nonEmptyCheckOwned );
    ASSERT_FALSE( executionPlan.mediaProbeExecutionOwned );
    ASSERT_FALSE( executionPlan.codecContainerValidationOwned );
    ASSERT_FALSE( executionPlan.frameCountValidationOwned );
    ASSERT_TRUE( executionPlan.receiptHashValidationContractReady );
    ASSERT_TRUE( executionPlan.receiptHashValidationPlanned );
    ASSERT_TRUE( executionPlan.receiptComparisonPlanned );
    ASSERT_TRUE( executionPlan.outputHashComparisonPlanned );
    ASSERT_FALSE( executionPlan.receiptReadOwned );
    ASSERT_FALSE( executionPlan.outputHashReadOwned );
    ASSERT_FALSE( executionPlan.receiptComparisonOwned );
    ASSERT_FALSE( executionPlan.outputHashComparisonOwned );
    ASSERT_FALSE( executionPlan.receiptComparisonReady );
    ASSERT_FALSE( executionPlan.outputHashComparisonReady );
    ASSERT_FALSE( executionPlan.receiptHashValidationOwned );
    ASSERT_FALSE( executionPlan.receiptHashValidationReady );
    ASSERT_FALSE( executionPlan.verificationExecutionReady );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(executionPlan.mediaProbeExecutable.toUtf8().constData()) );
    ASSERT_EQ( std::string("default-executable-name"),
               std::string(executionPlan.mediaProbeBinarySource
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(executionPlan.mediaProbeRequestedExecutable
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("ffprobe"),
               std::string(executionPlan.mediaProbeResolvedExecutable
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("h264"),
               std::string(executionPlan.expectedCodec.toUtf8().constData()) );
    ASSERT_EQ( std::string("mp4"),
               std::string(executionPlan.expectedContainer.toUtf8().constData()) );
    ASSERT_FALSE( executionPlan.mediaProbePathSearchOwned );
    ASSERT_FALSE( executionPlan.mediaProbePathSearchAttempted );
    ASSERT_FALSE( executionPlan.mediaProbeFoundOnPath );
    ASSERT_TRUE( executionPlan.mediaProbeCommandReady );
    ASSERT_TRUE( executionPlan.mediaProbeJsonContractReady );
    ASSERT_TRUE( executionPlan.mediaProbeJsonStdoutCapturePlanned );
    ASSERT_TRUE( executionPlan.mediaProbeJsonStderrCapturePlanned );
    ASSERT_TRUE( executionPlan.mediaProbeJsonExitCodeValidationPlanned );
    ASSERT_TRUE( executionPlan.mediaProbeJsonTimeoutPlanned );
    ASSERT_TRUE( executionPlan.mediaProbeJsonErrorReportingPlanned );
    ASSERT_TRUE( executionPlan.mediaProbeJsonDocumentPlanned );
    ASSERT_FALSE( executionPlan.mediaProbeJsonStdoutCaptureOwned );
    ASSERT_FALSE( executionPlan.mediaProbeJsonStderrCaptureOwned );
    ASSERT_FALSE( executionPlan.mediaProbeJsonExitCodeValidationOwned );
    ASSERT_FALSE( executionPlan.mediaProbeJsonTimeoutOwned );
    ASSERT_FALSE( executionPlan.mediaProbeJsonRawOutputOwned );
    ASSERT_FALSE( executionPlan.mediaProbeJsonParseOwned );
    ASSERT_FALSE( executionPlan.mediaProbeJsonRawOutputReady );
    ASSERT_FALSE( executionPlan.mediaProbeJsonParseReady );
    ASSERT_FALSE( executionPlan.mediaProbeJsonErrorFree );
    ASSERT_FALSE( executionPlan.mediaProbeJsonReady );
    ASSERT_TRUE( executionPlan.mediaProbeResultContractReady );
    ASSERT_TRUE( executionPlan.mediaProbeResultParsingPlanned );
    ASSERT_TRUE( executionPlan.mediaProbeResultSchemaReady );
    ASSERT_TRUE( executionPlan.mediaProbeResultCodecContainerFieldsPlanned );
    ASSERT_FALSE( executionPlan.mediaProbeResultFrameCountFieldPlanned );
    ASSERT_FALSE( executionPlan.mediaProbeResultDurationFieldPlanned );
    ASSERT_FALSE( executionPlan.mediaProbeResultOwned );
    ASSERT_FALSE( executionPlan.mediaProbeResultJsonParseOwned );
    ASSERT_FALSE( executionPlan.mediaProbeResultCodecContainerReady );
    ASSERT_FALSE( executionPlan.mediaProbeResultFrameCountReady );
    ASSERT_FALSE( executionPlan.mediaProbeResultDurationReady );
    ASSERT_FALSE( executionPlan.mediaProbeResultReady );
    const std::string executionSummary =
        std::string(batchRenderedVideoOutputVerificationExecutionPlanSummary(
            executionPlan).toUtf8().constData());
    ASSERT_TRUE( executionSummary.find("output-verification-exec-source=post-ffmpeg-output-verification-contract") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-path=C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-extension=.mp4") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-expected-frame-count=0") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-expected-duration-seconds=0.000000") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe=ffprobe") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-file-exists-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-nonempty-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-codec-container-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-codec-container-input-ready=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-codec-container-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-frame-count-planned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-frame-count-input-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-frame-count-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-codec-container-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-receipt-hash-source=output-verification-receipt-hash-validation-contract") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-receipt-hash-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-receipt-hash-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-receipt-compare-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-output-hash-compare-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-receipt-read-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-output-hash-read-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-receipt-compare-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-output-hash-compare-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-receipt-compare-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-output-hash-compare-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-receipt-hash-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-receipt-hash-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-command-line=ffprobe -v error -show_format -show_streams -of json C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-source=output-verification-probe-json-contract") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-stdout-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-stderr-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-exit-code-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-timeout-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-error-reporting-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-document-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-stdout-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-stderr-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-exit-code-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-timeout-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-raw-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-parse-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-raw-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-parse-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-error-free=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-json-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-source=output-verification-probe-result-contract") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-parse-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-schema-ready=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-codec-container-fields-planned=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-frame-count-field-planned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-duration-field-planned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-json-parse-owned=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-ready=false") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-probe-result-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( executionSummary.find("output-verification-exec-reason=none") != std::string::npos );

    BatchRenderedVideoFfmpegExecutionPlan invalidFfmpegExecutionPlan;
    executionPlan =
        batchRenderedVideoOutputVerificationExecutionPlanFromContracts(
            verificationPlan,
            invalidFfmpegExecutionPlan);
    ASSERT_FALSE( executionPlan.contractReady );
    ASSERT_TRUE( executionPlan.outputVerificationContractReady );
    ASSERT_FALSE( executionPlan.ffmpegExecutionContractReady );
    ASSERT_TRUE( executionPlan.mediaProbeJsonContractReady );
    ASSERT_TRUE( executionPlan.mediaProbeResultContractReady );
    ASSERT_EQ( std::string("rendered ffmpeg execution contract unavailable"),
               std::string(executionPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoReceiptHashValidationContract)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request),
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100,
                240));

    ASSERT_TRUE( plan.outputVerificationExecutionContractReady );
    ASSERT_TRUE( plan.receiptHashValidationContractReady );
    ASSERT_TRUE( plan.receiptHashValidationPlan.contractReady );
    ASSERT_EQ( std::string("output-verification-receipt-hash-validation-contract"),
               std::string(plan.receiptHashValidationPlan.source
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(plan.receiptHashValidationPlan
                   .expectedOutputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string(".mp4"),
               std::string(plan.receiptHashValidationPlan
                   .expectedExtension.toUtf8().constData()) );
    ASSERT_EQ( std::string("h264"),
               std::string(plan.receiptHashValidationPlan
                   .expectedCodec.toUtf8().constData()) );
    ASSERT_EQ( std::string("mp4"),
               std::string(plan.receiptHashValidationPlan
                   .expectedContainer.toUtf8().constData()) );
    ASSERT_EQ( 240, plan.receiptHashValidationPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(plan.receiptHashValidationPlan
        .expectedDurationSeconds - 10.01) < 0.000001 );
    ASSERT_TRUE( plan.receiptHashValidationPlan
        .outputVerificationExecutionContractReady );
    ASSERT_TRUE( plan.receiptHashValidationPlan.validationPlanned );
    ASSERT_TRUE( plan.receiptHashValidationPlan.receiptComparisonPlanned );
    ASSERT_TRUE( plan.receiptHashValidationPlan.outputHashComparisonPlanned );
    ASSERT_FALSE( plan.receiptHashValidationPlan.receiptReadOwned );
    ASSERT_FALSE( plan.receiptHashValidationPlan.outputHashReadOwned );
    ASSERT_FALSE( plan.receiptHashValidationPlan.receiptComparisonOwned );
    ASSERT_FALSE( plan.receiptHashValidationPlan.outputHashComparisonOwned );
    ASSERT_FALSE( plan.receiptHashValidationPlan.receiptComparisonReady );
    ASSERT_FALSE( plan.receiptHashValidationPlan.outputHashComparisonReady );
    ASSERT_FALSE( plan.receiptHashValidationPlan.receiptHashValidationOwned );
    ASSERT_FALSE( plan.receiptHashValidationPlan.receiptHashValidationReady );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan
        .receiptHashValidationContractReady );
    ASSERT_TRUE( plan.outputVerificationExecutionPlan
        .receiptHashValidationPlanned );
    ASSERT_FALSE( plan.outputVerificationExecutionPlan
        .receiptHashValidationReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .receiptHashValidationContractReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .receiptHashValidationPlanned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .receiptHashValidationReady );
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );

    const std::string receiptHashSummary =
        std::string(batchRenderedVideoReceiptHashValidationPlanSummary(
            plan.receiptHashValidationPlan).toUtf8().constData());
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-source=output-verification-receipt-hash-validation-contract") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-path=C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-expected-frame-count=240") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-expected-duration-seconds=10.010000") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-validation-planned=true") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-receipt-compare-planned=true") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-output-hash-planned=true") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-receipt-read-owned=false") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-output-hash-read-owned=false") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-receipt-compare-owned=false") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-output-hash-compare-owned=false") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-receipt-compare-ready=false") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-output-hash-ready=false") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-owned=false") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-ready=false") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( receiptHashSummary.find("output-verification-receipt-hash-reason=none") != std::string::npos );

    const std::string jobSummary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( jobSummary.find("output-verification-receipt-hash-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-exec-receipt-hash-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-decision-receipt-hash-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-decision-accepted=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("runner-output-verification-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("runnable=true") != std::string::npos );

    BatchRenderedVideoReceiptHashValidationPlan blockedPlan =
        batchRenderedVideoReceiptHashValidationPlanFromExecution(
            BatchRenderedVideoOutputVerificationExecutionPlan());
    ASSERT_FALSE( blockedPlan.contractReady );
    ASSERT_FALSE( blockedPlan.outputVerificationExecutionContractReady );
    ASSERT_FALSE( blockedPlan.validationPlanned );
    ASSERT_EQ( std::string("rendered receipt/hash validation contract unavailable"),
               std::string(blockedPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoOutputVerificationDecisionContract)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request),
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100,
                240));

    ASSERT_TRUE( plan.outputVerificationExecutionContractReady );
    ASSERT_TRUE( plan.outputVerificationDecisionContractReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan.contractReady );
    ASSERT_EQ( std::string("output-verification-decision-contract"),
               std::string(plan.outputVerificationDecisionPlan.source
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(plan.outputVerificationDecisionPlan
                   .expectedOutputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string(".mp4"),
               std::string(plan.outputVerificationDecisionPlan
                   .expectedExtension.toUtf8().constData()) );
    ASSERT_EQ( std::string("h264"),
               std::string(plan.outputVerificationDecisionPlan
                   .expectedCodec.toUtf8().constData()) );
    ASSERT_EQ( std::string("mp4"),
               std::string(plan.outputVerificationDecisionPlan
                   .expectedContainer.toUtf8().constData()) );
    ASSERT_EQ( 240, plan.outputVerificationDecisionPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(plan.outputVerificationDecisionPlan
        .expectedDurationSeconds - 10.01) < 0.000001 );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .outputVerificationExecutionContractReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan.fileExistenceCheckPlanned );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan.nonEmptyCheckPlanned );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan.fileChecksPlanned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan.filesystemInspectionOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan.fileExistenceCheckOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan.nonEmptyCheckOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan.fileChecksOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan.fileExistenceCheckReady );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan.nonEmptyCheckReady );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan.fileChecksReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .mediaProbeValidationContractReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .mediaProbeValidationPlanned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .mediaProbeValidationOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .mediaProbeValidationReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .codecContainerComparisonPlanned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .codecContainerComparisonReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .frameCountComparisonPlanned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .frameCountComparisonReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .durationComparisonPlanned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .durationComparisonReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .receiptHashValidationContractReady );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .receiptHashValidationPlanned );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .receiptComparisonPlanned );
    ASSERT_TRUE( plan.outputVerificationDecisionPlan
        .outputHashComparisonPlanned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .receiptReadOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .outputHashReadOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .receiptComparisonOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .outputHashComparisonOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .receiptComparisonReady );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .outputHashComparisonReady );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .receiptHashValidationOwned );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .receiptHashValidationReady );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .verificationExecutionReady );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan
        .verificationDecisionReady );
    ASSERT_FALSE( plan.outputVerificationDecisionPlan.outputAccepted );
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );

    const std::string decisionSummary =
        std::string(batchRenderedVideoOutputVerificationDecisionPlanSummary(
            plan.outputVerificationDecisionPlan).toUtf8().constData());
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-source=output-verification-decision-contract") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-path=C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-expected-frame-count=240") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-expected-duration-seconds=10.010000") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-file-checks-planned=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-file-checks-owned=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-file-checks-ready=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-probe-validation-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-probe-validation-planned=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-probe-validation-owned=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-probe-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-codec-container-planned=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-frame-count-planned=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-duration-planned=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-hash-source=output-verification-receipt-hash-validation-contract") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-hash-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-hash-planned=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-compare-planned=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-output-hash-compare-planned=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-read-owned=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-output-hash-read-owned=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-compare-owned=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-output-hash-compare-owned=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-compare-ready=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-output-hash-compare-ready=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-hash-owned=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-hash-ready=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-verification-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-ready=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-accepted=false") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-receipt-hash-reason=none") != std::string::npos );
    ASSERT_TRUE( decisionSummary.find("output-verification-decision-reason=none") != std::string::npos );

    const std::string jobSummary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( jobSummary.find("output-verification-decision-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-decision-accepted=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("runner-output-verification-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("runnable=true") != std::string::npos );

    BatchRenderedVideoOutputVerificationDecisionPlan blockedPlan =
        batchRenderedVideoOutputVerificationDecisionPlanFromExecution(
            BatchRenderedVideoOutputVerificationExecutionPlan());
    ASSERT_FALSE( blockedPlan.contractReady );
    ASSERT_FALSE( blockedPlan.outputVerificationExecutionContractReady );
    ASSERT_FALSE( blockedPlan.receiptHashValidationPlanned );
    ASSERT_EQ( std::string("rendered output verification decision contract unavailable"),
               std::string(blockedPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoOutputVerificationResultContract)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request),
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100,
                240));

    ASSERT_TRUE( plan.outputVerificationDecisionContractReady );
    ASSERT_TRUE( plan.outputVerificationResultContractReady );
    ASSERT_TRUE( plan.outputVerificationResultPlan.contractReady );
    ASSERT_EQ( std::string("output-verification-result-report-contract"),
               std::string(plan.outputVerificationResultPlan.source
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(plan.outputVerificationResultPlan
                   .expectedOutputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string(".mp4"),
               std::string(plan.outputVerificationResultPlan
                   .expectedExtension.toUtf8().constData()) );
    ASSERT_EQ( std::string("h264"),
               std::string(plan.outputVerificationResultPlan
                   .expectedCodec.toUtf8().constData()) );
    ASSERT_EQ( std::string("mp4"),
               std::string(plan.outputVerificationResultPlan
                   .expectedContainer.toUtf8().constData()) );
    ASSERT_EQ( 240, plan.outputVerificationResultPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(plan.outputVerificationResultPlan
        .expectedDurationSeconds - 10.01) < 0.000001 );
    ASSERT_EQ( std::string("blocked"),
               std::string(plan.outputVerificationResultPlan.resultState
                   .toUtf8().constData()) );
    ASSERT_TRUE( plan.outputVerificationResultPlan
        .outputVerificationDecisionContractReady );
    ASSERT_FALSE( plan.outputVerificationResultPlan.fileChecksReady );
    ASSERT_FALSE( plan.outputVerificationResultPlan.mediaProbeValidationReady );
    ASSERT_FALSE( plan.outputVerificationResultPlan.receiptHashValidationReady );
    ASSERT_FALSE( plan.outputVerificationResultPlan.verificationExecutionReady );
    ASSERT_FALSE( plan.outputVerificationResultPlan.verificationDecisionReady );
    ASSERT_FALSE( plan.outputVerificationResultPlan.outputAccepted );
    ASSERT_TRUE( plan.outputVerificationResultPlan.resultReportPlanned );
    ASSERT_TRUE( plan.outputVerificationResultPlan.failureReportPlanned );
    ASSERT_FALSE( plan.outputVerificationResultPlan.acceptedOutputReportPlanned );
    ASSERT_FALSE( plan.outputVerificationResultPlan.resultReportOwned );
    ASSERT_FALSE( plan.outputVerificationResultPlan.failureReportOwned );
    ASSERT_FALSE( plan.outputVerificationResultPlan.acceptedOutputReportOwned );
    ASSERT_FALSE( plan.outputVerificationResultPlan.resultReportReady );
    ASSERT_FALSE( plan.outputVerificationResultPlan.failureReportReady );
    ASSERT_FALSE( plan.outputVerificationResultPlan.acceptedOutputReportReady );
    ASSERT_FALSE( plan.outputVerificationResultPlan.outputVerificationReady );
    ASSERT_EQ( std::string("rendered output verification execution is not ready"),
               std::string(plan.outputVerificationResultPlan.failureReason
                   .toUtf8().constData()) );
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );

    const std::string resultSummary =
        std::string(batchRenderedVideoOutputVerificationResultPlanSummary(
            plan.outputVerificationResultPlan).toUtf8().constData());
    ASSERT_TRUE( resultSummary.find("output-verification-result-source=output-verification-result-report-contract") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-path=C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-expected-frame-count=240") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-expected-duration-seconds=10.010000") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-state=blocked") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-decision-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-file-checks-ready=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-probe-validation-ready=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-receipt-hash-ready=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-verification-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-decision-ready=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-accepted=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-report-planned=true") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-failure-report-planned=true") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-accepted-report-planned=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-report-owned=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-failure-report-owned=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-accepted-report-owned=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-report-ready=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-failure-report-ready=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-accepted-report-ready=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-failure-reason=rendered output verification execution is not ready") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( resultSummary.find("output-verification-result-reason=none") != std::string::npos );

    const std::string jobSummary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( jobSummary.find("output-verification-result-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-result-accepted=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-result-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("runner-output-verification-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("runnable=true") != std::string::npos );

    BatchRenderedVideoOutputVerificationResultPlan blockedPlan =
        batchRenderedVideoOutputVerificationResultPlanFromDecision(
            BatchRenderedVideoOutputVerificationDecisionPlan());
    ASSERT_FALSE( blockedPlan.contractReady );
    ASSERT_FALSE( blockedPlan.outputVerificationDecisionContractReady );
    ASSERT_FALSE( blockedPlan.resultReportPlanned );
    ASSERT_EQ( std::string("rendered output verification result report contract unavailable"),
               std::string(blockedPlan.reason.toUtf8().constData()) );
}

TEST(BatchExportFormat, PlansRenderedVideoOutputVerificationReportContract)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanWithMetadata(
            batchRenderedVideoJobPlanFromRequest(
                QStringLiteral("C:/clips/M16-1327.MLV"),
                QStringLiteral("C:/renders"),
                request),
            batchRenderedVideoSourceMetadata(
                5792,
                3872,
                24000.0 / 1001.0,
                STRETCH_H_100,
                STRETCH_V_100,
                240));

    ASSERT_TRUE( plan.outputVerificationResultContractReady );
    ASSERT_TRUE( plan.outputVerificationReportContractReady );
    ASSERT_TRUE( plan.outputVerificationReportPlan.contractReady );
    ASSERT_EQ( std::string("output-verification-report-artifact-contract"),
               std::string(plan.outputVerificationReportPlan.source
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(plan.outputVerificationReportPlan
                   .expectedOutputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string(".mp4"),
               std::string(plan.outputVerificationReportPlan
                   .expectedExtension.toUtf8().constData()) );
    ASSERT_EQ( std::string("h264"),
               std::string(plan.outputVerificationReportPlan
                   .expectedCodec.toUtf8().constData()) );
    ASSERT_EQ( std::string("mp4"),
               std::string(plan.outputVerificationReportPlan
                   .expectedContainer.toUtf8().constData()) );
    ASSERT_EQ( 240, plan.outputVerificationReportPlan.expectedFrameCount );
    ASSERT_TRUE( std::fabs(plan.outputVerificationReportPlan
        .expectedDurationSeconds - 10.01) < 0.000001 );
    ASSERT_EQ( std::string("blocked"),
               std::string(plan.outputVerificationReportPlan.resultState
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("failure"),
               std::string(plan.outputVerificationReportPlan.reportKind
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.verification-report.json"),
               std::string(plan.outputVerificationReportPlan.reportPath
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("json"),
               std::string(plan.outputVerificationReportPlan.reportFormat
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("rendered-output-verification-report.v1"),
               std::string(plan.outputVerificationReportPlan.reportSchema
                   .toUtf8().constData()) );
    ASSERT_TRUE( plan.outputVerificationReportPlan
        .outputVerificationResultContractReady );
    ASSERT_TRUE( plan.outputVerificationReportPlan.resultReportPlanned );
    ASSERT_TRUE( plan.outputVerificationReportPlan.failureReportPlanned );
    ASSERT_FALSE( plan.outputVerificationReportPlan.acceptedOutputReportPlanned );
    ASSERT_FALSE( plan.outputVerificationReportPlan.outputAccepted );
    ASSERT_TRUE( plan.outputVerificationReportPlan.reportArtifactPlanned );
    ASSERT_TRUE( plan.outputVerificationReportPlan.reportPathPlanned );
    ASSERT_TRUE( plan.outputVerificationReportPlan.reportPathReady );
    ASSERT_TRUE( plan.outputVerificationReportPlan.reportFormatReady );
    ASSERT_TRUE( plan.outputVerificationReportPlan.reportSchemaReady );
    ASSERT_TRUE( plan.outputVerificationReportPlan.reportContentPlanned );
    ASSERT_TRUE( plan.outputVerificationReportPlan.failureReportContentPlanned );
    ASSERT_FALSE( plan.outputVerificationReportPlan.acceptedReportContentPlanned );
    ASSERT_FALSE( plan.outputVerificationReportPlan.reportFileCreationPlanned );
    ASSERT_FALSE( plan.outputVerificationReportPlan.reportWritePlanned );
    ASSERT_FALSE( plan.outputVerificationReportPlan.reportFileCreationOwned );
    ASSERT_FALSE( plan.outputVerificationReportPlan.reportWriteOwned );
    ASSERT_FALSE( plan.outputVerificationReportPlan.reportFileReady );
    ASSERT_FALSE( plan.outputVerificationReportPlan.reportWriteReady );
    ASSERT_FALSE( plan.outputVerificationReportPlan.reportReady );
    ASSERT_FALSE( plan.outputVerificationReportPlan.outputVerificationReady );
    ASSERT_EQ( std::string("rendered output verification execution is not ready"),
               std::string(plan.outputVerificationReportPlan.failureReason
                   .toUtf8().constData()) );
    ASSERT_TRUE( plan.outputVerificationReportContentContractReady );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan.contractReady );
    ASSERT_EQ( std::string("output-verification-report-content-schema-contract"),
               std::string(plan.outputVerificationReportContentPlan.source
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(plan.outputVerificationReportContentPlan
                   .expectedOutputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.verification-report.json"),
               std::string(plan.outputVerificationReportContentPlan
                   .reportPath.toUtf8().constData()) );
    ASSERT_EQ( std::string("json"),
               std::string(plan.outputVerificationReportContentPlan
                   .reportFormat.toUtf8().constData()) );
    ASSERT_EQ( std::string("rendered-output-verification-report.v1"),
               std::string(plan.outputVerificationReportContentPlan
                   .reportSchema.toUtf8().constData()) );
    ASSERT_EQ( std::string("failure"),
               std::string(plan.outputVerificationReportContentPlan
                   .reportKind.toUtf8().constData()) );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan
        .outputVerificationReportContractReady );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan.reportPathReady );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan.reportFormatReady );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan.reportSchemaReady );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan.reportKindReady );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan.reportContentPlanned );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan
        .failureReportContentPlanned );
    ASSERT_FALSE( plan.outputVerificationReportContentPlan
        .acceptedReportContentPlanned );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan
        .topLevelKeysPlanned );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan
        .expectedOutputKeysPlanned );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan
        .verificationCheckKeysPlanned );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan
        .artifactKeysPlanned );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan
        .errorCategoriesPlanned );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan
        .activeErrorCategoryPlanned );
    ASSERT_TRUE( plan.outputVerificationReportContentPlan
        .schemaDetailContractReady );
    ASSERT_FALSE( plan.outputVerificationReportContentPlan
        .jsonSerializationPlanned );
    ASSERT_FALSE( plan.outputVerificationReportContentPlan
        .jsonSerializationOwned );
    ASSERT_FALSE( plan.outputVerificationReportContentPlan.reportWritePlanned );
    ASSERT_FALSE( plan.outputVerificationReportContentPlan.reportWriteOwned );
    ASSERT_FALSE( plan.outputVerificationReportContentPlan.reportReady );
    ASSERT_FALSE( plan.outputVerificationReportContentPlan
        .outputVerificationReady );
    ASSERT_EQ( std::string("output-verification-execution-not-ready"),
               std::string(plan.outputVerificationReportContentPlan
                   .activeErrorCategory.toUtf8().constData()) );
    ASSERT_TRUE( plan.outputVerificationReportWriterContractReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan.contractReady );
    ASSERT_EQ( std::string("output-verification-report-writer-preflight-contract"),
               std::string(plan.outputVerificationReportWriterPlan.source
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("output-verification-report-content-schema-contract"),
               std::string(plan.outputVerificationReportWriterPlan
                   .serializationInput.toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(plan.outputVerificationReportWriterPlan
                   .expectedOutputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.verification-report.json"),
               std::string(plan.outputVerificationReportWriterPlan
                   .reportPath.toUtf8().constData()) );
    ASSERT_EQ( std::string("C:/renders/M16-1327.verification-report.json.tmp"),
               std::string(plan.outputVerificationReportWriterPlan
                   .temporaryReportPath.toUtf8().constData()) );
    ASSERT_EQ( std::string("json"),
               std::string(plan.outputVerificationReportWriterPlan
                   .reportFormat.toUtf8().constData()) );
    ASSERT_EQ( std::string("rendered-output-verification-report.v1"),
               std::string(plan.outputVerificationReportWriterPlan
                   .reportSchema.toUtf8().constData()) );
    ASSERT_EQ( std::string("utf-8"),
               std::string(plan.outputVerificationReportWriterPlan
                   .reportEncoding.toUtf8().constData()) );
    ASSERT_EQ( std::string("atomic-json-sidecar"),
               std::string(plan.outputVerificationReportWriterPlan
                   .writeMode.toUtf8().constData()) );
    ASSERT_EQ( std::string("failure"),
               std::string(plan.outputVerificationReportWriterPlan
                   .reportKind.toUtf8().constData()) );
    ASSERT_EQ( std::string("output-verification-execution-not-ready"),
               std::string(plan.outputVerificationReportWriterPlan
                   .activeErrorCategory.toUtf8().constData()) );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan
        .outputVerificationReportContentContractReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan
        .schemaDetailContractReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan.reportPathReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan
        .temporaryReportPathPlanned );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan
        .temporaryReportPathReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan.reportFormatReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan.reportSchemaReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan.reportEncodingReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan.writeModeReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan
        .writerPreflightPlanned );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan
        .serializationInputReady );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan
        .serializationPreflightPlanned );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan
        .parentDirectoryPreflightPlanned );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan
        .fileCreationPreflightPlanned );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan.writePreflightPlanned );
    ASSERT_TRUE( plan.outputVerificationReportWriterPlan.cleanupPreflightPlanned );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan
        .jsonSerializationPlanned );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan
        .jsonSerializationOwned );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan
        .jsonSerializationReady );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan.parentDirectoryOwned );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan
        .reportFileCreationPlanned );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan
        .reportFileCreationOwned );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan.reportWritePlanned );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan.reportWriteOwned );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan.reportFileReady );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan.reportWriteReady );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan.reportReady );
    ASSERT_FALSE( plan.outputVerificationReportWriterPlan
        .outputVerificationReady );
    ASSERT_TRUE( plan.preflightReady );
    ASSERT_TRUE( plan.runnable );

    const std::string reportSummary =
        std::string(batchRenderedVideoOutputVerificationReportPlanSummary(
            plan.outputVerificationReportPlan).toUtf8().constData());
    ASSERT_TRUE( reportSummary.find("output-verification-report-source=output-verification-report-artifact-contract") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-output-path=C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-expected-codec=h264") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-expected-container=mp4") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-expected-frame-count=240") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-expected-duration-seconds=10.010000") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-result-state=blocked") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-kind=failure") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-path=C:/renders/M16-1327.verification-report.json") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-format=json") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-schema=rendered-output-verification-report.v1") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-result-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-result-report-planned=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-failure-report-planned=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-accepted-report-planned=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-output-accepted=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-artifact-planned=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-path-planned=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-path-ready=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-format-ready=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-schema-ready=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-content-planned=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-failure-content-planned=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-accepted-content-planned=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-file-create-planned=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-write-planned=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-file-create-owned=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-write-owned=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-file-ready=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-write-ready=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-ready=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-failure-reason=rendered output verification execution is not ready") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( reportSummary.find("output-verification-report-reason=none") != std::string::npos );

    const std::string contentSummary =
        std::string(batchRenderedVideoOutputVerificationReportContentPlanSummary(
            plan.outputVerificationReportContentPlan).toUtf8().constData());
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-source=output-verification-report-content-schema-contract") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-output-path=C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-result-state=blocked") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-kind=failure") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-path=C:/renders/M16-1327.verification-report.json") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-format=json") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-schema=rendered-output-verification-report.v1") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-top-level-keys=schema|kind|state|expected|checks|failure|artifacts") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-expected-keys=path|extension|codec|container|frame_count|duration_seconds") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-check-keys=file_exists|file_non_empty|ffprobe_json|codec_container|frame_count|duration|receipt_hash") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-artifact-keys=rendered_output|verification_report|ffprobe_command|receipt_sidecar") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-error-categories=runner-prerequisite|file-check|media-probe-execution|media-probe-json|codec-container-mismatch|frame-count-mismatch|duration-mismatch|receipt-hash-mismatch|report-write") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-active-error-category=output-verification-execution-not-ready") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-report-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-schema-detail-ready=true") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-json-serialization-planned=false") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-write-planned=false") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-report-ready=false") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-failure-reason=rendered output verification execution is not ready") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( contentSummary.find("output-verification-report-content-reason=none") != std::string::npos );

    const std::string writerSummary =
        std::string(batchRenderedVideoOutputVerificationReportWriterPlanSummary(
            plan.outputVerificationReportWriterPlan).toUtf8().constData());
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-source=output-verification-report-writer-preflight-contract") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-input=output-verification-report-content-schema-contract") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-output-path=C:/renders/M16-1327.mp4") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-report-path=C:/renders/M16-1327.verification-report.json") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-temp-path=C:/renders/M16-1327.verification-report.json.tmp") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-format=json") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-schema=rendered-output-verification-report.v1") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-encoding=utf-8") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-mode=atomic-json-sidecar") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-kind=failure") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-active-error-category=output-verification-execution-not-ready") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-content-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-schema-detail-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-path-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-temp-path-planned=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-temp-path-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-format-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-schema-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-encoding-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-mode-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-preflight-planned=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-serialization-input-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-serialization-preflight-planned=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-parent-dir-preflight-planned=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-file-create-preflight-planned=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-write-preflight-planned=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-cleanup-preflight-planned=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-json-serialization-planned=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-json-serialization-owned=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-json-serialization-ready=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-parent-dir-owned=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-file-create-planned=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-file-create-owned=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-write-planned=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-write-owned=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-file-ready=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-write-ready=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-report-ready=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-failure-reason=rendered output verification execution is not ready") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( writerSummary.find("output-verification-report-writer-reason=none") != std::string::npos );

    const std::string jobSummary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( jobSummary.find("output-verification-report-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-path=C:/renders/M16-1327.verification-report.json") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-write-planned=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-content-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-content-top-level-keys=schema|kind|state|expected|checks|failure|artifacts") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-content-write-planned=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-content-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-writer-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-writer-temp-path=C:/renders/M16-1327.verification-report.json.tmp") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-writer-json-serialization-planned=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-writer-write-planned=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("output-verification-report-writer-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("runner-output-verification-ready=true") != std::string::npos );
    ASSERT_TRUE( jobSummary.find("runnable=true") != std::string::npos );

    BatchRenderedVideoOutputVerificationReportPlan blockedPlan =
        batchRenderedVideoOutputVerificationReportPlanFromResult(
            BatchRenderedVideoOutputVerificationResultPlan());
    ASSERT_FALSE( blockedPlan.contractReady );
    ASSERT_FALSE( blockedPlan.outputVerificationResultContractReady );
    ASSERT_FALSE( blockedPlan.reportArtifactPlanned );
    ASSERT_FALSE( blockedPlan.reportPathPlanned );
    ASSERT_EQ( std::string("rendered output verification report artifact contract unavailable"),
               std::string(blockedPlan.reason.toUtf8().constData()) );
    BatchRenderedVideoOutputVerificationReportContentPlan blockedContentPlan =
        batchRenderedVideoOutputVerificationReportContentPlanFromReport(
            BatchRenderedVideoOutputVerificationReportPlan());
    ASSERT_FALSE( blockedContentPlan.contractReady );
    ASSERT_FALSE( blockedContentPlan.outputVerificationReportContractReady );
    ASSERT_FALSE( blockedContentPlan.topLevelKeysPlanned );
    ASSERT_FALSE( blockedContentPlan.schemaDetailContractReady );
    ASSERT_FALSE( blockedContentPlan.jsonSerializationPlanned );
    ASSERT_FALSE( blockedContentPlan.reportWritePlanned );
    ASSERT_EQ( std::string("rendered output verification report content contract unavailable"),
               std::string(blockedContentPlan.reason.toUtf8().constData()) );
    BatchRenderedVideoOutputVerificationReportWriterPlan blockedWriterPlan =
        batchRenderedVideoOutputVerificationReportWriterPlanFromContent(
            BatchRenderedVideoOutputVerificationReportContentPlan());
    ASSERT_FALSE( blockedWriterPlan.contractReady );
    ASSERT_FALSE( blockedWriterPlan.outputVerificationReportContentContractReady );
    ASSERT_FALSE( blockedWriterPlan.writerPreflightPlanned );
    ASSERT_FALSE( blockedWriterPlan.jsonSerializationPlanned );
    ASSERT_FALSE( blockedWriterPlan.reportWritePlanned );
    ASSERT_EQ( std::string("rendered output verification report writer contract unavailable"),
               std::string(blockedWriterPlan.reason.toUtf8().constData()) );
}

/* Renamed at E4-1: the runner is no longer "blocked", so the old name
 * (PlansRenderedVideoJobPreflightButKeepsRunnerBlocked) asserted something false in its
 * title. What the test actually pins is that a CAPABLE runner does not make an
 * INCOMPLETE job runnable. */
TEST(BatchExportFormat, PlansRenderedVideoJobPreflightButMetadatalessJobHasNoFfmpegCommand)
{
    BatchExportFormatRequest request =
        batchExportFormatRequestFromString(QStringLiteral("h264"));
    BatchRenderedVideoJobPlan plan =
        batchRenderedVideoJobPlanFromRequest(
            QStringLiteral("C:/clips/M16-1327.MLV"),
            QStringLiteral("C:/renders"),
            request);

    ASSERT_TRUE( plan.requestValid );
    ASSERT_TRUE( plan.targetReady );
    ASSERT_TRUE( plan.encoderReady );
    ASSERT_TRUE( plan.ffmpegVideoReady );
    ASSERT_TRUE( plan.ffmpegFilterReady );
    ASSERT_TRUE( plan.optionalFilterContractReady );
    ASSERT_TRUE( plan.optionalFilterPlan.contractReady );
    ASSERT_FALSE( plan.optionalFilterPlan.optionalFiltersRequested );
    ASSERT_FALSE( plan.optionalFilterPlan.optionalFilterExecutionReady );
    ASSERT_TRUE( plan.sourceAudioContractReady );
    ASSERT_TRUE( plan.sourceAudioPlan.contractReady );
    ASSERT_TRUE( plan.sourceAudioExtractionContractReady );
    ASSERT_TRUE( plan.sourceAudioExtractionPlan.contractReady );
    ASSERT_FALSE( plan.sourceAudioExtractionPlan.sourceAudioKnown );
    ASSERT_FALSE( plan.sourceAudioExtractionPlan.sourceAudioPresent );
    ASSERT_FALSE( plan.sourceAudioExtractionPlan.extractionPathPlanned );
    ASSERT_FALSE( plan.sourceAudioExtractionPlan.extractionProcessOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionPlan.tempFileOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionPlan.cleanupOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionPlan.extractionReady );
    ASSERT_TRUE( plan.sourceAudioExtractionExecutionContractReady );
    ASSERT_TRUE( plan.sourceAudioExtractionExecutionPlan.contractReady );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.sourceAudioKnown );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.sourceAudioPresent );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.sampleReadPlanned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.wavWritePlanned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.tempFileLifecyclePlanned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.sampleReadOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.wavHeaderWriteOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.wavSampleWriteOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.tempFileOpenOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.tempFileFinalizeOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.cleanupOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.extractionProcessOwned );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.tempFileLifecycleReady );
    ASSERT_FALSE( plan.sourceAudioExtractionExecutionPlan.extractionExecutionReady );
    ASSERT_TRUE( plan.ffmpegAudioInputHandoffContractReady );
    ASSERT_TRUE( plan.ffmpegAudioInputHandoffPlan.contractReady );
    ASSERT_FALSE( plan.ffmpegAudioInputHandoffPlan.sourceAudioKnown );
    ASSERT_FALSE( plan.ffmpegAudioInputHandoffPlan.sourceAudioPresent );
    ASSERT_FALSE( plan.ffmpegAudioInputHandoffPlan.audioInputPlanned );
    ASSERT_FALSE( plan.ffmpegAudioInputHandoffPlan.inputArgumentsPlanned );
    ASSERT_FALSE( plan.ffmpegAudioInputHandoffPlan.audioInputOwnershipPlanned );
    ASSERT_FALSE( plan.ffmpegAudioInputHandoffPlan.audioInputArgumentHandoffPlanned );
    ASSERT_FALSE( plan.ffmpegAudioInputHandoffPlan.audioInputOwned );
    ASSERT_FALSE( plan.ffmpegAudioInputHandoffPlan.audioInputArgumentHandoffOwned );
    ASSERT_FALSE( plan.ffmpegAudioInputHandoffPlan.audioInputHandoffReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.source-audio.wav"),
               std::string(plan.ffmpegAudioInputHandoffPlan.plannedAudioPath
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(plan.ffmpegAudioInputHandoffPlan.activeAudioArguments
                   .toUtf8().constData()) );
    ASSERT_TRUE( plan.ffmpegAudioInputContractReady );
    ASSERT_TRUE( plan.ffmpegAudioInputPlan.contractReady );
    ASSERT_FALSE( plan.ffmpegAudioInputPlan.sourceAudioKnown );
    ASSERT_FALSE( plan.ffmpegAudioInputPlan.sourceAudioPresent );
    ASSERT_FALSE( plan.ffmpegAudioInputPlan.audioInputPlanned );
    ASSERT_FALSE( plan.ffmpegAudioInputPlan.audioInputOwned );
    ASSERT_FALSE( plan.ffmpegAudioInputPlan.audioInputReady );
    ASSERT_EQ( std::string("C:/renders/M16-1327.source-audio.wav"),
               std::string(plan.ffmpegAudioInputPlan.plannedAudioPath
                   .toUtf8().constData()) );
    ASSERT_EQ( std::string("-an"),
               std::string(plan.ffmpegAudioInputPlan.activeAudioArguments
                   .toUtf8().constData()) );
    ASSERT_TRUE( plan.audioMuxPrerequisitesContractReady );
    ASSERT_TRUE( plan.audioMuxPrerequisitesPlan.contractReady );
    ASSERT_TRUE( plan.audioMuxPrerequisitesPlan.sourceAudioContractReady );
    ASSERT_TRUE( plan.audioMuxPrerequisitesPlan.sourceAudioExtractionContractReady );
    ASSERT_TRUE( plan.audioMuxPrerequisitesPlan.sourceAudioInputContractReady );
    ASSERT_TRUE( plan.audioMuxPrerequisitesPlan.videoOnlyFallbackReady );
    ASSERT_FALSE( plan.audioMuxPrerequisitesPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( plan.audioMuxPrerequisitesPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( plan.audioMuxPrerequisitesPlan.audioInputOwned );
    ASSERT_FALSE( plan.audioMuxPrerequisitesPlan.audioMuxOwned );
    ASSERT_FALSE( plan.audioMuxPrerequisitesPlan.audioSyncValidationOwned );
    ASSERT_FALSE( plan.audioMuxPrerequisitesPlan.muxReady );
    ASSERT_TRUE( plan.audioMuxExecutionContractReady );
    ASSERT_TRUE( plan.audioMuxExecutionPlan.contractReady );
    ASSERT_TRUE( plan.audioMuxExecutionPlan.sourceAudioContractReady );
    ASSERT_TRUE( plan.audioMuxExecutionPlan.audioInputHandoffContractReady );
    ASSERT_TRUE( plan.audioMuxExecutionPlan.audioMuxPrerequisitesContractReady );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.sourceAudioKnown );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.sourceAudioPresent );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.sourceAudioDiscoveryOwned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.sourceAudioExtractionOwned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioInputHandoffReady );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.tempAudioInputOwned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioMuxPlanned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioMuxArgumentHandoffPlanned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioSyncValidationPlanned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioMuxOwned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioMuxArgumentHandoffOwned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioSyncValidationOwned );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioMuxReady );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.audioSyncValidationReady );
    ASSERT_FALSE( plan.audioMuxExecutionPlan.muxExecutionReady );
    ASSERT_TRUE( plan.ffmpegAudioContractReady );
    ASSERT_TRUE( plan.ffmpegAudioPlan.audioInputContractReady );
    ASSERT_FALSE( plan.receiptApplicationContractReady );
    ASSERT_FALSE( plan.receiptApplicationPlan.contractReady );
    ASSERT_FALSE( plan.frameProcessingContractReady );
    ASSERT_FALSE( plan.ffmpegCommandReady );
    ASSERT_FALSE( plan.ffmpegExecutionContractReady );
    ASSERT_FALSE( plan.outputVerificationExecutionContractReady );
    ASSERT_FALSE( plan.outputVerificationDecisionContractReady );
    ASSERT_FALSE( plan.outputVerificationResultContractReady );
    ASSERT_FALSE( plan.outputVerificationReportContractReady );
    ASSERT_TRUE( plan.outputReady );
    ASSERT_TRUE( plan.preflightReady );

    /* E4-1 CHANGED WHAT THIS TEST IS ABOUT, and the distinction is the point.
     *
     * The runner PREREQUISITES are a property of the BUILD -- they say the headless
     * rendered-export runner exists -- and they are now satisfied. Whether a PARTICULAR
     * job can run is a separate question, and this plan still cannot: it was built from a
     * request alone, with no source metadata overlaid, so it has no frame geometry, no
     * ffmpeg command and no receipt application contract. Before E4-1 both answers were
     * "no" for the same reason and the test could not tell them apart. */
    ASSERT_TRUE( plan.runnerPrerequisites.processingParityReady );
    ASSERT_TRUE( plan.runnerPrerequisites.frameProcessingReady );
    ASSERT_TRUE( plan.runnerPrerequisites.ffmpegExecutionReady );
    ASSERT_TRUE( plan.runnerPrerequisites.outputVerificationReady );
    ASSERT_TRUE( plan.runnerPrerequisites.headlessRunnerReady );
    ASSERT_TRUE( plan.runnerPrerequisites.videoOnlyRunnerReady );
    ASSERT_TRUE( plan.runnerPrerequisites.ready );
    /* Audio muxing is deferred to E4-2 and stays legible as a limitation, not a blocker. */
    ASSERT_FALSE( plan.runnerPrerequisites.audioMuxReady );
    ASSERT_FALSE( plan.runnerPrerequisites.limitation.isEmpty() );

    /* WHAT `runnable` ACTUALLY MEANS, pinned here because E4-1 is the first change that
     * makes it observable: `runnable = preflightReady && runnerPrerequisites.ready`
     * (BatchTypes.h). It says "preflight passed AND this build HAS a rendered-video
     * runner". It does NOT say this particular plan carries a usable ffmpeg command --
     * this plan has none, because no source metadata was ever overlaid.
     *
     * That is not a hole. BatchRunner re-derives the invocation after the runnable gate
     * and refuses with exit 2 when it comes back empty
     * (batchRenderedVideoFfmpegArgumentsWithOutputPath returns "" for an unready command
     * plan), so a metadata-less plan cannot reach the encoder. The field is coarser than
     * its name suggests; renaming it would ripple through the whole plan layer and
     * belongs to E4-2, not to the walking skeleton. */
    ASSERT_TRUE( plan.runnable );
    ASSERT_FALSE( plan.ffmpegCommandReady );
    ASSERT_TRUE( batchRenderedVideoFfmpegArgumentsWithOutputPath(
                     plan.ffmpegCommandPlan,
                     QStringLiteral("C:/renders/M16-1327.mlvapp-partial.mp4") ).isEmpty() );
    ASSERT_EQ( std::string("C:/renders/M16-1327.mp4"),
               std::string(plan.outputPlan.outputPath.toUtf8().constData()) );
    ASSERT_EQ( std::string("runner-processing-parity-ready=true runner-frame-processing-ready=true runner-audio-mux-ready=false runner-ffmpeg-execution-ready=true runner-output-verification-ready=true runner-headless-export-ready=true runner-video-only-ready=true runner-ready=true runner-limitation=rendered export is video-only; source audio is discovered but not muxed (E4-2) runner-reason=none"),
               std::string(batchRenderedVideoRunnerPrerequisitesSummary(
                   plan.runnerPrerequisites).toUtf8().constData()) );

    const std::string summary =
        std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData());
    ASSERT_TRUE( summary.find("request=rendered-video codec=h264 container=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-video-args=-c:v libx264 -preset medium -crf 14 -pix_fmt yuv420p") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-filter-args=-vf scale=in_color_matrix=bt601:out_color_matrix=bt709") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-filter-optional-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-source=optional-filter-ownership-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-graph-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("optional-filter-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-source=video-only-undiscovered") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-sample-rate=0") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-channels=0") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-bits=0") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-bytes=0") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-discovery-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-known=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-video-only-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-source=source-audio-extraction-prerequisite-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-path-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-process-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-temp-file-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-cleanup-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-source=source-audio-extraction-execution-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-sample-read-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-wav-write-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-temp-lifecycle-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-sample-read-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-wav-header-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-wav-sample-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-temp-open-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-temp-finalize-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-cleanup-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-process-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-temp-lifecycle-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("source-audio-extraction-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-source=ffmpeg-audio-input-handoff-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-planned-args=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-active-args=-an") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-known=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-present=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-args-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-ownership-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-args-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-args-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-handoff-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-source=ffmpeg-audio-input-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-path=C:/renders/M16-1327.source-audio.wav") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-planned-args=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-active-args=-an") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-known=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-present=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-source=audio-mux-prerequisite-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-source-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-extraction-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-audio-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-video-only-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-source-discovery-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-extraction-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-input-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-mux-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-sync-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-source=audio-mux-execution-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-source-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-handoff-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-prereq-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-known=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-present=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-discovery-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-extraction-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-handoff-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-temp-input-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-mux-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-args-handoff-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-sync-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-mux-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-args-handoff-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-sync-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-mux-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-sync-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-source=video-only-to-mux-transition-contract") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-transition-args=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-source-discovery-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-extraction-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-input-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-exec-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-audio-mux-command-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("receipt-application-reason=rendered source metadata unavailable") != std::string::npos );
    ASSERT_TRUE( summary.find("render-processing-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-command-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-command-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("ffmpeg-execution-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("rendered-output=C:/renders/M16-1327.mp4 rendered-output-explicit-file=false rendered-output-input-clips=1 rendered-output-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-binary-source=default-executable-name") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-binary-command-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-probe-command-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-output-contract-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ffmpeg-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe=ffprobe") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-binary-source=default-executable-name") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-binary-command-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-ready=true") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-owned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-probe-command-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-exec-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-decision-exec-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-decision-receipt-hash-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-decision-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-decision-accepted=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-decision-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-result-decision-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-result-report-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-result-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-result-accepted=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-result-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-result-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-artifact-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-path-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-file-create-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-write-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-content-report-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-content-top-level-keys=none") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-content-schema-detail-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-content-json-serialization-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-content-write-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-content-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-content-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-writer-content-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-writer-temp-path=unspecified") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-writer-preflight-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-writer-json-serialization-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-writer-write-planned=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-writer-output-verification-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("output-verification-report-writer-contract-ready=false") != std::string::npos );
    ASSERT_TRUE( summary.find("preflight-ready=true runnable=true") != std::string::npos );

    BatchRenderedVideoRenderSettings invalidSettings =
        batchRenderedVideoRenderSettingsFromExplicitResize(
            true,
            0,
            1080,
            false);
    ASSERT_FALSE( invalidSettings.ready );
    plan = batchRenderedVideoJobPlanFromRequest(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders"),
        request,
        invalidSettings);

    ASSERT_TRUE( plan.requestValid );
    ASSERT_TRUE( plan.targetReady );
    ASSERT_TRUE( plan.encoderReady );
    ASSERT_TRUE( plan.ffmpegVideoReady );
    ASSERT_TRUE( plan.ffmpegFilterReady );
    ASSERT_TRUE( plan.optionalFilterContractReady );
    ASSERT_FALSE( plan.frameProcessingContractReady );
    ASSERT_FALSE( plan.ffmpegCommandReady );
    ASSERT_FALSE( plan.ffmpegExecutionContractReady );
    ASSERT_FALSE( plan.outputVerificationExecutionContractReady );
    ASSERT_FALSE( plan.outputVerificationDecisionContractReady );
    ASSERT_FALSE( plan.outputVerificationResultContractReady );
    ASSERT_FALSE( plan.outputVerificationReportContractReady );
    ASSERT_FALSE( plan.renderSettings.ready );
    ASSERT_TRUE( plan.outputReady );
    ASSERT_FALSE( plan.preflightReady );
    ASSERT_FALSE( plan.runnable );
    ASSERT_EQ( std::string("rendered resize dimensions invalid"),
               std::string(batchRenderedVideoJobPlanFirstBlocker(plan).toUtf8().constData()) );
    ASSERT_TRUE( std::string(batchRenderedVideoJobPlanSummary(plan).toUtf8().constData())
        .find("render-settings-ready=false render-settings-reason=rendered resize dimensions invalid") != std::string::npos );

    plan = batchRenderedVideoJobPlanFromRequest(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders/custom.mp4"),
        request,
        2);

    ASSERT_TRUE( plan.requestValid );
    ASSERT_TRUE( plan.targetReady );
    ASSERT_TRUE( plan.encoderReady );
    ASSERT_TRUE( plan.ffmpegVideoReady );
    ASSERT_TRUE( plan.ffmpegFilterReady );
    ASSERT_TRUE( plan.optionalFilterContractReady );
    ASSERT_FALSE( plan.frameProcessingContractReady );
    ASSERT_FALSE( plan.ffmpegCommandReady );
    ASSERT_FALSE( plan.ffmpegExecutionContractReady );
    ASSERT_FALSE( plan.outputVerificationExecutionContractReady );
    ASSERT_FALSE( plan.outputReady );
    ASSERT_FALSE( plan.preflightReady );
    ASSERT_FALSE( plan.runnable );
    ASSERT_TRUE( plan.outputPlan.explicitFileOutput );
    ASSERT_EQ( 2, plan.outputPlan.inputClipCount );
    ASSERT_EQ( std::string("explicit rendered output file requires a single input clip"),
               std::string(batchRenderedVideoJobPlanFirstBlocker(plan).toUtf8().constData()) );

    request = batchExportFormatRequestFromString(QStringLiteral("rendered-video"));
    plan = batchRenderedVideoJobPlanFromRequest(
        QStringLiteral("C:/clips/M16-1327.MLV"),
        QStringLiteral("C:/renders"),
        request);

    ASSERT_TRUE( plan.requestValid );
    ASSERT_FALSE( plan.targetReady );
    ASSERT_FALSE( plan.preflightReady );
    ASSERT_FALSE( plan.runnable );
    ASSERT_EQ( std::string("rendered target incomplete"),
               std::string(batchRenderedVideoJobPlanFirstBlocker(plan).toUtf8().constData()) );
}

TEST(BatchExportFormat, RejectsUnknownFormats)
{
    ASSERT_EQ( static_cast<int>(BatchExportFormat::Unknown),
               static_cast<int>(batchExportFormatFromString(QStringLiteral("gif"))) );
    ASSERT_EQ( std::string("unknown"),
               std::string(batchExportFormatName(BatchExportFormat::Unknown)) );
}

TEST(BatchRunner, PerClipReceiptCopyDoesNotMutateSharedBaseReceipt)
{
    ReceiptSettings base;
    base.setExposure(0);
    base.setTemperature(6000);
    base.setRawBlack(-1);
    base.setRawWhite(-1);
    base.setLookAssistBaselineValid(false);

    ReceiptSettings clipReceipt = base;
    clipReceipt.setExposure(178);
    clipReceipt.setTemperature(5840);
    clipReceipt.setRawBlack(20470);
    clipReceipt.setRawWhite(6000);
    clipReceipt.setLookAssistBaselineValid(true);

    ASSERT_EQ(0, base.exposure());
    ASSERT_EQ(6000, base.temperature());
    ASSERT_EQ(-1, base.rawBlack());
    ASSERT_EQ(-1, base.rawWhite());
    ASSERT_FALSE(base.lookAssistBaselineValid());
}
