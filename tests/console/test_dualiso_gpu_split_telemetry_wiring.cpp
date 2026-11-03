// Wiring test: CUDA-RECON-DUALISO-TIMER-SPLIT-1. The split of the dual_iso timer on the CUDA texture route
// (llrawproc.h llrpDualIsoGpuSplitTiming_t) rides the LLRAWPROC-RESIDUAL-SUBTIMERS-1 bundle: RenderFrameThread's
// insertLlrawprocDualIsoGpuSplitTelemetry is called only from insertLlrawprocResidualSubstageTelemetry (so the
// render-thread "this thread ran llrawproc" guard covers it), its fields are NOT subtracted from llrawproc_other_ms
// (they are inside llrawproc_dual_iso_ms), every key keeps the llrawproc_ prefix the CUDA texture route preserves,
// and MainWindow's playback_smoke.dual_iso_split line reads the same-run bundle, never the generic dual_iso key.
// The timers in llrawproc.c bracket the backend mutex wait and both run calls. RenderFrameThread.cpp and
// MainWindow.cpp need the full GUI build, so this reads the sources as text (as the residual wiring test does);
// tests/pipeline/test_dualiso_gpu_split_timing.cpp runs the C side on the fake backend.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QTextStream>

namespace
{

QString splitReadRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

QString splitFunctionBody(const QString & source, const QString & signature)
{
    const int at = source.indexOf(signature);
    ASSERT_TRUE(at >= 0);
    const int open = source.indexOf(QLatin1Char('{'), at);
    ASSERT_TRUE(open >= 0);
    int depth = 0;
    for (int i = open; i < source.size(); ++i) {
        if (source.at(i) == QLatin1Char('{')) ++depth;
        else if (source.at(i) == QLatin1Char('}') && --depth == 0) return source.mid(open, i - open + 1);
    }
    ASSERT_TRUE(false);
    return QString();
}

const char * const kSplitKeys[] = {
    "llrawproc_dual_iso_split_dual_iso_ms", "llrawproc_dual_iso_split_prepare_ms",
    "llrawproc_dual_iso_split_preupload_ms", "llrawproc_dual_iso_split_mutex_wait_ms",
    "llrawproc_dual_iso_split_configure_ms", "llrawproc_dual_iso_split_run_wall_ms",
    "llrawproc_dual_iso_split_run_gpu_ms", "llrawproc_dual_iso_split_sync_ms",
    "llrawproc_dual_iso_split_post_run_ms", "llrawproc_dual_iso_split_backend_calls",
    "llrawproc_dual_iso_split_other_ms"};

} // namespace

TEST(DualIsoGpuSplitTelemetryWiring, SplitRidesTheResidualBundleAndIsNotSubtractedFromOther)
{
    const QString source = splitReadRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    // Exactly one call, inside the residual bundle (whose two call sites carry the worker / render-thread guard).
    ASSERT_EQ(1, (int)source.count(QStringLiteral("insertLlrawprocDualIsoGpuSplitTelemetry( target );")));
    const QString residual = splitFunctionBody(source,
        QStringLiteral("double insertLlrawprocResidualSubstageTelemetry( QJsonObject &target,"));
    ASSERT_TRUE(residual.contains(QStringLiteral("insertLlrawprocDualIsoGpuSplitTelemetry( target );")));
    // The returned sum (subtracted into llrawproc_other_ms) names none of the split's fields.
    const int sumAt = residual.indexOf(QStringLiteral("const double substageMs ="));
    const int sumEnd = residual.indexOf(QLatin1Char(';'), sumAt);
    ASSERT_TRUE(sumAt >= 0 && sumEnd > sumAt);
    const QString sum = residual.mid(sumAt, sumEnd - sumAt);
    for (const char * field : {"prepare_ms", "preupload_ms", "mutex_wait_ms", "configure_ms", "run_wall_ms",
                               "run_gpu_ms", "sync_ms", "post_run_ms"})
        ASSERT_FALSE(sum.contains(QString::fromLatin1(field)));
}

TEST(DualIsoGpuSplitTelemetryWiring, EverySplitKeyCarriesThePreservedPrefixAndTheSameRunDualIso)
{
    const QString source = splitReadRepoFile(QStringLiteral("platform/qt/RenderFrameThread.cpp"));
    const QString body = splitFunctionBody(source,
        QStringLiteral("void insertLlrawprocDualIsoGpuSplitTelemetry( QJsonObject &target )"));
    const QRegularExpression key(QStringLiteral("QStringLiteral\\(\"([a-z0-9_]+)\"\\)"));
    int keys = 0;
    for (QRegularExpressionMatchIterator it = key.globalMatch(body); it.hasNext();) {
        const QString name = it.next().captured(1);
        ASSERT_TRUE(name.startsWith(QStringLiteral("llrawproc_dual_iso_split_")));
        ++keys;
    }
    ASSERT_EQ(11, keys);
    for (const char * k : kSplitKeys)
        ASSERT_TRUE(body.contains(QStringLiteral("\"%1\"").arg(QLatin1String(k))));
    // The bundle's dual_iso is this thread's last run, read beside the split (not a slot value from elsewhere).
    ASSERT_TRUE(body.contains(QStringLiteral("const double dualIsoMs = llrpGetLastDualIsoMilliseconds();")));
    ASSERT_TRUE(body.contains(QStringLiteral("llrpGetLastDualIsoGpuSplitTiming( &split );")));
    ASSERT_TRUE(source.contains(QStringLiteral("return key.startsWith( QStringLiteral(\"llrawproc_\") )")));
}

