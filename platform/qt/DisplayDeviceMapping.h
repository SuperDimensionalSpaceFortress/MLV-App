#ifndef DISPLAYDEVICEMAPPING_H
#define DISPLAYDEVICEMAPPING_H

// UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1: mapping a QScreen to the Windows GDI device (\\.\DISPLAYn).
//
// MEASURED (Ultra-Magnus, Qt 6.10.2 windows QPA, session 1, 2026-09-30; fixture
// tests/fixtures/display/um-qscreen-windows-mapping-20260930.txt): QScreen::name() is NOT the GDI
// device name. A monitor that carries an EDID name reports THAT ("PA329C", "LG TV") as name() and
// model(); only a monitor without one (a VM's virtual display) falls back to "\\.\DISPLAYn".
// PR #191 assumed the fallback was the rule, so a job-resolved "\\.\DISPLAY1" preference was compared
// with the string "PA329C", never matched, and the equal-4K refresh tie fell to the primary LG TV.
//
// So the GDI device is DERIVED here from what both worlds do agree on: Qt's QScreen::geometry()
// top-left is the monitor's NATIVE desktop origin (only its size is divided by the DPR), which is
// the rcMonitor / EnumDisplaySettings position Windows reports for the GDI device, and the
// physical size is the same on both sides. A match must be UNIQUE (exactly one monitor rect
// agrees); zero or several (a cloned/mirrored pair shares one rect) is UNKNOWN -- an empty string,
// never a guess -- and an unknown device matches no "\\.\" preference.
//
// Pure functions over plain Qt value types, so console_tests pins them with the recorded UM values;
// the Win32 enumerator below is the only platform code, and the qscreen probe calls the same one.
#include <QList>
#include <QPoint>
#include <QRect>
#include <QSize>
#include <QString>

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#endif

namespace DisplayDeviceMapping
{

struct MonitorRect
{
    QString device; // "\\.\DISPLAYn"
    QRect rect;     // native (physical) desktop rectangle
};

// A monitor rect agrees with a screen when the top-left is identical and each physical extent is
// within kSizeTolerancePx (Qt rounds a fractional-DPR logical size; the origin is never rounded).
static const int kSizeTolerancePx = 2;

// The GDI device of the ONE monitor whose native rect matches, or an empty string (UNKNOWN).
inline QString gdiDeviceForScreen( const QPoint &nativeTopLeft, const QSize &physicalSize,
                                   const QList<MonitorRect> &monitors )
{
    QString found;
    int matches = 0;
    for( const MonitorRect &m : monitors )
    {
        if( m.device.isEmpty() ) continue;
        if( m.rect.topLeft() != nativeTopLeft ) continue;
        if( qAbs( m.rect.width() - physicalSize.width() ) > kSizeTolerancePx ) continue;
        if( qAbs( m.rect.height() - physicalSize.height() ) > kSizeTolerancePx ) continue;
        found = m.device;
        ++matches;
    }
    return matches == 1 ? found : QString();
}

// Which QScreen field a --display-prefer value matched (for logging), or an empty string.
// A "\\.\DISPLAYn" preference is an EXACT device-name comparison and never falls through to the
// substring fields (so \\.\DISPLAY1 cannot also select \\.\DISPLAY10): it matches the DERIVED
// gdiDevice, or a screenName that already IS the device name (a monitor with no EDID name).
// Anything else is a case-insensitive substring of name, then model, then manufacturer.
inline QString preferenceMatchedField( const QString &screenName, const QString &model,
                                       const QString &manufacturer, const QString &gdiDevice,
                                       const QString &prefer )
{
    if( prefer.isEmpty() ) return QString();
    if( prefer.startsWith( QStringLiteral( "\\\\.\\" ) ) )
    {
        if( !gdiDevice.isEmpty() && gdiDevice.compare( prefer, Qt::CaseInsensitive ) == 0 ) return QStringLiteral("device_name");
        if( screenName.compare( prefer, Qt::CaseInsensitive ) == 0 ) return QStringLiteral("device_name");
        return QString();
    }
    if( screenName.contains( prefer, Qt::CaseInsensitive ) ) return QStringLiteral("name");
    if( model.contains( prefer, Qt::CaseInsensitive ) ) return QStringLiteral("model");
    if( manufacturer.contains( prefer, Qt::CaseInsensitive ) ) return QStringLiteral("manufacturer");
    return QString();
}

#ifdef Q_OS_WIN
// Every attached monitor's GDI device and native rectangle (read-only; no display state changes).
// The process must be per-monitor DPI aware for rcMonitor to be physical -- a Qt GUI process is.
inline QList<MonitorRect> enumerateGdiMonitors()
{
    QList<MonitorRect> monitors;
    auto onMonitor = []( HMONITOR handle, HDC, LPRECT, LPARAM data ) -> BOOL
    {
        MONITORINFOEXW info;
        info.cbSize = sizeof( info );
        if( GetMonitorInfoW( handle, &info ) )
        {
            MonitorRect m;
            m.device = QString::fromWCharArray( info.szDevice );
            m.rect = QRect( info.rcMonitor.left, info.rcMonitor.top,
                            info.rcMonitor.right - info.rcMonitor.left,
                            info.rcMonitor.bottom - info.rcMonitor.top );
            reinterpret_cast<QList<MonitorRect> *>( data )->append( m );
        }
        return TRUE;
    };
    EnumDisplayMonitors( nullptr, nullptr, onMonitor, reinterpret_cast<LPARAM>( &monitors ) );
    return monitors;
}
#endif

} // namespace DisplayDeviceMapping

#endif // DISPLAYDEVICEMAPPING_H
