/*!
 * \file LogLineField.h
 * \brief How a value from outside the app is written into a key=value log line, and how CrashForensics folds a log record onto
 *        one line (pure, unit-tested).
 *
 * PLAYBACK-PREP-CONFLATED-MOVE-FR1B-AB-1 r2 (adversary A1 and A2 on #726): the playback_smoke.* lines are read back by the
 * dual-venue runner as key=value fields separated by single spaces. envValueForLog used to map only space and tab, so an
 * inherited value kept CR, LF and Unicode spaces, and CrashForensics then turned CR/LF into separators: a launcher environment
 * holding MLVAPP_PLAYBACK_SCOPE_INTERVAL_MS="150\rprep_conflated_move_active=1" or MLVAPP_FORCE_SINGLETHREAD="0\rworker_threads=8"
 * wrote a field of its own onto the start line. fieldValue() maps every character that could end a field, or be read as one
 * later, to '_': C0 and C1 controls, DEL, every Unicode space, line and paragraph separator, every format character (zero-width
 * space and joiners, bidi marks) and '%' (a QString::arg placeholder, so a value cannot shift the fields after it).
 */

#ifndef LOGLINEFIELD_H
#define LOGLINEFIELD_H

#include <QChar>
#include <QString>
#include <QtGlobal>

namespace log_line_field
{

inline bool breaksField( QChar c )
{
    const ushort u = c.unicode();
    if( u < 0x20 || u == 0x7f || ( u >= 0x80 && u <= 0x9f ) ) return true;   // C0, DEL, C1
    if( u == '%' ) return true;
    if( c.isSpace() ) return true;
    switch( c.category() )
    {
    case QChar::Separator_Space:
    case QChar::Separator_Line:
    case QChar::Separator_Paragraph:
    case QChar::Other_Control:
    case QChar::Other_Format:
        return true;
    default:
        return false;
    }
}

// A value written as one field: every character that breaksField() is '_'. Empty stays empty.
inline QString fieldValue( const QString &raw )
{
    QString value = raw;
    for( int i = 0; i < value.size(); ++i )
        if( breaksField( value.at( i ) ) ) value[i] = QLatin1Char( '_' );
    return value;
}

// An environment variable as one field: "unset" when it is unset or empty.
inline QString envFieldValue( const char *name )
{
    const QString value = qEnvironmentVariable( name );
    return value.isEmpty() ? QStringLiteral( "unset" ) : fieldValue( value );
}

// CrashForensics::writeLogLine's fold: a record stays on one line of the log file (CR -> space, LF -> " | ").
inline QString recordMessage( const QString &message )
{
    QString sanitized = message;
    sanitized.replace( QLatin1Char( '\r' ), QLatin1Char( ' ' ) );
    sanitized.replace( QLatin1Char( '\n' ), QStringLiteral( " | " ) );
    return sanitized;
}

// The engagement record of the prep-conflated-move route for one playback session. It carries no text from outside the app, so
// nothing a launcher sets can forge it; the dual-venue runner reads engagement from this line only (Get-DvPrepConflatedMoveEvidence).
inline QString prepRouteLine( qulonglong session, bool prepConflatedMoveActive )
{
    return QStringLiteral( "playback_smoke.prep_route session=%1 prep_conflated_move_active=%2" )
        .arg( session )
        .arg( prepConflatedMoveActive ? 1 : 0 );
}

} // namespace log_line_field

#endif // LOGLINEFIELD_H
