#include "../common/minitest.h"
#include "../common/hash_helpers.h"
#include "../common/repo_paths.h"

#include <QByteArray>
#include <QDir>
#include <QDirIterator>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLibraryInfo>
#include <QProcess>
#include <QProcessEnvironment>
#include <QTemporaryDir>
#include <QTextStream>

#include <cmath>
#include <array>
#include <iostream>
#include <map>

static QString clip_fixture_path()
{
    return repo_file_path(QStringLiteral("tests/fixtures/clips/tiny_dual_iso.mlv"));
}

static QString clip_receipt_path()
{
    return repo_file_path(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"));
}

static QString clip_preview_receipt_path()
{
    return repo_file_path(QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_preview.marxml"));
}

static QString large_dual_iso_fixture_path()
{
    return repo_file_path(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv"));
}

static QString large_dual_iso_hq_receipt_path()
{
    return repo_file_path(QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml"));
}

static bool write_look_assist_receipt_fixture(const QString & receipt_path)
{
    QFile file(receipt_path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
        return false;
    }

    QTextStream out(&file);
    out << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
    out << "<receipt version=\"4\" mlvapp=\"test\">\n";
    out << "  <exposure>3</exposure>\n";
    out << "  <contrast>7</contrast>\n";
    out << "  <pivot>91</pivot>\n";
    out << "  <temperature>6400</temperature>\n";
    out << "  <tint>9</tint>\n";
    out << "  <vibrance>15</vibrance>\n";
    out << "  <shadows>22</shadows>\n";
    out << "  <highlights>-6</highlights>\n";
    out << "  <rawFixesEnabled>1</rawFixesEnabled>\n";
    out << "  <rawBlack>20470</rawBlack>\n";
    out << "  <rawWhite>2840</rawWhite>\n";
    out << "  <lookAssistEnabled>1</lookAssistEnabled>\n";
    out << "  <lookAssistBaselineValid>1</lookAssistBaselineValid>\n";
    out << "  <lookAssistBaselineExposure>10</lookAssistBaselineExposure>\n";
    out << "  <lookAssistBaselineContrast>-4</lookAssistBaselineContrast>\n";
    out << "  <lookAssistBaselinePivot>47</lookAssistBaselinePivot>\n";
    out << "  <lookAssistBaselineTemperature>5200</lookAssistBaselineTemperature>\n";
    out << "  <lookAssistBaselineTint>-7</lookAssistBaselineTint>\n";
    out << "  <lookAssistBaselineVibrance>5</lookAssistBaselineVibrance>\n";
    out << "  <lookAssistBaselineShadows>18</lookAssistBaselineShadows>\n";
    out << "  <lookAssistBaselineHighlights>-12</lookAssistBaselineHighlights>\n";
    out << "  <lookAssistBaselineRawBlack>20490</lookAssistBaselineRawBlack>\n";
    out << "  <lookAssistBaselineRawWhite>3021</lookAssistBaselineRawWhite>\n";
    out << "  <lookAssistBaselineStretchX>1</lookAssistBaselineStretchX>\n";
    out << "  <lookAssistBaselineStretchY>0.3333</lookAssistBaselineStretchY>\n";
    out << "</receipt>\n";

    return file.flush();
}

static bool write_look_toggle_receipt_fixture(const QString & receipt_path, bool look_enabled)
{
    QFile file(receipt_path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
        return false;
    }

    QTextStream out(&file);
    out << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
    out << "<receipt version=\"4\" mlvapp=\"test\">\n";
    out << "  <exposure>0</exposure>\n";
    out << "  <rawFixesEnabled>1</rawFixesEnabled>\n";
    out << "  <lookAssistEnabled>" << (look_enabled ? 1 : 0) << "</lookAssistEnabled>\n";
    out << "  <lookAssistBaselineValid>1</lookAssistBaselineValid>\n";
    out << "  <lookAssistBaselineExposure>10</lookAssistBaselineExposure>\n";
    out << "  <lookAssistBaselineTemperature>5200</lookAssistBaselineTemperature>\n";
    out << "  <lookAssistBaselineTint>-7</lookAssistBaselineTint>\n";
    out << "  <lookAssistBaselineRawBlack>20490</lookAssistBaselineRawBlack>\n";
    out << "  <lookAssistBaselineRawWhite>3021</lookAssistBaselineRawWhite>\n";
    out << "</receipt>\n";

    return file.flush();
}

static bool write_collapsed_cutout_receipt_fixture(const QString & receipt_path)
{
    QFile file(receipt_path);
    if (!file.open(QIODevice::WriteOnly | QIODevice::Text | QIODevice::Truncate)) {
        return false;
    }

    QTextStream out(&file);
    out << "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n";
    out << "<receipt version=\"4\" mlvapp=\"test\">\n";
    out << "  <exposure>0</exposure>\n";
    out << "  <contrast>0</contrast>\n";
    out << "  <pivot>75</pivot>\n";
    out << "  <temperature>5000</temperature>\n";
    out << "  <tint>0</tint>\n";
    out << "  <rawFixesEnabled>1</rawFixesEnabled>\n";
    out << "  <rawBlack>20470</rawBlack>\n";
    out << "  <rawWhite>2840</rawWhite>\n";
    out << "  <lookAssistEnabled>0</lookAssistEnabled>\n";
    out << "  <cutIn>1</cutIn>\n";
    out << "  <cutOut>1</cutOut>\n";
    out << "  <debayer>2</debayer>\n";
    out << "</receipt>\n";

    return file.flush();
}

static QString clip_manifest_path()
{
    return repo_file_path(QStringLiteral("tests/fixtures/golden/tiny_dual_iso_hq_dng_hashes.json"));
}

static QString batch_executable_path()
{
    const QString env_path = qEnvironmentVariable("MLVAPP_BATCH_EXE");
    if (!env_path.isEmpty()) {
        return env_path;
    }
    return QString();
}

static QString app_executable_path()
{
    const QString profile_path = qEnvironmentVariable("MLVAPP_PROFILE_EXE");
    if (!profile_path.isEmpty()) {
        return profile_path;
    }

    const QString batch_path = batch_executable_path();
    if (!batch_path.isEmpty()) {
        return batch_path;
    }

    return QString();
}

static void configure_qt_subprocess_environment(QProcessEnvironment & environment);

static QProcessEnvironment playback_profile_environment()
{
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    environment.insert(QStringLiteral("MLVAPP_FORCE_THREADS"), QStringLiteral("1"));
    environment.insert(QStringLiteral("OMP_NUM_THREADS"), QStringLiteral("1"));
    environment.insert(QStringLiteral("OMP_DYNAMIC"), QStringLiteral("FALSE"));
    environment.insert(QStringLiteral("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"), QStringLiteral("0"));
    environment.insert(QStringLiteral("MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW"), QStringLiteral("0"));
    configure_qt_subprocess_environment(environment);
    return environment;
}

static void configure_playback_profile_process(QProcess * process,
                                               const QString & app_exe,
                                               const QString & repo_root,
                                               const QStringList & arguments,
                                               const QList<QPair<QString, QString>> & extra_environment = {})
{
    if (!process) return;
    QProcessEnvironment environment = playback_profile_environment();
    for (const auto & entry : extra_environment) {
        environment.insert(entry.first, entry.second);
    }
    process->setProcessEnvironment(environment);
    process->setWorkingDirectory(repo_root);
    process->setProgram(app_exe);
    process->setArguments(arguments);
}

static void configure_qt_subprocess_environment(QProcessEnvironment & environment)
{
    const QString qt_bin = QLibraryInfo::path(QLibraryInfo::BinariesPath);
    const QString qt_plugins = QLibraryInfo::path(QLibraryInfo::PluginsPath);
    const QString qt_platform_plugins =
        QDir(qt_plugins).filePath(QStringLiteral("platforms"));

    QString path = environment.value(QStringLiteral("PATH"));
    if (!qt_bin.isEmpty()) {
        path = qt_bin + QDir::listSeparator() + path;
        environment.insert(QStringLiteral("PATH"), path);
    }
    if (!qt_plugins.isEmpty()) {
        environment.insert(QStringLiteral("QT_PLUGIN_PATH"), qt_plugins);
    }
    if (!qt_platform_plugins.isEmpty()) {
        environment.insert(QStringLiteral("QT_QPA_PLATFORM_PLUGIN_PATH"), qt_platform_plugins);
    }
    if (environment.value(QStringLiteral("QT_OPENGL")).isEmpty()) {
        environment.insert(QStringLiteral("QT_OPENGL"), QStringLiteral("desktop"));
    }
    // This workspace's local playback-profile seam currently assumes the native
    // Windows plugin is deployed. If Linux CI is ever added for this test, use
    // an Xvfb-backed run or deploy/allow the offscreen platform plugin there.
    environment.insert(QStringLiteral("QT_QPA_PLATFORM"), QStringLiteral("windows"));
}

static QJsonObject first_frame_with_raw_decode_telemetry(const QJsonArray & frames)
{
    for (const QJsonValue & value : frames) {
        if (!value.isObject()) continue;
        const QJsonObject sample = value.toObject();
        if (sample.value(QStringLiteral("raw_uint16_lj92_predictor")).toInt(-1) >= 0) {
            return sample;
        }
        if (sample.value(QStringLiteral("raw_uint16_ms")).toDouble() > 0.0) {
            return sample;
        }
        if (sample.value(QStringLiteral("raw_uint16_lj92_pred6_split_requested")).toBool()) {
            return sample;
        }
        if (sample.value(QStringLiteral("raw_uint16_lj92_generic_split_requested")).toBool()) {
            return sample;
        }
        if (sample.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_measurement_requested")).toBool()) {
            return sample;
        }
    }
    return QJsonObject();
}

static int count_raw_uint16_prefetch_hits(const QJsonArray & frames)
{
    int hits = 0;
    for (const QJsonValue & value : frames) {
        if (!value.isObject()) continue;
        const QJsonObject sample = value.toObject();
        if (sample.value(QStringLiteral("raw_uint16_prefetch_hit")).toBool()) {
            ++hits;
        }
    }
    return hits;
}

static std::map<std::string, std::string> load_expected_hashes(const QString & path)
{
    QFile file(path);
    if (!file.open(QIODevice::ReadOnly | QIODevice::Text)) {
        return {};
    }

    const QJsonDocument document = QJsonDocument::fromJson(file.readAll());
    if (!document.isObject()) {
        return {};
    }

    std::map<std::string, std::string> hashes;
    const QJsonObject object = document.object();
    for (auto it = object.begin(); it != object.end(); ++it) {
        hashes.emplace(it.key().toStdString(), it.value().toString().toStdString());
    }
    return hashes;
}

static std::map<std::string, std::string> collect_dng_hashes(const QString & directory)
{
    std::map<std::string, std::string> hashes;
    QDirIterator it(directory,
                    QStringList() << QStringLiteral("*.dng") << QStringLiteral("*.DNG"),
                    QDir::Files,
                    QDirIterator::Subdirectories);
    const QDir base_dir(directory);
    while (it.hasNext()) {
        const QString absolute_path = it.next();
        const QString relative_path =
            QDir::fromNativeSeparators(base_dir.relativeFilePath(absolute_path));
        hashes.emplace(relative_path.toStdString(), sha256_file(absolute_path));
    }
    return hashes;
}

static int run_batch_export(const QString & batch_exe,
                            const QString & repo_root,
                            const QString & input,
                            const QString & receipt,
                            const QString & output_dir,
                            const QList<QPair<QString, QString>> & extra_environment,
                            std::map<std::string, std::string> * output_hashes)
{
    QProcess process;
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    environment.insert(QStringLiteral("MLVAPP_FORCE_THREADS"), QStringLiteral("1"));
    environment.insert(QStringLiteral("OMP_NUM_THREADS"), QStringLiteral("1"));
    environment.insert(QStringLiteral("OMP_DYNAMIC"), QStringLiteral("FALSE"));
    for (const auto & entry : extra_environment) {
        environment.insert(entry.first, entry.second);
    }

    process.setProcessEnvironment(environment);
    process.setWorkingDirectory(repo_root);
    process.setProgram(batch_exe);
    process.setArguments(QStringList()
                         << QStringLiteral("--batch")
                         << QStringLiteral("--input") << input
                         << QStringLiteral("--output") << output_dir
                         << QStringLiteral("--receipt") << receipt);
    process.start();
    if (!process.waitForStarted()) {
        return -1;
    }
    if (!process.waitForFinished(-1)) {
        return -2;
    }
    if (output_hashes) {
        *output_hashes = collect_dng_hashes(output_dir);
    }
    return process.exitCode();
}

static void require_processing_timing_field(const QJsonObject & sample,
                                            const char * key,
                                            double * max_ms)
{
    const QString field_name = QString::fromLatin1(key);
    ASSERT_TRUE(sample.contains(field_name));

    const QJsonValue value = sample.value(field_name);
    ASSERT_TRUE(value.isDouble());

    const double field_ms = value.toDouble();
    ASSERT_TRUE(field_ms >= 0.0);
    if (max_ms && field_ms > *max_ms) {
        *max_ms = field_ms;
    }
}

static void require_no_gpu_amaze_backend_timing_fields(const QJsonObject & sample)
{
    ASSERT_TRUE(!sample.contains(QStringLiteral("gpu_amaze_debayer_upload_ms")));
    ASSERT_TRUE(!sample.contains(QStringLiteral("gpu_amaze_debayer_kernel_ms")));
    ASSERT_TRUE(!sample.contains(QStringLiteral("gpu_amaze_debayer_download_ms")));
    ASSERT_TRUE(!sample.contains(QStringLiteral("gpu_amaze_debayer_total_ms")));
}

static void require_gpu_amaze_backend_timing_fields(const QJsonObject & sample)
{
    require_processing_timing_field(sample, "gpu_amaze_debayer_upload_ms", nullptr);
    require_processing_timing_field(sample, "gpu_amaze_debayer_kernel_ms", nullptr);
    require_processing_timing_field(sample, "gpu_amaze_debayer_download_ms", nullptr);
    require_processing_timing_field(sample, "gpu_amaze_debayer_total_ms", nullptr);
}

TEST(ClipGolden, GpuAmazeBackendTimingTelemetryRequiresNumericShape)
{
    QJsonObject sample;
    sample.insert(QStringLiteral("gpu_amaze_debayer_upload_ms"), 1.0);
    sample.insert(QStringLiteral("gpu_amaze_debayer_kernel_ms"), 2.0);
    sample.insert(QStringLiteral("gpu_amaze_debayer_download_ms"), 3.0);
    sample.insert(QStringLiteral("gpu_amaze_debayer_total_ms"), 6.0);
    require_gpu_amaze_backend_timing_fields(sample);
}

TEST(ClipGolden, TinyDualIsoBatchExportMatchesGolden)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString manifest_path = clip_manifest_path();
    if (!QFileInfo::exists(manifest_path)) {
        SKIP_TEST("Missing golden manifest tests/fixtures/golden/tiny_dual_iso_hq_dng_hashes.json");
    }

    const QString batch_exe = batch_executable_path();
    if (batch_exe.isEmpty() || !QFileInfo::exists(batch_exe)) {
        SKIP_TEST("Set MLVAPP_BATCH_EXE to a built MLVApp binary with batch support");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    const std::map<std::string, std::string> expected_hashes = load_expected_hashes(manifest_path);
    if (expected_hashes.empty()) {
        SKIP_TEST("Golden manifest is empty or invalid");
    }

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_dir = temp_dir.filePath(QStringLiteral("out"));
    ASSERT_TRUE(QDir().mkpath(output_dir));

    QProcess process;
    QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
    environment.insert(QStringLiteral("MLVAPP_FORCE_THREADS"), QStringLiteral("1"));
    environment.insert(QStringLiteral("OMP_NUM_THREADS"), QStringLiteral("1"));
    environment.insert(QStringLiteral("OMP_DYNAMIC"), QStringLiteral("FALSE"));
    process.setProcessEnvironment(environment);
    process.setWorkingDirectory(repo_root);
    process.setProgram(batch_exe);
    process.setArguments(QStringList()
                         << QStringLiteral("--batch")
                         << QStringLiteral("--input") << fixture_path
                         << QStringLiteral("--output") << output_dir
                         << QStringLiteral("--receipt") << receipt_path);
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    const std::map<std::string, std::string> actual_hashes = collect_dng_hashes(output_dir);
    ASSERT_EQ(expected_hashes.size(), actual_hashes.size());
    if (expected_hashes != actual_hashes) {
        std::cerr << "[ClipGolden] Expected hashes:\n";
        for (const auto & entry : expected_hashes) {
            std::cerr << "  " << entry.first << " " << entry.second << "\n";
        }
        std::cerr << "[ClipGolden] Actual hashes:\n";
        for (const auto & entry : actual_hashes) {
            std::cerr << "  " << entry.first << " " << entry.second << "\n";
        }
    }
    ASSERT_TRUE(expected_hashes == actual_hashes);
}

TEST(ClipGolden, BatchKillSwitchAndDisabledReceiptSuppressLookAssistDngDefaults)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString batch_exe = batch_executable_path();
    if (batch_exe.isEmpty() || !QFileInfo::exists(batch_exe)) {
        SKIP_TEST("Set MLVAPP_BATCH_EXE to a built MLVApp binary with batch support");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());

    const QString enabled_receipt = temp_dir.filePath(QStringLiteral("look_enabled.marxml"));
    const QString disabled_receipt = temp_dir.filePath(QStringLiteral("look_disabled.marxml"));
    ASSERT_TRUE(write_look_toggle_receipt_fixture(enabled_receipt, true));
    ASSERT_TRUE(write_look_toggle_receipt_fixture(disabled_receipt, false));

    const QString look_on_dir = temp_dir.filePath(QStringLiteral("look_on"));
    const QString env_off_dir = temp_dir.filePath(QStringLiteral("env_off"));
    const QString disabled_dir = temp_dir.filePath(QStringLiteral("disabled"));
    ASSERT_TRUE(QDir().mkpath(look_on_dir));
    ASSERT_TRUE(QDir().mkpath(env_off_dir));
    ASSERT_TRUE(QDir().mkpath(disabled_dir));

    std::map<std::string, std::string> look_on_hashes;
    std::map<std::string, std::string> env_off_hashes;
    std::map<std::string, std::string> disabled_hashes;

    ASSERT_EQ(0, run_batch_export(batch_exe, repo_root, fixture_path, enabled_receipt,
                                  look_on_dir, {}, &look_on_hashes));
    ASSERT_EQ(0, run_batch_export(batch_exe, repo_root, fixture_path, enabled_receipt,
                                  env_off_dir,
                                  {{QStringLiteral("MLVAPP_NO_LOOK_ASSIST"), QStringLiteral("1")}},
                                  &env_off_hashes));
    ASSERT_EQ(0, run_batch_export(batch_exe, repo_root, fixture_path, disabled_receipt,
                                  disabled_dir, {}, &disabled_hashes));

    ASSERT_TRUE(!look_on_hashes.empty());
    ASSERT_TRUE(!env_off_hashes.empty());
    ASSERT_TRUE(!disabled_hashes.empty());
    ASSERT_TRUE(env_off_hashes == disabled_hashes);
    ASSERT_TRUE(look_on_hashes != env_off_hashes);
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileProducesJson)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile.json"));
    const QString stage_log = temp_dir.filePath(QStringLiteral("playback-stage.log"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--stage-log") << stage_log
            << QStringLiteral("--threads") << QStringLiteral("1"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject root = document.object();
    const QJsonArray frames = root.value(QStringLiteral("frames")).toArray();
    const QJsonObject metadata = root.value(QStringLiteral("metadata")).toObject();
    ASSERT_EQ(2, frames.size());
    ASSERT_EQ(2, metadata.value(QStringLiteral("measured_frames")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("scope")).toString() == QStringLiteral("none"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("window_visible")).toBool());
    ASSERT_TRUE(!metadata.value(QStringLiteral("wait_for_paint")).toBool());
    /* PROD-TELEMETRY-DURATION-AS-PROOF-3: "ran" is proved above by
     * measured_frames == 2 (an explicit run counter), not by this duration
     * reading positive -- a fast real run on a coarse fallback clock can
     * still read 0.0 ms. */
    ASSERT_TRUE(metadata.value(QStringLiteral("average_latency_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(metadata.value(QStringLiteral("measurement_model")).toString().contains(QStringLiteral("frameReady")));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_policy_active")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_debayer_request")).toString()
                == QStringLiteral("auto"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_debayer_effective")).toString()
                == QStringLiteral("bilinear"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_request")).toString()
                == QStringLiteral("auto"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_selected")).toString()
                == QStringLiteral("receipt"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_effective")).toString()
                == QStringLiteral("receipt"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("playback_processing_supported")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_debayer_receipt")).toString()
                == QStringLiteral("Bilinear"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("playback_debayer_uses_caching")).toBool());
    ASSERT_EQ(0, metadata.value(QStringLiteral("playback_debayer_engine_mode")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("gpu_preview_processing_backend_request")).toString()
                == QStringLiteral("auto"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("gpu_preview_processing_environment_requested")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("gpu_bilinear_debayer_backend_request")).toString()
                == QStringLiteral("auto"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("gpu_bilinear_debayer_environment_requested")).toBool());
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_bilinear_debayer_probe_available")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_bilinear_debayer_probe_reason")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_bilinear_debayer_probe_renderer")));
    ASSERT_TRUE(metadata.value(QStringLiteral("gpu_amaze_debayer_backend_request")).toString()
                == QStringLiteral("auto"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("gpu_amaze_debayer_environment_requested")).toBool());
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_amaze_debayer_probe_available")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_amaze_debayer_probe_reason")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_amaze_debayer_probe_renderer")));
    ASSERT_EQ(1, metadata.value(QStringLiteral("dual_iso_mode_selected")).toInt());
    ASSERT_EQ(2, metadata.value(QStringLiteral("dual_iso_mode_effective")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("dual_iso_preview_runtime_active")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("dual_iso_preview_override_active")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("qt_opengl_environment")).toString()
                == QStringLiteral("desktop"));
    ASSERT_TRUE(metadata.value(QStringLiteral("qt_qpa_platform_environment")).toString()
                == QStringLiteral("windows"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("play_start_preroll_active")).toBool());
    ASSERT_TRUE(!metadata.value(QStringLiteral("play_start_preroll_eligible")).toBool());
    ASSERT_TRUE(!metadata.value(QStringLiteral("play_start_preroll_disabled_by_environment")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_to_first_frame_measured")).toBool());
    /* PROD-TELEMETRY-DURATION-AS-PROOF-3: "ran" is proved by the measured
     * flag above, not by this duration reading positive. */
    ASSERT_TRUE(metadata.value(QStringLiteral("play_to_first_frame_ms")).toDouble() >= 0.0);
    const QJsonObject first_frame = frames.at(0).toObject();
    ASSERT_TRUE(!first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_split_active")).toBool());
    ASSERT_TRUE(!first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_split_requested")).toBool());
    ASSERT_TRUE(!first_frame.value(QStringLiteral("raw_uint16_lj92_generic_split_active")).toBool());
    ASSERT_TRUE(!first_frame.value(QStringLiteral("raw_uint16_lj92_generic_split_requested")).toBool());
    ASSERT_TRUE(!first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_measurement_requested")).toBool());
    ASSERT_TRUE(!first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_measurement_active")).toBool());
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_predictor")).toInt() >= -1);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_total_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_bitstream_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_predictor_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_other_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_total_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_bitstream_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_predictor_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_other_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_total_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_bitstream_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_predictor_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_other_ms")).toDouble() >= 0.0);

    for (const QJsonValue & value : frames) {
        ASSERT_TRUE(value.isObject());
        const QJsonObject sample = value.toObject();
        /* PROD-TELEMETRY-DURATION-AS-PROOF-3: "ran" is proved by
         * measured_frames == 2 and measurement_model containing "frameReady"
         * (asserted once above at the metadata level), not by this per-frame
         * duration reading positive. */
        ASSERT_TRUE(sample.value(QStringLiteral("latency_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.contains(QStringLiteral("engine_latency_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("presentation_overhead_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("draw_frame_ready_queue_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("draw_frame_ready_advance_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("draw_frame_ready_scene_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("draw_frame_ready_image_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("draw_frame_ready_present_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("draw_frame_ready_scopes_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("draw_frame_ready_overlay_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("draw_frame_ready_total_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_preview_histogram_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_preview_regression_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_preview_rowscale_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("headless_presented_color_analysis_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("presentation_overhead_excluding_headless_color_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_worker_threads")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_worker_thread_cap_active")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_uint16_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_uint16_disk_read_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_uint16_decompress_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_uint16_decompress_prepare_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_uint16_decompress_execute_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_uint16_unpack_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_uint16_copy_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_uint16_prefetch_hit")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_uint16_other_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_pre_dualiso_fix_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_total_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_dark_frame_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_vertical_stripes_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_focus_pixels_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_bad_pixels_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_pattern_noise_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_dual_iso_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_chroma_smooth_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("llrawproc_other_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_stage_pre_dualiso_fix_domain")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_stage_pre_dualiso_fix_width")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_stage_pre_dualiso_fix_height")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_stage_pre_dualiso_fix_pixels")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_preview_total_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_mean23_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_scratch_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_clear_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_squeeze_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_rawdata_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_setup_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_create_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_join_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_worker_total_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_worker_max_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_worker_count")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_postprocess_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_grayscale_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_edge_init_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_lut_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_edge_direction_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_edge_simd_batches")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_edge_allskip_batches")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_edge_mixed_batches")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_edge_fullsearch_batches")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_edge_fullsearch_pixels")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_edge_skip_pixels")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_edge_scalar_pixels")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_amaze_actual_interp_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_interp_border_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_mix_alias_map_setup_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_mix_alias_map_init_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_mix_alias_map_copy_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_mix_alias_map_filter_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_mix_alias_map_gaussian_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("dual_iso_full20_mix_alias_map_grayscale_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayered_frame_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("raw_float_convert_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayer_exclusive_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayer_engine_mode")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayer_basic_u16_avx2_available")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayer_basic_u16_avx2_used")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayer_wb_prepare_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayer_ca_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayer_kernel_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayer_wb_undo_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("debayer_pipeline_other_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processing_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processing_chroma_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processing_sharpen_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processing_grain_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processing_other_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processing_core_other_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processed16_total_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processed16_for_8bit_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processed16_to_8bit_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processed8_total_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processed8_direct_path_active")));
        ASSERT_TRUE(sample.contains(QStringLiteral("processed8_prefetch_hit")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_queue_wait_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_work_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_total_ms")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_factor_request")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_factor_effective")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_factor_clamped")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_rendered_width")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_rendered_height")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_target_width")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_target_height")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_upscaling")));
        ASSERT_TRUE(sample.contains(QStringLiteral("playback_prep_owned_rgb8_bytes")));
        ASSERT_TRUE(sample.contains(QStringLiteral("playback_prep_borrowed_rgb8_bytes")));
        ASSERT_TRUE(sample.contains(QStringLiteral("playback_prep_owned_scaled_rgb8_bytes")));
        ASSERT_TRUE(sample.contains(QStringLiteral("playback_prep_borrowed_scaled_rgb8_bytes")));
        ASSERT_TRUE(sample.contains(QStringLiteral("prep_generation")));
        ASSERT_TRUE(sample.contains(QStringLiteral("prep_active_generation")));
        ASSERT_TRUE(sample.contains(QStringLiteral("prep_generation_drops")));
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_preview_histogram_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_preview_regression_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_preview_rowscale_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_disk_read_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_decompress_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_decompress_prepare_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_decompress_execute_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_unpack_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_copy_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_prefetch_hit")).isBool());
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_other_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_pre_dualiso_fix_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_total_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_dark_frame_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_vertical_stripes_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_focus_pixels_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_bad_pixels_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_pattern_noise_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_dual_iso_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_chroma_smooth_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_other_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_stage_pre_dualiso_fix_domain")).toString()
                    == QStringLiteral("raw_bayer"));
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_stage_pre_dualiso_fix_width")).toInt() >= 0);
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_stage_pre_dualiso_fix_height")).toInt() >= 0);
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_stage_pre_dualiso_fix_pixels")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_mean23_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_scratch_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_clear_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_squeeze_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_rawdata_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_setup_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_create_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_join_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_worker_total_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_worker_max_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_demosaic_worker_count")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_postprocess_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_grayscale_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_edge_init_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_lut_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_edge_direction_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_edge_simd_batches")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_edge_allskip_batches")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_edge_mixed_batches")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_edge_fullsearch_batches")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_edge_fullsearch_pixels")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_edge_skip_pixels")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_edge_scalar_pixels")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_amaze_actual_interp_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_interp_border_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_mix_alias_map_setup_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_mix_alias_map_init_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_mix_alias_map_copy_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_mix_alias_map_filter_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_mix_alias_map_gaussian_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_full20_mix_alias_map_grayscale_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("dual_iso_preview_total_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("debayered_frame_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("raw_float_convert_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_exclusive_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_engine_mode")).toInt() >= -1);
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_basic_u16_avx2_available")).isBool());
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_basic_u16_avx2_used")).isBool());
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_wb_prepare_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_ca_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_kernel_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_wb_undo_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_pipeline_other_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("processing_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("processed16_total_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("processed16_for_8bit_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("processed16_to_8bit_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("processed8_total_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("draw_frame_ready_queue_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("draw_frame_ready_advance_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("draw_frame_ready_scene_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("draw_frame_ready_image_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("draw_frame_ready_present_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("draw_frame_ready_scopes_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("draw_frame_ready_overlay_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("draw_frame_ready_total_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("processed8_direct_path_active")).isBool());
        ASSERT_TRUE(sample.value(QStringLiteral("processed8_prefetch_hit")).isBool());
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_queue_wait_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_work_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_total_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_worker_threads")).toInt() > 0);
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_worker_thread_cap_active")).isBool());
        ASSERT_EQ(0.0, sample.value(QStringLiteral("playback_prep_owned_rgb8_bytes")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("playback_prep_borrowed_rgb8_bytes")).toDouble() > 0.0);
        ASSERT_EQ(0.0, sample.value(QStringLiteral("playback_prep_owned_scaled_rgb8_bytes")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("playback_prep_borrowed_scaled_rgb8_bytes")).toDouble() >= 0.0);
        const int requested_scale =
            sample.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt();
        const int effective_scale =
            sample.value(QStringLiteral("render_thread_playback_scale_factor_effective")).toInt();
        ASSERT_TRUE(requested_scale == 1 || requested_scale == 2 || requested_scale == 4 || requested_scale == 8);
        ASSERT_TRUE(effective_scale == 1 || effective_scale == 2 || effective_scale == 4 || effective_scale == 8);
        ASSERT_EQ(effective_scale != requested_scale,
                  sample.value(QStringLiteral("render_thread_playback_scale_factor_clamped")).toBool());
        double processing_max_substage_ms = 0.0;
        require_processing_timing_field(sample, "processing_setup_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_shadows_highlights_prep_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_highest_green_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_core_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_denoise_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_rbf_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_ca_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_chroma_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_sharpen_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_grain_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_core_levels_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_core_color_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_core_creative_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_core_output_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_other_ms", &processing_max_substage_ms);
        require_processing_timing_field(sample, "processing_core_other_ms", &processing_max_substage_ms);
        ASSERT_TRUE(sample.value(QStringLiteral("processing_ms")).toDouble() >= 0.0);
        ASSERT_TRUE(sample.value(QStringLiteral("processing_ms")).toDouble() >= processing_max_substage_ms);
        ASSERT_TRUE(sample.value(QStringLiteral("processed16_total_ms")).toDouble()
                    >= sample.value(QStringLiteral("processing_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("llrawproc_total_ms")).toDouble()
                    >= sample.value(QStringLiteral("llrawproc_dual_iso_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("debayered_frame_ms")).toDouble()
                    >= sample.value(QStringLiteral("raw_uint16_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_ms")).toDouble()
                    >= sample.value(QStringLiteral("raw_uint16_disk_read_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_ms")).toDouble()
                    >= sample.value(QStringLiteral("raw_uint16_decompress_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_ms")).toDouble()
                    >= sample.value(QStringLiteral("raw_uint16_unpack_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("raw_uint16_ms")).toDouble()
                    >= sample.value(QStringLiteral("raw_uint16_copy_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("debayered_frame_ms")).toDouble()
                    >= sample.value(QStringLiteral("llrawproc_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("debayer_exclusive_ms")).toDouble()
                    >= sample.value(QStringLiteral("debayer_kernel_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("processing_core_ms")).toDouble()
                    >= sample.value(QStringLiteral("processing_core_color_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("processing_core_ms")).toDouble()
                    >= sample.value(QStringLiteral("processing_core_levels_ms")).toDouble());
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_total_ms")).toDouble()
                    >= sample.value(QStringLiteral("render_thread_work_ms")).toDouble());
        ASSERT_TRUE(sample.contains(QStringLiteral("playback_processing_subset_active")));
        ASSERT_TRUE(!sample.value(QStringLiteral("playback_processing_subset_active")).toBool());
        ASSERT_TRUE(sample.contains(QStringLiteral("gpu16_preview_active")));
        ASSERT_TRUE(sample.contains(QStringLiteral("gpu_preview_processing_active")));
        ASSERT_TRUE(sample.contains(QStringLiteral("gpu_bilinear_debayer_active")));
    }
    /* PROD-TELEMETRY-DURATION-AS-PROOF-3: the preview rowscale pass ran on
     * every frame of this run -- proved structurally by
     * dual_iso_mode_effective == 2 and dual_iso_preview_runtime_active
     * (asserted above at the metadata level; diso_check is always 0 on this
     * live call site, so dualiso.c's diso_get_preview() always reaches the
     * unconditional rowscale write) -- not by latching a positive elapsed-ms
     * reading across frames, which a fast real pass can miss on a coarse
     * fallback clock. */

    ASSERT_TRUE(QFileInfo::exists(stage_log));
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileHonorsExplicitScaleTwo)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-scale-two.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("1")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1"),
        QList<QPair<QString, QString>>()
            << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"), QStringLiteral("1"))
            << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QStringLiteral("2")));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(1, frames.size());
    const QJsonObject sample = frames.at(0).toObject();
    ASSERT_EQ(2, sample.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
    ASSERT_EQ(2, sample.value(QStringLiteral("render_thread_playback_scale_factor_effective")).toInt());
    ASSERT_FALSE(sample.value(QStringLiteral("render_thread_playback_scale_factor_clamped")).toBool());
    ASSERT_TRUE(sample.value(QStringLiteral("render_thread_rendered_width")).toInt() > 0);
    ASSERT_TRUE(sample.value(QStringLiteral("render_thread_rendered_height")).toInt() > 0);
    ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_upscaling")));
    ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_target_width")));
    ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_target_height")));
    ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_playback_scale_bilinear")));
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileScaleToggleToOneSettles)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-scale-toggle.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--frames") << QStringLiteral("1")
            << QStringLiteral("--exercise-scale-toggle")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1"),
        QList<QPair<QString, QString>>()
            << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QString()));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_scale_toggle_smoke_requested")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_scale_toggle_smoke_ran")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_scale_toggle_smoke_stable")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_scale_toggle_smoke_failure")).toString().isEmpty());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_scale_toggle_inflight_smoke_ran")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_scale_toggle_inflight_smoke_stable")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_scale_toggle_inflight_smoke_failure")).toString().isEmpty());

    const QJsonObject before =
        metadata.value(QStringLiteral("playback_scale_toggle_before_state")).toObject();
    const QJsonObject after =
        metadata.value(QStringLiteral("playback_scale_toggle_after_state")).toObject();
    ASSERT_EQ(2, before.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
    ASSERT_EQ(2, before.value(QStringLiteral("render_thread_playback_scale_factor_effective")).toInt());
    ASSERT_FALSE(before.value(QStringLiteral("render_thread_playback_scale_factor_clamped")).toBool());
    ASSERT_EQ(1, after.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
    ASSERT_EQ(1, after.value(QStringLiteral("render_thread_playback_scale_factor_effective")).toInt());
    ASSERT_EQ(1, after.value(QStringLiteral("last_presented_active_scale")).toInt());
    ASSERT_TRUE(after.value(QStringLiteral("presentation_generation")).toDouble()
                > before.value(QStringLiteral("presentation_generation")).toDouble());
    ASSERT_EQ(after.value(QStringLiteral("presentation_generation")).toDouble(),
              after.value(QStringLiteral("last_presented_generation")).toDouble());
    ASSERT_TRUE(after.value(QStringLiteral("render_thread_rendered_width")).toInt() > 0);
    ASSERT_TRUE(after.value(QStringLiteral("render_thread_rendered_height")).toInt() > 0);

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(1, frames.size());
    const QJsonObject sample = frames.at(0).toObject();
    ASSERT_EQ(1, sample.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
    ASSERT_EQ(1, sample.value(QStringLiteral("render_thread_playback_scale_factor_effective")).toInt());
    ASSERT_EQ(sample.value(QStringLiteral("prep_generation")).toDouble(),
              sample.value(QStringLiteral("prep_active_generation")).toDouble());
}

TEST(ClipGolden, LocalM16ScaleFourToOneWithHistogramSettlesWhenAvailable)
{
    const QString fixture_path = QStringLiteral("C:/temp/MLV/M16-1327.MLV");
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing local regression clip C:/temp/MLV/M16-1327.MLV");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("m16-scale4-toggle.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--frames") << QStringLiteral("1")
            << QStringLiteral("--scope") << QStringLiteral("histogram")
            << QStringLiteral("--exercise-scale-toggle")
            << QStringLiteral("--exercise-scale-toggle-from") << QStringLiteral("4")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1"),
        QList<QPair<QString, QString>>()
            << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QString()));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_EQ(4, metadata.value(QStringLiteral("playback_scale_toggle_from")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_scale_toggle_smoke_stable")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_scale_toggle_inflight_smoke_stable")).toBool());
    const QJsonObject before =
        metadata.value(QStringLiteral("playback_scale_toggle_before_state")).toObject();
    const QJsonObject after =
        metadata.value(QStringLiteral("playback_scale_toggle_after_state")).toObject();
    ASSERT_EQ(4, before.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
    ASSERT_EQ(1, after.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
    ASSERT_EQ(1, after.value(QStringLiteral("last_presented_active_scale")).toInt());
}

TEST(ClipGolden, LocalM16StandardPreviewScaleEightKeepsProcessed8DirectPathActive)
{
    const std::array<QString, 3> fixture_paths = {
        QStringLiteral("C:/temp/MLV/M16-1327.MLV"),
        QStringLiteral("C:/temp/MLV/M16-1347.MLV"),
        QStringLiteral("C:/temp/MLV/M16-1446.MLV"),
    };

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());

    for (const QString & fixture_path : fixture_paths) {
        if (!QFileInfo::exists(fixture_path)) {
            SKIP_TEST((QStringLiteral("Missing local regression clip ") + fixture_path).toStdString());
        }

        const QString output_json =
            temp_dir.filePath(QFileInfo(fixture_path).baseName() + QStringLiteral("-direct8.json"));

        QProcess process;
        configure_playback_profile_process(
            &process,
            app_exe,
            repo_root,
            QStringList()
                << QStringLiteral("--profile-playback")
                << QStringLiteral("--input") << fixture_path
                << QStringLiteral("--frames") << QStringLiteral("1")
                << QStringLiteral("--output") << output_json
                << QStringLiteral("--threads") << QStringLiteral("1"),
            QList<QPair<QString, QString>>()
                << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QStringLiteral("8")));
        process.start();
        ASSERT_TRUE(process.waitForStarted());
        ASSERT_TRUE(process.waitForFinished(-1));
        ASSERT_EQ(0, process.exitCode());

        QFile json_file(output_json);
        ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
        const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
        ASSERT_TRUE(document.isObject());

        const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
        ASSERT_EQ(1, frames.size());
        const QJsonObject sample = frames.at(0).toObject();
        ASSERT_EQ(8, sample.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
        ASSERT_EQ(8, sample.value(QStringLiteral("render_thread_playback_scale_factor_effective")).toInt());
        ASSERT_TRUE(sample.contains(QStringLiteral("processed8_direct_path_active")));
        ASSERT_TRUE(sample.value(QStringLiteral("processed8_direct_path_active")).toBool());
        ASSERT_TRUE(sample.contains(QStringLiteral("processed8_direct_path_reason")));
        ASSERT_EQ(std::string("direct8-active"),
                  sample.value(QStringLiteral("processed8_direct_path_reason")).toString().toStdString());
        ASSERT_TRUE(sample.contains(QStringLiteral("processed8_prefetch_hit")));
        ASSERT_TRUE(sample.value(QStringLiteral("processed8_total_ms")).toDouble() >= 0.0);
    }
}

TEST(ClipGolden, LocalM16LookAssistRejectsExtremeGreenAutoWbWhenAvailable)
{
    const QString fixture_path = QStringLiteral("C:/temp/MLV/M16-1327.MLV");
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing local regression clip C:/temp/MLV/M16-1327.MLV");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("m16-look-assist.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--frames") << QStringLiteral("1")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--exercise-look-assist-settle"),
        QList<QPair<QString, QString>>()
            << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"), QStringLiteral("1"))
            << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QString()));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_settle_smoke_ran")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_settle_smoke_stable")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_diagnostics_valid")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_safety_fallback")).toBool());
    const std::string color_cast_warning =
        metadata.value(QStringLiteral("look_assist_color_cast_warning")).toString().toStdString();
    ASSERT_TRUE(color_cast_warning == std::string("processed-floor-lifted-awb-risk")
             || color_cast_warning == std::string("rejected-unstable-floor-lifted"));
    ASSERT_FALSE(metadata.value(QStringLiteral("look_assist_auto_wb_valid")).toBool());
    ASSERT_EQ(std::string("rejected-safety-fallback"),
              metadata.value(QStringLiteral("look_assist_auto_wb_source")).toString().toStdString());
    ASSERT_EQ(std::string("rejected-safety-fallback"),
              metadata.value(QStringLiteral("look_assist_auto_wb_decision")).toString().toStdString());
    ASSERT_EQ(6000, metadata.value(QStringLiteral("look_assist_original_raw_white")).toInt());
    ASSERT_EQ(6000, metadata.value(QStringLiteral("look_assist_auto_white_candidate")).toInt());
    ASSERT_EQ(6000, metadata.value(QStringLiteral("look_assist_raw_white")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_restricted_lossless_output_white")).toInt() > 15000);
    ASSERT_EQ(0, metadata.value(QStringLiteral("look_assist_exposure")).toInt());
    ASSERT_EQ(6000, metadata.value(QStringLiteral("look_assist_temperature")).toInt());
    ASSERT_EQ(0, metadata.value(QStringLiteral("look_assist_tint")).toInt());
    ASSERT_EQ(0, metadata.value(QStringLiteral("look_assist_preset_exposure")).toInt());
    ASSERT_EQ(1, metadata.value(QStringLiteral("look_assist_chroma_smooth")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_chroma_smooth_auto_applied")).toBool());
}

TEST(ClipGolden, LocalM16LookAssistRejectsBrightNeutralGreenClampWhenAvailable)
{
    const QString fixture_path = QStringLiteral("C:/temp/MLV/M16-1347.MLV");
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing local regression clip C:/temp/MLV/M16-1347.MLV");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("m16-1347-look-assist.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--frames") << QStringLiteral("1")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--exercise-look-assist-settle"),
        QList<QPair<QString, QString>>()
            << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"), QStringLiteral("1"))
            << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QString()));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_settle_smoke_ran")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_settle_smoke_stable")).toBool());
    ASSERT_FALSE(metadata.value(QStringLiteral("look_assist_safety_fallback")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_auto_wb_valid")).toBool());
    ASSERT_EQ(std::string("processed-neutral-patch"),
              metadata.value(QStringLiteral("look_assist_auto_wb_source")).toString().toStdString());
    ASSERT_EQ(6000, metadata.value(QStringLiteral("look_assist_raw_white")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_restricted_lossless_output_white")).toInt() > 15000);
    ASSERT_EQ(1, metadata.value(QStringLiteral("look_assist_chroma_smooth")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_chroma_smooth_auto_applied")).toBool());
    ASSERT_EQ(std::string("none"),
              metadata.value(QStringLiteral("look_assist_color_cast_warning")).toString().toStdString());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_tint")).toInt() > -20);
    const double look_assist_scale =
        std::pow(2.0, metadata.value(QStringLiteral("look_assist_preset_exposure")).toInt() / 100.0);
    const double projected_p95 =
        metadata.value(QStringLiteral("look_assist_p95")).toDouble() * look_assist_scale;
    const double projected_p99 =
        metadata.value(QStringLiteral("look_assist_p99")).toDouble() * look_assist_scale;
    ASSERT_TRUE(projected_p95 >= 95.0);
    ASSERT_TRUE(projected_p95 <= 126.0);
    ASSERT_TRUE(projected_p99 <= 152.0);
}

TEST(ClipGolden, LocalM16LookAssistCapsOnlyFlatNoiseFloorNightWhenAvailable)
{
    const QString flat_fixture_path = QStringLiteral("C:/temp/MLV/M16-1446.MLV");
    const QString control_fixture_path = QStringLiteral("C:/temp/MLV/M16-1243.MLV");
    if (!QFileInfo::exists(flat_fixture_path) || !QFileInfo::exists(control_fixture_path)) {
        SKIP_TEST("Missing local regression clips C:/temp/MLV/M16-1446.MLV or C:/temp/MLV/M16-1243.MLV");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const auto run_profile = [&](const QString &fixture_path, const QString &name) -> QJsonObject {
        const QString output_json = temp_dir.filePath(name + QStringLiteral(".json"));
        QProcess process;
        configure_playback_profile_process(
            &process,
            app_exe,
            repo_root,
            QStringList()
                << QStringLiteral("--profile-playback")
                << QStringLiteral("--input") << fixture_path
                << QStringLiteral("--frames") << QStringLiteral("3")
                << QStringLiteral("--output") << output_json
                << QStringLiteral("--threads") << QStringLiteral("1")
                << QStringLiteral("--exercise-look-assist-settle"),
            QList<QPair<QString, QString>>()
                << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"), QStringLiteral("1"))
                << qMakePair(QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QString()));
        process.start();
        ASSERT_TRUE(process.waitForStarted());
        ASSERT_TRUE(process.waitForFinished(-1));
        ASSERT_EQ(0, process.exitCode());

        QFile json_file(output_json);
        ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
        const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
        ASSERT_TRUE(document.isObject());
        return document.object().value(QStringLiteral("metadata")).toObject();
    };

    const QJsonObject flat_metadata =
        run_profile(flat_fixture_path, QStringLiteral("m16-1446-look-assist"));
    ASSERT_TRUE(flat_metadata.value(QStringLiteral("look_assist_diagnostics_valid")).toBool());
    ASSERT_FALSE(flat_metadata.value(QStringLiteral("look_assist_safety_fallback")).toBool());
    ASSERT_EQ(std::string("night"),
              flat_metadata.value(QStringLiteral("look_assist_scene")).toString().toStdString());
    ASSERT_TRUE(flat_metadata.value(QStringLiteral("look_assist_p99")).toDouble() <= 34.0);
    ASSERT_TRUE(flat_metadata.value(QStringLiteral("look_assist_exposure")).toInt() >= 150);
    ASSERT_TRUE(flat_metadata.value(QStringLiteral("look_assist_raw_white")).toInt() > 15000);

    const QJsonObject control_metadata =
        run_profile(control_fixture_path, QStringLiteral("m16-1243-look-assist"));
    ASSERT_TRUE(control_metadata.value(QStringLiteral("look_assist_diagnostics_valid")).toBool());
    ASSERT_TRUE(control_metadata.value(QStringLiteral("look_assist_safety_fallback")).toBool());
    ASSERT_EQ(std::string("processed-floor-lifted-post-invalid"),
              control_metadata.value(QStringLiteral("look_assist_color_cast_warning")).toString().toStdString());
    ASSERT_EQ(std::string("rejected-safety-fallback"),
              control_metadata.value(QStringLiteral("look_assist_auto_wb_source")).toString().toStdString());
    ASSERT_EQ(std::string("rejected-safety-fallback"),
              control_metadata.value(QStringLiteral("look_assist_auto_wb_decision")).toString().toStdString());
    ASSERT_TRUE(control_metadata.value(QStringLiteral("look_assist_p99")).toDouble() > 34.0);
    ASSERT_EQ(0, control_metadata.value(QStringLiteral("look_assist_exposure")).toInt());
    ASSERT_EQ(6000, control_metadata.value(QStringLiteral("look_assist_raw_white")).toInt());
    ASSERT_EQ(1, control_metadata.value(QStringLiteral("look_assist_chroma_smooth")).toInt());
    ASSERT_TRUE(control_metadata.value(QStringLiteral("look_assist_chroma_smooth_auto_applied")).toBool());
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfilePreviewReceiptStaysPreviewAtRuntime)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_preview_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_preview.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-preview.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_EQ(2, metadata.value(QStringLiteral("dual_iso_mode_selected")).toInt());
    ASSERT_EQ(2, metadata.value(QStringLiteral("dual_iso_mode_effective")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("dual_iso_preview_runtime_active")).toBool());
    ASSERT_TRUE(!metadata.value(QStringLiteral("dual_iso_preview_override_active")).toBool());

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(2, frames.size());
    for (const QJsonValue & value : frames) {
        const QJsonObject sample = value.toObject();
        ASSERT_TRUE(sample.contains(QStringLiteral("processed8_direct_path_active")));
        ASSERT_TRUE(sample.value(QStringLiteral("processed8_direct_path_active")).toBool());
    }
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileRestoresLookAssistBaselineAtRuntime)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString receipt_path = temp_dir.filePath(QStringLiteral("lookassist-runtime.marxml"));
    ASSERT_TRUE(write_look_assist_receipt_fixture(receipt_path));

    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-lookassist.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--exercise-look-assist-toggle"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_enabled")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_diagnostics_valid")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_analysis_source")).toString()
                == QStringLiteral("raw_debayered_downscale"));
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_toggle_smoke_requested")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_toggle_smoke_ran")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_toggle_smoke_stable")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_toggle_smoke_failure")).toString().isEmpty());
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_frame_settle_policy")).toString()
                .contains(QStringLiteral("exact presented frame match")));
    ASSERT_EQ(0, metadata.value(QStringLiteral("look_assist_unsettled_analysis_count")).toInt());
    const QJsonObject look_assist_load_state =
        metadata.value(QStringLiteral("look_assist_load_state")).toObject();
    const QJsonObject look_assist_toggle_state =
        metadata.value(QStringLiteral("look_assist_toggle_state")).toObject();
    ASSERT_TRUE(look_assist_load_state.value(QStringLiteral("last_presented_request_serial")).toDouble() > 0.0);
    ASSERT_TRUE(look_assist_toggle_state.value(QStringLiteral("last_presented_request_serial")).toDouble()
                >= look_assist_load_state.value(QStringLiteral("last_presented_request_serial")).toDouble());
    ASSERT_EQ(look_assist_load_state.value(QStringLiteral("frame")).toInt(),
              look_assist_load_state.value(QStringLiteral("last_presented_frame")).toInt());
    ASSERT_EQ(look_assist_toggle_state.value(QStringLiteral("frame")).toInt(),
              look_assist_toggle_state.value(QStringLiteral("last_presented_frame")).toInt());
    ASSERT_NE(3, metadata.value(QStringLiteral("look_assist_exposure")).toInt());
    ASSERT_NE(7, metadata.value(QStringLiteral("look_assist_contrast")).toInt());
    ASSERT_NE(91, metadata.value(QStringLiteral("look_assist_pivot")).toInt());
    ASSERT_NE(6400, metadata.value(QStringLiteral("look_assist_temperature")).toInt());
    ASSERT_NE(9, metadata.value(QStringLiteral("look_assist_tint")).toInt());
    ASSERT_NE(15, metadata.value(QStringLiteral("look_assist_vibrance")).toInt());
    ASSERT_NE(22, metadata.value(QStringLiteral("look_assist_shadows")).toInt());
    ASSERT_NE(-6, metadata.value(QStringLiteral("look_assist_highlights")).toInt());
    ASSERT_NE(20470, metadata.value(QStringLiteral("look_assist_raw_black")).toInt());
    ASSERT_NE(2840, metadata.value(QStringLiteral("look_assist_raw_white")).toInt());
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_original_raw_black")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_original_raw_white")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_white_candidate")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_restricted_lossless_output_white")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_chroma_smooth")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_chroma_smooth_auto_applied")));
    ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_scene")).toString().size() > 0);
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_preset_exposure")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_preset_contrast")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_preset_pivot")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_preset_shadows")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_preset_highlights")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_preset_vibrance")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_temperature_delta")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_tint_delta")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_valid")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_source")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_decision")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_damping")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_temperature")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_tint")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_candidate_temperature")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_candidate_tint")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_raw_x")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_raw_y")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_patch_luma")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_auto_wb_patch_chroma")));
    const bool auto_wb_valid = metadata.value(QStringLiteral("look_assist_auto_wb_valid")).toBool();
    if (auto_wb_valid) {
        ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_auto_wb_tint")).toInt() >= -35);
        ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_auto_wb_tint")).toInt() <= 18);
        ASSERT_EQ(metadata.value(QStringLiteral("look_assist_auto_wb_tint")).toInt(),
                  metadata.value(QStringLiteral("look_assist_tint")).toInt());
        ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_auto_wb_patch_luma")).toDouble() >= 70.0);
        ASSERT_TRUE(metadata.value(QStringLiteral("look_assist_auto_wb_patch_chroma")).toDouble() <= 40.0);
    } else {
        ASSERT_TRUE(std::abs(metadata.value(QStringLiteral("look_assist_tint_delta")).toInt()) <= 22);
    }
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_balance_source")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_balance_green_axis")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_balance_blue_amber_axis")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_balance_valid")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_balance_r")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_balance_g")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_balance_b")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_balance_samples")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_balance_green_axis")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_balance_blue_amber_axis")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_green_artifact_ratio")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_green_artifact_mean_axis")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_visible_green_axis")));
    const double post_green_artifact_ratio =
        metadata.value(QStringLiteral("look_assist_post_green_artifact_ratio")).toDouble(-1.0);
    const double post_green_artifact_axis =
        metadata.value(QStringLiteral("look_assist_post_green_artifact_mean_axis")).toDouble(-1.0);
    const double post_visible_green_axis =
        metadata.value(QStringLiteral("look_assist_post_visible_green_axis")).toDouble();
    ASSERT_TRUE(post_green_artifact_ratio >= 0.0);
    ASSERT_TRUE(post_green_artifact_ratio <= 1.0);
    ASSERT_TRUE(post_green_artifact_axis >= 0.0);
    ASSERT_TRUE(std::isfinite(post_visible_green_axis));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_temperature_delta")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_post_tint_delta")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("look_assist_color_cast_warning")));
    ASSERT_TRUE(std::abs(metadata.value(QStringLiteral("look_assist_post_tint_delta")).toInt()) <= 22);
    const double look_assist_scale =
        std::pow(2.0, metadata.value(QStringLiteral("look_assist_preset_exposure")).toInt() / 100.0);
    const double projected_p95 = metadata.value(QStringLiteral("look_assist_p95")).toDouble() * look_assist_scale;
    const double projected_p99 = metadata.value(QStringLiteral("look_assist_p99")).toDouble() * look_assist_scale;
    ASSERT_TRUE(projected_p95 >= 95.0);
    ASSERT_TRUE(projected_p95 <= 122.0);
    ASSERT_TRUE(projected_p99 <= 152.0);
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfilePlayActionAdvancesFrame)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-play-action.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("1")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--exercise-play-action"),
        QList<QPair<QString, QString>>()
            << qMakePair(QStringLiteral("MLVAPP_INTERACTIVE_TRACE"), QStringLiteral("1")));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_requested")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_started")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_frame_advanced")).toBool());
    ASSERT_TRUE(!metadata.value(QStringLiteral("play_action_smoke_timed_out")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_frame_ready_count")).toInt() > 0);
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_final_frame")).toInt()
                > metadata.value(QStringLiteral("play_action_smoke_initial_frame")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_failure")).toString().isEmpty());
    ASSERT_TRUE(metadata.value(QStringLiteral("diagnostic_log_file")).toString().size() > 0);

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(1, frames.size());
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfilePlayActionRepairsCollapsedCutOut)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString receipt_path = temp_dir.filePath(QStringLiteral("collapsed-cutout.marxml"));
    ASSERT_TRUE(write_collapsed_cutout_receipt_fixture(receipt_path));

    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-collapsed-cutout.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("1")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--exercise-play-action"),
        QList<QPair<QString, QString>>()
            << qMakePair(QStringLiteral("MLVAPP_INTERACTIVE_TRACE"), QStringLiteral("1")));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_requested")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_started")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_frame_advanced")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_failure")).toString().isEmpty());
    ASSERT_EQ(1, metadata.value(QStringLiteral("play_action_smoke_cut_in_after")).toInt());
    ASSERT_EQ(metadata.value(QStringLiteral("total_frames")).toInt(),
              metadata.value(QStringLiteral("play_action_smoke_cut_out_after")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_action_smoke_final_frame")).toInt()
                > metadata.value(QStringLiteral("play_action_smoke_initial_frame")).toInt());

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(1, frames.size());
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileCpuBackendProducesJson)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-cpu.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--gpu-preview-processing") << QStringLiteral("cpu"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("gpu_preview_processing_backend_request")).toString()
                == QStringLiteral("cpu"));

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(2, frames.size());
    for (const QJsonValue & value : frames) {
        const QJsonObject sample = value.toObject();
        ASSERT_TRUE(sample.contains(QStringLiteral("gpu_preview_processing_active")));
        ASSERT_TRUE(!sample.value(QStringLiteral("gpu_preview_processing_active")).toBool());
    }
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileGpuBilinearDebayerCpuBackendProducesJson)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-debayer-cpu.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--gpu-viewport")
            << QStringLiteral("--gpu-preview-processing") << QStringLiteral("gpu")
            << QStringLiteral("--gpu-bilinear-debayer") << QStringLiteral("cpu"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("gpu_preview_processing_backend_request")).toString()
                == QStringLiteral("gpu"));
    ASSERT_TRUE(metadata.value(QStringLiteral("gpu_bilinear_debayer_backend_request")).toString()
                == QStringLiteral("cpu"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("gpu_bilinear_debayer_environment_requested")).toBool());

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(2, frames.size());
    for (const QJsonValue & value : frames) {
        const QJsonObject sample = value.toObject();
        ASSERT_TRUE(sample.contains(QStringLiteral("gpu_bilinear_debayer_active")));
        ASSERT_TRUE(!sample.value(QStringLiteral("gpu_bilinear_debayer_active")).toBool());
        ASSERT_TRUE(sample.contains(QStringLiteral("gpu_amaze_debayer_active")));
        ASSERT_TRUE(!sample.value(QStringLiteral("gpu_amaze_debayer_active")).toBool());
        require_no_gpu_amaze_backend_timing_fields(sample);
    }
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileGpuBilinearDebayerGpuBackendProducesJson)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-debayer-gpu.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--gpu-viewport")
            << QStringLiteral("--gpu-preview-processing") << QStringLiteral("gpu")
            << QStringLiteral("--gpu-bilinear-debayer") << QStringLiteral("gpu"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("gpu_preview_processing_backend_request")).toString()
                == QStringLiteral("gpu"));
    ASSERT_TRUE(metadata.value(QStringLiteral("gpu_bilinear_debayer_backend_request")).toString()
                == QStringLiteral("gpu"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("gpu_bilinear_debayer_environment_requested")).toBool());
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_bilinear_debayer_probe_available")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_bilinear_debayer_probe_reason")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_bilinear_debayer_probe_renderer")));
    ASSERT_TRUE(metadata.value(QStringLiteral("gpu_amaze_debayer_backend_request")).toString()
                == QStringLiteral("auto"));
    ASSERT_TRUE(!metadata.value(QStringLiteral("gpu_amaze_debayer_environment_requested")).toBool());
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_amaze_debayer_probe_available")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_amaze_debayer_probe_reason")));
    ASSERT_TRUE(metadata.contains(QStringLiteral("gpu_amaze_debayer_probe_renderer")));
    ASSERT_TRUE(metadata.value(QStringLiteral("qt_opengl_environment")).toString()
                == QStringLiteral("desktop"));
    ASSERT_TRUE(metadata.value(QStringLiteral("qt_qpa_platform_environment")).toString()
                == QStringLiteral("windows"));

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(2, frames.size());
    for (const QJsonValue & value : frames) {
        const QJsonObject sample = value.toObject();
        ASSERT_TRUE(sample.contains(QStringLiteral("gpu_bilinear_debayer_active")));
        ASSERT_TRUE(sample.contains(QStringLiteral("gpu_amaze_debayer_active")));
        ASSERT_TRUE(!sample.value(QStringLiteral("gpu_amaze_debayer_active")).toBool());
        require_no_gpu_amaze_backend_timing_fields(sample);
        if (sample.contains(QStringLiteral("gpu_bilinear_debayer_fallback_reason"))) {
            ASSERT_TRUE(sample.contains(QStringLiteral("gpu_bilinear_debayer_renderer")));
        }
    }
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileAmazeCachedDebayerProducesJson)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-amaze-cached.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--raw-cache-mb") << QStringLiteral("128")
            << QStringLiteral("--cache-cpu-cores") << QStringLiteral("1")
            << QStringLiteral("--playback-debayer") << QStringLiteral("amaze-cached"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_policy_active")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_debayer_request")).toString()
                == QStringLiteral("amaze-cached"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_debayer_effective")).toString()
                == QStringLiteral("amaze-cached"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_debayer_uses_caching")).toBool());
    ASSERT_EQ(1, metadata.value(QStringLiteral("playback_debayer_engine_mode")).toInt());
    ASSERT_EQ(128, metadata.value(QStringLiteral("raw_cache_mb")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_start_preroll_active")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_start_preroll_eligible")).toBool());
    ASSERT_TRUE(!metadata.value(QStringLiteral("play_start_preroll_disabled_by_environment")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_to_first_frame_measured")).toBool());
    /* PROD-TELEMETRY-DURATION-AS-PROOF-3: "ran" is proved by the measured
     * flag above, not by this duration reading positive. */
    ASSERT_TRUE(metadata.value(QStringLiteral("play_to_first_frame_ms")).toDouble() >= 0.0);
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileAmazeCachedCanDisablePlayStartPrerollViaEnvironment)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-amaze-cached-preroll-disabled.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--raw-cache-mb") << QStringLiteral("128")
            << QStringLiteral("--cache-cpu-cores") << QStringLiteral("1")
            << QStringLiteral("--playback-debayer") << QStringLiteral("amaze-cached"),
        {{QStringLiteral("MLVAPP_DISABLE_PLAY_START_PREROLL"), QStringLiteral("1")}});
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_policy_active")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_debayer_effective")).toString()
                == QStringLiteral("amaze-cached"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_debayer_uses_caching")).toBool());
    ASSERT_TRUE(!metadata.value(QStringLiteral("play_start_preroll_active")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_start_preroll_eligible")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_start_preroll_disabled_by_environment")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("play_to_first_frame_measured")).toBool());
    /* PROD-TELEMETRY-DURATION-AS-PROOF-3: "ran" is proved by the measured
     * flag above, not by this duration reading positive. */
    ASSERT_TRUE(metadata.value(QStringLiteral("play_to_first_frame_ms")).toDouble() >= 0.0);
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileCanEnableLj92Pred6SplitViaEnvironment)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-lj92-pred6-split.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1"),
        {{QStringLiteral("MLVAPP_PROFILE_LJ92_PRED6_SPLIT"), QStringLiteral("1")}});
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_TRUE(!frames.isEmpty());
    const QJsonObject first_frame = first_frame_with_raw_decode_telemetry(frames);
    ASSERT_TRUE(!first_frame.isEmpty());
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_split_requested")).toBool());
    const int predictor = first_frame.value(QStringLiteral("raw_uint16_lj92_predictor")).toInt(-1);
    ASSERT_TRUE(predictor >= 0);
    ASSERT_TRUE(predictor <= 7);
    if (predictor == 6) {
        ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_split_active")).toBool());
    } else {
        ASSERT_TRUE(!first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_split_active")).toBool());
    }
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_total_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_bitstream_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_predictor_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred6_other_ms")).toDouble() >= 0.0);
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileCanEnableLj92GenericSplitViaEnvironment)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-lj92-generic-split.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1"),
        {{QStringLiteral("MLVAPP_PROFILE_LJ92_GENERIC_SPLIT"), QStringLiteral("1")}});
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_TRUE(!frames.isEmpty());
    const QJsonObject first_frame = first_frame_with_raw_decode_telemetry(frames);
    ASSERT_TRUE(!first_frame.isEmpty());
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_split_requested")).toBool());
    const int predictor = first_frame.value(QStringLiteral("raw_uint16_lj92_predictor")).toInt(-1);
    ASSERT_TRUE(predictor >= 0);
    ASSERT_TRUE(predictor <= 7);
    if (predictor == 6) {
        ASSERT_TRUE(!first_frame.value(QStringLiteral("raw_uint16_lj92_generic_split_active")).toBool());
    } else {
        ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_split_active")).toBool());
    }
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_total_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_bitstream_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_predictor_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_generic_other_ms")).toDouble() >= 0.0);
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileReceiptProcessingProducesJson)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-processing-receipt.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--playback-processing") << QStringLiteral("receipt"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_request")).toString()
                == QStringLiteral("receipt"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_selected")).toString()
                == QStringLiteral("receipt"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_effective")).toString()
                == QStringLiteral("receipt"));

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(2, frames.size());
    for (const QJsonValue & value : frames) {
        const QJsonObject sample = value.toObject();
        ASSERT_TRUE(sample.contains(QStringLiteral("playback_processing_subset_active")));
        ASSERT_TRUE(!sample.value(QStringLiteral("playback_processing_subset_active")).toBool());
    }
}

