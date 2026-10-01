/* PLAYBACK-CLIP-LENGTH-ENFORCE-3 round 2 (sol H2 / fable H5): an automation run uses a RUN-SCOPED settings store.
 *
 * automation_settings::openAppSettings() is how the whole app opens its settings. Outside an automation run it is the
 * native store the app always used; after isolate() it is an INI file inside a directory of the run's own, so the
 * venue's persisted pacing (fpsOverride, frameRate, dragFrameMode) is never read and nothing the run saves reaches
 * the owner's interactive settings. */

#include "../common/minitest.h"

#include <QDir>
#include <QFile>
#include <QSettings>
#include <QString>
#include <QUuid>

#include "../../platform/qt/AutomationSettings.h"

namespace
{

QString freshDir()
{
    return QDir::tempPath() + QStringLiteral( "/mlvapp-automation-settings-test-" )
         + QUuid::createUuid().toString( QUuid::WithoutBraces );
}

// Restores the native store however the test ends.
struct ReleaseGuard
{
    ~ReleaseGuard() { automation_settings::releaseForTest(); }
};

} // namespace

TEST( AutomationSettings, OutsideAnAutomationRunTheAppOpensTheNativeStoreItAlwaysUsed )
{
    ReleaseGuard guard;
    ASSERT_FALSE( automation_settings::isolated() );
    const std::unique_ptr<QSettings> settings = automation_settings::openAppSettings();
    ASSERT_TRUE( settings->format() == QSettings::NativeFormat );
    ASSERT_TRUE( automation_settings::storeFile().isEmpty() );
}

TEST( AutomationSettings, AnAutomationRunOpensAnIniFileInTheRunDirectoryAndWritesOnlyThere )
{
    ReleaseGuard guard;
    const QString dir = freshDir();
    bool created = true;
    ASSERT_EQ( dir.toStdString(), automation_settings::isolate( dir, &created ).toStdString() );
    ASSERT_FALSE( created );                                   // a caller-supplied directory is never ours to remove
    ASSERT_TRUE( automation_settings::isolated() );

    {
        const std::unique_ptr<QSettings> settings = automation_settings::openAppSettings();
        ASSERT_TRUE( settings->format() == QSettings::IniFormat );
        settings->setValue( "fpsOverride", true );
        settings->setValue( "frameRate", 12.0 );
        settings->sync();
    }
    ASSERT_TRUE( QFile::exists( QDir( dir ).filePath( QStringLiteral( "MLVApp.ini" ) ) ) );   // the run's own store ...

    automation_settings::releaseForTest();
    const std::unique_ptr<QSettings> native = automation_settings::openAppSettings();
    ASSERT_TRUE( native->format() == QSettings::NativeFormat );   // ... and the process is back on the native store
    ASSERT_FALSE( automation_settings::isolated() );
    ASSERT_TRUE( QDir( dir ).removeRecursively() );
}

TEST( AutomationSettings, ASeededPersistedOverrideIsReadOnlyFromTheRunScopedStore )
{
    ReleaseGuard guard;
    const QString dir = freshDir();
    ASSERT_TRUE( QDir().mkpath( dir ) );
    {
        QFile seeded( QDir( dir ).filePath( QStringLiteral( "MLVApp.ini" ) ) );
        ASSERT_TRUE( seeded.open( QIODevice::WriteOnly | QIODevice::Text ) );
        seeded.write( "[General]\nfpsOverride=true\nframeRate=12\ndragFrameMode=false\n" );
    }
    ASSERT_FALSE( automation_settings::isolate( dir ).isEmpty() );
    const std::unique_ptr<QSettings> settings = automation_settings::openAppSettings();
    ASSERT_TRUE( settings->value( "fpsOverride", false ).toBool() );
    ASSERT_EQ( 12, settings->value( "frameRate", 0 ).toInt() );
    ASSERT_FALSE( settings->value( "dragFrameMode", true ).toBool() );
}

TEST( AutomationSettings, WithNoRequestedDirectoryTheRunGetsAFreshEmptyStoreOfItsOwn )
{
    ReleaseGuard guard;
    bool created = false;
    const QString dir = automation_settings::isolate( QString(), &created );
    ASSERT_FALSE( dir.isEmpty() );
    ASSERT_TRUE( created );
    ASSERT_TRUE( QDir( dir ).exists() );
    const std::unique_ptr<QSettings> settings = automation_settings::openAppSettings();
    ASSERT_FALSE( settings->contains( "fpsOverride" ) );        // nothing of the venue's is visible
    ASSERT_FALSE( settings->contains( "frameRate" ) );
    ASSERT_FALSE( settings->contains( "dragFrameMode" ) );
    ASSERT_TRUE( QDir( dir ).removeRecursively() );
}

TEST( AutomationSettings, AnUncreatableDirectoryFailsClosedWithAnEmptyResult )
{
    ReleaseGuard guard;
    // A path whose parent is a FILE can never be created as a directory.
    const QString file = freshDir();
    ASSERT_TRUE( QDir().mkpath( file ) );
    const QString blocker = QDir( file ).filePath( QStringLiteral( "blocker" ) );
    {
        QFile f( blocker );
        ASSERT_TRUE( f.open( QIODevice::WriteOnly ) );
    }
    ASSERT_TRUE( automation_settings::isolate( blocker + QStringLiteral( "/inner" ) ).isEmpty() );
    ASSERT_FALSE( automation_settings::isolated() );
    ASSERT_TRUE( QDir( file ).removeRecursively() );
}
