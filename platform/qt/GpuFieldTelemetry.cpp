/*!
 * \file GpuFieldTelemetry.cpp
 * \brief GPU field telemetry (GPU-FIELD-TELEMETRY-1). See GpuFieldTelemetry.h;
 *        the line text, aggregate and rotation live in GpuFieldTelemetryCore.h.
 */
#include "GpuFieldTelemetry.h"
#include "GpuFieldTelemetryCore.h"
#include "CrashForensics.h"

#include "../../src/mlv/llrawproc/llrawproc.h"

#include <QByteArray>
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDeadlineTimer>
#include <QDir>
#include <QFileInfo>
#include <QMutex>
#include <QMutexLocker>
#include <QRandomGenerator>
#include <QSysInfo>
#include <QThreadPool>
#include <QWaitCondition>

#include <atomic>
#include <cstdlib>
#include <cstring>
#include <string>
#include <utility>
#include <vector>

#ifdef Q_OS_WIN
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#endif

#ifndef MLVAPP_GIT_SHA
#  define MLVAPP_GIT_SHA "unknown"
#endif

namespace GpuFieldTelemetry {

namespace {

namespace core = GpuFieldTelemetryCore;

constexpr qint64 kAfterFaultWaitMs = 5000;
constexpr qint64 kAfterFaultDebounceMs = 10000;
constexpr qint64 kSessionEndWorkerWaitMs = 2000;
constexpr std::size_t kMaxPendingLines = 64;

struct State
{
    QMutex mutex;
    bool installed = false;
    QString logsDir;
    core::SessionInfo session;
    std::string cudaModel;
    bool glIdentitySet = false;
    core::StageMap stages;
    std::vector<core::Fault> unwrittenFaults;
    unsigned long long faultsDropped = 0;
    bool haveClip = false;
    bool clipWritten = true;
    core::ClipInfo clip;
    std::vector<std::string> pendingLines;
    unsigned long long flushSeq = 0;
    std::function<void(const CudaFault &)> listener;
    bool afterFaultPending = false;
    bool haveLastAfterFaultRun = false;
    qint64 lastAfterFaultRunAtMs = 0;
    bool sessionEnded = false;
};

State & state()
{
    static State s;
    return s;
}

/* Serialises the append + rotation of concurrent flushes (GUI and worker). */
QMutex & writeMutex()
{
    static QMutex m;
    return m;
}

/* The after-fault worker waits here; session end wakes it so exit is not held. */
QMutex g_waitMutex;
QWaitCondition g_waitCondition;
bool g_shuttingDown = false;

std::atomic<bool> g_sessionEndDone{false};
std::atomic<bool> g_aboutToQuitConnected{false};

/* Created on the first fault or recovery only: no thread exists otherwise. */
QThreadPool * workerPool()
{
    static QThreadPool * pool = []() {
        QThreadPool * p = new QThreadPool();
        p->setMaxThreadCount(1);
        return p;
    }();
    return pool;
}
std::atomic<bool> g_workerPoolCreated{false};

QThreadPool * startedWorkerPool()
{
    g_workerPoolCreated.store(true);
    return workerPool();
}

std::string nowIsoUtc()
{
    return QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs).toStdString();
}

double nowUtcMs()
{
    return static_cast<double>(QDateTime::currentMSecsSinceEpoch());
}

std::string envValue(const char * name)
{
    const char * v = std::getenv(name);
    if (!v) return "unset";
    return core::sanitize(v, 32);
}

/* Returns false if session end interrupted the wait. */
bool waitUnlessShuttingDown(qint64 ms)
{
    QMutexLocker locker(&g_waitMutex);
    QDeadlineTimer deadline(ms);
    while (!g_shuttingDown)
    {
        if (!g_waitCondition.wait(&g_waitMutex, deadline)) break;
    }
    return !g_shuttingDown;
}

std::string reconDescribe()
{
    llrpGpuPlaybackReconBackendInfo_t info;
    if (!llrpGpuPlaybackReconGetBackendInfo(&info) || !info.available) return std::string();
    return core::gpuModelFromCudaDescribe(info.description);
}

#ifdef Q_OS_WIN
typedef HANDLE EvtHandle;
typedef EvtHandle (WINAPI * EvtQueryFn)(EvtHandle, LPCWSTR, LPCWSTR, DWORD);
typedef BOOL (WINAPI * EvtNextFn)(EvtHandle, DWORD, EvtHandle *, DWORD, DWORD, PDWORD);
typedef BOOL (WINAPI * EvtCloseFn)(EvtHandle);
typedef BOOL (WINAPI * EvtRenderFn)(EvtHandle, EvtHandle, DWORD, DWORD, PVOID, PDWORD, PDWORD);
constexpr DWORD kEvtQueryChannelPath = 0x1;
constexpr DWORD kEvtQueryForwardDirection = 0x100;
constexpr DWORD kEvtRenderEventXml = 1;
constexpr DWORD kEvtNextTimeoutMs = 1000;
constexpr int kMaxSystemLogEvents = 4096;

/* The value of attribute `name` (single- or double-quoted) after `from`. */
std::string xmlAttribute(const std::wstring & xml, const wchar_t * element, const wchar_t * name)
{
    const std::size_t at = xml.find(element);
    if (at == std::wstring::npos) return std::string();
    const std::size_t attr = xml.find(name, at);
    if (attr == std::wstring::npos) return std::string();
    std::size_t q = attr + std::wcslen(name);
    if (q >= xml.size()) return std::string();
    const wchar_t quote = xml[q];
    if (quote != L'\'' && quote != L'"') return std::string();
    const std::size_t end = xml.find(quote, q + 1);
    if (end == std::wstring::npos) return std::string();
    std::string out;
    for (std::size_t i = q + 1; i < end && out.size() < 40; ++i)
        out.push_back(xml[i] < 0x80 ? static_cast<char>(xml[i]) : '?');
    return core::sanitize(out);
}

int xmlEventId(const std::wstring & xml)
{
    const std::size_t at = xml.find(L"<EventID");
    if (at == std::wstring::npos) return -1;
    const std::size_t gt = xml.find(L'>', at);
    if (gt == std::wstring::npos) return -1;
    int id = 0;
    bool any = false;
    for (std::size_t i = gt + 1; i < xml.size() && xml[i] >= L'0' && xml[i] <= L'9'; ++i)
    {
        id = id * 10 + (xml[i] - L'0');
        any = true;
    }
    return any ? id : -1;
}

void noteEvent(core::SystemLogEvents & e, const std::string & utc)
{
    e.count += 1;
    if (e.firstUtc.empty()) e.firstUtc = utc;
    e.lastUtc = utc;
}

/* nvlddmkm 153 / Display 4101 in the System log since session start. Non-admin
 * read; never polled; at most one EvtNext wait of kEvtNextTimeoutMs per batch. */
core::SystemLogResult querySystemLog(const std::string & sinceUtc)
{
    core::SystemLogResult r;
    r.status = "unavailable";
    HMODULE dll = LoadLibraryW(L"wevtapi.dll");
    if (!dll) return r;
    const EvtQueryFn evtQuery = reinterpret_cast<EvtQueryFn>(reinterpret_cast<void *>(GetProcAddress(dll, "EvtQuery")));
    const EvtNextFn evtNext = reinterpret_cast<EvtNextFn>(reinterpret_cast<void *>(GetProcAddress(dll, "EvtNext")));
    const EvtCloseFn evtClose = reinterpret_cast<EvtCloseFn>(reinterpret_cast<void *>(GetProcAddress(dll, "EvtClose")));
    const EvtRenderFn evtRender = reinterpret_cast<EvtRenderFn>(reinterpret_cast<void *>(GetProcAddress(dll, "EvtRender")));
    if (!evtQuery || !evtNext || !evtClose || !evtRender)
    {
        FreeLibrary(dll);
        return r;
    }
    const std::wstring since = QString::fromStdString(sinceUtc).toStdWString();
    const std::wstring query =
        L"*[System[((Provider[@Name='nvlddmkm'] and EventID=153) or "
        L"(Provider[@Name='Display'] and EventID=4101)) and "
        L"TimeCreated[@SystemTime>='" + since + L"']]]";
    EvtHandle results = evtQuery(nullptr, L"System", query.c_str(),
                                 kEvtQueryChannelPath | kEvtQueryForwardDirection);
    if (!results)
    {
        r.status = GetLastError() == ERROR_ACCESS_DENIED ? "access_denied" : "unavailable";
        FreeLibrary(dll);
        return r;
    }
    r.status = "ok";
    std::vector<wchar_t> buffer(4096);
    int seen = 0;
    while (seen < kMaxSystemLogEvents)
    {
        EvtHandle events[16];
        DWORD returned = 0;
        if (!evtNext(results, 16, events, kEvtNextTimeoutMs, 0, &returned))
        {
            const DWORD e = GetLastError();
            if (e != ERROR_NO_MORE_ITEMS)
                r.status = e == ERROR_TIMEOUT ? "timeout" : (e == ERROR_ACCESS_DENIED ? "access_denied" : "unavailable");
            break;
        }
        for (DWORD i = 0; i < returned; ++i)
        {
            DWORD used = 0;
            DWORD properties = 0;
            BOOL ok = evtRender(nullptr, events[i], kEvtRenderEventXml,
                                static_cast<DWORD>(buffer.size() * sizeof(wchar_t)), buffer.data(), &used, &properties);
            if (!ok && GetLastError() == ERROR_INSUFFICIENT_BUFFER && used < (1u << 20))
            {
                buffer.resize(used / sizeof(wchar_t) + 1);
                ok = evtRender(nullptr, events[i], kEvtRenderEventXml,
                               static_cast<DWORD>(buffer.size() * sizeof(wchar_t)), buffer.data(), &used, &properties);
            }
            if (ok)
            {
                const std::wstring xml(buffer.data(), wcsnlen(buffer.data(), buffer.size()));
                const int id = xmlEventId(xml);
                const std::string utc = xmlAttribute(xml, L"<TimeCreated", L"SystemTime=");
                if (id == 153) noteEvent(r.nvlddmkm153, utc);
                else if (id == 4101) noteEvent(r.display4101, utc);
            }
            evtClose(events[i]);
        }
        seen += static_cast<int>(returned);
    }
    evtClose(results);
    FreeLibrary(dll);
    return r;
}
#else
core::SystemLogResult querySystemLog(const std::string &)
{
    core::SystemLogResult r;
    r.status = "unavailable";
    return r;
}
#endif

void flushImpl(const char * reason, bool withSystemLog)
{
    State & s = state();
    QString dir;
    std::string since;
    {
        QMutexLocker locker(&s.mutex);
        if (!s.installed) return;
        dir = s.logsDir;
        since = s.session.startUtc;
    }
    core::SystemLogResult systemLog;
    if (withSystemLog) systemLog = querySystemLog(since);
    const std::string cudaModel = reconDescribe();

    std::vector<std::string> lines;
    {
        QMutexLocker locker(&s.mutex);
        if (!cudaModel.empty()) s.cudaModel = cudaModel;
        s.session.gpuModel = s.cudaModel.empty() ? s.session.glRenderer : s.cudaModel;
        const std::string utc = nowIsoUtc();
        const std::string & id = s.session.sessionId;
        lines.push_back(core::formatHeader(s.session, utc));
        lines.push_back(core::formatFlush(id, utc, reason, ++s.flushSeq, s.faultsDropped));
        if (s.haveClip && !s.clipWritten)
        {
            lines.push_back(core::formatClip(id, utc, s.clip));
            s.clipWritten = true;
        }
        for (const auto & entry : s.stages.entries())
            lines.push_back(core::formatStage(id, utc, entry.second));
        for (const core::Fault & f : s.unwrittenFaults)
            lines.push_back(core::formatFault(id, utc, f));
        s.unwrittenFaults.clear();
        for (std::string & line : s.pendingLines)
            lines.push_back(std::move(line));
        s.pendingLines.clear();
        if (withSystemLog) lines.push_back(core::formatSystemLog(id, utc, reason, systemLog));
    }

    QMutexLocker writeLocker(&writeMutex());
    const std::filesystem::path live =
        QDir(dir).filePath(QString::fromLatin1(core::kLogFileName)).toStdWString();
    core::rotateIfNeeded(live, core::kRotateBytes, core::kRotateKeep);
    core::appendLines(live, lines);
}

void sessionEnd()
{
    bool expected = false;
    if (!g_sessionEndDone.compare_exchange_strong(expected, true)) return;
    {
        QMutexLocker locker(&state().mutex);
        state().sessionEnded = true;
    }
    {
        QMutexLocker locker(&g_waitMutex);
        g_shuttingDown = true;
        g_waitCondition.wakeAll();
    }
    if (g_workerPoolCreated.load()) workerPool()->waitForDone(kSessionEndWorkerWaitMs);
    flushImpl("session_end", true);
}

void sessionEndPostRoutine()
{
    sessionEnd();
}

/* QApplication does not exist yet when main() calls install(); the first
 * GUI-thread note after it does connects aboutToQuit. qAddPostRoutine (set in
 * install) is the backstop for runs that never emit aboutToQuit. */
void connectAboutToQuitOnce()
{
    QCoreApplication * app = QCoreApplication::instance();
    if (!app) return;
    bool expected = false;
    if (!g_aboutToQuitConnected.compare_exchange_strong(expected, true)) return;
    QObject::connect(app, &QCoreApplication::aboutToQuit, app, []() { sessionEnd(); });
}

void scheduleAfterFaultFlush()
{
    State & s = state();
    qint64 delayMs = 0;
    {
        QMutexLocker locker(&s.mutex);
        if (!s.installed || s.sessionEnded || s.afterFaultPending) return;
        const qint64 now = QDateTime::currentMSecsSinceEpoch();
        qint64 runAt = now + kAfterFaultWaitMs;
        if (s.haveLastAfterFaultRun && runAt < s.lastAfterFaultRunAtMs + kAfterFaultDebounceMs)
            runAt = s.lastAfterFaultRunAtMs + kAfterFaultDebounceMs;
        s.haveLastAfterFaultRun = true;
        s.lastAfterFaultRunAtMs = runAt;
        s.afterFaultPending = true;
        delayMs = runAt - now;
    }
    startedWorkerPool()->start([delayMs]() {
        if (!waitUnlessShuttingDown(delayMs)) return; // session_end writes it instead
        {
            QMutexLocker locker(&state().mutex);
            state().afterFaultPending = false;
        }
        flushImpl("after_fault", true);
    });
}

void reconStageSink(const char * stage, int rc, double ms, int cudaCode,
                    const char * cudaName, const char * site)
{
    if (rc == 0)
    {
        noteStage("recon", stage, ms);
        return;
    }
    CudaFault fault;
    fault.dll = "recon";
    fault.stage = stage;
    fault.rc = rc;
    fault.cudaCode = cudaCode;
    fault.cudaName = cudaName;
    fault.site = site;
    noteFault(fault);
}

} // namespace

