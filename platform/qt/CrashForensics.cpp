/*!
 * \file CrashForensics.cpp
 * \brief Implementation of the always-on rotating log sink, the Windows
 *        minidump handler, and the run-metadata helper.
 *
 * See CrashForensics.h for the contract.
 */

#include "CrashForensics.h"

#include "Phase3Breadcrumbs.h"

#include <QByteArray>
#include <QCoreApplication>
#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QList>
#include <QMutex>
#include <QMutexLocker>
#include <QProcess>
#include <QRegularExpression>
#include <QSettings>
#include "AutomationSettings.h"
#include <QStandardPaths>
#include <QString>
#include <QSysInfo>
#include <QThread>
#include <QtGlobal>

#include <atomic>
#include <cstdio>
#include <cstdlib>

/* The authoritative clean-build path generates this header in OUT_PWD. Use
 * it for run_metadata as well as the title/build stamp; otherwise a perfectly
 * stamped artifact can still report build_sha="unknown" when qmake-time git
 * discovery was unavailable. */
#if defined(__has_include)
#  if __has_include("build_buildinfo.h")
#    include "build_buildinfo.h"
#  endif
#endif

#ifdef Q_OS_WIN
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <windows.h>
#  include <dbghelp.h>
#endif

/* Build-time git SHA, passed in via `-DMLVAPP_GIT_SHA=...` in the .pro
 * file.  Fall back to "unknown" when the build system did not provide
 * one (e.g. standalone test builds). */
#ifndef MLVAPP_GIT_SHA
#  define MLVAPP_GIT_SHA "unknown"
#endif

