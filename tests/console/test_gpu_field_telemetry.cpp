// GPU-FIELD-TELEMETRY-1: the local GPU field log. Pins (1) the per-stage aggregate -- exact count,
// max, over_500ms, errors and sum, and p95 from the launch timer's fixed log-bucket histogram, over
// (input, expected) value tables rather than timing claims; (2) rotation at the cap: .1/.2/.3 shift,
// the oldest dropped, the live file reopened; (3) the line text and its privacy: no path separator,
// no path/user/host field; (4) the env gate (ON unless 0|off|false|no) and the device-loss codes;
// (5) text censuses: every telemetry call in GpuDebayer.cpp / GpuDisplayViewport.cpp / MainWindow.cpp
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
        {"platform/qt/GpuDisplayViewport.cpp", "noteViewportGlIdentity", 1},
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
                               "void flush(const char * reason)\n{\n    if (!enabled()) return;"})
    {
        ASSERT_TRUE(source.contains(QString::fromLatin1(entry)));
    }
    // The worker pool exists only once a fault or recovery asks for it; wevtapi is loaded at runtime.
    ASSERT_TRUE(source.contains(QStringLiteral("LoadLibraryW(L\"wevtapi.dll\")")));
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
