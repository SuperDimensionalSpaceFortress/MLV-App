// GPU-EVENT153-ROOTCAUSE-2: the CUDA backends' per-launch timer. Pins (1) the env gate --
// MLVAPP_CUDA_LAUNCH_TIMING is OFF unless it is exactly 1|on|true|yes, and the per-launch line
// threshold falls back to 0 on anything unparseable; (2) the exact text of every line the timer
// writes, which the bench lane parses out of result.json process.stderr; (3) the census: every
// kernel launch in both backend DLLs (and the stage-probe launch helpers the AMaZE DLL compiles
// in) sits inside IGPU_LT_LAUNCH, and every synchronising call inside IGPU_LT_SYNC_CALL, so a new
// launch cannot silently escape the timer; (4) OFF costs one cached-bool test per site; (5) backend
// teardown adds no synchronisation when ON, and the recorder destroys every event it created.
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

// A synchronising runtime call; C++ allows whitespace before the parenthesis.
const QRegularExpression & syncCallPattern()
{
    static const QRegularExpression re(QStringLiteral(
        "\\b(cuda\\w*Synchronize|cudaGraphicsMapResources|cudaGraphicsUnmapResources)\\s*\\("));
    return re;
}

// Each `<<<` / sync call in `source` that is not wrapped, as "label:line what".
QStringList unwrappedSitesIn(const QString & source, const QString & relativePath,
                             const QString & from = QString(), const QString & to = QString())
{
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
    auto it = syncCallPattern().globalMatch(source, begin);
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

QStringList unwrappedSites(const QString & relativePath, const QString & from = QString(), const QString & to = QString())
{
    return unwrappedSitesIn(readRepoFile(relativePath), relativePath, from, to);
}

// The body of the function whose definition starts with `signature`, up to its closing "\n}".
QString functionBody(const QString & source, const QString & signature)
{
    const int start = source.indexOf(signature);
    ASSERT_TRUE(start >= 0);
    const int end = source.indexOf(QStringLiteral("\n}"), start);
    ASSERT_TRUE(end > start);
    return source.mid(start, end - start);
}

// Every synchronising call written directly in a backend destructor, wrapped or not. Neither
// baseline destructor has one: the only waits at teardown are inside the helpers it already
// called (recon's preupload stream wait in reset_preupload_slots), so any hit is an addition.
QStringList destructorSyncs(const QString & body)
{
    QStringList hits;
    auto it = syncCallPattern().globalMatch(body);
    while (it.hasNext()) hits << it.next().captured(1);
    return hits;
}

const char * const kReconDestroy = "void igpu_recon_destroy(igpu_recon_backend* b)";
const char * const kAmazeDestroy = "extern \"C\" void igpu_amaze_debayer_destroy(igpu_amaze_debayer_backend * backend)";

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
    // A parser value table, not a timing claim: the threshold is a config value, and anything
    // unset or unparseable means "no threshold" (0).
    struct Case { const char * text; double want; };
    const Case cases[] = {
        {nullptr, 0.0}, {"", 0.0}, {"abc", 0.0}, {"-5", 0.0}, {"2ms", 0.0}, {"nan", 0.0},
        {"inf", 0.0}, {"2", 2.0}, {"0.5 ", 0.5},
    };
    for (const Case & c : cases)
        ASSERT_NEAR(c.want, igpu_lt_parse_min_ms(c.text), 1e-12);
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

// CUDA-RECON-PRESENT-OVERLAP-TIMELINE-1: device-clock stamps. A record whose pair resolved against
// the backend's anchor event carries has_dev, and its line APPENDS the two anchored stamps; a record
// without them renders byte-identical to the goldens above, whatever the dev fields hold.
TEST(CudaLaunchTiming, DeviceStampedRecordAppendsTheAnchoredStamps)
{
    IgpuLtRecord r = sampleKernel();
    r.has_dev = 1;
    r.dev_t0_utc_ms = 1791774588083.250;
    r.dev_t1_utc_ms = 1791774588083.662;
    ASSERT_EQ(std::string("igpu_launch_timing dll=recon kind=kernel stage=k_promote_match seq=12 "
                          "utc_ms=1791774588083.125 grid=120x51x1 block=16x16x1 w=1920 h=804 ms=0.412 "
                          "dev_t0_utc_ms=1791774588083.250 dev_t1_utc_ms=1791774588083.662"),
              formatRecord(r));
}

TEST(CudaLaunchTiming, RecordWithoutDeviceStampsIsByteIdenticalToTheGolden)
{
    const std::string golden("igpu_launch_timing dll=recon kind=kernel stage=k_promote_match seq=12 "
                             "utc_ms=1791774588083.125 grid=120x51x1 block=16x16x1 w=1920 h=804 ms=0.412");
    IgpuLtRecord r = sampleKernel();
    ASSERT_EQ(golden, formatRecord(r));
    // Stale dev values with has_dev=0 (e.g. an anchor that failed) never reach the line.
    r.dev_t0_utc_ms = 1.0;
    r.dev_t1_utc_ms = 2.0;
    r.dev_marks = 2;
    r.dev_mark_utc_ms[0] = 3.0;
    r.dev_mark_utc_ms[1] = 4.0;
    ASSERT_EQ(golden, formatRecord(r));
}

TEST(CudaLaunchTiming, RunSpanLineCarriesItsTwoInnerMarks)
{
    // The recon run span: [ev_start, ev_after_dl] with ev_after_up and ev_after_kernel as marks.
    IgpuLtRecord r = sampleKernel();
    r.kind = "span";
    r.stage = "run";
    r.grid = {0, 0, 0};
    r.block = {0, 0, 0};
    r.ms = 1.25;
    r.has_dev = 1;
    r.dev_t0_utc_ms = 1791774588083.000;
    r.dev_t1_utc_ms = 1791774588084.250;
    r.dev_marks = 2;
    r.dev_mark_utc_ms[0] = 1791774588083.100;
    r.dev_mark_utc_ms[1] = 1791774588083.900;
    ASSERT_EQ(std::string("igpu_launch_timing dll=recon kind=span stage=run seq=12 "
                          "utc_ms=1791774588083.125 grid=0x0x0 block=0x0x0 w=1920 h=804 ms=1.250 "
                          "dev_t0_utc_ms=1791774588083.000 dev_t1_utc_ms=1791774588084.250 "
                          "dev_marks_utc_ms=1791774588083.100,1791774588083.900"),
              formatRecord(r));
}

TEST(CudaLaunchTiming, AnchoredConfigLineNamesTheAnchorAndItsBracket)
{
    char line[256];
    igpu_lt_format_config_anchored(line, sizeof(line), "recon", 0.0, 1791774588000.125, 0.0120, 86400123.250);
    ASSERT_EQ(std::string("igpu_launch_timing_config dll=recon enabled=1 min_ms=0.000 "
                          "device_clock=anchored anchor_utc_ms=1791774588000.125 anchor_bracket_ms=0.012 "
                          "anchor_steady_ms=86400123.250"),
              std::string(line));
    // Without an anchor the config line is the unchanged golden.
    igpu_lt_format_config(line, sizeof(line), "amaze", 2.0);
    ASSERT_EQ(std::string("igpu_launch_timing_config dll=amaze enabled=1 min_ms=2.000"), std::string(line));
}

TEST(CudaLaunchTiming, AnchorTryAcrossAWallClockStepIsUnusable)
{
    // Adversary r2 A1, its input: before=1791774588000, after=1791774587000 (a 1000 ms backward step).
    double bracket = -1.0;
    ASSERT_EQ(0, igpu_lt_anchor_try_bracket(1791774588000.0, 1791774587000.0, 5000.0, 5000.125, &bracket));
    // A forward step inside the try is refused too; a negative monotonic interval never is a bracket.
    ASSERT_EQ(0, igpu_lt_anchor_try_bracket(1791774588000.0, 1791774589000.125, 5000.0, 5000.125, &bracket));
    ASSERT_EQ(0, igpu_lt_anchor_try_bracket(1791774588000.0, 1791774588000.0, 5000.125, 5000.0, &bracket));
    ASSERT_NEAR(-1.0, bracket, 0.0);
    // A clean try: the bracket is the longer of the two intervals (here the wall one).
    ASSERT_EQ(1, igpu_lt_anchor_try_bracket(1791774588000.0, 1791774588000.127, 5000.0, 5000.125, &bracket));
    // r3: at 1.79e12 ms a double's step is 2^-12 ms, so the wall interval is the representable one.
    ASSERT_NEAR(1791774588000.127 - 1791774588000.0, bracket, 1e-9);
}

TEST(CudaLaunchTiming, AnchorBracketCoversTheWallClockAnchor)
{
    // Adversary r2 pass 2 A1, its input: utc_before=U, steady 5000.0 -> 5000.020, utc_after=U+0.029
    // (inside the step tolerance), event at U+0.001. The anchor is utc_after, 0.028 ms after the
    // event, so the bracket must be at least that: it is the wall interval, 0.029.
    const double u = 1791774588000.0;
    double bracket = 0.0;
    ASSERT_EQ(1, igpu_lt_anchor_try_bracket(u, u + 0.029, 5000.0, 5000.020, &bracket));
    ASSERT_TRUE(bracket >= (u + 0.029) - (u + 0.001) - 1e-6);
    // And when the monotonic interval is the longer, it is kept.
    ASSERT_EQ(1, igpu_lt_anchor_try_bracket(u, u + 0.020, 5000.0, 5000.029, &bracket));
    ASSERT_NEAR(0.029, bracket, 1e-6);
}

TEST(CudaLaunchTiming, RecorderOnlyCudaFailuresDoNotStayPendingForTheBackend)
{
    // Adversary r2 pass 2 A2, its input: the anchor's event allocation fails at create, and the
    // backend's next CK(cudaGetLastError()) must not see it. Every recorder-only CUDA call that can
    // fail (anchor create/record/wait, pool create, elapsed read) reads the pending error first and
    // clears its own failure only when none was pending (drop_own_error), so a backend error is kept.
    const QString header = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_launch_timing.cuh"));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "    void establish_anchor_locked()\n    {\n        const cudaError_t prior = cudaPeekAtLastError();")));
    ASSERT_TRUE(header.contains(QStringLiteral("            if (cudaEventCreate(&ev) != cudaSuccess) break;")));
    ASSERT_TRUE(header.contains(QStringLiteral("            }\n        }\n        drop_own_error(prior);\n    }")));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "        const cudaError_t prior = cudaPeekAtLastError();\n        if (cudaEventCreate(ev) == cudaSuccess) return true;\n"
        "        drop_own_error(prior);\n        return false;")));
    ASSERT_TRUE(header.contains(QStringLiteral("    static void drop_own_error(cudaError_t prior)\n    {\n"
                                               "        if (prior == cudaSuccess) (void)cudaGetLastError();\n    }")));
    ASSERT_EQ(3, (int)header.count(QStringLiteral("drop_own_error(prior);")));
    // No early return inside the anchor loop skips the clear.
    const int def = header.indexOf(QStringLiteral("    void establish_anchor_locked()"));
    const int end = header.indexOf(QStringLiteral("        drop_own_error(prior);\n    }"), def);
    ASSERT_FALSE(header.mid(def, end - def).contains(QStringLiteral("return")));
}