namespace {

static QMutex g_logMutex;
static QString g_logFilePath;
static QString g_logsDir;
static QStringList g_commandLine;
static std::atomic<bool> g_installed{false};
static QtMessageHandler g_previousHandler = nullptr;
static QMutex g_machineFingerprintMutex;
static bool g_machineFingerprintCached = false;
static QJsonObject g_machineFingerprintCache;

static const char * kPerformanceFieldLogSetting =
    "PerformanceProfiling/FieldLogEnabled";
static const char * kCudaPlaybackProfilingSetting =
    "PerformanceProfiling/CudaPlaybackProfilingEnabled";
static const char * kDngAsyncCompressionProfilingSetting =
    "PerformanceProfiling/DngAsyncCompressionProfilingEnabled";
static const char * kDngAsyncCompressionQueueDepthSetting =
    "PerformanceProfiling/DngAsyncCompressionQueueDepth";
static const char * kDngAsyncCompressionThreadCountSetting =
    "PerformanceProfiling/DngAsyncCompressionThreadCount";

bool envFlagEnabled(const char *name)
{
    const QByteArray value = qgetenv(name).trimmed().toLower();
    return !value.isEmpty()
        && value != QByteArrayLiteral("0")
        && value != QByteArrayLiteral("false")
        && value != QByteArrayLiteral("off")
        && value != QByteArrayLiteral("no");
}

QJsonValue stringOrNull(const QString & value)
{
    return value.isEmpty() ? QJsonValue() : QJsonValue(value);
}

QJsonValue integerOrNull(qint64 value)
{
    return value < 0 ? QJsonValue() : QJsonValue(value);
}

/* build_sha (MLVAPP_GIT_SHA) identifies the commit the build was compiled
 * from; it says nothing about the bytes actually on disk right now. This
 * hashes the running executable itself, so a rebuilt-but-unstamped binary
 * or a stale copy of the exe is distinguishable from the commit it claims. */
QString executableSha256Hex()
{
    QFile exe(QCoreApplication::applicationFilePath());
    if (!exe.open(QIODevice::ReadOnly)) return QString();
    return QString::fromLatin1(
        QCryptographicHash::hash(exe.readAll(), QCryptographicHash::Sha256)
            .toHex());
}

QString cpuFeatureList()
{
    QStringList features;
#if defined(__GNUC__) && (defined(__i386__) || defined(__x86_64__))
    __builtin_cpu_init();
    if (__builtin_cpu_supports("sse4.2")) features << QStringLiteral("sse4.2");
    if (__builtin_cpu_supports("avx"))    features << QStringLiteral("avx");
    if (__builtin_cpu_supports("avx2"))   features << QStringLiteral("avx2");
    if (__builtin_cpu_supports("avx512f")) features << QStringLiteral("avx512f");
#endif
    return features.join(QLatin1Char(','));
}

QString cpuModelName()
{
#ifdef Q_OS_WIN
    QSettings cpuKey(QStringLiteral(
        "HKEY_LOCAL_MACHINE\\HARDWARE\\DESCRIPTION\\System\\CentralProcessor\\0"),
        QSettings::NativeFormat);
    const QString registryName =
        cpuKey.value(QStringLiteral("ProcessorNameString")).toString().trimmed();
    if (!registryName.isEmpty()) return registryName;
#endif
    const QString processorIdentifier =
        qEnvironmentVariable("PROCESSOR_IDENTIFIER").trimmed();
    if (!processorIdentifier.isEmpty()) return processorIdentifier;
    return QSysInfo::currentCpuArchitecture();
}

int physicalCpuCoreCount()
{
#ifdef Q_OS_WIN
    DWORD bytes = 0;
    if (!GetLogicalProcessorInformationEx(RelationProcessorCore, nullptr, &bytes)
        && GetLastError() == ERROR_INSUFFICIENT_BUFFER
        && bytes > 0) {
        QByteArray buffer(static_cast<int>(bytes), 0);
        PSYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX info =
            reinterpret_cast<PSYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX>(buffer.data());
        if (GetLogicalProcessorInformationEx(RelationProcessorCore, info, &bytes)) {
            int count = 0;
            char *cursor = buffer.data();
            const char *end = buffer.constData() + bytes;
            while (cursor < end) {
                PSYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX entry =
                    reinterpret_cast<PSYSTEM_LOGICAL_PROCESSOR_INFORMATION_EX>(cursor);
                if (entry->Relationship == RelationProcessorCore) {
                    ++count;
                }
                if (entry->Size == 0) break;
                cursor += entry->Size;
            }
            if (count > 0) return count;
        }
    }
#endif
    return QThread::idealThreadCount();
}

qint64 totalRamMb()
{
#ifdef Q_OS_WIN
    MEMORYSTATUSEX memoryStatus;
    memset(&memoryStatus, 0, sizeof(memoryStatus));
    memoryStatus.dwLength = sizeof(memoryStatus);
    if (GlobalMemoryStatusEx(&memoryStatus)) {
        return static_cast<qint64>(memoryStatus.ullTotalPhys / (1024ULL * 1024ULL));
    }
#endif
    return -1;
}

QString resolveNvidiaSmi()
{
    const QString fromPath = QStandardPaths::findExecutable(QStringLiteral("nvidia-smi"));
    if (!fromPath.isEmpty()) return fromPath;

    QStringList candidates;
#ifdef Q_OS_WIN
    const QString windowsDir = qEnvironmentVariable("WINDIR");
    if (!windowsDir.isEmpty()) {
        candidates << QDir(windowsDir).absoluteFilePath(
            QStringLiteral("System32/nvidia-smi.exe"));
    }
    const QString programFiles = qEnvironmentVariable("ProgramFiles");
    if (!programFiles.isEmpty()) {
        candidates << QDir(programFiles).absoluteFilePath(
            QStringLiteral("NVIDIA Corporation/NVSMI/nvidia-smi.exe"));
    }
    const QString programW6432 = qEnvironmentVariable("ProgramW6432");
    if (!programW6432.isEmpty()) {
        candidates << QDir(programW6432).absoluteFilePath(
            QStringLiteral("NVIDIA Corporation/NVSMI/nvidia-smi.exe"));
    }
#endif
    for (const QString &candidate : candidates) {
        if (!candidate.isEmpty() && QFileInfo::exists(candidate)) {
            return candidate;
        }
    }
    return QString();
}

QJsonObject nvidiaGpuFingerprint()
{
    QJsonObject gpu;
    const QString nvidiaSmi = resolveNvidiaSmi();
    if (nvidiaSmi.isEmpty()) return gpu;

    QProcess process;
    process.setProgram(nvidiaSmi);
    process.setArguments(QStringList()
        << QStringLiteral("--query-gpu=name,driver_version,compute_cap,memory.total")
        << QStringLiteral("--format=csv,noheader,nounits"));
    process.start();
    if (!process.waitForFinished(2500)) {
        process.kill();
        process.waitForFinished(500);
        return gpu;
    }
    if (process.exitStatus() != QProcess::NormalExit || process.exitCode() != 0) {
        return gpu;
    }

    const QString output = QString::fromLocal8Bit(process.readAllStandardOutput());
    const QStringList lines = output.split(QRegularExpression(QStringLiteral("[\\r\\n]+")),
                                           Qt::SkipEmptyParts);
    if (lines.isEmpty()) return gpu;

    const QStringList fields = lines.first().split(QLatin1Char(','));
    if (fields.size() < 4) return gpu;

    bool vramOk = false;
    const qint64 vramMb = fields.at(3).trimmed().toLongLong(&vramOk);
    gpu.insert(QStringLiteral("gpu_name"), fields.at(0).trimmed());
    gpu.insert(QStringLiteral("gpu_driver_version"), fields.at(1).trimmed());
    gpu.insert(QStringLiteral("gpu_compute_capability"), fields.at(2).trimmed());
    gpu.insert(QStringLiteral("gpu_vram_total_mb"),
               vramOk ? QJsonValue(vramMb) : QJsonValue());
    return gpu;
}

QString levelTag(QtMsgType type)
{
    switch (type) {
        case QtDebugMsg:    return QStringLiteral("DEBUG");
        case QtInfoMsg:     return QStringLiteral("INFO");
        case QtWarningMsg:  return QStringLiteral("WARNING");
        case QtCriticalMsg: return QStringLiteral("CRITICAL");
        case QtFatalMsg:    return QStringLiteral("FATAL");
    }
    return QStringLiteral("UNKNOWN");
}

QString todaysLogFileName()
{
    return QStringLiteral("mlvapp-")
        + QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd"))
        + QStringLiteral(".log");
}

QString defaultPerformanceFieldLogPath()
{
    if (g_logsDir.isEmpty()) return QString();
    return QDir(g_logsDir).absoluteFilePath(
        QStringLiteral("mlvapp-perf-field-%1.jsonl")
            .arg(QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd"))));
}

QString defaultExportStageProfilePath()
{
    if (g_logsDir.isEmpty()) return QString();
    return QDir(g_logsDir).absoluteFilePath(
        QStringLiteral("mlvapp-export-stage-profile-%1.json")
            .arg(QDateTime::currentDateTimeUtc().toString(
                QStringLiteral("yyyyMMdd-HHmmss-zzz"))));
}

void putNativePathEnv(const char *name, const QString &path)
{
    if (path.isEmpty()) return;
    qputenv(name, QDir::toNativeSeparators(path).toUtf8());
}

bool envEmpty(const char *name)
{
    return qEnvironmentVariable(name).trimmed().isEmpty();
}

int boundedProfilingSettingInt(const char *settingKey,
                               int defaultValue,
                               int minimum,
                               int maximum)
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    bool ok = false;
    int value = set.value(QString::fromLatin1(settingKey), defaultValue).toInt(&ok);
    if (!ok) value = defaultValue;
    if (value < minimum) return minimum;
    if (value > maximum) return maximum;
    return value;
}

bool profilingSettingEnabled(const char *settingKey)
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return set.value(QString::fromLatin1(settingKey), false).toBool();
}

void setManagedEnv(const char *name, const QByteArray &value, const char *managedName)
{
    qputenv(name, value);
    qputenv(managedName, QByteArrayLiteral("1"));
}

void unsetManagedEnv(const char *name, const char *managedName)
{
    if (!envFlagEnabled(managedName)) return;
    qunsetenv(name);
    qunsetenv(managedName);
}

void applyManagedExportStageProfilerEnvironment()
{
    if (!envFlagEnabled("MLVAPP_EXPORT_STAGE_PROFILER")) {
        qputenv("MLVAPP_EXPORT_STAGE_PROFILER", QByteArrayLiteral("1"));
        qputenv("MLVAPP_EXPORT_STAGE_PROFILER_GUI_MANAGED", QByteArrayLiteral("1"));
    }
    if (envEmpty("MLVAPP_EXPORT_STAGE_PROFILE_FILE")
     || envFlagEnabled("MLVAPP_EXPORT_STAGE_PROFILE_FILE_GUI_MANAGED")) {
        putNativePathEnv("MLVAPP_EXPORT_STAGE_PROFILE_FILE",
                         defaultExportStageProfilePath());
        qputenv("MLVAPP_EXPORT_STAGE_PROFILE_FILE_GUI_MANAGED",
                QByteArrayLiteral("1"));
    }
}

void clearManagedExportStageProfilerEnvironmentIfUnneeded()
{
    if (envFlagEnabled("MLVAPP_PERF_FIELD_LOG")
     || profilingSettingEnabled(kPerformanceFieldLogSetting)
     || profilingSettingEnabled(kDngAsyncCompressionProfilingSetting)) {
        return;
    }
    if (envFlagEnabled("MLVAPP_EXPORT_STAGE_PROFILER_GUI_MANAGED")) {
        qputenv("MLVAPP_EXPORT_STAGE_PROFILER", QByteArrayLiteral("0"));
        qunsetenv("MLVAPP_EXPORT_STAGE_PROFILER_GUI_MANAGED");
    }
    if (envFlagEnabled("MLVAPP_EXPORT_STAGE_PROFILE_FILE_GUI_MANAGED")) {
        qunsetenv("MLVAPP_EXPORT_STAGE_PROFILE_FILE");
        qunsetenv("MLVAPP_EXPORT_STAGE_PROFILE_FILE_GUI_MANAGED");
    }
}

void pruneOldLogs(const QString & logsDir, int keep)
{
    QDir dir(logsDir);
    if (!dir.exists()) return;
    const QRegularExpression pattern(
        QStringLiteral("^mlvapp-\\d{8}\\.log$"));
    QFileInfoList entries = dir.entryInfoList(
        QStringList() << QStringLiteral("mlvapp-*.log"),
        QDir::Files,
        QDir::Name);
    QFileInfoList matching;
    for (const QFileInfo & info : entries) {
        if (pattern.match(info.fileName()).hasMatch()) {
            matching.append(info);
        }
    }
    if (matching.size() <= keep) return;
    // Sort ascending by filename; older files come first. Delete all but
    // the last `keep`.
    std::sort(matching.begin(), matching.end(),
              [](const QFileInfo & a, const QFileInfo & b) {
                  return a.fileName() < b.fileName();
              });
    const int deleteCount = matching.size() - keep;
    for (int i = 0; i < deleteCount; ++i) {
        QFile::remove(matching.at(i).absoluteFilePath());
    }
}

void writeLogLine(QtMsgType type, const QMessageLogContext & ctx, const QString & message)
{
    if (g_logFilePath.isEmpty()) return;

    const QString timestamp =
        QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs);
    const QString thread = QStringLiteral("0x%1")
        .arg(reinterpret_cast<quintptr>(QThread::currentThreadId()), 0, 16);
    QString location;
    if (ctx.file && *ctx.file) {
        location = QStringLiteral(" (%1:%2)")
            .arg(QString::fromLocal8Bit(ctx.file))
            .arg(ctx.line);
    }

