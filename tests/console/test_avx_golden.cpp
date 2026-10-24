#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QDir>
#include <QDirIterator>
#include <QDebug>
#include <QElapsedTimer>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QProcessEnvironment>
#include <QRegularExpression>
#include <QStandardPaths>
#include <QStringList>
#include <QTemporaryDir>

#include <chrono>
#include <future>
#include <thread>

#ifdef Q_OS_WIN
#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifndef WIN32_LEAN_AND_MEAN
#define WIN32_LEAN_AND_MEAN
#endif
#include <windows.h>
#include <tlhelp32.h>
#endif

static bool running_on_x86()
{
#if defined(__i386__) || defined(__x86_64__) || defined(_M_IX86) || defined(_M_X64)
    return true;
#else
    return false;
#endif
}

static QString find_tool(const QString & env_name, const QStringList & candidates)
{
    const QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    if (environment.contains(env_name)) {
        const QString from_env = environment.value(env_name);
        if (!from_env.isEmpty()) {
            return from_env;
        }
    }

    for (const QString & candidate : candidates) {
        const QString executable = QStandardPaths::findExecutable(candidate);
        if (!executable.isEmpty()) {
            return executable;
        }
    }

#ifdef Q_OS_WIN
    for (const QString & candidate : candidates) {
        const QString windows_candidate = candidate.endsWith(QStringLiteral(".exe"))
            ? candidate
            : candidate + QStringLiteral(".exe");
        QDirIterator qt_installations(QStringLiteral("C:/Qt"),
                                      QStringList() << windows_candidate,
                                      QDir::Files,
                                      QDirIterator::Subdirectories);
        if (qt_installations.hasNext()) {
            return qt_installations.next();
        }
    }
#endif

    return QString();
}

static void prepend_to_path(QProcessEnvironment * environment, const QStringList & directories)
{
#ifdef Q_OS_WIN
    constexpr Qt::CaseSensitivity path_case_sensitivity = Qt::CaseInsensitive;
    QStringList parts;
    for (const QString & directory : directories) {
        if (!directory.isEmpty() && !parts.contains(directory, path_case_sensitivity)) {
            parts.append(directory);
        }
    }

    const QString windows_root = qEnvironmentVariable("SystemRoot", QStringLiteral("C:/Windows"));
    const QStringList system_directories = {
        QDir(windows_root).filePath(QStringLiteral("System32")),
        QDir(windows_root).absolutePath()
    };
    for (const QString & directory : system_directories) {
        if (!parts.contains(directory, path_case_sensitivity)) {
            parts.append(directory);
        }
    }
#else
    constexpr Qt::CaseSensitivity path_case_sensitivity = Qt::CaseSensitive;
    QString inherited_path;
    const QStringList inherited_keys = environment->keys();
    for (const QString & key : inherited_keys) {
        if (key == QStringLiteral("PATH")) {
            if (!inherited_path.isEmpty() && !environment->value(key).isEmpty()) {
                inherited_path.append(QDir::listSeparator());
            }
            inherited_path.append(environment->value(key));
            environment->remove(key);
        }
    }

    QStringList parts = inherited_path.split(QDir::listSeparator(), Qt::SkipEmptyParts);
    for (const QString & directory : directories) {
        if (!directory.isEmpty() && !parts.contains(directory, path_case_sensitivity)) {
            parts.prepend(directory);
        }
    }
#endif

    const QStringList keys = environment->keys();
    for (const QString & key : keys) {
#ifdef Q_OS_WIN
        if (key.compare(QStringLiteral("PATH"), Qt::CaseInsensitive) == 0) {
#else
        if (key == QStringLiteral("PATH")) {
#endif
            environment->remove(key);
        }
    }
#ifdef Q_OS_WIN
    environment->insert(QStringLiteral("Path"), parts.join(QDir::listSeparator()));
#else
    environment->insert(QStringLiteral("PATH"), parts.join(QDir::listSeparator()));
#endif
}

// qmake and make keep their historical bound.
static const int kBuildProcessTimeoutMs = 30 * 60 * 1000;
// One helper run renders two frames of two clips. On hosted CI the whole console_tests step,
// which includes both helper builds and both helper runs, took at most 278 s across 54 passing
// merge_group runs (2026-10-21..23), so a healthy helper run never comes near this bound. Before
// this bound, a hung helper held the merge queue for the 30 minute build bound.
static const int kHelperTimeoutMs = 5 * 60 * 1000;
static_assert(kHelperTimeoutMs <= 10 * 60 * 1000 && kHelperTimeoutMs < kBuildProcessTimeoutMs,
              "the helper bound must stay well under the build bound");
static const int kOutputTailBytes = 4096;
// A timed-out helper's tail holds its stall reports (stage, clip state, thread dump), so it prints more.
static const int kTimeoutOutputTailBytes = 16 * 1024;
// What the parent keeps of a child's merged output, so a chatty child cannot grow it without bound.
static const qsizetype kRetainedOutputBytes = 64 * 1024;
static_assert(kRetainedOutputBytes >= kOutputTailBytes, "the retained tail must cover the diagnostic print");
static_assert(kRetainedOutputBytes >= kTimeoutOutputTailBytes, "the retained tail must cover the timeout print");

// The last `capacity` bytes a child printed, plus the exact count of everything it printed.
class OutputTail {
public:
    explicit OutputTail(qsizetype capacity) : capacity_(capacity) {}

    void append(const QByteArray & chunk)
    {
        total_bytes_ += chunk.size();
        if (chunk.size() >= capacity_) {
            bytes_ = chunk.right(capacity_);
            return;
        }
        bytes_ += chunk;
        if (bytes_.size() > capacity_) {
            bytes_.remove(0, bytes_.size() - capacity_);
        }
    }

    const QByteArray & bytes() const { return bytes_; }
    qint64 total_bytes() const { return total_bytes_; }
    qsizetype capacity() const { return capacity_; }

private:
    qsizetype capacity_;
    qint64 total_bytes_ = 0;
    QByteArray bytes_;
};

// The last helper stage line ("avx_parity_helper t=...ms stage=...") seen in a child's output, and
// when it arrived on the run clock. Lines split across reads are joined; a partial line is bounded.
class StageTracker {
public:
    // True when the chunk completed a new stage line.
    bool append(const QByteArray & chunk, qint64 now_ms)
    {
        bool saw_stage = false;
        partial_ += chunk;
        qsizetype newline = partial_.indexOf('\n');
        while (newline >= 0) {
            const QByteArray line = partial_.left(newline).trimmed();
            partial_.remove(0, newline + 1);
            if (line.startsWith("avx_parity_helper t=")) {
                last_line_ = line.left(kMaxLineBytes);
                last_ms_ = now_ms;
                saw_stage = true;
            }
            newline = partial_.indexOf('\n');
        }
        if (partial_.size() > kMaxLineBytes) {
            partial_ = partial_.right(kMaxLineBytes);
        }
        return saw_stage;
    }

    const QByteArray & last_line() const { return last_line_; }
    qint64 last_ms() const { return last_ms_; }

private:
    static const qsizetype kMaxLineBytes = 1024;
    QByteArray partial_;
    QByteArray last_line_;
    qint64 last_ms_ = -1;
};

// A child's CPU time and thread count as seen from the parent (-1 where unknown or not Windows).
// Two CPU samples tell a spinning child (time grows) from a blocked one (time stands still).
struct ChildSample {
    qint64 cpu_ms = -1;
    int threads = -1;
};

static ChildSample sample_child(qint64 pid)
{
    ChildSample sample;
#ifdef Q_OS_WIN
    if (pid <= 0) {
        return sample;
    }
    const HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, static_cast<DWORD>(pid));
    if (process) {
        FILETIME created, exited, kernel, user;
        if (GetProcessTimes(process, &created, &exited, &kernel, &user)) {
            const auto as_u64 = [](const FILETIME & time) {
                return (static_cast<quint64>(time.dwHighDateTime) << 32) | time.dwLowDateTime;
            };
            sample.cpu_ms = static_cast<qint64>((as_u64(kernel) + as_u64(user)) / 10000u);
        }
        CloseHandle(process);
    }
    const HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot != INVALID_HANDLE_VALUE) {
        PROCESSENTRY32W entry;
        entry.dwSize = sizeof(entry);
        for (BOOL more = Process32FirstW(snapshot, &entry); more; more = Process32NextW(snapshot, &entry)) {
            if (entry.th32ProcessID == static_cast<DWORD>(pid)) {
                sample.threads = static_cast<int>(entry.cntThreads);
                break;
            }
        }
        CloseHandle(snapshot);
    }
