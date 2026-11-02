// PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r2 (adversary A1/A2 on #726): the hostile-environment run log, written by the app's real
// field sanitizer (log_line_field::envFieldValue, the body of MainWindow's envValueForLog) and CrashForensics' real record fold
// (log_line_field::recordMessage), for the dual-venue runner to read back. The start line keeps MainWindow's field order and names
// (beginPlaybackSmokeTelemetry); the non-env fields are fixed values. Used by log_line_field_fixture.exe (writes the committed
// fixtures under tests/fixtures/log_line_field/) and by test_log_line_field.cpp (recomposes them and compares byte for byte).
#ifndef TESTS_CONSOLE_LOG_LINE_FIELD_FIXTURE_H
#define TESTS_CONSOLE_LOG_LINE_FIELD_FIXTURE_H

#include "../../platform/qt/LogLineField.h"

#include <QByteArray>
#include <QString>
#include <QStringList>
#include <QtGlobal>

#include <cstdlib>

namespace log_line_field_fixture
{

struct Variant
{
    const char *name;
    const char *envName;
    QString hostileValue;
};

// CR and LF reach CrashForensics as separators; U+00A0 and U+2028 are Unicode spaces a \S+ reader splits on.
inline QList<Variant> variants()
{
    return {
        { "cr-prep", "MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS", QStringLiteral( "150\rprep_conflated_move_active=1" ) },
        { "lf-prep", "MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS", QStringLiteral( "150\nprep_conflated_move_active=1" ) },
        { "nbsp-prep", "MLVAPP_FORCE_THREADS", QString::fromUtf8( "0\xc2\xa0prep_conflated_move_active=1" ) },
        { "cr-worker", "MLVAPP_FORCE_SINGLETHREAD", QStringLiteral( "0\rworker_threads=8" ) },
        { "lf-worker", "MLVAPP_FORCE_SINGLETHREAD", QStringLiteral( "0\nworker_threads=8" ) },
        { "ls-worker", "MLVAPP_FORCE_SINGLETHREAD", QString::fromUtf8( "0\xe2\x80\xa8worker_threads=8" ) },
    };
}

inline const char *const *startEnvNames()
{
    static const char *const names[] = {
        "MLVAPP_FORCE_THREADS", "MLVAPP_FORCE_SINGLETHREAD", "MLVAPP_PLAYBACK_MAX_THREADS", "MLVAPP_DISABLE_PLAYBACK_THREAD_CAP",
        "MLVAPP_PLAYBACK_SCALE_FACTOR", "MLVAPP_PLAYBACK_PREFER_HQ_MEAN23", "MLVAPP_PLAYBACK_SMOKE_TELEMETRY",
        "MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS", "OMP_NUM_THREADS", "MLVAPP_DISABLE_RAW_UINT16_PREFETCH", "MLVAPP_DISABLE_PLAY_START_PREROLL",
        "MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW", "MLVAPP_PLAYBACK_PREVIEW_MODE", "MLVAPP_PLAYBACK_QUALITY_MODE", "MLVAPP_GPU_PLAYBACK_RECON",
        "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT", "MLVAPP_RAW_UINT16_PREFETCH_DECODERS", nullptr };
    return names;
}

// The exact UTF-16 value, as a launcher's wide environment block carries it (qputenv goes through the ANSI code page on Windows,
// which cannot hold U+2028).
inline void setEnv( const char *name, const QString &value )
{
#ifdef Q_OS_WIN
    _wputenv_s( reinterpret_cast<const wchar_t *>( QString::fromLatin1( name ).utf16() ), reinterpret_cast<const wchar_t *>( value.utf16() ) );
#else
    qputenv( name, value.toUtf8() );
#endif
}

inline QString record( const QString &message )
{
    return QStringLiteral( "[2026-10-31T00:00:00.000Z] [INFO] [0x1] " ) + log_line_field::recordMessage( message ) + QLatin1Char( '\n' );
}

// The run log of one measured session (id 3) at 12 workers with the variant's hostile value inherited from the launcher.
// withPrepRoute=false is the r1 head (67998675), which emits no playback_smoke.prep_route line.
inline QByteArray runLog( const Variant &variant, bool withPrepRoute )
{
    for( const char *const *n = startEnvNames(); *n; ++n ) qunsetenv( *n );
    setEnv( variant.envName, variant.hostileValue );
    auto env = []( const char *name ) { return log_line_field::envFieldValue( name ); };
    const QString start = QStringLiteral(
        "playback_smoke.start session=%1 position=%2 cut_in=%3 cut_out=%4 "
        "scale_request=%5 quality_mode=%6 worker_threads=%7 start_serial=%8 "
        "frame_telemetry=%9 drop_frame=%10 audio=%11 scopes=%12 zebras=%13 "
        "file_loaded=%14 env_force_threads=%15 env_force_singlethread=%16 "
        "env_playback_max_threads=%17 env_disable_playback_thread_cap=%18 "
        "env_playback_scale=%19 env_hq_mean23=%20 "
        "env_playback_smoke_telemetry=%21 "
        "env_playback_scope_interval_ms=%22 env_omp_num_threads=%23 "
        "env_disable_raw_uint16_prefetch=%24 "
        "env_disable_play_start_preroll=%25 "
        "preview_mode=%26 env_aggressive_preview=%27 env_preview_mode=%28 "
        "env_quality_mode=%29 env_gpu_playback_recon=%30 "
        "env_gpu_playback_recon_texture_present=%31 "
        "auto_target_fps=%32 auto_reason_start=%33 "
        "env_raw_uint16_prefetch_decoders=%34" )
        .arg( 3ULL ).arg( 0 ).arg( 0 ).arg( 1242 ).arg( 2 ).arg( 0 ).arg( 12 ).arg( 2ULL )
        .arg( 1 ).arg( 1 ).arg( 0 ).arg( 0 ).arg( 0 ).arg( 1 )
        .arg( env( "MLVAPP_FORCE_THREADS" ) ).arg( env( "MLVAPP_FORCE_SINGLETHREAD" ) ).arg( env( "MLVAPP_PLAYBACK_MAX_THREADS" ) )
        .arg( env( "MLVAPP_DISABLE_PLAYBACK_THREAD_CAP" ) ).arg( env( "MLVAPP_PLAYBACK_SCALE_FACTOR" ) )
        .arg( env( "MLVAPP_PLAYBACK_PREFER_HQ_MEAN23" ) ).arg( env( "MLVAPP_PLAYBACK_SMOKE_TELEMETRY" ) )
        .arg( env( "MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS" ) ).arg( env( "OMP_NUM_THREADS" ) )
        .arg( env( "MLVAPP_DISABLE_RAW_UINT16_PREFETCH" ) ).arg( env( "MLVAPP_DISABLE_PLAY_START_PREROLL" ) )
        .arg( QStringLiteral( "sharp_smooth" ) )
        .arg( env( "MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW" ) ).arg( env( "MLVAPP_PLAYBACK_PREVIEW_MODE" ) )
        .arg( env( "MLVAPP_PLAYBACK_QUALITY_MODE" ) ).arg( env( "MLVAPP_GPU_PLAYBACK_RECON" ) )
        .arg( env( "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT" ) )
        .arg( 0.0 ).arg( QStringLiteral( "none" ) )
        .arg( env( "MLVAPP_RAW_UINT16_PREFETCH_DECODERS" ) );
    QString log = record( QStringLiteral( "playback_smoke.measured_session id=3" ) ) + record( start );
    if( withPrepRoute ) log += record( log_line_field::prepRouteLine( 3, false ) );
    log += record( QStringLiteral( "playback_smoke.summary session=3 reason=reached presented_frames=600" ) );
    qunsetenv( variant.envName );
    return log.toUtf8();
}

} // namespace log_line_field_fixture

#endif // TESTS_CONSOLE_LOG_LINE_FIELD_FIXTURE_H