TEST(CudaLaunchTiming, DeviceStampsAreNeverCutByAnInternalBuffer)
{
    // Adversary r2 pass 2 A4: stamps of any magnitude reach the line, and the return is the full
    // length (so a short buffer reports truncation instead of a silently cut line).
    IgpuLtRecord r = sampleKernel();
    r.kind = "span";
    r.stage = "run";
    r.has_dev = 1;
    r.dev_t0_utc_ms = 1e60;
    r.dev_t1_utc_ms = 2e60;
    r.dev_marks = 2;
    r.dev_mark_utc_ms[0] = 3e60;
    r.dev_mark_utc_ms[1] = 4e60;
    const int need = igpu_lt_format_record(nullptr, 0, r);
    std::string line((std::size_t)need + 1, '\0');
    ASSERT_EQ(need, igpu_lt_format_record(&line[0], line.size(), r));
    line.resize((std::size_t)need);
    ASSERT_TRUE(line.find(" dev_marks_utc_ms=") != std::string::npos);
    ASSERT_EQ(std::size_t(line.size() - 4), line.rfind(".000"));
    char tiny[200];
    ASSERT_EQ(need, igpu_lt_format_record(tiny, sizeof(tiny), r));
    ASSERT_EQ(std::size_t(sizeof(tiny) - 1), std::strlen(tiny));
}

