// Wiring/census test for CUDA-PLAYBACK-CONTACT-SHEET-1's --contact-sheet-dir/
// --contact-sheet-frames option: pins (1) the option defaults to OFF and the capture block is
// gated on both fields being set, so an off run performs zero grabs and zero readbacks, (2) the
// capture block runs strictly AFTER playback is stopped (ui->actionPlay->setChecked( false ))
// and its idle-drain wait, which is also strictly after the timed while() loop that drives
// fps/frame-count measurement closes -- so N contact-sheet grabs never land inside the measured
// playback interval or perturb its telemetry, and (3) the CLI wires --contact-sheet-dir/
// --contact-sheet-frames together (main.cpp) with the option struct's defaults reproduced there.
// MainWindow.cpp/main.cpp need a full GUI build (not linked into console_tests), so this test
// reads the sources as text, mirroring test_gpu_window_swap_wiring.cpp's and
// test_playback_smoke_foreground_wiring.cpp's approach for the same reason.
#include "../common/minitest.h"
#include "../common/repo_paths.h"

#include "../../platform/qt/OutputDirIdentity.h"

#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QProcess>
#include <QString>
#include <QTemporaryDir>
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

QString sliceBetween(const QString & source, const QString & fromMarker, const QString & toMarker)
{
    const int at = source.indexOf(fromMarker);
    if (at < 0) return QString();
    const int next = source.indexOf(toMarker, at);
    if (next <= at) return QString();
    return source.mid(at, next - at);
}

} // namespace

TEST(ContactSheetCaptureWiring, OptionStructDefaultsToOff)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const QString optionsStruct = sliceBetween(header,
        QStringLiteral("struct GuiPlaybackSmokeOptions"),
        QStringLiteral("int runHeadlessPlaybackProfile"));
    ASSERT_FALSE(optionsStruct.isEmpty());
    ASSERT_TRUE(optionsStruct.contains(QStringLiteral("QString contactSheetDir;")));
    ASSERT_TRUE(optionsStruct.contains(QStringLiteral("int contactSheetFrames = 0;")));
}

TEST(ContactSheetCaptureWiring, CliDefaultsToOffAndRequiresBothFlagsTogether)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("QStringLiteral(\"contact-sheet-dir\")")));
    ASSERT_TRUE(source.contains(QStringLiteral("QStringLiteral(\"contact-sheet-frames\")")));
    // The frames option's own declared default is "0" (off); a caller who never passes either
    // flag gets an options.contactSheetFrames of 0 and an empty options.contactSheetDir.
    const QString framesOptBlock = sliceBetween(source,
        QStringLiteral("contactSheetFramesOpt("),
        QStringLiteral("parser.addOption(contactSheetFramesOpt)"));
    ASSERT_FALSE(framesOptBlock.isEmpty());
    ASSERT_TRUE(framesOptBlock.contains(QStringLiteral("QStringLiteral(\"0\")")));

    // Both-or-neither: -dir set XOR frames > 0 is refused before either option struct field is
    // ever populated, so a caller can never end up with a directory and no frame count or vice
    // versa reaching runGuiPlaybackSmoke().
    ASSERT_TRUE(source.contains(
        QStringLiteral("if (contactSheetDirSet != (contactSheetFrames > 0))")));
}

TEST(ContactSheetCaptureWiring, CaptureBlockIsGatedOnBothDirAndFramesBeingSet)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )")));
    // The per-frame grab loop is bounded by options.contactSheetFrames, and nowhere else in the
    // capture block hard-codes a frame count -- an off run (frames == 0) executes the loop body
    // zero times, performing zero grabPresentedFramebufferIfActive() readbacks.
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("for( int i = 0; i < options.contactSheetFrames; ++i )")));
    const int guardAt = smokeBody.indexOf(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )"));
    const int loopAt = smokeBody.indexOf(
        QStringLiteral("for( int i = 0; i < options.contactSheetFrames; ++i )"), guardAt);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(loopAt > guardAt);
}

TEST(ContactSheetCaptureWiring, FileWritingRunsStrictlyAfterPlaybackStopsAndAfterTheTimedLoopCloses)
{
    // CONTACT-SHEET-PLAYBACK-PARITY-1: the in-pass GRABS happen inside the measured interval (that
    // is the point: they are what played), but every PNG encode, sidecar write and the seek-mode
    // capture still run in the block below, strictly after the measured Play is stopped.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int timedLoopAt = smokeBody.indexOf(
        QStringLiteral("for( ;; )"),
        smokeBody.indexOf(QStringLiteral("playback_frame_range::PlayStopState measuredState")));
    const int stopPlaybackAt = smokeBody.indexOf(QStringLiteral("ui->actionPlay->setChecked( false );"));
    const int idleDrainAt = smokeBody.indexOf(
        QStringLiteral("for( int attempt = 0; attempt < 400 && m_pRenderThread && !m_pRenderThread->isIdle(); ++attempt )"),
        stopPlaybackAt);
    const int captureGuardAt = smokeBody.indexOf(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )"));
    const int doneLineAt = smokeBody.indexOf(QStringLiteral("out << \"[GUI-SMOKE] DONE"));

    ASSERT_TRUE(timedLoopAt >= 0);
    ASSERT_TRUE(stopPlaybackAt > timedLoopAt);
    ASSERT_TRUE(idleDrainAt > stopPlaybackAt);
    ASSERT_TRUE(captureGuardAt > idleDrainAt);
    ASSERT_TRUE(doneLineAt > captureGuardAt);
    // Every PNG encode in the smoke body sits in that block.
    const int firstSaveAt = smokeBody.indexOf(QStringLiteral(".save( pngPath, \"PNG\" )"));
    ASSERT_TRUE(firstSaveAt > captureGuardAt);

    // finishPlaybackSmokeTelemetry() runs synchronously inside on_actionPlay_toggled(false),
    // which setChecked(false) above triggers before the write block, so the fps/swap-cadence
    // session is closed before any file is written.
    const QString togglePlayBody = sliceBetween(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("void MainWindow::on_actionShowZebras_triggered()"));
    ASSERT_FALSE(togglePlayBody.isEmpty());
    ASSERT_TRUE(togglePlayBody.contains(QStringLiteral("finishPlaybackSmokeTelemetry( \"play-stop\" );")));
    const int checkedFalseBranchAt = togglePlayBody.indexOf(QStringLiteral("if( !checked )"));
    const int finishCallAt = togglePlayBody.indexOf(
        QStringLiteral("finishPlaybackSmokeTelemetry( \"play-stop\" );"), checkedFalseBranchAt);
    ASSERT_TRUE(checkedFalseBranchAt >= 0);
    ASSERT_TRUE(finishCallAt > checkedFalseBranchAt);
}

TEST(ContactSheetCaptureWiring, EachSidecarCarriesTheFieldsTheComposerAndOwnerNeed)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    static const char * kRequiredFields[] = {
        "index", "captured_utc", "serial", "display_frame", "elapsed_ms",
        "texture_source", "path", "look_assist_enabled", "look_assist_scene",
        "look_assist_exposure", "look_assist_contrast", "look_assist_pivot",
        "look_assist_temperature", "look_assist_tint", "settled", "saved",
        // LOOK-ASSIST-SCENE-CLASSIFY-1 r1c: where the balance came from, so a sheet can assert it.
        "look_assist_wb_source", "look_assist_wb_decision",
    };
    for (const char * field : kRequiredFields) {
        const QString needle = QStringLiteral("QStringLiteral(\"%1\")").arg(QString::fromLatin1(field));
        ASSERT_TRUE(smokeBody.contains(needle));
    }
}

TEST(ContactSheetCaptureWiring, SeekModeDefaultsToOffSoPlaybackPassCaptureIsTheDefault)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const QString optionsStruct = sliceBetween(header,
        QStringLiteral("struct GuiPlaybackSmokeOptions"),
        QStringLiteral("int runHeadlessPlaybackProfile"));
    ASSERT_FALSE(optionsStruct.isEmpty());
    ASSERT_TRUE(optionsStruct.contains(QStringLiteral("bool contactSheetSeekMode = false;")));
}

TEST(ContactSheetCaptureWiring, CliWiresTheSeekModeFlag)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(source.contains(QStringLiteral("QStringLiteral(\"contact-sheet-seek-mode\")")));
    ASSERT_TRUE(source.contains(
        QStringLiteral("options.contactSheetSeekMode = parser.isSet(contactSheetSeekModeOpt);")));
}

// --- CONTACT-SHEET-PLAYBACK-PARITY-1: the default sheet is captured DURING the measured Play ----

static QString smokeBodyOf(const QString & source)
{
    return sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
}

static const char * kInPassArmMarker =
    "// CONTACT-SHEET-PLAYBACK-PARITY-1: arm the in-pass contact sheet on THIS measured Play";
static const char * kInPassWriteMarker =
    "// CONTACT-SHEET-PLAYBACK-PARITY-1: write the in-pass grabs";
static const char * kSeekMarker =
    "// CONTACT-SHEET-PLAYBACK-PARITY-1: seek capture (explicit, labelled alternative)";

TEST(ContactSheetCaptureWiring, InPassCaptureIsArmedOnTheMeasuredPlayBeforeTheTimedLoop)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    ASSERT_FALSE(smokeBody.isEmpty());

    const int measuredPlayAt = smokeBody.indexOf(QStringLiteral("programmaticPlay( \"gui-smoke-measured\""));
    const int clockRestartAt = smokeBody.indexOf(QStringLiteral("playbackClock.restart();"), measuredPlayAt);
    const int armAt = smokeBody.indexOf(QString::fromLatin1(kInPassArmMarker));
    const int timedLoopAt = smokeBody.indexOf(
        QStringLiteral("for( ;; )"),
        smokeBody.indexOf(QStringLiteral("playback_frame_range::PlayStopState measuredState")));
    ASSERT_TRUE(measuredPlayAt >= 0);
    ASSERT_TRUE(clockRestartAt > measuredPlayAt);
    ASSERT_TRUE(armAt > clockRestartAt);
    ASSERT_TRUE(timedLoopAt > armAt);

    const QString armEnd = QStringLiteral("m_contactSheetCaptureActive = true;");
    const int armEndAt = smokeBody.indexOf(armEnd, armAt);
    ASSERT_TRUE(armEndAt > armAt);
    ASSERT_TRUE(timedLoopAt > armEndAt);
    const QString armBlock = smokeBody.mid(armAt, armEndAt + armEnd.size() - armAt);
    ASSERT_TRUE(armBlock.contains(QStringLiteral(
        "if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 && !options.contactSheetSeekMode )")));
    ASSERT_TRUE(armBlock.contains(QStringLiteral("playback_frame_range::contactSheetInPassTargetMs(")));
    ASSERT_TRUE(armBlock.contains(QStringLiteral("m_contactSheetCaptureClock.start();")));
    ASSERT_TRUE(armBlock.contains(QStringLiteral("m_contactSheetCaptureActive = true;")));
    // Arming never plays anything itself.
    ASSERT_FALSE(armBlock.contains(QStringLiteral("programmaticPlay(")));
    ASSERT_FALSE(armBlock.contains(QStringLiteral("actionPlay")));
}

TEST(ContactSheetCaptureWiring, InPassSheetAddsNoReplayAndNoSecondPlay)
{
    // Owner rule 2026-09-30: never replay. The ONE programmatic Play in the smoke body is the
    // measured one; the old replay pass, its restart machinery and its refusal are all gone.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    ASSERT_FALSE(smokeBody.isEmpty());
    ASSERT_EQ(1, smokeBody.count(QStringLiteral("programmaticPlay(")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("REPLAY_REFUSED: the playback-mode contact sheet")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("contactSheetMaxRestarts")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("gui-smoke-contact-sheet-restart")));

    const QString writeBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kInPassWriteMarker), QString::fromLatin1(kSeekMarker));
    ASSERT_FALSE(writeBranch.isEmpty());
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("actionPlay")));
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("actionLoop")));
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("seekAndSettleLoadedClip(")));
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("grabPresentedFramebufferIfActive(")));   // writes, never grabs
}

