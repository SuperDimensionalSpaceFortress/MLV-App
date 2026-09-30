# qscreen-probe: see main.cpp. Build with the repo toolchain:
#   PATH=C:\Qt\6.10.2\mingw_64\bin;C:\Qt\Tools\mingw1310_64\bin  qmake && mingw32-make release
QT       += core gui
CONFIG   += console release c++17
CONFIG   -= app_bundle
TARGET    = qscreen-probe
TEMPLATE  = app
SOURCES  += main.cpp
HEADERS  += ../../../platform/qt/DisplayDeviceMapping.h
INCLUDEPATH += ../../../platform/qt