#else
    Q_UNUSED(pid);
#endif
    return sample;
}

struct ProcessRunResult {
    bool started = false;
    bool timed_out = false;
    bool crashed = false;
    int exit_code = -1;
    qint64 elapsed_ms = 0;
    QByteArray output;
    qint64 output_total_bytes = 0;
    QString error_string;
    // On the run clock: when the last output arrived and when the last helper stage line arrived
    // (-1 if none), and the stage line itself.
    qint64 last_output_ms = -1;
    qint64 last_stage_ms = -1;
    QByteArray last_stage_line;
    // Recorded at the moment the bound expired, before the kill.
    qint64 killed_at_ms = -1;
    bool output_channel_open = false;  // the merged stdout+stderr pipe
    ChildSample child_at_kill;
    ChildSample child_at_last_stage;
    bool ok() const { return started && !timed_out && !crashed && exit_code == 0; }
};

static ProcessRunResult run_process_bounded(const QString & program,
                                            const QStringList & arguments,
                                            const QString & working_directory,
                                            const QProcessEnvironment & environment,
                                            int timeout_ms)
{
    ProcessRunResult result;
    QProcess process;
    process.setProgram(program);
    process.setArguments(arguments);
    process.setWorkingDirectory(working_directory);
    process.setProcessEnvironment(environment);
    process.setProcessChannelMode(QProcess::MergedChannels);
    bool output_channel_closed = false;
    QObject::connect(&process, &QProcess::readChannelFinished, [&output_channel_closed] {
        output_channel_closed = true;
    });
    QElapsedTimer timer;
    timer.start();
    process.start();
    if (!process.waitForStarted()) {
        result.error_string = process.errorString();
        result.elapsed_ms = timer.elapsed();
        return result;
    }
    result.started = true;
    const qint64 pid = process.processId();

    // Drain the merged channel while waiting, so a chatty child never stalls on a full pipe.
    OutputTail output(kRetainedOutputBytes);
    StageTracker stages;
    const auto drain = [&] {
        const QByteArray chunk = process.readAll();
        if (chunk.isEmpty()) {
            return;
        }
        output.append(chunk);
        result.last_output_ms = timer.elapsed();
        if (stages.append(chunk, result.last_output_ms)) {
            result.child_at_last_stage = sample_child(pid);
        }
    };
    while (!process.waitForFinished(250)) {
        drain();
        if (process.state() == QProcess::NotRunning) {
            break;
        }
        if (timer.elapsed() >= timeout_ms) {
            result.timed_out = true;
            result.killed_at_ms = timer.elapsed();
            result.output_channel_open = !output_channel_closed;
            result.child_at_kill = sample_child(pid);
            process.kill();
            process.waitForFinished(10 * 1000);
            break;
        }
    }
    drain();
    result.last_stage_line = stages.last_line();
    result.last_stage_ms = stages.last_ms();
    result.output = output.bytes();
    result.output_total_bytes = output.total_bytes();
    result.elapsed_ms = timer.elapsed();
    if (!result.timed_out) {
        result.crashed = process.exitStatus() == QProcess::CrashExit;
        result.exit_code = process.exitCode();
    }
    if (result.timed_out || result.crashed) {
        result.error_string = process.errorString();
    }
    return result;
}

static QString elapsed_seconds(const ProcessRunResult & result)
{
    return QString::number(result.elapsed_ms / 1000.0, 'f', 1);
}

static QString output_tail(const QByteArray & output)
{
    return QString::fromLocal8Bit(output.right(kOutputTailBytes));
}

static bool run_process(const QString & program,
                        const QStringList & arguments,
                        const QString & working_directory,
                        const QProcessEnvironment & environment,
                        QString * combined_output,
                        int timeout_ms = kBuildProcessTimeoutMs)
{
    const ProcessRunResult result = run_process_bounded(program, arguments, working_directory, environment, timeout_ms);
    if (combined_output) {
        if (!result.started) {
            *combined_output = result.error_string;
        } else if (result.timed_out) {
            *combined_output = QStringLiteral("Timed out after %1 s waiting for process: %2\n%3")
                                   .arg(elapsed_seconds(result), program, output_tail(result.output));
        } else if (result.output_total_bytes > result.output.size()) {
            *combined_output = QStringLiteral("(%1 bytes total, last %2 shown)\n%3")
                                   .arg(QString::number(result.output_total_bytes),
                                        QString::number(result.output.size()),
                                        QString::fromLocal8Bit(result.output));
        } else {
            *combined_output = QString::fromLocal8Bit(result.output);
        }
    }
    return result.ok();
}

