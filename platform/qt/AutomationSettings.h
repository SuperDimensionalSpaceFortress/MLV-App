#ifndef AUTOMATIONSETTINGS_H
#define AUTOMATIONSETTINGS_H

// PLAYBACK-CLIP-LENGTH-ENFORCE-3 round 2 (sol H2 / fable H5): an AUTOMATION run (--gui-smoke-playback,
// --profile-playback, the MLVAPP_AUTOPLAY_* hook) reads and writes a RUN-SCOPED settings store, never the venue's.
//
// QSettings( UserScope, org, app ) is always the NATIVE store (the registry on Windows) whatever
// QSettings::setDefaultFormat says, so the app cannot be redirected from outside: every place that opens the app's
// settings goes through openAppSettings(). Normally that is exactly the QSettings the app always used; in an
// automation run it is an INI file inside a directory of the run's own, so a persisted fpsOverride / frameRate /
// dragFrameMode is never even READ and nothing the run saves can reach the owner's interactive settings.
// isolateAutomationPacing() (MainWindow) stays as the second line of defence. A static class test fails on any raw
// QSettings( UserScope, ... ) in platform/qt outside this header.
//
//   MLVAPP_AUTOMATION_SETTINGS_DIR   the directory to use (a test seeds the INI file the app will read there);
//                                    unset = a fresh directory under the system temp location, removed at exit.

#include <QDir>
#include <QSettings>
#include <QString>
#include <QUuid>

#include <memory>

namespace automation_settings {

inline bool &isolatedFlag()
{
    static bool isolatedNow = false;
    return isolatedNow;
}

inline QString &storeFileRef()
{
    static QString file;
    return file;
}

inline bool isolated() { return isolatedFlag(); }

// The INI file an automation run reads and writes ("" outside an automation run).
inline QString storeFile() { return storeFileRef(); }

inline QString storeFileIn( const QString &dir ) { return QDir( dir ).filePath( QStringLiteral( "MLVApp.ini" ) ); }

// Returns the directory in use, or an empty string when it could not be created (the caller fails closed).
// `created` is set when this call made a fresh directory of its own (the caller removes that one at exit).
inline QString isolate( const QString &requestedDir, bool *created = nullptr )
{
    QString dir = requestedDir.trimmed();
    bool ownDir = false;
    if( dir.isEmpty() )
    {
        dir = QDir::tempPath() + QStringLiteral( "/mlvapp-automation-settings-" )
            + QUuid::createUuid().toString( QUuid::WithoutBraces );
        ownDir = true;
    }
    if( !QDir().mkpath( dir ) ) return QString();
    storeFileRef() = storeFileIn( dir );
    isolatedFlag() = true;
    if( created ) *created = ownDir;
    return dir;
}

// THE way the app opens its settings. Outside an automation run: the native QSettings the app has always used.
inline std::unique_ptr<QSettings> openAppSettings()
{
    if( isolatedFlag() )
        return std::unique_ptr<QSettings>( new QSettings( storeFileRef(), QSettings::IniFormat ) );
    return std::unique_ptr<QSettings>( new QSettings( QSettings::UserScope,
                                                      QStringLiteral( "magiclantern.MLVApp" ),
                                                      QStringLiteral( "MLVApp" ) ) );
}

// Test hook: put the process back on the native store so later tests in the same binary are unaffected.
inline void releaseForTest()
{
    isolatedFlag() = false;
    storeFileRef().clear();
}

} // namespace automation_settings

#endif // AUTOMATIONSETTINGS_H
