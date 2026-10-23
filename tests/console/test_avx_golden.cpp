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
#include <QStandardPaths>
#include <QStringList>

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

struct ProcessRunResult {
    bool started = false;
    bool timed_out = false;
    bool crashed = false;
    int exit_code = -1;
    qint64 elapsed_ms = 0;
    QByteArray output;
    QString error_string;
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
    QElapsedTimer timer;
    timer.start();
    process.start();
    if (!process.waitForStarted()) {
        result.error_string = process.errorString();
        result.elapsed_ms = timer.elapsed();
        return result;
    }
    result.started = true;

    // Drain the merged channel while waiting, so a chatty child never stalls on a full pipe.
    while (!process.waitForFinished(250)) {
        result.output += process.readAll();
        if (process.state() == QProcess::NotRunning) {
            break;
        }
        if (timer.elapsed() >= timeout_ms) {
            result.timed_out = true;
            process.kill();
            process.waitForFinished(10 * 1000);
            break;
        }
    }
    result.output += process.readAll();
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
                     QString::number(result.output.size()));
    if (!result.error_string.isEmpty()) {
        text += QStringLiteral(" error=") + result.error_string;
    }
    text += QStringLiteral("\n--- helper output tail (last %1 bytes) ---\n")
                .arg(qMin<qint64>(result.output.size(), kOutputTailBytes));
    text += output_tail(result.output);
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
    return helper_path;
}

static QJsonObject run_helper_and_collect(const QString & helper_path, const QString & label)
{
    const QString output_path = QDir(QFileInfo(helper_path).absolutePath()).filePath(QStringLiteral("avx_parity_hashes.json"));
    QFile::remove(output_path);

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

static QString missing_output_json()
{
    const QString path = QDir(QDir::tempPath()).filePath(QStringLiteral("avx_parity_runner_missing_output.json"));
    QFile::remove(path);
    return path;
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

    const QString output_path = missing_output_json();
    std::ostringstream diagnostics;
    const ProcessRunResult result = run_helper_with_retry(QStringLiteral("default"),
                                                          shell,
                                                          chatty_failure_arguments(),
                                                          QDir::tempPath(),
                                                          QProcessEnvironment::systemEnvironment(),
                                                          60 * 1000,
                                                          output_path,
                                                          diagnostics);

    ASSERT_FALSE(result.ok());
    ASSERT_FALSE(result.timed_out);
    ASSERT_EQ(7, result.exit_code);
    ASSERT_TRUE(result.output.size() > 1000000);
    // A non-zero exit is reported as is; only a timeout earns the single retry.
    ASSERT_TRUE(diagnostics.str().find("AVX_PARITY_HELPER_TIMEOUT_RETRY") == std::string::npos);

    const QString text = describe_process_failure(QStringLiteral("default"), shell, result, output_path);
    ASSERT_TRUE(text.contains(QStringLiteral("helper=default")));
    ASSERT_TRUE(text.contains(QStringLiteral("exit_code=7")));
    ASSERT_TRUE(text.contains(QStringLiteral("elapsed_s=")));
    ASSERT_TRUE(text.contains(QStringLiteral("output_json_exists=no")));
    ASSERT_TRUE(text.contains(QStringLiteral("AVX_PARITY_TAIL_MARKER")));
    ASSERT_TRUE(text.size() < 6000);
}

TEST(AvxParityRunner, HelperTimeoutRetriesOnceAndReportsTheFirstAttempt)
{
    const QString shell = stand_in_shell();
    if (shell.isEmpty()) {
        SKIP_TEST("no shell found to stand in for a hung helper");
    }

    std::ostringstream diagnostics;
    QElapsedTimer timer;
    timer.start();
    const ProcessRunResult result = run_helper_with_retry(QStringLiteral("avx"),
                                                          shell,
                                                          sleeper_arguments(20),
                                                          QDir::tempPath(),
                                                          QProcessEnvironment::systemEnvironment(),
                                                          1500,
                                                          missing_output_json(),
                                                          diagnostics);

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

    const QString marker = QDir(QDir::tempPath()).filePath(QStringLiteral("avx_parity_runner_first_run.marker"));
    QFile::remove(marker);
    const QString output_path = missing_output_json();
    std::ostringstream diagnostics;
    const ProcessRunResult result = run_helper_with_retry(QStringLiteral("default"),
                                                          shell,
                                                          hang_then_pass_arguments(marker, output_path),
                                                          QDir::tempPath(),
                                                          QProcessEnvironment::systemEnvironment(),
                                                          8000,
                                                          output_path,
                                                          diagnostics);
    QFile::remove(marker);

    // The first attempt hung, so the run fails however the second attempt ends.
    ASSERT_TRUE(result.timed_out);
    ASSERT_FALSE(result.ok());
    // The second attempt's hashes never reach the parity comparison.
    ASSERT_FALSE(QFileInfo::exists(output_path));
    const std::string text = diagnostics.str();
    ASSERT_TRUE(text.find("AVX_PARITY_HELPER_TIMEOUT_RETRY attempt=1") != std::string::npos);
    ASSERT_TRUE(text.find("AVX_PARITY_HELPER_DIAGNOSTIC_RERUN attempt=2 outcome=ok") != std::string::npos);
}
