// Wiring/census test: pins that GpuDisplayWindow's swap telemetry (1) is wired to BOTH
// real swap paths -- Qt's own automatic swap (QOpenGLWindow::frameSwapped) and the
// explicit manual swapBuffers() in grabPresentedFramebufferIfActive -- (2) reuses the
// existing MLVAPP_PLAYBACK_SMOKE_TELEMETRY gate rather than a new ad hoc flag, and (3) is
// actually reset/read by MainWindow's playback smoke session. GpuDisplayWindow.cpp itself
// needs a full GL/GUI build (not linked into console_tests), so this test reads the
// sources as text -- the emit sites are pinned by call-site markers, not by exercising a
// live GL window.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include <QFile>
#include <QString>
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

} // namespace

TEST(GpuWindowSwapWiring, HeaderDeclaresTheSwapTelemetryApi)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("static void resetSwapTelemetry(quint64 sessionId)")));
    ASSERT_TRUE(header.contains(QStringLiteral("static GpuWindowSwapTelemetrySnapshot swapTelemetrySnapshot(void)")));
    ASSERT_TRUE(header.contains(QStringLiteral("#include \"GpuWindowSwapTelemetry.h\"")));
}

TEST(GpuWindowSwapWiring, AutomaticSwapPathIsConnectedToTheRecorder)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    // Real swap path 1: Qt's own automatic swap after paintGL(), signaled by frameSwapped().
    ASSERT_TRUE(source.contains(
        QStringLiteral("connect(this, &QOpenGLWindow::frameSwapped, this, &GpuDisplayWindow::noteRealSwap)")));
}

TEST(GpuWindowSwapWiring, ManualCaptureSwapPathAlsoCallsTheRecorder)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    // Real swap path 2: the explicit swapBuffers() in grabPresentedFramebufferIfActive,
    // which runs outside Qt's own paint-event cycle so frameSwapped() never fires for it.
    const int swapBuffersCalls = countOccurrences(source, QStringLiteral("glContext->swapBuffers(win)"));
    const int noteRealSwapCalls = countOccurrences(source, QStringLiteral("win->noteRealSwap()"));
    ASSERT_EQ(1, swapBuffersCalls);
    ASSERT_EQ(1, noteRealSwapCalls);

    const int swapBuffersAt = source.indexOf(QStringLiteral("glContext->swapBuffers(win)"));
    const int noteRealSwapAt = source.indexOf(QStringLiteral("win->noteRealSwap()"));
    ASSERT_TRUE(swapBuffersAt >= 0 && noteRealSwapAt > swapBuffersAt);
}

TEST(GpuWindowSwapWiring, GatingReusesTheExistingSmokeTelemetryEnvVarNotANewFlag)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "qEnvironmentVariableIsSet( \"MLVAPP_PLAYBACK_SMOKE_TELEMETRY\" )")));
    // The recorder must check the gate before doing any per-swap work.
    ASSERT_TRUE(source.contains(QStringLiteral("if ( !swapTelemetryEnabled() ) return;")));
}

TEST(GpuWindowSwapWiring, PlaybackSmokeSessionResetsAndReadsSwapTelemetry)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(
        QStringLiteral("GpuDisplayWindow::resetSwapTelemetry( m_playbackSmokeSessionId )")));
    ASSERT_TRUE(source.contains(
        QStringLiteral("GpuDisplayWindow::swapTelemetrySnapshot()")));
    ASSERT_TRUE(source.contains(QStringLiteral("playback_smoke.gpu_window_swaps")));
}

// Round 2, fix 1 (sol BLOCKER 2): disabled telemetry must be zero-cost and zero-output --
// no frameSwapped connection, no noteRealSwap work, no summary line.

TEST(GpuWindowSwapWiring, AutomaticSwapConnectionIsGatedOnTelemetryEnabledAtConstruction)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    // The env var is cached and cannot change mid-run (see swapTelemetryEnabled()), so the
    // decision not to connect at all when disabled is made once, in the constructor --
    // not re-checked per swap via an early return in the slot alone.
    ASSERT_TRUE(source.contains(QStringLiteral(
        "if ( swapTelemetryEnabled() )\n"
        "    {\n"
        "        connect(this, &QOpenGLWindow::frameSwapped, this, &GpuDisplayWindow::noteRealSwap);\n"
        "    }")));
}

TEST(GpuWindowSwapWiring, NoteRealSwapStillEarlyReturnsOnTheExistingGate)
{
    // Pinned separately from the construction-time gate above: even if a future change
    // made the connection unconditional again, noteRealSwap() must still refuse to do any
    // per-swap work when telemetry is disabled.
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("if ( !swapTelemetryEnabled() ) return;")));
}

TEST(GpuWindowSwapWiring, GpuWindowSwapSummaryLineIsGatedOnTelemetryEnabled)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int gateAt = source.indexOf(QStringLiteral("if ( swapSnapshot.telemetryEnabled )"));
    const int lineAt = source.indexOf(QStringLiteral("playback_smoke.gpu_window_swaps"));
    ASSERT_TRUE(gateAt >= 0);
    ASSERT_TRUE(lineAt > gateAt);
    // ...and the emission must be the ONLY thing gated -- swapTelemetrySnapshot() (which
    // closes the session) still has to run unconditionally, every time, so a disabled-at-
    // begin/enabled-at-end (or vice versa) run cannot leave the session open forever.
    const int snapshotAt = source.indexOf(QStringLiteral("GpuDisplayWindow::swapTelemetrySnapshot()"));
    ASSERT_TRUE(snapshotAt >= 0 && snapshotAt < gateAt);
}