TEST(ClipGolden, LargeDualIsoHqScaleFourSuppressesRawUint16Prefetch)
{
    const QString fixture_path = large_dual_iso_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/large_dual_iso.mlv");
    }

    const QString receipt_path = large_dual_iso_hq_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/large_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("large-dual-iso-hq-scale4-prefetch-gate.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("6")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("4")
            << QStringLiteral("--playback-debayer") << QStringLiteral("receipt")
            << QStringLiteral("--playback-processing") << QStringLiteral("receipt"),
        {{QStringLiteral("MLVAPP_RAW_UINT16_PREFETCH"), QStringLiteral("1")},
         {QStringLiteral("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"), QStringLiteral("1")},
         {QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QStringLiteral("4")}});
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_EQ(1, metadata.value(QStringLiteral("dual_iso_mode_effective")).toInt());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_debayer_effective")).toString()
                == QStringLiteral("receipt"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_effective")).toString()
                == QStringLiteral("receipt"));

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_TRUE(frames.size() >= 2);
    ASSERT_EQ(0, count_raw_uint16_prefetch_hits(frames));

    bool saw_full20_frame = false;
    for (const QJsonValue & value : frames) {
        ASSERT_TRUE(value.isObject());
        const QJsonObject sample = value.toObject();
        ASSERT_EQ(4, sample.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
        ASSERT_EQ(4, sample.value(QStringLiteral("render_thread_playback_scale_factor_effective")).toInt());
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_preview_mode")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_aggressive_preview")));
        ASSERT_TRUE(!sample.value(QStringLiteral("render_thread_aggressive_preview")).toBool());
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_phase4b_fallback_reason")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_stage_raw_decode_pixels")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_stage_llrawproc_pixels")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_stage_rgb_output_pixels")));
        ASSERT_TRUE(sample.contains(QStringLiteral("render_thread_stage_processing_pixels")));
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_stage_raw_decode_domain")).toString()
                    == QStringLiteral("raw_bayer"));
        const double raw_decode_pixels =
            sample.value(QStringLiteral("render_thread_stage_raw_decode_pixels")).toDouble();
        const double llrawproc_pixels =
            sample.value(QStringLiteral("render_thread_stage_llrawproc_pixels")).toDouble();
        const double rgb_output_pixels =
            sample.value(QStringLiteral("render_thread_stage_rgb_output_pixels")).toDouble();
        const double processing_pixels =
            sample.value(QStringLiteral("render_thread_stage_processing_pixels")).toDouble();
        ASSERT_TRUE(raw_decode_pixels > 0.0);
        ASSERT_TRUE(llrawproc_pixels > 0.0);
        ASSERT_TRUE(rgb_output_pixels > 0.0);
        ASSERT_EQ(rgb_output_pixels, processing_pixels);
        if (sample.value(QStringLiteral("render_thread_phase4b_path")).toInt() == 0) {
            ASSERT_EQ(raw_decode_pixels, llrawproc_pixels);
            ASSERT_TRUE(!sample.value(QStringLiteral("render_thread_phase4b_fallback_reason"))
                         .toString().isEmpty());
        } else {
            ASSERT_TRUE(llrawproc_pixels < raw_decode_pixels);
        }
        saw_full20_frame = saw_full20_frame
            || sample.value(QStringLiteral("dual_iso_full20_valid")).toBool();
    }
    ASSERT_TRUE(saw_full20_frame);
}