TEST(ContactSheetCaptureWiring, InPassHookOnlyReadsBackAndTimesTheGrabInsideTheMeasuredInterval)
{
    // The hook runs on the GUI thread inside the measured interval. It may only do the readback
    // (timed) and keep the image; PNG encode and file I/O wait until the Play is stopped.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(QStringLiteral("m_contactSheetCaptureClock.elapsed()")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("m_contactSheetCaptureTargetMs")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("QElapsedTimer grabTimer;")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("QStringLiteral(\"grab_ms\")")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("m_contactSheetPendingGrabs.append(")));
    ASSERT_FALSE(hookBody.contains(QStringLiteral(".save(")));
    ASSERT_FALSE(hookBody.contains(QStringLiteral("QFile")));
    ASSERT_FALSE(hookBody.contains(QStringLiteral("toJson(")));
}

// VENUE-LEG-AGXOFF-UNPACED-HEADROOM-1 r2 (sol B1): the hook decides through the ONE predicate the header tests execute
// (test_playback_frame_range.cpp), keyed to source frames exactly when the run is unpaced the way the pace guard and
// the pace_unpaced disclosure define it; each sidecar records which basis made it due; and a short sheet still exits 14.
TEST(ContactSheetCaptureWiring, UnpacedInPassTargetsAreSourceFramesThroughTheSharedPredicateAndAShortSheetStillExits14)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    ASSERT_FALSE(smokeBody.isEmpty());
    const int armAt = smokeBody.indexOf(QString::fromLatin1(kInPassArmMarker));
    const int armEndAt = smokeBody.indexOf(QStringLiteral("m_contactSheetCaptureActive = true;"), armAt);
    ASSERT_TRUE(armAt >= 0 && armEndAt > armAt);
    const QString armBlock = smokeBody.mid(armAt, armEndAt - armAt);
    ASSERT_TRUE(armBlock.contains(QStringLiteral(
        "m_contactSheetCaptureUnpaced = m_automationPacingIsolated && mlvappPlaybackUnpacedRequested();")));
    ASSERT_TRUE(armBlock.contains(QStringLiteral("playback_frame_range::contactSheetInPassTargetSourceFrames(")));
    ASSERT_TRUE(armBlock.contains(QStringLiteral("m_playRequiredSourceFrames")));

    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(QStringLiteral("playback_frame_range::contactSheetInPassTargetDue(")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("m_sourceAdvance.consumed()")));
    ASSERT_FALSE(hookBody.contains(QStringLiteral("if( grabElapsedMs < m_contactSheetCaptureTargetMs")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("QStringLiteral(\"target_basis\")")));
    ASSERT_TRUE(hookBody.contains(QStringLiteral("QStringLiteral(\"source_frames\") : QStringLiteral(\"wall_clock\")")));

    ASSERT_TRUE(smokeBody.contains(QStringLiteral("in-pass targets were presented before the measured Play stopped")));
    const int incompleteAt = smokeBody.indexOf(QStringLiteral("contact sheet capture incomplete; requested="));
    ASSERT_TRUE(incompleteAt >= 0);
    const int returnAt = smokeBody.indexOf(QStringLiteral("return "), incompleteAt);
    ASSERT_TRUE(returnAt > incompleteAt);
    ASSERT_EQ(returnAt, smokeBody.indexOf(QStringLiteral("return 14;"), incompleteAt));
}

TEST(ContactSheetCaptureWiring, InPassWriterRecordsTheGrabCostAndWhetherEachGrabFellInTheMeasuredInterval)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    const QString writeBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kInPassWriteMarker), QString::fromLatin1(kSeekMarker));
    ASSERT_FALSE(writeBranch.isEmpty());
    ASSERT_TRUE(writeBranch.contains(QStringLiteral(".save( pngPath, \"PNG\" )")));
    ASSERT_TRUE(writeBranch.contains(QStringLiteral("QStringLiteral(\"grab_in_measured_interval\")")));
    ASSERT_TRUE(writeBranch.contains(QStringLiteral("playback_frame_range::fpsExcludingGrabCost(")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("grab_total_ms=")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("grabs_in_measured_interval=")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("fps_excluding_grabs=")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("playback_path=%")));
}

TEST(ContactSheetCaptureWiring, PairedSeekSheetIsOptInWritesItsOwnDirAndIsLabelledNotPlayback)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const QString optionsStruct = sliceBetween(header,
        QStringLiteral("struct GuiPlaybackSmokeOptions"),
        QStringLiteral("int runHeadlessPlaybackProfile"));
    ASSERT_TRUE(optionsStruct.contains(QStringLiteral("QString contactSheetSeekDir;")));
    const QString mainSource = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(mainSource.contains(QStringLiteral("QStringLiteral(\"contact-sheet-seek-dir\")")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker), QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(QStringLiteral("options.contactSheetSeekMode ? options.contactSheetDir : options.contactSheetSeekDir")));
    ASSERT_TRUE(seekBranch.contains(QStringLiteral("frameJson.insert( QStringLiteral(\"playback_path\"), false );")));
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("actionPlay->trigger")));
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("programmaticPlay(")));
}

TEST(ContactSheetCaptureWiring, PerPresentedFrameHookIsCalledFromTheSameSiteAsPlaybackSmokeTelemetryAndGatedOnItsOwnFlag)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    // Declared next to notePlaybackSmokePresentedFrame and called from the same real-
    // presented-frame call site (finishPresentedFrame), shortly after it -- so every
    // playback-mode contact-sheet grab is of a frame actually presented during playback,
    // the same event that drives the playback_smoke fps/frame counters.
    const int noteCallAt = source.indexOf(
        QStringLiteral("notePlaybackSmokePresentedFrame( displayFrame, readyFrame, requestContext );"));
    const int hookCallAt = source.indexOf(
        QStringLiteral("noteContactSheetPresentedFrame( displayFrame, readyFrame, requestContext );"));
    ASSERT_TRUE(noteCallAt >= 0);
    ASSERT_TRUE(hookCallAt > noteCallAt);
    ASSERT_TRUE(hookCallAt - noteCallAt < 700);

    // HARDENING (default-off, CUDA-PLAYBACK-CONTACT-SHEET-2): the call itself -- not just the
    // hook's own early return -- must be gated on a cheap member bool set only when the
    // contact-sheet options are actually present, so a smoke run with the options off makes
    // zero noteContactSheetPresentedFrame() calls on the measured-frame hot path.
    const int callGuardAt = source.lastIndexOf(
        QStringLiteral("if( m_contactSheetOptionsPresent )"), hookCallAt);
    ASSERT_TRUE(callGuardAt >= 0);
    ASSERT_TRUE(callGuardAt < hookCallAt);
    ASSERT_TRUE(hookCallAt - callGuardAt < 100);

    // Gated on its own flag, never on m_playbackSmokeActive -- it must keep working (and keep
    // counting) after the measured interval's playback_smoke session has already closed.
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(QStringLiteral("if( !m_contactSheetCaptureActive ) return;")));
    ASSERT_FALSE(hookBody.contains(QStringLiteral("m_playbackSmokeActive")));
}

TEST(ContactSheetCaptureWiring, DefaultOffOptionsPresentFlagIsSetOnlyInsideTheContactSheetBlock)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool m_contactSheetOptionsPresent = false;")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // CONTACT-SHEET-PLAYBACK-PARITY-1: the only capture that needs the per-present hook is the
    // in-pass one, so the flag is set where that capture is armed (seek mode never grabs there).
    const int guardAt = smokeBody.indexOf(QStringLiteral(
        "if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 && !options.contactSheetSeekMode )"));
    const int setTrueAt = smokeBody.indexOf(
        QStringLiteral("m_contactSheetOptionsPresent = true;"), guardAt);
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(setTrueAt > guardAt);
    ASSERT_TRUE(setTrueAt - guardAt < 2500);

    // Set true exactly once in the whole file, and only inside this block -- an unconditional
    // or earlier assignment would defeat the whole point of gating the per-present call on it.
    ASSERT_EQ(1, source.count(QStringLiteral("m_contactSheetOptionsPresent = true;")));
}

TEST(ContactSheetCaptureWiring, PlaybackModeSidecarRecordsRenderPathAndPlaybackPathTrue)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(QStringLiteral("QStringLiteral(\"render_path\")")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"playback_path\"), true );")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("mainWindowGpuPlaybackPipelineStatusToken( contactFramePipelineStatus )")));
}

TEST(ContactSheetCaptureWiring, SeekModeSidecarRecordsPlaybackPathFalse)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    // Not sliceBetween(..., "else"): the seek branch's own body contains several inner
    // if/else fallback chains, whose first "else" would truncate the slice long before the
    // sidecar-writing code below it. Slice to the playback-mode branch's own distinctive
    // first statement instead, which only appears after the seek branch's closing brace.
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"playback_path\"), false );")));
}

TEST(ContactSheetCaptureWiring, GpuWindowReadbackIsTriedBeforeAnyCpuPathFallback)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    const int guardAt = smokeBody.indexOf(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )"));
    ASSERT_TRUE(guardAt >= 0);
    const QString captureBlock = smokeBody.mid(guardAt);
    const int gpuActiveAt = captureBlock.indexOf(QStringLiteral("GpuDisplayWindow::isActive()"));
    const int gpuReadbackAt = captureBlock.indexOf(
        QStringLiteral("GpuDisplayWindow::grabPresentedFramebufferIfActive("));
    const int viewportFallbackAt = captureBlock.indexOf(
        QStringLiteral("app_internal_gl_viewport_grab"));
    const int pixmapFallbackAt = captureBlock.indexOf(
        QStringLiteral("app_internal_presented_pixmap"));
    ASSERT_TRUE(gpuActiveAt >= 0);
    ASSERT_TRUE(gpuReadbackAt > gpuActiveAt);
    ASSERT_TRUE(viewportFallbackAt > gpuReadbackAt);
    ASSERT_TRUE(pixmapFallbackAt > viewportFallbackAt);
}

// --- r1c pre-review fixes (sol, prereview-cs1-20260926T0315Z) --------------------------------

TEST(ContactSheetCaptureWiring, B1_HookDisarmsOnAnyFrameNotPresentedWhilePlayIsChecked)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());

    // A frame presented while Play is not checked (a seek, a paused redraw) is never kept as
    // playback_path=true: the hook refuses it strictly BEFORE the readback.
    const int activeGuardAt = hookBody.indexOf(QStringLiteral("if( !m_contactSheetCaptureActive ) return;"));
    const int checkedGuardAt = hookBody.indexOf(QStringLiteral("if( !ui->actionPlay->isChecked() ) return;"));
    const int grabAt = hookBody.indexOf(QStringLiteral("GpuDisplayWindow::grabPresentedFramebufferIfActive("));
    ASSERT_TRUE(activeGuardAt >= 0);
    ASSERT_TRUE(checkedGuardAt > activeGuardAt);
    ASSERT_TRUE(grabAt > checkedGuardAt);
}

