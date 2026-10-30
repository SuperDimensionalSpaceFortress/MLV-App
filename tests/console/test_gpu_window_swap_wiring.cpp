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
#include <QRegularExpression>
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
    // Real swap path 2: the explicit swapBuffers() in swapOutsidePaintEventAndRecord (the one
    // manual swap site; see ForcedCapturePaintReusesTheOneManualSwapSite below), which runs
    // outside Qt's own paint-event cycle so frameSwapped() never fires for it.
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

// PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2 (Dot 6d0e710f B1): the --visual-capture-dir forced
// paint (paintPendingNowIfActive) and the --screenshot grab both paint outside Qt's paint-event
// cycle. They share ONE manual swap site, so every frame they present is swapped once and the
// swap recorder sees it; a second inline swapBuffers() is what reddened the pin above.

TEST(GpuWindowSwapWiring, ForcedCapturePaintReusesTheOneManualSwapSite)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString call = QStringLiteral("swapOutsidePaintEventAndRecord(win, glContext);");

    const QString helper = functionBody(source,
        QStringLiteral("void GpuDisplayWindow::swapOutsidePaintEventAndRecord(GpuDisplayWindow *win, QOpenGLContext *glContext)"),
        QStringLiteral("bool GpuDisplayWindow::grabPresentedFramebufferIfActive("));
    ASSERT_FALSE(helper.isEmpty());
    const int swapAt = helper.indexOf(QStringLiteral("glContext->swapBuffers(win);"));
    const int noteAt = helper.indexOf(QStringLiteral("if ( swapTelemetryEnabled() ) win->noteRealSwap();"));
    ASSERT_TRUE(swapAt >= 0);
    ASSERT_TRUE(noteAt > swapAt);

    // The grab: one paint, one swap through the shared site, and no inline swap.
    const QString grab = functionBody(source,
        QStringLiteral("bool GpuDisplayWindow::grabPresentedFramebufferIfActive("),
        QStringLiteral("bool GpuDisplayWindow::presentedSerialIfActive("));
    ASSERT_FALSE(grab.isEmpty());
    ASSERT_EQ(1, countOccurrences(grab, call));
    ASSERT_EQ(0, countOccurrences(grab, QStringLiteral("swapBuffers(")));
    ASSERT_TRUE(grab.indexOf(call) > grab.indexOf(QStringLiteral("win->paintGL();")));

    // The forced capture paint: the same site, once, and only when a submitted frame is still
    // unpainted -- a frame already painted is never painted or swapped a second time.
    const QString forced = functionBody(source,
        QStringLiteral("bool GpuDisplayWindow::paintPendingNowIfActive(quint64 *presentedSerial)"),
        QStringLiteral("void GpuDisplayWindow::initializeGL()"));
    ASSERT_FALSE(forced.isEmpty());
    ASSERT_EQ(1, countOccurrences(forced, call));
    ASSERT_EQ(0, countOccurrences(forced, QStringLiteral("swapBuffers(")));
    ASSERT_EQ(1, countOccurrences(forced, QStringLiteral("win->paintGL();")));
    const int guardAt = forced.indexOf(QStringLiteral("if ( pending && glContext"));
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(forced.indexOf(QStringLiteral("win->paintGL();")) > guardAt);
    ASSERT_TRUE(forced.indexOf(call) > forced.indexOf(QStringLiteral("win->paintGL();")));
}

// Fate telemetry (CUDA-PLAYBACK-PRESENT-CADENCE-1): superseded-before-paint wiring. See
// docs/cuda-playback-present-cadence.md for why this, not GUI-thread work on a named
// component or swap-chain recreation, accounts for the gap between frames produced and
// frames shown.

TEST(GpuWindowSwapWiring, HeaderDeclaresTheFateTelemetryMethod)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("void noteSupersededBeforePaint(quint64 supersedingSerial);")));
}

TEST(GpuWindowSwapWiring, BothPresentRoutesCallTheFateTelemetryBeforeMutatingPendingState)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));

    // QImage route: called at the top of setPresentedImage(), before m_pendingImage etc.
    // are touched.
    const int setImageAt = source.indexOf(QStringLiteral("void GpuDisplayWindow::setPresentedImage("));
    const int setImageNoteAt = source.indexOf(
        QStringLiteral("if ( swapTelemetryEnabled() ) noteSupersededBeforePaint(presentationSerial);"),
        setImageAt);
    const int setImagePendingWriteAt = source.indexOf(
        QStringLiteral("m_pendingImage = image.format()"), setImageAt);
    ASSERT_TRUE(setImageAt >= 0);
    ASSERT_TRUE(setImageNoteAt > setImageAt);
    ASSERT_TRUE(setImagePendingWriteAt > setImageNoteAt);

    // GPU-recon-texture route: called right before the post-success pending-state mutation
    // block (every earlier `fail()` return leaves pending state untouched, so this call must
    // sit AFTER those, not at the function's own top).
    const int setTexAt = source.indexOf(
        QStringLiteral("bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture("));
    const int setTexNoteAt = source.indexOf(
        QStringLiteral("if ( swapTelemetryEnabled() ) noteSupersededBeforePaint(presentationSerial);"),
        setTexAt);
    const int setTexPendingWriteAt = source.indexOf(
        QStringLiteral("m_pendingImage = QImage();"), setTexAt);
    ASSERT_TRUE(setTexAt >= 0);
    ASSERT_TRUE(setTexNoteAt > setTexAt);
    ASSERT_TRUE(setTexPendingWriteAt > setTexNoteAt);
}

TEST(GpuWindowSwapWiring, FateTelemetryGatesOnBothTelemetryEnabledAndSessionActive)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int functionAt = source.indexOf(
        QStringLiteral("void GpuDisplayWindow::noteSupersededBeforePaint(quint64 supersedingSerial)"));
    ASSERT_TRUE(functionAt >= 0);
    const int nextFunctionAt = source.indexOf(
        QStringLiteral("bool GpuDisplayWindow::installInPreview("), functionAt);
    ASSERT_TRUE(nextFunctionAt > functionAt);
    const QString body = source.mid(functionAt, nextFunctionAt - functionAt);

    const int enabledGateAt = body.indexOf(QStringLiteral("if ( !swapTelemetryEnabled() ) return;"));
    const int activeGateAt = body.indexOf(QStringLiteral("if ( !g_swapTelemetrySessionActive ) return;"));
    const int pendingValidGateAt = body.indexOf(
        QStringLiteral("if ( !m_pendingPresentationSerialValid || m_texturePresentationActive ) return;"));
    ASSERT_TRUE(enabledGateAt >= 0);
    ASSERT_TRUE(activeGateAt > enabledGateAt);
    ASSERT_TRUE(pendingValidGateAt > activeGateAt);
}