TEST(ClipGolden, LargeDualIsoAggressiveX8SuppressesRawUint16PrefetchWhenReceiptNeedsFullResCoordinates)
{
    const QString fixture_path = large_dual_iso_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/large_dual_iso.mlv");
    }

    const QString receipt_path = large_dual_iso_hq_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/large_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("large-dual-iso-aggressive-x8-incompatible-prefetch.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("10")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("4")
            << QStringLiteral("--playback-debayer") << QStringLiteral("receipt")
            << QStringLiteral("--playback-processing") << QStringLiteral("receipt"),
        {{QStringLiteral("MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW"), QStringLiteral("1")},
         {QStringLiteral("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"), QStringLiteral("1")},
         {QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QStringLiteral("8")}});
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_aggressive_preview")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_preview_mode")).toString()
                == QStringLiteral("aggressive_performance"));

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_TRUE(frames.size() >= 4);
    ASSERT_EQ(0, count_raw_uint16_prefetch_hits(frames));

    for (const QJsonValue & value : frames) {
        ASSERT_TRUE(value.isObject());
        const QJsonObject sample = value.toObject();
        ASSERT_EQ(8, sample.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
        ASSERT_EQ(8, sample.value(QStringLiteral("render_thread_playback_scale_factor_effective")).toInt());
        ASSERT_EQ(0, sample.value(QStringLiteral("render_thread_phase4b_path")).toInt());
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_phase4b_fallback_reason")).toString()
                    == QStringLiteral("focus_pixels enabled"));
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_aggressive_preview")).toBool());
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_preview_mode")).toString()
                    == QStringLiteral("aggressive_performance"));
        const double raw_decode_pixels =
            sample.value(QStringLiteral("render_thread_stage_raw_decode_pixels")).toDouble();
        const double llrawproc_pixels =
            sample.value(QStringLiteral("render_thread_stage_llrawproc_pixels")).toDouble();
        ASSERT_TRUE(raw_decode_pixels > 0.0);
        ASSERT_TRUE(llrawproc_pixels > 0.0);
        ASSERT_EQ(raw_decode_pixels, llrawproc_pixels);
    }
}

