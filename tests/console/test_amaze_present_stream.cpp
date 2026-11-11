// HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r3 (the stream-0 clause): the GUI present path of the CUDA AMaZE
// backend runs its GL handoff on its own cudaStreamNonBlocking stream, ordered after the frame's AMaZE tile streams by
// an event, behind the kill switch MLVAPP_CUDA_AMAZE_PRESENT_STREAM (default ON). No GPU: the switch's parse, source
// pins on the stream / event ownership seam in tools/gpu/backend/igpu_amaze_debayer_cuda.cu, and (r4, 795-R3-I1) a
// host-side stub that injects a failed sync into the best-effort tile drain helper the .cu calls.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../tools/gpu/backend/igpu_amaze_present_stream.h"
#include "../../tools/gpu/backend/igpu_cuda_error_slot.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTextStream>

#include <vector>

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString amazeSource()
{
    return readRepoFile(QStringLiteral("tools/gpu/backend/igpu_amaze_debayer_cuda.cu"));
}

// The text of the one function whose definition starts with `signature`, up to its closing "\n}".
QString functionBody(const QString & source, const QString & signature)
{
    const int start = source.indexOf(signature);
    ASSERT_TRUE(start >= 0);
    ASSERT_EQ(-1, source.indexOf(signature, start + 1));
    const int end = source.indexOf(QStringLiteral("\n}"), start);
    ASSERT_TRUE(end > start);
    return source.mid(start, end - start);
}

const char * const kLiveDevice = "extern \"C\" int igpu_amaze_debayer_run_post_wb_gl_texture_from_device_bayer16(";
const char * const kLiveR16 = "extern \"C\" int igpu_amaze_debayer_run_post_wb_gl_texture_from_r16_gl_texture(";
const char * const kCopies[] = {"int copy_gl_r16_texture_to_bayer16(", "int copy_rgba16_to_gl_rgba16_texture(",
                                "int copy_rgb16_to_gl_rgba16_texture_post_wb_undo("};

} // namespace

TEST(AmazePresentStream, KillSwitchDefaultsOnAndParsesOffSpellings)
{
    using igpu_amaze_present::present_stream_enabled;
    ASSERT_EQ(std::string("MLVAPP_CUDA_AMAZE_PRESENT_STREAM"), std::string(IGPU_AMAZE_PRESENT_STREAM_ENV));
    // Unset or empty: ON (the new path is the default).
    ASSERT_TRUE(present_stream_enabled(nullptr));
    ASSERT_TRUE(present_stream_enabled(""));
    // The off spellings: back to stream 0.
    for (const char * off : {"0", "false", "False", "FALSE", "f", "no", "No", "N", "n", "off", "OFF", "Off", "oF"})
        ASSERT_FALSE(present_stream_enabled(off));
    // Everything else stays on, including "on" (o followed by n) and a lone "o".
    for (const char * on : {"1", "true", "True", "yes", "on", "ON", "o", "2", " 0", "x"})
        ASSERT_TRUE(present_stream_enabled(on));
}

TEST(AmazePresentStream, TheSwitchMatchesTheDirectRgbaStoreSpelling)
{
    // Same spelling rules as MLVAPP_CUDA_AMAZE_LIVE_DIRECT_RGBA_STORE (live_direct_rgba_store_from_env), so the two
    // switches never disagree on what "off" means.
    const QString cu = amazeSource();
    const QString direct = functionBody(cu, QStringLiteral("bool live_direct_rgba_store_from_env()"));
    ASSERT_TRUE(direct.contains(QStringLiteral("if (!value || !*value) return true;")));
    ASSERT_TRUE(direct.contains(QStringLiteral("first == '0' || first == 'f' || first == 'F'")));
    ASSERT_TRUE(direct.contains(QStringLiteral("|| first == 'n' || first == 'N')")));
    ASSERT_TRUE(direct.contains(QStringLiteral("(first == 'o' || first == 'O')")));
    ASSERT_TRUE(direct.contains(QStringLiteral("(value[1] == 'f' || value[1] == 'F')")));
}

