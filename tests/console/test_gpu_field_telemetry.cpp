// GPU-FIELD-TELEMETRY-1: the local GPU field log. Pins (1) the per-stage aggregate -- exact count,
// max, over_500ms, errors and sum, and p95 from the launch timer's fixed log-bucket histogram, over
// (input, expected) value tables rather than timing claims; (2) rotation at the cap: .1/.2/.3 shift,
// the oldest dropped, the live file reopened; (3) the line text and its privacy: no path separator,
// no path/user/host field; (4) the env gate (ON unless 0|off|false|no) and the device-loss codes;
// (5) text censuses: every telemetry call in GpuDebayer.cpp / GpuDisplayViewport.cpp /
// GpuDisplayWindow.cpp / MainWindow.cpp
// sits under GpuFieldTelemetry::enabled(), enabled() is a function-local static, no NVML /
// nvidia-smi / machine fingerprint anywhere in telemetry, the CK / CKN / check_cuda failure branches
// note the code inside the failure branch only, and igpu_launch_timing.cuh is byte-identical to the
// base 6be0df82 (CUDA-LT-TIMING-HARDENING-1 owns it). The injected-702 capture through the real
// llrawproc seam, and install() with telemetry OFF, need llrawproc and live in pipeline_tests
// (tests/pipeline/test_gpu_field_telemetry_capture.cpp).
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../platform/qt/GpuFieldTelemetryCore.h"

#include <QCryptographicHash>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTemporaryDir>
#include <QTextStream>

#include <string>
#include <vector>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

namespace GpuFieldTelemetryCore
{
// For minitest's failure message.
inline std::ostream & operator<<(std::ostream & os, WriteOutcome outcome)
{
    return os << (outcome == WriteOutcome::Written ? "Written"
                  : outcome == WriteOutcome::DroppedRotationFailed ? "DroppedRotationFailed" : "WriteFailed");
}
} // namespace GpuFieldTelemetryCore