TEST(CudaLaunchTiming, AnchorsAcrossAWallClockStepAreNotComparable)
{
    // Adversary r2 A1, its input: a 1000 ms backward wall-clock step between the two DLLs' anchors,
    // brackets 0.125 ms. Same monotonic gap, wall gap 1000 ms shorter: not comparable.
    ASSERT_EQ(0, igpu_lt_anchors_comparable(1791774590000.0, 7000.0, 0.125, 1791774588000.0 + 1000.0, 5000.0, 0.125));
    // No step: the wall and monotonic offsets agree, comparable.
    ASSERT_EQ(1, igpu_lt_anchors_comparable(1791774590000.0, 7000.0, 0.125, 1791774588000.0, 5000.0, 0.125));
    // Disagreement inside the summed brackets is still comparable.
    ASSERT_EQ(1, igpu_lt_anchors_comparable(1791774590000.2, 7000.0, 0.125, 1791774588000.0, 5000.0, 0.125));
}

TEST(CudaLaunchTiming, NoDeviceStampPastTheFloatPrecisionAge)
{
    // Adversary r2 A2, its input: anchor-relative elapsed times 16777216.5 and 16777215.5 ms, where a
    // float's step is 1-2 ms. Neither gets a stamp; a young elapsed time does, exactly.
    double out = -1.0;
    ASSERT_EQ(0, igpu_lt_dev_stamp(1791774588000.0, 16777216.5, &out));
    ASSERT_EQ(0, igpu_lt_dev_stamp(1791774588001.0, 16777215.5, &out));
    ASSERT_EQ(0, igpu_lt_dev_stamp(1791774588000.0, -0.5, &out));
    ASSERT_EQ(0, igpu_lt_dev_stamp(1791774588000.0, IGPU_LT_ANCHOR_MAX_AGE_MS, &out));
    ASSERT_NEAR(-1.0, out, 0.0);
    ASSERT_EQ(1, igpu_lt_dev_stamp(1791774588000.0, 1234.5, &out));
    ASSERT_NEAR(1791774589234.5, out, 1e-6);
    // Inside the limit a float's half-step is at most IGPU_LT_ANCHOR_ROUNDING_MS.
    const double young = IGPU_LT_ANCHOR_MAX_AGE_MS - 0.7;
    const float f = (float)young;
    ASSERT_TRUE(std::fabs((double)f - young) <= IGPU_LT_ANCHOR_ROUNDING_MS);
}