// One line naming which helper failed and how, then the tail of what it printed.
static QString describe_process_failure(const QString & label,
                                        const QString & program,
                                        const ProcessRunResult & result,
                                        const QString & output_path)
{
    const QString exit_status = !result.started ? QStringLiteral("not-started")
        : result.timed_out                      ? QStringLiteral("timeout")
        : result.crashed                        ? QStringLiteral("crash")
                                                : QStringLiteral("normal");
    QString text = QStringLiteral("AVX parity helper failed: helper=%1 program=%2 elapsed_s=%3 exit_status=%4")
                       .arg(label, program, elapsed_seconds(result), exit_status);
    text += QStringLiteral(" exit_code=%1 timed_out=%2 output_json_exists=%3 output_bytes=%4")
                .arg(QString::number(result.exit_code),
                     result.timed_out ? QStringLiteral("yes") : QStringLiteral("no"),
                     QFileInfo::exists(output_path) ? QStringLiteral("yes") : QStringLiteral("no"),
                     QString::number(result.output_total_bytes));
    if (!result.error_string.isEmpty()) {
        text += QStringLiteral(" error=") + result.error_string;
    }
    int tail_bytes = kOutputTailBytes;
    if (result.timed_out) {
        // What the parent saw just before the kill: how long the child had been silent, the last stage
        // it named, whether its output pipe was still open, and its CPU time (growing = spin).
        const auto seconds_since = [&result](qint64 at_ms) {
            return at_ms < 0 ? QStringLiteral("none")
                             : QString::number((result.killed_at_ms - at_ms) / 1000.0, 'f', 1);
        };
        const auto cpu_seconds = [](qint64 cpu_ms) {
            return cpu_ms < 0 ? QStringLiteral("unknown") : QString::number(cpu_ms / 1000.0, 'f', 1);
        };
        const bool have_cpu_delta = result.child_at_kill.cpu_ms >= 0 && result.child_at_last_stage.cpu_ms >= 0;
        text += QStringLiteral("\nAVX_PARITY_HELPER_TIMEOUT_SNAPSHOT since_last_output_s=%1 since_last_stage_s=%2"
                               " merged_output_channel=%3 child_threads=%4 child_cpu_s=%5 child_cpu_since_last_stage_s=%6"
                               " last_stage=\"%7\"")
                    .arg(seconds_since(result.last_output_ms),
                         seconds_since(result.last_stage_ms),
                         result.output_channel_open ? QStringLiteral("open") : QStringLiteral("closed"),
                         result.child_at_kill.threads < 0 ? QStringLiteral("unknown")
                                                          : QString::number(result.child_at_kill.threads),
                         cpu_seconds(result.child_at_kill.cpu_ms),
                         have_cpu_delta ? cpu_seconds(result.child_at_kill.cpu_ms - result.child_at_last_stage.cpu_ms)
                                        : QStringLiteral("unknown"),
                         QString::fromLocal8Bit(result.last_stage_line));
        tail_bytes = kTimeoutOutputTailBytes;
    }
    text += QStringLiteral("\n--- helper output tail (last %1 bytes of %2 bytes total) ---\n")
                .arg(qMin<qint64>(result.output.size(), tail_bytes))
                .arg(result.output_total_bytes);
    text += QString::fromLocal8Bit(result.output.right(tail_bytes));
    return text;
}

// A helper timeout is a failure. It earns one rerun for diagnostics only: the rerun's outcome is
// logged, its hash json is removed, and the first, timed-out result is returned, so a hang can
// never turn into PASS. A crash or a non-zero exit is reported as is, with no rerun.
static ProcessRunResult run_helper_with_retry(const QString & label,
                                              const QString & program,
                                              const QStringList & arguments,
                                              const QString & working_directory,
                                              const QProcessEnvironment & environment,
                                              int timeout_ms,
                                              const QString & output_path,
                                              std::ostream & diagnostics)
{
    const ProcessRunResult first = run_process_bounded(program, arguments, working_directory, environment, timeout_ms);
    if (!first.timed_out) {
        return first;
    }
    diagnostics << "AVX_PARITY_HELPER_TIMEOUT_RETRY attempt=1 "
                << describe_process_failure(label, program, first, output_path).toStdString() << "\n";
    diagnostics.flush();
    QFile::remove(output_path);
    const ProcessRunResult rerun = run_process_bounded(program, arguments, working_directory, environment, timeout_ms);
    diagnostics << "AVX_PARITY_HELPER_DIAGNOSTIC_RERUN attempt=2 outcome="
                << (rerun.ok() ? "ok" : "failed") << " (result discarded; attempt 1 timed out)\n";
    if (!rerun.ok()) {
        diagnostics << describe_process_failure(label, program, rerun, output_path).toStdString() << "\n";
    }
    diagnostics.flush();
    QFile::remove(output_path);
    return first;
}

static QString find_built_helper(const QString & build_directory)
{
#ifdef Q_OS_WIN
    const QString helper_name = QStringLiteral("avx_parity_helper.exe");
#else
    const QString helper_name = QStringLiteral("avx_parity_helper");
#endif

    QDirIterator it(build_directory,
                    QStringList() << helper_name,
                    QDir::Files,
                    QDirIterator::Subdirectories);
    if (it.hasNext()) {
        return it.next();
    }
    return QString();
}

static QJsonObject load_json_file(const QString & path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return QJsonObject();
    }
    return QJsonDocument::fromJson(file.readAll()).object();
}

// The default helper this console_tests process built, so the stall test reuses the parity test's
// fresh build rather than whatever an earlier checkout left in the build directory.
static QString g_default_helper_built_this_run;