namespace
{
namespace core = GpuFieldTelemetryCore;

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

std::string readFile(const std::filesystem::path & path)
{
    std::ifstream in(path, std::ios::binary);
    return std::string((std::istreambuf_iterator<char>(in)), std::istreambuf_iterator<char>());
}

QJsonObject parseLine(const std::string & line)
{
    QJsonParseError error;
    const QJsonDocument doc = QJsonDocument::fromJson(QByteArray::fromStdString(line), &error);
    ASSERT_EQ(QJsonParseError::NoError, error.error);
    ASSERT_TRUE(doc.isObject());
    return doc.object();
}

// The lines that call into telemetry in `relativePath`, outside the body of `helper` (whose own
// calls are reached only through a guarded call site). Each must carry the enabled() guard.
QStringList unguardedTelemetryCallsIn(const QString & source, const QString & relativePath,
                                      const QString & helper, int * guardedOut)
{
    int helperBegin = -1;
    int helperEnd = -1;
    if (!helper.isEmpty())
    {
        helperBegin = source.indexOf(QStringLiteral("void ") + helper + QStringLiteral("("));
        ASSERT_TRUE(helperBegin >= 0);
        helperEnd = source.indexOf(QStringLiteral("\n}\n"), helperBegin);
        ASSERT_TRUE(helperEnd > helperBegin);
    }
    QStringList bad;
    int guarded = 0;
    int lineStart = 0;
    int lineNo = 0;
    while (lineStart < source.size())
    {
        int lineEnd = source.indexOf(QLatin1Char('\n'), lineStart);
        if (lineEnd < 0) lineEnd = source.size();
        ++lineNo;
        const QString line = source.mid(lineStart, lineEnd - lineStart);
        const bool insideHelper = helperBegin >= 0 && lineStart >= helperBegin && lineStart < helperEnd;
        const bool callsTelemetry = line.contains(QStringLiteral("GpuFieldTelemetry::note"))
            || line.contains(QStringLiteral("GpuFieldTelemetry::flush"))
            || line.contains(QStringLiteral("GpuFieldTelemetry::install"))
            || (!helper.isEmpty() && line.contains(helper + QStringLiteral("("))
                && !line.trimmed().startsWith(QStringLiteral("void ")));
        if (callsTelemetry && !insideHelper)
        {
            if (line.contains(QStringLiteral("if ( GpuFieldTelemetry::enabled() ) "))) ++guarded;
            else bad << QStringLiteral("%1:%2").arg(relativePath).arg(lineNo);
        }
        lineStart = lineEnd + 1;
    }
    if (guardedOut) *guardedOut = guarded;
    return bad;
}

QStringList unguardedTelemetryCalls(const QString & relativePath, const QString & helper, int * guardedOut)
{
    return unguardedTelemetryCallsIn(readRepoFile(relativePath), relativePath, helper, guardedOut);
}

// `source` with comments and string / char literals blanked to spaces (offsets kept), so brace
// matching sees code only.
QString codeOnly(const QString & source)
{
    QString code = source;
    const int n = code.size();
    int i = 0;
    while (i < n)
    {
        const QChar c = code[i];
        const QChar next = i + 1 < n ? code[i + 1] : QChar();
        if (c == QLatin1Char('/') && next == QLatin1Char('/'))
        {
            while (i < n && code[i] != QLatin1Char('\n')) code[i++] = QLatin1Char(' ');
        }
        else if (c == QLatin1Char('/') && next == QLatin1Char('*'))
        {
            while (i < n && !(code[i] == QLatin1Char('*') && i + 1 < n && code[i + 1] == QLatin1Char('/')))
            {
                if (code[i] != QLatin1Char('\n')) code[i] = QLatin1Char(' ');
                ++i;
            }
            if (i < n) code[i++] = QLatin1Char(' ');
            if (i < n) code[i++] = QLatin1Char(' ');
        }
        else if (c == QLatin1Char('"') || c == QLatin1Char('\''))
        {
            ++i;
            while (i < n && code[i] != c && code[i] != QLatin1Char('\n'))
            {
                if (code[i] == QLatin1Char('\\') && i + 1 < n) code[i++] = QLatin1Char(' ');
                code[i++] = QLatin1Char(' ');
            }
            ++i;
        }
        else
        {
            ++i;
        }
    }
    return code;
}

// The innermost '{' still open at `pos`, or -1.
int enclosingOpen(const QString & code, int pos)
{
    int depth = 0;
    for (int i = pos - 1; i >= 0; --i)
    {
        if (code[i] == QLatin1Char('}')) ++depth;
        else if (code[i] == QLatin1Char('{'))
        {
            if (depth == 0) return i;
            --depth;
        }
    }
    return -1;
}

int matchingOpen(const QString & code, int close)
{
    return enclosingOpen(code, close);
}

// The statement head that opens the block at `open`: the code back to the previous ';', '{' or
// '}' (that position is returned in *stopOut), whitespace and macro line continuations folded.
QString blockHeader(const QString & code, int open, int * stopOut)
{
    int i = open - 1;
    while (i >= 0 && code[i] != QLatin1Char(';') && code[i] != QLatin1Char('{') && code[i] != QLatin1Char('}')) --i;
    if (stopOut) *stopOut = i;
    QString head = code.mid(i + 1, open - i - 1);
    head.replace(QLatin1Char('\\'), QLatin1Char(' '));
    return head.simplified();
}

bool isFailureTest(const QString & head)
{
    return (head.startsWith(QStringLiteral("if")) || head.startsWith(QStringLiteral("else if")))
        && (head.contains(QStringLiteral("!= cudaSuccess")) || head.contains(QStringLiteral("!=cudaSuccess")));
}

// GPU-FIELD-TELEMETRY-1 r3 (CENSUS-BRACE-SCOPE): a note is in a failure branch only when the block
// that holds it is opened by an rc != cudaSuccess test (directly, or through nested if-blocks), or
// is the else arm of an rc == cudaSuccess test, or the note is the body of a braceless
// `if (x != cudaSuccess)` on its own line. A note after an early-return check
// (`if (e != cudaSuccess) return ...; IGPU_CUDA_ERROR_NOTE(...)`) sits in the enclosing success
// path and is reported.
bool noteIsInFailureBranch(const QString & source, const QString & code, int pos)
{
    const int lineStart = source.lastIndexOf(QLatin1Char('\n'), pos) + 1;
    const QString before = code.mid(lineStart, pos - lineStart).trimmed();
    if (QRegularExpression(QStringLiteral("^if\\s*\\(.*!=\\s*cudaSuccess\\s*\\)$")).match(before).hasMatch()) return true;
    if (!before.isEmpty()) return false; // anything else before it on its line is not a branch head
    int open = enclosingOpen(code, pos);
    while (open >= 0)
    {
        int stop = -1;
        const QString head = blockHeader(code, open, &stop);
        if (head == QStringLiteral("else"))
        {
            if (stop < 0 || code[stop] != QLatin1Char('}')) return false;
            int ifStop = -1;
            const QString ifHead = blockHeader(code, matchingOpen(code, stop), &ifStop);
            return ifHead.startsWith(QStringLiteral("if")) && ifHead.contains(QStringLiteral("== cudaSuccess"));
        }
        if (isFailureTest(head)) return true;
        if (!head.startsWith(QStringLiteral("if")) && !head.startsWith(QStringLiteral("else if"))) return false;
        open = enclosingOpen(code, open);
    }
    return false;
}

// Every IGPU_CUDA_ERROR_NOTE( in `source` outside a failure branch, as label:line.
QStringList notesOutsideFailureBranch(const QString & source, const QString & label)
{
    const QString code = codeOnly(source);
    QStringList bad;
    const QString needle = QStringLiteral("IGPU_CUDA_ERROR_NOTE(");
    for (int pos = source.indexOf(needle); pos >= 0; pos = source.indexOf(needle, pos + 1))
    {
        if (!noteIsInFailureBranch(source, code, pos))
            bad << QStringLiteral("%1:%2").arg(label).arg(source.left(pos).count(QLatin1Char('\n')) + 1);
    }
    return bad;
}

// The whole failure census over the recon and AMaZE sources: exact note counts (15 recon: CK,
// CKN and 13 explicit branches; 22 AMaZE) and every note inside a failure branch.
constexpr int kReconNotes = 15;
constexpr int kAmazeNotes = 22;

QStringList failureCensus(const QString & recon, const QString & amaze)
{
    QStringList problems;
    const int reconNotes = (int)recon.count(QStringLiteral("IGPU_CUDA_ERROR_NOTE("));
    const int amazeNotes = (int)amaze.count(QStringLiteral("IGPU_CUDA_ERROR_NOTE("));
    if (reconNotes != kReconNotes) problems << QStringLiteral("recon notes %1 != %2").arg(reconNotes).arg(kReconNotes);
    if (amazeNotes != kAmazeNotes) problems << QStringLiteral("amaze notes %1 != %2").arg(amazeNotes).arg(kAmazeNotes);
    problems << notesOutsideFailureBranch(recon, QStringLiteral("recon"));
    problems << notesOutsideFailureBranch(amaze, QStringLiteral("amaze"));
    return problems;
}

QString replacedOnce(const QString & source, const QString & from, const QString & to)
{
    const int at = source.indexOf(from);
    ASSERT_TRUE(at >= 0);
    ASSERT_EQ(-1, source.indexOf(from, at + 1));
    QString out = source;
    out.replace(at, from.size(), to);
    return out;
}

std::uintmax_t bytesUnder(const std::filesystem::path & dir)
{
    std::uintmax_t total = 0;
    std::error_code ec;
    for (const auto & entry : std::filesystem::recursive_directory_iterator(dir, ec))
        if (entry.is_regular_file(ec)) total += entry.file_size(ec);
    return total;
}

core::StageEntry stageFrom(const std::vector<double> & ms, int errorsWithCode702)
{
    core::StageMap map;
    for (double v : ms) map.add("recon", "recon:run", v, 0);
    for (int i = 0; i < errorsWithCode702; ++i) map.add("recon", "recon:run", -1.0, 702);
    ASSERT_EQ(1, (int)map.entries().size());
    return map.entries().begin()->second;
}

} // namespace

TEST(GpuFieldTelemetry, EnvGateIsOnUnlessExplicitlyOff)
{
    struct Case { const char * value; bool on; };
    const Case cases[] = {
        {nullptr, true}, {"", true}, {"1", true}, {"on", true}, {"yes", true}, {"2", true},
        {"0", false}, {"off", false}, {"OFF", false}, {"false", false}, {"False", false},
        {"no", false}, {" 0 ", false}, {"no\n", false},
    };
    for (const Case & c : cases) ASSERT_EQ(c.on, core::parseEnabled(c.value));
    ASSERT_EQ(std::string("MLVAPP_GPU_TELEMETRY"), std::string(core::kEnvEnable));
}

TEST(GpuFieldTelemetry, DeviceLossCodes)
{
    struct Case { int code; bool loss; };
    const Case cases[] = {
        {700, true}, {702, true}, {719, true}, {999, true},
        {0, false}, {1, false}, {2, false}, {3, false}, {701, false}, {715, false},
    };
    for (const Case & c : cases) ASSERT_EQ(c.loss, core::isDeviceLossCode(c.code));
}

TEST(GpuFieldTelemetry, HistogramBucketsAreHalfOpenQuarterOctaves)
{
    // Bucket precision: bucket k >= 1 spans [0.01 * 2^((k-1)/4), 0.01 * 2^(k/4)) ms, a factor of
    // 2^(1/4) ~ 1.19; bucket 0 is everything under 0.01 ms and the last bucket everything from
    // ~119 s up. A value table, not a timing claim.
    struct Case { double ms; int bucket; };
    const Case cases[] = {
        {0.0, 0}, {0.005, 0}, {0.01, 1}, {0.0118, 1}, {0.0119, 2}, {1.0, 27},
        {600.0, 64}, {655.36, 65}, {2100.0, 71}, {200000.0, IGPU_LT_HIST_BUCKETS - 1},
    };
    for (const Case & c : cases) ASSERT_EQ(c.bucket, igpu_lt_hist_bucket(c.ms));
    ASSERT_NEAR(0.01, igpu_lt_hist_upper_ms(0), 1e-12);
    ASSERT_NEAR(655.36, igpu_lt_hist_upper_ms(64), 1e-9);
}

