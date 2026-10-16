/*!
 * \file AudioPlayback.cpp
 * \author masc4ii
 * \copyright 2017
 * \brief Handles the mlv audio playback
 */

#include "AudioPlayback.h"
#include <QtGlobal>
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
#include <QAudioDevice>
#include <QAudioFormat>
#include <QMediaDevices>
#endif
#include <QDebug>
#include <QMetaObject>
#include <QMutexLocker>
#include <QThread>
#include <QTimer>
#include <cmath>
#include <cstring>
#include <utility>
#ifdef Q_OS_WIN
#include <objbase.h>
#endif
#include "../../src/debug/StageTiming.h"
#include "PlaybackAudioGate.h"
#include "PlaybackWorkerJoin.h"

namespace
{
double audioNowMs()
{
    return mlv_stage_timing_now() * 1000.0;   // the clock the presents are stamped with
}

#ifdef Q_OS_LINUX
constexpr qsizetype kSinkBufferBytes = 131072;
#elif defined(Q_OS_WIN)
constexpr qsizetype kSinkBufferBytes = 32768;
#else //Q_OS_OSX
constexpr qsizetype kSinkBufferBytes = 524288;
#endif
}

//PLAYBACK-STARTUP-WAIT-PREWARM-1 r2: the QAudioSink and the gate it pulls from, as the start engine's SinkPort (audio
//thread only). A fresh gate per open: Qt 6.10 never disconnects a stream's readyRead connection to its device, so a
//gate reused after a reset would call into the destroyed stream.
class AudioPlayback::HeldSink : public playback_audio_start::SinkPort
{
public:
    HeldSink( AudioPlayback &owner, const QByteArray &content, const QAudioFormat &format )
        : m_owner( owner ), m_content( content ), m_format( format )
    {
    }
    ~HeldSink() override
    {
        resetSink();
        delete m_owner.m_pAudioOutput;
        m_owner.m_pAudioOutput = nullptr;
    }

    double nowMs() override { return audioNowMs(); }

    bool open( double primeMs ) override
    {
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
        // QAudioSink binds its device when it is made: after a default-device change, make it again on the new one
        const QAudioDevice device = QMediaDevices::defaultAudioOutput();
        if( m_owner.m_pAudioOutput && device.id() != m_deviceId )
        {
            resetSink();
            delete m_owner.m_pAudioOutput;
            m_owner.m_pAudioOutput = nullptr;
        }
        if( !m_owner.m_pAudioOutput )
        {
            m_owner.m_pAudioOutput = new QAudioSink( device, m_format, &m_owner );
            m_deviceId = device.id();
        }
#else
        if( !m_owner.m_pAudioOutput ) m_owner.m_pAudioOutput = new QAudioOutput( m_format, &m_owner );
#endif
        resetSink();
        m_owner.m_pAudioOutput->setBufferSize( kSinkBufferBytes );
        m_owner.m_pAudioOutput->setVolume( 1.0 );
        m_gate.reset( new PlaybackAudioGate( m_content, m_format.channelCount() * 2, m_format.sampleRate() ) );
        m_gate->closeGate( m_gate->bytesForMs( primeMs ) );
        m_owner.m_pAudioOutput->start( m_gate.get() );
        // the first fill runs on Qt's stream thread, started inside start(): give it a moment to land
        for( int i = 0; i < 20 && m_owner.m_pAudioOutput->processedUSecs() == 0
                        && m_owner.m_pAudioOutput->error() == QAudio::NoError; ++i )
            QThread::msleep( 1 );
        return m_owner.m_pAudioOutput->error() == QAudio::NoError
            && m_owner.m_pAudioOutput->state() != QAudio::StoppedState;
    }

    void close() override { resetSink(); }
    void suspend() override { if( m_owner.m_pAudioOutput ) m_owner.m_pAudioOutput->suspend(); }
    void resume() override { if( m_owner.m_pAudioOutput ) m_owner.m_pAudioOutput->resume(); }
    void silence() override { if( m_gate ) m_gate->closeGate( 0 ); }

