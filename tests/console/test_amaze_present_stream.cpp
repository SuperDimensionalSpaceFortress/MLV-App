// HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r3 (the stream-0 clause): the GUI present path of the CUDA AMaZE
// backend runs its GL handoff on its own cudaStreamNonBlocking stream, ordered after the frame's AMaZE tile streams by
// an event, behind the kill switch MLVAPP_CUDA_AMAZE_PRESENT_STREAM (default ON). No GPU: the switch's parse, source
// pins on the stream / event ownership seam in tools/gpu/backend/igpu_amaze_debayer_cuda.cu, and (r4, 795-R3-I1) a
// host-side stub that injects a failed sync into the best-effort tile drain helper the .cu calls.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../tools/gpu/backend/igpu_amaze_present_stream.h"

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
    const int raised = batch.indexOf(QStringLiteral("CK(tileDrainRc);"));
    ASSERT_TRUE(declared >= 0);
    ASSERT_TRUE(loop > declared);
    ASSERT_TRUE(drain > loop);
    ASSERT_TRUE(raised > drain);
    ASSERT_EQ(1, (int)batch.count(QStringLiteral("CK(tileDrainRc);")));
    // Between the ordering block and the raise nothing can throw: no CK at all.
    ASSERT_FALSE(batch.mid(loop, raised - loop).contains(QStringLiteral("CK(")));
}

TEST(AmazePresentStream, TheTileDrainIsBestEffortAndTheOrderingCodeIsRaisedFirst)
{
    // 795-R3-I1 (r4): the drain hands EVERY active tile stream to the shared best-effort helper, seeded with the
    // ordering code, through the timed sync wrapper; CK raises the one result after the call. No throwing CK is
    // left on any tile-stream sync after the launch loop (the slot-reuse sync inside the launch loop is pre-r3).
    const QString cu = amazeSource();
    const QString batch = functionBody(cu, QStringLiteral("void run_frame_to_device_rgb16_bayer16_post_wb_batched("));
    ASSERT_TRUE(batch.contains(QStringLiteral(
        "    const cudaError_t tileDrainRc = igpu_amaze_present::drain_every_stream(\n"
        "        streams, active, presentOrderRc, cudaSuccess, [&](cudaStream_t tile) -> cudaError_t {\n"
        "            return IGPU_LT_SYNC_CALL(\"tiles:drain_sync\", cudaStreamSynchronize(tile));\n"
        "        });\n"
        "    CK(tileDrainRc);")));
    const int afterLaunch = batch.indexOf(QStringLiteral("const std::size_t active = std::min(inFlight, batchSize);"));
    ASSERT_TRUE(afterLaunch >= 0);
    ASSERT_FALSE(batch.mid(afterLaunch).contains(QStringLiteral("CK(IGPU_LT_SYNC_CALL(")));
    ASSERT_FALSE(batch.contains(QStringLiteral("CK(presentOrderRc)")));
    // The batch body has no device-wide sync either.
    ASSERT_FALSE(batch.contains(QStringLiteral("cudaDeviceSynchronize")));
    ASSERT_FALSE(batch.contains(QStringLiteral("cudaStreamSynchronize(0)")));
}

namespace
{

// Host-side stub for the drain helper: a fake stream is its slot index, a fake error code is an int (0 = success).
// failAt maps a slot to the code its sync returns; every call is recorded in order.
struct StubSync
{
    std::vector<int> failAt;     // per slot: 0 = the sync succeeds, otherwise the code it returns
    std::vector<int> synced;     // the slots synchronized, in call order
    int operator()(int stream)
    {
        synced.push_back(stream);
        return failAt[static_cast<std::size_t>(stream)];
    }
};

std::vector<int> tileStreams(int n)
{
    std::vector<int> out;
    for (int i = 0; i < n; ++i) out.push_back(i);
    return out;
}

} // namespace

TEST(AmazePresentStream, StubDrainSyncsEveryStreamAfterAFailedSyncAndReturnsTheFirstError)
{
    using igpu_amaze_present::drain_every_stream;
    const std::vector<int> streams = tileStreams(6);
    // Stream k fails (for each k), with a second, later failure where there is room: every stream is still
    // synchronized exactly once, in order, and the FIRST failure is the code returned.
    for (int k = 0; k < 6; ++k)
    {
        StubSync stub{std::vector<int>(6, 0), {}};
        stub.failAt[static_cast<std::size_t>(k)] = 700 + k;
        if (k + 2 < 6) stub.failAt[static_cast<std::size_t>(k + 2)] = 900;
        const int rc = drain_every_stream(streams, streams.size(), 0, 0, [&](int s) { return stub(s); });
        ASSERT_EQ(700 + k, rc);
        ASSERT_TRUE(stub.synced == streams);
    }
    // Every stream fails: still six syncs, the first code wins.
    {
        StubSync stub{{11, 12, 13, 14, 15, 16}, {}};
        ASSERT_EQ(11, drain_every_stream(streams, streams.size(), 0, 0, [&](int s) { return stub(s); }));
        ASSERT_TRUE(stub.synced == streams);
    }
}

TEST(AmazePresentStream, StubDrainKeepsAnEarlierOrderingErrorAndStillDrainsEveryStream)
{
    using igpu_amaze_present::drain_every_stream;
    const std::vector<int> streams = tileStreams(4);
    // The ordering code is already set: it is the one returned, even when a drain fails too, and no stream is
    // skipped.
    StubSync stub{{0, 55, 0, 66}, {}};
    ASSERT_EQ(400, drain_every_stream(streams, streams.size(), 400, 0, [&](int s) { return stub(s); }));
    ASSERT_TRUE(stub.synced == streams);
    // All clean: success, every stream synchronized once.
    StubSync clean{{0, 0, 0, 0}, {}};
    ASSERT_EQ(0, drain_every_stream(streams, streams.size(), 0, 0, [&](int s) { return clean(s); }));
    ASSERT_TRUE(clean.synced == streams);
    // Only the active prefix is drained (fewer tiles than streams this frame), and zero active syncs nothing.
    StubSync prefix{{0, 0, 9, 0}, {}};
    ASSERT_EQ(0, drain_every_stream(streams, 2, 0, 0, [&](int s) { return prefix(s); }));
    ASSERT_TRUE(prefix.synced == tileStreams(2));
    StubSync none{{5, 5, 5, 5}, {}};
    ASSERT_EQ(0, drain_every_stream(streams, 0, 0, 0, [&](int s) { return none(s); }));
    ASSERT_TRUE(none.synced.empty());
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
