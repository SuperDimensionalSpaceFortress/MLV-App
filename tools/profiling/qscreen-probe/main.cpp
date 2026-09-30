// qscreen-probe (UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1): a console QGuiApplication that prints
// every QScreen field the display-identity chain reads. The gui_smoke.display_screen line is
// byte-for-byte the format MainWindow::logPlaybackSmokeDisplayInventory() logs, so the recorded
// output feeds the ONE shared parser (tools/profiling/gui-smoke-display-identity.ps1) unchanged.
// It reads no clip, opens no window and changes no display state.
#include <QGuiApplication>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRect>
#include <QScreen>
#include <QSizeF>
#include <QString>
#include <QStringList>
#include <QTextStream>

#include "DisplayDeviceMapping.h"

static QString bool01( bool value )
{
    return value ? QStringLiteral("1") : QStringLiteral("0");
}

int main( int argc, char *argv[] )
{
    QGuiApplication app( argc, argv );

    // --display-prefer <value> (repeatable): evaluate the app's own preference rule
    // (DisplayDeviceMapping::preferenceMatchedField, fed the way MainWindow.cpp feeds it) for every
    // screen, so a probe run states which screen each preference would select.
    QStringList preferValues;
    for( int a = 1; a + 1 < argc; ++a )
    {
        if( QString::fromLocal8Bit( argv[a] ) == QLatin1String( "--display-prefer" ) )
            preferValues << QString::fromLocal8Bit( argv[++a] );
    }
    QTextStream out( stdout );
    out.setEncoding( QStringConverter::Utf8 );

    out << "qscreen_probe.qt_version " << qVersion() << "\n";
    out << "qscreen_probe.platform " << QGuiApplication::platformName() << "\n";

    const QList<QScreen *> screens = QGuiApplication::screens();
    QScreen * const primary = QGuiApplication::primaryScreen();
    out << "qscreen_probe.screen_count " << screens.size() << "\n";

    // The exact monitor list the app derives each QScreen's GDI device from.
    const QList<DisplayDeviceMapping::MonitorRect> monitors = DisplayDeviceMapping::enumerateGdiMonitors();
    for( const DisplayDeviceMapping::MonitorRect &m : monitors )
    {
        out << QStringLiteral( "qscreen_probe.gdi_monitor device=\"%1\" rect=%2,%3 %4x%5" )
                   .arg( m.device ).arg( m.rect.x() ).arg( m.rect.y() ).arg( m.rect.width() ).arg( m.rect.height() )
            << "\n";
    }

    QJsonArray json;
    for( int i = 0; i < screens.size(); ++i )
    {
        QScreen *s = screens.at( i );
        if( !s ) continue;
        const QRect geo = s->geometry();
        const double dpr = s->devicePixelRatio();
        const int physicalWidth = qRound( geo.width() * dpr );
        const int physicalHeight = qRound( geo.height() * dpr );
        out << QStringLiteral(
                   "gui_smoke.display_screen index=%1 name=\"%2\" manufacturer=\"%3\" "
                   "model=\"%4\" serial=\"%5\" geometry=%6,%7 %8x%9 physical=%10x%11 "
                   "dpr=%12 refresh_hz=%13 primary=%14 device=\"%15\"" )
                   .arg( i )
                   .arg( s->name() )
                   .arg( s->manufacturer() )
                   .arg( s->model() )
                   .arg( s->serialNumber() )
                   .arg( geo.x() )
                   .arg( geo.y() )
                   .arg( geo.width() )
                   .arg( geo.height() )
                   .arg( physicalWidth )
                   .arg( physicalHeight )
                   .arg( dpr, 0, 'f', 2 )
                   .arg( s->refreshRate(), 0, 'f', 3 )
                   .arg( bool01( s == primary ) )
                   .arg( DisplayDeviceMapping::gdiDeviceForScreen(
                       geo.topLeft(), QSize( physicalWidth, physicalHeight ), monitors ) )
            << "\n";

        const QSizeF mm = s->physicalSize();
        out << QStringLiteral( "qscreen_probe.detail index=%1 physical_size_mm=%2x%3 logical_dpi=%4 "
                               "physical_dpi=%5 available=%6,%7 %8x%9 siblings=%10" )
                   .arg( i )
                   .arg( mm.width(), 0, 'f', 1 )
                   .arg( mm.height(), 0, 'f', 1 )
                   .arg( s->logicalDotsPerInch(), 0, 'f', 2 )
                   .arg( s->physicalDotsPerInch(), 0, 'f', 2 )
                   .arg( s->availableGeometry().x() )
                   .arg( s->availableGeometry().y() )
                   .arg( s->availableGeometry().width() )
                   .arg( s->availableGeometry().height() )
                   .arg( s->virtualSiblings().size() )
            << "\n";

        const QString derivedDevice = DisplayDeviceMapping::gdiDeviceForScreen(
            geo.topLeft(), QSize( physicalWidth, physicalHeight ), monitors );
        for( const QString &prefer : preferValues )
        {
            out << QStringLiteral( "qscreen_probe.prefer_match index=%1 name=\"%2\" prefer=\"%3\" matched=\"%4\"" )
                       .arg( i )
                       .arg( s->name() )
                       .arg( prefer )
                       .arg( DisplayDeviceMapping::preferenceMatchedField(
                           s->name(), s->model(), s->manufacturer(),
                           prefer.startsWith( QStringLiteral( "\\\\.\\" ) ) ? derivedDevice : QString(), prefer ) )
                << "\n";
        }

        QJsonObject o;
        o[QStringLiteral("index")] = i;
        o[QStringLiteral("name")] = s->name();
        o[QStringLiteral("manufacturer")] = s->manufacturer();
        o[QStringLiteral("model")] = s->model();
        o[QStringLiteral("serialNumber")] = s->serialNumber();
        o[QStringLiteral("geometryX")] = geo.x();
        o[QStringLiteral("geometryY")] = geo.y();
        o[QStringLiteral("geometryWidth")] = geo.width();
        o[QStringLiteral("geometryHeight")] = geo.height();
        o[QStringLiteral("devicePixelRatio")] = dpr;
        o[QStringLiteral("physicalWidth")] = physicalWidth;
        o[QStringLiteral("physicalHeight")] = physicalHeight;
        o[QStringLiteral("refreshRate")] = s->refreshRate();
        o[QStringLiteral("primary")] = ( s == primary );
        json.append( o );
    }
    out << "qscreen_probe.json " << QJsonDocument( json ).toJson( QJsonDocument::Compact ) << "\n";
    out.flush();
    return 0;
}
