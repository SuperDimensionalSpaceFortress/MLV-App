/*!
 * \file PlaybackAudioGate.h
 * \brief The device the audio sink pulls from: silence while held, the clip's audio from a chosen position once
 *        handed over (PLAYBACK-STARTUP-WAIT-PREWARM-1 r2, QtCore only, unit-tested).
 *
 * Qt 6.10 pulls a sink's QIODevice only for what bytesAvailable() reports (pullFromQIODeviceToRingbuffer), so a
 * closed gate (0 available) leaves the sink running on silence with nothing of the clip consumed. closeGate() can
 * prime a fixed run of zeros first (PlaybackAudioStartPolicy.h kAudioPrimeMs); openAt() serves the content from a
 * byte offset and emits readyRead so the sink pulls at once. Sequential and unbuffered: the position is the gate's.
 */

#ifndef PLAYBACKAUDIOGATE_H
#define PLAYBACKAUDIOGATE_H

#include <QByteArray>
#include <QIODevice>
#include <QtGlobal>

#include <algorithm>
#include <cmath>
#include <cstring>

class PlaybackAudioGate : public QIODevice
{
public:
    PlaybackAudioGate( const QByteArray &content, int bytesPerFrame, int sampleRate, QObject *parent = nullptr )
        : QIODevice( parent )
        , m_content( content )
        , m_bytesPerFrame( bytesPerFrame > 0 ? bytesPerFrame : 1 )
        , m_sampleRate( sampleRate > 0 ? sampleRate : 1 )
    {
        QIODevice::open( QIODevice::ReadOnly | QIODevice::Unbuffered );
    }

    bool isSequential() const override { return true; }

    /*! Whole frames for \a ms of audio (never negative). */
    qint64 bytesForMs( double ms ) const
    {
        if( !( ms > 0.0 ) ) return 0;
        return static_cast<qint64>( std::floor( ms * m_sampleRate / 1000.0 + 1e-9 ) ) * m_bytesPerFrame;
    }
    double msForBytes( qint64 bytes ) const
    {
        return static_cast<double>( bytes / m_bytesPerFrame ) * 1000.0 / m_sampleRate;
    }

    /*! Silence: serve \a primeZeroBytes of zeros (whole frames), then nothing. */
    void closeGate( qint64 primeZeroBytes = 0 )
    {
        m_open = false;
        m_primeLeft = primeZeroBytes > 0 ? primeZeroBytes - primeZeroBytes % m_bytesPerFrame : 0;
    }

    /*! Serve the content from \a byteOffset (rounded down to a frame, clamped to the content) and let the sink pull. */
    void openAt( qint64 byteOffset )
    {
        m_primeLeft = 0;
        m_pos = clampToContent( byteOffset );
        m_open = true;
        emit readyRead();
    }

    bool gateOpen() const { return m_open; }
    qint64 position() const { return m_pos; }
    qint64 servedBytes() const { return m_served; }
    void resetServed() { m_served = 0; }

    qint64 bytesAvailable() const override
    {
        const qint64 own = m_open ? m_content.size() - m_pos : m_primeLeft;
        return ( own > 0 ? own : 0 ) + QIODevice::bytesAvailable();
    }

protected:
    qint64 readData( char *data, qint64 maxSize ) override
    {
        if( maxSize <= 0 ) return 0;
        if( !m_open )
        {
            const qint64 zeros = std::min( maxSize - maxSize % m_bytesPerFrame, m_primeLeft );
            if( zeros <= 0 ) return 0;
            std::memset( data, 0, static_cast<size_t>( zeros ) );
            m_primeLeft -= zeros;
            m_served += zeros;
            return zeros;
        }
        const qint64 n = std::min( maxSize, static_cast<qint64>( m_content.size() ) - m_pos );
        if( n <= 0 ) return 0;
        std::memcpy( data, m_content.constData() + m_pos, static_cast<size_t>( n ) );
        m_pos += n;
        m_served += n;
        return n;
    }

    qint64 writeData( const char *, qint64 ) override { return -1; }

private:
    qint64 clampToContent( qint64 byteOffset ) const
    {
        qint64 pos = byteOffset > 0 ? byteOffset - byteOffset % m_bytesPerFrame : 0;
        const qint64 size = m_content.size() - m_content.size() % m_bytesPerFrame;
        return pos < size ? pos : size;
    }

    QByteArray m_content;
    int m_bytesPerFrame;
    int m_sampleRate;
    bool m_open = false;
    qint64 m_pos = 0;
    qint64 m_primeLeft = 0;
    qint64 m_served = 0;
};

#endif // PLAYBACKAUDIOGATE_H