bool enabled()
{
    static const bool on = GpuFieldTelemetryCore::parseEnabled(std::getenv(GpuFieldTelemetryCore::kEnvEnable));
    return on;
}

void install()
{
    if (!enabled()) return;
    const QString dir = CrashForensics::logsDirectoryPath();
    if (dir.isEmpty()) return;
    State & s = state();
    {
        QMutexLocker locker(&s.mutex);
        if (s.installed) return;
        s.installed = true;
        s.logsDir = dir;
        s.session.sessionId = QString::number(QRandomGenerator::global()->generate64(), 16)
                                  .rightJustified(16, QLatin1Char('0')).toStdString();
        s.session.startUtc = nowIsoUtc();
        s.session.appBuild = core::sanitize(
            (QCoreApplication::applicationVersion() + QStringLiteral("+")
             + QString::fromLatin1(MLVAPP_GIT_SHA).left(12)).toStdString(), 64);
        s.session.osBuild = core::sanitize(QSysInfo::kernelVersion().toStdString(), 64);
        s.session.glResetStrategy = "unknown";
        s.session.glResetStatus = "not_queried";
        s.session.launchTimingEnv = "enabled=" + envValue("MLVAPP_CUDA_LAUNCH_TIMING")
            + " min_ms=" + envValue("MLVAPP_CUDA_LAUNCH_TIMING_MIN_MS");
    }
    llrpSetGpuStageTelemetrySink(&reconStageSink);
    qAddPostRoutine(&sessionEndPostRoutine);
    connectAboutToQuitOnce();
}