    // Sanitise message (strip stray CR/LF within a line so each record
    // stays on a single line).
    QString sanitized = message;
    sanitized.replace(QLatin1Char('\r'), QLatin1Char(' '));
    sanitized.replace(QLatin1Char('\n'), QStringLiteral(" | "));

    const QString line = QStringLiteral("[%1] [%2] [%3] %4%5\n")
        .arg(timestamp)
        .arg(levelTag(type))
        .arg(thread)
        .arg(sanitized)
        .arg(location);

    QMutexLocker locker(&g_logMutex);
    QFile file(g_logFilePath);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Append | QIODevice::Text)) {
        return;
    }
    const QByteArray bytes = line.toUtf8();
    file.write(bytes);
    file.flush();
    file.close();
}

void messageHandler(QtMsgType type,
                    const QMessageLogContext & context,
                    const QString & message)
{
    writeLogLine(type, context, message);

    // Chain to the previous handler (if any) so stderr/stdout output is
    // preserved for developers running the app from a terminal.
    if (g_previousHandler) {
        g_previousHandler(type, context, message);
    }

    if (type == QtFatalMsg) {
        // Mirror Qt's default behaviour: abort after writing.
        std::abort();
    }
}

#ifdef Q_OS_WIN
/* Emergency crash-time log writer.  We CANNOT use qInfo/QFile here
 * because the heap may be corrupt; use plain Win32 calls instead. */