TEST(ContactSheetCaptureWiring, B2_ContactSheetStateNeverGatesPlaybackSmokeTelemetry)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString togglePlayBody = sliceBetween(source,
        QStringLiteral("void MainWindow::on_actionPlay_toggled(bool checked)"),
        QStringLiteral("void MainWindow::on_actionShowZebras_triggered()"));
    ASSERT_FALSE(togglePlayBody.isEmpty());

    // The guard that suppressed beginPlaybackSmokeTelemetry() existed only for the capture
    // REPLAY's restarts. There is no replay (CONTACT-SHEET-PLAYBACK-PARITY-1 captures during
    // the measured Play), so a Play toggle inside the measured loop (clip-lifecycle stress) must
    // open its telemetry session exactly as it does without a contact sheet.
    const int checkedBranchAt = togglePlayBody.indexOf(QStringLiteral("if( checked )"));
    ASSERT_TRUE(checkedBranchAt >= 0);
    ASSERT_FALSE(togglePlayBody.contains(
        QStringLiteral("if( !m_contactSheetCaptureActive ) beginPlaybackSmokeTelemetry();")));
    const int bareCallAt = togglePlayBody.indexOf(
        QStringLiteral("\n        beginPlaybackSmokeTelemetry();\n"), checkedBranchAt);
    ASSERT_TRUE(bareCallAt > checkedBranchAt);
}

TEST(ContactSheetCaptureWiring, H1_SeekAndSettleKeepsItsBoundedTimeoutParameterAndNoRestartSeekRemains)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // seekAndSettleLoadedClip keeps its optional timeoutMs parameter (default 8000) for the stress seeks
    // and the seek-mode capture; the contact-sheet RESTART call site that used to pass what remained of
    // the overall deadline is gone with the replay it served (PLAYBACK-CLIP-LENGTH-ENFORCE-2).
    const int lambdaAt = smokeBody.indexOf(QStringLiteral("auto seekAndSettleLoadedClip = [&]("));
    ASSERT_TRUE(lambdaAt >= 0);
    const QString lambdaHeader = smokeBody.mid(lambdaAt, 400);
    ASSERT_TRUE(lambdaHeader.contains(QStringLiteral("int timeoutMs = 8000")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("restartSeekTimeoutMs")));
}

TEST(ContactSheetCaptureWiring, H2_GpuWindowGrabFailureOrSerialMismatchFailsClosedRatherThanFallingBack)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());

    ASSERT_TRUE(hookBody.contains(QStringLiteral("bool gpuWindowGrabFailedClosed = false;")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("grabbedSerialValid && grabbedSerial != readyFrame.requestSerial")));
    // Every fallback below the GPU-window readback must be skipped once that leg has failed
    // closed -- a failed/mismatched GPU-window grab must never be silently replaced by a
    // viewport/pixmap grab still labelled playback_path=true.
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("if( !gpuWindowGrabFailedClosed && contactFrameImage.isNull()\n"
                        "     && ( GpuDisplayViewport::isTexturePresentationActive( ui->graphicsView )")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("if( !gpuWindowGrabFailedClosed && contactFrameImage.isNull() && m_pGraphicsItem )")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("if( !gpuWindowGrabFailedClosed && contactFrameImage.isNull()\n"
                        "     && ui->graphicsView && ui->graphicsView->viewport() )")));
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("bool frameOk = !gpuWindowGrabFailedClosed\n")));
}

TEST(ContactSheetCaptureWiring, H3_SidecarPathFieldIsRelativeNeverAbsolute)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"path\"), pngRelativeName );")));
    ASSERT_FALSE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"path\"), pngPath );")));

    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"path\"), pngRelativeName );")));
    ASSERT_FALSE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"path\"), pngPath );")));
}

TEST(ContactSheetCaptureWiring, B4_DoneLineOmitsFramesWrittenFieldEntirelyWhenOptionsAreOff)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int doneLineAt = smokeBody.indexOf(QStringLiteral("out << \"[GUI-SMOKE] DONE"));
    ASSERT_TRUE(doneLineAt >= 0);
    const int fieldGuardAt = smokeBody.indexOf(
        QStringLiteral("if( !options.contactSheetDir.isEmpty() && options.contactSheetFrames > 0 )\n"
                        "        out << \" contact_sheet_frames_written=\" << contactSheetFramesWritten;"),
        doneLineAt);
    ASSERT_TRUE(fieldGuardAt > doneLineAt);
    // The unconditional field append must not also remain right on the DONE out<< chain.
    ASSERT_FALSE(smokeBody.contains(
        QStringLiteral("<< \" diagnostic_log_file=\" << CrashForensics::currentLogFilePath()\n"
                        "        << \" contact_sheet_frames_written=\"")));
}

// --- r1d pre-review #2 finding (sol, prereview-cs1b-sol-20260926T0450Z) ----------------------

TEST(ContactSheetCaptureWiring, FinishTelemetryClearsFrameAndTimelineFlagsAfterEverySummaryReadsThem)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString finishBody = sliceBetween(source,
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry( const char *reason )"),
        QStringLiteral("bool MainWindow::primePlaybackCacheOnPlayStart( void )"));
    ASSERT_FALSE(finishBody.isEmpty());

    // HARDENING fix: a capture-playback frame emitted after the measured session closes (e.g.
    // a contact-sheet capture pass's frames, drawn via beginPlaybackSmokeTelemetry-suppressed
    // restarts) must stop passing the m_playbackSmokeFrameTelemetry/
    // m_playbackSmokeTimelineTelemetry gates elsewhere -- otherwise it is logged (e.g.
    // playback_auto.decision) carrying this now-closed session's id.
    const int clearFrameAt = finishBody.indexOf(QStringLiteral("m_playbackSmokeFrameTelemetry = false;"));
    const int clearTimelineAt = finishBody.indexOf(QStringLiteral("m_playbackSmokeTimelineTelemetry = false;"));
    ASSERT_TRUE(clearFrameAt >= 0);
    ASSERT_TRUE(clearTimelineAt >= 0);

    // Cleared LAST: every read of either flag inside this same function (the
    // playback_smoke.summary line's frame_telemetry field and the playback_smoke.foreground
    // line's telemetry_enabled field) must see the session's TRUE value, not a value this same
    // call already zeroed -- clearing earlier would make this session's own summary misreport
    // a telemetry-enabled session as disabled.
    const int summaryFrameTelemetryReadAt = finishBody.indexOf(
        QStringLiteral(".arg( bool01( m_playbackSmokeFrameTelemetry ) )"));
    const int foregroundGuardAt = finishBody.indexOf(QStringLiteral("if ( m_playbackSmokeFrameTelemetry )"));
    ASSERT_TRUE(summaryFrameTelemetryReadAt >= 0);
    ASSERT_TRUE(foregroundGuardAt >= 0);
    ASSERT_TRUE(clearFrameAt > summaryFrameTelemetryReadAt);
    ASSERT_TRUE(clearFrameAt > foregroundGuardAt);
    ASSERT_TRUE(clearTimelineAt > summaryFrameTelemetryReadAt);
    ASSERT_TRUE(clearTimelineAt > foregroundGuardAt);
}

TEST(ContactSheetCaptureWiring, MeasuredSessionMarkerIsLoggedOnceRightAfterTheMeasuredPlayTrigger)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool m_playbackSmokeMeasuredSessionLogged = false;")));

    // HARDENING (CUDA-PLAYBACK-CONTACT-SHEET-2): the marker must now be logged from
    // runGuiPlaybackSmoke() itself, immediately after the trigger() that opens the MEASURED
    // session -- not from finishPlaybackSmokeTelemetry() on a "play-stop", which a Look Assist
    // Auto-warmup settle or an in-loop clip-lifecycle-stress Play toggle could reach first and
    // mislabel. It must therefore sit strictly between the measured play trigger and the timed
    // measurement while() loop -- never inside a warmup/stress block above it, and never after
    // the loop has already started consuming the measured window.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    const int measuredTriggerAt = smokeBody.indexOf(QStringLiteral("forcePlaybackSmokeWindowForeground();"));
    const int markerGuardAt = smokeBody.indexOf(
        QStringLiteral("if( !m_playbackSmokeMeasuredSessionLogged )"), measuredTriggerAt);
    const int timedLoopAt = smokeBody.indexOf(
        QStringLiteral("for( ;; )"),
        smokeBody.indexOf(QStringLiteral("playback_frame_range::PlayStopState measuredState")));
    ASSERT_TRUE(measuredTriggerAt >= 0);
    ASSERT_TRUE(markerGuardAt > measuredTriggerAt);
    ASSERT_TRUE(timedLoopAt > markerGuardAt);

    const QString markerBlock = smokeBody.mid(markerGuardAt, 400);
    ASSERT_TRUE(markerBlock.contains(QStringLiteral("m_playbackSmokeMeasuredSessionLogged = true;")));
    ASSERT_TRUE(markerBlock.contains(QStringLiteral("\"playback_smoke.measured_session id=%1\"")));

    // The old first-"play-stop" binding must be gone from finishPlaybackSmokeTelemetry -- a
    // leftover copy there would double-log the marker (harmless for the FIRST such call since
    // the flag is already set, but proof the relocation actually happened, not just an addition).
    const QString finishBody = sliceBetween(source,
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry( const char *reason )"),
        QStringLiteral("bool MainWindow::primePlaybackCacheOnPlayStart( void )"));
    ASSERT_FALSE(finishBody.isEmpty());
    ASSERT_FALSE(finishBody.contains(QStringLiteral("m_playbackSmokeMeasuredSessionLogged = true;")));
    ASSERT_FALSE(finishBody.contains(QStringLiteral("qstrcmp( reason, \"play-stop\" )")));
}

// --- CUDA-PLAYBACK-CONTACT-SHEET-2 (BLOCKER fix: looped span collapse) -----------------------

