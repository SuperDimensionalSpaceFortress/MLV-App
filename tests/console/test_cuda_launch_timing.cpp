// GPU-EVENT153-ROOTCAUSE-2: the CUDA backends' per-launch timer. Pins (1) the env gate --
// MLVAPP_CUDA_LAUNCH_TIMING is OFF unless it is exactly 1|on|true|yes, and the per-launch line
// threshold falls back to 0 on anything unparseable; (2) the exact text of every line the timer
// writes, which the bench lane parses out of result.json process.stderr; (3) the census: every
// kernel launch in both backend DLLs (and the stage-probe launch helpers the AMaZE DLL compiles
// in) sits inside IGPU_LT_LAUNCH, and every synchronising call inside IGPU_LT_SYNC_CALL, so a new
// launch cannot silently escape the timer; (4) OFF costs one cached-bool test per site.
// The .cu files need nvcc, which console_tests does not have, so (3) and (4) read them as text,
// like test_contact_sheet_capture_wiring.cpp does for MainWindow.cpp.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../tools/gpu/backend/igpu_launch_timing_format.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTextStream>

#include <string>

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

IgpuLtRecord sampleKernel()
{
    IgpuLtRecord r;
    std::memset(&r, 0, sizeof(r));
    r.dll = "recon";
    r.kind = "kernel";
    r.stage = "k_promote_match";
    r.seq = 12;
    r.utc_ms = 1791774588083.125;
    r.grid = {120, 51, 1};
    r.block = {16, 16, 1};
    r.width = 1920;
    r.height = 804;
    r.tile_top = IGPU_LT_NO_TILE;
    r.tile_left = IGPU_LT_NO_TILE;
    r.ms = 0.412;
    return r;
}

std::string formatRecord(const IgpuLtRecord & r)
{
    char line[512];
    const int n = igpu_lt_format_record(line, sizeof(line), r);
    ASSERT_TRUE(n > 0 && n < (int)sizeof(line));
    return std::string(line);
}

// Each `<<<` / sync call that is not wrapped, as "file:line what".
QStringList unwrappedSites(const QString & relativePath, const QString & from = QString(), const QString & to = QString())
{
    const QString source = readRepoFile(relativePath);
    int begin = 0;
    int end = source.size();
    if (!from.isEmpty())
    {
        begin = source.indexOf(from);
        ASSERT_TRUE(begin >= 0);
        end = source.indexOf(to, begin);
        ASSERT_TRUE(end > begin);
    }
    QStringList bad;
    auto statementPrefix = [&](int at) {
        int start = begin;
        for (const QChar c : {QChar(';'), QChar('{'), QChar('}')})
        {
            const int p = source.lastIndexOf(c, at - 1);
            if (p + 1 > start) start = p + 1;
        }
        return source.mid(start, at - start);
    };
    auto lineOf = [&](int at) { return source.left(at).count(QLatin1Char('\n')) + 1; };

    for (int at = source.indexOf(QStringLiteral("<<<"), begin); at >= 0 && at < end;
         at = source.indexOf(QStringLiteral("<<<"), at + 3))
    {
        if (!statementPrefix(at).contains(QStringLiteral("IGPU_LT_LAUNCH(")))
            bad << QStringLiteral("%1:%2 launch").arg(relativePath).arg(lineOf(at));
    }
    const QRegularExpression syncCall(
        QStringLiteral("\\b(cuda\\w*Synchronize|cudaGraphicsMapResources|cudaGraphicsUnmapResources)\\("));
    auto it = syncCall.globalMatch(source, begin);
    while (it.hasNext())
    {
        const QRegularExpressionMatch m = it.next();
        const int at = m.capturedStart();
        if (at >= end) break;
        const int lineStart = source.lastIndexOf(QLatin1Char('\n'), at) + 1;
        if (source.mid(lineStart, at - lineStart).count(QLatin1Char('"')) % 2 == 1) continue; // inside an error message
        if (!statementPrefix(at).contains(QStringLiteral("IGPU_LT_SYNC_CALL(")))
            bad << QStringLiteral("%1:%2 %3").arg(relativePath).arg(lineOf(at)).arg(m.captured(1));
    }
    return bad;
}

} // namespace

