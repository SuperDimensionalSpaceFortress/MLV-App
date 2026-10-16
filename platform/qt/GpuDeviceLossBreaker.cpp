/*!
 * \file GpuDeviceLossBreaker.cpp
 * \brief GPU device-loss self-healing (GPU-DEVICE-LOSS-SELF-HEAL-1). See
 *        GpuDeviceLossBreaker.h; the rules live in GpuDeviceLossBreakerCore.h.
 */
#include "GpuDeviceLossBreaker.h"
#include "GpuDeviceLossBreakerCore.h"
#include "AutomationSettings.h"

#include "../../src/mlv/llrawproc/llrawproc.h"

#include <QByteArray>
#include <QCoreApplication>
#include <QDateTime>
#include <QMetaObject>
#include <QSettings>
#include <QStringList>

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

namespace GpuDeviceLossBreaker {

namespace {

namespace core = GpuDeviceLossBreakerCore;

/* Any thread. */
std::atomic<bool> g_sessionLoss{false};
std::atomic<int> g_sessionLossCode{0};
std::atomic<bool> g_startedSafe{false};

/* GUI thread only. */
bool g_installed = false;
std::function<void(const QString &)> g_noticeSink;
std::function<void()> g_refresh;
QString g_notice;
bool g_noticePending = false;
struct ClipProfile
{
    int width = 0;
    int height = 0;
    int bitsPerPixel = 0;
    bool compressed = false;
};
ClipProfile g_clip;

void showNotice(const QString & text)
{
    g_notice = text;
    g_noticePending = !g_noticeSink;
    if (g_noticeSink) g_noticeSink(text);
}

std::vector<core::Loss> readRecent(QSettings & settings)
{
    std::vector<core::Loss> recent;
    const QStringList stored = settings.value(QString::fromLatin1(core::kKeyRecentLosses)).toStringList();
    for (const QString & entry : stored)
    {
        core::Loss loss;
        if (core::parse(entry.toStdString(), &loss)) recent.push_back(loss);
    }
    return recent;
}

void writeRecent(QSettings & settings, const std::vector<core::Loss> & recent)
{
    QStringList out;
    for (const core::Loss & loss : recent) out << QString::fromStdString(core::serialize(loss));
    settings.setValue(QString::fromLatin1(core::kKeyRecentLosses), out);
}

void clearSafeStartKeys(QSettings & settings)
{
    for (const char * key : core::kSafeStartKeys) settings.remove(QString::fromLatin1(key));
    settings.sync();
}

/* Clears the flag (recording why) and lifts suppression unless this session
 * itself lost the device. */
void clearSafeStart(QSettings & settings, const char * outcome)
{
    clearSafeStartKeys(settings);
    GpuFieldTelemetry::noteRecovery("safe_start_flag", outcome);
    if (g_sessionLoss.load()) return;
    llrpSetGpuCudaSuppressed(0);
    if (!g_notice.isEmpty()) showNotice(QString());
    if (g_refresh) g_refresh();
}

/* GUI thread, queued by onFault once per session. */
void handleSessionLoss()
{
    const std::string codeName = core::cudaNameForCode(g_sessionLossCode.load());
    const GpuFieldTelemetry::GlIdentity id = GpuFieldTelemetry::glIdentity();
    core::Loss loss;
    loss.utcMs = QDateTime::currentMSecsSinceEpoch();
    loss.key = core::profileKey(id.renderer.toStdString(), id.driver.toStdString(), g_clip.width, g_clip.height,
                                g_clip.bitsPerPixel, g_clip.compressed);

    std::unique_ptr<QSettings> settings = automation_settings::openAppSettings();
    std::vector<core::Loss> recent = readRecent(*settings);
    const core::RecordResult result = core::recordLoss(recent, loss);
    writeRecent(*settings, recent);
    GpuFieldTelemetry::noteRecovery("device_loss", "cuda_suppressed_session");

    std::string text;
    if (result.setSafeStart)
    {
        settings->setValue(QString::fromLatin1(core::kKeySafeStartEnabled), true);
        settings->setValue(QString::fromLatin1(core::kKeySafeStartGlRenderer), id.renderer);
        settings->setValue(QString::fromLatin1(core::kKeySafeStartDriver), id.driver);
        settings->setValue(QString::fromLatin1(core::kKeySafeStartClipProfile),
                           QString::fromStdString(core::clipProfile(g_clip.width, g_clip.height,
                                                                    g_clip.bitsPerPixel, g_clip.compressed)));
        settings->setValue(QString::fromLatin1(core::kKeySafeStartSetUtc),
                           QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs));
        GpuFieldTelemetry::noteRecovery("safe_start_flag", "set");
        text = core::noticeText(core::kNoticeFlagSet, codeName);
    }
    else
    {
        text = core::noticeText(core::kNoticeSession, codeName);
    }
    settings->sync();
    showNotice(QString::fromStdString(text));
    if (g_refresh) g_refresh();
}

const core::FaultInject & faultInject()
{
    static const core::FaultInject inject = core::parseFaultInject(std::getenv(core::kEnvFaultInject));
    return inject;
}

} // namespace

