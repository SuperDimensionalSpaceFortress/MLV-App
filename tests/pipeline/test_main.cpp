#include "../common/minitest.h"
#include "../common/repo_paths.h"
#include "../common/test_artifacts.h"
#include "../common/test_runtime.h"

#include "playback_path_test_state.h"

#include "../../src/debug/ForceSingleThread.h"
#include "../../src/debug/StageTimingCsvSink.h"
#include "../../platform/qt/CrashForensics.h"

#include <QGuiApplication>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QStringList>

#include <algorithm>
#include <iostream>
#include <map>
#include <string>
#include <vector>

#ifdef Q_OS_WIN
#include <windows.h>
#endif

static bool golden_oracle_is_clean(const QString & golden_path, std::string * error_message)
{
    const QString repo_root = find_repo_root();
    if (repo_root.isEmpty()) {
        if (error_message) {
            *error_message = "Golden oracle cleanliness could not be verified: repository root not found";
        }
        return false;
    }

    const QString absolute_path = QFileInfo(golden_path).absoluteFilePath();
    const QString relative_path = QDir(repo_root).relativeFilePath(absolute_path);
    if (relative_path == QStringLiteral("..") ||
        relative_path.startsWith(QStringLiteral("../"))) {
        return true;
    }

    QProcess git;
    git.setProgram(QStringLiteral("git"));
    git.setArguments({QStringLiteral("-C"),
                      repo_root,
                      QStringLiteral("status"),
                      QStringLiteral("--porcelain=v1"),
                      QStringLiteral("--untracked-files=all"),
                      QStringLiteral("--"),
                      relative_path});
    git.start();
    if (!git.waitForStarted(10000)) {
        if (error_message) {
            *error_message = "Golden oracle cleanliness could not be verified: git did not start";
        }
        return false;
    }
    if (!git.waitForFinished(10000)) {
        git.kill();
        git.waitForFinished();
        if (error_message) {
            *error_message = "Golden oracle cleanliness could not be verified: git status timed out";
        }
        return false;
    }
    if (git.exitStatus() != QProcess::NormalExit || git.exitCode() != 0) {
        if (error_message) {
            *error_message = "Golden oracle cleanliness could not be verified: git status failed";
        }
        return false;
    }

    const QString dirty_output = QString::fromLocal8Bit(git.readAllStandardOutput());
    if (dirty_output.trimmed().isEmpty()) {
        return true;
    }

    if (error_message) {
        std::string message = "Golden oracle is dirty; refusing --check-golden:";
        const QStringList rows = dirty_output.split(QLatin1Char('\n'), Qt::SkipEmptyParts);
        for (QString row : rows) {
            if (row.endsWith(QLatin1Char('\r'))) {
                row.chop(1);
            }
            const QString path = row.size() > 3 ? row.mid(3).trimmed() : row.trimmed();
            message += " " + path.toStdString();
        }
        *error_message = message;
    }
    return false;
}

static bool compare_against_golden(const QString & golden_path, std::string * error_message)
{
    QFile golden_file(golden_path);
    if (!golden_file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        if (error_message) {
            *error_message = "Could not open golden artifact file: " + golden_path.toStdString();
        }
        return false;
    }

    QJsonParseError parse_error;
    const QJsonDocument document = QJsonDocument::fromJson(golden_file.readAll(), &parse_error);
    if (parse_error.error != QJsonParseError::NoError || !document.isObject()) {
        if (error_message) {
            *error_message = "Could not parse golden artifact file: " + golden_path.toStdString();
        }
        return false;
    }

    std::map<std::string, std::string> expected;
    const QJsonObject object = document.object();
    for (auto it = object.begin(); it != object.end(); ++it) {
        expected.emplace(it.key().toStdString(), it.value().toString().toStdString());
    }

    const auto & actual = test_artifacts::all();

    auto is_optional_gpu_key = [](const std::string & key) -> bool
    {
        return key.find(".gpu.") != std::string::npos;
    };

    std::vector<std::string> missing_keys;
    std::vector<std::string> mismatched_keys;

    for (const auto & expected_entry : expected) {
        const auto actual_it = actual.find(expected_entry.first);
        if (actual_it == actual.end()) {
            if (is_optional_gpu_key(expected_entry.first)) {
                continue;
            }
            missing_keys.push_back(expected_entry.first);
            continue;
        }
        if (actual_it->second != expected_entry.second) {
            mismatched_keys.push_back(expected_entry.first +
                                      " expected=" + expected_entry.second +
                                      " actual=" + actual_it->second);
        }
    }

    std::vector<std::string> unexpected_keys;
    for (const auto & actual_entry : actual) {
        if (expected.find(actual_entry.first) == expected.end()) {
            unexpected_keys.push_back(actual_entry.first);
        }
    }

    if (error_message && (!missing_keys.empty() || !mismatched_keys.empty() || !unexpected_keys.empty())) {
        std::string message;
        for (const auto & key : missing_keys) {
            message += "Golden artifact missing actual key: " + key + "\n";
        }
        for (const auto & key : mismatched_keys) {
            message += "Golden artifact mismatch: " + key + "\n";
        }
        for (const auto & key : unexpected_keys) {
            message += "Golden artifact contains unexpected key: " + key + "\n";
        }
        if (!message.empty() && message.back() == '\n') {
            message.pop_back();
        }
        *error_message = message;
    }

    return missing_keys.empty() && mismatched_keys.empty() && unexpected_keys.empty();
}

