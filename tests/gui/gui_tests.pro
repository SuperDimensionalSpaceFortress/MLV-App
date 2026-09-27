include(../common/test_defaults.pri)

QT += core gui widgets testlib opengl openglwidgets

TEMPLATE = app
CONFIG += testcase
TARGET = gui_tests

# GpuDisplayWindow.cpp's swap telemetry calls mlv_stage_timing_now(), whose non-QPC fallback is omp_get_wtime();
# the app links libgomp in MLVApp.pro, gui_tests must too (hosted Windows GUI Pilot link failure on #159/#161).
win32: LIBS += -llibgomp-1

SOURCES += \
    $$REPO_ROOT/platform/qt/ColorToolButton.cpp \
    $$REPO_ROOT/platform/qt/GpuDebayer.cpp \
    $$REPO_ROOT/platform/qt/GpuPreviewProcessing.cpp \
    $$REPO_ROOT/platform/qt/GpuDisplayWindow.cpp \
    $$REPO_ROOT/platform/qt/GpuDisplayViewport.cpp \
    $$REPO_ROOT/platform/qt/Histogram.cpp \
    $$REPO_ROOT/platform/qt/ScopesLabel.cpp \
    $$REPO_ROOT/platform/qt/VectorScope.cpp \
    $$REPO_ROOT/platform/qt/WaveFormMonitor.cpp \
    $$REPO_ROOT/tests/common/hash_helpers.cpp \
    $$REPO_ROOT/tests/common/image_regression.cpp \
    $$REPO_ROOT/tests/common/repo_paths.cpp \
    $$REPO_ROOT/tests/gui/raw_processing_gpu_preview_stubs.cpp \
    $$REPO_ROOT/tests/gui/test_gui_smoke.cpp

HEADERS += \
    $$REPO_ROOT/platform/qt/ColorToolButton.h \
    $$REPO_ROOT/platform/qt/GpuDebayer.h \
    $$REPO_ROOT/platform/qt/GpuPreviewProcessing.h \
    $$REPO_ROOT/platform/qt/DualIsoPatternMapping.h \
    $$REPO_ROOT/platform/qt/GpuDisplayWindow.h \
    $$REPO_ROOT/platform/qt/GpuDisplayViewport.h \
    $$REPO_ROOT/platform/qt/Histogram.h \
    $$REPO_ROOT/platform/qt/MainWindowGpuPreviewPolicy.h \
    $$REPO_ROOT/platform/qt/ScopesLabel.h \
    $$REPO_ROOT/platform/qt/VectorScope.h \
    $$REPO_ROOT/platform/qt/WaveFormMonitor.h \
    $$REPO_ROOT/platform/qt/ZebraThresholds.h \
    $$REPO_ROOT/src/batch/RawAspectStretchPolicy.h \
    $$REPO_ROOT/tests/common/hash_helpers.h \
    $$REPO_ROOT/tests/common/image_regression.h \
    $$REPO_ROOT/tests/common/repo_paths.h \
    $$REPO_ROOT/tests/common/test_runtime.h
