include(../common/test_defaults.pri)

# PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r2: the hostile-environment run-log writer (log_line_field_fixture.cpp).
QT += core
QT -= gui

TEMPLATE = app
TARGET = log_line_field_fixture

SOURCES += $$REPO_ROOT/tests/console/log_line_field_fixture.cpp
HEADERS += $$REPO_ROOT/tests/console/log_line_field_fixture.h \
           $$REPO_ROOT/platform/qt/LogLineField.h
