include(../common/test_defaults.pri)

QT += core gui opengl

TEMPLATE = app
TARGET = pipeline_tests

DEFINES += STDOUT_SILENT

# Compile video_mlv.c's real JPEG2000 frame-read branch into the test binary so
# the upstream-sync hardening regressions run the production lines rather than
# the "codec not enabled" stub. OpenJPH itself is not linked: the codec entry
# points come from tests/pipeline/ojph_decoder_test_stub.cpp, which is the only
# thing replaced. See that file for why.
DEFINES += ENABLE_JPEG2K

SOURCES += \
    $$REPO_ROOT/tests/common/test_artifacts.cpp \
    $$REPO_ROOT/tests/common/frame_compare.cpp \
    $$REPO_ROOT/tests/common/hash_helpers.cpp \
    $$REPO_ROOT/tests/common/repo_paths.cpp \
    $$REPO_ROOT/platform/qt/CrashForensics.cpp \
    $$REPO_ROOT/platform/qt/Phase3Breadcrumbs.cpp \
    $$REPO_ROOT/platform/qt/Phase3Checksums.cpp \
    $$REPO_ROOT/platform/qt/Phase3Mode.cpp \
    $$REPO_ROOT/platform/qt/Phase3StageTelemetry.cpp \
    $$REPO_ROOT/src/debug/ForceSingleThread.cpp \
    $$REPO_ROOT/src/debug/FrameChecksum.cpp \
    $$REPO_ROOT/src/debug/StageTimingCsvSink.cpp \
    $$REPO_ROOT/platform/qt/GpuDebayer.cpp \
    $$REPO_ROOT/platform/qt/GpuPreviewProcessing.cpp \
    $$REPO_ROOT/platform/qt/ReceiptSettings.cpp \
    $$REPO_ROOT/src/debayer/amaze_demosaic.c \
    $$REPO_ROOT/src/debayer/debayer.c \
    $$REPO_ROOT/src/debayer/conv.c \
    $$REPO_ROOT/src/debayer/basic.c \
    $$REPO_ROOT/src/debayer/ahdOld.c \
    $$REPO_ROOT/src/debayer/wb_conversion.c \
    $$REPO_ROOT/src/ca_correct/CA_correct_RT.c \
    $$REPO_ROOT/src/matrix/matrix.c \
    $$REPO_ROOT/src/mlv/frame_caching.c \
    $$REPO_ROOT/src/mlv/pipeline_stage_capture.c \
    $$REPO_ROOT/src/mlv/video_mlv.c \
    $$REPO_ROOT/src/mlv/video_mlv_misc.c \
    $$REPO_ROOT/src/mlv/audio_mlv.c \
    $$REPO_ROOT/src/mlv/liblj92/lj92.c \
    $$REPO_ROOT/src/mlv/camid/camera_id.c \
    $$REPO_ROOT/src/mlv/mcraw/mcraw.c \
    $$REPO_ROOT/src/mlv/mcraw/cJSON.c \
    $$REPO_ROOT/src/mlv/mcraw/RawData.cpp \
    $$REPO_ROOT/src/mlv/mcraw/RawData_Legacy.cpp \
    $$REPO_ROOT/src/mlv/llrawproc/llrawproc.c \
    $$REPO_ROOT/src/mlv/llrawproc/pixelproc.c \
    $$REPO_ROOT/src/mlv/llrawproc/stripes.c \
    $$REPO_ROOT/src/mlv/llrawproc/patternnoise.c \
    $$REPO_ROOT/src/mlv/llrawproc/chroma_smooth.c \
    $$REPO_ROOT/src/mlv/llrawproc/hist.c \
    $$REPO_ROOT/src/mlv/llrawproc/darkframe.c \
    $$REPO_ROOT/src/mlv/llrawproc/dualiso.c \
    $$REPO_ROOT/src/processing/raw_processing.c \
    $$REPO_ROOT/src/processing/playback_downsample.c \
    $$REPO_ROOT/src/processing/blur_threaded.c \
    $$REPO_ROOT/src/processing/filter/filter.c \
    $$REPO_ROOT/src/processing/filter/genann/genann.c \
    $$REPO_ROOT/src/processing/cube_lut.c \
    $$REPO_ROOT/src/processing/denoiser/denoiser_2d_median.c \
    $$REPO_ROOT/src/processing/interpolation/spline_helper.cpp \
    $$REPO_ROOT/src/processing/interpolation/cosine_interpolation.c \
    $$REPO_ROOT/src/processing/rbfilter/rbf_wrapper.cpp \
    $$REPO_ROOT/src/processing/rbfilter/RBFilterPlain.cpp \
    $$REPO_ROOT/src/processing/sobel/sobel.c \
    $$REPO_ROOT/src/processing/cafilter/ColorAberrationCorrection.c \
    $$REPO_ROOT/src/processing/tinyexpr/tinyexpr.c \
    $$REPO_ROOT/src/dng/dng.c \
    $$REPO_ROOT/src/dng/dng_reader.c \
    $$REPO_ROOT/platform/mlv_blender/MLVBlender.c \
    $$REPO_ROOT/src/batch/BatchLogger.cpp \
    $$REPO_ROOT/src/batch/BatchContext.cpp \
    $$REPO_ROOT/src/batch/BatchPrompts.cpp \
    $$REPO_ROOT/src/batch/CdngSequenceExport.cpp \
    $$REPO_ROOT/src/batch/BatchRunner.cpp \
    $$REPO_ROOT/src/batch/BatchRenderedVideoPlan.cpp \
    $$REPO_ROOT/src/batch/ReceiptLoader.cpp \
    $$REPO_ROOT/src/batch/ReceiptApplier.cpp \
    $$REPO_ROOT/src/librtprocess/src/include/librtprocesswrapper.cpp \
    $$REPO_ROOT/src/librtprocess/src/demosaic/ahd.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/amaze.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/bayerfast.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/border.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/dcb.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/hphd.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/igv.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/lmmse.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/markesteijn.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/rcd.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/vng4.cc \
    $$REPO_ROOT/src/librtprocess/src/demosaic/xtransfast.cc \
    $$REPO_ROOT/src/librtprocess/src/postprocess/hilite_recon.cc \
    $$REPO_ROOT/src/librtprocess/src/preprocess/CA_correct.cc \
    $$REPO_ROOT/tests/pipeline/ojph_decoder_test_stub.cpp \
    $$REPO_ROOT/tests/pipeline/mlv_pipeline_fixture.cpp \
    $$REPO_ROOT/tests/pipeline/backend_parametric_fixture.cpp \
    $$REPO_ROOT/tests/pipeline/test_main.cpp \
    $$REPO_ROOT/tests/pipeline/test_dual_iso_pipeline.cpp \
    $$REPO_ROOT/tests/pipeline/test_gpu_preview_processing.cpp \
    $$REPO_ROOT/tests/pipeline/test_backend_parametric_shell.cpp \
    $$REPO_ROOT/tests/pipeline/test_backend_parametric_debayer_shell.cpp \
    $$REPO_ROOT/tests/pipeline/test_processing_filters.cpp \
    $$REPO_ROOT/tests/pipeline/test_playback_scaling.cpp \
    $$REPO_ROOT/tests/pipeline/test_crash_forensics.cpp \
    $$REPO_ROOT/tests/pipeline/test_phase3_infrastructure.cpp \
    $$REPO_ROOT/tests/pipeline/test_upstream_sync_hardening.cpp \
    $$REPO_ROOT/tests/pipeline/test_phase3_parity.cpp \
    $$REPO_ROOT/tests/pipeline/test_cdng_sequence_export.cpp

