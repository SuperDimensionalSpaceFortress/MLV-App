#ifndef HUBHOSTPLAYBACKGUARD_H
#define HUBHOSTPLAYBACKGUARD_H

// HUB-VM-NO-LOCAL-PLAYBACK-1 (owner objection 2026-10-11): the hub VM (Virtual-Ten) is a VMware guest on Ultra-Magnus, so
// an automation playback run there shows a window on the owner's machine and contaminates the Ultra-Magnus venue legs.
// No MLVApp GUI or playback run of any kind starts on a hub host: playback, performance AND exactness, runs on a venue
// through the tracked venue chain. The refusal is placed where the action happens -- the app's own automation entries
// (--gui-smoke-playback, --profile-playback and the MLVAPP_AUTOPLAY_* hook) -- so a lane that writes its own launcher
// script is stopped as well as one that uses the tracked ones.
//
// Only the automation entries refuse. Normal interactive use of the app is untouched, and so are local builds and unit,
// console and pipeline tests.
//
// The host list and the three constants below are copies of tools/profiling/dual-venue/hub-hosts.json (the app cannot
// read a repo file at run time); tools/repo_hygiene/test_hub_vm_no_local_playback.py fails when a copy drifts.
//
//   MLVAPP_HUB_PLAYBACK_CI_FIXTURE=CI_FIXTURE_ONLY   lets a CI fixture / unit test exercise a guarded surface on a hub
//                                                    host. It is not a way to play on the hub: a command that assigns it
//                                                    in a lane is a hook-blocked act.

#include <QByteArray>
#include <QString>
#include <QSysInfo>

namespace hub_host_playback_guard {

inline const char *const kHubHosts[] = { "VIRTUAL-TEN" };
inline const char *const kRefusalToken = "HUB_VM_LOCAL_PLAYBACK_REFUSED";
inline const char *const kOverrideEnv = "MLVAPP_HUB_PLAYBACK_CI_FIXTURE";
inline const char *const kOverrideValue = "CI_FIXTURE_ONLY";
constexpr int kRefusalExitCode = 17;

// Whole-name, case-insensitive match against the tracked hub-host list.
inline bool isHubHost( const QString &computerName )
{
    const QString name = computerName.trimmed();
    if( name.isEmpty() ) return false;
    for( const char *host : kHubHosts )
    {
        if( name.compare( QString::fromLatin1( host ), Qt::CaseInsensitive ) == 0 ) return true;
    }
    return false;
}

// The exact value only: "1", "true" or any other spelling is not the override.
inline bool overrideActive( const QByteArray &overrideValue )
{
    return overrideValue.trimmed() == QByteArray( kOverrideValue );
}

// COMPUTERNAME first (what the venue table and every script key on), the OS host name as the fallback.
inline QString currentHostName( const QByteArray &computerNameEnv )
{
    const QString fromEnv = QString::fromLocal8Bit( computerNameEnv ).trimmed();
    return fromEnv.isEmpty() ? QSysInfo::machineHostName() : fromEnv;
}

// True when an automation playback entry must refuse to start on this host.
inline bool refuses( const QString &computerName, const QByteArray &overrideValue )
{
    return isHubHost( computerName ) && !overrideActive( overrideValue );
}

inline QString refusalMessage( const QString &computerName )
{
    return QStringLiteral( "[GUI-SMOKE] ERROR: " ) + QString::fromLatin1( kRefusalToken )
        + QStringLiteral( " (host " ) + computerName.trimmed()
        + QStringLiteral( " is the hub VM, a guest on Ultra-Magnus: no MLVApp playback, smoke or profile run starts here. "
                          "Run it as a venue job (Bachelor / Ultra-Magnus) through the tracked venue chain.)" );
}

} // namespace hub_host_playback_guard

#endif // HUBHOSTPLAYBACKGUARD_H