TEST(GpuWindowSwapWiring, FateTelemetryLogLineCarriesBothSerials)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("gpu_window.present_fate session=%1 fate=superseded_before_paint")));
    ASSERT_TRUE(source.contains(QStringLiteral("superseded_serial=%2 superseded_by_serial=%3")));
}

TEST(GpuWindowSwapWiring, SupersededCountsAreResetWithTheRestOfTheCountersAtSessionBegin)
{
    // resetSwapTelemetry() resets the whole GpuWindowSwapTelemetryCounters struct in one
    // assignment, so a new field there is reset for free -- pinned so a future refactor that
    // starts resetting individual fields cannot silently drop this one.
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("win->m_swapTelemetryCounters = GpuWindowSwapTelemetryCounters();")));
}

TEST(GpuWindowSwapWiring, PlaybackSmokeSummaryLineIncludesSupersededCounts)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int lineAt = source.indexOf(QStringLiteral("playback_smoke.gpu_window_swaps"));
    ASSERT_TRUE(lineAt >= 0);
    ASSERT_TRUE(source.contains(QStringLiteral("superseded_before_paint=%15")));
    ASSERT_TRUE(source.contains(
        QStringLiteral(".arg( static_cast<qulonglong>( swapSnapshot.summary.supersededCount ) )")));
    ASSERT_TRUE(source.contains(
        QStringLiteral(".arg( static_cast<qulonglong>( swapSnapshot.summary.lastSupersededSerial ) )")));
    ASSERT_TRUE(source.contains(
        QStringLiteral(".arg( static_cast<qulonglong>( swapSnapshot.summary.lastSupersededBySerial ) )")));
}

// Round 2 (CUDA-PLAYBACK-PRESENT-CADENCE-1): counters-only mode. A new opt-out flag
// suppresses only the per-swap/per-superseded-frame qInfo() lines, never the counting
// that feeds the one-shot session summary above.

TEST(GpuWindowSwapWiring, PerEventLogHelperDefaultsEnabledAndIsAnOptOutFlag)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int functionAt = source.indexOf(QStringLiteral("bool swapTelemetryPerEventLogEnabled()"));
    ASSERT_TRUE(functionAt >= 0);
    const QString body = source.mid(functionAt, 300);
    // Opt-out shape: a "disabled" bool read from the env var, negated on return -- unset
    // (windowEnvFlagEnabled("") is false) means disabled=false means the helper returns
    // true, i.e. legacy fully-verbose behavior by default.
    ASSERT_TRUE(body.contains(QStringLiteral(
        "windowEnvFlagEnabled( qgetenv( \"MLVAPP_PLAYBACK_SMOKE_TELEMETRY_DISABLE_FRAME_LOG\" ) );")));
    ASSERT_TRUE(body.contains(QStringLiteral("return !disabled;")));
}

TEST(GpuWindowSwapWiring, NoteRealSwapCountsBeforeAndOutsideThePerEventLogGate)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int functionAt = source.indexOf(QStringLiteral("void GpuDisplayWindow::noteRealSwap()"));
    ASSERT_TRUE(functionAt >= 0);
    const int nextFunctionAt = source.indexOf(
        QStringLiteral("void GpuDisplayWindow::noteSupersededBeforePaint("), functionAt);
    ASSERT_TRUE(nextFunctionAt > functionAt);
    const QString body = source.mid(functionAt, nextFunctionAt - functionAt);

    const int countAt = body.indexOf(QStringLiteral("++m_swapTelemetryCounters.swapCount"));
    const int gateAt = body.indexOf(QStringLiteral("if ( swapTelemetryPerEventLogEnabled() )"));
    const int logAt = body.indexOf(QStringLiteral("gpu_window.swap session=%1"));
    ASSERT_TRUE(countAt >= 0);
    ASSERT_TRUE(gateAt > countAt);
    ASSERT_TRUE(logAt > gateAt);
}

TEST(GpuWindowSwapWiring, NoteSupersededBeforePaintCountsBeforeAndOutsideThePerEventLogGate)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int functionAt = source.indexOf(
        QStringLiteral("void GpuDisplayWindow::noteSupersededBeforePaint(quint64 supersedingSerial)"));
    ASSERT_TRUE(functionAt >= 0);
    const int nextFunctionAt = source.indexOf(
        QStringLiteral("bool GpuDisplayWindow::installInPreview("), functionAt);
    ASSERT_TRUE(nextFunctionAt > functionAt);
    const QString body = source.mid(functionAt, nextFunctionAt - functionAt);

    const int countAt = body.indexOf(QStringLiteral("++m_swapTelemetryCounters.supersededCount;"));
    const int gateAt = body.indexOf(QStringLiteral("if ( swapTelemetryPerEventLogEnabled() )"));
    const int logAt = body.indexOf(QStringLiteral("gpu_window.present_fate session=%1"));
    ASSERT_TRUE(countAt >= 0);
    ASSERT_TRUE(gateAt > countAt);
    ASSERT_TRUE(logAt > gateAt);
}

// CUDA-PLAYBACK-PRESENT-CADENCE-2 round 1: paint-per-submit (real swap path 3) and the
// new-frame counter it makes meaningful.

TEST(GpuWindowSwapWiring, PaintPerSubmitOptOutHelperDefaultsEnabled)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int functionAt = source.indexOf(QStringLiteral("bool paintPerSubmitEnabled()"));
    ASSERT_TRUE(functionAt >= 0);
    const QString body = source.mid(functionAt, 400);
    // Opt-out shape (mirrors swapTelemetryEnabled()'s own "!= '0'" pattern): unset, or set
    // to anything but a literal "0", leaves the new path active.
    ASSERT_TRUE(body.contains(QStringLiteral(
        "!qEnvironmentVariableIsSet( \"MLVAPP_GPU_WINDOW_PAINT_PER_SUBMIT\" )")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "qEnvironmentVariable( \"MLVAPP_GPU_WINDOW_PAINT_PER_SUBMIT\" ) != QStringLiteral(\"0\")")));
}