TEST(GpuFieldTelemetry, StageAggregateValueTable)
{
    // (inputs, expected) rows: exact count / max / over_500ms / errors / sum; p95 is the upper edge
    // of the bucket holding the nearest-rank sample, capped at the exact max, so the true p95 <=
    // reported < 1.19x the true p95.
    struct Row
    {
        std::vector<double> ms;
        int errors;
        unsigned long long count;
        double max;
        unsigned long long over500;
        double sum;
        double p95;
    };
    std::vector<double> hundred;
    for (int i = 1; i <= 100; ++i) hundred.push_back(double(i));
    std::vector<double> mostlyFast(18, 1.0);
    mostlyFast.push_back(600.0);
    mostlyFast.push_back(2100.0);
    const Row rows[] = {
        // one sample: p95 is capped to the exact max (its bucket edge is 2204.35)
        {{2100.0}, 0, 1, 2100.0, 1, 2100.0, 2100.0},
        // rank 19 of 20 = 600 ms -> bucket 64's upper edge 655.36
        {mostlyFast, 1, 21, 2100.0, 2, 2718.0, 655.36},
        // rank 95 of 100 = 95 ms -> bucket 53's upper edge 0.01 * 2^13.25
        {hundred, 0, 100, 100.0, 0, 5050.0, 97.41979},
        // 500 ms counts as over (>= the threshold), 499.999 does not
        {{499.999, 500.0}, 0, 2, 500.0, 1, 999.999, 500.0},
    };
    for (const Row & r : rows)
    {
        const core::StageEntry e = stageFrom(r.ms, r.errors);
        ASSERT_EQ(r.count, e.stats.count);
        ASSERT_NEAR(r.max, e.stats.max_ms, 1e-9);
        ASSERT_EQ(r.over500, e.stats.over_min);
        ASSERT_EQ((unsigned long long)r.errors, e.stats.errors);
        ASSERT_NEAR(r.sum, e.stats.sum_ms, 1e-6);
        ASSERT_NEAR(r.p95, core::stageP95(e), 1e-4);
    }
}

TEST(GpuFieldTelemetry, StageThatOnlyFaultedHasZeroTimes)
{
    const core::StageEntry e = stageFrom({}, 2);
    ASSERT_EQ(2ull, e.stats.count);
    ASSERT_EQ(2ull, e.stats.errors);
    ASSERT_EQ(0ull, e.hist.total);
    ASSERT_NEAR(0.0, core::stageP95(e), 1e-12);
    ASSERT_EQ(std::string("{\"schema\":\"gpu-field-telemetry.v1\",\"kind\":\"stage\",\"session_id\":\"s1\","
                          "\"utc\":\"2026-10-12T09:00:00.000Z\",\"dll\":\"recon\",\"stage\":\"recon:run\","
                          "\"count\":2,\"max_ms\":0.000,\"p95_ms\":0.000,\"over_500ms\":0,\"errors\":2,"
                          "\"sum_ms\":0.000}"),
              core::formatStage("s1", "2026-10-12T09:00:00.000Z", e));
}

TEST(GpuFieldTelemetry, RotationShiftsKeepsThreeAndReopensTheLiveFile)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const std::filesystem::path live =
        std::filesystem::path(dir.path().toStdWString()) / core::kLogFileName;
    const std::uintmax_t cap = 16; // every generation below is 21 bytes
    auto generation = [](int g) { return std::string("generation-") + std::to_string(g) + "-pad"; };

    ASSERT_FALSE(core::rotateIfNeeded(live, cap, 3)); // nothing there yet
    for (int g = 1; g <= 5; ++g)
    {
        if (g > 1) ASSERT_TRUE(core::rotateIfNeeded(live, cap, 3));
        ASSERT_TRUE(core::appendLines(live, {generation(g)}));
    }
    ASSERT_EQ(generation(5) + "\n", readFile(live));
    ASSERT_EQ(generation(4) + "\n", readFile(core::rotatedPath(live, 1)));
    ASSERT_EQ(generation(3) + "\n", readFile(core::rotatedPath(live, 2)));
    ASSERT_EQ(generation(2) + "\n", readFile(core::rotatedPath(live, 3)));
    ASSERT_FALSE(std::filesystem::exists(core::rotatedPath(live, 4))); // generation 1 was dropped

    // Under the cap: no rotation, the live file keeps growing.
    ASSERT_FALSE(core::rotateIfNeeded(live, 1024, 3));
    ASSERT_TRUE(core::appendLines(live, {"more"}));
    ASSERT_EQ(generation(5) + "\nmore\n", readFile(live));
    ASSERT_EQ(std::string("gpu-field-telemetry.jsonl"), std::string(core::kLogFileName));
    ASSERT_EQ(1048576ull, (unsigned long long)core::kRotateBytes);
    ASSERT_EQ(3, core::kRotateKeep);
}

TEST(GpuFieldTelemetry, SanitizeMapsSeparatorsQuotesAndControls)
{
    struct Case { const char * in; const char * out; };
    const Case cases[] = {
        {"NVIDIA GeForce RTX 3060 Laptop GPU/PCIe/SSE2", "NVIDIA GeForce RTX 3060 Laptop GPU|PCIe|SSE2"},
        {"C:\\Users\\someone\\clip.MLV", "C:|Users|someone|clip.MLV"},
        {"say \"hi\"\n\t", "say 'hi'  "},
        {nullptr, ""},
    };
    for (const Case & c : cases) ASSERT_EQ(std::string(c.out), core::sanitize(c.in));
    ASSERT_EQ(std::string("abc"), core::sanitize("abcdef", 3));
}

TEST(GpuFieldTelemetry, IdentityParsers)
{
    ASSERT_EQ(std::string("610.74"), core::driverFromGlVersion("4.6.0 NVIDIA 610.74"));
    ASSERT_EQ(std::string("4.6 - Build 31.0.101.5186"), core::driverFromGlVersion("4.6 - Build 31.0.101.5186"));
    ASSERT_EQ(std::string(""), core::driverFromGlVersion(nullptr));
    ASSERT_EQ(std::string("NVIDIA GeForce RTX 3060 Laptop GPU"),
              core::gpuModelFromCudaDescribe("CUDA / NVIDIA GeForce RTX 3060 Laptop GPU"));
    ASSERT_EQ(std::string("NVIDIA GeForce RTX 4090"), core::gpuModelFromCudaDescribe("CUDA AMaZE / NVIDIA GeForce RTX 4090"));
    ASSERT_EQ(std::string("fake-test-backend (no GPU, C-seam only)"),
              core::gpuModelFromCudaDescribe("fake-test-backend (no GPU, C-seam only)"));
    ASSERT_EQ(std::string("unsupported"), core::glResetStrategyName(false, 0x8252));
    ASSERT_EQ(std::string("lose_context"), core::glResetStrategyName(true, 0x8252));
    ASSERT_EQ(std::string("none"), core::glResetStrategyName(true, 0x8261));
    ASSERT_EQ(std::string("unknown"), core::glResetStrategyName(true, 0));
    ASSERT_EQ(std::string("no_error"), core::glResetStatusName(0));
    ASSERT_EQ(std::string("guilty_context_reset"), core::glResetStatusName(0x8253));
    ASSERT_EQ(std::string("innocent_context_reset"), core::glResetStatusName(0x8254));
    ASSERT_EQ(std::string("unknown_context_reset"), core::glResetStatusName(0x8255));
}

