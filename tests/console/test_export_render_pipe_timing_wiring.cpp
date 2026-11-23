// Wiring test: EXPORT-RENDER-PIPE-TIMING-1. The rendered (ProRes) batch export emits llrawproc_dual_iso per frame (right
// after every llrawproc note in src/mlv/video_mlv.c, the same thread and frame, the value llrpGetLastDualIsoMilliseconds()
// holds for the llrawproc run that just finished) and export_scale / export_pipe_write per frame plus export_pipe_drain /
// export_verify once per clip (src/batch/BatchRunner.cpp), ONLY when MLVAPP_STAGE_TIMING is on.
// mlv_stage_timing_note / _note_elapsed (src/debug/StageTiming.h) write the thread-local snapshot BEFORE they check the
// enabled flag, so a new note outside the guard would cost a snapshot write (and a getter call / clock read) on every
// export frame with timing off. video_mlv.c and BatchRunner.cpp are not linked into console_tests, so this test reads
// the sources as text (mirrors test_llrawproc_residual_telemetry_wiring.cpp); the behaviour of the batch helper is
// tested natively in tests/pipeline/test_batch_render_stage_timing.cpp.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
#include <QRegularExpression>
#include <QString>
#include <QStringList>
#include <QTextStream>

namespace
{

QString readRepoFile(const QString & relativePath)
{
    const QString path = repo_file_path(relativePath);
    ASSERT_FALSE(path.isEmpty());
    QFile file(path);
    ASSERT_TRUE(file.open(QIODevice::ReadOnly | QIODevice::Text));
    QTextStream stream(&file);
    return stream.readAll();
}

int countOccurrences(const QString & haystack, const QString & needle)
{
    int count = 0;
    int from = 0;
    while (true) {
        const int at = haystack.indexOf(needle, from);
        if (at < 0) break;
        ++count;
        from = at + needle.length();
    }
    return count;
}

// The body of the first function whose text starts with `signature`, braces matched (the sources hold no brace in a
// string or comment inside these small bodies).
QString functionBody(const QString & source, const QString & signature)
{
    const int at = source.indexOf(signature);
    ASSERT_TRUE(at >= 0);
    const int open = source.indexOf(QLatin1Char('{'), at);
    ASSERT_TRUE(open > at);
    int depth = 0;
    for (int i = open; i < source.size(); ++i) {
        if (source.at(i) == QLatin1Char('{')) ++depth;
        else if (source.at(i) == QLatin1Char('}') && --depth == 0) return source.mid(open + 1, i - open - 1);
    }
    ASSERT_TRUE(false);
    return QString();
}

QString firstStatement(const QString & body)
{
    QString text = body;
    // drop leading comment lines and blank space
    text = text.trimmed();
    while (text.startsWith(QStringLiteral("/*"))) {
        const int end = text.indexOf(QStringLiteral("*/"));
        ASSERT_TRUE(end > 0);
        text = text.mid(end + 2).trimmed();
    }
    const int semi = text.indexOf(QLatin1Char(';'));
    ASSERT_TRUE(semi > 0);
    return text.left(semi + 1).simplified();
}

} // namespace

TEST(ExportRenderPipeTimingWiring, EveryLlrawprocNoteIsFollowedByTheGuardedDualIsoNote)
{
    const QString source = readRepoFile(QStringLiteral("src/mlv/video_mlv.c"));
    const QString llrawprocNote = QStringLiteral("mlv_stage_timing_note_elapsed(\"llrawproc\",");
    const int llrawprocNotes = countOccurrences(source, llrawprocNote);
    ASSERT_TRUE(llrawprocNotes >= 11);   // 11 sites on 2b79619832444c0a63a8573d04f6cd02a4a113dc

    const QString dualIsoCall = QStringLiteral("mlv_note_llrawproc_dual_iso(frameIndex);");
    ASSERT_EQ(llrawprocNotes, countOccurrences(source, dualIsoCall));

    // each llrawproc note line is followed by the dual-ISO note within the next three lines (the same frame, the same thread)
    const QStringList lines = source.split(QLatin1Char('\n'));
    int followed = 0;
    for (int i = 0; i < lines.size(); ++i) {
        if (!lines.at(i).contains(llrawprocNote)) continue;
        bool found = false;
        for (int k = i + 1; k <= i + 3 && k < lines.size(); ++k) {
            if (lines.at(k).contains(dualIsoCall)) { found = true; break; }
        }
        if (!found) std::cerr << "llrawproc note at video_mlv.c:" << (i + 1) << " has no dual-ISO note after it\n";
        ASSERT_TRUE(found);
        ++followed;
    }
    ASSERT_EQ(llrawprocNotes, followed);

    // the helper reads nothing and writes no snapshot when timing is off: its first statement is the guard
    const QString body = functionBody(source, QStringLiteral("static inline void mlv_note_llrawproc_dual_iso(uint64_t frameIndex)"));
    ASSERT_TRUE(firstStatement(body) == QStringLiteral("if (!mlv_stage_timing_enabled()) return;"));
    ASSERT_TRUE(body.contains(QStringLiteral("mlv_stage_timing_note_elapsed(MLV_STAGE_LLRAWPROC_DUAL_ISO, frameIndex, llrpGetLastDualIsoMilliseconds());")));
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("llrpGetLastDualIsoMilliseconds()")));
}

