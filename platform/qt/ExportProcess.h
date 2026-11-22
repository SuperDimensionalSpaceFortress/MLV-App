#pragma once

#include <QByteArray>
#include <QElapsedTimer>
#include <QProcess>
#include <QString>
#include <QStringList>
#include <QVector>
#include <memory>

namespace export_process
{

struct Invocation
{
    QString program;
    QStringList arguments;
};

struct ArgumentReplacement
{
    QString placeholder;
    QString value;
};

inline Invocation invocationFromTemplate( const QString &program,
                                          const QString &argumentTemplate,
                                          const QVector<ArgumentReplacement> &replacements = {} )
{
    QStringList arguments = QProcess::splitCommand( argumentTemplate );
    for( QString &argument : arguments )
    {
        for( const ArgumentReplacement &replacement : replacements )
        {
            argument.replace( replacement.placeholder, replacement.value );
        }
    }
    return Invocation{ program, arguments };
}

inline Invocation rawVideoInvocation( const QString &program,
                                      const QString &fps,
                                      const QString &resolution )
{
    return Invocation{
        program,
        QStringList{ QStringLiteral("-r"), fps,
                     QStringLiteral("-y"),
                     QStringLiteral("-f"), QStringLiteral("rawvideo"),
                     QStringLiteral("-s"), resolution,
                     QStringLiteral("-pix_fmt"), QStringLiteral("rgb48"),
                     QStringLiteral("-i"), QStringLiteral("-") }
    };
}

class StreamingProcess
{
public:
    bool start( const Invocation &invocation, int timeoutMs = 10000 )
    {
        m_process.setProcessChannelMode( QProcess::SeparateChannels );
        m_process.start( invocation.program, invocation.arguments, QIODevice::ReadWrite );
        if( m_process.waitForStarted( timeoutMs ) ) return true;
        captureDiagnostics();
        return false;
    }

    bool writeAll( const char *data, qint64 size, int timeoutMs = 30000 )
    {
        qint64 written = 0;
        while( written < size )
        {
            captureDiagnostics();
            const qint64 accepted = m_process.write( data + written, size - written );
            if( accepted < 0 )
            {
                captureDiagnostics();
                return false;
            }
            written += accepted;
            if( written < size && !m_process.waitForBytesWritten( timeoutMs ) )
            {
                captureDiagnostics();
                return false;
            }
            captureDiagnostics();
        }
        return true;
    }

    bool finish( int timeoutMs = 120000 )
    {
        m_process.closeWriteChannel();
        QElapsedTimer timer;
        timer.start();
        while( m_process.state() != QProcess::NotRunning )
        {
            captureDiagnostics();
            const qint64 remaining = timeoutMs - timer.elapsed();
            if( remaining <= 0 )
            {
                cancel();
                captureDiagnostics();
                return false;
            }
            m_process.waitForReadyRead( static_cast<int>( qMin<qint64>( remaining, 100 ) ) );
        }
        captureDiagnostics();
        return m_process.exitStatus() == QProcess::NormalExit && m_process.exitCode() == 0;
    }

    void cancel()
    {
        if( m_process.state() != QProcess::NotRunning )
        {
            m_process.terminate();
            if( !m_process.waitForFinished( 2000 ) )
            {
                m_process.kill();
                m_process.waitForFinished( 2000 );
            }
        }
        captureDiagnostics();
    }

    QString diagnostics() const { return QString::fromUtf8( m_diagnosticTail ); }

private:
    void captureDiagnostics()
    {
        m_diagnosticTail.append( m_process.readAllStandardError() );
        m_diagnosticTail.append( m_process.readAllStandardOutput() );
        static const int kMaxDiagnosticBytes = 8192;
        if( m_diagnosticTail.size() > kMaxDiagnosticBytes )
            m_diagnosticTail = m_diagnosticTail.right( kMaxDiagnosticBytes );
    }

    QProcess m_process;
    QByteArray m_diagnosticTail;
};

class StreamingPipeline
{
public:
    bool start( const QVector<Invocation> &invocations, int timeoutMs = 10000 )
    {
        if( invocations.isEmpty() ) return false;
        m_processes.clear();
        m_diagnosticTail.clear();
        for( int i = 0; i < invocations.size(); ++i )
        {
            auto process = std::make_unique<QProcess>();
            process->setProcessChannelMode( QProcess::SeparateChannels );
            m_processes.push_back( std::move( process ) );
        }
        for( size_t i = 0; i + 1 < m_processes.size(); ++i )
            m_processes[i]->setStandardOutputProcess( m_processes[i + 1].get() );

        for( size_t i = 0; i < m_processes.size(); ++i )
        {
            m_processes[i]->start( invocations[i].program,
                                   invocations[i].arguments,
                                   QIODevice::ReadWrite );
            if( !m_processes[i]->waitForStarted( timeoutMs ) )
            {
                captureDiagnostics();
                cancel();
                return false;
            }
        }
        return true;
    }

    bool writeAll( const char *data, qint64 size, int timeoutMs = 30000 )
    {
        if( m_processes.empty() ) return false;
        qint64 written = 0;
        while( written < size )
        {
            captureDiagnostics();
            const qint64 accepted = m_processes.front()->write( data + written, size - written );
            if( accepted < 0 ) { captureDiagnostics(); return false; }
            written += accepted;
            if( written < size && !m_processes.front()->waitForBytesWritten( timeoutMs ) )
            {
                captureDiagnostics();
                return false;
            }
        }
        captureDiagnostics();
        return true;
    }

    bool finish( int timeoutMs = 120000 )
    {
        if( m_processes.empty() ) return false;
        m_processes.front()->closeWriteChannel();
        QElapsedTimer timer;
        timer.start();
        while( true )
        {
            captureDiagnostics();
            bool running = false;
            for( const auto &process : m_processes )
                running = running || process->state() != QProcess::NotRunning;
            if( !running ) break;
            const qint64 remaining = timeoutMs - timer.elapsed();
            if( remaining <= 0 ) { cancel(); return false; }
            for( const auto &process : m_processes )
                process->waitForReadyRead( static_cast<int>( qMin<qint64>( remaining, 25 ) ) );
        }
        captureDiagnostics();
        for( const auto &process : m_processes )
        {
            if( process->exitStatus() != QProcess::NormalExit || process->exitCode() != 0 )
                return false;
        }
        return true;
    }

    void cancel()
    {
        for( const auto &process : m_processes )
            if( process->state() != QProcess::NotRunning ) process->terminate();
        for( const auto &process : m_processes )
            if( process->state() != QProcess::NotRunning && !process->waitForFinished( 2000 ) ) process->kill();
        for( const auto &process : m_processes )
            if( process->state() != QProcess::NotRunning ) process->waitForFinished( 2000 );
        captureDiagnostics();
    }

    QString diagnostics() const { return QString::fromUtf8( m_diagnosticTail ); }

private:
    void captureDiagnostics()
    {
        for( const auto &process : m_processes )
        {
            m_diagnosticTail.append( process->readAllStandardError() );
            m_diagnosticTail.append( process->readAllStandardOutput() );
        }
        static const int kMaxDiagnosticBytes = 8192;
        if( m_diagnosticTail.size() > kMaxDiagnosticBytes )
            m_diagnosticTail = m_diagnosticTail.right( kMaxDiagnosticBytes );
    }

    std::vector<std::unique_ptr<QProcess>> m_processes;
    QByteArray m_diagnosticTail;
};

} // namespace export_process
