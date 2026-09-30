// UM-DISPLAY-QT-WINDOWS-MAPPING-PROOF-1. PR #191 assumed QScreen::name() IS the GDI device name
// (\\.\DISPLAYn). Measured on Ultra-Magnus (Qt 6.10.2, windows QPA, session 1, 2026-09-30) it is
// NOT: a monitor with an EDID name reports that friendly name, and only a monitor without one
// (a VM's virtual display) falls back to the GDI name. So a job-resolved '\\.\DISPLAY1'
// preference was compared against the string "PA329C" and never matched -- the preference
// silently vanished and the equal-4K refresh tie fell to the primary LG TV. These tests pin the
// recorded UM values (fixture: tests/fixtures/display/um-qscreen-windows-mapping-20260930.txt)
// against the pure mapping functions the app and the probe both use.
#include "../common/minitest.h"

#include "../../platform/qt/DisplayDeviceMapping.h"

using DisplayDeviceMapping::MonitorRect;
using DisplayDeviceMapping::gdiDeviceForScreen;
using DisplayDeviceMapping::preferenceMatchedField;

namespace
{

// Recorded UM topology: EnumDisplaySettings position + mode per attached adapter (physical px).
QList<MonitorRect> umMonitors()
{
    return { { QStringLiteral("\\\\.\\DISPLAY1"), QRect( 3840, 0, 3840, 2160 ) },
             { QStringLiteral("\\\\.\\DISPLAY2"), QRect( 0, 0, 3840, 2160 ) } };
}

const QString kDisplay1 = QStringLiteral("\\\\.\\DISPLAY1");
const QString kDisplay2 = QStringLiteral("\\\\.\\DISPLAY2");

} // namespace

TEST(DisplayDeviceMapping, UmQScreensMapToTheirGdiDevicesByNativeTopLeftAndPhysicalSize)
{
    // Qt: "PA329C" geometry 3840,0 2560x1440 dpr 1.5 -> physical 3840x2160; "LG TV" geometry 0,0.
    ASSERT_TRUE( gdiDeviceForScreen( QPoint( 3840, 0 ), QSize( 3840, 2160 ), umMonitors() ) == kDisplay1 );
    ASSERT_TRUE( gdiDeviceForScreen( QPoint( 0, 0 ), QSize( 3840, 2160 ), umMonitors() ) == kDisplay2 );
}

TEST(DisplayDeviceMapping, UmDevicePreferenceSelectsTheAsusEvenThoughQtNamesItPA329C)
{
    // The recorded QScreen fields (the shape the resolver hands the app: --display-prefer \\.\DISPLAY1).
    const QString asusDevice = gdiDeviceForScreen( QPoint( 3840, 0 ), QSize( 3840, 2160 ), umMonitors() );
    const QString lgDevice = gdiDeviceForScreen( QPoint( 0, 0 ), QSize( 3840, 2160 ), umMonitors() );
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("PA329C"), QStringLiteral("PA329C"),
                                         QStringLiteral("ASUSTek COMPUTER INC"), asusDevice, kDisplay1 )
                 == QStringLiteral("device_name") );
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("LG TV"), QStringLiteral("LG TV"),
                                         QStringLiteral("on"), lgDevice, kDisplay1 ).isEmpty() );
}

TEST(DisplayDeviceMapping, AnUnmappableScreenNeverMatchesADevicePreference)
{
    // No monitor at that origin, a size that differs, or two monitors on one rect (clone mode):
    // the device is UNKNOWN (empty), and an unknown device matches no \\.\ preference.
    ASSERT_TRUE( gdiDeviceForScreen( QPoint( 7680, 0 ), QSize( 3840, 2160 ), umMonitors() ).isEmpty() );
    ASSERT_TRUE( gdiDeviceForScreen( QPoint( 3840, 0 ), QSize( 1920, 1080 ), umMonitors() ).isEmpty() );
    QList<MonitorRect> clone = umMonitors();
    clone[1].rect = QRect( 3840, 0, 3840, 2160 );
    ASSERT_TRUE( gdiDeviceForScreen( QPoint( 3840, 0 ), QSize( 3840, 2160 ), clone ).isEmpty() );
    ASSERT_TRUE( gdiDeviceForScreen( QPoint( 3840, 0 ), QSize( 3840, 2160 ), QList<MonitorRect>() ).isEmpty() );
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("PA329C"), QStringLiteral("PA329C"),
                                         QStringLiteral("ASUSTek COMPUTER INC"), QString(), kDisplay1 ).isEmpty() );
}

TEST(DisplayDeviceMapping, ADeviceNameThatIsAlreadyTheQScreenNameStillMatches)
{
    // A monitor with no EDID name (the VM's virtual display): Qt DOES report the GDI name.
    ASSERT_TRUE( preferenceMatchedField( kDisplay1, QString(), QString(), QString(), kDisplay1 )
                 == QStringLiteral("device_name") );
}

TEST(DisplayDeviceMapping, DeviceNamePreferenceIsExactSoDisplay1NeverSelectsDisplay10)
{
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("X"), QStringLiteral("X"), QStringLiteral("X"),
                                         QStringLiteral("\\\\.\\DISPLAY10"), kDisplay1 ).isEmpty() );
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("\\\\.\\DISPLAY10"), QString(), QString(),
                                         QString(), kDisplay1 ).isEmpty() );
}

TEST(DisplayDeviceMapping, ASubstringPreferenceStillMatchesNameModelOrManufacturerInThatOrder)
{
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("PA329C"), QStringLiteral("Z"), QStringLiteral("Y"),
                                         QString(), QStringLiteral("pa329") ) == QStringLiteral("name") );
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("A"), QStringLiteral("PA329C"), QStringLiteral("Y"),
                                         QString(), QStringLiteral("pa329") ) == QStringLiteral("model") );
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("A"), QStringLiteral("B"), QStringLiteral("ASUSTek"),
                                         QString(), QStringLiteral("asus") ) == QStringLiteral("manufacturer") );
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("A"), QStringLiteral("B"), QStringLiteral("C"),
                                         QString(), QStringLiteral("nomatch") ).isEmpty() );
    ASSERT_TRUE( preferenceMatchedField( QStringLiteral("A"), QStringLiteral("B"), QStringLiteral("C"),
                                         QString(), QString() ).isEmpty() );
}