TEST(ClipGolden, LargeDualIsoAggressiveX4SuppressesRawUint16PrefetchWhenReceiptNeedsFullResCoordinates)
{
    const QString fixture_path = large_dual_iso_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/large_dual_iso.mlv");
    }

    const QString receipt_path = large_dual_iso_hq_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/large_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("large-dual-iso-aggressive-x4-incompatible-prefetch.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("10")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("4")
            << QStringLiteral("--playback-debayer") << QStringLiteral("receipt")
            << QStringLiteral("--playback-processing") << QStringLiteral("receipt"),
        {{QStringLiteral("MLVAPP_RAW_UINT16_PREFETCH"), QStringLiteral("1")},
         {QStringLiteral("MLVAPP_PLAYBACK_AGGRESSIVE_PREVIEW"), QStringLiteral("1")},
         {QStringLiteral("MLVAPP_PLAYBACK_PREFER_HQ_MEAN23"), QStringLiteral("1")},
         {QStringLiteral("MLVAPP_PLAYBACK_SCALE_FACTOR"), QStringLiteral("4")}});
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_aggressive_preview")).toBool());
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_preview_mode")).toString()
                == QStringLiteral("aggressive_performance"));

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_TRUE(frames.size() >= 4);
    ASSERT_EQ(0, count_raw_uint16_prefetch_hits(frames));

    for (const QJsonValue & value : frames) {
        ASSERT_TRUE(value.isObject());
        const QJsonObject sample = value.toObject();
        ASSERT_EQ(4, sample.value(QStringLiteral("render_thread_playback_scale_factor_request")).toInt());
        ASSERT_EQ(4, sample.value(QStringLiteral("render_thread_playback_scale_factor_effective")).toInt());
        ASSERT_EQ(0, sample.value(QStringLiteral("render_thread_phase4b_path")).toInt());
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_phase4b_fallback_reason")).toString()
                    == QStringLiteral("focus_pixels enabled"));
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_aggressive_preview")).toBool());
        ASSERT_TRUE(sample.value(QStringLiteral("render_thread_preview_mode")).toString()
                    == QStringLiteral("aggressive_performance"));
        const double raw_decode_pixels =
            sample.value(QStringLiteral("render_thread_stage_raw_decode_pixels")).toDouble();
        const double llrawproc_pixels =
            sample.value(QStringLiteral("render_thread_stage_llrawproc_pixels")).toDouble();
        ASSERT_TRUE(raw_decode_pixels > 0.0);
        ASSERT_TRUE(llrawproc_pixels > 0.0);
        ASSERT_EQ(raw_decode_pixels, llrawproc_pixels);
    }
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfilePred1FastPathMeasurementProducesJson)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_preview_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing receipt tests/fixtures/receipts/tiny_dual_iso_preview.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-pred1-fast-path-measurement.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1"),
        {{QStringLiteral("MLVAPP_PRED1_FASTPATH_MEASUREMENT"), QStringLiteral("1")}});
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_TRUE(!frames.isEmpty());
    const QJsonObject first_frame = first_frame_with_raw_decode_telemetry(frames);
    ASSERT_TRUE(!first_frame.isEmpty());
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_measurement_requested")).toBool());
    ASSERT_TRUE(!first_frame.value(QStringLiteral("raw_uint16_lj92_generic_split_requested")).toBool());
    const int predictor = first_frame.value(QStringLiteral("raw_uint16_lj92_predictor")).toInt(-1);
    ASSERT_TRUE(predictor >= 0);
    ASSERT_TRUE(predictor <= 7);
    const bool fast_path_active =
        first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_active")).toBool();
    const bool measurement_active =
        first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_measurement_active")).toBool();
    ASSERT_EQ(1, predictor);
    ASSERT_TRUE(fast_path_active);
    ASSERT_TRUE(measurement_active);
    ASSERT_TRUE(measurement_active == fast_path_active);
    /* PROD-TELEMETRY-DURATION-AS-PROOF-3: "ran" is proved by fast_path_active
     * and measurement_active above, not by this duration reading positive. */
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_total_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_bitstream_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_predictor_ms")).toDouble() >= 0.0);
    ASSERT_TRUE(first_frame.value(QStringLiteral("raw_uint16_lj92_pred1_fast_path_other_ms")).toDouble() >= 0.0);
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileSubsetProcessingProducesJson)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString receipt_path = clip_receipt_path();
    if (!QFileInfo::exists(receipt_path)) {
        SKIP_TEST("Missing fixture receipt tests/fixtures/receipts/tiny_dual_iso_hq.marxml");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-processing-subset.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--receipt") << receipt_path
            << QStringLiteral("--frames") << QStringLiteral("2")
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--threads") << QStringLiteral("1")
            << QStringLiteral("--playback-processing") << QStringLiteral("subset"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(0, process.exitCode());

    QFile json_file(output_json);
    ASSERT_TRUE(json_file.open(QIODevice::ReadOnly | QIODevice::Text));
    const QJsonDocument document = QJsonDocument::fromJson(json_file.readAll());
    ASSERT_TRUE(document.isObject());

    const QJsonObject metadata = document.object().value(QStringLiteral("metadata")).toObject();
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_request")).toString()
                == QStringLiteral("subset"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_selected")).toString()
                == QStringLiteral("subset"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_effective")).toString()
                == QStringLiteral("subset"));
    ASSERT_TRUE(metadata.value(QStringLiteral("playback_processing_supported")).toBool());

    const QJsonArray frames = document.object().value(QStringLiteral("frames")).toArray();
    ASSERT_EQ(2, frames.size());
    for (const QJsonValue & value : frames) {
        const QJsonObject sample = value.toObject();
        ASSERT_TRUE(sample.contains(QStringLiteral("playback_processing_subset_active")));
        ASSERT_TRUE(sample.value(QStringLiteral("playback_processing_subset_active")).toBool());
    }
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileRejectsInvalidGpuPreviewProcessingBackend)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-invalid.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--gpu-preview-processing") << QStringLiteral("bogus"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(2, process.exitCode());

    const QString stderr_output = QString::fromLocal8Bit(process.readAllStandardError());
    ASSERT_TRUE(stderr_output.contains(
        QStringLiteral("--gpu-preview-processing must be one of auto, cpu, gpu")));
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileRejectsInvalidGpuBilinearDebayerBackend)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-invalid-debayer.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--gpu-bilinear-debayer") << QStringLiteral("bogus"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(2, process.exitCode());

    const QString stderr_output = QString::fromLocal8Bit(process.readAllStandardError());
    ASSERT_TRUE(stderr_output.contains(
        QStringLiteral("--gpu-bilinear-debayer must be one of auto, cpu, gpu")));
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileRejectsInvalidGpuAmazeDebayerBackend)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-invalid-amaze-debayer.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--gpu-amaze-debayer") << QStringLiteral("bogus"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(2, process.exitCode());

    const QString stderr_output = QString::fromLocal8Bit(process.readAllStandardError());
    ASSERT_TRUE(stderr_output.contains(
        QStringLiteral("--gpu-amaze-debayer must be one of auto, cpu, gpu")));
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileRejectsGpuAmazeWithCpuPreviewProcessing)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-amaze-cpu-processing.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--gpu-preview-processing") << QStringLiteral("cpu")
            << QStringLiteral("--gpu-amaze-debayer") << QStringLiteral("gpu"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(2, process.exitCode());

    const QString stderr_output = QString::fromLocal8Bit(process.readAllStandardError());
    ASSERT_TRUE(stderr_output.contains(
        QStringLiteral("--gpu-amaze-debayer gpu requires --gpu-preview-processing gpu or auto")));
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileRejectsInvalidPlaybackDebayer)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-invalid-playback-debayer.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--playback-debayer") << QStringLiteral("bogus"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(2, process.exitCode());

    const QString stderr_output = QString::fromLocal8Bit(process.readAllStandardError());
    ASSERT_TRUE(stderr_output.contains(
        QStringLiteral("--playback-debayer must be one of auto, receipt, none, simple, bilinear, lmmse, igv, amaze, ahd, rcd, dcb, amaze-cached")));
}