    void handOver( double contentMs ) override
    {
        if( m_gate ) m_gate->openAt( m_gate->bytesForMs( contentMs ) );
    }

    double processedMs() override
    {
        return m_owner.m_pAudioOutput ? m_owner.m_pAudioOutput->processedUSecs() / 1000.0 : 0.0;
    }
    double servedMs() override { return m_gate ? m_gate->msForBytes( m_gate->servedBytes() ) : 0.0; }

    const char *unhealthyReason() override
    {
        if( !m_owner.m_pAudioOutput || !m_gate ) return "closed";
        if( m_owner.m_pAudioOutput->error() != QAudio::NoError ) return "error";
        if( m_owner.m_pAudioOutput->state() == QAudio::StoppedState ) return "stopped";
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
        if( QMediaDevices::defaultAudioOutput().id() != m_deviceId ) return "device_changed";
#endif
        return nullptr;
    }

private:
    void resetSink()
    {
        // discard, not drain: the stream stops now and nothing it queued plays
        if( m_owner.m_pAudioOutput && m_gate ) m_owner.m_pAudioOutput->reset();
        m_gate.reset();
    }

    AudioPlayback &m_owner;
    QByteArray m_content;
    QAudioFormat m_format;
    QByteArray m_deviceId;
    std::unique_ptr<PlaybackAudioGate> m_gate;
};

//Constructor
AudioPlayback::AudioPlayback( QObject *parent )
    : QObject( parent )
{
    m_audioEngineInitialized = false;
    m_audioEngineRunning = false;
    m_pByteArrayAudio = nullptr;
    m_pAudioStream = nullptr;
    m_pAudioOutput = nullptr;
}

//Destructor
AudioPlayback::~AudioPlayback()
{
    if( m_audioEngineInitialized ) resetAudioEngine();
}

//PLAYBACK-STARTUP-WAIT-PREWARM-1 Row A: queue work on the audio thread (in order), or run it here without one
template <typename F>
void AudioPlayback::post( F &&work )
{
    if( m_pWorkerThread ) QMetaObject::invokeMethod( this, std::forward<F>( work ), Qt::QueuedConnection );
    else work();
}

//Move this (parentless) object to its own thread
void AudioPlayback::startWorkerThread()
{
    if( m_pWorkerThread || parent() ) return;
    m_pWorkerThread = new QThread();
    m_pWorkerThread->setObjectName( QStringLiteral( "MLVApp audio" ) );
#ifdef Q_OS_WIN
    // started and finished are emitted on the new thread itself: COM lives exactly as long as the thread
    static thread_local bool comInitialized = false;
    connect( m_pWorkerThread, &QThread::started, []()
    {
        const HRESULT hr = CoInitializeEx( nullptr, COINIT_APARTMENTTHREADED );
        comInitialized = SUCCEEDED( hr );
    } );
    connect( m_pWorkerThread, &QThread::finished, []()
    {
        if( comInitialized ) CoUninitialize();
        comInitialized = false;
    } );
#endif
    moveToThread( m_pWorkerThread );
    m_pWorkerThread->start();
}

//Reset the engine on the audio thread, bring this object back to the caller's thread, then join. Both waits are
//bounded (r2: a WASAPI call hung on the audio thread used to hang ~MainWindow); false means the caller must not
//delete this object
bool AudioPlayback::shutdownWorkerThread()
{
    if( !m_pWorkerThread ) return true;
    QThread *callerThread = QThread::currentThread();
    const bool handedBack = playback_worker_join::runBounded( this, [this, callerThread]()
    {
        if( m_audioEngineInitialized ) resetEngineNow();
        delete m_pHoldTimer;
        m_pHoldTimer = nullptr;
        moveToThread( callerThread );
    }, playback_worker_join::kHandBackTimeoutMs );
    if( !handedBack )
    {
        qWarning() << "AudioPlayback: the audio thread did not hand back within"
                   << playback_worker_join::kHandBackTimeoutMs << "ms; leaving it running";
        return false;
    }
    if( !playback_worker_join::joinBounded( m_pWorkerThread, playback_worker_join::kJoinTimeoutMs ) )
    {
        qWarning() << "AudioPlayback: the audio thread did not finish within"
                   << playback_worker_join::kJoinTimeoutMs << "ms; leaving it running";
        m_pWorkerThread = nullptr;
        return false;
    }
    delete m_pWorkerThread;
    m_pWorkerThread = nullptr;
    return true;
}