void emergencyAppendLine(const wchar_t * logPath, const char * line)
{
    HANDLE h = CreateFileW(logPath,
                           FILE_APPEND_DATA,
                           FILE_SHARE_READ,
                           nullptr,
                           OPEN_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL,
                           nullptr);
    if (h == INVALID_HANDLE_VALUE) return;
    DWORD written = 0;
    WriteFile(h, line, static_cast<DWORD>(strlen(line)), &written, nullptr);
    FlushFileBuffers(h);
    CloseHandle(h);
}

LONG WINAPI crashExceptionFilter(EXCEPTION_POINTERS * exceptionInfo)
{
    // Build dump path: <logsDir>/mlvapp-YYYYMMDD-HHMMSS.dmp
    const QString dumpName = QStringLiteral("mlvapp-")
        + QDateTime::currentDateTime().toString(QStringLiteral("yyyyMMdd-HHmmss"))
        + QStringLiteral(".dmp");
    const QString dumpPath = QDir(g_logsDir).absoluteFilePath(dumpName);
    const std::wstring dumpPathW =
        QDir::toNativeSeparators(dumpPath).toStdWString();
    const std::wstring logPathW =
        QDir::toNativeSeparators(g_logFilePath).toStdWString();

    HANDLE file = CreateFileW(dumpPathW.c_str(),
                              GENERIC_WRITE,
                              0,
                              nullptr,
                              CREATE_ALWAYS,
                              FILE_ATTRIBUTE_NORMAL,
                              nullptr);
    bool wroteDump = false;
    DWORD exceptionCode = 0;
    if (exceptionInfo && exceptionInfo->ExceptionRecord) {
        exceptionCode = exceptionInfo->ExceptionRecord->ExceptionCode;
    }
    if (file != INVALID_HANDLE_VALUE) {
        MINIDUMP_EXCEPTION_INFORMATION mei;
        mei.ThreadId = GetCurrentThreadId();
        mei.ExceptionPointers = exceptionInfo;
        mei.ClientPointers = FALSE;
        const MINIDUMP_TYPE dumpType = static_cast<MINIDUMP_TYPE>(
            MiniDumpWithDataSegs | MiniDumpWithHandleData | MiniDumpWithThreadInfo);
        wroteDump = MiniDumpWriteDump(GetCurrentProcess(),
                                      GetCurrentProcessId(),
                                      file,
                                      dumpType,
                                      exceptionInfo ? &mei : nullptr,
                                      nullptr,
                                      nullptr) != FALSE;
        CloseHandle(file);
    }

    // Append a "CRASH:" line to the rotating log directly, because the
    // normal message handler may not be safe at this point.
    char emergencyLine[1024];
    const QByteArray dumpPathUtf8 =
        QDir::toNativeSeparators(dumpPath).toUtf8();
    const QByteArray nowUtf8 =
        QDateTime::currentDateTimeUtc().toString(Qt::ISODateWithMs).toUtf8();
    std::snprintf(emergencyLine, sizeof(emergencyLine),
                  "[%s] [FATAL] [crash] CRASH: minidump at %s, exception code 0x%08lX, dump_written=%d\n",
                  nowUtf8.constData(),
                  dumpPathUtf8.constData(),
                  static_cast<unsigned long>(exceptionCode),
                  wroteDump ? 1 : 0);
    emergencyAppendLine(logPathW.c_str(), emergencyLine);
    Phase3Breadcrumbs::dumpToWindowsLogFile(logPathW.c_str());

    return EXCEPTION_EXECUTE_HANDLER;
}
#endif // Q_OS_WIN

} // namespace

