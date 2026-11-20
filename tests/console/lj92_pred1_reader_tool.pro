include(../common/test_defaults.pri)

# LJ92-PRED1-FAST-PATH-1: standalone console tool (no Qt) that A/Bs the predictor-1 fast path's two bit readers.
CONFIG -= qt

TEMPLATE = app
TARGET = lj92_pred1_reader_tool

SOURCES += \
    $$REPO_ROOT/src/mlv/liblj92/lj92.c \
    $$REPO_ROOT/tests/console/lj92_pred1_reader_tool.cpp

# The app compiles lj92.c with the C flags below (-O3 -fopenmp; StageTiming.h reads omp_get_wtime), so the tool does too.
win32 {
    QMAKE_CFLAGS += -std=c99 -fopenmp
    QMAKE_LFLAGS += -fopenmp
    QMAKE_CFLAGS_RELEASE -= -O2
    QMAKE_CFLAGS_RELEASE += -O3
}

linux-g++* {
    QMAKE_CFLAGS += -std=c99
}

macx {
    QMAKE_CFLAGS += -std=c99
}