//Initialize audio engine
void AudioPlayback::initAudioEngine( mlvObject_t *pMlvObject )
{
    if( !doesMlvHaveAudio( pMlvObject ) ) return;

    if( m_pWorkerThread )
    {
        // the mlvObject owns the samples: copy them here, on the caller's thread, and open the sink on the audio thread
        const QByteArray audio( reinterpret_cast<const char *>( getMlvAudioData( pMlvObject ) ),
                                static_cast<qsizetype>( getMlvAudioSize( pMlvObject ) ) );
        const uint32_t sampleRate = getMlvSampleRate( pMlvObject );
        const uint16_t channels = getMlvAudioChannels( pMlvObject );
        const double frameRate = getMlvFramerate( pMlvObject );
        post( [this, audio, sampleRate, channels, frameRate]()
        {
            initEngineFromBytes( audio, sampleRate, channels, frameRate );
        } );
        return;
    }

    m_audioSampleRate = getMlvSampleRate( pMlvObject );
    m_audioChannels = getMlvAudioChannels( pMlvObject );
    m_pMlvAudioData = getMlvAudioData( pMlvObject );
    m_mlvAudioSize = getMlvAudioSize( pMlvObject );
    m_mlvFrameRate = getMlvFramerate( pMlvObject );

    if( m_audioEngineInitialized ) resetAudioEngine();

    //Set up the format, eg.
    QAudioFormat format;
    format.setSampleRate( m_audioSampleRate );
    format.setChannelCount( m_audioChannels );
 #if QT_VERSION < QT_VERSION_CHECK(6, 0, 0)
    format.setSampleSize( 16 );
    format.setCodec( "audio/pcm" );
    format.setByteOrder( QAudioFormat::LittleEndian );
    format.setSampleType( QAudioFormat::SignedInt );
    m_pAudioOutput = new QAudioOutput( format, this );
#else
    format.setSampleFormat( QAudioFormat::Int16 );
    m_pAudioOutput = new QAudioSink(format, (QObject*)this );
#endif

    m_pByteArrayAudio = new QByteArray();
    m_pAudioStream = new QDataStream(m_pByteArrayAudio, QIODevice::ReadWrite);

    for( uint64_t x = 0; x < m_mlvAudioSize; x++ )
    {
        (*m_pAudioStream) << m_pMlvAudioData[x];
    }
    m_pAudioStream->device()->seek( 0 );
#ifdef Q_OS_LINUX
    m_pAudioOutput->setBufferSize( 131072 );
#elif defined(Q_OS_WIN)
    m_pAudioOutput->setBufferSize( 32768 );
#else //Q_OS_OSX
    m_pAudioOutput->setBufferSize( 524288 );
#endif
    m_pAudioOutput->setVolume( 1.0 );

    m_audioEngineInitialized = true;
    m_audioEngineRunning = false;
}

