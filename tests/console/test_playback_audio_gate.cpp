// PLAYBACK-STARTUP-WAIT-PREWARM-1 r2: the device the audio sink pulls from (PlaybackAudioGate.h) and the bounded
// hand-back and join of the audio thread (PlaybackWorkerJoin.h). Qt 6.10 pulls a sink's device only for what
// bytesAvailable() reports, so a closed gate keeps the sink running on silence with nothing of the clip consumed.
#include "../common/minitest.h"

#include "../../platform/qt/PlaybackAudioGate.h"
#include "../../platform/qt/PlaybackWorkerJoin.h"

#include <QByteArray>
#include <QElapsedTimer>
#include <QObject>
#include <QThread>

#include <atomic>

namespace
{

// 48 kHz stereo 16-bit: 4 bytes a frame, 192 bytes a millisecond; every byte holds its own index (mod 256)
QByteArray content(int ms)
{
    QByteArray bytes(ms * 192, '\0');
    for (int i = 0; i < bytes.size(); ++i) bytes[i] = static_cast<char>(i & 0xff);
    return bytes;
}

} // namespace

TEST(PlaybackAudioGate, AClosedGateServesOnlyItsPrimeOfZeros)
{
    PlaybackAudioGate gate(content(1000), 4, 48000);
    ASSERT_TRUE(gate.isSequential());
    ASSERT_EQ(0, static_cast<int>(gate.bytesAvailable())); // never opened: nothing for the sink to pull
    gate.closeGate(gate.bytesForMs(100.0));
    ASSERT_EQ(19200, static_cast<int>(gate.bytesAvailable()));
    char buffer[32768];
    ASSERT_EQ(19200, static_cast<int>(gate.read(buffer, sizeof(buffer))));
    for (int i = 0; i < 19200; ++i) ASSERT_EQ(0, static_cast<int>(buffer[i]));
    ASSERT_EQ(0, static_cast<int>(gate.bytesAvailable()));
    ASSERT_EQ(0, static_cast<int>(gate.read(buffer, sizeof(buffer))));
    ASSERT_EQ(19200, static_cast<int>(gate.servedBytes()));
    ASSERT_NEAR(100.0, gate.msForBytes(gate.servedBytes()), 1e-9);
}

TEST(PlaybackAudioGate, OpeningServesTheContentFromAFrameAndTellsTheSinkAtOnce)
{
    PlaybackAudioGate gate(content(1000), 4, 48000);
    gate.closeGate(gate.bytesForMs(100.0));
    int readyRead = 0;
    QObject::connect(&gate, &QIODevice::readyRead, [&readyRead]() { ++readyRead; });
    gate.openAt(gate.bytesForMs(250.0) + 3); // rounded down to the frame
    ASSERT_EQ(1, readyRead);
    ASSERT_TRUE(gate.gateOpen());
    ASSERT_EQ(48000, static_cast<int>(gate.position()));
    ASSERT_EQ(192000 - 48000, static_cast<int>(gate.bytesAvailable())); // the unpulled prime is dropped
    char buffer[8];
    ASSERT_EQ(8, static_cast<int>(gate.read(buffer, 8)));
    ASSERT_EQ(48000 & 0xff, static_cast<unsigned char>(buffer[0]));
    ASSERT_EQ(48008, static_cast<int>(gate.position()));
}

TEST(PlaybackAudioGate, PositionsAreClampedAndSilenceStopsTheContent)
{
    PlaybackAudioGate gate(content(10), 4, 48000);
    gate.openAt(-400);
    ASSERT_EQ(0, static_cast<int>(gate.position()));
    gate.openAt(gate.bytesForMs(5000.0)); // past the end: silence, never a read past the content
    ASSERT_EQ(1920, static_cast<int>(gate.position()));
    ASSERT_EQ(0, static_cast<int>(gate.bytesAvailable()));
    gate.openAt(0);
    gate.closeGate(0);                    // pause: nothing more of the clip moves
    ASSERT_FALSE(gate.gateOpen());
    ASSERT_EQ(0, static_cast<int>(gate.bytesAvailable()));
    ASSERT_EQ(0, static_cast<int>(gate.bytesForMs(-3.0)));
}

namespace
{

class Worker : public QObject
{
public:
    std::atomic<int> ran { 0 };
};

} // namespace

TEST(PlaybackWorkerJoin, AHealthyWorkerHandsBackAndJoinsInTime)
{
    QThread thread;
    Worker worker;
    worker.moveToThread(&thread);
    thread.start();
    QThread *caller = QThread::currentThread();
    ASSERT_TRUE(playback_worker_join::runBounded(&worker, [&worker, caller]()
    {
        ++worker.ran;
        worker.moveToThread(caller);
    }, playback_worker_join::kHandBackTimeoutMs));
    ASSERT_EQ(1, worker.ran.load());
    ASSERT_TRUE(worker.thread() == caller);
    ASSERT_TRUE(playback_worker_join::joinBounded(&thread, playback_worker_join::kJoinTimeoutMs));
    ASSERT_TRUE(thread.isFinished());
}

TEST(PlaybackWorkerJoin, AHungWorkerTimesOutInsteadOfHangingTheCaller)
{
    QThread thread;
    Worker worker;
    worker.moveToThread(&thread);
    thread.start();
    QElapsedTimer clock;
    clock.start();
    // a WASAPI call that does not return for 1.5 s, against a 200 ms bound
    ASSERT_FALSE(playback_worker_join::runBounded(&worker, [&worker]()
    {
        QThread::msleep(1500);
        ++worker.ran;
    }, 200));
    ASSERT_TRUE(clock.elapsed() < 1000);
    ASSERT_FALSE(playback_worker_join::joinBounded(&thread, 100)); // still inside the hung call
    // the test owns the objects, so it lets the hung call finish before they go
    ASSERT_TRUE(thread.wait(5000));
    ASSERT_EQ(1, worker.ran.load());
}