struct ParsedTestFilter {
    std::vector<std::string> include;
    std::vector<std::string> exclude;
};

static std::vector<std::string> split_filter_patterns(const std::string & value)
{
    std::vector<std::string> patterns;
    std::size_t start = 0;
    while (start <= value.size()) {
        const std::size_t end = value.find(':', start);
        const std::string pattern =
            value.substr(start, end == std::string::npos ? std::string::npos : end - start);
        if (!pattern.empty()) {
            patterns.push_back(pattern);
        }
        if (end == std::string::npos) {
            break;
        }
        start = end + 1;
    }
    return patterns;
}

static ParsedTestFilter parse_test_filter(const std::string & value)
{
    ParsedTestFilter filter;
    const std::size_t negative_separator = value.find('-');
    const std::string include_part = negative_separator == std::string::npos
        ? value
        : value.substr(0, negative_separator);
    const std::string exclude_part = negative_separator == std::string::npos
        ? std::string()
        : value.substr(negative_separator + 1);

    filter.include = split_filter_patterns(include_part);
    if (filter.include.empty()) {
        filter.include.push_back("*");
    }
    filter.exclude = split_filter_patterns(exclude_part);
    return filter;
}

static bool pattern_matches_test_name(const std::string & test_name,
                                      const std::string & pattern)
{
    if (pattern == "*") {
        return true;
    }
    const bool suffix_wildcard =
        !pattern.empty() && pattern[pattern.size() - 1] == '*';
    if (!suffix_wildcard) {
        return test_name == pattern;
    }

    const std::string prefix = pattern.substr(0, pattern.size() - 1);
    bool matches = test_name.compare(0, prefix.size(), prefix) == 0;
    if (!matches && !prefix.empty() && prefix[prefix.size() - 1] == '_') {
        std::string dotted_prefix = prefix;
        dotted_prefix[dotted_prefix.size() - 1] = '.';
        matches = test_name.compare(0, dotted_prefix.size(), dotted_prefix) == 0;
    }
    return matches;
}

static bool test_matches_filter(const minitest::TestCase & test,
                                const ParsedTestFilter & filter)
{
    const std::string test_name =
        std::string(test.suite) + "." + std::string(test.name);
    const bool included =
        std::any_of(filter.include.begin(),
                    filter.include.end(),
                    [&](const std::string & pattern) {
                        return pattern_matches_test_name(test_name, pattern);
                    });
    if (!included) {
        return false;
    }
    return std::none_of(filter.exclude.begin(),
                        filter.exclude.end(),
                        [&](const std::string & pattern) {
                            return pattern_matches_test_name(test_name, pattern);
                        });
}

static bool apply_complex_filter_to_registry(const std::string & value)
{
    const ParsedTestFilter filter = parse_test_filter(value);
    const bool needs_registry_filter =
        !filter.exclude.empty() || filter.include.size() > 1;
    if (!needs_registry_filter) {
        return false;
    }

    std::vector<minitest::TestCase> selected;
    for (const minitest::TestCase & test : minitest::registry()) {
        if (test_matches_filter(test, filter)) {
            selected.push_back(test);
        }
    }
    minitest::registry() = selected;
    return true;
}

static int run_all_with_playback_path_reset()
{
    int failed = 0;
    for (const minitest::TestCase & test : minitest::registry()) {
        const std::string test_name =
            std::string(test.suite) + "." + std::string(test.name);
        if (!minitest::active_filter().empty()) {
            const std::string & filter = minitest::active_filter();
            const bool suffix_wildcard =
                !filter.empty() && filter[filter.size() - 1] == '*';
            const std::string prefix =
                suffix_wildcard ? filter.substr(0, filter.size() - 1) : filter;
            if (suffix_wildcard) {
                bool matches = test_name.compare(0, prefix.size(), prefix) == 0;
                if (!matches && !prefix.empty() && prefix[prefix.size() - 1] == '_') {
                    std::string dotted_prefix = prefix;
                    dotted_prefix[dotted_prefix.size() - 1] = '.';
                    matches = test_name.compare(0, dotted_prefix.size(), dotted_prefix) == 0;
                }
                if (!matches) {
                    continue;
                }
            } else if (test_name != filter) {
                continue;
            }
        }

        ScopedPlaybackPathTestState::resetProcessStateForTest();
        try {
            test.fn();
            std::cout << "[PASS] " << test.suite << "." << test.name << "\n";
        } catch (const minitest::Skip & skip_error) {
            minitest::skip_count() += 1;
            std::cout << "[SKIP] " << test.suite << "." << test.name
                      << " - " << skip_error.what() << "\n";
        } catch (const minitest::Failure & failure) {
            failed += 1;
            std::cerr << "[FAIL] " << test.suite << "." << test.name
                      << " - " << failure.what() << "\n";
        } catch (const std::exception & error) {
            failed += 1;
            std::cerr << "[FAIL] " << test.suite << "." << test.name
                      << " - unexpected exception: " << error.what() << "\n";
        } catch (...) {
            failed += 1;
            std::cerr << "[FAIL] " << test.suite << "." << test.name
                      << " - unknown exception\n";
        }
        ScopedPlaybackPathTestState::resetProcessStateForTest();
    }

    std::cout << "[SUMMARY] tests=" << minitest::registry().size()
              << " assertions=" << minitest::assertion_count()
              << " skipped=" << minitest::skip_count()
              << " failed=" << failed << "\n";
    return failed;
}