TEST(CudaLaunchTiming, GateIsOffUnlessExplicitlyOn)
{
    ASSERT_EQ(0, igpu_lt_parse_enabled(nullptr));
    ASSERT_EQ(0, igpu_lt_parse_enabled(""));
    ASSERT_EQ(0, igpu_lt_parse_enabled("0"));
    ASSERT_EQ(0, igpu_lt_parse_enabled("off"));
    ASSERT_EQ(0, igpu_lt_parse_enabled("false"));
    ASSERT_EQ(0, igpu_lt_parse_enabled("2"));
    ASSERT_EQ(0, igpu_lt_parse_enabled("enabled"));
    ASSERT_EQ(0, igpu_lt_parse_enabled("1x"));
    ASSERT_EQ(1, igpu_lt_parse_enabled("1"));
    ASSERT_EQ(1, igpu_lt_parse_enabled("on"));
    ASSERT_EQ(1, igpu_lt_parse_enabled("TRUE"));
    ASSERT_EQ(1, igpu_lt_parse_enabled("Yes"));
    ASSERT_EQ(1, igpu_lt_parse_enabled(" 1 "));
}

TEST(CudaLaunchTiming, EnvNamesAreTheDocumentedOnes)
{
    ASSERT_EQ(std::string("MLVAPP_CUDA_LAUNCH_TIMING"), std::string(IGPU_LT_ENV_ENABLE));
    ASSERT_EQ(std::string("MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS"), std::string(IGPU_LT_ENV_MIN_MS));
}

TEST(CudaLaunchTiming, MinMsFallsBackToZero)
{
    ASSERT_NEAR(0.0, igpu_lt_parse_min_ms(nullptr), 1e-12);
    ASSERT_NEAR(0.0, igpu_lt_parse_min_ms(""), 1e-12);
    ASSERT_NEAR(0.0, igpu_lt_parse_min_ms("abc"), 1e-12);
    ASSERT_NEAR(0.0, igpu_lt_parse_min_ms("-5"), 1e-12);
    ASSERT_NEAR(0.0, igpu_lt_parse_min_ms("2ms"), 1e-12);
    ASSERT_NEAR(0.0, igpu_lt_parse_min_ms("nan"), 1e-12);
    ASSERT_NEAR(0.0, igpu_lt_parse_min_ms("inf"), 1e-12);
    ASSERT_NEAR(2.0, igpu_lt_parse_min_ms("2"), 1e-12);
    ASSERT_NEAR(0.5, igpu_lt_parse_min_ms("0.5 "), 1e-12);
}

TEST(CudaLaunchTiming, KernelLineFormat)
{
    ASSERT_EQ(std::string("igpu_launch_timing dll=recon kind=kernel stage=k_promote_match seq=12 "
                          "utc_ms=1791774588083.125 grid=120x51x1 block=16x16x1 w=1920 h=804 ms=0.412"),
              formatRecord(sampleKernel()));
}

TEST(CudaLaunchTiming, TiledKernelLineCarriesTheTileOrigin)
{
    IgpuLtRecord r = sampleKernel();
    r.dll = "amaze";
    r.stage = "k_gradients";
    r.grid = {32, 32, 1};
    r.tile_top = -16;
    r.tile_left = 464;
    r.ms = 0.811;
    ASSERT_EQ(std::string("igpu_launch_timing dll=amaze kind=kernel stage=k_gradients seq=12 "
                          "utc_ms=1791774588083.125 grid=32x32x1 block=16x16x1 w=1920 h=804 tile=-16:464 ms=0.811"),
              formatRecord(r));
}

TEST(CudaLaunchTiming, ErrorLineHasNoDurationAndNamesTheError)
{
    IgpuLtRecord r = sampleKernel();
    r.ms = -1.0;
    r.err = 700;
    r.err_name = "cudaErrorIllegalAddress";
    ASSERT_EQ(std::string("igpu_launch_timing dll=recon kind=kernel stage=k_promote_match seq=12 "
                          "utc_ms=1791774588083.125 grid=120x51x1 block=16x16x1 w=1920 h=804 ms=na "
                          "err=700 err_name=cudaErrorIllegalAddress"),
              formatRecord(r));
}