TEST(ContactSheetCaptureWiring, WrapFlagExistsResetsPerSessionAndIsSetWhenTheTimelineGoesBackwards)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("bool m_playbackSmokeWrapped = false;")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    // beginPlaybackSmokeTelemetry() must reset the flag for every new session, right alongside
    // the first/last-presented-frame fields it already resets -- otherwise a wrap detected in
    // an EARLIER session (e.g. a warmup) would wrongly force the wrapped span path for a later,
    // non-looping measured session in the same process.
    const QString beginBody = sliceBetween(source,
        QStringLiteral("void MainWindow::beginPlaybackSmokeTelemetry( void )"),
        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("));
    ASSERT_FALSE(beginBody.isEmpty());
    const int resetLastFrameAt = beginBody.indexOf(QStringLiteral("m_playbackSmokeLastPresentedFrame = -1;"));
    const int resetWrappedAt = beginBody.indexOf(QStringLiteral("m_playbackSmokeWrapped = false;"));
    ASSERT_TRUE(resetLastFrameAt >= 0);
    ASSERT_TRUE(resetWrappedAt > resetLastFrameAt);

    // notePlaybackSmokePresentedFrame() must set the flag when the presented-frame transition
    // qualifies as a loop wrap (playback_frame_range::isContactSheetLoopWrapTransition -- see
    // the round-2 LOOP-WRAP-QUALIFICATION test below), and must check this BEFORE
    // m_playbackSmokeLastPresentedFrame is overwritten with the new value, or the comparison
    // would always see the frame compared against itself.
    const QString noteBody = sliceBetween(source,
        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("),
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("));
    ASSERT_FALSE(noteBody.isEmpty());
    const int wrapCheckAt = noteBody.indexOf(
        QStringLiteral("playback_frame_range::isContactSheetLoopWrapTransition("));
    const int setWrappedAt = noteBody.indexOf(QStringLiteral("m_playbackSmokeWrapped = true;"), wrapCheckAt);
    const int overwriteLastFrameAt = noteBody.indexOf(
        QStringLiteral("m_playbackSmokeLastPresentedFrame = static_cast<int>( displayFrame );"));
    ASSERT_TRUE(wrapCheckAt >= 0);
    ASSERT_TRUE(setWrappedAt > wrapCheckAt);
    ASSERT_TRUE(overwriteLastFrameAt > setWrappedAt);
}

// --- CUDA-PLAYBACK-CONTACT-SHEET-2 round 2 (BLOCKER: wrapped-span final target unreachable in
// drop-frame looping playback; HARDENING: LOOP-WRAP-QUALIFICATION) -----------------------------

TEST(ContactSheetCaptureWiring, LoopWrapDetectionIsQualifiedByLoopStateAndJumpSize)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString noteBody = sliceBetween(source,
        QStringLiteral("void MainWindow::notePlaybackSmokePresentedFrame("),
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("));
    ASSERT_FALSE(noteBody.isEmpty());

    // A bare "went backward" test also fires for a backward scrub or stress seek with Loop off,
    // or to an arbitrary earlier frame -- the call must pass the live Loop state and both cut
    // spinboxes so the pure helper can require an actual loop-width-sized jump.
    const int callAt = noteBody.indexOf(
        QStringLiteral("playback_frame_range::isContactSheetLoopWrapTransition("));
    ASSERT_TRUE(callAt >= 0);
    const QString callBlock = noteBody.mid(callAt, 400);
    ASSERT_TRUE(callBlock.contains(QStringLiteral("ui->actionLoop->isChecked()")));
    ASSERT_TRUE(callBlock.contains(QStringLiteral("ui->spinBoxCutIn->value() - 1")));
    ASSERT_TRUE(callBlock.contains(QStringLiteral("ui->spinBoxCutOut->value() - 1")));
    ASSERT_TRUE(callBlock.contains(QStringLiteral("m_playbackSmokeLastPresentedFrame")));

    const QString header = readRepoFile(QStringLiteral("platform/qt/PlaybackFrameRange.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("inline bool isContactSheetLoopWrapTransition(")));
    // The un-qualified pre-round-2 shape must actually be gone, not just supplemented.
    ASSERT_FALSE(noteBody.contains(
        QStringLiteral("static_cast<int>( displayFrame ) < m_playbackSmokeLastPresentedFrame")));
}

TEST(ContactSheetCaptureWiring, PlaybackModeNoLongerTouchesDropFrameModeBecauseThereIsNoReplay)
{
    // The drop-frame force-off existed only to make the capture REPLAY deterministic. The replay is gone
    // (PLAYBACK-CLIP-LENGTH-ENFORCE-2), so nothing in the smoke body toggles drop-frame mode for it.
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    // The in-pass (default) capture rides the measured Play as configured: neither its arm block
    // nor its write block touches drop-frame mode.
    const int armAt = smokeBody.indexOf(QString::fromLatin1(kInPassArmMarker));
    ASSERT_TRUE(armAt >= 0);
    const QString armBlock = smokeBody.mid(armAt, 2500);
    ASSERT_FALSE(armBlock.contains(QStringLiteral("actionDropFrameMode")));
    const QString writeBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kInPassWriteMarker), QString::fromLatin1(kSeekMarker));
    ASSERT_FALSE(writeBranch.isEmpty());
    ASSERT_FALSE(writeBranch.contains(QStringLiteral("actionDropFrameMode")));
    ASSERT_FALSE(smokeBody.contains(QStringLiteral("contactSheetDropFrameModeBefore")));

    // The seek-mode branch never plays at all, so it must not be touched by this either.
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("actionDropFrameMode")));
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("actionPlay->trigger")));
}

TEST(ContactSheetCaptureWiring, ContactSheetTargetFramesAreComputedByTheSharedPureHelper)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // Both capture modes share ONE target list built from the same real, executable-tested
    // function (test_playback_frame_range.cpp), rather than each re-deriving the fraction
    // arithmetic inline where it could silently drift between the two modes.
    ASSERT_TRUE(smokeBody.contains(
        QStringLiteral("playback_frame_range::contactSheetTargetFrame(\n                i, options.contactSheetFrames, sheetStartFrame, sheetEndFrame )")));

    const QString header = readRepoFile(QStringLiteral("platform/qt/PlaybackFrameRange.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("inline int contactSheetTargetFrame(")));
}

TEST(ContactSheetCaptureWiring, WrappedSpanIsOverriddenToTheLoopCutInCutOutRangeNotLastPresentedFrame)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());

    // The span snapshot reads m_playbackSmokeWrapped and, when set, overrides BOTH
    // contactSheetStartFrame and contactSheetEndFrame from the Loop action's own cutIn/cutOut
    // spinboxes -- deterministic settings values, never a presented-frame artifact that can
    // differ between two hosts measuring the same clip.
    const int wrappedReadAt = smokeBody.indexOf(
        QStringLiteral("const bool contactSheetSpanWrapped = m_playbackSmokeWrapped;"));
    ASSERT_TRUE(wrappedReadAt >= 0);
    const int overrideGuardAt = smokeBody.indexOf(
        QStringLiteral("if( contactSheetSpanWrapped )"), wrappedReadAt);
    ASSERT_TRUE(overrideGuardAt > wrappedReadAt);
    const QString overrideBlock = smokeBody.mid(overrideGuardAt, 700);
    ASSERT_TRUE(overrideBlock.contains(QStringLiteral("ui->spinBoxCutIn->value()")));
    ASSERT_TRUE(overrideBlock.contains(QStringLiteral("ui->spinBoxCutOut->value()")));
    ASSERT_TRUE(overrideBlock.contains(QStringLiteral("contactSheetStartFrame = contactSheetLoopCutInFrame;")));
    ASSERT_TRUE(overrideBlock.contains(QStringLiteral("contactSheetEndFrame = contactSheetLoopCutOutFrame;")));

    // The override must run BEFORE sheetStartFrame/sheetEndFrame (the values actually used to
    // build the evenly spaced target-frame list) are computed from contactSheetStartFrame/
    // contactSheetEndFrame -- otherwise the wrap override would be silently discarded.
    const int sheetStartComputedAt = smokeBody.indexOf(QStringLiteral("const int sheetStartFrame = qBound("));
    ASSERT_TRUE(sheetStartComputedAt > overrideGuardAt);
}

TEST(ContactSheetCaptureWiring, PlaybackModeSidecarAndDoneLineCarrySpanStartEndAndWrappedFields)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));

    // noteContactSheetPresentedFrame() (the default playback-mode capture path) stamps every
    // sidecar with the span the job-level composer/owner needs to know what was actually
    // covered, using the member fields runGuiPlaybackSmoke set up once for the whole pass.
    const QString hookBody = sliceBetween(source,
        QStringLiteral("void MainWindow::noteContactSheetPresentedFrame("),
        QStringLiteral("void MainWindow::finishPlaybackSmokeTelemetry"));
    ASSERT_FALSE(hookBody.isEmpty());
    ASSERT_TRUE(hookBody.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_start\"), m_contactSheetCaptureStartFrame );")));
    // CONTACT-SHEET-PLAYBACK-PARITY-1: the in-pass grab happens before the measured span has
    // ended, so the writer (after the stop) stamps the measured span's end and wrap flag.
    const QString inPassSmokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    const QString writeBranch = sliceBetween(inPassSmokeBody,
        QString::fromLatin1(kInPassWriteMarker), QString::fromLatin1(kSeekMarker));
    ASSERT_TRUE(writeBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_end\"), sheetEndFrame );")));
    ASSERT_TRUE(writeBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_wrapped\"), contactSheetSpanWrapped );")));

    // The seek-mode branch (runs inline inside runGuiPlaybackSmoke, not through the hook above)
    // stamps the same three fields from its own local sheetStartFrame/sheetEndFrame/
    // contactSheetSpanWrapped -- a reader must see the same span regardless of which mode wrote
    // a given sidecar.
    const QString smokeBody = sliceBetween(source,
        QStringLiteral("int MainWindow::runGuiPlaybackSmoke(const GuiPlaybackSmokeOptions & options)"),
        QStringLiteral("bool MainWindow::importNewMlv(QString fileName)"));
    ASSERT_FALSE(smokeBody.isEmpty());
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_start\"), sheetStartFrame );")));
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_end\"), sheetEndFrame );")));
    ASSERT_TRUE(seekBranch.contains(
        QStringLiteral("frameJson.insert( QStringLiteral(\"span_wrapped\"), contactSheetSpanWrapped );")));

    // The job-level DONE line (gui_smoke.contact_sheet) also carries the wrapped flag, so a
    // reader who only has the raw log (not the per-frame sidecars) can still see it.
    ASSERT_TRUE(smokeBody.contains(QStringLiteral("start_frame=%4 end_frame=%5 wrapped=%6")));
    ASSERT_TRUE(smokeBody.contains(QStringLiteral(".arg( bool01( contactSheetSpanWrapped ) )")));
}

// CONTACT-SHEET-SEEK-FRAME-LIST-APP-1: --contact-sheet-seek-frames <list> seeks the seek stills to an
// explicit frame list. Each still proves its frame by observation (the presented request context),
// never by the clamped request; an out-of-range frame is refused before the measured Play.
TEST(ContactSheetSeekFrameListWiring, TheOptionStructHoldsTheListAndItDefaultsToEmpty)
{
    const QString header = readRepoFile(QStringLiteral("platform/qt/MainWindow.h"));
    const QString optionsStruct = sliceBetween(header,
        QStringLiteral("struct GuiPlaybackSmokeOptions"),
        QStringLiteral("int runHeadlessPlaybackProfile"));
    ASSERT_FALSE(optionsStruct.isEmpty());
    ASSERT_TRUE(optionsStruct.contains(QStringLiteral("QVector<int> contactSheetSeekFrames;")));
}

TEST(ContactSheetSeekFrameListWiring, TheCliParsesTheListAndRefusesItWithoutASeekCapture)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    const QString smokeParser = sliceBetween(source,
        QStringLiteral("static int runGuiPlaybackSmoke("),
        QStringLiteral("return window.runGuiPlaybackSmoke(options);"));
    ASSERT_FALSE(smokeParser.isEmpty());
    ASSERT_TRUE(smokeParser.contains(QStringLiteral("QStringLiteral(\"contact-sheet-seek-frames\")")));
    const int parseAt = smokeParser.indexOf(QStringLiteral("playback_frame_range::parseContactSheetSeekFrameList("));
    ASSERT_TRUE(parseAt > 0);
    // The list is validated after the seek dir is resolved, so both seek captures are known.
    ASSERT_TRUE(parseAt > smokeParser.indexOf(QStringLiteral("options.contactSheetSeekDir = QFileInfo(")));
    const int guardAt = smokeParser.indexOf(QStringLiteral("if (parser.isSet(contactSheetSeekFramesOpt))"));
    ASSERT_TRUE(guardAt > 0 && guardAt < parseAt);
    const QString validation = smokeParser.mid(guardAt, 1800);
    ASSERT_TRUE(validation.contains(QStringLiteral("options.contactSheetSeekDir.isEmpty() && !options.contactSheetSeekMode")));
    ASSERT_TRUE(validation.contains(QStringLiteral("--contact-sheet-seek-frames")));
    ASSERT_TRUE(validation.contains(QStringLiteral("errorToken")));
    ASSERT_TRUE(validation.contains(QStringLiteral("return 2;")));
    ASSERT_TRUE(validation.contains(QStringLiteral("options.contactSheetSeekFrames.append(")));
}