TEST(GpuFieldTelemetry, LinesCarryTheFieldsAndNoPrivateData)
{
    core::SessionInfo s;
    s.sessionId = "0123456789abcdef";
    s.startUtc = "2026-10-12T09:00:00.000Z";
    s.appBuild = "1.15.0.0+6be0df82ae54";
    s.osBuild = "10.0.26100";
    s.gpuModel = "NVIDIA GeForce RTX 3060 Laptop GPU";
    s.gpuDriver = core::driverFromGlVersion("4.6.0 NVIDIA 610.74");
    s.glRenderer = core::sanitize("NVIDIA GeForce RTX 3060 Laptop GPU/PCIe/SSE2");
    s.glVendor = "NVIDIA Corporation";
    s.glVersion = "4.6.0 NVIDIA 610.74";
    s.glResetStrategy = core::glResetStrategyName(false, 0);
    s.glResetStatus = "not_queried";
    s.launchTimingEnv = "enabled=unset min_ms=unset";
    core::ClipInfo clip;
    clip.width = 1920;
    clip.height = 804;
    clip.bitsPerPixel = 14;
    clip.compressed = true;
    clip.dualIsoGpu = true;
    clip.clipHash = "a1b2c3d4e5f6";
    core::Fault f;
    f.dll = "recon";
    f.stage = "recon:run";
    f.rc = -1;
    f.cudaCode = 702;
    f.cudaName = "cudaErrorLaunchTimeout";
    f.site = core::sanitize("IGPU_LT_SYNC_CALL(\"run:ev_after_dl\", cudaEventSynchronize(b->ev_after_dl)) /x\\y", 95);
    f.utcMs = 1791774588083.125;
    core::SystemLogResult sys;
    sys.status = "ok";
    sys.nvlddmkm153.count = 1;
    sys.nvlddmkm153.firstUtc = sys.nvlddmkm153.lastUtc = "2026-10-11T07:09:48.1234567Z";
    const std::string utc = "2026-10-12T09:00:01.000Z";

    const std::vector<std::string> lines = {
        core::formatHeader(s, utc),
        core::formatFlush(s.sessionId, utc, "after_fault", 2, 0),
        core::formatClip(s.sessionId, utc, clip),
        core::formatFault(s.sessionId, utc, f),
        core::formatSystemLog(s.sessionId, utc, "after_fault", sys),
        core::formatRecovery(s.sessionId, utc, "cuda_context", "reset_ok"),
    };
    const QRegularExpression privateKey(QStringLiteral("\"[a-z_]*(path|user|host|serial|message)[a-z_]*\":"));
    for (const std::string & line : lines)
    {
        ASSERT_EQ(std::string::npos, line.find('/'));
        ASSERT_EQ(std::string::npos, line.find('\\'));
        ASSERT_FALSE(QString::fromStdString(line).contains(privateKey));
        const QJsonObject o = parseLine(line);
        ASSERT_EQ(std::string("gpu-field-telemetry.v1"), o.value(QStringLiteral("schema")).toString().toStdString());
        ASSERT_EQ(std::string("0123456789abcdef"), o.value(QStringLiteral("session_id")).toString().toStdString());
    }

    const QJsonObject header = parseLine(lines[0]);
    const QStringList headerKeys = {
        "schema", "kind", "session_id", "utc", "start_utc", "app_build", "os_build", "gpu_model",
        "gpu_driver", "gl_renderer", "gl_vendor", "gl_version", "gl_reset_strategy", "gl_reset_status",
        "launch_timing_env", "upload"};
    ASSERT_EQ(headerKeys.size(), header.keys().size());
    for (const QString & k : headerKeys) ASSERT_TRUE(header.contains(k));
    ASSERT_EQ(std::string("none"), header.value(QStringLiteral("upload")).toString().toStdString());
    ASSERT_EQ(std::string("610.74"), header.value(QStringLiteral("gpu_driver")).toString().toStdString());
    ASSERT_EQ(std::string("unsupported"), header.value(QStringLiteral("gl_reset_strategy")).toString().toStdString());

    ASSERT_EQ(std::string("{\"schema\":\"gpu-field-telemetry.v1\",\"kind\":\"clip\",\"session_id\":\"0123456789abcdef\","
                          "\"utc\":\"2026-10-12T09:00:01.000Z\",\"width\":1920,\"height\":804,\"bits_per_pixel\":14,"
                          "\"compressed\":true,\"dual_iso_gpu\":true,\"clip_hash\":\"a1b2c3d4e5f6\"}"),
              lines[2]);
    const QJsonObject fault = parseLine(lines[3]);
    ASSERT_EQ(std::string("recon"), fault.value(QStringLiteral("dll")).toString().toStdString());
    ASSERT_EQ(std::string("recon:run"), fault.value(QStringLiteral("stage")).toString().toStdString());
    ASSERT_EQ(-1, fault.value(QStringLiteral("rc")).toInt());
    ASSERT_EQ(702, fault.value(QStringLiteral("cuda_code")).toInt());
    ASSERT_EQ(std::string("cudaErrorLaunchTimeout"), fault.value(QStringLiteral("cuda_name")).toString().toStdString());
    ASSERT_TRUE(fault.value(QStringLiteral("site")).toString().startsWith(QStringLiteral("IGPU_LT_SYNC_CALL('run:ev_after_dl'")));
    ASSERT_TRUE(fault.value(QStringLiteral("device_loss")).toBool());
    const QJsonObject sysLine = parseLine(lines[4]);
    ASSERT_EQ(std::string("ok"), sysLine.value(QStringLiteral("status")).toString().toStdString());
    ASSERT_EQ(1, sysLine.value(QStringLiteral("nvlddmkm_153")).toObject().value(QStringLiteral("count")).toInt());
    ASSERT_EQ(0, sysLine.value(QStringLiteral("display_4101")).toObject().value(QStringLiteral("count")).toInt());
}