void noteStage(const char * dll, const char * stage, double ms)
{
    if (!enabled()) return;
    State & s = state();
    QMutexLocker locker(&s.mutex);
    s.stages.add(dll, stage, ms, 0);
}

void noteFault(const CudaFault & fault)
{
    if (!enabled()) return;
    core::Fault f;
    f.dll = core::sanitize(fault.dll ? fault.dll : "?", 16);
    f.stage = core::sanitize(fault.stage ? fault.stage : "?", 48);
    f.rc = fault.rc;
    f.cudaCode = fault.cudaCode;
    f.cudaName = core::sanitize(fault.cudaName, 47);
    f.site = core::sanitize(fault.site, 95);
    f.utcMs = nowUtcMs();

    State & s = state();
    std::function<void(const CudaFault &)> listener;
    bool installed = false;
    {
        QMutexLocker locker(&s.mutex);
        s.stages.add(fault.dll, fault.stage, -1.0,
                     fault.cudaCode != 0 ? fault.cudaCode : (fault.rc != 0 ? fault.rc : -1));
        if (s.unwrittenFaults.size() < core::kMaxFaults) s.unwrittenFaults.push_back(f);
        else ++s.faultsDropped;
        listener = s.listener;
        installed = s.installed;
    }
    if (installed) scheduleAfterFaultFlush();
    if (listener)
    {
        CudaFault copy = fault;
        copy.utcMs = f.utcMs;
        listener(copy);
    }
}