static QString build_helper(bool enable_avx)
{
    const QString root = find_repo_root();
    const QString qmake = find_tool(QStringLiteral("QMAKE"),
                                    {QStringLiteral("qmake6"), QStringLiteral("qmake"), QStringLiteral("qmake-qt5")});
    const QString make_tool = find_tool(QStringLiteral("MAKE"),
                                        {QStringLiteral("mingw32-make"), QStringLiteral("jom"), QStringLiteral("nmake"), QStringLiteral("make")});

    if (qmake.isEmpty()) {
        SKIP_TEST("qmake was not found; skipping AVX parity build test");
    }
    if (make_tool.isEmpty()) {
        SKIP_TEST("make tool was not found; skipping AVX parity build test");
    }

    const QString flavor = enable_avx ? QStringLiteral("on") : QStringLiteral("off");
    const QString build_directory = QDir(root).filePath(QStringLiteral("tests/build-avx-parity/%1").arg(flavor));
    QDir build_directory_handle(build_directory);
    if (build_directory_handle.exists()) {
        ASSERT_TRUE(build_directory_handle.removeRecursively());
    }
    ASSERT_TRUE(QDir().mkpath(build_directory));

    const QString pro_path = QDir(root).filePath(QStringLiteral("tests/console/avx_parity_helper.pro"));
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    prepend_to_path(&environment,
                    {QFileInfo(qmake).absolutePath(), QFileInfo(make_tool).absolutePath()});
    environment.remove(QStringLiteral("MLVAPP_ENABLE_AVX"));
    environment.remove(QStringLiteral("MLVAPP_ENABLE_AVX2"));
    if (enable_avx) {
        environment.insert(QStringLiteral("MLVAPP_ENABLE_AVX"), QStringLiteral("1"));
    }

    QString output;
    const bool qmake_ok = run_process(qmake,
                                      {pro_path, QStringLiteral("CONFIG+=debug")},
                                      build_directory,
                                      environment,
                                      &output);
    if (!qmake_ok) {
        qCritical().noquote() << "AVX parity qmake failed:" << output;
        std::cerr << "AVX parity qmake failed: " << output.toStdString() << "\n";
    }
    ASSERT_TRUE(qmake_ok);
    const bool make_ok = run_process(make_tool,
                                     {},
                                     build_directory,
                                     environment,
                                     &output);
    if (!make_ok) {
        qCritical().noquote() << "AVX parity helper build failed:" << output;
        std::cerr << "AVX parity helper build failed: " << output.toStdString() << "\n";
    }
    ASSERT_TRUE(make_ok);

    const QString helper_path = find_built_helper(build_directory);
    ASSERT_FALSE(helper_path.isEmpty());
    if (!enable_avx) {
        g_default_helper_built_this_run = helper_path;
    }
    return helper_path;
}

static QProcessEnvironment helper_environment()
{
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    const QString qmake = find_tool(QStringLiteral("QMAKE"),
                                    {QStringLiteral("qmake6"), QStringLiteral("qmake"), QStringLiteral("qmake-qt5")});
    const QString make_tool = find_tool(QStringLiteral("MAKE"),
                                        {QStringLiteral("mingw32-make"), QStringLiteral("jom"), QStringLiteral("nmake"), QStringLiteral("make")});
    prepend_to_path(&environment,
                    {QFileInfo(qmake).absolutePath(), QFileInfo(make_tool).absolutePath()});
    environment.insert(QStringLiteral("MLVAPP_FORCE_THREADS"), QStringLiteral("1"));
    environment.insert(QStringLiteral("OMP_NUM_THREADS"), QStringLiteral("1"));
    environment.insert(QStringLiteral("OMP_DYNAMIC"), QStringLiteral("FALSE"));
    environment.remove(QStringLiteral("AVX_PARITY_HELPER_FAULT"));
    return environment;
}

static QJsonObject run_helper_and_collect(const QString & helper_path, const QString & label)
{
    const QString output_path = QDir(QFileInfo(helper_path).absolutePath()).filePath(QStringLiteral("avx_parity_hashes.json"));
    QFile::remove(output_path);

    const QProcessEnvironment environment = helper_environment();

    const ProcessRunResult result = run_helper_with_retry(label,
                                                          helper_path,
                                                          {QStringLiteral("--output"), output_path},
                                                          QFileInfo(helper_path).absolutePath(),
                                                          environment,
                                                          kHelperTimeoutMs,
                                                          output_path,
                                                          std::cerr);
    if (!result.ok()) {
        std::cerr << describe_process_failure(label, helper_path, result, output_path).toStdString() << "\n";
    } else {
        std::cout << "AVX parity helper " << label.toStdString() << " finished in "
                  << elapsed_seconds(result).toStdString() << " s\n";
    }
    ASSERT_TRUE(result.ok());

    const QJsonObject object = load_json_file(output_path);
    ASSERT_FALSE(object.isEmpty());
    return object;
}

TEST(AvxParity, DefaultAndAvxBuildsProduceMatchingFrameHashes)
{
    if (!running_on_x86()) {
        SKIP_TEST("AVX parity only applies to x86/x86_64 hosts");
    }

    const QString default_helper = build_helper(false);
    const QString avx_helper = build_helper(true);

    QJsonObject default_output = run_helper_and_collect(default_helper, QStringLiteral("default"));
    QJsonObject avx_output = run_helper_and_collect(avx_helper, QStringLiteral("avx"));

    ASSERT_TRUE(default_output.value(QStringLiteral("build_avx")).isBool());
    ASSERT_FALSE(default_output.value(QStringLiteral("build_avx")).toBool());
    ASSERT_TRUE(avx_output.value(QStringLiteral("build_avx")).isBool());
    ASSERT_TRUE(avx_output.value(QStringLiteral("build_avx")).toBool());

    default_output.remove(QStringLiteral("build_avx"));
    avx_output.remove(QStringLiteral("build_avx"));

    ASSERT_TRUE(default_output == avx_output);
}