TEST(CudaLaunchTiming, HugeMinMsNeverTruncatesTheAnchorFields)
{
    // Adversary r2 A3, its input: MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS=1e308 (accepted by the parser). The
    // line needs more than 256 bytes; sized from the formatter's return it keeps every anchor field.
    ASSERT_NEAR(1e308, igpu_lt_parse_min_ms("1e308"), 1e292);
    const int need = igpu_lt_format_config_anchored(nullptr, 0, "recon", 1e308, 1791774588000.125, 0.012, 5000.0);
    ASSERT_TRUE(need > 256);
    std::string line((std::size_t)need + 1, '\0');
    ASSERT_EQ(need, igpu_lt_format_config_anchored(&line[0], line.size(), "recon", 1e308, 1791774588000.125, 0.012, 5000.0));
    line.resize((std::size_t)need);
    ASSERT_TRUE(line.find(" device_clock=anchored anchor_utc_ms=1791774588000.125 anchor_bracket_ms=0.012 "
                          "anchor_steady_ms=5000.000") != std::string::npos);
    // The recorder writes every line through write_line, which sizes from that return.
    const QString header = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_launch_timing.cuh"));
    ASSERT_TRUE(header.contains(QStringLiteral("std::vector<char> big((std::size_t)need + 1);")));
    ASSERT_FALSE(header.contains(QStringLiteral("char line[")));
    // config (anchored and plain), record, summary.
    ASSERT_EQ(4, (int)header.count(QStringLiteral("write_line([&](char * out, std::size_t cap)")));
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

TEST(CudaLaunchTiming, CensusSeesASyncWithWhitespaceBeforeItsParenthesis)
{
    // sol r1: `cudaEventSynchronize (b->ev_after_dl)` used to pass the census.
    const QString recon = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_recon_cuda.cu"));
    const QString label = QStringLiteral("recon-mutant");
    ASSERT_EQ(std::string(), unwrappedSitesIn(recon, label).join(QStringLiteral("; ")).toStdString());
    for (const char * mutant : {"\nstatic void mutant(igpu_recon_backend* b) { cudaEventSynchronize (b->ev_after_dl); }\n",
                                "\nstatic void mutant(igpu_recon_backend* b) { cudaStreamSynchronize\t(b->preupload_stream); }\n",
                                "\nstatic void mutant() { cudaDeviceSynchronize\n(); }\n"})
    {
        const QStringList bad = unwrappedSitesIn(recon + QString::fromLatin1(mutant), label);
        ASSERT_EQ(1, (int)bad.size());
    }
}

TEST(CudaLaunchTiming, TimingOnAddsNoSynchronisationAtBackendTeardown)
{
    // Catalogue row (sol r1 blocker): timing enabled, outstanding CUDA work, backend teardown ->
    // no additional synchronisation. Neither destructor may call a synchronising API itself, under
    // the timing flag or not; the recorder is torn down as the destructor's last step, after the
    // completion points the baseline destructor already has, and teardown() never waits.
    const QString recon = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_recon_cuda.cu"));
    const QString amaze = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_amaze_debayer_cuda.cu"));
    const QString reconBody = functionBody(recon, QString::fromLatin1(kReconDestroy));
    const QString amazeBody = functionBody(amaze, QString::fromLatin1(kAmazeDestroy));
    ASSERT_EQ(std::string(), destructorSyncs(reconBody).join(QStringLiteral(", ")).toStdString());
    ASSERT_EQ(std::string(), destructorSyncs(amazeBody).join(QStringLiteral(", ")).toStdString());
    ASSERT_FALSE(reconBody.contains(QStringLiteral("igpu_lt::enabled()")));
    ASSERT_FALSE(amazeBody.contains(QStringLiteral("igpu_lt::enabled()")));
    ASSERT_TRUE(reconBody.endsWith(QStringLiteral("    free(b);\n    igpu_lt::teardown(\"destroy\");")));
    ASSERT_TRUE(amazeBody.endsWith(QStringLiteral("    delete backend;\n    igpu_lt::teardown(\"destroy\");")));
    // The recon preupload stream wait is the baseline's own completion point, and comes first.
    ASSERT_TRUE(reconBody.indexOf(QStringLiteral("reset_preupload_slots(b, 1);"))
                < reconBody.indexOf(QStringLiteral("igpu_lt::teardown(")));

    // RED on the r1 shape, and on a spelling with whitespace before the parenthesis.
    for (const char * added : {"    if (igpu_lt::enabled()) {\n"
                               "        (void)IGPU_LT_SYNC_CALL(\"destroy:device_sync\", cudaDeviceSynchronize());\n"
                               "    }\n",
                               "    if (igpu_lt::enabled()) cudaDeviceSynchronize ();\n"})
    {
        QString mutant = reconBody;
        mutant.insert(mutant.indexOf(QStringLiteral("    reset_preupload_slots(b, 1);")), QString::fromLatin1(added));
        ASSERT_EQ(std::string("cudaDeviceSynchronize"), destructorSyncs(mutant).join(QStringLiteral(", ")).toStdString());
    }
}

TEST(CudaLaunchTiming, RecorderDestroysEveryEventItCreates)
{
    // sol r1 hardening: teardown destroys the pool (and, for the last backend, the pairs still
    // pending), and the record/query/elapsed error branches destroy their handles instead of
    // dropping them. Each backend attaches on create and tears down on destroy.
    const QString header = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_launch_timing.cuh"));
    ASSERT_TRUE(header.contains(QStringLiteral("inline void teardown(const char * at)\n{\n    if (!enabled()) return;")));
    ASSERT_TRUE(header.contains(QStringLiteral("inline void attach()\n{\n    if (!enabled()) return;")));
    ASSERT_TRUE(header.contains(QStringLiteral("for (cudaEvent_t ev : pool_) cudaEventDestroy(ev);")));
    ASSERT_TRUE(header.contains(QStringLiteral("for (Pending & p : pending_) destroy_events_locked(p);")));
    // begin_kernel record failure, end_kernel record/launch failure, poll query/elapsed failure.
    ASSERT_TRUE(header.count(QStringLiteral("destroy_events_locked(p);")) >= 3);
    ASSERT_TRUE(header.contains(QStringLiteral("destroy_events_locked(*it);")));
    ASSERT_FALSE(header.contains(QStringLiteral("p.start = p.stop = nullptr;\n            return;")));
    // Adversary r2 A4: the one exception is the device-clock anchor, kept with the leaked recorder
    // (so later backends stamp against it); it is destroyed only when a better try replaces it, and
    // the contract says so.
    ASSERT_EQ(1, (int)header.count(QStringLiteral("cudaEventDestroy(anchor_)")));
    ASSERT_TRUE(header.contains(QStringLiteral("every event the recorder created, without waiting on the device, except\n"
                                               " *       the one device-clock anchor")));
    for (const char * dll : {"tools/gpu/backend/igpu_recon_cuda.cu", "tools/gpu/backend/igpu_amaze_debayer_cuda.cu"})
    {
        const QString source = readRepoFile(QString::fromLatin1(dll));
        ASSERT_EQ(1, (int)source.count(QStringLiteral("igpu_lt::attach();")));
        ASSERT_EQ(1, (int)source.count(QStringLiteral("igpu_lt::teardown(\"destroy\");")));
    }
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
    // The launch timer never synchronises per frame. CUDA-RECON-PRESENT-OVERLAP-TIMELINE-1: its one
    // synchronising call is the device-clock anchor's cudaEventSynchronize, inside
    // establish_anchor_locked(), which only the first attach() (backend create) reaches.
    QStringList syncs;
    auto it = syncCallPattern().globalMatch(header);
    while (it.hasNext()) syncs << it.next().captured(1);
    ASSERT_EQ(std::string("cudaEventSynchronize"), syncs.join(QStringLiteral(", ")).toStdString());
    const int anchorDef = header.indexOf(QStringLiteral("    void establish_anchor_locked()\n    {\n"));
    ASSERT_TRUE(anchorDef >= 0);
    const int anchorEnd = header.indexOf(QStringLiteral("\n        drop_own_error(prior);\n    }\n"), anchorDef);
    ASSERT_TRUE(anchorEnd > anchorDef);
    const int syncAt = header.indexOf(syncCallPattern());
    ASSERT_TRUE(syncAt > anchorDef && syncAt < anchorEnd);
    ASSERT_EQ(2, (int)header.count(QStringLiteral("establish_anchor_locked()")));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "        live_backends_ += 1;\n        if (!anchor_tried_)\n        {\n            anchor_tried_ = true;\n"
        "            establish_anchor_locked();")));
    // Adversary r2 pass 3 A5: the pin follows record_span's signature since pass 2 (issue_utc after stage).
    ASSERT_TRUE(header.contains(QStringLiteral("inline void record_span(const char * stage, double issue_utc, cudaEvent_t t0, cudaEvent_t mark0,\n"
                                               "                        cudaEvent_t mark1, cudaEvent_t t1, double ms)\n{\n    if (!enabled()) return;")));
    ASSERT_TRUE(header.contains(QStringLiteral("if (!enabled()) return;\n        active_ = Recorder::get().begin_kernel(pending_, stage, dim3(0, 0, 0)")));
}

