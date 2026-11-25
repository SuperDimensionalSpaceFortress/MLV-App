include(fuzz_target_defaults.pri)

TARGET = fuzz_lj92

SOURCES += \
    $$REPO_ROOT/src/mlv/liblj92/lj92.c \
    $$REPO_ROOT/tests/fuzz/fuzz_lj92.cpp

# lj92.c reaches omp_get_wtime through src/debug/StageTiming.h on every platform, so each scope links OpenMP the way
# tests/pipeline/pipeline_tests.pro does (LJ92-PRED1-DECODER-ONLY-1 r1 adversary A1 sibling; without it the MinGW link
# failed with an undefined omp_get_wtime reference).
win32 {
    QMAKE_CFLAGS += -std=c99 -fopenmp
    QMAKE_LFLAGS += -fopenmp
}

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