TEST(GpuFieldTelemetry, EveryCallSiteIsGuardedByEnabled)
{
    struct Site { const char * file; const char * helper; int guarded; };
    const Site sites[] = {
        {"platform/qt/GpuDebayer.cpp", "noteAmazeStageTelemetry", 4},
        {"platform/qt/GpuDisplayViewport.cpp", "", 1},
        {"platform/qt/GpuDisplayWindow.cpp", "", 1},
        {"platform/qt/MainWindow.cpp", "", 1},
    };
    for (const Site & site : sites)
    {
        int guarded = 0;
        const QStringList bad = unguardedTelemetryCalls(QString::fromLatin1(site.file), QString::fromLatin1(site.helper), &guarded);
        ASSERT_EQ(std::string(), bad.join(QStringLiteral("; ")).toStdString());
        ASSERT_EQ(site.guarded, guarded);
    }
    // The census sees a mutant: an unguarded call (direct, or of the helper) is reported.
    const QString debayer = readRepoFile(QStringLiteral("platform/qt/GpuDebayer.cpp"));
    const QString label = QStringLiteral("debayer-mutant");
    for (const char * added : {"\nstatic void m() { GpuFieldTelemetry::noteStage(\"amaze\", \"x\", 1.0); }\n",
                               "\nstatic void m() { noteAmazeStageTelemetry(nullptr, nullptr, \"x\", 0); }\n"})
    {
        int guarded = 0;
        const QStringList bad = unguardedTelemetryCallsIn(debayer + QString::fromLatin1(added), label,
                                                          QStringLiteral("noteAmazeStageTelemetry"), &guarded);
        ASSERT_EQ(1, (int)bad.size());
        ASSERT_EQ(4, guarded);
    }
    ASSERT_TRUE(debayer.contains(QStringLiteral("if ( GpuFieldTelemetry::enabled() ) noteAmazeStageTelemetry(&live.runtime, live.backend, \"amaze:r16_gl\", rc);")));
    // The AMaZE error symbol is optional: not in the required-symbol chain.
    ASSERT_FALSE(debayer.contains(QStringLiteral("resolveAmazeDebayerSymbol(&runtime->library,\n                                     \"igpu_amaze_debayer_last_cuda_error\"")));
    ASSERT_TRUE(debayer.contains(QStringLiteral("runtime->library.resolve(\"igpu_amaze_debayer_last_cuda_error\")")));
}

TEST(GpuFieldTelemetry, GateIsAFunctionLocalStaticAndOffReturnsFirst)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuFieldTelemetry.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "bool enabled()\n{\n    static const bool on = GpuFieldTelemetryCore::parseEnabled(std::getenv(GpuFieldTelemetryCore::kEnvEnable));\n    return on;\n}")));
    for (const char * entry : {"void install()\n{\n    if (!enabled()) return;",
                               "void noteStage(const char * dll, const char * stage, double ms)\n{\n    if (!enabled()) return;",
                               "void noteFault(const CudaFault & fault)\n{\n    if (!enabled()) return;",
                               "void noteRecovery(const char * what, const char * outcome)\n{\n    if (!enabled()) return;",
                               "void flush(const char * reason)\n{\n    if (!enabled()) return;",
                               "void noteGlContext(QOpenGLContext * context)\n{\n    if (!enabled() || !context) return;"})
    {
        ASSERT_TRUE(source.contains(QString::fromLatin1(entry)));
    }
    // The worker pool exists only once a fault or recovery asks for it; wevtapi is loaded at runtime,
    // from System32 only (r3 H2: never the application directory; WevtapiLoadsFromSystem32Only).
    ASSERT_TRUE(source.contains(QStringLiteral("LoadLibraryExW(L\"wevtapi.dll\", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32)")));
    ASSERT_FALSE(readRepoFile(QStringLiteral("platform/qt/MLVApp.pro")).contains(QStringLiteral("wevtapi")));
}

TEST(GpuFieldTelemetry, NeverNvmlNorNvidiaSmiNorMachineFingerprint)
{
    for (const char * file : {"platform/qt/GpuFieldTelemetry.h", "platform/qt/GpuFieldTelemetry.cpp",
                              "platform/qt/GpuFieldTelemetryCore.h"})
    {
        const QString source = readRepoFile(QString::fromLatin1(file)).toLower();
        ASSERT_FALSE(source.contains(QStringLiteral("nvidia-smi")));
        ASSERT_FALSE(source.contains(QStringLiteral("nvml")));
        ASSERT_FALSE(source.contains(QStringLiteral("machinefingerprint")));
    }
}

TEST(GpuFieldTelemetry, CudaFailureBranchesNoteTheCodeAndAddNoSync)
{
    const QString recon = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_recon_cuda.cu"));
    const QString amaze = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_amaze_debayer_cuda.cu"));
    const QString probe = readRepoFile(QStringLiteral("tools/gpu/cuda_amaze_debayer_stage_probe.cu"));
    // CK / CKN note inside the failure branch, before the existing fprintf and return.
    ASSERT_TRUE(recon.contains(QStringLiteral(
        "#define CK(call) do { cudaError_t _e=(call); if(_e!=cudaSuccess){ \\\n    IGPU_CUDA_ERROR_NOTE(_e, #call); \\\n"
        "    fprintf(stderr,\"[igpu_recon_cuda] CUDA error %s @ %s:%d: %s\\n\",#call,__FILE__,__LINE__, \\\n"
        "    cudaGetErrorString(_e)); return -1;} } while(0)")));
    ASSERT_TRUE(recon.contains(QStringLiteral(
        "#define CKN(call) do { cudaError_t _e=(call); if(_e!=cudaSuccess){ \\\n    IGPU_CUDA_ERROR_NOTE(_e, #call); \\\n")));
    // check_cuda: success returns first; the note sits on the library throw path only.
    ASSERT_TRUE(probe.contains(QStringLiteral(
        "    if (rc == cudaSuccess) return;\n#ifdef CUDA_AMAZE_DEBAYER_STAGE_PROBE_LIBRARY\n"
        "    IGPU_CUDA_ERROR_NOTE(rc, call);\n    throw CudaStageProbeError(")));
    // The explicit rc branches (map / copy / unmap / register / unregister) note too.
    for (const char * note : {"IGPU_CUDA_ERROR_NOTE(e, \"gl_r16:map\");",
                              "IGPU_CUDA_ERROR_NOTE(e, \"gl_r16:copy_to_array\");",
                              "IGPU_CUDA_ERROR_NOTE(unmap_error, \"gl_r16:unmap\");"})
        ASSERT_TRUE(recon.contains(QString::fromLatin1(note)));
    for (const char * note : {"IGPU_CUDA_ERROR_NOTE(rc, \"gl_r16_in:map\");",
                              "IGPU_CUDA_ERROR_NOTE(unmapRc, \"gl_rgba16_post_wb:unmap\");",
                              "IGPU_CUDA_ERROR_NOTE(rc, \"gl_rgba16_direct:copy_to_array\");"})
        ASSERT_TRUE(amaze.contains(QString::fromLatin1(note)));
    ASSERT_EQ(22, (int)amaze.count(QStringLiteral("IGPU_CUDA_ERROR_NOTE(")));
    ASSERT_TRUE(recon.count(QStringLiteral("IGPU_CUDA_ERROR_NOTE(")) >= 14);
    // r3: the exact counts and the brace-scope check (FailureCensusIsExactAndBraceScoped).
    ASSERT_EQ(std::string(), failureCensus(recon, amaze).join(QStringLiteral("; ")).toStdString());
    // Every note is in a failure branch: the line before it opens an rc/e != cudaSuccess branch,
    // or is one of the CK/CKN macro heads, or is the sync failure test itself.
    for (const QString * src : {&recon, &amaze})
    {
        const QStringList lines = src->split(QLatin1Char('\n'));
        for (int i = 0; i < lines.size(); ++i)
        {
            if (!lines[i].contains(QStringLiteral("IGPU_CUDA_ERROR_NOTE(")) || lines[i].contains(QStringLiteral("#define"))) continue;
            if (lines[i].contains(QStringLiteral("if (sync != cudaSuccess) IGPU_CUDA_ERROR_NOTE("))) continue;
            QString context;
            for (int back = 1; back <= 4 && i - back >= 0; ++back) context += lines[i - back];
            const bool inFailure = context.contains(QStringLiteral("!= cudaSuccess")) || context.contains(QStringLiteral("!=cudaSuccess"))
                || context.contains(QStringLiteral("} else {"));
            ASSERT_TRUE(inFailure);
        }
    }
    // The slot is host-only: no CUDA runtime call, no synchronisation.
    const QString slot = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_cuda_error_slot.h"));
    ASSERT_FALSE(slot.contains(QRegularExpression(QStringLiteral("\\bcuda\\w*Synchronize\\s*\\("))));
    ASSERT_FALSE(slot.contains(QRegularExpression(QStringLiteral("\\bcuda(Malloc|Memcpy|Event|Stream|Device)\\w*\\s*\\("))));
    // Both DLLs export the optional reader.
    ASSERT_TRUE(readRepoFile(QStringLiteral("tools/gpu/backend/igpu_recon_cuda.def")).contains(QStringLiteral("    igpu_recon_last_cuda_error")));
    ASSERT_TRUE(readRepoFile(QStringLiteral("tools/gpu/backend/igpu_amaze_debayer_cuda.def")).contains(QStringLiteral("    igpu_amaze_debayer_last_cuda_error")));
}