TEST(AmazePresentStream, TheBackendOwnsOneNonBlockingStreamAndEvent)
{
    const QString cu = amazeSource();
    ASSERT_TRUE(cu.contains(QStringLiteral("#include \"igpu_amaze_present_stream.h\"")));
    const QString make = functionBody(cu, QStringLiteral("cudaStream_t live_present_stream(igpu_amaze_debayer_backend * backend)"));
    ASSERT_TRUE(make.contains(QStringLiteral(
        "if (!igpu_amaze_present::present_stream_enabled(std::getenv(IGPU_AMAZE_PRESENT_STREAM_ENV))) return nullptr;")));
    ASSERT_TRUE(make.contains(QStringLiteral("CK(cudaStreamCreateWithFlags(&backend->presentStream, cudaStreamNonBlocking));")));
    ASSERT_TRUE(make.contains(QStringLiteral("CK(cudaEventCreateWithFlags(&backend->presentProducerDone, cudaEventDisableTiming));")));
    ASSERT_EQ(1, (int)cu.count(QStringLiteral("cudaStreamCreateWithFlags(&backend->presentStream")));
    // Destroyed with the backend.
    const QString freeIt = functionBody(cu, QStringLiteral("void free_present_stream(igpu_amaze_debayer_backend * backend)"));
    ASSERT_TRUE(freeIt.contains(QStringLiteral("cudaStreamDestroy(backend->presentStream);")));
    ASSERT_TRUE(freeIt.contains(QStringLiteral("cudaEventDestroy(backend->presentProducerDone);")));
    const QString destroy = functionBody(cu, QStringLiteral("extern \"C\" void igpu_amaze_debayer_destroy(igpu_amaze_debayer_backend * backend)"));
    ASSERT_TRUE(destroy.contains(QStringLiteral("    free_live_texture_buffers(backend);\n    free_present_stream(backend);\n")));
}

TEST(AmazePresentStream, TheDrainWaitsOnThePresentStreamAloneAndNeverTheDevice)
{
    const QString cu = amazeSource();
    const QString drain = functionBody(cu, QStringLiteral("cudaError_t drain_present_stream(cudaStream_t stream, const char * stage)"));
    ASSERT_TRUE(drain.contains(QStringLiteral("if (!stream) return cudaSuccess;")));
    ASSERT_TRUE(drain.contains(QStringLiteral("return IGPU_LT_SYNC_CALL(stage, cudaStreamSynchronize(stream));")));
    // No device-wide sync anywhere on the GUI present path (entries, copies, helpers).
    QStringList bodies;
    for (const char * sig : {kLiveDevice, kLiveR16}) bodies << functionBody(cu, QString::fromLatin1(sig));
    for (const char * sig : kCopies) bodies << functionBody(cu, QString::fromLatin1(sig));
    bodies << drain << functionBody(cu, QStringLiteral("void drain_present_stream_after_failure(cudaStream_t stream, const char * site)"));
    for (const QString & body : bodies)
    {
        ASSERT_FALSE(body.contains(QStringLiteral("cudaDeviceSynchronize")));
        ASSERT_FALSE(body.contains(QStringLiteral("cudaStreamSynchronize(0)")));
        ASSERT_FALSE(body.contains(QStringLiteral("cudaThreadSynchronize")));
    }
}

TEST(AmazePresentStream, TheTileLaunchIsOrderedByAnEventIntoThePresentStream)
{
    const QString cu = amazeSource();
    const QString batch = functionBody(cu, QStringLiteral("void run_frame_to_device_rgb16_bayer16_post_wb_batched("));
    const int record = batch.indexOf(QStringLiteral("presentOrderRc = cudaEventRecord(presentProducerDone, streams[slot]);"));
    const int wait = batch.indexOf(QStringLiteral(
        "if (presentOrderRc == cudaSuccess) presentOrderRc = cudaStreamWaitEvent(presentStream, presentProducerDone, 0);"));
    const int gate = batch.indexOf(QStringLiteral("if (presentStream && presentProducerDone)"));
    const int drain = batch.indexOf(QStringLiteral("igpu_amaze_present::drain_every_stream("));
    ASSERT_TRUE(gate >= 0);
    ASSERT_TRUE(record > gate);
    ASSERT_TRUE(wait > record);
    ASSERT_TRUE(drain > wait);
    // The tile streams themselves stay non-blocking (they never used stream 0).
    ASSERT_TRUE(cu.contains(QStringLiteral("            &backend->liveTileStreams[static_cast<std::size_t>(index)],\n"
                                           "            cudaStreamNonBlocking));")));
}

