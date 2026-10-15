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
#include "../../src/mlv_include.h"

class QThread;

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

    // PLAYBACK-STARTUP-WAIT-PREWARM-1 Row A: the audio device is opened, seeked and started on its own thread, so the
    // GUI thread never blocks on it (the first Play-present waited ~0.3-0.65 s on Bachelor). Without
    // startWorkerThread() (MLVAPP_PLAYBACK_AUDIO_SYNC_LEGACY=1) every call runs synchronously on the caller, as before.
    struct StartReport
    {
        bool valid = false;
        const char *mode = "none";      // prepared | reopen | legacy
        double prepareMs = 0.0;         // the silent open at Play (stop, seek, start, suspend)
        double resumeLagMs = 0.0;       // first present -> audio audible
        double reopenMs = 0.0;          // the open when the prepared sink could not be used
        int syncFrame = -1;             // the presented frame the audio is synced to
        int seekFrame = -1;             // the frame the audio content starts at
        double avOffsetEstMs = 0.0;     // audio content minus video timeline when audible (app-side estimate)
    };
    /*! Moves this (parentless) object to its own thread; every later call is queued there, in order. */
    void startWorkerThread( void );
    /*! GUI thread: resets the engine on the worker, brings the object back, quits and joins the thread. */
    void shutdownWorkerThread( void );
    bool usesWorkerThread( void ) const { return m_pWorkerThread != nullptr; }
    /*! At Play: open the sink silently at \a startFrame and keep it suspended (queued; worker thread only). */
    void prepare( int startFrame );
    /*! At a present: start the audio for \a frame, presented at \a presentWallMs (mlv_stage_timing_now() ms). */
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
    void prepareNow( int startFrame );
    void syncAndPlayNow( int frame, double presentWallMs );
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

    // Row A (worker thread only, except the report)
    QThread *m_pWorkerThread = nullptr;
    bool m_prepared = false;
    int m_preparedFrame = -1;
    double m_preparedMs = 0.0;
    double m_lastOpenMs = 0.0;
    mutable QMutex m_reportMutex;
    StartReport m_report;
};

#endif // AUDIOPLAYBACK_H