// The real helper, driven into the open-clip hang the CI logs showed (merge_group 40043249058: one
// open-clip stage line, then 300 s of silence). The injected fault parks the cache workers that
// openMlvClip starts, so the fixture's disableMlvCaching waits for them forever. The helper's stall
// report must then name that state, and the parent's timeout snapshot must name the stage.
TEST(AvxParity, InjectedOpenClipStallIsNamedByTheHelperStallReport)
{
    QString helper = g_default_helper_built_this_run;
    if (helper.isEmpty()) {
        helper = build_helper(false);
    }

    QTemporaryDir scratch;
    ASSERT_TRUE(scratch.isValid());
    const QString output_path = scratch.filePath(QStringLiteral("hashes.json"));
    QProcessEnvironment environment = helper_environment();
    environment.insert(QStringLiteral("AVX_PARITY_HELPER_FAULT"), QStringLiteral("cache-worker-start-pause"));
    environment.insert(QStringLiteral("AVX_PARITY_HELPER_STALL_MS"), QStringLiteral("1000"));

    const ProcessRunResult result = run_process_bounded(helper,
                                                        {QStringLiteral("--output"), output_path},
                                                        QFileInfo(helper).absolutePath(),
                                                        environment,
                                                        9000);
    const QString text = describe_process_failure(QStringLiteral("default"), helper, result, output_path);
    const QString output = QString::fromLocal8Bit(result.output);
    // Some report caught disableMlvCaching waiting: stop set, workers still counted.
    const bool named_quiesce_wait =
        QRegularExpression(QStringLiteral("cache_thread_count=[1-9][0-9]* stop_caching=1 ")).match(output).hasMatch();
#ifdef Q_OS_WIN
    const bool named_main_frame =
        QRegularExpression(QStringLiteral("thread tid=\\d+ main=yes [^\\n]*disableMlvCaching")).match(output).hasMatch();
#else
    const bool named_main_frame = true;
#endif
    if (!result.timed_out || !named_quiesce_wait || !named_main_frame) {
        std::cerr << text.toStdString() << "\n";
    }

    ASSERT_TRUE(result.timed_out);
    ASSERT_FALSE(output.contains(QStringLiteral("stage=load-receipt")));
    ASSERT_TRUE(result.last_stage_line.contains("stage=open-clip "));
    ASSERT_TRUE(output.contains(QStringLiteral("AVX_PARITY_HELPER_STALL report=1 ")));
    ASSERT_TRUE(named_quiesce_wait);
    ASSERT_TRUE(named_main_frame);
    ASSERT_TRUE(text.contains(QStringLiteral("AVX_PARITY_HELPER_TIMEOUT_SNAPSHOT ")));
    ASSERT_TRUE(text.contains(QStringLiteral("last_stage=\"avx_parity_helper t=")));
    ASSERT_TRUE(text.contains(QStringLiteral("merged_output_channel=open")));
}

// A shell stands in for the helper, so these tests pin the runner without a real hang.
static QString stand_in_shell()
{
#ifdef Q_OS_WIN
    return QStandardPaths::findExecutable(QStringLiteral("powershell"));
#else
    return QStandardPaths::findExecutable(QStringLiteral("sh"));
#endif
}

static QStringList stand_in_arguments(const QString & windows_script, const QString & posix_script)
{
#ifdef Q_OS_WIN
    Q_UNUSED(posix_script);
    return {QStringLiteral("-NoProfile"), QStringLiteral("-NonInteractive"),
            QStringLiteral("-Command"), windows_script};
#else
    Q_UNUSED(windows_script);
    return {QStringLiteral("-c"), posix_script};
#endif
}

static QStringList sleeper_arguments(int seconds)
{
    return stand_in_arguments(QStringLiteral("Start-Sleep -Seconds %1").arg(seconds),
                              QStringLiteral("sleep %1").arg(seconds));
}

// About 1 MB of output, far past any pipe buffer, then a marker and exit code 7.
static QStringList chatty_failure_arguments()
{
    return stand_in_arguments(
        QStringLiteral("$line = 'x' * 100; for ($i = 0; $i -lt 10000; $i++) { [Console]::Out.WriteLine($line) }; "
                       "[Console]::Out.WriteLine('AVX_PARITY_TAIL_MARKER'); exit 7"),
        QStringLiteral("line=$(printf '%0100d' 0); i=0; while [ $i -lt 10000 ]; do echo \"$line\"; i=$((i+1)); done; "
                       "echo AVX_PARITY_TAIL_MARKER; exit 7"));
}

// Where one run's scratch lives, kept so a test can check it is gone once the run has ended.
struct ScratchPaths {
    QString dir;
    QString marker;
    QString output_json;
};

// The first-run marker and hash json paths one runner test uses. Each run gets its own fresh
// directory under `root`, so two console_tests processes running at once never read or delete each
// other's files; the run removes only that directory when it ends.
class RunnerScratch {
public:
    explicit RunnerScratch(const QString & root)
        : dir_(QDir(root).filePath(QStringLiteral("avx_parity_runner-XXXXXX")))
    {
        ASSERT_TRUE(dir_.isValid());
    }

    QString dir() const { return dir_.path(); }
    QString marker() const { return dir_.filePath(QStringLiteral("first_run.marker")); }
    QString output_json() const { return dir_.filePath(QStringLiteral("hashes.json")); }
    ScratchPaths paths() const { return {dir(), marker(), output_json()}; }

private:
    QTemporaryDir dir_;
};

// Empty when a finished run's directory, marker and hash json are all gone; otherwise what is left.
static std::string scratch_left_behind(const ScratchPaths & paths)
{
    QStringList left;
    for (const QString & path : {paths.dir, paths.marker, paths.output_json}) {
        if (QFileInfo::exists(path)) {
            left << path;
        }
    }
    return left.join(QStringLiteral(", ")).toStdString();
}

TEST(AvxParityRunner, BoundedRunKillsAHungChildWithinItsTimeout)
{
    const QString shell = stand_in_shell();
    if (shell.isEmpty()) {
        SKIP_TEST("no shell found to stand in for a hung helper");
    }

    QString output;
    QElapsedTimer timer;
    timer.start();
    const bool ok = run_process(shell,
                                sleeper_arguments(20),
                                QDir::tempPath(),
                                QProcessEnvironment::systemEnvironment(),
                                &output,
                                2000);
    const qint64 elapsed_ms = timer.elapsed();

    ASSERT_FALSE(ok);
    ASSERT_TRUE(elapsed_ms < 15000);
    ASSERT_TRUE(output.contains(QStringLiteral("Timed out after")));
    ASSERT_TRUE(output.contains(shell));
}