HEADERS += \
    $$REPO_ROOT/tests/common/minitest.h \
    $$REPO_ROOT/tests/common/test_artifacts.h \
    $$REPO_ROOT/tests/common/test_runtime.h \
    $$REPO_ROOT/tests/common/frame_compare.h \
    $$REPO_ROOT/tests/common/hash_helpers.h \
    $$REPO_ROOT/tests/common/repo_paths.h \
    $$REPO_ROOT/platform/qt/CrashForensics.h \
    $$REPO_ROOT/platform/qt/Phase3Breadcrumbs.h \
    $$REPO_ROOT/platform/qt/Phase3Checksums.h \
    $$REPO_ROOT/platform/qt/Phase3Mode.h \
    $$REPO_ROOT/platform/qt/Phase3StageTelemetry.h \
    $$REPO_ROOT/src/debug/ForceSingleThread.h \
    $$REPO_ROOT/src/debug/FrameChecksum.h \
    $$REPO_ROOT/src/debug/StageTimingCsvSink.h \
    $$REPO_ROOT/platform/qt/GpuDebayer.h \
    $$REPO_ROOT/platform/qt/GpuPreviewProcessing.h \
    $$REPO_ROOT/tests/pipeline/ojph_decoder_test_stub.h \
    $$REPO_ROOT/tests/pipeline/mlv_pipeline_fixture.h \
    $$REPO_ROOT/tests/pipeline/backend_parametric_fixture.h \
    $$REPO_ROOT/tests/pipeline/playback_path_test_state.h \
    $$REPO_ROOT/src/batch/BatchRenderedVideoPlan.h \
    $$REPO_ROOT/src/batch/CdngSequenceExport.h

