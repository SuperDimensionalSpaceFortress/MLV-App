// GPU-DEVICE-LOSS-SELF-HEAL-1: the pure rules of the device-loss circuit breaker
// (platform/qt/GpuDeviceLossBreakerCore.h). Pins (1) MLVAPP_GPU_FAULT_INJECT parsing; (2) the
// breaker: one loss sets no flag, two on the same profile within 10 minutes do, two further apart or
// on different profiles do not, and at most 8 recent losses are kept; (3) the "utcMs|key" text form;
// (4) the identity-change rule (an unknown current identity is never a change); (5) no notice asks
// the user to reboot or restart (owner spec r2); (6) the persisted keys round-trip through the
// app's settings store (automation_settings::isolate); (7) a source-text pin: run_backend checks
// the suppression flag before it takes the backend mutex. The fault-to-suppression chain through
// the real llrawproc seam lives in tests/pipeline/test_gpu_device_loss_selfheal.cpp.
#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../../platform/qt/AutomationSettings.h"
#include "../../platform/qt/GpuDeviceLossBreakerCore.h"

#include <QDir>
#include <QFile>
#include <QSettings>
#include <QString>
#include <QStringList>
#include <QTemporaryDir>

#include <algorithm>
#include <cctype>
#include <memory>
#include <string>
#include <vector>