int main(int argc, char ** argv)
{
    test_runtime::force_single_threaded_pipeline();
    mlvapp_force_singlethread_init();
    test_runtime::prefer_desktop_opengl_on_windows();
#ifdef Q_OS_WIN
    // CUDA-PLAYBACK-LOOK-PARITY-1 round 2: a fail-fast crash in this process (e.g. a GL
    // driver abort) previously raised an interactive WER dialog on the owner's desktop.
    // Per-process only -- never a system setting -- matching tests/gui/test_gui_smoke.cpp.
    SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX | SEM_NOOPENFILEERRORBOX);
    if (qEnvironmentVariableIsEmpty("QT_QPA_PLATFORM")) {
        qputenv("QT_QPA_PLATFORM", QByteArrayLiteral("windows"));
    }
#else
    if (qEnvironmentVariableIsEmpty("QT_QPA_PLATFORM")) {
        qputenv("QT_QPA_PLATFORM", QByteArrayLiteral("offscreen"));
    }
#endif
    QGuiApplication app(argc, argv);

    /* Hosted CI can opt into the existing minidump/log handler without
     * changing the default local test-runner behavior or the CrashForensics
     * unit-test's idempotence/setup contract. */
    if (qEnvironmentVariableIntValue("MLVAPP_CRASH_FORENSICS_AUTOINSTALL") == 1) {
        CrashForensics::install(argc, argv);
        CrashForensics::logStartupMetadata();
    }

    std::string hash_output_path;
    std::string stage_csv_path;
    std::string test_filter;
    QString golden_input_path;
    bool check_golden_requested = false;
    for (int index = 1; index < argc; ++index) {
        const std::string argument = argv[index];
        if (argument == "--hash-output" && (index + 1) < argc) {
            hash_output_path = argv[++index];
        } else if (argument == "--profile-stages-to-csv" && (index + 1) < argc) {
            stage_csv_path = argv[++index];
        } else if (argument.rfind("--profile-stages-to-csv=", 0) == 0) {
            stage_csv_path = argument.substr(std::string("--profile-stages-to-csv=").size());
        } else if (argument == "--check-golden") {
            check_golden_requested = true;
            if ((index + 1) < argc && std::string(argv[index + 1]).rfind("--", 0) != 0) {
                golden_input_path = QString::fromLocal8Bit(argv[++index]);
            } else {
                golden_input_path = repo_file_path(QStringLiteral("tests/fixtures/golden/pipeline_hashes.json"));
            }
        } else if (argument == "--gtest_filter" && (index + 1) < argc) {
            test_filter = argv[++index];
        } else if (argument.rfind("--gtest_filter=", 0) == 0) {
            test_filter = argument.substr(std::string("--gtest_filter=").size());
        }
    }

    if (!test_filter.empty() && !apply_complex_filter_to_registry(test_filter)) {
        minitest::set_filter(test_filter);
    }

    if (!test_filter.empty()) {
        std::cerr << "[RECEIVED-FILTER] " << test_filter << "\n";
    }

    if (check_golden_requested) {
        std::string error_message;
        if (golden_input_path.isEmpty()) {
            error_message = "Golden oracle cleanliness could not be verified: golden path is empty";
        } else if (golden_oracle_is_clean(golden_input_path, &error_message)) {
            error_message.clear();
        }
        if (!error_message.empty()) {
            std::cerr << "[ERROR] " << error_message << "\n";
            return 5;
        }
    }

    if (!stage_csv_path.empty()
        && stage_timing_csv_sink_open(stage_csv_path.c_str()) == 0) {
        std::cerr << "[ERROR] Could not open stage CSV sink: " << stage_csv_path << "\n";
        return 4;
    }

    const int failed = run_all_with_playback_path_reset();
    stage_timing_csv_sink_close();

    if (!hash_output_path.empty()) {
        std::string error_message;
        if (!test_artifacts::write_json(hash_output_path, &error_message)) {
            std::cerr << "[ERROR] " << error_message << "\n";
            return 2;
        }
    }

    if (!golden_input_path.isEmpty()) {
        std::string error_message;
        if (!compare_against_golden(golden_input_path, &error_message)) {
            std::cerr << "[ERROR] " << error_message << "\n";
            return 3;
        }
    }

    return failed == 0 ? 0 : 1;
}
