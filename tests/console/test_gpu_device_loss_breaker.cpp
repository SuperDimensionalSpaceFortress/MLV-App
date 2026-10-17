// GPU-DEVICE-LOSS-SELF-HEAL-1: the pure rules of the device-loss circuit breaker
// (platform/qt/GpuDeviceLossBreakerCore.h). Pins (1) MLVAPP_GPU_FAULT_INJECT parsing; (2) the
// breaker: one loss sets no flag, two on the same profile within 10 minutes do, two further apart or
// on different profiles do not, and at most 8 recent losses are kept; (3) the "utcMs|key" text form;
// (4) the identity-change rule (an unknown current identity is never a change); (5) no notice asks
// the user to reboot or restart (owner spec r2); (6) the persisted keys round-trip through the
// app's settings store (automation_settings::isolate); (7) a source-text pin: run_backend checks
// the suppression flag before it takes the backend mutex; (8) the MLVAPP_GPU_SAFE_START=clear match;
// (9) every capturing unlock sets the flag on a device loss before it releases the backend mutex. The
// fault-to-suppression chain through the real llrawproc seam lives in
// tests/pipeline/test_gpu_device_loss_selfheal.cpp.
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

// GPU-LOSS-EMIT-BEFORE-UNLOCK-1 (c): MLVAPP_GPU_SAFE_START=clear is matched case-insensitively with surrounding
// whitespace ignored (the parseEnabled convention); no other word, and no near-miss, is a clear.
TEST(GpuDeviceLossBreaker, SafeStartClearAcceptsCaseAndWhitespaceVariantsOnly)
{
    for (const char * ok : {"clear", "CLEAR", " Clear\t", "clear\r\n"})
        ASSERT_TRUE(core::safeStartClearRequested(ok));
    ASSERT_FALSE(core::safeStartClearRequested(nullptr)); // unset
    for (const char * bad : {"", "0", "1", "cleared", "clear!", "c lear", "unclear", "clea", "clear clear"})
        ASSERT_FALSE(core::safeStartClearRequested(bad));
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

namespace
{
// r6 (CoS #437; ruling candidate mlv-gpu-guard-under-serializing-lock-r1, ADOPTED): every acquisition of a
// lock that serializes CUDA work, in `source`, is found by grep (never listed by hand). Each one whose code
// up to the end of its function submits CUDA work (one of `submits`) must read `guard` after the lock and
// before that first submission. Release, destroy, reset and teardown calls are not submissions: they free
// what a loss left behind and are never gated (llrawproc.h). Returns "<tag>:<line>" for each site that does
// not; a new site added without a re-check is reported here.
QStringList lockSitesWithoutAReCheck(const QString & source, const char * tag, const QStringList & locks,
                                     const QStringList & submits, const QString & guard)
{
    QStringList missing;
    for (const QString & lock : locks)
    {
        for (int at = source.indexOf(lock); at >= 0; at = source.indexOf(lock, at + 1))
        {
            const int end = source.indexOf(QStringLiteral("\n}\n"), at);
            const QString region = source.mid(at, (end < 0 ? source.size() : end) - at);
            int firstSubmit = -1;
            for (const QString & call : submits)
            {
                const int c = region.indexOf(call);
                if (c >= 0 && (firstSubmit < 0 || c < firstSubmit)) firstSubmit = c;
            }
            if (firstSubmit < 0) continue; // counters, info, release, teardown: no CUDA work behind this lock
            const int check = region.indexOf(guard);
            if (check < 0 || check > firstSubmit)
                missing << QStringLiteral("%1:%2").arg(QString::fromLatin1(tag)).arg(source.left(at).count(QLatin1Char('\n')) + 1);
        }
    }
    return missing;
}

QStringList llrawprocLockSitesWithoutAReCheck(const QString & source)
{
    return lockSitesWithoutAReCheck(source, "llrawproc.c",
        {QStringLiteral("pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex)"),
         QStringLiteral("pthread_mutex_trylock(&g_llrawproc_gpu_recon_backend_mutex)")},
        {QStringLiteral("g->run("), QStringLiteral("g->run_preuploaded("), QStringLiteral("g->set_clip("),
         QStringLiteral("g->set_luts("), QStringLiteral("g->set_reduced_iso_notch("), QStringLiteral("g->preupload_frame("),
         QStringLiteral("g->copy_last_device_output_to_gl_texture("), QStringLiteral("g->retain_last_device_output("),
         QStringLiteral("llrawproc_gpu_export_backend_available(")}, // creates the backend on first use
        QStringLiteral("llrpGpuCudaSuppressed()"));
}

QStringList debayerLockSitesWithoutAReCheck(const QString & source)
{
    return lockSitesWithoutAReCheck(source, "GpuDebayer.cpp",
        {QStringLiteral("QMutexLocker locker(&live.mutex);"), QStringLiteral("QMutexLocker locker(&mutex);"),
         QStringLiteral("live.mutex.tryLock(")},
        {QStringLiteral("runtime.create("), QStringLiteral("runtime.run("), QStringLiteral(".runPostWbGlTexture("),
         QStringLiteral(".runPostWbGlTextureFromR16GlTexture("), QStringLiteral(".runPostWbGlTextureFromDeviceBayer16("),
         QStringLiteral("setLiveReducedHnyquist("), QStringLiteral("runtime.setReducedHnyquist(")},
        QStringLiteral("llrpGpuCudaSuppressed()"));
}

QString replacedOnce(QString text, const QString & from, const QString & to)
{
    const int at = text.indexOf(from);
    ASSERT_TRUE(at >= 0);
    ASSERT_EQ(-1, text.indexOf(from, at + 1));
    return text.replace(at, from.size(), to);
}
} // namespace

// r6: suppression is read again after every serializing lock that guards CUDA work (the r1-r5 pin,
// RunBackendChecksTheFlagBeforeTheBackendMutex, locked in the check-before-the-lock-only shape). The check
// before each lock stays as the fast path.
TEST(GpuDeviceLossBreaker, EverySerializingLockSiteReChecksSuppressionAfterAcquiring)
{
    const QString llrawproc = readRepoFile(QStringLiteral("src/mlv/llrawproc/llrawproc.c"));
    const QString debayer = readRepoFile(QStringLiteral("platform/qt/GpuDebayer.cpp"));
    ASSERT_EQ(std::string(), llrawprocLockSitesWithoutAReCheck(llrawproc).join(QStringLiteral(", ")).toStdString());
    ASSERT_EQ(std::string(), debayerLockSitesWithoutAReCheck(debayer).join(QStringLiteral(", ")).toStdString());
    // The census sees every site that submits: run_backend, the preupload, the GL copy, the export probe; ensure,
    // and both live AMaZE runs. Each one's re-check removed is reported (the r5 shape).
    const QString runRecheck = QStringLiteral(
        "    pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);\n"
        "    /* r6 (CoS #437, guard under the serializing lock): a caller that passed the\n"
        "     * check above and then waited here through a loss must not reach CUDA. */\n"
        "    if(llrpGpuCudaSuppressed())\n");
    const QString noRunRecheck = replacedOnce(llrawproc, runRecheck,
        QStringLiteral("    pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);\n    if(0)\n"));
    ASSERT_EQ(1, (int)llrawprocLockSitesWithoutAReCheck(noRunRecheck).size());
    const QString noPreuploadRecheck = replacedOnce(llrawproc, QStringLiteral("    suppressed = llrpGpuCudaSuppressed();\n"),
                                                    QStringLiteral("    suppressed = 0;\n"));
    ASSERT_EQ(1, (int)llrawprocLockSitesWithoutAReCheck(noPreuploadRecheck).size());
    const QString liveRecheck = QStringLiteral(
        "    /* r6: read again under the lock: a caller queued here through a loss must not reach CUDA. */\n"
        "    if ( llrpGpuCudaSuppressed() ) return fail(cudaSuppressedReason());\n");
    ASSERT_EQ(2, (int)debayer.count(liveRecheck));
    QString noLiveRecheck = debayer;
    noLiveRecheck.replace(liveRecheck, QString());
    ASSERT_EQ(2, (int)debayerLockSitesWithoutAReCheck(noLiveRecheck).size());
    const QString noEnsureRecheck = replacedOnce(debayer,
        QStringLiteral("         * under the lock; a suppressed session leaves the runtime unattempted. */\n        if ( llrpGpuCudaSuppressed() )\n"),
        QStringLiteral("         * under the lock; a suppressed session leaves the runtime unattempted. */\n        if ( false )\n"));
    ASSERT_EQ(1, (int)debayerLockSitesWithoutAReCheck(noEnsureRecheck).size());
    // A new site added later without a re-check is reported.
    const QString added = llrawproc + QStringLiteral(
        "\nstatic int a_new_site(void)\n{\n"
        "    pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);\n"
        "    g->run(g->backend, 0, 0, 0, 0, 0);\n"
        "    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);\n    return 0;\n}\n");
    const QStringList newSite = llrawprocLockSitesWithoutAReCheck(added);
    ASSERT_EQ(1, (int)newSite.size());
    ASSERT_EQ((QStringLiteral("llrawproc.c:%1").arg(llrawproc.count(QLatin1Char('\n')) + 4)).toStdString(), newSite.at(0).toStdString());
    // The fast path stays: the run, the preupload and the GL copy refuse before their lock, the playback gate first.
    for (const char * entry : {"static int llrawproc_gpu_recon_run_backend(", "int llrpGpuPlaybackReconPreuploadFrame(uint64_t frame_id,\n                                      const uint16_t * raw_input_bayer14,\n                                      size_t raw_image_size)\n{",
                               "int llrpGpuPlaybackReconCopyLastDeviceBayer16ToGlTexture(unsigned int gl_texture_id,\n                                                         int * rc_out)\n{"})
    {
        const int fn = llrawproc.indexOf(QString::fromLatin1(entry));
        ASSERT_TRUE(fn >= 0);
        const int check = llrawproc.indexOf(QStringLiteral("if(llrpGpuCudaSuppressed())"), fn);
        int lock = llrawproc.indexOf(QStringLiteral("_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex)"), fn);
        const int trylock = llrawproc.indexOf(QStringLiteral("_mutex_trylock(&g_llrawproc_gpu_recon_backend_mutex)"), fn);
        if (trylock >= 0 && (lock < 0 || trylock < lock)) lock = trylock;
        ASSERT_TRUE(check > fn);
        ASSERT_TRUE(check < lock);
    }
    ASSERT_TRUE(llrawproc.contains(QStringLiteral("static int llrawproc_gpu_playback_recon_enabled(void)\n{\n"
                                                  "    if(llrpGpuCudaSuppressed()) return 0;")));
    // Both build variants define the flag.
    ASSERT_EQ(2, (int)llrawproc.count(QStringLiteral("int llrpGpuCudaSuppressed(void)\n{")));
    ASSERT_EQ(2, (int)llrawproc.count(QStringLiteral("void llrpSetGpuCudaSuppressed(int suppressed)\n{")));
}

namespace
{
// GPU-LOSS-EMIT-BEFORE-UNLOCK-1: each llrawproc section that captures CUDA codes under the backend mutex and
// emits them after releasing it scans the captured codes for a device loss as its last statement before the
// unlock, so the flag is set before any queued caller can take the lock. r2 (CoS I2): the sections are
// DISCOVERED, not listed: every function body (a column-0 "{" line up to the next column-0 "}") that contains
// both the backend unlock and an llrawproc_gpu_stage_telemetry_emit* call. For every emit call in such a body,
// the nearest unlock before it must have the scan as the one statement just ahead of it.
struct CapturingUnlockCensus
{
    QStringList sites;   // every discovered section, in source order
    QStringList missing; // the discovered sections that do not scan before the unlock
};

// The function name on the signature that ends just before `open` (the "\n" ahead of the column-0 "{"); the
// signature may wrap onto indented continuation lines.
QString functionNameBefore(const QString & source, int open)
{
    int lineStart = source.lastIndexOf(QLatin1Char('\n'), open - 1) + 1;
    while (lineStart > 1 && source.at(lineStart).isSpace())
        lineStart = source.lastIndexOf(QLatin1Char('\n'), lineStart - 2) + 1;
    QString name = source.mid(lineStart, open - lineStart).section(QLatin1Char('('), 0, 0).section(QLatin1Char(' '), -1);
    while (name.startsWith(QLatin1Char('*'))) name.remove(0, 1);
    return name;
}

CapturingUnlockCensus capturingUnlockCensus(const QString & source)
{
    CapturingUnlockCensus census;
    const QString suppress = QStringLiteral("llrawproc_gpu_suppress_on_device_loss_locked(");
    const QString unlock = QStringLiteral("pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);");
    const QString emitCall = QStringLiteral("llrawproc_gpu_stage_telemetry_emit");
    for (int open = source.indexOf(QStringLiteral("\n{\n")); open >= 0;
         open = source.indexOf(QStringLiteral("\n{\n"), open + 1))
    {
        const int close = source.indexOf(QStringLiteral("\n}"), open + 2);
        if (close < 0) break;
        const QString body = source.mid(open, close - open);
        if (!body.contains(unlock) || !body.contains(emitCall)) continue;
        const QString name = functionNameBefore(source, open);
        census.sites << name;
        bool ok = true;
        for (int emitAt = body.indexOf(emitCall); emitAt >= 0; emitAt = body.indexOf(emitCall, emitAt + 1))
        {
            const int unlockAt = body.lastIndexOf(unlock, emitAt - 1);
            const int at = unlockAt < 0 ? -1 : body.lastIndexOf(suppress, unlockAt - 1);
            if (at < 0 || body.mid(at, unlockAt - at).count(QStringLiteral(";\n")) != 1) ok = false;
        }
        if (!ok) census.missing << name;
    }
    return census;
}

QStringList capturingUnlocksWithoutSuppression(const QString & source)
{
    return capturingUnlockCensus(source).missing;
}
} // namespace

TEST(GpuDeviceLossBreaker, EveryCapturingUnlockSuppressesOnADeviceLossBeforeReleasing)
{
    const QString llrawproc = readRepoFile(QStringLiteral("src/mlv/llrawproc/llrawproc.c"));
    // Discovered, not listed (CoS I2). Exactly these four sections capture under the backend mutex and emit after
    // it: a new one that scans first is added to this line on purpose; one that does not also fails the next line.
    ASSERT_EQ(std::string("llrpGpuPlaybackReconDrainCudaErrorsAtTeardown, llrpGpuPlaybackReconPreuploadFrame, "
                          "llrawproc_gpu_recon_run_backend, llrpGpuPlaybackReconResetGlTextureResources"),
              capturingUnlockCensus(llrawproc).sites.join(QStringLiteral(", ")).toStdString());
    ASSERT_EQ(std::string(), capturingUnlocksWithoutSuppression(llrawproc).join(QStringLiteral(", ")).toStdString());
    // Fifth-site mutants: a copy of the teardown drain under a new name, with its scan moved after the unlock or
    // dropped, is discovered as a fifth section and reported; the four real sections stay clean.
    const QString drainEntry = QStringLiteral("int llrpGpuPlaybackReconDrainCudaErrorsAtTeardown(int wait_ms)\n{");
    const int drainAt = llrawproc.indexOf(drainEntry);
    ASSERT_TRUE(drainAt > 0);
    QString fifthSite = llrawproc.mid(drainAt, llrawproc.indexOf(QStringLiteral("\n}\n"), drainAt) + 3 - drainAt);
    fifthSite.replace(QStringLiteral("llrpGpuPlaybackReconDrainCudaErrorsAtTeardown"), QStringLiteral("llrpFifthCapturingSiteMutant"));
    const QString scanThenUnlock = QStringLiteral("    llrawproc_gpu_suppress_on_device_loss_locked(leftovers, 2);\n"
                                                  "    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);\n");
    for (const QString & fifthShape : {QStringLiteral("    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);\n"
                                                      "    llrawproc_gpu_suppress_on_device_loss_locked(leftovers, 2);\n"),
                                       QStringLiteral("    pthread_mutex_unlock(&g_llrawproc_gpu_recon_backend_mutex);\n")})
    {
        const QString withFifth = llrawproc + QStringLiteral("\n") + replacedOnce(fifthSite, scanThenUnlock, fifthShape);
        const CapturingUnlockCensus census = capturingUnlockCensus(withFifth);
        ASSERT_EQ(5, (int)census.sites.size());
        ASSERT_EQ(std::string("llrpFifthCapturingSiteMutant"), census.missing.join(QStringLiteral(", ")).toStdString());
    }
    // ...and the same copy left in its scanning shape is discovered and passes.
    ASSERT_EQ(5, (int)capturingUnlockCensus(llrawproc + QStringLiteral("\n") + fifthSite).sites.size());
    ASSERT_EQ(0, (int)capturingUnlocksWithoutSuppression(llrawproc + QStringLiteral("\n") + fifthSite).size());
    // The scan sets the flag and never clears it.
    const QString helper = llrawproc.mid(llrawproc.indexOf(QStringLiteral("static void llrawproc_gpu_suppress_on_device_loss_locked(")));
    ASSERT_TRUE(helper.left(helper.indexOf(QStringLiteral("\n}\n"))).contains(QStringLiteral("llrpSetGpuCudaSuppressed(1);")));
    ASSERT_FALSE(helper.left(helper.indexOf(QStringLiteral("\n}\n"))).contains(QStringLiteral("llrpSetGpuCudaSuppressed(0)")));
    // Mutants: each site's scan removed (the master shape) is reported.
    for (const char * call : {"    llrawproc_gpu_suppress_on_device_loss_locked(leftovers, 2);\n",
                              "    if(telemetry_entered) llrawproc_gpu_suppress_on_device_loss_locked(&telemetry_event, 1);\n",
                              "    llrawproc_gpu_suppress_on_device_loss_locked(telemetry_events, telemetry_event_count);\n",
                              "    if(telemetry_sink) llrawproc_gpu_suppress_on_device_loss_locked(leftovers, 2);\n"})
    {
        const QString mutant = replacedOnce(llrawproc, QString::fromLatin1(call), QString());
        ASSERT_EQ(1, (int)capturingUnlocksWithoutSuppression(mutant).size());
    }
    // The breaker registers the classifier when it installs its fault listener, and a reset clears it.
    const QString breaker = readRepoFile(QStringLiteral("platform/qt/GpuDeviceLossBreaker.cpp"));
    ASSERT_TRUE(breaker.contains(QStringLiteral("    llrpSetGpuDeviceLossClassifier(&classifyDeviceLossCodeForBackend);\n"
                                                "    GpuFieldTelemetry::setFaultListener(&onFault);\n")));
    ASSERT_TRUE(breaker.contains(QStringLiteral("    llrpSetGpuDeviceLossClassifier(nullptr);\n")));
}

// r6: every AMaZE run reads the flag again after its telemetry entry, which can deliver an orphan device-loss
// code to the breaker on the calling thread (the behaviour is pinned in tests/pipeline/test_gpu_device_loss_selfheal.cpp).
TEST(GpuDeviceLossBreaker, EveryAmazeRunReChecksSuppressionAfterItsTelemetryEntry)
{
    const QString debayer = readRepoFile(QStringLiteral("platform/qt/GpuDebayer.cpp"));
    ASSERT_TRUE(debayer.contains(QStringLiteral(
        "bool enterAmazeRunUnlessCudaSuppressed(GpuAmazeDebayerRuntime * runtime, bool telemetry)\n{\n"
        "    if ( telemetry ) enterAmazeStageTelemetry(runtime);\n"
        "    return !llrpGpuCudaSuppressed();\n}\n")));
    // The entry is made only there.
    ASSERT_EQ(2, (int)debayer.count(QStringLiteral("enterAmazeStageTelemetry(")));
    const QString enter = QStringLiteral("if ( !enterAmazeRunUnlessCudaSuppressed(&");
    ASSERT_EQ(4, (int)debayer.count(enter));
    for (const char * call : {"const int rc = runtime.run(backend,", "const int rc = runtime.runPostWbGlTexture(backend,",
                              "live.runtime.runPostWbGlTextureFromR16GlTexture(", "live.runtime.runPostWbGlTextureFromDeviceBayer16("})
    {
        const int callAt = debayer.indexOf(QString::fromLatin1(call));
        ASSERT_TRUE(callAt > 0);
        const int enterAt = debayer.lastIndexOf(enter, callAt);
        ASSERT_TRUE(enterAt > 0);
        // nothing between the re-check and the call but its refusal
        const QString between = debayer.mid(enterAt, callAt - enterAt);
        ASSERT_TRUE(between.contains(QStringLiteral("return fail(cudaSuppressedReason());")));
        ASSERT_EQ(1, (int)between.count(QStringLiteral(";\n")) - (int)between.count(QStringLiteral("runtime.destroy(backend);\n")));
    }
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

// r5 (the declared route): the CPU reduced recon (applyLLRawProcObjectWorker_with_dims, the reduced
// plan's only product caller) restamps the thread's CUDA-recon flag before it runs, as the full-res
// worker entry does. Without it every reduced frame after a mid-session loss was refused as a CUDA
// recon (Bachelor 89c82a63 L1: refused_gpu_playback_recon=397, 10.6 fps).
TEST(GpuDeviceLossBreaker, SuppressedSessionCpuReducedReconRestampsTheCudaReconFlag)
{
    const QString source = readRepoFile(QStringLiteral("src/mlv/llrawproc/llrawproc.c"));
    const QString reset = QStringLiteral("    llrawproc_gpu_playback_reset_last_run_state();\n");
    for (const char * entry : {"int applyLLRawProcObjectWorker_with_dims(mlvObject_t * video,",
                               "void applyLLRawProcObjectWorker(mlvObject_t * video,"})
    {
        const int fn = source.indexOf(QString::fromLatin1(entry));
        ASSERT_TRUE(fn >= 0);
        const int end = source.indexOf(QStringLiteral("\n}\n"), fn);
        const int at = source.indexOf(reset, fn);
        ASSERT_TRUE(at > fn);
        ASSERT_TRUE(at < end);
    }
    const int fn = source.indexOf(QStringLiteral("int applyLLRawProcObjectWorker_with_dims(mlvObject_t * video,"));
    ASSERT_TRUE(source.indexOf(reset, fn) < source.indexOf(QStringLiteral("return llrawproc_apply_with_dims_internal("), fn));
}