TEST(AvxParityRunner, FailingHelperDiagnosticNamesHelperExitCodeAndOutputTail)
{
    const QString shell = stand_in_shell();
    if (shell.isEmpty()) {
        SKIP_TEST("no shell found to stand in for a failing helper");
    }

    ScratchPaths scratch_paths;
    ProcessRunResult result;
    std::ostringstream diagnostics;
    {
        const RunnerScratch scratch(QDir::tempPath());
        scratch_paths = scratch.paths();
        result = run_helper_with_retry(QStringLiteral("default"),
                                       shell,
                                       chatty_failure_arguments(),
                                       QDir::tempPath(),
                                       QProcessEnvironment::systemEnvironment(),
                                       60 * 1000,
                                       scratch_paths.output_json,
                                       diagnostics);
    }
    // A failed run leaves nothing behind once it has ended.
    ASSERT_EQ(std::string(), scratch_left_behind(scratch_paths));
    const QString output_path = scratch_paths.output_json;

    ASSERT_FALSE(result.ok());
    ASSERT_FALSE(result.timed_out);
    ASSERT_EQ(7, result.exit_code);
    ASSERT_TRUE(result.output_total_bytes > 1000000);
    // The parent keeps a bounded tail of a chatty child, never all of it.
    ASSERT_TRUE(result.output.size() <= kRetainedOutputBytes);
    ASSERT_TRUE(result.output.contains("AVX_PARITY_TAIL_MARKER"));
    // A non-zero exit is reported as is; only a timeout earns the single retry.
    ASSERT_TRUE(diagnostics.str().find("AVX_PARITY_HELPER_TIMEOUT_RETRY") == std::string::npos);

    const QString text = describe_process_failure(QStringLiteral("default"), shell, result, output_path);
    ASSERT_TRUE(text.contains(QStringLiteral("helper=default")));
    ASSERT_TRUE(text.contains(QStringLiteral("exit_code=7")));
    ASSERT_TRUE(text.contains(QStringLiteral("elapsed_s=")));
    ASSERT_TRUE(text.contains(QStringLiteral("output_json_exists=no")));
    ASSERT_TRUE(text.contains(QStringLiteral("AVX_PARITY_TAIL_MARKER")));
    ASSERT_TRUE(text.contains(QStringLiteral("output_bytes=%1").arg(result.output_total_bytes)));
    ASSERT_TRUE(text.contains(QStringLiteral("of %1 bytes total").arg(result.output_total_bytes)));
    ASSERT_TRUE(text.size() < 6000);
}

TEST(AvxParityRunner, HelperTimeoutRetriesOnceAndReportsTheFirstAttempt)
{
    const QString shell = stand_in_shell();
    if (shell.isEmpty()) {
        SKIP_TEST("no shell found to stand in for a hung helper");
    }

    ScratchPaths scratch_paths;
    ProcessRunResult result;
    std::ostringstream diagnostics;
    QElapsedTimer timer;
    timer.start();
    {
        const RunnerScratch scratch(QDir::tempPath());
        scratch_paths = scratch.paths();
        result = run_helper_with_retry(QStringLiteral("avx"),
                                       shell,
                                       sleeper_arguments(20),
                                       QDir::tempPath(),
                                       QProcessEnvironment::systemEnvironment(),
                                       1500,
                                       scratch_paths.output_json,
                                       diagnostics);
    }
    // A run whose child was killed leaves nothing behind once it has ended.
    ASSERT_EQ(std::string(), scratch_left_behind(scratch_paths));

    ASSERT_TRUE(result.timed_out);
    ASSERT_FALSE(result.ok());
    ASSERT_TRUE(timer.elapsed() < 20000);

    const std::string text = diagnostics.str();
    const std::string marker = "AVX_PARITY_HELPER_TIMEOUT_RETRY";
    const std::size_t first = text.find(marker);
    ASSERT_TRUE(first != std::string::npos);
    ASSERT_TRUE(text.find(marker, first + marker.size()) == std::string::npos);
    ASSERT_TRUE(text.find("helper=avx") != std::string::npos);
    ASSERT_TRUE(text.find("timed_out=yes") != std::string::npos);
}

// Prints one helper stage line on stderr, then hangs silently: the shape of the 2026-10-24 hang.
static QStringList stage_then_hang_arguments()
{
    return stand_in_arguments(
        QStringLiteral("[Console]::Error.WriteLine('avx_parity_helper t=1ms stage=open-clip stand-in'); "
                       "[Console]::Error.Flush(); Start-Sleep -Seconds 60"),
        QStringLiteral("echo 'avx_parity_helper t=1ms stage=open-clip stand-in' >&2; sleep 60"));
}

TEST(AvxParityRunner, TimeoutSnapshotNamesTheLastStageAndHowLongTheChildWasSilent)
{
    const QString shell = stand_in_shell();
    if (shell.isEmpty()) {
        SKIP_TEST("no shell found to stand in for a hung helper");
    }

    const ProcessRunResult result = run_process_bounded(shell,
                                                        stage_then_hang_arguments(),
                                                        QDir::tempPath(),
                                                        QProcessEnvironment::systemEnvironment(),
                                                        8000);
    ASSERT_TRUE(result.timed_out);
    ASSERT_TRUE(result.last_stage_line == QByteArray("avx_parity_helper t=1ms stage=open-clip stand-in"));
    ASSERT_TRUE(result.last_stage_ms >= 0);
    ASSERT_TRUE(result.last_output_ms >= result.last_stage_ms);
    // The child printed its stage line, then nothing, and the parent measured the silence.
    ASSERT_TRUE(result.killed_at_ms - result.last_stage_ms >= 1000);
    // The child was alive with its pipe open when it was killed: a hang, not a lost line.
    ASSERT_TRUE(result.output_channel_open);
#ifdef Q_OS_WIN
    ASSERT_TRUE(result.child_at_kill.threads > 0);
    ASSERT_TRUE(result.child_at_kill.cpu_ms >= 0);
    ASSERT_TRUE(result.child_at_last_stage.cpu_ms >= 0);
#endif

    const QString text = describe_process_failure(QStringLiteral("avx"),
                                                  shell,
                                                  result,
                                                  QDir(QDir::tempPath()).filePath(QStringLiteral("no-such-hashes.json")));
    const QString since = QString::number((result.killed_at_ms - result.last_stage_ms) / 1000.0, 'f', 1);
    ASSERT_TRUE(text.contains(QStringLiteral("AVX_PARITY_HELPER_TIMEOUT_SNAPSHOT ")));
    ASSERT_TRUE(text.contains(QStringLiteral("since_last_stage_s=%1 ").arg(since)));
    ASSERT_TRUE(text.contains(QStringLiteral("merged_output_channel=open")));
    ASSERT_TRUE(text.contains(QStringLiteral("last_stage=\"avx_parity_helper t=1ms stage=open-clip stand-in\"")));
#ifdef Q_OS_WIN
    ASSERT_FALSE(text.contains(QStringLiteral("child_threads=unknown")));
#endif
}