TEST(ContactSheetSeekFrameListWiring, TheListOverridesTheTargetsInBothModesBeforeTheFirstSeek)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    ASSERT_FALSE(smokeBody.isEmpty());
    const int inPassAt = smokeBody.indexOf(QStringLiteral("contactSheetTargetFrames = inPassDisplayFrames;"));
    const int listAt = smokeBody.indexOf(QStringLiteral("contactSheetTargetFrames = options.contactSheetSeekFrames;"));
    const int inPassBranchAt = smokeBody.indexOf(QStringLiteral("if( !options.contactSheetSeekMode )"), smokeBody.indexOf(QString::fromLatin1(kInPassWriteMarker)));
    const int seekMarkerAt = smokeBody.indexOf(QString::fromLatin1(kSeekMarker));
    const int seekCallAt = smokeBody.indexOf(QStringLiteral("targetFrame, \"gui-smoke-contact-sheet-seek\""));
    ASSERT_TRUE(inPassAt > 0);
    ASSERT_TRUE(listAt > inPassAt);
    ASSERT_TRUE(seekCallAt > listAt);
    // Outside the in-pass-only branch, so --contact-sheet-seek-mode gets the list too: the
    // assignment sits after that branch closes (the fps block is its last statement) and before
    // the seek capture's marker.
    ASSERT_TRUE(inPassBranchAt > 0 && inPassBranchAt < inPassAt);
    ASSERT_TRUE(listAt > smokeBody.indexOf(QStringLiteral("contactSheetGrabTotalMs );"), inPassAt));
    ASSERT_TRUE(listAt < seekMarkerAt);
    ASSERT_TRUE(smokeBody.mid(listAt - 120, 120).contains(QStringLiteral("if( !options.contactSheetSeekFrames.isEmpty() )")));
}

TEST(ContactSheetSeekFrameListWiring, AnOutOfRangeFrameIsRefusedBeforeTheMeasuredPlay)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    ASSERT_FALSE(smokeBody.isEmpty());
    const int refuseAt = smokeBody.indexOf(QStringLiteral("SEEK_FRAME_OUT_OF_RANGE"));
    const int playAt = smokeBody.indexOf(QStringLiteral("programmaticPlay( \"gui-smoke-measured\""));
    ASSERT_TRUE(refuseAt > 0);
    ASSERT_TRUE(playAt > refuseAt);
    const QString refusal = smokeBody.mid(smokeBody.lastIndexOf(QStringLiteral("firstContactSheetSeekFrameOutOfRange("), refuseAt), 900);
    ASSERT_TRUE(refusal.contains(QStringLiteral("firstContactSheetSeekFrameOutOfRange(")));
    ASSERT_TRUE(refusal.contains(QStringLiteral("SEEK_FRAME_OUT_OF_RANGE frame=")));
    ASSERT_TRUE(refusal.contains(QStringLiteral("m_playbackSmokeFullscreenLossLatchArmed = false;")));
    ASSERT_TRUE(refusal.contains(QStringLiteral("return 14;")));
}

TEST(ContactSheetSeekFrameListWiring, EachSeekStillIsHonouredByObservationOrTheRunFailsByName)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    ASSERT_TRUE(seekBranch.contains(QStringLiteral("playback_frame_range::contactSheetSeekFrameHonoured(")));
    ASSERT_TRUE(seekBranch.contains(QStringLiteral(
        "m_lastPresentedRequestContextValid ? static_cast<int>( m_lastPresentedRequestContext.frameNumber ) : -1")));
    ASSERT_TRUE(seekBranch.contains(QStringLiteral("SEEK_FRAME_NOT_HONOURED requested=%2 shown=%3")));
    // The two new sidecar fields exist only under the list; without it the sidecar is unchanged.
    const int guardAt = seekBranch.indexOf(QStringLiteral("if( !options.contactSheetSeekFrames.isEmpty() )"),
                                           seekBranch.indexOf(QStringLiteral("frameJson.insert( QStringLiteral(\"settled\"), settled );")) - 2000);
    const int requestedAt = seekBranch.indexOf(QStringLiteral("frameJson.insert( QStringLiteral(\"requested_frame\"), targetFrame );"));
    const int honouredAt = seekBranch.indexOf(QStringLiteral("frameJson.insert( QStringLiteral(\"frame_honoured\"), seekFrameHonoured );"));
    ASSERT_TRUE(guardAt >= 0);
    ASSERT_TRUE(requestedAt > guardAt);
    ASSERT_TRUE(honouredAt > guardAt);
    ASSERT_EQ(1, seekBranch.count(QStringLiteral("QStringLiteral(\"requested_frame\")")));
    ASSERT_EQ(1, seekBranch.count(QStringLiteral("QStringLiteral(\"frame_honoured\")")));
    // A list still that is not honoured cannot be saved as a good frame.
    ASSERT_TRUE(seekBranch.contains(QStringLiteral(
        "&& ( options.contactSheetSeekFrames.isEmpty() || ( seekFrameHonoured && seekStillImageTied ) )")));
    // Adversary A1: the image must come from the surface that presented the frame -- the surface state is
    // read before the capture chain and the tie is decided on the source the chain actually used.
    const int surfaceAt = seekBranch.indexOf(QStringLiteral("seekGlWindowActive = GpuDisplayWindow::isActive();"));
    const int chainAt = seekBranch.indexOf(QStringLiteral("contactFrameSource = QStringLiteral(\"gl_window_framebuffer_readback\");"));
    const int tieAt = seekBranch.indexOf(QStringLiteral("playback_frame_range::contactSheetSeekStillUntiedReason("));
    ASSERT_TRUE(surfaceAt > 0 && chainAt > surfaceAt && tieAt > chainAt);
    ASSERT_TRUE(seekBranch.mid(tieAt, 400).contains(QStringLiteral("static_cast<uint64_t>( m_lastPresentedRequestSerial )")));
    ASSERT_TRUE(seekBranch.contains(QStringLiteral("SEEK_FRAME_IMAGE_UNTIED source=%2 reason=%3")));
    ASSERT_EQ(1, seekBranch.count(QStringLiteral("QStringLiteral(\"image_tied\")")));
    // The default (no list) display_frame stays the settled request, byte for byte.
    ASSERT_TRUE(seekBranch.contains(QStringLiteral(
        "frameJson.insert( QStringLiteral(\"display_frame\"), options.contactSheetSeekFrames.isEmpty() ? settledFrame : seekObservedFrame );")));
}

// r2 (adversary A1 on ea772731; Dot 6553359245/6553493773): a widget-submitted requested frame whose paint was
// withheld leaves both texture flags false, so the chain grabs the OLDER fallback pixmap. Certification must be
// bound to the serial of the surface the image was READ FROM, never inferred from the flags; a stale or unknown
// surface serial refuses the still (no PNG, no sidecar) with a typed failure line.
TEST(ContactSheetSeekFrameListWiring, AWidgetSubmittedFrameWithItsPaintWithheldIsRefusedNotCertifiedFromTheOlderPixmap)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    const QString seekBranch = sliceBetween(smokeBody,
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    // The flags no longer decide the tie (they read false when the widget's paint is withheld).
    ASSERT_FALSE(seekBranch.contains(QStringLiteral("seekTexturePresentationActive")));
    // Each source's own serial, read after the capture chain ran.
    const int chainEndAt = seekBranch.indexOf(QStringLiteral("contactFrameSource = QStringLiteral(\"app_internal_viewport_grab\");"),
                                              seekBranch.indexOf(QStringLiteral("contactFrameSource = QStringLiteral(\"app_internal_presented_pixmap\");")));
    const int pixmapSerialAt = seekBranch.indexOf(QStringLiteral("seekSurfaceSerial = static_cast<quint64>( m_graphicsItemPresentedSerial );"));
    const int widgetSerialAt = seekBranch.indexOf(QStringLiteral("seekSurfaceSerialValid = GpuDisplayViewport::paintedSerialFor( ui->graphicsView, &seekSurfaceSerial );"));
    const int windowSerialAt = seekBranch.indexOf(QStringLiteral("seekSurfaceSerialValid = contactFramePresentedSerialValid;"));
    const int reasonAt = seekBranch.indexOf(QStringLiteral("playback_frame_range::contactSheetSeekStillUntiedReason("));
    ASSERT_TRUE(chainEndAt > 0);
    ASSERT_TRUE(pixmapSerialAt > chainEndAt);
    ASSERT_TRUE(widgetSerialAt > chainEndAt);
    ASSERT_TRUE(windowSerialAt > chainEndAt);
    ASSERT_TRUE(reasonAt > pixmapSerialAt && reasonAt > widgetSerialAt && reasonAt > windowSerialAt);
    const QString tie = seekBranch.mid(reasonAt, 400);
    ASSERT_TRUE(tie.contains(QStringLiteral("seekSurfaceSerialValid")));
    ASSERT_TRUE(tie.contains(QStringLiteral("static_cast<uint64_t>( seekSurfaceSerial )")));
    ASSERT_TRUE(tie.contains(QStringLiteral("static_cast<uint64_t>( m_lastPresentedRequestSerial )")));
    ASSERT_TRUE(seekBranch.contains(QStringLiteral("const bool seekStillImageTied = settled && seekStillUntiedReason.isEmpty();")));
    // The refusal is typed: the reason token and both serials are on the failure line.
    ASSERT_TRUE(seekBranch.contains(QStringLiteral(
        "SEEK_FRAME_IMAGE_UNTIED source=%2 reason=%3 untied=%4 surface_serial=%5 request_serial=%6")));
    // Every viewport route carries its request serial, so a texture/recon paint can prove its frame too.
    ASSERT_TRUE(source.contains(QStringLiteral("task.gpuPresentationOptions.presentationSerial = task.requestSerial;")));
}