namespace CrashForensics {

QString install(int argc, char * argv[])
{
    bool expected = false;
    if (!g_installed.compare_exchange_strong(expected, true)) {
        return g_logFilePath;
    }

    g_commandLine.clear();
    for (int i = 0; i < argc; ++i) {
        g_commandLine.append(QString::fromLocal8Bit(argv[i]));
    }

    const QString overrideLogDir =
        qEnvironmentVariable("MLVAPP_CRASH_FORENSICS_LOG_DIR").trimmed();
    if (!overrideLogDir.isEmpty()) {
        const QFileInfo overrideInfo(overrideLogDir);
        g_logsDir = overrideInfo.isAbsolute()
            ? QDir::cleanPath(overrideInfo.absoluteFilePath())
            : QDir::cleanPath(QDir::current().absoluteFilePath(overrideLogDir));
    }
    else {
        const QString base =
            QStandardPaths::writableLocation(QStandardPaths::AppDataLocation);
        if (base.isEmpty()) {
            g_installed.store(false);
            return QString();
        }
        g_logsDir = QDir(base).absoluteFilePath(QStringLiteral("logs"));
    }
    QDir().mkpath(g_logsDir);
    pruneOldLogs(g_logsDir, 5);

    g_logFilePath = QDir(g_logsDir).absoluteFilePath(todaysLogFileName());

    g_previousHandler = qInstallMessageHandler(&messageHandler);

#ifdef Q_OS_WIN
    SetUnhandledExceptionFilter(&crashExceptionFilter);
#endif

    return g_logFilePath;
}

QString currentLogFilePath()
{
    return g_logFilePath;
}

QString logsDirectoryPath()
{
    return g_logsDir;
}

QJsonObject machineFingerprintObject()
{
    QMutexLocker locker(&g_machineFingerprintMutex);
    if (g_machineFingerprintCached) return g_machineFingerprintCache;

    const QJsonObject gpu = nvidiaGpuFingerprint();
    QJsonObject root;
    root.insert(QStringLiteral("schema"), QStringLiteral("machine-fingerprint.v1"));
    root.insert(QStringLiteral("build_sha"), QString::fromLatin1(MLVAPP_GIT_SHA));
    root.insert(QStringLiteral("executable_sha256"), stringOrNull(executableSha256Hex()));
    root.insert(QStringLiteral("gpu_name"),
                gpu.contains(QStringLiteral("gpu_name"))
                    ? gpu.value(QStringLiteral("gpu_name"))
                    : QJsonValue());
    root.insert(QStringLiteral("gpu_driver_version"),
                gpu.contains(QStringLiteral("gpu_driver_version"))
                    ? gpu.value(QStringLiteral("gpu_driver_version"))
                    : QJsonValue());
    root.insert(QStringLiteral("gpu_compute_capability"),
                gpu.contains(QStringLiteral("gpu_compute_capability"))
                    ? gpu.value(QStringLiteral("gpu_compute_capability"))
                    : QJsonValue());
    root.insert(QStringLiteral("gpu_vram_total_mb"),
                gpu.contains(QStringLiteral("gpu_vram_total_mb"))
                    ? gpu.value(QStringLiteral("gpu_vram_total_mb"))
                    : QJsonValue());
    root.insert(QStringLiteral("cpu_model"), stringOrNull(cpuModelName()));
    root.insert(QStringLiteral("cpu_cores"), integerOrNull(physicalCpuCoreCount()));
    root.insert(QStringLiteral("cpu_threads"), integerOrNull(QThread::idealThreadCount()));
    root.insert(QStringLiteral("ram_total_mb"), integerOrNull(totalRamMb()));
    root.insert(QStringLiteral("os_version"), stringOrNull(QSysInfo::prettyProductName()));

    g_machineFingerprintCache = root;
    g_machineFingerprintCached = true;
    return g_machineFingerprintCache;
}

QString machineFingerprintJson()
{
    return QString::fromUtf8(
        QJsonDocument(machineFingerprintObject()).toJson(QJsonDocument::Compact));
}

void publishMachineFingerprintEnvironment()
{
    const QJsonObject fp = machineFingerprintObject();
    auto publishString = [&fp](const char * envName, const char * key)
    {
        const QString value = fp.value(QString::fromLatin1(key)).toString();
        qputenv(envName, value.toUtf8());
    };
    auto publishNumber = [&fp](const char * envName, const char * key)
    {
        const QJsonValue value = fp.value(QString::fromLatin1(key));
        qputenv(envName,
                value.isDouble()
                    ? QByteArray::number(static_cast<qint64>(value.toDouble()))
                    : QByteArray());
    };

    qputenv("MLVAPP_MACHINE_FINGERPRINT_JSON",
            QJsonDocument(fp).toJson(QJsonDocument::Compact));
    publishString("MLVAPP_MACHINE_FINGERPRINT_SCHEMA", "schema");
    publishString("MLVAPP_MACHINE_FINGERPRINT_BUILD_SHA", "build_sha");
    publishString("MLVAPP_MACHINE_FINGERPRINT_GPU_NAME", "gpu_name");
    publishString("MLVAPP_MACHINE_FINGERPRINT_GPU_DRIVER_VERSION", "gpu_driver_version");
    publishString("MLVAPP_MACHINE_FINGERPRINT_GPU_COMPUTE_CAPABILITY", "gpu_compute_capability");
    publishNumber("MLVAPP_MACHINE_FINGERPRINT_GPU_VRAM_TOTAL_MB", "gpu_vram_total_mb");
    publishString("MLVAPP_MACHINE_FINGERPRINT_CPU_MODEL", "cpu_model");
    publishNumber("MLVAPP_MACHINE_FINGERPRINT_CPU_CORES", "cpu_cores");
    publishNumber("MLVAPP_MACHINE_FINGERPRINT_CPU_THREADS", "cpu_threads");
    publishNumber("MLVAPP_MACHINE_FINGERPRINT_RAM_TOTAL_MB", "ram_total_mb");
    publishString("MLVAPP_MACHINE_FINGERPRINT_OS_VERSION", "os_version");
}

bool performanceFieldLogSettingsEnabled()
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    return set.value(QString::fromLatin1(kPerformanceFieldLogSetting), false)
        .toBool();
}