TEST(AmazePresentStream, AnOrderingFailureStillDrainsEveryTileStreamBeforeItThrows)
{
    // Adversary A1 on fe115de4: CK throws, so a CK'd record/wait that failed before the tile drain returned with the
    // tiles still writing the live buffers. The record/wait now only capture their code; every tile stream is drained;
    // the code is raised after the drain loop. No CK on either call, and the first failure stops further records.
    const QString cu = amazeSource();
    const QString batch = functionBody(cu, QStringLiteral("void run_frame_to_device_rgb16_bayer16_post_wb_batched("));
    ASSERT_FALSE(batch.contains(QStringLiteral("CK(cudaEventRecord(presentProducerDone")));
    ASSERT_FALSE(batch.contains(QStringLiteral("CK(cudaStreamWaitEvent(presentStream")));
    const int declared = batch.indexOf(QStringLiteral("cudaError_t presentOrderRc = cudaSuccess;"));
    const int loop = batch.indexOf(QStringLiteral("for (std::size_t slot = 0; slot < active && presentOrderRc == cudaSuccess; ++slot)"));
    const int drain = batch.indexOf(QStringLiteral("igpu_amaze_present::drain_every_stream("));
    const int raised = batch.indexOf(QStringLiteral("raise_noted_cuda(tileDrainRc, \"tileDrainRc\", __FILE__, __LINE__);"));
    ASSERT_TRUE(declared >= 0);
    ASSERT_TRUE(loop > declared);
    ASSERT_TRUE(drain > loop);
    ASSERT_TRUE(raised > drain);
    ASSERT_EQ(1, (int)batch.count(QStringLiteral("raise_noted_cuda(")));
    // Between the ordering block and the raise nothing can throw: no CK at all.
    ASSERT_FALSE(batch.mid(loop, raised - loop).contains(QStringLiteral("CK(")));
}

TEST(AmazePresentStream, TheTileDrainIsBestEffortAndTheOrderingCodeIsRaisedFirst)
{
    // 795-R3-I1 (r4): the drain hands EVERY active tile stream to the shared best-effort helper, seeded with the
    // ordering code, through the timed sync wrapper, and raises the one result after the call. No throwing CK is
    // left on any tile-stream sync after the launch loop (the slot-reuse sync inside the launch loop is pre-r3).
    // r4 fix 2 (adversary A1 on f0058f46): each evaluated failure is noted once, in call order, at its own site (the
    // ordering code before the drain, each failed sync as it returns), and the raise does not note again.
    const QString cu = amazeSource();
    const QString batch = functionBody(cu, QStringLiteral("void run_frame_to_device_rgb16_bayer16_post_wb_batched("));
    ASSERT_TRUE(batch.contains(QStringLiteral(
        "            if (presentOrderRc == cudaSuccess) presentOrderRc = cudaStreamWaitEvent(presentStream, presentProducerDone, 0);\n"
        "        }\n"
        "        if (presentOrderRc != cudaSuccess)\n"
        "        {\n"
        "            IGPU_CUDA_ERROR_NOTE(presentOrderRc, \"tiles:present_order\");\n"
        "        }\n"
        "    }\n"
        "    const cudaError_t tileDrainRc = igpu_amaze_present::drain_every_stream(\n"
        "        streams,\n"
        "        active,\n"
        "        presentOrderRc,\n"
        "        cudaSuccess,\n"
        "        [&](cudaStream_t tile) -> cudaError_t {\n"
        "            return IGPU_LT_SYNC_CALL(\"tiles:drain_sync\", cudaStreamSynchronize(tile));\n"
        "        },\n"
        "        [](cudaError_t rc, bool first) {\n"
        "            if (rc != cudaSuccess && first)\n"
        "            {\n"
        "                IGPU_CUDA_ERROR_NOTE(rc, \"tiles:drain_sync\");\n"
        "            }\n"
        "            if (rc != cudaSuccess && !first)\n"
        "            {\n"
        "                IGPU_CUDA_ERROR_NOTE_IF_ROOM(rc, \"tiles:drain_sync\");\n"
        "            }\n"
        "        });\n"
        "    raise_noted_cuda(tileDrainRc, \"tileDrainRc\", __FILE__, __LINE__);")));
    const int afterLaunch = batch.indexOf(QStringLiteral("const std::size_t active = std::min(inFlight, batchSize);"));
    ASSERT_TRUE(afterLaunch >= 0);
    ASSERT_FALSE(batch.mid(afterLaunch).contains(QStringLiteral("CK(")));
    // The batch body has no device-wide sync either.
    ASSERT_FALSE(batch.contains(QStringLiteral("cudaDeviceSynchronize")));
    ASSERT_FALSE(batch.contains(QStringLiteral("cudaStreamSynchronize(0)")));
}