TEST(CudaLaunchTiming, SyncLineHasZeroGeometry)
{
    IgpuLtRecord r = sampleKernel();
    r.kind = "sync";
    r.stage = "run:ev_after_dl";
    r.grid = {0, 0, 0};
    r.block = {0, 0, 0};
    r.ms = 3.1;
    ASSERT_EQ(std::string("igpu_launch_timing dll=recon kind=sync stage=run:ev_after_dl seq=12 "
                          "utc_ms=1791774588083.125 grid=0x0x0 block=0x0x0 w=1920 h=804 ms=3.100"),
              formatRecord(r));
}

TEST(CudaLaunchTiming, ConfigLineFormat)
{
    char line[160];
    igpu_lt_format_config(line, sizeof(line), "amaze", 2.0);
    ASSERT_EQ(std::string("igpu_launch_timing_config dll=amaze enabled=1 min_ms=2.000"), std::string(line));
}

TEST(CudaLaunchTiming, StatsKeepTheLongestLaunchAndCountOverThreshold)
{
    IgpuLtStats s;
    std::memset(&s, 0, sizeof(s));
    IgpuLtRecord r = sampleKernel();
    r.ms = 0.5;
    igpu_lt_stats_add(&s, r, 2.0);
    r.seq = 13;
    r.ms = 2.5;
    igpu_lt_stats_add(&s, r, 2.0);
    r.seq = 14;
    r.ms = 1.0;
    igpu_lt_stats_add(&s, r, 2.0);
    r.seq = 15;
    r.ms = -1.0;
    r.err = 700;
    r.err_name = "cudaErrorIllegalAddress";
    igpu_lt_stats_add(&s, r, 2.0);
    ASSERT_EQ(4ull, s.count);
    ASSERT_EQ(1ull, s.over_min);
    ASSERT_EQ(1ull, s.errors);
    ASSERT_NEAR(4.0, s.sum_ms, 1e-9);
    ASSERT_NEAR(2.5, s.max_ms, 1e-9);
    ASSERT_EQ(13ull, s.max_rec.seq);

    char line[640];
    igpu_lt_format_summary(line, sizeof(line), "recon", "kernel", "k_promote_match", "periodic", s);
    ASSERT_EQ(std::string("igpu_launch_timing_summary dll=recon kind=kernel stage=k_promote_match at=periodic "
                          "count=4 over_min=1 errors=1 sum_ms=4.000 max_ms=2.500 max_seq=13 "
                          "max_utc_ms=1791774588083.125 max_grid=120x51x1 max_block=16x16x1 max_w=1920 max_h=804"),
              std::string(line));
}

TEST(CudaLaunchTiming, SummaryOfAStageThatOnlyFaultedHasNoMax)
{
    IgpuLtStats s;
    std::memset(&s, 0, sizeof(s));
    IgpuLtRecord r = sampleKernel();
    r.ms = -1.0;
    r.err = 719;
    r.err_name = "cudaErrorLaunchFailure";
    igpu_lt_stats_add(&s, r, 0.0);
    char line[640];
    igpu_lt_format_summary(line, sizeof(line), "amaze", "kernel", "k_gradients", "destroy", s);
    ASSERT_EQ(std::string("igpu_launch_timing_summary dll=amaze kind=kernel stage=k_gradients at=destroy "
                          "count=1 over_min=0 errors=1 sum_ms=0.000 max_ms=na"),
              std::string(line));
}

TEST(CudaLaunchTiming, PerLaunchLinesAreThresholdedButFirstsAndErrorsAlwaysLog)
{
    IgpuLtRecord r = sampleKernel();
    r.ms = 0.4;
    ASSERT_EQ(1, igpu_lt_should_log_record(r, 0.0, 0));  // default: every launch
    ASSERT_EQ(0, igpu_lt_should_log_record(r, 2.0, 0));  // under the threshold
    ASSERT_EQ(1, igpu_lt_should_log_record(r, 2.0, 1));  // first of its stage
    r.ms = 2.0;
    ASSERT_EQ(1, igpu_lt_should_log_record(r, 2.0, 0));  // at the threshold
    r.ms = -1.0;
    r.err = 702;
    r.err_name = "cudaErrorLaunchTimeout";
    ASSERT_EQ(1, igpu_lt_should_log_record(r, 2.0, 0));  // every error
}

