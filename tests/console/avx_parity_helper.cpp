#include "../common/hash_helpers.h"
#include "../common/repo_paths.h"
#include "../common/test_runtime.h"
#include "../pipeline/mlv_pipeline_fixture.h"

#include <QCoreApplication>
#include <QElapsedTimer>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>

#include <cstdio>

static QElapsedTimer g_helper_clock;

// Flushed stage breadcrumbs on stderr. The parent merges them into the output tail it prints
// on a timeout, so a hang names the stage it was stuck in.
static void note_stage(const char * stage, const QString & detail = QString())
{
    std::fprintf(stderr, "avx_parity_helper t=%lldms stage=%s %s\n",
                 static_cast<long long>(g_helper_clock.elapsed()),
                 stage,
                 detail.toLocal8Bit().constData());
    std::fflush(stderr);
}

static QJsonObject render_clip_hashes(const QString & clip_relative_path,
                                      const QString & receipt_relative_path,
                                      QString * error_message)
{
    MlvPipelineFixture fixture;
    note_stage("open-clip", clip_relative_path);
    if (!fixture.openClipFile(repo_file_path(clip_relative_path), error_message)) {
        return QJsonObject();
    }
    note_stage("load-receipt", receipt_relative_path);
    if (!fixture.loadReceipt(receipt_relative_path, error_message)) {
        return QJsonObject();
    }
    note_stage("apply-receipt");
    if (!fixture.applyReceipt(error_message)) {
        return QJsonObject();
    }

    note_stage("render", QStringLiteral("frame0_16"));
    const std::vector<uint16_t> frame0_16 = fixture.renderFrame16(0, 1);
    note_stage("render", QStringLiteral("frame1_16"));
    const std::vector<uint16_t> frame1_16 = fixture.renderFrame16(1, 1);
    note_stage("render", QStringLiteral("frame0_8"));
    const std::vector<uint8_t> frame0_8 = fixture.renderFrame8(0, 1);
    note_stage("render", QStringLiteral("frame1_8"));
    const std::vector<uint8_t> frame1_8 = fixture.renderFrame8(1, 1);

    QJsonObject object;
    object.insert(QStringLiteral("width"), fixture.width());
    object.insert(QStringLiteral("height"), fixture.height());
    object.insert(QStringLiteral("frame0_16"),
                  QString::fromStdString(sha256_bytes(frame0_16.data(), frame0_16.size() * sizeof(uint16_t))));
    object.insert(QStringLiteral("frame1_16"),
                  QString::fromStdString(sha256_bytes(frame1_16.data(), frame1_16.size() * sizeof(uint16_t))));
    object.insert(QStringLiteral("frame0_8"),
                  QString::fromStdString(sha256_bytes(frame0_8.data(), frame0_8.size())));
    object.insert(QStringLiteral("frame1_8"),
                  QString::fromStdString(sha256_bytes(frame1_8.data(), frame1_8.size())));
    // The fixture's destructor (freeMlvObject) runs after this line.
    note_stage("teardown", clip_relative_path);
    return object;
}

int main(int argc, char ** argv)
{
    g_helper_clock.start();
    test_runtime::force_single_threaded_pipeline();
    QCoreApplication app(argc, argv);

    QString output_path;
    for (int i = 1; i < argc; ++i) {
        const QString argument = QString::fromLocal8Bit(argv[i]);
        if (argument == QStringLiteral("--output") && (i + 1) < argc) {
            output_path = QString::fromLocal8Bit(argv[++i]);
        }
    }

    if (output_path.isEmpty()) {
        return 2;
    }

    QString error_message;
    QJsonObject root;
    root.insert(QStringLiteral("build_avx"),
#ifdef MLVAPP_BUILD_AVX
                true
#else
                false
#endif
    );

    root.insert(QStringLiteral("tiny_dual_iso"),
                render_clip_hashes(QStringLiteral("tests/fixtures/clips/tiny_dual_iso.mlv"),
                                   QStringLiteral("tests/fixtures/receipts/tiny_dual_iso_hq.marxml"),
                                   &error_message));
    if (!error_message.isEmpty()) {
        note_stage("error", error_message);
        return 3;
    }

    root.insert(QStringLiteral("large_dual_iso"),
                render_clip_hashes(QStringLiteral("tests/fixtures/clips/large_dual_iso.mlv"),
                                   QStringLiteral("tests/fixtures/receipts/large_dual_iso_hq.marxml"),
                                   &error_message));
    if (!error_message.isEmpty()) {
        note_stage("error", error_message);
        return 4;
    }

    note_stage("write-output", output_path);
    QFile output(output_path);
    if (!output.open(QIODevice::WriteOnly | QIODevice::Truncate | QIODevice::Text)) {
        note_stage("error", output.errorString());
        return 5;
    }
    output.write(QJsonDocument(root).toJson(QJsonDocument::Indented));
    output.close();
    note_stage("done");
    return 0;
}
