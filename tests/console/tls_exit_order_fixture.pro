include(../common/test_defaults.pri)

# CPU-SCOPE-LIVE-CRASH-FIX-1: thread_local destructor order at thread exit (tls_exit_order_fixture.cpp), with the app's own
# flags from platform/qt/MLVApp.pro. A console binary: it opens no window and creates no application object.
QT += core
QT -= gui

TEMPLATE = app
TARGET = tls_exit_order_fixture
CONFIG -= debug debug_and_release
CONFIG += release

QMAKE_CXXFLAGS_RELEASE -= -O2
QMAKE_CXXFLAGS_RELEASE += -O3
QMAKE_CXXFLAGS += -fopenmp -std=c++17
LIBS += -llibgomp-1 -lpsapi

SOURCES += $$REPO_ROOT/tests/console/tls_exit_order_fixture.cpp \
           $$REPO_ROOT/tests/console/tls_exit_order_inject.cpp \
           $$REPO_ROOT/src/processing/rbfilter/rbf_wrapper.cpp \
           $$REPO_ROOT/src/processing/rbfilter/RBFilterPlain.cpp
HEADERS += $$REPO_ROOT/src/processing/ThreadExitCache.h