TEST(GpuFieldTelemetry, LaunchTimingRecorderIsUntouched)
{
    // CUDA-LT-TIMING-HARDENING-1 owns igpu_launch_timing.cuh: byte-identical (line endings
    // normalised) to base 6be0df82ae5406dc4f33e02d6e1f80af7e084670.
    const QString path = repo_file_path(QStringLiteral("tools/gpu/backend/igpu_launch_timing.cuh"));
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly));
    QByteArray bytes = file.readAll();
    bytes.replace("\r", "");
    ASSERT_EQ(std::string("b5e89abf94802e68893ef21462b50c378289fe2c2260ad329e9b0a7ddf316291"),
              QCryptographicHash::hash(bytes, QCryptographicHash::Sha256).toHex().toStdString());
}

// r3 B1 (sol): a failed rotation must not let the live file grow. Every generation slot is a
// non-empty directory, so the remove and every rename fail on any platform; writeBounded then
// appends nothing once the live file is at the cap, and the dropped flushes are reported.
TEST(GpuFieldTelemetry, FailedRotationNeverGrowsTheLiveFilePastTheCap)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const std::filesystem::path root(dir.path().toStdWString());
    const std::filesystem::path live = root / core::kLogFileName;
    const std::uintmax_t cap = 256;
    const int keep = 3;
    for (int g = 1; g <= keep; ++g)
    {
        std::error_code ec;
        std::filesystem::create_directory(core::rotatedPath(live, g), ec);
        ASSERT_FALSE((bool)ec);
        ASSERT_TRUE(core::appendLines(core::rotatedPath(live, g) / "pin", {"x"}));
    }
    const std::vector<std::string> flush(4, std::string(39, 'f')); // 160 bytes per flush
    const std::uintmax_t flushBytes = 160;
    const std::uintmax_t pinned = bytesUnder(root);

    int written = 0;
    int dropped = 0;
    for (int i = 0; i < 200; ++i)
    {
        const core::WriteOutcome outcome = core::writeBounded(live, flush, cap, keep);
        if (outcome == core::WriteOutcome::Written) ++written;
        else if (outcome == core::WriteOutcome::DroppedRotationFailed) ++dropped;
        std::error_code ec;
        ASSERT_TRUE(std::filesystem::file_size(live, ec) < cap + flushBytes);
        ASSERT_TRUE(bytesUnder(root) - pinned < cap + flushBytes);
    }
    ASSERT_EQ(2, written);  // 0 -> 160 -> 320 bytes, then the cap holds
    ASSERT_EQ(198, dropped);
    ASSERT_EQ(320ull, (unsigned long long)std::filesystem::file_size(live));

    // The dropped lines are counted and reported by the next flush line that is written.
    core::Unwritten u;
    core::Taken t;
    core::commit(u, t, core::WriteOutcome::DroppedRotationFailed, 7);
    core::commit(u, t, core::WriteOutcome::DroppedRotationFailed, 5);
    core::commit(u, t, core::WriteOutcome::WriteFailed, 9); // a failed write is not a rotation drop
    ASSERT_EQ(12ull, u.droppedRotationFailed);
    const QJsonObject line = parseLine(core::formatFlush("s1", "2026-10-12T12:00:00.000Z", "after_fault", 3, 0,
                                                         u.droppedRotationFailed));
    ASSERT_EQ(12, line.value(QStringLiteral("dropped_rotation_failed")).toInt());

    // Once a generation can move again, the next flush rotates and writes a fresh live file.
    std::error_code ec;
    std::filesystem::remove_all(core::rotatedPath(live, keep), ec);
    std::filesystem::remove_all(core::rotatedPath(live, keep - 1), ec);
    std::filesystem::remove_all(core::rotatedPath(live, 1), ec);
    ASSERT_EQ(core::WriteOutcome::Written, core::writeBounded(live, flush, cap, keep));
    ASSERT_EQ((unsigned long long)flushBytes, (unsigned long long)std::filesystem::file_size(live));
    ASSERT_EQ(320ull, (unsigned long long)std::filesystem::file_size(core::rotatedPath(live, 1)));
}