TEST(CudaLaunchTiming, TruncationIsReportedNotOverrun)
{
    char tiny[16];
    const int n = igpu_lt_format_record(tiny, sizeof(tiny), sampleKernel());
    ASSERT_TRUE(n >= (int)sizeof(tiny));
    ASSERT_EQ(std::size_t(sizeof(tiny) - 1), std::strlen(tiny));
}

TEST(CudaLaunchTiming, EveryBackendLaunchAndSyncIsTimed)
{
    const QStringList bad =
        unwrappedSites(QStringLiteral("tools/gpu/backend/igpu_recon_cuda.cu"))
        + unwrappedSites(QStringLiteral("tools/gpu/backend/igpu_amaze_debayer_cuda.cu"))
        // The stage-probe launch helpers the AMaZE DLL compiles in (its own probe-only
        // timed-frame code below allocate_device is never reached from the DLL).
        + unwrappedSites(QStringLiteral("tools/gpu/cuda_amaze_debayer_stage_probe.cu"),
                         QStringLiteral("void launch_variance_selection_prefix("),
                         QStringLiteral("void allocate_device("));
    ASSERT_EQ(std::string(), bad.join(QStringLiteral("; ")).toStdString());
}

TEST(CudaLaunchTiming, CensusSeesTheSitesItGuards)
{
    // A census that matched nothing would pass vacuously: the backends really do contain the
    // wrapped forms it is looking for.
    const QString recon = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_recon_cuda.cu"));
    const QString amaze = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_amaze_debayer_cuda.cu"));
    ASSERT_TRUE(recon.count(QStringLiteral("IGPU_LT_LAUNCH(")) >= 25);
    ASSERT_TRUE(amaze.count(QStringLiteral("IGPU_LT_LAUNCH(")) >= 30);
    ASSERT_TRUE(recon.contains(QStringLiteral("IGPU_LT_SYNC_CALL(\"run:ev_after_dl\", cudaEventSynchronize(b->ev_after_dl))")));
    ASSERT_TRUE(amaze.contains(QStringLiteral("IGPU_LT_SYNC_CALL(\"tiles:drain_sync\", cudaStreamSynchronize(streams[slot]))")));
}

TEST(CudaLaunchTiming, EachDllNamesItselfAndPollsAfterItsOwnSync)
{
    const QString recon = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_recon_cuda.cu"));
    const QString amaze = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_amaze_debayer_cuda.cu"));
    ASSERT_TRUE(recon.contains(QStringLiteral("#define IGPU_LT_DLL_NAME \"recon\"\n#include \"igpu_launch_timing.cuh\"")));
    ASSERT_TRUE(amaze.contains(QStringLiteral("#define IGPU_LT_DLL_NAME \"amaze\"\n#include \"igpu_launch_timing.cuh\"")));
    // The recorder resolves events only after a sync the backend already does: no new sync.
    ASSERT_TRUE(recon.contains(QStringLiteral(
        "CK(IGPU_LT_SYNC_CALL(\"run:ev_after_dl\", cudaEventSynchronize(b->ev_after_dl)));\n    igpu_lt::poll();")));
    ASSERT_TRUE(amaze.count(QStringLiteral("igpu_lt::poll();")) >= 4);
}

TEST(CudaLaunchTiming, OffCostsOneCachedTestPerSite)
{
    const QString header = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_launch_timing.cuh"));
    // The gate is read once (a function-local static) ...
    ASSERT_TRUE(header.contains(QStringLiteral(
        "static const bool on = igpu_lt_parse_enabled(std::getenv(IGPU_LT_ENV_ENABLE)) != 0;")));
    // ... and every scope returns on it before touching CUDA.
    ASSERT_TRUE(header.contains(QStringLiteral("if (!enabled()) return;\n        active_ = Recorder::get().begin_kernel(")));
    ASSERT_TRUE(header.contains(QStringLiteral("if (!enabled()) return call();")));
    ASSERT_TRUE(header.contains(QStringLiteral("inline void poll(const char * at = nullptr)\n{\n    if (!enabled()) return;")));
    // The launch timer never synchronises: no Synchronize call anywhere in the recorder.
    ASSERT_FALSE(header.contains(QRegularExpression(QStringLiteral("cuda\\w*Synchronize\\("))));
}