// r2 (adversary A2): one directory for both captures would let the seek stills overwrite the in-pass sheet.
// r2 (adversary A3): a seek frame counts as written only when its whole sidecar reached the disk.
TEST(ContactSheetSeekFrameListWiring, TheSeekDirMustDifferAndASeekSidecarCountsOnlyWhenFullyWritten)
{
    const QString mainSource = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    const int seekDirAt = mainSource.indexOf(QStringLiteral("options.contactSheetSeekDir = QFileInfo(parser.value(contactSheetSeekDirOpt)).absoluteFilePath();"));
    ASSERT_TRUE(seekDirAt > 0);
    const QString sameDir = mainSource.mid(seekDirAt, 1000);
    ASSERT_TRUE(sameDir.contains(QStringLiteral(
        "QDir::cleanPath(options.contactSheetSeekDir).compare(QDir::cleanPath(options.contactSheetDir), seekDirCase) == 0")));
    ASSERT_TRUE(sameDir.contains(QStringLiteral("SEEK_DIR_SAME_AS_CONTACT_SHEET_DIR")));
    ASSERT_TRUE(sameDir.contains(QStringLiteral("return 2;")));

    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString seekBranch = sliceBetween(smokeBodyOf(source),
        QString::fromLatin1(kSeekMarker),
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"));
    ASSERT_FALSE(seekBranch.isEmpty());
    const int writeAt = seekBranch.indexOf(QStringLiteral("const bool sidecarWritten = sidecarFile.write( sidecarBytes ) == sidecarBytes.size()"));
    const int countAt = seekBranch.indexOf(QStringLiteral("++contactSheetSeekFramesWritten;"));
    ASSERT_TRUE(writeAt > 0 && countAt > writeAt);
    const QString write = seekBranch.mid(writeAt, countAt - writeAt);
    ASSERT_TRUE(write.contains(QStringLiteral("&& sidecarFile.flush();")));
    ASSERT_TRUE(write.contains(QStringLiteral("if( sidecarWritten && sidecarFile.error() == QFileDevice::NoError )")));
}

// r3 (sol B1): the separation check compares filesystem identity, so a junction or symlink alias of the in-pass
// directory (or of a directory nested in it) is refused, not just an identical path string.
namespace
{

// A directory junction needs no privilege on Windows; a symlink is the POSIX equivalent.
bool makeDirectoryAlias(const QString & aliasPath, const QString & targetPath)
{
#ifdef Q_OS_WIN
    QProcess mklink;
    mklink.start(QStringLiteral("cmd.exe"),
                 { QStringLiteral("/c"), QStringLiteral("mklink"), QStringLiteral("/J"),
                   QDir::toNativeSeparators(aliasPath), QDir::toNativeSeparators(targetPath) });
    if (!mklink.waitForFinished(30000)) return false;
    return mklink.exitStatus() == QProcess::NormalExit && mklink.exitCode() == 0 && QFileInfo(aliasPath).isDir();
#else
    return QFile::link(targetPath, aliasPath) && QFileInfo(aliasPath).isDir();
#endif
}

} // namespace

TEST(ContactSheetSeekDirIdentity, AJunctionAliasOfTheInPassDirIsTheSameDirectory)
{
    QTemporaryDir root;
    ASSERT_TRUE(root.isValid());
    const QString in = root.filePath(QStringLiteral("in"));
    const QString alias = root.filePath(QStringLiteral("alias"));
    ASSERT_TRUE(QDir().mkpath(in));
    ASSERT_TRUE(makeDirectoryAlias(alias, in));
    // The cleaned strings differ, which is all the b8f6155d check looked at.
    ASSERT_FALSE(QDir::cleanPath(alias).compare(QDir::cleanPath(in), Qt::CaseInsensitive) == 0);

    using output_dir_identity::DirRelation;
    ASSERT_EQ(static_cast<int>(DirRelation::Same),
              static_cast<int>(output_dir_identity::compareDirectories(alias, in).relation));
    ASSERT_EQ(static_cast<int>(DirRelation::Same),
              static_cast<int>(output_dir_identity::compareDirectories(in, alias).relation));
}

TEST(ContactSheetSeekDirIdentity, AnAliasOfADirectoryNestedInTheOtherIsRefusedAsNested)
{
    QTemporaryDir root;
    ASSERT_TRUE(root.isValid());
    const QString in = root.filePath(QStringLiteral("in"));
    const QString nested = root.filePath(QStringLiteral("in/sub"));
    const QString nestedAlias = root.filePath(QStringLiteral("subalias"));
    const QString inAlias = root.filePath(QStringLiteral("inalias"));
    ASSERT_TRUE(QDir().mkpath(nested));
    ASSERT_TRUE(makeDirectoryAlias(nestedAlias, nested));
    ASSERT_TRUE(makeDirectoryAlias(inAlias, in));

    using output_dir_identity::DirRelation;
    // seek dir = alias of in\sub, contact sheet dir = in: the first is inside the second.
    ASSERT_EQ(static_cast<int>(DirRelation::FirstInsideSecond),
              static_cast<int>(output_dir_identity::compareDirectories(nestedAlias, in).relation));
    // seek dir = alias of in, contact sheet dir = in\sub: the second is inside the first.
    ASSERT_EQ(static_cast<int>(DirRelation::SecondInsideFirst),
              static_cast<int>(output_dir_identity::compareDirectories(inAlias, nested).relation));
}

TEST(ContactSheetSeekDirIdentity, DistinctSiblingDirectoriesAreAcceptedAndAMissingOneFailsClosed)
{
    QTemporaryDir root;
    ASSERT_TRUE(root.isValid());
    const QString in = root.filePath(QStringLiteral("in"));
    const QString seek = root.filePath(QStringLiteral("seek"));
    // A sibling whose name starts with the other's ("in" / "in-seek") is not nested.
    const QString prefixSibling = root.filePath(QStringLiteral("in-seek"));
    ASSERT_TRUE(QDir().mkpath(in));
    ASSERT_TRUE(QDir().mkpath(seek));
    ASSERT_TRUE(QDir().mkpath(prefixSibling));

    using output_dir_identity::DirRelation;
    ASSERT_EQ(static_cast<int>(DirRelation::Distinct),
              static_cast<int>(output_dir_identity::compareDirectories(seek, in).relation));
    ASSERT_EQ(static_cast<int>(DirRelation::Distinct),
              static_cast<int>(output_dir_identity::compareDirectories(prefixSibling, in).relation));
    // No identity, no acceptance.
    const output_dir_identity::DirComparison missing =
        output_dir_identity::compareDirectories(root.filePath(QStringLiteral("absent")), in);
    ASSERT_EQ(static_cast<int>(DirRelation::Unknown), static_cast<int>(missing.relation));
    ASSERT_FALSE(missing.detail.isEmpty());
}

// r5 (opus key3 r4): the CLI used to mkpath both directories before the identity refusal and before the seek-list
// parse, so a refused run left new empty directories. The on-disk compare now runs after every other argument check
// (the seek-list parse included) through prepareSeparateDirectories, which removes what it created when refused.
TEST(ContactSheetSeekDirIdentity, TheCliComparesTheDirsOnDiskOnlyAfterEveryOtherArgumentCheck)
{
    const QString mainSource = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(mainSource.contains(QStringLiteral("#include \"OutputDirIdentity.h\"")));
    const int stringCheckAt = mainSource.indexOf(QStringLiteral("SEEK_DIR_SAME_AS_CONTACT_SHEET_DIR\\n"));
    const int framesAt = mainSource.indexOf(QStringLiteral("if (parser.isSet(contactSheetSeekFramesOpt))"));
    const int prepareAt = mainSource.indexOf(QStringLiteral("output_dir_identity::prepareSeparateDirectories(options.contactSheetSeekDir, options.contactSheetDir)"));
    const int windowAt = mainSource.indexOf(QStringLiteral("MainWindow window(smokeArgc, smokeArgv);"));
    ASSERT_TRUE(stringCheckAt > 0);
    ASSERT_TRUE(framesAt > stringCheckAt);
    ASSERT_TRUE(prepareAt > framesAt);
    ASSERT_TRUE(windowAt > prepareAt);
    // Nothing in the CLI creates a contact sheet directory on its own any more.
    ASSERT_FALSE(mainSource.contains(QStringLiteral("QDir().mkpath(options.contactSheet")));
    ASSERT_FALSE(mainSource.contains(QStringLiteral("output_dir_identity::compareDirectories(")));
    const QString identityBlock = mainSource.mid(prepareAt, windowAt - prepareAt);
    // The identity refusal is the last refusal before the run starts.
    ASSERT_EQ(1, static_cast<int>(identityBlock.count(QStringLiteral("return "))));
    ASSERT_TRUE(identityBlock.contains(QStringLiteral("!= output_dir_identity::DirRelation::Distinct")));
    ASSERT_TRUE(identityBlock.contains(QStringLiteral("SEEK_DIR_SAME_IDENTITY_AS_CONTACT_SHEET_DIR")));
    ASSERT_TRUE(identityBlock.contains(QStringLiteral("SEEK_DIR_NESTED_WITH_CONTACT_SHEET_DIR")));
    ASSERT_TRUE(identityBlock.contains(QStringLiteral("SEEK_DIR_IDENTITY_UNKNOWN")));
    ASSERT_TRUE(identityBlock.contains(QStringLiteral("contact_sheet_dir=")));
    ASSERT_TRUE(identityBlock.contains(QStringLiteral("seek_dir=")));
    ASSERT_EQ(1, static_cast<int>(identityBlock.count(QStringLiteral("return 2;"))));
}

// r4 (Dot item 1 / adversary A1): trailing separators are stripped by the platform's own rule. On POSIX a
// backslash is an ordinary filename character, so "/captures/in\" is a different directory from "/captures/in".
// The rules are data, so both run on any host.
TEST(ContactSheetSeekDirIdentity, EachPlatformStripsOnlyItsOwnTrailingSeparatorsAndKeepsARoot)
{
    using namespace output_dir_identity::detail;
    const PathRules posix = posixPathRules();
    const PathRules windows = windowsPathRules();
    const auto strip = [](const QString & path, const PathRules & rules) {
        return withoutTrailingSeparators(path, rules).toStdString();
    };

    ASSERT_EQ(std::string("/captures/in\\"), strip(QStringLiteral("/captures/in\\"), posix));
    ASSERT_EQ(std::string("/captures/in\\"), strip(QStringLiteral("/captures/in\\/"), posix));
    ASSERT_EQ(std::string("/a"), strip(QStringLiteral("/a/"), posix));
    ASSERT_EQ(std::string("/a"), strip(QStringLiteral("/a//"), posix));
    ASSERT_EQ(std::string("/a:"), strip(QStringLiteral("/a:/"), posix));
    ASSERT_EQ(std::string("/"), strip(QStringLiteral("/"), posix));

    ASSERT_EQ(std::string("C:\\cs\\in"), strip(QStringLiteral("C:\\cs\\in\\"), windows));
    ASSERT_EQ(std::string("C:\\cs\\in"), strip(QStringLiteral("C:\\cs\\in/"), windows));
    ASSERT_EQ(std::string("C:\\cs\\in"), strip(QStringLiteral("C:\\cs\\in\\/\\"), windows));
    ASSERT_EQ(std::string("C:\\"), strip(QStringLiteral("C:\\"), windows));
    ASSERT_EQ(std::string("\\\\?\\C:\\"), strip(QStringLiteral("\\\\?\\C:\\"), windows));
    ASSERT_EQ(std::string("\\\\?\\C:\\cs"), strip(QStringLiteral("\\\\?\\C:\\cs\\"), windows));
    ASSERT_EQ(std::string("\\\\server\\share"), strip(QStringLiteral("\\\\server\\share\\"), windows));
}

namespace
{

output_dir_identity::detail::DirIdentity provenIdentity(unsigned long long volume, unsigned long long index,
                                                        const QString & resolvedPath,
                                                        const output_dir_identity::detail::PathRules & rules)
{
    output_dir_identity::detail::DirIdentity id;
    id.ok = true;
    id.pathsComparable = true;
    id.volume = volume;
    id.index = index;
    id.finalPath = output_dir_identity::detail::withoutTrailingSeparators(resolvedPath, rules);
    return id;
}

} // namespace

TEST(ContactSheetSeekDirIdentity, PosixSiblingsThatDifferByATrailingBackslashAreDistinctAndItsChildIsNested)
{
    using namespace output_dir_identity::detail;
    using output_dir_identity::DirRelation;
    const PathRules posix = posixPathRules();
    const DirIdentity in = provenIdentity(1, 10, QStringLiteral("/captures/in"), posix);
    const DirIdentity inBackslash = provenIdentity(1, 11, QStringLiteral("/captures/in\\"), posix);
    const DirIdentity child = provenIdentity(1, 12, QStringLiteral("/captures/in\\/sub"), posix);

    ASSERT_EQ(static_cast<int>(DirRelation::Distinct), static_cast<int>(compareIdentities(inBackslash, in, posix).relation));
    ASSERT_EQ(static_cast<int>(DirRelation::Distinct), static_cast<int>(compareIdentities(in, inBackslash, posix).relation));
    ASSERT_EQ(static_cast<int>(DirRelation::FirstInsideSecond),
              static_cast<int>(compareIdentities(child, inBackslash, posix).relation));
    ASSERT_EQ(static_cast<int>(DirRelation::SecondInsideFirst),
              static_cast<int>(compareIdentities(inBackslash, child, posix).relation));
    // The child of "/captures/in\" is not inside "/captures/in".
    ASSERT_EQ(static_cast<int>(DirRelation::Distinct), static_cast<int>(compareIdentities(child, in, posix).relation));

    // Control, Windows rule: the same strings name one directory there, so they are Same.
    const PathRules windows = windowsPathRules();
    ASSERT_EQ(static_cast<int>(DirRelation::Same),
              static_cast<int>(compareIdentities(provenIdentity(1, 11, QStringLiteral("C:\\cs\\in\\"), windows),
                                                 provenIdentity(1, 10, QStringLiteral("C:\\cs\\in"), windows),
                                                 windows).relation));
}

// r4 (adversary A2): Same is decided by object identity on every platform. A POSIX bind mount gives one directory
// two canonical paths with one st_dev/st_ino; the r3 POSIX branch compared paths only and answered Distinct.
TEST(ContactSheetSeekDirIdentity, OneFilesystemObjectUnderTwoResolvedPathsIsTheSameDirectoryOnEveryPlatform)
{
    using namespace output_dir_identity::detail;
    using output_dir_identity::DirRelation;
    for (const PathRules & rules : { posixPathRules(), windowsPathRules() }) {
        const DirIdentity in = provenIdentity(7, 42, QStringLiteral("/captures/in"), rules);
        const DirIdentity bindAlias = provenIdentity(7, 42, QStringLiteral("/mnt/seek"), rules);
        ASSERT_EQ(static_cast<int>(DirRelation::Same), static_cast<int>(compareIdentities(bindAlias, in, rules).relation));
        ASSERT_EQ(static_cast<int>(DirRelation::Same), static_cast<int>(compareIdentities(in, bindAlias, rules).relation));
        // Controls: another inode on the same device, or the same inode number on another device, is another directory.
        ASSERT_EQ(static_cast<int>(DirRelation::Distinct),
                  static_cast<int>(compareIdentities(provenIdentity(7, 43, QStringLiteral("/mnt/seek"), rules), in, rules).relation));
        ASSERT_EQ(static_cast<int>(DirRelation::Distinct),
                  static_cast<int>(compareIdentities(provenIdentity(8, 42, QStringLiteral("/mnt/seek"), rules), in, rules).relation));
        // No identity, no acceptance.
        DirIdentity failed;
        failed.error = QStringLiteral("stat_failed errno=13");
        const output_dir_identity::DirComparison unknown = compareIdentities(failed, in, rules);
        ASSERT_EQ(static_cast<int>(DirRelation::Unknown), static_cast<int>(unknown.relation));
        ASSERT_TRUE(unknown.detail.contains(QStringLiteral("stat_failed errno=13")));
    }

    // The POSIX identity really is st_dev/st_ino of the canonical path, and a failed stat fails closed.
    // (Console tests run on Windows only, so the POSIX branch is pinned by its source; Linux and macOS CI compile it.)
    const QString header = readRepoFile(QStringLiteral("platform/qt/OutputDirIdentity.h"));
    const int posixAt = header.indexOf(QStringLiteral("#else\ninline DirIdentity identityOf("));
    ASSERT_TRUE(posixAt > 0);
    const QString posixBranch = header.mid(posixAt, header.indexOf(QStringLiteral("#endif"), posixAt) - posixAt);
    ASSERT_TRUE(posixBranch.contains(QStringLiteral("::stat(QFile::encodeName(canonical).constData(), &st)")));
    ASSERT_TRUE(posixBranch.contains(QStringLiteral("stat_failed errno=")));
    ASSERT_TRUE(posixBranch.contains(QStringLiteral("id.volume = static_cast<unsigned long long>(st.st_dev);")));
    ASSERT_TRUE(posixBranch.contains(QStringLiteral("id.index = static_cast<unsigned long long>(st.st_ino);")));
    ASSERT_TRUE(posixBranch.contains(QStringLiteral("withoutTrailingSeparators(canonical, posixPathRules())")));
}

namespace
{

// A Windows identity as identityOf builds it on a local volume: DOS final path plus the volume-GUID final path.
output_dir_identity::detail::DirIdentity guidIdentity(unsigned long long volume, unsigned long long index,
                                                      const QString & dosPath, const QString & guidPath)
{
    using namespace output_dir_identity::detail;
    DirIdentity id = provenIdentity(volume, index, dosPath, windowsPathRules());
    id.identityPath = withoutTrailingSeparators(guidPath, windowsPathRules());
    id.volumeName = volumeGuidRootOf(id.identityPath);
    return id;
}

} // namespace

// r5 (sol r4 BLOCKER): two independently mounted clones of one NTFS volume share the volume serial and every 64-bit
// file index, so serial+index alone answered Same for two different directories (exit 2,
// SEEK_DIR_SAME_IDENTITY_AS_CONTACT_SHEET_DIR). Their volume GUIDs differ, so they are Distinct.
TEST(ContactSheetSeekDirIdentity, DirectoriesOnClonedVolumesThatShareASerialAndAFileIndexAreDistinct)
{
    using namespace output_dir_identity::detail;
    using output_dir_identity::DirRelation;
    const PathRules windows = windowsPathRules();
    const QString volumeA = QStringLiteral("\\\\?\\Volume{0f1e2d3c-0000-0000-0000-00000000000a}");
    const QString volumeB = QStringLiteral("\\\\?\\Volume{0f1e2d3c-0000-0000-0000-00000000000b}");
    DirIdentity seek = guidIdentity(0x1234ABCDu, 0x0005000000000123ull, QStringLiteral("E:\\captures\\out"),
                                    volumeA + QStringLiteral("\\captures\\out"));
    DirIdentity in = guidIdentity(0x1234ABCDu, 0x0005000000000123ull, QStringLiteral("F:\\captures\\out"),
                                  volumeB + QStringLiteral("\\captures\\out"));
    ASSERT_EQ(volumeA.toStdString(), seek.volumeName.toStdString());
    ASSERT_EQ(static_cast<int>(DirRelation::Distinct), static_cast<int>(compareIdentities(seek, in, windows).relation));
    ASSERT_EQ(static_cast<int>(DirRelation::Distinct), static_cast<int>(compareIdentities(in, seek, windows).relation));
    // FileIdInfo does not help on NTFS (its 128-bit id is the 64-bit index, its serial is the clone's too).
    for (DirIdentity * id : { &seek, &in }) {
        id->hasWideId = true;
        id->wideVolume = 0x99887766001234ABull;
        id->wideIdLow = 0x0005000000000123ull;
        id->wideIdHigh = 0;
    }
    ASSERT_EQ(static_cast<int>(DirRelation::Distinct), static_cast<int>(compareIdentities(seek, in, windows).relation));

    // Controls. One GUID final path is one directory, whatever the ids say (a junction resolves to the same path).
    ASSERT_EQ(static_cast<int>(DirRelation::Same),
              static_cast<int>(compareIdentities(guidIdentity(1, 10, QStringLiteral("C:\\alias"), volumeA + QStringLiteral("\\cs\\in")),
                                                 guidIdentity(1, 11, QStringLiteral("C:\\cs\\in"), volumeA + QStringLiteral("\\CS\\in\\")),
                                                 windows).relation));
    // A share has no GUID path (\\localhost\C$\cs\in can be C:\cs\in), so an id match there still fails closed.
    DirIdentity share = provenIdentity(0x1234ABCDu, 0x0005000000000123ull, QStringLiteral("\\\\localhost\\C$\\captures\\out"), windows);
    share.identityPath = QStringLiteral("\\Device\\Mup\\localhost\\C$\\captures\\out");
    share.pathsComparable = false;   // as identityOf leaves it without a GUID path
    ASSERT_EQ(static_cast<int>(DirRelation::Same), static_cast<int>(compareIdentities(share, in, windows).relation));
    ASSERT_EQ(static_cast<int>(DirRelation::Same), static_cast<int>(compareIdentities(in, share, windows).relation));
    // r5 pass 6 (A3): \\localhost\C$\captures\out\seek is nested in F:\captures\out's twin with other ids and no
    // path in common. A path that cannot prove Distinct gives Unknown (exit 2), never Distinct.
    DirIdentity shareChild = provenIdentity(0x1234ABCDu, 0x0005000000000999ull,
                                            QStringLiteral("\\\\localhost\\C$\\captures\\out\\seek"), windows);
    shareChild.identityPath = QStringLiteral("\\Device\\Mup\\localhost\\C$\\captures\\out\\seek");
    shareChild.pathsComparable = false;
    const output_dir_identity::DirComparison viaShare = compareIdentities(shareChild, in, windows);
    ASSERT_EQ(static_cast<int>(DirRelation::Unknown), static_cast<int>(viaShare.relation));
    ASSERT_TRUE(viaShare.detail.contains(QStringLiteral("reason=no_comparable_volume_path")));
    ASSERT_EQ(static_cast<int>(DirRelation::Unknown), static_cast<int>(compareIdentities(in, shareChild, windows).relation));
    // A volume the mount manager does not list (NT path only) cannot prove Distinct either.
    DirIdentity ntOnly = provenIdentity(9, 1, QStringLiteral("R:\\caps\\seek"), windows);
    ntOnly.identityPath = QStringLiteral("\\Device\\RamDisk0\\caps\\seek");
    ntOnly.pathsComparable = false;
    ASSERT_EQ(static_cast<int>(DirRelation::Unknown), static_cast<int>(compareIdentities(ntOnly, in, windows).relation));
    // A default identity cannot prove Distinct.
    DirIdentity bare;
    bare.ok = true;
    bare.finalPath = QStringLiteral("G:\\other");
    ASSERT_EQ(static_cast<int>(DirRelation::Unknown), static_cast<int>(compareIdentities(bare, in, windows).relation));
    // One volume, two paths: a 64-bit index that repeats (ReFS without FileIdInfo) is still two directories.
    ASSERT_EQ(static_cast<int>(DirRelation::Distinct),
              static_cast<int>(compareIdentities(guidIdentity(1, 10, QStringLiteral("C:\\cs\\a"), volumeA + QStringLiteral("\\cs\\a")),
                                                 guidIdentity(1, 10, QStringLiteral("C:\\cs\\b"), volumeA + QStringLiteral("\\cs\\b")),
                                                 windows).relation));
    // Nesting is also read in the GUID namespace (a drive letter and a folder mount point of one volume).
    ASSERT_EQ(static_cast<int>(DirRelation::FirstInsideSecond),
              static_cast<int>(compareIdentities(guidIdentity(1, 12, QStringLiteral("E:\\in\\sub"), volumeA + QStringLiteral("\\in\\sub")),
                                                 guidIdentity(1, 11, QStringLiteral("C:\\mnt\\a\\in"), volumeA + QStringLiteral("\\in")),
                                                 windows).relation));
}

// r5: the wide FileIdInfo pair decides id equality when both sides have it (ReFS: the 64-bit index is not unique),
// and the GUID root parser accepts only a real \\?\Volume{...} prefix.
TEST(ContactSheetSeekDirIdentity, TheWideFileIdDecidesWhenBothSidesHaveItAndOnlyAGuidPrefixNamesAVolume)
{
    using namespace output_dir_identity::detail;
    const PathRules windows = windowsPathRules();
    DirIdentity a = provenIdentity(5, 77, QStringLiteral("\\\\srv\\share\\a"), windows);
    DirIdentity b = provenIdentity(5, 77, QStringLiteral("\\\\srv\\share\\b"), windows);
    ASSERT_TRUE(sameFileId(a, b));
    a.hasWideId = b.hasWideId = true;
    a.wideVolume = b.wideVolume = 5;
    a.wideIdLow = b.wideIdLow = 77;
    a.wideIdHigh = 1;
    b.wideIdHigh = 2;
    ASSERT_FALSE(sameFileId(a, b));
    ASSERT_EQ(static_cast<int>(output_dir_identity::DirRelation::Distinct),
              static_cast<int>(compareIdentities(a, b, windows).relation));
    a.pathsComparable = false;   // a real share: not Same by id, and not provably Distinct by path
    ASSERT_EQ(static_cast<int>(output_dir_identity::DirRelation::Unknown),
              static_cast<int>(compareIdentities(a, b, windows).relation));
    b.hasWideId = false;   // one side without FileIdInfo: the 64-bit pair is compared
    ASSERT_TRUE(sameFileId(a, b));

    ASSERT_EQ(std::string("\\\\?\\Volume{abc}"), volumeGuidRootOf(QStringLiteral("\\\\?\\Volume{abc}\\x\\y")).toStdString());
    ASSERT_EQ(std::string("\\\\?\\volume{abc}"), volumeGuidRootOf(QStringLiteral("\\\\?\\volume{abc}")).toStdString());
    ASSERT_TRUE(volumeGuidRootOf(QStringLiteral("\\\\?\\C:\\x")).isEmpty());
    ASSERT_TRUE(volumeGuidRootOf(QStringLiteral("\\Device\\HarddiskVolume3\\x")).isEmpty());
    ASSERT_TRUE(volumeGuidRootOf(QStringLiteral("\\\\?\\Volume{abc")).isEmpty());
    ASSERT_TRUE(volumeGuidRootOf(QStringLiteral("\\\\?\\Volume{abc}x")).isEmpty());
}

#ifdef Q_OS_WIN
// r5: on this host the real identityOf reads the GUID namespace and FileIdInfo, and the junction alias still
// resolves to one GUID path (the r3 RED stays GREEN through the new rule).
TEST(ContactSheetSeekDirIdentity, OnALocalVolumeTheIdentityCarriesTheGuidPathAndAJunctionSharesIt)
{
    QTemporaryDir root;
    ASSERT_TRUE(root.isValid());
    const QString in = root.filePath(QStringLiteral("in"));
    const QString alias = root.filePath(QStringLiteral("alias"));
    ASSERT_TRUE(QDir().mkpath(in));
    ASSERT_TRUE(makeDirectoryAlias(alias, in));
    using namespace output_dir_identity::detail;
    const DirIdentity inId = identityOf(in);
    const DirIdentity aliasId = identityOf(alias);
    ASSERT_TRUE(inId.ok && aliasId.ok);
    ASSERT_TRUE(inId.pathsComparable && aliasId.pathsComparable);
    ASSERT_TRUE(inId.volumeName.startsWith(QStringLiteral("\\\\?\\Volume{"), Qt::CaseInsensitive));
    ASSERT_TRUE(inId.hasWideId);
    ASSERT_EQ(inId.identityPath.toStdString(), aliasId.identityPath.toStdString());
    ASSERT_TRUE(inId.identityPath.endsWith(QStringLiteral("\\in"), Qt::CaseInsensitive));
}
#endif

// r5 (opus key3 r4): a refused comparison removes every directory it created; an accepted one keeps both.
TEST(ContactSheetSeekDirIdentity, ARefusedComparisonLeavesNoNewDirectoryBehind)
{
    QTemporaryDir root;
    ASSERT_TRUE(root.isValid());
    using output_dir_identity::DirRelation;
    using output_dir_identity::prepareSeparateDirectories;
    const auto entries = [&root]() {
        return QDir(root.path()).entryList(QDir::AllEntries | QDir::NoDotAndDotDot | QDir::Hidden).join(QLatin1Char(','));
    };

    // Both new, one nested in the other: refused, and neither "fresh" nor anything under it is left.
    const output_dir_identity::DirComparison nested = prepareSeparateDirectories(root.filePath(QStringLiteral("fresh/seek/inner")),
                                                            root.filePath(QStringLiteral("fresh/seek")));
    ASSERT_EQ(static_cast<int>(DirRelation::FirstInsideSecond), static_cast<int>(nested.relation));
    ASSERT_EQ(std::string(), entries().toStdString());

    // An existing in-pass dir and a new seek dir under it: the new levels are removed, the existing dir is kept.
    const QString in = root.filePath(QStringLiteral("in"));
    ASSERT_TRUE(QDir().mkpath(in));
    const output_dir_identity::DirComparison under = prepareSeparateDirectories(root.filePath(QStringLiteral("in/new/deeper")), in);
    ASSERT_EQ(static_cast<int>(DirRelation::FirstInsideSecond), static_cast<int>(under.relation));
    ASSERT_EQ(std::string("in"), entries().toStdString());
    ASSERT_TRUE(QDir(in).entryList(QDir::AllEntries | QDir::NoDotAndDotDot | QDir::Hidden).isEmpty());

    // A junction alias of the existing dir: Same, and nothing new is left.
    const QString alias = root.filePath(QStringLiteral("alias"));
    ASSERT_TRUE(makeDirectoryAlias(alias, in));
    const output_dir_identity::DirComparison same = prepareSeparateDirectories(alias, in);
    ASSERT_EQ(static_cast<int>(DirRelation::Same), static_cast<int>(same.relation));
    ASSERT_EQ(std::string("alias,in"), entries().toStdString());
    ASSERT_TRUE(QDir(in).entryList(QDir::AllEntries | QDir::NoDotAndDotDot | QDir::Hidden).isEmpty());

    // Creation fails on the second path (its parent is a file): Unknown, and the first path's new dirs are removed.
    QFile blocker(root.filePath(QStringLiteral("blocker")));
    ASSERT_TRUE(blocker.open(QIODevice::WriteOnly));
    blocker.close();
    const output_dir_identity::DirComparison failed = prepareSeparateDirectories(root.filePath(QStringLiteral("made/seek")),
                                                            root.filePath(QStringLiteral("blocker/in")));
    ASSERT_EQ(static_cast<int>(DirRelation::Unknown), static_cast<int>(failed.relation));
    ASSERT_TRUE(failed.detail.contains(QStringLiteral("reason=create_failed")));
    ASSERT_EQ(std::string("alias,blocker,in"), entries().toStdString());

    // r5 pass 6 (A1): distinct new siblings are accepted, and they are removed too. The capture creates its own
    // directories when it starts, so a later refusal before it (SEEK_FRAME_OUT_OF_RANGE, exit 14) leaves none.
    const output_dir_identity::DirComparison distinct = prepareSeparateDirectories(root.filePath(QStringLiteral("out/seek")),
                                                              root.filePath(QStringLiteral("out/in")));
    ASSERT_EQ(static_cast<int>(DirRelation::Distinct), static_cast<int>(distinct.relation));
    ASSERT_EQ(std::string("alias,blocker,in"), entries().toStdString());
    ASSERT_FALSE(distinct.detail.contains(QStringLiteral("cleanup_left=")));

    // The capture branches create a missing directory themselves, so removing it here costs nothing.
    const QString mainWindow = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    ASSERT_TRUE(mainWindow.contains(QStringLiteral("if( !inPassDirInfo.exists() && !QDir().mkpath( options.contactSheetDir ) )")));
    ASSERT_TRUE(mainWindow.contains(QStringLiteral("if( !contactSheetDirInfo.exists() && !QDir().mkpath( seekSheetDir ) )")));

    // r5 pass 7 (A1): because the check removed what it made, the seek branch proves the pair again after it creates
    // the seek dir and before the first seek write; anything but Distinct ends the run by name.
    const int seekMkpathAt = mainWindow.indexOf(QStringLiteral("if( !contactSheetDirInfo.exists() && !QDir().mkpath( seekSheetDir ) )"));
    const int seekLoopAt = mainWindow.indexOf(QStringLiteral("for( int i = 0; i < options.contactSheetFrames; ++i )"), seekMkpathAt);
    ASSERT_TRUE(seekMkpathAt > 0 && seekLoopAt > seekMkpathAt);
    const QString recheck = mainWindow.mid(seekMkpathAt, seekLoopAt - seekMkpathAt);
    ASSERT_TRUE(recheck.contains(QStringLiteral("if( !options.contactSheetSeekMode )")));
    ASSERT_TRUE(recheck.contains(QStringLiteral("output_dir_identity::compareDirectories( seekSheetDir, options.contactSheetDir )")));
    ASSERT_TRUE(recheck.contains(QStringLiteral("!= output_dir_identity::DirRelation::Distinct")));
    ASSERT_TRUE(recheck.contains(QStringLiteral("SEEK_DIR_NOT_SEPARATE_AT_CAPTURE")));
    ASSERT_TRUE(mainWindow.contains(QStringLiteral("#include \"OutputDirIdentity.h\"")));

    // r5 pass 7 (A2): an accepted run names a directory the check could not remove.
    const QString mainSource = readRepoFile(QStringLiteral("platform/qt/main.cpp"));
    ASSERT_TRUE(mainSource.contains(QStringLiteral("if (seekDirIdentity.detail.contains(QStringLiteral(\"cleanup_left=\")))")));
    ASSERT_TRUE(mainSource.contains(QStringLiteral("SEEK_DIR_CHECK_CLEANUP_LEFT")));
}

#ifdef Q_OS_WIN
// r5 pass 6 (A2): a directory another process holds open without delete sharing cannot be removed; it is named.
TEST(ContactSheetSeekDirIdentity, ADirectoryThatCannotBeRemovedIsReported)
{
    QTemporaryDir root;
    ASSERT_TRUE(root.isValid());
    using namespace output_dir_identity::detail;
    const QString held = QDir::cleanPath(root.filePath(QStringLiteral("held")));
    const QString released = QDir::cleanPath(root.filePath(QStringLiteral("free")));
    ASSERT_TRUE(QDir().mkdir(held));
    ASSERT_TRUE(QDir().mkdir(released));
    const HANDLE handle = CreateFileW(QDir::toNativeSeparators(held).toStdWString().c_str(), FILE_READ_ATTRIBUTES,
                                      FILE_SHARE_READ | FILE_SHARE_WRITE, nullptr, OPEN_EXISTING,
                                      FILE_FLAG_BACKUP_SEMANTICS, nullptr);
    ASSERT_TRUE(handle != INVALID_HANDLE_VALUE);
    const QStringList left = removeCreatedDirectories({ held, released });
    CloseHandle(handle);
    ASSERT_EQ(1, static_cast<int>(left.size()));
    ASSERT_EQ(held.toStdString(), left.first().toStdString());
    ASSERT_FALSE(QFileInfo::exists(released));
    // prepareSeparateDirectories copies that list into the refusal detail.
    const QString header = readRepoFile(QStringLiteral("platform/qt/OutputDirIdentity.h"));
    ASSERT_TRUE(header.contains(QStringLiteral("const QStringList left = detail::removeCreatedDirectories(created);")));
    ASSERT_TRUE(header.contains(QStringLiteral("result.detail += QStringLiteral(\" cleanup_left=%1\")")));
}
#endif

TEST(ContactSheetSeekFrameListWiring, TheLogLineNamesTheListOnlyWhenItIsSetAndTheDoneLineIsUnchanged)
{
    const QString source = readRepoFile(QStringLiteral("platform/qt/MainWindow.cpp"));
    const QString smokeBody = smokeBodyOf(source);
    const QString logBlock = sliceBetween(smokeBody,
        QStringLiteral("QStringLiteral(\"gui_smoke.contact_sheet\"),"),
        QStringLiteral("if( contactSheetFramesWritten != options.contactSheetFrames"));
    ASSERT_FALSE(logBlock.isEmpty());
    ASSERT_TRUE(logBlock.contains(QStringLiteral(
        "+ ( options.contactSheetSeekFrames.isEmpty() ? QString() : QStringLiteral(\" seek_targets=list\") )")));
    // The DONE line keeps its exact shape: the only contact-sheet field on it is still the written count.
    const QString doneBlock = sliceBetween(smokeBody,
        QStringLiteral("out << \"[GUI-SMOKE] DONE clip=\""),
        QStringLiteral("return 0;"));
    ASSERT_FALSE(doneBlock.isEmpty());
    ASSERT_FALSE(doneBlock.contains(QStringLiteral("seek")));
    ASSERT_TRUE(doneBlock.contains(QStringLiteral("out << \" contact_sheet_frames_written=\" << contactSheetFramesWritten;")));
}