TEST(AmazePresentStream, TheNotedRaiseThrowsCheckCudasErrorWithoutASecondNote)
{
    // r4 fix 2: raise_noted_cuda is check_cuda's library throw (same type, same message shape) minus its note.
    const QString cu = amazeSource();
    const QString raise = functionBody(cu, QStringLiteral("void raise_noted_cuda(cudaError_t rc, const char * call, const char * file, int line)"));
    ASSERT_TRUE(raise.contains(QStringLiteral("    if (rc == cudaSuccess) return;\n    throw CudaStageProbeError(\n"
                                              "        std::string(\"CUDA error \") + call + \" @ \" + file + \":\" +\n"
                                              "        std::to_string(line) + \": \" + cudaGetErrorString(rc));")));
    ASSERT_FALSE(raise.contains(QStringLiteral("IGPU_CUDA_ERROR_NOTE")));
    const QString probe = readRepoFile(QStringLiteral("tools/gpu/cuda_amaze_debayer_stage_probe.cu"));
    ASSERT_TRUE(probe.contains(QStringLiteral("    IGPU_CUDA_ERROR_NOTE(rc, call);\n    throw CudaStageProbeError(\n"
                                              "        std::string(\"CUDA error \") + call + \" @ \" + file + \":\" +\n"
                                              "        std::to_string(line) + \": \" + cudaGetErrorString(rc));")));
}

namespace
{

// Host-side stub for the drain helper: a fake stream is its slot index, a fake error code is an int (0 = success).
// failAt maps a slot to the code its sync returns; every sync and every note is recorded in call order, in one
// shared event log (a sync is +slot+1000, a note is the code itself).
struct StubSync
{
    std::vector<int> failAt;     // per slot: 0 = the sync succeeds, otherwise the code it returns
    std::vector<int> synced;     // the slots synchronized, in call order
    std::vector<int> noted;      // the codes handed to note(), in call order
    std::vector<int> events;     // syncs and notes interleaved
    std::vector<bool> firsts;    // per note: the helper's `first` flag
    int operator()(int stream)
    {
        synced.push_back(stream);
        events.push_back(1000 + stream);
        return failAt[static_cast<std::size_t>(stream)];
    }
    void note(int rc, bool first)
    {
        noted.push_back(rc);
        events.push_back(rc);
        firsts.push_back(first);
    }
};

int drainWith(StubSync & stub, const std::vector<int> & streams, std::size_t active, int seed)
{
    return igpu_amaze_present::drain_every_stream(streams, active, seed, 0, [&](int s) { return stub(s); },
                                                  [&](int rc, bool first) { stub.note(rc, first); });
}

std::vector<int> tileStreams(int n)
{
    std::vector<int> out;
    for (int i = 0; i < n; ++i) out.push_back(i);
    return out;
}

} // namespace