//Row A: the engine for the audio thread, from samples already copied: the sink pulls from a gate (r2)
void AudioPlayback::initEngineFromBytes( const QByteArray &audio, uint32_t sampleRate, uint16_t channels, double frameRate )
{
    m_audioSampleRate = sampleRate;
    m_audioChannels = channels;
    m_pMlvAudioData = nullptr;
    m_mlvAudioSize = static_cast<uint64_t>( audio.size() );
    m_mlvFrameRate = frameRate;

    if( m_audioEngineInitialized ) resetEngineNow();

    QAudioFormat format;
    format.setSampleRate( m_audioSampleRate );
    format.setChannelCount( m_audioChannels );
#if QT_VERSION < QT_VERSION_CHECK(6, 0, 0)
    format.setSampleSize( 16 );
    format.setCodec( "audio/pcm" );
    format.setByteOrder( QAudioFormat::LittleEndian );
    format.setSampleType( QAudioFormat::SignedInt );
#else
    format.setSampleFormat( QAudioFormat::Int16 );
#endif

    m_pHeldSink.reset( new HeldSink( *this, audio, format ) );
    m_pEngine.reset( new playback_audio_start::StartEngine( *m_pHeldSink ) );
    m_pEngine->setFps( m_mlvFrameRate );

    m_audioEngineInitialized = true;
    m_audioEngineRunning = false;
}

//Unload the audio of a mlv
void AudioPlayback::resetAudioEngine()
{
    post( [this]() { resetEngineNow(); } );
}

void AudioPlayback::resetEngineNow()
{
    if( !m_audioEngineInitialized ) return;

    if( m_pEngine )
    {
        // worker mode: close the held sink, then the sink itself (HeldSink owns it)
        if( m_pHoldTimer ) m_pHoldTimer->stop();
        m_pEngine->reset();
        m_pEngine.reset();
        m_pHeldSink.reset();
    }
    else
    {
        stopNow();
        delete m_pAudioStream;
        delete m_pByteArrayAudio;
        delete m_pAudioOutput;
    }
    m_pAudioStream = nullptr;
    m_pByteArrayAudio = nullptr;
    m_pAudioOutput = nullptr;

    m_audioEngineInitialized = false;
    m_audioEngineRunning = false;
}

//Jump to frame
void AudioPlayback::jumpToPos( int frame )
{
    post( [this, frame]() { jumpToPosNow( frame ); } );
}

void AudioPlayback::jumpToPosNow( int frame )
{
    if( !m_audioEngineInitialized || !m_pAudioStream ) return;   // the legacy engine only

    qint64 position = 4 * (qint64)( frame * m_audioSampleRate / m_mlvFrameRate );
    m_pAudioStream->device()->seek( position );
}

//Play audio
void AudioPlayback::play()
{
    post( [this]() { playNow(); } );
}

void AudioPlayback::playNow()
{
    if( !m_audioEngineInitialized || !m_pAudioStream ) return;   // the legacy engine only

    if( m_audioEngineRunning )
    {
        m_pAudioOutput->resume();
        return;
    }

    m_pAudioOutput->start( m_pAudioStream->device() );
    m_pAudioOutput->resume();
    m_audioEngineRunning = true;
}

//Stop audio
void AudioPlayback::stop()
{
    // r2 row c: a sync posted before this stop (Play, first present, Pause before the audio thread ran it) is dropped
    m_session.invalidate();
    post( [this]()
    {
        // Row A: silence, but keep a held sink open and running. Closing it (MainWindow::resizeEvent stops the audio,
        // and the smoke places its window ~10 ms before Play: r1 leg d1426fe1) only made the next Play re-open it.
        if( m_pEngine )
        {
            stopHeldNow();
            return;
        }
        stopNow();
    } );
}

void AudioPlayback::stopHeldNow()
{
    if( !m_audioEngineInitialized || !m_pEngine ) return;
    m_pEngine->stop();
    m_audioEngineRunning = false;
    armHoldTimer();
}

void AudioPlayback::stopNow()
{
    if( !m_audioEngineInitialized || !m_pAudioOutput ) return;

    if( !m_audioEngineRunning ) return;
    m_pAudioOutput->stop();
    m_pAudioOutput->reset();
    m_audioEngineRunning = false;
}

//Row A: hold the sink open, running and silent -- at every settled paused frame and at Play, so the first present
//only hands the content over. Prepares are coalesced: only the newest one queued runs (a scrub queues many). An open
//sink is kept at any frame; a suspended one is resumed (Play's prepare does that before the first present).
void AudioPlayback::prepare( int startFrame )
{
    Q_UNUSED( startFrame );   // the handover positions the content (PlaybackAudioStartPolicy.h)
    if( !m_pWorkerThread ) return;   // legacy: the start stays synchronous at the first present
    const quint64 ticket = ++m_prepareTicket;
    post( [this, ticket]()
    {
        if( ticket != m_prepareTicket.load() ) return;
        prepareNow();
    } );
}

