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