TEST(DualIsoGpuSplitTelemetryWiring, SmokeLineReadsTheBundleNotTheGenericDualIso)
{
    const QString source = splitReadRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int lineAt = source.indexOf(QStringLiteral("\"playback_smoke.dual_iso_split session=%1"));
    ASSERT_TRUE(lineAt >= 0);
    const int endAt = source.indexOf(QStringLiteral("qInfo()"), lineAt);
    ASSERT_TRUE(endAt > lineAt);
    const QString block = source.mid(lineAt, endAt - lineAt);
    for (const char * k : kSplitKeys)
        ASSERT_TRUE(block.contains(QStringLiteral("\"%1\"").arg(QLatin1String(k))));
    ASSERT_FALSE(block.contains(QStringLiteral("\"llrawproc_dual_iso_ms\"")));
    // It sits right after the residual line, under the same guard (no parsed line moves).
    const int residualAt = source.indexOf(QStringLiteral("\"playback_smoke.llrawproc_residual session=%1"));
    ASSERT_TRUE(residualAt >= 0 && residualAt < lineAt);
    ASSERT_EQ(lineAt, source.indexOf(QStringLiteral("\"playback_smoke."), residualAt + 1));
}

TEST(DualIsoGpuSplitTelemetryWiring, BackendTimersBracketTheMutexWaitAndBothRunCalls)
{
    const QString source = splitReadRepoFile(QStringLiteral("src/mlv/llrawproc/llrawproc.c"));
    const QString body = splitFunctionBody(source,
        QStringLiteral("static int llrawproc_gpu_recon_run_backend(const dualiso_gpu_recon_state_t * state,"));
    const int startAt = body.indexOf(QStringLiteral("const double split_lock_start_s = mlv_stage_timing_now();"));
    const int lockAt = body.indexOf(QStringLiteral("pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);"));
    const int acquiredAt = body.indexOf(QStringLiteral("split_lock_acquired_s = mlv_stage_timing_now();"));
    const int availableAt = body.indexOf(QStringLiteral("if(!llrawproc_gpu_export_backend_available(prefer_playback_dll))"));
    ASSERT_TRUE(startAt >= 0 && startAt < lockAt && lockAt < acquiredAt && acquiredAt < availableAt);
    ASSERT_EQ(1, (int)body.count(QStringLiteral("pthread_mutex_lock(&g_llrawproc_gpu_recon_backend_mutex);")));
    // Both run calls start the run timer right before the call; one end stamp follows both branches.
    for (const char * call : {"rc = g->run_preuploaded(g->backend,", "rc = g->run(g->backend,"}) {
        const int callAt = body.indexOf(QString::fromLatin1(call));
        ASSERT_TRUE(callAt >= 0);
        const int stampAt = body.lastIndexOf(QStringLiteral("split_run_start_s = mlv_stage_timing_now();"), callAt);
        ASSERT_TRUE(stampAt >= 0);
        // Nothing but the stamp's own statement sits between the stamp and the call it times.
        ASSERT_EQ(1, (int)body.mid(stampAt, callAt - stampAt).count(QLatin1Char(';')));
    }
    const int endAt = body.indexOf(QStringLiteral("split_run_end_s = mlv_stage_timing_now();"));
    ASSERT_TRUE(endAt > body.indexOf(QStringLiteral("rc = g->run(g->backend,")));
    ASSERT_TRUE(endAt < body.indexOf(QStringLiteral("InterlockedExchange(&g_llrawproc_gpu_recon_run_active, 0);")));
    // The held-time split is booked before the device-loss scan and the unlock that follows it.
    const int bookAt = body.indexOf(QStringLiteral("split->post_run_ms +="));
    const int scanAt = body.indexOf(QStringLiteral("llrawproc_gpu_suppress_on_device_loss_locked(telemetry_events, telemetry_event_count);"));
    ASSERT_TRUE(bookAt >= 0 && bookAt < scanAt);
    // Every reset point that zeroes the residual record zeroes the split too.
    ASSERT_EQ((int)source.count(QStringLiteral("memset(&g_llrawproc_last_residual, 0, sizeof(g_llrawproc_last_residual));")),
              (int)source.count(QStringLiteral("memset(&g_llrawproc_last_dual_iso_gpu_split, 0, sizeof(g_llrawproc_last_dual_iso_gpu_split));")));
}