void setPerformanceFieldLogSettingsEnabled(bool enabled)
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue(QString::fromLatin1(kPerformanceFieldLogSetting), enabled);
    set.sync();
}

bool performanceFieldLogRuntimeEnabled()
{
    return envFlagEnabled("MLVAPP_PERF_FIELD_LOG")
        || performanceFieldLogSettingsEnabled();
}

QString performanceFieldLogPath()
{
    const QString path = qEnvironmentVariable("MLVAPP_PERF_FIELD_LOG_PATH").trimmed();
    return path.isEmpty() ? defaultPerformanceFieldLogPath() : path;
}

void applyPerformanceFieldLogEnvironment(bool enabled)
{
    if (!enabled) {
        qputenv("MLVAPP_PERF_FIELD_LOG", QByteArrayLiteral("0"));
        if (envFlagEnabled("MLVAPP_PERF_FIELD_LOG_PATH_GUI_MANAGED")) {
            qunsetenv("MLVAPP_PERF_FIELD_LOG_PATH");
        }
        if (envFlagEnabled("MLVAPP_PERF_FIELD_LOG_DIR_GUI_MANAGED")) {
            qunsetenv("MLVAPP_PERF_FIELD_LOG_DIR");
        }
        if (envFlagEnabled("MLVAPP_EXPORT_STAGE_PROFILER_GUI_MANAGED")) {
            clearManagedExportStageProfilerEnvironmentIfUnneeded();
        }
        qunsetenv("MLVAPP_PERF_FIELD_LOG_GUI_MANAGED");
        qunsetenv("MLVAPP_PERF_FIELD_LOG_PATH_GUI_MANAGED");
        qunsetenv("MLVAPP_PERF_FIELD_LOG_DIR_GUI_MANAGED");
        return;
    }

    publishMachineFingerprintEnvironment();
    qputenv("MLVAPP_PERF_FIELD_LOG", QByteArrayLiteral("1"));
    qputenv("MLVAPP_PERF_FIELD_LOG_GUI_MANAGED", QByteArrayLiteral("1"));

    if (!g_logsDir.isEmpty()) {
        putNativePathEnv("MLVAPP_PERF_FIELD_LOG_DIR", g_logsDir);
        qputenv("MLVAPP_PERF_FIELD_LOG_DIR_GUI_MANAGED", QByteArrayLiteral("1"));
    }
    if (envEmpty("MLVAPP_PERF_FIELD_LOG_PATH")
     || envFlagEnabled("MLVAPP_PERF_FIELD_LOG_PATH_GUI_MANAGED")) {
        putNativePathEnv("MLVAPP_PERF_FIELD_LOG_PATH",
                         defaultPerformanceFieldLogPath());
        qputenv("MLVAPP_PERF_FIELD_LOG_PATH_GUI_MANAGED", QByteArrayLiteral("1"));
    }
    applyManagedExportStageProfilerEnvironment();
}