TEST(GpuWindowSwapWiring, ReconAmazeSubmitPaintsSynchronouslyBehindTheExposedValidGuard)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int functionAt = source.indexOf(
        QStringLiteral("bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture("));
    ASSERT_TRUE(functionAt >= 0);
    const int nextFunctionAt = source.indexOf(
        QStringLiteral("bool GpuDisplayWindow::readGpuReconSourceBayer16Texture("), functionAt);
    ASSERT_TRUE(nextFunctionAt > functionAt);
    const QString body = source.mid(functionAt, nextFunctionAt - functionAt);

    // Same guard as the grab idiom (isExposed()/isValid()), gated additionally on the
    // opt-out, and never during a non-exposed/mid-transition window (#171's own invariant --
    // Qt reports a window as not exposed during a fullscreen enter/exit).
    const int guardAt = body.indexOf(
        QStringLiteral("if ( paintPerSubmitEnabled() && isExposed() && isValid() )"));
    ASSERT_TRUE(guardAt >= 0);
    const int paintAt = body.indexOf(QStringLiteral("paintGL();"), guardAt);
    const int swapAt = body.indexOf(QStringLiteral("glContext->swapBuffers(this);"), guardAt);
    const int noteAt = body.indexOf(QStringLiteral("noteRealSwap();"), guardAt);
    ASSERT_TRUE(paintAt > guardAt);
    ASSERT_TRUE(swapAt > paintAt);
    ASSERT_TRUE(noteAt > swapAt);

    // The trailing update() must be conditional on NOT having already painted -- otherwise
    // Qt would paint (and vsync-block-swap) the same already-shown serial a second time.
    const int flagSetAt = body.indexOf(QStringLiteral("paintedSynchronously = true;"), guardAt);
    const int conditionalUpdateAt = body.indexOf(
        QStringLiteral("if ( !paintedSynchronously ) update();"), flagSetAt);
    ASSERT_TRUE(flagSetAt > guardAt);
    ASSERT_TRUE(conditionalUpdateAt > flagSetAt);
    // An unconditional update() call must not remain in this function alongside the
    // conditional one (a leftover would repaint every submit regardless of paintedSynchronously).
    ASSERT_EQ(0, countOccurrences(body.left(conditionalUpdateAt), QStringLiteral("\n    update();\n")));
}

TEST(GpuWindowSwapWiring, NewFrameSwapCountingUsesThisSwapsOwnRecordNotWindowState)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const int functionAt = source.indexOf(QStringLiteral("void GpuDisplayWindow::noteRealSwap()"));
    ASSERT_TRUE(functionAt >= 0);
    const int nextFunctionAt = source.indexOf(
        QStringLiteral("void GpuDisplayWindow::noteSupersededBeforePaint("), functionAt);
    ASSERT_TRUE(nextFunctionAt > functionAt);
    const QString body = source.mid(functionAt, nextFunctionAt - functionAt);

    // Reads record.presentedSerialValid/record.presentedSerial (this exact swap's own
    // just-written ring entry), never m_presentedSerial/m_presentedSerialValid directly --
    // see GpuWindowSwapTelemetryPolicy::isNewFrameSwap's own wiring comment.
    const int policyCallAt = body.indexOf(QStringLiteral("GpuWindowSwapTelemetryPolicy::isNewFrameSwap("));
    ASSERT_TRUE(policyCallAt >= 0);
    const QString callRegion = body.mid(policyCallAt, 220);
    ASSERT_TRUE(callRegion.contains(QStringLiteral("record.presentedSerialValid")));
    ASSERT_TRUE(callRegion.contains(QStringLiteral("record.presentedSerial")));
    ASSERT_TRUE(callRegion.contains(
        QStringLiteral("m_swapTelemetryCounters.lastCountedNewFramePresentedSerial")));

    const int recordWriteAt = body.indexOf(
        QStringLiteral("m_swapTelemetryRing[ static_cast<std::size_t>"));
    ASSERT_TRUE(recordWriteAt >= 0 && recordWriteAt < policyCallAt);
}

TEST(GpuWindowSwapWiring, GpuWindowSwapSummaryLineCarriesNewFrameFields)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int lineAt = source.indexOf(QStringLiteral("playback_smoke.gpu_window_swaps"));
    ASSERT_TRUE(lineAt >= 0);
    const QString region = source.mid(lineAt, 2700);
    ASSERT_TRUE(region.contains(QStringLiteral("new_frame_swaps=%18")));
    ASSERT_TRUE(region.contains(QStringLiteral("swapSnapshot.summary.newFrameSwapCount")));
}


// PIN-CUDA-PRESENT-INVARIANTS-1: direct pins for two invariants of the paint-per-submit
// present path (PR #185) that no earlier test asserted -- (1) the presented serial is
// promoted in paintGL() and nowhere else, only after a real draw; (2) the fail-closed LUT
// gates on the GPU-recon (post-WB-undo, linear) texture route. Source-text pins, same
// technique as the tests above (GpuDisplayWindow.cpp is not linked into console_tests).

namespace
{
int countMatches(const QString & haystack, const QString & pattern)
{
    QRegularExpressionMatchIterator it = QRegularExpression(pattern).globalMatch(haystack);
    int count = 0;
    while (it.hasNext()) {
        it.next();
        ++count;
    }
    return count;
}

QString reconSubmitBody(const QString & source)
{
    return functionBody(source,
        QStringLiteral("bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture("),
        QStringLiteral("bool GpuDisplayWindow::readGpuReconSourceBayer16Texture("));
}

// paintGL() is the last function in GpuDisplayWindow.cpp, so its body runs to end of file.
QString paintGlBody(const QString & source)
{
    const int at = source.indexOf(QStringLiteral("void GpuDisplayWindow::paintGL()"));
    return at < 0 ? QString() : source.mid(at);
}
} // namespace