TEST(AmazePresentStream, StubDrainSyncsEveryStreamAfterAFailedSyncAndReturnsTheFirstError)
{
    const std::vector<int> streams = tileStreams(6);
    // Stream k fails (for each k), with a second, later failure where there is room: every stream is still
    // synchronized exactly once, in order, and the FIRST failure is the code returned.
    for (int k = 0; k < 6; ++k)
    {
        StubSync stub{std::vector<int>(6, 0), {}, {}, {}};
        stub.failAt[static_cast<std::size_t>(k)] = 700 + k;
        if (k + 2 < 6) stub.failAt[static_cast<std::size_t>(k + 2)] = 900;
        ASSERT_EQ(700 + k, drainWith(stub, streams, streams.size(), 0));
        ASSERT_TRUE(stub.synced == streams);
    }
    // Every stream fails: still six syncs, the first code wins.
    {
        StubSync stub{{11, 12, 13, 14, 15, 16}, {}, {}, {}};
        ASSERT_EQ(11, drainWith(stub, streams, streams.size(), 0));
        ASSERT_TRUE(stub.synced == streams);
    }
}

TEST(AmazePresentStream, StubDrainKeepsAnEarlierOrderingErrorAndStillDrainsEveryStream)
{
    const std::vector<int> streams = tileStreams(4);
    // The ordering code is already set: it is the one returned, even when a drain fails too, and no stream is
    // skipped.
    StubSync stub{{0, 55, 0, 66}, {}, {}, {}};
    ASSERT_EQ(400, drainWith(stub, streams, streams.size(), 400));
    ASSERT_TRUE(stub.synced == streams);
    // All clean: success, every stream synchronized once, nothing noted.
    StubSync clean{{0, 0, 0, 0}, {}, {}, {}};
    ASSERT_EQ(0, drainWith(clean, streams, streams.size(), 0));
    ASSERT_TRUE(clean.synced == streams);
    ASSERT_TRUE(clean.noted.empty());
    // Only the active prefix is drained (fewer tiles than streams this frame), and zero active syncs nothing.
    StubSync prefix{{0, 0, 9, 0}, {}, {}, {}};
    ASSERT_EQ(0, drainWith(prefix, streams, 2, 0));
    ASSERT_TRUE(prefix.synced == tileStreams(2));
    ASSERT_TRUE(prefix.noted.empty());
    StubSync none{{5, 5, 5, 5}, {}, {}, {}};
    ASSERT_EQ(0, drainWith(none, streams, 0, 0));
    ASSERT_TRUE(none.synced.empty());
    ASSERT_TRUE(none.noted.empty());
}

TEST(AmazePresentStream, StubDrainNotesEveryFailedSyncOnceInCallOrder)
{
    // r4 fix 2 (adversary A1 on f0058f46, its input: two active streams, both final syncs fail with 700): both
    // failures are noted, each right after its own sync, the returned one first, so field telemetry gets a primary
    // and a secondary record instead of one.
    const std::vector<int> two = tileStreams(2);
    StubSync both{{700, 700}, {}, {}, {}};
    ASSERT_EQ(700, drainWith(both, two, two.size(), 0));
    ASSERT_TRUE(both.noted == (std::vector<int>{700, 700}));
    ASSERT_TRUE(both.events == (std::vector<int>{1000, 700, 1001, 700}));
    ASSERT_TRUE(both.firsts == (std::vector<bool>{true, false}));
    // Mixed: the notes follow the syncs in call order and only failures are noted.
    const std::vector<int> five = tileStreams(5);
    StubSync mixed{{0, 21, 0, 23, 24}, {}, {}, {}};
    ASSERT_EQ(21, drainWith(mixed, five, five.size(), 0));
    ASSERT_TRUE(mixed.events == (std::vector<int>{1000, 1001, 21, 1002, 1003, 23, 1004, 24}));
    ASSERT_TRUE(mixed.firsts == (std::vector<bool>{true, false, false}));
    // A failed seed (the ordering code, noted by the caller before the call) is returned but not noted again; the
    // drain failures after it are still each noted once, none of them first.
    StubSync seeded{{0, 31, 32}, {}, {}, {}};
    ASSERT_EQ(400, drainWith(seeded, tileStreams(3), 3, 400));
    ASSERT_TRUE(seeded.noted == (std::vector<int>{31, 32}));
    ASSERT_TRUE(seeded.firsts == (std::vector<bool>{false, false}));
}

