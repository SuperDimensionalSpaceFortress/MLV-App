// HFR-NATIVE-M16-1457-GUI-TEXTURE-PRESENT-OFFLOAD-1 r3 (the stream-0 clause): the GUI present path of the CUDA AMaZE
// backend runs its GL handoff on its own cudaStreamNonBlocking stream, ordered after the frame's AMaZE tile streams by
// an event, behind the kill switch MLVAPP_CUDA_AMAZE_PRESENT_STREAM (default ON). No GPU: the switch's parse, and
// source pins on the stream / event ownership seam in tools/gpu/backend/igpu_amaze_debayer_cuda.cu.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../tools/gpu/backend/igpu_amaze_present_stream.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTextStream>

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
    const int record = batch.indexOf(QStringLiteral("CK(cudaEventRecord(presentProducerDone, streams[slot]));"));
    const int wait = batch.indexOf(QStringLiteral("CK(cudaStreamWaitEvent(presentStream, presentProducerDone, 0));"));
    const int gate = batch.indexOf(QStringLiteral("if (presentStream && presentProducerDone)"));
    const int drain = batch.indexOf(QStringLiteral("CK(IGPU_LT_SYNC_CALL(\"tiles:drain_sync\", cudaStreamSynchronize(streams[slot])));"));
    ASSERT_TRUE(gate >= 0);
    ASSERT_TRUE(record > gate);
    ASSERT_TRUE(wait > record);
    ASSERT_TRUE(drain > wait);
    // The tile streams themselves stay non-blocking (they never used stream 0).
    ASSERT_TRUE(cu.contains(QStringLiteral("            &backend->liveTileStreams[static_cast<std::size_t>(index)],\n"
                                           "            cudaStreamNonBlocking));")));
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