TEST(GpuWindowPresentInvariants, PresentedSerialIsPromotedInPaintGlAndNowhereElse)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));

    // Exactly one promotion of each field, ever. A second one (for example in a submit
    // path) would mark a frame as presented that no paint has drawn.
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("m_presentedSerial = m_pendingPresentationSerial;")));
    ASSERT_EQ(1, countOccurrences(source,
        QStringLiteral("m_presentedSerialValid = m_pendingPresentationSerialValid;")));

    // Every assignment to the presented fields in the file: m_presentedSerial is written
    // by the promotion and by clearPresented()'s reset; m_presentedSerialValid by those
    // two and by paintGL()'s refused-paint branch. Anything more is a new writer.
    ASSERT_EQ(2, countMatches(source, QStringLiteral("\\bm_presentedSerial\\s*=[^=]")));
    ASSERT_EQ(3, countMatches(source, QStringLiteral("\\bm_presentedSerialValid\\s*=[^=]")));

    const QString body = paintGlBody(source);
    ASSERT_FALSE(body.isEmpty());
    ASSERT_EQ(1, countOccurrences(body, QStringLiteral("m_presentedSerial = m_pendingPresentationSerial;")));
    ASSERT_EQ(1, countOccurrences(body,
        QStringLiteral("m_presentedSerialValid = m_pendingPresentationSerialValid;")));
}

TEST(GpuWindowPresentInvariants, PaintGlPromotesOnlyAfterTheDrawAndNeverOnARefusedPaint)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString body = paintGlBody(source);
    ASSERT_FALSE(body.isEmpty());

    const int refusalGateAt = body.indexOf(QStringLiteral("|| reconRefused )"));
    ASSERT_TRUE(refusalGateAt >= 0);
    const int refusalReturnAt = body.indexOf(QStringLiteral("return;"), refusalGateAt);
    const int drawAt = body.indexOf(QStringLiteral("glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);"));
    const int activeAt = body.indexOf(QStringLiteral("m_texturePresentationActive = true;"), drawAt);
    const int promoteAt = body.indexOf(QStringLiteral("m_presentedSerial = m_pendingPresentationSerial;"));
    const int promoteValidAt = body.indexOf(
        QStringLiteral("m_presentedSerialValid = m_pendingPresentationSerialValid;"));
    ASSERT_TRUE(refusalReturnAt > refusalGateAt);
    ASSERT_TRUE(drawAt > refusalReturnAt);
    ASSERT_TRUE(activeAt > drawAt);
    ASSERT_TRUE(promoteAt > activeAt);
    ASSERT_TRUE(promoteValidAt > promoteAt);

    // The refused/empty paint branch presents nothing: it must invalidate the presented
    // serial (the new-frame counter rejects presentedSerialValid=false swaps) and must not
    // promote the pending serial.
    const QString refusedBranch = body.mid(refusalGateAt, refusalReturnAt - refusalGateAt);
    ASSERT_TRUE(refusedBranch.contains(QStringLiteral("m_texturePresentationActive = false;")));
    ASSERT_TRUE(refusedBranch.contains(QStringLiteral("m_presentedSerialValid = false;")));
    ASSERT_FALSE(refusedBranch.contains(QStringLiteral("m_presentedSerial = ")));
}

TEST(GpuWindowPresentInvariants, SubmitRoutesOnlyStagePendingSerialsAndNeverPromoteThem)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    // A write to either presented field (comments may name them; only assignments count).
    const QString presentedWrite = QStringLiteral("\\bm_presentedSerial(Valid)?\\s*=[^=]");

    const QString reconBody = reconSubmitBody(source);
    ASSERT_FALSE(reconBody.isEmpty());
    ASSERT_TRUE(reconBody.contains(QStringLiteral("m_pendingPresentationSerial = presentationSerial;")));
    ASSERT_TRUE(reconBody.contains(
        QStringLiteral("m_pendingPresentationSerialValid = presentationSerial != 0;")));
    ASSERT_EQ(0, countMatches(reconBody, presentedWrite));

    const QString imageBody = functionBody(source,
        QStringLiteral("void GpuDisplayWindow::setPresentedImage("),
        QStringLiteral("void GpuDisplayWindow::clearPresented()"));
    ASSERT_FALSE(imageBody.isEmpty());
    ASSERT_TRUE(imageBody.contains(QStringLiteral("m_pendingPresentationSerial = presentationSerial;")));
    ASSERT_TRUE(imageBody.contains(
        QStringLiteral("m_pendingPresentationSerialValid = presentationSerial != 0;")));
    ASSERT_EQ(0, countMatches(imageBody, presentedWrite));

    const QString uploadBody = functionBody(source,
        QStringLiteral("void GpuDisplayWindow::updateTextureIfNeeded()"),
        QStringLiteral("void GpuDisplayWindow::paintGL()"));
    ASSERT_FALSE(uploadBody.isEmpty());
    ASSERT_EQ(0, countMatches(uploadBody, presentedWrite));
}

// Fail-closed LUT gates (GPU-TEXNR-S1-DARK-GREEN-1): the recon texture is linear post-WB-undo
// camera RGB, so it must never be drawn without the display LUTs. Three refusals guard it;
// the first two run at submit time, the third re-checks at paint time.

TEST(GpuWindowPresentInvariants, LutGate1SubmitRefusesWhenPreviewProcessingOptionsAreNotUsable)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString body = reconSubmitBody(source);
    ASSERT_FALSE(body.isEmpty());

    const int usableAt = body.indexOf(QStringLiteral("const bool previewProcessingOptionsUsable ="));
    ASSERT_TRUE(usableAt >= 0);
    const QString definition = body.mid(usableAt, 700);
    ASSERT_TRUE(definition.contains(QStringLiteral("previewProcessing.enabled")));
    ASSERT_TRUE(definition.contains(QStringLiteral("previewProcessing.levelsLut.size()")));
    ASSERT_TRUE(definition.contains(QStringLiteral("previewProcessing.matrixLutR.size()")));
    ASSERT_TRUE(definition.contains(QStringLiteral("previewProcessing.matrixLutG.size()")));
    ASSERT_TRUE(definition.contains(QStringLiteral("previewProcessing.matrixLutB.size()")));
    ASSERT_TRUE(definition.contains(QStringLiteral("previewProcessing.gammaLut.size()")));
    // All five LUTs must be held to the full 65536-entry uint16 size.
    ASSERT_EQ(5, countOccurrences(definition,
        QStringLiteral(">= static_cast<int>(65536u * sizeof(uint16_t))")));

    const int gateAt = body.indexOf(QStringLiteral("if ( !previewProcessingOptionsUsable )"));
    ASSERT_TRUE(gateAt > usableAt);
    const QString refusal = body.mid(gateAt, 400);
    ASSERT_TRUE(refusal.contains(QStringLiteral("return fail(")));
    ASSERT_TRUE(refusal.contains(QStringLiteral("trace=gpu_window_recon_missing_processing_options")));

    // "Before any GL work": ahead of context acquisition, the LUT upload and every write to
    // pending state.
    ASSERT_TRUE(body.indexOf(QStringLiteral("QOpenGLContext *glContext = context();")) > gateAt);
    ASSERT_TRUE(body.indexOf(QStringLiteral("gpuPreviewProcessingUpdateLutTextureSet(")) > gateAt);
    ASSERT_TRUE(body.indexOf(QStringLiteral("m_pendingPresentationSerial = presentationSerial;")) > gateAt);
}