namespace
{

// The .cu's note callback, on the real host-only slot (igpu_cuda_error_slot.h) with a fake name: the first failure is
// an ordinary note, every later one a note_if_room.
void slotNote(int rc, bool first)
{
    if (first) igpu_cuda_error_slot::note(rc, "stub", "tiles:drain_sync");
    else igpu_cuda_error_slot::note_if_room(rc, "stub", "tiles:drain_sync");
}

int drainSlot(std::vector<igpu_cuda_error_t> & out, unsigned long long & dropped)
{
    out.assign(IGPU_CUDA_ERROR_PENDING_MAX, igpu_cuda_error_t());
    return igpu_cuda_error_slot::drain(IGPU_CUDA_ERROR_CHANNEL_RENDER, out.data(), (int)out.size(), &dropped);
}

} // namespace

TEST(AmazePresentStream, SlotKeepsTheFirstDrainFailureWhenLaterOnesOverflowTheRing)
{
    // r4 fix 3 (adversary A1 on fa4699ca, its input: nine active tile streams, all nine final syncs return 700). The
    // ring holds IGPU_CUDA_ERROR_PENDING_MAX (8) notes. The first failure must stay the oldest note (telemetry's
    // primary) and the overflow must be counted, not evict it.
    std::vector<igpu_cuda_error_t> out;
    unsigned long long dropped = 0;
    drainSlot(out, dropped); // start empty
    const int n = IGPU_CUDA_ERROR_PENDING_MAX + 1;
    std::vector<int> fails(static_cast<std::size_t>(n), 0);
    for (int i = 0; i < n; ++i) fails[static_cast<std::size_t>(i)] = 700 + i; // distinct, so order is visible
    const std::vector<int> streams = tileStreams(n);
    const int rc = igpu_amaze_present::drain_every_stream(
        streams, streams.size(), 0, 0, [&](int s) { return fails[static_cast<std::size_t>(s)]; }, slotNote);
    ASSERT_EQ(700, rc);
    ASSERT_EQ(IGPU_CUDA_ERROR_PENDING_MAX, drainSlot(out, dropped));
    ASSERT_EQ(700, out[0].code);   // the first failure is still the oldest: primary
    for (int i = 1; i < IGPU_CUDA_ERROR_PENDING_MAX; ++i) ASSERT_EQ(700 + i, out[static_cast<std::size_t>(i)].code);
    ASSERT_EQ(1ULL, dropped);      // the ninth was counted, not queued

    // A failed ordering code noted first (ordinary note), then nine failed syncs: the ordering code stays primary.
    igpu_cuda_error_slot::note(400, "stub", "tiles:present_order");
    const int seeded = igpu_amaze_present::drain_every_stream(
        streams, streams.size(), 400, 0, [&](int s) { return fails[static_cast<std::size_t>(s)]; }, slotNote);
    ASSERT_EQ(400, seeded);
    ASSERT_EQ(IGPU_CUDA_ERROR_PENDING_MAX, drainSlot(out, dropped));
    ASSERT_EQ(400, out[0].code);
    ASSERT_EQ(700, out[1].code);
    ASSERT_EQ(2ULL, dropped);      // 1 + 9 notes for 8 places
    ASSERT_EQ(0, drainSlot(out, dropped));
}