// Round 2, fix 2 (sol BLOCKER 1): swapTelemetrySnapshot() closes the session so swaps
// after the gate (queued or screenshot-capture swaps included) are not recorded under it.

TEST(GpuWindowSwapWiring, SwapTelemetrySnapshotClosesTheSessionBeforeReturning)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    // Two occurrences expected: the file-scope declaration ("... = false;") and the
    // deactivation inside swapTelemetrySnapshot(); counting occurrences (rather than a
    // plain contains()) so a mutation that deletes the deactivation but leaves the
    // declaration intact still fails this test.
    ASSERT_EQ(2, countOccurrences(source, QStringLiteral("g_swapTelemetrySessionActive = false;")));

    const int functionAt = source.indexOf(QStringLiteral("GpuWindowSwapTelemetrySnapshot GpuDisplayWindow::swapTelemetrySnapshot()"));
    ASSERT_TRUE(functionAt >= 0);
    const int deactivateAt = source.indexOf(QStringLiteral("g_swapTelemetrySessionActive = false;"), functionAt);
    ASSERT_TRUE(deactivateAt > functionAt);
}

TEST(GpuWindowSwapWiring, NoteRealSwapRefusesToRecordOnceTheSessionIsClosed)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int enabledGateAt = source.indexOf(QStringLiteral("if ( !swapTelemetryEnabled() ) return;"));
    const int activeGateAt = source.indexOf(QStringLiteral("if ( !g_swapTelemetrySessionActive ) return;"));
    ASSERT_TRUE(enabledGateAt >= 0);
    ASSERT_TRUE(activeGateAt > enabledGateAt);
}

// Round 2, fix 4 (sol+fable hardening): the session id must be set regardless of whether
// a window is active yet, so it survives the window being inactive at begin or recreated
// mid-session.

TEST(GpuWindowSwapWiring, ResetSwapTelemetrySetsSessionIdBeforeAnyWindowLookup)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int functionAt = source.indexOf(QStringLiteral("void GpuDisplayWindow::resetSwapTelemetry(quint64 sessionId)"));
    ASSERT_TRUE(functionAt >= 0);
    const int nextFunctionAt = source.indexOf(QStringLiteral("GpuWindowSwapTelemetrySnapshot GpuDisplayWindow::swapTelemetrySnapshot()"), functionAt);
    ASSERT_TRUE(nextFunctionAt > functionAt);
    const QString body = source.mid(functionAt, nextFunctionAt - functionAt);

    const int sessionIdAt = body.indexOf(QStringLiteral("g_swapTelemetrySessionId = sessionId;"));
    const int windowLookupAt = body.indexOf(QStringLiteral("g_activeWindow.load(std::memory_order_acquire)"));
    ASSERT_TRUE(sessionIdAt >= 0);
    ASSERT_TRUE(windowLookupAt > sessionIdAt);
}

// CUDA-PERF-DISPLAY-IDENTITY-3 (sol BLOCKER on #161): with telemetry off the instrument does no work at all --
// no clock sample and no state change at session begin/end, and the screenshot-path swap never enters the recorder.

namespace
{
QString functionBody(const QString & source, const QString & signature, const QString & nextSignature)
{
    const int at = source.indexOf(signature);
    const int next = source.indexOf(nextSignature, at);
    if (at < 0 || next <= at) return QString();
    return source.mid(at, next - at);
}
} // namespace

TEST(GpuWindowSwapWiring, TelemetryOffSessionBeginDoesNoWork)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("void GpuDisplayWindow::resetSwapTelemetry(quint64 sessionId)"),
        QStringLiteral("GpuWindowSwapTelemetrySnapshot GpuDisplayWindow::swapTelemetrySnapshot()"));
    ASSERT_FALSE(body.isEmpty());
    const int gateAt = body.indexOf(QStringLiteral("if ( !swapTelemetryEnabled() ) return;"));
    ASSERT_TRUE(gateAt >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("mlv_stage_timing_now")) > gateAt);
    ASSERT_TRUE(body.indexOf(QStringLiteral("g_swapTelemetrySessionActive = true;")) > gateAt);
}

TEST(GpuWindowSwapWiring, TelemetryOffSessionEndDoesNoWork)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("GpuWindowSwapTelemetrySnapshot GpuDisplayWindow::swapTelemetrySnapshot()"),
        QStringLiteral("void GpuDisplayWindow::noteRealSwap()"));
    ASSERT_FALSE(body.isEmpty());
    const int gateAt = body.indexOf(QStringLiteral("if ( !swapTelemetryEnabled() ) return GpuWindowSwapTelemetrySnapshot();"));
    ASSERT_TRUE(gateAt >= 0);
    ASSERT_TRUE(body.indexOf(QStringLiteral("mlv_stage_timing_now")) > gateAt);
    ASSERT_TRUE(body.indexOf(QStringLiteral("g_swapTelemetrySessionActive = false;")) > gateAt);
}

TEST(GpuWindowSwapWiring, TelemetryOffCaptureSwapNeverEntersTheRecorder)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("if ( swapTelemetryEnabled() ) win->noteRealSwap();")));
}

TEST(GpuWindowSwapWiring, GuiTestsLinkTheOpenMpRuntimeTheSwapClockNeeds)
{
    // mlv_stage_timing_now() falls back to omp_get_wtime(); without libgomp gui_tests failed to link
    // (hosted Windows GUI Pilot, #159 and #161).
    const QString pro = readRepoFile(QStringLiteral("tests/gui/gui_tests.pro"));
    ASSERT_TRUE(pro.contains(QStringLiteral("win32: LIBS += -llibgomp-1")));
}