#ifdef _WIN32
// r3 B1, sol's own repro: generation .1 held open without FILE_SHARE_DELETE (an antivirus or a
// log viewer). The shift and the live rename fail; the live file still never passes the cap.
TEST(GpuFieldTelemetry, GenerationHeldWithoutShareDeleteKeepsTheLiveFileBounded)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const std::filesystem::path root(dir.path().toStdWString());
    const std::filesystem::path live = root / core::kLogFileName;
    const std::uintmax_t cap = 256;
    ASSERT_TRUE(core::appendLines(core::rotatedPath(live, 1), {"held"}));
    HANDLE held = CreateFileW(core::rotatedPath(live, 1).wstring().c_str(), GENERIC_READ,
                              FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                              FILE_ATTRIBUTE_NORMAL, nullptr);
    ASSERT_TRUE(held != INVALID_HANDLE_VALUE);
    const std::vector<std::string> flush(4, std::string(39, 'f'));
    int dropped = 0;
    for (int i = 0; i < 100; ++i)
    {
        if (core::writeBounded(live, flush, cap, 3) == core::WriteOutcome::DroppedRotationFailed) ++dropped;
        ASSERT_TRUE(std::filesystem::file_size(live) < cap + 160);
    }
    ASSERT_EQ(98, dropped);
    CloseHandle(held);
    ASSERT_EQ(core::WriteOutcome::Written, core::writeBounded(live, flush, cap, 3));
    ASSERT_EQ(160ull, (unsigned long long)std::filesystem::file_size(live));
}
#endif

// r3 H1 (sol PERSISTENCE-FAILURE + B1): a dropped or failed write keeps the faults, the extra lines
// and the clip for the next flush (still capped at 64 each); a successful write removes exactly
// what it took, so a fault noted between take and commit survives.
TEST(GpuFieldTelemetry, FailedWriteKeepsFaultsLinesAndClipForTheNextFlush)
{
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    const std::filesystem::path good = std::filesystem::path(dir.path().toStdWString()) / core::kLogFileName;
    const std::filesystem::path unwritable =
        std::filesystem::path(dir.path().toStdWString()) / "missing-dir" / core::kLogFileName;

    core::Unwritten u;
    core::ClipInfo clip;
    clip.width = 1808;
    clip.height = 2268;
    clip.clipHash = "a3d03684e47a";
    u.setClip(clip);
    for (int i = 0; i < 3; ++i)
    {
        core::Fault f;
        f.dll = "recon";
        f.stage = "recon:preupload";
        f.rc = -1;
        f.cudaCode = 2;
        u.addFault(f);
    }
    u.addLine("recovery-1");

    std::vector<std::string> lines;
    core::Taken t = core::takeInto(u, "s1", "utc", lines);
    ASSERT_EQ(5, (int)lines.size()); // clip, 3 faults, 1 recovery line
    ASSERT_EQ(core::WriteOutcome::WriteFailed, core::writeBounded(unwritable, lines, core::kRotateBytes, 3));
    core::commit(u, t, core::WriteOutcome::WriteFailed, lines.size());
    ASSERT_EQ(3, (int)u.faults.size());
    ASSERT_EQ(1, (int)u.lines.size());
    ASSERT_TRUE(u.clipSeq != u.clipWrittenSeq);

    // A rotation drop keeps them too.
    core::commit(u, t, core::WriteOutcome::DroppedRotationFailed, lines.size());
    ASSERT_EQ(3, (int)u.faults.size());
    ASSERT_EQ(5ull, u.droppedRotationFailed);

    // Still bounded while the disk stays bad: 64 kept, the rest counted.
    core::Unwritten capped = u;
    for (int i = 0; i < 70; ++i) capped.addFault(core::Fault());
    for (int i = 0; i < 70; ++i) capped.addLine("recovery-n");
    ASSERT_EQ(64, (int)capped.faults.size());
    ASSERT_EQ(9ull, capped.faultsDropped);
    ASSERT_EQ(64, (int)capped.lines.size());

    // The retry writes them; one fault noted between take and commit stays for the next flush.
    lines.clear();
    t = core::takeInto(u, "s1", "utc", lines);
    ASSERT_EQ(5, (int)lines.size());
    core::Fault late;
    late.stage = "recon:run";
    u.addFault(late);
    ASSERT_EQ(core::WriteOutcome::Written, core::writeBounded(good, lines, core::kRotateBytes, 3));
    core::commit(u, t, core::WriteOutcome::Written, lines.size());
    ASSERT_EQ(1, (int)u.faults.size());
    ASSERT_EQ(std::string("recon:run"), u.faults[0].stage);
    ASSERT_EQ(0, (int)u.lines.size());
    ASSERT_EQ(u.clipSeq, u.clipWrittenSeq);
    lines.clear();
    core::takeInto(u, "s1", "utc", lines);
    ASSERT_EQ(1, (int)lines.size()); // only the late fault: the clip is not written twice
    ASSERT_TRUE(readFile(good).find("\"clip_hash\":\"a3d03684e47a\"") != std::string::npos);
}

// r3 H1, product wiring: flushImpl takes before the write and commits the outcome after it.
TEST(GpuFieldTelemetry, FlushCommitsOnlyWhatTheWriteKept)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuFieldTelemetry.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("taken = core::takeInto(s.unwritten, id, utc, lines);")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "const core::WriteOutcome outcome = core::writeBounded(live, lines, core::kRotateBytes, core::kRotateKeep);\n"
        "    QMutexLocker locker(&s.mutex);\n"
        "    core::commit(s.unwritten, taken, outcome, lines.size());")));
    ASSERT_FALSE(source.contains(QStringLiteral("core::rotateIfNeeded(")));
    ASSERT_FALSE(source.contains(QStringLiteral("core::appendLines(")));
    ASSERT_FALSE(source.contains(QStringLiteral(".clear();")));
}

// r3 H2 (fable WEVTAPI-SYSTEM32): wevtapi.dll only from System32, never the default search order.
TEST(GpuFieldTelemetry, WevtapiLoadsFromSystem32Only)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuFieldTelemetry.cpp"));
    ASSERT_EQ(1, (int)source.count(QStringLiteral("LoadLibraryExW(L\"wevtapi.dll\", nullptr, LOAD_LIBRARY_SEARCH_SYSTEM32)")));
    ASSERT_FALSE(source.contains(QStringLiteral("LoadLibraryW(")));
    ASSERT_FALSE(source.contains(QStringLiteral("LoadLibraryA(")));
}

