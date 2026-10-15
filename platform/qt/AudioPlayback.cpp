/*!
 * \file AudioPlayback.cpp
 * \author masc4ii
 * \copyright 2017
 * \brief Handles the mlv audio playback
 */

#include "AudioPlayback.h"
#include <QtGlobal>
#if QT_VERSION >= QT_VERSION_CHECK(6, 0, 0)
#include <QAudioFormat>
#endif
#include <QDebug>
#include <QMetaObject>
#include <QMutexLocker>
#include <QThread>
#include <utility>
#ifdef Q_OS_WIN
#include <objbase.h>
#endif
#include "../../src/debug/StageTiming.h"
#include "PlaybackAudioStartPolicy.h"

namespace
{
double audioNowMs()
{
    return mlv_stage_timing_now() * 1000.0;   // the clock the presents are stamped with
}
}

//Constructor
AudioPlayback::AudioPlayback( QObject *parent )
    : QObject( parent )
{
    m_audioEngineInitialized = false;
    m_audioEngineRunning = false;
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

//Reset the engine on the audio thread, bring this object back to the caller's thread, then join
void AudioPlayback::shutdownWorkerThread()
{
    if( !m_pWorkerThread ) return;
    QThread *callerThread = QThread::currentThread();
    QMetaObject::invokeMethod( this, [this, callerThread]()
    {
        if( m_audioEngineInitialized ) resetEngineNow();
        moveToThread( callerThread );
    }, Qt::BlockingQueuedConnection );
    m_pWorkerThread->quit();
    m_pWorkerThread->wait();
    delete m_pWorkerThread;
    m_pWorkerThread = nullptr;
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

//Row A: the same engine as initAudioEngine, from samples already copied (audio thread)
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
    m_pAudioOutput = new QAudioOutput( format, this );
#else
    format.setSampleFormat( QAudioFormat::Int16 );
    m_pAudioOutput = new QAudioSink( format, (QObject*)this );
#endif

    m_pByteArrayAudio = new QByteArray( audio );
    m_pAudioStream = new QDataStream( m_pByteArrayAudio, QIODevice::ReadWrite );
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
    m_prepared = false;
}

//Unload the audio of a mlv
void AudioPlayback::resetAudioEngine()
{
    post( [this]() { resetEngineNow(); } );
}

void AudioPlayback::resetEngineNow()
{
    if( !m_audioEngineInitialized ) return;

    stopNow();
    delete m_pAudioStream;
    delete m_pByteArrayAudio;
    delete m_pAudioOutput;

    m_audioEngineInitialized = false;
    m_audioEngineRunning = false;
    m_prepared = false;
}

//Jump to frame
void AudioPlayback::jumpToPos( int frame )
{
    post( [this, frame]() { jumpToPosNow( frame ); } );
}

void AudioPlayback::jumpToPosNow( int frame )
{
    if( !m_audioEngineInitialized ) return;

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
    if( !m_audioEngineInitialized ) return;

    if( m_prepared )
    {
        // a prepared sink is silent: playing it outright makes it audible
        m_prepared = false;
        m_pAudioOutput->setVolume( 1.0 );
    }

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
    post( [this]()
    {
        // Row A: a prepared sink is already silent and suspended. Closing it (MainWindow::resizeEvent stops the audio,
        // and the smoke places its window ~10 ms before Play: r1 leg d1426fe1) only made the next Play re-open it.
        if( m_prepared ) return;
        stopNow();
    } );
}

void AudioPlayback::stopNow()
{
    if( !m_audioEngineInitialized ) return;

    m_prepared = false;
    if( !m_audioEngineRunning ) return;
    m_pAudioOutput->stop();
    m_pAudioOutput->reset();
    m_audioEngineRunning = false;
}

//Row A: open the sink silently at a frame and hold it suspended -- at every settled paused frame and at Play, so Play
//only resumes it. Prepares are coalesced: only the newest one queued runs (a scrub queues many), and a sink already
//prepared at that frame is kept.
void AudioPlayback::prepare( int startFrame )
{
    if( !m_pWorkerThread ) return;   // legacy: the start stays synchronous at the first present
    const quint64 ticket = ++m_prepareTicket;
    post( [this, startFrame, ticket]()
    {
        if( ticket != m_prepareTicket.load() ) return;
        prepareNow( startFrame );
    } );
}

void AudioPlayback::prepareNow( int startFrame )
{
    if( !m_audioEngineInitialized ) return;
    if( m_prepared && m_audioEngineRunning && m_preparedFrame == startFrame ) return;

    const double startMs = audioNowMs();
    stopNow();
    m_pAudioOutput->setVolume( 0.0 );
    jumpToPosNow( startFrame );
    m_pAudioOutput->start( m_pAudioStream->device() );
    const double openedMs = audioNowMs();
    m_pAudioOutput->suspend();
    m_audioEngineRunning = true;
    m_prepared = true;
    m_preparedFrame = startFrame;
    m_lastOpenMs = openedMs - startMs;
    m_preparedMs = audioNowMs() - startMs;
}

//Row A, at a present: resume the prepared sink when it is on time, otherwise re-open once ahead of the video
void AudioPlayback::syncAndPlay( int frame, double presentWallMs )
{
    post( [this, frame, presentWallMs]() { syncAndPlayNow( frame, presentWallMs ); } );
}

void AudioPlayback::syncAndPlayNow( int frame, double presentWallMs )
{
    if( !m_audioEngineInitialized ) return;

    using namespace playback_audio_start;
    const double fps = m_mlvFrameRate;
    const double lagMs = audioNowMs() - presentWallMs;
    const Mode mode = chooseMode( false, m_prepared && m_audioEngineRunning, m_preparedFrame, frame, lagMs );

    StartReport report;
    report.valid = true;
    report.mode = modeName( mode );
    report.prepareMs = m_preparedMs;
    report.syncFrame = frame;
    if( mode == Mode::Prepared )
    {
        // what the sink played (silently) before it was suspended is content the audio is already past
        const double playedSilentMs = m_pAudioOutput->processedUSecs() / 1000.0;
        m_pAudioOutput->setVolume( 1.0 );
        m_pAudioOutput->resume();
        const double audibleMs = audioNowMs();
        report.resumeLagMs = audibleMs - presentWallMs;
        report.seekFrame = m_preparedFrame;
        report.avOffsetEstMs = avOffsetEstMs( m_preparedFrame, playedSilentMs, frame, audibleMs - presentWallMs, fps );
    }
    else
    {
        const double startMs = audioNowMs();
        const int seekFrame = reopenSeekFrame( frame, lagMs, m_lastOpenMs, fps );
        stopNow();
        jumpToPosNow( seekFrame );
        m_pAudioOutput->setVolume( 1.0 );
        m_pAudioOutput->start( m_pAudioStream->device() );
        m_audioEngineRunning = true;
        const double audibleMs = audioNowMs();
        m_lastOpenMs = audibleMs - startMs;
        report.reopenMs = audibleMs - startMs;
        report.resumeLagMs = audibleMs - presentWallMs;
        report.seekFrame = seekFrame;
        report.avOffsetEstMs = avOffsetEstMs( seekFrame, m_pAudioOutput->processedUSecs() / 1000.0,
                                              frame, audibleMs - presentWallMs, fps );
    }
    m_prepared = false;
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