bool isDeviceLossCode(int cudaCode)
{
    return GpuFieldTelemetryCore::isDeviceLossCode(cudaCode);
}

void setFaultListener(std::function<void(const CudaFault &)> listener)
{
    State & s = state();
    QMutexLocker locker(&s.mutex);
    s.listener = std::move(listener);
}

void noteRecovery(const char * what, const char * outcome)
{
    if (!enabled()) return;
    State & s = state();
    {
        QMutexLocker locker(&s.mutex);
        if (!s.installed || s.sessionEnded) return;
        if (s.pendingLines.size() < kMaxPendingLines)
            s.pendingLines.push_back(core::formatRecovery(s.session.sessionId, nowIsoUtc(), what, outcome));
    }
    startedWorkerPool()->start([]() { flushImpl("recovery", false); });
}

void noteClip(int width, int height, int bitsPerPixel, bool compressed,
              bool dualIsoGpu, const QString & basename)
{
    if (!enabled()) return;
    State & s = state();
    {
        QMutexLocker locker(&s.mutex);
        if (!s.installed) return;
        s.clip.width = width;
        s.clip.height = height;
        s.clip.bitsPerPixel = bitsPerPixel;
        s.clip.compressed = compressed;
        s.clip.dualIsoGpu = dualIsoGpu;
        s.clip.clipHash = clipHash(basename).toStdString();
        s.haveClip = true;
        s.clipWritten = false;
    }
    connectAboutToQuitOnce();
    flushImpl("clip_open", false);
}