// r3 H3 (sol CENSUS + fable CENSUS-BRACE-SCOPE): the census pins the exact counts and the brace
// scope, and each check turns red on its mutation.
TEST(GpuFieldTelemetry, FailureCensusIsExactAndBraceScoped)
{
    const QString recon = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_recon_cuda.cu"));
    const QString amaze = readRepoFile(QStringLiteral("tools/gpu/backend/igpu_amaze_debayer_cuda.cu"));
    ASSERT_EQ(kReconNotes, (int)recon.count(QStringLiteral("IGPU_CUDA_ERROR_NOTE(")));
    ASSERT_EQ(kAmazeNotes, (int)amaze.count(QStringLiteral("IGPU_CUDA_ERROR_NOTE(")));
    ASSERT_EQ(std::string(), failureCensus(recon, amaze).join(QStringLiteral("; ")).toStdString());

    // Mutation 1 (sol's replay): drop the preupload:submit note -> recon count 14.
    const QString noSubmit = replacedOnce(recon, QStringLiteral("        IGPU_CUDA_ERROR_NOTE(e, \"preupload:submit\");\n"), QString());
    ASSERT_EQ(std::string("recon notes 14 != 15"), failureCensus(noSubmit, amaze).join(QStringLiteral("; ")).toStdString());
    // Mutation 2: drop one AMaZE note -> 21.
    const QString noMap = replacedOnce(amaze, QStringLiteral("        IGPU_CUDA_ERROR_NOTE(rc, \"gl_r16_in:map\");\n"), QString());
    ASSERT_EQ(std::string("amaze notes 21 != 22"), failureCensus(recon, noMap).join(QStringLiteral("; ")).toStdString());
    // Mutation 3 (fable's repro): the note moved after an early-return failure check, into the
    // success path. Same count; the old 4-line context check accepted it.
    const QString earlyReturn = replacedOnce(recon,
        QStringLiteral("    if (e != cudaSuccess) {\n        IGPU_CUDA_ERROR_NOTE(e, \"preupload:malloc_host\");\n        return -1;\n    }\n"),
        QStringLiteral("    if (e != cudaSuccess) return -1;\n    IGPU_CUDA_ERROR_NOTE(e, \"preupload:malloc_host\");\n"));
    const QStringList earlyProblems = failureCensus(earlyReturn, amaze);
    ASSERT_EQ(1, (int)earlyProblems.size());
    ASSERT_TRUE(earlyProblems[0].startsWith(QStringLiteral("recon:")));
    // Mutation 4: the same on one line.
    const QString oneLine = replacedOnce(recon,
        QStringLiteral("    if (e != cudaSuccess) {\n        IGPU_CUDA_ERROR_NOTE(e, \"preupload:malloc_host\");\n        return -1;\n    }\n"),
        QStringLiteral("    if (e != cudaSuccess) return -1; IGPU_CUDA_ERROR_NOTE(e, \"preupload:malloc_host\");\n"));
    ASSERT_EQ(1, (int)failureCensus(oneLine, amaze).size());
    // Mutation 5: an AMaZE note after its block closes.
    const QString amazeAfter = replacedOnce(amaze,
        QStringLiteral("    if (rc != cudaSuccess)\n    {\n        IGPU_CUDA_ERROR_NOTE(rc, \"gl_r16_in:map\");\n"),
        QStringLiteral("    IGPU_CUDA_ERROR_NOTE(rc, \"gl_r16_in:map\");\n    if (rc != cudaSuccess)\n    {\n"));
    ASSERT_EQ(1, (int)failureCensus(recon, amazeAfter).size());
    // Mutation 6: a success-arm note (the if tests == cudaSuccess, the note is not in its else).
    const QString successArm = replacedOnce(recon,
        QStringLiteral("    if (e == cudaSuccess) {\n        slot->bytes = input_bytes;\n"),
        QStringLiteral("    if (e == cudaSuccess) {\n        IGPU_CUDA_ERROR_NOTE(e, \"preupload:success_arm\");\n        slot->bytes = input_bytes;\n"));
    ASSERT_TRUE(failureCensus(successArm, amaze).join(QStringLiteral("; ")).contains(QStringLiteral("recon notes 16 != 15")));
    ASSERT_EQ(2, (int)failureCensus(successArm, amaze).size());
}

// r3 H4 (both keys, exit join): the System-log read ends inside the session-end wait, quit is held
// for at most ~2 s, the worker stops before each step, and State outlives a worker session end
// gave up on. EvtNext waits are a value table, not a timing claim.
TEST(GpuFieldTelemetry, SystemLogDeadlineEndsInsideTheSessionEndWait)
{
    ASSERT_TRUE(core::kSystemLogDeadlineMs < core::kSessionEndWaitMs);
    ASSERT_TRUE(core::kSessionEndWaitMs <= 2000);
    struct Case { long long remaining; unsigned long wait; };
    const Case cases[] = {{-1, 0}, {0, 0}, {1, 1}, {999, 999}, {1000, 1000}, {1500, 1000}, {60000, 1000}};
    for (const Case & c : cases) ASSERT_EQ(c.wait, core::evtNextWaitMs(c.remaining));
}

QStringList exitJoinCensus(const QString & source)
{
    QStringList missing;
    const char * required[] = {
        // State and the write mutex are never destroyed.
        "State & state()\n{\n    static State * s = new State();\n    return *s;\n}",
        "    static QMutex * m = new QMutex();",
        // Session end sets the stop flag, waits once, and skips the final write if the wait expired.
        "    g_workerStop.store(true);",
        "    if (g_workerPoolCreated.load() && !workerPool()->waitForDone(static_cast<int>(core::kSessionEndWaitMs)))\n        return;",
        "    flushImpl(\"session_end\", true, false,\n              remainingMs < core::kSystemLogDeadlineMs ? remainingMs : core::kSystemLogDeadlineMs);",
        // The worker checks the flag before each step.
        "        if (!waitUnlessShuttingDown(delayMs)) return; // session_end writes it instead\n        if (workerStopped(true)) return;",
        "        flushImpl(\"after_fault\", true, true);",
        "[]() { flushImpl(\"recovery\", false, true); }",
        // The System-log read is bounded by its deadline and stops at session end.
        "        const DWORD waitMs = core::evtNextWaitMs(deadline.remainingTime());",
        "        if (workerStopped(onWorker))\n        {\n            r.status = \"interrupted\";",
    };
    for (const char * r : required)
        if (!source.contains(QString::fromLatin1(r))) missing << QString::fromLatin1(r).left(60);
    if (source.count(QStringLiteral("    if (workerStopped(onWorker)) return;")) != 3)
        missing << QStringLiteral("flushImpl stop checks != 3");
    if (source.contains(QStringLiteral("static State s;"))) missing << QStringLiteral("function-static State");
    return missing;
}

TEST(GpuFieldTelemetry, SessionEndJoinsTheWorkerOrSkipsTheFinalWrite)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuFieldTelemetry.cpp"));
    ASSERT_EQ(std::string(), exitJoinCensus(source).join(QStringLiteral("; ")).toStdString());
    // Mutations: the r1 shape of each piece is reported.
    const QString ignoredWait = replacedOnce(source,
        QStringLiteral("    if (g_workerPoolCreated.load() && !workerPool()->waitForDone(static_cast<int>(core::kSessionEndWaitMs)))\n        return;"),
        QStringLiteral("    if (g_workerPoolCreated.load()) workerPool()->waitForDone(static_cast<int>(core::kSessionEndWaitMs));"));
    ASSERT_EQ(1, (int)exitJoinCensus(ignoredWait).size());
    const QString staticState = replacedOnce(source,
        QStringLiteral("    static State * s = new State();\n    return *s;"), QStringLiteral("    static State s;\n    return s;"));
    ASSERT_EQ(2, (int)exitJoinCensus(staticState).size());
    const QString noStepCheck = replacedOnce(source,
        QStringLiteral("    const std::string cudaModel = reconDescribe();\n    if (workerStopped(onWorker)) return;\n"),
        QStringLiteral("    const std::string cudaModel = reconDescribe();\n"));
    ASSERT_EQ(1, (int)exitJoinCensus(noStepCheck).size());
}