TEST(ExportRenderPipeTimingWiring, StageNamesAreTheJudgesNames)
{
    const QString header = readRepoFile(QStringLiteral("src/debug/StageTiming.h"));
    const char * const pairs[][2] = {
        { "MLV_STAGE_LLRAWPROC_DUAL_ISO", "\"llrawproc_dual_iso\"" },
        { "MLV_STAGE_EXPORT_SCALE", "\"export_scale\"" },
        { "MLV_STAGE_EXPORT_PIPE_WRITE", "\"export_pipe_write\"" },
        { "MLV_STAGE_EXPORT_PIPE_DRAIN", "\"export_pipe_drain\"" },
        { "MLV_STAGE_EXPORT_VERIFY", "\"export_verify\"" },
    };
    for (const auto & pair : pairs) {
        const QRegularExpression define(QStringLiteral("#define\\s+%1\\s+%2").arg(QLatin1String(pair[0]), QRegularExpression::escape(QLatin1String(pair[1]))));
        ASSERT_TRUE(define.match(header).hasMatch());
    }
}

TEST(ExportRenderPipeTimingWiring, EveryBatchExportNoteGoesThroughTheGuardedHelper)
{
    const QString runner = readRepoFile(QStringLiteral("src/batch/BatchRunner.cpp"));
    ASSERT_TRUE(runner.contains(QStringLiteral("#include \"BatchRenderStageTiming.h\"")));
    // no direct note: every export stage is noted by the helper, whose note is inside `if( !on ) return;`
    ASSERT_EQ(0, countOccurrences(runner, QStringLiteral("mlv_stage_timing_note(")));
    ASSERT_EQ(0, countOccurrences(runner, QStringLiteral("mlv_stage_timing_note_elapsed(")));
    ASSERT_EQ(0, countOccurrences(runner, QStringLiteral("mlv_stage_timing_now(")));
    const QStringList stages = { QStringLiteral("MLV_STAGE_EXPORT_SCALE"), QStringLiteral("MLV_STAGE_EXPORT_PIPE_WRITE"),
                                 QStringLiteral("MLV_STAGE_EXPORT_PIPE_DRAIN"), QStringLiteral("MLV_STAGE_EXPORT_VERIFY") };
    for (const QString & stage : stages) {
        ASSERT_EQ(1, countOccurrences(runner, stage));
        ASSERT_EQ(1, countOccurrences(runner, QStringLiteral("stageTiming.note( %1,").arg(stage)));
    }
    // the stage-timing flag is read once, before the frame loop
    const int declared = runner.indexOf(QStringLiteral("const BatchRenderStageTiming stageTiming;"));
    const int loop = runner.indexOf(QStringLiteral("for( uint32_t frameIndex = cutIn - 1; frameIndex < cutOut; frameIndex++ )"));
    ASSERT_TRUE(declared > 0);
    ASSERT_TRUE(loop > declared);

    const QString helper = readRepoFile(QStringLiteral("src/batch/BatchRenderStageTiming.h"));
    ASSERT_TRUE(firstStatement(functionBody(helper, QStringLiteral("void note("))) == QStringLiteral("if( !on ) return;"));
    ASSERT_TRUE(firstStatement(functionBody(helper, QStringLiteral("double start() const"))) == QStringLiteral("return on ? mlv_stage_timing_now() : 0.0;"));
}
