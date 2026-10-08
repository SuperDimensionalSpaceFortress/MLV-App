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