bool cudaPlaybackProfilingSettingsEnabled()
{
    return profilingSettingEnabled(kCudaPlaybackProfilingSetting);
}

void setCudaPlaybackProfilingSettingsEnabled(bool enabled)
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    set.setValue(QString::fromLatin1(kCudaPlaybackProfilingSetting), enabled);
    set.sync();
}

void applyCudaPlaybackProfilingEnvironment(bool enabled)
{
    const struct ManagedFlag
    {
        const char *name;
        const char *managedName;
        const char *value;
    } flags[] = {
        { "MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT",
          "MLVAPP_EXPERIMENTAL_GL_WINDOW_VIEWPORT_GUI_MANAGED", "1" },
        { "MLVAPP_EXPERIMENTAL_GPU_PROCESSING",
          "MLVAPP_EXPERIMENTAL_GPU_PROCESSING_GUI_MANAGED", "1" },
        { "MLVAPP_GPU_PLAYBACK_RECON",
          "MLVAPP_GPU_PLAYBACK_RECON_GUI_MANAGED", "1" },
        { "MLVAPP_GPU_PLAYBACK_RECON_BACKEND",
          "MLVAPP_GPU_PLAYBACK_RECON_BACKEND_GUI_MANAGED", "cuda" },
        { "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT",
          "MLVAPP_EXPERIMENTAL_GPU_PLAYBACK_RECON_TEXTURE_PRESENT_GUI_MANAGED", "1" },
        { "MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER",
          "MLVAPP_EXPERIMENTAL_GPU_AMAZE_DEBAYER_GUI_MANAGED", "1" },
        { "MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT",
          "MLVAPP_EXPERIMENTAL_GPU_AMAZE_TEXTURE_PRESENT_GUI_MANAGED", "1" },
        { "MLVAPP_PLAYBACK_PHASE3_UNATTENDED",
          "MLVAPP_PLAYBACK_PHASE3_UNATTENDED_GUI_MANAGED", "1" },
        { "MLVAPP_PLAYBACK_QUALITY_MODE",
          "MLVAPP_PLAYBACK_QUALITY_MODE_GUI_MANAGED", "phase3_hq" },
        { "MLVAPP_PLAYBACK_SCALE_FACTOR",
          "MLVAPP_PLAYBACK_SCALE_FACTOR_GUI_MANAGED", "1" },
        { "GOMP_SPINCOUNT",
          "GOMP_SPINCOUNT_GUI_MANAGED", "0" }
    };
    const ManagedFlag legacyFlags[] = {
        { "MLVAPP_EXPERIMENTAL_GL_VIEWPORT",
          "MLVAPP_EXPERIMENTAL_GL_VIEWPORT_GUI_MANAGED", "" }
    };

    if (!enabled) {
        for (const ManagedFlag &flag : flags) {
            unsetManagedEnv(flag.name, flag.managedName);
        }
        for (const ManagedFlag &flag : legacyFlags) {
            unsetManagedEnv(flag.name, flag.managedName);
        }
        return;
    }

    for (const ManagedFlag &flag : legacyFlags) {
        unsetManagedEnv(flag.name, flag.managedName);
    }
    for (const ManagedFlag &flag : flags) {
        setManagedEnv(flag.name, QByteArray(flag.value), flag.managedName);
    }
}

bool dngAsyncCompressionProfilingSettingsEnabled()
{
    return profilingSettingEnabled(kDngAsyncCompressionProfilingSetting);
}

int dngAsyncCompressionQueueDepthSettingsValue()
{
    return boundedProfilingSettingInt(kDngAsyncCompressionQueueDepthSetting,
                                      2,
                                      1,
                                      8);
}

int dngAsyncCompressionThreadCountSettingsValue()
{
    return boundedProfilingSettingInt(kDngAsyncCompressionThreadCountSetting,
                                      2,
                                      1,
                                      4);
}

void setDngAsyncCompressionProfilingSettings(bool enabled,
                                             int queueDepth,
                                             int threadCount)
{
    auto setStore = automation_settings::openAppSettings(); QSettings &set = *setStore;
    const int boundedQueueDepth = qBound(1, queueDepth, 8);
    const int boundedThreadCount = qBound(1, threadCount, 4);
    set.setValue(QString::fromLatin1(kDngAsyncCompressionProfilingSetting),
                 enabled);
    set.setValue(QString::fromLatin1(kDngAsyncCompressionQueueDepthSetting),
                 boundedQueueDepth);
    set.setValue(QString::fromLatin1(kDngAsyncCompressionThreadCountSetting),
                 boundedThreadCount);
    set.sync();
}