namespace
{
namespace core = GpuDeviceLossBreakerCore;

constexpr long long kT0 = 1760000000000LL; // an arbitrary UTC ms

std::string lower(std::string s)
{
    std::transform(s.begin(), s.end(), s.begin(), [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    return QString::fromUtf8(file.readAll());
}

struct ReleaseGuard
{
    ~ReleaseGuard() { automation_settings::releaseForTest(); }
};
} // namespace

TEST(GpuDeviceLossBreaker, ParseFaultInject)
{
    const core::FaultInject a = core::parseFaultInject("702@120");
    ASSERT_TRUE(a.armed);
    ASSERT_EQ(702, a.code);
    ASSERT_EQ(120LL, a.n);
    for (const char * ok : {"700@1", "719@5", "999@3", " 702@2 "})
        ASSERT_TRUE(core::parseFaultInject(ok).armed);
    for (const char * bad : {"", "702", "@5", "702@", "702@0", "702@-1", "701@5", "2@5", "702@5x", "x702@5",
                             "702@ 5", "702@5@6", "abc"})
        ASSERT_FALSE(core::parseFaultInject(bad).armed);
    ASSERT_FALSE(core::parseFaultInject(nullptr).armed); // unset
}

TEST(GpuDeviceLossBreaker, CudaNamesForTheDeviceLossCodes)
{
    ASSERT_EQ(std::string("cudaErrorIllegalAddress"), std::string(core::cudaNameForCode(700)));
    ASSERT_EQ(std::string("cudaErrorLaunchTimeout"), std::string(core::cudaNameForCode(702)));
    ASSERT_EQ(std::string("cudaErrorLaunchFailure"), std::string(core::cudaNameForCode(719)));
    ASSERT_EQ(std::string("cudaErrorUnknown"), std::string(core::cudaNameForCode(999)));
}

TEST(GpuDeviceLossBreaker, ProfileKeySanitizesEveryPart)
{
    const std::string key = core::profileKey("NVIDIA GeForce RTX 3060 Laptop GPU/PCIe/SSE2", "610.74", 1920, 804, 14, true);
    ASSERT_EQ(std::string("NVIDIA GeForce RTX 3060 Laptop GPU|PCIe|SSE2;610.74;1920x804x14c"), key);
    ASSERT_EQ(std::string("unknown;unknown;unknown"), core::profileKey("", "", 0, 0, 0, false));
    ASSERT_TRUE(core::profileKey("a\"b\\c", "d", 1, 1, 12, false).find('"') == std::string::npos);
    ASSERT_TRUE(core::profileKey("a\"b\\c", "d", 1, 1, 12, false).find('\\') == std::string::npos);
}

TEST(GpuDeviceLossBreaker, OneLossSetsNoFlag)
{
    std::vector<core::Loss> recent;
    const core::RecordResult r = core::recordLoss(recent, {kT0, "k"});
    ASSERT_EQ(1, r.sameProfileInWindow);
    ASSERT_FALSE(r.setSafeStart);
    ASSERT_EQ(1, (int)recent.size());
}

TEST(GpuDeviceLossBreaker, TwoLossesOnTheSameProfileWithinTenMinutesSetTheFlag)
{
    std::vector<core::Loss> recent;
    ASSERT_FALSE(core::recordLoss(recent, {kT0, "k"}).setSafeStart);
    const core::RecordResult r = core::recordLoss(recent, {kT0 + core::kWindowMs, "k"}); // exactly 10 min: inside
    ASSERT_EQ(2, r.sameProfileInWindow);
    ASSERT_TRUE(r.setSafeStart);
}

TEST(GpuDeviceLossBreaker, TwoLossesMoreThanTenMinutesApartSetNoFlag)
{
    std::vector<core::Loss> recent;
    ASSERT_FALSE(core::recordLoss(recent, {kT0, "k"}).setSafeStart);
    const core::RecordResult r = core::recordLoss(recent, {kT0 + core::kWindowMs + 1, "k"});
    ASSERT_EQ(1, r.sameProfileInWindow);
    ASSERT_FALSE(r.setSafeStart);
    ASSERT_EQ(1, (int)recent.size()); // the old one was pruned
}

TEST(GpuDeviceLossBreaker, TwoLossesOnDifferentProfilesSetNoFlag)
{
    std::vector<core::Loss> recent;
    ASSERT_FALSE(core::recordLoss(recent, {kT0, "gpuA;610.74;1920x804x14c"}).setSafeStart);
    const core::RecordResult r = core::recordLoss(recent, {kT0 + 1000, "gpuA;610.74;1920x1080x14c"});
    ASSERT_EQ(1, r.sameProfileInWindow);
    ASSERT_FALSE(r.setSafeStart);
    ASSERT_EQ(2, (int)recent.size());
}

TEST(GpuDeviceLossBreaker, AtMostEightRecentLossesAreKept)
{
    std::vector<core::Loss> recent;
    for (int i = 0; i < 20; ++i) core::recordLoss(recent, {kT0 + i, "p" + std::to_string(i)});
    ASSERT_EQ((int)core::kMaxRecent, (int)recent.size());
    ASSERT_EQ(std::string("p12"), recent.front().key); // the newest eight
    ASSERT_EQ(std::string("p19"), recent.back().key);
}

TEST(GpuDeviceLossBreaker, SerializeParseRoundTrip)
{
    const core::Loss in{kT0, "NVIDIA|PCIe;610.74;1920x804x14c"};
    const std::string text = core::serialize(in);
    ASSERT_EQ(std::string("1760000000000|NVIDIA|PCIe;610.74;1920x804x14c"), text);
    core::Loss out;
    ASSERT_TRUE(core::parse(text, &out));
    ASSERT_EQ(in.utcMs, out.utcMs);
    ASSERT_EQ(in.key, out.key);
    for (const char * bad : {"", "|k", "123|", "12a|k", "nokey"})
        ASSERT_FALSE(core::parse(bad, &out));
}

TEST(GpuDeviceLossBreaker, IdentityChangedRule)
{
    ASSERT_FALSE(core::identityChanged("RTX 3060", "610.74", "RTX 3060", "610.74"));
    ASSERT_TRUE(core::identityChanged("RTX 3060", "610.74", "RTX 3060", "611.02")); // driver update
    ASSERT_TRUE(core::identityChanged("RTX 3060", "610.74", "RTX 4090", "610.74")); // another GPU
    // An unknown current identity (GL not initialised yet) is never a change.
    ASSERT_FALSE(core::identityChanged("RTX 3060", "610.74", "", "610.74"));
    ASSERT_FALSE(core::identityChanged("RTX 3060", "610.74", "RTX 4090", ""));
    ASSERT_FALSE(core::identityChanged("RTX 3060", "610.74", "", ""));
}

TEST(GpuDeviceLossBreaker, NoticesNeverAskForARebootOrRestart)
{
    const std::vector<std::string> notices = {
        core::noticeText(core::kNoticeSession, "cudaErrorLaunchTimeout"),
        core::noticeText(core::kNoticeFlagSet, "cudaErrorLaunchTimeout"),
        core::noticeText(core::kNoticeStartup, "cudaErrorLaunchTimeout"),
    };
    for (const std::string & text : notices)
    {
        ASSERT_TRUE(lower(text).find("reboot") == std::string::npos);
        ASSERT_TRUE(lower(text).find("restart") == std::string::npos);
        ASSERT_TRUE(text.find("%1") == std::string::npos);
    }
    ASSERT_EQ(std::string("GPU playback stopped after a graphics driver fault (cudaErrorLaunchTimeout). Playback "
                          "continues on the CPU for this session."),
              notices[0]);
    ASSERT_EQ(std::string("Playback starts on the CPU after repeated graphics driver faults on this GPU and driver. "
                          "Turn on the CUDA playback preset (Performance profiling) to try the GPU again."),
              notices[2]);
}

TEST(GpuDeviceLossBreaker, PersistedKeysRoundTripThroughTheAppSettingsStore)
{
    ReleaseGuard guard;
    QTemporaryDir dir;
    ASSERT_TRUE(dir.isValid());
    ASSERT_EQ(dir.path().toStdString(), automation_settings::isolate(dir.path()).toStdString());

    std::vector<core::Loss> recent;
    core::recordLoss(recent, {kT0, "gpu;610.74;1920x804x14c"});
    core::recordLoss(recent, {kT0 + 5000, "gpu;610.74;1920x804x14c"});
    {
        const std::unique_ptr<QSettings> s = automation_settings::openAppSettings();
        QStringList stored;
        for (const core::Loss & loss : recent) stored << QString::fromStdString(core::serialize(loss));
        s->setValue(QString::fromLatin1(core::kKeyRecentLosses), stored);
        s->setValue(QString::fromLatin1(core::kKeySafeStartEnabled), true);
        s->setValue(QString::fromLatin1(core::kKeySafeStartGlRenderer), QStringLiteral("NVIDIA GeForce RTX 3060"));
        s->setValue(QString::fromLatin1(core::kKeySafeStartDriver), QStringLiteral("610.74"));
        s->setValue(QString::fromLatin1(core::kKeySafeStartClipProfile), QStringLiteral("1920x804x14c"));
        s->setValue(QString::fromLatin1(core::kKeySafeStartSetUtc), QStringLiteral("2026-10-14T08:00:00.000Z"));
        s->sync();
    }
    ASSERT_TRUE(QFile::exists(automation_settings::storeFile()));
    {
        const std::unique_ptr<QSettings> s = automation_settings::openAppSettings();
        ASSERT_TRUE(s->value(QString::fromLatin1(core::kKeySafeStartEnabled), false).toBool());
        ASSERT_EQ(std::string("610.74"), s->value(QString::fromLatin1(core::kKeySafeStartDriver)).toString().toStdString());
        const QStringList stored = s->value(QString::fromLatin1(core::kKeyRecentLosses)).toStringList();
        ASSERT_EQ(2, (int)stored.size());
        core::Loss back;
        ASSERT_TRUE(core::parse(stored.at(1).toStdString(), &back));
        ASSERT_EQ(kT0 + 5000, back.utcMs);
        ASSERT_EQ(std::string("gpu;610.74;1920x804x14c"), back.key);
        for (const char * key : core::kSafeStartKeys) s->remove(QString::fromLatin1(key));
        s->sync();
    }
    {
        const std::unique_ptr<QSettings> s = automation_settings::openAppSettings();
        for (const char * key : core::kSafeStartKeys) ASSERT_FALSE(s->contains(QString::fromLatin1(key)));
        ASSERT_TRUE(s->contains(QString::fromLatin1(core::kKeyRecentLosses))); // a clear keeps the history
    }
    automation_settings::releaseForTest();
}

// The suppression check in run_backend comes before the backend mutex, and before any backend use.
TEST(GpuDeviceLossBreaker, RunBackendChecksTheFlagBeforeTheBackendMutex)
{
    const QString source = readRepoFile(QStringLiteral("src/mlv/llrawproc/llrawproc.c"));
    const int fn = source.indexOf(QStringLiteral("static int llrawproc_gpu_recon_run_backend("));
    ASSERT_TRUE(fn >= 0);
    const int check = source.indexOf(QStringLiteral("if(llrpGpuCudaSuppressed())"), fn);
    const int lock = source.indexOf(QStringLiteral("pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex)"), fn);
    ASSERT_TRUE(check > fn);
    ASSERT_TRUE(lock > fn);
    ASSERT_TRUE(check < lock);
    ASSERT_TRUE(source.mid(check, 160).contains(QStringLiteral("LLRP_GPU_PLAYBACK_RECON_RC_CUDA_SUPPRESSED")));
    // The preupload refuses before its own trylock, and the playback gate before anything else.
    const int pre = source.indexOf(QStringLiteral("int llrpGpuPlaybackReconPreuploadFrame(uint64_t frame_id,\n"
                                                  "                                      const uint16_t * raw_input_bayer14,\n"
                                                  "                                      size_t raw_image_size)\n{"));
    ASSERT_TRUE(pre >= 0);
    const int preCheck = source.indexOf(QStringLiteral("if(llrpGpuCudaSuppressed()) return 0;"), pre);
    const int preLock = source.indexOf(QStringLiteral("pthread_mutex_trylock(&g_llrawproc_gpu_recon_backend_mutex)"), pre);
    ASSERT_TRUE(preCheck > pre && preCheck < preLock);
    ASSERT_TRUE(source.contains(QStringLiteral("static int llrawproc_gpu_playback_recon_enabled(void)\n{\n"
                                               "    if(llrpGpuCudaSuppressed()) return 0;")));
    // Both build variants define the flag.
    ASSERT_EQ(2, (int)source.count(QStringLiteral("int llrpGpuCudaSuppressed(void)\n{")));
    ASSERT_EQ(2, (int)source.count(QStringLiteral("void llrpSetGpuCudaSuppressed(int suppressed)\n{")));
}

// Every CUDA AMaZE entry in GpuDebayer.cpp refuses first while suppressed.
TEST(GpuDeviceLossBreaker, EveryCudaAmazeEntryRefusesWhileSuppressed)
{
    const QString debayer = readRepoFile(QStringLiteral("platform/qt/GpuDebayer.cpp"));
    ASSERT_TRUE(debayer.contains(QStringLiteral("CUDA suppressed after a GPU device loss")));
    for (const char * entry : {"bool gpuAmazeDebayerApplyGpuOffscreen(", "bool gpuAmazeDebayerRenderPostWbGlTexture(",
                               "bool gpuAmazeDebayerRenderPostWbGlTextureFromR16GlTexture(",
                               "bool gpuAmazeDebayerRenderPostWbGlTextureFromDeviceBayer16(",
                               "bool gpuAmazeDebayerApplyGpuOffscreenPostWb(",
                               "GpuAmazeDebayerBackendAvailability gpuAmazeDebayerProbeBackend(",
                               "GpuAmazeDebayerBackendAvailability gpuAmazeDebayerProbeR16TextureBackend(",
                               "bool gpuAmazeDebayerReducedHnyquistAvailable("})
    {
        const int at = debayer.indexOf(QString::fromLatin1(entry));
        ASSERT_TRUE(at >= 0);
        const int body = debayer.indexOf(QStringLiteral("\n{\n"), at);
        const int end = debayer.indexOf(QStringLiteral("\n}\n"), body);
        const QString text = debayer.mid(body, end - body);
        const int check = text.indexOf(QStringLiteral("llrpGpuCudaSuppressed()"));
        ASSERT_TRUE(check >= 0);
        for (const char * call : {"loadAmazeDebayerRuntime(", ".ensure(", "runtime.create(", "liveAmazeTextureRuntime()",
                                  "gpuAmazeDebayerApplyGpuOffscreen("})
        {
            const int c = text.indexOf(QString::fromLatin1(call));
            ASSERT_TRUE(c < 0 || check < c);
        }
    }
}

// r3: a suppressed session takes the plain CPU route, not GL preview processing (which presented
// 6.4-10.2 fps on Bachelor against 21.8 fps for the CPU route). Every policy builder in MainWindow.cpp
// reads MLVAPP_EXPERIMENTAL_GPU_PROCESSING through the suppression-aware helper, and the two sites that
// force it on are reachable only through playback_recon_requested_by_environment(), which refuses first.
TEST(GpuDeviceLossBreaker, SuppressedSessionTurnsOffGpuPreviewProcessingAtEveryPolicyBuilder)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString assign = QStringLiteral("gpuPreviewProcessingEnvironmentRequested =");
    const QString gated = QStringLiteral(
        "gpuPreviewProcessingEnvironmentRequested =\n"
        "        mainWindowGpuPreviewProcessingRequestedForSession(\n"
        "            gpuPreviewProcessingRequestedByEnvironment(), llrpGpuCudaSuppressed() != 0 );");
    int builders = 0;
    int forced = 0;
    for (int at = source.indexOf(assign); at >= 0; at = source.indexOf(assign, at + 1))
    {
        if (source.mid(at, assign.size() + 6) == assign + QStringLiteral(" true;"))
        {
            // The forced-true site sits under a condition that requires the (suppression-gated) recon request.
            const int cond = source.lastIndexOf(QStringLiteral("const bool "), at);
            ASSERT_TRUE(cond >= 0);
            ASSERT_TRUE(source.mid(cond, at - cond).contains(QStringLiteral("gpuPlaybackReconEnvironmentRequested\n")));
            ++forced;
            continue;
        }
        ASSERT_TRUE(source.mid(at, gated.size()) == gated);
        ++builders;
    }
    ASSERT_EQ(5, builders);
    ASSERT_EQ(2, forced);
    ASSERT_EQ(0, (int)source.count(QStringLiteral("gpuPreviewProcessingEnvironmentRequested =\n"
                                                  "        gpuPreviewProcessingRequestedByEnvironment();")));
    ASSERT_TRUE(source.contains(QStringLiteral("static bool playback_recon_requested_by_environment()\n{\n"
                                               "    // GPU-DEVICE-LOSS-SELF-HEAL-1: no CUDA recon after a device loss or on a safe start.\n"
                                               "    if( llrpGpuCudaSuppressed() ) return false;")));
    // Both policy-state recon requests come from that function.
    ASSERT_EQ(2, (int)source.count(QStringLiteral("gpuPlaybackReconEnvironmentRequested =\n"
                                                  "        playback_recon_requested_by_environment();")));
}

// r4: the suppressed session's CPU dual-ISO recon refused its reduced plan with "GPU playback recon
// requested" (the CUDA preset's env), so it reconstructed full res: 5.6-10.0 fps against 21.5 for the
// CPU route on Bachelor. The core reads that env in one helper, which refuses first when suppressed.
TEST(GpuDeviceLossBreaker, SuppressedSessionCpuReconIgnoresTheGpuReconEnv)
{
    const QString source = readRepoFile(QStringLiteral("src/mlv/video_mlv.c"));
    const QString read = QStringLiteral("getenv(\"MLVAPP_GPU_PLAYBACK_RECON\")");
    ASSERT_EQ(1, (int)source.count(read));
    const int helper = source.indexOf(QStringLiteral("static int mlv_gpu_playback_recon_requested_via_env(void)\n{\n"));
    ASSERT_TRUE(helper >= 0);
    const int gate = source.indexOf(QStringLiteral("    if (llrpGpuCudaSuppressed()) return 0;\n"), helper);
    const int at = source.indexOf(read, helper);
    ASSERT_TRUE(gate > helper);
    ASSERT_TRUE(at > gate);
    ASSERT_EQ(-1, source.mid(helper, at - helper).indexOf(QStringLiteral("\n}\n")));
}