// Hangs on the first run; on the second run writes a valid hash json and exits 0.
static QStringList hang_then_pass_arguments(const QString & first_run_marker, const QString & output_path)
{
    return stand_in_arguments(
        QStringLiteral("if (Test-Path -LiteralPath '%1') { "
                       "Set-Content -LiteralPath '%2' -Value '{\"build_avx\":false,\"clip\":\"0123abcd\"}'; exit 0 "
                       "} else { New-Item -ItemType File -Path '%1' | Out-Null; Start-Sleep -Seconds 60 }")
            .arg(first_run_marker, output_path),
        QStringLiteral("if [ -e '%1' ]; then printf '{\"build_avx\":false,\"clip\":\"0123abcd\"}' > '%2'; exit 0; "
                       "else : > '%1'; sleep 60; fi")
            .arg(first_run_marker, output_path));
}

TEST(AvxParityRunner, FirstHelperTimeoutFailsEvenWhenTheRetryWouldPass)
{
    const QString shell = stand_in_shell();
    if (shell.isEmpty()) {
        SKIP_TEST("no shell found to stand in for a hung helper");
    }

    ScratchPaths scratch_paths;
    ProcessRunResult result;
    std::ostringstream diagnostics;
    {
        const RunnerScratch scratch(QDir::tempPath());
        scratch_paths = scratch.paths();
        result = run_helper_with_retry(QStringLiteral("default"),
                                       shell,
                                       hang_then_pass_arguments(scratch_paths.marker, scratch_paths.output_json),
                                       QDir::tempPath(),
                                       QProcessEnvironment::systemEnvironment(),
                                       8000,
                                       scratch_paths.output_json,
                                       diagnostics);

        // The first attempt hung, so the run fails however the second attempt ends.
        ASSERT_TRUE(result.timed_out);
        ASSERT_FALSE(result.ok());
        // The second attempt's hashes never reach the parity comparison.
        ASSERT_FALSE(QFileInfo::exists(scratch_paths.output_json));
        // The killed first attempt left its marker, which the second attempt found.
        ASSERT_TRUE(QFileInfo::exists(scratch_paths.marker));
    }
    // Once the run has ended, its marker and directory are gone too.
    ASSERT_EQ(std::string(), scratch_left_behind(scratch_paths));
    const std::string text = diagnostics.str();
    ASSERT_TRUE(text.find("AVX_PARITY_HELPER_TIMEOUT_RETRY attempt=1") != std::string::npos);
    ASSERT_TRUE(text.find("AVX_PARITY_HELPER_DIAGNOSTIC_RERUN attempt=2 outcome=ok") != std::string::npos);
}

// Two runs against one temp root stand in for two console_tests processes running at once.
TEST(AvxParityRunner, RunsSharingATempRootNeverShareOrRemoveEachOthersFiles)
{
    QTemporaryDir root;
    ASSERT_TRUE(root.isValid());

    const auto write_file = [](const QString & path) {
        QFile file(path);
        return file.open(QIODevice::WriteOnly) && file.write("x") == 1;
    };

    QString first_marker;
    QString first_output_json;
    {
        const RunnerScratch first(root.path());
        first_marker = first.marker();
        first_output_json = first.output_json();
        ASSERT_TRUE(write_file(first_marker));
        ASSERT_TRUE(write_file(first_output_json));
        {
            const RunnerScratch second(root.path());
            // Starting the second run leaves the first run's files alone.
            ASSERT_TRUE(QFileInfo::exists(first_marker));
            ASSERT_TRUE(QFileInfo::exists(first_output_json));
            ASSERT_TRUE(second.marker() != first_marker);
            ASSERT_TRUE(second.output_json() != first_output_json);
            ASSERT_TRUE(write_file(second.marker()));
            ASSERT_TRUE(write_file(second.output_json()));
        }
        // Finishing the second run leaves them alone too.
        ASSERT_TRUE(QFileInfo::exists(first_marker));
        ASSERT_TRUE(QFileInfo::exists(first_output_json));
    }
    // Each run removes what it created.
    ASSERT_FALSE(QFileInfo::exists(first_marker));
    ASSERT_FALSE(QFileInfo::exists(first_output_json));
    ASSERT_TRUE(QDir(root.path()).entryList(QDir::AllEntries | QDir::NoDotAndDotDot | QDir::Hidden).isEmpty());
}

// The filler line an overlapping run prints over and over; it names the run, so any of it found in
// the other run's output came from the wrong child.
static QByteArray overlap_filler_tag(const QString & label)
{
    return ("AVX_OVERLAP_FILL " + label + " ").toUtf8();
}

// How many filler lines an overlapping run prints. Each line is the tag five times, so 1000 lines
// come to more than 100 KB, well past what the parent keeps.
static const int kOverlapFillerLines = 1000;

// Marks its own start, waits until the peer run has marked its start, prints its filler lines, then
// its marker last, writes a hash json naming itself and exits 0. If the peer has not started within
// `peer_wait_seconds` it exits 9, so two such runs only both pass when they are alive at the same time.
static QStringList overlapping_run_arguments(const QString & label,
                                             const QString & own_started,
                                             const QString & peer_started,
                                             const QString & output_path,
                                             int peer_wait_seconds)
{
    const QString filler = QString::fromUtf8(overlap_filler_tag(label));
    return stand_in_arguments(
        QStringLiteral("New-Item -ItemType File -Path '%2' | Out-Null; $deadline = (Get-Date).AddSeconds(%5); "
                       "while (-not (Test-Path -LiteralPath '%3')) { if ((Get-Date) -gt $deadline) { "
                       "[Console]::Out.WriteLine('AVX_OVERLAP_PEER_NEVER_STARTED %1'); exit 9 }; Start-Sleep -Milliseconds 50 }; "
                       "$line = '%6' * 5; for ($i = 0; $i -lt %7; $i++) { [Console]::Out.WriteLine($line) }; "
                       "[Console]::Out.WriteLine('AVX_OVERLAP_SAW_PEER %1'); "
                       "Set-Content -LiteralPath '%4' -Value '{\"label\":\"%1\"}'; exit 0")
            .arg(label, own_started, peer_started, output_path, QString::number(peer_wait_seconds), filler,
                 QString::number(kOverlapFillerLines)),
        QStringLiteral(": > '%2'; i=0; while [ ! -e '%3' ]; do if [ $i -ge %5 ]; then "
                       "echo 'AVX_OVERLAP_PEER_NEVER_STARTED %1'; exit 9; fi; sleep 0.1; i=$((i+1)); done; "
                       "line='%6%6%6%6%6'; i=0; while [ $i -lt %7 ]; do echo \"$line\"; i=$((i+1)); done; "
                       "echo 'AVX_OVERLAP_SAW_PEER %1'; printf '{\"label\":\"%1\"}' > '%4'; exit 0")
            .arg(label, own_started, peer_started, output_path, QString::number(peer_wait_seconds * 10), filler,
                 QString::number(kOverlapFillerLines)));
}