win32{
    QMAKE_CFLAGS_RELEASE -= -O2
    QMAKE_CFLAGS_RELEASE += -O3
    QMAKE_CXXFLAGS_RELEASE -= -O2
    QMAKE_CXXFLAGS_RELEASE += -O3
    QMAKE_CFLAGS += -O3 -fopenmp -mssse3 -msse3 -msse2 -msse -D_FILE_OFFSET_BITS=64 -std=c99 -ftree-vectorize
    LIBS += -llibgomp-1
    # dbghelp: CrashForensics links MiniDumpWriteDump via dbghelp.
    LIBS += -ldbghelp
    QMAKE_CXXFLAGS += -fopenmp -std=c++17 -ftree-vectorize
    WINDOWS_TEST_RUNTIME_DEPLOY = $$relative_path($$REPO_ROOT/tools/testing/deploy-windows-test-runtime.ps1, $$OUT_PWD)
    QMAKE_POST_LINK += powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $$WINDOWS_TEST_RUNTIME_DEPLOY -TargetDir release -QtBinDir $$[QT_INSTALL_BINS] $$escape_expand(\n\t)
}

linux-g++*{
    gcc {
        QMAKE_CFLAGS += -std=gnu99
    } else {
        QMAKE_CFLAGS += -std=c99
    }
    QMAKE_CFLAGS += -O3 -fopenmp -ftree-vectorize
    QMAKE_CXXFLAGS += -fopenmp -std=c++17 -ftree-vectorize
    LIBS += -lgomp
    equals(QT_ARCH, x86_64) {
        QMAKE_CFLAGS += -msse4.1 -mssse3 -msse3 -msse2 -msse
    }
}

macx{
    equals(QT_ARCH, x86_64) {
        QMAKE_CFLAGS += -fopenmp -ftree-vectorize
        QMAKE_CXXFLAGS += -fopenmp -std=c++17 -ftree-vectorize
        INCLUDEPATH += -I/usr/local/opt/llvm/include
        LIBS += -L/usr/local/opt/llvm/lib -lomp -L/usr/local/opt/openssl/lib -lssl
    }
    equals(QT_ARCH, arm64) {
        QMAKE_CFLAGS += -fopenmp -ftree-vectorize
        QMAKE_CXXFLAGS += -fopenmp -std=c++17 -ftree-vectorize
        INCLUDEPATH += -I/opt/homebrew/opt/llvm/include
        LIBS += -L/opt/homebrew/opt/llvm/lib -lomp -L/opt/homebrew/opt/llvm/lib/unwind -lunwind -L/opt/homebrew/opt/openssl/lib -lssl -L/opt/homebrew/opt/llvm/lib/c++ -lc++ -lc++abi
    }
}

include($$REPO_ROOT/platform/qt/avx_optin.pri)