TEST(GpuWindowPresentInvariants, LutGate2SubmitRefusesWhenTheLutTextureUploadIsNotReady)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString body = reconSubmitBody(source);
    ASSERT_FALSE(body.isEmpty());

    // PLAYBACK-GL-PRESENT-SETUP-STALL-1: the upload now also reports whether the set was rebuilt
    // (and which group moved) into the present's setup split; same call, same order.
    const int uploadAt = body.indexOf(
        QStringLiteral("gpuPreviewProcessingUpdateLutTextureSet(m_lutSet, previewProcessing, &setupTiming);"));
    ASSERT_TRUE(uploadAt >= 0);
    const int gateAt = body.indexOf(
        QStringLiteral("if ( !gpuPreviewProcessingLutTextureSetReady(m_lutSet, previewProcessing) )"), uploadAt);
    ASSERT_TRUE(gateAt > uploadAt);
    const QString refusal = body.mid(gateAt, 400);
    ASSERT_TRUE(refusal.contains(QStringLiteral("return fail(")));
    ASSERT_TRUE(refusal.contains(QStringLiteral("trace=gpu_window_recon_lut_upload_failed")));

    // The refusal must land before the fate call and every pending-state write, so a
    // refused submit leaves the pending slot (and its serial) untouched.
    ASSERT_TRUE(body.indexOf(
        QStringLiteral("if ( swapTelemetryEnabled() ) noteSupersededBeforePaint(presentationSerial);")) > gateAt);
    ASSERT_TRUE(body.indexOf(QStringLiteral("m_pendingPresentationSerial = presentationSerial;")) > gateAt);
    ASSERT_TRUE(body.indexOf(QStringLiteral("paintGL();")) > gateAt);
}

TEST(GpuWindowPresentInvariants, LutGate3PaintGlRefusesAReconTextureWhoseLutsAreNotReady)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString body = paintGlBody(source);
    ASSERT_FALSE(body.isEmpty());

    const int readyAt = body.indexOf(QStringLiteral("const bool reconLutsReady = presentingReconTexture"));
    ASSERT_TRUE(readyAt >= 0);
    ASSERT_TRUE(body.mid(readyAt, 250).contains(QStringLiteral(
        "gpuPreviewProcessingLutTextureSetReady(m_lutSet, m_reconPresentationOptions.previewProcessing)")));

    const int refusedAt = body.indexOf(QStringLiteral("const bool reconRefused ="));
    ASSERT_TRUE(refusedAt > readyAt);
    ASSERT_TRUE(body.mid(refusedAt, 250).contains(QStringLiteral(
        "gpuPreviewProcessingReconTexturePresentationRefused(\n"
        "        presentingReconTexture, m_lutSet, m_reconPresentationOptions.previewProcessing)")));

    // The refusal is part of the early-return condition that precedes the draw, and the
    // recon route draws through the preview-processing program, never the passthrough one.
    const int gateAt = body.indexOf(
        QStringLiteral("if ( !m_texture || !activeProgram || width() <= 0 || height() <= 0 || reconRefused )"));
    ASSERT_TRUE(gateAt > refusedAt);
    ASSERT_TRUE(body.indexOf(QStringLiteral("glDrawArrays(GL_TRIANGLE_STRIP, 0, 4);")) > gateAt);
    ASSERT_TRUE(body.contains(QStringLiteral(
        "presentingReconTexture ? m_previewProcessingProgram : m_program")));
    ASSERT_TRUE(body.contains(QStringLiteral(
        "displayUniforms, reconLutsReady);")));
}

// PLAYBACK-GL-PRESENT-SETUP-STALL-1 (row R3, T4): the HEAVY pace legs charged every >= 50 ms texture-present
// setup to the glGetError drain in front of the per-frame shadows/highlights blur upload (the GUI thread waits
// there for the driver thread). The steady-state upload -- into a texture an earlier call created at this exact
// size -- must take no glGetError; the (re)allocation upload keeps its drain and its post-upload check.
TEST(GpuPresentSetupStall, SteadyStateBlurUploadTakesNoGlGetErrorButTheAllocationUploadIsDrainedAndChecked)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuPreviewProcessing.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("bool gpuPreviewProcessingUpdateShadowsHighlightsBlurTexture("),
        QStringLiteral("void gpuPreviewProcessingBindDisplayUniformsAndTextures("));
    ASSERT_FALSE(body.isEmpty());
    const QString glGetErrorCall = QStringLiteral("gl->glGetError()");
    const QString upload = QStringLiteral(
        "set.shadowsHighlightsBlur->setData(QOpenGLTexture::RGBA, QOpenGLTexture::UInt16, packed.constData());");

    // A (re)allocation marks the call; the steady-state branch follows it and returns on its own.
    const int allocatedAt = body.indexOf(QStringLiteral("allocatedThisCall = true;"));
    ASSERT_TRUE(allocatedAt >= 0);
    const int steadyAt = body.indexOf(QStringLiteral("if ( !allocatedThisCall )"));
    ASSERT_TRUE(steadyAt > allocatedAt);
    const int steadyReturnAt = body.indexOf(QStringLiteral("return true;"), steadyAt);
    ASSERT_TRUE(steadyReturnAt > steadyAt);
    const QString steady = body.mid(steadyAt, steadyReturnAt - steadyAt);
    ASSERT_EQ(1, countOccurrences(steady, upload));
    ASSERT_TRUE(steady.contains(QStringLiteral("set.shadowsHighlightsBlurReady = true;")));
    ASSERT_EQ(0, countOccurrences(steady, glGetErrorCall));
    ASSERT_EQ(0, countOccurrences(steady, QStringLiteral("glGetError(")));

    // Nothing before the steady branch syncs either.
    ASSERT_EQ(0, countOccurrences(body.left(steadyAt), glGetErrorCall));

    // The allocation upload: drain, upload, check, in that order.
    const QString allocation = body.mid(steadyReturnAt);
    ASSERT_EQ(2, countOccurrences(allocation, glGetErrorCall));
    const int drainAt = allocation.indexOf(glGetErrorCall);
    const int allocationUploadAt = allocation.indexOf(upload);
    const int checkAt = allocation.indexOf(glGetErrorCall, drainAt + glGetErrorCall.size());
    ASSERT_TRUE(drainAt >= 0);
    ASSERT_TRUE(allocationUploadAt > drainAt);
    ASSERT_TRUE(checkAt > allocationUploadAt);
    ASSERT_TRUE(allocation.mid(checkAt).contains(QStringLiteral("return failClosed();")));
}

// PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r2 (Dot 6d0e710f B2, CoS 6219217683): the --visual-capture-dir forced paint
// (paintPendingNowIfActive above, or the viewport's repaint()) draws the frame the label names even when the
// display's own deferred paint never lands, so a forced capture alone can never show a frozen display. Every forced
// capture kind is therefore paired with an unforced "_unbound" twin, and only the "_unbound" kinds skip the paint.
// 32626bd5 force-painted every play capture with no twin. The stop and paused twins are taken just before their forced
// capture, with no event handled in between; the play twins are ordered by the r3 test below.
TEST(VisualCaptureBinding, EveryForcedStopAndPausedCaptureIsPairedWithAnUnforcedCaptureTakenJustBeforeIt)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int blockAt = source.indexOf(QStringLiteral("if( !options.visualCaptureDir.isEmpty() && ui->actionPlay->isChecked() )"));
    const int blockEnd = source.indexOf(QStringLiteral("// (4) files:"), blockAt);
    ASSERT_TRUE(blockAt >= 0 && blockEnd > blockAt);
    const QString block = source.mid(blockAt, blockEnd - blockAt);

    // Only the "_unbound" kinds skip the forced paint.
    ASSERT_TRUE(block.contains(QStringLiteral("!kind.endsWith( QStringLiteral(\"_unbound\") )")));

    const QString pairs[][2] = {
        { QStringLiteral("grabVisual( QStringLiteral(\"stop_unbound\"), 0, false );"),
          QStringLiteral("grabVisual( QStringLiteral(\"stop\"), 0, false );") },
        { QStringLiteral("grabVisual( QStringLiteral(\"paused_unbound\"), 0, visualSettled );"),
          QStringLiteral("grabVisual( QStringLiteral(\"paused\"), 0, visualSettled );") },
    };
    for (const auto & pair : pairs) {
        ASSERT_EQ(1, countOccurrences(block, pair[0]));
        ASSERT_EQ(1, countOccurrences(block, pair[1]));
        const int unforcedAt = block.indexOf(pair[0]);
        const int forcedAt = block.indexOf(pair[1]);
        ASSERT_TRUE(unforcedAt >= 0 && forcedAt > unforcedAt);
        ASSERT_FALSE(block.mid(unforcedAt, forcedAt - unforcedAt).contains(QStringLiteral("processEvents(")));
    }
    // No forced kind exists without its twin: three forced grabs, three unforced, nothing else.
    ASSERT_EQ(6, countOccurrences(block, QStringLiteral("grabVisual( QStringLiteral(")));
}

// PLAYBACK-CPU-VISUAL-CAPTURE-TIMING-2 r3 (Dot 7dc103f1 B2): r2 took unforced play k and then forced play k in each
// slice. A forced paint stays on screen when the display's own deferred paint never lands, so unforced k+1 showed
// forced k (trace initial,F0..F4): six moving pictures over a frozen display. Every forced play capture now comes
// before the first unforced one, no forced capture falls between two unforced play captures or between the last of
// them and stop_unbound, and every capture records how many forced paints preceded it (forced_paints_before) so the
// runner judges only an interval no forced paint entered.
TEST(VisualCaptureBinding, NoForcedPaintFallsInsideTheUnforcedPlayObservationInterval)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int blockAt = source.indexOf(QStringLiteral("if( !options.visualCaptureDir.isEmpty() && ui->actionPlay->isChecked() )"));
    const int blockEnd = source.indexOf(QStringLiteral("// (4) files:"), blockAt);
    ASSERT_TRUE(blockAt >= 0 && blockEnd > blockAt);
    const QString block = source.mid(blockAt, blockEnd - blockAt);

    const QString forcedPlay = QStringLiteral("grabVisual( QStringLiteral(\"play\"), k, false );");
    const QString unforcedPlay = QStringLiteral("grabVisual( QStringLiteral(\"play_unbound\"), k, false );");
    const QString stopUnforced = QStringLiteral("grabVisual( QStringLiteral(\"stop_unbound\"), 0, false );");
    ASSERT_EQ(1, countOccurrences(block, forcedPlay));
    ASSERT_EQ(1, countOccurrences(block, unforcedPlay));
    const int forcedAt = block.indexOf(forcedPlay);
    const int unforcedAt = block.indexOf(unforcedPlay);
    const int stopUnforcedAt = block.indexOf(stopUnforced);
    ASSERT_TRUE(forcedAt >= 0 && unforcedAt > forcedAt && stopUnforcedAt > unforcedAt);
    // The two play captures sit in two separate loops: the forced loop closes before the unforced one opens.
    const QString forcedToUnforced = block.mid(forcedAt, unforcedAt - forcedAt);
    ASSERT_EQ(1, countOccurrences(forcedToUnforced, QStringLiteral("for( int k = 0; k < visualPlayFrames; ++k )")));
    // From the unforced play capture to stop_unbound: no other capture, only the pause.
    const QString interval = block.mid(unforcedAt, stopUnforcedAt - unforcedAt);
    ASSERT_EQ(1, countOccurrences(interval, QStringLiteral("grabVisual(")));
    ASSERT_TRUE(interval.contains(QStringLiteral("ui->actionPlay->setChecked( false );")));

    // The ordinal is read before this capture's own paint and advanced only by a forced capture; visual.json carries it.
    const int lambdaAt = block.indexOf(QStringLiteral("auto grabVisual = [&]"));
    const int lambdaEnd = block.indexOf(QStringLiteral("visualGrabs.append( grab );"), lambdaAt);
    ASSERT_TRUE(lambdaAt >= 0 && lambdaEnd > lambdaAt);
    const QString lambda = block.mid(lambdaAt, lambdaEnd - lambdaAt);
    const int ordinalAt = lambda.indexOf(QStringLiteral("grab.forcedPaintsBefore = visualForcedPaints;"));
    const int paintAt = lambda.indexOf(QStringLiteral("paintPresentedFrameForCapture("));
    const int bumpAt = lambda.indexOf(QStringLiteral("if( forcePaint ) ++visualForcedPaints;"));
    ASSERT_TRUE(ordinalAt >= 0 && paintAt > ordinalAt && bumpAt > paintAt);
    ASSERT_EQ(1, countOccurrences(block, QStringLiteral("++visualForcedPaints")));
    ASSERT_TRUE(source.contains(QStringLiteral(
        "frameJson.insert( QStringLiteral(\"forced_paints_before\"), grab.forcedPaintsBefore );")));
}