void noteGlIdentity(const QString & renderer, const QString & vendor,
                    const QString & version, const QString & resetStrategy,
                    const QString & resetStatus)
{
    if (!enabled()) return;
    State & s = state();
    {
        QMutexLocker locker(&s.mutex);
        if (s.glIdentitySet) return;
        s.glIdentitySet = true;
        s.session.glRenderer = core::sanitize(renderer.toStdString(), 96);
        s.session.glVendor = core::sanitize(vendor.toStdString(), 64);
        s.session.glVersion = core::sanitize(version.toStdString(), 96);
        s.session.gpuDriver = core::driverFromGlVersion(version.toStdString().c_str());
        s.session.glResetStrategy = core::sanitize(resetStrategy.toStdString(), 32);
        s.session.glResetStatus = core::sanitize(resetStatus.toStdString(), 32);
    }
    connectAboutToQuitOnce();
}

void flush(const char * reason)
{
    if (!enabled()) return;
    if (reason && std::strcmp(reason, "session_end") == 0)
    {
        sessionEnd();
        return;
    }
    flushImpl(reason ? reason : "manual", reason && std::strcmp(reason, "after_fault") == 0);
}

QString clipHash(const QString & basenameOrPath)
{
    const QString name = QFileInfo(basenameOrPath).fileName();
    return QString::fromLatin1(
        QCryptographicHash::hash(name.toUtf8(), QCryptographicHash::Sha256).toHex().left(12));
}

} // namespace GpuFieldTelemetry