void applyDngAsyncCompressionProfilingEnvironment(bool enabled,
                                                  int queueDepth,
                                                  int threadCount)
{
    if (!enabled) {
        unsetManagedEnv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER",
                        "MLVAPP_CDNG_EXPORT_ASYNC_WRITER_GUI_MANAGED");
        unsetManagedEnv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_COMPRESS",
                        "MLVAPP_CDNG_EXPORT_ASYNC_WRITER_COMPRESS_GUI_MANAGED");
        unsetManagedEnv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_QUEUE_DEPTH",
                        "MLVAPP_CDNG_EXPORT_ASYNC_WRITER_QUEUE_DEPTH_GUI_MANAGED");
        unsetManagedEnv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_THREADS",
                        "MLVAPP_CDNG_EXPORT_ASYNC_WRITER_THREADS_GUI_MANAGED");
        clearManagedExportStageProfilerEnvironmentIfUnneeded();
        return;
    }

    publishMachineFingerprintEnvironment();
    applyManagedExportStageProfilerEnvironment();
    setManagedEnv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER",
                  QByteArrayLiteral("1"),
                  "MLVAPP_CDNG_EXPORT_ASYNC_WRITER_GUI_MANAGED");
    setManagedEnv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_COMPRESS",
                  QByteArrayLiteral("1"),
                  "MLVAPP_CDNG_EXPORT_ASYNC_WRITER_COMPRESS_GUI_MANAGED");
    setManagedEnv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_QUEUE_DEPTH",
                  QByteArray::number(qBound(1, queueDepth, 8)),
                  "MLVAPP_CDNG_EXPORT_ASYNC_WRITER_QUEUE_DEPTH_GUI_MANAGED");
    setManagedEnv("MLVAPP_CDNG_EXPORT_ASYNC_WRITER_THREADS",
                  QByteArray::number(qBound(1, threadCount, 4)),
                  "MLVAPP_CDNG_EXPORT_ASYNC_WRITER_THREADS_GUI_MANAGED");
}

QString runMetadataJson()
{
    QJsonObject root;
    root.insert(QStringLiteral("build_sha"),
                QString::fromLatin1(MLVAPP_GIT_SHA));
    QString appVersion = QCoreApplication::applicationVersion();
    if (appVersion.isEmpty()) {
#ifdef VERSION_MAJOR
#  define MLVAPP_STRINGIFY_INNER(X) #X
#  define MLVAPP_STRINGIFY(X) MLVAPP_STRINGIFY_INNER(X)
        appVersion = QStringLiteral("%1.%2.%3.%4")
            .arg(QString::fromLatin1(MLVAPP_STRINGIFY(VERSION_MAJOR)))
            .arg(QString::fromLatin1(MLVAPP_STRINGIFY(VERSION_MINOR)))
            .arg(QString::fromLatin1(MLVAPP_STRINGIFY(VERSION_PATCH)))
            .arg(QString::fromLatin1(MLVAPP_STRINGIFY(VERSION_BUILD)));
#  undef MLVAPP_STRINGIFY
#  undef MLVAPP_STRINGIFY_INNER
#else
        appVersion = QStringLiteral("unknown");
#endif
    }
    root.insert(QStringLiteral("app_version"), appVersion);
    root.insert(QStringLiteral("qt_version"),
                QString::fromLatin1(qVersion()));

    QJsonObject os;
    os.insert(QStringLiteral("pretty"), QSysInfo::prettyProductName());
    os.insert(QStringLiteral("kernel_version"), QSysInfo::kernelVersion());
    os.insert(QStringLiteral("kernel_type"), QSysInfo::kernelType());
    os.insert(QStringLiteral("cpu_architecture"), QSysInfo::currentCpuArchitecture());
    root.insert(QStringLiteral("os"), os);

    QJsonArray features;
    const QString featureStr = cpuFeatureList();
    if (!featureStr.isEmpty()) {
        const QStringList split = featureStr.split(QLatin1Char(','), Qt::SkipEmptyParts);
        for (const QString & f : split) features.append(f);
    }
    root.insert(QStringLiteral("cpu_features"), features);

    QJsonArray cmdline;
    for (const QString & arg : g_commandLine) cmdline.append(arg);
    root.insert(QStringLiteral("command_line"), cmdline);
    root.insert(QStringLiteral("process_id"),
                static_cast<double>( QCoreApplication::applicationPid() ));
    root.insert(QStringLiteral("gui_smoke_run_nonce"),
                qEnvironmentVariable("MLVAPP_GUI_SMOKE_RUN_NONCE"));

    root.insert(QStringLiteral("log_file"), g_logFilePath);
    root.insert(QStringLiteral("logs_directory"), g_logsDir);

    return QString::fromUtf8(QJsonDocument(root).toJson(QJsonDocument::Compact));
}

void logStartupMetadata()
{
    const QString json = runMetadataJson();
    qInfo().noquote() << QStringLiteral("run_metadata=") + json;
}

} // namespace CrashForensics