// Two runner calls on two threads, each running its own helper process against one shared temp root,
// stand in for two console_tests processes whose helpers run at once.
TEST(AvxParityRunner, OverlappingHelperRunsKeepTheirOwnOutputsAndRemoveTheirScratch)
{
    const QString shell = stand_in_shell();
    if (shell.isEmpty()) {
        SKIP_TEST("no shell found to stand in for overlapping helpers");
    }

    QTemporaryDir root;
    ASSERT_TRUE(root.isValid());

    struct Run {
        QString label;
        ScratchPaths paths;
        QString started;
        ProcessRunResult result;
        std::ostringstream diagnostics;
        qint64 start_ms = -1;
        qint64 end_ms = -1;
    };
    Run runs[2];
    runs[0].label = QStringLiteral("first");
    runs[1].label = QStringLiteral("second");
    {
        const RunnerScratch first(root.path());
        const RunnerScratch second(root.path());
        runs[0].paths = first.paths();
        runs[1].paths = second.paths();
        for (Run & run : runs) {
            run.started = QDir(run.paths.dir).filePath(QStringLiteral("started"));
        }

        // Both threads wait on one gate so their helpers start together. Each runner call is bounded
        // by its helper timeout, so a hung helper fails the test instead of stalling the joins.
        const auto clock_start = std::chrono::steady_clock::now();
        const auto now_ms = [clock_start]() {
            return static_cast<qint64>(std::chrono::duration_cast<std::chrono::milliseconds>(
                                           std::chrono::steady_clock::now() - clock_start).count());
        };
        std::promise<void> go;
        const std::shared_future<void> gate = go.get_future().share();
        const auto invoke = [gate, &shell, now_ms](Run & run, const Run & peer) {
            gate.wait();
            run.start_ms = now_ms();
            run.result = run_helper_with_retry(run.label,
                                               shell,
                                               overlapping_run_arguments(run.label, run.started, peer.started,
                                                                         run.paths.output_json, 20),
                                               run.paths.dir,
                                               QProcessEnvironment::systemEnvironment(),
                                               45 * 1000,
                                               run.paths.output_json,
                                               run.diagnostics);
            run.end_ms = now_ms();
        };
        std::thread first_thread(invoke, std::ref(runs[0]), std::cref(runs[1]));
        std::thread second_thread(invoke, std::ref(runs[1]), std::cref(runs[0]));
        go.set_value();
        first_thread.join();
        second_thread.join();

        std::cout << "AVX_OVERLAP_EVIDENCE first=[" << runs[0].start_ms << "," << runs[0].end_ms
                  << "] ms second=[" << runs[1].start_ms << "," << runs[1].end_ms << "] ms\n";
        for (const Run & run : runs) {
            if (!run.result.ok()) {
                std::cerr << run.diagnostics.str()
                          << describe_process_failure(run.label, shell, run.result, run.paths.output_json).toStdString()
                          << "\n";
            }
        }
        for (const Run & run : runs) {
            ASSERT_TRUE(run.result.ok());
            // The helper saw its peer's start mark before it finished, so the two processes overlapped.
            ASSERT_TRUE(run.result.output.contains(("AVX_OVERLAP_SAW_PEER " + run.label).toUtf8()));
        }
        // Each run printed more than the parent keeps, so what it kept is a bounded tail of that run's
        // output alone: its own filler and marker, and not one line of the peer's.
        for (int index = 0; index < 2; ++index) {
            const Run & run = runs[index];
            const Run & peer = runs[1 - index];
            ASSERT_TRUE(run.result.output_total_bytes > kRetainedOutputBytes);
            ASSERT_TRUE(run.result.output.contains(overlap_filler_tag(run.label)));
            ASSERT_FALSE(run.result.output.contains(overlap_filler_tag(peer.label)));
            ASSERT_FALSE(run.result.output.contains(("AVX_OVERLAP_SAW_PEER " + peer.label).toUtf8()));
            ASSERT_TRUE(run.result.output.size() <= kRetainedOutputBytes);
        }
        // Each runner call started before the other one returned.
        ASSERT_TRUE(runs[0].start_ms < runs[1].end_ms);
        ASSERT_TRUE(runs[1].start_ms < runs[0].end_ms);
        // Each run's hash json is its own.
        ASSERT_TRUE(runs[0].paths.output_json != runs[1].paths.output_json);
        for (const Run & run : runs) {
            ASSERT_EQ(run.label.toStdString(),
                      load_json_file(run.paths.output_json).value(QStringLiteral("label")).toString().toStdString());
        }
    }
    // Once both runs have ended, each one's scratch is gone and nothing is left in the shared root.
    for (const Run & run : runs) {
        ASSERT_EQ(std::string(), scratch_left_behind(run.paths));
        ASSERT_FALSE(QFileInfo::exists(run.started));
    }
    ASSERT_TRUE(QDir(root.path()).entryList(QDir::AllEntries | QDir::NoDotAndDotDot | QDir::Hidden).isEmpty());
}

TEST(AvxParityRunner, OutputTailStaysBoundedAndCountsEveryByte)
{
    const qsizetype capacity = 2 * kOutputTailBytes;
    OutputTail tail(capacity);
    qint64 fed = 0;
    QByteArray last_chunk;
    for (int i = 0; i < 1000; ++i) {
        last_chunk = QByteArray(97, static_cast<char>('a' + (i % 26)));
        tail.append(last_chunk);
        fed += last_chunk.size();
        ASSERT_TRUE(tail.bytes().size() <= capacity);
    }
    ASSERT_EQ(fed, tail.total_bytes());
    ASSERT_EQ(capacity, tail.bytes().size());
    ASSERT_TRUE(tail.bytes().endsWith(last_chunk));

    // One chunk larger than the whole buffer keeps only its own last bytes.
    QByteArray big(3 * capacity, 'y');
    big.append("END");
    tail.append(big);
    fed += big.size();
    ASSERT_EQ(fed, tail.total_bytes());
    ASSERT_EQ(capacity, tail.bytes().size());
    ASSERT_TRUE(tail.bytes().endsWith("yEND"));
    ASSERT_FALSE(tail.bytes().contains(last_chunk.left(1)));
}
