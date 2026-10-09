REPO_ROOT = $$clean_path($$PWD/../..)

CONFIG += c++17 console warn_on
CONFIG -= app_bundle

INCLUDEPATH += $$REPO_ROOT \
               $$REPO_ROOT/src \
               $$REPO_ROOT/src/librtprocess/src/include \
               $$REPO_ROOT/platform/qt \
               $$REPO_ROOT/tests/common

DEPENDPATH += $$INCLUDEPATH

# SUITE-RUNNER-REPO-ROOT-PIN-1: record the checkout this binary is built from, so tests/common/repo_paths.cpp can refuse a
# different checkout found above the binary (a build dir placed under another checkout's run dir).
mlvapp_test_source_root.input = $$PWD/mlvapp_test_source_root.h.in
mlvapp_test_source_root.output = $$OUT_PWD/mlvapp_test_source_root.h
QMAKE_SUBSTITUTES += mlvapp_test_source_root
INCLUDEPATH += $$OUT_PWD