TEST(ClipGolden, TinyDualIsoHeadlessPlaybackProfileRejectsInvalidPlaybackProcessing)
{
    const QString fixture_path = clip_fixture_path();
    if (!QFileInfo::exists(fixture_path)) {
        SKIP_TEST("Missing fixture clip tests/fixtures/clips/tiny_dual_iso.mlv");
    }

    const QString app_exe = app_executable_path();
    if (app_exe.isEmpty() || !QFileInfo::exists(app_exe)) {
        SKIP_TEST("Set MLVAPP_PROFILE_EXE or MLVAPP_BATCH_EXE to a built MLVApp binary");
    }

    const QString repo_root = find_repo_root();
    ASSERT_TRUE(!repo_root.isEmpty());

    QTemporaryDir temp_dir;
    ASSERT_TRUE(temp_dir.isValid());
    const QString output_json = temp_dir.filePath(QStringLiteral("playback-profile-invalid-processing.json"));

    QProcess process;
    configure_playback_profile_process(
        &process,
        app_exe,
        repo_root,
        QStringList()
            << QStringLiteral("--profile-playback")
            << QStringLiteral("--input") << fixture_path
            << QStringLiteral("--output") << output_json
            << QStringLiteral("--playback-processing") << QStringLiteral("bogus"));
    process.start();
    ASSERT_TRUE(process.waitForStarted());
    ASSERT_TRUE(process.waitForFinished(-1));
    ASSERT_EQ(2, process.exitCode());

    const QString stderr_output = QString::fromLocal8Bit(process.readAllStandardError());
    ASSERT_TRUE(stderr_output.contains(
        QStringLiteral("--playback-processing must be one of auto, receipt, subset")));
}
