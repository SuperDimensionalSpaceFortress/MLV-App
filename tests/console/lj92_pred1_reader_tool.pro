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

# lj92.c reaches omp_get_wtime through StageTiming.h's fallbacks on every platform (r1 adversary A1), so the Linux and
# macOS scopes link OpenMP the way tests/pipeline/pipeline_tests.pro does.
linux-g++* {
    QMAKE_CFLAGS += -std=c99 -fopenmp
    LIBS += -lgomp
}

macx {
    QMAKE_CFLAGS += -std=c99
    equals(QT_ARCH, x86_64) {
        QMAKE_CFLAGS += -fopenmp
        INCLUDEPATH += /usr/local/opt/llvm/include
        LIBS += -L/usr/local/opt/llvm/lib -lomp
    }
    equals(QT_ARCH, arm64) {
        QMAKE_CFLAGS += -fopenmp
        INCLUDEPATH += /opt/homebrew/opt/llvm/include
        LIBS += -L/opt/homebrew/opt/llvm/lib -lomp
    }
}