TEST(VisualCaptureBinding, EveryCaptureNamesWhereItsPaintedSerialCameFrom)
{
    // Dot improvement on 60541263: on the pixmap route painted_serial == serial holds by construction, so the
    // receipt says so (label_bound_basis) instead of passing it off as an independent paint-path record.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("frameJson.insert( QStringLiteral(\"label_bound_basis\"), grab.labelBoundBasis );")));
    const int helperAt = source.indexOf(QStringLiteral("static bool paintPresentedFrameForCapture("));
    ASSERT_TRUE(helperAt >= 0);
    const QString helper = source.mid(helperAt, 2200);
    const int windowAt = helper.indexOf(QStringLiteral("basis = QStringLiteral(\"gl_window_paint\");"));
    const int viewportAt = helper.indexOf(QStringLiteral("basis = QStringLiteral(\"gl_viewport_paint\");"));
    const int pixmapAt = helper.indexOf(QStringLiteral("basis = QStringLiteral(\"pixmap_item_by_construction\");"));
    ASSERT_TRUE(windowAt >= 0 && viewportAt > windowAt && pixmapAt > viewportAt);
    // the pixmap-route basis is set only beside the item's own serial, never for a GL route
    ASSERT_TRUE(helper.lastIndexOf(QStringLiteral("serial = pixmapItemSerial;"), pixmapAt) > viewportAt);
}

// PLAYBACK-PRESENT-TAIL-TIMING-1 (T9): the tail markers around the path-3 paint/swap and at the next setup start are
// CPU clock reads and POD stores only. A GL call there (an error drain, a finish, a fence) would itself move the stall
// the markers bucket; an allocation would put a per-frame cost on the GUI thread.
namespace
{
QString regionBetween(const QString & source, const QString & from, const QString & to)
{
    const int at = source.indexOf(from);
    const int end = at < 0 ? -1 : source.indexOf(to, at);
    if (at < 0 || end <= at) return QString();
    return source.mid(at, end + to.size() - at);
}

void assertNoGlSyncAndNoAllocation(const QString & region)
{
    ASSERT_FALSE(region.isEmpty());
    for (const char * call : { "glGetError", "glFinish", "glFlush", "glFenceSync", "glClientWaitSync", "glBeginQuery" })
        ASSERT_EQ(0, countOccurrences(region, QString::fromLatin1(call)));
    for (const char * token : { "new ", "QString", "QStringLiteral", "QByteArray", "push_back", "append(", "std::vector" })
        ASSERT_EQ(0, countOccurrences(region, QString::fromLatin1(token)));
}

int glCallCount(const QString & region)
{
    return region.count(QRegularExpression(QStringLiteral("\\bgl[A-Z]\\w*\\s*\\(")));
}
} // namespace