void install()
{
    if (!GpuFieldTelemetry::enabled()) return;
    if (g_installed) return;
    g_installed = true;
    std::unique_ptr<QSettings> settings = automation_settings::openAppSettings();
    if (core::safeStartClearRequested(qgetenv(core::kEnvSafeStart).constData()))
    {
        clearSafeStartKeys(*settings);
        GpuFieldTelemetry::noteRecovery("safe_start_flag", "cleared_env");
    }
    if (settings->value(QString::fromLatin1(core::kKeySafeStartEnabled), false).toBool())
    {
        llrpSetGpuCudaSuppressed(1);
        g_startedSafe.store(true);
        GpuFieldTelemetry::noteRecovery("safe_start", "cpu_route");
        showNotice(QString::fromLatin1(core::kNoticeStartup));
    }
    llrpSetGpuDeviceLossClassifier(&classifyDeviceLossCodeForBackend);
    GpuFieldTelemetry::setFaultListener(&onFault);
}

int classifyDeviceLossCodeForBackend(int cudaCode)
{
    return GpuFieldTelemetry::isDeviceLossCode(cudaCode) ? 1 : 0;
}

void onFault(const GpuFieldTelemetry::CudaFault & fault)
{
    if (!GpuFieldTelemetry::isDeviceLossCode(fault.cudaCode)) return;
    if (g_sessionLoss.exchange(true)) return;
    g_sessionLossCode.store(fault.cudaCode);
    llrpSetGpuCudaSuppressed(1);
    QCoreApplication * app = QCoreApplication::instance();
    if (app) QMetaObject::invokeMethod(app, &handleSessionLoss, Qt::QueuedConnection);
}

void noteClipOpened(int width, int height, int bitsPerPixel, bool compressed)
{
    if (!g_installed) return;
    g_clip.width = width;
    g_clip.height = height;
    g_clip.bitsPerPixel = bitsPerPixel;
    g_clip.compressed = compressed;
    std::unique_ptr<QSettings> settings = automation_settings::openAppSettings();
    if (!settings->value(QString::fromLatin1(core::kKeySafeStartEnabled), false).toBool()) return;
    const GpuFieldTelemetry::GlIdentity id = GpuFieldTelemetry::glIdentity();
    const std::string storedRenderer =
        settings->value(QString::fromLatin1(core::kKeySafeStartGlRenderer)).toString().toStdString();
    const std::string storedDriver =
        settings->value(QString::fromLatin1(core::kKeySafeStartDriver)).toString().toStdString();
    if (core::identityChanged(storedRenderer, storedDriver, id.renderer.toStdString(), id.driver.toStdString()))
        clearSafeStart(*settings, "cleared_driver_change");
}

void userReenableGpu()
{
    if (!g_installed) return;
    std::unique_ptr<QSettings> settings = automation_settings::openAppSettings();
    if (!settings->value(QString::fromLatin1(core::kKeySafeStartEnabled), false).toBool()) return;
    clearSafeStart(*settings, "cleared_user");
}

void setNoticeSink(std::function<void(const QString &)> sink)
{
    g_noticeSink = std::move(sink);
    if (g_noticeSink && g_noticePending)
    {
        g_noticePending = false;
        g_noticeSink(g_notice);
    }
}

void setRefreshCallback(std::function<void()> callback)
{
    g_refresh = std::move(callback);
}

QString currentNotice()
{
    return g_notice;
}

bool startedSafe()
{
    return g_startedSafe.load();
}

bool faultInjectArmed()
{
    static const bool armed = faultInject().armed;
    return armed;
}

void noteRunStageForInjection(const char * stage)
{
    if (!stage) return;
    if (std::strcmp(stage, "recon:run") != 0 && std::strncmp(stage, "amaze:", 6) != 0) return;
    const core::FaultInject & inject = faultInject();
    if (!inject.armed) return;
    static std::atomic<long long> seen{0};
    static std::atomic<bool> fired{false};
    if (seen.fetch_add(1) + 1 < inject.n) return;
    if (fired.exchange(true)) return;
    GpuFieldTelemetry::CudaFault fault;
    fault.dll = "inject";
    fault.stage = stage;
    fault.rc = -1;
    fault.cudaCode = inject.code;
    fault.cudaName = core::cudaNameForCode(inject.code);
    fault.site = core::kFaultInjectSite;
    GpuFieldTelemetry::noteFault(fault);
}

void resetForTesting()
{
    g_sessionLoss.store(false);
    g_sessionLossCode.store(0);
    g_startedSafe.store(false);
    llrpSetGpuDeviceLossClassifier(nullptr);
    llrpSetGpuCudaSuppressed(0);
    g_installed = false;
    g_noticeSink = nullptr;
    g_refresh = nullptr;
    g_notice.clear();
    g_noticePending = false;
    g_clip = ClipProfile();
}

} // namespace GpuDeviceLossBreaker