void AudioPlayback::prepareNow()
{
    if( !m_audioEngineInitialized || !m_pEngine ) return;
    m_pEngine->prepare();
    if( !m_pEngine->isPlaying() ) armHoldTimer();
}

//An active stream can keep the machine from sleeping: a held sink that sees no Play for kAudioHeldRunningMs is
//suspended, and the next prepare or sync resumes it
void AudioPlayback::armHoldTimer()
{
    if( !m_pHoldTimer )
    {
        m_pHoldTimer = new QTimer( this );
        m_pHoldTimer->setSingleShot( true );
        m_pHoldTimer->setInterval( static_cast<int>( playback_audio_start::kAudioHeldRunningMs ) );
        connect( m_pHoldTimer, &QTimer::timeout, this, [this]()
        {
            if( m_pEngine ) m_pEngine->holdExpired();
        } );
    }
    m_pHoldTimer->start();
}

//Row A, at a present: hand the content over so it is audible on the video timeline (resume or re-open first if needed)
void AudioPlayback::syncAndPlay( int frame, double presentWallMs )
{
    const quint64 session = m_session.current();
    post( [this, frame, presentWallMs, session]() { syncAndPlayNow( frame, presentWallMs, session ); } );
}

void AudioPlayback::syncAndPlayNow( int frame, double presentWallMs, quint64 session )
{
    if( !m_audioEngineInitialized || !m_pEngine ) return;
    if( !m_session.isCurrent( session ) ) return;   // stopped since it was posted: no blip

    if( m_pHoldTimer ) m_pHoldTimer->stop();
    const StartReport report = m_pEngine->sync( frame, presentWallMs );
    m_audioEngineRunning = m_pEngine->isPlaying();
    if( report.resumeMs > playback_audio_start::kAudioResumeSlackMs )
        qInfo().noquote() << QStringLiteral( "AudioPlayback: slow resume %1 ms; the handover re-anchored the audio" )
                                 .arg( report.resumeMs, 0, 'f', 3 );
    if( std::strcmp( report.mode, "reopen" ) == 0 && std::strcmp( report.health, "closed" ) != 0 )
        qInfo().noquote() << QStringLiteral( "AudioPlayback: held sink unhealthy (%1); re-opened in %2 ms" )
                                 .arg( QString::fromLatin1( report.health ) )
                                 .arg( report.reopenMs, 0, 'f', 3 );
    storeReport( report );
}

//The legacy synchronous start (stop, jumpToPos, play on the GUI thread) just ran
void AudioPlayback::noteLegacyStart( int syncFrame, int displayFrame )
{
    StartReport report;
    report.valid = true;
    report.mode = playback_audio_start::modeName( playback_audio_start::Mode::Legacy );
    report.syncFrame = displayFrame;
    report.seekFrame = syncFrame;
    // the GUI thread was blocked through the open and the pace clock starts after it: the audio leads the video by
    // the frames the sync position is ahead of the presented one
    if( m_mlvFrameRate > 0.0 )
        report.avOffsetEstMs = ( syncFrame - displayFrame ) * 1000.0 / m_mlvFrameRate;
    storeReport( report );
}

void AudioPlayback::storeReport( const StartReport &report )
{
    QMutexLocker lock( &m_reportMutex );
    if( !m_report.valid ) m_report = report;   // the Play's first start; later re-syncs (seek, loop) keep it
}

void AudioPlayback::clearStartReport()
{
    QMutexLocker lock( &m_reportMutex );
    m_report = StartReport();
}

AudioPlayback::StartReport AudioPlayback::startReport() const
{
    QMutexLocker lock( &m_reportMutex );
    return m_report;
}