TEST(AmazePresentStream, SlotNoteIfRoomNeverEvictsButNoteStillDoes)
{
    // note() keeps its pre-r4 behaviour (a full ring drops its oldest); note_if_room() never evicts.
    std::vector<igpu_cuda_error_t> out;
    unsigned long long dropped = 0;
    drainSlot(out, dropped);
    for (int i = 0; i < IGPU_CUDA_ERROR_PENDING_MAX; ++i) igpu_cuda_error_slot::note(100 + i, "stub", "tiles:x");
    ASSERT_FALSE(igpu_cuda_error_slot::note_if_room(999, "stub", "tiles:x"));
    igpu_cuda_error_slot::note(200, "stub", "tiles:x");
    ASSERT_EQ(IGPU_CUDA_ERROR_PENDING_MAX, drainSlot(out, dropped));
    ASSERT_EQ(101, out[0].code);   // note() evicted 100
    ASSERT_EQ(200, out[static_cast<std::size_t>(IGPU_CUDA_ERROR_PENDING_MAX - 1)].code);
    ASSERT_EQ(2ULL, dropped);      // the skipped 999 and the evicted 100
    ASSERT_TRUE(igpu_cuda_error_slot::note_if_room(5, "stub", "tiles:x"));
    ASSERT_FALSE(igpu_cuda_error_slot::note_if_room(0, "stub", "tiles:x"));
    ASSERT_EQ(1, drainSlot(out, dropped));
    ASSERT_EQ(5, out[0].code);
    ASSERT_EQ(0ULL, dropped);
}

TEST(AmazePresentStream, BothLiveEntriesRunTheHandoffOnThePresentStream)
{
    const QString cu = amazeSource();
    for (const char * sig : {kLiveDevice, kLiveR16})
    {
        const QString body = functionBody(cu, QString::fromLatin1(sig));
        ASSERT_TRUE(body.contains(QStringLiteral("    cudaStream_t present = nullptr;\n    try\n")));
        ASSERT_TRUE(body.contains(QStringLiteral("        present = live_present_stream(backend);\n")));
        ASSERT_TRUE(body.contains(QStringLiteral("            probe,\n            present,\n"
                                                 "            present ? backend->presentProducerDone : nullptr);")));
        // H-Nyquist, the direct copy and the post-WB copy all take the present stream.
        ASSERT_TRUE(body.contains(QStringLiteral("                                       height,\n"
                                                 "                                       present);")));
        ASSERT_TRUE(body.contains(QStringLiteral("                    &backend->liveOutputRgba16Resource,\n"
                                                 "                    present)\n")));
        ASSERT_TRUE(body.contains(QStringLiteral("                    &backend->liveOutputRgba16Resource,\n"
                                                 "                    present);")));
        // A failed or thrown run drains what it queued.
        ASSERT_EQ(2, (int)body.count(QStringLiteral("drain_present_stream_after_failure(present, ")));
    }
    // The R16 upload also runs on it.
    ASSERT_TRUE(functionBody(cu, QString::fromLatin1(kLiveR16)).contains(
        QStringLiteral("                                                          &backend->liveInputR16Resource,\n"
                       "                                                          present);")));
}