TEST(GpuPresentTailTiming, TailMarkersTakeNoGlCallAndNoAllocation)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/GpuDisplayWindow.cpp"));
    const QString body = functionBody(source,
        QStringLiteral("bool GpuDisplayWindow::setPresentedGpuPlaybackReconAmazePostWbTexture("),
        QStringLiteral("bool GpuDisplayWindow::readGpuReconSourceBayer16Texture("));
    ASSERT_FALSE(body.isEmpty());
    const QString setupStamp = QStringLiteral("const qint64 tailSetupStartNs = m_tailClock.nsecsElapsed();");
    const QString paintStamp = QStringLiteral("const double postPaintStartMs = elapsedMs();");
    const QString swapStamp = QStringLiteral("m_tailSwapReturnNs = m_tailClock.nsecsElapsed();");
    const QString swapCall = QStringLiteral("glContext->swapBuffers(this);");
    const QString swapCallMsLine = QStringLiteral("const double swapCallMs = elapsedMs() - swapCallStartMs;");
    ASSERT_EQ(1, countOccurrences(body, setupStamp));
    ASSERT_EQ(1, countOccurrences(body, paintStamp));
    ASSERT_EQ(1, countOccurrences(body, swapStamp));

    // r1: post_paint stamp -> swap-return stamp. Only paintGL() and the path-3 swap are GL-ish.
    const QString r1 = regionBetween(body, paintStamp, swapStamp);
    assertNoGlSyncAndNoAllocation(r1);
    ASSERT_EQ(1, countOccurrences(r1, QStringLiteral("paintGL();")));
    ASSERT_EQ(1, countOccurrences(r1, swapCall));
    QString r1Rest = r1;
    r1Rest.remove(QStringLiteral("paintGL();")).remove(swapCall);
    ASSERT_EQ(0, r1Rest.count(QRegularExpression(QStringLiteral("gl\\w*\\s*\\("),
                                                 QRegularExpression::CaseInsensitiveOption)));
    // The swap-return stamp follows the swap with only the swapCallMs line between.
    const int swapAt = r1.indexOf(swapCall);
    const int stampAt = r1.indexOf(swapStamp);
    ASSERT_TRUE(swapAt >= 0 && stampAt > swapAt);
    ASSERT_EQ(swapCallMsLine.toStdString(),
              r1.mid(swapAt + swapCall.size(), stampAt - swapAt - swapCall.size()).trimmed().toStdString());

    // r2: the setup-start tail block, after setupStartMs and before ensurePreviewProcessingProgram().
    const int setupStartAt = body.indexOf(QStringLiteral("const double setupStartMs = elapsedMs();"));
    const int ensureAt = body.indexOf(QStringLiteral("ensurePreviewProcessingProgram();"), setupStartAt);
    const int setupStampAt = body.indexOf(setupStamp);
    ASSERT_TRUE(setupStartAt >= 0 && setupStampAt > setupStartAt && ensureAt > setupStampAt);
    const QString r2 = body.mid(setupStampAt, ensureAt - setupStampAt);
    ASSERT_TRUE(r2.contains(QStringLiteral("m_tailSwapReturnNs = -1;")));
    ASSERT_TRUE(r2.contains(QStringLiteral("m_tailBlockStartNs = -1;")));
    // Adversary A2: a block still open at setup start never reported its wake, so its span is not idle.
    ASSERT_TRUE(r2.contains(QStringLiteral("tail.gui_idle_known = m_tailIdleProbe && m_tailBlockStartNs < 0;")));
    ASSERT_TRUE(r2.contains(QStringLiteral("tail.gui_idle_ms = static_cast<double>(m_tailIdleNs) / 1000000.0;")));
    ASSERT_EQ(0, countOccurrences(r2, QStringLiteral("- m_tailBlockStartNs")));

    // Adversary A2 sibling and ADV-A1: only the dispatcher whose awake follows the wait before any dispatch gets a probe;
    // on every other one a wake's events run before the next awake, so the idle stays not known.
    const int dispatcherGateAt = source.indexOf(QStringLiteral("if ( dispatcher && dispatcher->inherits(\"QEventDispatcherWin32\") )"));
    ASSERT_TRUE(dispatcherGateAt >= 0);
    ASSERT_TRUE(source.indexOf(QStringLiteral("&QAbstractEventDispatcher::aboutToBlock")) > dispatcherGateAt);
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("&QAbstractEventDispatcher::aboutToBlock")));
    ASSERT_EQ(1, countOccurrences(source, QStringLiteral("&QAbstractEventDispatcher::awake")));

    // Adversary A1: frameSwapped slots run in connection order, so the path-1 pair clock is read before the recorder
    // can log; connected after it, a per-swap log line is timed as paint+swap.
    const int pairSlotAt = source.indexOf(QStringLiteral("&GpuDisplayWindow::noteQtPaintSwap);"));
    const int recorderSlotAt = source.indexOf(QStringLiteral("&GpuDisplayWindow::noteRealSwap);"));
    ASSERT_TRUE(pairSlotAt >= 0 && recorderSlotAt > pairSlotAt);

    // r3-r5: the path-1 paint stamp, its frameSwapped slot and the dispatcher idle lambdas.
    const QString r3 = functionBody(source, QStringLiteral("void GpuDisplayWindow::paintUnderGL()"),
                                    QStringLiteral("void GpuDisplayWindow::noteQtPaintSwap()"));
    ASSERT_TRUE(r3.contains(QStringLiteral("if ( swapTelemetryEnabled() ) m_qtPaintStartNs = m_tailClock.nsecsElapsed();")));
    const QString r4 = functionBody(source, QStringLiteral("void GpuDisplayWindow::noteQtPaintSwap()"),
                                    QStringLiteral("GpuDisplayWindow::~GpuDisplayWindow()"));
    const QString r5 = regionBetween(source, QStringLiteral("&QAbstractEventDispatcher::aboutToBlock"),
                                     QStringLiteral("m_tailIdleProbe = true;"));
    ASSERT_TRUE(r5.contains(QStringLiteral("&QAbstractEventDispatcher::awake")));
    for (const QString & region : { r2, r3, r4, r5 }) {
        assertNoGlSyncAndNoAllocation(region);
        ASSERT_EQ(0, glCallCount(region));
    }

    const QString header = readRepoFile(QStringLiteral("platform/qt/GpuPreviewProcessing.h"));
    ASSERT_TRUE(header.contains(QStringLiteral(
        "static_assert(std::is_trivially_copyable<GpuPresentTailTiming>::value")));
}

// PLAYBACK-PRESENT-TAIL-TIMING-1 (W1): slip_summary appends the max-setup tail keys after present_setup_first_ms (the
// existing keys keep their order), and no tail field rides the per-frame stageTimingTelemetry hash.
TEST(GpuPresentTailTiming, SlipSummaryAppendsTheTailKeysAndNoTailFieldUsesStageTimingTelemetry)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const int firstAt = source.indexOf(QStringLiteral("QStringLiteral(\"present_setup_first_ms=%1\")"));
    ASSERT_TRUE(firstAt >= 0);
    int previousAt = firstAt;
    for (const char * key : { "present_setup_max_prev_paint_ms=", "present_setup_max_prev_swap_ms=",
                              "present_setup_max_swap_return_to_setup_ms=", "present_setup_max_gui_busy_ms=",
                              "present_setup_max_qt_paint_swaps=", "present_setup_max_qt_paint_swap_max_ms=",
                              "present_setup_max_grab_swaps=", "present_setup_max_display=",
                              "present_setup_max_bucket=" }) {
        const QString needle = QStringLiteral("QStringLiteral(\"%1").arg(QLatin1String(key));
        ASSERT_EQ(1, countOccurrences(source, needle));
        const int at = source.indexOf(needle);
        ASSERT_TRUE(at > previousAt);
        previousAt = at;
    }
    ASSERT_TRUE(source.indexOf(QStringLiteral("qInfo().noquote() << fields.join( QLatin1Char(' ') );"), firstAt)
                > previousAt);

    const QRegularExpression insertKey(QStringLiteral(
        "stageTimingTelemetry\\.insert\\(\\s*QStringLiteral\\(\"([^\"]*)\""));
    const QRegularExpression tailToken(QStringLiteral(
        "post_paint|post_swap|prev_paint|prev_swap|prev_post|swap_return|qt_paint|grab_swap|gui_idle|gui_busy|"
        "setup_bucket|present_tail|tail_timing"));
    int keys = 0;
    for (auto it = insertKey.globalMatch(source); it.hasNext(); ++keys)
        ASSERT_FALSE(tailToken.match(it.next().captured(1)).hasMatch());
    ASSERT_TRUE(keys > 100);
}
