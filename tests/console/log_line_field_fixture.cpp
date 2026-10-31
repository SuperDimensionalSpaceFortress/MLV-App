// PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r2: writes the hostile-environment run logs (log_line_field_fixture.h) to
// <outDir>/<prefix>-<variant>.log. Usage: log_line_field_fixture <outDir> <prefix> [--no-prep-route]
// tests/fixtures/log_line_field/green-*.log are this program's output on the r2 head; red-*.log are its output built against
// the r1 head's transformation (see that folder's README.md). tools/repo_hygiene/test_log_line_field_e2e.py reads both.
#include "log_line_field_fixture.h"

#include <QCoreApplication>
#include <QDir>
#include <QFile>

#include <cstdio>

int main( int argc, char **argv )
{
    QCoreApplication app( argc, argv );
    if( argc < 3 )
    {
        std::fprintf( stderr, "usage: log_line_field_fixture <outDir> <prefix> [--no-prep-route]\n" );
        return 2;
    }
    const QString outDir = QString::fromLocal8Bit( argv[1] );
    const QString prefix = QString::fromLocal8Bit( argv[2] );
    const bool withPrepRoute = !( argc > 3 && qstrcmp( argv[3], "--no-prep-route" ) == 0 );
    QDir().mkpath( outDir );
    for( const auto &variant : log_line_field_fixture::variants() )
    {
        QFile file( QDir( outDir ).filePath( prefix + QLatin1Char( '-' ) + QString::fromLatin1( variant.name ) + QStringLiteral( ".log" ) ) );
        if( !file.open( QIODevice::WriteOnly | QIODevice::Truncate ) ) return 1;
        // r3 (sol on #726): a failed or short write is a failed fixture, never a success exit.
        const QByteArray bytes = log_line_field_fixture::runLog( variant, withPrepRoute );
        if( file.write( bytes ) != bytes.size() || !file.flush() )
        {
            std::fprintf( stderr, "log_line_field_fixture: short write to %s\n", qPrintable( file.fileName() ) );
            return 1;
        }
        std::printf( "%s\n", qPrintable( file.fileName() ) );
    }
    return 0;
}
