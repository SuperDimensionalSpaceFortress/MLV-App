/*!
 * \file AudioPlayback.h
 * \author masc4ii
 * \copyright 2017
 * \brief Handles the mlv audio playback
 */

#ifndef AUDIOPLAYBACK_H
#define AUDIOPLAYBACK_H

#include <QtGlobal>
#if QT_VERSION < QT_VERSION_CHECK(6, 0, 0)
#include <QAudioOutput>
#else
#include <QAudioSink>
#endif
#include <QByteArray>
#include <QDataStream>
#include <QMutex>
#include <Qt>
#include <atomic>
#include <memory>
#include "../../src/mlv_include.h"
#include "PlaybackAudioStartPolicy.h"

class QThread;
class QTimer;

class AudioPlayback : public QObject
{
    Q_OBJECT
public:
    explicit AudioPlayback( QObject *parent = Q_NULLPTR );
    ~AudioPlayback();
    void initAudioEngine( mlvObject_t *pMlvObject );
    void resetAudioEngine( void );
    void jumpToPos( int frame );
    void play( void );
    void stop( void );

    // PLAYBACK-STARTUP-WAIT-PREWARM-1 Row A: the audio device is opened, held and handed its content on its own thread,
    // so the GUI thread never blocks on it (the first Play-present waited ~0.3-0.65 s on Bachelor). The policy is
    // PlaybackAudioStartPolicy.h. Without startWorkerThread() (MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY=1) every call runs
    // synchronously on the caller, as before.
    using StartReport = playback_audio_start::StartReport;
    /*! Moves this (parentless) object to its own thread; every later call is queued there, in order. */
    void startWorkerThread( void );
    /*! GUI thread: resets the engine on the worker, brings the object back, quits and joins the thread, each wait
     *  bounded (PlaybackWorkerJoin.h). False when a wait timed out: the worker may still use this object, so the
     *  caller must not delete it. */
    bool shutdownWorkerThread( void );
    bool usesWorkerThread( void ) const { return m_pWorkerThread != nullptr; }
    /*! At a settled paused frame and at Play: hold the sink open, running and silent (queued; coalesced to the
     *  newest). An open sink is kept whatever \a startFrame is: the handover positions the content. No-op without
     *  the worker thread. */
    void prepare( int startFrame );
    /*! At a present: hand the audio over for \a frame, presented at \a presentWallMs (mlv_stage_timing_now() ms).
     *  Dropped on the audio thread when stop() was called after it was posted. */
    void syncAndPlay( int frame, double presentWallMs );
    /*! The legacy synchronous start just ran (GUI thread): record it for the report. */
    void noteLegacyStart( int syncFrame, int displayFrame );
    void clearStartReport( void );
    StartReport startReport( void ) const;

private:
    void initEngineFromBytes( const QByteArray &audio, uint32_t sampleRate, uint16_t channels, double frameRate );
    void resetEngineNow( void );
    void jumpToPosNow( int frame );
    void playNow( void );
    void stopNow( void );
    void prepareNow( void );
    void syncAndPlayNow( int frame, double presentWallMs, quint64 session );
    void stopHeldNow( void );
    void armHoldTimer( void );
    void storeReport( const StartReport &report );
    template <typename F> void post( F &&work );

    uint8_t *m_pMlvAudioData;
    uint64_t m_mlvAudioSize;
    uint32_t m_audioSampleRate;
    uint16_t m_audioChannels;

    bool m_audioEngineInitialized;
    bool m_audioEngineRunning;

    QByteArray *m_pByteArrayAudio;
    QDataStream *m_pAudioStream;
#if QT_VERSION < QT_VERSION_CHECK(6, 0, 0)
    QAudioOutput *m_pAudioOutput;
#else
    QAudioSink *m_pAudioOutput;
#endif

    double m_mlvFrameRate = 0.0;

    // Row A (worker thread only, except the report and the session stamp)
    class HeldSink;
    QThread *m_pWorkerThread = nullptr;
    std::unique_ptr<HeldSink> m_pHeldSink;              // QAudioSink + the gate it pulls from, as the engine's SinkPort
    std::unique_ptr<playback_audio_start::StartEngine> m_pEngine;
    QTimer *m_pHoldTimer = nullptr;                     // suspends a held sink after kAudioHeldRunningMs
    std::atomic<quint64> m_prepareTicket { 0 };         // GUI thread bumps it per prepare(); only the newest runs
    playback_audio_start::SessionStamp m_session;      // stop() invalidates syncs posted before it
    mutable QMutex m_reportMutex;
    StartReport m_report;
};

#endif // AUDIOPLAYBACK_H