TEST(AmazePresentStream, EachCopyKeepsThePreR3CallsOnStreamZero)
{
    // Kill switch off == today: each copy defaults to stream 0, and its stream-0 branch is the pre-r3 synchronous
    // copy; map, unmap and the span take the same `stream` (0 when off); the drain is a no-op on 0.
    const QString cu = amazeSource();
    const struct
    {
        const char * sig;
        const char * prefix;
        const char * syncCopy;
        const char * asyncCopy;
    } copies[] = {
        {kCopies[0], "gl_r16_in", "cudaMemcpy2DFromArray(dBayer16,", "cudaMemcpy2DFromArrayAsync(dBayer16,"},
        {kCopies[1], "gl_rgba16_direct", "cudaMemcpy2DToArray(mappedArray,", "cudaMemcpy2DToArrayAsync(mappedArray,"},
        {kCopies[2], "gl_rgba16_post_wb", "cudaMemcpy2DToArray(mappedArray,", "cudaMemcpy2DToArrayAsync(mappedArray,"},
    };
    for (const auto & c : copies)
    {
        const QString body = functionBody(cu, QString::fromLatin1(c.sig));
        const QString p = QString::fromLatin1(c.prefix);
        ASSERT_TRUE(body.contains(QStringLiteral("cudaStream_t stream = 0)")));
        ASSERT_TRUE(body.contains(QStringLiteral("IGPU_LT_SYNC_CALL(\"%1:map\", cudaGraphicsMapResources(1, &resource, stream));").arg(p)));
        ASSERT_TRUE(body.contains(QStringLiteral("IGPU_LT_SYNC_CALL(\"%1:unmap\", cudaGraphicsUnmapResources(1, &resource, stream));").arg(p)));
        ASSERT_FALSE(body.contains(QStringLiteral("cudaGraphicsMapResources(1, &resource, 0)")));
        ASSERT_FALSE(body.contains(QStringLiteral("cudaGraphicsUnmapResources(1, &resource, 0)")));
        // The ternary: stream ? async on `stream` : the pre-r3 synchronous copy.
        const int ternary = body.indexOf(QStringLiteral("rc = stream\n"));
        const int async = body.indexOf(QString::fromLatin1(c.asyncCopy));
        const int sync = body.indexOf(QString::fromLatin1(c.syncCopy));
        ASSERT_TRUE(ternary >= 0);
        ASSERT_TRUE(async > ternary);
        ASSERT_TRUE(sync > async);
        ASSERT_TRUE(body.mid(async, sync - async).contains(QStringLiteral("cudaMemcpyDeviceToDevice,\n")));
        ASSERT_TRUE(body.mid(async, sync - async).contains(QStringLiteral("stream))\n")));
        // The drain sits after the unmap, before any unregister / cache reset, and is evaluated with the rest.
        const int unmap = body.indexOf(QStringLiteral("cudaGraphicsUnmapResources(1, &resource, stream)"));
        // (the timing stage is :present_sync; :present_drain is the note site alone, so the census sees one site)
        const int drain = body.indexOf(QStringLiteral("const cudaError_t drainRc = drain_present_stream(stream, \"%1:present_sync\");").arg(p));
        const int unregister = body.lastIndexOf(QStringLiteral("const cudaError_t unregisterRc ="));
        ASSERT_TRUE(drain > unmap);
        ASSERT_TRUE(unregister > drain);
        ASSERT_TRUE(body.contains(QStringLiteral("{(int)drainRc, \"%1:present_drain\"},").arg(p)));
        ASSERT_TRUE(body.contains(QStringLiteral("|| drainRc != cudaSuccess")));
    }
    // The pack and the H-Nyquist filter launch on the given stream (0 when off).
    ASSERT_TRUE(cu.contains(QStringLiteral("k_pack_rgb16_to_rgba16_post_wb_undo<<<blocks, threads, 0, stream>>>(dRgb16,")));
    ASSERT_TRUE(cu.contains(QStringLiteral(
        "void launch_reduced_hnyquist121(const uint16_t * in, uint16_t * out, int w, int h, cudaStream_t stream = 0)")));
    ASSERT_TRUE(cu.contains(QStringLiteral("k_reduced_hnyquist121<<<grid, block, 0, stream>>>(in, out, w, h)")));
}

TEST(AmazePresentStream, TheOfflineAndNonLiveEntriesStayOnStreamZero)
{
    // Only the two live GL entries (the GUI present path) take the present stream; the offline entries and the
    // uncached RGB16 copy are untouched.
    const QString cu = amazeSource();
    for (const char * sig : {"extern \"C\" int igpu_amaze_debayer_run_post_wb_gl_texture(",
                             "extern \"C\" int igpu_amaze_debayer_run_gl_texture(", "int copy_rgb16_to_gl_rgba16_texture("})
    {
        const QString body = functionBody(cu, QString::fromLatin1(sig));
        ASSERT_FALSE(body.contains(QStringLiteral("live_present_stream")));
        ASSERT_FALSE(body.contains(QStringLiteral("present)")));
    }
    ASSERT_EQ(2, (int)cu.count(QStringLiteral("present = live_present_stream(backend);")));
}
