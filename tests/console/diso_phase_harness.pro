include(../common/test_defaults.pri)

QT += core gui widgets

TEMPLATE = app
TARGET = diso_phase_harness

DEFINES += STDOUT_SILENT

include(../common/pipeline_runtime.pri)

SOURCES += \
    $$REPO_ROOT/src/dng/dng_reader.c \
    $$REPO_ROOT/tests/console/diso_phase_harness.cpp