TEST(CudaLaunchTiming, DeviceSpansSitOnTheirOwnStreamAfterTheExistingWaits)
{
    // CUDA-RECON-PRESENT-OVERLAP-TIMELINE-1. Recon: one run span per run, after the run's own
    // cudaEventSynchronize(ev_after_dl) and its elapsed reads, so its four events are complete and
    // no wait is added. AMaZE: the stream-0 GL handoff (map, copy, unmap) is bracketed by a span
    // on stream 0, closed right after the unmap.
    const QString recon = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_recon_cuda.cu"));
    const QString amaze = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_amaze_debayer_cuda.cu"));
    const QString span = QStringLiteral(
        "igpu_lt::record_span(\"run\", lt_issue_utc, b->ev_start, b->ev_after_up, b->ev_after_kernel, b->ev_after_dl,\n"
        "                         (double)tot);");
    ASSERT_EQ(1, (int)recon.count(span));
    // Adversary r2 pass 2 A3: the span's utc_ms is the host clock at issue, read just before ev_start.
    ASSERT_TRUE(recon.contains(QStringLiteral(
        "    const double lt_issue_utc = igpu_lt::issue_utc_ms(); /* the run span's host issue time (env-gated) */\n"
        "    CK(cudaEventRecord(b->ev_start, 0));")));
    const QString header = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_launch_timing.cuh"));
    ASSERT_TRUE(header.contains(QStringLiteral("        fill_locked(&rec, \"span\", stage);\n        rec.utc_ms = issue_utc;")));
    ASSERT_TRUE(header.contains(QStringLiteral("inline double issue_utc_ms()\n{\n    if (!enabled()) return 0.0;")));
    ASSERT_TRUE(recon.indexOf(QStringLiteral("CK(cudaEventElapsedTime(&tot,  b->ev_start,        b->ev_after_dl));"))
                < recon.indexOf(span));
    ASSERT_TRUE(recon.indexOf(QStringLiteral("cudaEventSynchronize(b->ev_after_dl)")) < recon.indexOf(span));
    ASSERT_TRUE(amaze.contains(QStringLiteral(
        "igpu_lt::SpanScope handoff(\"gl_rgba16_direct:handoff\", 0);\n"
        "        cudaError_t rc = IGPU_LT_SYNC_CALL(\"gl_rgba16_direct:map\", cudaGraphicsMapResources(1, &resource, 0));")));
    ASSERT_TRUE(amaze.contains(QStringLiteral(
        "IGPU_LT_SYNC_CALL(\"gl_rgba16_direct:unmap\", cudaGraphicsUnmapResources(1, &resource, 0));\n"
        "        handoff.end();")));
    ASSERT_EQ(1, (int)amaze.count(QStringLiteral("igpu_lt::SpanScope ")));
}
